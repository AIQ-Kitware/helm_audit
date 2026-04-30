#!/usr/bin/env bash
# Generate the 3-model × 14-benchmark reproducibility heatmap.
# Reads the core_metric_report.json files written by 20_run.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
OUT_ROOT="${OUT_ROOT:-$STORE_ROOT/eee-only-reproducibility-heatmap}"
FROM_EEE_OUT="${FROM_EEE_OUT:-$OUT_ROOT/from_eee_out}"
HEATMAP_OUT="${HEATMAP_OUT:-$OUT_ROOT/heatmap}"
ABS_TOL="${ABS_TOL:-1e-9}"

cd "$ROOT"

if ! find "$FROM_EEE_OUT" -name "core_metric_report.json" 2>/dev/null | grep -q .; then
  echo "FAIL: no core_metric_report.json files under '$FROM_EEE_OUT'." >&2
  echo "      Run ./20_run.sh first." >&2
  exit 1
fi

echo "Generating reproducibility heatmap ..."
echo "  analysis root: $FROM_EEE_OUT"
echo "  output dir:    $HEATMAP_OUT"
echo "  abs_tol:       $ABS_TOL"
echo

python3 -m eval_audit.reports.eee_only_heatmap \
  --analysis-root "$FROM_EEE_OUT" \
  --out-dir "$HEATMAP_OUT" \
  --abs-tol "$ABS_TOL"

echo
echo "Outputs:"
find "$HEATMAP_OUT" -type f | sort | sed 's/^/  /'
