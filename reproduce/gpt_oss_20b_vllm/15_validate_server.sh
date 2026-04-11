#!/usr/bin/env bash
set -euo pipefail

ENV_FPATH="${LITELLM_ENV_FPATH:-/data/service/service-repo/vllm/generated/.env}"
LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://localhost:14000}"

if [[ -f "$ENV_FPATH" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FPATH"
fi

if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "LITELLM_MASTER_KEY is not set and $ENV_FPATH did not provide it" >&2
  exit 1
fi

curl -fsS \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  "${LITELLM_BASE_URL}/v1/models" | jq '.data[:5]'
