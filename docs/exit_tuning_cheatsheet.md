# Exit Logic Tuning Cheatsheet (v20260828+)

> **Snapshot warning:** `RISK_*` / `RiskConfig` values are **snapshotted into each
> cluster at entry** (`build_risk_config()` in [utils/risk_integration.py](../utils/risk_integration.py)).
> Edits only affect **newly opened** positions. Invalidation params (sections A–B)
> are read **live every cycle** — they affect **open** positions immediately.

## Where exits are decided (order per cycle)

1. **Phase A0** — `enforce_global_invalidation_sweep()` in `scheduled_runner_v1.3.py`:
   flattens ANY open OANDA position (tracked or not) failing the multi-factor check.
2. **Phase A** — `manage_open_positions()`: for each tracked instrument,
   `check_multi_factor_invalidation()` runs **before** SL/trailing/time-decay.
3. If still open — `DynamicRiskManager.update()`: break-even → Chandelier trail →
   HWM profit-lock → time-decay.

---

## A. Reversal / "quick exit" sensitivity (read LIVE each cycle — `config.py`)

| Knob | Default | More sensitive (exits earlier) | Less sensitive (holds longer) |
|---|---|---|---|
| `STRATEGY_INVALIDATION_MIN_GAP` | `0.2` | ↑ raise (e.g. `1.0`) | ↓ lower (e.g. `0.15` = old value) |
| `STRATEGY_INVALIDATION_TOP_TIER_FRACTION` | `0.5` | ↑ (`0.33` → must be top-third) | ↓ (`0.75` → tolerate weaker rank) |
| `STRATEGY_INVALIDATION_PROPORTIONAL_FACTOR` | `0.4` | ↑ (`0.6` → demand near-entry-grade gap) | ↓ (`0.2` → loose) |
| `ENABLE_STRATEGY_INVALIDATION_RANK_CHECK` | `True` | — | `False` disables rank factor |
| `ENABLE_STRATEGY_INVALIDATION_PROPORTIONAL_CHECK` | `True` | — | `False` disables dynamic cutoff |
| `TECHNICAL_INVALIDATION_REQUIRE_ALIGNED` | `2` (of H4,H1,M30) | `3` = any single TF flipping closes | `2` tolerates a 1-TF pullback (e.g. M30) |
| `ENABLE_TECHNICAL_INVALIDATION_CLOSE` | `True` | — | `False` = never close on MA5 flip |
| `ENABLE_GLOBAL_INVALIDATION_SWEEP` | `True` | — | `False` = untracked/manual trades never swept |

**Trigger recap:**
- *Gap robustness* — LONG: `strength[base] − strength[JPY] < +MIN_GAP` · SHORT: `> −MIN_GAP`
- *Rank tier* — LONG: base not in top `⌈N × TOP_TIER_FRACTION⌉` · SHORT: not in bottom tier
- *Proportional cutoff* — gap must clear `max(top_gap, 0) × PROPORTIONAL_FACTOR`
  (`top_gap` = best non-JPY score − JPY this cycle)
- *Technical mixed* — fewer than `REQUIRE_ALIGNED` of `SIGNAL_TIMEFRAMES` (H4, H1, M30)
  agree on side of MA5 · *Technical opposite* — alignment fully flipped

## B. Multi-factor combiner — the actual close gate (`config.py`)

| Knob | Default | Effect |
|---|---|---|
| `ENABLE_MULTI_FACTOR_INVALIDATION` | `True` | master switch for combined scoring |
| `INVALIDATION_DETERIORATION_SCORE_THRESHOLD` | `2.0` | **Currently requires ≥2 failing factors (multi-factor consensus).** ↓ to `1.0` = any single factor closes (max sensitivity) |
| `INVALIDATION_WEIGHT_GAP_ROBUSTNESS` | `1.0` | set `<` threshold to make gap-alone insufficient |
| `INVALIDATION_WEIGHT_RANK_TIER` | `1.0` | same idea for rank |
| `INVALIDATION_WEIGHT_PROPORTIONAL_CUTOFF` | `1.0` | same idea for dynamic cutoff |
| `INVALIDATION_WEIGHT_TECHNICAL_MIXED` | `1.0` | e.g. `0.5` → mixed-MA alone won't close |
| `INVALIDATION_WEIGHT_TECHNICAL_OPPOSITE` | `2.0` | full MA flip always closes (≥ any sane threshold) |

**Recipe — "only close on confluence" (CURRENT):** threshold = `2.0`, weights at `1.0`.
**Recipe — "max sensitivity":** threshold = `1.0`, any single failing factor closes.
**Recipe — "ignore mixed MA, only flip or strength loss":**
`INVALIDATION_WEIGHT_TECHNICAL_MIXED = 0.5`, threshold = `1.0`.

## C. Profit protection — High-Water Mark lock (`utils/dynamic_risk_manager.py`, `RiskConfig` defaults; NOT in `config.py`)

| Knob | Default | Tighter (lock profit sooner) | Looser (let winners run) |
|---|---|---|---|
| `enable_profit_lock` | `True` | — | `False` → pure Chandelier only |
| `profit_lock_threshold_r` | `1.5` | ↓ `1.0` (arm earlier) | ↑ `2.0` |
| `profit_retracement_ratio` | `0.65` | ↓ `0.5`/`0.3` (exit on 50%/30% give-back) | ↑ `0.75` |

Arms (sticky) once peak floating profit ≥ `threshold_r`; FULL_CLOSE when
`(peak_r − r) / peak_r ≥ ratio`. State (`peak_r`, `profit_lock_armed`) persists in
`state/open_clusters.json`.

## D. Classic trailing / BE / time-stop (`config.py` → snapshot at entry)

| Knob | Default | Tighter | Looser |
|---|---|---|---|
| `RISK_ATR_MULTIPLIER_INIT` | `2.0` | ↓ `1.5` | ↑ `2.5` |
| `RISK_BE_TRIGGER_R` | `1.0` | ↓ `0.5` | ↑ `1.5` |
| `RISK_CHANDELIER_K_DEFAULT` | `3.0` | ↓ `2.5` | ↑ `3.5` |
| k-tighten ladder (`chandelier_k_tighten_at_1_5r` / `_at_2r` / `_time_decay_lock` in `RiskConfig`) | `2.5` / `2.0` / `1.0` | lower values | higher values |
| `RISK_T_EXPECTED_HOURS` | `24.0` | ↓ (decay sooner) | ↑ `48.0` (code default) |
| `RISK_TIME_REDUCE_THRESHOLD` / `RISK_TIME_REDUCE_RATIO` | `1.0` / `0.5` | ↓ / ↑ | ↑ / ↓ |
| `RISK_TIME_EXIT_THRESHOLD` | `1.5` | ↓ | ↑ |
| `RISK_VOL_COMPRESSION_FRAC` | `0.6` | ↑ `0.8` (stagnation easier) | ↓ `0.4` |
| `RISK_EXTREME_LOOKBACK_GRANULARITY` | `"H1"` | `"M30"` (faster HH/LL) | `"H4"` |

## E. Verify in logs

| Grep for | Meaning |
|---|---|
| `CLOSE_REASON_STRATEGY_INVALIDATION` | strength gap/rank/cutoff close (also `logs/v2_trade_outcomes.jsonl`) |
| `CLOSE_REASON_TECHNICAL_INVALIDATION` | MA5 mixed/flipped close |
| `CLOSE_REASON_MULTI_FACTOR_INVALIDATION` | combined score close |
| `PROFIT_LOCK` | HWM retracement close |
| `Profit-lock armed at peak=` | breaker armed |
| `[RISK] Global sweep flattened:` | Phase A0 kill switch fired |
