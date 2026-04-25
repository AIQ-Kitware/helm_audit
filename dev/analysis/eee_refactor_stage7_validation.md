# Stage 7 — Validation against ARCHITECTURE.md

Lightweight check: re-read ARCHITECTURE.md item by item, confirm the
implementation matches, note any narrow gaps without triggering expensive
reruns. No raw HELM reruns; smoke tests + targeted self-pair checks only.

## Test results

```
tests/test_normalized_smoke.py:       4 passed
tests/test_normalized_compare.py:     3 passed
```

End-to-end self-pair on the EEE-shipped HELM fixture (mmlu philosophy,
gpt2) through `_build_pair`:

* 80 (sample, core-metric) instance rows joined
* run-level abs_delta = 0.0 across all matching metrics
* instance-level agreement_ratio = 1.0 at abs_tol = 0
* diagnosis label produced (HelmRunDiff path still wired for run-spec
  semantic facts)
* `artifact_formats` populated on the pair payload from the call site

## Goal-by-goal check

### Separate official/public and local/audit indexes (canonical)

* Both indexes still produced by their dedicated CLIs
  (`index_historic_helm_runs`, `index_results`). Schema lives in
  `helm_audit.indexing.schema` and now carries `artifact_format` +
  `eee_artifact_path` on both column lists.
* No combined index of truth. `build_reports_summary` derives breakdowns
  from per-packet manifests + the local index; the `artifact_format`
  string on each aggregate row is a derived view.

### Explicit planning/comparison intent

* `NormalizedPlannerComponent` records `source_kind`,
  `artifact_format`, and `eee_artifact_path` as first-class fields.
* `to_manifest_component` emits both fields so packet manifests on disk
  fully describe what the planner intended to compare and how.

### Manifest-driven comparison packets

* Existing packet manifest format preserved. New fields added at the
  component level; existing readers tolerate them (extra dict keys).
* `_build_pair` accepts the planner component dicts directly so the
  loader is selected from manifest content, not from filesystem
  inspection.

### Warning/caveat propagation: planner → reports → aggregates

* Planner: `warnings` / `caveats` lists on
  `NormalizedPlannerComponent` and on packet manifests; unchanged in
  this refactor.
* Stage-5 reports: `_warnings_payload` / `_warning_summary_lines` in
  `core_metrics.py` still emit `warnings.latest.{json,txt}`.
* Stage-6 aggregates: `packet_warnings`, `packet_caveats`,
  `comparison_warning_count`, `report_warning_count`, and
  `has_report_warnings` keys still populated on aggregate rows.
* No regression — none of the warning surfaces were touched.

### Aggregate/publication surfaces with drilldown to evidence

* `Origin.helm_run_path` on every `NormalizedRunRef` preserves the
  pointer back to the raw HELM run directory.
* Stage-5 component records still carry `run_path` and (now)
  `eee_artifact_path`, so downstream drill-down navigates from
  aggregate row → packet → component → raw HELM run.

### Cheap rebuilds of derived artifacts

* `helm_audit.normalized.load_run` is the single load entry point.
  Re-rendering reports against existing on-disk EEE artifacts (when
  `artifact_format='eee'`) skips the in-memory HELM->EEE conversion
  entirely.
* `reproduce.latest.sh` scripts emitted by the report stages remain
  unchanged; they invoke the same Stage-5/6 commands which now flow
  through the normalized layer.

### Clear separation: raw sources vs. canonical derived analysis vs. human-facing reports

* Raw HELM JSONs untouched on disk. `compat/helm_outputs.py` is now
  documented as discovery-only.
* Derived analysis lives under the planner / Stage-5 reports as
  before; the new `helm_audit.normalized` package sits between them
  and the raw artifacts.
* `reports/` remains the publication surface; no schema changes here.

## ADR check

| ADR | Status |
|---|---|
| 1. Separate public/local indexes | Honored — schemas still distinct, only common columns added |
| 2. Raw vs. derived | Honored — raw HELM tree never written to from new code |
| 3. `reports/` as publication surface | Honored — no aggregate format change |
| 4. Filesystem as interface | Honored — `eee_artifact_path` makes the canonical EEE artifact tree first-class |
| 5. Reproduce scripts near outputs | No change |
| 6. Filtering remembered and reportable | No change — `filter_inventory.json` still produced |
| 7. Browseable paths | Honored — Origin preserves the HELM run path for evidence drilldown |
| 8. Log every meaningful write | No change |
| 9. Plot inputs not silently truncated | No change |
| 10. Plot labels explain counts | No change |

## Generality check (not POC-shaped)

* `helm_audit.normalized.loaders` uses a registry keyed by
  `ArtifactFormat`. Adding a future format (e.g. `lm_eval`,
  `inspect_ai`) is a one-line `register_loader(...)` plus the loader
  class.
* `compare.py` is loader-agnostic — it consumes only `NormalizedRun`
  and uses metric classification that is itself open to override.
* The bridge `helm_compat.helm_view` works for any `NormalizedRun`
  whose `Origin.helm_run_path` resolves; non-HELM-derived runs fall
  back to the cached `raw_helm` slot.
* Tests exercise both loaders end-to-end; no logic depends on the
  POC sweep output structure.

## Narrow gaps (recorded, not fixed)

* `helm_compat.helm_view_from_path` produced for the Stage-3 raw-only
  bridge is still imported by some callers (pair_report,
  pair_samples, quantiles, compare_batch). It is functionally
  identical to the new path; collapsing those callers onto
  `_load_normalized` + `helm_view` would shave a function but is
  cosmetic and was deferred per Stage-6 instructions.
* The current EEE HELM converter produces one
  `InstanceLevelEvaluationLog` per sample with a single `score` and
  `evaluation_result_id == None`. Per-(sample, metric) granularity is
  preserved by the `HelmRawLoader` (which reads
  `per_instance_stats.json` directly) but not yet by the
  `EeeArtifactLoader` for converted artifacts. To fix at the converter
  level, set `evaluation_result_id` per metric in
  `every_eval_ever/converters/helm/instance_level_adapter.py`. Out of
  scope for this refactor.
* `compare_batch.py` still uses HelmOutputs.coerce for discovery
  (legitimate raw-source ingest) and only routes the comparison
  arguments through the normalized bridge. The HelmRunDiff measurement
  there is not yet on `ncompare`; this batch tool is operational and
  not exercised by Stage-5 reports.
* When migrating to a fully EEE-shape diagnosis (replacing the
  `HelmRunDiff.summary_dict(level=20)` call inside `_build_pair`),
  port the run-spec/scenario/comparability checks to read from
  `NormalizedRun.evaluation_log.eval_library` and
  `NormalizedRun.raw_helm['run_spec']`. Defer until needed; the diff
  outputs are stable and consumed by aggregate reports.

## Conclusion

The implementation matches ARCHITECTURE.md target for the EEE-as-primary
normalized comparison layer. The framework is general (registry-based
loaders, format-agnostic compare core) and not POC-shaped. Source
separation, warning propagation, drilldown evidence, and cheap-rebuild
properties are preserved. Remaining gaps are narrow and documented above.
