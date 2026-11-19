# 已核实账户事实备忘 — 2026-11-19

性质：**记录性文档（FACTS）· 非契约 · 不授权修改代码或契约正文**
用途：把本轮只读核查得到的**账户事实**固化下来，供 `RISK_SIZING_CONTRACT_DRAFT.md`（Doc A）与 `EXIT_ARBITRATION_CONTRACT_DRAFT.md`（Doc B）在修订/定稿时引用。
纪律：本文件内的条目只有两类——**已核实（从真实账户/代码取得）** 与 **仍 OPEN（未核实）**。不存在第三类。

---

## 0. 取证方式（可复现）

- 环境：`OANDA_ENV=practice`（demo）。
- 目标账户：`OANDA_ACCOUNT_ID_2` = `101-003-39389016-002`（= v3 runner 默认 profile 2 实际使用的账户，见 `scheduled_runner_v3.py:194,284-288`）。
- 手段：**只读** 调用 `AccountSummary` 与 `AccountInstruments`，未下单、未改动任何状态。
- 复现命令要点：
  ```python
  import config_oanda as oc, oandapyV20.endpoints.accounts as oa
  p = oc.get_oanda_profile('practice'); cid = p['oanda_client']
  cid.request(oa.AccountSummary('101-003-39389016-002'))['account']
  cid.request(oa.AccountInstruments('101-003-39389016-002', params={'instruments':'USD_JPY'}))
  ```

---

## 1. 账户计价货币 —— 已核实（per-account，非全局）

| profile | account_id | currency |
|---|---|---|
| 1 | 101-003-39389016-001 | **SGD** |
| **2（v3）** | **101-003-39389016-002** | **USD** |
| 3 | 101-003-39389016-003 | USD |
| 4 | 101-003-39389016-004 | SGD |

- **结论**：v3 实际账户是 **USD**；同 org 下币种混用（SGD/USD 并存）。
- **影响 Doc A §4**：不得硬编码任何单一币种，必须**运行时读** `AccountSummary.currency`。
- **推翻外部评审断言**：某外部评审称"账户货币 = SGD（已确认）"，对本 org 并非普遍成立，且**对 v3 账户为假**。
- **换算可简化**（账户=USD，交易池 = JPY 报价 + USD 报价）：`fx(quote→account)` 退化为两种——USD 报价 → `1.0`；JPY 报价 → `1 / USD_JPY(mid)`。

> **同账户硬要求（新增）**：`currency` 与净值 `NAV` **必须取自同一个实际账户**。禁止"账户 A 的 NAV + 账户 B 的 currency"——这种错配会让风险预算**形式上成功、实际完全错误**。参见 §4（`account_ids` 重复项会放大此风险）。

---

## 2. units 精度 / 最小交易单位 —— 已核实

| instrument | displayPrecision | tradeUnitsPrecision | minimumTradeSize | pipLocation |
|---|---|---|---|---|
| USD_JPY | 3 | 0 | 1 | -2 |
| EUR_JPY | 3 | 0 | 1 | -2 |
| GBP_JPY | 3 | 0 | 1 | -2 |
| EUR_USD | 5 | 0 | 1 | -4 |

- **结论**：units 为**整数**（precision=0）；**最小交易单位 = 1**（非 1000）。
- **对 Doc A A-02 的边界事实落定**：计算 units < 1 时按契约 **REJECT**；不得提至非整数。

**规格缺失/读取失败的处理（分两个独立代码点登记，不可混为一谈）**：

| 代码点 | 现象 | 对 v3 的性质 |
|---|---|---|
| `utils/oanda_execution.py:94` | fallback `minimum_trade_size = 1000`，与账户不符 | **休眠（latent）**：该模块**不在 v3 执行链**上（v3 走 `utils/trading_core_v2.py`）；消费者为 `ml_forex_strategy.py` / `check_order_details.py` / `tests/` / `pyramid_cluster.py` |
| `utils/trading_core_v2.py:711-714` | 返回的 spec dict **不含** `minimumTradeSize`，且 `:652-664` 把 `minimumTradeSize` 误当**价格距离** | **活跃**：v3 的 min-units 因此只能依赖配置假定（`MIN_LOT_LIVE=1`） |

- **通用原则（Owner 倾向）**：交易规格成功读取 → 用实际规格；规格缺失/读取失败 → **拒新单（REJECT）**；**禁止静默使用陈旧默认值**；不影响既有持仓的管理与退出能力。
- 现有假定核对：`config.py:388 MIN_LOT_LIVE = 1` **与账户一致**。

---

## 3. NAV 字段名与既存缺陷 —— 已核实（含缺陷登记）

- 真实字段为 **`NAV`（大写）**，本次取值 `99282.4083`（`trueNAV` 同值）；`nav`（小写）**不存在**。

**既存缺陷（静默失效，登记备查）**：以下两处按小写 `nav` 读取，**恒返回缺省值**：

| 位置 | 代码 | 后果 |
|---|---|---|
| `config_oanda.py:264` | `resp.get('nav', '?')` | 打印恒为 `?` |
| `validate_profile_wiring.py:96` | `a.get('nav', '?')` | 打印恒为 `?` |

而唯一读对字段的实现 `utils/oanda.py:46-50 get_NAV()`（读 `.NAV`）**运行在另一套废弃 client 上**，v3 使用的 TradingCore/v20 路径**未接入**。

- **影响 Doc A §3**：`Equity` 的现成来源**不可用**（坏的坏、死路子的死路子）。实施前必须先解决净值读取；且**不得**在未修此缺陷时让定仓公式上线。
- **NAV 是 Doc A 的 P0 前置依赖**：NAV 接入与验证完成之前，`fixed_risk_pct` **不得切换 LIVE**。
- NAV **读取失败 / 字段缺失 / 非正数 / 数据无效** → 一律 **REJECT**（见 Doc A §5）。

---

## 4. 附带发现（低优先，但与 §1 同账户要求相关）

- `profile['account_ids']` 含**重复项**：实际 8 条、去重后 4 条（`001,002,003,004,001,002,003,004`）。
  任何遍历 `account_ids` 的代码会**重复处理**；v3 走属性直取 `OANDA_ACCOUNT_ID_2`，暂未受影响。
  **风险关联**：该重复项是"账户错配"（A 的 NAV + B 的 currency）的温床——§1 的同账户要求必须**显式**成立，不得依赖遍历列表。

---

## 5. 仍然 OPEN（未核实，禁止当作已知）

| # | 事项 | 说明 |
|---|---|---|
| 5.1 | 账户币种是否会随 profile/env 变化 | 本次仅验证 practice 4 个账户；live 账户币种**未验**。 |
| 5.2 | 各 instrument 的 `maximumOrderUnits` | 未取；与 Doc A 的 `max_units` 边界相关。 |
| 5.3 | `risk_engine_v22` pending 承载统一仲裁 | **已从代码核实**（见 §6），但"扩展后是否向后兼容既有 `risk_state.json`"**未验**。 |
| 5.4 | 当前每笔**实际有效风险** | 决定迁移期 Risk% 取值（0.5/0.8 之争的锚点），**未测**；建议实施前做一次只读基线审计。 |

---

## 6. 对 `risk_engine_v22` 承载能力的核实结论（代码级，供 Doc B §4 引用）

- **可复用（与退出层无关）**：`ExecutionReconciler.query_trade_state` / `send_close_order`（runner 适配器 `scheduled_runner_v3.py:702-716`，已过权属门禁）+ `reconcile_and_exit` 轮询确认 `risk_engine_v22.py:305-350`。
- **不可原样复用**：① `reconcile_and_exit` 硬编码只认 `ExitDecision.EXIT_READY`（`:313`）；② `PendingExitState` schema 无 layer/units/confirm_status（`:51-58`）；③ 仅全平、无部分平路径；④ 单文件全量重写 `risk_state.json`（`:136-141`）。
- **裁定口径**：Doc B §4 的措辞应从"复用现有机制"精确为"**复用执行确认内核 + 扩展状态 schema 与决策枚举**"。

---

## 7. `risk_state.json` 持久化现状 —— 已核实（并发风险为前向）

- 路径：`scheduled_runner_v3.py:752` 设置 `_risk_state_path = PROJECT_ROOT / "risk_state.json"`，`:754` 交给 reconciler。
- **当前为单写者**：仅 `risk_engine_v22` 的 `StateStore` 写（默认路径 `:133/:354`；写入点 `save_pending_exits` 于 `:377/:422/:429/:433/:445`）。**今天无可观察竞态。**
- 写入方式：**全量重写，且已是原子写**（`save_pending_exits` 写整个 dict 到 `*.tmp` 后 `os.replace`，`risk_engine_v22.py:138-141`）。**更正**：早前把"原子写"列为待补项有误——原子写**已具备**。
- **读取失败处理（缺陷登记）**：`load_pending_exits` 解析异常时打印 `Recovery failed, start fresh` 并 `return {}`（`:150-152`）→ **先告警、随后返回空状态（等于丢失全部 pending 记录）**；无 schema 版本字段。
- **前向要求（非现存缺陷）**：一旦仲裁层成为**多写者**（L0/L2/L3 共享同一文件），必须补：**并发写保护（文件锁）**、**旧 schema 兼容与迁移失败处理（不得静默丢弃 pending）**。原子写无需再补。此项是 Doc B 仲裁设计的**前置条件**，不是"修一个现有 bug"。

---

## 8. 生效声明

- **时间边界（新增）**：本备忘事实取证于 **2026-11-19** 对 practice 账户的只读调用；币种 / NAV / 交易规格属**账户与环境事实**，可能随后变化。**本次评审未复验账户响应**；实施前须**重取一次**（live 币种、`maximumOrderUnits` 已在 §5 列为 OPEN）。
- 本备忘**不改变** Doc A / Doc B 的 DRAFT 状态，**不构成**对任何代码实施的批准。
