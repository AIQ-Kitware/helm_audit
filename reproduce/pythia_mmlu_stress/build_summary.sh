#!/usr/bin/env bash
# Build the aggregate publication surface for the Pythia × MMLU virtual
# experiment: story-arc sankeys, agreement curves, prioritized examples,
# README. Runs against the synthesized index slice produced by compose.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
MANIFEST_FPATH="${MANIFEST_FPATH:-$ROOT/configs/virtual-experiments/pythia-mmlu-stress.yaml}"
PYTHON_BIN="${PYTHON_BIN:-python}"

cd "$ROOT"

# Pull the virtual experiment name + output root straight from the manifest
# so the script stays in sync if either is renamed in the YAML.
read -r EXPERIMENT_NAME OUTPUT_ROOT <<<"$("$PYTHON_BIN" -c "
import sys, yaml
data = yaml.safe_load(open('$MANIFEST_FPATH'))
print(data['name'], data['output']['root'])
")"

INDEX_FPATH="$OUTPUT_ROOT/indexes/audit_results_index.csv"
SUMMARY_ROOT="$OUTPUT_ROOT/reports/aggregate-summary"

if [[ ! -f "$INDEX_FPATH" ]]; then
    echo "synthesized index not found: $INDEX_FPATH" >&2
    echo "run ./compose.sh first." >&2
    exit 1
fi

# --analysis-root points the per-packet scan at the virtual experiment's
# own analysis tree. Without it, _load_all_repro_rows scans only the
# canonical/publication/legacy locations and finds zero rows here, so
# prioritized examples and breakdowns come up empty.
#
# --filter-inventory-json: prefer the scoped inventory the composer wrote,
# which carries Stage-1 eligibility decisions re-stamped with manifest
# scope. That makes Sankey A (Universe -> Scope) render the manifest
# scope as the terminal gate. If the manifest didn't declare a pre_filter
# the file won't exist; fall back to --no-filter-inventory in that case
# so the global Stage-1 inventory doesn't pollute the surface.
SCOPED_FILTER_INVENTORY="$OUTPUT_ROOT/scoped_filter_inventory.json"
if [[ -f "$SCOPED_FILTER_INVENTORY" ]]; then
    INVENTORY_FLAGS=(--filter-inventory-json "$SCOPED_FILTER_INVENTORY")
else
    INVENTORY_FLAGS=(--no-filter-inventory)
fi

PYTHONPATH="$ROOT" "$PYTHON_BIN" -m helm_audit.workflows.build_reports_summary \
    --experiment-name "$EXPERIMENT_NAME" \
    --index-fpath "$INDEX_FPATH" \
    --summary-root "$SUMMARY_ROOT" \
    --analysis-root "$OUTPUT_ROOT" \
    "${INVENTORY_FLAGS[@]}" \
    "$@"

echo
echo "Aggregate publication surface: $SUMMARY_ROOT"
