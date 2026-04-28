# Stage 1 — Current vs. Target Architecture Map (EEE refactor)

This document maps the current `eval_audit` architecture against the target
defined in `ARCHITECTURE.md`, with specific attention to where Every Eval Ever
(EEE) becomes the normalized comparison layer.

## Target (ARCHITECTURE.md, restated)

* **Two indexes** (canonical): one for public/official HELM runs, one for
  local/audited HELM runs. They share schema where useful but stay separate
  sources of truth.
* **Raw vs. derived** separation: raw runs are read-only evidence; derived
  analysis is cheap to rebuild.
* **Packet-driven planning**: the planner emits "packets" — bundles of
  normalized run components, comparisons declared between them, and
  comparability metadata.
* **Comparison core** operates on normalized representations, not on
  HELM-specific run readers. Source kind (official/local) and artifact
  format (helm/eee/...) are explicit. Subject-level granularity (e.g. MMLU
  per-subject) is preserved.
* **Warnings/caveats** propagate planner → reports → aggregates.
* **Reports surface** is the publication/browsing layer with story order:
  universe → eligible → attempted → completed → analyzed → reproducibility.

## Current architecture (where things live)

### Indexing (Stage 1, Stage 4)

* `eval_audit.cli.index_historic_helm_runs` — discovers public HELM runs from
  filesystem; writes `official_public_index_*.csv`, `run_specs.yaml`, and a
  filter inventory JSON. Uses `magnet.backends.helm.helm_outputs.HelmOutputs`
  via direct import (`HelmOutputs.coerce(bo)`).
* `eval_audit.workflows.index_results` — discovers local audit results from a
  results root; writes `audit_results_index_*.csv`. Uses
  `eval_audit.compat.helm_outputs.HelmOutputs.coerce(bo)`.
* `eval_audit.indexing.schema` — shared CSV column schema and identity helpers
  (`OFFICIAL_COMPONENT_COLUMNS`, `LOCAL_COMPONENT_COLUMNS`,
  `extract_run_spec_fields`, `component_id_for_official/local`,
  `logical_run_key_for_*`). This is already the right place to hang a
  source_kind-aware loader contract.

The two indexes already exist and are physically separate. Schema overlap
exists at the `COMMON_COMPONENT_COLUMNS` level.

### Run reading (HELM-native)

* `eval_audit.compat.helm_outputs` — local re-implementation of the
  `magnet.backends.helm.helm_outputs` API: `HelmRun`, `HelmSuite`,
  `HelmOutputs`. Pure JSON readers over `run_spec.json`,
  `scenario_state.json`, `stats.json`, `per_instance_stats.json`.
* `eval_audit.helm.analysis.HelmRunAnalysis` — wraps a `HelmRun`, caches
  derived indices: `stat_index()`, `joined_instance_stat_table()`,
  `summary_dict()`. The join logic that builds
  `JoinedInstanceStatTable` is the central HELM-shaped computation.
* `eval_audit.helm.diff.HelmRunDiff` — pairwise comparison of two
  `HelmRunAnalysis` instances. Produces tolerance sweeps, value-distance
  profiles, instance agreement, run-spec semantic diff, diagnosis labels.
  ~2,629 lines — this is the single largest HELM-shaped surface.
* `eval_audit.helm.hashers`, `eval_audit.helm.metrics`,
  `eval_audit.helm.run_entries` — small utilities (stable hashing, metric
  classification, run-name parsing). Some are general (`run_entries`
  parsing); `metrics.classify_metric` is HELM-specific.

### Comparison planning

* `eval_audit.planning.core_report_planner` — consumes the official + local
  CSV indexes, normalizes rows into `NormalizedPlannerComponent`, applies an
  "official selection policy" (latest suite version per public track), and
  emits packets containing components + comparisons + comparability facts +
  warnings. Already source-kind-aware.
* `eval_audit.workflows.plan_core_report_packets` — packet manifest writer.
* `eval_audit.reports.core_packet` / `core_packet_summary` — packet manifest
  IO + small summary helpers.

### Pairwise comparison + report core

* `eval_audit.reports.core_metrics` — the central per-packet report
  generator. It loads each `(component_id, run_path)` pair via
  `HelmRun.coerce(run_path)` and `HelmRunAnalysis`, then runs
  `HelmRunDiff` to compute `_run_level_core_rows` and
  `_instance_level_core_rows`. These reach into `joined_instance_stat_table`
  and `stat_index` for HELM-specific structures.
* `eval_audit.reports.pair_report` — a thinner pair report (writes
  `pair_report_*.json`); also goes through `HelmRun` + `HelmRunDiff`.
* `eval_audit.reports.pair_samples` — text dump of mismatched instance
  samples, also via `HelmRunDiff`.
* `eval_audit.reports.quantiles` — left/right pair quantile comparison via
  `HelmRunDiff`.

### Aggregates / publication

* `eval_audit.workflows.analyze_experiment` — runs the planner per experiment
  and triggers `rebuild_core_report_main` for each packet.
* `eval_audit.workflows.rebuild_core_report` — drives `core_metrics.main`.
* `eval_audit.workflows.build_reports_summary` — Stage 6 aggregate. Loads
  per-packet `core_metric_report.latest.json`, joins with the index, emits
  end-to-end + reproducibility sankeys, breakdowns, and prioritized-example
  drilldowns. Already operates on the **normalized JSON outputs** of Stage 5,
  not on raw HELM artifacts. This is the easy half.

### Warnings & caveats

Already first-class in the planner:
`NormalizedPlannerComponent.warnings/caveats`, packet-level
`warnings`/`caveats`/`comparability_facts`, comparison-level
`warnings/caveats/comparability_facts`. `core_metrics._warnings_payload`
flattens these into `warnings.latest.json` per report. Aggregate Stage 6
reads these JSONs and propagates flags.

### EEE assets (current state)

* `submodules/every_eval_ever/every_eval_ever/` — the EEE package, with
  `EvaluationLog` (eval_types.py) and `InstanceLevelEvaluationLog`
  (instance_level_types.py) Pydantic schemas, plus a
  `converters/helm/` adapter exposing `every_eval_ever convert helm`.
* `dev/poc/eee-audit/sweep.py` — POC: shells out to `every_eval_ever convert
  helm` for every public HELM run; writes converted JSONs under
  `/data/crfm-helm-audit-store/crfm-helm-public-eee-test/{suite}/{ver}/{run}/eee_output/`.
  Has its own SQLite manifest + retry logic.
* `dev/poc/eee-audit/find_preexisting_helm_runs.py` — POC lookup for the
  Pythia-MMLU smoke case.
* `dev/poc/eee-audit/pr_justification.md` — five HELM-converter bug fixes
  surfaced by the corpus sweep (already enumerated; some may already be
  patched in the submodule).

EEE is **not** wired into the eval_audit code paths today. Comparison still
goes through `HelmRunDiff` end-to-end.

## Seams identified for the refactor

The audit pipeline already has the right shape. The work is to insert a
normalized-format boundary at the loader layer, leave the planner
contract alone, and replace the HELM-shaped objects passed into comparison
with EEE-shaped equivalents.

| # | Seam | Current | Target |
|---|---|---|---|
| 1 | Index loader | `HelmOutputs.coerce` over `benchmark_output/runs` | `NormalizedRunSource.discover` that yields `NormalizedRunRef`s with `source_kind`, `artifact_format`, and stable origin path |
| 2 | Per-run reader | `HelmRun(run_dpath)` + `HelmRunAnalysis(run)` | `NormalizedRun.load(ref)` → backed by EEE schemas; `helm` artifact_format converts on read |
| 3 | Planner output | Manifests carry `run_path` + `source_kind` | Same fields, plus `artifact_format` and (when known) `eee_path` for the converted/native EEE artifacts |
| 4 | Pairwise comparison | `HelmRunDiff(run_a, run_b)` reads HELM JSONs and joins via `JoinedInstanceStatTable` | `NormalizedRunDiff(run_a, run_b)` operates on EEE `EvaluationLog` + `InstanceLevelEvaluationLog`; legacy HelmRunDiff isolated behind a normalized adapter |
| 5 | Per-metric agreement curves | `_instance_level_core_rows` joins per-instance stats by HELM `(instance_id, train_trial_index, perturbation_id)` | EEE `sample_id`/`sample_hash` + `evaluation_result_id` (already in the schema) gives a portable join key without HELM internals |
| 6 | Aggregates | Reads Stage 5 JSON only | Unchanged — derived; just gains `artifact_format` provenance on rows |
| 7 | Filesystem layout | `reports/`, `audit_store/indexes/`, `analysis/` (latest aliases + history) | Unchanged. New canonical EEE-converted artifacts live under audit_store, with `*.latest.*` pointers and a nearby reproduce script |

## Where HELM-native assumptions live

* **Stat join logic** (`HelmRunAnalysis.joined_instance_stat_table` and
  `JoinedInstanceStatTable`) — encodes HELM's perturbation/sub-split shape.
  Replace at the loader boundary by reading EEE
  `InstanceLevelEvaluationLog` records (one per (sample, metric)) directly.
* **`stats.json` shape** — `_collect_stat_means`, `_run_level_core_rows`,
  `_metric_class` filters live in `core_metrics.py` and `helm/metrics.py`.
  Replace with EEE `EvaluationResult.metric_config.metric_id/kind`.
* **`scenario_state.json` request_states** — `_run_diagnostics` reaches into
  `scenario_state['request_states'][...]['result']['completions']` for empty
  completion / token count diagnostics. EEE
  `InstanceLevelEvaluationLog.output.raw` + `token_usage` provides the
  equivalent.
* **Run-spec metadata** — `extract_run_spec_fields` reads
  `adapter_spec.model`, `scenario_spec.class_name` from `run_spec.json`.
  Equivalent EEE fields: `model_info.id`, `evaluation_results[].evaluation_name`
  (per-eval) or `additional_details` for HELM-only fields. `run_spec.json`
  itself is preserved as raw evidence.
* **Run-name parsing** — `helm.run_entries.parse_run_name_to_kv` parses
  `bench:k=v,...` syntax. This is HELM-specific surface for filesystem layout
  but doesn't need to leak into the comparison layer; it stays at the index
  loader.

## Where EEE POC hooks already exist

* The POC sweep produces a stable converted artifact tree. We can read those
  JSON artifacts directly with `every_eval_ever.eval_types.EvaluationLog` and
  `every_eval_ever.instance_level_types.InstanceLevelEvaluationLog` (both
  Pydantic models with `.model_validate_json`).
* `find_preexisting_helm_runs.find_poc_paths` already demonstrates the
  source_kind-aware lookup style we need at the framework level.
* The PR-justification bug list in `dev/poc/eee-audit/pr_justification.md`
  enumerates the small converter patches we should land in
  `submodules/every_eval_ever/` before rerunning conversions on tricky
  benchmarks. Per the working constraints, these get isolated commits in
  the submodule.

## Filesystem boundary for the new layer

To keep raw vs. derived separation honest, the new normalized artifacts
live at:

```
$AUDIT_STORE_ROOT/
  eee/
    public/<track>/<suite_version>/<run_name>/   # converted from official HELM
      eval.json                                  # EvaluationLog
      instance_level.jsonl                       # InstanceLevelEvaluationLog
      provenance.json                            # source_kind, helm_run_path, converter_version
      reproduce.sh                               # reruns the conversion
    local/<experiment>/<job_id>/<run_name>/      # converted from local audit runs
      eval.json
      instance_level.jsonl
      provenance.json
      reproduce.sh
  indexes/
    eee_public_index_*.csv                       # catalog of converted public artifacts
    eee_local_index_*.csv                        # catalog of converted local artifacts
```

Source kind and artifact format columns become first-class on the existing
`OFFICIAL_COMPONENT_COLUMNS` / `LOCAL_COMPONENT_COLUMNS`, and the planner
prefers EEE artifacts when present, falling back to on-the-fly HELM→EEE
conversion (in-memory) when not.

## Stage-by-stage execution plan (preview)

1. **Stage 2** — introduce `eval_audit.normalized` package containing
   `NormalizedRunRef`, `NormalizedRun`, and an `Loader` registry keyed by
   `artifact_format`. Two loaders: `EeeLoader` (reads converted JSON) and
   `HelmJsonLoader` (in-memory HELM→EEE conversion using
   `every_eval_ever.converters.helm`). Loader picks normalized
   `EvaluationLog` + `InstanceLevelEvaluationLog`. No comparison logic
   moves yet; just replace the file-reading boundary.
2. **Stage 3** — replace `HelmRun.coerce(run_path)` call sites in
   `core_metrics.py`, `pair_report.py`, `pair_samples.py`,
   `quantiles.py`, `compare_batch.py` with the normalized loader.
   Comparison core continues to consume the old objects via a thin shim
   that exposes `stat_index()` / `joined_instance_stat_table()` from the
   normalized representation.
3. **Stage 4** — replace `HelmRunDiff` for the core-metrics path with a
   `NormalizedDiff` that operates on `EvaluationLog` + per-instance
   records. Preserve diagnostic flags, warnings/caveats, run-level &
   instance-level agreement curves, per-metric breakdowns. Keep
   `HelmRunDiff` for run_spec semantic diff (still read from
   `run_spec.json` raw).
4. **Stage 5** — planner emits `artifact_format` and `eee_path` on every
   component; manifests grow these fields; aggregate sankeys/breakdowns
   carry `artifact_format` provenance. Combined union artifacts (for
   Sankeys) remain dumb derived views.
5. **Stage 6** — quarantine `eval_audit.helm.analysis` /
   `eval_audit.helm.diff` to "raw HELM evidence inspection only". Update
   `compat/helm_outputs.py` to be the documented raw-source ingest path
   for the loader (not for comparison). Update docs/pipeline.md.
6. **Stage 7** — re-read ARCHITECTURE.md and check item-by-item; smoke
   tests on a couple of converted runs.

## Smoke-test target for early validation

The POC has already produced converted artifacts for runs like the
Pythia-6.9b MMLU subjects. Each subsequent stage validates against:

```
/data/crfm-helm-public/classic/.../mmlu:subject=us_foreign_policy,...
/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.3.0/mmlu:subject=us_foreign_policy,.../eee_output/
```

vs. a corresponding local audit run if available; otherwise a public/public
self-comparison (which should produce strict agreement at `abs_tol=0`).
