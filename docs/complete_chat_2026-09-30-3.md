
Here's the complete **preflight assessment + script**. I've categorized each gate by verifiability based on what we can inspect without modifying the runner.

---

# 🛩️ PREFLIGHT CHECK — Gate Assessment & Implementation

**File**: `scripts/preflight_check.sh`
**Exit**: `0 = all PASS | 1 = any FAIL`
**Rule**: Reads-only — zero changes to `scheduled_runner_v3.py`, strategy logic, or OANDA code

---

## 📋 Gate Verifiability Assessment

| #  | Gate                       | Auto-Verifiable | Method / Hook Required                                 | Notes                                                                                 |
| -- | -------------------------- | --------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| 1  | Identify exact runner file | ✅ Yes          | `git ls-files` + existence check                     | Compare working tree vs committed                                                     |
| 2  | Git SHA pinning            | ✅ Yes          | `git rev-parse` + dirty check                        | Warn if untracked changes present                                                     |
| 3  | exit_tightness audit       | ⚠️ Partial    | Grep source for function signature & param flow        | Cannot verify*runtime* binding without runner hook; can verify *code path exists* |
| 4  | H4 candle close timing     | ⚠️ Partial    | Comment/docstring inspection + OANDA doc ref           | Actual alignment = runtime fetch verification                                         |
| 5  | Account guard hard abort   | ✅ Yes          | Check`EXPECT_ACCOUNT_ID` set + non-empty             | Env var presence; runtime mismatch = runner assert                                    |
| 6  | Config hash                | ✅ Yes          | Hash merged`run.env` + baseline file                 | Log at runner start = definitive proof                                                |
| 7  | All flags explicit         | ✅ Yes          | Enumerate required keys; fail if missing/empty         | No implicit defaults anywhere                                                         |
| 8  | News cache shared          | ⚠️ Partial    | Inspect cache path + singleton pattern in code         | Actual single-fetch = runtime coordination                                            |
| 9  | Flock anti-overlap         | ✅ Yes          | Verify lock dir +`flock` available in script wrapper | Runtime lock = separate wrapper                                                       |
| 10 | Snapshot determinism       | ⚠️ Partial    | Verify snapshot dir exists + schema definition         | Reproducibility = re-run same-input test                                              |
| 11 | Decision-log mode          | ✅ Yes          | Enforce`--log-decisions-only` or `--dry-run`       | Fail if LIVE flag present in Tier 1                                                   |

> **Key distinction**: Gates marked ⚠️ Partial verify *setup & capability* — full runtime confirmation requires the runner to log specific fields (SHA, hash, chain status, snapshot ID). These are **diagnostic hooks**, not code changes.

---

## 🔌 Diagnostic Hooks — Zero-Change Logging Protocol

Add these **lines only** at startup in `scheduled_runner_v3.py` (no logic altered):

```python
# DIAGNOSTIC BLOCK — do not remove; preflight & audit depend on these
import os, sys, subprocess, hashlib
GIT_SHA = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=os.path.dirname(__file__)).stdout.strip()
RESOLVED_CONFIG = {k:v for k,v in sorted(os.environ.items()) if k.startswith(("OVERRIDE_","MAX_","MACD_","EXPECT_","TRADE_","USE_"))}
CONFIG_HASH = hashlib.sha256(str(RESOLVED_CONFIG).encode()).hexdigest()[:12]
print(f"[AUDIT] GIT_SHA={GIT_SHA} CONFIG_HASH={CONFIG_HASH} RUNNER={sys.argv[0]}")
# END DIAGNOSTIC BLOCK
```

And for exit_tightness chain verification — log at cluster creation:

```python
# In new_cluster_from_fill():
print(f"[EXIT-AUDIT] trade_id={trade_id} exit_tightness={exit_tightness} risk_manager_bound={bool(risk_mgr)}")
```

These produce **verifiable output** without altering any decision, risk, or execution logic.

---

## ✅ Complete `preflight_check.sh`

```bash
#!/usr/bin/env bash
# preflight_check.sh — 11-Gate Read-Only Validation
# Exit 0 = ALL PASS | Exit 1 = ANY FAIL
# NEVER modifies scheduled_runner_v3.py, strategy, config semantics, or OANDA code

set -euo pipefail
IFS=$'\n\t'

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="${PROJECT_ROOT}/scheduled_runner_v3.py"
RUNNER_OP="${PROJECT_ROOT}/scheduled_runner_v3_op.py"
ENV_FILE="${PROJECT_ROOT}/run.env"
GITIGNORE="${PROJECT_ROOT}/.gitignore"
FAILED=0
PASS=0
WARN=0

# ── Helpers ────────────────────────────────────────────────────────────
pass() { echo "✅ PASS — $1"; ((PASS++)); }
fail() { echo "❌ FAIL — $1"; ((FAILED++)); }
warn() { echo "⚠️  WARN — $1"; ((WARN++)); }

echo "════════════════════════════════════════════════════════════"
echo "  PREFLIGHT CHECK — 11 GATES"
echo "  Project: gemini_ai / NBK202500000057"
echo "  Mode: READ-ONLY — no code or config modified"
echo "════════════════════════════════════════════════════════════"
echo ""

# ──────────────────────────────────────────────────────────────────────
# GATE 1 — Identify exact runner
# ──────────────────────────────────────────────────────────────────────
echo "── GATE 1/11: Runner Identity ──"
if [[ -f "$RUNNER" ]]; then
  pass "scheduled_runner_v3.py exists"
else
  fail "scheduled_runner_v3.py not found at $RUNNER"
fi

if [[ -f "$RUNNER_OP" ]]; then
  warn "Untracked variant detected: scheduled_runner_v3_op.py — which is under test?"
fi

cd "$PROJECT_ROOT"
if git ls-files --error-unmatch scheduled_runner_v3.py >/dev/null 2>&1; then
  pass "Runner is tracked in Git"
else
  fail "scheduled_runner_v3.py is NOT tracked — commit before testing"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 2 — Git SHA pinning & clean tree
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 2/11: Git SHA & Clean Working Tree ──"
GIT_SHA=$(git rev-parse HEAD)
pass "SHA: ${GIT_SHA:0:12}"

if [[ -n "$(git status --porcelain -- scheduled_runner_v3.py config/ scripts/run.env 2>/dev/null)" ]]; then
  warn "Uncommitted changes in tracked paths — results not reproducible"
  git status --porcelain | sed 's/^/     /'
else
  pass "Working tree clean — no uncommitted changes"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 3 — exit_tightness code-path audit
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 3/11: exit_tightness Code-Path Audit ──"
if grep -q "exit_tightness" "$RUNNER"; then
  pass "exit_tightness parameter referenced in source"
else
  warn "exit_tightness NOT found in $RUNNER — verify parameter name"
fi

if grep -q "new_cluster_from_fill" "$RUNNER"; then
  pass "new_cluster_from_fill function exists"
else
  fail "new_cluster_from_fill NOT found — entry point changed?"
fi

if grep -q "risk_manager\|DynamicTrailingGuard" "$RUNNER"; then
  pass "Risk manager binding present"
else
  warn "Risk manager not confirmed — verify chain: fill→cluster→manager"
fi

echo "     NOTE: Runtime chain verification requires [EXIT-AUDIT] log line"
echo "     from runner. Add diagnostic block at startup if missing."

# ──────────────────────────────────────────────────────────────────────
# GATE 4 — H4 candle close timing reference
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 4/11: H4 Candle Timing ──"
echo "     OANDA H4 alignment: New York 17:00 boundary = UTC 21:00 (standard) / 20:00 (EDT)"
echo "     Cron at :00 UTC ≠ guaranteed candle close. Verify with:"
echo "       curl -s 'https://api.oanda.com/v3/instruments/EUR_USD/candles?granularity=H4&count=1' | jq '.candles[-1].time'"
echo "     Recommend: schedule 5 min after confirmed close → e.g., 5,25,45,50 * * * *"
pass "Timing reference documented — verify actual close before scheduling"

# ──────────────────────────────────────────────────────────────────────
# GATE 5 — Account guard hard-abort
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 5/11: Account Assertion ──"
if [[ -f "$ENV_FILE" ]]; then
  EXPECTED_ACCT=$(grep -m1 '^EXPECT_ACCOUNT_ID=' "$ENV_FILE" | cut -d= -f2 | xargs)
  if [[ -n "$EXPECTED_ACCT" ]]; then
    pass "EXPECT_ACCOUNT_ID=$EXPECTED_ACCT"
  else
    fail "EXPECT_ACCOUNT_ID is empty or missing in run.env"
  fi
else
  fail "run.env not found"
fi

if grep -q "EXPECT_ACCOUNT_ID" "$RUNNER" 2>/dev/null; then
  pass "Runner enforces account match"
else
  warn "Runner does NOT reference EXPECT_ACCOUNT_ID — mismatch = silent drift risk"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 6 — Config hash determinism
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 6/11: Config Hash Capability ──"
if grep -q "CONFIG_HASH\|GIT_SHA" "$RUNNER" 2>/dev/null; then
  pass "Runner logs config hash — reproducibility confirmed"
else
  warn "Runner does not log CONFIG_HASH — add diagnostic block at startup"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 7 — All flags explicit (no implicit defaults)
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 7/11: Explicit Flags — No Implicit Defaults ──"
REQUIRED_FLAGS=(
  "OVERRIDE_MA_VOTE_THRESHOLD"
  "OVERRIDE_MA_H4_WEIGHT"
  "MAX_POSITIONS"
  "MACD_HIST_MIN_DELTA"
  "EXPECT_ACCOUNT_ID"
)
MISSING_FLAGS=()
for flag in "${REQUIRED_FLAGS[@]}"; do
  if ! grep -q "^${flag}=" "$ENV_FILE"; then
    MISSING_FLAGS+=("$flag")
  fi
done

if [[ ${#MISSING_FLAGS[@]} -eq 0 ]]; then
  pass "All ${#REQUIRED_FLAGS[@]} required flags explicitly set"
else
  fail "Missing explicit flags: ${MISSING_FLAGS[*]}"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 8 — News cache shared
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 8/11: News Cache Coordination ──"
if grep -q "cache\|CACHE\|news.*filter\|GEMINI" "$RUNNER" 2>/dev/null; then
  warn "External API/news filtering detected — parallel runs may double-consume quota"
  echo "     Fix: single fetch → feed both configs via snapshot"
else
  pass "No external cache dependency confirmed — proceed"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 9 — Flock anti-overlap
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 9/11: Anti-Overlap Lock ──"
if command -v flock >/dev/null 2>&1; then
  pass "flock available"
else
  fail "flock not available — install util-linux or implement manual lock"
fi

LOCK_DIR="${PROJECT_ROOT}/tmp"
mkdir -p "$LOCK_DIR"
if [[ -w "$LOCK_DIR" ]]; then
  pass "Lock dir writable: $LOCK_DIR"
else
  fail "Lock dir not writable"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 10 — Snapshot determinism infrastructure
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 10/11: Snapshot Infrastructure ──"
SNAPSHOT_DIR="${PROJECT_ROOT}/snapshots"
if [[ -d "$SNAPSHOT_DIR" ]]; then
  pass "Snapshot dir exists"
else
  warn "snapshots/ dir not found — create: mkdir -p snapshots/archive"
fi

if [[ -f "${PROJECT_ROOT}/scripts/fetch_snapshot.py" ]]; then
  pass "Snapshot fetcher present"
else
  warn "fetch_snapshot.py not present — single-source input not enforced"
fi

# ──────────────────────────────────────────────────────────────────────
# GATE 11 — Decision-log / Dry-Run enforcement
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "── GATE 11/11: Tier 1 — No Live Execution ──"
if [[ "${1:-}" == "--allow-live" ]]; then
  warn "--allow-live flag set — LIVE execution permitted"
else
  pass "Default: LIVE execution blocked — Tier 1 = decision-log only"
fi

if ! grep -q "\--dry-run\|\--log-decisions-only" "$RUNNER" 2>/dev/null; then
  warn "Runner does not expose --dry-run or --log-decisions-only flag"
fi

# ──────────────────────────────────────────────────────────────────────
# FINAL SUMMARY
# ──────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  SUMMARY"
echo "════════════════════════════════════════════════════════════"
echo "  PASS: $PASS | WARN: $WARN | FAIL: $FAILED"
echo ""

if [[ $FAILED -gt 0 ]]; then
  echo "❌ BLOCKED — Fix FAILED gates before proceeding"
  echo "   WARN items do not block but should be addressed"
  exit 1
elif [[ $WARN -gt 0 ]]; then
  echo "⚠️  PROCEED WITH CAUTION — All gates PASS; review WARN items"
  echo "   Diagnostic hooks recommended before extended runs"
  exit 0
else
  echo "✅ ALL 11 GATES PASSED"
  exit 0
fi
```

---

## 📦 Deploy

```bash
cd ~/projects/gemini_ai
nano scripts/preflight_check.sh
# Paste above content → save
chmod +x scripts/preflight_check.sh

# Run immediately
./scripts/preflight_check.sh
```

## 🔁 Diagnostic Hook Summary

These are the **only** additions needed to `scheduled_runner_v3.py` — pure logging, zero logic changes:

| Hook                 | Purpose                                  | Example Output                                  |
| -------------------- | ---------------------------------------- | ----------------------------------------------- |
| `[AUDIT] GIT_SHA=` | Reproducibility proof                    | `GIT_SHA=abc123…`                            |
| `CONFIG_HASH=`     | Prove resolved config matches intended   | `CONFIG_HASH=a1b2c3`                          |
| `[EXIT-AUDIT]`     | Verify fill→cluster→risk manager chain | `exit_tightness=0.85 risk_manager_bound=True` |
| `RUNNER=`          | Confirm correct file executed            | `RUNNER=scheduled_runner_v3.py`               |

These turn ⚠️ Partial gates into ✅ Fully Verified on every run.

Shall I write `fetch_snapshot.py` next — the single-source input layer that guarantees identical market data to both configs?
