# HELM Audit End-to-End Pipeline

This document covers the complete reproducibility audit pipeline: from discovering historic HELM runs through aggregating final reports.

**Quick Links:**
- Operator runbook: [`reproduce/README.md`](../reproduce/README.md)
- Package reference: [`README.md`](../README.md)
- Design journal: [`dev/journals/codex.md`](../dev/journals/codex.md)
- Normalized layer (EEE): [`eval_audit/normalized/`](../eval_audit/normalized/) — `NormalizedRun`, loaders for `helm` and `eee` artifact formats, compare core. All per-pair comparison code paths flow through this boundary; raw HELM JSONs remain on disk as evidence.

---

## Storage Layout

Generated artifacts are now split across two roots:

```text
/data/crfm-helm-audit-store/   ← canonical writable machine-readable store
  configs/
    run_specs.yaml
    run_details.yaml
    manifests/
  indexes/
  analysis/

reports/                       ← repo-local browsable report surface
  filtering/
  core-run-analysis/
  aggregate-summary/
```

Use `AUDIT_STORE_ROOT` to override `/data/crfm-helm-audit-store`. The intent is:
- keep checked-in `configs/` for curated source-controlled inputs and overrides only
- keep generated manifests, run selections, inventories, and indexes out of the repo
- keep `reports/` in the repo because it is a convenient browsing surface

Each report family keeps human-facing `*.latest.*` links in the visible directories and hides stamped history under `.history/`. Each generated report root should also contain:
- `reproduce.latest.sh`: rerun the full computation that produced that report root
- `rebuild_analysis.latest.sh` when the plotting/aggregation step can be rebuilt from saved machine-readable inputs without recomputing upstream work

---

## Report Layout

Generated report artifacts live under the repo-level `reports/` tree:

```text
reports/
  filtering/            ← Stage 1 discovery/filtering inventory + plots
  core-run-analysis/    ← Stage 5 per-experiment and per-run reproducibility reports
  aggregate-summary/    ← Stage 6 aggregate operator-facing summaries
```

## Stage 0: Environment Setup

Before running the pipeline, ensure:

```bash
# Python 3.13+ with uv
/home/agent/.local/uv/envs/uvpy3.13.2/bin/python --version

# Dependencies installed (in editable mode for local development)
cd /home/joncrall/code/helm_audit
uv pip install -e .

# MAGNeT backend (required for historic HELM discovery and execution)
cd /home/joncrall/code/aiq-magnet
uv pip install -e .

# HELM with all benchmarks (required for scenario/model registry)
uv pip install 'crfm-helm[all]' -U

# HuggingFace credentials (required for model downloads)
huggingface-cli login  # or pass --token to index_historic_helm_runs

# Optional but recommended: static Plotly export support for JPG/PNG sidecars
# Python deps already include plotly + kaleido via pyproject.toml
uv pip install -e .
bash reproduce/setup/10_install_plotly_chrome_ubuntu2404.sh
PYTHONPATH=. python -m eval_audit.cli.check_env --plotly-static-only

# Chrome is searched in this order:
#   1. .cache/plotly-chrome/chrome-linux64/chrome
#   2. ~/.plotly/chrome/chrome-linux64/chrome
#   3. the choreographer package cache, if present
```

---

## Stage 1: Discover & Filter Historic HELM Runs

**Purpose:** Index all historic HELM run outputs from a CRFM public or private data source, apply eligibility filters, and emit a reproducible list.

**Command:**
```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"

python -m eval_audit.cli.index_historic_helm_runs \
  /data/crfm-helm-public \
  --out_fpath "$AUDIT_STORE_ROOT/configs/run_specs.yaml" \
  --out_detail_fpath "$AUDIT_STORE_ROOT/configs/run_details.yaml" \
  --out_inventory_json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

**Key Arguments:**
- `roots` (positional): One or more directories containing HELM `benchmark_output/` subdirectories.
- `--suite_pattern`: Glob pattern for suite selection (default: `*` = all).
- `--run_pattern`: Glob pattern for run selection within each suite (default: `*:*` = HELM run format).
- `--require_per_instance_stats`: If True, only include runs with `per_instance_stats.json` (slow; default False).
- `--include_max_eval_instances`: If True, infer `max_eval_instances` from per-instance data (slow; default False).
- `--out_fpath`: Write `run_spec_name` list as YAML. The package default is now `$AUDIT_STORE_ROOT/configs/run_specs.yaml`.
- `--out_detail_fpath`: Write full row data with all metadata as YAML. The package default is now `$AUDIT_STORE_ROOT/configs/run_details.yaml`.
- `--out_inventory_json`: Write the full Stage 1 filter inventory as JSON for later analysis / plotting.
- `--dedupe`: If True (default), deduplicate identical `(run_spec_name, max_eval_instances)` rows.

**Filtering Logic:**

1. **Structural Filter** (all runs, not just models):
   - Requires: `run_spec.json`, `scenario_state.json`, `stats.json`, `per_instance_stats.json`
   - Counts: How many runs have incomplete file sets

2. **Model Eligibility Filter** (all models found in structurally-complete runs):
   - Text-like tags: Must have at least one of `TEXT_MODEL_TAG`, `FULL_FUNCTIONALITY_TEXT_MODEL_TAG`, `INSTRUCTION_FOLLOWING_MODEL_TAG`
   - Excluded tags: Must NOT have `VISION_LANGUAGE_MODEL_TAG`, `AUDIO_LANGUAGE_MODEL_TAG`, `IMAGE_MODEL_TAG`, `TEXT_TO_IMAGE_MODEL_TAG`, or `CODE_MODEL_TAG`
   - Size: `num_parameters <= 10B` (conservative for local GPU execution; None = unknown, allowed)
   - Access: `access == 'open'` in HELM model registry
   - Deployment: Must have a HuggingFace client deployment, OR appear in `KNOWN_HF_OVERRIDES` (currently: 3 Qwen models that route via Together but can run locally)

   A model may fail multiple criteria simultaneously (e.g., size AND no HF deployment). All failure reasons are logged and included in the filter report Sankey.

**Outputs:**
- `$AUDIT_STORE_ROOT/configs/run_specs.yaml`: selected `run_spec_name` strings
- `$AUDIT_STORE_ROOT/configs/run_details.yaml`: detailed Stage 1 rows
- `$AUDIT_STORE_ROOT/analysis/filter_inventory.json` (or your chosen path): full machine-readable Stage 1 inventory

### Stage 1a: Build Filter Reports From Saved Inventory

Use this when you want to iterate on the filter analyses, Sankeys, or report directory structure without re-scanning HELM outputs:

```bash
PYTHONPATH=. python -m eval_audit.cli.reports filter \
  --report-dpath reports/filtering \
  --inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

This step owns all output under `reports/filtering/`. Stage 1 itself no longer writes into the report tree.

Outputs from this analysis step:
- `reports/filtering/interactive/sankey_model_filter.latest.html`, `reports/filtering/static/sankey_model_filter.latest.{jpg,txt}`: flat Stage 1 filter flow.
- `reports/filtering/static/model_filter_report.latest.txt`: concise Stage 1 filter summary.
- `reports/filtering/static/tables/*.latest.tsv`: run inventory plus grouped breakdown tables by model, benchmark, dataset slice, and scenario.
- `reports/filtering/machine/model_filter_inventory.latest.json`: latest copied inventory inside the report bundle for later plot iteration.
- `reports/filtering/analysis/*`: secondary analysis artifacts rebuilt from the saved inventory, including coverage fractions, chosen-vs-not-chosen explanations, and grouped candidate summaries.
- `reports/filtering/.history/`: stamped history hidden from the main browsing surface.
- `reports/filtering/reproduce.latest.sh`: rerun the reporting step.
- `reports/filtering/rebuild_analysis.latest.sh`: rebuild the filter plots/tables from the saved inventory only.

If you already have a report bundle, you can rerun just the analysis layer with:

```bash
bash reports/filtering/rebuild_analysis.latest.sh
```

For the richer secondary analysis, also inspect:
- `reports/filtering/analysis/interactive/sankey_hierarchical_filter_path.latest.html`: cumulative eligibility funnel from all discovered runs to the selected subset.
- `reports/filtering/analysis/static/filter_candidate_analysis.latest.{txt,md}`: narrative summary with coverage fractions and exclusion examples.
- `reports/filtering/analysis/static/tables/*.latest.tsv`: grouped tables by model, benchmark, dataset slice, scenario, and exclusion reason.

**Example Filter Report Sankey:**
- Shows all runs entering from the left
- Splits by filter_reason (structurally-incomplete, not-text-like, too-large, not-open-access, no-hf-deployment, selected)
- Flows to outcome (selected → next stage, excluded → end)

---

## Stage 2: Generate Experiment Manifests

**Purpose:** Convert a stored run selection into a kwdagger-ready experiment manifest without writing generated YAML back into the repo.

**Command:**
```bash
eval-audit-make-manifest historic \
  --run-specs-fpath "$AUDIT_STORE_ROOT/configs/run_specs.yaml" \
  --run-details-fpath "$AUDIT_STORE_ROOT/configs/run_details.yaml" \
  --experiment-name audit-historic-grid \
  --suite audit-historic-grid \
  --output "$AUDIT_STORE_ROOT/configs/manifests/historic_grid.generated.yaml"
```

**Key Arguments:**
- `historic`: use the historic-selection manifest builder
- `--run-specs-fpath`: YAML list from Stage 1
- `--run-details-fpath`: optional detailed Stage 1 metadata used for selection sidecars
- `--experiment-name`: label for this batch
- `--suite`: suite name recorded into the manifest
- `--output`: write the generated manifest into the audit store
- `--selection-output`: optional explicit path for the generated selection sidecar; defaults next to the manifest as `*.selection.yaml`

**Key Behavior:**
- Automatically selects the repo-checked-in `configs/debug/repro_model_overrides.yaml` when the chosen runs need a local deployment override.
- Writes one batch manifest plus a machine-readable selection sidecar summarizing the filtered entries that went into it.

**Outputs:**
- `$AUDIT_STORE_ROOT/configs/manifests/historic_grid.generated.yaml`
- `$AUDIT_STORE_ROOT/configs/manifests/historic_grid.generated.yaml.selection.yaml`

---

## Stage 3: Execute Runs on Target Machines

**Purpose:** Preview or execute a kwdagger schedule from one manifest at a time.

**Command (preview mode — default):**
```bash
eval-audit-run \
  "$AUDIT_STORE_ROOT/configs/manifests/historic_grid.generated.yaml"
```

**Command (execute mode):**
```bash
eval-audit-run \
  "$AUDIT_STORE_ROOT/configs/manifests/historic_grid.generated.yaml" \
  --run 1
```

**Key Behavior:**
- **Preview mode** (default, `--run 0`): prints the kwdagger invocation and resolved manifest context without executing.
- **Execute mode** (`--run 1`): actually submits the schedule.
- Uses `kwdagger` as the task scheduler for multi-GPU/multi-machine execution.
- Supports overrides like `--root-dpath`, `--devices`, `--tmux-workers`, `--backend`, and `--queue-name` at invocation time.

**Machine-Specific Considerations:**
- Each target machine (aiq-gpu, namek, yardrat, etc.) may have different hardware (GPU type, memory, CPU cores).
- Large models (72B) may only fit on aiq-gpu; smaller models (7B) may run on namek/yardrat.
- Failed runs due to GPU out-of-memory, data unavailability, etc., are recorded in logs.

**Outputs:**
- Per-run job directories: `<experiment_name>/<run_spec_name>/...` containing:
  - `run_spec.json`: The canonical run spec used.
  - `scenario_state.json`: Frozen scenario inputs.
  - `stats.json`: Aggregated metrics.
  - `per_instance_stats.json`: Per-instance metric breakdown.
  - `helm-run.log`: Execution log (captured stderr/stdout).

**Sync Back to Analysis Host:**
```bash
rsync -avz --progress user@<gpu_host>:results/ /home/joncrall/data/helm_runs/
```

---

## Stage 4: Build Result Index

**Purpose:** Scan executed audit results and write timestamped indexes into the audit store, not into the repo.

**Command:**
```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"

eval-audit-index \
  --results-root /data/crfm-helm-audit \
  --report-dpath "$AUDIT_STORE_ROOT/indexes"
```

**Key Arguments:**
- `--results-root`: root containing kwdagger job directories and materialized HELM outputs
- `--report-dpath`: destination directory for timestamped index artifacts; default is `$AUDIT_STORE_ROOT/indexes`

**Outputs:**
- `$AUDIT_STORE_ROOT/indexes/audit_results_index_*.csv`: Central join table with columns:
  - `experiment_name`, `job_id`, `run_entry`: core identifiers
  - `status`: completed, reused, unknown, failed
  - `has_run_spec`, `has_stats`, `has_per_instance_stats`: boolean flags
  - `run_dir`: path to run output directory
  - `machine_host`, `benchmark`, `suite`, `model`: categorization
  - Plus failure reason columns if status != completed

---

## Stage 5: Per-Run Reproducibility Analysis

**Purpose:** Compare pairs of runs (e.g., official vs. local, repeat vs. local) and compute core metric agreement at multiple tolerance thresholds.

If you already have run outputs under `/data/crfm-helm-audit` and want to rebuild the full reporting stack from existing data, the practical path is:
1. rebuild the index in `$AUDIT_STORE_ROOT/indexes`
2. rebuild experiment-level core reports with `eval-audit-analyze-experiment`
3. rebuild the aggregate summary from those Stage 5 outputs

### Analysis-Only Rebuild From Existing Data

Use this when the benchmark runs already exist on disk and you want to regenerate the current analysis products without rerunning HELM:

```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
export AUDIT_RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
export EXPERIMENT_NAME="${EXPERIMENT_NAME:-audit-historic-grid}"

eval-audit-index \
  --results-root "$AUDIT_RESULTS_ROOT" \
  --report-dpath "$AUDIT_STORE_ROOT/indexes"

eval-audit-analyze-experiment \
  --experiment-name "$EXPERIMENT_NAME" \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --allow-single-repeat

python -m eval_audit.workflows.build_reports_summary \
  --experiment-name "$EXPERIMENT_NAME" \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --filter-inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

That sequence rebuilds:
- the latest timestamped index under `$AUDIT_STORE_ROOT/indexes/`
- per-run core reports under `reports/core-run-analysis/experiment-analysis-<slug>/`
- aggregate summary views under `reports/aggregate-summary/`

### Analysis-Only Rebuild For Every Experiment In The Latest Index

Use this when you want to rebuild Stage 5b for every experiment currently present in the latest index, then refresh the all-results aggregate summary:

```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
export AUDIT_RESULTS_ROOT="${AUDIT_RESULTS_ROOT:-/data/crfm-helm-audit}"
export PYTHON_BIN="${PYTHON_BIN:-/home/agent/.local/uv/envs/uvpy3.13.2/bin/python}"

eval-audit-index \
  --results-root "$AUDIT_RESULTS_ROOT" \
  --report-dpath "$AUDIT_STORE_ROOT/indexes"

LATEST_INDEX="$(
  ls -1 "$AUDIT_STORE_ROOT"/indexes/audit_results_index_*.csv | sort | tail -n 1
)"

for EXPERIMENT_NAME in $(
  "$PYTHON_BIN" - <<'PY'
import csv
import glob
import os

paths = sorted(glob.glob(os.path.join(os.environ["AUDIT_STORE_ROOT"], "indexes", "audit_results_index_*.csv")))
latest = paths[-1]
names = []
with open(latest, newline="") as file:
    for row in csv.DictReader(file):
        name = (row.get("experiment_name") or "").strip()
        if name and name not in names:
            names.append(name)
print(" ".join(names))
PY
)
do
  eval-audit-analyze-experiment \
    --experiment-name "$EXPERIMENT_NAME" \
    --index-fpath "$LATEST_INDEX" \
    --allow-single-repeat
done

python -m eval_audit.workflows.build_reports_summary \
  --index-fpath "$LATEST_INDEX" \
  --filter-inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

### 5a. Rebuild/Analyze Core Metrics

**Command:**
```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
eval-audit-rebuild-core \
  --run-entry "boolq:model=eleutherai/pythia-6.9b,data_augmentation=canonical" \
  --experiment-name audit-historic-grid \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --report-dpath reports/core-run-analysis/manual/core-metrics-boolq-pythia
```

**Outputs:**
- `reports/core-run-analysis/manual/core-metrics-<slug>/core_metric_report.latest.json`: Full reproducibility metrics
  - `pairs`: list of pair comparisons (left, right, optional cross-machine)
  - Each pair includes:
    - `agreement_vs_abs_tol`: list of `{abs_tol, agree_ratio}` at 13 thresholds (0 to 1.0)
    - `per_metric_agreement`: dict mapping metric name → agreement curve (NEW)
    - `instance_level` and `run_level` quantile distributions
    - `diagnosis`: mismatch reason classification

### 5b. Analyze by Experiment

**Command:**
```bash

export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
eval-audit-analyze-experiment \
  --experiment-name audit-historic-grid \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --allow-single-repeat
```

**Outputs:**
- `reports/core-run-analysis/experiment-analysis-<slug>/`: Directory tree with:
  - `core-reports/`: One per run entry, containing `core_metric_report.latest.json`
  - `experiment_summary.latest.csv`: Cross-run summary table
  - `cross-machine-aiq-gpu/`: Optional pair reports comparing aiq-gpu vs. other machines
  - `reproduce.latest.sh`: rerun the experiment-level report generation

---

## Stage 6: Aggregate Summary & Reporting

**Purpose:** Load all per-run reports, synthesize findings into operator-facing views, and generate publication-ready artifacts.

This stage is safe to rerun while `eval-audit-analyze-experiment` is still in progress. It snapshots whatever Stage 5 reports currently exist on disk, so intermediate rebuilds are useful. Runs that have completed execution but are not yet analyzed will appear in the high-level summaries as `completed_not_yet_analyzed` or `not_analyzed_yet`, then move downstream on the next rebuild.

**Command:**
```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
python -m eval_audit.workflows.build_reports_summary \
  --experiment-name audit-historic-grid \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --filter-inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

**Key Arguments:**
- `--experiment-name`: optional drill-down for a single experiment; omit for the default `all-results` scope.
- `--filter-inventory-json`: optional Stage 1 inventory used for the end-to-end Sankey family; defaults to `$AUDIT_STORE_ROOT/analysis/filter_inventory.json` when present.
- `--summary-root`: override the aggregate report family root (default: `reports/aggregate-summary`).
- `--breakdown-dims`: optional list of breakdown dimensions to materialize.

**Pipeline Inside build_reports_summary:**

1. **Load all reproducibility rows** from `reports/core-run-analysis/experiment-analysis-*/core-reports/*/core_metric_report.latest.json`
2. **Build enriched rows** (job-level metadata + reproducibility status)
3. **Emit end-to-end Sankey diagrams** linking:
   - all discovered historic HELM runs
   - Stage 1 filter pool and final filter outcome
   - execution coverage in the current scope
   - analysis coverage
   - reproducibility bucket at the chosen tolerance
4. **Emit additional Sankey diagrams focused on execution and reproducibility only:**
   - `sankey_operational.{html,jpg}`: Full pipeline (group → lifecycle → outcome)
   - `sankey_end_to_end.{html,jpg}`: Stage 1 filtering through exact-match reproducibility (`abs_tol=0`)
   - `sankey_end_to_end_tol001.{html,jpg}`: same end-to-end flow at `abs_tol=0.001`
   - `sankey_end_to_end_tol010.{html,jpg}`: same end-to-end flow at `abs_tol=0.01`
   - `sankey_end_to_end_tol050.{html,jpg}`: same end-to-end flow at `abs_tol=0.05`
   - `sankey_reproducibility.{html,jpg}`: Analyzed jobs only, at `abs_tol=0` (exact match)
   - `sankey_repro_tol001.{html,jpg}`: at `abs_tol=0.001`
   - `sankey_repro_tol010.{html,jpg}`: at `abs_tol=0.01`
   - `sankey_repro_tol050.{html,jpg}`: at `abs_tol=0.05`
   - `sankey_repro_by_metric.{html,jpg}`: Per-metric drift breakdown (run-level max delta)
5. **Emit four diagnostic plots:**
   - `benchmark_status.{html,jpg}`: Coverage by benchmark and analysis status
   - `reproducibility_buckets.{html,jpg}`: Distribution across agreement buckets
   - `agreement_curve.{html,jpg}`: Agreement ratio vs. tolerance (all runs)
   - `agreement_curve_per_metric.{html,jpg}`: Agreement per metric (NEW; one subplot per metric)
   - `coverage_matrix.{html,jpg}`: Model × Benchmark heatmap (best status across runs)
   - `failure_taxonomy.{html,jpg}`: Root-cause breakdown (hardware / data access / infra / unknown)
6. **Generate breakdown dimensions** (5 default: experiment_name, model, benchmark, suite, machine_host)
   - For each dimension value, create a subscope with tables only (no visuals)
   - Recursively nest: level_002 → breakdowns → by_<dim> → <value> → level_001 (tables) → level_002 (drill)
7. **Write READMEs** with:
   - Executive summary (counts, key takeaways)
   - Artifact directory structure
   - Links to all plots and tables
8. **Create symlinks** (`*.latest.*`) for easy access at scope root
9. **Write `reproduce.latest.sh`** so aggregate views can be regenerated independently of rerunning experiments

**Output Structure:**
```
reports/
  aggregate-summary/
    all-results/
      README.latest.txt           ← start here
      reproduce.latest.sh         ← rerun just the aggregate summary
      level_001.latest/           → symlink to versioned level_001
      level_002.latest/           → symlink to versioned level_002
      *.latest.html / *.latest.jpg ← symlinks to interactive/static
      .history/
        20260404/
          20260404T033318Z/
            level_001/
              machine/            ← JSON data
              interactive/        ← HTML plots
              static/             ← JPG/PNG/TXT/CSV
              next_level -> ../level_002
            level_002/
              breakdowns/
                by_benchmark/
                by_experiment_name/
                by_model/
                by_suite/
                by_machine_host/
              up_level -> ../level_001
              static/
```

### Stage 6a: Rebuild Aggregate Plots/Tables Only

Use this when you already have Stage 5 reports and want to iterate on directory structure, aggregate tables, or Plotly/Sankey outputs:

```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
PYTHONPATH=. python -m eval_audit.workflows.build_reports_summary \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --filter-inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"
```

This step is independent of recomputing model executions. It only reads existing Stage 5 reports from `reports/core-run-analysis/experiment-analysis-*/`.

**JPG Sidecar Generation:**
- Every `.html` plot is rendered to a `.jpg` sidecar UNLESS:
  - `HELM_AUDIT_SKIP_PLOTLY=1` (skip all plotly rendering)
  - `HELM_AUDIT_SKIP_STATIC_IMAGES=1` (skip JPG, but render HTML)
  - Chrome/Kaleido not found (graceful degradation; `plotly_error` recorded)
  - Data is empty (no plot to render)

**Note on `agreement_curve_per_metric.{html,jpg}`:**
- Requires `per_metric_agreement` data in individual core-metric reports (Stage 5a).
- If re-running Stage 6 with existing Stage 5a reports that were generated BEFORE `per_metric_agreement` was added to the code:
  - Both HTML and JPG will be missing (data not available)
  - Re-running Stage 5a will populate `per_metric_agreement`
  - Re-running Stage 6 will then generate the plots

---

## End-to-End Runbook

### Scenario: Reproduce Qwen models on multiple machines

```bash
export AUDIT_STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"

# Stage 1: Discover & filter
python -m eval_audit.cli.index_historic_helm_runs \
  /data/crfm-helm-public \
  --out_fpath "$AUDIT_STORE_ROOT/configs/qwen_run_specs.yaml" \
  --out_inventory_json "$AUDIT_STORE_ROOT/analysis/qwen_filter_inventory.json"

# Stage 1a: Build filter analysis from saved inventory
python -m eval_audit.cli.reports filter \
  --report-dpath reports/filtering/qwen \
  --inventory-json "$AUDIT_STORE_ROOT/analysis/qwen_filter_inventory.json"

# Stage 2: Generate a stored manifest
eval-audit-make-manifest historic \
  --run-specs-fpath "$AUDIT_STORE_ROOT/configs/qwen_run_specs.yaml" \
  --experiment-name audit-qwen25-7b \
  --suite audit-qwen25-7b \
  --output "$AUDIT_STORE_ROOT/configs/manifests/qwen.generated.yaml"

# Stage 3: Execute (preview first, then run)
eval-audit-run "$AUDIT_STORE_ROOT/configs/manifests/qwen.generated.yaml"

# When ready to execute:
eval-audit-run "$AUDIT_STORE_ROOT/configs/manifests/qwen.generated.yaml" --run 1

# Sync results back to analysis host (run on GPU host or via CI/CD)
rsync -avz --progress results/ /home/joncrall/data/helm_runs/

# Stage 4: Index results
eval-audit-index \
  --results-root /data/crfm-helm-audit \
  --report-dpath "$AUDIT_STORE_ROOT/indexes"

# Stage 5: Analyze per-run reproducibility
eval-audit-analyze-experiment \
  --experiment-name audit-qwen25-7b \
  --index-dpath "$AUDIT_STORE_ROOT/indexes"

# Stage 6: Build aggregate reports
python -m eval_audit.workflows.build_reports_summary \
  --index-dpath "$AUDIT_STORE_ROOT/indexes" \
  --filter-inventory-json "$AUDIT_STORE_ROOT/analysis/filter_inventory.json"

# Open reports
firefox reports/aggregate-summary/all-results/README.latest.txt
firefox reports/aggregate-summary/all-results/sankey_operational.latest.html
firefox reports/aggregate-summary/all-results/agreement_curve.latest.html
```

---

## Troubleshooting

### "Only 240/469 jobs have run artifacts" — why?

See the filter report Sankey from Stage 1:
- How many were structurally incomplete (missing files)?
- How many were filtered by model eligibility?
- How many made it to execution but failed (see Stage 3 logs)?

Open `reports/filtering/interactive/sankey_model_filter.latest.html` to visualize the breakdown.

### "agreement_curve_per_metric.html is missing"

This happens if re-running Stage 6 with Stage 5a reports generated BEFORE `per_metric_agreement` was added to the code. Solution: re-run Stage 5a to repopulate reports, then re-run Stage 6.

### "Chrome not found" for JPG rendering

Searched paths:
- `.cache/plotly-chrome/chrome-linux64/chrome`
- `~/.plotly/chrome/chrome-linux64/chrome`
- `<choreographer-package>/chrome-linux64/chrome`

On this repo's headless Ubuntu 24.04 workflow, use:

```bash
uv pip install -e .
bash reproduce/setup/10_install_plotly_chrome_ubuntu2404.sh
PYTHONPATH=. python -m eval_audit.cli.check_env --plotly-static-only
```

The installer downloads Chrome into the repo-local cache at `.cache/plotly-chrome/`, which is the first location searched by the shared Plotly helper. If Chrome is still absent, HTMLs will render and JPG/PNG sidecars will be skipped with `plotly_error` recorded in the generated report metadata.

---

## References

- **HELM Public Data:** https://github.com/stanford-crfm/helm (benchmark definitions, model registry)
- **kwdagger Documentation:** `aiq-magnet` repo
- **plotly Rendering:** https://plotly.com/python/static-image-export/
- **HELM Manifest Format:** https://github.com/stanford-crfm/helm/blob/main/README.md
