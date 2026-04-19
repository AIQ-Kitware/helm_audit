#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_ROOT="${VLLM_SERVICE_ROOT:-$ROOT/submodules/vllm_service}"
KUBEAI_NAMESPACE="${KUBEAI_NAMESPACE:-kubeai}"
KUBEAI_BASE_URL="${KUBEAI_BASE_URL:-http://127.0.0.1:8000/openai/v1}"

echo "ROOT=$ROOT"
echo "SERVICE_ROOT=$SERVICE_ROOT"
echo "KUBEAI_NAMESPACE=$KUBEAI_NAMESPACE"
echo "KUBEAI_BASE_URL=$KUBEAI_BASE_URL"

command -v python3 >/dev/null
command -v kubectl >/dev/null

kubectl -n "$KUBEAI_NAMESPACE" get pods
kubectl -n "$KUBEAI_NAMESPACE" get model
