#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="ai-sprint"

# Create directories
mkdir -p "${PROJECT_ROOT}/tests/unit"
mkdir -p "${PROJECT_ROOT}/tests/integration"
mkdir -p "${PROJECT_ROOT}/tests/regression"

# Create files
touch "${PROJECT_ROOT}/tests/conftest.py"

touch "${PROJECT_ROOT}/tests/unit/test_breadth.py"
touch "${PROJECT_ROOT}/tests/unit/test_rank.py"
touch "${PROJECT_ROOT}/tests/unit/test_scale_behavior.py"
touch "${PROJECT_ROOT}/tests/unit/test_score_bounds.py"
touch "${PROJECT_ROOT}/tests/unit/test_sign_preservation.py"
touch "${PROJECT_ROOT}/tests/unit/test_translation_invariance.py"
touch "${PROJECT_ROOT}/tests/unit/test_weight_validation.py"

touch "${PROJECT_ROOT}/tests/integration/test_jpy_example.py"

touch "${PROJECT_ROOT}/tests/regression/test_proposal_reference.py"

echo "Project structure created successfully."

tree "${PROJECT_ROOT}" || find "${PROJECT_ROOT}" -type f | sort
