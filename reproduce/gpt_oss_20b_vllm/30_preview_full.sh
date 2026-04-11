#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="$STORE_ROOT/local-bundles/gpt_oss_20b_vllm"
cd "$ROOT"
bash reproduce/gpt_oss_20b_vllm/05_write_bundle.sh >/dev/null
helm-audit-run --run=0 "$BUNDLE_ROOT/full_manifest.yaml"
