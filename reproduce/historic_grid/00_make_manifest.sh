#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
helm-audit-make-manifest historic \
  --output configs/generated/historic_grid.generated.yaml \
  --experiment-name audit-historic-grid \
  --suite audit-historic-grid
