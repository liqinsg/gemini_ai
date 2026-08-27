# QUICK AUDIT SUMMARY

**Status:** Execution path audit completed | **Date:** 2026-08-26

---

## THE CONTRADICTION EXPLAINED

**Observation 1:** "[RISK] No instruments currently under dynamic risk management."  
**Observation 2:** "[CYCLE] Already holding a BUY position in AUD_JPY matching the signal direction. Skipping."  

These refer to **two completely different position sources** and are **both correct**:

| Message | Source | Query | Result |
|---------|--------|-------|--------|
| "No instruments..." | Cluster state file (`state/open_clusters.json`) | Read JSON clusters dict | AUD_JPY key is absent |
| "Already holding..." | OANDA broker API (`/positions` endpoint) | Live OANDA query | AUD_JPY exists with 10000 BUY units |

**They measure different systems and are NOT contradictory.**

---

## EXECUTION ORDER (PROVEN)

```
1. Line 60:  manage_open_positions()  ← Phase A runs FIRST
   ├─ Reads cluster state (empty for AUD_JPY)
   ├─ Loops only over managed instruments (AUD_JPY not in it)
   └─ Returns empty list

2. Line 68:  Print "No instruments currently under dynamic risk management"

3. Line 74:  analyze_custom_strategy() ← Phase B generates signal
   └─ Finds valid BUY alignment for AUD_JPY

4. Line 93:  resolve_and_prepare_entry(pair, action) ← Phase C queries OANDA
   ├─ Calls get_open_position("AUD_JPY")
   ├─ Gets OANDA response: long 10000 units = BUY
   └─ Returns PositionDecision.SKIP_SAME_DIRECTION

5. Line 96:  Print "Already holding a BUY position in AUD_JPY matching the signal direction. Skipping."
   └─ Exit run_cycle()
```

---

## KEY FINDINGS

✅ **manage_open_positions() is NOT bypassed** — runs at line 60, before signal generation  
✅ **Time-decay/break-even/trailing logic never reaches AUD_JPY** — not in cluster state, not iterated in Phase A loop  
✅ **AUD_JPY exists at OANDA but is not registered with DynamicRiskManager** — not in `state/open_clusters.json`  
✅ **Both messages are factually correct** — they query different sources; both report accurate state  
✅ **System is working as designed** — unmanaged positions are detected at OANDA and correctly prevent same-direction re-entry  

---

## WHAT HAPPENED TO AUD_JPY THIS CYCLE

1. **Phase A (Position Management):** Runs normally, finds empty cluster state for AUD_JPY, nothing to manage
2. **Phase B (Signal Generation):** Detects valid alignment, generates BUY signal
3. **Phase C (Direction Check):** Queries OANDA, finds existing BUY position (same direction), correctly skips entry
4. **Result:** No risk actions, no new order, cycle completes

---

## ROOT CAUSE OF THE POSITION

The AUD_JPY position was opened **outside the runner's risk-management pipeline** (either manually, by another system, or before Phase 2 was activated). It exists at OANDA but is not registered in `state/open_clusters.json`, so Phase A's DynamicRiskManager doesn't know about it or manage it.

**By design:** Only positions created via `new_cluster_from_fill()` → `save_cluster_data()` are tracked in cluster state. Pre-existing or manual positions are outside DynamicRiskManager's scope.

---

## IS THIS A BUG?

**No.** The system is:
- ✅ Detecting the position correctly (at OANDA)
- ✅ Preventing same-direction re-entry (correct)
- ✅ Reporting its internal state accurately (no managed instruments)
- ✅ Running all phases in the correct order

The two log messages appear contradictory only if you assume they refer to the same state system — they don't. Once you trace the actual code, it's clear and consistent.

---

## COULD WE IMPROVE THIS?

**Possible enhancement (not a bug fix):** Auto-detect and adopt pre-existing positions into DynamicRiskManager in Phase A, so unmanaged positions also receive time-decay/trailing treatment.

**Cost:** Additional OANDA API call per cycle to query positions and compare with cluster state.  
**Complexity:** Would need to decide how to register pre-existing positions and handle partial fills/slippage.  
**Current design:** Positions not in cluster state are outside DynamicRiskManager's scope — by design, not oversight.

---

## FULL AUDIT DOCUMENT

See [FORENSIC_EXECUTION_PATH_AUDIT.md](FORENSIC_EXECUTION_PATH_AUDIT.md) for complete line-by-line analysis with source code evidence for every finding.

---

**Audit status:** Complete | **Confidence level:** PROVEN | **Code changes:** NONE
