# Portfolio 统一规划总纲（Master Plan）

状态：**PLAN · 未授权代码修改 · 仅文档**
日期：2026-11-27
权威规格：`docs/Global_Portfolio_Router_v1_1_5.md`（**路由层唯一权威 spec**）
配套文档：
- [Development_plan.md](Development_plan.md)（定仓 / 出场仲裁 收纳母文档）
- [RISK_SIZING_CONTRACT_DRAFT.md](RISK_SIZING_CONTRACT_DRAFT.md)（Doc A 入场定仓）
- [EXIT_ARBITRATION_CONTRACT_DRAFT.md](EXIT_ARBITRATION_CONTRACT_DRAFT.md)（Doc B 出场仲裁）
- [Global_Portfolio_Router_v1_1_5_理解报告.md](Global_Portfolio_Router_v1_1_5_理解报告.md)（路由层理解报告）

纪律（沿用 `AGENTS.md`）：源码即真相；分析 / 设计 / 审计类任务**不改码**；严格区分 **implemented / intended / observed / proposed**。本总纲**不授权任何代码或配置修改**。

---

## 0. 一句话

项目历史上"**货币对按报价货币分组、各组独立决策**"；随着信号增多，真实需求变成"**在全局横截面上，统盘决定这一刻交易哪一笔**"。本总纲把这一范式切换、以及它牵出的三个未解痛点——**存菁去芜（剪枝/轮换）**、**动态调参闭环**、**策略灵活性**——收拢成一份可执行的路线图：**先冻结文档 → 脚本对齐 → 接入主程序**。

---

## 1. 前提与范围（Owner 已确认）

| 编号 | 裁定 | 对规划的影响 |
| :--- | :--- | :--- |
| **D1** | **仓位以 Unit 计**（非经典 10 万合约）；**live/shadow/demo 执行细节本轮不管**；仓位控好即风险可控 | 定仓模型**不动**。本轮范围收敛到**决策与选择层**（选哪笔 / 留哪笔 / 何时轮换）。<br>注：路由器的 `RISK_UNIT_EXPOSURE`（1 笔 = ±1 风险单位）与"Unit 计价"天然同构，敞口 cap 以"笔数 / 风险单位"衡量。 |
| **D2** | **文档是唯一事实源**；差异清理 | 见 §2。 |
| **D3** | **第一阶段：脚本对齐文档**（不是反过来） | 文档冻结为 spec；脚本逐条对齐或显式标 `pending`。 |
| **D4** | 文档落地后 → **接入主程序** | 接入点 = `scheduled_runner_v3.py::_pick_global_basket`，且必须走**统一执行出口**。 |

**范围边界（本轮排除）**：真实下单语义、影子执行引擎、OANDA 账户/凭证细节、货币换算落地。这些属 [Development_plan.md](Development_plan.md) 的 Stage E 范畴。

---

## 2. 文档地图与版本清理（Step 1）

### 2.1 版本清理（observed → **已执行 2026-11-27**）

| 文件 | 问题 | 处置（已执行） |
| :--- | :--- | :--- |
| `Global_Portfolio_Router_v1_1_3.md` | 文件名 `v1.1.3` / 文件头 `v1.1.4` / §4 指向不存在的 `_v1_1_3.py`（三处漂移） | **已删除**（只保留 v1.1.5） |
| [Global_Portfolio_Router_v1_1_5.md](Global_Portfolio_Router_v1_1_5.md) | 原指向尚不存在的 `global_portfolio_router_v1_1_5.py` | 保留为**路由层唯一权威 spec**；对应实现已于 Step 2 落地 |
| `global_portfolio_router.py` | 同源旧副本（Meta 可"整包提升参数"） | **已删除** |
| `global_portfolio_router_v1_1_4.py` | 过渡副本；v1.1.5 §8 九项缺口未实现 | **已退役**（Step 2 由 `_v1_1_5.py` 取代，原为 git-tracked，可还原） |
| `global_portfolio_router_v1_1_5.py` | v1.1.4 基线 + v1.1.5 §8 九项补齐（毛敞口 / 通用覆盖率 / 相关性保守阻断 / H1 成本归一 / 默认时间型 EMA / 被拒账本 / 影子真实退出 / 货币换算 / WLS 斜率） | **唯一实现基线**（Step 2 已落地，自测 P3–P16 全绿） |
| `trading_data.yml` | 的 `pairs:` 与 `mc_data.yml` **完全重复** | **已删除**；路由器 P9 已改读 `mc_data.yml` |

> 注：本轮的物理删除均**可经 git 还原**（三份文件原为 git-tracked）。

### 2.2 版本权威链（intended）

```
Global_Portfolio_Router_v1_1_5.md   ← 路由层唯一权威 spec（行为目标）
        └─ 实现基线：global_portfolio_router_v1_1_5.py（Step 2 已落地；v1.1.4 已退役）
数据源：mc_data.yml                  ← 货币对唯一源（28 对，与 MC 价格对齐；路由器 P9 已读）
Development_plan.md                 ← 定仓 / 出场仲裁 收纳母文档（Doc A / Doc B）
本文件（Portfolio_Master_Plan.md）   ← 跨层统一索引 + 范式切换 + 三痛点路线图
```

### 2.3 清理纪律

- **只做版本号 / 路径 / 状态行对齐**，不修改语义内容（语义变更走各文档自己的修订记录）。
- 不自动把任何 `DRAFT` 改为 `APPROVED`。
- 归档旧副本时**保留文件**（加 `_archived` 后缀或移入 `docs/archive/`），不物理删除，便于追溯。

---

## 3. 统盘选择：分组 → 单一全局横截面

### 3.1 现状（observed）

| 机制 | 位置 | 角色 |
| :--- | :--- | :--- |
| **分组**（按报价货币） | `config_bot_v3.py::STRATEGY_GROUPS`（JPY/USD/CHF，各带 `tag_prefix`、`MIN_*` 门槛） | 信号来源 / 标签；旧"跨组互斥 `CROSS_GROUP_MUTEX_*`"已 `DEPRECATED` |
| **全局选择**（已在用） | `scheduled_runner_v3.py::_pick_global_basket` | 汇总全部组信号 → 按 `abs(strength_score) × MC权重` 排序 → 贪心取 Top-N，施加**净敞口 cap** + **base 论点冲突**检查 |
| **平行实现**（未接线） | `global_portfolio_router*.py` | 另一套"全局路由器"：货币强度 Z-score → 横截面打分 → 敞口/相关护栏 → Top-1。**仓内无任何生产代码导入** |
| **强度包**（可复用） | `currency_strength/`（`composite_strength` / `zscore` / `rank` / `breadth` / `dispersion`） | 已有独立货币强度能力 + 测试 |

### 3.2 核心冲突（intended 需要收敛的点）

- **分组范式**的隐含前提：按报价货币切分，各自独立决策。
- **横截面范式**的隐含前提：货币强度 / Z-score **只有在全宇宙一起算时才成立**（部分宇宙污染 Z-score）。→ **分组与横截面在数学上对立。**
- 结论：**分组降级为"信号来源/标签"，决策边界上移到全局横截面。**

### 3.3 收敛目标（intended）

1. **唯一权威选择器**：明确"统盘选择"的单一实现（建议：以 `_pick_global_basket` 为**执行器**，路由器作为其**意图/评分前端**）。
2. **单一强度真相源**：强度计算统一走 `currency_strength/` 包，路由器**不自造第二套强度**。
3. **可比口径**：见 §4 的统一 `HoldingScore`。

> ⚠️ 在 §3.3 的"唯一权威选择器"敲定前，**不得**把 `GlobalPortfolioRouter` 接入 runner —— 否则出现"两套真相"。

---

## 4. 存菁去芜（本轮核心 / proposed）

### 4.1 心法：把 MAX_OPEN 一个数，拆成三个决策

| 决策 | 问题 | 现状 | 归属 |
| :--- | :--- | :--- | :--- |
| ① **入池** | 候选够不够格？ | 已有（alignment / 敞口 / 相关性门禁） | 入场门禁 |
| ② **轮换** | 满了要不要**强换弱**？ | **缺** | 存菁去芜算法（本节） |
| ③ **留池** | 在仓该不该**主动去芜**？ | 部分（Exit Arbitration） | Doc B 出场仲裁 |

### 4.2 Owner 裁决（已确认）

- **MAX_OPEN 倾向放开硬上限**：38 对不会都最佳，实际入选很少。
- **真正的命门是"收缩/去芜条件"** —— 至今缺一套"存菁去芜"策略。
- **排序口径：统一 `HoldingScore`**（在仓与候选同尺可排）。

### 4.3 统一评分（proposed）

```
候选/在仓统一评分（同源 final_score 扩展）：
  SignalScore   = w_raw·Raw + w_align·tanh(Align) + w_space·Space + w_cost·Cost   # 现有 final_score
  ProgressScore = 未实现 R 倍数            # 候选 = 0；在仓才有
  DecayScore    = −max(0, 持有时长/预期时长 − 1)                                  # 时间衰减
  CorrPenalty   = 与其他在仓的平均 |corr|
  ─────────────────────────────────────────
  HoldingScore  = wS·Signal + wP·Progress + wD·Decay − wC·Corr
```

**有效开仓数（涌现，非写死）**：

```
K_effective = min( MAX_OPEN_HARD,
                   floor(总风险预算 / 单笔风险%),
                   净敞口 cap,
                   毛敞口 cap )        # 取最紧一项
```

**选择**：`在仓 ∪ 候选` 按 `HoldingScore` 排序，取前 `K_effective`；被挤出者 = "芜"。

### 4.4 收缩 / 去芜触发条件（proposed —— 回答"收缩条件是什么"）

| # | 触发 | 机制 | 效果 |
| :- | :--- | :--- | :--- |
| C1 | **相关性升高** | 同簇只留最强 | 同质仓位回撤 |
| C2 | **全局风险 regime 转坏** | MC = CONSOLIDATION / 高波动 | `K` 自动降 |
| C3 | **净值回撤** | 风险预算缩水 | `K` 自动降（预算驱动） |
| C4 | **论点衰减** | `alignment` 翻负 → Weak-Exit | 自然离场 |

> 关键：**四条全是"涌现"，不是拍脑袋的固定阈值**。因此"扩大 vs 收缩"之争自动消解。

### 4.5 强换弱 / 轮换（proposed —— 回答"新最佳被拦"）

```
若  new.HoldingScore > 最弱在仓.HoldingScore + Δ_rot
   且  最弱在仓 不在受保护态（紧急 / 移动止盈 / 正 R 保护）
   且  换仓净收益 > 0（扣点差 + 离场成本）
→ 先平最弱，再开 new（平仓交 Exit Arbitration 决策）
```

- **保护态优先级**直接复用 Doc B：`L0 Emergency > L1 Broker SL/TP > L2 Risk-momentum > L3 Weak-exit > L4 Scale-out`。
- **防抖（churn）**：`Δ_rot` margin + 滞后（hysteresis）+ "净收益 > 0"闸门；禁止为略好的新仓换掉正在盈利的仓。

### 4.6 选谁做"最弱"（proposed 输出物）

每个周期输出**存菁去芜遥测**（只记录，不动作，Phase 1）：
- 全部在仓 + 候选的 `HoldingScore` 明细与排名；
- 被挤出名单（若执行轮换会换掉谁）；
- 各收缩条件 C1–C4 的**当前取值**与是否触发。

> 目的：让"未来"的收缩阈值（`Δ_rot`、`wS/wP/wD/wC`）**由数据决定**，而非现在拍板。

---

## 5. 动态调参闭环（数据驱动 / proposed）

### 5.1 Owner 主张（已表达）

> "等影子 logs / CSV 积累多了，设计一套算法分析。"

→ 本节的**节奏即由此决定**：**先积累数据，后设计分析算法**。**不提前设定** `min_alignment / K / Δ_rot` 的值。

### 5.2 闭环三件套（proposed）

| 组件 | 作用 | 依赖 |
| :--- | :--- | :--- |
| **① Rejected Book**（被拒交易账本） | 每个被拒候选记：假设入场(价/时刻) → N 根后**假设盈亏** | 回答"被拒的若开了会不会赢"，用于调 `min_alignment / max_corr / K`。**v1.1.5 §8 已标为关键依赖** |
| **② Shadow Trials**（参数 A/B） | 多套参数（`min_alignment / w_align / w_cost / K / PER_PAIR 周期权重`）在**同一快照**并行跑 | 需 §1 的快照一致性 + 覆盖率门禁 |
| **③ Meta Promote**（晋升护栏） | 连续 3 窗胜出 → **逐参**晋升 Live；样本 `n ≥ 20/窗`；**晋升后变差自动回滚** | 依赖 ①② 的数据 |

### 5.3 与 shadow 现状的接口

- Stage C 影子遥测已获 Owner 放行（**只写日志、不改单**，独立存储；见 [Development_plan.md](Development_plan.md) §6）。
- 本节的 Rejected Book / HoldingScore 遥测**沿用同一纪律**：独立存储、不触生产 `risk_state.json`。

---

## 6. 策略灵活性（proposed）

### 6.1 现状

- 只有**一套策略**；路由器的 `raw_score` 是**死输入**。

### 6.2 改造点很小 —— `raw_score` 本就是插口

```
策略注册表：[趋势, 均值回归, 突破, carry/动量, ...]
Regime 分类：MC regime × 强度离散度 × 波动 → 决定各策略权重
raw_score = Σ 策略权重 × 各策略分
```

- "**动态调策略**" = 调**策略权重**（不是重写策略）；策略权重**同样可进 §5 的 Shadow/Meta 晋升循环**。
- **先做 2 套**（趋势 + 均值回归）验证 regime 路由跑通，再扩。

---

## 7. 28 对白名单 + 接口边界（proposed）

### 7.1 白名单扩展（observed → intended）

| 源 | 当前 | 目标 |
| :--- | :--- | :--- |
| `config_bot_v3.py::ALL_PAIRS` | **12 对**（majors + JPY 叉盘） | **28 对** |
| `config.py::TRADE_PAIRS` | **4 对**（`*_JPY`） | 随 ALL_PAIRS 扩展 |
| `mc_data.yml::pairs` | **28 对**（与 MC 价格对齐；路由器 P9 已读） | **唯一宇宙源**，避免多处硬编码 |
| ~~`trading_data.yml`~~ | 已删除（与 `mc_data.yml` 的 `pairs:` 完全重复） | — |
| 路由器 `_LEGACY_UNIVERSE` | 硬编码副本（P9 的 fallback） | 改由 `mc_data.yml` 单源（P9 已具备） |

> **已定（Owner 2026-11-27）**：货币对**蓝本 = `mc_data.yml`**（28 对）；`trading_data.yml` 已删。**M-1 关闭**（目标宇宙 = 28 对，单一源 = `mc_data.yml`）。

### 7.2 接口边界（intended）

- **入谈**：路由器/强度 → `_pick_global_basket`（唯一选择器）。
- **出谈**：所有交易动作（开 / 平 / 减）走项目**统一执行出口**（实时校验 + TOCTOU 防护），禁止路由器自带 `place_market_order`。
- **与 Doc B 的链接**：`L3 Weak-exit` 与 §4 的 C4（论点衰减）语义对齐；轮换的"平最弱"必须经 Doc B 仲裁。

---

## 8. 路线图（Step 1 → 2 → 3）

```
Step 1  文档统一（本总纲 + §2 版本清理）        ← 冻结 spec
   ↓
Step 2  脚本对齐文档（Phase 1：以文档为准）      ← 逐条对齐 / 显式标 pending
   ↓
Step 3  接入主程序（路由器 → _pick_global_basket → 统一执行出口）
```

**各步验收条件（proposed）：**

| 阶段 | 交付 | 验收 |
| :--- | :--- | :--- |
| **Step 1** | 本总纲 + 版本清理 | 文档间无版本/路径冲突；权威链唯一 |
| **Step 2** | 脚本对齐 | 逐条对照 v1.1.5；差异要么修要么标 `pending`（含理由） |
| **Step 3** | 接入 | 统一选择器生效；走统一执行出口；无"两套真相" |

**Step 3 进展（2026-11-27）**：**3a 只读影子已接入** —— `router_shadow_bridge.py`（15 测试全绿）+ `scheduled_runner_v3.py` 开关 `ROUTER_SHADOW`（默认 off）在 `_pick_global_basket` 之后对比路由层评分前端；执行器一字未改、零回归（全量对照 16F/194P 一致）。**B（数据保真）已落地**：`custom_strategy_v3.py` 信号附加真实 `atr`/`spread`，桥接据此计算 `space_score`（`SPACE_REL_CAP=0.5%`）与 `cost_score`（复用 router 的 `live_cost_score`），不再喂中性 0；`returns_series` 仍为已知缺口。**practice/live 影子观察期已开启**（`run.env: ROUTER_SHADOW=true`；纯只读、fail-safe；观察 `agreed` 比例与 `data_gaps`）。**3b**（用路由器评分替换排序键）待观察期无回归后再灰度。

**Step 2 已知待对齐项**（源自 v1.1.5 §7/§8）：毛敞口 cap、通用覆盖率门禁、相关性**保守阻断**（替换 fail-open）、H1 成本归一、默认时间型 EMA、被拒交易结果追踪、影子真实退出、货币换算、WLS 斜率。**已实现 2026-11-27**：全部落地于 `global_portfolio_router_v1_1_5.py`，py_compile + 自测 P3–P16 通过；新开关默认关，`TIME_BASED_EMA` 默认开。

---

## 9. Owner 决策汇总（本轮）

| 编号 | 裁定 | 落点 | 状态 |
| :--- | :--- | :--- | :--- |
| **D1** | 仓位以 Unit 计；live/shadow/demo 本轮不管 | §1 | **CONFIRMED** |
| **D2** | V1.1.5 为路由层唯一权威；建本总纲 | §2 | **CONFIRMED** |
| **D3** | 第一阶段：脚本对齐文档 | §8 | **CONFIRMED** |
| **D4** | 文档落地后接入主程序 | §8 | **CONFIRMED** |
| **P1** | MAX_OPEN 倾向放开硬上限；命门是**收缩/去芜条件** | §4 | **CONFIRMED** |
| **P2** | 排序口径 = **统一 HoldingScore** | §4.3 | **CONFIRMED** |
| **P3** | 强度：**多时间轴分解 + 复用 `currency_strength` 包** | §3.3 | **CONFIRMED** |
| **P4** | 动态调参：**先积累 logs/CSV，后设计分析算法** | §5.1 | **CONFIRMED** |

---

## 10. 先验值表（proposed —— 待 Owner 批准）

> 纪律：以下均为**工程初始值**，非最优；**不提前生效**，随 §5 数据积累后再定。

| 参数 | 先验值 | 说明 |
| :--- | :--- | :--- |
| `MAX_OPEN_HARD` | 待定（倾向放宽） | 仅作极端保护上限；有效数由 `K_effective` 涌现 |
| `单笔风险%` | 沿用 Doc A 的 `Risk% = 0.5%`（工程初始值） | **不得自行提高**（Doc A A-01） |
| `Δ_rot`（轮换 margin） | 待数据 | 防抖，先由 §4.6 遥测观察后定 |
| `wS / wP / wD / wC` | 待数据 | HoldingScore 权重，先遥测后定 |
| `max_ccy_units`（净） | 2（现网基础值） | 沿用 |
| `max_ccy_gross_units`（毛） | 3（v1.1.5 建议） | v1.1.5 新增，**已实现**（`SW.GROSS_EXPOSURE_CAP` 默认关） |
| `max_corr` | 0.70 | 沿用 |
| 强度时间轴权重（短/中/长） | 待数据 | §6 需先定时间轴分解口径 |

---

## 11. OPEN 清单（未决 / 前置 —— 禁止当作已知）

| # | 事项 | 关联 | 卡点 |
| :--- | :--- | :--- | :--- |
| **M-1** | ~~**目标宇宙 = 28 还是 38？**~~ → **已关闭（2026-11-27）** | §7.1 | 蓝本 = `mc_data.yml`（28 对）；`trading_data.yml` 已删，单一宇宙源确立 |
| **M-2** | ~~**唯一权威选择器**未定~~ → **已裁定（2026-11-27）** | §3.3 | 执行器 = `_pick_global_basket`；路由器 = 评分前端（3a 影子已接入） |
| **M-3** | **强度单一真相源**未迁移 | §3.3 | 路由器强度 → `currency_strength/` 包 |
| **M-4** | **收缩阈值 / HoldingScore 权重**未定 | §4.4 / §10 | **故意延后到数据积累后**（P4） |
| **M-5** | **Rejected Book 未建** | §5.2 | 脚本内 `ShadowRejectedBook` 已实现（`SW.SHADOW_REJECTED`）；仍缺**生产数据回流**与调参闭环 |
| **M-6** | **策略注册表未建** | §6 | 先 2 套验证 |
| **M-7** | 记录类遥测（HoldingScore 明细）**未建** | §4.6 | 依赖 Step 2 落地 |

---

## 12. 修订记录

| 版本 | 日期 | 变更 |
| :--- | :--- | :--- |
| v0.1 | 2026-11-27 | 初建：汇总 Owner 决策 D1–D4 / P1–P4；范式切换（分组→横截面）；存菁去芜（统一 HoldingScore + 涌现 K + 4 条收缩条件 + 强换弱）；动态调参闭环（数据驱动）；策略灵活性；28 对白名单与接口；三阶段路线图；OPEN 清单 |
| v0.2 | 2026-11-27 | Step 2 落地：`global_portfolio_router_v1_1_5.py` 实现 v1.1.5 §8 九项缺口（py_compile + 自测 P3–P16 通过）；v1.1.4 退役；更新权威链与待对齐项状态 |
| v0.3 | 2026-11-27 | Step 3a 接入：只读 Router 影子（`router_shadow_bridge.py`，12 测试全绿）挂入 `scheduled_runner_v3.py`（开关 `ROUTER_SHADOW` 默认 off）；执行器未改、零回归；§11 M-2 标记已裁定（执行器 = basket，路由器 = 评分前端） |
| v0.4 | 2026-11-27 | 文档一致性修复：§11 M-1 与 §7.1 对齐（标记**已关闭**，蓝本 = `mc_data.yml` 28 对） |
| v0.5 | 2026-11-27 | Step 3a + B：信号保真（真实 `atr`/`spread` → `space_score`/`cost_score`，桥接测试 12→15）；开启 `ROUTER_SHADOW=true` 影子观察期（practice/live，只读 fail-safe） |
