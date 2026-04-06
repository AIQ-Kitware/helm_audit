## 2026-04-04 02:45:38 +0000

Summary of user intent: three sessions' worth of work on `build_reports_summary.py` and related infrastructure. (1) Reorganize output so `.json` files go into a `machine/` subfolder and human-readable files (`.html`, `.jpg`, `.png`, `.txt`, `.csv`, `.sh`) stay at the top level of `level_001`. (2) Add threshold context to all figures that use `abs_tol=0` agreement buckets without saying so. (3) Add three new diagnostic plots: agreement tolerance curve, model×benchmark coverage matrix, and failure root-cause taxonomy.

Model and configuration: claude-sonnet-4-6, Claude Code CLI.

**machine/ subfolder reorganization**

The original level_001 directory mixed 40+ files — JSON data blobs, HTML visualizations, TXT summaries, CSVs — at the same level. The operator opening it to find a Sankey diagram had to visually wade through the data files. The fix was clean: add an optional `machine_dpath` parameter to `emit_sankey_artifacts` (in `sankey.py`), `_write_table_artifacts`, and `_write_plotly_bar` (both in `build_reports_summary.py`). When provided, the `.json` file and its `.latest.json` alias go to `machine_dpath`; everything else stays in `report_dpath`. In `_render_scope_summary`, we create `level_001/machine/` and `level_002/machine/` and pass them everywhere. The summary manifest itself also goes into `machine/`. This pattern is non-invasive: callers that don't pass `machine_dpath` continue working as before, which is important for breakdown scopes that run with `include_visuals=False`.

The alternative considered was writing everything to `machine/` and symlinking visual files back up — that was rejected because symlink chains pointing across directories are harder to follow manually and would make the human-readable files look like they live in `machine/` in some editors.

**Threshold context on figures**

The core problem: `official_instance_agree_bucket` is always computed at `abs_tol=0` (exact match). Five figures used it as a color or axis without saying that. Specifically: the strict reproducibility Sankey (“Executive Reproducibility Summary”), the operational Sankey's outcome stage, the reproducibility buckets bar chart, the benchmark status bar chart, and the per-metric drift Sankey. The fix was entirely in title strings and `stage_defs` descriptions — no logic changes. Added `(instance-level, abs_tol=0 exact match)` to titles, changed axis labels from machine-key strings to human descriptions, and expanded `stage_defs` for both the strict Sankey and the multi-tolerance Sankeys to spell out what the bucket labels mean (fraction of instances agreeing at that tolerance). Also added `xaxis_title`/`yaxis_title` optional overrides to `_write_plotly_bar` since plotly's default of replacing underscores with spaces produces confusing labels.

**Three new diagnostic plots**

Three questions drove the new plots:
- “How does agreement change as tolerance relaxes?” → agreement tolerance curve
- “What subset of HELM (model × benchmark) are we running, and at what analysis stage?” → coverage matrix
- “Why are the other jobs failing — hardware limit, data access, or special infra?” → failure taxonomy

For the tolerance curve, I extended `_load_all_repro_rows` to store the full `agreement_vs_abs_tol` list (13 thresholds) per row, then built a `go.Scatter` line plot with log-scale x-axis, one line per run colored by benchmark. Using `go.Figure` directly rather than `px.line` was necessary because the data shape (curves as separate lists per row) doesn't fit the px tidy-data model well, and because I needed `legendgroup` to deduplicate benchmark colors in the legend while showing all 30 individual run lines.

For the coverage matrix, I used `go.Heatmap` with a custom colorscale mapping -1 (not attempted) through 5 (exact/near-exact) to six distinct colors. The aggregation per `(model, benchmark)` cell takes the *best* status across all runs in that cell — conservative in the sense that seeing one exact-match run means “we can do it,” which is the right semantic for a coverage question. The matrix shows immediately why our HELM coverage looks the way it does: `mmlu_clinical_afr` and `winogrande_afr` have high completion rates while `mmlu` (the large one) has mostly failures except for 4 models × a few subjects.

For the failure taxonomy, I defined a static `_FAILURE_CATEGORIES` dict mapping each raw `failure_reason` to one of four root-cause categories: `hardware_timeout`, `data_access`, `missing_infrastructure`, `unknown`. This is important because the categories answer fundamentally different questions: hardware timeout means “get more GPU” or “run fewer instances”; data access means “cache the dataset locally”; missing infrastructure means “get OpenAI credentials or special datasets.” A stacked bar per benchmark makes the dominant barrier for each benchmark immediately legible.

Risks and uncertainties: the coverage matrix color encoding (-1 through 5) could confuse readers who expect heatmaps to be continuous. The colorbar ticktext helps, but interactive hover is the real safeguard. The failure taxonomy assumes the static `_FAILURE_CATEGORIES` mapping is comprehensive; new failure reasons not in the dict fall back to “unknown / other” which is correct defensive behavior. The agreement curve x-axis substitutes `1e-13` for `abs_tol=0` to avoid `log(0)` — visually this looks like the zero-tolerance point lands at the leftmost tick, which is accurate enough.

Design takeaways:
1. Separate machine-readable data from human-readable views at the directory level, not just the file level — it changes the first-open experience from “where is the HTML?” to “obviously here.”
2. When a visualization figure's color/axis encoding carries an implicit assumption (threshold, comparison direction, etc.), put that assumption in the title or axis label, not just in a key file. Readers look at figures before reading keys.
3. Failure taxonomy charts are most useful when categories are defined by *what action fixes them*, not by symptom. “Hardware timeout,” “data access barrier,” and “missing credentials” each suggest a different next step for an operator.

## 2026-04-04 03:14:22 +0000

Summary of user intent: identify and implement high-value refactors to reduce technical debt, improve code readability, and make the codebase easier to reason about and extend.

Model and configuration: claude-sonnet-4-6, Claude Code CLI (VSCode extension).

**Analysis**

Before touching anything, I did a full codebase audit using the Explore subagent. The two highest-impact categories of debt were: (1) the same small utility functions copy-pasted across three modules with subtle drift between versions, and (2) deeply nested `(((obj.get('a') or {}).get('b') or {}).get('c') or {}).get('d')` chains that appeared verbatim across `build_reports_summary.py`, `analyze_experiment.py`, and `aggregate.py`, making the data access intent completely opaque.

**Duplicate utility consolidation**

`_safe_float` existed in three files — `helm/analysis.py`, `helm/diff.py`, and `reports/core_metrics.py` — and had silently diverged. The `analysis.py` version was the most defensive: it included a `math.isnan()` guard that the other two lacked. Left unaddressed, any bug fix to one copy would not propagate to the others. Similarly, `_quantile` appeared in `diff.py` and `core_metrics.py`; the `core_metrics.py` version sorted its input internally while `diff.py`'s assumed pre-sorted input (callers in `diff.py` happened to sort first, so both worked, but the inconsistency was a trap for anyone adding a new call site).

The fix: create `helm_audit/utils/numeric.py` with canonical, documented implementations of `safe_float`, `quantile` (sorts internally, the safer choice), and `nested_get` (new). Each file that previously defined these locally now imports from `utils.numeric` using a private alias (`_safe_float = safe_float`) so call sites need zero changes. The `analysis.py`'s version was adopted as canonical since it was most defensive; callers in `diff.py` that happened to pre-sort still work correctly since sorting an already-sorted list is a no-op.

**nested_get helper**

The 4-level `.get()` chains appear in dict-heavy report assembly code where HELM's JSON payload has a fixed schema but where callers defensively handle missing keys at every level. These chains are correct but deeply unfriendly: a 120-character line like `((((official.get("run_level") or {}).get("overall_quantiles") or {}).get("abs_delta") or {}).get("max"))` encodes a simple "give me `official['run_level']['overall_quantiles']['abs_delta']['max']` or None" intent behind 5 layers of syntactic noise.

`nested_get(obj, *keys, default=None)` replaces all of these. It stops at the first missing or non-dict step and returns `default`. The replacement is semantically identical because the original `or {}` pattern also stops propagating at a missing key (it just does so via an empty dict sentinel). One subtle case to watch: if an intermediate value is legitimately present but is `None` (e.g., a field explicitly set to `null` in the JSON), `nested_get` correctly returns `default` because `None` fails the `isinstance(obj, dict)` check — same behavior as the original `(value or {}).get(...)` pattern.

Applied to 16 sites across `build_reports_summary.py`, `analyze_experiment.py`, and `aggregate.py`. In `build_reports_summary.py`, I also extracted `official_instance_level` and `official_agree_curve` as named locals, eliminating the repeated `.get("instance_level")` traversal inside a single dict comprehension block and making the loop structure cleaner.

**What was not done**

The god-module problem in `build_reports_summary.py` (1694 LOC, 36+ functions covering data loading, visualization, and export) is a real issue but a higher-risk refactor that should come after better test coverage. Left as-is with a note in this journal. Similarly, the CLI argument parsing inconsistency (raw `argparse` vs. `scriptconfig`) was deferred because it has no behavioral impact and the risk of accidentally changing CLI behavior outweighs the benefit at this point.

**Testing**

All 13 existing tests pass. Doctests in `utils/numeric.py` pass. All module imports clean after the changes.

Risks: the `nested_get` semantics differ from the original chains only in the "explicitly None intermediate" edge case, which shouldn't occur in real HELM payloads but isn't tested. Worth adding a test if this bites.

Design takeaways:
1. When the same function appears in 3+ files with different internal details, the right canonical version is the most defensive one — its extra guards are there because someone hit a real edge case.
2. Chained `(obj.get('a') or {}).get('b')` patterns should be viewed as a code smell for missing abstraction, not defensive programming — extract a helper the moment they appear in 3+ places.
3. A god module is best decomposed after tests exist for it, not before; refactoring without tests trades one risk (readability) for another (silent behavioral regression).

## 2026-04-04 04:35:00 +0000

Summary of user intent: Improve end-to-end pipeline visibility and documentation. (1) Add filter-step analysis with Sankey showing what `index_historic_helm_runs.py` kept/dropped and why. (2) Create `docs/pipeline.md` with technical reference covering all 7 stages and troubleshooting. (3) Reorganize all-results README to better guide operators through reports in dependency order. (4) Ensure all plotly HTML outputs have JPG sidecars (already working; `agreement_curve_per_metric` will render on next re-run).

Model and configuration: claude-haiku-4-5-20251001, Claude Code CLI (VSCode extension).

**Filter-Step Analysis**

The key insight was that models may fail multiple filter criteria simultaneously (e.g., size AND no HF deployment). Rather than recording only the "first" failure, the solution expands multi-failure models into one sankey row per failure reason. This means the sankey row count exceeds the model count, which is intentional — it shows the total "count of filter hits" by reason. Operators can immediately see that "too-large" is a bigger contributor than "no-hf-deployment" by the row thickness in the flow.

Added `out_report_dpath` argument to `index_historic_helm_runs.py` (optional, non-breaking). When provided:
1. Builds `model_filter_rows` list with all failure reasons per model
2. Expands into `sankey_rows` (one row per model per failure reason)
3. Calls `emit_sankey_artifacts` with `stage_order=[('filter_reason', ...), ('outcome', ...)]`
4. Writes text report with count summary

The filter-step Sankey lives alongside `run_specs.yaml` in the `out_report_dpath` directory, making it discoverable by operators running Stage 1 independently. The all-results README now points to this artifact under "understand_upstream_filtering."

**End-to-End Documentation**

Created `docs/pipeline.md` as the canonical technical reference:
- Stage 0–6 with exact CLI commands, arguments, and outputs
- Filtering logic spelled out (5 model criteria + structural completeness)
- Each stage's input/output structure
- Full runbook example (Qwen scenario)
- Troubleshooting section
- Why "agreement_curve_per_metric is missing" (data availability; will fix on re-run)

This document is intended to survive as the primary operator handoff — it is more detailed than reproduce/README.md (which focuses on scenarios) and more focused than dev/journals/ (which is historical context). It answers "what does each stage do" and "why is the output organized this way."

**README Reorganization**

Updated `_build_high_level_readme()` in build_reports_summary.py to restructure "start_here" into four labeled sections:
- `understand_upstream_filtering`: points to Stage 1 filter report
- `explore_execution_coverage`: operational sankey, per-metric, coverage
- `understand_reproducibility`: reproducibility sankeys at different tolerances, agreement curves
- `diagnose_failures`: failure reasons, taxonomy, bucket distribution

Each section has 1–4 action items ordered by "you should read this first" logic. This is a UX improvement — operators opening the README now see a clear path through the artifacts instead of a flat list.

**JPG Sidecars**

The infrastructure was already correct (all existing plotly functions write JPG when Chrome is available). `agreement_curve_per_metric` is currently missing HTML+JPG because the underlying `per_metric_agreement` data field was added to the code AFTER the most recent Stage 5a run. This is not a bug — it's expected transience. When Stage 5a is re-run, the reports will include `per_metric_agreement`, and Stage 6 will then render the HTML+JPG. Documented this in docs/pipeline.md under "Note on `agreement_curve_per_metric`."

**Risks and Uncertainties**

The filter-step Sankey row expansion (one row per failure reason) is mathematically sound but visually different from "single exit point per model." If an operator expects the total row count in the sankey to equal the model count, they may be confused. Addressed by labeling the stage as "Exclusion Criterion" and documenting in `docs/pipeline.md` that multi-failure models contribute multiple rows.

The `docs/pipeline.md` is long (~350 lines) and assumes familiarity with HELM's run_spec/scenario/model ecosystem. It is not a beginner's introduction; it is a reference for operators who have already run at least one scenario and want to understand the audit machinery around it. Acceptable tradeoff because the reproduce/README.md scenarios still serve as onboarding.

**Testing**

1. `python -m py_compile` passes on index_historic_helm_runs.py and build_reports_summary.py
2. Reviewed filter-report generation logic: structurally-incomplete counter added, model_filter_rows list building correct, sankey_rows expansion correct (one row per reason)
3. README restructuring is textual only; no behavioral changes

**Design Takeaways**

1. When filtering logic has multi-criterion failures, show all reasons in the output, not just the first — it surfaces the full picture of what stopped a run.
2. Documentation for a multi-stage pipeline should have three layers: scenario-based runbooks (reproduce/), stage-by-stage technical reference (docs/pipeline.md), and detailed design history (journal/). Each has a different reader.
3. Reorganizing human-facing output (README) by logical "sections the operator cares about" is higher-value than reorganizing by artifact type — operators follow question paths, not file listings.


## 2026-04-04 21:20:00 +0000

**Follow-up: Filter-step Sankey HTML/JPG Rendering Fix**

After the initial implementation, the filter report was generating JSON and TXT files but no HTML or JPG. Root cause: `emit_sankey_artifacts()` was being called without `interactive_dpath`, `static_dpath`, and `machine_dpath` parameters, causing all artifacts to be written to the flat `report_dpath` directory.

**Fix implemented:**
1. Create subdirectories: `interactive/`, `static/`, `machine/` within `report_dpath`
2. Pass these to `emit_sankey_artifacts()` so it knows where to write each artifact type
3. `emit_sankey_artifacts()` already handles creating `.latest.*` symlinks, so no additional symlink logic needed

**Result:**
- `interactive/sankey_model_filter.latest.html` (8.1 KB, interactive Plotly)
- `static/sankey_model_filter.latest.jpg` (136 KB, static image)
- `machine/sankey_model_filter.latest.json` (2.3 MB, data)
- `static/sankey_model_filter.latest.txt` (graph summary)
- `static/model_filter_report.txt` (custom statistics report)

**Key insight:** The artifact organization pattern (machine/ for JSON, interactive/ for HTML, static/ for JPG/TXT) is already established in `build_reports_summary.py` and `emit_sankey_artifacts()`. Consistency matters — operators expect the same directory layout across all report generation.

**Verification:** Ran full filter indexing with real CRFM data:
- 13,579 discovered runs
- 13,504 structurally complete
- 152 unique models
- 7 selected models (passed all 5 criteria)
- 270 selected runs
- Top exclusion reason: no-hf-deployment (10,601 runs)

The fix ensures operators always get JPG sidecars alongside HTML for easy sharing and offline viewing.
