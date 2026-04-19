#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="${SMALL_MODELS_KUBEAI_BUNDLE_ROOT:-$STORE_ROOT/local-bundles/small_models_kubeai_overnight}"
KUBEAI_BASE_URL="${KUBEAI_BASE_URL:-http://127.0.0.1:8000/openai/v1}"

cd "$ROOT"
python3 -m helm_audit.integrations.vllm_service export-benchmark-bundle \
  --preset small_models_kubeai_overnight \
  --bundle-root "$BUNDLE_ROOT" \
  --base-url "$KUBEAI_BASE_URL"

echo "$BUNDLE_ROOT"
