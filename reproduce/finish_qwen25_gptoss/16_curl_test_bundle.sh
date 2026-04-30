#!/usr/bin/env bash
# Issue a 1-token completion to every model_deployment in the bundle,
# using the exact ``api_key`` and ``base_url`` HELM will use.
#
# This is the more-diagnostic sibling of 15_validate_server.sh:
#   - 15_validate_server.sh curls the LiteLLM router using
#     ``$LITELLM_MASTER_KEY`` from the host environment, which tells
#     you whether the *router* is healthy.
#   - 16_curl_test_bundle.sh reads ``model_deployments.yaml`` from the
#     bundle and curls each entry with **the key HELM will send**,
#     which tells you whether the *bundle* is good. If 15 passes but
#     16 fails, the bundle has the wrong api_key embedded — typically
#     because the .env wasn't sourced when 05_write_bundle.sh ran or
#     the profile's ``api_key_env`` doesn't match the .env's variable
#     name.
#
# Output: a per-deployment OK / FAIL line plus the first 200 chars of
# any error response.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="${BUNDLE_ROOT:-$STORE_ROOT/local-bundles/finish_qwen25_gptoss}"
DEPLOYMENTS_FPATH="$BUNDLE_ROOT/model_deployments.yaml"

cd "$ROOT"

if [[ ! -f "$DEPLOYMENTS_FPATH" ]]; then
  echo "FAIL: model_deployments.yaml missing at $DEPLOYMENTS_FPATH" >&2
  echo "      Run ./05_write_bundle.sh first." >&2
  exit 1
fi

echo "Reading deployments from: $DEPLOYMENTS_FPATH"
echo

# Walk the YAML in Python; emit one ``name TAB base_url TAB key TAB
# protocol_mode TAB client_class`` line per deployment. ``protocol_mode``
# is inferred from the client_class (Chat* → chat; LegacyCompletions*
# → completions).
DEPLOYMENTS_FPATH="$DEPLOYMENTS_FPATH" python3 <<'PY' > /tmp/finish_qwen25_gptoss_deployments.tsv
import os, yaml
from pathlib import Path
data = yaml.safe_load(Path(os.environ["DEPLOYMENTS_FPATH"]).read_text())
for entry in (data.get("model_deployments") or []):
    name = entry.get("name", "")
    cs = entry.get("client_spec") or {}
    cls = cs.get("class_name", "")
    args = cs.get("args") or {}
    base = args.get("base_url", "")
    api_key = args.get("api_key", "") or ""
    proto = "completions" if "Completion" in cls or "Legacy" in cls else "chat"
    print(f"{name}\t{base}\t{api_key}\t{proto}\t{cls}")
PY

n_total=0
n_pass=0
n_fail=0
while IFS=$'\t' read -r name base_url api_key proto cls; do
  n_total=$((n_total + 1))
  echo "=== $name ==="
  echo "  base_url:   $base_url"
  echo "  client:     $cls"
  echo "  protocol:   $proto"
  if [[ -n "$api_key" ]]; then
    # Show only the prefix and length so secrets aren't dumped to logs.
    head4="${api_key:0:4}"
    echo "  api_key:    ${head4}..  (len=${#api_key})"
  else
    echo "  api_key:    (empty)"
  fi

  if [[ "$proto" == "completions" ]]; then
    url="${base_url%/}/completions"
    payload="$(printf '{"model": "%s", "prompt": "Hello, ", "max_tokens": 1}' "$name")"
  else
    url="${base_url%/}/chat/completions"
    payload="$(printf '{"model": "%s", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1}' "$name")"
  fi
  echo "  URL:        $url"

  http_code="$(curl -sS -o /tmp/finish_qwen25_gptoss_curl_body.txt -w '%{http_code}' \
    -H "Content-Type: application/json" \
    ${api_key:+-H "Authorization: Bearer ${api_key}"} \
    -d "$payload" \
    --max-time 60 \
    "$url" || echo "000")"

  if [[ "$http_code" == "200" ]]; then
    echo "  status:     OK ($http_code)"
    n_pass=$((n_pass + 1))
  else
    echo "  status:     FAIL ($http_code)" >&2
    echo "  response:   $(head -c 200 /tmp/finish_qwen25_gptoss_curl_body.txt)" >&2
    n_fail=$((n_fail + 1))
  fi
  echo
done < /tmp/finish_qwen25_gptoss_deployments.tsv

echo "Summary: $n_pass / $n_total passed, $n_fail failed."
rm -f /tmp/finish_qwen25_gptoss_deployments.tsv /tmp/finish_qwen25_gptoss_curl_body.txt
if [[ "$n_fail" -gt 0 ]]; then
  echo
  echo "Diagnosis tips when curl tests fail:"
  echo "  - 401 'LiteLLM Virtual Key expected ... start with sk-':"
  echo "      The api_key in the bundle does not start with 'sk-'."
  echo "      Either the env var the bundle reads has the wrong value,"
  echo "      or LiteLLM was started with a non-sk- master key."
  echo "      Inspect the embedded key with:"
  echo "        grep api_key $BUNDLE_ROOT/model_deployments.yaml"
  echo "  - 401 with no message:"
  echo "      The api_key value in the bundle doesn't match LITELLM_MASTER_KEY"
  echo "      that LiteLLM was started with. Re-source the .env and re-run"
  echo "      05_write_bundle.sh, or pass --api-key-value to the export-bundle"
  echo "      step in 05_write_bundle.sh."
  echo "  - 404 model not found:"
  echo "      LiteLLM router is up but doesn't have an alias for the"
  echo "      model name HELM is using. Check the profile's router.aliases."
  echo "  - connection refused / timeout:"
  echo "      Service container is not running on the target port."
  echo "      ./10_start_service.sh and confirm with 'docker compose ps'."
  exit 1
fi
