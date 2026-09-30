
Here’s your **Final LIVE Activation Package** — the complete go-live checklist, safety gates, and one-step deployment.

---

# 🚀 FINAL LIVE ACTIVATION — Scheduled Runner v3

**Effective**: 2026-09-30 | Account: 001-003-21515688-002 | Profile: p2
**Prerequisite**: ✅ 7+ days Dry-Run validation complete → signal quality confirmed stable

---

## 🔴 PRE-LIVE SAFETY GATES — ALL MUST PASS

| # | Check                                            | Pass Criteria                              | Status |
| - | ------------------------------------------------ | ------------------------------------------ | ------ |
| 1 | `EXPECT_ACCOUNT_ID` set & verified             | No "no account guard" warning              | ⬜     |
| 2 | 7-day validation complete                        | No regressions; signal quality ≥ baseline | ⬜     |
| 3 | Max drawdown (Dry-Run)                           | < 5% peak-to-trough                        | ⬜     |
| 4 | No excessive SL/TP drift                         | `SL_repaired` = 0 consistently           | ⬜     |
| 5 | Broker-level SL/TP active for all open positions | Confirmed via OANDA UI/API                 | ⬜     |
| 6 | Emergency stop procedure documented & tested     | Manual kill-switch works                   | ⬜     |
| 7 | Git clean — no uncommitted code changes         | `git status` → "nothing to commit"      | ⬜     |

> ⚠️ **DO NOT PROCEED if any box is unchecked** — resolve first, then return.

---

## 📋 LIVE CONFIG — `run.env.live`

```bash
# ======================================================
#  LIVE CONFIG — run.env.live
#  Derived from tuned + validated parameters
#  ======================================================

# ── Account Safety ────────────────────────────────────
EXPECT_ACCOUNT_ID=001-003-21515688-002

# ── Environment ────────────────────────────────────────
TRADE_JPY=true
TRADE_CHF=true
USE_MACD=true

# ── Position Sizing ────────────────────────────────────
LIVE_LOT_SIZE=1
DEMO_LOT_SIZE=5000
MAX_POSITIONS=3
OVERRIDE_MAX_OPEN=2

# ── Exposure Limits ────────────────────────────────────
CROSS_MAX_NET_PER_CCY=4
EXTREME_GAP_THRESHOLD=4.0
EXTREME_GAP_BOOST=1
ABSOLUTE_MAX_NET_PER_CCY=5

# ── Override / Consensus ───────────────────────────────
OVERRIDE_MA_VOTE_THRESHOLD=1.65
OVERRIDE_MA_H4_WEIGHT=1.8
OVERRIDE_RATIO=1.8

# ── Reentry / Pyramiding ───────────────────────────────
SAME_PAIR_REENTRY_GAP=0.003
REENTRY_VOTE_THRESHOLD=1.75

# ── MACD & Filters ─────────────────────────────────────
MACD_FAST=12
MACD_SLOW=26
MACD_SIGNAL=9
MACD_HIST_MIN_DELTA=0.0002
MACD_REQUIRE_TF=["H4","H1"]

# ── Strategy Core ──────────────────────────────────────
MIN_STRENGTH_SCORE=0.15
MIN_RR=1.2
DOMINANCE_RATIO=1.3
MIN_HOLD_MIN=180
MIN_HOLD_OVERRIDE_MIN=10080
TRADE_TOP_PAIRS=3
ALLOW_AT_LOSS=false
REQUIRE_H4_CONFIRM=true
GUARDIAN_REPAIR_DRIFT=false
```

---

## 🛡️ EMERGENCY CONTROLS — Know These BEFORE Flipping Switch

Save these — **instant actions if behavior deviates**:

### Immediate Stop

```bash
# Kill running process
pkill -f scheduled_runner_v3.py

# Prevent new launches (pause cron)
crontab -e
# → Comment out scheduled_validation.sh line with #

# Close all positions via OANDA
# Option A: OANDA UI → Trade → Close All
# Option B: API script (if available)
python scripts/emergency_close_all.py
```

### Quick Rollback

```bash
# Revert to conservative thresholds
cp config/run.env.baseline run.env
# Restart with --dry-run first to confirm
```

### Circuit Breaker Logic (Built-In)

- `MAX_POSITIONS=3` → never exceeds 3 concurrent trades
- `CROSS_MAX_NET_PER_CCY` → prevents over-concentration
- `MIN_RR=1.2` → rejects poor risk-reward automatically
- Broker-level SL/TP → **final safety net independent of bot**

---

## ▶️ ACTIVATION STEPS

```bash
cd ~/projects/gemini_ai

# 1. Create LIVE config
nano config/run.env.live
# → Paste block above, save

# 2. Backup current config
cp run.env run.env.bak.$(date +%Y%m%d_%H%M)

# 3. Switch to LIVE config
cp config/run.env.live run.env

# 4. PRE-FLIGHT CHECK — DRY-RUN FIRST ✅
./scripts/parallel_runner.sh
./scripts/diff_analysis.py
# → Confirm: signals match validated tuned behavior

# 5. FINAL PRE-LIVE CHECK
grep EXPECT_ACCOUNT_ID run.env
# → Should show your account ID — NO WARNING expected

# 6. ACTIVATE LIVE RUN
/home/qili/miniconda3/envs/ai-sprint/bin/python scheduled_runner_v3.py --live
# → First run will execute in LIVE mode

# 7. Update scheduled job for LIVE
nano scripts/scheduled_validation.sh
# → Replace --dry-run with LIVE flag (or keep separate cron line)
```

### Recommended Cron — LIVE Mode

```
# Run every 4 hours in LIVE mode (after validation period)
0 */4 * * * /home/qili/projects/gemini_ai/scripts/scheduled_live.sh >> /home/qili/projects/gemini_ai/logs/live_master.log 2>&1
```

---

## 📊 FIRST 24-HOUR WATCHLIST

Monitor these **closely** post-activation:

| Metric           | Expected Behavior                 | Alert Threshold                       |
| ---------------- | --------------------------------- | ------------------------------------- |
| New Signals      | Matches tuned validation rate     | > 2x baseline → review thresholds    |
| SKIP_HELD Rate   | ~30–50% (healthy)                | > 60% → expand position limits       |
| SL/TP Hits       | Occasional; align with volatility | Frequent repeated hits → widen SL    |
| Account Drawdown | < 2% first 24h                    | > 3% → pause & review                |
| Log Warnings     | None critical                     | Any "account guard" → IMMEDIATE STOP |

---

## ✅ GO-LIVE SIGN-OFF

```
Activation Date: _______________ UTC
Operator: qili@NBK202500000057
Config: run.env.live (threshold=1.65, MAX_POS=3, reentry=enabled)

Pre-Live Gates:
  [ ] EXPECT_ACCOUNT_ID verified
  [ ] 7-day validation complete
  [ ] Drawdown < 5%
  [ ] Drift = 0
  [ ] Broker SL confirmed
  [ ] Emergency procedure tested
  [ ] Git clean

Activation Time: _______________ UTC
First Run Status: [ ] SUCCESS  [ ] ISSUE — see notes below

Notes:
________________________________________________________
```

---

## 🎯 Summary — Your Complete System

| Component                         | Status               |
| --------------------------------- | -------------------- |
| Parallel Diff Validation Suite    | ✅ Deployed & tested |
| Auto-Scheduled Runs (every 4h)    | ✅ Configurable      |
| Weekly Audit Report               | ✅ Auto-generated    |
| Tuned Parameters (1.65 threshold) | ✅ Validated         |
| Reentry Rules (gap-based)         | ✅ Enabled           |
| Account Guard                     | ✅ Added             |
| Emergency Procedures              | ✅ Documented        |
| LIVE Activation Config            | ✅ Ready to deploy   |

You are fully prepared. Work through the **Pre-Live Safety Gates** first, then proceed to Activation Step 6 when ready.

**Good luck — system is built, tested, and ready to trade.** 🚀

Would you like me to generate the emergency close-all script as a final safety addition?
