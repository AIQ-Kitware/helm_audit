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

if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
  echo "LITELLM_MASTER_KEY is not set and $ENV_FPATH did not provide it" >&2
  exit 1
fi

mkdir -p "$BUNDLE_ROOT"

cat > "$BUNDLE_ROOT/model_deployments.yaml" <<YAML
model_deployments:
  - name: litellm/gpt-oss-20b-local
    model_name: openai/gpt-oss-20b
    tokenizer_name: openai/o200k_harmony
    max_sequence_length: 32768
    client_spec:
      class_name: "helm.clients.openai_client.OpenAILegacyCompletionsClient"
      args:
        base_url: "${LITELLM_BASE_URL}/v1"
        api_key: "${LITELLM_MASTER_KEY}"
        openai_model_name: "openai/gpt-oss-20b"
  - name: litellm/gpt-oss-20b-chat-local
    model_name: openai/gpt-oss-20b
    tokenizer_name: openai/o200k_harmony
    max_sequence_length: 32768
    client_spec:
      class_name: "helm.clients.openai_client.OpenAIClient"
      args:
        base_url: "${LITELLM_BASE_URL}/v1"
        api_key: "${LITELLM_MASTER_KEY}"
        openai_model_name: "openai/gpt-oss-20b"
YAML

cat > "$BUNDLE_ROOT/smoke_manifest.yaml" <<YAML
schema_version: 1
experiment_name: audit-gpt-oss-20b-vllm-smoke
description: Smoke-test HELM batch for openai/gpt-oss-20b through the local LiteLLM-backed vLLM service.
run_entries:
  - ifeval:model=openai/gpt-oss-20b
  - bbq:subject=all,method=multiple_choice_joint,max_train_instances=0,model=openai/gpt-oss-20b
max_eval_instances: 5
suite: audit-gpt-oss-20b-vllm-smoke
mode: compute_if_missing
materialize: symlink
backend: tmux
devices: 0
tmux_workers: 1
local_path: prod_env
precomputed_root: null
require_per_instance_stats: true
model_deployments_fpath: "$BUNDLE_ROOT/model_deployments.yaml"
enable_huggingface_models: []
enable_local_huggingface_models: []
YAML

cat > "$BUNDLE_ROOT/full_manifest.yaml" <<YAML
schema_version: 1
experiment_name: audit-historic-grid-gpt-oss-20b-vllm
description: Targeted historic-grid extension for openai/gpt-oss-20b using the local LiteLLM-backed vLLM service.
run_entries:
  - bbq:subject=all,method=multiple_choice_joint,max_train_instances=0,model=openai/gpt-oss-20b
  - gpqa:subset=gpqa_main,use_chain_of_thought=true,use_few_shot=false,model=openai/gpt-oss-20b
  - ifeval:model=openai/gpt-oss-20b
  - mmlu_pro:subset=all,use_chain_of_thought=true,use_few_shot=false,model=openai/gpt-oss-20b
max_eval_instances: 1000
suite: audit-historic-grid-gpt-oss-20b-vllm
mode: compute_if_missing
materialize: symlink
backend: tmux
devices: 0
tmux_workers: 1
local_path: prod_env
precomputed_root: null
require_per_instance_stats: true
model_deployments_fpath: "$BUNDLE_ROOT/model_deployments.yaml"
enable_huggingface_models: []
enable_local_huggingface_models: []
YAML

echo "$BUNDLE_ROOT"
