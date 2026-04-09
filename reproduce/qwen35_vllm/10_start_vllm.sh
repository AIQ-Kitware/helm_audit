#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
exec bash configs/local_models/qwen35_9b_vllm/start_vllm.sh "$@"
