# inspectai_helm_eee_compare — when are two evals really comparable?

This runbook composes a deliberately heterogeneous EEE input bundle and
runs the EEE-only analysis path (`eval-audit-from-eee`) to see what the
planner can and can't conclude about comparability. The four artifacts
all claim to be "MMLU on `eleutherai/pythia-6.9b`":

| side | source | samples | metric | harness | sidecar |
|---|---|---|---|---|---|
| `official` | public HELM EEE conversion (`mmlu:subject=us_foreign_policy`) | 111 | `prefix_exact_match` | HELM | yes (`run_spec.json`) |
| `local/audit-mmlu-usfp-pythia-r1` | our audit reproduction, attempt 1 | 111 | `quasi_prefix_exact_match` | HELM | yes |
| `local/audit-mmlu-usfp-pythia-r2` | our audit reproduction, attempt 2 | 111 | `quasi_prefix_exact_match` | HELM | yes |
| `local/inspectai` | InspectAI dump of MMLU (full benchmark, all subjects) | 13937 | `accuracy` | InspectAI | no |

The first three are HELM-shaped and share a `run_spec.json` sidecar.
The InspectAI artifact is the cross-harness contributor: same model,
same benchmark *family*, but different scope (full MMLU vs. one
subject), different metric, different harness, no sidecar.

## What you get out

```bash
./00_check_artifacts.sh   # verify all four sources are on disk
./10_link_tree.sh         # build a from_eee-shaped symlink tree, copy HELM sidecars
./20_run.sh               # eval-audit-from-eee → core_metric_report.json + management summary
./30_inspect.sh           # print comparability facts per pair + side-by-side EEE field dump
```

Override `AUDIT_STORE_ROOT`, `AUDIT_RESULTS_ROOT`, `HELM_PUBLIC_ROOT`,
or `OUT_ROOT` if your store layout differs.

## How the planner answers "are these comparable?" today

The planner pairs components by **logical run key** — for these
artifacts that resolves to `mmlu:model=eleutherai/pythia-6.9b` for all
four. It then computes a fixed set of comparability facts from
`run_spec.json`:

```
same_model, same_scenario_class, same_benchmark_family,
same_deployment, same_instructions, same_max_eval_instances,
same_suite_or_track_version
```

Each fact reports `status ∈ {yes, no, unknown}` plus the set of values
seen. `unknown` is the planner's "I don't have the data to decide,"
emitted as a `comparability_unknown:<fact>` warning. When a side has
no `run_spec.json` (the InspectAI case), the planner also emits
`missing_run_spec:<component_id>` and `missing_scenario_class:<...>`.

## What 30_inspect.sh shows on this bundle

Five pairs are produced (one packet, four components, paired
exhaustively):

- **Pair 1 — official ↔ InspectAI** (`official_vs_local`).
  `same_deployment` is `unknown` (HELM side has it, InspectAI side
  doesn't). All other facts come back `yes` — but only because the
  set-of-seen-values has a single non-null entry, not because the
  planner verified agreement. The warnings explicitly say
  `missing_run_spec` / `missing_scenario_class` for the InspectAI side.
  **`agree@0` is `None`** because the metric names don't overlap
  (`prefix_exact_match` vs. `accuracy`).

- **Pairs 2 & 3 — official ↔ local audit r1/r2** (`official_vs_local`).
  All facts agree, no warnings. `agree@0` instance-level = **0.94** —
  this is the real reproducibility signal. Run-level is 0.0 because
  the public side emits `prefix_exact_match` and the local side emits
  `quasi_prefix_exact_match` — a known schema-naming drift between
  HELM versions, not a model behavior difference.

- **Pairs 4 & 5 — InspectAI ↔ local audit r1/r2** (`local_repeat`).
  Same warning pattern as pair 1; `agree@0=None`.

So the planner is **already telling you this bundle is partly
incomparable** — but only via two channels: (a) `missing_run_spec` /
`missing_scenario_class` warnings, and (b) silent `agree@0=None` when
the metric vocabularies don't overlap.

## Where the planner doesn't help (yet)

The InspectAI artifact carries plenty of EEE-native signal that the
planner doesn't currently consult:

- `source_data.dataset_name` — both sides say "mmlu", but matching
  on name alone hides the next field's mismatch.
- `source_data.samples_number` — **111 vs. 13937**. Different scope
  (one subject vs. full benchmark). The planner's `same_max_eval_instances`
  check reads `run_spec.json`, not the EEE artifact, so this divergence
  is invisible to it.
- `metric_config.evaluation_description` — HELM `prefix_exact_match`
  vs. InspectAI `accuracy`. Different scoring rules; not just a
  rename.
- `eval_library.name` — `HELM` vs. `inspect`. The cross-harness label
  the user ultimately wants surfaced as a first-class fact.
- `generation_config.additional_details` — 5-shot config, prompt
  template choice, stop sequences, etc.

A future planner extension could lift these into EEE-native
comparability facts (`same_dataset_scope`, `same_metric_family`,
`same_eval_library`) so that bundles with no `run_spec.json` on either
side still get a meaningful comparability verdict instead of a silent
metric-name mismatch.

## Bottom line

Yes, there's enough information in EEE alone to detect this
particular cross-harness mismatch — but the current planner only
detects it indirectly (via missing-sidecar warnings and absent
`agree@0`). The constructive ask: extend the planner to derive
comparability facts from EEE-native fields, so cross-harness bundles
fail the comparability check **loudly** rather than producing a
`None` agreement number.
