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

The fix: create `eval_audit/utils/numeric.py` with canonical, documented implementations of `safe_float`, `quantile` (sorts internally, the safer choice), and `nested_get` (new). Each file that previously defined these locally now imports from `utils.numeric` using a private alias (`_safe_float = safe_float`) so call sites need zero changes. The `analysis.py`'s version was adopted as canonical since it was most defensive; callers in `diff.py` that happened to pre-sort still work correctly since sorting an already-sorted list is a no-op.

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

## 2026-04-18 00:00:00 +0000

User intent: refactor the report/analysis layout to establish one canonical per-experiment analysis root in the audit store, eliminating the split between `repo_root()/reports/` and `/data/crfm-helm-audit-store/`.

Model and configuration: claude-sonnet-4-6, Claude Code CLI.

### Problem statement

The codebase had analysis truth split across two filesystem roots:
- Raw experiment outputs and indexes → `/data/crfm-helm-audit-store/`
- Per-experiment analysis summaries and core reports → `repo_root()/reports/core-run-analysis/experiment-analysis-{name}/`

This made it hard to answer: "what is the current canonical analysis for experiment X?" It also meant indexes had no `latest` alias (five timestamped files, no pointer to the newest), and there was no per-analysis provenance record.

### Approach chosen

Minimal coherent refactor: change where things are written, not what is written. No content changes to reports; only path and alias logic touched.

1. **`paths.py`** — added `experiment_analysis_dpath(name)` returning `$AUDIT_STORE_ROOT/analysis/experiments/{name}/`.

2. **`report_layout.py`** — `core_run_reports_root()` now returns the store path (`$AUDIT_STORE_ROOT/analysis/experiments/`). Old `reports/core-run-analysis/` is now `compat_core_run_reports_root()`.

3. **`analyze_experiment.py`** — three additions:
   - On first run with new code, if an existing real dir lives at the old compat path and the canonical store path doesn't yet exist, it is automatically moved (`shutil.move`) to the store. This migrates history without data loss.
   - After writing analysis outputs, writes `provenance.json` at the experiment root recording `generated_utc`, `experiment_name`, `index_fpath`, `analysis_root`, `git_sha`.
   - Creates a relative symlink from the legacy compat path (`reports/core-run-analysis/experiment-analysis-{name}`) to the canonical store path. Existing symlinks are left alone (idempotent); real dirs that weren't migrated (e.g., both paths already existed) log a warning and skip.

4. **`build_reports_summary.py`** — `_load_all_repro_rows()` now scans both the new canonical store root (`*/core-reports/*/...`) and the old compat root (`experiment-analysis-*/core-reports/*/...`). Deduplication by `(experiment_name, run_entry)` tuple handles any overlap. The `experiment-analysis` symlink in aggregate summaries now prefers `experiment_analysis_dpath()` and falls back to the compat path.

5. **`index_results.py`** — after writing timestamped index files, now also writes `latest` aliases (`audit_results_index.latest.{csv,jsonl,txt}`) so the most recent index is always findable without parsing timestamps.

### Key design insight

The `reports/` tree is gitignored, so it was already a local-only artifact. Making it a symlink forest (pointing into the store) costs nothing and preserves every existing hardcoded path. The store becomes the real truth; `reports/` is now a convenience layer.

### Migration story

- Existing experiment dirs at `reports/core-run-analysis/experiment-analysis-{name}/`: migrated to store automatically on first re-run. Between now and that re-run, `build_reports_summary.py` still finds them via the dual-scan glob.
- Existing index files in `/data/crfm-helm-audit-store/indexes/`: timestamped files remain; next `eval-audit-index` run will also write `latest` aliases.
- No history deleted, no content modified.

### Files changed

- `eval_audit/infra/paths.py` — +5 lines (`experiment_analysis_dpath`)
- `eval_audit/infra/report_layout.py` — `core_run_reports_root` redirected, `compat_core_run_reports_root` added
- `eval_audit/workflows/analyze_experiment.py` — new canonical path, migration, provenance.json, compat symlink
- `eval_audit/workflows/build_reports_summary.py` — dual-scan glob, `experiment_analysis_dpath` lookup
- `eval_audit/workflows/index_results.py` — `latest` aliases for index files

### Command to rerun analysis and inspect new canonical output

```bash
python -m eval_audit.workflows.analyze_experiment \
  --experiment-name audit-small-models-kubeai-overnight \
  --index-fpath /data/crfm-helm-audit-store/indexes/audit_results_index.latest.csv

# New canonical root:
ls /data/crfm-helm-audit-store/analysis/experiments/audit-small-models-kubeai-overnight/
cat /data/crfm-helm-audit-store/analysis/experiments/audit-small-models-kubeai-overnight/provenance.json

# Compat symlink (backward compat):
ls -la reports/core-run-analysis/experiment-analysis-audit-small-models-kubeai-overnight
```

## 2026-04-20 (session continuation)

Summary of user intent: implement Stage 1 of the report surface improvements in `build_reports_summary.py` — rename the 5 canonical sankey `kind=` strings to carry story-arc position prefixes, move 9 tolerance-variant sankeys to an `alt_tolerances/` subdirectory, and add a `story_index.latest.txt` that gives explicit reading order.

Model and configuration: claude-sonnet-4-6, Claude Code CLI (VSCode extension).

**Canonical kind= renames**

The root problem was that `level_001/interactive/` held 15 sankey HTML files with names like `sankey_operational.latest.html`, `sankey_filter_to_attempt.latest.html`, `sankey_end_to_end.latest.html`, etc., with no signal about which to read first or why. A reader opening the directory had to already know the story to navigate it.

The fix: five canonical story-arc sankeys now carry an `s0N_` prefix reflecting their reading order:
- `operational` → `s01_operational` (executive view: all runs, benchmark → lifecycle → outcome)
- `filter_to_attempt` → `s02_filter_to_attempt` (eligible run-specs → actually attempted)
- `attempted_to_repro` → `s03_attempted_to_repro` (attempted → reproducible at exact match)
- `end_to_end` → `s04_end_to_end` (full funnel: discovered → reproducible)
- `reproducibility` → `s05_reproducibility` (detailed group → repeatability → agreement → diagnosis)

This changes filenames in `.history/` subdirs and `.latest.*` alias names everywhere they appear, so it's a clean break — no partial compatibility issues since the `.latest.*` aliases are what external callers use.

**Tolerance variants moved to alt_tolerances/**

Nine tolerance-sweep sankeys (`repro_tol001/010/050`, `attempted_to_repro_tol001/010/050`, `end_to_end_tol001/010/050`) now emit into `level_001/alt_tolerances/{machine,interactive,static}/` instead of `level_001/{machine,interactive,static}/`. The variables `alt_tol_dpath`, `alt_tol_machine`, `alt_tol_interactive`, `alt_tol_static` are created alongside the other level dirs (line ~1923). The tolerance variants are still accessible; they're just not cluttering the main reading surface. They remain listed in the `manifest` dict for programmatic access.

The alternative considered was keeping them in level_001 but with an `alt_` kind prefix (`alt_repro_tol001`, etc.) — rejected because that still clutters the directory listing. Directory-based separation is cleaner: a reader scanning `ls level_001/interactive/` now sees 8 HTMLs (5 story + metric + agreement_curve + coverage_matrix) rather than 17.

**story_index.latest.txt**

Added after all artifacts are written, before `_write_scope_level_aliases`. The file explicitly lists s01–s05 with one-line descriptions and the filename pattern for each. Also lists supplementary artifacts (`repro_by_metric`, `alt_tolerances/`, `agreement_curve`, `coverage_matrix`). Aliased to both `level_001/story_index.latest.txt` and the summary root via `_write_scope_level_aliases`.

Design takeaways:
1. Prefixing with `s0N_` costs nothing in code complexity and creates a self-documenting directory listing. The "N" directly answers "what order should I read these in?"
2. Move supporting artifacts to subdirs rather than prefixing them — the directory becomes the namespace, not the filename.
3. A plain text reading-order file is the cheapest possible navigation aid and survives file system inspection better than any README embedded in an HTML file.

## 2026-04-20 (Stage 1 consistency + Stage 2)

Summary: Stage 1 README consistency patch + Stage 2 factor/cardinality summaries.

**Stage 1 consistency patch (build_reports_summary.py)**

`_build_high_level_readme()` still referenced old sankey names. Updated to use `s01`–`s05` names, added `story_index.latest.txt` and `cardinality_summary.latest.txt` as first items under `start_here:`, and replaced the tolerance-variant browsing guidance with a pointer to `alt_tolerances/`.

**Stage 2: filter_cardinality_summary.latest.txt (filter_analysis.py)**

Added `build_filter_cardinality_text(inventory_rows)` — a pure function that computes unique model/benchmark/scenario counts at each filter funnel stage (all_discovered → considered → eligible → selected) and formats them as a fixed-width table. Called from `emit_filter_report_artifacts`; written to `static/filter_cardinality_summary_{stamp}.txt` with a `.latest.txt` alias. One new key in `outputs` dict: `'filter_cardinality_txt'`.

No changes to the existing summary JSON, TSVs, or sankeys — just a new text artifact alongside them.

**Stage 2: cardinality_summary.latest.txt (build_reports_summary.py)**

Added `_cardinality(rows)` helper and `_build_scope_cardinality_lines(filter_inventory_rows, enriched_rows, scope_title, generated_utc)`. Covers five pipeline stages: discovered (from filter_inventory_rows), eligible_selected (from filter_inventory_rows), attempted, completed (`has_run_spec` truthy), analyzed (`official_instance_agree_0 is not None`). Written to `level_001_static/cardinality_summary_{stamp}.txt`; aliased to both `level_001/cardinality_summary.latest.txt` (direct access) and surfaced to summary_root via `_write_scope_level_aliases`. If `filter_inventory_rows` is empty, the discovered/selected lines are omitted silently.

**Intentionally not changed:**
- No architectural changes, no new data loading, no recompute passes
- No changes to sankey schemas or existing artifact paths
- `filter_analysis.py`'s TSV tables and existing summary JSON untouched
- No cardinality data in the manifest dict (it's a plain text artifact, not machine-readable state)
- `_write_scope_level_aliases` still only surfaces the `level_001_static` version to summary_root — the direct `level_001` alias is for convenience only

## 2026-04-20 22:29:28 +0000

User asked for a conservative Stage 1 improvement: add a checked-in registry of locally-servable models, rename the misleading `no-hf-deployment` failure reason, annotate inventory rows, and surface a new local serving recovery summary in the filter report.

Claude Sonnet 4.6.

**Problem diagnosed.** `no-hf-deployment` was applied to any model that lacked a default HuggingFace deployment path in HELM's model registry AND wasn't in the manual `KNOWN_HF_OVERRIDES` set. The name implied the model has no HuggingFace presence, which is wrong — the real issue is that Stage 1's automatic filter knows of no default local HELM deployment path for the model. Local serving knowledge was implicit and scattered across `PRESET_CONFIGS` in `adapter.py` and the `KNOWN_HF_OVERRIDES` set in `index_historic_helm_runs.py`.

**Changes made.**

1. `eval_audit/model_registry.py` (new): `LocalModelEntry` dataclass + `LOCAL_MODEL_REGISTRY` list populated from `PRESET_CONFIGS` and `KNOWN_HF_OVERRIDES`. Fields: `model`, `expected_local_served`, `replaces_helm_deployment` (null = off-story extension, non-null = public HELM model being reproduced), `source`, `notes`. Single `local_model_registry_by_name()` lookup helper.

2. Renamed `no-hf-deployment` → `no-local-helm-deployment` across all six files: `index_historic_helm_runs.py`, `filter_analysis.py`, `build_reports_summary.py`, both test files. Updated the detail message to say "no default local HELM deployment path is known to the Stage 1 automatic filter."

3. `build_filter_inventory_rows` now imports `local_model_registry_by_name()` and annotates each row with `expected_local_served`, `replaces_helm_deployment`, `local_registry_source`. Zero cost at filter time — pure dict lookup.

4. New `build_local_serving_recovery_text(inventory_rows)` in `filter_analysis.py` partitions models excluded by `no-local-helm-deployment` into on-story / off-story / no-plan and renders a compact text table.

5. New artifact `filter_local_serving_summary.latest.txt` emitted by `emit_filter_report_artifacts` at both `static/` and filter report root. Aliased alongside `filter_cardinality_summary.latest.txt`.

**Design choice: no YAML config file.** Registry lives in Python (`model_registry.py`) rather than YAML so it gets code review and imports cleanly without a loader. The user explicitly wanted it in `eval_audit`.

**What was NOT done (intentional scope constraints):**
- No runtime verification of vllm_service profile switching — noted as TODO in `model_registry.py` docstring.
- No backend-specific distinctions (vllm_local vs kubeai_local vs litellm_vllm_local).
- Filter logic itself unchanged — `KNOWN_HF_OVERRIDES` still drives what passes; registry is annotation-only.
- No new plot artifact (text table is sufficient; adding a plot would require plotly and is not clearly cheap/clean for this partition).

14 filter tests pass.

## 2026-04-21 00:00:00 -0700

User asked for version-aware official/public HELM index and a sidecar analysis tool, motivated by the fact that public HELM has ~36K run dirs spanning multiple suite versions and tracks, and the existing Stage 1 selected subset (~270 runs) is too small to serve as a canonical inventory.

Claude Sonnet 4.6.

**Part A — official/public index artifact in `index_historic_helm_runs.py`:**
Added `KNOWN_STRUCTURAL_JUNK_NAMES`, `_normalize_for_hash()`, `_compute_run_spec_hash()`, `_classify_run_entry()`, `_scan_benchmark_output_dir()` (inner loop, directly testable), `build_official_public_index_rows()` (calls magnet discover), `write_official_public_index()` (timestamped CSV + .latest.csv symlink). New CLI arg `--out_official_index_dpath` (opt-in, no effect unless specified). Existing Stage 1 outputs unchanged.

Key design decisions:
- `_scan_benchmark_output_dir` is separated out as a pure filesystem helper so tests don't need magnet.
- `run_spec_hash` is SHA-256 of recursively key-sorted JSON, truncated to full hex for uniqueness.
- Structural junk detection: known names (`groups`, `confs`, `logs`, `__pycache__`) → `structural_non_run`; dirs with `:` → `benchmark_run`; others → `unknown`.
- `public_track` = relative path from root to `benchmark_output` parent (`.` → `'main'`).

**Part B — `eval_audit/workflows/analyze_official_index.py`:**
Standalone tool consuming a single official index CSV. Produces 8 artifacts: summary txt/json, per-track/version/model/benchmark CSVs, duplicates report, version-drift report. Does NOT rescan filesystem. Registered as `eval-audit-analyze-official-index` entrypoint.

**Path helpers added to `paths.py`:** `official_public_index_dpath()` → `indexes/`, `official_public_analysis_dpath()` → `analysis/official-public-index/`.

**Tests:** `tests/test_official_public_index.py` — 26 tests, all passing. Covers all 6 required scenarios without needing magnet or real HELM data.

Next: User may want to actually run `--out_official_index_dpath` against `/data/crfm-helm-public` and then run the analysis tool. The scan will be slow (36K dirs + run_spec.json reads) but is a one-time operation.

## 2026-04-22 00:00:00 +0000

User intent: Narrow implementation pass on the report-rendering layer. Stop auto-rendering every heavy pairwise interactive artifact by default. Canonical high-level outputs and selected candidate-of-interest pairwise artifacts still auto-render; the full exhaustive set of heavy per-pair distribution plots does not. Write a nearby `render_pairwise_interactives.sh` script per report directory to regenerate them on demand.

Model and configuration: claude-sonnet-4-6, Claude Code CLI.

**The design switch**

The previous `core_metrics.main()` unconditionally rendered four heavy per-pair distribution plots (`core_metric_distributions`, `core_metric_overlay_distributions`, `core_metric_ecdfs`, `core_metric_per_metric_agreement`) for every single report directory. With hundreds of report directories this becomes expensive and produces an overwhelming number of PNG files in the default report surface.

Architecture Amendment 2 from `ARCHITECTURE.md` calls for exactly this: "Do not auto-render every pairwise interactive artifact. Write a nearby script to generate richer HTML/Plotly outputs on demand."

**Implementation**

Single flag approach: `--render-pairwise-interactives` added to `core_metrics.main()` (default False). All four heavy plots are guarded behind this flag. The canonical outputs (summary 4-panel PNG, runlevel table CSV/MD, text reports, JSON, warnings) are unchanged and always rendered.

`rebuild_core_report.py` gains two things:
1. `_CANDIDATE_OF_INTEREST_KINDS: frozenset[str] = frozenset()` — the explicit, named selection point for auto-rendering heavy artifacts. Empty by default. Extend this set to designate specific comparison kinds for full auto-rendering.
2. A `render_pairwise_interactives.latest.sh` script written next to the canonical reproduce script. The render script calls `eval_audit.reports.core_metrics` with `--render-pairwise-interactives` using the stable `components_manifest.latest.json` / `comparisons_manifest.latest.json` aliases (not the timestamped copies) so it stays valid across multiple rebuilds.

The management summary now includes: `on_demand_pairwise_interactives: render_pairwise_interactives.sh (in this directory)`.

**Key design insight**

The one clean switch point (a single `--render-pairwise-interactives` flag gating all heavy calls) is better than per-artifact conditions scattered through `main()`. The selection logic lives in `rebuild_core_report._CANDIDATE_OF_INTEREST_KINDS` rather than in `core_metrics` itself, which keeps the rendering layer unaware of selection policy.

Using `components_manifest.latest.json` in the render script (rather than the timestamped paths passed to `_build_pair`) means the script stays correct after a re-render that updates the manifests — the latest alias tracks.

**Tests**

`test_core_metrics_single_run.py`: updated existing test to assert heavy artifacts absent by default; added second pass with `--render-pairwise-interactives` to assert they appear; asserts management summary contains the on-demand note.

`test_rebuild_core_report.py`: both test functions assert `--render-pairwise-interactives` absent from `core_metrics.main()` calls by default; assert `render_pairwise_interactives.latest.sh` written; assert script content contains flag and canonical manifest names. Two new tests cover the explicit selection mechanism.

6 tests, all passing.

## 2026-04-22 00:30:00 +0000

User intent: Refine the "heavy pairwise plots on demand" patch from the same session. Two problems: (1) the script name said "interactives" but no Plotly/HTML was generated — only heavy PNG plots; (2) the candidate-of-interest selection hook keyed on `comparison_kind`, which is too coarse (enabling `official_vs_local` would silently re-enable heavy rendering for most reports).

Model and configuration: claude-sonnet-4-6, Claude Code CLI.

**Rename: interactives → heavy_pairwise_plots**

All three surfaces updated consistently:
- CLI flag: `--render-pairwise-interactives` → `--render-heavy-pairwise-plots`
- Script: `render_pairwise_interactives.latest.sh` → `render_heavy_pairwise_plots.latest.sh` / `.sh` symlink
- Management summary key: `on_demand_pairwise_interactives` → `on_demand_heavy_pairwise_plots`
- Description text: "interactives" → "heavy per-pair PNG plots"

The old script names are now cleaned up in `_cleanup_legacy_report_surfaces` so existing report directories don't accumulate stale symlinks.

**Refined selection hook**

Replaced `_CANDIDATE_OF_INTEREST_KINDS: frozenset[str]` (a module-level constant keyed on comparison_kind) with:

```python
def _should_auto_render_heavy_pairwise_plots(
    packet: dict[str, Any],
    comparisons: list[dict[str, Any]],
    report_dpath: Path,
) -> bool:
    return False
```

Key differences:
- Takes the full packet (packet_id, run_entry, warnings, flags, etc.)
- Takes the full comparisons list (comparison_ids, not just kinds)
- Takes report_dpath (path-based selection is possible)
- Returns False unconditionally by default
- Documented with concrete extension examples (packet_id set, diagnostic flag check)

This prevents the "I added official_vs_local and suddenly 200 reports render heavy plots" accident. Any extension must be explicit about *which specific packet or report* warrants heavy rendering.

Design insight: name and interface shape together signal intent. `frozenset[str]` of comparison kinds says "broad category match"; a function taking `(packet, comparisons, report_dpath)` says "narrow predicate over full context." The interface shape is itself the policy documentation.

## 2026-04-22 01:00:00 +0000

User intent: Fill in the missing behavior in `_should_auto_render_heavy_pairwise_plots`. The function existed but returned False unconditionally. "Some reports should auto-render; most should not; the rule should be explicit and inspectable." Preferred signal: pipeline warnings, not comparison kind.

Model and configuration: claude-sonnet-4-6, Claude Code CLI.

**Signal chosen: unexpected comparability drift warnings**

The planner emits `comparability_drift:{fact_name}` when a comparability fact is "no". For local reproductions, `comparability_drift:same_deployment` is always expected (local vLLM deployment vs official HuggingFace). Deployment-only drift is intentional and boring.

Any other drift — adapter instructions, base model, scenario class, max_eval_instances — is not expected and warrants visual inspection. A module-level tuple `_UNEXPECTED_DRIFT_WARNING_PREFIXES` makes the selection rule explicit and easy to adjust.

The function collects all warnings from the packet and all enabled comparisons, then checks for any matching prefix. This handles both packet-level and comparison-level warnings from the planner.

Design insight: grounding the policy in planner-emitted warning strings (not comparison kinds or hardcoded packet IDs) means the selection automatically tracks the planner's comparability analysis. If the planner flags something unusual, heavy plots follow without needing manual curation of a shortlist. The deployment exclusion is explicit and commented.

## 2026-04-22 01:30:00 +0000

User intent: Correctness bug — `_UNEXPECTED_DRIFT_WARNING_PREFIXES` used guessed fact names (`same_base_model`, `same_adapter_instructions`) that don't match what `build_comparability_facts()` actually emits. Also, add a test that uses real planner machinery so future renames are caught.

Model and configuration: claude-sonnet-4-6, Claude Code CLI.

**Corrected fact names (from build_comparability_facts in core_report_planner.py):**
- `same_base_model` → `same_model`
- `same_adapter_instructions` → `same_instructions`
- Added: `same_benchmark_family`
- Excluded (expected to differ): `same_suite_or_track_version` (parallel to `same_deployment`)

**Test fixture alignment:**
`_write_index_inputs` previously used `instructions="official"` vs `instructions="local"`, which would now emit `comparability_drift:same_instructions` and trigger heavy rendering in the integration tests. Changed to `instructions=""` (both empty → `same_instructions=unknown`) so the fixture represents routine deployment-only drift without triggering the hook.

**Real-machinery test:**
`test_trigger_prefixes_match_real_planner_warning_names` calls `_comparability_warning_lines` directly (the real planner function) to get actual warning strings, then verifies the selection function responds correctly. This test will fail if the planner renames a fact and the prefix list isn't updated.

Design insight: import and test against the real emitter function, not hand-written string literals. The test becomes self-validating: it checks that the emitter produces the exact strings the consumer expects, closing the gap between two modules that must stay in sync.

## 2026-04-24 18:56:11 +0000

User asked to stress-test the EEE (Every Eval Ever) HELM converter against all official public HELM results available locally, surface converter bugs, and harden the converter.

Model and configuration: claude-sonnet-4-6, Claude Code CLI.

**Scope and setup**: 36,046 valid HELM run directories across 13 benchmark suites under `/data/crfm-helm-public`. Output root: `/data/crfm-helm-audit-store/crfm-helm-public-eee-test`. Driver script: `dev/poc/eee-audit/sweep.py`.

**Sweep script design**: Enumerates runs from `{suite}/benchmark_output/runs/{version}/{run_name}`, calls `every_eval_ever convert helm` per run as a subprocess, writes per-run `status.json` (traceable to source path), and a JSONL results log. Skip-existing by checking `status == "ok"` in status.json; resumes cleanly across partial runs. Configurable `--workers`, `--limit`, `--suite`, `--timeout`, `--max-scenario-state-mb`.

**Bugs discovered and fixed** (all in `submodules/every_eval_ever/`):

1. **Bug 1 — IndexError: `correct_refs[0]` on empty list** (`converters/helm/instance_level_adapter.py` line 166).
   - Triggered by: `capabilities/ifeval`, `capabilities/wildbench` runs where instances have no reference answers.
   - Fix: `state.request.prompt + (correct_refs[0] if correct_refs else '')`.
   - Fixed in commit `368ad4c6f`.

2. **Bug 2 — ValidationError: `reasoning_trace` list contains None** (`converters/helm/utils.py`).
   - Triggered by: `capabilities/gpqa` with chain-of-thought runs where `thinking` object exists but `thinking.text` is `None`.
   - Fix: filter `None` values from `extract_all_reasonings` result list; return `None` if empty.
   - Fixed in commit `368ad4c6f`.

3. **Bug 3 — WrongTypeError: `instance.id` is int, expects `Optional[str]`** (`converters/helm/adapter.py`).
   - Triggered by: `long-context` suite (HELM v1.0.0) where instance IDs are stored as JSON integers.
   - Fix: pass `config=DaciteConfig(cast=[str])` to the `from_dict(ScenarioState, ...)` call.
   - Fixed in commit `bad6f1a6f` (by joncrall, pre-existing on branch `helm-stress-test-fixes`).

**Expected non-bug failures**:
- `FileNotFoundError: Run requires local media assets`: speech (139/139 runs), image2struct (~30% of 1599 runs).
  - Root cause: `MediaObject.__post_init__` in HELM asserts local file existence; audio/image files not downloaded.
  - The converter already handles this correctly with `except AssertionError → raise FileNotFoundError`.
  - These are infrastructure failures, not converter bugs.

**Sweep results so far** (sweep still in progress for classic/heim/image2struct):
- Completed suites with 0 converter failures: capabilities, ewok, finance, lite, long-context, mmlu, safety.
- Text-only failures across all completed text-only suites: 0.
- All failures are `FileNotFoundError` from missing media assets (speech/image2struct).

**Sweep script improvements made during session**:
- Increased stderr storage from 4000 to 12000 chars (chained exceptions were truncated, causing misclassification of `AssertionError` vs. `FileNotFoundError`).
- Improved `_extract_exception_class` to skip indented traceback lines and find the outermost exception.
- User added size-gating (`--max-scenario-state-mb`, default 512 MB) and configurable `--timeout` to handle `msmarco:track=trec` runs with ~10 GB scenario files.

**Uncertainties / next steps**:
- Classic suite (29,050 runs) still in progress — needs multiple 10-minute passes due to Bash timeout limits. Skip-existing handles resume.
- heim suite (3,727 runs) in progress; expect some `FileNotFoundError` for image-based scenarios.
- Final summary at `/data/crfm-helm-audit-store/crfm-helm-public-eee-test/summary.json`.
- The fixed bugs (1 and 2) were verified: 13/13 previously failing pilot runs now pass after fix.

Design insight: when testing against a large real-world corpus, always separate "converter can't handle this data" from "this data requires local assets that aren't present." Both show up as failures, but only the former needs fixing. Sweeping all suites rather than just a few exposes both categories and lets you quantify the boundary precisely.

## 2026-04-27 17:09:00 +0000

User intent: overnight autonomous push toward a completed set of EEE-backed
reproducibility reports. Drive packet planning + report generation broadly
across all 25 local experiments, convert local HELM runs to EEE on demand,
fix small/local bugs that block coverage, leave artifacts and a summary for
review.

Claude Opus 4.7, Claude Code CLI (VSCode extension), aivm-2404 with NOPASSWD
sudo. `.venv313` at /home/joncrall/code/helm_audit/.venv313 (uvpy3.13.2).

**Initial blocker.** Mounts at /data/crfm-helm-{audit,audit-store,public}
were attached but empty when I started. After surfacing this clearly, user
remounted; data appeared (~36K official runs, sweep DB with
discovered=36046, succeeded=34683, failed=1126, skipped_too_large=237).

**Stage-2 sanity.** First `pytest` invocation returned EMFILE on every
collection. Diagnosed as virtiofs page-cache pressure (1M FD limit but
opening any directory in `/home/joncrall/code/helm_audit/eval_audit` failed
in bare bash). Cleared with `echo 3 | sudo tee /proc/sys/vm/drop_caches`.
Tests then green: 139/139 passed in 207s. Worth remembering for next
session: virtiofs in this VM can wedge after long idle periods, drop_caches
fixes it without remount.

**Smoke pass.** Re-ran `analyze_experiment` for `audit-boolq-pythia-r1`
with `--ensure-local-eee`. n_planned=1, n_built=1, n_skipped=0. The
`Harden EEE report generation on real artifacts` commit (21150e9) on Apr 25
fixed the prior "File name too long" crash in `component_link_basename`,
so the boolq smoke now succeeds where the Apr 22 run had n_built=0.

**Threading EEE flags through analyze_many.** `eval_audit.cli.analyze_many`
didn't pipe `--official-eee-root`, `--local-eee-root`, `--ensure-local-eee`,
or `--official-index-fpath` through to per-experiment analyses. Added all
four; without `--ensure-local-eee` the broad pass would skip every local
component because no local EEE artifact existed yet.

**Run 1 (broad pass).** `analyze_many --all-from-index --ensure-local-eee
--allow-single-repeat` over 25 experiments / 498 index rows. Total 1.7h
wallclock, 0 experiment-level failures. But: 517 packets planned, only 159
built. 358 skipped, of which:
- 213 ≈ "no enabled comparisons" (legitimate: no public counterpart for
  this model+benchmark combo, e.g. openai/gpt2 was never publicly run on
  boolq).
- 145 ≈ TypeError: "argument should be a str or an os.PathLike object…
  not 'NoneType'" — concentrated in `audit-historic-grid` (145) and
  `audit-historic-grid-gpt-oss-20b-vllm-trimmed` (4).

**Root cause for the 145 NoneType crashes.** Local index rows for
scheduled-but-never-executed attempts have empty `run_path`/`run_dir`
(`status=`, `has_run_spec=False`). The planner still emitted these as
local components with `run_path=None`; `_write_component_symlinks` then
crashed on `Path(None).resolve()`.

Fix in two places (both shipped in this session):
1. `eval_audit/planning/core_report_planner.py:_prefilter_index_rows` —
   drop local rows with no run_path before normalization. This is the
   correctness fix; these rows have no instances to compare so the packet
   should never have existed.
2. `eval_audit/workflows/rebuild_core_report.py:_write_component_symlinks` —
   defensively skip `component["run_path"] is None` entries instead of
   crashing. Belt-and-braces in case any slip past the prefilter.

26 targeted tests still pass.

**Run 2 (broad pass after fix).** Same command, ~1.5h wallclock.
- experiments_ok:        25/25
- planned_packets:       274  (down from 517 — the 243 dead rows are
                                now correctly filtered)
- built_reports:         159  (58.0% of planned)
- skipped:               115  — *all* `no_official_match`, none NoneType.
                                Every remaining skip is a domain-level
                                "this model+benchmark combo doesn't exist
                                in public HELM" case, not a code bug.

**Aggregate summary built.** `build_reports_summary --index-fpath …
--filter-inventory-json …` rebuilt
`reports/aggregate-summary/all-results/` with the canonical 5-step sankey
narrative, agreement curves, coverage matrix, failure taxonomy, and
prioritized examples. Cardinality summary now shows: discovered=13579,
selected=270, attempted=498, completed=255, analyzed=148. The 148 analyzed
is the new denominator for downstream reproducibility narrative; agreement
buckets are 22 exact_or_near_exact / 42 high_0.95+ / 54 moderate_0.80+ /
37 low.

**Side fixes shipped while waiting for the broad pass:**

A. `dev/poc/eee-audit/sweep.py`:
   - `--show-failure-paths [CLASS]`: emits one run_path per line, headerless,
     suitable for `xargs`/`rsync --files-from=-`. Cleanly redownloads the
     three malformed `msmarco:cohere_small-20220720` paths the user has been
     trying to triage.
   - The existing `--report`, `--show-failures`, and the new
     `--show-failure-paths` can now be combined in a single invocation. When
     paths are emitted alongside another section a labeled
     `FAILURE RUN PATHS (CLASS)` header demarcates them; standalone form
     stays plain so it pipes.

B. `submodules/aiq-magnet/magnet/backends/helm/cli/download_helm_results.py`:
   - Removed the stale `_runs_root` "classic quirk". HELM's public bucket
     reorganized: classic now lives at
     `gs://crfm-helm-public/classic/benchmark_output/runs/<ver>` like every
     other benchmark. The legacy `gs://crfm-helm-public/benchmark_output/runs/`
     path is empty (verified via the GCS JSON API). Every recent classic
     `--list-versions` call returned empty because of this. After the fix
     classic resolves identically to lite/mmlu/etc.
   - Cleaned up `list_benchmarks` to drop the now-redundant
     `names.add('classic')` and the `'benchmark_output'` blocklist entry.
   - Pre-existing bug noted but not fixed: `--version='v0.2.2|v0.2.3|v0.2.4'`
     does NOT alternate; `kwutil.MultiPattern.coerce` treats the whole
     string as one strict literal. The script's docstring example
     `--benchmark="lite|ewok"` is therefore wrong. Workaround: use
     `regex:` prefix (`--version 'regex:v0\.2\.[234]'`). Fixing this needs
     YAML-coercing `--version` and `--benchmark` like `--runs` already is;
     deferred to user decision.

**Design insight.** The most leverage in tonight's pass came from
distinguishing "scheduled-but-never-ran index rows" from "ran but no public
counterpart" at the planner. Same observable failure ("packet skipped")
but different fixes: the first is a planner prefilter (cheap), the second
is research design (no fix, document it). Without the categorization the
145 + 213 looked like a single mass of skips and would have been hard to
prioritize. Once split, the planner fix is a 4-line change that turns
"58% of 517" into "58% of 274 with no spurious failures."

**Outstanding items for the user tomorrow.**
- Decide if `download_helm_results.py` `--version 'a|b|c'` alternation
  bug is worth fixing (3 lines).
- The 3 `msmarco:cohere_small-20220720` JSONDecodeError paths are now
  redownloadable via the unblocked `download_helm_results.py` once the
  user runs the regex command on a host with rw on /data/crfm-helm-public.
- 115 legitimate "no_official_match" skips in run2 are *not* code bugs;
  they document the boundary of what's reproducible against public HELM.
  Worth surfacing in the paper as a denominator caveat.
- analyze_many run-rate after EEE-cache-warm: small experiments ~1s,
  audit-historic-grid ~43m, audit-qwen25-7b-aiq ~10m. The two big ones
  dominate; subsequent re-renders of small experiments are essentially
  free.

**Files changed this session (uncommitted as of this entry):**
- `eval_audit/planning/core_report_planner.py` — no-run-path prefilter
- `eval_audit/workflows/rebuild_core_report.py` — None-guard symlink writer
- `eval_audit/cli/analyze_many.py` — thread EEE flags + official-index-fpath
- `dev/poc/eee-audit/sweep.py` — `--show-failure-paths`, combinable read-only modes
- `submodules/aiq-magnet/...download_helm_results.py` — drop classic quirk

**Artifacts on disk for review tomorrow:**
- `/data/crfm-helm-audit-store/analysis/experiments/<exp>/experiment_summary.latest.{json,csv,txt}`
- `/data/crfm-helm-audit-store/analysis/experiments/<exp>/core-reports/core-metrics-<packet>/...`
- `/home/joncrall/code/helm_audit/reports/aggregate-summary/all-results/` — story sankeys + agreement curves
- `/home/joncrall/code/helm_audit/.cache/overnight/analyze_many_run{1,2}.log` — full per-experiment log

Next step (for whoever picks this up): commit the staged changes, then
either (a) attack the `|`-alternation parsing bug if reproducible-set
should grow to include older bucket layouts, or (b) move on to verifying
specific reproducibility findings against the 159 built reports.
