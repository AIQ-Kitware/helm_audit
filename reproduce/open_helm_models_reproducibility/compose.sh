#!/usr/bin/env bash
# Compose the open-HELM-models virtual experiment from existing audit data.
# Pre-existing results required; this script does not re-run any benchmarks.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
MANIFEST_FPATH="${MANIFEST_FPATH:-$ROOT/configs/virtual-experiments/open-helm-models-reproducibility.yaml}"
PYTHON_BIN="${PYTHON_BIN:-python}"

required_inputs=(
    "$AUDIT_STORE_ROOT/indexes/audit_results_index.latest.csv"
    "$AUDIT_STORE_ROOT/indexes/official_public_index.latest.csv"
    "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
    "$MANIFEST_FPATH"
)
for path in "${required_inputs[@]}"; do
    if [[ ! -e "$path" ]]; then
        echo "missing required input: $path" >&2
        exit 1
    fi
done

cd "$ROOT"
PYTHONPATH="$ROOT" "$PYTHON_BIN" -m eval_audit.cli.build_virtual_experiment \
    --manifest "$MANIFEST_FPATH" \
    --ensure-local-eee \
    --allow-single-repeat \
    "$@"
