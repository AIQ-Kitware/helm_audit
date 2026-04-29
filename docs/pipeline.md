# eval_audit pipeline

This document describes the **active** pipeline: how the
[analysis-only runbooks](../reproduce/pythia_mmlu_stress/) and
[`open_helm_models_reproducibility`](../reproduce/open_helm_models_reproducibility/)
actually run end-to-end, top to bottom.

The pre-EEE-refactor pipeline (with execution stages: manifest building,
`kwdagger` scheduling, vLLM/KubeAI serving) is preserved as
[`historical/pipeline-pre-eee-refactor.md`](historical/pipeline-pre-eee-refactor.md).
That older flow has not been exercised in months and is **not** described here.

## Mental model

```
Public HELM corpus              Local audit results
(/data/crfm-helm-public)        (/data/crfm-helm-audit/<exp>/...)
        │                                  │
        │   already executed elsewhere     │
        ▼                                  ▼
        ┌──────────────────────────────────┐
        │  1. EEE conversion               │  every_eval_ever convert helm
        │     (per-run, on demand)         │  → eee_output/<dataset>/<dev>/<model>/<uuid>.json
        └──────────────────────────────────┘
                          │
                          ▼
        ┌──────────────────────────────────┐
        │  2. Virtual experiment compose   │  eval-audit-build-virtual-experiment
        │     (YAML-declared slice)        │  → coverage funnel, packet manifest
        └──────────────────────────────────┘
                          │
                          ▼
        ┌──────────────────────────────────┐
        │  3. Per-packet core analysis     │  eval-audit-analyze-experiment / -many
        │     (planner + core metrics)     │  → core_report/<packet>/...
        └──────────────────────────────────┘
                          │
                          ▼
        ┌──────────────────────────────────┐
        │  4. Aggregate / publication      │  eval-audit-build-summary
        │     (sankeys, prioritized,       │  → virtual-experiments/<name>/reports/...
        │      coverage matrix, README)    │
        └──────────────────────────────────┘
```

No model is run; no benchmark is downloaded. The pipeline is read-only over
the audit results that already exist on disk.

### Tutorial path: `eval-audit-from-eee`

If you already have *both* sides of the comparison in EEE format (one
directory of "official" EEE artifacts and one directory of "local" EEE
artifacts that you'd like to compare against them), you can skip Stages 1
and 2 entirely and run

```
eval-audit-from-eee \
    --eee-root <root>/{official,local}/... \
    --out-dpath <out> \
    --build-aggregate-summary
```

This walks the artifact tree, synthesizes the same in-memory index rows
the official and local indexes would have produced, runs the planner +
core-metrics + aggregate summary, and writes per-packet reports +
cross-packet summary under `<out>/`. See
[`reproduce/eee_only_demo/README.md`](../reproduce/eee_only_demo/README.md)
for a worked tutorial against a checked-in 3×3 fixture, including the
expected agreement-bucket counts. Comparability facts that the
HELM-shaped pipeline derives from `run_spec.json` (scenario class,
deployment, instructions, max_eval_instances) collapse to `unknown` for
EEE-only inputs and surface as `comparability_unknown:*` warnings.

## Stage 1 — EEE conversion

The canonical comparison input is the EEE artifact format (`every_eval_ever`,
under [`submodules/every_eval_ever/`](../submodules/every_eval_ever/)). Both
public HELM runs and local audit runs get converted into the same shape.

**Conversion code:** [`eval_audit/normalized/eee_artifacts.py`](../eval_audit/normalized/eee_artifacts.py)
calls `every_eval_ever.converters.helm.adapter.HELMAdapter`.

**Output:**

```
$AUDIT_STORE_ROOT/eee/local/<experiment>/<helm_id>/<run-slug>/
├── eee_output/<dataset>/<developer>/<model>/<uuid>.json   # one per evaluation log
├── status.json
├── provenance.json
└── reproduce.sh
```

For public HELM runs, the equivalent tree lives under
`$AUDIT_STORE_ROOT/crfm-helm-public-eee-test/<suite>/<version>/<run>/eee_output/`.
That sweep is driven by [`dev/poc/eee-audit/sweep.py`](../dev/poc/eee-audit/sweep.py)
— it converts ~36k public runs and is the slow upstream step. **UNSURE**: the
exact set of suites/versions currently in scope and how often this is rerun;
the script's own header documents the latest invocation it knows about.

EEE artifacts carry `source_organization_name=eval_audit_local` for local
runs (renamed from `helm_audit_local` on 2026-04-28; see
[`dev/oneoff/migrate_eee_source_org_tag.py`](../dev/oneoff/migrate_eee_source_org_tag.py)
to backfill old artifacts).

## Stage 2 — Virtual experiment compose

A virtual experiment is a YAML-declared *slice* over the existing audit data.

**Manifest:** `configs/virtual-experiments/<name>.yaml`. Two checked-in
examples:

- [`pythia-mmlu-stress.yaml`](../configs/virtual-experiments/pythia-mmlu-stress.yaml)
- [`open-helm-models-reproducibility.yaml`](../configs/virtual-experiments/open-helm-models-reproducibility.yaml)

A manifest declares:

- `sources` — which official-public-index rows and local-audit-index rows are
  in scope (by model glob, benchmark glob, etc.). Sources can include a
  `pre_filter` block referencing the Stage-1 filter inventory so the Sankey
  shows the funnel from the universe of all HELM runs down to the manifest
  scope.
- `scope` — `MultiPattern` filters applied to those sources.
- Provenance metadata for the publication surface.

**CLI:** `eval-audit-build-virtual-experiment <manifest>`.

**What it does:** loads the official + local indexes, applies the manifest
scope, computes the three-level coverage funnel
([`eval_audit/virtual/coverage.py`](../eval_audit/virtual/coverage.py)):

| level | meaning |
|---|---|
| logical | same scenario + model + augmentation |
| recipe-canonical | + same scenario_spec, prompt, decoding, max_train_instances after schema-collapsing the run_spec |
| recipe-identical | byte-for-byte `run_spec_hash` match |

Why three levels: HELM's run_spec schema evolves between releases, so the
raw `run_spec_hash` produces 0 matches even when the recipe is semantically
identical. The canonical-recipe hash (in `_canonical_recipe_hash`) collapses
known schema-evolution fields (`chain_of_thought_prefix`, `global_suffix`,
`num_trials`, `model_deployment`, etc.) before hashing. See
[`docs/helm-gotchas.md`](helm-gotchas.md) §G1.

**Output:** writes a packet manifest plus coverage artifacts to
`$AUDIT_STORE_ROOT/virtual-experiments/<name>/`.

## Stage 3 — Per-packet core analysis

A "packet" is one local-run / public-row pairing being compared. The packet
planner ([`eval_audit/planning/core_report_planner.py`](../eval_audit/planning/core_report_planner.py))
turns the virtual-experiment compose output into individual analysis jobs.

**CLI:** `eval-audit-analyze-experiment` for a single experiment;
`eval-audit-analyze-many` to batch across experiments.

**What it does:** for each packet, loads both sides via the normalized
loader ([`eval_audit/normalized/loaders.py`](../eval_audit/normalized/loaders.py)),
runs the comparison
([`eval_audit/normalized/compare.py`](../eval_audit/normalized/compare.py)),
emits a per-packet core-metric report
([`eval_audit/reports/core_metrics.py`](../eval_audit/reports/core_metrics.py))
including per-instance ECDFs, agreement curves, comparability facts, and a
diagnosis (`deployment_drift`, `execution_spec_drift`,
`completion_content_drift`, `multiple_primary_reasons`, etc.).

**Output:**

```
$AUDIT_STORE_ROOT/analysis/core-reports/<packet-slug>/
├── components_manifest.latest.json
├── core_metric_management_summary.latest.txt
├── core_metric_ecdfs.latest.png         # per-metric agreement ECDF
├── *.latest.json                         # comparability facts, etc.
└── .history/                             # stamped past runs
```

## Stage 4 — Aggregate / publication

**CLI:** `eval-audit-build-summary` (with `--analysis-root` and
`--no-filter-inventory` flags exposed for virtual-experiment scope).

**What it does** (see
[`eval_audit/workflows/build_reports_summary.py`](../eval_audit/workflows/build_reports_summary.py)):

1. **Sankey A — Universe → Scope:** how the 13k+ universe of discovered HELM
   runs narrows to the manifest's in-scope rows. Stages: structural gate,
   metadata gate, open-weight gate, tag gate, deployment gate, size gate,
   manifest scope.
2. **Sankey B — Scope → Reproduced → Analyzed:** how in-scope rows funnel
   to logical match → recipe-canonical match → analyzed packet → agreement
   bucket.
3. **Coverage funnel summary:** the three-level table from Stage 2,
   formatted as `coverage_funnel_summary.latest.txt`.
4. **Prioritized examples:** quantile-bucketed example packets
   (`score_ge_95`, `best`, `mid`, `worst`, `score_lt_80`, `flagged`).
5. **Aggregate README:** narrative report combining the above.

**Output:**

```
$AUDIT_STORE_ROOT/virtual-experiments/<name>/
├── manifest.yaml
├── provenance.json
├── scoped_filter_inventory.json
├── reports/
│   ├── aggregate-summary/all-results/
│   │   ├── README.latest.txt
│   │   ├── sankey_a_universe_to_scope.latest.html
│   │   ├── sankey_b_scope_to_analyzed.latest.html
│   │   └── prioritized_examples.latest/{score_ge_95,best,mid,worst,score_lt_80,flagged}/
│   └── scoped_funnel/
│       ├── coverage_funnel_summary.latest.txt
│       └── missing_targets.latest.csv
└── REPRODUCIBILITY_REPORT.md           # hand-written narrative
```

## Filesystem-as-interface

`*.latest.<ext>` are symlinks to the most recent stamped run; the stamps
live under `.history/`. Many directories also carry a `reproduce.latest.sh`
that re-runs the computation that produced that directory. ADRs 4 ("the
filesystem is part of the interface") and 5 ("every meaningful generated
output gets a reproduce script") in
[`ARCHITECTURE.md`](../ARCHITECTURE.md#appendix-architecture-decision-records)
describe the convention.

## Indexing (used by Stages 2–4)

Both Stage 2 and Stage 4 read from two indexes:

- **Local audit index:** `eval-audit-index` builds the audit-results index
  CSV/JSONL at `$AUDIT_STORE_ROOT/indexes/audit_results_index_<timestamp>.{csv,jsonl,txt}`.
  Re-run before composing if new audit runs have appeared on disk.
- **Official public index:** built by [`eval_audit/workflows/analyze_official_index.py`](../eval_audit/workflows/analyze_official_index.py)
  from the public HELM corpus mirror at `/data/crfm-helm-public/`. **UNSURE**:
  exact regeneration cadence; check `$AUDIT_STORE_ROOT/indexes/official_public_index*` modification times.

## What this pipeline does *not* cover

- Building execution manifests from scratch (`eval-audit-make-manifest`).
- Scheduling local HELM runs (`eval-audit-run`, `kwdagger`).
- Standing up vLLM / KubeAI / LiteLLM serving for those runs.
- Refreshing the public-HELM mirror at `/data/crfm-helm-public/`.

Those flows existed and may still work, but none have been re-validated
recently. Their last-known-good runbooks are in
[`reproduce/`](../reproduce/) under `apples/`, `historic_grid/`,
`smoke/`, `qwen2_72b_vllm/`, `qwen35_vllm/`, `gpt_oss_20b_vllm/`, and
`small_models_kubeai/` — all marked **UNSURE** in the top-level
[`README.md`](../README.md).
