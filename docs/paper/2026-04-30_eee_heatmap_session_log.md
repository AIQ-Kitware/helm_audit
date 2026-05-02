# EEE-only reproducibility heatmap — session work log

Date: 2026-04-30 → 2026-05-01 (UTC)
Audit scope: paper-driving ablation that produces a model × benchmark
heatmap of instance-level reproducibility (official public HELM
artifacts vs. local open-weight reproductions on `aiq-gpu`).

This document records what we built, what we broke and fixed, what
we learned about HELM/EEE internals, and the open questions that
should land in the paper or in upstream PRs.

## Table of contents

- [1. Goal and scope](#1-goal-and-scope)
- [2. Runbook architecture](#2-runbook-architecture)
  - [2.1 Three-state cell rendering](#21-three-state-cell-rendering)
  - [2.2 Origin-experiment filter](#22-origin-experiment-filter)
- [3. Bugs found and fixed (chronological)](#3-bugs-found-and-fixed-chronological)
  - [3.1 The CRFM EEE store has *both* old-format and new-format conversions per dir](#31-the-crfm-eee-store-has-both-old-format-and-new-format-conversions-per-dir)
  - [3.2 The aggregate-summary phase silently re-rendered per-pair reports](#32-the-aggregate-summary-phase-silently-re-rendered-per-pair-reports)
  - [3.3 kwdagger schedule.py was hard-coding `job.log = False` post-submission](#33-kwdagger-schedulepy-was-hard-coding-joblog--false-post-submission)
  - [3.4 HELM `mmlu_pro` accepts `subject=` but writes `subset=` in the display name](#34-helm-mmlu_pro-accepts-subject-but-writes-subset-in-the-display-name)
  - [3.5 Every-eval-ever converter writes aggregate to `eee_output/unknown/...` for some scenarios](#35-every-eval-ever-converter-writes-aggregate-to-eee_outputunknown-for-some-scenarios)
  - [3.6 The dedupe oneoff](#36-the-dedupe-oneoff-devoneoffdedupe_old_eee_conversionspy)
  - [3.7 mid-session course correction: my dual-key join fix was wrong](#37-mid-session-course-correction-my-dual-key-join-fix-was-wrong)
  - [3.8 Other smaller fixes from the session](#38-other-smaller-fixes-from-the-session)
- [4. Performance work](#4-performance-work)
  - [4.1 kwdagger schedule `--workers` (packet-level parallelism)](#41-kwdagger-schedule---workers-packet-level-parallelism)
  - [4.2 core_metrics per-packet component cache](#42-core_metrics-per-packet-component-cache)
  - [4.3 venv auto-activation in spawned jobs](#43-venv-auto-activation-in-spawned-jobs)
  - [4.4 `HELM_AUDIT_SKIP_PLOTLY` / `FAST_AGG_SUMMARY=1`](#44-helm_audit_skip_plotly--fast_agg_summary1)
  - [4.5 line_profiler instrumentation](#45-line_profiler-instrumentation)
  - [4.6 Empirical timing summary](#46-empirical-timing-summary)
- [5. Findings about HELM data shape](#5-findings-about-helm-data-shape)
  - [5.1 HELM instance subsampling is non-deterministic across re-runs](#51-helm-instance-subsampling-is-non-deterministic-across-re-runs)
  - [5.2 `sample_hash` vs `sample_id` semantics](#52-sample_hash-vs-sample_id-semantics)
  - [5.3 The CRFM EEE store conflates schema versions](#53-the-crfm-eee-store-conflates-schema-versions)
  - [5.4 The aggregate-extractor doesn't recognize newer scenario stat shapes](#54-the-aggregate-extractor-doesnt-recognize-newer-scenario-stat-shapes)
- [6. Tooling shipped](#6-tooling-shipped)
- [7. Repository state at end of session](#7-repository-state-at-end-of-session)
  - [7.1 Commits queued for push (parent repo)](#71-commits-queued-for-push-parent-repo)
  - [7.2 Submodule pushes already done](#72-submodule-pushes-already-done)
  - [7.3 Uncommitted, intentionally not staged](#73-uncommitted-intentionally-not-staged)
- [8. Open follow-ups](#8-open-follow-ups)
  - [8.1 QuAC re-conversion (closes 2 cells)](#81-quac-re-conversion-closes-2-cells)
  - [8.2 Entity-matching / synthetic-reasoning-natural cells](#82-entity-matching--synthetic-reasoning-natural-cells)
  - [8.3 every_eval_ever PR](#83-every_eval_ever-pr)
  - [8.4 toothbrush / aiq-gpu sync](#84-toothbrush--aiq-gpu-sync)
  - [8.5 Paper limitations note](#85-paper-limitations-note)
- [9. Quick command reference](#9-quick-command-reference)
- [10. Files changed in this session](#10-files-changed-in-this-session)

---

## 1. Goal and scope

Produce a 3 × 14 reproducibility heatmap for the paper:

| Axis | Values |
|---|---|
| Models | `eleutherai/pythia-2.8b-v0`, `eleutherai/pythia-6.9b`, `lmsys/vicuna-7b-v1.3` |
| Benchmarks | boolq, civil_comments, entity_data_imputation, entity_matching, gsm, imdb, lsat_qa, mmlu, narrativeqa, quac, synthetic_reasoning, sythetic_reasoning_natural, truthful_qa, wikifact |
| Cell value | Instance-level `agree_ratio` at `abs_tol=1e-9`, micro-averaged across all `official_vs_local` pairs in the (model, benchmark) packet |

Scope decisions made during this session:

- Only `aiq-gpu` runs count. Secondary reproductions on `namek` and
  `yardrat` are explicitly excluded. The local-side composite
  `open-helm-models-reproducibility` mixes runs from many origin
  experiments — we filter via `EXCLUDE_ORIGIN_EXPERIMENTS`.
- Pythia-2.8B's row will mostly be missing because HELM v0.2.4 only
  publishes EEE artifacts for `boolq` and `civil_comments` for that
  model. That's a public-coverage gap, not a bug.
- One representative sub-benchmark per family for benchmarks with
  multiple sub-runs (e.g. `mmlu:subject=us_foreign_policy`,
  `wikifact:k=5,subject=place_of_birth`,
  `civil_comments:demographic=all`). Picking just one keeps planner
  ambiguity manageable; we sacrifice breadth-within-a-family for
  unambiguous packet pairing.

Final coverage:

- **24 / 30 cells** with real agreement numbers in the 0.92–1.00 band.
- **4 cells** (`entity_matching` × {pythia-6.9b, vicuna-7b}, plus
  `sythetic_reasoning_natural` × {pythia-6.9b, vicuna-7b}) show as
  `join_failed`: the official and local runs sampled *different
  instances* from the same scenario. Documented as a methodology
  limitation; not fixable without re-running with a deterministic
  HELM instance-id assignment.
- **2 cells** (`quac` × {pythia-6.9b, vicuna-7b}) show as `join_failed`:
  public CRFM EEE store has only the old-format conversion for QuAC.
  Re-conversion would close it; not done in this session.
- **12 missing cells** (Pythia-2.8B row except boolq + civil_comments)
  reflect public-store coverage, not anything we can fix.

---

## 2. Runbook architecture

`reproduce/eee_only_reproducibility_heatmap/` — four-step runbook:

```
00_check_artifacts.sh   → coverage table: which (model, benchmark) pairs
                          have both official and local EEE artifacts
10_link_tree.sh         → builds a symlink tree shaped for
                          eval-audit-from-eee (official/ + local/<exp>/)
20_run.sh               → eval-audit-from-eee → per-packet
                          core_metric_report.json
30_heatmap.sh           → eval_audit.reports.eee_only_heatmap →
                          PNG + JSON + text-table heatmap
```

Plus the analysis module: `eval_audit/reports/eee_only_heatmap.py`.

### 2.1 Three-state cell rendering

Cells are `present` / `join_failed` / `missing`:

- **present** (RdYlGn): real `agree_ratio` shown, color-coded.
- **join_failed** (light amber + diagonal hatching, "join 0/N"):
  packet exists, both sides have data, but `instance_level.n_rows == 0`
  for every `official_vs_local` pair. Means a join key mismatch —
  data is there but unjoinable.
- **missing** (solid gray, "—"): no packet for this cell. Either the
  official side never published it (Pythia-2.8B's row) or one side's
  artifact never made it into the link tree.

The distinction matters because the two failure modes have entirely
different fixes. Conflating them was a bug we eliminated mid-session
(see § 3.2).

### 2.2 Origin-experiment filter

The local composite folder `eee/local/open-helm-models-reproducibility/`
is a 197-artifact pool sourced from 22+ origin audit experiments
(`audit-historic-grid`, `audit-qwen25-7b-aiq`,
`audit-small-models-kubeai-overnight`, `audit-namek-subset`,
`audit-yardrat-subset`, etc.). Each artifact's `status.json` records
`run_path`, the third path component of which is the origin-experiment
name.

`10_link_tree.sh` reads each candidate's `status.json`, blocks any
artifact whose origin matches `EXCLUDE_ORIGIN_EXPERIMENTS` (default
`audit-namek-subset,audit-yardrat-subset`). Skip count is surfaced
in the per-cell summary line:

```
local: mmlu/eleutherai/pythia-6.9b  (4 artifacts)  [skipped 2 from excluded origins]
```

Override via env var: `EXCLUDE_ORIGIN_EXPERIMENTS=foo,bar bash 10_link_tree.sh`.

---

## 3. Bugs found and fixed (chronological)

### 3.1 The CRFM EEE store has *both* old-format and new-format conversions per dir

**Symptom.** Initial heatmap ran, showed 10 / 30 present and 20 join_failed.

**Root cause.** `crfm-helm-public-eee-test/classic/v0.X.Y/<run-spec>/eee_output/<bench>/<dev>/<model>/` directories typically contain **3+ separate `<uuid>.json` aggregates** with their `_samples.jsonl` siblings — successive re-conversions of the same HELM run. Older converters emitted **one record per (sample, train_trial) with `evaluation_result_id=None`** (all metrics collapsed into a single nameless score). Newer converters emit **per-(sample, metric) records with `evaluation_result_id` set** (the modern schema, ~21x more records).

Local artifacts under `eee/local/open-helm-models-reproducibility/` are uniformly the newer schema. When the join key is `(sample_hash, metric_id)` and one side has `metric_id="num_references"` while the other has `metric_id=None`, the intersection is empty.

Same data, same HELM source dir, same converter version — different on-disk schema due to *which* file the loader picks.

**Fix.** `10_link_tree.sh` originally picked `official_jsons[0]` after a plain alphabetical sort of UUID-named files — essentially a coin flip per cell. Changed to read each candidate's `retrieved_timestamp` and pick the latest.

```python
results.sort(key=lambda tp: (-tp[0], str(tp[1])))  # newest first
return [p for (_t, p) in results]
```

**Commit.** `f20f796` "heatmap/10: pick newest EEE aggregate by retrieved_timestamp"

**Verification path.** A spot-check on `civil_comments × pythia-2.8b-v0`:

```bash
python3 -c "
import json
fname = '/data/crfm-helm-audit-store/.../80d21175-...samples.jsonl'
recs = [json.loads(l) for l in open(fname).readlines()[:3]]
print('first 3 eval_result_ids:', [r.get('evaluation_result_id') for r in recs])
"
# After fix: ['num_references', 'num_train_trials', 'num_prompt_tokens']
# Before fix: [None, None, None]
```

### 3.2 The aggregate-summary phase silently re-rendered per-pair reports

**Symptom.** `LINE_PROFILE=1 eval-audit-build-summary` showed
`_repair_prioritized_example_reports` consuming **98% of
`_render_scope_summary` wall-clock** (~88 s out of an 88 s run for
the heatmap).

**Root cause: a layering violation that became self-perpetuating.**

`_repair_prioritized_example_reports` iterated each prioritized
example, checked whether its report dir contained every filename in
`prioritized_example_artifact_names(packet)`, and if any were missing
shelled back into `rebuild_core_report_main(argv)` to regenerate the
entire packet from scratch.

The **expected-artifacts list was over-inclusive**:

1. `prioritized_example_artifact_names` listed one filename per
   `comparison_id` in the packet's `comparisons` list, **including
   ones marked `enabled: false`** by the planner. The renderer skips
   disabled comparisons, so those filenames never exist on disk.
2. Static images (`core_metric_report.png`) and text summaries
   (`*_summary.txt`) were also in the list. When the user set
   `HELM_AUDIT_SKIP_PLOTLY=1` to skip Chromium, those went missing
   and the repair fired anyway.

So every aggregate-summary run did a full per-pair `core_metrics`
re-render of every prioritized example. The re-render produced the
same output (still skipping disabled comparisons, still respecting
the env flag), so the next run repeated. Self-perpetuating.

**Two fixes shipped:**

1. **Filter expected artifacts to enabled comparisons only.** Commit `ccf8c7d`
   "core_packet_summary: skip disabled comparisons in expected-artifact list".
   Helped, but not enough — base artifacts could still trigger repair.
2. **Stop the repair entirely.** Commit `f6b22e8`
   "build_reports_summary: stop regenerating per-pair reports during summary".
   The function is now a pure verification pass: classifies each example
   as `already_ok` / `incomplete` / `missing_report_dir` and **does not
   regenerate**. Incomplete reports are logged at WARNING with a pointer
   to the per-pair `redraw_plots.sh` / `reproduce.sh` (which already
   exist for exactly this purpose). The publish step's existing
   `if exists():` guards handle missing artifacts gracefully.

The cross-phase import (`from eval_audit.workflows.rebuild_core_report
import main as rebuild_core_report_main`) was removed — aggregate-
summary should be a pure read-pass over what the analyze step
produced. A comment at the import site explains why.

**Why this was always wrong.** The aggregate-summary step:

- Has no GPU dependencies and is meant to be cheap.
- Is meant to be re-runnable for plot tweaks without re-running analysis.
- Should fail loudly if upstream data is incomplete, not paper over it
  with a slow implicit re-render.

**Verification path.** After the layering fix, line_profiler showed
`_repair_prioritized_example_reports`: **88 s → 0.33 s**. Total
build_reports_summary wall-clock: **88 s → 37 s**. The remaining
~30 s is genuine Chromium-bound plot rendering (sankey + plotly bar
+ matplotlib).

### 3.3 kwdagger schedule.py was hard-coding `job.log = False` post-submission

**Symptom.** With our `--log=True` plumbed through `kwdagger
schedule`, the rendered cmd_queue bash scripts still had no `2>&1 |
tee <log_fpath>` wrapper. Failed jobs left no logs to inspect.

**Root cause.** `schedule.py:258-260` had this loop *after*
`dag.submit_jobs(log=config['log'])` set each BashJob's log
attribute correctly:

```python
for job in queue.jobs:
    # TODO: should be able to set this as a queue param.
    job.log = False
```

A pre-existing placeholder waiting for the queue param mechanism we
were just adding. With `--log` plumbed through, this loop became
actively harmful — silently overwriting our intent.

**Fix.** Removed the loop (commit `386a81a` in `submodules/kwdagger`).
Replaced with a comment explaining why future contributors shouldn't
re-add it.

**Regression coverage.**

- `submodules/cmd_queue/tests/test_submit_log_flag.py` (3 tests):
  `Queue.submit(..., log=...)` plumbing → BashJob → rendered tee.
- `submodules/kwdagger/tests/test_submit_jobs_log_flag.py` (3 tests):
  `Pipeline.submit_jobs(log=...)` → all the way through.
- `submodules/kwdagger/tests/test_schedule_cli_log_flag.py` (3 tests):
  CLI-level test that drives `kwdagger schedule --run=0` as a
  subprocess and asserts against the rendered bash script. Catches
  the schedule.py-level layer that the in-process tests can't see.

This is the exact scenario the user predicted: in-process API tests
miss CLI-layer regressions.

### 3.4 HELM `mmlu_pro` accepts `subject=` but writes `subset=` in the display name

**Symptom.** `helm-run` succeeded for `mmlu_pro:subject=all,...`, but
`materialize_helm_run.py` then crashed with "could not be located/
validated" — couldn't find the run dir HELM had just produced.

**Root cause.** HELM's `get_mmlu_pro_spec(subject=...)` accepts
`subject` as the kwarg but writes `mmlu_pro:subset=<subject>,...`
into `run_spec.name` (the display string used as the directory
name). The matcher in `run_dir_matches_requested` keyed on exact
kwarg-name match.

**Fix.** Added `_BENCHMARK_KWARG_ALIASES` table for benchmark-specific
kwarg ↔ display-token renames:

```python
_BENCHMARK_KWARG_ALIASES = {"mmlu_pro": {"subject": "subset"}}
```

Applied in both `aiq-magnet/.../materialize_helm_run.py` and
`eval_audit/helm/run_entries.py` (the two places that do this match).

**Commits.**
- `aiq-magnet`: built into the existing `f62dde6` "wip" commit (user
  committed during the session).
- `helm_audit`: previous-session commit `5f2bdbf` plus session work.

### 3.5 Every-eval-ever converter writes aggregate to `eee_output/unknown/...` for some scenarios

**Symptom.** During the dedupe oneoff investigation, found one
artifact directory layout that broke our `_samples_sibling()`
assumption:

```
eee_output/
  mmlu_pro/openai/gpt-oss-20b/<uuid>_samples.jsonl   ← samples (correct)
  unknown/openai/gpt-oss-20b/<uuid>.json             ← aggregate (WRONG)
```

The aggregate landed in `unknown/...` while samples went in the
correct `mmlu_pro/...`. Same converter, same `--output-dir`, same
HELM source.

**Root cause traced.** Two code paths in `every_eval_ever 0.2.2`
build output dirs from the same EvaluationLog and disagree:

1. `every_eval_ever/converters/helm/adapter.py:484` (samples writer):
   reads `source_data.dataset_name` from the in-progress conversion
   context. Gets `'mmlu_pro'`.
2. `every_eval_ever/cli.py:_output_dir_for_log` (aggregate writer):
   reads `log.evaluation_results[0].source_data.dataset_name`. For
   newer scenarios (mmlu_pro, ifeval, capabilities/safety entries)
   the aggregate's `evaluation_results` array comes back empty
   because the aggregate-extractor doesn't recognize the stat shape
   in `stats.json`. The fallback path kicks in:

   ```python
   def _output_dir_for_log(base_output, log):
       dataset = 'unknown'
       if log.evaluation_results and log.evaluation_results[0].source_data:
           dataset = (log.evaluation_results[0].source_data.dataset_name or 'unknown')
       ...
   ```

   With empty `evaluation_results`, the `if` fails and the literal
   string `'unknown'` is used.

**MWE for upstream PR:**
`submodules/every_eval_ever/dev/repro_helm_aggregate_unknown_dir/`

- `README.md` — bug description, root cause, two proposed fixes.
- `repro.sh` — runs the converter against the bundled fixture and
  asserts that aggregate + samples land in the same directory.
- `make_fixture.sh` — regenerates the fixture from any HELM source.
- `fixture/` — 2.7 MB slimmed `ifeval × openai_gpt-oss-20b` HELM run
  dir (down from 40 MB by keeping 3 instances per file). Bug
  reproduces identically to the full-size original.

**Status.** Not committed; left for the user to inspect, verify, and
file as a PR against the every_eval_ever repo. Two fixes proposed
in the README:

- *Cheap*: `_output_dir_for_log` falls back to a hint passed by the
  caller (the per-instance pipeline already has the right
  `dataset_name`) instead of literal `'unknown'`.
- *Real*: fix the aggregate-extractor to actually populate
  `evaluation_results` for the affected scenario shapes.

**Workaround in our dedupe script:**
`dev/oneoff/dedupe_old_eee_conversions.py:_samples_sibling()` searches
the entire `eee_output/` subtree for the matching `<stem>_samples.jsonl`,
not just `aggregate.parent`. Catches the cross-directory case so
the dedupe doesn't orphan the samples file when removing the
aggregate.

### 3.6 The dedupe oneoff (`dev/oneoff/dedupe_old_eee_conversions.py`)

**Why.** Per § 3.1, the public CRFM store has 2-3 conversions per dir.
The link tree builder picks the newest, but the old files still
take disk space and confuse downstream tooling that doesn't sort by
timestamp.

**What.** Walks the public store, identifies dirs with 2+
aggregate JSONs, keeps the one with the highest `retrieved_timestamp`
(plus its `_samples.jsonl` sibling), deletes the rest.

**Safety.**

- Default `--paper-scope` (5 paper models × ~25 benchmark prefixes).
  Override with `--all-suites` to widen.
- Default dry-run. Pass `--apply` to actually delete.
- Refuses to delete if any aggregate fails to parse.
- Cross-directory samples lookup (per § 3.5).
- Rich-formatted output: KEEP / DELETE / SKIP color-coded, clickable
  file:// links to each candidate.
- High-contrast `DRY RUN` / `APPLY` mode badges in the header.

**Paper-scope dry-run:** 176 dirs, 586 files queued, ~2.2 GB to
reclaim. After user verified and `--apply`-ed, the heatmap re-ran
with cleaner state and 10/30 → 24/30 cells.

### 3.7 mid-session course correction: my dual-key join fix was wrong

**Story arc.** Looking at the 6 join_failed cells after the dedupe,
I observed:

- For `entity_matching × pythia-6.9b`: official and local both
  new-format, **same 1000 sample_ids overlap**, but **0/1000
  sample_hash overlap**.
- I shipped a "dual-key join" (commit `5d45158`) that fell back to
  `(sample_id, metric_id)` when `(sample_hash, metric_id)` produced
  no overlap.

The user pushed back: "So for the entity matching we were checking
hashes of samples?" — prompting a closer look.

**What the closer look revealed.** Pulled the raw record content for
`id2221` from each side:

- OFFICIAL `id2221`: a question about a Sony VAIO laptop bag.
- LOCAL `id2221`: a question about a Sony memory stick adapter.

**Same `sample_id`, completely different content.** The Sony VAIO
bag question existed in the local artifact too — but under
`sample_id="id4831"` instead of `id2221`. HELM's instance-id
assignment is not stable across re-runs.

The overall content-overlap analysis showed only **249 of ~1000**
*questions* in common between official and local for entity_matching.
The two runs sampled different subsets of the Abt_Buy test set.

**My dual-key fix would have joined unrelated instances** — pairing
the panasonic-bag question on the official side with the
sony-memory-stick question on the local side and reporting a
spurious "agreement" number.

**Reverted** (`ae8c0ed`). The hash-based join is correct: it refuses
to pair non-comparable instances, and the resulting `join_failed`
cell is the right truthful signal.

**Lesson.** When you see a "key matches but hash doesn't" pattern,
investigate the underlying content before changing the join. The
hash mismatch was diagnosing real divergence, not a converter bug.

### 3.8 Other smaller fixes from the session

- `submodule-status.sh`: added `origin (resolved)` column so it's
  obvious which URL each submodule actually fetches from. Catches
  divergence between `.gitmodules` and per-clone URLs.
- `pull-submodules.sh`: added `git submodule sync --recursive` at
  the top so URL changes propagated through `.gitmodules` actually
  take effect on each pull. Without it, every clone retains its
  original URL and a `.gitmodules`-side flip (e.g. `every_eval_ever`
  to Erotemic) is silently ignored.
- `pull-submodules.sh`: also auto-attaches detached-HEAD submodules
  to their pinned branch (creating the local branch from
  `origin/<branch>` if it doesn't exist). Day-to-day work in
  submodules now stays on a real branch; pulls fast-forward instead
  of detaching.
- `cmd_queue` URL: aiq-gpu had SSH for cmd_queue while `.gitmodules`
  had HTTPS. Aligned all three layers (`.gitmodules`, parent
  `.git/config`, submodule's own `remote.origin.url`) to HTTPS.
- HELM submodule had a force-push on `origin/main` mid-session. Fixed
  by `git reset --hard origin/main` after saving the local commit
  as a tag — a polished version of the same change had been
  force-pushed by the user from another machine, so nothing was lost.

---

## 4. Performance work

### 4.1 kwdagger schedule `--workers` (packet-level parallelism)

**Status.** Shipped (`acd1895` "from_eee: render packets in parallel").

`eval-audit-from-eee` had a serial loop over packets. Each packet
calls `core_metrics` as a subprocess. Independent packets, no
contention — easy ThreadPoolExecutor over `subprocess.run`.

```bash
WORKERS=8 bash 20_run.sh   # default = nproc / 2
WORKERS=1 bash 20_run.sh   # serialize (preserved as default)
WORKERS=0 bash 20_run.sh   # auto-select (cpu_count // 2)
```

`as_completed` ordering so per-packet "rendered: <path> (K/N)" lines
print as they finish. All workers run to completion even if one
packet fails; the first failure is captured and re-raised after the
pool drains.

### 4.2 core_metrics per-packet component cache

**Status.** Shipped (`6d7f6ac` "core_metrics: cache NormalizedRun
across pairs in one packet").

Each `_build_pair` previously called `_load_normalized` for both
sides of the comparison. For a packet with N comparisons sharing the
same official component (1 official vs N locals + N-1 local_repeat
comparisons), the official artifact was loaded ~N times.

Added `component_cache: dict[component_id, NormalizedRun]` scoped to
one `core_metrics.main` invocation. Threaded through `_build_pair`
and `_load_component_run`. Default `None` preserves no-cache behavior.

For the heatmap's worst packets (boolq-vicuna 19 pairs, mmlu-vicuna
15 pairs): roughly 2-3× fewer NormalizedRun loads.

### 4.3 venv auto-activation in spawned jobs

**Status.** Shipped (`d65a5be` "kwdagger_bridge: auto-activate venv in
spawned jobs").

`eval_audit/integrations/kwdagger_bridge.py` now detects the running
venv (via `$VIRTUAL_ENV` or `sys.prefix != sys.base_prefix`) and
passes `--virtualenv_cmd=source <venv>/bin/activate` to
`kwdagger schedule`. cmd_queue then prepends that activation to
every spawned job, so tmux/serial/slurm subprocesses use the same
Python the CLI was invoked with — instead of whatever happens to be
on PATH after the user's shell rc loads.

This was a real source of pain: jobs were silently using random
PATH-resolved Pythons (sometimes Python 3.14 with broken `dill` /
`datasets`, sometimes a system `/usr/bin/python3` without our deps).

### 4.4 `HELM_AUDIT_SKIP_PLOTLY` / `FAST_AGG_SUMMARY=1`

**Status.** Already plumbed through 6 sites in `build_reports_summary`
and `sankey.emit_sankey_artifacts`. Surfaced in `20_run.sh` as
`FAST_AGG_SUMMARY=1`. Commit `6931f48`.

After § 3.2 (the layering fix), the remaining ~30 s of
`build_reports_summary` time is genuinely Chromium-bound (plotly
sankey PNG/JPG via kaleido). For inner-loop iterations:

```bash
FAST_AGG_SUMMARY=1 bash 20_run.sh
```

Skips static plot rendering. JSON / CSV / TXT / HTML artifacts still
get written; PNG / JPG can be regenerated later via the saved
`redraw_plots.sh` for any specific report dir.

### 4.5 line_profiler instrumentation

**Status.** Shipped (`dfd1c26` + `a849902`). Decorated 13 hot functions
across `build_reports_summary.py`, `sankey.py`, and
`rebuild_core_report.py` with `@line_profiler.profile` (zero-overhead
when `LINE_PROFILE` env unset; falls back to identity wrapper when
line_profiler isn't installed).

Workflow:

```bash
uv pip install line_profiler  # one-time
LINE_PROFILE=1 eval-audit-build-summary [args...]
# writes profile_output_<timestamp>.txt to CWD with per-line tables.
```

This is what diagnosed § 3.2 in 30 seconds when six different
hypotheses (Chromium, recursive breakdown, file I/O, etc.) had been
in the air.

### 4.6 Empirical timing summary

| Run | Total wall-clock | Dominant cost |
|---|---|---|
| Heatmap pre-fix (serial) | ~90-130 min projected | Per-packet rendering at ~3-5 min each, mostly NormalizedRun reloads |
| After § 4.1 + § 4.2 + § 4.3 | ~10-18 min observed | Aggregate summary phase |
| Aggregate summary, before § 3.2 | 88 s | Repair-loop self-perpetuating re-render (~98% of time) |
| Aggregate summary, after § 3.2 | 37 s | Plotly Chromium write_image (~30 s) |
| Aggregate summary, with `HELM_AUDIT_SKIP_PLOTLY=1` | ~7 s expected | Pure-Python aggregation only |

---

## 5. Findings about HELM data shape

These are paper-relevant observations that surfaced during the
audit. Worth documenting in the limitations / methodology section.

### 5.1 HELM instance subsampling is non-deterministic across re-runs

For benchmarks like `entity_matching` and `sythetic_reasoning_natural`,
the official run and the local re-run both targeted
`dataset=Abt_Buy,model=eleutherai_pythia-6.9b` but **evaluated
different test-instance subsets**. Concretely:

- Each run produced 1000 unique `sample_id`s from a larger Abt_Buy
  test set.
- Only **249 of 1000** *question texts* were common between the two
  runs.
- The same `sample_id` mapped to *different content* on each side
  (e.g., `id2221` was a "Panasonic" question on official, a "Sony
  memory stick" question on local).

This means HELM's instance-id assignment is **per-run sequential
indexing of a shuffled subsample**, not a content-stable identifier.
Two runs of the same benchmark on the same scenario can — and do —
end up evaluating disjoint test instances.

**Implication.** For these benchmarks, a fair pair-level reproducibility
audit can't compare by `sample_id`. The two correct options are:

1. **Match by content** (input prompt text) and report agreement on
   the overlap fraction. Implement via a separate join strategy in
   `eval_audit/normalized/joins.py` keyed on input.raw rather than
   sample_id/sample_hash.
2. **Run both sides with deterministic subsampling**. Would require
   changes upstream in HELM to expose a seed for instance selection,
   or to disable subsampling entirely for audit runs.

The current heatmap correctly reports these as `join_failed`. We
opted to document this as a methodology limitation rather than chase
the content-based join in this session.

### 5.2 `sample_hash` vs `sample_id` semantics

`InstanceRecord.join_key` returns `(sample_hash or sample_id,
metric_id)`. The intent is "prefer hash because sample_id can drift
between HELM versions; fall back to id otherwise."

**Real-world behavior.** For our audit (same HELM version, official
vs. local re-run):

- `sample_id` matches but is *not* a stable identifier across re-runs
  (per § 5.1). Joining by it would pair unrelated instances.
- `sample_hash` is content-derived and correctly distinguishes
  different instances. **It is the right join key.**
- However, for benchmarks where the local converter folds
  model-deployment-specific tokens (chat template, request
  envelope) into the prompt before hashing, the hash also drifts
  between official and local for *the same logical content*. We
  did not hit this case in the actual data; the entity_matching
  case looked like "hash drift on same content" but turned out to
  be "different content under same id" (§ 3.7).

Net: `sample_hash` is correct for now. If we ever observe a real
case of "same content, different hash because of prompt-rendering
differences", the right fix is in the converter (canonical
content-only hashing), not in the join.

### 5.3 The CRFM EEE store conflates schema versions

`crfm-helm-public-eee-test/classic/v0.X.Y/<run-spec>/eee_output/.../`
contains **multiple `<uuid>.json` aggregates** in a single directory,
representing successive re-conversions of the same HELM run with
different `every_eval_ever` versions. The schema differs across
versions:

- Old: 1 record per (sample, train_trial); `evaluation_result_id=None`.
- New: 1 record per (sample, metric); `evaluation_result_id` populated.

The `EeeArtifactLoader` picks the newest by `retrieved_timestamp`,
which is *usually* the new schema. But our heatmap link tree builder
originally picked alphabetically, which produced random schema
selection per cell (§ 3.1).

**Recommendation for upstream.** Either:

- The CRFM EEE store should be re-converted in bulk and old files
  deleted. (Our `dev/oneoff/dedupe_old_eee_conversions.py` does
  this for paper-scope; full-store would need `--all-suites`.)
- Or `every_eval_ever` should refuse to overwrite an existing aggregate
  in the same dir without an explicit `--force`, surfacing the
  ambiguity to whoever runs the conversion pipeline.

### 5.4 The aggregate-extractor doesn't recognize newer scenario stat shapes

Per § 3.5, for `mmlu_pro`, `ifeval`, and capabilities/safety
benchmarks, `every_eval_ever` produces an `EvaluationLog` whose
`evaluation_results` array is empty even though the per-instance
samples extract correctly. Cause: `helm/adapter.py:HELMAdapter`
doesn't recognize the metric-name layout in newer `stats.json`
files.

**Implication.** Aggregate JSONs for these scenarios are missing
top-level metric scores. Anything that uses `joined_metric_means`
on them will return an empty dict, and run-level joins will fail.
Per-instance joins still work because the samples writer takes a
different path through `per_instance_stats.json`.

Worth surfacing in the every_eval_ever PR alongside the
`unknown/` path bug.

---

## 6. Tooling shipped

| Tool | Path | Purpose |
|---|---|---|
| Heatmap runbook | `reproduce/eee_only_reproducibility_heatmap/` | 4-step pipeline (00–30) + `eval_audit/reports/eee_only_heatmap.py` analysis module |
| Submodule maintenance | `Makefile` + `dev/scripts/{configure,pull,push,status}-submodule*.sh` | One-time `configure-submodule-branches` + day-to-day `pull-submodules` / `push-submodules` / `submodule-status` |
| Dedupe oneoff | `dev/oneoff/dedupe_old_eee_conversions.py` | Per-dir keep-newest-by-timestamp; rich color output; paper-scope default |
| EEE converter MWE (uncommitted) | `submodules/every_eval_ever/dev/repro_helm_aggregate_unknown_dir/` | Self-contained 2.7 MB fixture + repro.sh + README for the upstream PR (§ 3.5) |
| Profiling decorators | 13 functions across `build_reports_summary.py`, `sankey.py`, `rebuild_core_report.py` | Zero-cost when `LINE_PROFILE` unset; full per-line attribution when set |

---

## 7. Repository state at end of session

### 7.1 Commits queued for push (parent repo)

`git log @{u}..HEAD` from `helm_audit/` will list them. Selected highlights:

| Commit | Subject |
|---|---|
| `283080d` | reports: EEE-only reproducibility heatmap (3 models × 14 benchmarks) |
| `f20f796` | heatmap/10: pick newest EEE aggregate by retrieved_timestamp |
| `65cc9e3` | heatmap: distinguish missing vs join_failed cells |
| `b91ffe3` | oneoff: dedupe_old_eee_conversions.py — keep newest EEE per HELM run |
| `824204b` | heatmap/10: blocklist secondary-host origin experiments (namek, yardrat) |
| `acd1895` | from_eee: render packets in parallel (--workers N) |
| `6d7f6ac` | core_metrics: cache NormalizedRun across pairs in one packet |
| `d65a5be` | kwdagger_bridge: auto-activate venv in spawned jobs |
| `dfd1c26`, `a849902` | line_profiler instrumentation |
| `ccf8c7d` | core_packet_summary: skip disabled comparisons in expected-artifact list |
| `f6b22e8` | build_reports_summary: stop regenerating per-pair reports during summary |
| `6931f48` | heatmap/20: opt-in FAST_AGG_SUMMARY skips Chromium plot rendering |
| `5d45158` (reverted by `ae8c0ed`) | joins: fall back to sample_id when sample_hash drifts (later determined wrong) |

### 7.2 Submodule pushes already done

`cmd_queue`, `kwdagger`, `aiq-magnet` are 0/0 against their tracked
upstreams. Earlier commits (test files, schedule.py log fix, mmlu_pro
matcher fix) were pushed by the user during the session. The parent's
gitlinks were bumped via commit `144caef`.

### 7.3 Uncommitted, intentionally not staged

- `submodules/every_eval_ever/dev/repro_helm_aggregate_unknown_dir/` — § 3.5 MWE for upstream PR. User to inspect and submit.
- This document.

---

## 8. Open follow-ups

### 8.1 QuAC re-conversion (closes 2 cells)

`crfm-helm-public-eee-test/classic/v0.3.0/quac:model=...` directories
contain only old-format conversions. The dedupe correctly keeps
those (they're the only ones available); the loader gets `metric_id=None`
and the heatmap shows `join_failed`. Re-converting from
`/data/crfm-helm-public/classic/benchmark_output/runs/v0.3.0/quac:model=.../`
with the current `every_eval_ever 0.2.2` would produce new-format
output and close those two cells.

Scope: 2 dirs total (one per model). Each conversion takes ~30 s.
Could be a 5-line addition to `dev/oneoff/dedupe_old_eee_conversions.py`
or a separate `reconvert_quac_for_paper.sh`.

### 8.2 Entity-matching / synthetic-reasoning-natural cells

Per § 5.1, fair comparison requires either:

- A content-based join in `eval_audit/normalized/joins.py` (paper
  scope question: do we need these cells, or is "the local re-run
  evaluated different instances" the publishable finding?)
- Or coordinating with HELM upstream for deterministic instance
  subsampling.

### 8.3 every_eval_ever PR

Submit `dev/repro_helm_aggregate_unknown_dir/` (§ 3.5) plus one of
the proposed fixes. Both bugs (the unknown/ path fallback and the
empty-evaluation_results extractor) are still present in current
`every_eval_ever 0.2.2` (verified via reconversion in this session).

### 8.4 toothbrush / aiq-gpu sync

Pull-and-rerun on aiq-gpu when ready. Push the remaining ~17 commits
in the parent repo. Confirmed during the session: virtiofs setup
between toothbrush (`joncrall@toothbrush`) and aivm-2404
(`agent@aivm-2404`) shares both `~/code/helm_audit/` and `/data/...`,
so no copying needed for those two; aiq-gpu is the third machine
that needs an actual `git pull`.

### 8.5 Paper limitations note

Add a short paragraph explaining:
- The audit operates on `aiq-gpu`-only reproductions (excludes namek/yardrat).
- Pythia-2.8B has limited public-store coverage.
- For benchmarks with non-deterministic instance subsampling (entity_matching,
  synthetic_reasoning_natural), official and local runs evaluated
  different test subsets; we report `join_failed` for these and do
  not fabricate an agreement number.
- Public CRFM EEE artifacts span multiple converter schema versions;
  we standardize on the newest per dir.

---

## 9. Quick command reference

```bash
# 1. Build the heatmap from scratch
cd ~/code/helm_audit
bash reproduce/eee_only_reproducibility_heatmap/00_check_artifacts.sh
bash reproduce/eee_only_reproducibility_heatmap/10_link_tree.sh
WORKERS=8 FAST_AGG_SUMMARY=1 bash reproduce/eee_only_reproducibility_heatmap/20_run.sh
bash reproduce/eee_only_reproducibility_heatmap/30_heatmap.sh

# Outputs:
#   /data/crfm-helm-audit-store/eee-only-reproducibility-heatmap/heatmap/
#     reproducibility_heatmap.png
#     reproducibility_heatmap.txt
#     cell_data.json

# 2. Profile aggregate-summary
LINE_PROFILE=1 eval-audit-build-summary \
    --analysis-root /data/crfm-helm-audit-store/eee-only-reproducibility-heatmap/from_eee_out \
    --index-fpath  /data/crfm-helm-audit-store/eee-only-reproducibility-heatmap/from_eee_out/audit_results_index.csv \
    --summary-root /tmp/profile-test/aggregate-summary \
    --no-filter-inventory \
    --no-canonical-scan
# → profile_output_<timestamp>.txt in CWD

# 3. Dedupe old EEE conversions (paper scope, dry run)
python dev/oneoff/dedupe_old_eee_conversions.py
# After review:
python dev/oneoff/dedupe_old_eee_conversions.py --apply

# 4. Submodule maintenance
make submodule-status              # read-only diagnostic
make pull-submodules               # sync each along its pinned branch
make push-submodules               # push branches with unpushed commits
make configure-submodule-branches  # one-time: pin each to current branch

# 5. Verify the every_eval_ever bug repro (uncommitted)
cd submodules/every_eval_ever
bash dev/repro_helm_aggregate_unknown_dir/repro.sh
# Exit 1 = bug present; exit 0 = fixed.
```

---

## 10. Files changed in this session

```
# Paper-relevant analysis
eval_audit/reports/eee_only_heatmap.py                         (new)
reproduce/eee_only_reproducibility_heatmap/                    (new dir)
  00_check_artifacts.sh
  10_link_tree.sh
  20_run.sh
  30_heatmap.sh
  README.md

# Bug fixes
eval_audit/reports/core_packet_summary.py                      (skip disabled comparisons)
eval_audit/workflows/build_reports_summary.py                  (no-regen + line_profiler)
eval_audit/utils/sankey.py                                     (line_profiler)
eval_audit/workflows/rebuild_core_report.py                    (line_profiler)

# Performance
eval_audit/cli/from_eee.py                                     (--workers)
eval_audit/integrations/kwdagger_bridge.py                     (venv auto-activation)
eval_audit/reports/core_metrics.py                             (component cache)

# Submodule maintenance
Makefile                                                       (new)
dev/scripts/                                                   (new dir)
  submodule-status.sh
  pull-submodules.sh
  push-submodules.sh
  configure-submodule-branches.sh

# Oneoffs
dev/oneoff/dedupe_old_eee_conversions.py                       (new)

# Submodule changes
submodules/cmd_queue/tests/test_submit_log_flag.py             (new)
submodules/kwdagger/tests/test_submit_jobs_log_flag.py         (new)
submodules/kwdagger/tests/test_schedule_cli_log_flag.py        (new)
submodules/kwdagger/kwdagger/schedule.py                       (--log flag, removed rogue reset)
submodules/kwdagger/kwdagger/pipeline.py                       (extra_submitkw log)
submodules/aiq-magnet/magnet/backends/helm/cli/materialize_helm_run.py  (mmlu_pro alias)
eval_audit/helm/run_entries.py                                 (mmlu_pro alias)

# Uncommitted (for upstream PR)
submodules/every_eval_ever/dev/repro_helm_aggregate_unknown_dir/
  README.md
  repro.sh
  make_fixture.sh
  fixture/  (8 files, 2.7 MB)
```
