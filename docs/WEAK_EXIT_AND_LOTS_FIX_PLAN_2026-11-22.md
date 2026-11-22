# weak-exit 提前出局 & live 仓位管理 修复方案（备案）

- 日期：2026-11-22
- 状态：**问题一（weak-exit）与问题二（并发上限 4 + MC 置信度 1..4 手数）均已实施并通过回归测试**；MC 接线按「复用现有 regime」已直接落到实盘接线（无新增阈值）
- 范围：`utils/weak_strength_exit.py`、`config.py`、`config_bot.py`、`scheduled_runner_v3.py`、`scheduled_runner_v1441.py`、`utils/prob_zone_sizing.py`、`config/run.env.*`、`tests/test_weak_strength_exit.py`、`tests/test_problem_two_wiring.py`

---

## 0. 结论速览

1. **主动出局（weak-strength exit）确实过松**：当某 quote 组只有 1 笔持仓时，该笔必然"垫底"→ 必平，导致任何持仓满 `MIN_HOLD_MIN`(180min) 后被无差别平掉，随后立刻重开，形成 churn。
2. **仓位管理开关是打开的**（`PROB_ZONE_SIZING.ENABLED=True` 等），但 live 下因基数=1 而**空转（永远 1 手）**，且**没有最大手数上限**；另有一处 `MIN_LOT` 键错配使 DEMO 最小手数下限失效。

---

## 1. 问题一：weak-exit 提前出局

### 1.1 现状与配置
- 开关：`WEAK_STRENGTH_EXIT.ENABLED = True`，`N = 1`，`MIN_HOLD_MIN = 180`（`config.py`）。
- 逻辑：按 quote 分组 → 组内按支撑分升序 → 平掉垫底的 N 名。**无强度反转条件、无盈亏条件**。
- 单笔组规则：`utils/weak_strength_exit.py` 注释明确「组内只有 1 笔时，该笔即『垫底』→ 照平」。

### 1.2 证据（影子日志，含真实平仓 `dry_run:false`、`applied_ids` 非空）
- `logs/weak_exit_shadow_20261120.jsonl`：7 笔平仓，**全部 `group_size=1`、`rank=1`、age≈180–195min**。
- `logs/weak_exit_shadow_20261119.jsonl`：4 笔，同样模式。
- 现象：`AUD_JPY` / `GBP_JPY` SELL 反复"开 → 3h 后平 → 再开"，被点差/费用磨损。

### 1.3 修复项

| 编号 | 方案 | 优先级 | 落点 |
|---|---|---|---|
| A | **组规模闸**：`len(members) < MIN_GROUP_SIZE`（新键，默认 2）时跳过该组，不平仓 | 必做 | `weak_strength_exit.py` 分组循环；`_CFG_FALLBACK` 增 `MIN_GROUP_SIZE: 2`；`config.py` 的 `WEAK_STRENGTH_EXIT` 增 `MIN_GROUP_SIZE: 2` |
| B | **绝对强度闸**：仅当 `support < SUPPORT_FLOOR`（或相对入场下降 ≥ `DROP_MIN`）才平 | 可选（默认关） | 同上 eligible 过滤段 |
| C | **P/L 闸**：仅当浮亏（或 ≤ `-xR`）才主动平；盈利单交给 scale-out/SL/TP | 可选（默认关） | 需 runner 向 positions 传入 `unrealizedPL`/`r` |
| D | **最小持仓时间**：`MIN_HOLD_MIN` 180 → 1440 | 可选 | `config.py` |

- **预期效果**：仅做 A，则按 11-20 日志关闭数 7 → **0**（全部组规模=1）。
- 建议**先只做 A**（最小改动、直接消除问题），B/C/D 视回测再定。

### 1.4 附带一致性问题（可选）
- v3 用 `_gated_close_trade_by_id`（含权属门禁），v1441 用 `close_trade_by_id`（**无门禁**）。同账户若有其它 runner/人工单会被误平，建议对齐。

---

## 2. 问题二：live 仓位管理（上限 clamp + 归一化）

### 2.1 现状
| 编号 | 问题 | 位置 |
|---|---|---|
| P1 | `_resolve_effective_lots()` **无 min/max clamp** | `scheduled_runner_v3.py` / `scheduled_runner_v1441.py` |
| P2 | live `LIVE_LOT_SIZE=1` → 基数=1 → 缩仓层 `int(1*scale+0.5)=0`→`max(1,…)=1`，**永远 1 手** | `config/run.env.tuned`、`utils/prob_zone_sizing.py` |
| P3 | **键错配**：prob-zone 读 `MIN_LOT`，config 定义的是 `MIN_LOT_LIVE`/`MIN_LOT_DEMO` → 后者为死键，DEMO 1000 下限未生效 | `utils/prob_zone_sizing.py` vs `config.py` |

### 2.2 期望行为（对齐 `~/projects/ai_training_cnn/`）
- live：**最小 1 手，最大 = 命令行参数与 run.env 设置（now 4）**。
- 只出现**整数手**，缩仓层把基数从 max 缩到 1..max 的整数区间。

### 2.3 修复项

| 编号 | 方案 | 优先级 | 落点 |
|---|---|---|---|
| A | **手数夹取 `[1, max]`**：`max = 解析出的名义手数`（`--lots` > run.env `LIVE_LOT_SIZE/DEMO_LOT_SIZE` > config_bot）；所有下游层（post-exit gate → prob-zone → dynamic-lot）结果统一 `clamp(units, 1, max)` | 必做 | 两个 runner 计算完 `_order_units`/`effective_units` 后 |
| B | **让缩仓真正生效**：live 基数设为 max=4（`run.env` 改 `LIVE_LOT_SIZE=4`）；可选新增 `MAX_LOT_SIZE`（默认=名义手数）显式封顶、`MIN_LOT_SIZE`（默认 1） | 必做（配合 A） | `config/run.env.*` |
| C | **修 MIN_LOT 键错配**：runner 调用前按环境注入 `{**cfg, "MIN_LOT": cfg["MIN_LOT_LIVE"] if is_live else cfg["MIN_LOT_DEMO"]}` | 必做 | 两个 runner 调 `compute_prob_plan` 前 |
| D | 整数化 | 已满足 | `int(...+0.5)` + `max(min_lot,…)` 已在位，无需处理 |

- 效果：DEMO 最小手数下限恢复生效；live 仍为 1（下限），缩仓层可在 1..4 产生整数手差异。

---

## 3. 验证计划（改完必须跑）
1. **weak-exit 单测（新增回归）**：构造"组内仅 1 笔"positions，断言 `close_ids == []`；组内 ≥2 笔且垫底者通过时仍能平。当前无该模块测试，需新建（放 `tests/`）。
2. **手数**：`--live --dry-run` 跑一轮，核对 banner `Lots:` 与 `logs/lots_sizing_shadow_*.jsonl` 的 `size_units` 落在 `[1, max]` 整数区间。
3. **回归**：`WEAK_STRENGTH_EXIT.ENABLED=False`、`MIN_GROUP_SIZE` 缺省时保持受控行为。

---

## 4. 风险与兼容
- A：weak-exit 在"单笔持仓"时基本休眠——这正是消除提前出局的目的；如需保留"弱强度主动离场"，建议配 B/C 后再启用。
- B：live 基数 1→4 会**放大大盘实际下单手数**（1 变到最多 4），属真实资金风险，**须明确批准后**才改 run.env。
- 涉及交易逻辑/风控参数，按项目规则待逐条确认后再动代码。

---

## 5. 待决策项（Checklist）
- [ ] 问题一：是否采用 A（组规模闸）？B/C/D 是否启用？
- [ ] 问题一：是否对齐 v1441 的权属门禁？
- [ ] 问题二：是否接受 live 基数 1→4（真实资金放大）？
- [ ] 问题二：是否新增 `MAX_LOT_SIZE` / `MIN_LOT_SIZE` 显式键？
- [ ] 问题二：是否修 `MIN_LOT` 键错配？

---

## 6. 决策更新（2026-11-22 第二轮）

### 6.1 已确认（用户拍板）
- [x] **问题一（weak-exit）**：**删除 `MIN_HOLD_MIN` 时间闸 + 新增"组内 ≥2 笔才评估"闸**（组规模闸，`MIN_GROUP_SIZE=2`）。落点：`utils/weak_strength_exit.py`（分组循环、`_CFG_FALLBACK`）、`config.py` 的 `WEAK_STRENGTH_EXIT`。
- [x] **主动平仓口径**：每次只平"组内最垫底的一笔"（`N=1`，逐个来，**不平整个 group**）——与当前实现一致，显式固定。
- [x] **问题二（开仓）**：**并发持仓上限 = 4，每个周期最多开 1 笔**（逐步建仓到 4，不一次性满仓）。入场节奏固定为**逐个入场，间隔一个周期 = 15 分钟**。

### 6.2 MC 接线方向（用户决策：直接改接线）
把 MC 从"风控上限"升级为"信号强度 / regime 驱动"：
- 强共识 regime（如 JPY 7/7 同向、5/7 STRONG_MOMENTUM）→ **火力全开 / 满仓运行**：并发上限取 4、单笔按 MC 置信度映射 1..4（高置信给满）、同向篮子按置信排序。
- 入场仍逐个来（每周期 1 笔）。
- 持仓管理：MC 大周期未翻转前，不因小周期动量减弱而过早平仓（与 6.1 的 weak-exit 改动联动）。

**规则草案（待确认）**：
- 触发阈值：同货币对同向 pair 数 ≥ K、`|P(dir)|` ≥ 阈值、`STRONG_MOMENTUM` 占比 ≥ X%。
- 可复用旋钮：`MC_MAX_POSITIONS_AGGRESSIVE`、`ENABLE_MC_BASKET_EXECUTION`、`MAX_NEW_ENTRIES_PER_CYCLE`、JPY regime（`JPY_REGIME_STRONG_COUNT`）。

### 6.3 待确认
- [x] "满仓"口径**已定**：**并发上限 4 + 单笔手数按 MC 置信度映射 1..4**（不是仅并发数 = 4）。
- [x] 强共识触发**已定**：**复用现有 MC regime**（`STRONG_MOMENTUM`→`aggressive`→满仓），**不新增阈值**。
- [x] MC 接线落点**已定**：**直接改实盘接线**（v1441 与 v3 一致）。
- [ ] 是否以 W（周）维度作为"大周期"依据（当前 W 仅 observation，未驱动决策）
- [ ] MC 数据质量：本次报告 35 pairs 但含重复货币对（EURUSD/GBPUSD/NZDUSD/USDCHF 重复；USDJPY 出现 159.713 与 159.705 两个价格）→ 正式统计前需去重 + 快照一致性校验
- [ ] MC 验证闭环：`MC_ALIGNMENT_LOG_FIELDS` 无写入方（schema 死置），是否补 writer 以积累 outcome 数据

### 6.4 落地记录（问题一，已完成）
- 代码：
  - `utils/weak_strength_exit.py`
    - `_CFG_FALLBACK`：删除 `MIN_HOLD_MIN`，新增 `MIN_GROUP_SIZE: 2`。
    - `compute_weak_exit_plan`：删除 `min_hold` 时间闸；新增组规模闸——组内**可评估**笔数 `< MIN_GROUP_SIZE` 时整组本轮不主动平，并把每笔记入 `skipped`（`reason=group_too_small(n<m)`）。
    - 保留 fail-closed：`no_support_score` / `no_age` 仍跳过；命中时仍只平垫底 `N` 笔。
    - 更新模块 docstring 与 `__main__` 冒烟样例（去掉 `MIN_HOLD_MIN`）。
  - `config.py`：`WEAK_STRENGTH_EXIT` 去 `MIN_HOLD_MIN`、加 `MIN_GROUP_SIZE: 2`，注释同步。
  - `scheduled_runner_v1441.py`、`scheduled_runner_v3.py`：`_weak_exit_scan_*` docstring 由「保留最短持仓时间闸」改为「组规模闸」。
- 回归测试：新增 `tests/test_weak_strength_exit.py`（12 项，全绿）：
  - 单笔组不平（`group_too_small`）；组内 ≥2 笔仍平垫底 1 笔且 `group_size` 正确。
  - 年龄不再拦截（`age=5` 亦可参与排名）；旧键 `MIN_HOLD_MIN` 被忽略且无副作用。
  - `no_support_score` / `no_age` / `ENABLED=False` / 畸形输入**永不抛异常**。
- 冒烟：`python utils/weak_strength_exit.py` → JPY 组平 T2、CHF 单笔组记 `group_too_small`。
- 未做（仍待定）：v1441 权属门禁对齐（v3 用 `_gated_close_trade_by_id`，v1441 用无门禁的 `close_trade_by_id`）。

### 6.5 落地记录（问题二 + MC 接线，已完成）
按 6.1/6.2/6.3 口径落地：**并发上限 4 + 每周期 1 笔 + 单笔按 MC 置信度 1..4（shrink-only）**，MC 接线复用现有 MC regime，直接改实盘接线。

- 并发上限（config 层）：
  - `config.py`：`MC_MAX_POSITIONS_NEUTRAL=4` / `MC_MAX_POSITIONS_CONSOLIDATION=1` / `MC_MAX_POSITIONS_AGGRESSIVE=4`（注释同步为「并发上限 = 4」）。
  - `config_bot.py`：新增同值 `MC_MAX_POSITIONS_*`（v3 通过 `getattr(_config_bot, ...)` 读取，必须与 `config.py` 一致）。
- 并发闸（v1441，此前**无实盘并发闸**，只有 pair 级幂等）：
  - 新增 `_strategy_open_count()`：只统计本 runner 名下（`_is_jpy_strength_trade`，即 `JPY-STRENGTH` tag）的在场持仓；查询失败返回 `None`。
  - `run_cycle` 开仓循环内新增闸：`_open_n is None or _open_n >= max_positions` → 跳过本轮入新仓（`entries_blocked += 1`，`final_action=HOLD`，`reason=max_positions(...)`）；`max_positions` 取自 `_regime_params(mode)`（复用现有 MC regime）。
  - 每周期最多开 1 笔：沿用既有 `max_entries=1`（`--live` 时）与逐个 `continue` 语义。
- 单笔手数（MC 置信度 1..4）：
  - `run.env`：`LIVE_LOT_SIZE=1 → 4`（名义满仓 = 4）；`config/run.env.tuned` 同步 `LIVE_LOT_SIZE=4`、`MAX_POSITIONS=4`。
  - prob-zone 为 shrink-only（`size_scale ∈ [F_LO=0.25, 1.0]`）→ 基数 4 映射为整数手 1..4。
  - 两个 runner 在 prob-zone 计算后新增 clamp：`max(1, min(int(units), int(_EFFECTIVE_LOTS)))`（v1441 用 `effective_units`；v3 用 `_order_units`）。
  - `MIN_LOT` 键错配修复：runner 调 `compute_prob_plan` 前按环境注入 `MIN_LOT = cfg["MIN_LOT_LIVE" if is_live else "MIN_LOT_DEMO"]`（v1441 用 `_args.live`；v3 用 `_IS_LIVE`）。
- 回归测试：新增 `tests/test_problem_two_wiring.py`（16 项，全绿）：
  - 配置矩阵：`config.py` 与 `config_bot.py` 的 `MC_MAX_POSITIONS_*` 均为 4/1/4；`WEAK_STRENGTH_EXIT` 含 `MIN_GROUP_SIZE=2` 且无 `MIN_HOLD_MIN`；`PROB_ZONE_SIZING` 只暴露 `MIN_LOT_LIVE/DEMO`（记录键错配根因）。
  - `_strategy_open_count()`：只计本策略（含 `GEMINIAIBOT_V3::` 前缀形态）持仓；查询失败返回 `None`。
  - 并发闸端到端（dry-run，全 mock）：达上限 → 拦截入新仓、`Entries blocked: 1`、`Core reason: max_positions(...)`、不进入下单校验分支；未达上限 → 放行；查询失败 → fail-closed。
  - 名义手数：CLI `--lots` 优先；`--live` 默认取自 `run.env LIVE_LOT_SIZE=4`。
  - MIN_LOT 注入契约：未注入 → 模块默认 1；注入 → 下限生效；shrink-only 永不放大。
  - 接线守卫：两个 runner 源码均含 MIN_LOT 注入与整数手 clamp。
- 全量验证：`python3 -m py_compile`（4 文件）通过；`tests.test_weak_strength_exit`(12) + `tests.test_v1441_hardening` + `tests.test_scheduled_runner_tradingcore_v2_wiring`(共 32) 全绿；新增 16 项全绿。

### 6.6 落地记录（run.env 主动出局软开关，已完成）
- 结论先验（本轮确认，无需改代码）：
  - 非JPY 探测/交易：`run.env TRADE_JPY_ONLY=true` → v3 只跑 JPY 组（USD/CHF 组整体跳过）；v1441 本就 JPY-only。**保持现状**。
  - 入场选择：保持 `abs-max` 单笔（每周期最多 1 笔）。**保持现状**。
- 新增软开关 `WEAK_EXIT_ENABLED`（**无条件生效**，不受 `--live` 限制，覆盖 `config.WEAK_STRENGTH_EXIT["ENABLED"]`）：
  - `run.env`：新增第 7 节 `WEAK_EXIT_ENABLED=true`（true=启用真平仓；false=关闭本轮主动平仓）。
  - `scheduled_runner_v1441.py`：`_candidates` 增加 `WEAK_EXIT_ENABLED`；`_weak_exit_scan_v1441` 读 `_ENV_LOADED_KEYS`/`os.environ` 并按 `_parse_bool_env` 覆盖 `ENABLED`；未设则回退 config。
  - `scheduled_runner_v3.py`：`_candidates` 增加 `WEAK_EXIT_ENABLED`；`_weak_exit_scan_v31` 用 `_env_or_config("WEAK_EXIT_ENABLED", None, value_type=bool)` 覆盖 `ENABLED`；未设则回退 config。
  - 语义边界：开关只控制 `ENABLED`；`SHADOW` 不变（`ENABLED=true` 时仍写真影子日志）。
- 回归测试：新增 `tests/test_weak_exit_switch.py`（7 项，全绿）：
  - v1441 端到端（mock）：env=false+config=true → 不平；env=true+config=false → 平垫底 1 笔；未设 → 回退 config（true/false 两向）。
  - 接线守卫：`run.env` 声明 `WEAK_EXIT_ENABLED=`；v3 源码含 `_env_or_config("WEAK_EXIT_ENABLED"`；config 默认 `ENABLED=True`。
- 全量验证：`py_compile`（v1441/v3）通过；55 项测试全绿（12+7+16+20）。启动日志确认 `[CONFIG] loaded from run.env: WEAK_EXIT_ENABLED=true`。

### 6.7 落地记录（MC regime → 最小仓位 / 阶梯补仓，已完成）
- 需求（用户拍板）：让 MC dataset 的 regime 指导仓位。新增「最小仓位」变量，并新增一条规则：某周期老仓只有 1 个 Unit（可能是昨天开的），今天发现 STRONG MOMENTUM 且方向一致 → 追加同向单补仓。
- 作用对象：最小**单笔手数**；只针对**特定 regime 变强的货币对**做增量（其余不动）；**只改 v3**（v1441 不动）。
- 软开关与数值（`run.env` 第 8 节，**无条件生效**）：
  - `MIN_LOT_STRONG_ENABLED=true`（false=整条规则休眠）。
  - `MIN_LOT_LIVE=1`、`MIN_LOT_DEMO=1000`（最小单笔手数；阶梯基数 = 最小仓位 × 1/2/4/8）。
- 新开仓分档（`scheduled_runner_v3.py::_execute_single_signal`）：
  - `_regime_tier_units()` 映射：`CONSOLIDATION→最小仓位` / `NEUTRAL→最小×2` / `STRONG_MOMENTUM→最大仓位`。
  - 以 regime 档为基数，与动态仓位（DLOT）取较小值，prob-zone 只能再缩；最终 clamp 到 `[最小仓位, 名义最大仓位]`。
- 老仓补仓（新增 `_mc_min_position_topup_scan_v31`，挂在 run_cycle 中 MC 缓存加载之后）：
  - 每轮遍历本 runner 名下 bot-owned 老仓，按 pair 聚合净持仓。
  - 触发条件：`regime == STRONG_MOMENTUM` **且** MC 方向概率与持仓方向一致。
  - 加仓节奏：**每轮只升一级**（`_next_ladder_units`，最小仓位 × 1/2/4/8），目标 = regime 目标档（STRONG=最大），上限 = 名义最大仓位；绝不一次倒满、绝不超最大仓位。
  - 受控绕过新开仓的幂等保护（`ALREADY_HELD`）——这是唯一被允许追加同向单的入口；缺 SL/TP 则 fail-safe 跳过。
- 无关项保持现状：Cooldown（`utils/active_exit_risk.py` 全仓零引用死代码）不启用、不提高门槛；`PostExitGate` 保持现状；v1441 不改。
- 回归测试：新增 `tests/test_mc_min_position.py`（19 项全绿）：regime 档映射、阶梯逐级 +1、封顶不超最大、方向不符/非 STRONG/开关关闭/无 SL-TP/手动仓 均不动、dry-run 不下单、接线守卫。

