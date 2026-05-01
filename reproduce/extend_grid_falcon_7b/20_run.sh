#!/usr/bin/env bash
# Execute the Falcon-7B grid via the
# eval-audit-run / kwdagger / magnet / helm-run chain.
#
# Long-running step. With DEVICES=0,1,2,3 and TMUX_WORKERS=4 the 40
# run-specs dispatch four at a time; expect a few hours total
# depending on benchmark size and GPU class.
#
# HELM's compute_if_missing mode skips run-specs that already produced
# a DONE marker, so re-invoking after a partial run picks up where
# the previous attempt left off. The preflight below lists the
# run-specs that are already DONE on this host vs. pending.
#
# If the eval-audit-run / kwdagger chain has bit-rotted, see the
# Fallback section in this directory's README for a direct helm-run
# invocation that bypasses it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
EXP="${EXP_NAME:-audit-falcon-7b-helm-grid}"

cd "$ROOT"

MANIFEST_FPATH="$STORE_ROOT/configs/manifests/$EXP.yaml"
if [[ ! -f "$MANIFEST_FPATH" ]]; then
  echo "FAIL: manifest not found at $MANIFEST_FPATH; run 10_make_manifest.sh first" >&2
  exit 1
fi

# Preflight: which run-specs in this manifest already have a DONE marker.
echo "== existing on-disk runs under $RESULTS_ROOT/$EXP =="
EXP_DIR="$RESULTS_ROOT/$EXP"
if [[ -d "$EXP_DIR" ]]; then
  MANIFEST_ENTRIES=$(awk '
    /^run_entries:/ {in_block=1; next}
    in_block && /^  - / {sub(/^  - /,""); print; next}
    in_block && /^[A-Za-z]/ {in_block=0}
  ' "$MANIFEST_FPATH")
  n_done=0; n_pending=0
  while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    # HELM rewrites the run dir to use _-form for the model
    # (tiiuae/falcon-7b -> tiiuae_falcon-7b). Mirror that.
    name_us=$(printf '%s' "$entry" | sed 's|model=tiiuae/|model=tiiuae_|')
    matched=$(find "$EXP_DIR" -mindepth 4 -maxdepth 5 -type f -name DONE 2>/dev/null \
              | xargs -I{} dirname {} 2>/dev/null \
              | grep -F "/${name_us}" | head -1 || true)
    if [[ -n "$matched" ]]; then
      n_done=$((n_done + 1))
    else
      n_pending=$((n_pending + 1))
    fi
  done <<<"$MANIFEST_ENTRIES"
  echo "  $n_done DONE, $n_pending pending (of $((n_done + n_pending)) run_entries)"
else
  n_total=$(grep -c '^  - ' "$MANIFEST_FPATH" || true)
  echo "  (no prior runs at $EXP_DIR; HELM will run all $n_total entries from scratch)"
fi
echo

# Step 1 — preview. eval-audit-run --run=0 dry-runs the kwdagger argv.
echo "== preview (eval-audit-run --run=0) =="
eval-audit-run --run=0 "$MANIFEST_FPATH"

# Step 2 — execute.
echo
echo "== execute (eval-audit-run --run=1) =="
eval-audit-run --run=1 "$MANIFEST_FPATH"

echo
echo "OK: run finished. Inspect output under $EXP_DIR/helm/."
echo "Next: ./30_index_local.sh to refresh the audit index before rsync."
