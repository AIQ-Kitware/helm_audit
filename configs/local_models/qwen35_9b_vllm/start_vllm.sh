#!/usr/bin/env bash
set -euo pipefail

MODEL_NAME="${MODEL_NAME:-Qwen/Qwen3.5-9B}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
TP_SIZE="${TP_SIZE:-1}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.9}"

# HELM's vLLM client sends api_key="EMPTY".
# The simplest compatible path is to omit --api-key or set it to EMPTY.
exec vllm serve "${MODEL_NAME}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --tensor-parallel-size "${TP_SIZE}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --gpu-memory-utilization "${GPU_MEM_UTIL}"
