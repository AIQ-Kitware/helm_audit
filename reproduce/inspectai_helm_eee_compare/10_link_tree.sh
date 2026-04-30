#!/usr/bin/env bash
# Build a from_eee-shaped symlink tree that mixes three EEE sources
# the user "believes should all be comparable":
#
#   $OUT_TREE/
#     official/mmlu/eleutherai/pythia-6.9b/<uuid>.{json,_samples.jsonl}
#     official/mmlu/eleutherai/pythia-6.9b/run_spec.json   (HELM sidecar)
#     local/audit-mmlu-usfp-pythia-r1/mmlu/eleutherai/pythia-6.9b/<uuid>.{json,_samples.jsonl}
#     local/audit-mmlu-usfp-pythia-r2/mmlu/eleutherai/pythia-6.9b/<uuid>.{json,_samples.jsonl}
#     local/inspectai/mmlu/eleutherai/pythia-6.9b/<uuid>.{json,_samples.jsonl}
#
# The first two HELM-derived locals get their HELM run_spec.json
# sidecars copied so the planner picks them up; the InspectAI artifact
# deliberately has no sidecar — that's the cross-harness case the
# runbook is meant to expose.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
PUBLIC_HELM_ROOT="${HELM_PUBLIC_ROOT:-/data/crfm-helm-public}"
OUT_ROOT="${OUT_ROOT:-$STORE_ROOT/inspectai-helm-eee-compare}"
OUT_TREE="$OUT_ROOT/eee_artifacts"

OFFICIAL_RUN_DIR="$STORE_ROOT/crfm-helm-public-eee-test/classic/v0.3.0/mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
# Canonical HELM run dir for the public-side run_spec.json (lives in
# the public-helm tree, not the EEE-converted output).
OFFICIAL_RUN_SPEC_DIR="$PUBLIC_HELM_ROOT/classic/benchmark_output/runs/v0.3.0/mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
LOCAL_EXPS=(
  audit-mmlu-usfp-pythia-r1
  audit-mmlu-usfp-pythia-r2
)
INSPECTAI_DIR="$STORE_ROOT/inspectai-eee-results/MMLU-Inspect-EEE"

cd "$ROOT"

echo "Cleaning $OUT_TREE ..."
rm -rf "$OUT_TREE"
mkdir -p "$OUT_TREE/official" "$OUT_TREE/local"

# --- helper: link <uuid>.json + <uuid>_samples.jsonl from a source dir
# into a destination dir. If a HELM run_spec.json sidecar lives next
# to (or one dir up from) the EEE artifact, copy it too.
link_eee_pair() {
  local src_aggregate="$1"
  local dst_dir="$2"
  local sidecar_root="${3:-}"   # optional: where to look for run_spec.json
  local src_dir
  src_dir="$(dirname "$src_aggregate")"
  local uuid
  uuid="$(basename "$src_aggregate" .json)"
  local src_samples="$src_dir/${uuid}_samples.jsonl"
  if [[ ! -f "$src_samples" ]]; then
    echo "  FAIL: missing samples sibling for $src_aggregate" >&2
    exit 1
  fi
  mkdir -p "$dst_dir"
  ln -sf "$src_aggregate" "$dst_dir/$uuid.json"
  ln -sf "$src_samples" "$dst_dir/${uuid}_samples.jsonl"
  if [[ -n "$sidecar_root" && -f "$sidecar_root/run_spec.json" ]]; then
    ln -sf "$sidecar_root/run_spec.json" "$dst_dir/run_spec.json"
  fi
}

# --- Official HELM side: pick one aggregate from the eee_output dir,
# link with the HELM run_spec.json sidecar.
echo
echo "== linking official =="
src_aggregate="$(find "$OFFICIAL_RUN_DIR/eee_output" -name '*.json' ! -name '*_samples.jsonl' 2>/dev/null | sort | head -1)"
if [[ -z "$src_aggregate" ]]; then
  echo "  FAIL: no aggregate JSON under $OFFICIAL_RUN_DIR/eee_output" >&2
  exit 1
fi
dst_dir="$OUT_TREE/official/mmlu/eleutherai/pythia-6.9b"
if [[ -f "$OFFICIAL_RUN_SPEC_DIR/run_spec.json" ]]; then
  sidecar_root="$OFFICIAL_RUN_SPEC_DIR"
else
  sidecar_root=""
fi
link_eee_pair "$src_aggregate" "$dst_dir" "$sidecar_root"
echo "  linked: official/mmlu/eleutherai/pythia-6.9b/$(basename "$src_aggregate")"
if [[ -n "$sidecar_root" ]]; then
  echo "  + run_spec.json sidecar from $sidecar_root"
else
  echo "  no run_spec.json sidecar found at $OFFICIAL_RUN_SPEC_DIR"
fi

# --- Local HELM-derived audits: also try to find run_spec.json next
# to the source HELM run dir so the planner picks up the same
# adapter_spec the public side has.
for exp in "${LOCAL_EXPS[@]}"; do
  echo
  echo "== linking local/$exp =="
  src_aggregate="$(find "$STORE_ROOT/eee/local/$exp" -path '*/eee_output/*' -name '*.json' ! -name '*_samples.jsonl' 2>/dev/null | sort | head -1)"
  if [[ -z "$src_aggregate" ]]; then
    echo "  FAIL: no aggregate JSON under $STORE_ROOT/eee/local/$exp" >&2
    exit 1
  fi
  # Locate the HELM run_spec.json. It lives under
  # $AUDIT_RESULTS_ROOT/<exp>/helm/helm_id_*/benchmark_output/runs/
  # <exp>/<scenario_slug>/run_spec.json — the EEE store keeps the
  # converted artifacts but not the source HELM run dir.
  sidecar_root="$(find "$RESULTS_ROOT/$exp" -maxdepth 8 -name 'run_spec.json' 2>/dev/null | head -1)"
  sidecar_root="$(dirname "${sidecar_root:-}")"
  if [[ -z "$sidecar_root" || ! -f "$sidecar_root/run_spec.json" ]]; then
    sidecar_root=""
  fi
  dst_dir="$OUT_TREE/local/$exp/mmlu/eleutherai/pythia-6.9b"
  link_eee_pair "$src_aggregate" "$dst_dir" "$sidecar_root"
  echo "  linked: local/$exp/mmlu/eleutherai/pythia-6.9b/$(basename "$src_aggregate")"
  if [[ -n "$sidecar_root" ]]; then
    echo "  + run_spec.json sidecar from $sidecar_root"
  else
    echo "  no run_spec.json sidecar found at $helm_run_dir (planner facts will collapse to unknown for this entry)"
  fi
done

# --- InspectAI side: deliberately link without any sidecar. This is
# the cross-harness contributor whose comparability the user wants to
# probe. The planner will see a same-model, same-benchmark-name
# logical key but no HELM-derived facts.
echo
echo "== linking local/inspectai =="
src_aggregate="$(find "$INSPECTAI_DIR" -maxdepth 2 -name '*.json' ! -name '*_samples.jsonl' 2>/dev/null | sort | head -1)"
if [[ -z "$src_aggregate" ]]; then
  echo "  FAIL: no aggregate JSON in $INSPECTAI_DIR" >&2
  exit 1
fi
dst_dir="$OUT_TREE/local/inspectai/mmlu/eleutherai/pythia-6.9b"
link_eee_pair "$src_aggregate" "$dst_dir" ""   # no sidecar by design
echo "  linked: local/inspectai/mmlu/eleutherai/pythia-6.9b/$(basename "$src_aggregate")"
echo "  (intentionally no run_spec.json — this is the cross-harness probe)"

echo
echo "Tree ready: $OUT_TREE"
echo "Next: ./20_run.sh"
