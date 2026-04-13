#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
helm-audit-run --run=0 configs/qwen2_72b_vllm_manifest.yaml
