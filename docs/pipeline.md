# HELM Audit End-to-End Pipeline

This document covers the complete reproducibility audit pipeline: from discovering historic HELM runs through aggregating final reports.

**Quick Links:**
- Operator runbook: [`reproduce/README.md`](../reproduce/README.md)
- Package reference: [`README.md`](../README.md)
- Design journal: [`dev/journals/codex.md`](../dev/journals/codex.md)

---

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

# Optional: plotly rendering (Chrome/Kaleido for JPG sidecar generation)
# Chrome is searched at: ~/.plotly/chrome/ or via choreographer package
```

---

## Stage 1: Discover & Filter Historic HELM Runs

**Purpose:** Index all historic HELM run outputs from a CRFM public or private data source, apply eligibility filters, and emit a reproducible list.

**Command:**
```bash
python -m helm_audit.cli.index_historic_helm_runs \
  /data/crfm-helm-public \
  --out_fpath run_specs.yaml \
  --out_detail_fpath run_details.yaml \
  --out_report_dpath filter_report
```

**Key Arguments:**
- `roots` (positional): One or more directories containing HELM `benchmark_output/` subdirectories.
- `--suite_pattern`: Glob pattern for suite selection (default: `*` = all).
- `--run_pattern`: Glob pattern for run selection within each suite (default: `*:*` = HELM run format).
- `--require_per_instance_stats`: If True, only include runs with `per_instance_stats.json` (slow; default False).
- `--include_max_eval_instances`: If True, infer `max_eval_instances` from per-instance data (slow; default False).
- `--out_fpath`: Write `run_spec_name` list as YAML (fed to kwdagger scheduler).
- `--out_detail_fpath`: Write full row data with all metadata as YAML.
- `--out_report_dpath` (new): Write filter-step analysis: Sankey diagram + text report showing what was excluded and why.
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
- `run_specs.yaml`: List of selected `run_spec_name` strings (one per line), ready to feed into kwdagger.
- `run_details.yaml`: Full dict rows with model, scenario_class, max_eval_instances, etc.
- `filter_report/sankey_*.{html,jpg,txt}`: Sankey showing run flow: `filter_reason → outcome`.
- `filter_report/model_filter_report.txt`: Text summary of filter statistics.

**Example Filter Report Sankey:**
- Shows all runs entering from the left
- Splits by filter_reason (structurally-incomplete, not-text-like, too-large, not-open-access, no-hf-deployment, selected)
- Flows to outcome (selected → next stage, excluded → end)

---

## Stage 2: Generate Experiment Manifests

**Purpose:** Convert a list of historic run specs into experiment manifests suitable for scheduling on specific machines, with optional model deployment overrides.

**Command:**
```bash
helm-audit-make-manifest \
  --index_fpath audit_results_index_20260404.csv \
  --run_entries_fpath run_specs.yaml \
  --experiment_name audit-historic-grid \
  --out_dpath manifests
```

**Key Arguments:**
- `--index_fpath`: CSV index from `helm-audit-index` (if re-running; optional for first run).
- `--run_entries_fpath`: YAML list from Stage 1 `--out_fpath`.
- `--experiment_name`: Label for this batch (e.g., `audit-historic-grid`, `audit-qwen25-7b-aiq`).
- `--out_dpath`: Write manifests here (one per run spec).

**Key Behavior:**
- Automatically selects `model_deployments.yaml` override file if the run's model appears in known overrides (e.g., Qwen models → local HF instead of Together).
- Creates one YAML manifest per unique run spec, with fully resolved model name and scenario parameters.

**Outputs:**
- `manifests/<run_spec_slug>.yaml`: HELM manifest, ready to execute.

---

## Stage 3: Execute Runs on Target Machines

**Purpose:** Schedule and execute manifests across one or more machines (GPU clusters, single hosts, etc.), capturing outputs and logs.

**Command (preview mode — default):**
```bash
helm-audit-run \
  --experiment_name audit-historic-grid \
  --manifests_dpath manifests \
  --max_jobs 50
```

**Command (execute mode):**
```bash
helm-audit-run \
  --experiment_name audit-historic-grid \
  --manifests_dpath manifests \
  --max_jobs 50 \
  --run 1
```

**Key Behavior:**
- **Preview mode** (default, `--run 0`): Prints what kwdagger would schedule, but doesn't execute.
- **Execute mode** (`--run 1`): Actually submits jobs to kwdagger.
- Uses `kwdagger` as the task scheduler for multi-GPU/multi-machine execution.
- Respects `--max_jobs` to limit concurrent jobs per machine.

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

**Purpose:** Scan the executed runs and create a master CSV index mapping job metadata to run outputs.

**Command:**
```bash
helm-audit-index \
  --root_dpaths /home/joncrall/data/helm_runs \
  --out_fpath audit_results_index_20260404.csv \
  --experiment_names audit-historic-grid
```

**Key Arguments:**
- `--root_dpaths`: Directories containing run output subdirectories.
- `--out_fpath`: Write index CSV here.
- `--experiment_names`: Filter to specific experiments (optional).

**Outputs:**
- `audit_results_index_*.csv`: Central join table with columns:
  - `experiment_name`, `run_entry`, `run_spec_name`: ID triple
  - `status`: completed, reused, unknown, failed
  - `has_run_spec`, `has_stats`, `has_per_instance_stats`: boolean flags
  - `run_dir`: path to run output directory
  - `machine_host`, `benchmark`, `suite`, `model`: categorization
  - Plus failure reason columns if status != completed

---

## Stage 5: Per-Run Reproducibility Analysis

**Purpose:** Compare pairs of runs (e.g., official vs. local, repeat vs. local) and compute core metric agreement at multiple tolerance thresholds.

### 5a. Rebuild/Analyze Core Metrics

**Command:**
```bash
helm-audit-rebuild-core \
  --left_run_a <official_run_dir> \
  --left_run_b <local_run_dir> \
  --right_run_a <local_run_dir> \
  --right_run_b <repeat_run_dir> \
  --report_dpath reports/core-metrics-<slug>
```

**Outputs:**
- `reports/core-metrics-<slug>/core_metric_report.latest.json`: Full reproducibility metrics
  - `pairs`: list of pair comparisons (left, right, optional cross-machine)
  - Each pair includes:
    - `agreement_vs_abs_tol`: list of `{abs_tol, agree_ratio}` at 13 thresholds (0 to 1.0)
    - `per_metric_agreement`: dict mapping metric name → agreement curve (NEW)
    - `instance_level` and `run_level` quantile distributions
    - `diagnosis`: mismatch reason classification

### 5b. Analyze by Experiment

**Command:**
```bash
helm-audit-analyze-experiment \
  --experiment_name audit-historic-grid \
  --index_fpath audit_results_index_20260404.csv
```

**Outputs:**
- `experiment-analysis-<slug>/`: Directory tree with:
  - `core-reports/`: One per run entry, containing `core_metric_report.latest.json`
  - `experiment_summary.latest.csv`: Cross-run summary table
  - `cross-machine-aiq-gpu/`: Optional pair reports comparing aiq-gpu vs. other machines

---

## Stage 6: Aggregate Summary & Reporting

**Purpose:** Load all per-run reports, synthesize findings into operator-facing views, and generate publication-ready artifacts.

**Command:**
```bash
python -m helm_audit.workflows.build_reports_summary \
  --scope all_results \
  --include_visuals 1
```

**Key Arguments:**
- `--scope`: one of `all_results` (default), or a specific `experiment_name` slug for drill-down.
- `--include_visuals`: If 1, render all Sankey and Plotly charts; if 0, tables only.

**Pipeline Inside build_reports_summary:**

1. **Load all reproducibility rows** from `experiment-analysis-*/core-reports/*/core_metric_report.latest.json`
2. **Build enriched rows** (job-level metadata + reproducibility status)
3. **Emit six Sankey diagrams:**
   - `sankey_operational.{html,jpg}`: Full pipeline (group → lifecycle → outcome)
   - `sankey_reproducibility.{html,jpg}`: Analyzed jobs only, at `abs_tol=0` (exact match)
   - `sankey_repro_tol001.{html,jpg}`: at `abs_tol=0.001`
   - `sankey_repro_tol010.{html,jpg}`: at `abs_tol=0.01`
   - `sankey_repro_tol050.{html,jpg}`: at `abs_tol=0.05`
   - `sankey_repro_by_metric.{html,jpg}`: Per-metric drift breakdown (run-level max delta)
4. **Emit four diagnostic plots:**
   - `benchmark_status.{html,jpg}`: Coverage by benchmark and analysis status
   - `reproducibility_buckets.{html,jpg}`: Distribution across agreement buckets
   - `agreement_curve.{html,jpg}`: Agreement ratio vs. tolerance (all runs)
   - `agreement_curve_per_metric.{html,jpg}`: Agreement per metric (NEW; one subplot per metric)
   - `coverage_matrix.{html,jpg}`: Model × Benchmark heatmap (best status across runs)
   - `failure_taxonomy.{html,jpg}`: Root-cause breakdown (hardware / data access / infra / unknown)
5. **Generate breakdown dimensions** (5 default: experiment_name, model, benchmark, suite, machine_host)
   - For each dimension value, create a subscope with tables only (no visuals)
   - Recursively nest: level_002 → breakdowns → by_<dim> → <value> → level_001 (tables) → level_002 (drill)
6. **Write READMEs** with:
   - Executive summary (counts, key takeaways)
   - Artifact directory structure
   - Links to all plots and tables
7. **Create symlinks** (`*.latest.*`) for easy access at scope root

**Output Structure:**
```
reports-summary/
  all-results/
    README.latest.txt           ← start here
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
# Stage 1: Discover & filter
python -m helm_audit.cli.index_historic_helm_runs \
  /data/crfm-helm-public \
  --out_fpath qwen_run_specs.yaml \
  --out_report_dpath filter_report_qwen

# Stage 2: Generate manifests
helm-audit-make-manifest \
  --run_entries_fpath qwen_run_specs.yaml \
  --experiment_name audit-qwen25-7b \
  --out_dpath manifests_qwen

# Stage 3: Execute (preview first, then run)
helm-audit-run \
  --experiment_name audit-qwen25-7b \
  --manifests_dpath manifests_qwen \
  --max_jobs 50

# When ready to execute:
helm-audit-run \
  --experiment_name audit-qwen25-7b \
  --manifests_dpath manifests_qwen \
  --max_jobs 50 \
  --run 1

# Sync results back to analysis host (run on GPU host or via CI/CD)
rsync -avz --progress results/ /home/joncrall/data/helm_runs/

# Stage 4: Index results
helm-audit-index \
  --root_dpaths /home/joncrall/data/helm_runs \
  --out_fpath audit_results_index_qwen.csv \
  --experiment_names audit-qwen25-7b

# Stage 5: Analyze per-run reproducibility
helm-audit-analyze-experiment \
  --experiment_name audit-qwen25-7b \
  --index_fpath audit_results_index_qwen.csv

# Stage 6: Build aggregate reports
python -m helm_audit.workflows.build_reports_summary

# Open reports
firefox reports-summary/all-results/README.latest.txt
firefox reports-summary/all-results/sankey_operational.latest.html
firefox reports-summary/all-results/agreement_curve.latest.html
```

---

## Troubleshooting

### "Only 240/469 jobs have run artifacts" — why?

See the filter report Sankey from Stage 1:
- How many were structurally incomplete (missing files)?
- How many were filtered by model eligibility?
- How many made it to execution but failed (see Stage 3 logs)?

Open `filter_report/sankey_model_filter.latest.html` to visualize the breakdown.

### "agreement_curve_per_metric.html is missing"

This happens if re-running Stage 6 with Stage 5a reports generated BEFORE `per_metric_agreement` was added to the code. Solution: re-run Stage 5a to repopulate reports, then re-run Stage 6.

### "Chrome not found" for JPG rendering

Searched paths:
- `~/.plotly/chrome/chrome-linux64/chrome`
- `<choreographer-package>/chrome-linux64/chrome`

Either install Kaleido (`pip install kaleido`) or download Chrome to one of these paths. HTMLs will render; JPGs will be skipped.

---

## References

- **HELM Public Data:** https://github.com/stanford-crfm/helm (benchmark definitions, model registry)
- **kwdagger Documentation:** `aiq-magnet` repo
- **plotly Rendering:** https://plotly.com/python/static-image-export/
- **HELM Manifest Format:** https://github.com/stanford-crfm/helm/blob/main/README.md
