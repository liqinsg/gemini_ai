
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
