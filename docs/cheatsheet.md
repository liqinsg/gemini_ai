# JPY Composite Index — Full Cheatsheet

**Updated:** 2026-10-05  
**Branch:** `v2c`  
**Status:** ✅ Live | ✅ Rank Restriction Removed | ✅ Direction Verified

---

# 📐 Core Concept

JPY strength is evaluated against a weighted basket of currencies rather than global rank alone.

## Basket Weights

| Currency | Weight |
|----------|---------:|
| EUR | 2.0 |
| USD | 1.5 |
| GBP | 1.0 |
| AUD | 0.5 |
| CHF | 0.1 |

> **Note:** JPY is the target currency and is never included in its own basket.

## Formula

```text
RawIndex = Σ (JPY_score - Opponent_score) × NormalizedWeight
```

Interpretation:

```text
RawIndex > 0
→ JPY stronger than basket

RawIndex < 0
→ JPY weaker than basket
```

---

# ⚙️ Current Runtime Configuration

## Gate Controls

```bash
TRADE_JPY=true

JPY_ONLY_USE_WEIGHTED_GATE=true

JPY_REQUIRE_GLOBAL_EXTREME=false
```

### Meaning

```text
✅ Composite Index is active

✅ Rank restriction removed

✅ JPY can trade at Rank 1–6

❌ No longer limited to strongest or weakest currency
```

---

# 📊 Thresholds

```bash
JPY_WEIGHTED_PASS_THRESHOLD=0.8

JPY_WEIGHTED_STRONG_MIN_COUNT=2

JPY_WEIGHTED_CCY_MIN_ABS_SCORE=0.1
```

### Requirements

```text
|RawIndex| ≥ 0.8

AND

At least 2 same-direction contributors
```

---

# 📖 Direction Mapping (Verified)

This section reflects actual live execution behavior.

---

## ✅ JPY Bull

### Condition

```text
RawIndex ≥ +0.8
```

### Meaning

```text
JPY is stronger than the basket currencies.
```

### Expected Trades

```text
SELL EUR_JPY
SELL GBP_JPY
SELL AUD_JPY
SELL USD_JPY
```

### Why

```text
JPY ↑

EUR_JPY ↓
GBP_JPY ↓
AUD_JPY ↓
USD_JPY ↓
```

### Verified Example

```text
RawIndex = +1.489

System Result:
SELL EUR_JPY ✅
```

---

## ⚠️ JPY Bear

### Condition

```text
RawIndex ≤ -0.8
```

### Meaning

```text
JPY is weaker than the basket currencies.
```

### Expected Trades

```text
BUY EUR_JPY
BUY GBP_JPY
BUY AUD_JPY
BUY USD_JPY
```

### Why

```text
JPY ↓

EUR_JPY ↑
GBP_JPY ↑
AUD_JPY ↑
USD_JPY ↑
```

---

## ⏸ Neutral

### Condition

```text
-0.8 < RawIndex < +0.8
```

### Result

```text
No JPY trading signal.
```

---

# 🔄 Decision Flow

```text
Start
  │
  ▼

Build Global Strength Matrix
  │
  ▼

Compute JPY Composite Index
  │
  ▼

Check Thresholds

|RawIndex| ≥ 0.8 ?
StrongCount ≥ 2 ?

  │
  ├── NO ──► No Signal
  │
  └── YES
          │
          ▼

Evaluate JPY Pairs
          │
          ▼

Apply Dominance / MA / MACD Filters
          │
          ▼

Generate Trade Signal
          │
          ▼

Execute Order
```

---

# ✅ Verified Live Example

## Strength Ranking

```text
1. CHF  +1.190
2. JPY  +0.882
3. GBP  +0.535
4. USD  +0.181
5. AUD  +0.093
6. EUR  -2.034
```

## Composite Result

```text
RawIndex           = +1.489

DirectionalBalance = 99.6

WeightedBreadth    = 98%
```

## Trade Decision

```text
SELL EUR_JPY ✅
```

## Conclusion

```text
Rank = 2/6

Still evaluated ✅

Still traded ✅

Rank restriction successfully removed ✅
```

---

# 🎛 Recommended Presets

## ⚖️ Balanced (Recommended)

```bash
JPY_REQUIRE_GLOBAL_EXTREME=false
JPY_WEIGHTED_PASS_THRESHOLD=0.8
JPY_WEIGHTED_STRONG_MIN_COUNT=2
```

### Characteristics

- Good signal quality
- More opportunities
- Recommended default mode

---

## 🔒 Conservative

```bash
JPY_REQUIRE_GLOBAL_EXTREME=true
JPY_WEIGHTED_PASS_THRESHOLD=1.0
JPY_WEIGHTED_STRONG_MIN_COUNT=2
```

### Characteristics

- Fewer signals
- Higher conviction
- Closest to original behavior

---

## ⚡ Aggressive

```bash
JPY_REQUIRE_GLOBAL_EXTREME=false
JPY_WEIGHTED_PASS_THRESHOLD=0.5
JPY_WEIGHTED_STRONG_MIN_COUNT=1
JPY_WEIGHTED_CCY_MIN_ABS_SCORE=0.05
```

### Characteristics

- Highest sensitivity
- More trades
- Higher drawdown risk

---

# 🐛 Troubleshooting

## Still seeing:

```text
SKIP rank 2/6
```

Check:

```bash
grep JPY_REQUIRE_GLOBAL_EXTREME run.env
```

Expected:

```bash
JPY_REQUIRE_GLOBAL_EXTREME=false
```

---

## Composite Index Appears but No Trade

Verify:

```bash
JPY_WEIGHTED_PASS_THRESHOLD

JPY_WEIGHTED_STRONG_MIN_COUNT

JPY_WEIGHTED_CCY_MIN_ABS_SCORE
```

---

## Unsure About Trade Direction

### Golden Rule

```text
JPY Bull
=
SELL JPY Crosses

(EUR_JPY, GBP_JPY, AUD_JPY, USD_JPY)
```

```text
JPY Bear
=
BUY JPY Crosses

(EUR_JPY, GBP_JPY, AUD_JPY, USD_JPY)
```

---

# 📁 Key Files

## Runtime Configuration

```text
run.env
```

Contains all runtime settings and thresholds.

---

## Composite Calculation Engine

```text
utils/jpy_composite_index.py
```

Calculates the JPY Composite Index.

---

## Live Display & Diagnostics

```text
utils/jpy_index_live.py
```

Produces real-time index output and analysis.

---

## Main Strategy Runner

```text
scheduled_runner_v3.py
```

Controls pair selection, filtering, and execution.

---

## Historical Index Log

```text
logs/jpy_index_history.csv
```

Stores Composite Index snapshots for historical analysis.

---

# ✅ Production Validation Checklist

```text
✅ JPY_REQUIRE_GLOBAL_EXTREME=false

✅ Rank 2/6 still evaluated

✅ Composite Index calculated

✅ Threshold passed

✅ Pair evaluation executed

✅ EUR_JPY selected

✅ Order filled

✅ Composite Gate operational

✅ Direction mapping verified

✅ Production ready
```
