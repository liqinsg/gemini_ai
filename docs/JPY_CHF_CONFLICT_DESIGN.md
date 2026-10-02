# JPY–CHF 冲突问题：三方案设计说明书

> 生成日期：2026-10-02
> 作者：代码审查材料（供其他 LLM 审核）
> 状态：方案 C（当前）已实现；方案 A、B 为待评估备选
> 阅读前提：熟悉 scheduled_runner_v3.py 的分组架构、global strength matrix、
> JPY_REQUIRE_GLOBAL_EXTREME gate 三处位置。

---

## 0. 背景与问题陈述

### 0.1 当前架构
Runner 每 cycle 调用一次 `build_strength_matrix()` 生成 6 种货币（USD/EUR/GBP/AUD/JPY/CHF）
的 global strength score，存入 `_global_scores` dict，共享给所有 group。
JPY 组在 `_run_single_group()` 头部有两道 gate：

```
Layer 1 (runner-level)  : JPY_REQUIRE_GLOBAL_EXTREME — JPY 必须 rank 1 或 rank 6 才放行
Layer 2 (strategy-internal): JPY_REQUIRE_EXTREME_RANK — JPY TOP → 只 SELL；BOTTOM → 只 BUY
```

### 0.2 冲突的本质
CHF 和 JPY 分数几乎永远同向移动（二者都是传统避险货币）：

- RISK ON 环境：CHF 和 JPY 同时变弱（一起被抛）
- RISK OFF 环境：CHF 和 JPY 同时变强（一起被买）

结果是 global extreme gate 的 "1 或 6" 条件永远被"同类挤兑"干扰——
CHF 和 JPY 要么同时出现在 top-3，要么同时出现在 bottom-3，
导致 JPY 很少真正排到极端位置。

### 0.3 当前已实现的缓解措施
在 `--trade-jpy-only` 模式下，`build_strength_matrix(exclude_currencies=["CHF"])`
调用时排除 CHF，使 rank 变成 5 种货币，减少 CHF 对 JPY rank 的"拖后腿"效应。
代码改动在：

| 文件 | 行 | 变更 |
|---|---|---|
| `utils/strategy_helpers.py:169` | `build_strength_matrix()` 签名 | 加了可选参数 `exclude_currencies=None` |
| `scheduled_runner_v3.py:3727-3731` | runner 主流程 | `_GROUP_ONLY_NAME=="JPY"` 时传 `["CHF"]` |

**局限**：hard code 只在 `--trade-jpy-only` 时触发，正常运行（所有 group）不排除任何货币。

### 0.4 问题发生的典型场景（真实日志）

```
正常运行（6 种货币，某次 cycle）:
  1. USD: +1.6641
  2. JPY: +0.7997  ← 避险强
  3. CHF: +0.3545  ← 也避险强，方向相同但分数稍低
  4. GBP: +0.3473
  5. EUR: -1.6327
  6. AUD: -1.6832
  JPY ranks 2/6 → 不是 1 或 6 → gate 关闭 → 无 JPY entry

--trade-jpy-only + 排除 CHF（5 种货币，同一 cycle）:
  1. USD: +2.1688
  2. JPY: +0.7997  ← 位置升到 top-2，但仍然不是极端
  3. GBP: +0.3473
  4. EUR: -1.6327
  5. AUD: -1.6832
  JPY ranks 2/5 → 仍然不是极端 → gate 还是关闭
```

**观察**：即使排除 CHF，某些 RISK OFF 环境下 USD/AUD/EUR 的分数差太大，
JPY 还是无法成为真正的极端。这说明 CHF 只是干扰因素之一。

---

## 方案 A：动态同方向检测（Dynamic Correlation Gate）

### A.1 核心思路
不 hard code CHF 排除，而是在每个 cycle 构建完 strength matrix 之后，
**检测 JPY 与 CHF 的分数方向**——如果二者同向（同正或同负）且绝对分数
都在排名的 top/bottom 40%，则动态排除排名靠后的那个；如果方向相反（极罕见），
则两个都保留。

### A.2 改动范围

#### A.2.1 `scheduled_runner_v3.py` — runner 主流程（~20 行新增）
在第 3727 行 `_global_scores = build_strength_matrix(...)` 之前，
增加动态检测逻辑：

```python
# 伪代码（位置：scheduled_runner_v3.py 约 L3726）
_sm_exclude = []
if _GROUP_ONLY_NAME == "JPY":
    # 先算一版不含排除的完整分数用于检测
    _full_scores = build_strength_matrix()
    _jpy_s = _full_scores.get("JPY", 0.0)
    _chf_s = _full_scores.get("CHF", 0.0)

    if _jpy_s * _chf_s > 0:                    # 同方向（乘积为正）
        _ranked = sorted(_full_scores.items(), key=lambda x: x[1])
        _total = len(_ranked)
        # 取同方向货币的排名
        _jpy_rank = next(i for i, (c, _) in enumerate(_ranked, 1) if c == "JPY")
        _chf_rank = next(i for i, (c, _) in enumerate(_ranked, 1) if c == "CHF")
        _pct_jpy = _jpy_rank / _total
        _pct_chf = _chf_rank / _total
        # 二者都在 top-40% 或 bottom-40%（"真正"在极端区）
        if (_pct_jpy <= 0.4 or _pct_jpy >= 0.6) and (_pct_chf <= 0.4 or _pct_chf >= 0.6):
            # 排除排名靠后的那个
            _to_exclude = "CHF" if _chf_s < _jpy_s else "JPY"
            if _to_exclude == "CHF":
                _sm_exclude = ["CHF"]
                print(f"  [MATRIX] JPY+CHF 同方向 + 同为极端区 → 排除 {_to_exclude}")
            else:
                _sm_exclude = ["JPY"]   # 这个分支理论上不太会触发
                print(f"  [MATRIX] JPY+CHF 同方向 + 同为极端区 → 排除 {_to_exclude}")

_global_scores = build_strength_matrix(exclude_currencies=_sm_exclude)
```

#### A.2.2 `utils/strategy_helpers.py` — 无改动
上次改动已给 `build_strength_matrix()` 加了 `exclude_currencies` 参数。

#### A.2.3 `custom_strategy_v3.py` — 无改动
strategy 层拿到的就是已经"干净"的 `_global_scores`，不感知排除过程。

### A.3 三个典型 cycle 的预期行为

| Cycle | JPY score | CHF score | 方向 | 同方向且极端？ | 排除 | JPY 新 rank |
|---|---|---|---|---|---|---|
| RISK OFF 强（USD=+2.2, JPY=+0.8, CHF=+0.4） | +0.8 | +0.4 | 同正 | ✅ 都是 top-40% | CHF | 2/5（仍不是极端）|
| RISK OFF 极强（USD=+3.0, JPY=-2.0, CHF=-1.9） | -2.0 | -1.9 | 同负 | ✅ 都是 bottom-40% | CHF | 可能升为 5/5 极端 |
| JPY 真强于 CHF（USD=+1.0, JPY=+1.5, CHF=-0.3） | +1.5 | -0.3 | **相反** | ❌ | 无 | 不变 |

### A.4 优点
- **只在真正冲突时才排除**，CHF 和 JPY 方向相反时保留 CHF 作为参照
- 消除了 hard code 的"一刀切"，更符合实际市场状态
- 改动集中在 runner 开头，不影响 strategy 层和下单路径
- 现有 `exclude_currencies` 参数已就位，只需加判断逻辑

### A.5 缺点
- **多算一遍 matrix**（先算完整版本做检测，再算排除后版本），
  约多 12×3=36 次 candle fetch。在 OANDA 有 rate limit 的场景下需注意
  （当前 runner 单次约 50-80 次 fetch，翻倍后 100-160 次，仍在限额内）
- 引入了**新的调参点**：40% 这个阈值需要观察和调整
- 排除 JPY 自身的分支（`_to_exclude == "JPY"`）理论上不应触发，
  但保留这个分支增加了防御性同时也增加了理复杂度

### A.6 实施复杂度：★★☆☆☆（中等）
新增代码约 30 行，集中在 `scheduled_runner_v3.py` 单一位置。
不修改任何已有函数签名，只需新增条件分支。

### A.7 关键风险提示
如果排除了 CHF，USD 组和 CHF 组的 MAINTAIN 就不应该用排除后的
`_global_scores`——应该用完整版本。当前 runner 里 `_global_scores` 是
全局共享的。如果未来要支持"只让 JPY 组用排除后的 matrix，其他组用完整的"，
需要在 runner 里维持两个 scores dict，改动会从 30 行变成 150 行。

---

## 方案 B：避险货币簇 Gate（Safe-Haven Cluster Gate）

### B.1 核心思路
不再把 JPY 和 CHF 当作独立货币放进 global rank，而是把它们合并成一个
**"避险簇"（safe-haven cluster）**：

```python
safe_haven_cluster_score = (scores["JPY"] + scores["CHF"]) / 2
```

把这个簇分数替代 JPY 的位置放进 6 种货币排名。
JPY_REQUIRE_GLOBAL_EXTREME gate 检查的是"避险簇"是不是 global extreme，
而不是 JPY 自己。

### B.2 改动范围

#### B.2.1 `scheduled_runner_v3.py` — gate 层（~25 行）
修改 `_quote_ccy_global_rank()` 调用的输入，或者在 `_global_scores` 之上
构建一个 cluster-aware 的"逻辑 rank"。

**方式 1（推荐）：保持原始 `_global_scores` 不变，在 gate 层单独处理**

```python
# _run_single_group 内的 extreme gate 处（约 L2260）
_rank, _total, _rank_reason = _quote_ccy_global_rank(quote_ccy, global_scores)
_extreme_gate = quote_ccy == "JPY" and JPY_REQUIRE_GLOBAL_EXTREME

# 新增：cluster-aware override
if _extreme_gate and _GROUP_ONLY_NAME == "JPY":
    _scores_copy = dict(global_scores)  # 不修改原始 dict
    if "CHF" in _scores_copy and "JPY" in _scores_copy:
        _cluster_score = (_scores_copy["JPY"] + _scores_copy["CHF"]) / 2
        _scores_copy["SAFE_HAVEN_CLUSTER"] = _cluster_score
        del _scores_copy["CHF"]       # 去掉 CHF 独立位置
        # rank 里 JPY 替换成 cluster_score
        _scores_copy["JPY"] = _cluster_score
        _rank, _total, _rank_reason = _quote_ccy_global_rank("JPY", _scores_copy)
        _rank_reason = f"cluster(JPY+CHF avg={_cluster_score:+.4f})"
```

#### B.2.2 `custom_strategy_v3.py` — strategy 层（~5 行）
`group_strength_rank()` 取 `scores.get("JPY")` 作为 quote_score 基准。
如果 gate 层已经处理好，strategy 层可以不变——它拿到的就是 cluster-aware
的 JPY score。但这要求 runner 在调用 `strategy.generate_signals()` 之前
也要对 scores 做同样的 cluster 替换。

#### B.2.3 设计层面的语义变化
**当前语义**：JPY 自己是不是 extreme → gate pass/fail
**簇语义**：避险情绪整体是不是 extreme → gate pass/fail

这是**根本性的语义改变**。gate 通过后 strategy 内部用的 `scores["JPY"]`
也变成了 `(JPY+CHF)/2`，意味着：

- `USD_JPY` 的 strength 计算变成 `scores["USD"] - cluster_score`
- CHF 自身的分数被"稀释"掉了，只以一半权重进入计算

### B.3 三个典型 cycle 的预期行为

| Cycle | JPY | CHF | Cluster | 新 rank | Gate？ |
|---|---|---|---|---|---|
| 之前那个 `USD=+1.7, JPY=+0.8, CHF=+0.35, AUD=-1.7, EUR=-1.6` | +0.8 | +0.35 | +0.575 | 5/6 是 AUD(-1.7), 4/6 EUR(-1.6), 3/6 cluster(+0.575), 2/6 JPY(+0.8), 1/6 USD(+1.7) → cluster rank 3/6 | ❌ 仍不是极端 |
| 如果 CHF 更弱 `USD=+1.7, JPY=-0.1, CHF=-0.5` | -0.1 | -0.5 | -0.300 | cluster 在 bottom | 可能过 gate |
| CHF 方向相反 `USD=+1.0, JPY=+1.5, CHF=-0.3` | +1.5 | -0.3 | +0.600 | cluster 分数被 CHF 拉低 | gate 变严格 |

### B.4 优点
- **从根本上解决了"同类货币挤 rank"的问题**——不再是 JPY 和 CHF 两个独立的避险货币互相干扰，而是作为一个单元出现
- 语义更清晰："避险情绪极端"才开 JPY 组仓位，符合 JPY 组策略初衷
- gate 通过后 strategy 内部的分数计算自然吸收了 CHF 信息，
  不需要再做额外的 pair-level 特殊处理

### B.5 缺点
- **语义改变太大**，JPY 组的所有 pair strength 都会受到 CHF 分数的影响
  （比如 CHF 某次大幅异动会"稀释" JPY 的强度判断）
- 改动分散在 runner gate 层 + strategy `group_strength_rank()`，
  至少要触达 3 个文件、4 个函数
- **难以回滚**：一旦 cluster 语义生效，JPY 组历史表现的 baseline 就变了
- CHF 自身分数在"方向相反"的 rare case 下会**错误稀释** JPY 的信号
  （比如 JPY 真强、CHF 因独立事件变弱，cluster 平均后 JPY 强度被消弱）

### B.6 实施复杂度：★★★★☆（高）
改动需要协调 gate 层、strategy 层和 `_global_scores` dict 的一致性，
约新增 80-120 行，且需要在至少 3 个位置做 unit-test 验证。

### B.7 关键风险提示
当前 runner 架构是"一次算 matrix，全组共享"。如果 JPY 组要用 cluster-aware
的 scores 而 USD/CHF 组要用原始 scores，就必须在 runner 里存两个 dict，
并在每个 group 循环里正确选择。这会**打破**"一 dict 全共享"的架构简洁性。

---

## 方案 C：run.env 开关 + 当前 hard code（Configurable Hard-Code）

### C.1 核心思路
保持当前已实现的 `--trade-jpy-only → 排除 CHF` hard code 逻辑，
再加一个 run.env 环境变量开关，让操作者可以在不切 `--trade-jpy-only` 的情况下
手动控制 JPY 组排除哪些货币。

### C.2 改动范围

#### C.2.1 新增 run.env 变量
在 `run.env` 里加：

```env
# JPY 组的 strength matrix 排除列表。逗号分隔。
# 在 --trade-jpy-only 时自动生效（默认排除 CHF），
# 正常运行时也可手动开启（让 JPY 组独立观察）。
JPY_EXCLUDE_CURRENCIES=CHF
```

#### C.2.2 `scheduled_runner_v3.py` — 配置解析（~10 行）
在模块级配置解析区（约 L1100-1140 附近）加：

```python
_JPY_EXCLUDE_SOURCE = "defaults (empty)"
_jpy_exclude_list: list[str] = []
if "JPY_EXCLUDE_CURRENCIES" in _ENV_LOADED_KEYS:
    _jpy_exclude_list = [
        c.strip().upper()
        for c in _ENV_LOADED_KEYS["JPY_EXCLUDE_CURRENCIES"].split(",")
        if c.strip()
    ]
    _JPY_EXCLUDE_SOURCE = f"run.env JPY_EXCLUDE_CURRENCIES={_ENV_LOADED_KEYS['JPY_EXCLUDE_CURRENCIES']}"
print(f"[CONFIG] JPY_EXCLUDE_CURRENCIES = {_jpy_exclude_list or '(none)'}  (source: {_JPY_EXCLUDE_SOURCE})")
```

#### C.2.3 `scheduled_runner_v3.py` — runner 主流程（修改 ~5 行）
把当前的硬编码逻辑替换成环境变量驱动：

```python
# 当前（L3727-3731）：
#   _sm_exclude: list[str] = []
#   if _GROUP_ONLY_NAME == "JPY":
#       _sm_exclude = ["CHF"]
#       print("  [MATRIX] Excluding CHF ...")
#   _global_scores = build_strength_matrix(exclude_currencies=_sm_exclude)

# 改为：
_sm_exclude: list[str] = []
if _GROUP_ONLY_NAME == "JPY":
    _sm_exclude = list(_jpy_exclude_list)   # 从 run.env 读取
    if _sm_exclude:
        print(f"  [MATRIX] Excluding {','.join(_sm_exclude)} (JPY-only group)")
    else:
        print("  [MATRIX] No exclusions (JPY-only group with full matrix)")
_global_scores = build_strength_matrix(exclude_currencies=_sm_exclude)
```

### C.3 三个典型场景的预期行为

| 场景 | run.env 设置 | CLI | 行为 |
|---|---|---|---|
| `--trade-jpy-only` + 排除 | `JPY_EXCLUDE_CURRENCIES=CHF` | `--trade-jpy-only` | 排除 CHF ✅ |
| `--trade-jpy-only` + 不排除 | `JPY_EXCLUDE_CURRENCIES=` | `--trade-jpy-only` | 6 种全量 |
| 正常运行（所有组） | 任意 | `--live --dry-run` | `_GROUP_ONLY_NAME=None` → `_sm_exclude=[]`，零排除 |

### C.4 优点
- **完全向后兼容**：默认 `JPY_EXCLUDE_CURRENCIES` 为空时，
  且不传 `--trade-jpy-only`，行为和之前完全一样
- 配置可视化：run.env 里能直接看到当前 JPY 组排除了什么
- 操作者自主权：可以临时改成 `JPY_EXCLUDE_CURRENCIES=CHF,AUD` 试效果
- 改动极小：新增代码约 20 行（配置解析）+ 替换 5 行硬编码
- 不影响 strategy 层，不修改 `_global_scores` 语义

### C.5 缺点
- **本质上还是 hard code 的配置化**——不解决"CHF 和 JPY 方向相反时
  我们还在错误排除 CHF"这个根本问题
- run.env 里多了一个变量，维护成本略增

### C.6 实施复杂度：★☆☆☆☆（极低）
配置解析 + runner 替换，约 25 行改动，全在 `scheduled_runner_v3.py`。

### C.7 关键风险提示
无显著风险。环境变量不设则零排除，完全安全。

---

## 方案对比总表

| 维度 | A. 动态检测 | B. 簇 Gate | C. 开关配置 |
|---|---|---|---|
| **解决根因** | 部分（只在冲突时排除） | 彻底（重定义极端） | 否（仍 hard code） |
| **语义变化** | 无 | 根本性 | 无 |
| **代码改动量** | ~30 行，runner 1 处 | ~80-120 行，3 个文件 | ~25 行，runner 1 处 |
| **架构侵入性** | 低 | 高（破共享 dict 假设） | 极低 |
| **额外 API 调用** | 翻倍（需先算完整版本） | 无 | 无 |
| **可回滚性** | 容易（去掉条件） | 困难（语义不可逆） | 容易（删 env var） |
| **需新调参** | 有（40% 阈值） | 有（avg 权重） | 无 |
| **建议阶段** | Phase 2（观察后） | Phase 3（重设计） | **Phase 1（当前）** |

---

## 实施优先级建议

**Phase 1（当前已完成 + 可选增强）**：方案 C — 把 hard code 暴露成 run.env 开关。
让操作者对排除行为有可见性和控制权，同时保持零架构风险。

**Phase 2（观察 4-6 周后）**：如果发现 JPY 和 CHF 方向相反的 case 确实存在
（即 hard code 排除 CHF 在某些 cycle 里是错的），再实施方案 A 的动态检测。
Phase 1 积累的周期数据可以帮助校准 40% 阈值。

**Phase 3（需要时）**：如果 Phase 2 仍显示"JPY 极端化困难"是因为 CHF 独立行为
干扰（而非二者方向一致），考虑方案 B 的簇语义。但此时应该先做一次完整的
历史回测，确认簇语义不会在 CHF/JPY 方向相反时产生 false negative。

---

## 附录：已实现方案的代码清单

当前已完成的 hard code 修改（Phase 1 的基础部分）：

```
utils/strategy_helpers.py:169
  def build_strength_matrix(exclude_currencies=None) -> Dict[str, float]:
      # 新增 exclude_currencies 参数
      # 过滤 CURRENCIES 和 STRENGTH_PAIRS

scheduled_runner_v3.py:3727-3731
  _sm_exclude: list[str] = []
  if _GROUP_ONLY_NAME == "JPY":
      _sm_exclude = ["CHF"]
      print("  [MATRIX] Excluding CHF ...")
  _global_scores = build_strength_matrix(exclude_currencies=_sm_exclude)
```

**下一步**：按方案 C 把 hard code 改成 run.env 驱动（~25 行）。