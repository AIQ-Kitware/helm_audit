#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="${GPT_OSS_BUNDLE_ROOT:-$STORE_ROOT/local-bundles/gpt_oss_20b_vllm}"
ENV_FPATH="${LITELLM_ENV_FPATH:-/data/service/service-repo/vllm/generated/.env}"
LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://localhost:14000}"

if [[ -f "$ENV_FPATH" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FPATH"
fi

cd "$ROOT"
python -m eval_audit.integrations.vllm_service export-benchmark-bundle \
  --preset gpt_oss_20b_vllm \
  --bundle-root "$BUNDLE_ROOT" \
  --base-url "${LITELLM_BASE_URL}/v1"

echo "$BUNDLE_ROOT"
