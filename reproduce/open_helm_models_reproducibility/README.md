# Open HELM Models — Reproducibility Case Study

A virtual experiment that asks: *how reproducible are the open-weight
models in the public HELM corpus, given the local audit results we
currently have?*

The manifest is checked in at
[`configs/virtual-experiments/open-helm-models-reproducibility.yaml`](../../../home/joncrall/code/helm_audit/configs/virtual-experiments/open-helm-models-reproducibility.yaml).
It declares scope as a regex over five open-weight model families, and
combines the audit-results-index + the public-HELM-index +
the Stage-1 filter inventory as a `pre_filter` so Sankey A renders the
full Universe → Stage-1 gates → manifest scope → selected funnel.

## Scope

Five open-weight model families that we have local audit reproductions
for AND that have at least one matching public HELM row:

| model                            | benchmarks with overlap | unique keys |
| -------------------------------- | ----------------------: | ----------: |
| `eleutherai/pythia-2.8b-v0`      |                       2 |           3 |
| `eleutherai/pythia-6.9b`         |                      14 |          41 |
| `lmsys/vicuna-7b-v1.3`           |                      14 |          44 |
| `qwen/qwen2.5-7b-instruct-turbo` |                       7 |          24 |
| `openai/gpt-oss-20b`             |                       2 |           2 |

Total potential reproducibility cases: 39 (model, benchmark) pairs over
19 unique benchmarks; ~114 unique logical run-spec keys.

## What this assumes

- The local audit-results index at `$AUDIT_STORE_ROOT/indexes/audit_results_index.csv`
- The official-public-HELM index at `$AUDIT_STORE_ROOT/indexes/official_public_index.csv`
- The Stage-1 filter inventory at `$AUDIT_STORE_ROOT/analysis/filter_inventory.json`
- All EEE conversions (official sweep + local on-demand) are produced
  on the fly via `--ensure-local-eee`.

## Steps

```
./compose.sh           # filter sources, run analyze_experiment per packet,
                       # compute Stage-B coverage, write scoped filter inventory
./build_summary.sh     # build aggregate publication surface (sankey A + B + s05,
                       # prioritized examples, per-metric drift)
```

## Output layout

```
$AUDIT_STORE_ROOT/virtual-experiments/open-helm-models-reproducibility/
├── manifest.yaml
├── provenance.json
├── scoped_filter_inventory.json
├── indexes/
├── analysis/
│   ├── planning/
│   ├── core-reports/<one per packet>/
│   ├── experiment_summary.{json,csv,txt}
│   └── reproduce.sh
└── reports/
    ├── scoped_funnel/                       (Stage-B coverage funnel)
    │   ├── coverage_funnel_summary.txt
    │   ├── coverage_funnel.json
    │   ├── missing_targets.csv
    │   ├── coverage_by_<dim>.csv
    │   └── sankey_b_scope_to_analyzed.{html,jpg,txt}
    └── aggregate-summary/<scope>/           (publication surface)
        ├── sankey_a_universe_to_scope.{html,jpg,txt}
        ├── sankey_b_scope_to_analyzed.{html,jpg,txt}
        ├── sankey_s05_reproducibility.{html,jpg,txt}
        ├── prioritized_examples.latest/{score_ge_95,best,mid,worst,score_lt_80,flagged}/
        ├── agreement_curve.html
        ├── coverage_matrix.html
        └── README.txt
```
