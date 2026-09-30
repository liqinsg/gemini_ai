下面是整合双方评审意见后的**最终确定性验证框架 v2.1**——严格分层、快照驱动、单变量验证、先审计再执行、完整可追溯。

---

# ✅ DETERMINISTIC DECISION VALIDATION — Final Architecture

**Version**: 2.1 | Date: 2026-09-30
**Core Insight**: Decision Validation ≠ Execution Validation ≠ LIVE Validation — never mix them.

---

## 🎯 Revised Blockers — 11 Pre-Flight Gates

**None skipped — all must pass before any comparison run**

| #  | Gate                               | What to Verify                          | Pass Condition                                                                           |
| -- | ---------------------------------- | --------------------------------------- | ---------------------------------------------------------------------------------------- |
| 1  | **Identify exact runner**    | Confirm which file is under test        | `scheduled_runner_v3.py` committed; no untracked `_op.py` shadow                     |
| 2  | **Git SHA pinning**          | Every run logs code version             | Header line:`GIT_SHA=abc123…`                                                         |
| 3  | **exit_tightness audit**     | Dynamic risk manager actually connected | Log shows:`cluster → risk_manager → SL submitted → broker confirmed` chain complete |
| 4  | **H4 candle close timing**   | Confirm OANDA actual boundaries vs UTC  | Fetch sample H4; verify cron fires*after* close with margin                            |
| 5  | **Account guard hard abort** | Mismatch = exit non-zero, not warning   | `EXPECT_ACCOUNT_ID` mismatch → FATAL                                                  |
| 6  | **Config hash**              | Prove resolved values match intended    | `CONFIG_HASH=sha256(merged_env)` logged; no drift                                      |
| 7  | **All flags explicit**       | Zero implicit defaults                  | Every`ENABLE_*`/threshold present in file; no fallback                                 |
| 8  | **News cache shared**        | Avoid double-quota consumption          | Single fetch → feed both configs                                                        |
| 9  | **Flock anti-overlap**       | One run at a time                       | `flock` wrapper; stale lock detection                                                  |
| 10 | **Snapshot determinism**     | Same inputs → reproducible             | Snapshot hash matches across re-runs                                                     |
| 11 | **Decision-log mode**        | Phase 1 = no orders at all              | `--log-decisions-only` enforced                                                        |

---

## 🏗️ Two-Tier Validation Architecture — Final Design

```
┌─────────────────────────────────────────────────────────────┐
│              TIER 1 — DETERMINISTIC DECISION VALIDATION      │
│  SAME SNAPSHOT → DIFFERENT CONFIG → DIRECT COMPARISON        │
│  NO TRADES → NO ACCOUNT CROSS-TALK → ZERO INTERFERENCE       │
├─────────────────────────────────────────────────────────────┤
│  MARKET SNAPSHOT LAYER                                       │
│  candles / strength matrix / MC / MACD / news / position state│
│  timestamped + hashed → immutable audit record              │
│  └── one fetch → fed to both configs                         │
│              │                     │                         │
│  BASELINE    │  env only           │  CANDIDATE              │
│  threshold=1.80                  threshold=1.65              │
│  everything else IDENTICAL        everything else IDENTICAL   │
│              │                     │                         │
│  DECISION A  │                     │  DECISION B             │
│              │                     │                         │
│  └───────────┴─────────────────────┘                         │
│                     DIFF ENGINE                               │
│  direction? same/opposite? reason? param changed?            │
│  Δ captured + attributed                                     │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│           TIER 2 — ISOLATED EXECUTION VALIDATION             │
│  Separate accounts → measure fills/spread/latency ONLY       │
│  NOT used to judge strategy quality                          │
├─────────────────────────────────────────────────────────────┤
│  Demo Account A (Baseline)        Demo Account B (Candidate) │
│  execution metrics logged         execution metrics logged   │
│  fill / slippage / latency        fill / slippage / latency   │
│  SL/TP round-trip confirm         SL/TP round-trip confirm    │
│  └── never compared for "better" → compared for reliability │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                    TIER 3 — LIVE ACTIVATION                   │
│  Single account → micro-lot sizing → phased ramp-up          │
│  Pre-commit: all Tier 1 + 2 gates passed                     │
└─────────────────────────────────────────────────────────────┘
```

**Key Principle**:

- **Tier 1 answers**: Did the parameter change *alter* decisions? → Pure, deterministic
- **Tier 2 answers**: Does the system *execute* reliably? → Broker mechanics only
- **Never** mix "which decision" with "which fill"

---

## 🧪 E1–E5 Reduced Scope — MAX_POSITIONS Held Constant Until Validated

**One variable at a time — no bundled changes**

| Exp          | Test                      | Baseline → Candidate | All Else Identical                                   | Purpose                  |
| ------------ | ------------------------- | --------------------- | ---------------------------------------------------- | ------------------------ |
| **E1** | MA threshold              | 1.80 → 1.65          | ✅ MAX_POSITIONS=2, reentry=off, H4=2.0, MACD=0.0005 | Core signal sensitivity  |
| **E2** | H4 weight                 | 2.0 → 1.8            | ✅ MAX_POSITIONS=2, threshold=1.80, reentry=off      | Higher-TF emphasis       |
| **E3** | MACD sensitivity          | 0.0005 → 0.0002      | ✅ MAX_POSITIONS=2, threshold=E1 result              | Early signal capture     |
| **E4** | Reentry rules             | Disabled → Enabled   | ✅ MAX_POSITIONS=2, threshold=E1 result              | Pyramiding behavior      |
| **E5** | Per-currency exposure cap | Baseline → New cap   | ✅ MAX_POSITIONS=2, threshold=E1 result              | Correlation risk control |
| **E6** | Capacity (after E5)       | 2 → 3                | ✅ cap active; threshold=E1 result                   | Scaling limit            |
| **E7** | Cadence                   | 15min → H4 aligned   | ✅ All validated signal params                       | Frequency impact         |

> **MAX_POSITIONS=2 throughout E1–E4** — no position-count contamination of signal-quality results

---

## 📊 Expanded Metrics — Including "Trend Capture"

Beyond win rate → answer: *Did we exit too early?*

| Category              | Metric                            | Definition                                             |
| --------------------- | --------------------------------- | ------------------------------------------------------ |
| **Decision**    | `decision_match_pct`            | Same BUY/SELL/HOLD across configs                      |
|                       | `attribution`                   | Exact parameter causing difference                     |
| **Quality**     | `R_multiple_realized`           | PnL / SL distance                                      |
|                       | `post_exit_favorable_pips`      | After exit — how far price went in original direction |
|                       | `trend_captured_pct`            | Exit price / eventual peak price                       |
|                       | `MAE_pips`                      | Max adverse excursion while in position                |
|                       | `MFE_pips`                      | Max favorable excursion while in position              |
| **Reliability** | `exit_tightness_chain_complete` | Entry → cluster → risk mgr → broker SL confirm      |
|                       | `slippage_bps`                  | Expected vs actual fill                                |
| **Sample**      | `eligible_cycles`               | Market open + valid signals present                    |
|                       | `min_calendar_days`             | ≥ 7 days elapsed                                      |
|                       | `min_decisions`                 | ≥ 30 eligible evaluations                             |

### Acceptance Criteria — Written Before Results

```
Tier 1 — Decision Validation PASS iff:
  [ ] ≥ 7 calendar days completed
  [ ] ≥ 30 eligible decision cycles logged
  [ ] 0 assertion failures / account mismatches
  [ ] 0 overlapping-run conflicts
  [ ] Snapshot integrity hash verified every cycle
  [ ] Attribution resolves ≥ 95% of differences
  [ ] exit_tightness chain = 100% complete
  [ ] Post-exit opportunity loss documented (not penalized)

Tier 2 — Execution PASS iff:
  [ ] SL/TP broker confirmation rate = 100%
  [ ] Avg slippage < 5 bps
  [ ] No orphaned positions / state drift
  [ ] Dual-account reconciliation clean
```

---

## 🔍 Priority #1: exit_tightness Audit — Immediate Action

Before any validation runs, run this diagnostic and capture output:

```bash
python -c "
import sys
from scheduled_runner_v3 import audit_exit_tightness

audit_exit_tightness(verbose=True)
print('EXIT_TIGHTNESS_AUDIT_COMPLETE')
"
```

**Required Evidence in Log**:

```
[EXIT-AUDIT] Trade: #123 Cluster: C-456
  Input exit_tightness: 0.85
  Function signature: new_cluster_from_fill(..., exit_tightness=✓ received)
  Risk manager bound: DynamicTrailingGuard(threshold=0.85 ✓)
  SL calculated: 1.2345 → 1.2350 (adjusted ✓)
  Submitted to broker: ✓ order #789
  Broker confirmed: 1.2350 ✓
  CHAIN_STATUS: COMPLETE ✅
```

- ❌ **Missing any step → pause all tuning** → fix the chain first
- Without this, SL/TP behavior is unknown → all results unreliable

---

## 📁 Final File Structure

```
gemini_ai/
├── .gitignore               # logs/ run.env.* secrets/
├── config/
│   ├── baseline.env         # 1.80 / H4=2.0 / MACD=0.0005 / MAX_POS=2 / reentry=off
│   ├── E1_threshold.env     # 1.65 / everything else = baseline
│   ├── E2_h4weight.env
│   ├── E3_macd.env
│   ├── E4_reentry.env
│   ├── E5_capacity.env
│   └── acceptance_criteria.md  # FROZEN — uneditable post-commit
├── snapshots/
│   ├── latest.json          # timestamped + hashed
│   └── archive/YYYYMMDD/
├── scripts/
│   ├── fetch_snapshot.py    # single market capture
│   ├── compare_decisions.py # same-input diff + attribution
│   ├── run_tier1.sh         # decision-log only → no trades
│   ├── run_tier2.sh         # dual-account demo
│   ├── preflight_check.sh   # 11 gates → block proceed if fail
│   ├── kill_switch.sh       # broker-aware halt
│   └── lib/assertions.sh    # SHA/hash/account/flag enforcement
├── docs/
│   ├── validation_roadmap_v2.1.md
│   └── complete_chat_2026-09-30.md
└── logs/
    ├── decisions/
    ├── snapshots/
    └── audit/
```

---

## 🚦 Immediate Next Steps

1. **Run exit_tightness audit** → capture chain status
2. **Commit exact runner file** → confirm `scheduled_runner_v3.py` is the single source
3. **Execute preflight_check.sh** → all 11 gates green
4. **Start Tier 1 — Decision-Log Only** → `./scripts/run_tier1.sh E1`
   - No orders → pure comparison
   - 7 days / 30 decisions minimum
   - Review `docs/validation_audit.md` daily
5. **E1 PASS → E2 → E3…** → sequentially
6. **Signal params locked → E5 exposure architecture → E6 capacity → Tier 2 demo**
7. **Tier 2 clean → LIVE gated by `--confirm-live` + manual sign-off**

---

This framework now fully integrates **both** reviews:

- ✅ Claude: blockers, dual-account separation, SHA pinning, H4 timing, flock, explicit flags
- ✅ Your refinement: snapshot-first, decision vs execution split, MAX_POSITIONS deferred, trend-capture metrics, sample-size dual condition, exit_tightness prioritized

Which script should I draft first: **`fetch_snapshot.py`** (single source of truth), **`compare_decisions.py`** (attribution engine), or **`preflight_check.sh`** (11-gate gatekeeper)?
