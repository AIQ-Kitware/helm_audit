#!/usr/bin/env bash
# Run eval-audit-from-eee against the symlink tree from 10_link_tree.sh.
# Produces per-pair core_metric_report.json files used by 30_heatmap.sh.
#
# Outputs under $FROM_EEE_OUT (default $OUT_ROOT/from_eee_out):
#   audit_results_index.csv
#   official_public_index.csv
#   open-helm-models-reproducibility/core-reports/<packet>/
#     core_metric_report.{txt,json,png}
#   aggregate-summary/all-results/README.txt
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
OUT_ROOT="${OUT_ROOT:-$STORE_ROOT/eee-only-reproducibility-heatmap}"
OUT_TREE="${OUT_TREE:-$OUT_ROOT/eee_artifacts}"
FROM_EEE_OUT="${FROM_EEE_OUT:-$OUT_ROOT/from_eee_out}"

cd "$ROOT"

if [ ! -d "$OUT_TREE" ]; then
  echo "FAIL: artifact tree '$OUT_TREE' missing; run ./10_link_tree.sh first." >&2
  exit 1
fi

echo "Running eval-audit-from-eee ..."
echo "  tree:   $OUT_TREE"
echo "  output: $FROM_EEE_OUT"
echo

# Default to half the host's CPU cores (rounded down) so packet
# rendering parallelizes without starving the OS / your editor.
# Override by exporting WORKERS=N before invoking 20_run.sh.
WORKERS="${WORKERS:-$(( $(nproc 2>/dev/null || echo 2) / 2 ))}"
echo "Parallelism: WORKERS=$WORKERS  (set WORKERS=1 to serialize, =0 for auto)"

eval-audit-from-eee \
  --eee-root "$OUT_TREE" \
  --out-dpath "$FROM_EEE_OUT" \
  --workers "$WORKERS" \
  --clean \
  --build-aggregate-summary

echo
echo "Per-packet reports:"
find "$FROM_EEE_OUT" -mindepth 3 -maxdepth 3 -type d -path '*/core-reports/*' -printf '  %p\n' | sort
echo
echo "Aggregate summary: $FROM_EEE_OUT/aggregate-summary/all-results/README.txt"
echo
echo "Next: ./30_heatmap.sh"
