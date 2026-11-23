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

---

## 二、整改优先级清单

| 编号  | 优先级 | 问题                                                       | 涉及位置          | 状态       |
| ----- | ------ | ---------------------------------------------------------- | ----------------- | ---------- |
| **A** | 🔴 P0  | MC-TOPUP 补仓绕过 cross-net-cap **和** MAX_OPEN_POSITIONS  | L5128-5249        | 待修复     |
| **B** | 🟡 P1  | weak-exit 扫描混入 manual 持仓                             | L5042-5082        | 待确认意图 |
| **C** | 🟡 P1  | OVERRIDE 交易在 weak-exit / scale-out 中无豁免             | L5026, L4913      | 待确认意图 |
| **D** | 🟢 P2  | shrink-only 并非严格：dlot floor 回退 + clamp floor 可放大 | L4752-4757, L4832 | 观察       |
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

#### 风险量级

由于 TOPUP 新增一笔 broker trade 同时影响两个 cap：

1. **cross-net-cap**：新增一笔同向 trade → base ccy 净方向 `+1×sign`，quote ccy 净方向 `-1×sign`。**不按手数计**，只加一个方向单位。
2. **MAX_OPEN_POSITIONS**：新增一笔 bot-owned trade → `_strategy_open` + 1。若某 group 已经达到 cap=3，TOPUP 可以让它变成 4 或更高。

#### 同轮多笔累积问题

MC-TOPUP 扫描多个 pair。每个 pair 独立读一次 `_build_open_exposure()` 看到的是**原始基准敞口**。如果 EUR_USD BUY 和 GBP_USD BUY 都触发 STRONG_MOMENTUM：

- EUR_USD 模拟 → USD 净敞口 2 → OK（cap=2）
- GBP_USD 模拟 → USD 净敞口 2 → OK（cap=2）
- **实际补仓后** USD 净敞口 = 3 → 超 cap

**每笔检查必须基于"当前模拟中的运行时敞口"，而不是初始敞口。**

#### 修复方案（完整）

修复分两步：**先前置 MC 计算**（解决时序），**再在 TOPUP 内维护运行时模拟 + fail-closed 异常处理**。

##### 步骤 1：前置 MC 计算（解决 `_cross_net_cap` / `_MAX_OPEN_POSITIONS` 时序）

将 L6214-6306（MC regime 分档 + cross_net_cap 动态计算）移到 L6209 MC-TOPUP 调用**之前**。

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

前置后 TOPUP 可以直接访问本轮正确的 `_cross_net_cap` 和 `_MAX_OPEN_POSITIONS`。

##### 步骤 2：TOPUP 内的完整 cap guard（含运行时模拟 + fail-closed）

在 `_mc_min_position_topup_scan_v31()` 的 L5176 `for instr, rec in agg.items()` 循环之前，初始化运行时模拟状态；循环内每笔补仓先检查、成功后更新模拟状态。

**前置改动**：TOPUP L5149-5150 聚合持仓时，目前只做 `is_bot_owned_trade(t)` 过滤，**缺少 `_strategy_groups` 的 tag_prefix 过滤**。而 cap 路径（`_execute_single_signal` L4553-4557、`_build_open_exposure` L4153-4157）都有 tag_prefix 过滤。如果存在 bot-owned 但不属于任何 `_strategy_groups` 的持仓：

- TOPUP 会对这些持仓做补仓
- 补仓产生的新 trade 不属于 cap 的统计范围，不会进入 `_rt_net` / `_rt_open_count`
- cap guard 看到的持仓数比实际少，可能放行超限补仓

**必须在 TOPUP 聚合循环（L5149）处加上 tag_prefix 过滤**，让 TOPUP 处理的持仓范围与 cap 统计范围完全一致。具体改动是在 L5149 `if not is_bot_owned_trade(t): continue` 之后，紧接着插入与 L4155-4158 完全相同的 tag_prefix 过滤逻辑。

```python
# ============ PATCH A: L5149 之后插入（统一 TOPUP 聚合范围与 cap 统计范围） ============
# TOPUP 当前只按 is_bot_owned_trade 过滤，但 cap 还按 _strategy_groups 的 tag_prefix 过滤。
# 两者必须一致，否则 TOPUP 补仓的 trade 不进入 cap 模拟计数 → cap guard 放行超限。
_rt_prefixes_local = {cfg["tag_prefix"] for cfg in _strategy_groups.values()}
_rt_tag_local = str((t.get("clientExtensions") or {}).get("tag") or "")
_rt_raw_local = _extract_raw_tag(_rt_tag_local)
if not any(_p in _rt_raw_local for _p in _rt_prefixes_local):
    continue

# ============ PATCH B: L5176 之前插入（初始化运行时模拟） ============
# 初始化本轮运行时模拟敞口（用于同轮多笔累积检测）。
# 必须同时从 broker trade 快照逐笔计算两项：
#   _rt_net       → cross_net_cap 的运行时模拟（方向单位累计）
#   _rt_open_count → MAX_OPEN_POSITIONS 的运行时模拟（逐笔计数）
#
# 注意：绝对不能用 _build_open_exposure() 返回的 held 集合推导 _rt_open_count。
# held 是 set()，同一 instrument+direction 的多笔 trade 会被合并成 1 个元素
# （见 L4165: held.add((inst, s))），而 _execute_single_signal 的 _strategy_open
# 是逐笔 +=1 计数（见 L4557）。两者口径不同，用 len(held) 会系统性低估。
#
# 注意：cap guard 和 TOPUP 聚合必须使用同一份 _rt_trades 快照（直接复用本函数
# 已获取的 open_trades），并用完全相同的 is_bot_owned_trade + tag_prefix 过滤，
# 确保 TOPUP 处理的持仓 ⊆ cap 统计的持仓。
try:
    _rt_net = {}
    _rt_open_count = 0
    _rt_trades = open_trades
    _rt_prefixes = {cfg["tag_prefix"] for cfg in _strategy_groups.values()}
    for _t in _rt_trades:
        if not is_bot_owned_trade(_t):
            continue
        _rt_tag = _t.get("clientExtensions", {}).get("tag", "") or ""
        _rt_raw = _extract_raw_tag(_rt_tag)
        if not any(_p in _rt_raw for _p in _rt_prefixes):
            continue
        # 与 _build_open_exposure() 的 net 构建逻辑完全一致
        _rt_inst = _t["instrument"]
        _rt_base, _rt_quote = _rt_inst.split("_")
        _rt_units = float(_t.get("currentUnits", 0))
        _rt_s = 1 if _rt_units > 0 else -1
        _rt_net[_rt_base] = _rt_net.get(_rt_base, 0) + _rt_s
        _rt_net[_rt_quote] = _rt_net.get(_rt_quote, 0) - _rt_s
        # 与 _execute_single_signal() 的 _strategy_open 口径完全一致：逐笔 +1
        _rt_open_count += 1
except Exception as _init_exc:
    print(f"  [MC-TOPUP] ❌ 敞口初始化失败 → fail-closed, 全轮跳过: {_init_exc}")
    return

# ============ PATCH C: L5176 循环内，regime 检查之前插入 ============
# 检查 1：MAX_OPEN_POSITIONS（基于运行时逐笔计数）
if _rt_open_count >= _MAX_OPEN_POSITIONS:
    print(
        f"  [MC-TOPUP] {instr}: 运行时持仓 {_rt_open_count}/{_MAX_OPEN_POSITIONS} → 跳过"
    )
    continue

# 检查 2：cross_net_cap（基于运行时方向累计 + TOPUP 方向增量）
# 新增一笔同向 trade 对 net 的影响 +sign（base）和 -sign（quote），
# 与 _build_open_exposure() 中每笔 trade 的贡献口径一致。
try:
    _base_ccy, _quote_ccy = instr.split("_")
    _sign = 1 if net > 0 else -1
    _sim_base = abs(_rt_net.get(_base_ccy, 0) + _sign)
    _sim_quote = abs(_rt_net.get(_quote_ccy, 0) - _sign)
    if _sim_base > _cross_net_cap or _sim_quote > _cross_net_cap:
        print(
            f"  [MC-TOPUP] {instr}: cap guard block — "
            f"{_base_ccy}={_sim_base}/{_cross_net_cap} "
            f"或 {_quote_ccy}={_sim_quote}/{_cross_net_cap} → 跳过"
        )
        continue
except Exception as _cap_exc:
    # ⚠️ fail-closed：cap 检查异常 → 跳过该 pair，绝不放行
    print(
        f"  [MC-TOPUP] {instr}: cap guard 异常 → fail-closed, skip: {_cap_exc}"
    )
    continue

# ... 原有的 regime 检查、MC 方向检查、SL/TP 检查保持不变 ...

# ============ PATCH D: L5246 下单成功后插入 ============
if _res:
    # 不能仅凭 _res 为真就假定新增一笔 trade。当前
    # utils/trading_core_v2.py 从 tradeOpened / tradeReduced 任取 trade_id，
    # 返回摘要不保留 fill 类型；账户模式和 fill 结果都没有在此验证。
    # 成功后用新的 broker 快照确认该 pair 的预期 units 变化，并重建运行时
    # 敞口和逐笔计数；若结果不可见、读取或解析失败，停止本轮剩余 TOPUP。
    try:
        _rt_trades = _trading_core.get_all_open_trades()
        _rt_net = {}
        _rt_open_count = 0
        _post_pair_net = 0.0
        for _t in _rt_trades:
            if not is_bot_owned_trade(_t):
                continue
            _rt_tag = _t.get("clientExtensions", {}).get("tag", "") or ""
            _rt_raw = _extract_raw_tag(_rt_tag)
            if not any(_p in _rt_raw for _p in _rt_prefixes):
                continue
            _rt_inst = _t["instrument"]
            _rt_base, _rt_quote = _rt_inst.split("_")
            _rt_units = float(_t.get("currentUnits", 0))
            _rt_s = 1 if _rt_units > 0 else -1
            _rt_net[_rt_base] = _rt_net.get(_rt_base, 0) + _rt_s
            _rt_net[_rt_quote] = _rt_net.get(_rt_quote, 0) - _rt_s
            _rt_open_count += 1
            if _rt_inst == instr:
                _post_pair_net += _rt_units
        _expected_pair_net = net + (add_units if side == "BUY" else -add_units)
        if _post_pair_net != _expected_pair_net:
            print(
                f"  [MC-TOPUP] ❌ {instr}: broker 快照尚未确认预期成交 "
                f"(expected={_expected_pair_net}, observed={_post_pair_net}) "
                "→ 停止本轮剩余补仓"
            )
            return
    except Exception as _refresh_exc:
        print(
            f"  [MC-TOPUP] ❌ 成交后敞口复核失败 → fail-closed, "
            f"停止本轮剩余补仓: {_refresh_exc}"
        )
        return
    print(f"  [MC-TOPUP] ✅ {side} {instr} +{add_units} 手 submitted")
```

**修复要点对照**：

| 用户指出的缺陷                           | 本修复方案如何解决                                                                                                                                                           |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ① 漏了 MAX_OPEN_POSITIONS                | 新增检查 1：`_rt_open_count >= _MAX_OPEN_POSITIONS`（逐笔计数）                                                                                                              |
| ② 异常时放行（fail-open）                | except 块内 `continue` 跳过该 pair，**fail-closed**。**初始化失败直接 return**（全轮 fail-closed）                                                                           |
| ③ 同轮多笔累积                           | 用 `_rt_net` / `_rt_open_count` 在循环内逐笔更新，下一笔检查基于已运行时模拟状态                                                                                             |
| 伪代码 `getattr(run_cycle_globals, ...)` | 直接访问 `_cross_net_cap`（global 变量，前置后本轮已赋值）                                                                                                                   |
| `_cross_net_cap` 时序                    | 步骤 1 将 MC 计算前置到 TOPUP 之前                                                                                                                                           |
| `len(held)` 低估持仓笔数                 | 从 broker 快照**逐笔**计算 `_rt_open_count`（L4550-4557 口径），不从 `held` set 推导                                                                                         |
| TOPUP 与 cap TAG 范围不一致              | **PATCH A**：TOPUP L5149 聚合时加 `_strategy_groups` tag_prefix 过滤；cap guard 初始化也用同一份过滤规则                                                                     |
| 补仓后 broker trade 计数前提不明         | **PATCH D**：成功后重新读取 broker 快照并重建敞口/逐笔计数；当前交易封装未保留 `tradeOpened` / `tradeReduced` 类型，不能仅凭 `_res` 真值推断新增一笔 |

**两项运行时状态的口径来源**：

| 状态             | 计数方式                                                          | 与现有口径一致吗                                                      | 来源位置   |
| ---------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------- | ---------- |
| `_rt_net`        | 逐笔累计方向单位（`s = ±1`, `net[base] += s`, `net[quote] -= s`） | ✅ 与 `_build_open_exposure()` 的 net 构建**逐行完全一致**            | L4162-4164 |
| `_rt_open_count` | 逐笔 `+= 1`（bot-owned + tag_prefix 匹配）                        | ✅ 与 `_execute_single_signal()` 的 `_strategy_open` **逐行完全一致** | L4550-4557 |

为什么两者口径不同？因为设计意图不同：`held` set 服务于"同 instrument 是否已有 bot 持仓"的幂等判断（同方向 1 笔和 5 笔对幂等来说都是"已持有"），而 `_strategy_open` 服务于"总持仓是否超限"的 cap 判断（5 笔比 1 笔风险大）。MC-TOPUP 在同一 instrument 上逐轮加仓，所以必须用逐笔口径。

**TOPUP 聚合范围 vs cap 统计范围对齐**：

| 过滤步骤              | TOPUP 聚合循环（L5148-5172） | cap guard 初始化（PATCH B） | cap 路径 `_execute_single_signal`（L4550-4557） |
| --------------------- | ---------------------------- | --------------------------- | ----------------------------------------------- |
| ① is_bot_owned        | ✅ L5149                     | ✅ PATCH B L228             | ✅ L4552                                        |
| ② tag_prefix 匹配     | ✅ **PATCH A 新增** L5149+   | ✅ PATCH B L230-232         | ✅ L4554-4556                                   |
| ③ base/quote 方向累计 | N/A（按 instrument 聚合）    | ✅ PATCH B L234-240         | N/A                                             |
| ④ 逐笔计数            | N/A（只聚合方向）            | ✅ PATCH B L242             | ✅ L4557                                        |

加 PATCH A 前 TOPUP 只做 ①、不做 ②，范围比 cap 宽；加 PATCH A 后 TOPUP ⊆ cap，所有 TOPUP 处理的持仓都进入 cap 统计。

**tag 字段必须与 cap 路径一致**：过滤仅读取 `clientExtensions.tag`，与 `_build_open_exposure()` / `_execute_single_signal()` 相同；不得用顶层 `tag` fallback 扩大 TOPUP 范围，否则仍会出现"能补仓但不计入 cap"的 trade。

**API 调用开销**：初始化直接复用 TOPUP 已获取的 `open_trades`，不再单独读取；每次成功下单后需要重新读取 broker 快照，核实实际敞口和逐笔持仓数，再决定是否允许后续 pair 补仓。

**一致性要求**：初始化必须使用同一份 `open_trades`，不能再次拉取后分别用两份快照做聚合与 cap 计数；否则两份数据在并发成交/平仓时可能不一致。成交后复核则是有意的新快照。

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

低风险。OANDA broker 确实不接受 < 1 units 的订单，floor 回退是合理的 broker 约束适配。如果要严格 shrink-only，可以把 L4756 改成"不足 1 手则 skip entry（fail-closed）"，但这会导致小额信号完全无法入场，是策略层面的改动而非 bug 修复。

#### 结论

**观察，暂不改动**。如果未来有新 broker 支持 fractional units，可再评估。

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

---

## 四、安全性审查矩阵（修正版）

| 风险维度                     | 是否覆盖  | 位置                                 | 备注                                                                     |
| ---------------------------- | --------- | ------------------------------------ | ------------------------------------------------------------------------ |
| 跨币种净敞口 cap             | ❌ 缺失   | `_pick_global_basket` L6513          | **MC-TOPUP 补仓绕过**（问题 A）                                          |
| 总持仓数 cap                 | ❌ 缺失   | `_execute_single_signal` L4582       | **MC-TOPUP 补仓绕过**（问题 A）                                          |
| OVERRIDE 集中度 cap          | ✅        | basket + execute 双检查              | PER_CYCLE + MAX_OPEN                                                     |
| 同 instrument 幂等           | ✅        | basket + execute                     | MC-TOPUP 绕过（设计确认）                                                |
| MC 方向冲突                  | ✅        | `_check_mc_direction_conflict`       | SEVERE / MODERATE 两档                                                   |
| Base ccy thesis 冲突         | ✅        | `_pick_global_basket` L4352          | quote ccy 结构性冲突不拦                                                 |
| 仓位 shrink-only 链          | 🟡 有条件 | execute 层                           | dlot floor 回退 + clamp floor 可放大（问题 D）                           |
| 所有权隔离（敞口视图）       | ✅        | `_build_open_exposure`               | bot-owned only                                                           |
| 所有权隔离（weak-exit 扫描） | ❌        | `_weak_exit_scan_v31` L5049          | 混入 manual（问题 B）                                                    |
| OVERRIDE 退出保护            | 🟡 有条件 | early-exit only                      | weak-exit / scale-out 不豁免（问题 C）                                   |
| Emergency stop               | ✅        | `_check_emergency_lock`              | 文件锁触发                                                               |
| 多 host 并发                  | 🟡        | `_single_host_recent_entry_detected` | 可选的 best-effort broker witness；仅正常入场路径使用，不是原子锁，MC-TOPUP 绕过 |
| 账户 guard                   | ✅        | L6022-6034                           | run_cycle 启动时一次性检查，MCTOPUP 在其后                               |
| SL/TP 强制挂单               | ✅        | `_execute_single_signal` + MC-TOPUP  | 两条下单路径都强制                                                       |
| Early Exit 五门              | ✅        | `_maintain_group_positions`          | OVERRIDE 豁免                                                            |
| 分批止盈                     | ✅        | `_scale_out_scan_v31`                | 锚定初始 R，永不漂移                                                     |
| 主动出局                     | 🟡        | `_weak_exit_scan_v31`                | 混入 manual 排名（问题 B），不豁免 OVERRIDE（问题 C）                    |
| MC-TOPUP 敞口                | ❌        | —                                    | **缺失 cross-net-cap + MAX_POSITIONS 检查 + 运行时模拟；亦绕过可选 single-host witness**（问题 A）；快照方案不提供多 host 原子性 |
| fail-closed 优先             | ✅ 大体   | 全文件                               | dlot 层唯一 fail-open（L4746），MC-TOPUP SL/TP 检查是 fail-safe（L5211） |

---

## 五、修复实施计划

### 步骤 1：前置 MC 计算（解决时序）

**目标**：让 TOPUP 在执行时能访问本轮正确的 `_cross_net_cap` 和 `_MAX_OPEN_POSITIONS`。

**操作**：将 L6214-6306（MC regime 分档 + cross_net_cap 动态 boost + STRONG_GAP synthesis）剪切到 L6209 之前。

**验证前置可行性**：

| 计算块             | 依赖变量                      | 就绪位置                      |
| ------------------ | ----------------------------- | ----------------------------- |
| `_all_trade_pairs` | L6185-6188                    | ✅                            |
| MC cache           | L6190 `_load_mc_cache`        | ✅                            |
| `_global_scores`   | L6147 `build_strength_matrix` | ✅                            |
| `_PROB_INDEX`      | L6198-6206 `_load_prob_index` | ✅（可选，MC 计算本身不依赖） |

**改动前后的代码流**：

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
  L6307+:  _mc_min_position_topup_scan_v31(dry_run)     ← 可以访问本轮正确值
  L6488:  _build_open_exposure()                         ← 不变
  L6513:  _pick_global_basket(...)                       ← 不变
```

### 步骤 2：TOPUP 增加完整 cap guard（含运行时模拟 + fail-closed）

在前置后的 `_mc_min_position_topup_scan_v31()` 中按以下插入点实施。具体代码见**问题 A → 修复方案 → 步骤 2**。

**插入点**：

1. L5176 `for instr, rec in agg.items():` **之前** → 初始化运行时模拟状态
2. L5176 循环内 regime 检查**之前** → MAX_OPEN_POSITIONS + cross_net_cap 检查（fail-closed）
3. TOPUP 第一次下单前 → 若 `SINGLE_HOST_GUARD_ENABLED`，调用现有 broker witness；该 witness 为 best-effort，不提供跨 host 原子性
4. 下单返回成功后、输出成功日志前 → 刷新快照并确认目标 pair units 按预期变化，再重建运行时模拟状态；不匹配或刷新失败时停止本轮剩余补仓

### 步骤 3（可选，设计确认后）：weak-exit 增加 bot-owned 预过滤（问题 B）

在 L5049 的 for 循环内增加一行：

```python
if not is_bot_owned_trade(t):
    continue
```

### 步骤 4（可选，设计确认后）：OVERRIDE 退出路径统一（问题 C）

二选一：

- **C1**：weak-exit 和 scale-out 的扫描循环内增加 tag 匹配（排除 OVERRIDE tag）
- **C2**：在 L5026 注释和 config 注释中显式声明 "OVERRIDE 的退出路径 = broker SL/TP + scale-out + weak-exit（除 early-exit 外无豁免）"

### 步骤 5（可选）：普通 boost 分支加硬上限（问题 E）

L6262 之后加 `_cross_net_cap = min(_cross_net_cap, 8)`，与 L6295 对齐。

---

## 六、附录：run_cycle 精确行号锚定（当前工作区）

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
| TOPUP 内部 per-instrument 循环        | L5176      | cap guard 插入位置                            |
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
4. **问题 C 建议采用 C2，保留现有行为并消除文档歧义**：OVERRIDE 的 7d MIN_HOLD / runner-close 豁免仅适用于 `_maintain_group_positions` 的 early-exit；weak-exit 与 scale-out 仍可退出 OVERRIDE。若 Owner 原意是 OVERRIDE 完全不受主动退出/分批止盈影响，则应改选 C1，并补测试后再实现。
5. **问题 D/E 继续观察**：现有证据不足以证明值得改变下单下限或扩展 cap 配置语义；本轮不建议随 P0 修复一起改动。

### Owner 确认门槛

- **A（P0）**：批准 TOPUP 使用本轮动态 cap，且不使用 OVERRIDE 的 `MAX_OPEN_POSITIONS` bypass；`SINGLE_HOST_GUARD_ENABLED` 打开时也须遵从该 witness。每次成功下单后通过新 broker 快照确认并重建运行时敞口，不支持复核时 fail-closed 停止本轮剩余补仓。另确认多 host 硬 cap 是否为要求；当前 broker witness 非原子锁，不能提供该保证。
- **B（P1）**：建议 B1（扫描时仅纳入 bot-owned 持仓）；此举改变 manual 持仓对组排名 / MIN_GROUP_SIZE 的影响，须确认后再改代码。
- **C（P1）**：建议 C2（OVERRIDE 只豁免 early-exit）；Owner 确认其完整退出生命周期后，更新代码注释和相关测试。

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
