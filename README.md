# eval_audit

`eval_audit` is a Python-first repository for reproducing, auditing, and
reporting on public HELM runs under a local open-weight recipe.

This repo now treats the experiment as its own product:

- `eval_audit/` is the package
- `reproduce/` is the human-readable runbook layer
- `configs/` holds checked-in manifests and overrides only
- `/data/crfm-helm-audit-store` (or `$AUDIT_STORE_ROOT`) holds generated manifests, selection files, inventories, and indexes
- `reports/` holds generated lightweight analysis artifacts we still want to browse in-repo

## Layout

- `eval_audit/cli/`
  Thin CLI wrappers and entrypoints.
- `eval_audit/workflows/`
  End-to-end sequencing for indexing, comparison, and experiment analysis.
- `eval_audit/integrations/`
  External-tool bridges. `kwdagger` friction lives in
  [`eval_audit/integrations/kwdagger_bridge.py`](eval_audit/integrations/kwdagger_bridge.py).
- `eval_audit/helm/`
  Local HELM output readers, analysis, metric categorization, and diff logic.
- `eval_audit/reports/`
  Pair reports, core metric reports, aggregate summaries, and paper label helpers.
  The `eval-audit-compare-pair`, `eval-audit-report-core`, and
  `eval-audit-report-aggregate` entrypoints resolve directly to modules here.
- `eval_audit/utils/`
  Generic Sankey helpers and shared utilities.
- `reproduce/`
  Numbered scenario runbooks for smoke runs, apples-to-apples controls, historic
  grid generation, and cross-machine analysis.

## CLI

The stable CLI surface is intentionally small:

- `eval-audit-check-env`
- `eval-audit-make-manifest`
- `eval-audit-run`
- `eval-audit-index`
- `eval-audit-compare-pair`
- `eval-audit-compare-batch`
- `eval-audit-report-core`
- `eval-audit-report-aggregate`
- `eval-audit-rebuild-core`
- `eval-audit-analyze-experiment`

Additional report-oriented entrypoints are available in `pyproject.toml`.

`eval-audit-run` is inspect-first by default. Use `--run=0` to preview the
generated `kwdagger` invocation and `--run=1` to execute it intentionally.

## Reproduce

Start in:

- [`reproduce/README.md`](reproduce/README.md)
- [`docs/pipeline.md`](docs/pipeline.md)

Those runbooks are the primary operator surface now. They are intentionally
small and shell-light: each step calls a Python CLI entrypoint rather than
relying on repo-specific shell glue.

If you want Plotly JPG/PNG sidecars on a headless Ubuntu 24.04 VM, the repo now
documents and scripts the extra Chrome dependency. Use
[`reproduce/setup/10_install_plotly_chrome_ubuntu2404.sh`](reproduce/setup/10_install_plotly_chrome_ubuntu2404.sh),
then verify with `PYTHONPATH=. python -m eval_audit.cli.check_env --plotly-static-only`.

Generated reports are converging on a family layout under [`reports/`](reports):
- `reports/filtering/`
- `reports/core-run-analysis/`
- `reports/aggregate-summary/`

Generated machine-readable workflow state now defaults to:
- `$AUDIT_STORE_ROOT/configs/run_specs.yaml`
- `$AUDIT_STORE_ROOT/configs/run_details.yaml`
- `$AUDIT_STORE_ROOT/configs/manifests/`
- `$AUDIT_STORE_ROOT/indexes/`

## Notes

- `kwdagger` remains the real external scheduling boundary.
- Runtime execution controls for `kwdagger` are handled in
  [`eval_audit/integrations/kwdagger_bridge.py`](eval_audit/integrations/kwdagger_bridge.py)
  via a small explicit runtime object.
- HELM diff / analysis ownership now lives in `eval_audit/helm/`.
- Generic Sankey machinery lives in:
  - [`eval_audit/utils/sankey.py`](eval_audit/utils/sankey.py)
  - [`eval_audit/utils/sankey_builder.py`](eval_audit/utils/sankey_builder.py)
- One demo-only MAGNeT seam remains in
  [`eval_audit/helm/outputs.py`](eval_audit/helm/outputs.py).
