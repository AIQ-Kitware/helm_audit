#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
BUNDLE_ROOT="${SMALL_MODELS_KUBEAI_BUNDLE_ROOT:-$STORE_ROOT/local-bundles/small_models_kubeai_overnight}"
cd "$ROOT"
bash reproduce/small_models_kubeai/10_write_bundle.sh >/dev/null
eval-audit-run "$BUNDLE_ROOT/full_manifest.yaml"
