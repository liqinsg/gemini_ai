# Exit Arbitration Contract — v3（DRAFT v0.2）

状态：**DRAFT · NOT APPROVED · NO CODE AUTHORIZATION**
日期：2026-11-19
范围：`scheduled_runner_v3.py`（= v31 血统）的开仓后**出场链路**。
配套：`docs/RISK_SIZING_CONTRACT_DRAFT.md`（Doc A，入场定仓）、`docs/VERIFIED_ACCOUNT_FACTS_2026-11-19.md`（已核实账户事实备忘）。
上级参考：`docs/Development_plan.md`（本契约评审通过后由其收纳，**不自动生效**）。
产出目的：当前出场由 **5 条独立路径**各自决策、各自执行、各自记录，**没有统一优先级、没有统一幂等、没有统一原因字段**。本契约定义**单一仲裁层**。**先立契约，再动代码。**

---

## 修订摘要（v0.1 → v0.2）

本版为**纯文档修订**，**未修改任何 Python 文件、配置或运行行为**。变更清单：

| # | 变更 | 落点 |
| -- | ---- | ---- |
| 1 | 新增**三条核心边界**（Decision ≠ Request ≠ Confirmation ≠ Cleanup）为全契约基础原则 | §0.1 |
| 2 | 统一执行链补入 **Live Validation** 环节，并声明"快照 ≠ 执行授权" | §2 |
| 3 | 落实 **B-01**：L2 优先 L3，被压制方标记 `SUPPRESSED_BY_HIGHER_PRIORITY`，禁止失败降级 | §3.2 |
| 4 | 落实 **B-02**：全平优先为**硬规则**，在仲裁阶段禁止低优先级进入执行队列（不依赖函数顺序） | §3.1 |
| 5 | 新增 **L4 独立部分平仓执行状态机**（B-04，五态），并与全平 pending 状态机解耦 | §4.2 |
| 6 | 落实 **B-05**：L3 改用稳定 req_id；**区分**决策身份 / 请求身份 / 确认身份；不宣称服务端 exactly-once | §4.1 |
| 7 | 落实 **B-06**：确认交易关闭**之后**清理 scale-out / partial / pending 残留；UNKNOWN 时保留 | §4.2 / §6 |
| 8 | 新增**执行失败语义**分类；UNKNOWN 一律先对账、禁止盲目重发 | §6 |
| 9 | 新增 **L1 Broker SL/TP 定义**：发现机制、SL/TP 识别、归属判定、防误记 | §7 |
| 10 | §7 六项 Owner Decision 改标为 `OWNER DECIDED — PENDING DOCUMENT INCORPORATION`，并新增待核实技术问题 | §9 |
| 11 | §8 验收指标新增 8 条；并声明"跳过次数 >0"仅作正向证据、冲突需**模拟候选**测试 | §10 |
| 12 | §9 实施阶段细化为 **Stage A–E**，各阶段独立验收 | §11 |
| 13 | 补入 `risk_engine_v22` pending/reconcile 复用能力的**代码级核实结论**与**待验证项** | §4.3 |
| 14 | 补入 `risk_state.json` **持久化安全**（原子写 / 并发保护 / 迁移兼容）为仲裁落地的**前置条件** | §4.3 V6–V7 / §9.2 Q6 / §10 |
| 15 | **联合审核收口**：唯一快照扩为**账户 + 持仓视图**（入场 Doc A 与出场共享）；出场态命名 `EXIT_REJECTED` | §2 / §4.1 / §8 |
| 16 | **Codex 评审收口**：明确 **① 周期决策快照** 与 **② 执行前定向复核（= Live Validation）** 为两层，消除"唯一快照 = 禁止实时校验"的误读 | §2 |

未决技术问题**未被删除**，统一迁入 §9.2（见 §13.3）。

---

## 0. 一句话

出场不是一个函数，是**一堆互不知道对方存在的函数**。同一周期里，risk-engine 可能判定"动量反转全平"，weak-exit 可能判定"排名垫底全平"，scale-out 可能判定"+1R 平 50%"——**三者对同一个 trade 可以同时成立**。今天能不出事，只因为它们的触发条件恰好不撞。本契约把"恰好不撞"变成"**保证不撞**"。

### 0.1 核心原则：三个边界（全契约基础）

本契约要解决的重复平仓、重复减仓、状态丢失，根因都是把下列三组概念**混为一谈**。三者必须严格分离：

1. **Decision ≠ Request**：认为该退出，**不代表**请求已经发出。
2. **Request ≠ Confirmation**：请求成功返回，**不代表**仓位已经按预期变化。
3. **Confirmation ≠ Cleanup**：**必须确认交易最终状态之后**，才允许清理关联状态。

> 推论：任何仅凭"本地已提交/已标记 fired"就更新完成状态或清理状态的做法，均**违反本契约**。

---

## 1. 现状（代码事实）

### 1.1 出场路径清单

| #  | 路径               | 触发                              | 行为                        | 执行入口                                                                                                                    | 证据                                                        |
| -- | ------------------ | --------------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| L0 | Emergency          | `.emergency_close_lock_v3` 存在 | 全平（组前缀）              | `_gated_close_trade_by_id`                                                                                                | `:1970` `_emergency_close_all` → `:2000` 调用门禁    |
| L1 | Broker SL/TP       | OANDA 服务端                      | 全平（外部）                | **不在 runner 内**                                                                                                    | 无 runner 代码；仅 guardian 事后补挂                        |
| L2 | Risk-engine 早退   | H1/D1 动量反向                    | 全平                        | `RiskManagementRunner.process_h1_bar` / `process_daily_bar` → `reconcile_and_exit` → `send_close_order`（= 门禁） | `risk_engine_v22.py:388`/`:457`；runner 适配器 `:710` |
| L3 | Weak-strength exit | 组内支撑分垫底                    | 全平                        | `_gated_close_trade_by_id`（**已走门禁**）                                                                          | `:4815` `_weak_exit_scan_v31` → `:4884`              |
| L4 | Scale-out          | +1R / +2R                         | **部分平**（50%/50%） | `_trading_core.close_trade_partial`（**未走门禁**）                                                                 | `:4706` `_scale_out_scan_v31` → `:4779`              |
| — | SL/TP guardian     | 服务端丢了 SL/TP                  | **补挂**（非平仓）    | `_gated_attach_sltp_by_id`                                                                                                | `:2079` `_sltp_guardian` → `:2222`                   |

### 1.2 循环内执行顺序（`run_cycle`）

```
:5798  _sltp_guardian            （补挂 SL/TP，先跑）
:5816  _maintain_group_positions （L2 risk-engine 早退 + override，逐组）
:5821  _weak_exit_scan_v31       （L3 全平）
:5827  _scale_out_scan_v31       （L4 部分平）
```

**观察**：全平（L3）在部分平（L4）**之前**——方向正确，但这是**顺序巧合**，不是**仲裁保证**。L2 在 `_maintain_group_positions` 内、L3/L4 在外层；三者**取各自独立快照**，互不感知。

### 1.3 幂等性对照

| 路径           | client_request_id / 状态                                                            | 同周期幂等 | 跨周期幂等                      |
| -------------- | ----------------------------------------------------------------------------------- | ---------- | ------------------------------- |
| L2 Risk-engine | `reconcile` 生成 + `pending_exits` 状态机（`risk_engine_v22.py:136/357/388`） | ✅         | ✅（pending 保留）              |
| L3 Weak-exit   | `WEAKEXIT_{tid}_{stamp}`，`stamp=now.timestamp()`                               | ✅         | ❌（**每周期 stamp 变**） |
| L4 Scale-out   | `SCALEOUT-{tid}-R{rung}` + `mark_fired` 状态（`utils/scale_out.py`）          | ✅         | ✅（rung 稳定）                 |
| L0 Emergency   | 无统一 req_id                                                                       | —         | —                              |

> **L3 的跨周期不幂等是真实缺口**：若一周期的全平请求已提交但快照未及时更新，下一周期会以**新 stamp** 再发一次同 trade 全平。多数情况 OANDA 会以"trade 已关"拒绝，但这是**依赖外部行为兜底**，非本地保证。（修法见 §4.1 / B-05。）

### 1.4 权属一致性

| 路径         | 权属处理                                                                                                        | 证据                   |
| ------------ | --------------------------------------------------------------------------------------------------------------- | ---------------------- |
| L2 / L3 / L0 | 平仓时**再过门禁**（`is_bot_owned_trade` 实时校验）                                                     | `:532`               |
| L4 Scale-out | 仅**快照期**过滤 `is_bot_owned_trade`（`:4730`），平仓走 `close_trade_partial` **无二次门禁** | `:4730` → `:4779` |

> **L4 是唯一"快照过滤 + 无二次校验"的平仓路径**（TOCTOU：快照与平仓之间账户可能被人工/其它进程占用）。（修法见 §3.3 / B-03。）

### 1.5 Exit Reason

各路径各自 `print` / `_safe_emit`，**无统一枚举、无统一字段**。事后无法用一个 query 回答"这笔 trade 因何而平"。

**结论**：出场链路**功能上可用**（方向对、权属多数对），但**结构上无仲裁**——多路径冲突、id 生成不统一、原因不统一，是**结构性缺口**（非当前故障）。

---

## 2. 目标：统一执行链

```
每次 run_cycle：
  ① Snapshot（唯一）        一次 broker 快照 —— 本周期唯一的**账户 + 持仓视图**（含 NAV / currency / 开放交易 SL）；入场（Doc A §3/§7）与出场（本契约）共享
  ② Proposals               各路径只"提议"，不执行；产出候选决策集合
  ③ Arbitration             按 L0–L4 优先级仲裁 —— 同一 trade 只允许一个终态决策
  ④ Live Validation         ★ 执行前实时校验：trade 存在 / 权属 / Tag / 状态允许本次操作
  ⑤ Execution               唯一出口 —— 全平与部分平都过统一门禁
  ⑥ Confirmation            对账 —— pending/partial 状态机 + exit_reason
```

> **关键一**：路径从"执行者"降级为"提议者"，执行权收归仲裁层一处。
> **关键二（新增）**：**快照只是决策输入，不是执行授权**。快照获取到执行之间，可能发生：人工关闭交易、其它程序改变仓位、Broker SL/TP 已成交、units 已变化。因此 ④ 的实时校验**不可省略**，且**全平（L0–L3）与部分平（L4）都必须走同一出口**，任何路径不得保留绕过执行层的特权。
> **与 Doc A 对齐（两层读取）**：本行 ① 快照 = **周期决策快照**（唯一、只读、供决策）；④ Live Validation = **执行前定向复核**（执行前允许一次定向 broker 读取，失败即拒）。二者**不同层**——"唯一快照"**不等于**禁止执行前实时校验。

---

## 3. 优先级与冲突规则

### 3.1 优先级表

| 层           | 名称                       | 粒度   | 冲突权重                           |
| ------------ | -------------------------- | ------ | ---------------------------------- |
| **L0** | Emergency                  | 全平   | 最高                               |
| **L1** | Broker SL/TP（外部已成交） | 全平   | 对账权威（不可"提议"，只能"发现"） |
| **L2** | Risk-engine 动量早退       | 全平   | 高                                 |
| **L3** | Weak-strength exit         | 全平   | 中                                 |
| **L4** | Scale-out                  | 部分平 | 低                                 |

### 3.2 冲突规则（不可颠倒）

1. **全平 > 部分平 —— 硬规则（B-02）**：同一 trade 本周期若存在任一 **L0–L3 全平决策**，**L4 不得进入执行队列**。禁止"先部分平仓、再全平"。
   - 实现要求：这**不是**把全平函数排在 scale-out 前面，而是**在仲裁阶段就禁止低优先级决策进入执行队列**。优先级**不得**依赖函数执行顺序。
2. **高优先级覆盖低优先级 —— L2 优先 L3（B-01）**：同一 trade 同周期同时命中 L2 与 L3 → **选 L2**（止损纪律 > 相对排名）。
   - L3 标记为 `SUPPRESSED_BY_HIGHER_PRIORITY`。
   - **禁止**因 L2 执行失败而自动降级为 L3 再发一次全平请求。
3. **一 trade 一决策**：仲裁后，同一 trade 在本周期**至多产生一个** close 请求。
4. **L1 独立于仲裁**：broker 已平是**既成事实**，不是仲裁层发出的请求；仲裁层只做"发现 + 清理"，**不发单**（定义见 §7）。
5. **L0 最高**：Emergency lock 存在时，其余层的提议一律 `SUPPRESSED_BY_HIGHER_PRIORITY`。

> 每笔被压制的决策必须记录：`chosen_decision`、`suppressed_decision`、`suppression_reason = HIGHER_PRIORITY_EXIT`。

### 3.3 统一执行出口与实时校验（Live Validation · B-03）

- **唯出口**：全平（L0–L3）与部分平（L4）**共用同一个执行出口**，任何路径**不得**保留绕过此出口的特权。
- **执行前实时校验**（快照之后的独立步骤，不可省略）：
  1. trade **当前存在**；
  2. trade **仍属于本 Bot**（`is_bot_owned_trade` 实时校验）；
  3. **Tag 符合权属契约**；
  4. **实时状态允许**本次操作（如剩余 units 足够部分平）。
- **任一校验失败 → 拒绝执行**（记录对应 reason，如 `OWNERSHIP_CHANGED`）。
- **权属查询失败 → 按 REJECT 处理**，不执行。
- **明确边界**：**不得**只在 scale-out 代码外层检查一次就认为后续所有 partial close 都安全——**每一次实际平仓动作都必须经过本出口的实时校验**。

> 对应 §1.4 的缺口：L4 目前仅"快照期过滤 + 无二次校验"，本出口即为修法（B-03）。

---

## 4. 状态管理

### 4.1 L0–L3 全平 Pending Exit 状态机（统一）

```
NONE ──提议(全平)──▶ PENDING(decision, since, req_id)
   PENDING ──确认已关──▶ CONFIRMED_CLOSED（清 pending；清 scale-out 残留）
   PENDING ──OANDA 拒单─▶ REJECTED → 保留决策，下周期按状态机决定重试（不降级）
   PENDING ──状态未知───▶ UNKNOWN → 保留 pending，下周期先 query_trade_state
   PENDING ──超时──────▶ STALE → 告警（不静默放弃）
```

- **适用范围**：仅 **L0–L3（全平）** 建 pending。**L4（部分平）不建此 pending**，改用独立的 §4.2 状态。
- **命名（避免与 Doc A 混淆）**：本契约的 `REJECTED` 是**出场态**，日志/对账记为 **`EXIT_REJECTED`**；Doc A 的 `REJECT` 是**入场态**（记为 `ENTRY_REJECT`）。二者**同词异义**。
- **决策身份 / 请求身份 / 确认身份（B-05，三者不可混同）**：
  - **决策身份**：本地产出的终态决策（layer + trade_id + 周期）。
  - **请求身份**：`client_request_id`，用于本地幂等去重。
  - **确认身份**：OANDA 回执 / 实际仓位变化，用于判定 CONFIRMED。
- **req_id 规则（B-05）**：全平用**稳定 id**（不带时间戳）；L3 由 `WEAKEXIT_{tid}_{stamp}` 改为 **`EXIT_WEAK_{trade_id}`**，消除 §1.3 的跨周期不幂等缺口。
  - **不得宣称**稳定 `client_request_id` 等同于 OANDA 服务端 **exactly-once** 保证；本地稳定 id ≠ 服务端幂等承诺（待核实项见 §9.2）。
- **幂等**：pending 期间重复的同 trade 全平提议 = **no-op**。
- **失败恢复**：REJECTED/UNKNOWN **不降级为"保留仓位"**，保持 pending 并于下周期重试（与 Doc A 的 fail 姿态一致：不确定时**不放弃风险动作**）。

### 4.2 L4 部分平仓执行状态（独立 · B-04）

> **裁定**：L4 **不纳入** §4.1 的全平 pending 状态机，**但也不能仅依赖 `fired` 标记**——`fired=True` 只代表"尝试过"，**不等于 OANDA 已确认部分平仓成功**。

**独立 Partial Exit Execution State（每个 rung 一份）**：

| 状态        | 定义        |
| --------- | --------- |
| NOT_FIRED | 尚未触发      |
| REQUESTED | 已发送部分平仓请求 |
| CONFIRMED | 已确认实际减少仓位 |
| REJECTED  | 明确收到拒绝    |
| UNKNOWN   | 无法确认执行结果  |

**要求**：

1. **REQUESTED ≠ CONFIRMED**：发送成功不得直接标记完成。
2. **UNKNOWN 必须先查询 OANDA 实际仓位**，确认是否已部分成交。
3. **禁止 UNKNOWN 状态直接重复提交部分平仓**（防重复减仓）。
4. **只有确认实际仓位变化后**，才允许更新该 rung 的完成状态为 CONFIRMED。
5. 明确拒绝（REJECTED）后的重试必须遵循状态机与幂等规则。
6. **执行前须通过 §2 ④ 的 Live Validation**（B-03，含 L4 二次权属门禁，见 §3.3）。

**残留清理（B-06）**：确认**交易已关闭之后**，清理该 trade 的 scale-out fired/rung 状态、对应 partial execution 状态、对应 pending 记录。
- **禁止**在全平请求刚发送、交易关闭**未确认**之前就清理。
- 当交易状态为 **UNKNOWN** 时，**保留**全部必要状态，等待对账。

### 4.3 复用 `risk_engine_v22` pending/reconcile —— 方向与核实要求

**方向（保留）**：仲裁层**复用**而非重造 `risk_engine_v22` 的 pending/reconcile 能力（`PendingExitState` / `save_pending_exits` / `prune_pending_exits` / `reconcile_and_exit`，`risk_engine_v22.py:136/357/364/305`）。

**代码级已核实（见 `VERIFIED_ACCOUNT_FACTS_2026-11-19.md` §6）**：

- **可复用（与退出层无关）**：`ExecutionReconciler.query_trade_state` / `send_close_order`（runner 适配器 `:710`，已过权属门禁）+ `reconcile_and_exit` 的轮询确认（`risk_engine_v22.py:305`）。
- **不可原样复用**：① `reconcile_and_exit` 硬编码只认 `ExitDecision.EXIT_READY`（`:313`）；② `PendingExitState` schema 无 layer / units / confirm_status（`:51-58`）；③ 仅全平、**无部分平路径**；④ 单文件全量重写 `risk_state.json`（`:136-141`）。

**因此本节的裁定口径为**：`pending` **可复用内核 + 需扩展外壳**（状态 schema、决策枚举、部分平确认）。这是"复用**哪一半**"的问题，不是"免费复用"。

**实施前必须验证（未验证不得认定为已满足）**：

| # | 待验证项 | 目的 |
| - | -------- | ---- |
| V1 | `PendingExitState` 是否支持 **L0 / L2 / L3 的统一决策身份** | 多来源全平可共存于同一状态机 |
| V2 | 是否支持**稳定 req_id** | 承载 B-05 |
| V3 | `REJECTED` / `UNKNOWN` / `STALE` 是否有可恢复语义 | 承载 §6 失败恢复 |
| V4 | 是否能按 **trade_id 精确清理** | 承载 B-06 |
| V5 | 扩展 schema 后是否**破坏现有 Risk-engine 行为**、是否兼容既有 `risk_state.json` | 零回归 |
| V6 | `risk_state.json` 写入是否**原子**（tmp+rename）；多写者下是否有**并发写保护**（文件锁） | 防状态覆盖 |
| V7 | 旧 schema 的**兼容与迁移失败处理**（迁移失败不得静默丢弃 pending） | 升级不丢状态 |

> **持久化安全（前向要求，非现存缺陷）**：`risk_state.json` 今天为**单写者**（仅 `risk_engine_v22` 的 `StateStore` 写；见 `VERIFIED_ACCOUNT_FACTS_2026-11-19.md` §7），**无可观察竞态**。但一旦 L0/L2/L3 共享该文件，**全量重写**（`save_pending_exits`）即成为覆盖风险。因此 **V6/V7 是仲裁层落地的前置条件**，必须在 Stage C 影子实现之前满足。

---

## 5. 统一 Exit Reason 枚举

```
EMERGENCY
BROKER_SL | BROKER_TP
RISK_MOMENTUM_H1 | RISK_MOMENTUM_DAILY
WEAK_STRENGTH
SCALE_OUT_R1 | SCALE_OUT_R2
MANUAL
UNCLASSIFIED      ← 新增：能确认已平、但无法归因
```

**统一记录字段**（每笔实际平仓一行）：
`ts, trade_id, pair, side, source_layer, exit_reason, units_closed, remaining_units, realized_pl, req_id, confirm_status`

**归属判定与防误记（配合 §7）**：

- 能确认交易已关闭、但**无法归因**为某条已知路径时，记 `UNCLASSIFIED`——**不得**默认记为 `BROKER_SL` 或 `BROKER_TP`。
- 归因优先级：有本地决策记录 → 用本地 `source_layer`；无本地记录但 broker 显示 SL/TP 成交 → 归 `BROKER_SL/TP`；两者皆无 → `UNCLASSIFIED`。
- **禁止**把其它程序 / 人工的关闭**误记**为 `BROKER_SL/TP`。

---

## 6. 执行失败语义

必须区分下列结果，**不得合并处理**：

| 结果 | 判据 | 后续 |
| ---- | ---- | ---- |
| 明确拒绝 | OANDA 返回 reject / 明确错误码 | → `REJECTED`，按状态机重试 |
| 网络超时 | 请求未达 / 超时 | → `UNKNOWN` |
| 响应丢失 | 已发但未收响应 | → `UNKNOWN` |
| Broker 已执行但本地未确认 | 本地无回执，实盘已变 | → `UNKNOWN`，先对账后补记 CONFIRMED |
| Trade 已不存在 | `query_trade_state` 查无此 trade | → `CONFIRMED_CLOSED`（视为已平空）+ 触发 B-06 清理 |
| 权属变化 | Live Validation 失败（非本 Bot / Tag 不再匹配） | → **拒绝执行**，记录 `OWNERSHIP_CHANGED` |

**UNKNOWN 铁律**：一律**先对账**（`query_trade_state` / 查询实际 units），**禁止盲目重发**全平或部分平。只有对账后仍处于未知，才保留状态进入下周期。

---

## 7. L1 Broker SL/TP 定义（外部事实层）

**定位**：L1 是**外部既成事实**，**不是**仲裁层发出的退出请求。它在**周期开始的对账**与**执行前对账**时被识别，**不进入普通提议/仲裁队列**。

必须定义：

1. **发现机制**：如何在每周期对账中识别"broker 已关闭某 trade"（对比上一周期快照 / 本地持仓与实际持仓）。
2. **清理**：发现 broker 已平后，如何清理该 trade 的对应 pending（全平）与 partial 状态（L4）。
3. **识别 SL vs TP**：依据实际成交价与 trade 的 SL/TP 价关系判定（成交价≈SL 价 → `BROKER_SL`；≈TP 价 → `BROKER_TP`）。
4. **无法识别退出原因**：使用 `UNCLASSIFIED`（§5）；禁止猜测填 `BROKER_SL/TP`。
5. **防误记**：如何避免把**其它程序 / 人工**的关闭误记为 `BROKER_SL/TP`——仅在成交价与 SL/TP 价在容差内匹配时归因，否则 `UNCLASSIFIED`。

> 归属判定的不确定性是**已知风险**，不写成"已解决"；识别能力属待核实项（§9.2）。

---

## 8. 循环内顺序（修订后）

```
1. 对账已有 pending（先收拾上周期遗留）
2. 唯一 broker 快照            —— 仅作决策输入，非执行授权；账户+持仓视图，入场（Doc A）与出场共享
3. 发现 L1（broker 已平）      —— 外部事实，清 pending/partial
4. 收集候选决策：L0 / L2 / L3 / L4 —— 各路径只"提议"
5. 仲裁（L0–L4 优先级）        —— 全平优先硬规则；一 trade 一决策
6. Live Validation             —— 执行前实时权属/状态校验（统一出口）
7. 执行（唯一出口）            —— 全平与部分平
8. Confirmation + 统一记录     —— 状态机推进 + exit_reason（或 UNCLASSIFIED）
9. SL/TP guardian（补挂）      —— 不与平仓冲突，可任意时点
```

> 与 v0.1 的差别：**L4 的跳过由"仲裁阶段"保证，而非"排在后面"**；**执行前新增 Live Validation**；**L1 从优先级队列移出为对账发现层**。

---

## 9. Owner Decisions 与待核实问题

### 9.1 Owner Decisions（状态：`OWNER DECIDED — PENDING DOCUMENT INCORPORATION`）

以下六项**已由 Owner 裁定**，**不再列为未决问题**；其落实位置见对照表（§13.2）。

| 编号 | 裁定摘要 | 落实位置 |
| ---- | -------- | -------- |
| B-01 | L2 优先于 L3；被压制方记 `SUPPRESSED_BY_HIGHER_PRIORITY`；禁止失败降级 | §3.2 |
| B-02 | 全平优先为**硬规则**；仲裁阶段禁止低优先级进入执行队列；不依赖函数顺序 | §3.2 / §8 |
| B-03 | L4 必须补执行前实时权属门禁；统一出口统一校验；任一失败即拒绝 | §2 / §3.3 |
| B-04 | L4 建立**独立** Partial Exit Execution State（五态）；UNKNOWN 先对账、禁止盲目重发 | §4.2 |
| B-05 | L3 改用稳定 req_id（`EXIT_WEAK_{trade_id}`）；区分决策/请求/确认身份；不宣称服务端 exactly-once | §4.1 |
| B-06 | 确认交易关闭**之后**清理 scale-out/partial/pending 残留；UNKNOWN 保留状态 | §4.2 / §6 |

### 9.2 仍待技术核实的问题（新增，未解决，不删）

| # | 问题 | 关联 |
| - | ---- | ---- |
| Q1 | OANDA `client_request_id` 的**实际幂等保证**究竟为何 | B-05 |
| Q2 | `risk_engine_v22` pending 接口的**兼容性**（schema 扩展、旧 `risk_state.json`） | V1–V5 |
| Q3 | **L4 partial 状态的持久化方式**（存哪、跨进程可见性、崩溃恢复） | B-04 |
| Q4 | **Broker SL/TP 与人工/其它程序关闭的识别能力**（是否可靠可归因） | §7 |
| Q5 | 同一 trade 被**外部程序改变仓位**时的恢复策略 | §6 |
| Q6 | `risk_state.json` 状态持久化的**原子性、并发写保护、旧 schema 兼容与迁移失败处理** | V6–V7 / B-04 |

> 原 v0.1 §7 的六项问题**未删除**，其裁定已落于 §9.1；技术性延伸并入本表。

---

## 10. 日志与验收指标（带闭环）

**核心指标**：同一 `trade_id` 在**任一单周期**内的"平仓请求次数"。

**闭环判定**：

- 达标：连续 **≥ 20 周期**日志中，无 `trade_id` 在同周期出现在 **≥2** 条平仓请求（全平或部分平）。
- 不达标：定位串场路径（多为快照不一致），修正后重测。
- 任一"同周期同 trade 双平"（不论是否被 OANDA 拒）→ 视为 **P0 缺陷**，回滚仲裁开关。

**新增验收条款（11 条）**：

1. 同一 trade 同周期**最多一个** close request。
2. **L4 二次权属检查覆盖率 100%**。
3. **UNKNOWN 不发生盲目重发**。
4. **全平未确认时，不提前清理** scale-out 状态。
5. L2/L3 冲突时**始终选择 L2**。
6. L0–L3 命中时 **L4 执行次数为零**。
7. L4 请求成功但**仓位未确认减少时，不得标记 CONFIRMED**。
8. 每个实际退出必须具有**可追溯的 `exit_reason`** 或明确的 **UNKNOWN reason**。
9. **崩溃/重启恢复**：pending 与 partial 状态**不丢失、不重复执行**（恢复即先对账）。
10. **多写者并发**：L0/L2/L3 共享 `risk_state.json` 时**不出现状态覆盖**（并发写测试）。
11. **旧 schema 迁移**：迁移**失败时不得静默丢弃** pending，须保留并可回滚。

**关于冲突证据的说明**：

- "L4 被高优先级跳过次数 > 0"**只能作为冲突覆盖能力的正向证据**，**不得作为唯一验收条件**（自然冲突可能不发生）。
- 没有自然冲突时，**必须使用模拟候选决策测试**验证仲裁（构造 L2/L3 同命中、L0–L3 与 L4 同命中等场景）。

---

## 11. 实施阶段与边界

模式开关：`MODE: "legacy" | "shadow" | "arbitrated"`，**默认 `legacy`**（不改变现状）。

| 阶段 | 内容 | 独立验收条件 |
| ---- | ---- | ------------ |
| **Stage A** | 纯文档契约审议（本契约 + Doc A） | 两份契约相互闭合、接口无冲突 |
| **Stage B** | 只读代码接口核实（§4.3 V1–V7、§9.2 Q1–Q6） | 每项给出"已核实/未核实"结论，含代码行号 |
| **Stage C** | 影子仲裁实现与测试（`MODE=shadow`，只写日志不改单） | §10 全部 11 条 + 模拟候选决策测试通过 |
| **Stage D** | 影子运行验收（并行对照，不改真实下单） | 连续 ≥20 周期指标达标、新旧决策差异可解释 |
| **Stage E** | 切换 `arbitrated` | **Owner 单独批准**后才可执行 |

- **不删**现有路径，只把"执行权"从各路径**收拢**到仲裁层。
- 与 Doc A（定仓）、SHADOW 改名**解耦，不同 commit**。

---

## 12. 与外部评审的取舍

- **采纳 ChatGPT**：L0–L4 优先级、Pending Exit 状态机、同周期幂等、Exit Reason 统一、失败恢复与下周期处理。
- **采纳并细化 Owner 裁定**：三边界（Decision/Request/Confirmation/Cleanup）、L4 独立部分平状态机、稳定 req_id、确认后清理、Live Validation。
- **修正/补充（代码核实）**：
  - L4 的部分平**未走权属门禁**（§1.4）——须纳入统一出口；
  - L3 的 req_id 带时间戳**跨周期不幂等**（§1.3）——须改稳定 id；
  - `risk_engine_v22` **已具备** pending/reconcile 内核，但**外壳需扩展**（§4.3）——仲裁层应**复用内核**，勿重造。

---

## 13. 交付附录

### 13.1 修订摘要

见文首「修订摘要（v0.1 → v0.2）」。

### 13.2 Owner Decision 对照表

| 编号 | Owner 裁定 | 落实章节 | 状态 |
| ---- | ---------- | -------- | ---- |
| B-01 | L2 优先 L3 | §3.2 | `OWNER DECIDED — PENDING DOCUMENT INCORPORATION` |
| B-02 | 全平优先硬规则 | §3.2 / §8 | 同上 |
| B-03 | L4 二次门禁 | §2 / §3.3 | 同上 |
| B-04 | L4 独立部分平状态机 | §4.2 | 同上 |
| B-05 | 稳定 req_id | §4.1 | 同上 |
| B-06 | 确认关闭后清理 | §4.2 / §6 | 同上 |

### 13.3 尚未解决的技术问题

见 §9.2（Q1–Q6）与 §4.3（V1–V7）。原 v0.1 §7 的问题**均未被删除**。

### 13.4 声明

- 本版为**纯文档修订**：**未修改任何 Python 文件、配置或运行行为**。
- **未修改** `docs/Development_plan.md`。
- 文件状态**继续保持**：**DRAFT · NOT APPROVED · NO CODE AUTHORIZATION**。
- 未经验证的接口能力**未**被写成"已确认的事实"；未决问题**未**被删除。
- 下一步：与 **Doc A v0.2** 做联合契约审核，再决定是否更新 `Development_plan.md`。**目前无任何代码修改授权。**
