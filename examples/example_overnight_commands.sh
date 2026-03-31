#!/usr/bin/env bash
set -euo pipefail

# Example overnight control on a richer generation-style task.

scripts/make_repeat_pair_manifests.sh \
  'narrative_qa:model=eleutherai/pythia-6.9b,data_augmentation=canonical' \
  audit-narrative-pythia \
  configs/generated \
  --max-eval-instances 1000 \
  --devices 0 \
  --tmux-workers 1

scripts/run_from_manifest.sh \
  configs/generated/audit-narrative-pythia_r1.yaml

scripts/run_from_manifest.sh \
  configs/generated/audit-narrative-pythia_r2.yaml

scripts/compare_batch.sh \
  configs/generated/audit-narrative-pythia_r1.yaml

scripts/compare_batch.sh \
  configs/generated/audit-narrative-pythia_r2.yaml
