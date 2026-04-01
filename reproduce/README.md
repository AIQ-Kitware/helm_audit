# reproduce/

This directory is the operator runbook layer for `helm_audit`.

Each scenario folder is a short numbered sequence:

- `00_*`: environment checks or indexing setup
- `10_*`: manifest generation or analysis selection
- `20_*`: execution or rebuild step
- `30_*`: comparison or follow-on reporting

Current scenarios:

- `smoke/`: minimal end-to-end sanity run
- `apples/`: apples-to-apples reproduction control
- `historic_grid/`: historic public-run manifest and rebuild flow
- `machine_compare/`: cross-machine indexing, analysis, and pairwise compare

The shell files here are intentionally thin. They are runbook steps, not the
implementation. Each one should delegate to a `helm_audit` Python CLI such as
`helm-audit-run`, `helm-audit-index`, or `helm-audit-analyze-experiment`.
