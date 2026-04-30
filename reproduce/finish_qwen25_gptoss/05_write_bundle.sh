#!/usr/bin/env bash
# Write the eval-audit benchmark bundle for the finish_qwen25_gptoss
# preset. The bundle materializes the smoke + full HELM manifests and
# the run_details.yaml that pins the LiteLLM router URL + per-model
# deployment names.
set -euo pipefail
set -x

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="${BUNDLE_ROOT:-$STORE_ROOT/local-bundles/finish_qwen25_gptoss}"
ENV_FPATH="${LITELLM_ENV_FPATH:-/data/service/service-repo/vllm/generated/.env}"
LITELLM_BASE_URL="${LITELLM_BASE_URL:-http://localhost:14000}"

# Clear any stale shell value before sourcing so the .env file is
# the authoritative source. Without this, an old export in the user's
# shell would shadow the file's value and the bundle would embed a
# key that doesn't match what's actually written to the .env. (We
# stop using whatever was in env *before* this script ran.)
unset LITELLM_MASTER_KEY VLLM_BACKEND_API_KEY VLLM_API_KEY KUBEAI_OPENAI_API_KEY

if [[ ! -f "$ENV_FPATH" ]]; then
  echo "FAIL: env file not found at $ENV_FPATH" >&2
  echo "      Set LITELLM_ENV_FPATH=/abs/path/to/.env to override." >&2
  exit 1
fi

# ``set -a`` auto-exports every variable assigned by the sourced file
# so plain ``KEY=value`` lines in vllm_service's generated/.env (no
# ``export`` prefix) propagate to the python subprocess below.
set -a
# shellcheck disable=SC1090
source "$ENV_FPATH"
set +a
if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "FAIL: LITELLM_MASTER_KEY is not set after sourcing $ENV_FPATH." >&2
  echo "      Either the file is missing the variable or it isn't a key=value file" >&2
  echo "      bash can source. Set LITELLM_MASTER_KEY=... in your shell first, or" >&2
  echo "      override LITELLM_ENV_FPATH=/path/to/.env if the default path is wrong." >&2
  exit 1
fi
if [[ ! "$LITELLM_MASTER_KEY" =~ ^sk- ]]; then
  # LiteLLM's proxy validates incoming bearer tokens against the
  # master key but also enforces that virtual keys start with ``sk-``.
  # When the master key itself doesn't start with ``sk-``, the same
  # validator path rejects it with
  #   401 Authentication Error: LiteLLM Virtual Key expected.
  #   Received=<mykey> expected to start with 'sk-'
  # mid-run. Fail at bundle-write time with a clear remediation
  # message instead.
  echo "FAIL: LITELLM_MASTER_KEY does not start with 'sk-'. LiteLLM's" >&2
  echo "      proxy will reject bearer tokens lacking that prefix with" >&2
  echo "      a 401 (\"LiteLLM Virtual Key expected\") mid-run." >&2
  echo "      Fix:" >&2
  echo "        1. Prepend 'sk-' to the value in $ENV_FPATH" >&2
  echo "        2. Restart the litellm container:" >&2
  echo "             cd \"$(dirname "$ENV_FPATH")/.." >&2
  echo "             docker compose -f generated/docker-compose.yml \\" >&2
  echo "                 --env-file generated/.env up -d \\" >&2
  echo "                 --no-deps --force-recreate litellm" >&2
  echo "        3. Re-run this script." >&2
  exit 1
fi

# Log the key prefix so the user can verify the bundle is using the
# expected value. Show only the first 4 chars + length; never dump
# the full secret to stdout.
echo "Sourced LITELLM_MASTER_KEY from $ENV_FPATH" \
     "(prefix=${LITELLM_MASTER_KEY:0:4}.. len=${#LITELLM_MASTER_KEY})"

cd "$ROOT"
python -m eval_audit.integrations.vllm_service export-benchmark-bundle \
  --preset finish_qwen25_gptoss \
  --bundle-root "$BUNDLE_ROOT" \
  --base-url "${LITELLM_BASE_URL}/v1"

echo
echo "Bundle: $BUNDLE_ROOT"
echo "  smoke manifest: $BUNDLE_ROOT/smoke_manifest.yaml"
echo "  full manifest:  $BUNDLE_ROOT/full_manifest.yaml"
