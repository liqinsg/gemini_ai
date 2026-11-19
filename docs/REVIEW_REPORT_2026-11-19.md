# 修改与审核报告 — 2026-11-19（盘中改动）

审查范围：今日提交的 3 个 commit（`b05c828..873dabb`），
主 runner = `scheduled_runner_v1441.py`（crontab `--profile 1`），
活跃策略文件 = `custom_strategy_v1.py`（注意：**不是**根目录的 `custom_strategy.py`）。

---

## 一、今天改了什么（修改报告）

| # | Commit | 时间 | 文件 | 内容 |
|---|--------|------|------|------|
| 1 | `873dabb formatfix` | 12:34 | `config.py`, `scheduled_runner_v1441.py` | black 格式化、补齐文件末尾换行、注释空格。**无逻辑变更** |
| 2 | `effd0c4 trading_core` | 08:43 | `utils/trading_core_v2.py` | 移除 `TradeClose` 的 `client_request_id` header 传参（OANDA 该接口不接受 headers），改为仅日志用途。**Bugfix** |
| 3 | `24c1338 proactively exit` | 08:24 | `config.py`, `scheduled_runner_v1441.py`, `scheduled_runner_v3.py`, `utils/weak_strength_exit.py`(新增), 删除 `scheduled_runner_v31.py` | **今天的功能主体**：新增"弱强度主动离场" |

### 今天功能主体：弱强度主动离场（Weak-Strength Exit）

- 新增纯函数模块 `utils/weak_strength_exit.py`（253 行，永不抛异常、fail-closed）。
- `config.py` 新增配置块 `WEAK_STRENGTH_EXIT`（`ENABLED=True, SHADOW=True, N=1, MIN_HOLD_MIN=180`）。
- `scheduled_runner_v1441.py` 新增 `_weak_exit_scan_v1441()`，接线在 **EARLY-EXIT 之后、SCALE-OUT 之前**。
- `scheduled_runner_v3.py` 大幅扩展（+1497 行）：把 `prob_zone_sizing` / `scale_out` / `weak_strength_exit` / 动态仓位（`_compute_extension_lot_scale`、`DYNAMIC_LOT_*`）/ JPY 动态权重 v2 等**对齐到了 v3 runner**；同时删除旧文件 `scheduled_runner_v31.py`。

> 一句话：**今天新增了"主动减/平仓"能力**（弱强度离场），并把 v3 runner 补齐了仓位/出场相关功能。

---

## 二、审核发现（对应你关心的 3 点）

### 1. 开仓策略 / SL / TP / entry & exit

**入场（`custom_strategy_v1.py`）**
- 货币强弱排名：`build_strength_matrix()`（H1/H4/H8 动量加权，`utils/strategy_helpers.py:190`）→ JPY 强弱差。
- MA5 多周期对齐：H4/H1/M30 三周期，`REQUIRE_ALIGNED = 3`（`config.py:50,55`）。
- 方向一致性 + 动态强度门槛 `max_gap × 0.4`（`STRENGTH_CUTOFF_RATIO`）。
- 最终只选强度最强的 **top1** pair。

**SL / TP（当前 = ATR 倍数法，`config.py:153 ENABLE_ATR_SLTP=True`）**
- SL = `ATR(14) × 乘数`，乘数按日线 ATR 的 z-score 选 **1.8 / 2.2 / 2.8**（`custom_strategy_v1.py:327-331`）。
- TP = SL 距离 × **2.0**（`ATR_RR_MULTIPLE`）；最低 R:R = 1.2；另加 ATR 低波动过滤。
- 已确认是"ATR × 系数"，与你的判断一致。

**关键发现：支撑/阻力版 SL/TP 其实已经写好，只是被关掉了**
- `custom_strategy_v1.py:396-449` 的 `else` 分支是**完整的结构 S/R 版**：SL 放日线支撑/阻力外侧 + 缓冲，TP 锚日线/周线阻力/支撑并 front-run，突破后扩展到周线。
- 它仅在 `ENABLE_ATR_SLTP=False` 时启用。也就是说：**"改用支撑阻力"不需要从零开发**，切开关 + 调参 + 回测即可。
- `utils/find_support_resistence.py`（分形算法）当前只在**诊断日志**和被禁用的周线保护（`ENABLE_MACRO_PROTECTION=False`）中用到；`utils/calculate_pivots.py` 是**无引用死代码**。

**Exit（6 条规则，执行顺序隐含优先级）**
1. Broker 原生硬 SL / 硬 TP（下单时挂入 + Guardian 修复缺失）
2. MA 反向早期退出（`require_aligned=2`，比入场宽松）
3. **弱强度主动离场（今天新增）**
4. 分批止盈 SCALE-OUT
5. 反向信号平仓、6. 紧急全平

**⚠️ 冲突点：硬 TP 与 SCALE-OUT 第二档重合／被抢先**
- 策略默认 TP = 2R，而 SCALE-OUT 第二档也是 **+2R** → 价格触及硬 TP 时整仓被 broker 一次平掉，第二档几乎永远轮不到（只有 +1R 第一档有效）。
- 更严重：`_apply_tp_multiplier()`（`scheduled_runner_v1441.py:1055`）会按 MC regime 把 TP 改成 `entry ± risk × 1.5/0.8/1.0`（`config.py:508-510`）。CONSOLIDATION 时 TP=**0.8R**，**低于 SCALE-OUT 的 +1R 档** → 硬 TP 先触发，整个分批止盈形同失效。

### 1.2 仓位控制（lots、关仓、减仓）

**开仓 lots：不是"风险百分比"模式**
- 默认固定单位（`RISK_LEVEL=10 → 10000`，或 `run.env` 的 `LIVE_LOT_SIZE/DEMO_LOT_SIZE`）。
- 叠加 `PROB_ZONE_SIZING`（只缩不放，`0.25 ~ 1.0`）。
- **没有"按账户货币的风险上限"**，也**不按 SL 距离反推 units**。→ 不同波动的 pair 用同样单位，每笔风险并不一致（与 Codex 判断一致）。

**减仓 vs 平仓——概念要分清**
| 机制 | 行为 | 状态 |
|------|------|------|
| SCALE-OUT | **部分减仓**：+1R 平 50%，+2R 平 50%（幂等，R 锚定初始 entry/SL） | `ENABLED=True, SHADOW=True`（11-18 "lots scaling" 提交引入，非今天） |
| WEAK-STRENGTH EXIT（今天新增） | **整仓全平**（`close_trade_by_id`，ALL units） | `ENABLED=True, SHADOW=True` |
| MA 反向 EARLY-EXIT | 整仓全平 | 已有 |

> 你说的"减仓策略"最贴切的是 **SCALE-OUT**；今天的 **WEAK-EXIT 是"主动整仓平掉组内最弱持仓"**，不是 reduce。

**弱强度离场逻辑（`utils/weak_strength_exit.py`）**
- 按计价货币（quote）分组 → 组内按"方向调整支撑分"升序（BUY=base−quote，SELL=quote−base）→ 平掉垫底 `N=1` 名。
- `MIN_HOLD_MIN=180` 分钟保护新仓；入场用 `build_strength_matrix()`，出场也用同一个函数 → **入场/出场对称，设计合理**。
- ⚠️ **无 tag/comment 所有权过滤**（注释称"bot 账号不手工开仓故安全"）。若**同账户**存在其它 runner 或人工单，会被误平。

**减仓后 SL 不动**
- SCALE-OUT 只调用 `close_trade_partial()`，**不改 SL/TP**（`utils/scale_out.py:177-263`）。
- 保本/追踪（breakeven/chandelier）在**已退役**的 `dynamic_risk_manager.py`（`ENABLE_DYNAMIC_RISK_MANAGER=False`），v1441 未接线 → **减仓后剩余仓位仍按原始 SL 承担全风险**。

**fail 语义不一致（需修）**
- `prob_zone_sizing.compute_prob_plan()` 内部是 **fail-closed**（异常/数据缺失 → 降到 0.25 地板仓）。
- 但 runner 外层 `try/except`（`scheduled_runner_v1441.py:1868-1898`）是 **fail-open**（异常时保留原 units）。
- 两者语义相反，注释与实现不一致（与 Codex 判断一致）。

### 2. ML 与概率

**ML：当前完全关闭，且只是过滤器**
- `ENABLE_ML_CONFIRMATION = False`（`config.py:187`，`config.py:223` 再次覆盖为 False）→ `should_avoid_pair()` 直接放行、`get_confidence()` 返回 0.5。
- 若开启：预测**未来 3 根 H1 涨跌**；SVM(RBF) + Platt 校准；**per-pair** 独立模型；单次 80/20 holdout。
- 质量门槛 `ML_MIN_HOLDOUT_F1 = 0.0` → **形同虚设**。
- ML 是 **fail-open**（推理失败返回中性 0.5，不拦交易）。

**概率（MC）：只用于 sizing，且尺度不匹配**
- `PROB_ZONE_SIZING ENABLED=True`，shrink-only（三轴取 min：方向确信度 / 风险封顶 / regime 倍率）。
- `VERDICT_MODE=soft`（NO_EDGE 仅 ×0.5）；`REQUIRE_ALIGNED=False`（方向冲突**不拦截**，只缩仓）。
- MC 数据是**日线快照**，而入场是 H4/H1/M30 → **时间尺度不匹配**；概率未见校准验证。

### 3. 其它审核发现（低风险，但建议记录）

- **`build_strength_matrix` 签名不匹配（已实测验证）**：
  `utils/strategy_helpers.build_strength_matrix()` 签名是 `() -> Dict[str,float]`（无参），
  但 `scheduled_runner_v1441.py:1596` 和 `:1978` 用**带参 + 3 元组解包**调用 → 每次 `TypeError` 被外层 `try/except` 吞掉。
  后果：`cur_sc` 诊断 与 comment 里的 `open_rk`（strength_rank）**长期为 None**。仅影响诊断/审计数据，不影响下单。（此问题**先于今天**存在）
- **死代码/未接线**：`utils/sl_tp_helper.py` 空文件仍被 import；`calculate_pivots.py` 无引用；`dynamic_risk_manager.py` / `pyramid_cluster.py` / `risk_integration.py` 整条 risk 线未接入（保本/追踪/时间止损全部未启用）。
- **多份策略副本**：`custom_strategy.py` / `custom_strategy_v1.py` / `custom_strategy_v3.py` / `utils/custom_strategy.py`；ML 也有两份（根目录 + `utils/`）。易误改。
- **共识崩溃不主动平仓**：策略侧有 dominance guard 标志，但 v1441 **未读取** → 共识不足时仅"HOLD"，不清理已有仓位。
- **账户/runner 边界需确认**：crontab 同时跑 `scheduled_runner_v1441.py --profile 1` 与 `scheduled_runner_v3.py`。请确认二者**是否同账户、品种是否重叠**；结合 weak-exit 无所有权过滤，若同账户会彼此误平。

---

## 三、改进方向

**P0（先做，风险最高）**
1. **风险金额化定仓**：以 `账户净值 × 单笔风险% ÷ SL距离` 反推 units，让不同波动 pair 的每笔风险一致；先补"SL 距离 → 账户货币"换算。
2. **统一 fail 语义**：明确概率数据缺失/异常时到底是"拒单 / 缩到地板仓 / 放行基准仓"，让 runner 与 `prob_zone_sizing` 一致（目前相反）。
3. **修 SCALE-OUT 与硬 TP 冲突**：避免 2R 硬 TP 抢先第二档；解决 MC 把 TP 压到 0.8R 时第一档永远不触发的问题（例如：启用 scale-out 时把硬 TP 外移，或让 MC TP 只在 ≥1R 时生效）。

**P1（策略升级）**
4. **SL/TP 从"ATR×系数"升级**：直接启用已存在的结构性 S/R 分支并回测（`ENABLE_ATR_SLTP=False`）；或做"**结构优先 + ATR 保底**"混合——SL 放 S/R 外侧加缓冲、TP 锚 S/R，ATR 只做上下限/低波动过滤。
5. **用 ML 定 SL/TP 而非固定倍数**：让模型输出波动/方向，动态给 SL/TP 距离（需先解决下面 P2 的校准与验证）。
6. **出场显式状态机**：把硬 SL/硬 TP/MA 反向/弱强度/分批的**优先级与冲突处理写成规范**；减仓后是否上移 SL（保本/追踪）要明确落地。

**P2（ML/概率治理）**
7. **ML 准入**：walk-forward 验证 + 概率校准（ECE）+ 扣成本后期望收益；先把 `ML_MIN_HOLDOUT_F1` 提到有意义的门槛，再逐 pair 开启。
8. **MC 概率**：做校准 + 时间尺度对齐（日线快照 vs H1/H4 入场）。

**P3（你点名的"一锅烩"→ 逐 pair 差异化）**
9. 引入 **per-pair 参数表**：SL/TP 倍数、最小 SL 距离、点差过滤、最大持仓时间。
10. 先做 **pair 级绩效审计**（净 R、胜率、最大回撤、平均持仓时间、SL/TP 触发分布），用数据决定差异，而非拍脑袋。
11. 高波动/易跳空的 **GBP_JPY、AUD_JPY** 与 **USD_JPY、EUR_JPY** 分开处理（当前 4 对共用 `config.py:33-38` + 同一套 ATR 倍数）。

---

## 四、结论

- 今天的改动方向正确：**补上了"主动出场"这一环**，且弱强度离场与入场对称、实现稳健（纯函数、fail-closed、幂等）。
- 但**仓位风险仍未金额化**，且**出场规则之间冲突未定义优先级**（硬 TP 与分批、MC TP 与分批），这是当前最需要先解决的。
- 你现在想问的两件事都已具备落地条件：
  - **SL/TP 用支撑阻力**：代码已存在，切开关即可验证；
  - **减仓**：SCALE-OUT 已在跑（注意它是 partial reduce，weak-exit 是整仓平）。
- "一锅烩"确认属实：入场门槛、ATR 倍数、2R TP 对 4 个 JPY 对完全相同。
