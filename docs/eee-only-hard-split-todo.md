# EEE-only hard split — TODO

**Status:** soft separation in place (commit `0403ac3`,
`--skip-diagnosis` / `EVAL_AUDIT_SKIP_HELM_DIAGNOSIS=1`).
Hard split deferred until after the first-pass analysis lands.

**Owner:** Jon. Pick up after the heatmap-paper analysis run is done.

---

## Why this exists

Case Study 3 of the paper claims:

> *"EEE's per-instance schema is sufficient for reproducibility analysis
> at multiple granularities."*

A reviewer will read that and ask: *did the analysis code actually use
only EEE? Or did it secretly fall back to HELM `run_spec.json` when
nobody was looking?*

The current implementation gives the right answer **softly**:
[`eval_audit/normalized/helm_compat.py`](../eval_audit/normalized/helm_compat.py)
provides a `HelmRunView` adapter that lets `HelmRunDiff` consume a
`NormalizedRun`. When a run has no HELM origin, it returns
shape-correct empty defaults, and the diagnosis facts collapse to
`status='unknown'`. That's the right behavior. But it's a graceful
degradation, not an architectural separation. The reviewer has to read
the shim code and trust that no edge case leaks.

We want a **hard split**: the EEE-only entry points (`from_eee`,
`compare_pair_eee`, `build_virtual_experiment` when fed
external-EEE-only sources) should be physically incapable of importing
`eval_audit.helm.*`. Then "did this analysis use HELM?" becomes a
trivial `grep` instead of a code review.

## Current state (post-`--skip-diagnosis`)

What `EVAL_AUDIT_SKIP_HELM_DIAGNOSIS=1` covers:

- [`_build_pair`](../eval_audit/reports/core_metrics.py#L653) bypasses
  `HelmRunDiff` entirely. The agreement-ratio numbers come from
  `eval_audit.normalized.compare` (EEE-native). The `diagnosis` field
  in each pair report is `{}`.

What the flag does **not** cover (still HELM-shaped, even with the
flag set):

- [`pair_samples.write_pair_samples`](../eval_audit/reports/pair_samples.py#L37)
  — instantiates `HelmRunDiff` and calls `summarize_instances` for the
  per-pair text comparison. Falls through `helm_compat`'s empty
  defaults for EEE-only data, but the import is still in scope.
- [`pair_report.py`](../eval_audit/reports/pair_report.py),
  [`quantiles.py`](../eval_audit/reports/quantiles.py),
  [`workflows/compare_batch.py`](../eval_audit/workflows/compare_batch.py)
  — all instantiate `HelmRunDiff`. Same pattern.
- The `from_eee` CLI imports `rebuild_core_report` which imports
  `core_metrics` which imports `HelmRunDiff`. The import is unconditional.

## Goal

Two production analysis paths:

| Path | Imports `eval_audit.helm.*`? | Purpose |
|---|---|---|
| **`eee_only/`** (new namespace) | ❌ No | Paper analysis; consumed by `from_eee`, `compare_pair_eee`, `eee_only_heatmap`, the EEE branch of `build_virtual_experiment` |
| **`helm_driven/`** (current `eval_audit.helm.*` + `eval_audit.reports.*`) | ✅ Yes | HELM converter validation, debugging, the legacy comparison core |

A grep of `eee_only/` for `from eval_audit.helm` returns zero matches. A
runtime check (`sys.modules` after `import eval_audit.cli.from_eee`)
shows no `eval_audit.helm.*` modules loaded.

## Work plan

### 1. Inventory what HELM-shaped fields the EEE path consumes today

Search `eval_audit/` (excluding `helm/` and `cli/index_historic_helm_runs.py`)
for every place that touches:

- `run_spec.json` (HELM artifact)
- `scenario.json`
- `scenario_state.json`
- `stats.json` (HELM-side; EEE has its own equivalent)
- `per_instance_stats.json` (same)
- `_NormalizedJsonView._load`
- `helm_view`, `helm_view_from_path`, `HelmRunView`
- `HelmRunDiff`
- `HelmRunAnalysis`

Sites we already know about (call out as the migration starting list):

- `eval_audit/reports/core_metrics.py:_build_pair` (lines 670-690 — only the `helm_view(...)` + `HelmRunDiff(...)` block when `skip_diagnosis=False`)
- `eval_audit/reports/pair_samples.py:write_pair_samples`
- `eval_audit/reports/pair_report.py`
- `eval_audit/reports/quantiles.py`
- `eval_audit/workflows/compare_batch.py`

### 2. Decide what comparability facts the EEE schema needs to carry natively

The diagnosis labels (`recipe_clean` / `deployment_drift` /
`execution_spec_drift` / `comparability_unknown:*`) come from
`HelmRunDiff._diagnose_repro` ([diff.py:1396](../eval_audit/helm/diff.py#L1396))
which combines facts derived from `run_spec.json`. The relevant fields
are already documented in
[`docs/eee-vs-helm-metadata.md`](eee-vs-helm-metadata.md):

- `scenario_class` (HELM `run_spec.json:scenario_spec.class_name`)
- `model_deployment` (`adapter_spec.model_deployment`)
- `instructions` (`adapter_spec.instructions`)
- `max_eval_instances` (`adapter_spec.max_eval_instances`)
- `benchmark_family`
- run-spec-name string (for the "same recipe" check)
- scenario-spec hash (for the semantic equality check)

**Decision needed**: do we (a) extend the EEE schema to embed these
fields at conversion time, or (b) ship them as a separate sidecar
that the EEE-only loader can consume, or (c) drop the diagnosis
labels from the EEE-only pipeline entirely?

Recommendation: **(a) extend the schema**. The fields are scalar
metadata; the EEE aggregate JSON already has free-form sections for
`evaluator_metadata`. Adding a `recipe_facts` block at conversion
time makes the EEE artifact the single source of truth and removes the
need for `_NormalizedJsonView` to ever look at HELM JSONs.

This requires a coordinated change in
[`submodules/every_eval_ever/`](../submodules/every_eval_ever) — the
converter writes the aggregate JSON. Talk to upstream EEE before
shipping.

### 3. Build the `eee_only/` namespace

Create `eval_audit/eee_only/` containing:

- `compare.py` — wraps `eval_audit.normalized.compare` (which is
  already EEE-native — verify it has no `eval_audit.helm.*` imports).
- `diagnose.py` — re-implements `_diagnose_repro` using only the
  `recipe_facts` block from EEE artifacts. Returns the same label
  shape so downstream consumers (sankey buckets, text reports) work
  unchanged.
- `core_metrics.py` — port of the report renderer that *does not* import
  `HelmRunDiff`, `helm_view`, or anything under `eval_audit.helm.*`.
  Reuses `_load_normalized` / `_load_component_run` from the existing
  module if those are already HELM-free; otherwise port those too.
- `pair_samples.py` — port that uses `eval_audit.normalized.compare`
  for the per-pair instance comparison text instead of
  `HelmRunDiff.summarize_instances`.

Switch the EEE entry points to import from `eval_audit.eee_only.*`:

- `eval_audit/cli/from_eee.py`
- `eval_audit/cli/compare_pair_eee.py`
- `eval_audit/reports/eee_only_heatmap.py`
- `eval_audit/cli/build_virtual_experiment.py` (when manifest declares
  `external_eee` only or `eee_root` is set)

Leave the existing `eval_audit.reports.core_metrics` / `pair_samples` /
etc. in place for the HELM-driven path. Don't delete; the audit pipeline
in `index_historic_helm_runs.py` and the converter-validation flow still
need them.

### 4. Verification gates

Add to `tests/`:

```python
# tests/test_eee_only_isolation.py
def test_from_eee_loads_no_helm_modules():
    # Fresh interpreter so import side-effects from prior tests don't pollute.
    import subprocess, sys, json
    out = subprocess.check_output(
        [sys.executable, "-c",
         "import eval_audit.cli.from_eee, sys, json; "
         "print(json.dumps([m for m in sys.modules if m.startswith('eval_audit.helm')]))"],
        text=True,
    )
    leaked = json.loads(out)
    assert leaked == [], f"EEE-only path leaked HELM imports: {leaked}"


def test_eee_only_grep_clean():
    # Static check: no `eval_audit.helm` imports in eee_only/ or in any of
    # the EEE-only entry points after the split.
    import pathlib, re
    EEE_ONLY_FILES = [
        "eval_audit/eee_only/",
        "eval_audit/cli/from_eee.py",
        "eval_audit/cli/compare_pair_eee.py",
        "eval_audit/reports/eee_only_heatmap.py",
    ]
    pattern = re.compile(r"\bfrom\s+eval_audit\.helm\b|\bimport\s+eval_audit\.helm\b")
    for path in EEE_ONLY_FILES:
        for f in pathlib.Path(path).rglob("*.py") if pathlib.Path(path).is_dir() else [pathlib.Path(path)]:
            text = f.read_text()
            assert not pattern.search(text), f"{f}: imports HELM"
```

Add a fixture-based end-to-end test:

```python
# tests/test_eee_only_no_run_spec.py
def test_from_eee_runs_with_no_run_spec_json(tmp_path):
    # Use the existing eee_only_demo fixture but assert we can render
    # the full report tree without ever touching a run_spec.json.
    # Strace-style: monkey-patch Path.read_text or open() to crash if
    # called on any path containing "run_spec.json".
    ...
```

(The strace-style monkey-patch is the actual proof-of-no-leakage. The
import test is the cheap continuous check that catches accidental
re-imports during refactors.)

### 5. Update the paper section

Once the hard split lands, the paper's methods section can say:

> "All Case Study 3 numbers in this paper are produced by
> `eval_audit.eee_only.*`, a sub-module that does not import any
> `eval_audit.helm.*` symbol. Verification is enforced by
> `tests/test_eee_only_isolation.py`, which asserts that
> `import eval_audit.cli.from_eee` produces no `eval_audit.helm.*`
> entries in `sys.modules`. Static and dynamic checks both pass."

That's a defensible claim under audit. Today's claim with the soft
separation requires a longer caveat.

## Out of scope

- We are **not** breaking the HELM-driven analysis path. It stays as a
  separate tool for converter validation, debugging EEE conversion
  drift, and the converter-sweep flow at
  [`dev/poc/eee-audit/sweep.py`](../dev/poc/eee-audit/sweep.py).
- We are **not** removing the `helm_compat.py` shim. It documents the
  legacy bridge; future readers should still be able to find it. We're
  just removing its in-process callers from the EEE-only path.
- We are **not** changing the EEE artifact contents on the local sweep
  side until upstream EEE has the new `recipe_facts` schema slot. Until
  then, EEE-only runs without `recipe_facts` get `comparability_unknown`
  diagnoses, which is the correct signal.

## Pointers

- Soft-separation landing: commit `0403ac3` (this repo) — the
  `--skip-diagnosis` flag.
- Profile output that motivated the optimization side-effect:
  `profile_output_2026-05-01T105906.txt` (root of repo, ignore from git).
- HELM↔EEE field mapping: [`docs/eee-vs-helm-metadata.md`](eee-vs-helm-metadata.md).
- The compat shim: [`eval_audit/normalized/helm_compat.py`](../eval_audit/normalized/helm_compat.py).
- `_diagnose_repro` (the diagnosis logic to re-implement EEE-natively):
  [`eval_audit/helm/diff.py:1396`](../eval_audit/helm/diff.py#L1396).
- Existing EEE-native compare: `eval_audit/normalized/compare.py` —
  audit this file for any `eval_audit.helm.*` imports as a starting
  point; it should already be clean.

## Order of operations when picking this up

1. Run the current paper analysis with `EVAL_AUDIT_SKIP_HELM_DIAGNOSIS=1`
   set. Confirm the heatmap renders identically to the with-diagnosis
   version (numbers should match exactly — only the `diagnosis` dict
   changes from populated to empty).
2. Inventory step (work plan §1) — produce the actual list of HELM
   touchpoints, with line numbers, in a follow-up doc next to this one.
3. Decide the EEE schema extension (work plan §2) — file an issue on
   `submodules/every_eval_ever/` or coordinate with upstream.
4. Build `eval_audit/eee_only/` (work plan §3), one file at a time.
5. Add the verification tests (work plan §4) before flipping the entry
   points.
6. Re-run the paper analysis through the new path; numbers should
   match the post-step-1 baseline exactly. Diagnoses should populate
   when `recipe_facts` is present in EEE, collapse to
   `comparability_unknown` when absent.
7. Update `docs/helm-reproduction-research-journal.md` with the
   verification result.
8. Update the paper methods section.

If step 6 produces *different* numbers than step 1, **stop**. That's
evidence the soft separation was load-bearing somewhere we didn't
realize and the paper claim needs revisiting before the hard split is
trustworthy.
