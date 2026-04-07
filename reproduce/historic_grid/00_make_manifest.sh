#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
cd "$ROOT"
helm-audit-make-manifest historic \
  --output "$STORE_ROOT/configs/manifests/historic_grid.generated.yaml" \
  --experiment-name audit-historic-grid \
  --suite audit-historic-grid
