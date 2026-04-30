# EEE-only reproducibility heatmap (3 models × 14 benchmarks)

Produces a model × benchmark heatmap of instance-level agreement between
official public HELM evaluations and local open-weight reproductions,
using the EEE (Every Eval Ever) artifact format throughout. No HELM run
dirs, no GPU, no internet access required at report time.

## Scope

| Axis | Values |
|---|---|
| **Models** | `eleutherai/pythia-2.8b-v0`, `eleutherai/pythia-6.9b`, `lmsys/vicuna-7b-v1.3` |
| **Benchmarks** | boolq, civil_comments, entity_data_imputation, entity_matching, gsm, imdb, lsat_qa, mmlu, narrativeqa, quac, synthetic_reasoning, sythetic_reasoning_natural, truthful_qa, wikifact |
| **Official source** | `crfm-helm-public-eee-test/classic/v0.2.4` (pythia-2.8b only) and `v0.3.0` (pythia-6.9b + vicuna) |
| **Local source** | `eee/local/open-helm-models-reproducibility` experiment |
| **Primary threshold** | `abs_tol=1e-9` (between exact-match and 10 pico tolerance) |

Note: `pythia-2.8b-v0` has only `boolq` and `civil_comments` with both
official and local coverage. Other cells for that model are shown as
"no data" (gray) in the heatmap.

Note: benchmark name `sythetic_reasoning_natural` preserves the
original EEE conversion typo present in the stored artifacts.

## Sub-benchmark representatives

For benchmarks with multiple sub-runs, one representative is used per cell:

| Benchmark | Representative |
|---|---|
| `mmlu` | `subject=us_foreign_policy` |
| `civil_comments` | `demographic=all` |
| `entity_data_imputation` | `dataset=Buy` |
| `entity_matching` | `dataset=Abt_Buy` |
| `synthetic_reasoning` | `mode=variable_substitution` |
| `sythetic_reasoning_natural` | `difficulty=easy` |
| `wikifact` | `subject=place_of_birth` |
| `lsat_qa` | `task=all` |
| `truthful_qa` | `task=mc_single` |
| others | single sub-run |

## What this measures

**Instance-level reproducibility fraction**: the fraction of evaluation
instances where `|official_score - local_score| ≤ abs_tol`. Each cell
micro-averages across all matching instance pairs. A value near 1.0
means the local run reproduced the official result almost exactly; a
value near 0.0 indicates systematic disagreement.

Missing cells (gray): either no official public EEE artifact or no local
EEE artifact exists for that (model, benchmark) combination.

## How to run

```bash
# Step 0: verify artifact coverage
bash 00_check_artifacts.sh

# Step 1: build the official/local symlink tree (no bytes copied)
bash 10_link_tree.sh

# Step 2: run eval-audit-from-eee to produce per-pair core_metric_reports
bash 20_run.sh

# Step 3: generate the reproducibility heatmap
bash 30_heatmap.sh
```

All steps are idempotent. Override paths with environment variables:
- `AUDIT_STORE_ROOT` (default: `/data/crfm-helm-audit-store`)
- `OUT_ROOT` (default: `$AUDIT_STORE_ROOT/eee-only-reproducibility-heatmap`)

## Output

```
$OUT_ROOT/
  eee_artifacts/        # symlink tree (official/ + local/)
  from_eee_out/         # per-pair core_metric_report.json files
    <experiment>/core-reports/<packet>/core_metric_report.json
    ...
  heatmap/
    reproducibility_heatmap.png   # the main figure
    reproducibility_heatmap.txt   # text table fallback
    cell_data.json                # raw values per (model, benchmark)
```
