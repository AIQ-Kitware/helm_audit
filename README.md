# helm_audit

`helm_audit` is a Python-first repository for reproducing, auditing, and
reporting on public HELM runs under a local open-weight recipe.

This repo now treats the experiment as its own product:

- `helm_audit/` is the package
- `reproduce/` is the human-readable runbook layer
- `configs/` holds checked-in manifests and overrides
- `reports/` holds generated lightweight analysis artifacts

## Layout

- `helm_audit/cli/`
  Thin CLI wrappers and entrypoints.
- `helm_audit/workflows/`
  End-to-end sequencing for indexing, comparison, and experiment analysis.
- `helm_audit/integrations/`
  External-tool bridges. `kwdagger` friction lives in
  [kwdagger_bridge.py](/home/joncrall/code/helm-reproducibility/helm_audit/integrations/kwdagger_bridge.py).
- `helm_audit/helm/`
  Local HELM output readers, analysis, metric categorization, and diff logic.
- `helm_audit/reports/`
  Pair reports, core metric reports, aggregate summaries, and paper label helpers.
- `helm_audit/utils/`
  Generic Sankey helpers and shared utilities.
- `reproduce/`
  Numbered scenario runbooks for smoke runs, apples-to-apples controls, historic
  grid generation, and cross-machine analysis.

## CLI

The stable CLI surface is intentionally small:

- `helm-audit-check-env`
- `helm-audit-make-manifest`
- `helm-audit-run`
- `helm-audit-index`
- `helm-audit-compare-pair`
- `helm-audit-compare-batch`
- `helm-audit-report-core`
- `helm-audit-analyze-experiment`

Additional report-oriented entrypoints are available in `pyproject.toml`.

## Reproduce

Start in:

- [reproduce/README.md](/home/joncrall/code/helm-reproducibility/reproduce/README.md)

Those runbooks are the primary operator surface now. They are intentionally
small and shell-light: each step calls a Python CLI entrypoint rather than
relying on repo-specific shell glue.

## Notes

- `kwdagger` remains the real external scheduling boundary.
- HELM diff / analysis ownership now lives in `helm_audit/helm/`.
- Generic Sankey machinery lives in:
  - [sankey.py](/home/joncrall/code/helm-reproducibility/helm_audit/utils/sankey.py)
  - [sankey_builder.py](/home/joncrall/code/helm-reproducibility/helm_audit/utils/sankey_builder.py)
- One demo-only MAGNeT seam remains in
  [helm_audit/helm/outputs.py](/home/joncrall/code/helm-reproducibility/helm_audit/helm/outputs.py).
