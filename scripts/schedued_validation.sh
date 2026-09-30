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