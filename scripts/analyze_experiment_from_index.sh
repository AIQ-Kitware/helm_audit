#!/usr/bin/env bash
set -euo pipefail
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
audit::set_defaults

"$AIQ_PYTHON" -m helm_reproducibility.analyze_experiment_from_index "$@"
