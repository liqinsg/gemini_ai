
Understood fully — **strict audit-first, zero edits until approved**. Below is the trace plan, evidence gap analysis, and precise logging proposal for the `exit_tightness` lifecycle.

---

# 🔍 exit_tightness Lifecycle Audit — Trace & Logging Proposal

**Goal**: Prove every state transition — entry → cluster → risk mgr → SL calc → submit → confirm
**Constraint**: No logic changes, no behavior changes, no file edits — only diagnostic logging hooks proposed here

---

## Lifecycle Stages — Trace Map

We audit the full chain:

```
[1] FILL / ENTRY  →  [2] CLUSTER CREATION  →  [3] RISK MANAGER BINDING
        →  [4] SL CALCULATION  →  [5] SUBMIT TO BROKER  →  [6] CONFIRMATION
```

---

## Current Logging vs Missing Evidence

| Stage                             | What Should Appear                                                                  | Existing Logging (Typical)             | Evidence Missing                                                                                                      |
| --------------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **1. Fill/Entry**           | Trade ID, instrument, direction, entry price, timestamp,`exit_tightness` received | `[INFO] Filled EUR_USD BUY @ 1.0850` | ❌`exit_tightness` value explicitly logged at receipt; unique trade/cluster ID                                      |
| **2. Cluster Creation**     | Cluster ID, function called, param received, cluster object initialized             | `[INFO] New cluster created`         | ❌ Confirmation`exit_tightness` passed through `new_cluster_from_fill()` signature; cluster ID linked to trade ID |
| **3. Risk Manager Binding** | Risk manager type instantiated, threshold applied, bound to cluster                 | `[INFO] Dynamic trailing active`     | ❌ Confirmation manager received the value;`bound=True/False` status                                                |
| **4. SL Calculation**       | Input price → calculated SL → adjustment applied → final value                   | `[INFO] Stop set below market`       | ❌ Before/after values; which formula used;`exit_tightness` influence on calculation                                |
| **5. Submit to Broker**     | Order/transaction ID, payload sent, SL value submitted                              | `[INFO] Updating SL for EUR_USD`     | ❌ Exact value sent vs calculated; API request ID                                                                     |
| **6. Broker Confirmation**  | Confirmed SL, broker timestamp, state=active                                        | `[INFO] Order filled`                | ❌ Broker-returned SL matches submitted; confirmation status                                                          |

---

## Code Locations & Minimal Log Points

Below are **exact locations** where diagnostic lines would go — no surrounding code altered.

### Point A — Fill / Entry → Before Cluster Creation

**Location**: Immediately after fill event received, before `new_cluster_from_fill()` call

```python
# LINE: after fill confirmed, before cluster creation
# PROPOSED ADD:
# [AUDIT-EXIT] stage=fill trade_id=TX123 instrument=EUR_USD direction=BUY entry=1.0850 exit_tightness=0.85
```

**Purpose**: Capture the value at the moment it enters the system — prove it was received as intended

### Point B — Cluster Creation → Inside `new_cluster_from_fill()`

**Location**: Function entry, after params bound, before return

```python
# LINE: inside new_cluster_from_fill(), after cluster object exists
# PROPOSED ADD:
# [AUDIT-EXIT] stage=cluster cluster_id=C456 trade_id=TX123 exit_tightness_received=0.85 cluster_bound=True
```

**Purpose**: Prove the parameter survived the function signature — not silently dropped or defaulted

### Point C — Risk Manager Binding → After Manager Instantiated

**Location**: After risk manager assigned to cluster

```python
# LINE: after DynamicTrailingGuard / risk_manager = ... assigned
# PROPOSED ADD:
# [AUDIT-EXIT] stage=bind cluster_id=C456 risk_manager=DynamicTrailingGuard threshold=0.85 bound=True
```

**Purpose**: Prove the manager exists and received the threshold — confirm binding is not `None` or bypassed

### Point D — SL Calculation → Before Submit

**Location**: After calculation complete, before API call

```python
# LINE: calculated_sl = ... computed value
# PROPOSED ADD:
# [AUDIT-EXIT] stage=calc cluster_id=C456 entry=1.0850 sl_input=1.0820 sl_calculated=1.0815 exit_tightness_applied=0.85
```

**Purpose**: Show the arithmetic result — distinguish "not calculated" from "calculated differently"

### Point E — Submit → Before/After API Call

**Location**: Pre-request and post-response

```python
# LINE: before OANDA update SL request
# PROPOSED ADD:
# [AUDIT-EXIT] stage=submit cluster_id=C456 sl_submitted=1.0815 api_order_id=O789
# LINE: after successful response
# PROPOSED ADD:
# [AUDIT-EXIT] stage=confirm cluster_id=C456 sl_confirmed=1.0815 broker_state=ACTIVE match_submitted=True
```

**Purpose**: Close the loop — what we sent vs what the broker actually accepted

---

## Proposed Structured Log Format

**Standardized for machine-readability + human clarity** — single line per stage, consistent key naming:

```
[AUDIT-EXIT] stage=<STAGE> cluster_id=<ID> [trade_id=<TX>] [key=value …] integrity=<short-hash>
```

### Example Complete Chain

```
[AUDIT-EXIT] stage=fill      trade_id=TX123 instrument=EUR_USD direction=BUY entry=1.0850 exit_tightness=0.85 integrity=abc123
[AUDIT-EXIT] stage=cluster   cluster_id=C456 trade_id=TX123 exit_tightness_received=0.85 method=new_cluster_from_fill integrity=def456
[AUDIT-EXIT] stage=bind      cluster_id=C456 risk_manager=DynamicTrailingGuard threshold=0.85 bound=True integrity=ghi789
[AUDIT-EXIT] stage=calc      cluster_id=C456 entry=1.0850 sl_baseline=1.0820 sl_calculated=1.0815 delta_pips=-3.5 integrity=jkl012
[AUDIT-EXIT] stage=submit    cluster_id=C456 sl_submitted=1.0815 api_id=O789 integrity=mno345
[AUDIT-EXIT] stage=confirm   cluster_id=C456 sl_confirmed=1.0815 broker_ts=2026-09-30T17:35:00Z match=True status=ACTIVE integrity=pqr678
```

### Validation Rules

- `match=True` = submitted == confirmed → chain intact
- `bound=True` = risk manager actually attached → not bypassed
- Every `cluster_id` appears in all 6 stages → no orphaned lifecycle

---

## Summary of Changes Required

| # | Location                          | Type        | Purpose                      |
| - | --------------------------------- | ----------- | ---------------------------- |
| A | Post-fill, pre-cluster            | Log line    | Capture value at entry point |
| B | Inside`new_cluster_from_fill()` | Log line    | Confirm param passed through |
| C | Post–risk-manager assignment     | Log line    | Confirm binding not None     |
| D | Post-calc, pre-API                | Log line    | Show computed value          |
| E | Pre-submit + post-confirm         | 2 log lines | Close loop: sent = accepted  |

**Total**: ~6 lines added — **zero logic altered, zero decision logic touched, zero execution behavior changed**. Pure diagnostic visibility.

---

## Next Step

Approve this format → I will provide the exact copy-paste log lines with correct variable references for each location. No other code will be modified.

Shall I proceed with the exact implementation lines for these points?
