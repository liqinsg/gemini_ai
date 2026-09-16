#!/bin/bash
# ============================================================
# 🔍 VERIFY: --debug CLI + Unified Config Loader
# ============================================================

RUNNER="scheduled_runner_v144.py"
PROFILE="--profile 2"

echo "============================================================"
echo "🔍 VERIFICATION — --debug Mode Thresholds"
echo "============================================================"

echo ""
echo "✅ TEST 1/4 — DEFAULT (no --debug) → STRICT MODE"
echo "👉 Command: python $RUNNER $PROFILE"
echo "📋 Expected:"
echo "   ▸ NO debug banner appears"
echo "   ▸ Thresholds: ALIGN=3  GAP=1.5  MIN=-2.0"
echo "────────────────────────────────────────────────────────────"

echo ""
echo "🔴 TEST 2/4 — --debug 3 → FULL OPEN"
echo "👉 Command: python $RUNNER $PROFILE --debug 3"
echo "📋 Expected:"
echo "   ▸ Banner: 🔧 [CONFIG] DEBUG LEVEL 3"
echo "   ▸ Thresholds: ALIGN=1  GAP=999  MIN=-999"
echo "────────────────────────────────────────────────────────────"

echo ""
echo "🟡 TEST 3/4 — --debug 2 → RELAXED"
echo "👉 Command: python $RUNNER $PROFILE --debug 2"
echo "📋 Expected:"
echo "   ▸ Banner: 🔧 [CONFIG] DEBUG LEVEL 2"
echo "   ▸ Thresholds: ALIGN=2  GAP=999  MIN=-999"
echo "────────────────────────────────────────────────────────────"

echo ""
echo "🟢 TEST 4/4 — --debug 1 → MILD"
echo "👉 Command: python $RUNNER $PROFILE --debug 1"
echo "📋 Expected:"
echo "   ▸ Banner: 🔧 [CONFIG] DEBUG LEVEL 1"
echo "   ▸ Thresholds: ALIGN=2  GAP=3.0  MIN=-3.0"
echo "────────────────────────────────────────────────────────────"

echo ""
echo "============================================================"
echo "✅ USAGE: Copy-paste commands above one by one"
echo "============================================================"
echo "📋 CHECKLIST:"
echo "   [ ] No --debug → strict, NO banner"
echo "   [ ] --debug 3 → LEVEL 3 + all filters open"
echo "   [ ] --debug 2 → LEVEL 2 + relaxed"
echo "   [ ] --debug 1 → LEVEL 1 + mild"
echo "============================================================"
