#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
cd "$ROOT"
EXPERIMENT_NAME="${1:?experiment name required}"
helm-audit-analyze-experiment \
  --experiment-name "$EXPERIMENT_NAME" \
  --index-dpath "$STORE_ROOT/indexes" \
  --allow-single-repeat
