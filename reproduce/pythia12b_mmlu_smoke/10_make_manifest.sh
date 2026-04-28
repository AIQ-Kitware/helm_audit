#!/usr/bin/env bash
# Generate the manifest YAML for the pythia-12b-v0 × MMLU run.
#
# Originally a 1-subject smoke (abstract_algebra) to exercise the dormant
# eval-audit-run / kwdagger / magnet / helm-run chain; that run succeeded on
# 2026-04-28 and reproduced the public HELM result exactly. The runbook now
# defaults to the full 5-subject set with public pythia-12b-v0 reference
# data, matching the pythia-mmlu-stress virtual-experiment scope:
#
#   abstract_algebra, college_chemistry, computer_security,
#   econometrics, us_foreign_policy
#
# HELM's compute_if_missing mode skips run-specs that already produced a
# DONE run (so re-invoking after the smoke completes the remaining 4
# subjects rather than re-running abstract_algebra).
#
# Override the subject list via HELM_MMLU_SUBJECTS as space-separated names.
# eval-audit-make-manifest helper isn't usable here because pythia-12b-v0
# was Stage-1-filtered out of $STORE_ROOT/configs/run_specs.yaml (size gate),
# so we write the manifest directly. Format matches
# eval_audit/manifests/models.py:ManifestSpec.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
EXP="${EXP_NAME:-audit-pythia-12b-mmlu-smoke}"
DEFAULT_SUBJECTS="abstract_algebra college_chemistry computer_security econometrics us_foreign_policy"
SUBJECTS="${HELM_MMLU_SUBJECTS:-$DEFAULT_SUBJECTS}"
MAX_EVAL_INSTANCES="${MAX_EVAL_INSTANCES:-1000}"
DEVICES="${DEVICES:-0,1,2,3}"
TMUX_WORKERS="${TMUX_WORKERS:-4}"

MANIFEST_FPATH="$STORE_ROOT/configs/manifests/$EXP.yaml"
mkdir -p "$(dirname "$MANIFEST_FPATH")"

# Render the run_entries block one line per subject. Don't emit the line
# for `abstract_algebra` from the smoke if the user passed only it — same
# treatment regardless; HELM dedups by run_spec_hash.
RUN_ENTRIES=""
SUBJECT_LIST=""
for s in $SUBJECTS; do
  RUN_ENTRIES+="  - mmlu:subject=$s,method=multiple_choice_joint,model=eleutherai/pythia-12b-v0,data_augmentation=canonical"$'\n'
  SUBJECT_LIST+="$s, "
done
SUBJECT_LIST="${SUBJECT_LIST%, }"

cat >"$MANIFEST_FPATH" <<EOF
schema_version: 1
experiment_name: $EXP
description: >-
  eleutherai/pythia-12b-v0 on MMLU subjects ($SUBJECT_LIST), via HELM's
  built-in huggingface/pythia-12b-v0 deployment (HuggingFaceClient,
  EleutherAI/gpt-neox-20b tokenizer, max_sequence_length 2048). Runs
  through eval-audit-run / kwdagger / magnet / helm-run on aiq-gpu.
run_entries:
${RUN_ENTRIES}suite: $EXP
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
enable_huggingface_models: []
enable_local_huggingface_models: []
EOF

echo "wrote: $MANIFEST_FPATH"
echo
cat "$MANIFEST_FPATH"
