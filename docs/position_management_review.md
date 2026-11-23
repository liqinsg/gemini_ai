# Scheduled Runner v3 仓位管理策略 · 整改报告

> **审核对象**: `scheduled_runner_v3.py`
> **审核日期**: 2026-11-23
> **审核性质**: 仓位管理风控审查 + 整改计划
> **代码版本**: v3.2.0

---

## 一、执行分层架构（run_cycle 调用顺序）

理解仓位管理必须先理解执行顺序。以下是 `run_cycle()` 中所有影响仓位的步骤，按实际执行先后排列：

```
run_cycle()
│
│── 账户 guard 检查 (EXPECT_ACCOUNT_ID)         L6022-6034   一次性启动检查，account mismatch → sys.exit
│── 紧急锁检查 (.emergency_close_lock_v3)        L6054-6058
│── profile 锁 / per-host 隔离（非跨 host 锁）   L6036-6052
│
├─ ① 老仓维护（逐 group 循环）
│     _maintain_group_positions()                L5255+
│     ├─ 所有权过滤（is_bot_owned_trade）         ✅ 严格
│     ├─ Risk Engine (H1/SL 追踪)
│     └─ 5-Gate Early Exit                        OVERRIDE 豁免（7d MIN_HOLD + runner-close 禁用）
│
├─ ② 主动出局
│     _weak_exit_scan_v31()                      L5022+
│     ├─ 全量快照（不先过滤 bot-owned）            ⚠️ 见问题 B
│     ├─ 组内支撑分排名 → 垫底 N 名
│     └─ 平仓时 _gated_close_trade_by_id 拦截     ✅ 不会误平 manual
│
├─ ③ 分批止盈
│     _scale_out_scan_v31()                      L4913+
│     ├─ bot-owned 过滤                          ✅ L4937
│     └─ +1R 平 50% / +2R 平 50%（R 锚定初始 entry/SL）
│
├─ ④ MC 阶梯补仓
│     _mc_min_position_topup_scan_v31()          L5128+
│     ├─ bot-owned 过滤 + pair 聚合               ✅ L5149
│     ├─ 仅 STRONG_MOMENTUM + MC 方向一致
│     ├─ 逐轮 +1 级（1→2→4→8），不一次倒满
│     └─ ⚠️ 直接 execute_market_trade，绕过 cap 与常规 entry guard  ← 问题 A
│
│   至此补仓已提交。以下步骤重建敞口视图。
│
├─ ⑤ 全局 MC regime 计算
│     max_positions 分档 (CONSOLIDATION / NEUTRAL / STRONG_MOMENTUM)    L6214-6231
│     cross_net_cap 动态 boost (base / STRONG_MOMENTUM / STRONG_GAP)    L6233-6306
│
├─ ⑥ 重建敞口视图
│     _open_net, _held = _build_open_exposure()   L6488
│     ↓ fail-closed（读取失败 → HOLD 本 cycle）   L6489-6511
│
├─ ⑦ 全局选篮 + 信号生成
│     _pick_global_basket(open_net=_open_net)     L6513
│     _execute_single_signal()                    L4413+
│     ├─ 同 pair 幂等（held / duplicate / reverse）
│     ├─ Single-host guard（broker witness）
│     ├─ OVERRIDE PER_CYCLE + MAX_OPEN 双 cap
│     ├─ MAX_OPEN_POSITIONS cap（OVERRIDE 可绕过）
│     ├─ CROSS_MAX_NET_PER_CCY cap
│     └─ 仓位 shrink-only 链：
│          dlot scale → MC regime tier → prob-zone → clamp
│
└─ ⑧ 信号执行
      _execute_single_signal() 内部               L4413+
      ├─ Single-host guard → fail-closed
      ├─ OVERRIDE 二次 cap 检查（防御性）
      ├─ MAX_POSITIONS 二次检查
      └─ 强制 SL/TP 挂单
```

### 关键口径说明

**1. 跨币种净敞口（cross-net-cap）的计数单位**：`_build_open_exposure()`（L4138-4167）按 **bot-owned 持仓的笔数方向** 累计，每笔记 `s = 1 if units > 0 else -1`，计入 base 和 quote ccy。**不按手数计量**。

举例：EUR_USD BUY +1 → 净敞口计作 `EUR:+1, USD:-1`。再开 GBP_USD BUY +1 → `USD:-2`。若 `CROSS_MAX_NET_PER_CCY=2`，USD 已达 cap，不能再开任何 USD quote 的 BUY 或 USD base 的 SELL。

**2. 总持仓数（MAX_OPEN_POSITIONS）的计数口径**：`_execute_single_signal()` 内部（L4550-4557）遍历 broker 全量快照，按 `is_bot_owned_trade + tag_prefix 匹配` 统计持仓笔数 `_strategy_open`，然后 `_strategy_open >= _MAX_OPEN_POSITIONS` 触发 cap。**每新增一笔 broker trade 就 +1**（与 cross-net-cap 的"每笔记 +1/-1"是同一个计数事件）。

**限制范围**：以上两个 cap 都按 bot-owned 的 broker trade 笔数计数，不按 `currentUnits` 计量，也不包括 manual / other-bot trades。因此它们是策略内 cap，不是共享账户的全量风险上限；若账户允许外部持仓，真实账户敞口会被低估。

---

## 二、整改优先级清单

| 编号  | 优先级 | 问题                                                       | 涉及位置          | 状态       |
| ----- | ------ | ---------------------------------------------------------- | ----------------- | ---------- |
| **A** | 🔴 P0  | MC-TOPUP 补仓绕过两项 cap；enforce 会改变 ladder 深度且不限制单笔 units | L5128-5249 | 待 shadow / Owner 决策 |
| **B** | 🟡 P1  | weak-exit 扫描混入 manual 持仓                             | L5042-5082        | 待确认意图 |
| **C** | 🟡 P1  | OVERRIDE 交易在 weak-exit / scale-out 中无豁免             | L5026, L4913      | 待确认意图 |
| **D** | 🟢 P2  | shrink-only 并非严格：dlot floor 回退 + clamp floor 可放大 | L4752-4757, L4832 | 实测 effective units 后定级 |
| **E** | 🟢 P2  | 普通 boost 分支无硬上限（仅 STRONG_GAP 有 `min(..., 8)`）  | L6260 vs L6295    | 观察       |

---

## 三、问题详情与修复方案

### 🔴 问题 A：MC-TOPUP 补仓绕过 cross-net-cap 和 MAX_OPEN_POSITIONS（P0）

#### 问题描述

`_mc_min_position_topup_scan_v31()`（L5128）是 run_cycle 中**唯一直接调用 `_trading_core.execute_market_trade()` 且未经过 `_execute_single_signal()` 风险门**的下单路径。它只检查：

1. `_MC_MIN_POS_ENABLED` 开关
2. MC regime == STRONG_MOMENTUM
3. MC 方向概率与持仓方向一致
4. SL/TP 存在（fail-safe）

然后在 L5234 直接下单。**绕过了两层 cap**，也未调用 `_execute_single_signal()` 中配置可选的 `SINGLE_HOST_GUARD_ENABLED` broker witness：

| cap                   | 正常信号路径检查                  | TOPUP 是否检查 | 计数口径                           |
| --------------------- | --------------------------------- | -------------- | ---------------------------------- |
| `_cross_net_cap`      | ✅ `_pick_global_basket` L6513    | ❌ 未检查      | 笔数方向 `±1`，按 base + quote ccy |
| `_MAX_OPEN_POSITIONS` | ✅ `_execute_single_signal` L4582 | ❌ 未检查      | bot-owned + tag 匹配的持仓笔数     |
| `SINGLE_HOST_GUARD_ENABLED` | ✅ `_execute_single_signal` L4456 | ❌ 未检查 | 可选、best-effort 的近期 bot trade broker witness |

**并发边界**：该 guard 是可选的近期交易 witness，不是跨主机原子锁；仅在 `_execute_single_signal()` 执行。即使为 TOPUP 增加相同检查，两个 host 仍可能同时通过检查后下单。因此基于快照的运行时 cap 能处理单轮顺序累积，但**不能单独保证多 host 并发下的硬 cap**。若要求 account-wide 硬上限，需另行批准共享串行化机制；否则计划和验收必须明确该剩余风险。

#### 时序问题

```
L6209:  _mc_min_position_topup_scan_v31(dry_run)     ← 补仓发生
         └─ 直接 execute_market_trade()              ← 绕过所有 cap

L6214-6306:  _global_mc / _MAX_OPEN_POSITIONS / _cross_net_cap 计算
             ↑ 这些值在 TOPUP 之后才被赋值！
             TOPUP 执行时 _cross_net_cap / _MAX_OPEN_POSITIONS
             保留的是上一个 run_cycle 的值。

L6488:  _open_net, _held = _build_open_exposure()     ← 重建敞口（看到补仓结果）
L6513:  _pick_global_basket(open_net=_open_net)      ← basket 层会 cap 新信号
```

basket 层会看到补仓后的敞口并阻止新信号，但**补仓本身不受 cap 约束**。

#### 风险量级与策略影响

由于 TOPUP 新增一笔 broker trade（在每次 fill 都新开独立 trade 的账户模式下）同时影响两个 cap：

1. **cross-net-cap**：新增一笔同向 trade → base ccy 净方向 `+1×sign`，quote ccy 净方向 `-1×sign`。**不按手数计**，只加一个方向单位。
2. **MAX_OPEN_POSITIONS**：新增一笔 bot-owned trade → `_strategy_open` + 1。若某 group 已经达到 cap=3，TOPUP 可以让它变成 4 或更高。

这意味着把 cap 从"未检查"改成"强制检查"**不只是安全补丁，也会改变 ladder 行为**。例如在 `CROSS_MAX_NET_PER_CCY=2` 且既有 EUR_USD BUY 已贡献 USD=-1 时，第一次新增同向 trade 可到 cap=-2；后续阶梯若再新增独立 trade 会被拦截。反过来，若账户将加仓并入既有 trade，该笔数 cap 仍可能只看到一笔，不能限制 2/4/8 units 的实际规模。部署账户的 fill 模式和阶梯深度必须在 enforcement 前实测确认。

因此 `CROSS_MAX_NET_PER_CCY` / `MAX_OPEN_POSITIONS` 只能限制**笔数**，不能替代按 units 或风险金额计算的仓位上限。Shadow 结果应报告各 rung 的候选/阻断次数及 pair units；若 Owner 期望限制实际风险，应另行定义 units/risk cap，而不是把笔数 cap 当作等价物。

#### 并发与 single-host witness

`_single_host_recent_entry_detected()`（L1923）检查所有 bot-owned 持仓的 `openTime`，不区分 host、profile 或本进程创建者；它是可选的 best-effort witness，不是共享原子锁。MC-TOPUP 当前不调用该 gate。若 TOPUP 在本周期创建 trade，随后 `_execute_single_signal()` 会把本进程刚开的 trade 也当作 recent entry，从而可能阻断本周期正常信号。要让 TOPUP 遵循 witness，需同时让当前进程在同周期 entry 检查中排除其已确认的 TOPUP trade ID；其他 host 不应拥有该本地排除集，仍应看到该 trade。并发 host 的同时检查竞态仍需单独的共享串行化机制。
#### 同轮多笔累积问题

MC-TOPUP 扫描多个 pair。每个 pair 独立读一次 `_build_open_exposure()` 看到的是**原始基准敞口**。如果 EUR_USD BUY 和 GBP_USD BUY 都触发 STRONG_MOMENTUM：

- EUR_USD 模拟 → USD 净敞口 2 → OK（cap=2）
- GBP_USD 模拟 → USD 净敞口 2 → OK（cap=2）
- **实际补仓后** USD 净敞口 = 3 → 超 cap

**每笔检查必须基于"当前模拟中的运行时敞口"，而不是初始敞口。**

#### 修复方案（完整）

修复应分阶段：**先前置并显式传入 cap 值**（无行为变化），**再统一敞口 helper 并开启 shadow 观测**，经 Owner 审批和观测后才进入 enforce。Enforce 会改变 ladder 深度，不能默认当作纯安全补丁上线。

##### 阶段 1：前置 MC 计算并显式传参（单独提交，不改行为）

将 MC regime 分档、`_MAX_OPEN_POSITIONS`、`_cross_net_cap` 计算移到 TOPUP 调用之前；完成后以显式参数传给 TOPUP，不在函数内读取上一轮可能遗留的 module global：

```python
_mc_min_position_topup_scan_v31(
    dry_run,
    cross_net_cap=_cross_net_cap,
    max_open_positions=_MAX_OPEN_POSITIONS,
    cap_guard_mode=_MC_TOPUP_CAP_GUARD,
)
```

此阶段只改变计算顺序和依赖传递方式，保持 cap guard mode 为 `off`，独立提交并验证正常篮选行为不变。

**前置条件确认**：

| 计算块                                                         | 依赖                                          | 是否已就绪                       |
| -------------------------------------------------------------- | --------------------------------------------- | -------------------------------- |
| `_global_mc = _get_global_mc_regime(_all_trade_pairs)` (L6214) | `_all_trade_pairs` (L6188) + MC cache (L6190) | ✅ L6190 已加载 MC cache         |
| `_MAX_OPEN_POSITIONS = MC_CONSOLIDATION/...` (L6217-6231)      | `_global_mc`                                  | ✅ 前置后立即可用                |
| `_boost_net_cap` / `_cross_net_cap` (L6233-6272)               | `_global_mc` + `_global_scores`               | ✅ L6147 已构建 `_global_scores` |
| STRONG_GAP synthesis (L6287-6306)                              | 同上                                          | ✅                               |

**改动**：

```
原位置:
  L6209:  _mc_min_position_topup_scan_v31(dry_run)
  L6214-6306:  MC 计算（global_mc / max_positions / cross_net_cap）

新位置:
  L6214-6306:  MC 计算（global_mc / max_positions / cross_net_cap）  ← 提前
  L6307+（新位置）: _mc_min_position_topup_scan_v31(dry_run)         ← 移到之后
```

前置后 TOPUP 收到本轮正确的 `_cross_net_cap` 和 `_MAX_OPEN_POSITIONS`，不依赖隐式全局状态。

##### 阶段 2：统一敞口 helper 并加入 shadow / enforce guard

新增唯一的 `_strategy_exposure(trades) -> (net, count, eligible)` helper，统一 bot-owned、`clientExtensions.tag` 的 prefix 过滤、逐笔方向净敞口和 trade 数计算。然后让：

- `_build_open_exposure()` 从 helper 的 `eligible` 构建 `held`，并复用 `net`。
- `_execute_single_signal()` 从 helper 取得逐笔 `_strategy_open` 和 pair 是否已持有。
- MC-TOPUP 从相同 `eligible` 聚合 ladder 候选，从相同 `net/count` 初始化运行时状态。
- 成功下单后的新快照也通过同一 helper 重建；不再在 PATCH A/B/D 分别复制过滤和计数实现。

加入 `MC_TOPUP_CAP_GUARD=off|shadow|enforce`：

| Mode | 行为 |
|---|---|
| `off` | 不阻断订单，保持当前行为；不声称 P0 风险已修复。建议为默认值，避免未经批准改变 ladder。 |
| `shadow` | 对已通过 regime、方向、ladder、SL/TP 等原有资格检查的候选计算 cap；只记录 would-block，不阻断下单。 |
| `enforce` | 相同检查命中时阻断下单；仅在 shadow 结果审阅并获 Owner 单独批准后启用。 |

cap 检查必须放在所有资格检查之后、实际 order call 之前，避免对本来就不会下单的 pair 产生误导性 block 日志。Shadow 阶段至少观测 7 个连续日历日，记录候选/阻断次数、各 ladder rung、pair units 和 cap 值；Owner 审阅后显式决定是否切换到 `enforce`，不得自动切换。

Enforce 模式中：初始化必须复用 TOPUP 已取得的 `open_trades`；使用显式传入的当前 cycle caps；逐候选维护 `_rt_net` / `_rt_open_count`；每次成功下单后用 `_all_open_trades_snapshot()` 和 `_strategy_exposure()` 刷新，使用 `math.isclose(..., rel_tol=0, abs_tol=1e-6)` 确认该 pair 的 eligible net units 按预期变化。刷新失败或结果未确认时停止本轮后续 TOPUP，并通过结构化事件/现有 operator alert 通道告警，不能只 `print` 后继续。

**注意**：早期 PATCH A/B/D 草稿中的逐段过滤伪代码已由共享 helper 方案取代；最终代码只实现一份 `_strategy_exposure()`，不复制三套过滤逻辑。

若 `SINGLE_HOST_GUARD_ENABLED` 打开，TOPUP 开始前应用现有 witness；记录本进程本周期已确认的 TOPUP trade ID，并使后续 `_execute_single_signal()` witness 排除这些本地 ID，避免将本进程刚开的仓误判为另一 host 的 entry。其他 host 没有该本地排除集，仍应看见这些 trade。该 witness 仍非原子锁；account-wide hard cap 需要另行批准共享串行化。

**修复要点对照**：

| 风险/缺陷 | 处理 |
|---|---|
| 漏了 `MAX_OPEN_POSITIONS` | Enforce mode 按 `_strategy_exposure()` 的逐笔 `count` 检查，并将 cap 显式传入 TOPUP。 |
| Fail-open 与同轮多笔累积 | Shadow 只记录不阻断；Enforce 遇到读取/解析/复核失败即停止后续补仓。每次确认成交后用新 broker 快照重建 `net/count`。 |
| `held` set 低估 trade 数 | `held` 只供 pair 幂等；cap 计数使用 helper 的逐笔 `count`。 |
| 重复过滤逻辑易漂移 | `_build_open_exposure()`、`_execute_single_signal()`、TOPUP 和 post-fill reconciliation 共用 `_strategy_exposure()`；增加 parity 测试。 |
| cap 值时序/隐式全局 | Stage 1 前置计算并以显式参数传入，单独提交且 guard 默认 `off`。 |
| fill 是否已反映无法确认 | 比较 broker fresh snapshot 的 pair units 与预期值（容差 `1e-6`）；不匹配即发结构化告警并停止剩余 scan。 |
| 多 host 并发 | 现有 witness best-effort；硬 cap 需另行共享串行化，不能由本地运行时模拟保证。 |

**共享 helper 的输出契约**：

| 返回值 | 定义 |
|---|---|
| `net` | 对每笔 eligible trade 累计方向单位：`s = ±1`, `net[base] += s`, `net[quote] -= s`；与 `_build_open_exposure()` 口径一致。 |
| `count` | 每笔 eligible broker trade `+= 1`；与 `_execute_single_signal()` 的 `_strategy_open` 口径一致，不从 `held` 推导。 |
| `eligible` | 原始 trade 列表，限定为 bot-owned 且 `clientExtensions.tag` 匹配任一 `_strategy_groups` prefix。TOPUP 可在此集合上做 pair 聚合，entry 执行可检查 instrument 是否已持有。 |

为什么两者口径不同？因为设计意图不同：`held` set 服务于"同 instrument 是否已有 bot 持仓"的幂等判断（同方向 1 笔和 5 笔对幂等来说都是"已持有"），而 `_strategy_open` 服务于"总持仓是否超限"的 cap 判断（5 笔比 1 笔风险大）。MC-TOPUP 在同一 instrument 上逐轮加仓，所以必须用逐笔口径。

**统一过滤与计数要求**：唯一的 `_strategy_exposure(trades)` 按 `is_bot_owned_trade` + `clientExtensions.tag` prefix 过滤，并返回原始 eligible trades、逐笔方向 `net` 和逐笔 `count`。不得从 `held` 去重集合推导 `count`，也不得在 TOPUP 单独增加顶层 `tag` fallback。

**快照一致性要求**：初始化时 TOPUP 聚合和 cap 检查必须复用同一份 `open_trades`。成交后通过 `_all_open_trades_snapshot()` 获取新快照，并用同一 helper 重建；对 pair units 的预期变化使用容差比较。若刷新失败或成交未反映，除停止剩余 scan 外，还需发出结构化告警。

---

### 🟡 问题 B：weak-exit 扫描混入 manual 持仓（P1，待确认设计意图）

#### 问题描述

`_weak_exit_scan_v31()`（L5022-5125）的流程：

```
L5042  open_trades = _all_open_trades_snapshot()          ← 全量 broker 快照
L5049-5078  遍历所有 trade，计算支撑分，构建 positions    ← 无 is_bot_owned_trade 过滤
L5082  plan = _weak_exit.compute_weak_exit_plan(positions, cfg=we_cfg)  ← manual 参与排名
L5098  ok, info = _gated_close_trade_by_id(tid, ...)      ← 平仓时才 gate（manual 会被拒）
```

对比 `_maintain_group_positions()`（L5283-5294）：**先做 bot-owned 过滤，再做所有逻辑**。weak-exit 的 close gate 确实不会误平 manual，但 manual 持仓会：

1. 进入组内排名分母，稀释 bot 持仓的垫底概率
2. 导致组规模（MIN_GROUP_SIZE）误判
3. 污染 shadow 日志

#### 原注释（L5026）

> "不做 tag/comment 所有权过滤，也不跳过 OVERRIDE；bot 账号不手工开仓 → 条件够一律关安全。"

这个注释的前提 "bot 账号不手工开仓" 已经不再绝对成立（账户可能被人工/其他进程占用）。

#### 处理选项

| 选项                            | 改动                                                      | 适用场景                              |
| ------------------------------- | --------------------------------------------------------- | ------------------------------------- |
| B1：扫描阶段增加 bot-owned 过滤 | L5049 循环内增加 `if not is_bot_owned_trade(t): continue` | 意图是"只评估 bot 仓位的排名"         |
| B2：保持现状，增强日志提示      | 在 L5088 audit line 中标记 manual 持仓数量                | 意图是"manual 影响排名是可接受副作用" |

**建议**：选 B1，与 `_maintain_group_positions` 和 `_scale_out_scan_v31` 保持一致的所有权过滤模式。close gate 留作第二道防线。

---

### 🟡 问题 C：OVERRIDE 交易在 weak-exit / scale-out 中无豁免（P1，待确认设计意图）

#### 问题描述

OVERRIDE 交易的 early-exit 豁免（L5255+ `_maintain_group_positions` 中的 Gate-0 MIN_HOLD_RISK_PROCESS_MIN 7d + runner-close 禁用）**只覆盖 `_maintain_group_positions` 路径**。两个退出路径不豁免：

| 路径                                          | OVERRIDE 豁免？                    | 代码位置    |
| --------------------------------------------- | ---------------------------------- | ----------- |
| `_maintain_group_positions` early-exit 5-Gate | ✅ 7d MIN_HOLD + runner-close 禁用 | ~L5380-6000 |
| `_weak_exit_scan_v31`                         | ❌ L5026 注释明确"不跳过 OVERRIDE" | L5026       |
| `_scale_out_scan_v31`                         | ❌ 无 OVERRIDE tag 检查            | L4913-5019  |

#### 原注释（L5026）

> "不做 tag/comment 所有权过滤，也不跳过 OVERRIDE"

这个注释描述的是**有意的设计决策**，不是遗漏。但它与 OVERRIDE 在 `_maintain_group_positions` 中享有特殊保护形成了不对称。

#### 处理选项

| 选项                                             | 改动                                                 | 适用场景                                                  |
| ------------------------------------------------ | ---------------------------------------------------- | --------------------------------------------------------- |
| C1：weak-exit / scale-out 增加 OVERRIDE tag 检查 | 扫描循环内增加 tag 匹配过滤                          | 意图：OVERRIDE 生命周期完全由 broker SL/TP 管控           |
| C2：保持现状，更新 OVERRIDE 生命周期文档         | 在 config 注释或 docs 中明确 OVERRIDE 的完整退出路径 | 意图：只有 early-exit 豁免，其他退出路径正常处理 OVERRIDE |

**建议**：选 C1 或 C2 都可以，但**必须二选一并明确**。当前代码只在一条路径上有豁免，容易造成"OVERRIDE 只靠 broker SL/TP"的误解。

---

### 🟢 问题 D：shrink-only 并非严格（P2，观察）

#### 问题描述

`_execute_single_signal` 的仓位链不是严格的"每层只缩不放大"：

| 层             | 代码       | 行为                                                                                    |
| -------------- | ---------- | --------------------------------------------------------------------------------------- |
| dlot scale     | L4750-4761 | 基础 `_order_units = _EFFECTIVE_LOTS`，若 `< 1 unit` 则**回退满仓** `_dlot_scale = 1.0` |
| MC regime tier | L4783      | `if tier < _order_units: _order_units = tier` → 只缩 ✅                                 |
| prob-zone      | L4822      | `_order_units = int(pz_plan["size_units"])` → 取决于 pz_plan 实现，注释说"only shrinks" |
| clamp floor    | L4832      | `max(_lot_floor, ...)` → **floor 会提升**                                               |

两个"可放大"的点：

1. **dlot floor 回退**（L4756）：`LOT_SIZE=1 × dlot_scale=0.5 → 0.5 < 1 → 回退满仓 LOT_SIZE`。本意是避免 broker 拒单（OANDA 要求 units ≥ 1 整数），结果是在极端追势信号里反而**跳过减仓**。
2. **clamp floor**（L4832）：`_lot_floor = _MC_MIN_LOT if MC enabled else 1`。如果前面几层缩到 0.5 手（整数截断后变成 0），floor 会把它拉到 `_lot_floor`（通常也是 1）。实际效果接近 fail-safe。

#### 风险评估

影响取决于实际运行配置，不能仅凭默认/文档值定为低风险：如果 production `_EFFECTIVE_LOTS` 较小，dlot floor 可能让该层缩仓无效；整数截断与 clamp floor 也可能抵消 prob-zone shrink。实施任何结论前，先只读核对 production 的 effective units、dlot scale、prob-zone units、最终 units，并用 shadow/dry-run 输出量化频率和差值。此步骤不改配置、不下单。

#### 结论

**观察，暂不改动，但风险等级待实测**。只有量化确认实际影响低且 Owner 接受后，才能维持低优先级；若缩仓经常被 floor 抵消，需独立讨论 fail-closed skip 与 broker 最小单位的策略取舍。

---

### 🟢 问题 E：普通 boost 分支无硬上限（P2，观察）

#### 问题描述

`_cross_net_cap` 的 clamp 保护只在 STRONG_GAP synthesis 分支有：

```python
# 普通 boost (STRONG_MOMENTUM) — L6260
_cross_net_cap = max(_CROSS_NET_CAP_BASE, 3)          # ❌ 无 min(..., 8)

# STRONG_GAP synthesis — L6293-6295
_cross_net_cap = max(_CROSS_NET_CAP_BASE, _cross_net_cap + 1)
_cross_net_cap = min(_cross_net_cap, 8)                # ✅ 有硬上限
```

#### 风险评估

极低风险。注释 L6248-6253 已明确警告 `BASE>2` 会让 boost 无意义：

> "All boost branches collapse to the same base; differentiation lost. Recommended BASE=2."

而且 `max(base, 3)` 在 BASE=2 时只会到 3，不会更高。只有 BASE≥4 时这个问题才会显现——此时 BASE≥4 本身就违背注释警告，属于配置问题。

#### 可选修复

如果想防御性加 clamp，在 L6260 后面加一行：

```python
_cross_net_cap = (
    max(_CROSS_NET_CAP_BASE, 3) if _boost_net_cap else _CROSS_NET_CAP_BASE
)
_cross_net_cap = min(_cross_net_cap, 8)   # ← 与 STRONG_GAP 分支对齐
```

普通 boost 的 startup warning（`_CROSS_NET_CAP_BASE > 2`）当前已存在；可保留并增加日志测试，不需要随本次开发计划改变 cap 数值语义。

---

## 四、安全性审查矩阵（修正版）

| 风险维度                     | 是否覆盖  | 位置                                 | 备注                                                                     |
| ---------------------------- | --------- | ------------------------------------ | ------------------------------------------------------------------------ |
| 跨币种净敞口 cap             | 🟡 部分   | basket / MC-TOPUP                    | 正常信号路径 ✅；MC-TOPUP ❌（问题 A）；只按 broker trade 笔数计           |
| 总持仓数 cap                 | 🟡 部分   | `_execute_single_signal` / TOPUP     | 正常信号路径 ✅；MC-TOPUP ❌（问题 A）；OVERRIDE 可绕过常规 entry cap      |
| 共享账户总敞口               | ❌ 未覆盖 | strategy-owned filters               | manual / other-bot trades 不计入，非 account-wide 风险上限                 |
| OVERRIDE 集中度 cap          | ✅        | basket + execute 双检查              | PER_CYCLE + MAX_OPEN                                                     |
| 同 instrument 幂等           | ✅        | basket + execute                     | MC-TOPUP 绕过（设计确认）                                                |
| MC 方向冲突                  | ✅        | `_check_mc_direction_conflict`       | SEVERE / MODERATE 两档                                                   |
| Base ccy thesis 冲突         | ✅        | `_pick_global_basket` L4352          | quote ccy 结构性冲突不拦                                                 |
| 仓位 shrink-only 链          | 🟡 有条件 | execute 层                           | dlot floor 回退 + clamp floor 可放大（问题 D）                           |
| 所有权隔离（敞口视图）       | ✅        | `_build_open_exposure`               | bot-owned only                                                           |
| 所有权隔离（weak-exit 扫描） | ❌        | `_weak_exit_scan_v31` L5049          | 混入 manual（问题 B）                                                    |
| OVERRIDE 退出保护            | 🟡 有条件 | early-exit only                      | weak-exit / scale-out 不豁免（问题 C）                                   |
| Emergency stop               | ✅        | `_check_emergency_lock`              | 文件锁触发                                                               |
| 多 host 并发                  | 🟡        | `_single_host_recent_entry_detected` | 可选的 best-effort broker witness；不分 host/profile、不是原子锁；TOPUP 绕过且本周期 TOPUP trade 会误触发后续 entry 检查 |
| 账户 guard                   | ✅        | L6022-6034                           | run_cycle 启动时一次性检查，MCTOPUP 在其后                               |
| SL/TP 强制挂单               | ✅        | `_execute_single_signal` + MC-TOPUP  | 两条下单路径都强制                                                       |
| Early Exit 五门              | ✅        | `_maintain_group_positions`          | OVERRIDE 豁免                                                            |
| 分批止盈                     | ✅        | `_scale_out_scan_v31`                | 锚定初始 R，永不漂移                                                     |
| 主动出局                     | 🟡        | `_weak_exit_scan_v31`                | 混入 manual 排名（问题 B），不豁免 OVERRIDE（问题 C）                    |
| MC-TOPUP 敞口                | ❌        | —                                    | **缺失 cross-net-cap + MAX_POSITIONS 检查 + 运行时模拟；亦绕过可选 single-host witness**（问题 A）；快照方案不提供多 host 原子性 |
| fail-closed 优先             | ✅ 大体   | 全文件                               | dlot 层唯一 fail-open（L4746），MC-TOPUP SL/TP 检查是 fail-safe（L5211） |

---

## 五、修复实施计划

### 阶段 1：前置 MC 计算并显式传参（单独提交，不改行为）

**目标**：让 TOPUP 收到本轮正确的 `_cross_net_cap` 和 `_MAX_OPEN_POSITIONS`，并消除对 module globals 的隐式读取。

**操作**：将 MC regime 分档及动态 cap 计算移到 `_mc_min_position_topup_scan_v31()` 之前，并以显式参数传入。`MC_TOPUP_CAP_GUARD` 保持 `off`，单独提交并验证正常篮选行为不变。

```python
_mc_min_position_topup_scan_v31(
    dry_run,
    cross_net_cap=_cross_net_cap,
    max_open_positions=_MAX_OPEN_POSITIONS,
    cap_guard_mode=_MC_TOPUP_CAP_GUARD,
)
```

**验证前置可行性**：

| 计算块             | 依赖变量                      | 就绪位置                      |
| ------------------ | ----------------------------- | ----------------------------- |
| `_all_trade_pairs` | L6185-6188                    | ✅                            |
| MC cache           | L6190 `_load_mc_cache`        | ✅                            |
| `_global_scores`   | L6147 `build_strength_matrix` | ✅                            |
| `_PROB_INDEX`      | L6198-6206 `_load_prob_index` | ✅（可选，MC 计算本身不依赖） |

**当前与目标执行顺序概览**：

```
改动前:
  L6162:  for gname, gcfg in _enabled_groups: _maintain_group_positions(...)
  L6167:  _weak_exit_scan_v31(dry_run)
  L6173:  _scale_out_scan_v31(dry_run)
  L6185:  _all_trade_pairs = ...
  L6190:  _load_mc_cache(_all_trade_pairs)
  L6209:  _mc_min_position_topup_scan_v31(dry_run)     ← _cross_net_cap 还是上一轮的值
  L6214:  _global_mc = _get_global_mc_regime(...)
  L6217:  _MAX_OPEN_POSITIONS = MC_CONSOLIDATION/...
  L6260:  _cross_net_cap = max(_CROSS_NET_CAP_BASE, 3) if boost else base

改动后:
  L6162-6206:  （保持不变）
  L6214-6306:  MC 计算（提前到这里）
               _global_mc → _MAX_OPEN_POSITIONS → _cross_net_cap
  L6307+:  _mc_min_position_topup_scan_v31(dry_run, cross_net_cap=..., max_open_positions=..., cap_guard_mode=...)
  L6488:  _build_open_exposure()                         ← 不变
  L6513:  _pick_global_basket(...)                       ← 不变
```

### 阶段 2：共享敞口 helper 与 shadow/enforce guard

实现唯一的 `_strategy_exposure(trades) -> (net, count, eligible)`，由 `_build_open_exposure()`、`_execute_single_signal()`、TOPUP 和 post-fill reconciliation 共用。cap 检查只针对通过原有资格检查的候选，并支持 `MC_TOPUP_CAP_GUARD=off|shadow|enforce`。具体测试和验收见 [position_management_development_plan.md](position_management_development_plan.md)。

**行为边界**：

- `off` 保持当前行为，不视为风险修复；`shadow` 仅记录 would-block，至少运行 7 日；`enforce` 需 Owner 单独批准。
- 检查置于 regime、方向、ladder、SL/TP 等资格检查之后、实际 order call 之前。
- Enforce 下每次 fill 后重新取快照，通过共享 helper 重建 exposure，并以容差确认 pair units；不匹配/失败时停止后续补仓并发结构化告警。
- 启用 single-host witness 时，记录本进程本周期的 TOPUP trade IDs，使后续 entry 检查可排除本地刚开仓；仍需明确该 witness 不是原子锁。

### 阶段 3：Shadow 审阅后单独批准 enforce

至少 7 个连续日历日后审阅 shadow 的 rung 阻断率和 units 影响；若 per-trade cap 与预期 ladder depth 不兼容，先决定是否另设 units/risk cap，不得默认放宽或例外绕过现有 cap。

### 阶段 4：P1 / P2

Owner 确认后实施 B1（weak-exit 只对 bot-owned 排名）和 C2（OVERRIDE 只豁免 early-exit）。D 在核实 production effective units 与各层 shrink 影响后再定风险等级；E 保留现有 `BASE>2` startup warning，不改变 cap 数值语义。

---

## 六、附录：run_cycle 精确行号锚定（当前工作区）

以下行号仅对应本次源码核对快照；实施时应使用函数 / symbol 锚点，Stage 1 的代码移动后不得依赖旧行号。

| 步骤                                  | 行号       | 说明                                          |
| ------------------------------------- | ---------- | --------------------------------------------- |
| run_cycle 入口                        | L6017      |                                               |
| 账户 guard                            | L6022-6034 | EXPECT_ACCOUNT_ID                             |
| MC-TOPUP 调用（当前位置）             | L6209      | **建议移到 L6306 之后**                       |
| MC regime 计算                        | L6214-6231 | 建议前置到 TOPUP 之前                         |
| cross_net_cap 动态计算（boost）       | L6233-6272 | 建议前置到 TOPUP 之前                         |
| cross_net_cap（STRONG_GAP synthesis） | L6273-6306 | 建议前置到 TOPUP 之前                         |
| `_build_open_exposure` 调用           | L6488      | ← basket 层重建敞口（TOPUP 已发生）           |
| `_pick_global_basket` 调用            | L6513      |                                               |
| `_execute_single_signal` 调用         | L6543 附近 |                                               |
| MC-TOPUP 函数定义                     | L5128      |                                               |
| TOPUP 内部 per-instrument 循环        | L5176      | 当前循环位置；实际 guard 放在候选资格检查后、order 前 |
| TOPUP 下单执行位置                    | L5234      | `_trading_core.execute_market_trade`          |
| TOPUP 下单成功日志位置                | L5246      | 运行时模拟状态更新位置                        |
| weak-exit 函数定义                    | L5022      |                                               |
| weak-exit 快照位置                    | L5042      | 全量 broker 快照，无 bot-owned 过滤           |
| scale-out 函数定义                    | L4913      |                                               |
| `_build_open_exposure` 定义           | L4138      | bot-owned + tag prefix 过滤，按笔数方向计     |
| cross-net-cap 口径                    | L4162-4164 | `s = ±1`, `net[base] += s`, `net[quote] -= s` |
| MAX_OPEN_POSITIONS 检查位置           | L4582      | `_strategy_open >= _MAX_OPEN_POSITIONS`       |
| MAX_OPEN_POSITIONS 计数口径           | L4550-4557 | bot-owned + tag_prefix 匹配的持仓笔数         |
| `_cross_net_cap` 声明为 global        | L6060      | run_cycle 顶部 `global _cross_net_cap`        |

---

## 七、审核结论与待决事项

### 审核结论

1. **问题 A 仍为 P0，需优先修复**：当前 MC-TOPUP 在 MC regime 与两个 cap 本轮值确定前执行，直接下单，绕过正常信号路径的 cap 和可选 single-host witness。
2. **运行时模拟必须按笔数统计**：`held` 是去重集合，不能用于 `MAX_OPEN_POSITIONS`。初始化时必须复用 TOPUP 已拉取的同一份 `open_trades`，并与 cap 使用相同的 bot-owned / `clientExtensions.tag` 前缀过滤；每笔成功下单后再读取 broker 快照，确认该 instrument 的预期 units 变化已可见，再重建运行时敞口和逐笔计数。不能仅凭返回值真值假设新增一笔 trade。
3. **问题 B 是策略范围决策，不是平仓门禁缺陷**：当前 manual trade 会进入 weak-exit 的排名及组规模，但 `_gated_close_trade_by_id` 在执行时会阻止关闭非 bot-owned trade。建议候选扫描阶段也过滤非 bot-owned，以免 manual trade 改变 bot 仓位排名；该行为改变须由 Owner 确认。
4. **问题 C 建议采用 C2，但须作为风险决策确认**：OVERRIDE 可绕过常规 entry cap，却仍可能被 weak-exit 清退；其 7d MIN_HOLD / runner-close 豁免仅适用于 early-exit。Owner 须明确接受此不对称，不能仅以补充注释代替确认。
5. **问题 D/E 分开处理**：D 需只读量化 production 实际 units 与 shrink 层效果后再定风险等级；E 的 `BASE>2` startup warning 已存在，保留并测试即可，不应与 A 的 enforce 捆绑。

### Owner 确认门槛

- **A（P0）**：先批准 `off|shadow|enforce` 生命周期与至少 7 日 shadow 观测；再审阅 per-trade cap 对 1→2→4→8 ladder 的实际阻断影响。明确 cap 只限制 trade 笔数、不限制单笔 units；若目标是限制 units/risk，需定义独立 cap。`SINGLE_HOST_GUARD_ENABLED` 打开时 TOPUP 须遵从 witness，并避免本进程新 trade 误拦同周期 entry。另确认是否要求多 host 硬 cap；当前 witness/快照均非原子锁。
- **B（P1）**：建议 B1（扫描时仅纳入 bot-owned 持仓）；此举改变 manual 持仓对组排名 / MIN_GROUP_SIZE 的影响，须确认后再改代码。
- **C（P1）**：建议 C2（OVERRIDE 只豁免 early-exit）；Owner 明确确认 OVERRIDE 可能仍被 weak-exit 清退后，更新代码注释和相关测试。

本报告完成的是**源码核对与整改建议定稿**，不代表上述行为变更已获批准或已实施。逐阶段编码与验收见 [position_management_development_plan.md](position_management_development_plan.md)。

## 八、变更记录

| 版本 | 日期       | 审核人                        | 说明                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ---- | ---------- | ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| v1.0 | 2026-11-23 | 双路独立审核（Agent + Codex） | 原始审核报告，5 项问题                                                                                                                                                                                                                                                                                                                                                                                                                         |
| v1.1 | 2026-11-23 | Agent + Codex 交叉核验        | 修正 B（扫描混入 manual）、C（OVERRIDE 退出路径不对称）、D（shrink-only 非严格）、E（STRONG_GAP 有 clamp 而普通 boost 无）；修正 `_build_open_exposure` 行号为 L4138，调用为 L6488                                                                                                                                                                                                                                                             |
| v1.2 | 2026-11-23 | Agent + Codex 交叉核验        | 问题 A 大幅扩展：① 补充 MAX_OPEN_POSITIONS 绕过；② cap guard 统一 fail-closed（异常跳过，绝不放行）；③ 新增同轮多笔运行时模拟；④ 去掉伪代码 `getattr(run_cycle_globals, ...)`；⑤ 完整的前置 MC 计算时序分析（含依赖就绪检查）；新增修复方案的逐行代码和要点对照表                                                                                                                                                                              |
| v1.3 | 2026-11-23 | Agent + Codex 交叉核验        | **修正 `_rt_open_count` 计数口径错误**：v1.2 错误地用 `len(held)` set 推导持仓笔数，但 held 是 set 去重（同 instrument+direction 多笔合并为 1），与 `_strategy_open` 逐笔计数口径不一致。修复方案改为从 broker 快照逐笔构建 `_rt_net` 和 `_rt_open_count`，并附两项口径与现有代码的逐行对照。MC-TOPUP 在同一 instrument 上逐轮加仓是本问题的直接触发场景                                                                                       |
| v1.4 | 2026-11-23 | Agent + Codex 交叉核验        | **统一 TOPUP 与 cap 的持仓范围 + 明确 hedging 模式前提**。① TOPUP 聚合循环（L5149）只做 `is_bot_owned_trade` 过滤，缺少 `_strategy_groups` tag_prefix 过滤，导致 TOPUP 能处理不属于 cap 统计范围的持仓；PATCH A 在 L5149 之后补上与 L4155-4158 相同的 tag_prefix 过滤。② 补仓后 `_rt_open_count += 1` 的前提是 hedging 模式下新开独立 trade（trading_core_v2.py L304 `tradeOpened`），PATCH D 注释里明确 hedging 假设和 netting 模式的改写要求 |
| v1.5 | 2026-11-23 | 源码复核                      | 修正 PATCH A/B 必须使用与 cap 路径完全相同的 `clientExtensions.tag` 过滤并复用 TOPUP 原始快照，避免范围或快照不一致；修正 PATCH D：当前交易封装不保留 `tradeOpened` / `tradeReduced` 类型，成功后须用新 broker 快照确认预期 pair units 变化并重建敞口/逐笔计数，复核失败停止本轮剩余补仓；指出 MC-TOPUP 绕过可选 single-host witness 且快照检查不提供跨主机原子性；新增 B/C 审核建议、Owner 确认门槛及独立开发计划。v1.4 的 hedging 说明为历史建议，不能作为运行时模式已验证的证据 |
| v1.6 | 2026-11-23 | 复核意见整合                  | 将 cap enforce 定性为可能改变 ladder 深度的行为变更，新增 `off|shadow|enforce` 与 7 日 shadow gate；计划提取共享 `_strategy_exposure()`、显式传 cap 参数；记录 single-host witness 不分 host/profile 且会误触发同周期 entry；补充 per-trade cap 不限制 units、共享账户 manual exposure 未计入及 D 实际配置量化要求；修正矩阵与实现阶段锚点 |
