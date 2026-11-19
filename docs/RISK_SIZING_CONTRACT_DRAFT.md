# Risk-Based Position Sizing Contract — v3（DRAFT v0.2）

状态：**DRAFT · NOT APPROVED · NO CODE AUTHORIZATION**
日期：2026-11-19
范围：`scheduled_runner_v3.py`（= v31 血统）的开仓手数决定链路。
配套：`docs/EXIT_ARBITRATION_CONTRACT_DRAFT.md`（Doc B，出场仲裁）、`docs/VERIFIED_ACCOUNT_FACTS_2026-11-19.md`（已核实账户事实备忘）。
上级参考：`docs/Development_plan.md`（本契约评审通过后由其收纳，**不自动生效**）。
产出目的：把"固定手数 + 两层缩放"升级为"**按账户风险金额定仓**"。**先立契约，再动代码。**

---

## 修订摘要（v0.1 → v0.2）

本版为**纯文档修订**，**未修改任何 Python 文件、配置或运行行为**。变更清单：

| # | 变更 | 落点 |
| -- | ---- | ---- |
| 1 | 修正净值事实：**现成 `get_NAV()` 不可复用**（跑在废弃 client 上）；真字段是 `AccountSummary.NAV`（大写）；两处小写 `nav` 读取失效（登记缺陷） | §1 |
| 2 | 补入已核实账户事实：币种 per-account（v3=USD）、units 整数、最小单位 1 | §1 / §4 |
| 3 | 风险预算折减由"相乘"改为 **`max(0.25, min(D_ext, D_MC))`**（A-03 / A-04） | §3 |
| 4 | `Equity` 数据契约明确为 `AccountSummary.NAV`（**与 currency 同账户**）；无效 → REJECT | §3 |
| 5 | 货币换算改为**运行时账户币种**驱动；禁止硬编码、禁止 1.0 默认回退 | §4 |
| 6 | Fail 状态机：REJECT 扩至 NAV/汇率/SL/**交易规格**读取失败；`FAILCLOSED_SKIP=True`（A-05） | §5 |
| 7 | §6 由"开放问题"改为 **Owner Decisions（A-01~A-06）** + 待核实清单 | §6 |
| 8 | 新增**账户级组合风险上限**定义（金额换算求和，不做相关性抵扣） | §7 |
| 9 | 验收指标补 **NAV 与实际账户交叉核对**、币种同账户校验、基线审计 | §8 |
| 10 | 影子并行明确为**切换 LIVE 的必要前置**（A-06） | §9 |
| 11 | **联合审核收口**：`Equity` 与组合风险消费**本周期唯一快照**；入场补「尺寸快照 ≠ 执行授权」；入场态命名 `ENTRY_REJECT` | §2 / §3 / §5 / §7 |
| 12 | **Codex 评审收口**：区分 **① 周期决策快照** 与 **② 执行前定向复核**（消除与"下单前重验"的表述冲突）；units **向下取整**并复验；验收改**逐笔对照**（删 `std/mean<5%`）；`D_ext` 失败归类为 DEGRADED | §2 / §3 / §4 / §5 / §8 |
| 13 | **Codex 二次收口**：§8 测试集口径由"多货币对风险**一致性**"改为"多货币对**公式与换算正确性**"（与逐笔对照口径统一，避免被读作不同交易风险金额必须相同） | §8 |
| 14 | **三次评审收口**：§3 执行前定向复核字段补 **开放交易集合 + 各自 SL**；新增**同周期多笔入场串行 / 预留本地周期预算**规则；§7 组合风险校验同步对齐两级读取 | §3 / §7 |

未决技术问题**未被删除**，统一迁入 §6.2（见 §11.3）。

---

## 0. 一句话

今天的定仓调整的是**交易数量**，不是**风险金额**。同样 2000 手，EURJPY(SL 40pip) 与 GBPJPY(SL 80pip) 的真实亏损差一倍。本契约把两者统一到"**每笔风险 = 账户净值的固定百分比**"。

---

## 1. 现状（代码事实）

| 事实 | 证据 |
|---|---|
| 手数 = 固定值 × 动态仓位 × MC 概率 | `scheduled_runner_v3.py:4601`（`_order_units=_EFFECTIVE_LOTS`）→ `:4603`（×dlot）→ `:4634/:4646`（×prob） |
| 固定值不含净值、不含 SL 距离 | `_resolve_effective_lots()` `scheduled_runner_v3.py:6285`：仅 `--lots` / `LIVE_LOT_SIZE`/`DEMO_LOT_SIZE` / config |
| **runner 无账户净值来源** | 全 runner grep 无 `equity/NAV/AccountSummary` |
| **净值现成实现不可复用（修正）** | `utils/oanda.py:46 get_NAV()` 读的是正确的 `.NAV`，但运行在**另一套废弃 client** 上，v3 的 TradingCore/v20 路径**未接入** |
| **真字段为 `NAV`（大写）** | `AccountSummary` 返回键 `NAV`/`trueNAV`；`nav`（小写）**不存在**（本次实测） |
| **两处小写读取失效（缺陷）** | `config_oanda.py:264` 与 `validate_profile_wiring.py:96` 均 `resp.get('nav','?')` → **恒为 `?`** |
| 账户币种 **per-account**（已核实） | profile2（v3）= **USD**；profile1/4 = SGD；见 `VERIFIED_ACCOUNT_FACTS_2026-11-19.md` §1 |
| units 精度 / 最小单位（已核实） | `tradeUnitsPrecision=0`（整数）、`minimumTradeSize=1`；见同备忘 §2 |
| 入场价/SL 在信号期可得 | 信号含 `sig["entry"]`、`sig["stop_loss"]`（`scheduled_runner_v3.py:4654`） |
| 报价货币/点值可得 | `PIP_SIZE_BY_QUOTE`；`_weak_exit.split_pair()` 拆分 base/quote |

**结论**：定仓与"风险"完全脱钩；两层缩放只是把同一个固定手数缩小。**结构性缺口确认。** 并且**净值的现成来源不可用**——这是本契约的 P0 前置依赖（§3 / §6.2）。

---

## 2. 目标定仓顺序（不可颠倒）

```
① Signal        方向 + 入场资格（现有入场引擎，不变）
② Final SL      最终有效止损价（含 MC 加宽 / 结构 S/R 的最终值）
③ RiskBudget    账户风险预算（本契约定义）
④ Units         用最终 SL 距离反推手数
⑤ Validate      手数边界 / 账户风险上限 / 订单约束
```

> 关键一：**必须先确定最终 SL，再算手数**；不允许"先定手数、事后接受不同风险"。
> 关键二（新增）：**尺寸快照 ≠ 执行授权**（与 Doc B §3.3 对称）。下单前须做**执行前定向复核**（§3 两层读取之 ②）——快照到下单之间账户状态可能已变。

---

## 3. 风险预算公式

```
D_final          = max(0.25, min(D_ext, D_MC))        # 仅 PASS / DEGRADED；REJECT 不适用地板
RiskBudget_final = Equity × Risk% × D_final
Units            = RiskBudget_final / ( |entry − SL| × fx(quote → account) )
```

- **`Equity` 数据契约（本版新增）**：来源为 `AccountSummary` 的 **`NAV`（大写）**，经由 **v3 当前使用的 OANDA client（TradingCore/v20）** 读取。
  - **必须与 `currency` 取自同一个实际账户**——禁止"账户 A 的 NAV + 账户 B 的 currency"（形式成功、实际全错）。
  - **读取失败 / 字段缺失 / 非正数 / 数据无效 → REJECT**（§5）。
  - 取自**① 周期决策快照**（Doc B §2①）；**决策**链路不得散落多次读取（**② 执行前定向复核**见下，不属于"散落"）。
  - **不得**将现有 `get_NAV()` 视为可复用能力（§1）。
- **两层读取（必须区分；避免"唯一快照"被误读为禁止实时校验）**：
  - **① 周期决策快照**：每周期一次、唯一、只读；供 Doc A 定仓与 Doc B 出场的**决策**使用（含 NAV / currency / 开放交易 SL）。"不得散落多次读取"仅指**决策**层。
  - **② 执行前定向复核**：下单/平仓**执行前**允许一次**定向** broker 读取，仅限授权所需字段：trade 存在性 / 权属（Tag）/ 实时 units / **NAV** / **开放交易集合（trade_id 列表）及每笔当前 SL**。失败或权属变化 → **REJECT（拒绝执行）**。
  - **复核结果刷新组合风险**：执行前复核得到的 **NAV、开放交易集合及各自 SL** **必须**用于**最终**组合风险上限校验（§7），不得沿用已过期的决策快照值。
  - **同周期多笔入场（必须串行 / 预留本地周期预算）**：一个周期内若有多笔入场，**必须串行处理**（按顺序逐笔定仓）；每笔**被接受后其 `RiskAmount` 立即计入本地周期预算**——**即使 broker 端可见性滞后**（尚未反映为新成交）。后续每笔的组合风险校验 = ① 复核到的**既有持仓**风险 + ② **本周期已接受但 broker 尚不可见**的入场风险 + ③ 本笔拟风险。**禁止**仅凭 broker 可见持仓校验而漏掉同周期已接受订单，以免合计突破账户级上限（§7）。
- `Risk%`：单笔风险占比（**Owner 决策 = 0.5%**，见 §6.1 A-01）。
- `D_ext`：动态仓位折减因子（原 `_compute_extension_lot_scale` 的 scale ∈ {1.0, 0.5, 0.25}）。
- `D_MC`：MC 概率折减因子（原 `prob_zone_sizing` 的 size_scale ∈ [0.25, 1.0]）。
- `fx(quote → account)`：报价货币 → 账户货币汇率（见 §4）。

**核心改造**：`D_ext`、`D_MC` 从"两个独立手数缩放器"改为"**风险预算的折减因子**"，且两者**取较严格者（min），不相乘**（A-03）——避免 0.25×0.25=0.0625 的叠加折减与同因重复惩罚。两者仍**分别保留**各自的计算、日志与解释。

---

## 4. 货币转换与精度

- **账户计价货币**：**运行时读取** `AccountSummary.currency`，**禁止硬编码**（同 org 下 SGD/USD 混用）；并与 `NAV` **同账户**（§3）。
  - 已核实：v3（profile 2）= **USD**。
- **换算路径**（账户=USD，交易池 = JPY 报价 + USD 报价）：

  | 报价货币 | `fx(quote→account)` |
  |---|---|
  | USD | `1.0` |
  | JPY | `1 / USD_JPY(mid)` |
  | 其它 | 需显式映射；**无法确定路径 → REJECT** |

  - 转换率获取：复用 `trading_core.get_bid_ask()`。
  - **取不到汇率 → REJECT**（§5），**不得回退到 1.0 默认值**。
- **units 精度**：整数（`tradeUnitsPrecision=0`）；**最小交易单位 = 1**；结果须为 ≥1 的整数，**且必须向下取整（floor）**——取整**后**再复验：单笔预算不超、`≥1`、`≤ max_units`、账户组合上限（§7）。
- **交易规格读取**：min-units / 精度应取自 `AccountInstruments`。
  - **规格缺失或读取失败 → 拒新单（REJECT）**；**禁止静默使用陈旧默认值**。
  - 已知两个代码点（详见 `VERIFIED_ACCOUNT_FACTS_2026-11-19.md` §2）：v3 路径 `trading_core_v2.get_instrument_spec_raw()` **缺字段**（活跃）；`utils/oanda_execution.py:94` fallback `1000` **陈旧默认**（休眠，不在 v3 执行链）。
- **边界**：`max_units`、保证金占用、**账户级组合风险上限**（§7）。

---

## 5. Fail 状态机（全链统一）

| 状态 | 含义 | 允许行为 |
|---|---|---|
| **PASS** | 净值/汇率/SL/概率/规格均有效 | 正常执行 |
| **DEGRADED** | 部分数据退化但有定义（如 MC 缺，用 `D_MC` 地板 0.25） | 按批准的折减执行 |
| **REJECT** | 无法可靠确定风险 | **不开仓** |

**REJECT 触发条件（本版扩充）**：`NAV` 读取失败/缺失/非正/无效；汇率取不到；最终 SL 无效；**交易规格读取失败**。

> **命名（避免与 Doc B 混淆）**：本契约的 `REJECT` 是**入场态**，日志/对账记为 **`ENTRY_REJECT`**；Doc B 的 `REJECTED` 是**出场态**，记为 **`EXIT_REJECTED`**。二者**同词异义**，不可混读。

> **弱 vs 不可算（必须区分）**：**信号/概率弱**（MC 低、`D_ext` 小）风险金额**仍可算** → **DEGRADED**（`D_final` 地板 0.25，至少保留 25% 基准预算）。**`D_ext` 计算失败（异常）** → 现状为 fail-open（回退 1.0），与本契约 fail-closed 姿态冲突；本契约口径：**按 DEGRADED 处理**（该层不折减，视为 1.0），仍受 `D_MC` 与地板约束，**不得回满仓**；若 `D_MC` 亦无法确定 → **REJECT**。

**Owner 裁定（A-05）**：`FAILCLOSED_SKIP = True` —— 状态不确定时**不开仓（fail-closed）**，**不得**回退到原始固定手数。
> 概念区分（必须）：**DEGRADED** 仍有可信计算结果（如 MC 缺失但风险金额可算）；**REJECT** 风险金额无法可靠确定。两者不可混淆。
> **地板 0.25 只适用于 PASS / DEGRADED**；**REJECT 不得靠地板机制恢复为可交易**。

**明令禁止**：
- 计算异常后回满仓；
- 数据缺失默认 `scale=1.0`；
- 风险预算无法确认却仍提交订单；
- 为凑最小手数而突破风险预算（A-02）；
- 交易规格读不到时静默使用陈旧默认（§4）。

> 现状对照：`prob_zone_sizing` 内部为 **DEGRADED→0.25**（非 REJECT）；外层 `except`（`:4647`）为 **fail-open**（保留原 units）——与本节冲突，待统一。

---

## 6. Owner Decisions 与待核实问题

### 6.1 Owner Decisions（状态：`OWNER DECIDED — INCORPORATED IN DOCUMENT; CONTRACT APPROVAL PENDING`）

| 编号 | 裁定摘要 | 落点 |
| ---- | -------- | ---- |
| A-01 | `Risk%` **初始基准 = 0.5%**；为工程初始值、非最优证明；**不得自行提高**；调整须经 Owner | §3 |
| A-02 | 反推 units < 最小单位时 **拒单**（`REJECT_MIN_UNITS_EXCEED_RISK_BUDGET`）；禁止提至最小手数/恢复固定手数/绕过折减 | §5 / §7 |
| A-03 | `D_ext` 与 `D_MC` **取 min，不相乘** | §3 |
| A-04 | 统一折减**地板 = 0.25**（`max(0.25, min(...))`），仅适用 PASS/DEGRADED；REJECT 不复活 | §3 / §5 |
| A-05 | `FAILCLOSED_SKIP = True`（不确定即不开仓；不回退固定手数） | §5 |
| A-06 | **影子并行是切换 LIVE 的必要前置**（非按小时数批准） | §9 |

### 6.2 仍待技术核实的问题（新增，未解决，不删）

| # | 问题 | 说明 |
| - | ---- | ---- |
| Q1 | **风险基线未测** | 当前每笔实际有效风险（SL 距离 / units / NAV / 换算后 SL 风险金额 / 单笔风险÷NAV / 开放交易风险总和）**未测**；0.5% 是拟定目标，不代表现状已更低 |
| Q2 | **NAV 接入未完成** | `AccountSummary.NAV` 与 v3 client 的接入 + 验证，是 `fixed_risk_pct` 切 LIVE 的 **P0 前置**（§1/§3） |
| Q3 | `maximumOrderUnits` 未取 | 与 `max_units` 边界相关 |
| Q4 | live 账户币种未验 | 本次仅验 practice 4 账户 |
| Q5 | 交易规格读取源未统一 | `trading_core_v2` 缺字段 vs `oanda_execution` 陈旧默认（§4） |

> 原 v0.1 §6 的六项开放问题**未删除**，其裁定已落于 §6.1；技术性延伸并入本表。

---

## 7. 账户级组合风险上限

单笔 0.5% 不构成充分约束，必须有**账户级**上限：

- 初值（待验证）：**单账户同时风险 ≤ 3 × Risk%**。
- **计算方式（必须定义，不可只数名义手数）**：
  ```
  PortfolioRisk = Σ_i RiskAmount_i        # 统一换算到账户计价货币后求和
  ```
  - 每笔 `RiskAmount_i` 按账户币种换算（§4），**不因相关性给予抵扣**（第一版保守求和）。
  - 须计入：**已有开放交易的剩余 SL 风险**、**新交易拟增加的风险**、**SL 已移动后的实际剩余风险**、**无法确认 SL 的交易**（按保守估计或拒新单）。
  - **决策阶段**：开放交易的剩余 SL 风险取自**本周期唯一快照**（Doc B §2①）。
  - **执行前的最终组合校验**：用 **② 执行前定向复核**刷新的 **NAV、开放交易集合及各自 SL**（§3）重算，并**叠加同周期已接受但 broker 尚不可见**的入场风险（§3，防多笔合计突破上限）；**不得**沿用已过期的决策快照值。
  - **禁止**因新交易单独满足 0.5% 就忽略已有仓位风险。
- **账户计价货币未确认前，不得以默认 USD 计算**（§4）。

---

## 8. 日志与验收指标（带闭环）

每笔开仓记录：`pair, action, entry, final_sl, sl_distance, fx_rate, account_ccy, equity, risk_pct, D_ext, D_MC, D_final, risk_amount, units, fail_state`。

**闭环判定**（**逐笔对照**，不再要求各 pair 风险金额彼此相等——`D_final` 本就使各笔预算不同）：

- 指标：每笔 `actual_risk_amount` 与其**自身**目标 `Equity × Risk% × D_final` 的**相对偏差** = `|actual − target| / target`。
- 达标：**每笔**相对偏差 **< 5%**；且**账户组合风险上限（§7）始终遵守**；且**无超预算订单**（`units` 向下取整后仍不超单笔预算）。
- 不达标：定位该笔的汇率 / SL / 取整误差，修正后重测。
- 任一 `fail_state=REJECT` 的遗漏（本应拒单却下单）→ 视为 **P0 缺陷**，立即回滚。

**新增验收条款**：

1. **NAV 交叉核对**：每周期记录的 `equity` 与 `AccountSummary.NAV`（同账户）一致。
2. **同账户校验**：`equity` 与 `account_ccy` 必须来自同一账户 ID（防错配）。
3. **基线审计（Q1）**：切换前完成一次只读基线（当前实际风险 vs 0.5% 目标），量化"新旧风险变化"。
4. **A-06 前置测试集**：计算正确性、汇率转换、最小手数边界、REJECT 异常注入、**多货币对公式与换算正确性**（验证同一公式在不同 quote 货币下换算正确，**非**要求不同交易风险金额相同）、真实运行数据与 OANDA 账户核对。

---

## 9. 实施约束

- **零行为变更优先**：新逻辑先以 `MODE: shadow` 跑——旧 `fixed_lot` 继续真实下单，新 `fixed_risk_pct` **只计算不改变订单**。
  - 两套结果必须使用**同一入场信号、同一最终 SL、同一账户快照**；记录新旧 units、风险金额及差异原因。
- **影子并行是切 LIVE 的必要前置（A-06）**：**不得**仅凭运行时长达到某小时数即批准切换；须通过 §8 的测试集与验收条款。
- 开关化：`POSITION_SIZING = {"MODE": "fixed_lot" | "fixed_risk_pct" | "shadow", "RISK_PER_TRADE_PCT": ...}`，默认 `fixed_lot`（不改变现状）。
- **不删**现有的 `D_ext`/`D_MC` 逻辑，改造其"作用点"（预算折减，而非手数缩放）。
- 与 `SHADOW` 改名解耦，**不同 commit**。

---

## 10. 与外部评审的取舍

- **采纳**：ChatGPT 的定仓顺序、"折减因子改造"、"最小手数拒单"、三态 Fail、验收须记初始风险而非仅净 R；**NAV 为 P0 前置**与"同账户 NAV+currency"要求。
- **修正**：`minimumTradeSize=1000` 定级为 **sibling 模块休眠缺陷**（v3 执行链外）；v3 侧的真实问题是 `trading_core_v2` **缺字段**（活跃）。两者分开登记（§4）。
- **弃用**：Dola 的 `risk_to_units()` 直接照抄（`min_units=100` 与 live `MIN_LOT=1` 冲突、`quote_to_account_rate` 默认 1.0 会静默错误、无 REJECT 分支）——思想可用，实现须按其真实账户/精度重写。

---

## 11. 交付附录

### 11.1 修订摘要

见文首「修订摘要（v0.1 → v0.2）」。

### 11.2 Owner Decision 对照表

| 编号 | Owner 裁定 | 落实章节 | 状态 |
| ---- | ---------- | -------- | ---- |
| A-01 | Risk% = 0.5% | §3 | `OWNER DECIDED — INCORPORATED IN DOCUMENT; CONTRACT APPROVAL PENDING` |
| A-02 | 最小手数冲突拒单 | §5 / §7 | 同上 |
| A-03 | 两层折减取 min | §3 | 同上 |
| A-04 | 地板 0.25 | §3 / §5 | 同上 |
| A-05 | `FAILCLOSED_SKIP = True` | §5 | 同上 |
| A-06 | 影子并行是 LIVE 前置 | §9 | 同上 |

### 11.3 尚未解决的技术问题

见 §6.2（Q1–Q5）。原 v0.1 §6 的问题**均未被删除**。

### 11.4 声明

- 本版为**纯文档修订**：**未修改任何 Python 文件、配置或运行行为**。
- **未修改** `docs/Development_plan.md`。
- 文件状态**继续保持**：**DRAFT · NOT APPROVED · NO CODE AUTHORIZATION**。
- 未经验证的能力**未**被写成"已确认的事实"；未决问题**未**被删除。
- **在 NAV 接入与验证完成之前，`fixed_risk_pct` 不得切换 LIVE。**
