# Global Portfolio Router v1.1.5 理解报告

**报告日期:** 2026-11-27
**报告对象:** `docs/Global_Portfolio_Router_v1_1_5.md`（对照上一版 `docs/Global_Portfolio_Router_v1_1_3.md`）
**被评估代码:** `global_portfolio_router.py`、`global_portfolio_router_v1_1_4.py`
**报告范围:**
1. v1.1.5 设计文档理解；
2. Python 脚本是否符合设计理念；
3. 项目整体架构分析（货币对分组 → 统盘选择）。

> **后续状态更新（2026-11-27，清理已执行）：** 本报告为**时点分析**，写作时下述对照文件均存在。同日按 Owner 指令完成版本清理，以下文件**已物理删除**（均可经 git 还原）：`docs/Global_Portfolio_Router_v1_1_3.md`、`global_portfolio_router.py`、`trading_data.yml`（其 `pairs:` 与 `mc_data.yml` 完全重复）。唯一实现基线 = `global_portfolio_router_v1_1_4.py`（P9 已改读 `mc_data.yml`）；唯一路由层 spec = `docs/Global_Portfolio_Router_v1_1_5.md`。文中对上述已删文件的引用**仅作历史对照**，不再指向现存文件。详见 [Portfolio_Master_Plan.md](Portfolio_Master_Plan.md) §2.1。

---

## 0. 结论速览（TL;DR）

- **v1.1.5 本质是一份"加固规格书"**，不是新代码。它明确声明：配套代码 `global_portfolio_router_v1_1_5.py` **尚不存在**，仓库里可用的实现是 v1.1.4，且 v1.1.5 中描述的若干安全项**尚未实现、未对实盘验证**。
- **文档对代码的自评是诚实的**：逐条核对后，v1.1.5 §7/§8 中"v1.1.4 已实现 / 未实现"的判断与 `global_portfolio_router_v1_1_4.py` 实际代码**完全一致**（见第 3 节对照表）。
- **两个脚本是同源的两个副本，且存在漂移**：`global_portfolio_router_v1_1_4.py` 是较新、较严格的版本（新增 P8 收盘触发去重、P9 从 `trading_data.yml` 读货币对、更严格的 Meta 门禁）；`global_portfolio_router.py` 是较旧副本（Meta 无 STRICT_PROMOTION 门禁、允许"整包提升"）。**两份并存本身就是设计理念所反对的"漂移风险"**。
- **架构层面存在一个必须先解决的矛盾**：项目实际上**已经有**一个"统盘选择"入口 —— `scheduled_runner_v3.py::_pick_global_basket()`；而 `GlobalPortfolioRouter` 是**另一个独立、未接线**的同类实现。二者逻辑重叠但口径不同。用户提出的"从分组走向统盘考虑选哪个货币对"，**核心不是新写一个路由器，而是统一这两个入口、并复用已有的 `currency_strength/` 包**。

---

## 1. 文档定位与版本核对

### 1.1 v1.1.5 的自我定位

| 项目 | v1.1.5 声明 |
| :--- | :--- |
| 性质 | 设计/评审文档，**下一步代码修订**的规格 |
| 配套代码 | `global_portfolio_router_v1_1_5.py` ——**不存在** |
| 可用实现 | `global_portfolio_router_v1_1_4.py` |
| 状态 | 默认 practice + `DRY_RUN`；有离线自测；**未对 OANDA 实盘响应验证** |

**关键要求（§0 状态栏原文语义）：** 在配套代码补齐并通过评审前，**不得把 v1.1.5 描述的行为当作已实现**。

### 1.2 文档自身的版本不一致（需修订）

| 文件 | 文件头 `Version` | 指向代码 | 备注 |
| :--- | :--- | :--- | :--- |
| `Global_Portfolio_Router_v1_1_3.md` | **v1.1.4** | §0 指向 `global_portfolio_router_v1_1_4.py`，但 **§4 仍写 `global_portfolio_router_v1_1_3.py`** | 文件名/文件头/§4 三处不一致，且 `*_v1_1_3.py` 不存在 |
| `Global_Portfolio_Router_v1_1_5.md` | v1.1.5 | 指向尚不存在的 `global_portfolio_router_v1_1_5.py` | 文件头自洽 |

**判断：** 名为 v1.1.3 的文档实际内容已是"v1.1.4 评审稿"，其 §4 遗留了一句过时的代码路径引用。这属于文档漂移，建议在 v1.1.5 定稿时一并修正，否则后续会继续误导读者对"当前实现是哪个版本"的判断。

---

## 2. v1.1.5 设计理念解读

### 2.1 五层架构（§1）

```
Raw Signal Layer（28 货币对，既有策略）
        ↓
[1] Global Snapshot Barrier       全局快照屏障
        ↓
[2] Currency Strength            货币强度（衰减 + 多周期 + 按对周期）
        ↓
[3] Cross-Sectional Scoring      横截面打分
        ↓
[4] Exposure & Correlation Guard 敞口与相关性护栏
        ↓
[5] Shadow + Meta Dynamic Tuning 影子 + 元动态调参（不下单，只纠偏）
        ↓
Execution Engine（OANDA v20，Top-1 only）
```

**核心运行规则（§1 末尾）：** Live 只用**一套参数**；Shadow 用同一快照并行跑 N 套参数；Meta 每个窗口比对；**只有同时满足 Sharpe 与回撤优势、且连续 3 个窗口胜出的 Shadow，才逐个参数提升到 Live**。

> 理解要点：这是一套"**单实盘 / 多影子 / 证据驱动晋升**"的动态调参架构，风险控制的核心假设是"**Live 保守、晋升缓慢**"。

### 2.2 时间框架是"一等公民"（§2）

- **周期跟着货币对走**（§2.1）：每个对有自己的 primary / confirm / 各周期权重（`PAIR_TIMEFRAME_CONFIG`）。低波动 majors 用 H1，高波动交叉盘用 H4，商品货币 H1 + H4 过滤，JPY 盘看东京/伦敦重叠。未列出的对用 `DEFAULT`（H1 主 / H4 确认）。
- **决策滴答是"按对"而非"全局"**（§2.2）：理想设计是"某对的主周期 K 线收盘时，才对该对做决策"，且快照只用**已收盘**K 线，避免"未走完的 H4"与"已走完的 H1"被直接比较。
- **多周期强度**（§2.3）：
  $$
  M_{X/Y,tf}=\frac{Slope(X/Y,tf)}{ATR(X/Y,tf)},\quad
  M_{X/Y}=\sum_{tf} w_{tf,pair}\cdot M_{X/Y,tf},\quad
  S_{ccy,t}=\alpha S_{ccy,new}+(1-\alpha)S_{ccy,t-1}
  $$
  然后**跨货币 Z-score 标准化**（必开，否则 GBP/JPY 的动量会以约 3×EUR/USD 的量级污染整个矩阵）。
- **周期错配惩罚**（§2.4）：信号周期 ≠ 该对主周期时，`space_score` 乘系数（主周期 1.0 / confirm 0.85 / 其他 0.5）。例如"GBP/JPY 上的 M15 信号"会被压到 0.5，需极强对齐才能胜出——**这是抑制高波动交叉盘过度交易的主要手段**。

### 2.3 核心算法（§3）

**最终得分（§3.1）：**
$$
FinalScore=w_{raw}\cdot Raw+w_{align}\cdot\tanh(Align)+w_{space}\cdot Space+w_{cost}\cdot CostScore
$$
- `Align` 用 `tanh` 压缩到 (-1,1)，防止单次 JPY 冲击把其他因子全部压垮；
- **硬否决**：`Align < min_alignment` 即拒单；
- `CostScore∈[0,1]`，**越大越便宜**（因此是"加"而非"减"）；
- 权重默认 `(0.40, 0.35, 0.15, 0.10)`；日内建议把 `w_cost` 提到 0.15–0.20 并同幅下调其他项。

**敞口护栏（§3.2）：** 数"持仓笔数"是错的，因为不同货币对里"一笔"并不等价。
- **Legacy（已弃用）**：敞口 = size × ATR，单位与 cap 不一致，勿用；
- **Risk-Unit 模式（推荐）**：每笔持仓记为 **±1 风险单位**（假设固定风险定仓）。**净敞口**为带符号求和；候选若把任一货币净敞口推过 `max_ccy_units` 则拒单。默认 `max_ccy_units=2.0`。
- **v1.1.5 新增目标 —— 毛敞口上限 `max_ccy_gross_units`（建议默认 3.0）**：防止"对冲型持仓"掩盖真实风险（如 EUR/USD LONG 与 USD/CHF SHORT 都含 USD）。**这是 v1.1.5 相对 v1.1.4 最重要的新增护栏**（v1.1.4 只有净敞口）。
- **滚动相关性阻断**：候选 vs 每个在仓；目标口径是"**对齐的、已收盘的、统一 H1 的收益序列**"，方向调整后 `effective_corr > max_corr(0.70)` 即跳过。**数据缺失目标行为是"保守阻断候选"，而 v1.1.4 是"跳过该仓的相关性检查"（fail-open）**。

### 2.4 计划中的代码接口与开关（§4）

```python
router = GlobalPortfolioRouter(RouterParams())
best = router.select_best_trade(candidates, pair_momentums, open_positions)
shadow = ShadowEngine([RouterParams(min_alignment=0.2), RouterParams(min_alignment=0.3)])
meta = MetaController(router, shadow)
meta.evaluate_window(live_metrics, [shadow_metrics_0, shadow_metrics_1], field_name="min_alignment")
decision_tick(router, candidates, shadow)   # 下单那行被注释掉
```

开关（`class SW`）默认全关（§4.1）。v1.1.5 相对 v1.1.4 的开关口径变化：
- `TIME_BASED_EMA`：**目标改为默认开启**（v1.1.4 默认关）；
- `PARALLEL_SNAPSHOT`：v1.1.4 中**只有这条路径**才强制覆盖率门禁；
- `DRY_RUN`：**默认开**。

### 2.5 OANDA 集成与上线计划（§5、§6）

- 命名 `GBP_JPY` ↔ `GBP/JPY` 只在边界转换；凭证走环境变量；非 practice 下单还需 `ALLOW_LIVE_ORDERS=YES`。
- **快照完整性目标**：顺序/并行取数后都要做覆盖率门禁，且要**按货币可代表性**衡量（24/28 的门槛仍可能漏掉 4 个对）；**v1.1.4 仅在 `PARALLEL_SNAPSHOT` 开时检查**。
- **速率限制**：单次 tick 约 60–80 次 API 请求，目标需实现 **tick 内共享 K 线缓存 + 429 指数退避重试**（**当前均未实现、未验证**）。
- 下单止损 `SL_ATR_MULT × ATR`（JPY 3 位小数，其余 5 位），`REQUIRE_SL` 强制；`size_units = risk/stop`；**非账户货币报价的对未实现汇率转换**。
- **上线计划（§6 已重写）**：v1.1.4 的顺序**不足以覆盖 v1.1.5 目标**；在待办项实现且离线自测通过前，保持 practice + `DRY_RUN`。

### 2.6 已知缺口（§8）

**未建：** ① 按 K 线收盘的决策触发（`decision_tick` 由调用方驱动）；② **被拒交易的结果追踪**（Week 2 分析的关键依赖）；③ 毛敞口上限；④ 通用快照覆盖率门禁；⑤ 统一 H1 收益序列 + 保守缺失处理；⑥ H1 ATR 成本归一 + 默认时间型 EMA；⑦ 影子退出（当前按固定 tick 数 `SHADOW_HOLD_STEPS` 过期）；⑧ `size_units` 货币换算；⑨ 加权最小二乘斜率（当前为 OLS）。

**未验证：** 一切对 OANDA 实盘响应的行为；以及 `COST_CAP / SHADOW_HOLD_STEPS / SL_ATR_MULT / max_ccy_units / max_ccy_gross_units` 等假设型可调参。

**已知边界：** **Top-1 only** —— 单次 tick 只选最优一笔，是**刻意的风控选择，不是 bug**。

---

## 3. v1.1.3 → v1.1.5 的差异清单

v1.1.5 相对 v1.1.3（内部实为 v1.1.4 稿）**没有推翻架构**，而是同一架构的"加固 + 诚实化"：

| # | 维度 | v1.1.3（v1.1.4 稿） | v1.1.5 |
| :- | :--- | :--- | :--- |
| 1 | 定位声明 | 描述"本仓库 v1.1.4 代码" | 明确：v1.1.5 行为**尚未实现**；可用实现是 v1.1.4；勿混用 |
| 2 | 敞口护栏 | 仅**净**敞口 `max_ccy_units` | **新增毛敞口** `max_ccy_gross_units`（建议 3.0），净+毛双 cap |
| 3 | 相关性 | 缺失数据时**跳过**（fail-open） | 目标：缺失时**保守阻断候选**；且要统一 H1 对齐收益 |
| 4 | 快照覆盖率 | 仅 `PARALLEL_SNAPSHOT` 路径检查 24/28 | 目标：**两条路径都检查**，且按货币代表性定义门槛 |
| 5 | 成本归一 | 用候选自带 ATR | 目标：**统一用 ATR_H1**（即便 H1 不是该对动量周期） |
| 6 | EMA | 默认 per-call `alpha=0.15` | 目标：**默认时间型 EMA**（`HALF_LIFE_SEC`）；Z-score 标准差下界 `max(std,1e-6)` |
| 7 | 被拒交易 | 只记原因（"是 Shadow 归因的原料"） | 明确为**关键依赖**：需记录假设入场、出场视界/规则、成本、结果 |
| 8 | 速率限制 | 未提 | 明确要求**共享 K 线缓存 + 429 退避**（未实现） |
| 9 | 上线顺序 | 3 周计划视为充分 | 判定 v1.1.4 顺序**不足**，先补待办项再 practice |

**一句话：** v1.1.5 = 把 v1.1.4 里"说了但没做"的安全项显式列为"待办 + 明确不该此时上线"，并新增了"毛敞口"这一实质性护栏。

---

## 4. Python 脚本是否符合设计理念

### 4.1 两个脚本的关系（同源漂移）

两者文件头注释**逐字相同**，都是 "patched build (all patches switch-gated, default OFF)"，均自称 v1.1.4。差异如下（`diff global_portfolio_router.py global_portfolio_router_v1_1_4.py` 归纳）：

| 差异点 | `global_portfolio_router.py`（旧副本） | `global_portfolio_router_v1_1_4.py`（新副本） |
| :--- | :--- | :--- |
| 货币对来源 | 硬编码 `UNIVERSE`（第 78–85 行） | **P9**：`load_universe()` 从 `trading_data.yml` 读，含 `_normalize_pair` 归一、去重、拒绝非 OANDA 方位、缺文件/空表/低于 `MIN_COVERAGE` 直接抛错，**无静默回退** |
| 收盘触发 P8 | 有开关，`_is_tf_just_closed` 用 `minute/hour` 朴素判断 | 改用 **epoch 对齐**（`timestamp % interval <= tol`），并新增 `_tf_close_epoch` + 全局 `_LAST_PROCESSED_BAR_CLOSE`，**同一次收盘每进程只处理一次** |
| Meta 门禁 | `evaluate_window` **无** `STRICT_PROMOTION` 门禁；`field_name=None` 时**整包**替换参数 | `evaluate_window` 内部先 `if not SW.STRICT_PROMOTION: return None`；`field_name` **必填**、校验字段存在、校验 shadow 数量 |
| 收尾自测 | 无 P9 测试 | 增加 P9 用例（脏文件去重/拒绝、坏文件抛错、缺文件抛错） |

**结论：** `global_portfolio_router.py` 是较旧/较松的副本，`global_portfolio_router_v1_1_4.py` 是较新/较严的副本。**两份并存本身违反设计理念**（v1.1.5 追求"单一可信实现 + 文档不漂移"）。建议：保留 `global_portfolio_router_v1_1_4.py` 为唯一基线，删除或明确归档旧副本。

### 4.2 逐条核对：v1.1.5 对 v1.1.4 的"已实现/未实现"判断是否属实

以 `global_portfolio_router_v1_1_4.py` 为准逐条核对：

| v1.1.5 声明 | 代码位置 | 核对结果 |
| :--- | :--- | :--- |
| 净敞口 cap 存在、**毛敞口**未实现 | `select_best_trade` 只算 `abs(exposure+delta) > cap`；`_get_weighted_exposure` 返回带符号净值 | ✅ 属实（只有净） |
| 覆盖率门禁**仅在并行路径** | `build_market_snapshot` 只在 `if SW.PARALLEL_SNAPSHOT:` 分支内检查 `MIN_COVERAGE`；顺序分支直接返回 | ✅ 属实 |
| Z-score 用 `std + 1e-9` | `compute_currency_strength`：`vals.std() + 1e-9` | ✅ 属实 |
| `TIME_BASED_EMA` 默认关 | `class SW: TIME_BASED_EMA = False` | ✅ 属实 |
| 成本用**候选 ATR**，非 H1 | `decision_tick`：`live_cost_score(spreads[c.pair], c.atr)` | ✅ 属实 |
| 相关性缺失数据时 **fail-open** | `_is_correlated`：`n<20: continue`；空数组也 `continue` | ✅ 属实 |
| 收盘触发未建、`decision_tick` 由调用方驱动 | `PER_PAIR_BAR_CLOSE=False` 默认；否则走调用方传入的 `now_utc` | ✅ 属实 |
| 无被拒交易结果追踪 | 全文件无结果归因代码 | ✅ 属实 |
| 影子退出按固定 tick | `SHADOW_HOLD_STEPS=12`，`step()` 内 `p.age < SHADOW_HOLD_STEPS` | ✅ 属实 |
| `size_units` 无汇率换算 | docstring 明写"convert yourself" | ✅ 属实 |
| 斜率用 OLS | `np.polyfit(...)` | ✅ 属实 |
| 下单行被注释 | `decision_tick` 末尾 `place_market_order` 为注释 | ✅ 属实 |

**判断：文档对代码的自评准确、无夸大。** 这是一个"文档诚实、代码部分落地"的良性状态。

### 4.3 脚本是否符合"设计理念"

分两层看：

**(A) 与 v1.1.5 目标规格的符合度 = 部分符合（文档自己也承认）。**
- 符合：五层架构的**骨架**（快照 → 强度 → 横截面打分 → 敞口/相关护栏 → 影子/Meta）均在代码中有对应实体；安全默认（`DRY_RUN`、practice、环境变量凭证、`REQUIRE_SL`、`ALLOW_LIVE_ORDERS` 闸门）；一个开关一个开关上线的约束（`class SW` 全关）。
- 不符合（= v1.1.5 待办）：毛敞口、通用覆盖率、保守相关性缺失处理、H1 成本归一、默认时间型 EMA、被拒交易归因、影子真实退出、货币换算。**这些恰是"风控/可验证性"最关键的几项**，所以**当前脚本不能视为满足 v1.1.5**。

**(B) 与项目既有风控理念的符合度 = 存在理念冲突（见第 5 节）。**
- 项目既有理念（见 `project_memory` 与用户偏好）强调：**失败关闭（fail-closed）**、**统一执行出口/实时校验防 TOCTOU**、**影子并行是切 LIVE 的前置条件**。
- 路由器脚本中 `_is_correlated` 的缺失数据 **fail-open**、顺序快照路径**无覆盖率门禁**、`decision_tick` **直接旁路执行出口**（下单行注释、且未接入统一门禁）——**与"失败关闭/统一出口"理念冲突**。这正是 v1.1.5 §3.2/§5.4 要修的点。

### 4.4 代码层面的具体风险点（除文档已列外）

以下为阅读代码时发现的、文档未逐条点明的问题，供评审参考（**均未改动，仅报告**）：

1. **旧副本 `global_portfolio_router.py` 的 Meta 可"整包提升参数"**，与"一次只晋升一个参数以免归因混乱"的设计直接矛盾（新副本已修）。
2. **`compute_atr` legacy 分支 `np.std(np.diff(c))`** 与 `TRUE_ATR` 分支量纲不同，`LIVE_COST` 用错分支会显著改变 `cost_score`（文档提了，但风险等级值得标红）。
3. **`_LEGACY_UNIVERSE` 上方注释**称"有一个非法 `USDCAD` 条目为字节兼容而保留"，但实际列表中**并无该条目**——注释与代码不符（小的文档漂移）。
4. **P8 的 `_LAST_PROCESSED_BAR_CLOSE` 是进程级全局**：进程重启后若恰在同一 tolerance 窗口内，可能对**同一根收盘**再处理一次。
5. **`PAIR_TIMEFRAME_CONFIG` 未覆盖全部 28 对**，未列出的对落到 `DEFAULT`（H1/H4）——符合文档，但意味着"高波动对"若未登记就会以错误周期参与评分。
6. **`select_best_trade` 只返回 Top-1，不补位**：文档说是刻意设计，但代码注释未说明，易被误读为能力缺失。
7. **无任何模块被项目运行时导入**：`Grep` 全仓仅在这两个文件内出现 `GlobalPortfolioRouter/select_best_trade`，说明它**尚未接入 `scheduled_runner_v3.py`**（详见第 5 节）。

---

## 5. 项目整体架构分析：从"分组"到"统盘选择"

### 5.1 现状一：货币对按"报价货币"分组（分组范式）

- `config_bot_v3.py::STRATEGY_GROUPS`（约 330 行）把货币对按**报价货币**分为 `JPY / USD / CHF` 三组，每组带 `quote_ccy`、`tag_prefix`（如 `JPY-STRENGTH`）与自己的 `MIN_STRENGTH_PASSING_PAIRS / MIN_DOMINANT_PAIRS`。
- `config.py` 另有 `JPY_GROUP = [p for p in TRADE_PAIRS if p.endswith("_JPY")]`，供 runner/guardian"只在相关工具集上操作"。
- `config_bot_v3.py::D_STRATEGY_GROUPS` 是**按具体货币对**（如 `GBP_AUD`）配置日线模式的持仓/止损粒度。
- 旧的**跨组互斥** `CROSS_GROUP_MUTEX_*` 已标注 **DEPRECATED**。

### 5.2 现状二：其实已存在"统盘选择"入口（runner 内）

`scheduled_runner_v3.py::_pick_global_basket()`（约 4245 行）**已经在做跨组统盘选择**，其文档串明确写道：
> Rank all signals across ALL groups by `abs(strength_score) × MC weights`, then greedily pick top `max_entries` while respecting: (1) already-held (skip dup / flag reversal), (2) **cross-currency net exposure cap**, (3) **base-currency thesis contradiction**.

即：**把全部组的信号汇总 → 按 `|strength_score| × MC权重` 排序 → 贪心取 Top-N**，并施加：
1. 已持仓去重/反转幂等；
2. **跨货币净敞口 cap**（`CROSS_MAX_NET_PER_CCY`，基础 2，可按行情动态抬到 3/4；base 与 quote 两侧都累计都受限）；
3. **base 货币论点冲突**检查（只查 base，因 quote 侧符号是结构性的）。

**因此"统盘考虑选哪个货币对"在工程上并非从零开始** —— runner 已有该能力，且其互斥逻辑已从"分组互斥"演进为"基于敞口的全局挑选"。

### 5.3 现状三：还有一个"平行的"全局路由器（未接线）

`GlobalPortfolioRouter` 是**另一个同类目标的独立实现**：全局快照 → 货币强度 Z-score → 横截面打分 → 敞口/相关护栏 → Top-1。但它：
- **没有任何生产代码导入它**（全仓仅自引用）；
- 下单行被注释；
- 与 `_pick_global_basket` 口径不同（见下表）。

### 5.4 两套"统盘选择"的口径对比（核心矛盾）

| 维度 | `_pick_global_basket()`（runner 内，在用） | `GlobalPortfolioRouter`（独立，未接线） |
| :--- | :--- | :--- |
| 排序依据 | `abs(strength_score) × MC权重` | 加权 `Raw + tanh(Align) + Space + Cost` |
| 方向依据 | 组内策略已定的 `action` | 由**跨货币强度差** infer（`d×(S_base−S_quote)`） |
| 周期处理 | 组/策略内部决定 | **按对**指定 primary/confirm + 错配惩罚 |
| 敞口单位 | **整数风险单位/货币**（净），动态 cap | 净 cap（+目标毛 cap） |
| 相关性护栏 | 无 | 有（滚动 corr，当前 fail-open） |
| MC 融合 | 有（SEVERE/MODERATE 权重折减） | **无** |
| 选择数量 | Top-N（`max_entries`） | **Top-1 only** |
| 货币强度 | 各策略自算 | **统一 Z-score 强度矩阵** |
| 是否接线 | ✅ 在跑 | ❌ 未接线 |

**矛盾点：** 二者都声称"统盘"，但**强度口径、暴露口径、是否融合 MC、选几笔**都不一致。若两套并存运行，会出现"两套真相"。设计理念（v1.1.5 §5.4 的"统一口径"与用户偏好的"口径自洽"）要求**二选一并统一**。

### 5.5 "分组 vs 统盘"的本质冲突（为什么这是一次范式切换）

- **分组的隐含前提**：按报价货币把市场切成互不干扰的子集，各自独立决策。
- **路由器（统盘）的隐含前提**：货币强度 `/ Z-score across currencies` **只有在"全 28 对一起看"时才有意义**。文档 §2.3/§5.4 明确：**部分宇宙会污染 Z-score**。也就是说 —— **分组的做法与"跨货币横截面评分"在数学上是对立的**：分组越多、每组的对越少，强度估计越偏。
- 因此用户提出的"现在要统盘考虑选择交易哪一个货币对"，本质是：**把"分组作为决策边界"降级为"分组只作为信号来源/标签"，把决策边界上移到全局横截面**。

### 5.6 一个被忽视的复用点：`currency_strength/` 包

仓库已有一个独立的 `currency_strength/` Python 包，提供 `composite_strength`（含 rank/breadth/dispersion/contribution）、`zscore`、`percentile`、`gap`、`basket_weights`，并有完整的 unit/integration/regression 测试（`tests/`）。

- 路由器 `GlobalPortfolioRouter.compute_currency_strength` 的"跨货币 Z-score + 聚合"**与该包的职责高度重叠**。
- 设计理念要求"口径自洽、单一真相源" → **强度计算应统一走 `currency_strength/`，路由器不应自造第二套强度**。

---

## 6. 结论与建议

### 6.1 结论

1. **v1.1.5 是一份诚实的加固规格**，其"v1.1.4 已/未实现"的判断与代码**逐条吻合**；配套代码 `*_v1_1_5.py` 尚不存在。
2. **两个脚本同源漂移**：`global_portfolio_router_v1_1_4.py`（新、严）应为唯一基线；`global_portfolio_router.py`（旧、松）应归档或删除。
3. **脚本满足"架构骨架"，但未满足 v1.1.5 的风控/可验证性目标**（毛敞口、通用覆盖率、保守相关性、H1 成本、时间型 EMA、被拒交易归因、真实影子退出、货币换算）。
4. **脚本存在与项目既有理念（fail-closed / 统一执行出口）冲突的地方**：相关性 fail-open、顺序快照无门禁、旁路执行出口。
5. **架构上"统盘选择"已部分存在**（`_pick_global_basket`），路由器是**未接线的平行实现**；分组范式与横截面强度在数学上对立。**真正的任务是把两者收敛为一套口径自洽的全局选择。**

### 6.2 建议（按优先级，**仅为建议，未实施**）

- **P0 统一实现与口径**：先决定"统盘选择"的**唯一权威实现**（候选：以 `_pick_global_basket` 为执行器，路由器作为其"意图生成/评分"前端；或反之）。在此之前**不要**把 `GlobalPortfolioRouter` 接入 runner，以免出现两套真相。
- **P0 强度单一真相源**：路由器的 `compute_currency_strength` 改为复用 `currency_strength/` 包，消除重复与口径漂移。
- **P1 失败关闭**：把 v1.1.5 的"通用覆盖率门禁 + 相关性缺失保守阻断"落地，替换当前的 fail-open。
- **P1 毛敞口护栏**：实现 `max_ccy_gross_units`，与净敞口一起在统一入口检查。
- **P1 被拒交易归因**：这是 Week 2 的关键依赖，落地后才能评估 `min_alignment` 是否过严。
- **P2 文档收敛**：修正 `Global_Portfolio_Router_v1_1_3.md` 中 §4 的过时路径引用；统一版本号；仅保留一份可信代码文件。
- **P2 复用执行出口**：路由器未来的下单路径必须走项目"统一执行出口（实时校验 + TOCTOU 防护）"，而非自带的 `place_market_order`。

---

### 附：本报告核对的事实来源

- `docs/Global_Portfolio_Router_v1_1_5.md`、`docs/Global_Portfolio_Router_v1_1_3.md`
- `global_portfolio_router.py`、`global_portfolio_router_v1_1_4.py`
- `scheduled_runner_v3.py::_pick_global_basket`（约 4245 行）、`config.py`、`config_bot_v3.py::STRATEGY_GROUPS`（约 330 行）
- `currency_strength/src/currency_strength/{utils/composite_strength.py, normalization/zscore.py}`
- `trading_data.yml`（28 对，供 P9 读取）
