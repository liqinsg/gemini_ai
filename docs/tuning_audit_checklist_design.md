下面给出完整可直接部署的 **双配置并行对照脚本 + 差异分析工具**，用于基线/新参数并排验证，全程 Dry-Run 安全无风险。

---

# 🔬 Parallel Diff Validation Suite — Scheduled Runner v3

**Purpose**: Run baseline vs tuned config side-by-side; compare signals, votes, and decisions without LIVE risk
**Version**: 1.0 | Date: 2026-09-30

---

## 1. File Layout

```
gemini_ai/
├── config/
│   ├── run.env.baseline      # Original thresholds
│   └── run.env.tuned         # New v1.65 thresholds
├── scripts/
│   ├── parallel_runner.sh    # Launch both in parallel
│   └── diff_analysis.py      # Parse & compare logs
├── docs/
│   └── tuning_audit_checklist_v1.md
└── logs/
    ├── baseline/
    └── tuned/
```

---

## 2. Environment Configs

### `config/run.env.baseline`

```bash
# === BASELINE — ORIGINAL THRESHOLDS ===
EXPECT_ACCOUNT_ID=001-003-21515688-002
CROSS_MAX_NET_PER_CCY=2
DEMO_LOT_SIZE=5000
LIVE_LOT_SIZE=1
OVERRIDE_MAX_OPEN=2
TRADE_JPY=true
TRADE_CHF=true
USE_MACD=true

# Override — ORIGINAL
OVERRIDE_MA_VOTE_THRESHOLD=1.8
OVERRIDE_MA_H4_WEIGHT=2.0

# Positioning
MAX_POSITIONS=2
SAME_PAIR_REENTRY_GAP=0
REENTRY_VOTE_THRESHOLD=1.8

# MACD
MACD_HIST_MIN_DELTA=0.0005
MACD_REQUIRE_TF=["H4","H1","M30"]
MACD_FAST=12
MACD_SLOW=26
MACD_SIGNAL=9

# Strategy
MIN_STRENGTH_SCORE=0.15
MIN_RR=1.2
DOMINANCE_RATIO=1.3
OVERRIDE_RATIO=1.8
MIN_HOLD_MIN=180
MIN_HOLD_OVERRIDE_MIN=10080
TRADE_TOP_PAIRS=3
```

### `config/run.env.tuned`

```bash
# === TUNED — v1.65 THRESHOLD + reentry rules ===
EXPECT_ACCOUNT_ID=001-003-21515688-002
CROSS_MAX_NET_PER_CCY=4
DEMO_LOT_SIZE=5000
LIVE_LOT_SIZE=1
OVERRIDE_MAX_OPEN=2
TRADE_JPY=true
TRADE_CHF=true
USE_MACD=true

# Override — LOWERED consensus
OVERRIDE_MA_VOTE_THRESHOLD=1.65
OVERRIDE_MA_H4_WEIGHT=1.8

# Positioning — allow 3 + gap-based reentry
MAX_POSITIONS=3
SAME_PAIR_REENTRY_GAP=0.003
REENTRY_VOTE_THRESHOLD=1.75

# MACD — tighter noise filter
MACD_HIST_MIN_DELTA=0.0002
MACD_REQUIRE_TF=["H4","H1"]
MACD_FAST=12
MACD_SLOW=26
MACD_SIGNAL=9

# Exposure — extreme gap
EXTREME_GAP_THRESHOLD=4.0
EXTREME_GAP_BOOST=1
ABSOLUTE_MAX_NET_PER_CCY=5

# Strategy — unchanged
MIN_STRENGTH_SCORE=0.15
MIN_RR=1.2
DOMINANCE_RATIO=1.3
OVERRIDE_RATIO=1.8
MIN_HOLD_MIN=180
MIN_HOLD_OVERRIDE_MIN=10080
TRADE_TOP_PAIRS=3
```

---

## 3. Parallel Runner Script

### `scripts/parallel_runner.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Config ──────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PY_BIN="/home/qili/miniconda3/envs/ai-sprint/bin/python"
RUNNER="${PROJECT_ROOT}/scheduled_runner_v3.py"
CONFIG_DIR="${PROJECT_ROOT}/config"
LOG_DIR="${PROJECT_ROOT}/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# ── Setup ───────────────────────────────
mkdir -p "${LOG_DIR}/baseline" "${LOG_DIR}/tuned"

run_instance() {
    local name="$1"
    local env_file="$2"
    local log_path="${LOG_DIR}/${name}/run_${TIMESTAMP}.log"

    echo "▶ Starting ${name}..."
    (
        set -a
        # shellcheck source=/dev/null
        source "${env_file}"
        set +a
        exec "${PY_BIN}" "${RUNNER}" --live --dry-run > "${log_path}" 2>&1
    ) &
    echo "${name} PID: $!"
}

# ── Launch ──────────────────────────────
echo "══════════════════════════════════════"
echo "  PARALLEL VALIDATION — ${TIMESTAMP}"
echo "  Baseline: 1.8 threshold / MAX_POS=2"
echo "  Tuned:    1.65 threshold / MAX_POS=3"
echo "══════════════════════════════════════"

run_instance "baseline" "${CONFIG_DIR}/run.env.baseline"
run_instance "tuned"    "${CONFIG_DIR}/run.env.tuned"

wait
echo "✅ Both runs completed"
echo "Logs: ${LOG_DIR}/baseline/ & ${LOG_DIR}/tuned/"
echo ""
echo "Run diff analysis:"
echo "  python ${SCRIPT_DIR}/diff_analysis.py"
```

---

## 4. Diff Analysis Tool

### `scripts/diff_analysis.py`

```python
#!/usr/bin/env python3
import re, json, sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"

PATTERNS = {
    "timestamp": re.compile(r"Time\s+:\s+([^\n]+)"),
    "gap": re.compile(r"GLOBAL_MAX_GAP:\s+([\d.]+)"),
    "jpy_signal": re.compile(r"\[SELECTION-JPY\].*?NO TRADE|Selected.*?JPY"),
    "usd_signal": re.compile(r"\[SELECTION-USD\].*?NO TRADE|Selected.*?USD"),
    "chf_signal": re.compile(r"\[SELECTION-CHF\].*?NO TRADE|Selected.*?USD_CHF"),
    "jpy_vote": re.compile(r"AUD_JPY.*?No consensus: buy=([\d.]+).*sell=([\d.]+).*need\s+([\d.]+)", re.DOTALL),
    "usd_vote": re.compile(r"AUD_USD.*?No consensus: buy=([\d.]+).*sell=([\d.]+).*need\s+([\d.]+)", re.DOTALL),
    "positions": re.compile(r"open=(\d+)|MAX_POSITIONS=(\d+)"),
    "decision": re.compile(r"\[GLOBAL\](.*?HOLD|Selected.*?BUY|Selected.*?SELL)"),
    "skip_held": re.compile(r"\[SKIP HELD\].*$", re.MULTILINE),
}

def parse_log(name: str, path: Path):
    text = path.read_text()
    res = {"name": name, "path": str(path)}

    m = PATTERNS["gap"].search(text)
    res["gap"] = float(m.group(1)) if m else None

    res["jpy"] = "NO_TRADE" if "NO TRADE" in (PATTERNS["jpy_signal"].search(text) or {"": ""}).group(0) else "SIGNAL"
    res["usd"] = "NO_TRADE" if "NO TRADE" in (PATTERNS["usd_signal"].search(text) or {"": ""}).group(0) else "SIGNAL"
    res["chf"] = "NO_TRADE" if "NO TRADE" in (PATTERNS["chf_signal"].search(text) or {"": ""}).group(0) else "SIGNAL"

    for label, key in [("AUD_JPY", "jpy_vote"), ("AUD_USD", "usd_vote")]:
        m = PATTERNS[key].search(text)
        if m:
            res[f"{key}_buy"] = float(m.group(1))
            res[f"{key}_sell"] = float(m.group(2))
            res[f"{key}_thr"] = float(m.group(3))

    m = PATTERNS["decision"].search(text)
    res["decision"] = m.group(1).strip() if m else "UNKNOWN"
    res["skip_held"] = bool(PATTERNS["skip_held"].search(text))

    return res

def latest_log(d: Path) -> Path | None:
    files = sorted(d.glob("run_*.log"), reverse=True)
    return files[0] if files else None

def main():
    bl_path = latest_log(LOGS / "baseline")
    td_path = latest_log(LOGS / "tuned")

    if not bl_path or not td_path:
        print("⚠️  Run both configs first via parallel_runner.sh")
        sys.exit(1)

    bl = parse_log("BASELINE", bl_path)
    td = parse_log("TUNED", td_path)

    print("="*60)
    print(f"  DIFF COMPARISON — {datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    print(f"  Baseline: {bl_path.name}")
    print(f"  Tuned:    {td_path.name}")
    print("="*60)

    rows = [
        ("GAP", bl["gap"], td["gap"], "same"),
        ("JPY Group", bl["jpy"], td["jpy"], "✅" if bl["jpy"]==td["jpy"] else "🔼 CHANGE"),
        ("USD Group", bl["usd"], td["usd"], "✅" if bl["usd"]==td["usd"] else "🔼 CHANGE"),
        ("CHF Group", bl["chf"], td["chf"], "✅" if bl["chf"]==td["chf"] else "🔼 CHANGE"),
        ("Global Decision", bl["decision"], td["decision"], "✅" if bl["decision"]==td["decision"] else "🔼 CHANGE"),
        ("SKIP_HELD", bl["skip_held"], td["skip_held"], "same"),
    ]

    for k, vb, vt, flag in rows:
        print(f"\n{k:<20} | BASELINE={vb!s:<20} TUNED={vt!s:<20} {flag}")

    print("\n── Vote Threshold Detail ──")
    for pair in ["jpy_vote", "usd_vote"]:
        if f"{pair}_buy" not in bl: continue
        b_buy, b_thr = bl[f"{pair}_buy"], bl[f"{pair}_thr"]
        t_buy, t_thr = td.get(f"{pair}_buy", b_buy), td.get(f"{pair}_thr", b_thr)
        status = "🔼 PASSES" if b_buy >= t_thr else "🚫 still blocked"
        print(f"  {pair.upper().replace('_VOTE','')}: score={b_buy:.2f} | baseline={b_thr:.2f} tuned={t_thr:.2f} → {status}")

if __name__ == "__main__":
    main()
```

---

## 5. Deploy & Run

```bash
# Create directories
mkdir -p ~/projects/gemini_ai/{config,scripts,logs/baseline,logs/tuned}

# Write files — paste above content into each
nano ~/projects/gemini_ai/config/run.env.baseline
nano ~/projects/gemini_ai/config/run.env.tuned
nano ~/projects/gemini_ai/scripts/parallel_runner.sh
nano ~/projects/gemini_ai/scripts/diff_analysis.py

# Make executable
chmod +x ~/projects/gemini_ai/scripts/*.sh ~/projects/gemini_ai/scripts/*.py

# Run parallel validation
cd ~/projects/gemini_ai
./scripts/parallel_runner.sh

# View comparison
./scripts/diff_analysis.py
```

---

## 6. Expected Output Example

```
══════════════════════════════════════
  DIFF COMPARISON — 2026-09-30 08:36 UTC
  Baseline: run_20260930_083600.log
  Tuned:    run_20260930_083601.log
══════════════════════════════════════

GAP                  | BASELINE=4.2009             TUNED=4.2009             same
JPY Group            | BASELINE=NO_TRADE           TUNED=SIGNAL              🔼 CHANGE
USD Group            | BASELINE=NO_TRADE           TUNED=SIGNAL              🔼 CHANGE
CHF Group            | BASELINE=SIGNAL             TUNED=SIGNAL             ✅
Global Decision      | BASELINE=HOLD               TUNED=ENTER               🔼 CHANGE

── Vote Threshold Detail ──
  AUD_JPY: score=1.70 | baseline=1.80 tuned=1.65 → 🔼 PASSES
  AUD_USD: score=1.70 | baseline=1.80 tuned=1.65 → 🔼 PASSES
```

---

## 7. Git Commit

```bash
git add config/ scripts/ docs/tuning_audit_checklist_v1.md
git commit -m "Add parallel validation suite + audit checklist v1"
git branch validation/parallel-diff
git push -u origin validation/parallel-diff
```

---

## 8. Next Steps

- Run `./scripts/parallel_runner.sh` **once per cycle** (align with your scheduled interval)
- Log results to the weekly template in `tuning_audit_checklist_v1.md`
- After **7 consecutive days** with consistent/improved signal quality → proceed to Phase 3 (reentry rules enabled)
- If false positives increase → revert to `run.env.baseline` and try **1.70** as intermediate threshold

Want me to also add a **summary report generator** that aggregates 7+ days of parallel runs into a single performance tabl

下面直接给出完整可保存的 **tuning_audit_checklist_v1.md**，内容整合了配置、对照、复盘模板，可直接写入版本库使用。

---

# Tuning & Audit Checklist — Scheduled Runner v3

**Version**: 1.0 | Date: 2026-09-30
**Profile**: p2 | Account: 001-003-21515688-002 | Environment: LIVE / Dry-Run
**Purpose**: Standardize parameter tuning, log audit, and weekly iteration tracking for the strength-MACD forex bot

---

## 🔐 1. Environment & Safety Baseline

Add to `run.env` — **mandatory before LIVE deployment**

```bash
# === ACCOUNT GUARD — prevent cross-account execution ===
EXPECT_ACCOUNT_ID=001-003-21515688-002

# === LOCK / ISOLATION (verify on each host) ===
# Lock path: /tmp/runner_v3_h_nbk202500000057_acc_001-003-21515688-002_p2.lock
# Isolation: hostname + account + profile — OK if no concurrent-block warnings
```

**Audit Check**:

- [ ] No `WARNING: live run without EXPECT_ACCOUNT_ID` in log
- [ ] `Isolation OK` present; no stale lock errors
- [ ] `Market: OPEN` / correct session hours

---

## ⚙️ 2. Recommended Parameter Set — run.env

### 2.1 OVERRIDE Mode Consensus (Primary Fix)

```bash
# OVERRIDE MODE — MA Cross weighted-vote thresholds
# Lowered from 1.8 → 1.65 to catch near-consensus signals
# H4 weight kept elevated to respect higher-timeframe trend
OVERRIDE_MA_VOTE_THRESHOLD=1.65
OVERRIDE_MA_H4_WEIGHT=1.8

# Fallback: aggressive (after 7–14d dry-run validation)
# OVERRIDE_MA_VOTE_THRESHOLD=1.6
# OVERRIDE_MA_H4_WEIGHT=1.7
```

### 2.2 Position & Reentry Rules

```bash
# Max concurrent open positions
MAX_POSITIONS=3

# Minimum price gap before re-entry on same instrument (0.003 = ~30 pips for 0.01-pip pairs)
SAME_PAIR_REENTRY_GAP=0.003

# Higher consensus bar for pyramiding/re-entries
REENTRY_VOTE_THRESHOLD=1.75

# Net exposure limits
CROSS_MAX_NET_PER_CCY=4
EXTREME_GAP_THRESHOLD=4.0
EXTREME_GAP_BOOST=1
ABSOLUTE_MAX_NET_PER_CCY=5
```

### 2.3 MACD & Noise Filter

```bash
# MACD histogram delta threshold — lower = more sensitive
MACD_HIST_MIN_DELTA=0.0002

# Restrict MACD confirmation to higher timeframes only
MACD_REQUIRE_TF=["H4","H1"]

# Base MACD params (keep unless differentiated)
MACD_FAST=12
MACD_SLOW=26
MACD_SIGNAL=9
```

### 2.4 Strategy Core (Unchanged Unless Backtest-Driven)

```bash
MIN_STRENGTH_SCORE=±0.15
MIN_RR=1.2
DOMINANCE_RATIO=1.3
OVERRIDE_RATIO=1.8
MIN_HOLD_MIN=180
MIN_HOLD_OVERRIDE_MIN=10080
TRADE_TOP_PAIRS=3
```

---

## 📊 3. Log Audit Cheat Sheet

Search these keywords on every cycle run

| Keyword / Log Line                            | Meaning                                                 | Action                                   |
| --------------------------------------------- | ------------------------------------------------------- | ---------------------------------------- |
| `DryRun: True`                              | Simulation — no real orders ✅                         | Confirm before LIVE switch               |
| `GAP=X.X ≥1.8 → STRONG_GAP`               | High-differentiation market; signals carry more weight  | Verify exposure boost applied            |
| `No consensus: X.X / need ≥1.8`            | Near-threshold signal — candidate for threshold tuning | Compare vs`OVERRIDE_MA_VOTE_THRESHOLD` |
| `[SKIP HELD]`                               | Signal valid but already in portfolio                   | Evaluate reentry rules                   |
| `override-open=2/2`                         | Override-mode position cap reached                      | Raise`MAX_POSITIONS` or wait exit      |
| `MACD H4 noise Δ=… < threshold → ignore` | Weak MACD momentum — filter active                     | Adjust`MACD_HIST_MIN_DELTA`            |
| `MC=STRONG_MOMENTUM`                        | Aggressive mode active; MA threshold lowered            | Confirm signal passed lowered bar        |
| `SL_repaired / TP_repaired > 0`             | Price drift detected — recalc SL/TP logic              | Investigate pricing source latency       |
| `Only X valid pair(s) — need ≥Y`          | Insufficient candidates                                 | Check strength distribution              |

---

## 📈 4. Per-Cycle Quick Audit Template

Copy & fill after each run

```
Cycle Date: ________ UTC
DryRun: [ ] True  [ ] False
Market Status: _______
Global GAP: _______  Level: [ ] Normal  [ ] Strong  [ ] Extreme

Currency Ranking:
1st ______ (+____) | 2nd ______ (+____) | Weakest ______ (-____)

Signals Generated:
[JPY] ______ | Status: _________
[USD] ______ | Status: _________
[CHF] ______ | Status: _________

Selected: __________ | Result: [ ] Entered  [ ] Held  [ ] Skipped: _______

Open Positions: __ | Max Pos: __ | Net Cap Used: __/4
SL/TP Guardian: OK=__ | Repaired=__

Notes:
-
```

---

## 📅 5. Weekly Iteration Template

Track performance and parameter changes

| Week Ending | Params Active                 | Signals | Win Rate | Avg R:R | Max Drawdown | Notes / Observation          |
| ----------- | ----------------------------- | ------- | -------- | ------- | ------------ | ---------------------------- |
| 2026-10-03  | Baseline v1.0 (1.8 threshold) | —      | —       | —      | —           | Initial state                |
| 2026-10-10  | v1.1 (1.65 threshold)         |         |          |         |              | First lowered-threshold week |
| 2026-10-17  | v1.2 (if validated)           |         |          |         |              | Evaluate reentry rules       |

### Metrics Definitions

- **Signals**: New entries triggered (excluding skipped/held)
- **Win Rate**: Closed trades net positive / total closed
- **Avg R:R**: Realized risk-reward vs projected at entry
- **Max Drawdown**: Peak-to-trough equity drop %
- **Trigger Quality**: % of signals where H4 direction matched eventual outcome

---

## 🧭 6. Known Behaviors & Roadmap

### Current Confirmed

- ✅ Isolation/locking working across accounts
- ✅ CHF STRONG_MOMENTUM correctly lowers MA threshold to 1.4
- ✅ USD_CHF signal valid — skipped only due to existing position
- ⚠️ JPY/USD groups blocked by vote threshold; not strength issue

### Roadmap

| Phase                    | Timeline              | Focus                                                          |
| ------------------------ | --------------------- | -------------------------------------------------------------- |
| **1 — Safety**    | Days 1–2             | Add`EXPECT_ACCOUNT_ID`; verify all runs match target account |
| **2 — Threshold** | Days 2–7             | Apply 1.65 threshold; compare baseline vs new side-by-side     |
| **3 — Reentry**   | Days 7–14            | Enable`MAX_POSITIONS=3` + gap-based pyramiding               |
| **4 — Refine**    | Week 3+               | Differentiate MACD params per TF; dynamic`MIN_STRENGTH_PASS` |
| **5 — LIVE**      | After 2+ stable weeks | Switch`--live` with `EXPECT_ACCOUNT_ID` + guard active     |

---

## ✅ 7. Pre-LIVE Go/No-Go Checklist

- [ ] `EXPECT_ACCOUNT_ID` set and verified
- [ ] 7+ days Dry-Run with no unexpected signals
- [ ] No `SL_repaired` drift > 0.1%
- [ ] Max drawdown < 5% observed
- [ ] Log review automated or scheduled daily
- [ ] Stop-loss at broker level confirmed for all open positions
- [ ] Emergency kill-switch / position-flush procedure documented

---

> **File Location**: `docs/tuning_audit_checklist_v1.md`
> **Next Update**: After first full week of lowered-threshold runs
> **Maintainer**: AI-Sprint / NBK202500000057

---

### How to Save

```bash
mkdir -p ~/projects/gemini_ai/docs
# Paste above content into file
nano ~/projects/gemini_ai/docs/tuning_audit_checklist_v1.md
# Commit
git add docs/tuning_audit_checklist_v1.md
git commit -m "Add tuning & audit checklist v1.0"
```

Do you want me to generate the **side-by-side baseline vs new parameter diff script** so you can run both configurations in parallel during dry-run validation?
