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
- `qwen35_vllm/`: local vLLM smoke run for `qwen/qwen3.5-9b` through the existing `kwdagger` and materialized HELM path

The shell files here are intentionally thin. They are runbook steps, not the
implementation. Each one should delegate to a `helm_audit` Python CLI such as
`helm-audit-run`, `helm-audit-index`, or `helm-audit-analyze-experiment`.
For `helm-audit-run`, preview is the default. Use `--run=1` in runbooks when
you actually want to execute the scheduled `kwdagger` job.

Generated manifests referenced by these runbooks now default to
`$AUDIT_STORE_ROOT/configs/manifests/` with
`AUDIT_STORE_ROOT=/data/crfm-helm-audit-store` as the fallback. Checked-in
`configs/` files remain source-controlled inputs and overrides, not a sink for
generated experiment state.

The `qwen35_vllm/` runbook assumes:
- a local vLLM OpenAI-compatible server is available on `http://localhost:8000/v1`
- the downstream `materialize_helm_run.py` copies the manifest's `model_deployments_fpath` into `<job_dir>/prod_env/model_deployments.yaml` before invoking `helm-run`
