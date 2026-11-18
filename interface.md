
# 🧭 FX-Monte-Carlo Data Access Contract & Example

本模块为外部系统提供标准化的 **Monte Carlo 概率区间 + Fibonacci 空间分割** 纯数据契约。

---

## 1. 核心原则

* **MC提供客观的量化测算结果**（包含模拟概率、统计分位数、Fibonacci 网格区间及当前价格映射）。
* **可自由设计策略绑定**：例如用于仓位控制、网格边界、风控止损或趋势过滤。

---

## 2. API 调用参考

### `get_latest_fib_mc_data(day, timeframe="D")`

* **作用**：按日期前缀自动通配匹配，加载当日最新的一份复合 JSON（原样返回，不改写）。
* 返回 `None` 表示**未找到**（目录不存在 / 无匹配文件 / 读取失败）。

### `get_pair_analytics(pair, day, timeframe="D")`

* **作用**：加载指定日期最新生成的复合 JSON，并返回特定交易对的完整分析数据结构。
* **返回数据结构示例 (JSON Schema)**：

```json
{
  "timeframe": "D",
  "pair": "EURUSD=X",
  "current_price": 1.12575,
  "range_90": [1.11374, 1.13566],
  "percentile_rank": 56.0,
  "p_up": 44.0,
  "fib": {
    "levels": {
      "fib_0": 1.11374,
      "fib_50": 1.1247,
      "fib_100": 1.13566
    },
    "zones": [...],
    "current_zone": {
      "tag": "F5",
      "label": "Above Equilibrium",
      "position_in_zone_pct": 40.6
    },
    "zone_index": 3
  },
  "distribution": { "quantiles": {"0.05": 1.11374, "0.95": 1.13566}, "degenerate": false },
  "prob_bands": { "band_count": 5, "band_index": 3, "current": {...}, "degenerate": false },
  "belief": { "verdict": "NO_EDGE", "advisory": true, "generated_utc": "...", "expires_utc": "..." }
}
```

> `distribution` / `prob_bands` / `belief` 自 schema `1.2.0` 起才写入；更早产物**没有**这三块。
> 消费方必须按缺块降级（`get_pair_advisory` 会返回 `state == "invalid"`），**不得**报错。

### `is_snapshot_fresh(data, now_utc=None)` / `is_pair_advisory_fresh(rec, now_utc=None)`

* **作用**：判断静态标注快照是否仍在有效期内（**硬 TTL，闭左**）。
* 判定基准为 `belief.expires_utc`；缺失 / 非法 / 无时区偏移 / 已过期 → `False`（fail-closed）。
* `is_snapshot_fresh` 要求**全部** pair 均新鲜，否则 `False`。
* `now_utc` 必须带时区；传裸 `datetime` 会抛 `ValueError`。

### `get_pair_advisory(rec, now_utc=None)`

* **作用**：返回单个 pair 的**显式、fail-closed** 标注状态，供调用方做单一健康检查。

```json
{
  "state": "healthy",
  "available": true,
  "fresh": true,
  "stale": false,
  "degenerate": false,
  "narrow_range": false,
  "verdict": "NO_EDGE",
  "advisory": true
}
```

`state` 判定优先级（自上而下，任一命中即降级）：

| state | 条件 |
|-------|------|
| `invalid` | 缺 `prob_bands` / `belief`，或块结构不合法（含 1.1.0 及更早产物） |
| `stale` | 快照已过期（`now_utc >= expires_utc`） |
| `degenerate` | `distribution.degenerate` 或 `prob_bands.degenerate` 为真 |
| `narrow_range` | 宽度不可用，或 `range_90_pct < 1.2`（%） |
| `healthy` | 以上皆不成立 |

* `verdict` **仅在 `state == "healthy"` 时**透传 payload 值，其余一律 `"NO_EDGE"`。
* 本函数**只读**，不回写 payload；也**不**替代 `p_up` / `p_down`（方向概率唯一真理源）。

---

## 3. 调用者自由消费示例

调用者只需将 `interface.py` 放到本地，即可按需提取任意指标：

```python
from interface import get_pair_analytics

# 获取某交易对的今日数据
data = get_pair_analytics("EURUSD=X", "20261105")

if data:
    current_price = data["current_price"]
    zone_idx = data["fib"]["zone_index"]
    zone_label = data["fib"]["current_zone"]["label"]
  
    print(f"Price: {current_price} | Fib Zone Index: {zone_idx} ({zone_label})")
  
    # 调用者自行实现个性化逻辑（例如：根据 zone_index 自行决定风控、减仓或挂单）
    if zone_idx >= 4:
        # 开发者自己的高位策略...
        pass
```

---

## 4. ⚠️ 使用须知与已知限制

### 4.1 示例函数请勿直接复用

`interface.py` 中的 `example_calculate_position_multiplier()` 仅为**演示如何 fail-closed 地消费数据**的参考
实现，**不要直接用于生产策略**：

* **与方向无关**：其 K 系数仅按 `zone_index` 对称分档，未区分 BUY / SELL。
  同一个 zone 对多头与空头的含义并不相同，生产策略必须自行做**方向感知**处理。
* 该示例已改为 **fail-closed**：先过 `get_pair_advisory()` 闸门，
  缺块 / 过期 / 退化 / 窄区间时返回 `multiplier_k = None`，由调用方显式决策，**绝不默认满仓**。

调用方请只把该函数当作"如何读取 `fib` 与标注状态"的语法示例，仓位与风控逻辑自行实现。

> ⚠️ 注意：`fib.risk_model.multiplier_k` 在**窄区间 / 退化**时会给出 `1.0`（生产者侧既有行为）。
> 若直接采信该字段，等价于 fail-open。请改用 `get_pair_advisory()` 的 fail-closed 闸门。

### 4.2 每次调用都会重新读取文件

`get_pair_analytics()` / `get_latest_fib_mc_data()` 每次调用都会重新读取并解析整个 JSON。
若需处理多个交易对，请**只调用一次**并复用返回的 `results` 字典，避免 N 次重复文件 IO。

### 4.3 时间框架匹配规则

`get_latest_fib_mc_data(day, timeframe)` 按**文件名中的日期做精确匹配**：

* `D`：文件名日期即当日，可正常使用。
* `W`：文件名日期为**当周周一**。非周一调用时必须自行传入该周一的日期，否则取不到数据。

### 4.4 历史快照可能不含 fib

fib 块自 schema `1.1.0` 起才写入。更早生成的快照没有 `schema_version` 与
`fib_segmentation`，其交易对记录中**不存在 `fib` 字段**。
消费方必须处理该字段缺失的情况（降级而非报错）。

同理，`distribution` / `prob_bands` / `belief` 自 schema `1.2.0` 起才写入；
更早产物这三块均缺失，`get_pair_advisory()` 会对它们返回 `state == "invalid"`。

### 4.5 时效必须显式校验（不要依赖文件名排序）

`get_latest_fib_mc_data()` 按文件名时间戳取"最新"，但这**不代表数据新鲜**：
cron 停摆、数据源未更新时，旧文件依然是"最新文件"。

请显式调用：

```python
bundle = get_latest_fib_mc_data(day, tf)
if not is_snapshot_fresh(bundle):          # 硬 TTL，闭左
    ...                                    # 降级：不下单 / 不调仓 / 仅告警
```

* 有效期基准是每个 pair 的 `belief.expires_utc`（= `generated_utc + 24h`）。
* `now_utc >= expires_utc` 即失效（**闭左**）。
* 缺 `belief`（1.1.0 及更早）或时间戳非法 → 视为**不新鲜**（fail-closed）。

### 4.6 概率标注的消费语义（L1 / L2 不得混用）

* **L0（唯一真理源）**：`p_up` / `p_down` / `percentile_rank` —— MC 模型给出的方向概率事实。
* **L1（静态参考）**：`prob_bands` —— 当前价在终值分布中的**分位位置**；
  其 `prob_mass` 是**构造值**，不是实测命中概率。
* **L2（advisory）**：`belief` —— 证据是否达到判定阈值，**不取代** L0。

三者可以并存而不矛盾：价格处于高分位 ≠ 方向信念达到阈值 ≠ 未来上涨概率。
**禁止**把 `belief.verdict == "UP"` 读作"上涨概率 = `posterior.UP`"，
**禁止**把 `shape.mode` 当作真实样本众数。

