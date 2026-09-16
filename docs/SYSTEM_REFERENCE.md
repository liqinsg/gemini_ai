# GEMINI API - TECHNICAL SYSTEM REFERENCE

**Date:** 2026-08-26  
**Repository:** `liqinsg/gemini_ai` (branch: `main`)  
**Purpose:** Automated Forex trading system (JPY crosses, demo account)  
**Status:** Production trading code with optional Phase 2 risk management

---

## 1. SOURCE OF TRUTH & AUDIT BASIS

This document is grounded in direct inspection of the current repository (`/home/nie/projects/gemini_api`). All major claims are traceable to specific files and functions listed with line numbers and commit SHAs where relevant.

**Audit date:** 2026-08-26  
**Current HEAD:** `e66dfeb` (check_oanda_account)  
**Branch:** `main`

---

## 2. SYSTEM PURPOSE & SCOPE

**Primary Function:** Automated Forex trading system that:
1. Runs on a 15-minute cycle (cron-triggered)
2. Scans JPY pairs (`USD_JPY`, `EUR_JPY`, `GBP_JPY`, `AUD_JPY`) for alignment signals
3. Enters positions based on multi-timeframe MA5 alignment (`H4`, `H1`, `M30`)
4. Manages open positions via optional dynamic risk layer (Phase 2)
5. Persists position state across process restarts
6. Logs observations for offline analysis

**Environment:** Demo account (configured, can be changed to live with explicit flag change)

**Runtime Model:** 
- Entry point: [scheduled_runner_v1.3.py](scheduled_runner_v1.3.py)
- Execution: Fresh Python process per 15-minute cycle (cron-driven, no in-process loop)
- One entry per cycle, maximum

---

## 3. RUNTIME ARCHITECTURE

### Entry Point

| File | Function | Line | Invocation |
|---|---|---|---|
| [scheduled_runner_v1.3.py](scheduled_runner_v1.3.py#L172) | `__main__` block | 172–187 | Cron trigger: `python scheduled_runner_v1.3.py` |
| [scheduled_runner_v1.3.py](scheduled_runner_v1.3.py#L49) | `run_cycle()` | 49–170 | Called from `__main__` |

**Versions:**
- `scheduled_runner_v1.3.py` — **ACTIVE** (current, Phase 2 with dynamic risk manager)
- `scheduled_runner_v1.2.py` — Inactive (identical logic, precursor)
- `scheduled_runner.py` — Inactive (v1.0, no risk manager)

### Live Execution Path

```
run_cycle() [scheduled_runner_v1.3.py:49]
  │
  ├─ PHASE A: Manage existing positions (if ENABLE_DYNAMIC_RISK_MANAGER=True)
  │   └─ risk_integration.manage_open_positions() [utils/risk_integration.py:624]
  │      ├─ Load cluster state [cluster_state_store.py]
  │      ├─ Reconcile with OANDA live positions
  │      ├─ Execute risk actions (BE, Chandelier trail, time-decay close)
  │      ├─ Persist updated state
  │      └─ Return set of managed instruments
  │
  ├─ PHASE B: Scan for entry signals
  │   ├─ analyze_custom_strategy() [custom_strategy_v1.py:606]
  │   │   └─ JPYTrendStrategy.generate_signals()
  │   │       ├─ Compute strength matrix
  │   │       ├─ Rank pairs
  │   │       ├─ MA5 alignment check per pair
  │   │       ├─ Log signal observations [signal_instrumentation.py]
  │   │       └─ Return signals list
  │   │
  │   └─ get_last_signal() [custom_strategy_v1.py:615]
  │       └─ Return best signal or None
  │
  ├─ PHASE C: Resolve position direction
  │   └─ resolve_and_prepare_entry(pair, action) [position_direction.py:110]
  │       ├─ Query OANDA position direction
  │       ├─ Return decision (ENTER, SKIP_*, CLOSE_THEN_ENTER, SKIP_HEDGED)
  │       └─ If CLOSE_THEN_ENTER: close opposite position at OANDA
  │
  └─ PHASE D: Execute entry
      └─ If ENABLE_DYNAMIC_RISK_MANAGER=True:
         └─ open_oanda_order(signal, units) [oanda_execution.py:20]
            ├─ Submit market order to OANDA
            ├─ On fill: new_cluster_from_fill() [risk_integration.py:521]
            │   └─ Create PyramidCluster + DynamicRiskManager
            └─ save_cluster_data() [risk_integration.py:170]
                └─ Persist to state/open_clusters.json
         Else (risk manager disabled):
         └─ execute_market_trade(signal, units) [trading_core.py]
            └─ Legacy path (simpler order flow)
```

**Key Constraint:** Each cycle processes a maximum of **one new entry**. Once an entry signal is found and executed, the runner exits. No pyramiding, no multi-entry per cycle.

---

## 4. MAJOR COMPONENTS & LIVE INVOCATION

### A. Configuration & Scheduling

| File | Module | Purpose | Used By | Status |
|---|---|---|---|---|
| [config.py](config.py) | Configuration | Central source for all parameters (risk level, pairs, timeframes, ATR params, OANDA credentials, feature flags) | Every module | **ACTIVE** |

**Critical parameters (actual values in config.py lines ~280+):**
- `ENABLE_DYNAMIC_RISK_MANAGER` = True
- `RISK_LEVEL` = 10
- `CHECK_INTERVAL_MINUTES` = 15
- `TRADE_PAIRS` = ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"]
- `SIGNAL_TIMEFRAMES` = ["H4", "H1", "M30"]

### B. Strategy & Signal Generation

| File | Function/Class | Purpose | Input | Output | Live |
|---|---|---|---|---|---|
| [custom_strategy_v1.py](custom_strategy_v1.py#L120) | `JPYTrendStrategy` | Generates entry signals based on MA5 alignment + ATR SL/TP | OANDA candles, strength matrix, S/R levels | List of signals (action, pair, entry, SL, TP, RR) | **YES** — called every cycle |
| [strategy_helpers.py](utils/strategy_helpers.py) | Utility functions | Computes EMA, ATR, alignment, slope, S/R detection | Candles, indicators | Feature values | **YES** — called by strategy |
| [currency_strength.py](utils/currency_strength.py) | Strength calculation | Builds currency strength matrix (e.g., USD: +0.4, JPY: -0.3) | OANDA candles, pair list | Strength dict | **YES** — called every cycle |
| [find_support_resistence.py](utils/find_support_resistence.py) | S/R detection | Identifies support/resistance levels | Candles, lookback | Level list | **YES** — called by strategy |

**Signal Generation Flow:**
1. Fetch candles for all pairs at H4, H1, M30 timeframes
2. Compute strength matrix (currency pair strength vs JPY)
3. Rank pairs by strength
4. Filter by minimum strength gap (0.03)
5. For each pair: check MA5 alignment across all 3 timeframes
6. If aligned: compute SL/TP using ATR with volatility scaling
7. Validate Risk:Reward ≥ 1.2
8. Log observation (non-blocking)
9. Return all signals, keep best in `_last_signal`

**No pyramiding, no scale-ins in current strategy.**

### C. Position Management & Direction Checking

| File | Function | Purpose | Input | Output | Live |
|---|---|---|---|---|---|
| [position_direction.py](utils/position_direction.py#L110) | `resolve_and_prepare_entry()` | Checks existing position direction; prevents same-direction re-entry; closes opposite-direction positions | Pair, signal action | PositionDecision enum + OANDA close (if needed) | **YES** — called every cycle before entry |

**Decisions:**
- `ENTER` — No position exists, proceed
- `SKIP_SAME_DIRECTION` — Already holding correct direction, skip
- `CLOSE_THEN_ENTER` — Opposite position exists, close it first, then enter
- `SKIP_HEDGED` — Both long and short units exist (hedged), skip for safety

### D. Risk Management & Position Lifecycle (Phase 2)

| File | Component | Purpose | Live | Controlled By |
|---|---|---|---|---|
| [risk_integration.py](utils/risk_integration.py#L624) | `manage_open_positions()` | Master orchestrator: load clusters, reconcile OANDA, execute risk actions, persist | **YES** (if flag enabled) | `ENABLE_DYNAMIC_RISK_MANAGER` |
| [dynamic_risk_manager.py](utils/dynamic_risk_manager.py) | `DynamicRiskManager` state machine | Position lifecycle: INIT → BREAK_EVEN → TRAILING_CHANDELIER → TIME_DECAY → CLOSED | **YES** (if flag enabled) | Cluster lifecycle |
| [pyramid_cluster.py](utils/pyramid_cluster.py) | `PyramidCluster` | Position wrapper holding PositionUnits + shared RiskManager; guards size, entry price, R | **YES** (if flag enabled) | Created on entry, managed via risk_integration |
| [cluster_state_store.py](utils/cluster_state_store.py) | `ClusterStateStore` | JSON-locked persistence for open clusters | **YES** (if flag enabled) | Every cycle load/save |

**Risk Actions per Cycle (if position is managed):**
1. **BREAK_EVEN** — Move SL to entry price (after 1R profit reached)
2. **TRAILING_CHANDELIER** — Dynamic SL trailing via highest-high/lowest-low since entry (K=3.0 default)
3. **TIME_DECAY** — Reduce position size if trade is open > 24h (default T_EXPECTED_HOURS)
4. **FULL_CLOSE** — Close entire position if time-decay threshold hit
5. **NO_CHANGE** — Hold current SL

**Position Persistence:**
- File: `state/open_clusters.json` (FileLocked)
- Schema: `{schema_version: 1, clusters: {USD_JPY: {...}, ...}}`
- Survives process restart; authoritative; read/write every cycle

### E. Order Execution

| File | Function | Purpose | Trigger | Live |
|---|---|---|---|---|
| [oanda_execution.py](utils/oanda_execution.py#L20) | `open_oanda_order()` | Submit market order with SL/TP on fill | Entry signal approved + direction ok | **YES** (if risk manager enabled) |
| [trading_core.py](utils/trading_core.py) | `execute_market_trade()` | Legacy order path (if risk manager disabled) | Entry signal approved + direction ok | Conditional |

**OANDA Order Payload:**
- Type: `MARKET`
- Units: From `RISK_PROFILE[RISK_LEVEL]["units"]` (e.g., 10,000)
- Stop Loss: From signal or risk calculation
- Take Profit: From signal or risk calculation
- Time-in-Force: `FOK` (Fill or Kill)

**Return:** Fill dict with `{status, order_id, trade_id, filled_price, current_units, ...}`

### F. Observation & Instrumentation (V2 Phase 4.1)

| File | Function | Purpose | Input | Output | Affects Trading |
|---|---|---|---|---|---|
| [signal_instrumentation.py](utils/signal_instrumentation.py#L301) | `log_executed_signal()` | Records executed signal with diagnostics (slope, volatility, price location, thesis_label) | Cycle ID, pair, direction, diagnostics dict | Appends JSON-line to `logs/v2_signal_observations.jsonl` | **NO** — observation only |
| [signal_instrumentation.py](utils/signal_instrumentation.py#L319) | `log_trade_outcome()` | Records position close with outcome (entry, exit, realized R) | Instrument, direction, prices, close reason | Appends JSON-line to `logs/v2_trade_outcomes.jsonl` | **NO** — observation only |

**Key Property:** Observation logging is **wrapped in try/except**. If logging fails, trade execution is **not** aborted. Observation never influences trading decisions, never makes broker API calls, never modifies cluster state.

**Joins:** Outcome records can be correlated to executed signal records via `instrument` (current architecture assumes ≤1 open cluster per instrument at a time).

### G. Data Acquisition

| File | Function | Purpose | API | Frequency |
|---|---|---|---|---|
| [strategy_helpers.py](utils/strategy_helpers.py) | `get_candles()` | Fetch OANDA candles | OANDA GET `/candles` | Every cycle (4 pairs × 3 timeframes = 12 calls minimum) |
| [trading_core.py](utils/trading_core.py) | `get_latest_price()` | Fetch live bid/ask | OANDA GET `/pricing` | Per candidate pair, per risk action |
| [currency_strength.py](utils/currency_strength.py) | `build_strength_matrix()` | Calls get_candles for each pair on weekly/daily | OANDA GET `/candles` | Once per cycle (memoized in safe_build_strength_matrix_once) |

---

## 5. END-TO-END WORKFLOW & DATA FLOW

### Per-Cycle Execution (15-minute interval)

**Trigger:** Cron job executes `python /home/nie/projects/gemini_api/scheduled_runner_v1.3.py`

**Timeline:**

```
T+0:00 — Cron trigger
  └─ New Python process starts
     └─ __main__ block [line 172–187]
        └─ Print config banner (including ENABLE_DYNAMIC_RISK_MANAGER status) [line 56]
        └─ Call run_cycle() [line 187]

T+0:05 ≈ PHASE A: Manage Existing Risk
  └─ Load all cluster state [cluster_state_store.py line 172]
  └─ For each instrument with open cluster:
     ├─ Reconcile vs OANDA [risk_integration.py:286]
     │   └─ Fetch live positions from OANDA GET /trades
     │   └─ Remove closed units; sync size drifts
     ├─ Fetch market context (price, ATR, highest-high, lowest-low) [risk_integration.py:330]
     ├─ Call risk_manager.update() [dynamic_risk_manager.py]
     │   └─ Compute next action based on state machine
     ├─ Execute action (UPDATE_SL via PUT /trades or CLOSE via DELETE /trades) [risk_integration.py:398]
     └─ Persist updated cluster (or delete if closed) [cluster_state_store.py]
  └─ Log outcome if position closed [signal_instrumentation.py:log_trade_outcome()]
  └─ Return set of managed instruments (e.g., {USD_JPY, GBP_JPY})

T+0:10 ≈ PHASE B: Scan for Signals
  └─ Call analyze_custom_strategy() [custom_strategy_v1.py:606]
     ├─ Fetch candles for all pairs (H4, H1, M30) [strategy_helpers.py]
     ├─ Build strength matrix (weekly + daily candles) [currency_strength.py]
     ├─ For each pair:
     │   ├─ Compute MA5 alignment (all 3 timeframes) [strategy_helpers.py:check_ma5_alignment]
     │   ├─ If aligned:
     │   │   ├─ Compute SL/TP (ATR-based with vol scaling) [strategy_helpers.py]
     │   │   ├─ Log signal observation [signal_instrumentation.py:log_signal_observation]
     │   │   └─ Append to signals list
     │   └─ Store diagnostics (slope, volatility, price_location)
     └─ Return all signals; store best in _last_signal
  └─ Call get_last_signal() [custom_strategy_v1.py:615]
     └─ Return {pair, action, entry, stop_loss, take_profit, risk_reward, diagnostics}

T+0:15 ≈ PHASE C: Resolve Position Direction
  └─ If signal is None:
     └─ Print "No qualifying signals this cycle. HOLD." [line 82]
     └─ Exit run_cycle()
  └─ Extract pair, action from signal
  └─ If pair in managed_instruments (from Phase A):
     └─ Print "{pair} already under dynamic risk management. Skipping new entry."
     └─ Exit run_cycle()
  └─ Call resolve_and_prepare_entry(pair, action) [position_direction.py:110]
     ├─ Query OANDA GET /positions/{pair}
     ├─ Determine decision (ENTER / SKIP_SAME_DIRECTION / etc.)
     └─ If CLOSE_THEN_ENTER:
        └─ Call close_position(pair) [trading_core.py]
           └─ DELETE /trades for opposite direction
  └─ Handle decision:
     ├─ SKIP_SAME_DIRECTION → Exit run_cycle()
     ├─ SKIP_HEDGED → Exit run_cycle()
     ├─ CLOSE_THEN_ENTER → Print close message
     └─ ENTER → Proceed to Phase D

T+0:18 ≈ PHASE D: Execute Entry
  └─ Print signal details (pair, action, SL, TP, RR) [line 117–121]
  └─ If ENABLE_DYNAMIC_RISK_MANAGER:
     ├─ Call open_oanda_order(signal, units) [oanda_execution.py:20]
     │   └─ POST /orders to OANDA
     │   └─ Return fill: {status, order_id, trade_id, filled_price, current_units}
     ├─ If status == SUCCESS:
     │   ├─ Call new_cluster_from_fill(signal, fill) [risk_integration.py:521]
     │   │   └─ Create PyramidCluster + DynamicRiskManager
     │   ├─ Call save_cluster_data(pair, cluster.to_dict()) [risk_integration.py:170]
     │   │   └─ Write to state/open_clusters.json with FileLock
     │   ├─ Call log_executed_signal() [signal_instrumentation.py:301] with thesis_label
     │   └─ Print "✅ Order filled: {trade_id} @ {filled_price}"
     └─ Else:
        └─ Print "❌ Order NOT confirmed: {message}"
  └─ Else (risk manager disabled):
     ├─ Build TradeSignal object
     ├─ Call execute_market_trade(signal, units) [trading_core.py]
     └─ Print success/failure

T+0:20 ≈ Process exit, Python process terminates
  └─ Next cycle will be triggered at T+15:00
```

**API Call Volume per Cycle:**
- Candle fetches: ~12 (4 pairs × 3 timeframes)
- Position queries: ~8 (per managed instrument + per candidate)
- Price queries: ~4 (per candidate)
- Order submit: 0–1 (if signal qualifies + entry approved)
- Risk actions (SL/TP updates, closes): 0–∞ (per managed instrument)
- **Total:** 24–40 API calls average; up to 100+ if many risk actions

---

## 6. CORE TRADING LOGIC

### A. Entry Signal Generation

**Required Alignment:** All three timeframes (H4, H1, M30) must agree on direction (BUY or SELL).

**Input Data per Pair:**
1. Candles: H4 (24 bars), H1 (72 bars), M30 (240 bars)
2. EMA: EMA(5) computed from each candle set
3. Price: Current bid/ask

**Alignment Logic:**
```python
def check_ma5_alignment(pair, timeframes, require_aligned=True):
    # Timeframes: [H4, H1, M30]
    # Returns: (direction, reason) where direction = "BUY" | "SELL" | None
    
    for tf in timeframes:
        candles = get_candles(pair, tf, count=...)
        close_prices = [c["bid"]["c"] for c in candles]
        ema5 = compute_ema(close_prices, period=5)
        
        if close_prices[-1] > ema5[-1]:
            direction = "BUY"
        else:
            direction = "SELL"
        
        # Store direction per timeframe
    
    # Check agreement:
    if all directions agree:
        return (direction, "All timeframes aligned")
    else:
        return (None, "Timeframes misaligned")
```

**Strength Matrix:**
- Computed once per cycle (memoized)
- Uses weekly + daily candles
- Result: {USD: score, EUR: score, GBP: score, AUD: score, JPY: score}
- Score = weighted average of (close - EMA) / ATR

**Pair Ranking:**
```python
strength_vs_jpy = {
    "USD_JPY": USD_strength - JPY_strength,
    "EUR_JPY": EUR_strength - JPY_strength,
    ...
}
```

**SL/TP Calculation (ATR-based):**
```python
atr = compute_atr(pair, period=14, lookback=100)

if direction == "BUY":
    entry_price = current_ask
    stop_loss = entry_price - (atr * ATR_SL_MULTIPLIER)  # default 2.2
    
    # Volatility scaling on TP:
    vol_class = classify_volatility(atr_z_score)  # NORMAL, HIGH, LOW, EXTREME
    if vol_class == "HIGH":
        tp_multiplier = 1.8
    elif vol_class == "LOW":
        tp_multiplier = 2.5
    else:
        tp_multiplier = 2.0
    
    take_profit = entry_price + (atr * tp_multiplier)

risk_distance = entry_price - stop_loss
reward_distance = take_profit - entry_price
risk_reward = reward_distance / risk_distance

if risk_reward < MIN_RR (1.2):
    skip signal
```

**Position Sizing:**
- All entry units from `RISK_PROFILE[RISK_LEVEL]["units"]` (fixed, no Kelly, no dynamic sizing)
- No pyramiding in current strategy

---

### B. Exit & Position Management (Phase 2 Risk Manager)

**State Machine Entry Points:**
1. **INIT**: Cluster created from fill
2. **BREAK_EVEN**: Triggered when unrealized_r ≥ RISK_BE_TRIGGER_R (1.0R default)
3. **TRAILING_CHANDELIER**: After BE, compute rolling highest-high/lowest-low; SL follows at K=3.0 ATR below high
4. **TIME_DECAY**: If trade open > T_EXPECTED_HOURS (24h), start reducing size (time_reduce_ratio=0.5 default)
5. **CLOSED**: Position closed (native TP/SL hit, manual close, time-decay final close, margin liquidation)

**Chandelier Trailing:**
```python
# Fetch H1 candles since entry (rolling window)
candles_since_entry = get_candles(pair, granularity="H1", start=entry_time)
highs = [c["bid"]["h"] for c in candles_since_entry]
lows = [c["bid"]["l"] for c in candles_since_entry]

highest_high = max(highs)
lowest_low = min(lows)

# For BUY position:
atr = get_atr(...)
chandelier_sl = highest_high - (atr * K)  # K=3.0

# Move SL to chandelier_sl if it's better (higher for BUY, lower for SELL)
if chandelier_sl > current_sl:
    new_sl = chandelier_sl
```

**Time Decay:**
```python
elapsed_hours = (now - entry_time) / 3600
if elapsed_hours > T_EXPECTED_HOURS:
    if close_reason_condition:
        action = FULL_CLOSE
    else:
        # Partial reduce
        close_units = current_units * time_reduce_ratio
        action = PARTIAL_CLOSE(close_ratio=time_reduce_ratio)
```

**Reconciliation vs OANDA:**
- Every cycle, fetch live position via GET /trades
- If trade_id no longer open → unit is removed from cluster
- If size drifted → update to OANDA's reported size (broker is source of truth)

---

### C. Observation & Instrumentation

**Observation is strictly passive:**
- No conditionals in run_cycle depend on observation
- No state reads from observation files
- Logging failures are caught and never propagate to trading logic
- Observation threads do not lock cluster updates

**Recorded Fields (log_executed_signal):**
```json
{
  "log_type": "signal_executed",
  "timestamp_utc": "2026-08-26T15:30:00Z",
  "cycle_id": "2026-08-26T15:30:00Z",
  "pair": "EUR_JPY",
  "direction": "BUY",
  "thesis_label": "BUY_STRONG_EXTREME",  // Derived: direction_slope_volatility
  "diagnostics": {
    "slope": {"combined_label": "STRONG", "...": "..."},
    "volatility": {"class": "EXTREME", "z_score": 2.5},
    "price_location": {"cluster_strength": 1, "...": "..."}
  }
}
```

**Recorded Fields (log_trade_outcome):**
```json
{
  "log_type": "trade_outcome",
  "timestamp_utc": "2026-08-26T16:45:00Z",
  "instrument": "EUR_JPY",
  "direction": 1,
  "entry_price_0": 159.500,
  "r_unit_0": 0.040,
  "close_price": 159.200,  // Approximation, not exact fill price
  "close_price_source": "reconciliation_detected_external_close",
  "realized_r": -0.75,
  "close_reason": "closed_externally_unknown",
  "final_state": "CLOSED"
}
```

---

## 7. CONFIGURATION & PARAMETERS

All parameters live in [config.py](config.py). Below are the parameters that **materially affect runtime behavior**:

| Parameter | Value | Unit | Location | Effect |
|---|---|---|---|---|
| **Scheduling** |
| `CHECK_INTERVAL_MINUTES` | 15 | minutes | Line ~30 | Cron frequency |
| `SIGNAL_TIMEFRAMES` | ["H4", "H1", "M30"] | granularity | Line ~45 | Alignment check scope |
| `REQUIRE_ALIGNED` | 3 | count | Line ~50 | All must agree |
| **Pairs** |
| `TRADE_PAIRS` | ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"] | instrument | Line ~35 | Traded universe |
| **Position Sizing** |
| `RISK_LEVEL` | 10 | level 1–10 | Line ~80 | Maps to RISK_PROFILE units |
| `RISK_PROFILE[10]` | {"units": 10000, "min_confidence": 0.40} | units | Line ~85 | Entry size (fixed, no scaling) |
| **Entry Thresholds** |
| `MIN_VALID_PAIRS_TO_TRADE` | 2 | count | Line ~55 | Abort if < 2 pairs align |
| `MIN_RR` | 1.2 | ratio | custom_strategy_v1.py:330 | Skip if Risk:Reward < 1.2 |
| **ATR & Volatility** |
| `JPY_ATR_PERIOD` | 14 | candles | Line ~125 | ATR lookback |
| `JPY_ATR_HISTORY_LOOKBACK` | 100 | candles | Line ~130 | ATR z-score base |
| `JPY_ATR_SL_MULTIPLIER_NORMAL` | 2.2 | × ATR | Line ~135 | SL distance (normal vol) |
| `JPY_ATR_SL_MULTIPLIER_HIGH_VOL` | 2.8 | × ATR | Line ~140 | SL distance (high vol) |
| `JPY_ATR_SL_MULTIPLIER_LOW_VOL` | 1.6 | × ATR | Line ~145 | SL distance (low vol) |
| `JPY_ATR_RR_MULTIPLE` | 2.0 | × ATR | Line ~150 | TP distance base |
| **Phase 2 Risk Management** |
| `ENABLE_DYNAMIC_RISK_MANAGER` | True | boolean | Line ~281 | Activate risk layer |
| `RISK_ATR_MULTIPLIER_INIT` | 2.0 | × ATR | Line ~300 | Initial SL for break-even |
| `RISK_BE_TRIGGER_R` | 1.0 | R units | Line ~305 | Profit threshold for BE |
| `RISK_CHANDELIER_K_DEFAULT` | 3.0 | × ATR | Line ~310 | Trailing SL constant |
| `RISK_ENABLE_TIME_STOP` | True | boolean | Line ~315 | Enable time-decay closes |
| `RISK_T_EXPECTED_HOURS` | 24.0 | hours | Line ~320 | Hold duration before time-decay |
| `RISK_TIME_REDUCE_THRESHOLD` | 1.0 | R units | Line ~325 | Profit to trigger reduce |
| `RISK_TIME_REDUCE_RATIO` | 0.5 | fraction | Line ~330 | Reduce by 50% |
| `RISK_TIME_EXIT_THRESHOLD` | 1.5 | R units | Line ~335 | Profit to trigger full close |
| `RISK_TIME_TIGHTEN_THRESHOLD` | 1.5 | R units | Line ~340 | Profit to tighten SL to BE |
| **API Credentials** |
| `OANDA_ACCOUNT_ID` | (from .env) | account ID | Line ~10 | Demo account identifier |
| `OANDA_API_TOKEN` | (from .env) | token | Line ~15 | OANDA authentication |
| `USE_GEMINI_AI` | False | boolean | Line ~90 | Disable Gemini (unused in live path) |
| **Persistence** |
| `CLUSTER_STATE_PATH` | "state/open_clusters.json" | path | Line ~285 | Position state file |

---

## 8. PERSISTENCE & STATE MANAGEMENT

### A. Position State (Cluster State)

**File:** `state/open_clusters.json`

**Lock:** `state/open_clusters.json.lock` (FileLock, exclusive access)

**Owner:** [cluster_state_store.py](utils/cluster_state_store.py)

**Schema:**
```json
{
  "schema_version": 1,
  "clusters": {
    "USD_JPY": {
      "direction": 1,
      "entry_price_0": 150.250,
      "entry_time": "2026-08-26T10:00:00+00:00",
      "atr_entry": 0.150,
      "units": [
        {
          "size": 10000,
          "trade_id": "T-12345",
          "entry_price": 150.250,
          "entry_time": "2026-08-26T10:00:00+00:00"
        }
      ],
      "risk_manager": {
        "state": "BREAK_EVEN",
        "entry_time": "2026-08-26T10:00:00+00:00",
        "entry_price_0": 150.250,
        "r_unit_0": 0.040,
        "current_sl": 150.100,
        "chandelier_k": 3.0,
        "time_reduce_fired": false,
        ...
      },
      "risk_config": {
        "atr_multiplier_init": 2.0,
        "be_trigger_r": 1.0,
        ...
      }
    },
    "EUR_JPY": { ... }
  }
}
```

**Lifecycle:**
1. **Created:** On trade fill (via `new_cluster_from_fill()`)
2. **Updated:** Every cycle (via `manage_open_positions()`)
3. **Deleted:** When position closes (marked with `mark_closed()`, then deleted from store)
4. **Survives:** Process restart, system reboot
5. **Authoritative:** Yes (OANDA is secondary/source-of-truth for live sizes only)

**Read/Write Points:**
- **Read:** `run_cycle()` Phase A, line 60 [risk_integration.manage_open_positions()]
- **Write:** On entry fill (line 154) + after each risk action (line 606)
- **Corrupt File:** Automatically quarantined with `.corrupt.TIMESTAMP` suffix; fresh state created

---

### B. Signal Observations (Instrumentation)

**File:** `logs/v2_signal_observations.jsonl`

**Format:** JSON-lines (one record per line, append-only)

**Lock:** Threading lock (in-memory, not file-level)

**Record Type:** log_executed_signal

**Lifecycle:**
1. **Created:** Per trade entry (appended by custom_strategy_v1.py line 547)
2. **Updated:** No (append-only)
3. **Deleted:** Never (intended for offline analysis)
4. **Survives:** Indefinitely (no cleanup in current codebase)
5. **Authoritative:** No (for analysis only, not consulted by trading logic)

---

### C. Trade Outcomes (Instrumentation)

**File:** `logs/v2_trade_outcomes.jsonl`

**Format:** JSON-lines (one record per line, append-only)

**Lock:** Threading lock (in-memory)

**Record Type:** log_trade_outcome

**Lifecycle:**
1. **Created:** On position close (appended by risk_integration.py line 607)
2. **Updated:** No (append-only)
3. **Deleted:** Never
4. **Survives:** Indefinitely
5. **Authoritative:** No (for analysis only)

---

### D. Thesis State (Inactive)

**File:** `state/thesis_observation_state.json` (hypothetical, not currently written)

**Status:** Exists in codebase but **not invoked in live path**. 

- [thesis_state_store.py](utils/thesis_state_store.py) exists and defines persistence
- No call from `run_cycle()` to any thesis-related functions
- Observation modules ([thesis_observation.py](utils/thesis_observation.py) — actually a test file) import thesis functions but are not imported by trading path

---

## 9. FAILURE, RESTART & RECOVERY BEHAVIOR

### A. API Call Failures

| Failure | Module | Behavior | Outcome |
|---|---|---|---|
| OANDA candle fetch fails | [strategy_helpers.py](utils/strategy_helpers.py) | Exception caught in `analyze_custom_strategy()` try/except (line 606) | No signal generated this cycle; cycle completes silently |
| OANDA position query fails | [position_direction.py](utils/position_direction.py) | Exception caught (line 110) | Log error, return `PositionDirectionError`; skip entry this cycle |
| OANDA order submit fails | [oanda_execution.py](utils/oanda_execution.py) | Returns `{status: "FAILED", message: "..."}` | Print error, no position created; cycle continues |
| Risk action (SL update) fails | [risk_integration.py](utils/risk_integration.py:398) | Exception raised, caught in `manage_open_positions()` | Cluster persisted in pre-action state; retry next cycle |

### B. State File Issues

| Scenario | Module | Behavior | Outcome |
|---|---|---|---|
| Cluster state file missing | [cluster_state_store.py](utils/cluster_state_store.py:100) | `load_thesis_state()` returns empty state | Fresh state created; position lifecycle restarts (Phase A no-op this cycle) |
| Cluster state file corrupted (invalid JSON) | [cluster_state_store.py](utils/cluster_state_store.py:115) | File quarantined as `.corrupt.TIMESTAMP`; fresh state created | Existing clusters not recovered this cycle; manually intervene if critical |
| State file lock timeout | [cluster_state_store.py](utils/cluster_state_store.py) | Timeout exception (10s default) | Raises ClusterStateStoreError; caught in risk_integration, logged; position action skipped |

### C. Position/Reconciliation Issues

| Scenario | Module | Behavior | Outcome |
|---|---|---|---|
| Position closed externally (manual, TP hit, margin liquidation, etc.) | [risk_integration.py](utils/risk_integration.py:286) | `reconcile_with_oanda()` detects trade_id not in live trades | Unit removed; cluster marked closed if no units left; outcome logged |
| Size drift (broker adjustment, partial fill, etc.) | [risk_integration.py](utils/risk_integration.py:300) | Size synced to OANDA's reported size (broker is source of truth) | Cluster updated; SL/TP recalculated on next risk action |
| Hedged position (both long and short) | [position_direction.py](utils/position_direction.py:140) | Decision = `SKIP_HEDGED` | No close; printed as ambiguous; manual intervention required |

### D. Process Restart

| Trigger | Behavior |
|---|---|
| Process crashes / OOM | Next cron cycle starts fresh Python process → `run_cycle()` loads cluster state from JSON → continues normal flow |
| Machine reboot | Next cron cycle after reboot starts fresh → cluster state file survives → position management resumes |
| Scheduled restart (deployment, etc.) | If restart is timed mid-position, position state is durable; next cycle reconciles with OANDA and continues |

**Durability:** Open clusters and observation logs survive process/system restarts. Observation logs are immutable (append-only). Position state is reloaded and reconciled every cycle.

---

## 10. TESTS & VERIFICATION

### A. Test Files in Repository

| File | Type | Coverage | Status |
|---|---|---|---|
| `tests/test_custom_strategy_instrumentation.py` | Integration | Strategy signal generation, diagnostics capture | Present |
| `tests/test_signal_instrumentation.py` | Unit | Log record creation (executed_signal, trade_outcome) | Present |
| `tests/test_risk_integration_instrumentation.py` | Integration | Risk action execution, logging passivity | Present |
| `tests/test_position_direction.py` | Unit | Position direction resolution, hedge detection | Present (moved from utils/) |
| `tests/test_phase2_activation.py` | Integration | Phase 2 risk manager activation | Present (moved from utils/) |
| `utils/thesis_observation.py` | Test suite | Thesis observation pure functions, subprocess boundary tests | Present (file is test content, implementation missing) |
| `tests/test_thesis_observation.py` | Test suite | Duplicate/moved thesis tests | Present |
| `utils/test_thesis_label.py` | Unit | thesis_label derivation in log_executed_signal | Added 2026-08-26 |

### B. Verified Behaviors

| Behavior | Test | Result |
|---|---|---|
| Signal generation produces aligned signals only | `test_custom_strategy_instrumentation.py` | ✅ All 3 timeframes aligned before entry |
| Observation logging never aborts trade | `test_risk_integration_instrumentation.py` | ✅ Logging failure wrapped in try/except |
| Position direction prevents same-direction re-entry | `test_position_direction.py` | ✅ SKIP_SAME_DIRECTION decision |
| Cluster state persists across process restart | `test_phase2_activation.py::test_*_real_subprocess` | ✅ Subprocess boundary tests pass |
| thesis_label derivation handles missing diagnostics | `utils/test_thesis_label.py` | ✅ 6 tests pass (2026-08-26) |

### C. Verification Gaps

| Behavior | Status |
|---|---|
| **Live OANDA Trading** | NOT VERIFIED — Only tested on demo account; no live P&L data available |
| **Margin Liquidation** | NOT TESTED — No test simulates account bankruptcy |
| **Rate Limiting** | NOT TESTED — OANDA rate limits not mocked; behavior unknown |
| **Data Gaps** | NOT TESTED — No test for missing/stale candles |
| **Thesis Integration** | NOT ACTIVE — Observation modules exist but not invoked in live path |

---

## 11. DESIGN CONSTRAINTS & IMPORTANT ASSUMPTIONS

### Critical Constraints (Enforced in Current Implementation)

1. **One Entry Per Cycle**
   - Each 15-minute cron cycle scans for signals and executes at most one order
   - Rationale: Prevents over-allocation and simplifies state management
   - Enforced by: [scheduled_runner_v1.3.py](scheduled_runner_v1.3.py#L82) early exit if signal found

2. **No Pyramiding**
   - Strategy never generates "add to position" signals
   - Rationale: Simplifies risk calculation and cluster state
   - Enforced by: [custom_strategy_v1.py](custom_strategy_v1.py#L545) only returns single best signal

3. **JPY Pairs Only**
   - Only `USD_JPY`, `EUR_JPY`, `GBP_JPY`, `AUD_JPY` are traded
   - Rationale: Focus on carry trade pairs, simplifies FX dynamics
   - Enforced by: [config.py](config.py#L35) TRADE_PAIRS list

4. **All Timeframes Must Align**
   - Entry signals require MA5 alignment on H4, H1, **and** M30
   - Rationale: High confirmation bar, reduces false signals
   - Enforced by: [strategy_helpers.py](utils/strategy_helpers.py#L195) alignment check

5. **Completed Candles Only**
   - Candle data is fetched as-is from OANDA; "completed" flag is used to filter forming candles
   - Rationale: Prevents whipsaw from incomplete candles
   - Enforced by: [strategy_helpers.py](utils/strategy_helpers.py#L240) filter on `complete` flag

6. **Max One Cluster per Instrument**
   - Current risk architecture assumes ≤1 open PyramidCluster per instrument
   - Rationale: Simplifies cluster state identity and outcome correlation
   - Enforced by: [run_cycle()](scheduled_runner_v1.3.py#L95) skips entry if pair already managed

7. **OANDA is Source of Truth for Position Size**
   - Live position size is always synced to OANDA's reported size
   - Rationale: Broker is authoritative; local state can diverge (partial fills, manual adjustments)
   - Enforced by: [reconcile_with_oanda()](utils/risk_integration.py#L300) every cycle

8. **Observation is Passive & Asynchronous**
   - Logging never influences entry/exit logic and never blocks order execution
   - Rationale: Observation is for offline analysis; live trading must not depend on it
   - Enforced by: [log calls wrapped in try/except](utils/risk_integration.py#L608) with no signal back to caller

9. **Cron-Driven Execution**
   - No in-process loop; each cycle is a fresh Python process
   - Rationale: Simple restartability, fault isolation, clean state each cycle
   - Enforced by: System cron job scheduling

10. **Demo Account Default**
    - System is configured for demo account; live trading requires explicit config change
    - Rationale: Safety guard against accidental live trading
    - Enforced by: OANDA_ACCOUNT_ID configuration

---

## 12. KNOWN LIMITATIONS & UNKNOWNS

### Known Limitations (Visible in Current Implementation)

1. **No Live OANDA Verification**
   - Code is tested on demo account; no live trading verification data available
   - Impact: Unknown slippage, real P&L, actual margin behavior

2. **Thesis Observation Inactive**
   - Modules exist but are not invoked from live path
   - Impact: No real-time thesis state tracking; intended feature not operational

3. **No Pyramiding**
   - Strategy cannot scale into profitable positions
   - Impact: Misses scaling opportunities

4. **Fixed Position Size**
   - No dynamic sizing based on account equity or volatility
   - Impact: Over/under-allocation risk if equity changes significantly

5. **Candle Stale Risk**
   - If OANDA candle API fails, fallback to Yahoo Finance (UNKNOWN if this works in practice)
   - Impact: Delayed/stale candle data could cause misaligned signals

6. **Manual Intervention Not Supported**
   - No API to manually close/adjust positions outside of risk manager
   - Impact: Manual override requires direct OANDA API calls outside this system

7. **No Test Coverage for Rate Limits**
   - OANDA rate limits are not tested; behavior unknown
   - Impact: Could hit rate limit and fail mid-cycle

### UNKNOWN (Cannot be established from repository)

1. **Live OANDA Behavior**
   - How does OANDA behave on margin liquidation?
   - Does the omit-takeProfit contract (OANDA TradeCRCDO null-vs-omit semantics) work as documented?
   - Status: **NOT YET LIVE-VERIFIED** (noted in [risk_integration.py](utils/risk_integration.py#L42))

2. **Observation Log Join Quality**
   - Current assumption: ≤1 open cluster per instrument → outcome can join to signal by instrument alone
   - Reality: What if overlapping clusters exist (impossible by design)?
   - Status: **By design constraint, but no test for multi-cluster scenarios**

3. **Time Zone Handling**
   - All timestamps are UTC; no handling of DST or regional market hours
   - Impact on behavior: **UNKNOWN**

4. **Gemini AI Integration**
   - USE_GEMINI_AI flag exists but is not used in live path
   - Design intention for Gemini: **UNKNOWN**

5. **Thesis State Purpose**
   - Thesis state persistence exists but is not invoked
   - What was the original design intent?: **UNKNOWN**

6. **Missing Candles**
   - What happens if a candle request returns fewer bars than requested?
   - Current behavior: **NOT TESTED**

7. **Market Gaps & Slippage**
   - No model for slippage between signal computation and order fill
   - Real slippage impact: **UNKNOWN**

---

## 13. ONE-PAGE SYSTEM SUMMARY

### Runtime

**Trigger:** Cron job every 15 minutes  
**Process:** Fresh Python process per cycle, executes [scheduled_runner_v1.3.py](scheduled_runner_v1.3.py#L49)  
**Lifecycle:** Load state → Manage existing positions → Scan for signals → Resolve direction → Execute entry → Persist → Exit

**Execution Time:** ~20–30 seconds average (varies with API response times)

---

### Decision Path (Entry)

```
Signal Generation:
  1. Fetch candles (H4, H1, M30) for all 4 JPY pairs from OANDA
  2. Compute currency strength matrix (weekly + daily data)
  3. Rank pairs by strength vs JPY
  4. For each pair: Check MA5 alignment across all 3 timeframes
  5. If aligned: Compute SL/TP via ATR with volatility scaling
  6. Validate Risk:Reward ≥ 1.2
  7. Log observation (non-blocking)
  8. Keep best signal in memory

Entry Approval:
  1. If no signal: Hold
  2. If signal's pair already managed: Skip (risk layer already active)
  3. Query OANDA position direction
  4. Decide: ENTER / SKIP / CLOSE_OPPOSITE_FIRST
  5. If direction is wrong (same-direction re-entry): Skip
  6. If both long + short (hedged): Skip
  7. Submit market order with SL/TP on fill
  8. On fill: Create cluster, persist state
```

---

### Risk Path (Position Management)

```
Every Cycle (if ENABLE_DYNAMIC_RISK_MANAGER=True):
  1. Load all cluster state from state/open_clusters.json
  2. For each open position:
     a. Reconcile size vs OANDA (broker is source of truth)
     b. Fetch live price, ATR, highest-high, lowest-low
     c. Run state machine (INIT → BE → CHANDELIER → TIME_DECAY → CLOSED)
     d. Compute risk action (UPDATE_SL | PARTIAL_CLOSE | FULL_CLOSE | NO_CHANGE)
     e. Execute action via OANDA API (PUT for SL update, DELETE for close)
     f. Update cluster state and persist
     g. Log outcome if closed

State Machine Triggers:
  - BREAK_EVEN: When unrealized_r ≥ 1.0R
  - TRAILING_CHANDELIER: After BE, continuous rolling SL
  - TIME_DECAY: After 24h, start reducing size
  - CLOSED: On native TP/SL hit or time-decay threshold
```

---

### State

**Persistent State:**
- `state/open_clusters.json` — All open positions + risk manager state + RiskConfig snapshot
- `logs/v2_signal_observations.jsonl` — Signal execution history (observation only)
- `logs/v2_trade_outcomes.jsonl` — Position close outcomes (observation only)

**Survives:** Process restart, system reboot  
**Authoritative:** Cluster state (position state); observation logs are historical  
**Reconciliation:** Every cycle, position sizes synced to OANDA (broker is source of truth)

---

### Observation

**What is observed:**
- Entry signal details: pair, action, SL/TP, Risk:Reward, slope/volatility/price-location diagnostics
- Thesis label: Derived from slope + volatility (BUY_STRONG_EXTREME, SELL_WEAK_NORMAL, etc.)
- Position close outcomes: Entry price, exit price, realized R, close reason

**Where stored:**
- `logs/v2_signal_observations.jsonl` (JSON-lines)
- `logs/v2_trade_outcomes.jsonl` (JSON-lines)

**Lifecycle:** Append-only, unbounded (no cleanup in current codebase)

**Passivity:** Observation is never consulted by trading logic; logging failure never aborts trade

---

### External Dependencies

| Service | Purpose | Frequency |
|---|---|---|
| **OANDA v20 REST API** | Candles, position queries, order submission, SL/TP updates, closes | 24–40 calls/cycle |
| **Internet connectivity** | Required; system fails silently if unreachable | Every cycle |
| **System cron daemon** | Triggers Python process every 15 minutes | System level |
| **Python 3.12 (via Miniforge)** | Runtime | Required |
| **OANDA Account (demo)** | Paper account for testing; live account not supported yet | Required |

**Optional (Disabled):**
- Google Gemini AI — Flag USE_GEMINI_AI exists but is not used in live trading path
- Yahoo Finance — Fallback only if OANDA candles fail

---

### Critical Constraints for Future Developers

1. **One entry per cycle** — Do not add multi-entry logic without redesigning state identity
2. **All 3 timeframes must align** — This is the core filter; changing it changes signal quality significantly
3. **Observation must remain passive** — No observation data should influence trading decisions
4. **OANDA is source of truth** — Local position state is secondary; always reconcile on load
5. **Cluster state is identity** — Each instrument has at most one PyramidCluster; uniqueness is critical
6. **Risk layer is optional** — Set ENABLE_DYNAMIC_RISK_MANAGER=False to disable Phase 2 entirely (reverts to simple execution)
7. **State survives restarts** — Position state is durable; clean shutdown and process termination are safe
8. **Demo-by-default** — Live trading requires explicit config change; never auto-switch accounts
9. **Cron-driven only** — No in-process loop; scaling requires changes to execution model
10. **Observation logs unbounded** — Implement log rotation/cleanup before large-scale deployment

---

## DOCUMENT METADATA

| Field | Value |
|---|---|
| **Document Type** | Technical System Reference (As-Is) |
| **Repository** | liqinsg/gemini_ai (main branch) |
| **Audit Date** | 2026-08-26 |
| **Auditor** | Automated codebase analysis + manual verification |
| **Status** | Production trading code |
| **Last Updated** | 2026-08-26 |
| **Next Review** | After major feature changes or production incident |

---

## END OF SYSTEM REFERENCE

This document reflects the actual current state of the gemini_api repository. It is intended as a factual baseline for developers, auditors, and stakeholders. It makes no recommendations for change, only documents what currently exists and how it executes.
