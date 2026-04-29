#!/usr/bin/env bash
# Smoke-test the LiteLLM router for the four profile services.
#
# Verifies that each model alias resolves and that a 1-token completion
# round-trips end-to-end. This catches the most common failure modes
# before the audit runs hit them: router alias not registered, vLLM
# container failed to load, chat-compat shim mis-configured, etc.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FPATH="${LITELLM_ENV_FPATH:-/data/service/service-repo/vllm/generated/.env}"
LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://localhost:14000}"

if [[ -f "$ENV_FPATH" ]]; then
  # ``set -a`` so plain ``KEY=value`` lines in vllm_service's
  # generated/.env (no ``export`` prefix) make it into env for the
  # curl invocations below.
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FPATH"
  set +a
fi
if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "WARN: LITELLM_MASTER_KEY not set; the unauthenticated check below may fail." >&2
fi

ALIASES=(
  "openai/gpt-oss-20b"
  "qwen/qwen2.5-7b-instruct-turbo"
  "eleutherai/pythia-6.9b"
  "eleutherai/pythia-2.8b-v0"
)

cd "$ROOT"

echo "== /v1/models =="
curl -fsS "${LITELLM_BASE_URL}/v1/models" \
  ${LITELLM_MASTER_KEY:+-H "Authorization: Bearer ${LITELLM_MASTER_KEY}"} \
  | python -c 'import json,sys; data=json.load(sys.stdin); print("\n".join(m["id"] for m in data.get("data",[])))'

echo
echo "== per-alias smoke =="
for alias in "${ALIASES[@]}"; do
  echo "--- $alias ---"
  # Pythia services serve /v1/completions only; everything else uses chat.
  if [[ "$alias" == eleutherai/pythia-* ]]; then
    PAYLOAD="$(printf '{"model": "%s", "prompt": "Hello, ", "max_tokens": 1}' "$alias")"
    URL="${LITELLM_BASE_URL}/v1/completions"
  else
    PAYLOAD="$(printf '{"model": "%s", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1}' "$alias")"
    URL="${LITELLM_BASE_URL}/v1/chat/completions"
  fi
  if ! curl -fsS "$URL" \
        -H "Content-Type: application/json" \
        ${LITELLM_MASTER_KEY:+-H "Authorization: Bearer ${LITELLM_MASTER_KEY}"} \
        -d "$PAYLOAD" \
        --max-time 60 \
      | python -c 'import json,sys; d=json.load(sys.stdin); print("ok") if (d.get("choices") or d.get("usage")) else (sys.exit(1))'; then
    echo "FAIL: $alias did not respond with a usable completion." >&2
    exit 1
  fi
done

echo
echo "All four model aliases respond. Next: ./20_preview_smoke.sh"
