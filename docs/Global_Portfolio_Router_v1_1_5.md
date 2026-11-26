# Global Portfolio Router — Multi-Pair Arbitration, Shadow Correction & Dynamic Tuning

**Version:** v1.1.5 — With Review Feedback
**Date:** 2026-11-26
**Stack:** Python + OANDA v20 API
**Code:** Planned companion: `global_portfolio_router_v1_1_5.py` (not present yet; the available implementation is `global_portfolio_router_v1_1_4.py`)
**Status:** Design/review document for the next code revision. Do not treat the v1.1.5 changes described here as implemented until the companion code is added and reviewed. The available v1.1.4 code defaults to practice + `DRY_RUN`; it has offline self-tests but has not been validated against live OANDA responses.

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
- **Z-score across currencies** is always on. Without it, GBP/JPY momentum (~3x EUR/USD) biases the whole matrix. The target implementation should floor the standard deviation (for example, `max(std, 1e-6)`) to avoid amplifying numerical noise when currency momentums are nearly equal. The available v1.1.4 code uses `std + 1e-9` instead.
- **Smoothing target:** enable `TIME_BASED_EMA` by default. It sets alpha from elapsed time with `HALF_LIFE_SEC` (default 6 hours), making the half-life independent of call frequency. The available v1.1.4 code leaves this switch off by default and otherwise uses per-call alpha.

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
- **Hard veto:** `Align < min_alignment` ⇒ reject. Rejection reasons are logged, but that alone does not provide outcome attribution; rejected-trade outcome tracking is still pending (see §8).
- **Cost target:** with `LIVE_COST` on, `CostScore = clip(1 − (spread / ATR_H1) / COST_CAP, 0, 1)` from the live pricing endpoint. Define `ATR_H1` consistently and fetch it even when H1 is not a pair's momentum timeframe. `COST_CAP = 0.25` is an assumption to tune. The available v1.1.4 implementation uses the candidate ATR, which can be the pair's primary timeframe, so this H1 standardization is not yet implemented there.
- **Weights:** default `w_cost = 0.10`. For intraday, raise `w_cost` to 0.15–0.20 and reduce another weight so the total stays 1.0 (not automated).

Example: `S_JPY = +2.1`, `S_GBP = +1.4`, SHORT GBP/JPY: `Align = -1 × (1.4 − 2.1) = 0.7`. It passes the veto, but AUD/JPY ranks far higher on the same snapshot. This is "right direction, wrong pair".

### 3.2 Exposure Guard

Counting positions is wrong because one trade is not one trade across pairs. Two modes exist:

- **Legacy (deprecated):** position exposure is `size × ATR`. Do not use.
- **Risk-unit mode (`RISK_UNIT_EXPOSURE` on, recommended):** every position counts as **±1 risk unit**, assuming fixed-risk sizing.
    - **Net Exposure:** Net exposure per currency is the signed sum. A candidate is rejected if it pushes a currency's net exposure beyond `max_ccy_units`.
    - **Gross Exposure target:** To prevent offsetting positions from masking true risk (e.g., USD in EUR/USD LONG and USD/CHF SHORT), also enforce a gross cap, `max_ccy_gross_units` (proposed default 3.0), calculated as the sum of absolute risk units by currency, including the candidate.
    - Reject a candidate if it breaches either cap. The available v1.1.4 implementation checks only net exposure; the gross cap is a v1.1.5 requirement, not a current protection.

`max_ccy_units` defaults to **2.0**. At 1.0, no two open trades may share a currency in the same direction.

**Rolling correlation block:** candidate vs each open position.
- **Target data:** calculate correlation from aligned, closed-bar returns on a uniform H1 timeframe for both the candidate and all open positions. Define the common lookback in elapsed time; equal array lengths alone do not guarantee aligned timestamps.
- The direction-adjusted `effective_corr > max_corr (0.70)` ⇒ skip.
- **Missing data target:** when a position's returns are unavailable or insufficient, conservatively block the candidate and report the data gap. The available v1.1.4 implementation skips that position's correlation check, so it can fail open.

---

## 4. Planned Code Interface

The planned companion file is `global_portfolio_router_v1_1_5.py`; it has not been added yet. The currently available implementation is `global_portfolio_router_v1_1_4.py`, and it does not include all v1.1.5 target behavior described here. Reconcile this interface with the code when that file is implemented. Main interfaces:

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

Everything defaults to off (`class SW`) unless specified. Enable one at a time in practice.

| Switch                                                                  | Patch | Effect                                                                |
| :---------------------------------------------------------------------- | :---- | :-------------------------------------------------------------------- |
| `TIME_BASED_EMA`                                                      | P7    | Target default on. Sets EMA alpha based on elapsed time; off by default in available v1.1.4 code. |
| `CLOSED_BARS_ONLY`                                                    | P2    | Drops OANDA's in-progress candle                                      |
| `USE_REAL_POS_DATA`                                                   | P1    | Real ATR and returns for open positions; never drops a position       |
| `RISK_UNIT_EXPOSURE`                                                  | P5    | Exposure in risk units; net cap exists in v1.1.4, gross cap is a v1.1.5 target |
| `REQUIRE_SL`                                                          | P3    | Orders without a stop loss are refused                                |
| `STRICT_PROMOTION`                                                    | P4    | 3 consecutive windows, Sharpe **and** drawdown, additive margin |
| `PARALLEL_SNAPSHOT`                                                   | P6    | Parallel fetch for candle data. In available v1.1.4 code, only this path enforces coverage. |
| `MTF_STRENGTH`, `TRUE_ATR`, `LIVE_COST`, `SHADOW_OWN_POSITIONS` | P7    | See §2.3, §3.1; shadow routers track simulated positions            |
| `DRY_RUN`                                                             | —    | **On by default.** Orders are printed, not sent                 |

### 4.2 Fallback monitoring

If a position's ATR/returns cannot be fetched, it is **kept** with placeholder values (hidden exposure is worse than missing data) and logged as `FALLBACK_EXPOSURE`. In risk-unit mode a fallback position still counts as 1 unit; in legacy mode it does not.

---

## 5. OANDA Integration

OANDA-specific rules and requirements:

1. **Naming:** OANDA uses `GBP_JPY`, the router uses `GBP/JPY`; convert at the boundary only.
2. **Credentials:** from env vars `OANDA_TOKEN`, `OANDA_ACCOUNT_ID`, `OANDA_ENV` (default `practice`). Never hardcode them. Orders on any non-practice environment also require `ALLOW_LIVE_ORDERS=YES`.
3. **Closed bars only:** filter on `complete: true` (switch `CLOSED_BARS_ONLY`).
4. **Snapshot Integrity target:** Apply a coverage gate after either sequential or parallel fetch and before currency strength is computed. A simple threshold of 24/28 still permits four missing pairs and does not guarantee that every currency is adequately represented. Define required coverage by pair/currency, justify the threshold, and skip the tick when that requirement is not met. The available v1.1.4 code checks the threshold only when `PARALLEL_SNAPSHOT` is on.
5. **Rate Limits:** The high number of API requests per tick (~60-80) creates a risk of rate limiting. A shared candle cache within a single tick and an exponential backoff-and-retry mechanism on 429 errors should be implemented. These mitigations are not currently implemented or validated.
6. **Spread:** for execution-time cost, use the pricing endpoint, not historical spread.
7. **Stop loss:** the intended order path carries `stopLossOnFill` with distance `SL_ATR_MULT × ATR` (3 decimals for JPY pairs, 5 otherwise); `REQUIRE_SL` enforces it. In available v1.1.4 code, the call that places the order is commented out, so this execution path has not been validated.
8. **Sizing:** `size_units(risk_amount, sl_distance) = risk / stop`, rejecting bad inputs and results under 1 unit. `risk_amount` must be in the pair's **quote currency**; converting from your account currency for non-matching pairs is **not implemented**.
9. **Practice before live:** Shadow and every switch run on `practice` first.

---

## 6. Rollout Plan

The v1.1.4 sequence is not sufficient for the added v1.1.5 targets. Keep practice + `DRY_RUN` until the pending behavior is implemented and its offline checks pass.

1. **Before practice evaluation:** implement and verify the universal snapshot gate, gross exposure cap, aligned H1 correlation data with conservative missing-data behavior, H1 cost ATR, and time-based EMA default. Keep each safeguard independently switchable during initial validation.
2. **Practice, `DRY_RUN` on:** first validate closed-bar filtering and snapshot coverage; then validate position fallback handling, net/gross exposure and correlation decisions against constructed examples. Confirm rejected-candidate outcome logging records a hypothetical entry, exit horizon/rule, spread/cost, and outcome.
3. **Practice order path:** only after the safety checks above are confirmed, validate `REQUIRE_SL` and order payloads on practice. Do not enable live orders based on document review alone.
4. **Shadow and Meta:** enable shadow tracking after rejected-outcome tracking and shadow exit rules are defined. Require the documented trade-count and promotion criteria to be evaluated from recorded outcomes.

The number of API calls, shared candle caching, and 429 retry/backoff remain operational validation items; measure them on practice before increasing tick frequency or enabling parallel requests.

---

## 7. Summary

Available in v1.1.4, with offline self-tests: per-pair timeframe config, Z-scored decayed strength, cross-sectional scoring with a timeframe penalty, switch-gated risk-unit net exposure, correlation checks when comparable returns are available, parallel-path snapshot coverage gate, stop-loss enforcement, strict one-parameter-at-a-time promotion, and safety defaults (`DRY_RUN`, practice environment, env-var credentials). The v1.1.5 targets in this document—gross exposure, universal coverage enforcement, conservative correlation fallback, H1 cost normalization, and default time-based EMA—remain pending implementation and verification.

- Timeframe is per-pair, not global.
- Scoring is cross-sectional, not single-pair.
- Exposure target is measured in risk units with both net and gross caps; only the net cap is present in v1.1.4.
- Shadow does not trade; Meta promotes only on sustained evidence.

---

## 8. Known Gaps and Open Questions

**Not built in available v1.1.4 code / pending for v1.1.5**

- **Bar-close decision trigger.** `decision_tick` is caller-driven.
- **Outcome tracking for rejected trades.** This is a critical dependency for Week 2 analysis. A simple implementation is to log every rejected candidate to a "shadow rejected" portfolio with its entry price and a timestamp. After N bars, its hypothetical win/loss can be calculated and aggregated. This can reuse the `SHADOW_OWN_POSITIONS` mechanism.
- **Gross exposure cap.** Net exposure can hide offsetting currency risk. Implement `max_ccy_gross_units` and include candidate exposure in the cap check.
- **Universal snapshot coverage gate.** The current v1.1.4 gate only runs with `PARALLEL_SNAPSHOT`. Apply coverage checks to both snapshot paths and validate representation by currency as well as pair count.
- **Uniform H1 returns for correlation and conservative missing-data handling.** The current v1.1.4 correlation check can compare pair-primary timeframes and skips positions with missing returns.
- **H1 ATR cost normalization and default time-based EMA.** The current v1.1.4 implementation uses the candidate's available ATR and leaves `TIME_BASED_EMA` off by default.
- **Shadow exits.** Simulated shadow positions expire after a fixed tick count (`SHADOW_HOLD_STEPS`), not on real exit logic.
- **Currency conversion** in `size_units` for pairs whose quote currency is not the account currency (e.g., JPY pairs for a USD account). This affects the relevant non-USD-quote instruments; confirm the share against the configured universe rather than assuming a fixed percentage.
- **Weighted least squares** slope (code uses ordinary least squares).

**Unvalidated**

- Everything against live OANDA responses (candle and position payloads, rate limits under parallel fetch).
- Tunables that are assumptions: `COST_CAP`, `SHADOW_HOLD_STEPS`, `SL_ATR_MULT`, `max_ccy_units`, and proposed `max_ccy_gross_units`.

**Known limits**

- **Top-1 Only:** The system is intentionally designed to select only the single best trade per tick. This means in a strong, uniform market move (e.g., all JPY pairs weakening), it will only enter one position, leaving other opportunities for subsequent ticks. This is a deliberate choice to limit risk, not a bug.
