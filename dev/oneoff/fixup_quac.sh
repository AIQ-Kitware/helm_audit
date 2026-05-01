#!/usr/bin/env bash
# Re-convert public-store quac artifacts with the current every_eval_ever
# converter so the metric_id schema matches what the local audit produces.
# Then dedupe to remove the old aggregates left behind.
set -euo pipefail

PUBLIC_RUNS=/data/crfm-helm-public/classic/benchmark_output/runs
EEE_STORE=/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic

# Slim-paper scope: 3 models. Add more model_dir tokens here to widen.
MODEL_DIRS=(
    "eleutherai_pythia-6.9b"
    "lmsys_vicuna-7b-v1.3"
    "tiiuae_falcon-7b"
)

# v0.3.0 is where these models' quac runs live.
VERSION=v0.3.0

n_done=0
n_skipped=0
n_failed=0
for model_dir in "${MODEL_DIRS[@]}"; do
    # Find every quac run-spec dir for this model in the source HELM tree.
    # ``2>/dev/null`` swallows the no-match case so a missing model
    # (e.g. falcon-7b never had the public quac run if it lands later)
    # doesn't abort the whole loop.
    while IFS= read -r src; do
        [[ -z "$src" ]] && continue
        run_name=$(basename "$src")
        out_dir="$EEE_STORE/$VERSION/$run_name/eee_output"
        echo "[$((n_done + 1))] reconvert: $run_name"
        if every_eval_ever convert helm \
            --log_path "$src" \
            --output_dir "$out_dir" \
            --source_organization_name CRFM \
            --evaluator_relationship third_party \
            --eval_library_name HELM \
            --eval_library_version unknown \
            >/dev/null 2>&1; then
            n_done=$((n_done + 1))
        else
            echo "  FAIL — kept existing artifacts" >&2
            n_failed=$((n_failed + 1))
        fi
    done < <(find "$PUBLIC_RUNS/$VERSION" -maxdepth 1 -type d -name "quac:*model=${model_dir}*" 2>/dev/null)
done

echo
echo "Re-converted: $n_done | Failed: $n_failed"
echo

# Dedupe: each re-converted dir now has BOTH the old aggregate (from
# the original sweep) and the new aggregate (with current metric_id
# schema). The dedupe script keeps the newest by retrieved_timestamp
# and unlinks the rest, which is exactly what we want.
python3 dev/oneoff/dedupe_old_eee_conversions.py \
    --root "$EEE_STORE/$VERSION" \
    --all-suites \
    --apply
