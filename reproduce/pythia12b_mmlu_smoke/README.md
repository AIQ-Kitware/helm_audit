# pythia-12b-v0 × MMLU

Local audit run for `eleutherai/pythia-12b-v0` on the 5 MMLU subjects with
public HELM reference data, scheduled through the `eval-audit-run` →
`kwdagger` → `magnet` → `helm-run` execution chain on `aiq-gpu`. Results
fold into the `pythia-mmlu-stress` virtual experiment alongside the
existing `pythia-6.9b` runs.

The runbook directory still says "smoke" because it started life as a
1-subject smoke (`abstract_algebra`) on 2026-04-28 to verify the dormant
execution chain still worked. That run reproduced the public HELM result
exactly (`agree@0=1.000`, `max |Δ|=0.0`); the experiment is no longer a
smoke. The directory name is preserved so the git history of the runbook
stays continuous.

## Scope

- Model: `eleutherai/pythia-12b-v0`
- 5 MMLU subjects (the same set that has public reference data for both
  pythia-6.9b and pythia-12b-v0):
  - `abstract_algebra`
  - `college_chemistry`
  - `computer_security`
  - `econometrics`
  - `us_foreign_policy`
- Override via `HELM_MMLU_SUBJECTS=...` (space-separated). One subject
  is fine if you want to redo a single packet.
- Suite version target: matches public HELM v0.2.4 / v0.3.0 rows under
  `/data/crfm-helm-public/classic/benchmark_output/runs/v{0.2.4,0.3.0}/...`
- `max_eval_instances`: 1000 (matches the public reference; override via
  `MAX_EVAL_INSTANCES`)
- Re-running is idempotent: `mode: compute_if_missing` skips run-specs
  that already produced a `DONE` marker, so re-invoking after the
  abstract_algebra smoke completes the remaining 4 subjects rather than
  redoing the first one.

## Hardware assumptions (aiq-gpu)

- 4 GPUs with **≥ 28 GB free VRAM each** in parallel by default
  (`DEVICES=0,1,2,3`, `TMUX_WORKERS=4`). pythia-12b at fp16 is ~24 GB plus
  KV cache; 80 GB-class cards are comfortable. Override `DEVICES` and
  `TMUX_WORKERS` for fewer/different GPUs (24 GB cards are **not** enough
  — the model won't load).
- Disk: ~30 GB free under `$HOME/.cache/huggingface/` for the first download
  (shared across subjects after the first download).
- Network: outbound HTTPS to `huggingface.co` for the initial weight pull.
- Python: same `eval_audit` environment used here, with `crfm-helm` and
  `magnet` installed (`uv pip install -e .` in the repo root).

## Run order (on aiq-gpu)

```bash
cd /home/joncrall/code/helm_audit       # or wherever the repo lives on aiq-gpu

bash reproduce/pythia12b_mmlu_smoke/00_check_env.sh
bash reproduce/pythia12b_mmlu_smoke/10_make_manifest.sh
bash reproduce/pythia12b_mmlu_smoke/20_run.sh        # this is the long step
bash reproduce/pythia12b_mmlu_smoke/30_index_local.sh
```

Each script is short, set -euo, and prints what it's about to do. Step 20
is the only long one — expect minutes-to-hours depending on the GPU.

If `20_run.sh` fails because the dormant kwdagger path is broken, see the
**Fallback** section below for a direct `helm-run` invocation.

## Rsync results back

After `30_index_local.sh` finishes on aiq-gpu, the artifacts to rsync are:

```bash
# from the analysis machine (this one):
rsync --exclude scenarios --exclude cache -avPR \
  aiq-gpu:/data/./crfm-helm-audit/audit-pythia-12b-mmlu-smoke /data
rsync -avPR aiq-gpu:/data/./crfm-helm-audit-store/indexes /data
```

Path layout produced on aiq-gpu (and mirrored to this machine after rsync):

```
/data/crfm-helm-audit/audit-pythia-12b-mmlu-smoke/
└── helm/helm_id_<hash>/
    └── benchmark_output/runs/audit-pythia-12b-mmlu-smoke/
        ├── mmlu:subject=abstract_algebra,...,model=eleutherai_pythia-12b-v0,data_augmentation=canonical/
        ├── mmlu:subject=college_chemistry,...,model=eleutherai_pythia-12b-v0,data_augmentation=canonical/
        ├── mmlu:subject=computer_security,...,model=eleutherai_pythia-12b-v0,data_augmentation=canonical/
        ├── mmlu:subject=econometrics,...,model=eleutherai_pythia-12b-v0,data_augmentation=canonical/
        └── mmlu:subject=us_foreign_policy,...,model=eleutherai_pythia-12b-v0,data_augmentation=canonical/
            (each contains run_spec.json, per_instance_stats.json, stats.json, scenario_state.json, ...)
```

## Post-rsync steps (on the analysis machine)

After the rsync lands, the new runs still need to be folded into the
analysis surface. The `audit-pythia-12b-mmlu-smoke` experiment is already
listed in the pythia-mmlu-stress manifest's `include_experiments` (added
2026-04-28 for the first smoke), so only a reindex + recompose is needed:

1. Refresh the local audit-results index so the new packets are picked up:

   ```bash
   eval-audit-index \
     --results-root /data/crfm-helm-audit \
     --report-dpath /data/crfm-helm-audit-store/indexes
   ```

2. Recompose and rebuild the report:

   ```bash
   ./reproduce/pythia_mmlu_stress/compose.sh
   ./reproduce/pythia_mmlu_stress/build_summary.sh
   ```

   The pythia-12b-v0 per-model row should now show 10/10 reproduced
   (was 2/10 after the smoke), and the missing-targets CSV at
   [`reports/scoped_funnel/missing_targets.latest.csv`](../../../data/crfm-helm-audit-store/virtual-experiments/pythia-mmlu-stress/reports/scoped_funnel/missing_targets.latest.csv)
   should be empty.

## Fallback: direct `helm-run` (skip the eval-audit/kwdagger layer)

If `20_run.sh` fails — likely if the dormant kwdagger or magnet integration
has bit-rotted — bypass it and invoke `helm-run` directly. This won't write
the eval-audit experiment provenance, but it produces output at the same
HELM-shaped path so the indexer + analysis side still pick it up:

```bash
EXP=audit-pythia-12b-mmlu-smoke
SUBJECTS="${HELM_MMLU_SUBJECTS:-abstract_algebra college_chemistry computer_security econometrics us_foreign_policy}"
RUN_DIR=/data/crfm-helm-audit/$EXP/helm/helm_id_manual
mkdir -p "$RUN_DIR"
cd "$RUN_DIR"
RUN_ENTRIES=()
for s in $SUBJECTS; do
  RUN_ENTRIES+=("mmlu:subject=$s,method=multiple_choice_joint,model=eleutherai/pythia-12b-v0,data_augmentation=canonical")
done
helm-run \
  --run-entries "${RUN_ENTRIES[@]}" \
  --suite "$EXP" \
  --max-eval-instances "${MAX_EVAL_INSTANCES:-1000}" \
  --num-threads 1 \
  --local-path prod_env
```

(HELM's built-in `huggingface/pythia-12b-v0` deployment is auto-resolved
from the model alias; no `--enable-huggingface-models` needed.)

That command is canonical HELM usage; if it fails, the failure is in HELM /
transformers / GPU, not in eval-audit code.

## Result (2026-04-28 first run)

The runbook ran end-to-end on aiq-gpu (4× NVIDIA RTX PRO 6000 Blackwell,
~98 GB each) through the `eval-audit-run` → `kwdagger` → `magnet` →
`helm-run` chain, no manual intervention. Two `helm_id_*` directories
landed under `/data/crfm-helm-audit/audit-pythia-12b-mmlu-smoke/helm/`:
one successful (`helm_id_52r6fe0fb4po`, with `run_spec.json`,
`per_instance_stats.json`, `stats.json`, and a `DONE` marker) and one
truncated/incomplete attempt (`helm_id_nlq7z0by5704`).

After rsync + reindex + recompose against `pythia-mmlu-stress`:

- The 12b run matched both v0.2.4 and v0.3.0 official rows.
- abs_tol=0 agreement = **1.000** across 888 instances and all 8 metrics.
- max |Δ| = 0.0; the diagnosis label `deployment_drift` is the
  category-name for HF-vs-HF reproductions, not a signal of actual drift.

This is the same result as 6.9b on `abstract_algebra` — a clean
HuggingFaceClient-on-both-sides reproduction with no detectable
divergence.

**Implication for the wider docs:** the dormant execution path described
in the top-level README is alive — at least the basic
HuggingFaceClient + kwdagger + helm-run combination. The vLLM/KubeAI/
LiteLLM extensions and `eval-audit-make-manifest` remain unverified;
this run did not exercise those.
