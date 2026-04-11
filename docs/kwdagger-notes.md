# kwdagger Notes

Date started: 2026-03-24
Project: `/home/joncrall/code/helm-reproducibility`

## Important Points

- `kwdagger schedule` is correctly building the HELM smoke-test DAG as one node per `run_entry`; for the current smoke manifest that means 6 jobs total.
- With `backend=tmux`, `devices=0,1`, and `tmux_workers=2`, the queue is split into two tmux sessions, one per visible GPU, with jobs serialized within each session.
- `kwdagger` may render node defaults into the generated CLI even when the manifest omits them.
  - Observed examples:
    - `--precomputed_root=None`
    - `--model_deployments_fpath=None`
    - bare `--enable_huggingface_models`
    - bare `--enable_local_huggingface_models`
- Because of that behavior, downstream CLIs should normalize null-like placeholders such as `None`, `null`, and empty strings instead of assuming omitted values stay omitted.
- For the HELM audit node, it is better to omit unset optional params when rendering the final command than to rely only on downstream normalization.
  - The custom node `command` property now drops `None` and empty-list values before building the shell command.
- For list-valued params passed through `kwdagger`, prefer a single key/value YAML string over `nargs='*'`.
  - Current audit/HELM example:
    - `helm.enable_huggingface_models: '["repo-a", "repo-b"]'`
    - `helm.enable_local_huggingface_models: '["/models/a"]'`
  - The pipeline-facing script can decode that with `kwutil.Yaml.coerce`, then expand it into the downstream CLI format if needed.
- `scriptconfig` emitted a smartcast warning for comma-separated `devices`; this did not block scheduling, but it is a hint that list parsing behavior may change in a future version.
- Existing `cmd_queue` tmux sessions with the same queue name can trigger an interactive prompt asking whether older sessions should be killed.
  - This is important for automation and unattended runs.
  - Current mitigation in the audit runner: derive `--queue_name` from `experiment_name` instead of reusing the generic `schedule-eval`.
- The audit comparison step needs to discover completed jobs recursively under the kwdagger results root.
  - Real layout is nested like: `<results>/<node_name>/<job_id>/DONE`
  - A shallow glob like `results/*/DONE` will miss all HELM jobs and incorrectly report `missing_kwdg_match`.

---

## Migration note

This document was originally written in the `aiq-magnet` repository and copied into
`helm-reproducibility` during the repository split.

Original context:
- workspace/repo at time of writing: `/home/joncrall/code/aiq-magnet`
- original audit workflow location: `dev/experiments/audit-helm-reproduction`

Current equivalents in this repo:
- repo root: `/home/joncrall/code/helm-reproducibility`
- workflow roots: `configs/`, `reproduce/`, `helm_audit/`, `reports/`

Unless explicitly stated otherwise, historical paths and commands above should be
interpreted as pre-split references.

---

## Error reporting gap observed in `audit-historic-grid-gpt-oss-20b-vllm`

Date observed: 2026-04-11
Experiment root: `/data/crfm-helm-audit/audit-historic-grid-gpt-oss-20b-vllm`
Manifest: `/data/crfm-helm-audit-store/local-bundles/gpt_oss_20b_vllm/full_manifest.yaml`

Observed behavior:
- `kwdagger schedule` returned `0`, which correctly means scheduling succeeded, but the operator-facing queue summary only surfaced aggregate counts (`passed=2`, `failed=8`) without the actionable failure causes.
- Important failure details were only visible by opening individual tmux session output or per-job files under `helm/helm_id_*/`.
- At least one failed job (`mmlu_pro`) had almost no useful traceback in `helm-run.log` or `helm-run.debug.log`, so the queue summary did not point to the next place an operator should inspect.

Failure families recoverable only by manual artifact inspection:
- Gated dataset access:
  - `gpqa:subset=gpqa_main,use_chain_of_thought=true,use_few_shot=false,model=openai/gpt-oss-20b`
  - HELM raised `DatasetNotFoundError` because `Idavidrein/gpqa` is gated on Hugging Face.
- Missing annotator / judge API key:
  - `anthropic_red_team:model=openai/gpt-oss-20b`
  - `harm_bench:model=openai/gpt-oss-20b`
  - `omni_math:model=openai/gpt-oss-20b`
  - `simple_safety_tests:model=openai/gpt-oss-20b`
  - `xstest:model=openai/gpt-oss-20b`
  - These failed during HELM annotation/execution with `The api_key client option must be set ... or by setting the OPENAI_API_KEY environment variable`.
- Deployment/client mismatch:
  - `wildbench:subset=v2,model=openai/gpt-oss-20b`
  - The local deployment used legacy completions, but this run reached a chat-style request path and ultimately failed with `Either prompt or prompt_embeds must be provided and non-empty`.
- Opaque / poorly surfaced failure:
  - `mmlu_pro:subset=all,use_chain_of_thought=true,use_few_shot=false,model=openai/gpt-oss-20b`
  - The synced per-job HELM logs stop almost immediately after startup, so the real cause is not discoverable from the standard result files alone.

Concrete improvements worth making in `kwdagger` / cmd-queue-facing tooling:
- Distinguish scheduling success from payload success explicitly in the final summary.
  - Example: `schedule_status=success`, `job_status=8 failed of 10`.
- Print the failed `run_entry` values directly in the terminal summary, not just aggregate counts.
- Extract and print one short exception summary per failed job when available.
  - Examples:
    - `gpqa ... -> gated dataset Idavidrein/gpqa`
    - `xstest ... -> missing OPENAI_API_KEY for annotator`
    - `wildbench ... -> completions client received empty prompt`
- Print the exact artifact path to inspect for each failed job.
  - Example: `<result_root>/helm/<job_id>/helm-run.log`
- Group failures by shared cause so operators can see whether they have one broken dependency or many unrelated issues.
- Preserve stderr / uncaught exceptions from the wrapped workload even when the inner tool exits before writing a rich log.
  - The `mmlu_pro` case suggests there is still a path where the operator loses the real traceback.

Practical takeaway:
- The current final queue table is good for "did the batch finish?", but not yet good enough for "what do I fix next?".
- For long HELM reproductions, a post-queue failure digest would save substantial operator time.
