#!/usr/bin/env bash
# Run the EEE-only analysis pipeline against the symlink tree built
# by 10_link_tree.sh. Pure analysis; no model loads.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
OUT_ROOT="${OUT_ROOT:-$STORE_ROOT/inspectai-helm-eee-compare}"
OUT_TREE="${OUT_TREE:-$OUT_ROOT/eee_artifacts}"
FROM_EEE_OUT="${FROM_EEE_OUT:-$OUT_ROOT/from_eee_out}"

cd "$ROOT"

if [[ ! -d "$OUT_TREE" ]]; then
  echo "FAIL: artifact tree '$OUT_TREE' missing; run ./10_link_tree.sh first." >&2
  exit 1
fi

eval-audit-from-eee \
  --eee-root "$OUT_TREE" \
  --out-dpath "$FROM_EEE_OUT" \
  --clean

echo
echo "Per-packet reports:"
find "$FROM_EEE_OUT" -mindepth 3 -maxdepth 3 -type d -path '*/core-reports/*' -printf '  %p\n' | sort
echo
echo "Quick read: $FROM_EEE_OUT/<experiment>/core-reports/<packet>/core_metric_report.txt"
echo "Next: ./30_inspect.sh"
