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