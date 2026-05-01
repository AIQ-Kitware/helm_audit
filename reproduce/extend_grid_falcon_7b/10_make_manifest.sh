#!/usr/bin/env bash
# Generate the manifest YAML for the Falcon-7B heatmap-grid run.
#
# 40 run-specs across the heatmap's 14 benchmarks, exactly matching
# the public HELM v0.3.0 sweep for tiiuae/falcon-7b. The list was
# extracted from
#   /data/crfm-helm-public/classic/benchmark_output/runs/v0.3.0/
# so the local recipe matches the public recipe and the
# reproducibility comparison has no scenario-arg drift.
#
# HELM's huggingface/falcon-7b deployment is auto-resolved from the
# model alias; no enable_huggingface_models manifest entry is needed.
#
# Override knobs:
#   * EXP_NAME         — manifest experiment name (default
#                        audit-falcon-7b-helm-grid)
#   * HELM_RUN_ENTRIES — full override; one run-entry per line.
#                        If set, replaces the default list below.
#   * MAX_EVAL_INSTANCES, DEVICES, TMUX_WORKERS — pass-through.
#
# Writes: $AUDIT_STORE_ROOT/configs/manifests/$EXP_NAME.yaml
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
EXP="${EXP_NAME:-audit-falcon-7b-helm-grid}"

MAX_EVAL_INSTANCES="${MAX_EVAL_INSTANCES:-1000}"
DEVICES="${DEVICES:-0,1,2,3}"
TMUX_WORKERS="${TMUX_WORKERS:-4}"

# The 40 public-HELM v0.3.0 run-specs for tiiuae/falcon-7b across the
# heatmap's 14 benchmarks. Format matches HELM's run_entries (slash
# form for the model id; HELM rewrites to underscore form for the
# run-dir name on disk).
DEFAULT_RUN_ENTRIES=$(cat <<'RUNS'
boolq:model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=LGBTQ,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=all,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=black,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=christian,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=female,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=male,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=muslim,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=other_religions,model=tiiuae/falcon-7b,data_augmentation=canonical
civil_comments:demographic=white,model=tiiuae/falcon-7b,data_augmentation=canonical
entity_data_imputation:dataset=Buy,model=tiiuae/falcon-7b
entity_data_imputation:dataset=Restaurant,model=tiiuae/falcon-7b
entity_matching:dataset=Abt_Buy,model=tiiuae/falcon-7b
entity_matching:dataset=Beer,model=tiiuae/falcon-7b
entity_matching:dataset=Dirty_iTunes_Amazon,model=tiiuae/falcon-7b
gsm:model=tiiuae/falcon-7b
imdb:model=tiiuae/falcon-7b,data_augmentation=canonical
lsat_qa:task=all,method=multiple_choice_joint,model=tiiuae/falcon-7b
mmlu:subject=abstract_algebra,method=multiple_choice_joint,model=tiiuae/falcon-7b,data_augmentation=canonical
mmlu:subject=college_chemistry,method=multiple_choice_joint,model=tiiuae/falcon-7b,data_augmentation=canonical
mmlu:subject=computer_security,method=multiple_choice_joint,model=tiiuae/falcon-7b,data_augmentation=canonical
mmlu:subject=econometrics,method=multiple_choice_joint,model=tiiuae/falcon-7b,data_augmentation=canonical
mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=tiiuae/falcon-7b,data_augmentation=canonical
narrative_qa:model=tiiuae/falcon-7b,data_augmentation=canonical
quac:model=tiiuae/falcon-7b,data_augmentation=canonical
synthetic_reasoning:mode=induction,model=tiiuae/falcon-7b
synthetic_reasoning:mode=pattern_match,model=tiiuae/falcon-7b
synthetic_reasoning:mode=variable_substitution,model=tiiuae/falcon-7b
synthetic_reasoning_natural:difficulty=easy,model=tiiuae/falcon-7b
synthetic_reasoning_natural:difficulty=hard,model=tiiuae/falcon-7b
truthful_qa:task=mc_single,method=multiple_choice_joint,model=tiiuae/falcon-7b,data_augmentation=canonical
wikifact:k=5,subject=author,model=tiiuae/falcon-7b
wikifact:k=5,subject=currency,model=tiiuae/falcon-7b
wikifact:k=5,subject=discoverer_or_inventor,model=tiiuae/falcon-7b
wikifact:k=5,subject=instance_of,model=tiiuae/falcon-7b
wikifact:k=5,subject=medical_condition_treated,model=tiiuae/falcon-7b
wikifact:k=5,subject=part_of,model=tiiuae/falcon-7b
wikifact:k=5,subject=place_of_birth,model=tiiuae/falcon-7b
wikifact:k=5,subject=plaintiff,model=tiiuae/falcon-7b
wikifact:k=5,subject=position_held,model=tiiuae/falcon-7b
wikifact:k=5,subject=symptoms_and_signs,model=tiiuae/falcon-7b
RUNS
)

RUN_ENTRIES="${HELM_RUN_ENTRIES-$DEFAULT_RUN_ENTRIES}"

if [[ -z "$RUN_ENTRIES" ]]; then
  echo "FAIL: HELM_RUN_ENTRIES is empty (and the default list was overridden away)." >&2
  exit 1
fi

RUN_ENTRIES_BLOCK=""
while IFS= read -r entry; do
  [[ -z "$entry" ]] && continue
  RUN_ENTRIES_BLOCK+="  - ${entry}"$'\n'
done <<<"$RUN_ENTRIES"

n_entries=$(printf '%s' "$RUN_ENTRIES_BLOCK" | grep -c '^  - ' || true)
# `paste -sd, -` uses a single-char delimiter so the benchmark list
# joins as `a,b,c`. -d', ' (two chars) makes paste cycle delimiters,
# producing `a,b c,d e,f` — visually broken.
BENCHMARK_DESC=$(printf '%s' "$RUN_ENTRIES_BLOCK" | awk -F'[: ]' '/^  - / {print $4}' | sort -u | paste -sd, -)

MANIFEST_FPATH="$STORE_ROOT/configs/manifests/$EXP.yaml"
mkdir -p "$(dirname "$MANIFEST_FPATH")"

cat >"$MANIFEST_FPATH" <<EOF
schema_version: 1
experiment_name: $EXP
description: >-
  tiiuae/falcon-7b grid on benchmarks ($BENCHMARK_DESC) via HELM's
  HuggingFaceClient (fp16, in-process, max_sequence_length 2048).
  $n_entries run-specs matching public HELM Classic v0.3.0 for the
  EEE-only reproducibility heatmap grid extension. The
  enable_huggingface_models list below is required because upstream
  HELM only ships a together/falcon-7b deployment — without this,
  HELM auto-resolves model=tiiuae/falcon-7b to the Together API and
  the run dies with TogetherClientError. Pythia/Vicuna don't need
  this because HELM ships built-in huggingface/pythia-* deployments.
run_entries:
${RUN_ENTRIES_BLOCK}suite: $EXP
max_eval_instances: $MAX_EVAL_INSTANCES
mode: compute_if_missing
materialize: symlink
backend: tmux
devices: "$DEVICES"
tmux_workers: $TMUX_WORKERS
local_path: prod_env
precomputed_root: null
require_per_instance_stats: true
model_deployments_fpath: null
# Force HELM to register and use an in-process HuggingFaceClient
# deployment for tiiuae/falcon-7b (registered last → wins the
# "last non-deprecated deployment" rule in
# get_default_model_deployment_for_model). Without this, HELM
# auto-resolves to together/falcon-7b which requires togetherApiKey.
enable_huggingface_models:
  - tiiuae/falcon-7b
enable_local_huggingface_models: []
EOF

echo "wrote: $MANIFEST_FPATH ($n_entries run_entries across: $BENCHMARK_DESC)"
echo
head -20 "$MANIFEST_FPATH"
echo "  ... ($n_entries total run_entries)"
