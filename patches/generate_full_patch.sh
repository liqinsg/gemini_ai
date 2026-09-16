#!/usr/bin/env bash
set -euo pipefail

# generate_full_patch.sh
# Usage: run from repo root: ./patches/generate_full_patch.sh
# Creates: patches/scheduled_runner_jcs_v3.patch

REPO_ROOT=$(pwd)
PATCH_DIR="$REPO_ROOT/patches"
PATCH_NAME="scheduled_runner_jcs_v3.patch"
PATCH_PATH="$PATCH_DIR/$PATCH_NAME"

# Files mapping: target -> source patch file (relative to repo root)
# Note: patches must exist in patches/ before running this script.
MAPPINGS=(
  "scheduled_runner_jcs.py:patches/scheduled_runner_jcs_v3.py"
  "utils/safety_helpers.py:patches/utils_safety_helpers.py"
)

echo "Creating git-format patch (staged diff) at: $PATCH_PATH"

# Backup original files (if present)
BACKUPS=()
for mapping in "${MAPPINGS[@]}"; do
  target="${mapping%%:*}"
  src="${mapping##*:}"
  if [ -f "$target" ]; then
    backup="$PATCH_DIR/backup_$(basename "$target")"
    cp "$target" "$backup"
    BACKUPS+=("$target:$backup")
  fi
done

# Copy patched versions into place and stage
for mapping in "${MAPPINGS[@]}"; do
  target="${mapping%%:*}"
  src="${mapping##*:}"

  if [ ! -f "$src" ]; then
    echo "ERROR: patch source file missing: $src" >&2
    exit 2
  fi

  mkdir -p "$(dirname "$target")"
  cp "$src" "$target"
  git add "$target"
done

# Create the patch
git diff --staged > "$PATCH_PATH" || true

echo "Patch created: $PATCH_PATH"

# Restore originals and unstage
for mapping in "${MAPPINGS[@]}"; do
  target="${mapping%%:*}"
  # If the file existed before, restore it from backup
  restored=false
  for b in "${BACKUPS[@]}"; do
    orig="${b%%:*}"
    backup="${b##*:}"
    if [ "$orig" = "$target" ]; then
      mv "$backup" "$target"
      git restore --staged "$target" || git reset HEAD "$target" || true
      restored=true
      break
    fi
  done
  if ! $restored; then
    # file didn't exist before; remove the temporary file and unstage
    git rm --cached "$target" || true
    rm -f "$target"
  fi
done

# Clean any leftover staged state
git reset --hard --quiet || true

echo "Done. Inspect the patch at: $PATCH_PATH"

exit 0
