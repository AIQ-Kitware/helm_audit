# EEE-only demo: tutorial runbook

This runbook is a self-contained tutorial for the **EEE-only analysis path**.
It answers the question:

> *"I have official evaluations in `every_eval_ever` (EEE) format, and I have
> local reproduction attempts of those same evaluations also in EEE format.
> How do I compare them with `eval_audit` and get a per-pair report?"*

No HELM run dirs, no `run_spec.json`, no `audit_results_index.csv` from the
filtering pipeline — just two trees of EEE artifacts and one CLI:
[`eval-audit-from-eee`](../../eval_audit/cli/from_eee.py).

## What it shows

The fixture under [`tests/fixtures/eee_only_demo/eee_artifacts/`](../../tests/fixtures/eee_only_demo/eee_artifacts/)
contains 3 toy models × 3 toy benchmarks of synthetic EEE artifacts, deliberately
constructed so the resulting analysis report demonstrates the full range of
outcomes a real comparison can produce:

| (model, benchmark) | Run-level agreement at `abs_tol=0` | Instance-level agreement at `abs_tol=0` | What the pair illustrates |
|---|---|---|---|
| `toy/m1-small` × `arc_easy` | 1.0 | 1.0 | **Perfect reproduction** — local matches official exactly. Also has a *repeat* local attempt, so the packet contains a `local_repeat` comparison alongside the two `official_vs_local` ones. |
| `toy/m1-small` × `truthful_qa` | 0.0 | 0.75 | **Run-level metrics differ but most instances agree** — one of four instances flipped, dragging the per-metric mean off. Useful for seeing how aggregate disagreement can be milder than it looks at the run level. |
| `toy/m1-small` × `imdb` | 0.0 | 0.0 | **Full divergence** — every instance disagrees. The plotted distribution should show `|Δ|=1` everywhere. |
| `toy/m2-medium` × `imdb` | 0.0 | 0.75 | Same one-instance-flipped pattern but on a different model, so aggregate views can pick it up too. |
| (everything else) | 1.0 | 1.0 | Perfect reproduction baseline. |

These patterns are baked into [`tests/fixtures/eee_only_demo/build_fixture.py`](../../tests/fixtures/eee_only_demo/build_fixture.py)
via a `DRIFT` map, so the fixture is reproducible bit-for-bit (uuid5-keyed) and
the test in [`tests/test_eee_only_demo.py`](../../tests/test_eee_only_demo.py)
can assert on the agreement curves.

## What the pipeline does

`eval-audit-from-eee` is the EEE-only seam through the rest of `eval_audit`:

1. Walks `<eee-root>/official/` and `<eee-root>/local/` for EEE aggregate
   JSONs (each next to a `<uuid>_samples.jsonl` of instance-level rows).
2. Synthesizes in-memory index rows with `artifact_format=eee` and an
   `eee_artifact_path` pointer, then writes them out as
   `official_public_index.latest.csv` + `audit_results_index.latest.csv` so
   the rest of the pipeline (which is index-driven) sees the same shape it
   does for the HELM-based path.
3. Calls `core_report_planner.build_planning_artifact` to pair up official
   and local rows by `<benchmark>:model=<model_id>`. When a logical run key
   has more than one local artifact, the planner emits a `local_repeat`
   comparison alongside each `official_vs_local`.
4. Renders one core-metric report per packet (run-level + instance-level
   agreement curves, per-metric breakdowns, comparability facts,
   warnings, redraw_plots.sh).

Comparability facts that the HELM-based path derives from `run_spec.json`
(`same_scenario_class`, `same_deployment`, `same_instructions`, …) collapse
to `status=unknown` for EEE-only inputs, surfacing as warnings. This is the
correct behavior: the report flags the missing evidence rather than silently
asserting compatibility we can't verify.

## How to run it

```bash
# From repo root.
./reproduce/eee_only_demo/00_build_fixture.sh
./reproduce/eee_only_demo/10_run_analysis.sh
```

`00_build_fixture.sh` is **safe to skip if `tests/fixtures/eee_only_demo/eee_artifacts/`
already exists** — the fixture is checked in. Re-run only if you want to
regenerate it from scratch (e.g., after editing `build_fixture.py`). The
output is uuid5-deterministic so regenerated artifacts collide bit-for-bit
with the checked-in copy.

`10_run_analysis.sh` runs `eval-audit-from-eee` against the fixture and writes:

- per-packet reports under
  `/tmp/eee_only_demo_out/<experiment_name>/core-reports/<packet>/core_metric_report.latest.{txt,json,png}`
- an aggregate cross-packet summary under
  `/tmp/eee_only_demo_out/aggregate-summary/all-results/` with the README,
  agreement-curve plot, reproducibility-buckets bar chart, sankeys, and
  drill-down tables (set `BUILD_AGGREGATE=0` to skip).

Override the output dir with `OUT_DPATH=...`.

The aggregate-summary numbers should match the engineered drift map exactly:

```
total_jobs: 10                 # 9 single-attempt + 1 repeat
completed_and_analyzed: 9      # 9 packets
analyzed reproducibility buckets:
  * exact_or_near_exact: 6     # all arc_easy + imdb m3 + truthful_qa m2/m3
  * low_agreement_0.00+: 2     # imdb m2 + truthful_qa m1 (1-of-4 instance flips)
  * zero_agreement: 1          # imdb m1 (full divergence)
```

If those numbers ever drift, either the fixture changed or the planner /
core-metrics / aggregate-summary pipeline regressed; both are worth
investigating before treating the report as trustworthy.

## Adapting it for your own EEE-format evals

1. Arrange your artifacts as

   ```
   <my-eee-root>/
     official/<benchmark>/<dev>/<model>/<uuid>.json
     official/<benchmark>/<dev>/<model>/<uuid>_samples.jsonl
     local/<experiment>/<benchmark>/<dev>/<model>/<uuid>.json
     local/<experiment>/<benchmark>/<dev>/<model>/<uuid>_samples.jsonl
   ```

   The `<dev>` rung can be any string (typically the model org). Multiple
   `<uuid>` artifact pairs in a single `<model>` directory get paired up
   as repeat attempts of the same logical run.

2. Run

   ```bash
   eval-audit-from-eee \
     --eee-root <my-eee-root> \
     --out-dpath <my-output-dir> \
     --build-aggregate-summary
   ```

3. Inspect
   `<my-output-dir>/<experiment_name>/core-reports/<packet>/core_metric_report.latest.txt`
   for the per-pair agreement curves and comparability facts. The
   `redraw_plots.latest.sh` sibling lets you iterate on plot styling
   without re-running the analysis. Drop `--build-aggregate-summary` if
   you only want the per-packet reports.

4. Inspect `<my-output-dir>/aggregate-summary/all-results/README.latest.txt`
   for the cross-packet roll-up: agreement-bucket counts, per-metric
   curves, drill-down tables by model/benchmark/experiment, and a
   prioritized-examples tree that links straight to the per-packet
   reports for the worst/median/best comparisons.

## Why the "unknown" comparability facts matter

When the EEE format is the only thing you have, several of the
comparability checks `eval_audit` performs against full HELM run dirs
(scenario class identity, deployment identity, instructions string match,
max-eval-instances match, suite/track identity for local-vs-local) cannot
be answered from EEE artifacts alone. The report registers each one as
`status=unknown` with a corresponding warning (`comparability_unknown:*`)
and a caveat. This is intentional: the analysis still runs, the agreement
curves are still trustworthy, but the reader is warned that several
identity assertions the HELM path could verify here cannot. If you ship
your evals with the HELM `run_spec.json` alongside the EEE artifacts, those
facts flip to `yes`/`no` and the warnings disappear.

## What's *not* in this runbook

- No GPU. No model loads. No HELM CLI. The fixture is synthetic and the
  CLI is pure Python.
- No `eval-audit-make-manifest`, `eval-audit-run`, `eval-audit-index`. The
  EEE-only path skips the discovery+execution stages because the EEE
  artifacts already encode "what was run on what."
- No Stage-1 filter sankey. The EEE-only path bypasses HELM run-spec
  discovery and the eligibility-filter funnel — the artifacts you provide
  already define the scope. The aggregate summary skips the filter
  inventory automatically (`--no-filter-inventory`) and excludes the host
  machine's pre-existing experiment store from the scan
  (`--no-canonical-scan`), so the report describes only the EEE artifacts
  you passed in.
