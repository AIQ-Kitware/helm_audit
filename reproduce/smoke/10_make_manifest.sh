#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
helm-audit-make-manifest preset \
  --manifest-type smoke \
  --output configs/generated/smoke_manifest.generated.yaml
