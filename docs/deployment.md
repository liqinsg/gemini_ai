
Here’s the complete **automated scheduled setup** — fully integrated with your parallel validation suite, running at your preferred interval with zero manual work.

---

# ⏰ Scheduled Automation Setup — Cron Job Suite

**Goal**: Auto-run parallel validation → generate diff → update audit → commit
**Interval**: Every 4 hours (aligned with H4 / main decision cycle)
**User**: qili@NBK202500000057
**Project**: `~/projects/gemini_ai`

---

## 1. Master Orchestrator Script

### `scripts/scheduled_validation.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

# ── Paths ──────────────────────────────────────────
PROJECT_DIR="$HOME/projects/gemini_ai"
SCRIPTS_DIR="$PROJECT_DIR/scripts"
cd "$PROJECT_DIR"

# ── Log header ─────────────────────────────────────
echo "══════════════════════════════════════════════════"
echo "  VALIDATION RUN — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "══════════════════════════════════════════════════"

# ── Step 1: Launch parallel baseline + tuned ──────
echo "▶ Step 1/4: Running parallel configs..."
"$SCRIPTS_DIR/parallel_runner.sh"
sleep 5  # allow file flush

# ── Step 2: Quick console diff ────────────────────
echo -e "\n▶ Step 2/4: Console diff check..."
"$SCRIPTS_DIR/diff_analysis.py"

# ── Step 3: Update weekly audit ───────────────────
echo -e "\n▶ Step 3/4: Generating weekly summary..."
"$SCRIPTS_DIR/weekly_summary.py"

# ── Step 4: Commit to git ─────────────────────────
echo -e "\n▶ Step 4/4: Committing artifacts..."
git add docs/validation_audit.md logs/baseline/ logs/tuned/
git diff --staged --quiet && { echo "✅ No new changes to commit"; exit 0; }

COMMIT_MSG="validation: auto-update $(date -u '+%Y-%m-%d %H:%M UTC')"
git commit -m "$COMMIT_MSG"
git push  # push to your remote branch

echo -e "\n✅ RUN COMPLETE — next run in 4 hours"
```

---

## 2. Install & Permissions

```bash
# Save the orchestrator
nano ~/projects/gemini_ai/scripts/scheduled_validation.sh

# Make executable
chmod +x ~/projects/gemini_ai/scripts/*.sh ~/projects/gemini_ai/scripts/*.py
```

---

## 3. Cron Schedule Setup

Edit your crontab:

```bash
crontab -e
```

Paste this line (runs **every 4 hours** at :00 UTC — matches H4 candles):

```
0 */4 * * * /home/qili/projects/gemini_ai/scripts/scheduled_validation.sh >> /home/qili/projects/gemini_ai/logs/cron_master.log 2>&1
```

### Alternative Intervals

| Frequency                   | Cron Line           |
| --------------------------- | ------------------- |
| Every 4 hours (recommended) | `0 */4 * * * ...` |
| Every 2 hours               | `0 */2 * * * ...` |
| Daily 00:00 UTC             | `0 0 * * * ...`   |
| Weekdays only 00:00 UTC     | `0 0 * * 1-5 ...` |

### Verify It’s Active

```bash
crontab -l
# You should see your new line listed
```

---

## 4. Logging & Monitoring

### Master Log

All cron output → `logs/cron_master.log`

```bash
# Tail live feed
tail -f ~/projects/gemini_ai/logs/cron_master.log

# See last 20 runs
grep "VALIDATION RUN" ~/projects/gemini_ai/logs/cron_master.log | tail -20
```

### Rotation (Prevent Disk Bloat)

Add log rotation so files don’t grow indefinitely:

```bash
sudo nano /etc/logrotate.d/gemini_validation
```

Paste:

```
/home/qili/projects/gemini_ai/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
}
```

---

## 5. Manual Override Controls

Use these anytime to interact with the system:

```bash
# Run immediately (test trigger)
~/projects/gemini_ai/scripts/scheduled_validation.sh

# Pause scheduled runs temporarily
crontab -e  # → comment out the cron line with #

# Resume
crontab -e  # → uncomment

# Check last audit report
cat ~/projects/gemini_ai/docs/validation_audit.md
```

---

## 6. Full System Architecture

```
gemini_ai/
├── config/
│   ├── run.env.baseline        # Original thresholds
│   └── run.env.tuned           # 1.65 + reentry rules
├── scripts/
│   ├── parallel_runner.sh      # Launch both configs
│   ├── diff_analysis.py        # Console comparison
│   ├── weekly_summary.py       # 7-day markdown audit
│   └── scheduled_validation.sh ← cron entry point
├── logs/
│   ├── baseline/                # Historical runs
│   ├── tuned/
│   └── cron_master.log         # Cron job output
└── docs/
    ├── tuning_audit_checklist_v1.md   # Standards
    └── validation_audit.md            # Auto-updating report
```

---

## 7. First Activation

After saving all files, run this **once now** to seed your baseline:

```bash
cd ~/projects/gemini_ai
./scripts/scheduled_validation.sh
```

You’ll see:

- ✅ Both configs execute in parallel
- ✅ Console diff shows immediate comparison
- ✅ `validation_audit.md` created
- ✅ Git commit pushed

---

## 8. Success Criteria — 7-Day Roadmap

| Day      | Checkpoint                                                  | Decision              |
| -------- | ----------------------------------------------------------- | --------------------- |
| Day 1–2 | Cron runs consistently; no errors                           | ✅ System stable      |
| Day 3    | Compare daily diffs — more signals = threshold working     | Monitor quality       |
| Day 5    | SKIP_HELD rate > 40% → enable reentry rules                | Edit`run.env.tuned` |
| Day 7    | Review`validation_audit.md` → signal quality maintained  | ✅ Go LIVE            |
| Day 8+   | Switch`--dry-run` → LIVE mode with `EXPECT_ACCOUNT_ID` | Real trading          |

---

Everything is fully wired and ready. Just:

1. Save `scheduled_validation.sh`
2. Paste the cron line
3. Run the activation command once

Your validation suite will then run itself and produce the audit report automatically — no further manual intervention needed.

Want me to also prepare the **final LIVE mode activation checklist** for when you’re ready to go live?
