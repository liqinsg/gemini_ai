#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_DIR="$ROOT_DIR/patches"
ORIG="$ROOT_DIR/scheduled_runner_jcs.py"
NEW="$PATCH_DIR/scheduled_runner_jcs_v2.py"
OUT_PATCH="$PATCH_DIR/scheduled_runner_jcs_v2.patch"

if [ ! -f "$NEW" ]; then
  echo "Patched file not found: $NEW"
  exit 1
fi
if [ ! -f "$ORIG" ]; then
  echo "Original file not found: $ORIG"
  exit 1
fi

# Make a timestamped backup of original
BACKUP="$ORIG.bak.$(date +%s)"
cp "$ORIG" "$BACKUP"

# Overwrite original with new, stage, create patch, then restore original
cp "$NEW" "$ORIG"

echo "Creating git-format patch (staged diff) at: $OUT_PATCH"
# Stage the file and produce a patch (will not leave staged changes)
git add "$ORIG"
git diff --staged --binary > "$OUT_PATCH"
# Unstage and restore original
git reset HEAD -- "$ORIG" >/dev/null 2>&1 || true
mv "$BACKUP" "$ORIG"

if [ -s "$OUT_PATCH" ]; then
  echo "Patch created: $OUT_PATCH"
else
  echo "Patch creation failed or produced empty patch. Check git status and try again." >&2
  exit 2
fi

echo "Done. You can apply the patch with: git apply $OUT_PATCH"