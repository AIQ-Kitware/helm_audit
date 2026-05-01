# 2026-05-01 — heatmap-paper-slim session log

Companion to [`2026-04-30_eee_heatmap_session_log.md`](2026-04-30_eee_heatmap_session_log.md).
This session was the post-handoff push to (a) ship Falcon-7B as a 4th
model, (b) drive compose wall-clock down for paper iteration, (c)
audit + harden the EEE-only path against silent HELM fallthroughs
that would invalidate the paper claim.

---

## TL;DR

- **Model grid**: Pythia-6.9B + Vicuna-7B-v1.3 + Falcon-7B (3 models × 14 benchmarks)
  - Pythia-2.8B-v0 dropped (only 2/14 cells of public coverage)
  - Falcon-7B added; HF backend; runs locally via `reproduce/extend_grid_falcon_7b/`
- **Paper validity flags** all wired up as env vars (table below). Default behavior unchanged; set them to opt into the EEE-only/fast path.
- **Compose wall-clock** dropped from 525 s → expected ~150 s with the slim manifest + flags + ~16 separate optimizations.
- **`entity_matching` join_failed is a feature**, not a bug. Don't re-add sample_id fallback.
- **`quac` join_failed is a real bug**: converter-version drift in the public store. Hypothesis-confirmed fix queued (re-convert + dedupe).
- **EEE-only "hard split"** deferred per [`docs/eee-only-hard-split-todo.md`](../docs/eee-only-hard-split-todo.md). Today's flag-driven soft separation is good enough for the paper-pass; reviewers can audit the env vars.

---

## User preferences (durable, observed)

- **Reviewers will audit this code.** No nasty hacks even if convenient. If a hack is necessary as a stopgap, document it visibly (env var + code comment + TODO).
- **Always commit code, worst case is we undo it.** Be aggressive about commits, not stingy.
- **Hates "edit this file" workflows** — prefers programmatic config edits + idempotent re-runs. CLI/env-var beats sed-and-pray.
- **Wants concrete numbers** for any optimization claim — microbenchmarks, before/after, line-profiler output.
- **Wants split-up code lines** so `LINE_PROFILE=1` shows where time actually goes (e.g., `samples_path.read_bytes().split(b"\n")` collapsed to one line hides three operations).
- **Doesn't want to re-add reverted hacks** even if the reversion costs perf. The reversion was usually a deliberate signal preservation.
- **Modern `line_profiler` usage**: `from line_profiler import profile` + `@profile`, activates on `LINE_PROFILE=1` env var. **NOT** the old kernprof-CLI workflow. Output goes to `profile_output_<timestamp>.txt` automatically.
- **Prefers terse responses**, but values structured comparison tables and clear diagnostics.
- **`/loop` and other slash-commands** exist; user uses them.
- **Setup**: data store mounted via virtiofs. aivm-2404 has FD-limit issues (per CLAUDE.md) — never run heavy analysis there. Real runs go to toothbrush.

## Hardware / environment

| Host | Role | Notes |
|---|---|---|
| **toothbrush** | Real GPUs, real disk; canonical run host | Where the user runs `compose.sh`, `build_summary.sh`, the heatmap pipeline. |
| **aivm-2404** | Read-only inspection | Same `/data` via virtiofs. Known FD-limit issue; **never** run pytest/compose here. |
| **aiq-gpu** | 4×96 GB host for vLLM serving | Used by `finish_qwen25_gptoss/`, the LLaMA-2-70B scaffold targets it. |

- Python: `/home/joncrall/.local/uv/envs/uvpy3.13.2/bin/python` (3.13.2)
- Build backend: `uv_build` (switched from setuptools this session — editable install dropped from minutes to seconds; user added `[tool.uv] exclude-newer = "14 days"` to `pyproject.toml` for stable resolutions)
- HuggingFace credentials: present, but **LLaMA-2 gated repos not yet approved** (was the trigger for the Falcon-only pivot; LLaMA-2-7B/13B/70B remain blocked pending HF approval)

## Paper claim under test

> Case Study 3: *EEE's per-instance schema is sufficient for reproducibility analysis at multiple granularities.*

The numerical content of the heatmap (agree_ratio, per-metric breakdowns, tolerance sweeps) flows through `eval_audit.normalized.compare` operating on `NormalizedRun.instances` (EEE-derived). The auxiliary `diagnosis` field still went through `HelmRunDiff` until this session — gated now via env var. Hard split deferred per the TODO doc.

---

## Env-var stack (all introduced or confirmed this session)

Set in the shell before running compose / build_summary / the heatmap pipeline:

```bash
# Manifest selector for compose.sh and build_summary.sh
export MANIFEST_FPATH=$PWD/configs/virtual-experiments/heatmap-paper-slim.yaml

# EEE-only paper validity guards (default-OFF; opt in for paper run)
export EVAL_AUDIT_EEE_STRICT=1                # disables silent HELM fallback in EeeArtifactLoader
export EVAL_AUDIT_TRUST_EEE_SCHEMA=1          # skip pydantic in samples-loop hot path (~2.4x)
export EVAL_AUDIT_SKIP_HELM_DIAGNOSIS=1       # bypasses HelmRunDiff in _build_pair + pair_samples
export EVAL_AUDIT_SKIP_LOCAL_REPEAT=1         # skip replica-vs-replica pairs in the planner

# Render-side speedups
export EVAL_AUDIT_NO_PLOTS=1                  # skip matplotlib in core_metrics.main
export HELM_AUDIT_SKIP_PLOTLY=1               # skip Plotly/Chromium PNG export
export HELM_AUDIT_SKIP_STATIC_IMAGES=1        # skip sankey static image emission

# Profiling (optional)
export LINE_PROFILE=1                         # writes profile_output_<UTC>.txt at process exit
```

CLI flag equivalents (where applicable): `--skip-diagnosis`, `--no-plots`. Env vars take precedence over defaults; CLI flags override env vars.

## Pipeline

```bash
# 1. Compose the slim slice (fast; uses the slim manifest's tighter scope)
./reproduce/open_helm_models_reproducibility/compose.sh

# 2. Build the aggregate summary
./reproduce/open_helm_models_reproducibility/build_summary.sh

# 3. Heatmap render. OUT_ROOT keeps slim output away from the wider experiment's tree.
OUT_ROOT=/data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim \
  bash reproduce/eee_only_reproducibility_heatmap/10_link_tree.sh

OUT_ROOT=/data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim \
FAST_AGG_SUMMARY=1 \
  bash reproduce/eee_only_reproducibility_heatmap/20_run.sh

OUT_ROOT=/data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim \
PER_METRIC=1 \
  bash reproduce/eee_only_reproducibility_heatmap/30_heatmap.sh
```

Final outputs at `<OUT_ROOT>/heatmap/`:
- `reproducibility_heatmap.png` — 3 cols × 14 rows
- `reproducibility_heatmap_per_metric/<metric>.png` — one per metric (auto-trims rows)

---

## Slim manifest

[`configs/virtual-experiments/heatmap-paper-slim.yaml`](../configs/virtual-experiments/heatmap-paper-slim.yaml):

- `scope.models`: 3 regex (pythia-6.9b, vicuna-7b-v1.3, falcon-7b)
- `scope.benchmarks`: 14 literal names (incl. typo'd `sythetic_reasoning_natural`)
- **No `pre_filter`** — Stage-1 filter inventory is stale and vetoes Falcon with `no-hf-deployment`. Slim path doesn't need the funnel context.
- `output.root`: `/data/crfm-helm-audit-store/virtual-experiments/heatmap-paper-slim/`

The broader [`configs/virtual-experiments/open-helm-models-reproducibility.yaml`](../configs/virtual-experiments/open-helm-models-reproducibility.yaml) keeps the prefilter for its full Sankey funnel, but it under-counts Falcon for the same reason. Regenerating `analysis/filter_inventory.json` is a separate followup.

---

## Storage layout (memorize this)

### Public-store EEE
```
/data/crfm-helm-audit-store/crfm-helm-public-eee-test/
  classic/
    v0.2.4/  v0.2.3/  v0.3.0/  v0.4.0/
      <run_dir>/                # e.g. quac:model=eleutherai_pythia-6.9b,...
        eee_output/<bench>/<dev>/<model>/<uuid>.{json,_samples.jsonl}
        status.json
  mmlu/v1.13.0/  speech/v1.0.0/  ...
  results.jsonl                 # converter sweep manifest (~68k rows)
```

Re-running the converter on the same source HELM dir writes a NEW `<uuid>.json` next to the OLD one in the same `eee_output/<bench>/<dev>/<model>/` dir. Multiple aggregates per dir = converter-version drift. **Dedupe is correct here** (keep newest by `retrieved_timestamp`).

### Local audit EEE (compose-produced)
```
/data/crfm-helm-audit-store/eee/local/
  <experiment_name>/             # audit-falcon-7b-helm-grid/, open-helm-models-reproducibility/, ...
    <helm_id>/                   # unique per HELM-run invocation
      <run_slug>/                # e.g. boolq-model-eleutherai_pythia-6.9b-...
        eee_output/<bench>/<dev>/<model>/<uuid>.{json,_samples.jsonl}
        status.json
        provenance.json
        reproduce.sh
```

A genuine replica run produces a new `<helm_id>/<run_slug>/` parent dir. The dedupe script groups by parent dir → never collapses replicas. **Dedupe is safe here** (verified empirically: 10 boolq pythia-6.9b artifacts, 10 distinct parent dirs, 0 dedupe candidates).

### Heatmap link tree (built by `10_link_tree.sh`)
```
$OUT_ROOT/eee_artifacts/
  official/<bench>/<dev>/<model>/<uuid>.{json,_samples.jsonl}     # symlinks to public store
  local/open-helm-models-reproducibility/<bench>/<dev>/<model>/   # symlinks to local store (any source experiment)
```

The local symlink-tree dir name is hardcoded to `"open-helm-models-reproducibility"` for backward compat with `eval-audit-from-eee`'s reader, but the **source files come from any experiment under `eee/local/`** (Falcon's land under `audit-falcon-7b-helm-grid/`).

---

## Investigation findings

### entity_matching — pandas merge row-order is pandas-version-dependent

**Updated 2026-05-01 evening (post-compact).** The earlier framing of
this as "HELM `id*` namespace instability" was *narrow* — the symptom
is real but the underlying mechanism is more specific. Full evidence
chain in §"Additional investigation (post-compact)" below.

For SAME `sample_id=id2221` on official vs local Pythia-6.9B:
- official content: "panasonic silver dect 6.0 cordless telephone..."
- local content:    "samsung 19' black flat panel series 6 lcd hdtv..."

The id-namespace itself IS stable within each run — `np.random.seed(0)
+ np.random.choice` at [`runner.py:123`](../submodules/helm/src/helm/benchmark/runner.py#L123)
is deterministic. The divergence is upstream: `pd.merge` produces
different row orderings between pandas 2.0.x (HELM v0.3.0 era) and
pandas 2.2.x+ (current) on the byte-identical Abt-Buy CSVs. Same
recipe, same data, same seed, same code → different row binding to
each `id<i>`.

**Conclusion**: `sample_hash`-only join correctly rejects this. The
`join_failed` cell IS the paper's evidence that EEE's hash-based
identity catches a deep upstream reproducibility leak that HELM's
id-based comparison silently hides. **Do not re-add sample_id
fallback** (was reverted in `ae8c0ed`).

### quac — converter-version drift (real bug)

```
Old public-store (current):  4321 insts, single 'quac' metric per sample
New (re-converted):         86420 insts, per-bookkeeping-metric records (matches local)
overlap on (hash, metric): old=0/4321  new=82840/86420 (96%)
```

Public-store `quac:*` artifacts were converted long ago with an older `every_eval_ever`; local audits used the current converter. Schema-incompatible.

**Fix queued (not yet run)**: re-convert all `quac:*` for the slim-paper models, then dedupe. Script in §Open work below.

### synthetic_reasoning + sythetic_reasoning_natural

**Updated 2026-05-01 evening (post-compact).** The earlier "NOT
broken" framing was wrong about `sythetic_reasoning_natural`. Two
distinct issues there, only one of them at the data level:

1. **`sythetic_reasoning_natural` was missing from the metric
   classifier.** The benchmark's real metrics are `f1_set_match`,
   `iou_set_match`, `exact_set_match` — none of which were in
   `eval_audit/helm/metrics.py:CORE_PREFIXES`. `classify_metric`
   returned `('untracked', None)` for all three, the comparator's
   class filter dropped 100% of the rows, and the cell rendered as
   `join_failed` despite the 1000/1000 sample_hash overlap. Fixed in
   `36fc52e` by registering the prefixes.
2. **`sythetic_reasoning_natural × pythia-6.9b` is broken at the
   inference level**, but on the OFFICIAL side (Together-hosted),
   not ours. Full evidence in §"Additional investigation
   (post-compact)" below — the OFFICIAL Pythia run produced empty
   `completion.text` on every prompt due to `stop_sequences=['\n']`
   trimming a leading `\n` token that Together's hosted Pythia
   produced as its greedy first token. HELM published 0% accuracy
   for this cell; our HF-local re-run produces ~20% accuracy.

`synthetic_reasoning` (no `_natural`) has always been clean: its real
metrics (`exact_match`, `quasi_exact_match`, `prefix_exact_match`,
`quasi_prefix_exact_match`) were already in CORE_PREFIXES, and it
joins cleanly. Final post-fix heatmap shows it at 0.982 / 0.997 /
0.999 across the three models.

### Display-name typo (preserved)

`_BENCHMARK_DISPLAY` in `eee_only_heatmap.py` has the key `sythetic_reasoning_natural` (missing the second 'n' in "synthetic"). The on-disk run-dir uses correctly-spelled `synthetic_reasoning_natural`. The `10_link_tree.sh` ENTRIES tuple maps these explicitly. **Don't fix the typo** — would orphan existing data.

---

## Optimization log

Cumulative wall-clock impact on the heatmap-pipeline compose:

| Commit | Speedup | Note |
|---|---|---|
| `2ad620f` `loaders: EVAL_AUDIT_EEE_STRICT` | (paper validity) | Disables silent HELM fallback in `EeeArtifactLoader.load`. Without this, same EEE artifact + same code → different numbers depending on whether HELM run dir is also on disk. |
| `0403ac3` `core_metrics: --skip-diagnosis` | 57 s/packet | `diff.summary_dict(level=20).get('diagnosis', {})` was 61% of `_build_pair`. >99% of the work was discarded. |
| `8e582ea` `core_metrics: --no-plots` | 200+ s | matplotlib plots in core_metrics are NOT gated by `HELM_AUDIT_SKIP_PLOTLY`; `EVAL_AUDIT_NO_PLOTS=1` is the master switch. |
| `f8dcab8` `rebuild_core_report: gate pair_samples on SKIP_HELM_DIAGNOSIS` | 11 s | pair_samples uses HelmRunDiff too. |
| `a272362` `core_metrics: thread component_cache into runlevel-table writer` | 18 s | Was re-loading every component a 2nd time. |
| `9f38236` `_group_quantiles → np.quantile` | ~10× per call | Was sorting same vector 7 times for 6 percentiles. |
| `9f38236` `_official_sweep_results_by_run_path` drop `.resolve()` | 13 s → 0.76 s | 17× on first call (cached after). 130k+ stat() syscalls saved on the 68k-row results.jsonl walk. |
| `4e4e473` `_agreement_curve → np.searchsorted` | 23× per call (~10 s) | Was `sum(v <= t for v in vals)` per threshold. |
| `484c67e` `compare: cache classify_metric + drop redundant per-row call` | ~7 s | 2.7M calls in `instance_level_core_rows`; only ~50 distinct strings. |
| `d25cb15` + `2402c6a` + `157e1ff` `EeeArtifactLoader: orjson + trust mode + stream` | ~50% on samples loop | Layered: orjson, model_validate vs model_validate_json, trust-schema + InstanceRecord positional ctor + file-iter vs read+split. |
| `e7b7db4` `EVAL_AUDIT_SKIP_LOCAL_REPEAT` | ~80% of `_build_pair` calls | Pythia-6.9B mmlu had 15 components → 14 local_repeat pairs/cell. |
| `b01806f` `drop Pythia-2.8B column` | UI cleanup | 12 of 14 cells were unavoidably blank for that model. |
| `f7fdfba` + `3324f8b` `prefilter cleanup + stale-inventory deletion` | correctness | Stage-1 inventory's `no-hf-deployment` veto for Falcon was stale; pre_filter dropped from slim manifest; compose now actively unlinks stale `scoped_filter_inventory.json` when the manifest changes. |
| `afbcea5` `link-tree walks all eee/local/* experiment dirs` | correctness | Falcon's artifacts live under `audit-falcon-7b-helm-grid/`, not `open-helm-models-reproducibility/`. |
| `648cdc9` `rich_link no longer follows symlinks` | UX | "Write link 🔗:" URLs now point to symlink, not target. |
| `6b2e3e4` `profile-attribution: split compound lines` | profile readability | E.g. `setdefault().append()`, `to_markdown() + '\n'`, `read_bytes().split(b"\n")` all split into separate lines. |
| `1b2688d` `pyproject [tool.uv] exclude-newer` | reproducibility | 14-day window so fresh installs don't grab brand-new releases. |
| `72d880f` `pyproject: switch to uv_build` | seconds vs minutes | setuptools editable was the actual bottleneck; not virtiofs / not source-tree walking. |

**Profile baseline trajectory** (rough):
- 525 s — full compose, no flags
- ~207 s — slim manifest + skip-diagnosis + no-plots + skip-local-repeat
- expected ~150 s after pending optimizations + dedupe

The dominant residual is `EeeArtifactLoader.load` per-line work: even with trust-schema, 12.5M lines × ~10 µs/line = ~125 s on a full compose. Further optimization needs schema-level changes (e.g. msgpack instead of JSON, or pickle-cached NormalizedRuns). Not worth pursuing for paper-pass.

---

## Falcon-7B integration details

### Runbook
[`reproduce/extend_grid_falcon_7b/`](../reproduce/extend_grid_falcon_7b/) — HF-backend, single GPU, no vLLM. 41 run-specs across 14 heatmap benchmarks at HELM Classic v0.3.0.

### HELM deployment gotcha
HELM upstream ships only `together/falcon-7b` (Together API, requires `togetherApiKey`). It does **not** ship `huggingface/falcon-7b`. The manifest sets:

```yaml
enable_huggingface_models:
  - tiiuae/falcon-7b
```

…so `helm-run --enable-huggingface-models tiiuae/falcon-7b` registers an in-process `HuggingFaceClient` deployment at startup. The dynamic deployment is appended last to `ALL_MODEL_DEPLOYMENTS` and wins the "last non-deprecated deployment" rule in `get_default_model_deployment_for_model`. Without this flag, runs fail with `TogetherClientError: togetherApiKey not set`.

Pythia/Vicuna don't need this flag because HELM ships built-in `huggingface/pythia-*` / `huggingface/vicuna-*` deployments.

### LLaMA-2-70B vLLM scaffold (deferred)
[`reproduce/llama2_70b_helm_audit/README.md`](../reproduce/llama2_70b_helm_audit/README.md) documents the new `pythia-llama2-70b-mixed-4x96` profile (in `submodules/vllm_service/`). Drops gpt-oss-20b for the profile so LLaMA-2-70B can use tp=2 across GPUs 0+1 at fp16. **HF gated access for LLaMA-2 not yet granted**; runbook scaffold exists, full step scripts not written. User pivoted to Falcon-only for this session.

### LLaMA-2-13B + Falcon-7B candidates
LLaMA-2-13B same situation (gated). Other candidates with v0.3.0 public coverage and full 14-bench match if the user broadens later: meta_llama-7b, mosaicml_mpt-30b, stanford_alpaca-7b, together_redpajama-incite-base-7b, together_gpt-j-6b, together_gpt-neox-20b, tiiuae_falcon-7b-instruct, lmsys_vicuna-13b-v1.3, eleutherai_pythia-12b-v0.

### Pythia-12B partial runbook
[`reproduce/pythia12b_mmlu_smoke/`](../reproduce/pythia12b_mmlu_smoke/) is partially run (1 of 5 mmlu subjects + boolq + imdb + truthful_qa). Completing it would add ~4 cells of free coverage. Not blocking.

---

## Open work (immediate, in order)

### 1. Apply the public-store dedupe (session ended mid-decision)

User was reviewing dry-run: **25,652 files, 139 GB queued, 7,933 dirs with 2-4 aggregates each**. Confirmed safe (every deletion is a stale converter output; per-dir distribution is 3042×2-aggregate, 4889×3-aggregate, 2×4-aggregate). Run:

```bash
python3 dev/oneoff/dedupe_old_eee_conversions.py \
    --root /data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.3.0 \
    --all-suites \
    --apply
```

### 2. Re-convert quac for the slim scope

Hypothesis confirmed on one path (Pythia-6.9B v0.3.0): 0/4321 → 82840/86420. Run for all slim models:

```bash
PUBLIC_RUNS=/data/crfm-helm-public/classic/benchmark_output/runs
EEE_STORE=/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic
VERSION=v0.3.0
for model_dir in eleutherai_pythia-6.9b lmsys_vicuna-7b-v1.3 tiiuae_falcon-7b; do
    while IFS= read -r src; do
        [[ -z "$src" ]] && continue
        out_dir="$EEE_STORE/$VERSION/$(basename "$src")/eee_output"
        every_eval_ever convert helm \
            --log_path "$src" --output_dir "$out_dir" \
            --source_organization_name CRFM --evaluator_relationship third_party \
            --eval_library_name HELM --eval_library_version unknown
    done < <(find "$PUBLIC_RUNS/$VERSION" -maxdepth 1 -type d -name "quac:*model=${model_dir}*" 2>/dev/null)
done
# Re-dedupe to remove old aggregates that survive next to new ones
python3 dev/oneoff/dedupe_old_eee_conversions.py \
    --root "$EEE_STORE/$VERSION" --all-suites --apply
```

Not yet committed as a script — could land at `dev/oneoff/reconvert_quac_public.sh`.

### 3. Re-run the heatmap pipeline

After dedupe + quac fix, re-run from `10_link_tree.sh`. Quac cells should switch from `join_failed` → populated agreement_ratio. Entity-matching stays `join_failed` (paper's intended signal).

### 4. Verify final heatmap

```bash
ls /data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim/heatmap/
# Expected:
#   reproducibility_heatmap.png    (3 cols × 14 rows)
#   reproducibility_heatmap_per_metric/<metric>.png  (one per metric)
#   cell_data.json
#   cell_data_per_metric.json
#   reproducibility_heatmap.txt
#   reproducibility_heatmap_per_metric.txt
```

Falcon's column will have `1 artifact` per cell (no replicas yet) and use `official_vs_local` only.

### 5. Followups (not blocking the paper)

- **EEE-only hard split** per [`docs/eee-only-hard-split-todo.md`](../docs/eee-only-hard-split-todo.md) — short-term flags are auditable; long-term we want `eval_audit/eee_only/` namespace with verifiable zero `eval_audit.helm.*` imports.
- **Stage-1 filter inventory regeneration** — needed if the broader manifest is run again. Currently vetoes Falcon with `no-hf-deployment`.
- **Pythia-12B runbook completion** — would extend the heatmap with a 4th model (Pythia size sweep: 2.8B → 6.9B → 12B); single-GPU HF-backend; cheap.
- **LLaMA-2-13B / 70B** — pending HF gated repo approval. Scaffold ready.
- **Append per-metric findings** to this session log once final heatmap renders.

---

## Key file map

### Hot paths (load-bearing for the heatmap)
- [`eval_audit/normalized/loaders.py`](../eval_audit/normalized/loaders.py) — `EeeArtifactLoader.load` (the dominant cost; trust-schema + stream + positional ctor optimizations live here)
- [`eval_audit/normalized/compare.py`](../eval_audit/normalized/compare.py) — `instance_level_core_rows` / `run_level_core_rows` (the EEE-native measurement core)
- [`eval_audit/normalized/joins.py`](../eval_audit/normalized/joins.py) — `index_instances` / `join_instances` (sample_hash join; do NOT add sample_id fallback)
- [`eval_audit/normalized/helm_compat.py`](../eval_audit/normalized/helm_compat.py) — `HelmRunView` adapter (lets HelmRunDiff read NormalizedRun; deprecated path)
- [`eval_audit/normalized/eee_artifacts.py`](../eval_audit/normalized/eee_artifacts.py) — EEE artifact resolution + `_official_sweep_results_by_run_path` (cached, post-opt)
- [`eval_audit/reports/core_metrics.py`](../eval_audit/reports/core_metrics.py) — main report renderer + `main()` CLI; `_build_pair`, `_agreement_curve`, `_group_quantiles`, `_metric_quantiles`, `_write_comparison_runlevel_table`, plot family
- [`eval_audit/reports/eee_only_heatmap.py`](../eval_audit/reports/eee_only_heatmap.py) — heatmap PNG + per-metric subplots
- [`eval_audit/reports/pair_samples.py`](../eval_audit/reports/pair_samples.py) — per-pair text comparison (HelmRunDiff-shaped; gated)
- [`eval_audit/helm/diff.py`](../eval_audit/helm/diff.py) — `HelmRunDiff` (legacy diagnosis core; use is gated by env var)
- [`eval_audit/planning/core_report_planner.py`](../eval_audit/planning/core_report_planner.py) — packet planner; local_repeat generation gated here
- [`eval_audit/cli/build_virtual_experiment.py`](../eval_audit/cli/build_virtual_experiment.py) — compose entry; stale-inventory cleanup lives here
- [`eval_audit/workflows/build_reports_summary.py`](../eval_audit/workflows/build_reports_summary.py) — aggregate summary
- [`eval_audit/workflows/rebuild_core_report.py`](../eval_audit/workflows/rebuild_core_report.py) — per-packet renderer
- [`eval_audit/workflows/analyze_experiment.py`](../eval_audit/workflows/analyze_experiment.py) — packet analysis driver
- [`eval_audit/cli/from_eee.py`](../eval_audit/cli/from_eee.py) — EEE-only entry (the demo path; `eval-audit-from-eee` CLI)

### Configs / runbooks
- [`configs/virtual-experiments/heatmap-paper-slim.yaml`](../configs/virtual-experiments/heatmap-paper-slim.yaml) — paper-pass manifest
- [`configs/virtual-experiments/open-helm-models-reproducibility.yaml`](../configs/virtual-experiments/open-helm-models-reproducibility.yaml) — broader manifest (pre_filter still in)
- [`reproduce/eee_only_reproducibility_heatmap/`](../reproduce/eee_only_reproducibility_heatmap/) — `10_link_tree.sh` / `20_run.sh` / `30_heatmap.sh`
- [`reproduce/open_helm_models_reproducibility/`](../reproduce/open_helm_models_reproducibility/) — `compose.sh` / `build_summary.sh` (manifest-driven; honors `MANIFEST_FPATH`)
- [`reproduce/extend_grid_falcon_7b/`](../reproduce/extend_grid_falcon_7b/) — Falcon-7B HF backend runbook
- [`reproduce/llama2_70b_helm_audit/`](../reproduce/llama2_70b_helm_audit/) — LLaMA-2-70B vLLM scaffold (README only)
- [`reproduce/finish_qwen25_gptoss/`](../reproduce/finish_qwen25_gptoss/) — reference vLLM mixed-profile runbook
- [`reproduce/pythia12b_mmlu_smoke/`](../reproduce/pythia12b_mmlu_smoke/) — partial Pythia-12B grid

### Tooling
- [`dev/oneoff/dedupe_old_eee_conversions.py`](../dev/oneoff/dedupe_old_eee_conversions.py) — newest-by-timestamp dedupe; safe by design (groups by parent dir)
- [`dev/oneoff/package_eee_helm_official.py`](../dev/oneoff/package_eee_helm_official.py) — zip EEE artifacts for sharing (this session's; helm_mmlu/helm_classic/helm_air_bench)

### Submodules
- [`submodules/every_eval_ever/`](../submodules/every_eval_ever/) — EEE schema + converter
- [`submodules/aiq-magnet/`](../submodules/aiq-magnet/) — HELM run materialization
- [`submodules/helm/`](../submodules/helm/) — HELM benchmark code (read-only mirror)
- [`submodules/kwdagger/`](../submodules/kwdagger/) — scheduler
- [`submodules/vllm_service/`](../submodules/vllm_service/) — multi-model vLLM serving (touched this session: `helm-llama-2-13b/70b` profiles + `pythia-llama2-70b-mixed-4x96` co-resident profile)
- [`submodules/cmd_queue/`](../submodules/cmd_queue/) — cmd queue

### Docs
- [`CLAUDE.md`](../CLAUDE.md) — project conventions; aivm-2404 FD-limit warning
- [`docs/pipeline.md`](../docs/pipeline.md) — pipeline architecture
- [`docs/helm-reproduction-research-journal.md`](../docs/helm-reproduction-research-journal.md) — research framing
- [`docs/eee-vs-helm-metadata.md`](../docs/eee-vs-helm-metadata.md) — HELM↔EEE field mapping
- [`docs/eee-only-hard-split-todo.md`](../docs/eee-only-hard-split-todo.md) — deferred architectural plan from this session
- [`paper_draft/2026-04-30_eee_heatmap_session_log.md`](2026-04-30_eee_heatmap_session_log.md) — prior session's exhaustive log
- [`paper_draft/2026-05-01_session_log.md`](2026-05-01_session_log.md) — THIS doc

---

## Recent commit list (this session, head-first)

```
b01806f heatmap: drop Pythia-2.8B-v0 column
e7b7db4 core_report_planner: EVAL_AUDIT_SKIP_LOCAL_REPEAT=1 skips replica pairs
afbcea5 heatmap link-tree: walk every experiment dir under eee/local
3324f8b build_virtual_experiment: clean up stale scoped_filter_inventory.json
f7fdfba heatmap-paper-slim: drop stale Stage-1 prefilter so Falcon-7B can land
157e1ff EeeArtifactLoader: stream samples.jsonl + positional InstanceRecord ctor
6b2e3e4 profile-attribution: split compound lines + numpy-pure ratio in _agreement_curve
4e4e473 core_metrics: vectorize _agreement_curve with np.searchsorted
484c67e compare: cache classify_metric + drop redundant per-row call
9f38236 core_metrics + eee_artifacts: vectorize _group_quantiles, drop redundant Path.resolve in sweep index
648cdc9 logging: rich_link no longer follows symlinks
2402c6a EeeArtifactLoader: EVAL_AUDIT_TRUST_EEE_SCHEMA=1 fast path skips pydantic
69fec04 loaders: hoist per-call imports to module level
d25cb15 EeeArtifactLoader: orjson + model_validate on the samples.jsonl hot loop
a84682d loaders: @profile on EEE / HELM-raw load paths
3fc5e00 configs: heatmap-paper-slim virtual-experiment manifest
0403ac3 core_metrics: add --skip-diagnosis flag for EEE-only paper path
2ad620f loaders: EVAL_AUDIT_EEE_STRICT=1 disables silent HELM fallback in EeeArtifactLoader
8e582ea core_metrics: add --no-plots / EVAL_AUDIT_NO_PLOTS for fast iteration
1b2688d pyproject: pin [tool.uv] exclude-newer = 14 days for stable resolutions
72d880f pyproject: switch to uv_build + drop stale aivm-2404 notes
06a97f3 heatmap: add Falcon-7B column + LINE_PROFILE instrumentation
163a590 eee_only_heatmap: per-metric drill-down + rich_link/safer
6881dc5 llama2_70b_helm_audit: new vLLM profile + runbook scaffold
b2afd1e extend_grid_falcon_7b: pin manifest to HF backend, not Together
7ef3679 extend_grid_falcon_7b: HF-backend runbook for Falcon-7B v0.3.0 grid
bf26f18 docs: TODO for the EEE-only hard split (paper-validity follow-up)
bdf21ed oneoff: package_eee_helm_official.py — zip EEE artifacts to share
2089f2b (vllm_service submodule) profiles: add helm-llama-2-13b/70b + pythia-llama2-70b-mixed-4x96
```

---

## Additional investigation (post-compact, 2026-05-01 evening)

After /compact the user picked back up with: (a) build a runtime
audit of the EEE-only paper claim, (b) close the loop on
entity_matching with concrete evidence, (c) drill into the next
suspicious cells (SR-Natural × Pythia at 0.788, WikiFact at ~0.92).
The session produced three independent reproducibility-failure case
studies with full mechanistic explanations, plus a meta-finding about
EEE's audit/forensics scope boundary.

### Tooling shipped this session

| Commit | File(s) | Purpose |
|---|---|---|
| `c4746fd` | [`dev/oneoff/audit_eee_only_run.py`](../dev/oneoff/audit_eee_only_run.py) | Runtime proof of EEE-only file access. Wraps a command with `sys.addaudithook` via `sitecustomize.py` injection on `PYTHONPATH`, propagates through every child Python process, classifies file opens, emits PASS/FAIL verdict. |
| `36fc52e` | `eval_audit/helm/metrics.py`, `eval_audit/normalized/compare.py`, `eval_audit/reports/core_metrics.py`, `eval_audit/reports/eee_only_heatmap.py`, `tests/test_normalized_compare.py` | Add `f1_set_match` / `exact_set_match` / `iou_set_match` to `CORE_PREFIXES`; add `no_core_metrics` heatmap status (distinct from `join_failed`); plumb `n_joined_pairs` through compare → core_metrics → heatmap collector. |
| `77d90af` | `eval_audit/reports/eee_only_heatmap.py` | Propagate `n_joined_pairs` into `cell_data.json` (collector accumulated it but emitter dropped it). |
| `efa6cdf` | [`dev/oneoff/diagnose_entity_matching_join.py`](../dev/oneoff/diagnose_entity_matching_join.py) | Read-only diagnostic for the entity_matching official↔local hash divergence: tests Q1-Q5 (content overlap, id permutation, sample_hash function, fewshot stability, HELM metadata) plus cross-model official-side consistency. |
| `5fe2f83` | [`dev/oneoff/em_pandas_mwe.sh`](../dev/oneoff/em_pandas_mwe.sh), [`dev/oneoff/em_pandas_mwe_run.py`](../dev/oneoff/em_pandas_mwe_run.py) | Pure-pandas MWE: runs ONLY the merge sequence from `EntityMatchingScenario.read_blocked_pairs` against the deepmatcher Abt-Buy CSVs across 5 (pandas, numpy) version combos and diffs `full_order_digest`. |
| `541a116`, `ad3cd7f`, `93af14b`, `766146d`, `872b264` | [`dev/oneoff/em_helm_mwe.sh`](../dev/oneoff/em_helm_mwe.sh), [`dev/oneoff/em_helm_mwe_run.py`](../dev/oneoff/em_helm_mwe_run.py) | End-to-end HELM-in-the-loop MWE: runs `EntityMatchingScenario.get_instances → with_instance_ids → downsample_eval_instances` and diffs the resulting (rank, id, content_sig) sequence against captured `scenario_state.json` from real HELM runs. Builds two venvs (current vs `crfm-helm==0.3.0`) and reports a 4-cell verdict matrix. |

### Case study A — entity_matching: pandas merge row-order drift

**Symptom (slim heatmap)**: 0/~999 sample_hash overlap between
official and local across all 3 models, despite 1000/1000 sample_id
overlap.

**Mechanism (proven end-to-end)**: pandas `pd.merge(labels, tableA,
right_on='id', left_on='ltable_id')` produces different row orderings
between pandas 2.0.3 and pandas 2.2.3+ on the byte-identical Abt-Buy
CSVs, even though documentation suggests `how='inner', sort=False`
should be stable. HELM's downstream `with_instance_ids` then assigns
`id<i>` positionally over the merged list, so the same `id<i>` points
to a different row under different pandas versions.
[`runner.py:123`](../submodules/helm/src/helm/benchmark/runner.py#L123)
hardcodes `np.random.seed(0)` for the downsample, so both sides pick
the same indices into the (drifted) array — same id sequence, different
content.

**Evidence chain**:

1. *Pure-pandas MWE* (`5fe2f83`) on the deepmatcher Abt-Buy
   `valid.csv`: 5 (pandas, numpy) combos, numpy held fixed at 1.26.4
   for the middle three, deliberately split:
   - pandas 2.0.3 + numpy {1.23.5, 1.26.4} → digest `d449ad71...`
   - pandas 2.2.3+ × any numpy → digest `77d68109...`
   - **numpy version irrelevant; pandas 2.0→2.2 is the inflection.**
   First divergence at row 1 of the merged DataFrame.
2. *End-to-end HELM MWE* (`541a116` + `ad3cd7f` + `93af14b` +
   `766146d` + `872b264`): under pandas 2.3.3 + crfm-helm 0.5.x, the
   live `EntityMatchingScenario` chain reproduces the LOCAL captured
   `scenario_state.json` byte-for-byte (digest `f05853ba...`, 1000/1000
   sigs). Under pandas 2.0.3 + crfm-helm 0.3.0 (Python 3.10 venv —
   v0.3.0 set `python_requires=>=3.8,<3.11`, `pyext` deps fail on
   3.11+), the live chain reproduces the OFFICIAL v0.3.0
   `scenario_state.json` byte-for-byte (digest `5fb72964...`, 1000/1000
   sigs).
3. Verdict matrix (4 cells):

   | | LOCAL `scenario_state` | OFFICIAL v0.3.0 `scenario_state` |
   |---|---|---|
   | current venv (pandas 2.3.3) | EXACT MATCH ✅ 1000/1000 | mismatch (0/1000 sigs, 1000/1000 ids — same id, different content) |
   | v0.3.0 venv (pandas 2.0.3) | mismatch | EXACT MATCH ✅ 1000/1000 |

**Note for Yifan / HELM upstream**: this is independent of HELM PR
#1475 (the numpy random-state fix from 2023-04-19). Both v0.3.0 and
v0.5.x call `set_fixed_random_state_for_dataset`. The reproducibility
leak is the implicit pandas-version dependence at
[`entity_matching_scenario.py:99-100`](../submodules/helm/src/helm/benchmark/scenarios/entity_matching_scenario.py#L99-L100).
Same recipe + same data + same seed + same code → different sample
under different pandas. Three independent leaks not addressed by PR
#1475:

- The pandas merge ordering above.
- `ensure_file_downloaded` at [`common/general.py:80`](../submodules/helm/src/helm/common/general.py#L80)
  doesn't checksum (comment: `"Assume it's all good"` if path exists).
- `with_instance_ids` at [`scenario.py:284`](../submodules/helm/src/helm/benchmark/scenarios/scenario.py#L284)
  binds `id<i>` to scenario-emit position, so any upstream ordering
  instability silently rebinds id↔content.

### Case study B — SR-Natural × Pythia-6.9B (0.788): broken OFFICIAL inference

**Symptom**: heatmap shows 0.788 instance-level agree_ratio, lowest
non-`join_failed` cell. Pythia-only outlier — Vicuna and Falcon are at
0.998 / 0.997 on the same benchmark.

**Mechanism**: OFFICIAL Pythia-6.9B at HELM Classic v0.3.0 was
inferenced via Together.ai's hosted API (HELM v0.3.0
[`auto_client.py:184-200`](../submodules/helm/src/helm/proxy/clients/auto_client.py)
routes any `eleutherai/*`, `lmsys/*`, `tiiuae/*` etc. with
`model_deployment=None` to `TogetherClient`). On Together's hosted
Pythia at temperature=0, the greedy first token after a prompt ending
in `:` is `\n` (Ċ). HELM's `stop_sequences=['\n']` trims
`completion.text` at the first `\n` → the actual answer ("The dog is
sad.", tokens 2-6 of every 20-token completion) is *discarded*. Result:
**every (sample, metric) pair on the OFFICIAL side scores 0.0**.

Our LOCAL reproduction with `model_deployment="huggingface/pythia-6.9b"`
uses HuggingFace transformers locally; greedy first token is `'The'`
not `\n`; completion is 5 tokens of real answer; ~20% accuracy.

**Evidence (4-layer cross-check)**:

| Layer | What was checked | Result |
|---|---|---|
| HELM `per_instance_stats.json` (HELM-computed at run time) | Per-(instance, metric) score | 1000/1000 score=0.0 for f1/iou/exact_set_match |
| HELM `scenario_state.json` (raw HELM output) | `completion.text` per request_state | 1000/1000 empty string, all with `n_tokens=20` (= max_tokens, model never hit EOS) |
| HELM `scenario_state.json` `completion.tokens` | First 8 tokens for sample | `['Ċ', 'The', 'Ġdog', 'Ġis', 'Ġsad', '.', 'Ċ', 'Ċ']` — real answer present, just trimmed by stop_sequence |
| EEE `answer_attribution.extracted_value` | What EEE recorded | 20000/20000 records `extracted_value=""` — EEE faithfully mirrors HELM |

The OFFICIAL CRFM-published Pythia-6.9B score on SR-Natural-easy is
**0% accuracy** — that's an artifact of (Together's first-token
distribution) × (HELM's stop_sequences trim), not a measurement of the
model's capability. Our local re-run lifts it to ~20% and that
appears to be the model's actual performance on the task.

**The 0.788 understates the gap.** Three independent reasons:

1. *Per-metric averaging*: 0.788 is over 3 metrics × 1000 instances.
   Per-metric: exact_set_match=0.819, f1_set_match=0.772,
   iou_set_match=0.772. Tight cluster, so this isn't dominant.
2. *Degenerate-zero agreement*: with OFFICIAL=0.0 on every row, every
   "agreement" is a 0=0 collision. The 0.788 reads as "fraction of
   prompts where LOCAL also failed", not "fraction of model
   behaviors that agree".
3. *Aggregate metric scores*: from `stats.json`, official means are
   0.000/0.000/0.000 for the three set_match metrics; local means
   are 0.215/0.208/0.185. The leaderboard-cited numbers differ by
   the entire ~20% accuracy of the model. The instance-level 0.788
   does not capture this.

### Case study C — WikiFact × all 3 (~0.92): stochastic noise floor

**Symptom**: across all three models, WikiFact agrees at 0.920–0.927,
substantially below the 0.99+ that other present cells achieve.

**Mechanism**: recipe (verified bytewise on both sides):
`temperature=1.0, num_outputs=5`. Stochastic sampling **by design** —
HELM uses temperature=1 to draw 5 diverse outputs for the
`exact_match@5` family. Two independent runs of the same recipe will
not produce identical outputs by construction. For binary metrics
with hit rate `p`, agreement converges to `p² + (1-p)²` ≈ 0.85–0.95
across realistic `p`. Our observed 0.92 sits squarely in that range.

**Note on the 1-vs-5 output appearance**: OFFICIAL EEE has
`output.raw=["a"]` (1 element); LOCAL EEE has
`output.raw=["a","b","c","d","e"]` (5 elements). This is a *converter
cosmetic*, not a HELM run difference: HELM computed the @5 metrics
correctly using all 5 outputs at run time; only EEE's display field
differs because the OFFICIAL conversion (older `every_eval_ever`)
truncated `output.raw` to 1 element while the current converter keeps
all of them. The per-metric `evaluation.score` values are computed by
HELM independently and reflect the full 5-output computation on both
sides.

### Meta-finding — EEE audit/forensics scope boundary

EEE is **sufficient for audit** (do two runs of the same recipe agree?
where do they diverge? by how much? on which instances?) but
**insufficient for forensics** (why do they diverge? what backend
served the inference? what library versions were active?). All three
case studies above were *detected* from EEE alone (sample_hash overlap
counts; per-metric agree_ratios; aggregate score gaps) but their
*mechanisms* required walking back to HELM's `run_spec.json`,
`scenario_state.json`, and the framework source code.

This is a deliberate scope decision, not an oversight — captured as a
new section "Audit vs forensics: scope distinction" in
[`docs/eee-vs-helm-metadata.md`](../docs/eee-vs-helm-metadata.md).
The doc also adds caveats for two micro-averaged-`agree_ratio` failure
modes surfaced by the case studies: **degenerate-zero agreement**
(case B) and **stochastic noise floor** (case C).

### Final post-fix slim heatmap (3 × 14)

After `36fc52e` (metric classifier) + `77d90af` (n_joined_pairs
emitter) + the user's quac re-conversion + dedupe, the heatmap stands
at **39 present / 3 join_failed / 0 no_core_metrics / 0 missing** of
42 cells. The 3 `join_failed` are the entity_matching row across all
3 models (Case study A). All other 39 cells render with real
agree_ratio numbers. Falcon-7B was newly added this session via the
[`reproduce/extend_grid_falcon_7b/`](../reproduce/extend_grid_falcon_7b/)
runbook.

The full heatmap re-run was wrapped under the
`audit_eee_only_run.py` audit hook with
`EVAL_AUDIT_EEE_STRICT=1` (the `--require-strict-flag` gate). Verdict:
**PASS — 0 HELM-shaped opens across 88 child Python processes,
205,798 total file opens, 6,902 unique paths.** Report at
`/data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim/audit/audit_eee_only_run.report.json`.

### Open paper-draft to-dos surfaced this session

- Update Case Study 3 prose to lead with the three-failure-mode
  framing (pandas merge / Together stop_sequence / stochastic noise)
  rather than a generic "EEE schema is sufficient" claim.
- Decide whether to file an upstream HELM issue for the SR-Natural
  Pythia stop_sequence trim (the published 0% Pythia score is a real
  artifact of inference backend × text post-processing). Independent
  of the pandas-merge issue.
- The micro-averaged `agree_ratio` caveats deserve a paper-side
  surfacing: per-cell (instance-level agree_ratio, run-level mean off,
  run-level mean loc) is honester than `agree_ratio` alone. Worth
  adding to the heatmap text-table or a supplementary panel.

---

## Don'ts (anti-patterns we explicitly rejected)

- **Don't re-add the `(sample_id, sample_hash)` dual-key join** in `eval_audit/normalized/joins.py` (was reverted in `ae8c0ed`). The single-key sample_hash is correct; entity_matching's `join_failed` is the paper signal.
- **Don't reintroduce the repair loop** in `build_reports_summary._repair_prioritized_example_reports` (was 98% of wall-clock; removed in `f6b22e8`).
- **Don't add tolerance ladder** to the heatmap. User declined: "We have graphs showing the fraction is fairly stable at a given threshold."
- **Don't run pytest in parallel on aivm-2404.** Serial only.
- **Don't run heavy stuff on aivm-2404.** Even `uv pip install -e .` should be done on toothbrush. The /data store is virtiofs-shared so files are visible from both — one host for compute, the other for read-only inspection.
- **Don't fix the `sythetic_reasoning_natural` typo** in `_BENCHMARK_DISPLAY` — would orphan existing data.
- **Don't strip trailing newline before `orjson.loads`.** orjson tolerates it; the strip costs more than the parse.
- **Don't follow symlinks in `rich_link`.** "Write link 🔗:" should land on the symlink itself.
- **Don't use `--no-deps --no-build-isolation` for `uv pip install -e .`** — irrelevant after the `uv_build` switch.
- **Don't trust HELM `id*` sample_ids as a stable namespace.** They're sequential per-conversion.
