# extend_grid_falcon_7b — local Falcon-7B reproduction

Local audit run for `tiiuae/falcon-7b` against the heatmap's 14
benchmarks at HELM Classic v0.3.0. Falcon-7B is fully open weights
(no HF gating), runs via HELM's built-in HuggingFace backend, and
fits on a single GPU at fp16 (~14 GB) — same execution path as the
existing Pythia-6.9B / Vicuna-7B-v1.3 locals.

This is the first model in the heatmap-grid extension. LLaMA-2-13B
was the original second target but is gated; once HF access is
granted the same recipe shape applies — just swap the model id and
run-specs (or split into a sibling `extend_grid_llama_2_13b/`).

## Scope

- Model: `tiiuae/falcon-7b` (base model, completions protocol)
- Suite version target: HELM Classic v0.3.0 (matches the existing
  Pythia-6.9B / Vicuna-7B locals, which are also at v0.3.0)
- 41 run-specs across the heatmap's 14 benchmarks:

  | benchmark | count |
  |---|---:|
  | boolq | 1 |
  | civil_comments (× 9 demographics) | 9 |
  | entity_data_imputation (Buy, Restaurant) | 2 |
  | entity_matching (Abt_Buy, Beer, Dirty_iTunes_Amazon) | 3 |
  | gsm | 1 |
  | imdb | 1 |
  | lsat_qa | 1 |
  | mmlu (× 5 subjects) | 5 |
  | narrative_qa | 1 |
  | quac | 1 |
  | synthetic_reasoning (× 3 modes) | 3 |
  | synthetic_reasoning_natural (easy, hard) | 2 |
  | truthful_qa (mc_single, multiple_choice_joint) | 1 |
  | wikifact (× 10 subjects, k=5) | 10 |
  | **total** | **41** |

  These are the exact run-spec strings the public HELM v0.3.0 sweep
  used (verified against
  `/data/crfm-helm-public/classic/benchmark_output/runs/v0.3.0/`),
  so the local recipe matches the public recipe — no scenario-arg
  drift to confound the reproducibility comparison.

- `max_eval_instances`: 1000 (matches public HELM v0.3.0; override
  via `MAX_EVAL_INSTANCES`)
- `mode: compute_if_missing` — re-running picks up where a partial
  run left off (DONE-marker skips).

## Hardware assumptions

- **GPU**: any single GPU with ≥ 16 GB free VRAM. Falcon-7B at fp16
  is ~14 GB plus KV cache; 24 GB-class cards (RTX 3090/4090, A4000)
  fit easily. With `DEVICES=0,1,2,3` and `TMUX_WORKERS=4` the 40
  run-specs are dispatched four at a time.
- **Disk**: ~30 GB free under `$HOME/.cache/huggingface/` for the
  Falcon weight pull (one-time; cached after).
- **Network**: outbound HTTPS to `huggingface.co` for the initial
  weight pull. Falcon-7B is not gated; no HF token required.
- **Python**: same `eval_audit` env as the Pythia recipe with
  `crfm-helm[all]` installed. `uv pip install 'crfm-helm[all]' -U`
  if missing.

## Run order

```bash
cd /home/joncrall/code/helm_audit       # or wherever the repo lives on the run host

bash reproduce/extend_grid_falcon_7b/00_check_env.sh
bash reproduce/extend_grid_falcon_7b/10_make_manifest.sh
bash reproduce/extend_grid_falcon_7b/20_run.sh        # the long step
bash reproduce/extend_grid_falcon_7b/30_index_local.sh
```

Each script is `set -euo` and prints what it's about to do. Step 20
is the only long one — expect a few hours on 4×24 GB cards depending
on benchmark size.

## Override knobs

- `EXP_NAME` — manifest experiment name (default
  `audit-falcon-7b-helm-grid`).
- `HELM_RUN_ENTRIES` — newline-separated full override of the
  default run-spec list. Useful for testing one benchmark at a time
  before launching the full grid.
- `MAX_EVAL_INSTANCES` — clamp on the public-HELM 1000.
- `DEVICES` / `TMUX_WORKERS` — GPU pinning + parallelism.
- `AUDIT_STORE_ROOT`, `AUDIT_RESULTS_ROOT` — paths.

## Rsync results back

After `30_index_local.sh` finishes on the run host:

```bash
# from the analysis machine:
rsync --exclude scenarios --exclude cache -avPR \
  <run-host>:/data/./crfm-helm-audit/audit-falcon-7b-helm-grid /data
rsync -avPR <run-host>:/data/./crfm-helm-audit-store/indexes /data
```

## Post-rsync — fold into the heatmap

After the rsync lands on the analysis machine:

1. The audit index is already refreshed via the rsync. If you ran
   anything else in the meantime, refresh with:

   ```bash
   eval-audit-index \
     --results-root /data/crfm-helm-audit \
     --report-dpath /data/crfm-helm-audit-store/indexes
   ```

2. Add `tiiuae/falcon-7b` to the heatmap-paper virtual experiment
   scope at
   [`configs/virtual-experiments/open-helm-models-reproducibility.yaml`](../../configs/virtual-experiments/open-helm-models-reproducibility.yaml)
   (extend the `models:` regex list), then recompose:

   ```bash
   ./reproduce/open_helm_models_reproducibility/compose.sh
   ./reproduce/open_helm_models_reproducibility/build_summary.sh
   ```

3. Re-render the heatmap (per-metric mode included):

   ```bash
   PER_METRIC=1 ./reproduce/eee_only_reproducibility_heatmap/30_heatmap.sh
   ```

   Falcon-7B should now appear as a 4th column in the main heatmap
   and contribute rows to each per-metric figure.

4. Also add Falcon-7B to the heatmap module's display tables
   ([`eval_audit/reports/eee_only_heatmap.py`](../../eval_audit/reports/eee_only_heatmap.py)
   `_MODEL_DISPLAY` / `_MODEL_ORDER`) so the column has a friendly
   label and a stable position. Without this it falls through to the
   raw model id and lands at the end of the column order — fine
   functionally, but ugly.

## Fallback: direct `helm-run`

If `20_run.sh` fails because the dormant `eval-audit-run` /
`kwdagger` chain has bit-rotted, bypass it. HELM's
`huggingface/falcon-7b` deployment is auto-resolved from the model
alias; no `--enable-huggingface-models` is needed.

```bash
EXP=audit-falcon-7b-helm-grid
RUN_DIR=/data/crfm-helm-audit/$EXP/helm/helm_id_manual
mkdir -p "$RUN_DIR"
cd "$RUN_DIR"
helm-run \
  --run-entries \
    "boolq:model=tiiuae/falcon-7b,data_augmentation=canonical" \
    "imdb:model=tiiuae/falcon-7b,data_augmentation=canonical" \
    # ... the full 40-entry list lives in 10_make_manifest.sh \
  --suite "$EXP" \
  --max-eval-instances "${MAX_EVAL_INSTANCES:-1000}" \
  --num-threads 1 \
  --local-path prod_env
```

That command is canonical HELM usage; if it fails, the failure is
in HELM / transformers / GPU, not in eval-audit code.
