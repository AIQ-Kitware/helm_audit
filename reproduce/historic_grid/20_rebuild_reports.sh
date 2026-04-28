#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
EXPERIMENT_NAME="${EXPERIMENT_NAME:-audit-historic-grid}"
cd "$ROOT"
eval-audit-index \
  --results-root "$RESULTS_ROOT" \
  --report-dpath "$STORE_ROOT/indexes"
eval-audit-analyze-experiment \
  --experiment-name "$EXPERIMENT_NAME" \
  --index-dpath "$STORE_ROOT/indexes" \
  --allow-single-repeat
python -m eval_audit.workflows.build_reports_summary \
  --experiment-name "$EXPERIMENT_NAME" \
  --index-dpath "$STORE_ROOT/indexes"
