#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

audit::set_defaults

OUTPUT="${1:-${AUDIT_ROOT}/configs/generated/smoke_manifest.generated.yaml}"
if [[ $# -gt 0 ]]; then
    shift
fi
mkdir -p "$(dirname "$OUTPUT")"

"$AIQ_PYTHON" -m helm_reproducibility.make_manifest \
    --manifest-type smoke \
    --output "$OUTPUT" \
    "$@"

printf 'Wrote smoke manifest: %s\n' "$OUTPUT"
