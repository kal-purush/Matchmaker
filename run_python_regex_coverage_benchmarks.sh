#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  else
    PYTHON_BIN=python3
  fi
fi

COMMON_ARGS=(
  --runs 30
  --n-positive 1000
  --n-negative 1000
  --workers 1
  --external-tool-timeout-seconds 10
)

"$PYTHON_BIN" \
  "$ROOT_DIR/benchmarks/python_regex_function_coverage_benchmark.py" \
  "${COMMON_ARGS[@]}"

# "$PYTHON_BIN" \
#   "$ROOT_DIR/benchmarks/python_regex_function_combined_tools_coverage_benchmark.py" \
#   "${COMMON_ARGS[@]}"
