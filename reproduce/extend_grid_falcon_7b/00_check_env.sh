#!/usr/bin/env bash
# Preflight for the Falcon-7B heatmap-grid extension run.
#
# Verifies:
#   - eval_audit CLI scripts are on $PATH
#   - a GPU is visible to nvidia-smi (and reports free VRAM)
#   - $AUDIT_STORE_ROOT / $AUDIT_RESULTS_ROOT exist + writable
#   - HuggingFace cache dir is writable
#   - helm-run is on $PATH (the actual model loader)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
HF_CACHE_DIR="${HF_HOME:-$HOME/.cache/huggingface}"

cd "$ROOT"

echo "== eval_audit env =="
which eval-audit-check-env || { echo "FAIL: eval-audit-check-env not on PATH; run 'uv pip install -e .' first" >&2; exit 1; }
which eval-audit-run || { echo "FAIL: eval-audit-run not on PATH" >&2; exit 1; }
which eval-audit-index || { echo "FAIL: eval-audit-index not on PATH" >&2; exit 1; }
eval-audit-check-env

echo
echo "== GPU =="
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "FAIL: nvidia-smi not found; this runbook requires a GPU" >&2
  exit 1
fi
nvidia-smi --query-gpu=index,name,memory.free,memory.total --format=csv,noheader
echo "(falcon-7b needs ~14 GB VRAM at fp16; verify a single GPU has enough free)"

# Falcon-7B fits in 16 GB headroom. If the smallest visible GPU has
# < 16 GB free, warn — the run will still try, but is likely to OOM.
MIN_FREE_MIB="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | sort -n | head -1)"
if [[ -n "${MIN_FREE_MIB:-}" && "$MIN_FREE_MIB" -lt 16000 ]]; then
  echo "WARN: smallest GPU has only ${MIN_FREE_MIB} MiB free; falcon-7b expects ~14 GB headroom." >&2
fi

echo
echo "== paths =="
mkdir -p "$STORE_ROOT/configs/manifests" "$RESULTS_ROOT" "$HF_CACHE_DIR"
for d in "$STORE_ROOT" "$RESULTS_ROOT" "$HF_CACHE_DIR"; do
  if [[ ! -w "$d" ]]; then
    echo "FAIL: $d is not writable by $(whoami)" >&2
    exit 1
  fi
  echo "  OK: $d"
done
df -h "$RESULTS_ROOT" "$HF_CACHE_DIR" | sed 's/^/    /'

echo
echo "== helm-run =="
if command -v helm-run >/dev/null 2>&1; then
  echo "helm-run found at: $(command -v helm-run)"
  python -c "import helm; print('helm package:', helm.__file__)" || true
else
  echo "FAIL: helm-run not on PATH; eval-audit-run will fail. Install with 'uv pip install crfm-helm[all] -U'." >&2
  exit 1
fi

# Falcon-7B is not gated; we still warn if the user has no HF token,
# because some downstream HF resources (datasets, tokenizers) may
# rate-limit anonymous traffic during long runs.
echo
echo "== HF auth =="
if huggingface-cli whoami >/dev/null 2>&1; then
  echo "  huggingface-cli logged in as: $(huggingface-cli whoami | head -1)"
else
  echo "  WARN: not logged into huggingface-cli. Falcon-7B is open weights" >&2
  echo "        so it will still download, but anonymous traffic can hit" >&2
  echo "        rate limits during a 40-entry run. Run 'huggingface-cli login'" >&2
  echo "        if you have a token." >&2
fi

echo
echo "OK: preflight passed; next: ./10_make_manifest.sh"
