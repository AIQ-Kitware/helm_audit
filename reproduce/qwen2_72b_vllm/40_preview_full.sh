#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="${QWEN2_72B_BUNDLE_ROOT:-$STORE_ROOT/local-bundles/qwen2_72b_vllm}"
cd "$ROOT"
bash reproduce/qwen2_72b_vllm/05_write_bundle.sh >/dev/null
eval-audit-run --run=0 "$BUNDLE_ROOT/full_manifest.yaml"
