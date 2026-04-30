#!/usr/bin/env bash
# Pre-cache the HuggingFace datasets HELM needs for the
# finish_qwen25_gptoss benchmarks. Run this once on aiq-gpu before
# 50_run_full.sh. Subsequent invocations are cheap (HF cache hits).
#
# Why this is a separate step: HELM scenarios load datasets via
# ``datasets.load_dataset`` at run time. If the host is offline (or
# the cache hasn't seen the dataset yet), the run crashes with
# ``FileNotFoundError: Couldn't find a dataset script at
# .../hendrycks/competition_math/competition_math.py`` mid-batch.
# Pre-caching surfaces the network problem before the audit starts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if ! command -v huggingface-cli >/dev/null 2>&1; then
  echo "FAIL: huggingface-cli not on PATH; install with 'pip install huggingface_hub[cli]'" >&2
  exit 1
fi

# HF dataset IDs the manifest's HELM scenarios will pull. Add to this
# list when you extend the manifest. Use ``|| true`` so a single
# missing repo (e.g. v1.14.0 safety datasets that may not be public
# yet) doesn't abort the rest of the warmup.
DATASETS=(
  # ``hendrycks/competition_math`` (HELM ``math:*`` run_entries) is
  # intentionally absent: the math benchmark is disabled in the
  # finish_qwen25_gptoss preset because the dataset is not reliably
  # reachable from aiq-gpu today. Re-add this line and the 7 math
  # run_entries in adapter.py if access is restored.
  # ``natural_questions`` (HELM ``natural_qa:*`` run_entries) is
  # disabled: HELM fetches NQ from a Google Storage URL that returns
  # HTTP 403 from aiq-gpu (observed 2026-04-30 against
  # qwen/qwen2.5-7b-instruct-turbo). Re-add this line and the 2
  # natural_qa run_entries in adapter.py if the access path is fixed.
  # (was: natural_questions)
  google/IFEval                   # ifeval (gpt-oss smoke)
  Idavidrein/gpqa                 # gpqa
  TIGER-Lab/MMLU-Pro              # mmlu_pro
  walledai/HarmBench              # harm_bench
  Anthropic/hh-rlhf               # anthropic_red_team (best-effort)
  walledai/SimpleSafetyTests      # simple_safety_tests
  natolambert/xstest-v2-copy      # xstest (varies by HELM version)
)

echo "== HF dataset warmup =="
for ds in "${DATASETS[@]}"; do
  echo "--- $ds ---"
  if huggingface-cli download "$ds" --repo-type dataset --quiet 2>&1 \
      | tail -3; then
    echo "  OK"
  else
    echo "  WARN: $ds not pre-cached; the corresponding HELM run-entry will fail at run time" >&2
  fi
done

echo
echo "Done. HF cache primed at: ${HF_HOME:-$HOME/.cache/huggingface}/datasets"
echo "Next: ./05_write_bundle.sh (or skip to ./30_run_smoke.sh if the bundle is already written)"
