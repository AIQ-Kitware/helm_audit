#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="${QWEN2_72B_BUNDLE_ROOT:-$STORE_ROOT/local-bundles/qwen2_72b_vllm}"
QWEN_VLLM_BASE_URL="${QWEN_VLLM_BASE_URL:-http://localhost:8000/v1}"

cd "$ROOT"
python -m helm_audit.integrations.vllm_service export-benchmark-bundle \
  --preset qwen2_72b_vllm \
  --bundle-root "$BUNDLE_ROOT" \
  --base-url "$QWEN_VLLM_BASE_URL"

echo "$BUNDLE_ROOT"
