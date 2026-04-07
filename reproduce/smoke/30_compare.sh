#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
cd "$ROOT"
helm-audit-compare-batch --manifest "$STORE_ROOT/configs/manifests/smoke_manifest.generated.yaml"
