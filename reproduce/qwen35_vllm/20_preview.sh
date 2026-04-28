#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
eval-audit-run --run=0 configs/qwen35_vllm_smoke_manifest.yaml
