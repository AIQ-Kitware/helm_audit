# HELM ↔ EEE metadata: what's there, what's missing, and how to keep more of it

`eval_audit` now supports two parallel comparison surfaces:

- **HELM-driven** (`eval-audit-compare-pair`, `eval-audit-analyze-experiment`,
  …) — consumes HELM run dirs that contain `run_spec.json`, `scenario.json`,
  `stats.json`, `per_instance_stats.json`, and `scenario_state.json`.
- **EEE-driven** (`eval-audit-from-eee`, `eval-audit-compare-pair-eee`) —
  consumes [`every_eval_ever`](../submodules/every_eval_ever/) artifacts:
  one `<uuid>.json` aggregate file plus a sibling `<uuid>_samples.jsonl`
  with per-instance metric values.

Both routes go through the same comparison-intent planner
(`eval_audit/planning/core_report_planner.py`) and the same core-metric
renderer (`eval_audit/reports/core_metrics.py`). They produce the same
`core_metric_report.latest.{txt,json,png}` shape. **The difference is in
the comparability-fact metadata each side can substantiate.**

This document catalogues what HELM has that EEE doesn't, what that means
for the report, and how to preserve the missing metadata when you have
it.

## At a glance

| Comparability fact | Source field | EEE-only outcome | With sidecar `run_spec.json` |
|---|---|---|---|
| `same_model` | EEE `model_info.id` (or HELM `adapter_spec.model`) | ✅ evaluated | ✅ evaluated |
| `same_suite_or_track_version` | from_eee defaults / `experiment_name` | ✅ evaluated (per-CLI defaults) | ✅ evaluated |
| `same_scenario_class` | HELM `run_spec.json:scenario_spec.class_name` | ⚠️ `unknown` | ✅ evaluated |
| `same_benchmark_family` | derived from scenario class taxonomy | ⚠️ `unknown` | ✅ evaluated |
| `same_deployment` | HELM `run_spec.json:adapter_spec.model_deployment` | ⚠️ `unknown` | ✅ evaluated |
| `same_instructions` | HELM `run_spec.json:adapter_spec.instructions` | ⚠️ `unknown` | ✅ evaluated |
| `same_max_eval_instances` | HELM `run_spec.json:adapter_spec.max_eval_instances` | ⚠️ `unknown` | ✅ evaluated |

⚠️ `unknown` shows up in the report as `comparability_unknown:<fact>`
warnings and `<fact>=unknown` caveats — the comparison still runs, but
the planner declines to assert agreement on a fact it cannot evaluate.

## What does *not* depend on HELM metadata

Agreement metrics — and they are what most readers actually care about
— are computed from the EEE-side data alone:

- **Run-level metrics** (`abs_delta` quantiles, agreement curve at every
  tolerance threshold) come from the per-metric scores in the EEE
  aggregate JSON.
- **Instance-level metrics** (per-instance `abs_delta`, agreement
  curves, per-metric breakdowns) come from `<uuid>_samples.jsonl`.
- **Same-model identity** is derived from EEE `model_info`.
- **Logical run key** (`<benchmark>:model=<model_id>`) is derived from
  EEE `evaluation_results[0].source_data.dataset_name` plus
  `model_info.id`.

In other words, the *quantitative* answer to "are these two runs
reproducible?" is the same regardless of whether you have HELM
metadata. The *qualitative* answer to "are they the same kind of
comparison?" is what the missing metadata gives you.

## What HELM has that EEE doesn't (in detail)

### `run_spec.json`

| Field | What it is | Why the planner cares |
|---|---|---|
| `name` | the canonical run-spec slug (`benchmark:k=v,k=v,...`) | logical-run-key fallback when the EEE benchmark string is ambiguous |
| `adapter_spec.model` | model identity | redundant with EEE `model_info` for same-model checks |
| `adapter_spec.model_deployment` | deployment identity (e.g. `huggingface/llama-3-8b`, `vllm/qwen2.5-7b`) | `same_deployment` fact — distinguishes "same model, different serving stack" |
| `adapter_spec.instructions` | exact prompt-prefix string | `same_instructions` fact — one of the biggest sources of cross-run drift in HELM |
| `adapter_spec.max_eval_instances` | scope of the evaluation | `same_max_eval_instances` fact — same model + same instructions, but one ran 100 instances and the other 1000, is *not* a clean comparison |
| `scenario_spec.class_name` | the scenario class (e.g. `helm.MMLUScenario`) | `same_scenario_class` fact + the basis for `same_benchmark_family` |
| `scenario_spec.args` | scenario constructor args (e.g. MMLU subject) | not currently surfaced as a comparability fact, but useful for scenario-aware filtering |

### `scenario.json`

Captures the *resolved* scenario — full benchmark identity including
splits, train/dev/test partition, instance counts, and any scenario
parameters that aren't visible in `run_spec.json`. The planner does not
currently use this directly, but the HELM-driven path consumes it via
`HelmRunDiff` for run-vs-run diagnosis.

### `stats.json` and `per_instance_stats.json`

The HELM-native equivalents of what EEE distills into the
`evaluation_results` block + `<uuid>_samples.jsonl`. The numeric
content is the same; the shape is different. EEE is a normalized
re-shape and intentionally drops HELM's per-stat metadata fields
(`split`, `sub_split`, `perturbation`, etc.). Most reproducibility
analyses do not need these — agreement curves don't change shape based
on which split a metric came from — but if you want to filter agreement
by perturbation or split, you need either HELM raw or an EEE schema
extension.

### `scenario_state.json`

The full prompt+completion record per request. Neither EEE nor the
planner consumes this for reproducibility analysis; it lives in HELM
land for deep-dive debugging only.

## Recommendations

### 1. Ship `run_spec.json` next to your EEE artifacts when you have it

Both `eval-audit-from-eee` and `eval-audit-compare-pair-eee` look for
`run_spec.json` in the same directory as `<uuid>.json`. When present,
the planner reads it via the existing
`eval_audit.indexing.schema.extract_run_spec_fields` helper, and all
five comparability facts that would otherwise be `unknown` evaluate
normally.

This is a no-op cost: the file is already in the HELM run dir that the
EEE converter ran against. Ship it alongside the EEE artifact.

```
<artifact_dir>/
├── <uuid>.json              # EEE aggregate
├── <uuid>_samples.jsonl     # EEE per-instance
└── run_spec.json            # ← optional sidecar; auto-detected
```

### 2. Pin scenario class + deployment in your local pipeline

If you can't ship `run_spec.json` (e.g. your local pipeline doesn't run
HELM directly), the next-best thing is to embed the equivalent fields
in a **conventional** location your tooling can read. Two practical
options:

- **JSON sidecar with the same shape as `run_spec.json`.** Simplest —
  reuses the existing reader. Fields: `adapter_spec.model_deployment`,
  `adapter_spec.instructions`, `adapter_spec.max_eval_instances`,
  `scenario_spec.class_name`.

- **Extend EEE.** Add a `comparison_metadata` block at the top level
  of `<uuid>.json` capturing the same fields. The downside is a
  schema change in `every_eval_ever`; the upside is metadata travels
  with the artifact rather than as a sidecar. If you're already
  patching the EEE converter for your local pipeline, this is
  cheap.

We currently support option (1) and not (2). Option (2) is a
reasonable next step for the EEE schema; tracked as a todo on the
EEE side rather than the eval_audit side.

### 3. When you can't preserve the metadata, surface that to the reader

If your pipeline genuinely doesn't have HELM provenance, the right move
is *not* to fabricate it. Run the EEE-only comparison; the report's
`comparability_unknown:*` warnings + `eee_metadata_caveats.latest.txt`
file accurately tell the reader "we couldn't verify these identity
claims." That's better than a `same_deployment=yes` that quietly
asserts equivalence the data doesn't support.

## Tools that respect this contract

| Tool | Behavior with HELM run dir | Behavior with EEE artifacts | With EEE + sidecar |
|---|---|---|---|
| `eval-audit-compare-pair` | full comparability | n/a (HELM-only) | n/a |
| `eval-audit-compare-pair-eee` | n/a (EEE-only) | 4–5 facts `unknown` | full comparability |
| `eval-audit-from-eee` | n/a (EEE-only) | 4–5 facts `unknown` | full comparability |
| `eval-audit-build-virtual-experiment` | full comparability via `audit_index` + `official_public_index` | full pipeline via `eee_root` / `external_eee` source kinds (4–5 facts `unknown` per row without sidecar) | full comparability |
| `eval-audit-analyze-experiment` | full comparability | accepts EEE rows (e.g. composed by a virtual experiment) and renders per-packet reports | full comparability |
| `eval-audit-build-summary` | full comparability | aggregates the per-pair findings as-is; warnings flow into the summary | sidecar status flows through automatically |

### Virtual experiments over EEE

The virtual-experiment composer accepts two EEE-aware source kinds in
addition to the HELM-driven `audit_index` and `official_public_index`:

```yaml
sources:
  - kind: eee_root
    root: /path/to/eee/tree   # contains official/ and local/ subdirs
    side: both                 # "both" | "official" | "local"
    experiment_name: optional  # defaults to subdir under local/

  - kind: external_eee
    components:
      - id: my-component
        eee_artifact_path: /path/to/<uuid>.json or its dir
        run_entry: "<benchmark>:model=<model_id>"   # pins the planner key
        side: local             # "local" (default) | "official"
        display_name: "..."
        provenance: {tool: inspect-ai, version: ...}
```

Both source kinds materialize into the synthesized indexes the same
way HELM-driven sources do; the compose step applies the manifest's
`scope` filter uniformly across all source kinds. Rows from EEE
sources interleave with HELM rows in the synthesized indexes, and the
planner accepts the mix via the `artifact_format=eee` path. See
[`configs/virtual-experiments/eee-only-demo.yaml`](../configs/virtual-experiments/eee-only-demo.yaml)
for a worked manifest against the checked-in fixture and
[`tests/test_virtual_experiment_eee.py`](../tests/test_virtual_experiment_eee.py)
for an end-to-end test.

## Test fixtures that exercise both modes

- [`tests/fixtures/eee_only_demo/eee_artifacts/`](../tests/fixtures/eee_only_demo/eee_artifacts/)
  — 3 toy models × 3 toy benchmarks of synthetic EEE artifacts
  *without* HELM sidecars. Used by `tests/test_eee_only_demo.py` to
  pin agreement-bucket counts and assert on the `unknown` collapse.

- [`tests/test_compare_pair_eee.py`](../tests/test_compare_pair_eee.py)
  — exercises `eval-audit-compare-pair-eee` against the same fixture
  in both modes (without a sidecar → 4 facts `unknown`; with a
  synthesized sidecar → all facts evaluable).

- [`tests/test_virtual_experiment_eee.py`](../tests/test_virtual_experiment_eee.py)
  — exercises the virtual-experiment composer + analyzer + aggregate
  summary end-to-end against the same fixture. Asserts the engineered
  agreement-bucket counts (6 exact / 2 low / 1 zero) come through the
  `eee_root` source unchanged, and that an `external_eee` component
  materializes into a planner-visible row.

Run them with `pytest --run-slow` (slow-marked because they shell out
to the analysis pipeline).
