#!/usr/bin/env bash
# Re-index the local audit results so the Falcon-7B run shows up in
# $AUDIT_STORE_ROOT/indexes/audit_results_index.csv. Run on the
# execution host before rsync so the index travels with the run data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
EXP="${EXP_NAME:-audit-falcon-7b-helm-grid}"

cd "$ROOT"

EXP_DIR="$RESULTS_ROOT/$EXP"
if [[ ! -d "$EXP_DIR" ]]; then
  echo "FAIL: no run dir at $EXP_DIR; did 20_run.sh succeed?" >&2
  exit 1
fi

echo "found run dir: $EXP_DIR"
echo "sample run_spec.json files:"
find "$EXP_DIR" -maxdepth 4 -name 'run_spec.json' | head -5 | sed 's/^/  /'

echo
echo "== eval-audit-index =="
eval-audit-index \
  --results-root "$RESULTS_ROOT" \
  --report-dpath "$STORE_ROOT/indexes"

echo
echo "OK: index refreshed."
echo "  Latest CSV: $STORE_ROOT/indexes/audit_results_index.csv"
echo
echo "Now rsync these back to the analysis host:"
echo "  rsync --exclude scenarios --exclude cache -avPR \\"
echo "    \$THIS_HOST:/data/./crfm-helm-audit/$EXP /data"
echo "  rsync -avPR \$THIS_HOST:/data/./crfm-helm-audit-store/indexes /data"
