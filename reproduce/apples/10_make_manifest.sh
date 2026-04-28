#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
cd "$ROOT"
eval-audit-make-manifest preset \
  --manifest-type apples \
  --experiment-name audit-smoke-apples \
  --suite audit-smoke-apples \
  --output "$STORE_ROOT/configs/manifests/apples_manifest.generated.yaml"
