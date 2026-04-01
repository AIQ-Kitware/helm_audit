#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
helm-audit-make-manifest preset \
  --manifest-type apples \
  --experiment-name audit-smoke-apples \
  --suite audit-smoke-apples \
  --output configs/generated/apples_manifest.generated.yaml
