#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_ROOT="${VLLM_SERVICE_ROOT:-$ROOT/submodules/vllm_service}"
KUBEAI_NAMESPACE="${KUBEAI_NAMESPACE:-kubeai}"
PYTHON_BIN="${VLLM_SERVICE_PYTHON:-python3}"

cd "$SERVICE_ROOT"

"$PYTHON_BIN" manage.py setup \
  --backend kubeai \
  --profile qwen2-5-7b-instruct-turbo-default \
  --namespace "$KUBEAI_NAMESPACE"

"$PYTHON_BIN" manage.py validate
"$PYTHON_BIN" manage.py deploy

# Apply Vicuna as an additional KubeAI Model. `kubectl apply` is additive here,
# so the earlier Qwen Model remains live on the cluster.
"$PYTHON_BIN" manage.py switch vicuna-7b-v1-3-no-chat-template --apply --namespace "$KUBEAI_NAMESPACE"

# Restore the default active profile locally without disturbing the already
# applied KubeAI Models so future one-model commands stay unsurprising.
"$PYTHON_BIN" manage.py switch qwen2-5-7b-instruct-turbo-default --namespace "$KUBEAI_NAMESPACE"
"$PYTHON_BIN" manage.py status
