#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_DIR="$ROOT_DIR/patches"
ORIG="$ROOT_DIR/scheduled_runner_jcs.py"
NEW="$PATCH_DIR/scheduled_runner_jcs_v2.py"
BACKUP="$ROOT_DIR/scheduled_runner_jcs.py.bak.$(date +%s)"

if [ ! -f "$NEW" ]; then
  echo "New patched file not found: $NEW"
  exit 1
fi
if [ ! -f "$ORIG" ]; then
  echo "Original file not found: $ORIG"
  exit 1
fi

echo "Backing up original to: $BACKUP"
cp "$ORIG" "$BACKUP"

echo "Showing unified diff (original -> patched):"
# Use diff if available
if command -v diff >/dev/null 2>&1; then
  diff -u "$ORIG" "$NEW" || true
else
  echo "diff not available; skipping diff preview"
fi

read -p "Apply patch (replace original with patched file)? [y/N]: " ans
if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
  echo "Aborting. Original preserved at $BACKUP"
  exit 0
fi

mv "$NEW" "$ORIG"
chmod 644 "$ORIG"

# Quick syntax check
echo "Running python syntax check..."
python -m py_compile "$ORIG"

echo "Patch applied and syntax check passed. Original backed up at: $BACKUP"

echo "If you want to generate a git patch file for review, run:" 
echo "  git add scheduled_runner_jcs.py && git diff --staged > patches/scheduled_runner_jcs_v2.patch"

exit 0
