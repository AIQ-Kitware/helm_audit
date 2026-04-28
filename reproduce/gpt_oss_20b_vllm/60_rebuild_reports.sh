#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
EXPERIMENT_NAME="${EXPERIMENT_NAME:-audit-historic-grid-gpt-oss-20b-vllm}"
cd "$ROOT"

python -m eval_audit.workflows.index_results \
  --results-root "$RESULTS_ROOT" \
  --report-dpath "$STORE_ROOT/indexes"

python -m eval_audit.workflows.analyze_experiment \
  --experiment-name "$EXPERIMENT_NAME" \
  --index-dpath "$STORE_ROOT/indexes"

python -m eval_audit.workflows.build_reports_summary \
  --experiment-name "$EXPERIMENT_NAME"
