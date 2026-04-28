#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
cd "$ROOT"
eval-audit-run --run=1 "$STORE_ROOT/configs/manifests/historic_grid.generated.yaml"
