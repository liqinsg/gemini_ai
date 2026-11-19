# Development Plan — v3 定仓 + 出场仲裁（收纳母文档）

状态：**PLAN · NOT APPROVED · NO CODE AUTHORIZATION**
日期：2026-11-19
性质：本文件是两份 **DRAFT** 契约的**收纳母文档**与实施阶段索引。它 **不替代** 契约正文，**不授权任何代码修改**；契约正文以各自文件为准。
纪律（沿用 `AGENTS.md`）：源码即真相；分析/审计/设计类任务**不改码**；严格区分 **implemented / intended / observed / proposed**。

---

## 0. 一句话

v3 有两条**结构性缺口**：① 定仓与"风险"脱钩（决定交易数量，不决定风险金额）；② 出场是一堆互不感知的函数，**无统一仲裁**。本 Plan 把两条缺口的契约（Doc A / Doc B）收纳为一份可执行阶段索引，**先立契约，再（经批准）动代码**。

---

## 1. 文档地图（层级与真相来源）

| 层级 | 文件 | 角色 | 状态 |
|---|---|---|---|
| 事实 | [VERIFIED_ACCOUNT_FACTS_2026-11-19.md](file:///home/ubuntu/projects/gemini_ai/docs/VERIFIED_ACCOUNT_FACTS_2026-11-19.md) | 只读取证：账户币种 / units 精度 / NAV 字段 / 既存缺陷 | FACTS（有**时间边界**，实施前须重取） |
| 审计 | [V3_CODE_AUDIT_2026-11-19.md](file:///home/ubuntu/projects/gemini_ai/docs/V3_CODE_AUDIT_2026-11-19.md) | 静态代码审核（对应 commit `24c1338`） | 报告（weak-exit 结论已更正为"已修复"） |
| 契约 A | [RISK_SIZING_CONTRACT_DRAFT.md](file:///home/ubuntu/projects/gemini_ai/docs/RISK_SIZING_CONTRACT_DRAFT.md) | 入场 **定仓**（风险金额化） | **DRAFT v0.2 · NOT APPROVED** |
| 契约 B | [EXIT_ARBITRATION_CONTRACT_DRAFT.md](file:///home/ubuntu/projects/gemini_ai/docs/EXIT_ARBITRATION_CONTRACT_DRAFT.md) | 出场 **仲裁**（统一优先级/幂等/原因） | **DRAFT v0.2 · NOT APPROVED** |
| 母文档 | **本文件** | 收纳 + 阶段 + Owner 决策 + OPEN 清单 | **PLAN** |

> 历史设计参考 `docs/development_design_rfc_v2.0.md` **不在本 Plan 的授权链内**，未并入。

---

## 2. 现状速览（来自审计 + FACTS，均经源码核查）

- **定仓**：`手数 = 固定值 × 动态仓位 × MC 概率`；固定值不含 NAV、不含 SL 距离（`scheduled_runner_v3.py:4601`）。定仓与风险完全脱钩。
- **净值**：runner **无 NAV 来源**；真字段是 `AccountSummary.NAV`（大写）；`utils/oanda.py get_NAV()` 读对但跑在**废弃 client**，v3 未接入；`config_oanda.py:264` / `validate_profile_wiring.py:96` 按小写 `nav` 读取 → 恒为 `?`（缺陷）。
- **出场**：L0–L4 五条路径**各自决策/执行/记录**，无统一优先级、无统一幂等、无统一原因字段；全平(3)在部分平(4)之前属**顺序巧合**，非仲裁保证。
- **权属**：L0/L2/L3 平仓时**再过门禁**；**weak-exit 已修复**（改走 `_gated_close_trade_by_id`，`scheduled_runner_v3.py:4884`）。**唯一缺口 = L4 scale-out**：仅**快照期**过滤 `is_bot_owned_trade`，**平仓时无二次实时校验**（TOCTOU）→ 契约 B §3.3 修法。
- **LIVE 语义（澄清）**：`--dry-run` 默认关闭 → runner **会尝试真实下单**；但**是否连 live 账户**仅由 `--live` 决定（未传则 `OANDA_ENV=practice`，`scheduled_runner_v3.py:281-284`）。二者**不可混为一谈**。
- **持久化**：`risk_state.json` 现为**单写者**（仅 `risk_engine_v22` 写），**当前无竞态**；但对齐仲裁后将成为**多写者** → 原子写/并发保护/迁移是**前置条件**（前向要求，非现存 bug）。

---

## 3. 契约摘要

### 3.1 Doc A — 入场定仓（风险金额化）

**目标顺序（不可颠倒）**：`Signal → Final SL → RiskBudget → Units → Validate`。

```
D_final          = max(0.25, min(D_ext, D_MC))     # 两层折减取 min，不相乘；地板仅 PASS/DEGRADED
RiskBudget_final = Equity × Risk% × D_final
Units            = RiskBudget_final / ( |entry − SL| × fx(quote → account) )   # 向下取整(floor)
```

要点：
- `Equity` = `AccountSummary.NAV`（大写），经 **v3 当前 client** 读取；**与 `currency` 同账户**；失败/缺失/非正/无效 → **REJECT**。
- **fail 三态**：**PASS** / **DEGRADED**（有可信结果，按折减执行）/ **REJECT**（风险金额无法确定，不开仓）。**弱 ≠ 不可算**：信号/概率弱 → DEGRADED；`D_ext` 计算失败 → 按 DEGRADED 处理（该层视为 1.0，不得回满仓）；`D_MC` 亦无法确定 → REJECT。
- **`FAILCLOSED_SKIP = True`**（A-05）：不确定即不开仓，**不得回退固定手数**。
- **账户级组合风险上限**：`PortfolioRisk = Σ RiskAmount_i`（统一换算后求和，第一版不做相关性抵扣）；初值 ≤ 3×Risk%。
- **命名**：入场态记 **`ENTRY_REJECT`**（与 Doc B 的 `EXIT_REJECTED` 同词异义）。

### 3.2 Doc B — 出场仲裁

**统一执行链**：`① 快照 → ② 提议 → ③ 仲裁 → ④ Live Validation → ⑤ 执行(唯一出口) → ⑥ 对账`。

**三边界（基础原则）**：`Decision ≠ Request ≠ Confirmation ≠ Cleanup`。

**优先级 L0–L4**：`L0 Emergency > L1 Broker SL/TP（外部事实/只发现不发单）> L2 Risk-momentum > L3 Weak-exit > L4 Scale-out`。

硬规则：
- **全平优先（B-02）**：同一 trade 本周期若有任一 L0–L3 全平决策 → **L4 不得进入执行队列**；**不依赖函数执行顺序**。
- **L2 优先 L3（B-01）**：被压制记 `SUPPRESSED_BY_HIGHER_PRIORITY`；**禁止失败降级**。
- **唯一出口（B-03）**：全平与部分平共用同一执行出口，**每次实际平仓都过实时校验**（L4 补二次权属门禁）。
- **状态机**：L0–L3 用统一 **Pending Exit**（NONE→PENDING→CONFIRMED_CLOSED / REJECTED / UNKNOWN / STALE）；**L4 用独立五态**（NOT_FIRED / REQUESTED / CONFIRMED / REJECTED / UNKNOWN），**REQUESTED ≠ CONFIRMED**。
- **稳定 req_id（B-05）**：L3 由 `WEAKEXIT_{tid}_{stamp}` 改 **`EXIT_WEAK_{trade_id}`**；**不宣称**服务端 exactly-once。
- **确认后清理（B-06）**：交易关闭**确认之后**才清残留；UNKNOWN 保留状态、先对账。

---

## 4. 接口闭环（跨文档硬标准）

两条主线在**同一周期快照**上闭合，硬标准 = **执行出口唯一**。

**两层读取（必须区分；"唯一快照" ≠ 禁止实时校验）**：

| 层 | 名称 | 性质 | 用途 |
|---|---|---|---|
| ① | **周期决策快照** | 每周期一次、唯一、只读 | 供 Doc A 定仓与 Doc B 出场的**决策**；含 NAV / currency / 开放交易 SL；入场与出场**共享** |
| ② | **执行前定向复核**（= Doc B ④ Live Validation） | 下单/平仓**执行前**允许一次**定向** broker 读 | 仅限授权字段：trade 存在性 / 权属(Tag) / 实时 units / NAV；**失败或权属变化 → REJECT**；复核结果**必须**刷新最终组合风险上限校验 |

> 命名对齐：Doc A §3 的"两层读取" == Doc B §2 的 `① 快照 / ④ Live Validation`。**二者不同层**，实现者**不得**把"唯一快照"误读为"禁止执行前校验"。

---

## 5. Owner Decisions 汇总

| 编号 | 裁定 | 落点 | 状态 |
|---|---|---|---|
| **A-01** | `Risk%` 初始基准 = **0.5%**（工程初始值，非最优；不得自行提高） | Doc A §3 | OWNER DECIDED — PENDING DOCUMENT INCORPORATION |
| **A-02** | 反推 units < 最小单位 → **拒单**（`REJECT_MIN_UNITS_EXCEED_RISK_BUDGET`）；不得提至最小手数 | Doc A §5/§7 | 同上 |
| **A-03** | `D_ext` 与 `D_MC` **取 min，不相乘** | Doc A §3 | 同上 |
| **A-04** | 折减**地板 = 0.25**，仅 PASS/DEGRADED；REJECT 不复活 | Doc A §3/§5 | 同上 |
| **A-05** | `FAILCLOSED_SKIP = True` | Doc A §5 | 同上 |
| **A-06** | **影子并行是切 LIVE 的必要前置**（非按小时数批准） | Doc A §9 | 同上 |
| **B-01** | L2 优先 L3；被压制记 `SUPPRESSED_BY_HIGHER_PRIORITY`；禁止失败降级 | Doc B §3.2 | 同上 |
| **B-02** | 全平优先**硬规则**；仲裁阶段禁止低优先级入队 | Doc B §3.2/§8 | 同上 |
| **B-03** | L4 必须补执行前实时权属门禁；统一出口统一校验 | Doc B §2/§3.3 | 同上 |
| **B-04** | L4 独立五态部分平状态机；UNKNOWN 先对账、禁止盲目重发 | Doc B §4.2 | 同上 |
| **B-05** | L3 改用稳定 req_id；区分决策/请求/确认身份；不宣称 exactly-once | Doc B §4.1 | 同上 |
| **B-06** | 确认关闭**之后**清理残留；UNKNOWN 保留 | Doc B §4.2/§6 | 同上 |

---

## 6. 实施阶段

模式开关：Doc A `POSITION_SIZING={MODE: fixed_lot|shadow|fixed_risk_pct}`（默认 `fixed_lot`）；Doc B `MODE: legacy|shadow|arbitrated`（默认 `legacy`）。两线**解耦、不同 commit**。

| 阶段 | 内容 | 独立验收条件 |
|---|---|---|
| **Stage A** | 纯文档契约审议（Doc A + Doc B + 本 Plan） | 两份契约相互闭合、接口无冲突 |
| **Stage B** | **只读代码接口核实**（Doc B §4.3 V1–V7、§9.2 Q1–Q6；Doc A §6.2 Q1–Q5） | 每项给出"已核实/未核实"结论，附代码行号 |
| **Stage C** | 影子实现与测试（`MODE=shadow`，只写日志不改单） | Doc B §10 全部 11 条 + 模拟候选决策测试；Doc A §8 逐笔对照通过 |
| **Stage D** | 影子运行验收（并行对照，不改真实下单） | 连续 ≥20 周期指标达标、新旧差异可解释 |
| **Stage E** | 切换正式模式（`arbitrated` / `fixed_risk_pct`） | **Owner 单独批准**后才可执行 |

约束：**零行为变更优先**；**不删**现有路径/逻辑（只改"作用点"与"执行权归属"）；与 `SHADOW` 改名解耦。

### 6.1 Stage B — 只读接口核实结果（2026-11-19）

方法：静态阅读源码（未运行、未改码）。证据行号以当前工作区为准（`scheduled_runner_v3.py` / `risk_engine_v22.py` / `utils/scale_out.py` / `utils/trading_core_v2.py`）。**结论分三档：已核实 / 部分核实 / 未核实。**

**总判定：Stage B 未全部关闭 —— 退出仲裁侧 V1/V2/V5/V7 与 Q1/Q4/Q5 仍未满足或未核实；定仓侧 Q1/Q3/Q4 未核实。故 Stage C 前置条件不满足。**

**（1）退出仲裁 §4.3 V1–V7**

| 项 | 结论 | 证据 | 处理结论 |
|---|---|---|---|
| V1 支持 L0/L2/L3 统一决策身份 | **未满足** | `PendingExitState` 无 layer/决策身份字段（`risk_engine_v22.py:51-58`）；`reconcile_and_exit` 硬编码仅受理 `EXIT_READY`（`:313`） | 需扩展状态 schema + 决策枚举 |
| V2 支持稳定 `req_id` | **未满足** | `req_id = f"exit_{trade_id}_{uuid.uuid4().hex[:8]}"`，**每次随机且不持久化**（`:436`） | 需改稳定 id 并持久化 |
| V3 REJECTED/UNKNOWN/STALE 可恢复语义 | **部分** | `ReconcileStatus` 有 `EXECUTION_REJECTED/STATE_UNKNOWN/PARTIAL_FILL`（`:34-40`）；runner 对非关闭态**保留 pending 下周期重试**（`:442-452`）；但 `PendingExitState` **无 status 字段**；4h 超时→`TIMEOUT_FORCED_EXIT`（`:210-211`），**非**"STALE 告警"语义 | 需新增 status 字段；STALE 语义需定义 |
| V4 按 `trade_id` 精确清理 | **可（已具备）** | 单笔 `del self.pending_exits[trade_id]`（`:444`）；`prune_pending_exits` 按 open 集合差批量删（`:371-374`） | 可复用 |
| V5 扩展 schema 零回归 / 兼容旧文件 | **未核实（有风险）** | `load_pending_exits` 以 `PendingExitState(**d)` **严格构造、无版本字段**（`:149`）；新增必填字段将使旧 `risk_state.json` 加载失败 | 需实测 + 加 schema version |
| V6 原子写 / 并发保护 | **原子写=已具备；并发锁=缺** | `save_pending_exits` 用 `tmp + os.replace`（`:138-141`）→ **原子**（**更正 FACTS §7 的"待补原子写"表述**）；文件内**无 flock/fcntl** | 单写者现状安全；多写者前须补并发写保护 |
| V7 旧 schema 迁移失败处理 | **未满足** | `load_pending_exits` 异常 → `print("Recovery failed, start fresh")` + `return {}`（`:150-152`）→ **静默丢弃全部 pending** | 需保留 + 可回滚，禁止静默丢弃 |

**（2）退出仲裁 §9.2 Q1–Q6**

| 项 | 结论 | 证据 | 处理结论 |
|---|---|---|---|
| Q1 OANDA `client_request_id` 实际幂等保证 | **未核实**（需 OANDA 文档/实测） | 代码侧三种 id：L2=`exit_{tid}_{uuid4}`（随机，`:436`）、L3=`WEAKEXIT_{tid}_{stamp}`（带时间戳，`:4886`）、L4=`SCALEOUT-{tid}-R{rung}`（**稳定**，`:4782`） | 需外部/实测核实 |
| Q2 `risk_engine_v22` pending 接口兼容性 | **未核实** | 同 V1/V5 | 需实测 |
| Q3 L4 partial 状态持久化 | **部分核实** | 存 `logs/scale_out_state.json`；`save_state` **原子**（tmp+replace，`scale_out.py:287-292`）；仅存 `fired` + `original_units`，**无五态 status**；损坏 → `load_state` 返回 `{}`（`:277-279`），fired 丢失，靠"remaining vs target"自愈（`:14`） | 需补五态 + 恢复语义 |
| Q4 Broker SL/TP 与人工/其它程序关闭的识别 | **未实现** | runner **无** broker SL/TP 成交识别代码；Doc B §7 属目标定义 | 需新增（依赖 Q1/Q4 可行性） |
| Q5 外部程序改变仓位时的恢复策略 | **未实现** | `query_trade_state` 仅判 `EXISTS/CLOSED/UNKNOWN`（`scheduled_runner_v3.py:702-708`） | 需新增 |
| Q6 `risk_state.json` 原子性/并发/迁移 | **同 V6/V7** | 同上 | 同上 |

**（3）定仓 §6.2 Q1–Q5**

| 项 | 结论 | 证据 | 处理结论 |
|---|---|---|---|
| Q1 风险基线未测 | **未核实**（非代码可判） | 需只读运行时/账户审计 | 实施前实测（O-2） |
| Q2 NAV 接入未完成 | **已核实（未完成）** | FACTS §3：现成 `get_NAV()` 跑废弃 client；runner 无 NAV 来源 | P0 前置 |
| Q3 `maximumOrderUnits` 未取 | **未核实** | 需 `AccountInstruments` 读取 | 需取（O-3） |
| Q4 live 账户币种未验 | **未核实** | 本次仅验 practice 4 账户 | 需验（O-4） |
| Q5 交易规格读取源未统一 | **已核实（有缺陷）** | `get_instrument_spec_raw` 仅返回 `tick_size/min_stop_distance`、**无 `minimumTradeSize`**（`trading_core_v2.py:711-714`），且 `:652-664` **误把 `minimumTradeSize` 当价格距离**候选；规格读取异常走**合成默认 fallback**（`:715-729`）而非 REJECT | 需统一读取源 + 失败即拒（不得静默默认） |

> **额外发现（Stage B 顺手，属代码事实）**：`_gated_close_trade_by_id`（`scheduled_runner_v3.py:513-556`）在每次变更前**执行一次全新 broker 读**（`_find_open_trade_by_id`）做实时权属校验 —— 即 Doc B §2④ Live Validation 的**现成内核已存在**（L0/L2/L3 已用，L4 未接）。

---

## 7. OPEN 清单（未核实 / 前置依赖 —— 禁止当作已知）

| # | 事项 | 关联 | 卡点 |
|---|---|---|---|
| O-1 | **NAV 接入未完成** | Doc A §1/§3 | `AccountSummary.NAV` 与 v3 client 的接入+验证；**`fixed_risk_pct` 切 LIVE 的 P0 前置** |
| O-2 | **风险基线未测（Q1）** | Doc A §6.2/§8 | 当前每笔实际有效风险 vs 0.5% 目标；切前须做只读基线审计 |
| O-3 | **`maximumOrderUnits` 未取** | Doc A §4/§6.2 | max_units 边界 |
| O-4 | **live 账户币种未验** | FACTS §5.1 | 本次仅验 practice 4 账户 |
| O-5 | **交易规格读取源未统一** | Doc A §4/§6.2 Q5 | `trading_core_v2` **缺字段（活跃）** vs `oanda_execution:94` fallback 1000（休眠） |
| O-6 | **`risk_state.json` 持久化安全** | Doc B §4.3 V6–V7 / §9.2 Q6 | **原子写已具备**（tmp+os.replace，`risk_engine_v22.py:138-141`）；**待补**：并发写保护（文件锁）、旧 schema 兼容与**迁移失败不静默丢弃**（当前 `load` 异常→丢弃全部，`:150-152`）；**Stage C 之前的硬前置** |
| O-7 | **FACTS 时间边界** | FACTS §8 | 事实取证于 2026-11-19；实施前须**重取一次**（含 O-3/O-4） |
| O-8 | **pending 状态 schema 扩展（V1/V2/V3/V5）** | Doc B §4.1/§4.3 | `PendingExitState` 无 layer/status/稳定 req_id 字段；`reconcile_and_exit` 仅受理 `EXIT_READY`；扩展须零回归且兼容旧文件 |
| O-9 | **L1 识别 / 外部改仓恢复（Q4/Q5）** | Doc B §7/§9.2 | runner **未实现** broker SL/TP 成交识别与外部改仓恢复；属待建能力（依赖 Q1 可行性） |

---

## 8. 纪律与边界（禁止项）

- **本 Plan 与两份契约均不授权任何代码修改**；任何 Python / 配置 / 运行行为改动须 **Owner 单独批准**。
- **不得**自动把 DRAFT 改为 APPROVED。
- **不得**仅凭运行时长批准切 LIVE（A-06）。
- 计算异常后**不得**回满仓；数据缺失**不得**默认 `scale=1.0`；风险预算无法确认**不得**提交订单；**不得**为凑最小手数突破预算（A-02）。
- 遵循 `AGENTS.md`：分析/审计/设计不改码；行为含糊时**停止并报告**，不猜测。

---

## 9. 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-11-19 | 初建：收纳 Doc A v0.2 / Doc B v0.2 / FACTS / V3_CODE_AUDIT；汇总 12 项 Owner Decisions、Stage A–E、接口闭环（两层读取）、OPEN 清单 |
| v0.2 | 2026-11-19 | 新增 **§6.1 Stage B 只读接口核实结果**（Doc B V1–V7 / Q1–Q6、Doc A Q1–Q5 逐项结论+证据+处理）；OPEN 清单补 O-8/O-9、更正 O-6（原子写已具备）；同步清理 V3_CODE_AUDIT 残留 weak-exit 旧建议、更正 FACTS §7 原子写表述、修正 Doc A §8 测试集口径 |
