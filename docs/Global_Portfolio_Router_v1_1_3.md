# Global Portfolio Router — Multi-Pair Arbitration, Shadow Correction & Dynamic Tuning

**Version:** v1.1.4 — Draft for Review
**Date:** 2026-11-25
**Stack:** Python + OANDA v20 API
**Code:** `global_portfolio_router_v1_1_4.py` (the code in this repo is the reference; this document describes it)
**Status:** Practice account + `DRY_RUN` only. Self-tests pass offline; nothing has been validated against live OANDA responses yet. See §8 for what is not built.

---

## 1. Architecture: 5 Layers

```
Raw Signal Layer (28 pairs, existing strategies)
        ↓
[1] Global Snapshot Barrier
        ↓
[2] Currency Strength (Decay + Multi-Timeframe + Per-Pair Timeframe)
        ↓
[3] Cross-Sectional Scoring
        ↓
[4] Exposure & Correlation Guard
        ↓
[5] Shadow + Meta Dynamic Tuning Layer (No live orders, only correction)
        ↓
Execution Engine (OANDA v20, Top-1 only)
```

**Key rule:** Live trades with one parameter set. Shadow runs N parameter sets in parallel on the same snapshot. Meta compares them every window. Only a Shadow that wins on Sharpe **and** drawdown for 3 consecutive windows gets promoted to Live, one parameter at a time.

---

## 2. Timeframe Design: First-Class Input

Different pairs have different volatility, session behavior, and optimal horizons. A single global timeframe will systematically misrank them.

### 2.1 Principle: Timeframe Follows the Pair

Each pair has a **primary decision timeframe**, **confirmation timeframes**, and per-timeframe **weights** (`PAIR_TIMEFRAME_CONFIG` in the code).

| Pair Group                                           | Primary | Confirmation | Reason                                                 |
| :--------------------------------------------------- | :------ | :----------- | :----------------------------------------------------- |
| Low-volatility majors — EUR/USD, USD/CHF, EUR/GBP   | H1      | H4 / D1      | Trends are slow, noise on M15 is high                  |
| High-volatility crosses — GBP/JPY, GBP/AUD, EUR/JPY | H4      | D1 / H1      | ATR is 2–3x majors, M15 / H1 signals are mostly noise |
| Commodity pairs — AUD/USD, NZD/USD, USD/CAD         | H1      | H4 / D1      | Commodity-session driven, need an H4 filter            |
| JPY pairs — USD/JPY, AUD/JPY, NZD/JPY               | H1      | H4 / D1      | Session driven, Tokyo / London overlap matters         |

Any pair not listed uses `DEFAULT` (H1 primary, H4 confirmation).

Example: GBP/JPY on M15 produces far more signals than EUR/USD on M15, and most are false. GBP/JPY is judged on H4. M15 is for entry timing, never for direction.

### 2.2 Decision Tick: Per-Pair, Not Global

Intended design: a pair's decision happens when **its primary-timeframe bar closes** (GBP/JPY every H4 close, EUR/USD every H1 close), and the snapshot uses only **closed** bars, so an incomplete H4 candle is never compared against a completed H1 candle.

**Current implementation:** closed-bar filtering exists (`CLOSED_BARS_ONLY`, uses OANDA's `complete` flag). The bar-close *trigger* is **not built**: `decision_tick` runs whenever the caller invokes it. See the open question in §8.

### 2.3 Multi-Timeframe Strength

Per pair, momentum is the slope of closes divided by ATR on each timeframe, blended with that pair's weights. Pair momentums are then aggregated per currency, Z-scored across currencies, and smoothed.

$$
M_{X/Y,tf} = \frac{Slope(X/Y,tf)}{ATR(X/Y,tf)}
$$

$$
M_{X/Y} = \sum_{tf} w_{tf,pair} \cdot M_{X/Y,tf}
$$

$$
S_{ccy,t} = \alpha \cdot S_{ccy,new} + (1-\alpha) \cdot S_{ccy,t-1}
$$

Implementation notes:

- **Slope** is an ordinary least-squares fit (`np.polyfit`). Weighted least squares is not implemented.
- **Per-pair timeframe weights** are only used when `MTF_STRENGTH` is on; otherwise momentum uses the primary timeframe alone.
- **ATR** is `std(diff(close))` by default (a proxy); `TRUE_ATR` switches to a 14-bar true-range average.
- **Z-score across currencies** is always on. Without it, GBP/JPY momentum (~3x EUR/USD) biases the whole matrix.
- **Smoothing:** default `alpha = 0.15` applied per call gives a half-life of about **4.3 calls**, not 6. For a true 6-tick half-life use `alpha ≈ 0.11`, or switch on `TIME_BASED_EMA`, which sets alpha from elapsed time with `HALF_LIFE_SEC` (default 6 hours). Note that a time-based half-life is only meaningful relative to how often you tick.

Example: GBP/AUD rallies on D1, `S_GBP` goes positive and `S_AUD` negative. The system sees "GBP strong, AUD weak", not just "GBP strong".

### 2.4 Timeframe Mismatch Penalty

If a candidate's signal timeframe differs from its pair's primary timeframe, `space_score` is scaled:

| Signal timeframe                    | Multiplier |
| :---------------------------------- | :--------- |
| Primary                             | 1.0        |
| In `confirm` list                 | 0.85       |
| Anything else (e.g. M15 on GBP/JPY) | 0.5        |

The signal can still win if alignment is very strong, but it must overcome the penalty. This removes most overtrading on high-volatility crosses.

---

## 3. Core Algorithms

### 3.1 Final Score

$$
FinalScore = w_{raw} \cdot Raw + w_{align} \cdot \tanh(Align) + w_{space} \cdot Space + w_{cost} \cdot CostScore
$$

`CostScore` is in [0, 1] where **higher means cheaper** (so it is added, not subtracted).

Rules:

- `Align` is `tanh()`-compressed to (-1, 1), otherwise one JPY spike makes Align = 5 and crushes every other factor.
- **Hard veto:** `Align < min_alignment` ⇒ reject. The rejected trade is logged with its reason; that log is the raw material for Shadow attribution.
- **Cost:** with `LIVE_COST` on, `CostScore = clip(1 − (spread / ATR) / COST_CAP, 0, 1)` from the live pricing endpoint. `COST_CAP = 0.25` is an assumption to tune.
- **Weights:** default `w_cost = 0.10`. For intraday, raise `w_cost` to 0.15–0.20 and reduce another weight so the total stays 1.0 (not automated).

Example: `S_JPY = +2.1`, `S_GBP = +1.4`, SHORT GBP/JPY: `Align = -1 × (1.4 − 2.1) = 0.7`. It passes the veto, but AUD/JPY ranks far higher on the same snapshot. This is "right direction, wrong pair".

### 3.2 Exposure Guard

Counting positions is wrong because one trade is not one trade across pairs. Two modes exist:

- **Legacy (default, `RISK_UNIT_EXPOSURE` off):** position exposure is `size × ATR`, the candidate's is `returns.std()`. These units are inconsistent with each other and with the 1.0 cap. Do not rely on it.
- **Risk-unit mode (`RISK_UNIT_EXPOSURE` on, recommended):** every position counts as **1 risk unit**, assuming fixed-risk sizing. Net exposure per currency is the signed sum. A candidate is rejected if it pushes either currency's net beyond `max_ccy_units`.

`max_ccy_units` defaults to **2.0**. At 1.0, no two open trades may share a currency in the same direction. Choose this deliberately.

Second guard, **rolling correlation block:** candidate vs each open position on the last 60 bars of returns, direction-adjusted `effective_corr > max_corr (0.70)` ⇒ skip. AUD/JPY vs NZD/JPY (~0.85) keeps only one. The guard is **inactive for any position whose returns are missing**, which is why `USE_REAL_POS_DATA` matters.

---

## 4. Code

The code lives in `global_portfolio_router_v1_1_3.py`. It is not embedded here, so this document cannot drift from it. Main interfaces:

```python
router = GlobalPortfolioRouter(RouterParams())
best = router.select_best_trade(candidates, pair_momentums, open_positions)
# -> {"pair", "side", "final_score", "alignment", ...} or {"pair": None, "rejected": [...]}

shadow = ShadowEngine([RouterParams(min_alignment=0.2), RouterParams(min_alignment=0.3)])
meta = MetaController(router, shadow)
meta.evaluate_window(live_metrics, [shadow_metrics_0, shadow_metrics_1], field_name="min_alignment")
# metrics: {"sharpe": float, "max_dd": float (positive), "n_trades": int}

decision_tick(router, candidates, shadow)   # snapshot -> positions -> route -> (order commented out)
```

### 4.1 Switches

Everything defaults to off (`class SW`). Enable one at a time in practice.

| Switch                                                                                      | Patch | Effect                                                                |
| :------------------------------------------------------------------------------------------ | :---- | :-------------------------------------------------------------------- |
| `CLOSED_BARS_ONLY`                                                                        | P2    | Drops OANDA's in-progress candle                                      |
| `USE_REAL_POS_DATA`                                                                       | P1    | Real ATR and returns for open positions; never drops a position       |
| `RISK_UNIT_EXPOSURE`                                                                      | P5    | Exposure in risk units                                                |
| `REQUIRE_SL`                                                                              | P3    | Orders without a stop loss are refused                                |
| `STRICT_PROMOTION`                                                                        | P4    | 3 consecutive windows, Sharpe **and** drawdown, additive margin |
| `PARALLEL_SNAPSHOT`                                                                       | P6    | Parallel fetch; skip the tick if fewer than 24 of 28 pairs return     |
| `MTF_STRENGTH`, `TRUE_ATR`, `TIME_BASED_EMA`, `LIVE_COST`, `SHADOW_OWN_POSITIONS` | P7    | See §2.3, §3.1; shadow routers track simulated positions            |
| `DRY_RUN`                                                                                 | —    | **On by default.** Orders are printed, not sent                 |

### 4.2 Fallback monitoring

If a position's ATR/returns cannot be fetched, it is **kept** with placeholder values (hidden exposure is worse than missing data) and logged as `FALLBACK_EXPOSURE`. Each tick prints the cumulative fallback ratio. In risk-unit mode a fallback position still counts as 1 unit; in legacy mode it does not.

---

## 5. OANDA Integration

OANDA-specific rules the code follows:

1. **Naming:** OANDA uses `GBP_JPY`, the router uses `GBP/JPY`; convert at the boundary only.
2. **Credentials:** from env vars `OANDA_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV` (default `practice`). Never hardcode them. Orders on any non-practice environment also require `ALLOW_LIVE_ORDERS=YES`.
3. **Closed bars only:** filter on `complete: true` (switch `CLOSED_BARS_ONLY`).
4. **Snapshot:** sequential fetching under a 2 s timeout always drops the same pairs at the end of the list. Use `PARALLEL_SNAPSHOT` (8 workers). A partial universe skews the Z-score, so the tick is skipped below 24 of 28 pairs. This gate only exists when that switch is on.
5. **Spread:** for execution-time cost, use the pricing endpoint, not historical spread.
6. **Stop loss:** every order carries `stopLossOnFill` with distance `SL_ATR_MULT × ATR` (3 decimals for JPY pairs, 5 otherwise); `REQUIRE_SL` enforces it.
7. **Sizing:** `size_units(risk_amount, sl_distance) = risk / stop`, rejecting bad inputs and results under 1 unit. `risk_amount` must be in the pair's **quote currency**; converting from your account currency for non-matching pairs is **not implemented**.
8. **Practice before live:** Shadow and every switch run on `practice` first.

---

## 6. Rollout Plan

Everything below runs on a **practice** account.

### Week 1: Router only, no Shadow, `DRY_RUN` on

1. **P2 alone** (`CLOSED_BARS_ONLY`). Confirm no in-progress candles reach the snapshot.
2. **P1 + P5 together** (`USE_REAL_POS_DATA` + `RISK_UNIT_EXPOSURE`). They are coupled; P1 alone is not meaningful. Watch the `[FALLBACK]` ratio. P5 is needed regardless of that ratio because the legacy exposure units are inconsistent.
3. **P3** (`REQUIRE_SL`). Confirm refusal without a stop.
4. When the above run clean, set `DRY_RUN=False` on the **practice** environment. This is the first real test of the stop-loss order path.

Log every rejection with its reason (e.g. `GBP/JPY SHORT align=0.7`, `corr>0.7`, `ccy_exposure`). Run 3–5 days.

### Week 2: Shadow, log only, no parameter change

1. **P6** first (`PARALLEL_SNAPSHOT`), so the coverage gate exists.
2. **P7 switches one at a time:** `TRUE_ATR`, `TIME_BASED_EMA`, `MTF_STRENGTH`, `LIVE_COST`, `SHADOW_OWN_POSITIONS`.
3. Run 4–5 Shadow parameter sets in parallel.

Key question is **not PnL**: *if a trade rejected by Live had been taken, would it have won?* If rejected trades consistently win, `min_alignment` is too strict; if they lose, the filter is right. Answering it requires outcome tracking, which is **not built** (see §8).

### Week 3: Meta, dynamic tuning

Turn on `STRICT_PROMOTION`. Meta runs once per window (e.g. every 4 hours) and promotes only after **3 consecutive windows** where the Shadow beats Live on Sharpe by a margin **and** has lower drawdown, with at least 20 trades per window. Promote **one parameter at a time** via `field_name`: first `min_alignment`, then `w_align`. Per-pair timeframe weights live in `PAIR_TIMEFRAME_CONFIG`, not `RouterParams`, so they cannot be promoted yet.

### Initial parameter suggestions

- **1H–4H trend:** `min_alignment=0.10, max_corr=0.70, max_ccy_units=2.0`, weights `(0.40, 0.35, 0.15, 0.10)`.
- **15m intraday:** `w_cost` 0.15–0.20 (rebalance the others), `min_alignment=0.05`. Strength dispersion is smaller intraday, so too strict a veto means no trades. GBP/JPY's primary should shift from H4 to H1 or you will get zero signals.
- **Small account / concentrated:** keep `max_ccy_units` tight and do not relax it.

---

## 7. Summary

Built, with offline self-tests: per-pair timeframe config, Z-scored decayed strength, cross-sectional scoring with a timeframe penalty, risk-unit exposure and correlation guards, parallel snapshot with a coverage gate, stop-loss enforcement, strict one-parameter-at-a-time promotion, and safety defaults (`DRY_RUN`, practice environment, env-var credentials).

- Timeframe is per-pair, not global.
- Scoring is cross-sectional, not single-pair.
- Exposure should be measured in risk units, not position counts (legacy mode is unreliable).
- Shadow does not trade; Meta promotes only on sustained evidence.

---

## 8. Known Gaps and Open Questions

**Not built**

- **Bar-close decision trigger.** `decision_tick` is caller-driven. Decision needed: should H4 pairs wait for their own H4 close, or should everything tick on each H1 close (H4 pairs then use the latest closed H4 bar, up to 4 hours stale)?
- **Outcome tracking and metrics.** Shadow results are not scored; Sharpe, drawdown and "would the rejected trade have won?" must be computed elsewhere and passed to `evaluate_window`.
- **Shadow exits.** Simulated shadow positions expire after a fixed tick count (`SHADOW_HOLD_STEPS`), not on real exit logic.
- **Currency conversion** in `size_units` for pairs whose quote currency is not the account currency.
- **Weighted least squares** slope (code uses ordinary least squares).

**Unvalidated**

- Everything against live OANDA responses (candle and position payloads, rate limits under parallel fetch).
- Tunables that are assumptions: `COST_CAP`, `SHADOW_HOLD_STEPS`, `SL_ATR_MULT`, `max_ccy_units`.

**Known limits**

- Legacy exposure mode and the correlation guard silently weaken when position data is missing; use risk-unit mode with `USE_REAL_POS_DATA`.
- Fixed-risk sizing is assumed by risk-unit exposure; if sizing varies, set `Position.risk_units` accordingly.
