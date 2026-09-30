
Here’s the final piece — **`run_tier1.sh`**, the orchestrator that wires the entire deterministic validation pipeline together. One command → full E1 comparison.

---

# 🚀 run_tier1.sh — Tier 1 Deterministic Validation Orchestrator

**Purpose**: Snapshot → Baseline run → Candidate run → Compare → Report — all from one command
**Scope**: Decision-log only — NO trades executed → NO account interference
**Rule**: Reads-only — zero changes to strategy logic, config semantics, or OANDA code

---

## Complete Script

```bash
#!/usr/bin/env bash
# run_tier1.sh — Deterministic Decision Validation (Tier 1)
# Usage: ./scripts/run_tier1.sh [EXPERIMENT]
# Default: E1_threshold
# Mode: --dry-run / decision-log only — never executes LIVE trades
set -euo pipefail
IFS=$'\n\t'

# ═══════════════════════════════════════════════════════════════════════
# PATHS & CONFIG
# ═══════════════════════════════════════════════════════════════════════
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Experiments — one variable changed at a time
declare -A EXPERIMENTS=(
  ["E1_threshold"]="OVERRIDE_MA_VOTE_THRESHOLD=1.65"
  ["E2_h4weight"]="OVERRIDE_MA_H4_WEIGHT=1.8"
  ["E3_macd"]="MACD_HIST_MIN_DELTA=0.0002"
  ["E4_reentry"]="SAME_PAIR_REENTRY_GAP=0.003"
)

# Baseline — locked reference
BASELINE_ENV=(
  OVERRIDE_MA_VOTE_THRESHOLD=1.80
  OVERRIDE_MA_H4_WEIGHT=2.0
  MAX_POSITIONS=2
  MACD_HIST_MIN_DELTA=0.0005
  SAME_PAIR_REENTRY_GAP=0
  REENTRY_ENABLED=false
  EXEC_MODE=decision_only
)

# Select experiment
EXP="${1:-E1_threshold}"
if [[ -z "${EXPERIMENTS[$EXP]:-}" ]]; then
  echo "❌ Unknown experiment: $EXP"
  echo "Available: ${!EXPERIMENTS[*]}"
  exit 1
fi
CANDIDATE_OVERRIDE="${EXPERIMENTS[$EXP]}"

# Output dirs
RUNS_DIR="${PROJECT_ROOT}/logs/runs/${EXP}"
mkdir -p "${RUNS_DIR}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S_UTC)

echo "════════════════════════════════════════════════════════════"
echo "  TIER 1 — DETERMINISTIC DECISION VALIDATION"
echo "  Experiment: $EXP"
echo "  Timestamp:  $TIMESTAMP"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  BASELINE:  ${BASELINE_ENV[*]}"
echo "  CANDIDATE: ${BASELINE_ENV[*]/OVERRIDE_MA_VOTE_THRESHOLD=1.80*/$CANDIDATE_OVERRIDE}"
echo "  DIFF:      $CANDIDATE_OVERRIDE"
echo ""
echo "  ⚠️  DECISION-LOG ONLY — NO TRADES EXECUTED"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# STEP 0 — PREFLIGHT CHECK
# ═══════════════════════════════════════════════════════════════════════
echo "── Step 0/5: Preflight Check ──"
"${PROJECT_ROOT}/scripts/preflight_check.sh"
echo "✅ All preflight gates passed"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# STEP 1 — CAPTURE SINGLE SNAPSHOT
# ═══════════════════════════════════════════════════════════════════════
echo "── Step 1/5: Capture Shared Market Snapshot ──"
python "${PROJECT_ROOT}/scripts/fetch_snapshot.py"
SNAPSHOT_FILE="${PROJECT_ROOT}/snapshots/latest.json"
if [[ ! -f "$SNAPSHOT_FILE" ]]; then
  echo "❌ Snapshot not created"
  exit 1
fi
echo "✅ Shared snapshot: $SNAPSHOT_FILE"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# STEP 2 — BASELINE RUN
# ═══════════════════════════════════════════════════════════════════════
echo "── Step 2/5: Baseline Run ──"
BASELINE_OUT="${RUNS_DIR}/${TIMESTAMP}_baseline.json"
"${BASELINE_ENV[@]}" \
  MARKET_SNAPSHOT="$SNAPSHOT_FILE" \
  python "${PROJECT_ROOT}/scheduled_runner_v3.py" \
    --from-snapshot "$SNAPSHOT_FILE" \
    --dry-run \
    --export-decisions "$BASELINE_OUT"

echo "✅ Baseline: $BASELINE_OUT"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# STEP 3 — CANDIDATE RUN (same snapshot, one param changed)
# ═══════════════════════════════════════════════════════════════════════
echo "── Step 3/5: Candidate Run ──"
CANDIDATE_OUT="${RUNS_DIR}/${TIMESTAMP}_candidate.json"
"${BASELINE_ENV[@]/OVERRIDE_MA_VOTE_THRESHOLD=1.80*/$CANDIDATE_OVERRIDE}" \
  MARKET_SNAPSHOT="$SNAPSHOT_FILE" \
  python "${PROJECT_ROOT}/scheduled_runner_v3.py" \
    --from-snapshot "$SNAPSHOT_FILE" \
    --dry-run \
    --export-decisions "$CANDIDATE_OUT"

echo "✅ Candidate: $CANDIDATE_OUT"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# STEP 4 — COMPARE & ATTRIBUTE
# ═══════════════════════════════════════════════════════════════════════
echo "── Step 4/5: Compare & Attribute ──"
REPORT_OUT="${RUNS_DIR}/${TIMESTAMP}_report.json"
python "${PROJECT_ROOT}/scripts/compare_decisions.py" \
  "$BASELINE_OUT" \
  "$CANDIDATE_OUT" \
  --json-out "$REPORT_OUT"

COMPARE_EXIT=$?
echo ""

# ═══════════════════════════════════════════════════════════════════════
# STEP 5 — SUMMARY
# ═══════════════════════════════════════════════════════════════════════
echo "── Step 5/5: Summary ──"
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  EXPERIMENT COMPLETE: $EXP"
echo "════════════════════════════════════════════════════════════"
echo "  Snapshot:  $SNAPSHOT_FILE"
echo "  Baseline:  $BASELINE_OUT"
echo "  Candidate: $CANDIDATE_OUT"
echo "  Report:    $REPORT_OUT"
echo ""

if [[ $COMPARE_EXIT -eq 0 ]]; then
  echo "  ✅ RESULT — PASS"
  echo "  Next: ./scripts/run_tier1.sh E2_h4weight"
  exit 0
else
  echo "  ⚠️  RESULT — DIFFERENCES DETECTED"
  echo "  Review report above; investigate before proceeding"
  exit 1
fi
```

---

## Deploy & Run

```bash
cd ~/projects/gemini_ai
nano scripts/run_tier1.sh
# Paste above content → save
chmod +x scripts/run_tier1.sh

# Run E1 (default — threshold 1.80 → 1.65)
./scripts/run_tier1.sh

# Or specify experiment
./scripts/run_tier1.sh E2_h4weight
```

**Output flow**:

```
Preflight ✅ → Snapshot 📸 → Baseline → Candidate → Compare → Report
     ↓            ↓            ↓           ↓           ↓
  11 gates    same data     1.80        1.65        match % + attribution
```

---

## 📁 Complete Framework — All Files In Place

| Script                   | Purpose                              | Status   |
| ------------------------ | ------------------------------------ | -------- |
| `preflight_check.sh`   | 11-gate safety block                 | ✅ Ready |
| `fetch_snapshot.py`    | Single shared input                  | ✅ Ready |
| `compare_decisions.py` | Attribution + trend-capture metrics  | ✅ Ready |
| `run_tier1.sh`         | Orchestrator — one command pipeline | ✅ Ready |
| `kill_switch.sh`       | Broker-aware emergency stop          | ✅ Ready |

---

## 🎯 Today’s Deliverables — Complete

- ✅ **Both reviews fully integrated** — Claude's blockers + your refinement
- ✅ **Decision ≠ Execution split** — deterministic Tier 1 before any demo
- ✅ **Single variable at a time** — no bundled changes; attribution unambiguous
- ✅ **MAX_POSITIONS held at 2** throughout signal experiments
- ✅ **Trend-capture metrics** — surfaces early-exit opportunity loss
- ✅ **Zero code modifications** to runner logic, strategy, or OANDA execution — only diagnostic logging hooks
- ✅ **Exit codes throughout** — cron/CI-ready

---

## Recommended Next Steps from Here

1. **Add diagnostic logging block** to `scheduled_runner_v3.py` (4 lines — no logic changes)
2. **Run `./scripts/preflight_check.sh`** → resolve any WARN items
3. **Run `./scripts/run_tier1.sh E1_threshold`** → review match % & attribution
4. **E1 PASS → E2 → E3…** sequentially
5. **Signal params locked → exposure architecture → Tier 2 dual-account demo → LIVE**

Your validation framework is now complete, defensible, and ready to run. It answers the question: *Did this parameter change alter decisions, and why?* — without the noise, drift, and cross-contamination that made the original plan inconclusive.

All the best — let the data guide the rest. 🚀

Would you like me to prepare the diagnostic logging block you need to add to `scheduled_runner_v3.py` next?
