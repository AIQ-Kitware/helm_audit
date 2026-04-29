# eval_audit

`eval_audit` is the workflow around HELM benchmark *audit* runs: indexing
the public HELM corpus, running local reproductions, comparing local vs.
public results at instance and metric level, and writing publication-quality
report bundles.

The recent (2026 Q1–Q2) line of work has been almost entirely on the
**analysis side** — composing virtual-experiment slices over already-existing
audit runs and producing reproducibility reports. The execution side
(`eval-audit-run` → `kwdagger` → `magnet` → `helm-run`) was confirmed working
on **2026-04-28** by [`reproduce/pythia12b_mmlu_smoke/`](reproduce/pythia12b_mmlu_smoke/),
which produced a perfect-agreement local reproduction of pythia-12b-v0 ×
MMLU on aiq-gpu through the HuggingFace transformers path. The serving-stack
extensions (vLLM, KubeAI, LiteLLM) and the `eval-audit-make-manifest` path
have **not** been re-validated; those remain marked **UNSURE** below.

> If you only want the active path, jump to [Analysis runbooks](#analysis-runbooks-actively-maintained).

## What lives where

```
eval_audit/                 the Python package (renamed from helm_audit on 2026-04-28)
├── cli/                    argparse entrypoints — most CLIs are thin wrappers
├── workflows/              end-to-end sequencing (analyze, index, build summary, …)
├── reports/                pair report, core metrics, aggregate summary, paper labels
├── virtual/                virtual-experiment composer (recent, actively maintained)
├── normalized/             normalized comparison layer (EEE-aware)
├── planning/               comparison-intent planner used by core metrics
├── manifests/              manifest builders / presets  [not recently exercised]
├── helm/                   HELM-specific readers + diff helpers (analysis.py, diff.py,
│                           hashers.py, metrics.py, run_entries.py)
├── indexing/               run-spec hash + schema helpers
├── infra/                  paths, env, yaml IO, logging, plotly env
├── integrations/           kwdagger_bridge.py + vllm_service/  [not recently exercised]
├── compat/                 backward-compat shims
└── model_registry.py
```

External directories the workflow depends on:

- `reproduce/` — runbooks; one folder per scenario. Most are execution-shaped
  shell sequences (`00_check_env`, `10_make_manifest`, `20_run`, `30_compare`)
  and are **UNSURE** as of 2026-04. The two **analysis-only** runbooks at
  [`reproduce/pythia_mmlu_stress/`](reproduce/pythia_mmlu_stress/) and
  [`reproduce/open_helm_models_reproducibility/`](reproduce/open_helm_models_reproducibility/)
  *are* known-good — those are what the recent commits exercise.
- `configs/` — checked-in manifests and overrides only; generated state lives
  outside the repo.
- `docs/` — supporting docs. Several are **STALE** and need triage; see
  [Documentation status](#documentation-status) below.
- `reports/` — small generated artifacts that are still useful in-repo
  (`reports/filtering/`, `reports/core-run-analysis/`,
  `reports/aggregate-summary/`).

The big mutable working tree is on the data store, not in the repo:

```
$AUDIT_STORE_ROOT  (default: /data/crfm-helm-audit-store)
├── configs/                    generated run_specs.yaml, manifests/, run_details.yaml
├── indexes/                    audit_results_index_*.csv|jsonl|txt + official index
├── eee/local/<exp>/<run>/      EEE-converted local audit artifacts
├── crfm-helm-public-eee-test/  EEE-converted public HELM corpus (stress sweep)
├── analysis/                   per-experiment analysis (core-reports, eee-readiness, …)
├── virtual-experiments/<exp>/  virtual-experiment composition outputs
└── local-bundles/              per-bundle deployment YAMLs / process_context

$AUDIT_RESULTS_ROOT  (default: /data/crfm-helm-audit)
└── <experiment>/helm/helm_id_<hash>/...   raw local HELM run outputs
```

## Analysis runbooks (actively maintained)

These are what the 2026 Q1–Q2 commits exercise. They consume already-existing
audit runs and produce reproducibility reports. **No model is run; no
benchmark is downloaded.**

```bash
# Pythia × MMLU slice — 5 subjects, 5 packets, 4,536 instances
./reproduce/pythia_mmlu_stress/compose.sh
./reproduce/pythia_mmlu_stress/build_summary.sh

# Wider open-weight × benchmark slice — 121 packets, 431,605 instances
./reproduce/open_helm_models_reproducibility/compose.sh
./reproduce/open_helm_models_reproducibility/build_summary.sh
```

Each runbook is a thin wrapper over `eval-audit-build-virtual-experiment`
and `eval-audit-build-summary`, working from a checked-in YAML manifest at
`configs/virtual-experiments/<name>.yaml`. Outputs land at
`$AUDIT_STORE_ROOT/virtual-experiments/<name>/`.

The corresponding reproducibility narratives are in
[`reproduce/pythia_mmlu_stress/REPRODUCIBILITY_REPORT.md`](reproduce/pythia_mmlu_stress/REPRODUCIBILITY_REPORT.md)
and
[`reproduce/open_helm_models_reproducibility/REPRODUCIBILITY_REPORT.md`](reproduce/open_helm_models_reproducibility/REPRODUCIBILITY_REPORT.md).

The HELM-specific gotchas surfaced while building the comparison pipeline are
catalogued in [`docs/helm-gotchas.md`](docs/helm-gotchas.md) — that file is
current.

## Execution runbooks

These are the original framing of the project: schedule a local HELM run via
`kwdagger`, point HELM at a model deployment (vLLM, KubeAI, LiteLLM, or
HuggingFace), then compare. The core chain
(`eval-audit-run` → `kwdagger` → `magnet` → `helm-run`) was confirmed working
on **2026-04-28** by the [`pythia12b_mmlu_smoke`](reproduce/pythia12b_mmlu_smoke/)
runbook on aiq-gpu — pythia-12b-v0 × MMLU abstract_algebra, 1000 instances,
HELM `huggingface/*` HuggingFaceClient deployment. That run reproduced the
public HELM v0.2.4/v0.3.0 reference *exactly* (1.000 agreement, max |Δ| = 0.0
across all 8 metrics). So the basic execution stack is alive.

The other runbooks bring in additional serving stacks (vLLM, LiteLLM, KubeAI)
and additional scenario-specific assumptions (server URLs, deployment YAML,
namespace setup) that **have not been re-validated**. Pick one, run it, and
update its README before claiming it's still good.

| runbook | what it claims to do | status |
|---|---|---|
| `reproduce/pythia12b_mmlu_smoke/` | pythia-12b-v0 × abstract_algebra via HF transformers + kwdagger | **WORKING** (2026-04-28) |
| `reproduce/pythia_mmlu_stress/` | analysis-only pythia × MMLU slice | **WORKING** (analysis) |
| `reproduce/open_helm_models_reproducibility/` | analysis-only open-weight × benchmark slice | **WORKING** (analysis) |
| `reproduce/eee_only_demo/` | tutorial: pure-EEE comparison via `eval-audit-from-eee` against checked-in 3×3 fixture | **WORKING** (2026-04-29) |
| `reproduce/smoke/` | minimal end-to-end sanity run | **UNSURE** |
| `reproduce/apples/` | apples-to-apples reproduction control | **UNSURE** |
| `reproduce/historic_grid/` | regenerate a historic public-run manifest grid | **UNSURE** |
| `reproduce/machine_compare/` | cross-machine indexing + pairwise comparison | **UNSURE** |
| `reproduce/qwen35_vllm/` | local vLLM smoke for `qwen/qwen3.5-9b` | **UNSURE** (vLLM-side) |
| `reproduce/qwen2_72b_vllm/` | vLLM smoke + EWOK historic grid for qwen2-72b | **UNSURE** (vLLM-side) |
| `reproduce/gpt_oss_20b_vllm/` | LiteLLM-fronted vLLM batch for gpt-oss-20b | **UNSURE** (vLLM/LiteLLM-side) |
| `reproduce/small_models_kubeai/` | KubeAI overnight batch (qwen2.5-7b + vicuna-7b) | **UNSURE** (KubeAI-side) |
| `reproduce/setup/` | one-time host setup scripts | **UNSURE** but harmless |

Re-validating any of these is its own piece of work — the assumptions in
their READMEs (server URLs, KubeAI namespaces, LiteLLM keys, deployment YAML
shape) drift fast. Pick one, run it, and update its README before claiming
it's still good.

## CLI

Entry points are declared in [`pyproject.toml`](pyproject.toml#L42). Active /
dormant breakdown:

**Active (exercised by the analysis runbooks):**

- `eval-audit-build-virtual-experiment` — compose a virtual-experiment slice from a YAML manifest. Source kinds: `audit_index`, `official_public_index` (HELM-driven), `eee_root` (walk an `every_eval_ever` tree), `external_eee` (cherry-pick individual EEE artifacts). All four can mix in one manifest; the planner accepts the synthesized index regardless of artifact format.
- `eval-audit-build-summary` — build the publication surface (sankeys, prioritized examples, coverage matrix, README)
- `eval-audit-analyze-experiment` — per-experiment analysis (delegates to packet planner + core metrics)
- `eval-audit-analyze-many` — batched experiment analysis
- `eval-audit-analyze-index-snapshot` — snapshot the audit-results index
- `eval-audit-rebuild-core` — rebuild the per-packet core metric report
- `eval-audit-report-core` / `eval-audit-report-aggregate` — single-packet and aggregate reporting
- `eval-audit-compare-pair` / `eval-audit-compare-batch` — pair-level comparison
- `eval-audit-index` — build the audit-results index
- `eval-audit-portfolio-status` — multi-experiment status snapshot
- `eval-audit-prepare-eee` — prepare EEE artifacts for downstream analysis
- `eval-audit-from-eee` — **EEE-only tutorial path.** Walks an
  ``official/`` + ``local/`` tree of `every_eval_ever` artifacts, runs the
  planner, renders per-packet core-metric reports, and (with
  ``--build-aggregate-summary``) produces a cross-packet aggregate
  report. Skips Stage-1 filter discovery and the HELM execution chain —
  the inputs *are* the scope. See
  [`reproduce/eee_only_demo/README.md`](reproduce/eee_only_demo/README.md)
  for a worked tutorial against a checked-in 3×3 fixture.
- `eval-audit-compare-pair-eee` — **EEE-only single-pair report.**
  The EEE analogue of `eval-audit-compare-pair`. Takes one official EEE
  artifact and one local EEE artifact, produces the same shape of
  core-metric report ``eval-audit-from-eee`` writes per pair. If you
  ship the original ``run_spec.json`` next to the EEE artifact, the
  HELM-side comparability facts (scenario class, deployment,
  instructions, max_eval_instances) flip from `unknown` to `yes`/`no`.
  See [`docs/eee-vs-helm-metadata.md`](docs/eee-vs-helm-metadata.md)
  for the full HELM↔EEE field mapping and recommendations.

**Execution path (verified 2026-04-28 by `pythia12b_mmlu_smoke`):**

- `eval-audit-check-env` — host-environment preflight (light; works)
- `eval-audit-run` — preview/execute a kwdagger experiment from a manifest
  (default is preview; `--run=1` to execute). End-to-end chain through
  kwdagger → magnet → helm-run is alive.

**UNSURE:**

- `eval-audit-make-manifest` — `historic` and `preset` subcommands read from
  `$STORE_ROOT/configs/run_specs.yaml`; not exercised by the recent runbook
  (which writes its manifest by hand because pythia-12b-v0 was Stage-1
  filtered out). The two subcommands haven't been touched in months.

`eval-audit-run` was originally the scheduling boundary. It still imports
cleanly but its kwdagger and HELM-execution side-effects haven't been
re-tested.

## Install

```bash
uv pip install -e .
```

Then the CLI scripts above are on `$PATH`. For analysis-only work this is
all you need.

For Plotly JPG/PNG sidecars on a headless Ubuntu 24.04 VM, install the Chrome
dependency once with
[`reproduce/setup/10_install_plotly_chrome_ubuntu2404.sh`](reproduce/setup/10_install_plotly_chrome_ubuntu2404.sh)
(also UNSURE — it has not been re-validated on the current images, but it's a
straightforward apt invocation).

## Documentation status

| file | status | note |
|---|---|---|
| [`docs/pipeline.md`](docs/pipeline.md) | **CURRENT** | rewritten 2026-04-28 to match the active EEE-driven analysis pipeline; the prior version is preserved at [`docs/historical/pipeline-pre-eee-refactor.md`](docs/historical/pipeline-pre-eee-refactor.md) |
| [`docs/helm-gotchas.md`](docs/helm-gotchas.md) | **CURRENT** | running ledger of HELM-specific behaviors hit during analysis |
| [`docs/helm-reproduction-research-journal.md`](docs/helm-reproduction-research-journal.md) | **CURRENT** | research context, failure taxonomies |
| [`docs/eee-vs-helm-metadata.md`](docs/eee-vs-helm-metadata.md) | **CURRENT** | what HELM has that EEE doesn't, what `unknown` comparability facts mean, how to ship sidecar metadata so they evaluate normally |
| [`docs/kwdagger-notes.md`](docs/kwdagger-notes.md) | **UNSURE** | small file, may still be accurate |
| [`docs/helm-null-completion-text-patch-proposal.md`](docs/helm-null-completion-text-patch-proposal.md) | **UNSURE** | pre-EEE patch proposal; outcome unclear |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | **PARTIALLY STALE** | core ADRs (raw vs derived, reports/, filesystem-as-interface) still hold; specific module/CLI lists drifted with the rename and recent refactors |

Moved into [`docs/historical/`](docs/historical/) on 2026-04-28 (preserved
verbatim — they may still be useful as records of *how* a problem was
approached at the time):

- `historical/pipeline-pre-eee-refactor.md` — the older end-to-end pipeline doc
- `historical/helm-reproduction-status-checkpoint.md`
- `historical/open-model-helm-reproduction-master-plan.md`
- `historical/reproduce-helm-session-v2.md`
- `historical/helm-reproduction-agent-brief.md`

## Caveats / things to verify before relying on a claim here

- **STALE** annotations above mean "I (the writer of this README on
  2026-04-28) couldn't quickly verify the file was still correct." It does
  not mean the file is wrong — only that nobody has confirmed it isn't.
- The `eval-audit-run` execution path still compiles and imports. It has not
  been *run* in months, so the kwdagger-side, vLLM-side, and
  manifest-building integration-test surface is **unverified**.
- The `crfm-helm-audit-store` and `crfm-helm-audit` data-store paths are
  preserved verbatim from the pre-rename world (HELM-the-benchmark naming);
  see [`docs/helm-gotchas.md`](docs/helm-gotchas.md).
- The `eval_audit_local` source-organization tag is the rename of
  `helm_audit_local`. Existing on-disk EEE artifacts that pre-date the rename
  still carry the old tag; see
  [`dev/oneoff/migrate_eee_source_org_tag.py`](dev/oneoff/migrate_eee_source_org_tag.py)
  to port them.
