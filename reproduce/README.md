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
- `qwen2_72b_vllm/`: local vLLM smoke plus full EWOK historic-grid batch for `qwen/qwen2-72b-instruct` using the `helm-qwen2-72b-instruct` server profile
- `gpt_oss_20b_vllm/`: local LiteLLM-backed vLLM smoke plus targeted overnight batch for the `openai/gpt-oss-20b` runs that were filtered out only because they had no local deployment path
- `small_models_kubeai/`: KubeAI-backed overnight batch that keeps both `qwen/qwen2.5-7b-instruct-turbo` and `lmsys/vicuna-7b-v1.3` live together on the cluster and emits one combined benchmark bundle

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

The `qwen2_72b_vllm/` runbook assumes:
- a local vLLM OpenAI-compatible server is available on `http://localhost:8000/v1`
- the service is launched with the `helm-qwen2-72b-instruct` profile, or equivalently serves `Qwen/Qwen2-72B-Instruct` with the HELM alias `qwen/qwen2-72b-instruct`
- the downstream `materialize_helm_run.py` copies the manifest's `model_deployments_fpath` into `<job_dir>/prod_env/model_deployments.yaml` before invoking `helm-run`

The `gpt_oss_20b_vllm/` runbook assumes:
- the local service is exposed through LiteLLM on `http://localhost:14000/v1`
- `LITELLM_MASTER_KEY` is available, either already exported or via `/data/service/service-repo/vllm/generated/.env`
- the runbook writes a machine-local bundle under `$AUDIT_STORE_ROOT/local-bundles/gpt_oss_20b_vllm/` so secrets and absolute paths do not need to live in checked-in YAML
- the local `gpt-oss` deployment should use the legacy completions path, not chat completions, because the observed chat response shape returned `message.content: null` for this backend/model combination
- chat-oriented runs can still opt into an explicit chat deployment via `model_deployment=litellm/gpt-oss-20b-chat-local` when that is the cleaner scenario-level fit
- the overnight manifest is now trimmed to the in-scope subset and does not schedule benchmarks that require proprietary / credentialed judges by default

The `small_models_kubeai/` runbook assumes:
- the KubeAI chart is already installed and reachable at `KUBEAI_BASE_URL` (default `http://127.0.0.1:8000/openai/v1`, typically via `kubectl port-forward`)
- the `vllm_service` repo is configured for the same KubeAI namespace and can `switch --apply` the `qwen2-5-7b-instruct-turbo-default` and `vicuna-7b-v1-3-no-chat-template` profiles
- applying the second profile is additive on the cluster, so both KubeAI `Model` objects remain resident for the combined overnight manifest
- on `aiq-gpu`, the KubeAI Helm release currently lives in the `default` namespace, so these scripts default `KUBEAI_NAMESPACE=default`
- tonight's runbook also applies an explicit post-deploy patch so both model CRs use `resourceProfile=gpu-single-default:1`, `minReplicas=1`, and `--served-model-name=<public profile name>` to match the routed OpenAI model ids exposed by `/openai/v1/models`
- the benchmark bundle export normalizes HELM-facing tokenizer aliases before writing `model_deployments.yaml`; regenerate the bundle after pulling changes if smoke/full previously failed on tokenizer lookup

Exact `aiq-gpu` flow:

```bash
cd /home/joncrall/code/helm_audit
export KUBEAI_NAMESPACE=default
export KUBEAI_BASE_URL=http://127.0.0.1:8000/openai/v1

bash reproduce/small_models_kubeai/00_check_env.sh
bash reproduce/small_models_kubeai/05_deploy_models.sh
bash reproduce/small_models_kubeai/15_wait_ready.sh
bash reproduce/small_models_kubeai/10_write_bundle.sh
bash reproduce/small_models_kubeai/30_run_smoke.sh
bash reproduce/small_models_kubeai/50_run_full.sh
```

One-command overnight entrypoint:

```bash
cd /home/joncrall/code/helm_audit
export KUBEAI_NAMESPACE=default
export KUBEAI_BASE_URL=http://127.0.0.1:8000/openai/v1
bash reproduce/small_models_kubeai/99_run_tonight.sh
```
