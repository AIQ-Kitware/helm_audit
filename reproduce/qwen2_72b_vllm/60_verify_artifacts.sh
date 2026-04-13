#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/helm-run-dir" >&2
  exit 2
fi

python configs/local_models/qwen2_72b_vllm/verify_run_artifacts.py "$1"
