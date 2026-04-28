#!/usr/bin/env bash
# Build the aggregate publication surface for the open-HELM-models virtual
# experiment. Runs against the synthesized index slice produced by compose.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
MANIFEST_FPATH="${MANIFEST_FPATH:-$ROOT/configs/virtual-experiments/open-helm-models-reproducibility.yaml}"
PYTHON_BIN="${PYTHON_BIN:-python}"

cd "$ROOT"

read -r EXPERIMENT_NAME OUTPUT_ROOT <<<"$("$PYTHON_BIN" -c "
import yaml
data = yaml.safe_load(open('$MANIFEST_FPATH'))
print(data['name'], data['output']['root'])
")"

INDEX_FPATH="$OUTPUT_ROOT/indexes/audit_results_index.csv"
SUMMARY_ROOT="$OUTPUT_ROOT/reports/aggregate-summary"
SCOPED_FILTER_INVENTORY="$OUTPUT_ROOT/scoped_filter_inventory.json"

if [[ ! -f "$INDEX_FPATH" ]]; then
    echo "synthesized index not found: $INDEX_FPATH" >&2
    echo "run ./compose.sh first." >&2
    exit 1
fi

# Use the scoped filter inventory if the manifest declared a pre_filter.
if [[ -f "$SCOPED_FILTER_INVENTORY" ]]; then
    INVENTORY_FLAGS=(--filter-inventory-json "$SCOPED_FILTER_INVENTORY")
else
    INVENTORY_FLAGS=(--no-filter-inventory)
fi

PYTHONPATH="$ROOT" "$PYTHON_BIN" -m eval_audit.workflows.build_reports_summary \
    --experiment-name "$EXPERIMENT_NAME" \
    --index-fpath "$INDEX_FPATH" \
    --summary-root "$SUMMARY_ROOT" \
    --analysis-root "$OUTPUT_ROOT" \
    "${INVENTORY_FLAGS[@]}" \
    "$@"

echo
echo "Aggregate publication surface: $SUMMARY_ROOT"
