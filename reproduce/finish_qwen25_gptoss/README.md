# finish_qwen25_gptoss — close the Qwen 2.5 + gpt-oss audit gaps

This runbook closes the two remaining open-weight HELM reproducibility
gaps surfaced by Case Study 3 (the EEE NeurIPS paper appendix and
[`reproduce/open_helm_models_reproducibility/REPRODUCIBILITY_REPORT.md`](../open_helm_models_reproducibility/REPRODUCIBILITY_REPORT.md)):

- **Qwen 2.5 7B Instruct** is currently 2 / 38 recipe-clean. The other
  36 packets are `execution_spec_drift` because the local audit didn't
  replicate the prompt-prefix the public Qwen runs use; rerunning here
  pulls the public run_specs (with the prefix intact) and re-executes
  them against the local vLLM service. This runbook also covers the 9
  unique `lite/v1.9.0` Qwen run_entries that have no local repro at
  all (natural_qa × 2; the 7 math entries are disabled — see Caveats).
- **gpt-oss 20B** is currently 0 / 2 recipe-clean. This runbook covers
  the 8 missing `capabilities/v1.12.0` and `safety/v1.14.0` entries.

The runbook brings up a four-service vLLM profile that co-resides
Qwen 2.5 + gpt-oss alongside the two Pythia services someone else is
already using on the same `aiq-gpu` host (the existing
`pythia-qwen3.6-mixed-4x96` profile has Pythia 6.9B on GPU 2 and
Pythia 2.8B on GPU 3; this runbook's profile keeps those two services
in place and rotates Qwen 3.6 35B on GPUs 0–1 out for Qwen 2.5 7B on
GPU 1 and gpt-oss 20B on GPU 0).

## Hardware assumption

A 4×96 GB GPU host (the original `pythia-qwen3.6-mixed-4x96` recipe's
target). On 24 GB-class GPUs (3090, 4090, A4000), gpt-oss 20B will not
fit on a single GPU and the profile would need to be respecialized
with `tensor_parallel_size: 2` for the gpt-oss service — not a path
this runbook supports today.

## Steps

```bash
./00_check_env.sh         # eval-audit-check-env + verify GPU layout
./02_warmup_data.sh       # pre-cache the HF datasets HELM needs (natural_qa, gpqa, ifeval, ...)
./05_write_bundle.sh      # write the eval-audit benchmark bundle
./10_start_service.sh     # vllm_service: switch to the new profile
./15_validate_server.sh   # smoke-test the LiteLLM router with $LITELLM_MASTER_KEY from env
./16_curl_test_bundle.sh  # smoke-test using the *exact* api_key/base_url HELM will use (reads bundle's model_deployments.yaml)
./20_preview_smoke.sh     # eval-audit-run --run=0 (dry-run) on the smoke manifest
./30_run_smoke.sh         # eval-audit-run --run=1 on the smoke manifest (~5 instances per model)
./40_preview_full.sh      # dry-run the full 24-entry manifest
./50_run_full.sh          # execute the full manifest (long; max_eval_instances=1000)
./60_index_local.sh       # eval-audit-index after the run, before rsync
./70_rsync_back.sh        # push the run dir + refreshed index back to the analysis host
```

Each step is idempotent (compute_if_missing skips DONE markers).

## Inputs and outputs

```
$AUDIT_STORE_ROOT/local-bundles/finish_qwen25_gptoss/
├── full_manifest.yaml          # eval-audit-run input (full)
├── smoke_manifest.yaml    # eval-audit-run input (smoke)
├── run_details.yaml       # transport bindings (LiteLLM URL, deployment names)
└── README.md              # auto-generated bundle description

$AUDIT_RESULTS_ROOT/audit-finish-qwen25-gptoss/         # full run output
$AUDIT_RESULTS_ROOT/audit-finish-qwen25-gptoss-smoke/   # smoke output
$AUDIT_STORE_ROOT/indexes/audit_results_index.csv       # refreshed by 60_index_local.sh
```

## Override knobs

- `AUDIT_STORE_ROOT` (default `/data/crfm-helm-audit-store`)
- `AUDIT_RESULTS_ROOT` (default `/data/crfm-helm-audit`)
- `LITELLM_BASE_URL` (default `http://localhost:14000`) — the LiteLLM
  router URL the bundle's transport configuration will encode.
- `LITELLM_ENV_FPATH` (default
  `/data/service/service-repo/vllm/generated/.env`) — sourced by
  `05_write_bundle.sh` so `LITELLM_MASTER_KEY` is in env when the
  bundle is written.
- `MAX_EVAL_INSTANCES` (default `1000`) — clamp on the full run size.
- `RSYNC_DEST` — the destination URI for `70_rsync_back.sh`
  (e.g. `joncrall@analysis-host:/data/crfm-helm-audit/`).
- `RSYNC_DEST_INDEXES` — destination for the indexes
  (e.g. `joncrall@analysis-host:/data/crfm-helm-audit-store/indexes/`).

## Caveats

- **MATH benchmark is disabled** in this preset.
  HELM's `math:` runs load `hendrycks/competition_math` from
  HuggingFace at run time, and that dataset is not reliably reachable
  from `aiq-gpu` today. The 7 math run_entries (algebra,
  counting_and_probability, geometry, intermediate_algebra,
  number_theory, prealgebra, precalculus — all level=1, CoT=True) are
  commented out of the `finish_qwen25_gptoss` preset's full manifest,
  and the smoke manifest's Qwen entry uses `mmlu:us_foreign_policy`
  instead. To re-enable, restore the 7 entries in
  `eval_audit/integrations/vllm_service/adapter.py` and add
  `hendrycks/competition_math` back to the warmup list in
  `02_warmup_data.sh`.

  The same Hub-cache workaround applies if any other HF-backed
  benchmark (`gpqa`, `mmlu_pro`, `omni_math`, etc.) starts failing
  for the same reason: pre-cache with
  `huggingface-cli download <repo> --repo-type dataset`.

- **HELM-version requirement for the safety entries**:
  `anthropic_red_team`, `harm_bench`, `simple_safety_tests`, and
  `xstest` are on suite `safety/v1.14.0`. The local `crfm-helm`
  install on `aiq-gpu` may not yet support these scenarios; verify by
  running `helm-run --help` and looking for the `RunSpec` parser
  registry, or check `helm-run --list-run-spec-functions`. If the
  scenarios resolve, `50_run_full.sh` will execute them; if not, only
  the resolvable subset will run and the missing ones will surface as
  failed manifest entries in the run output.
- **Pythia services stay running.** This profile keeps `pythia-69b`
  on GPU 2 and `pythia-28b` on GPU 3 with the same chat-compat shim
  the existing `pythia-qwen3.6-mixed-4x96` profile uses, so any
  in-flight InspectAI / Inspect Evals MMLU work on those models is
  unaffected by the profile switch. Only the GPU 0–1 services rotate
  (Qwen 3.6 35B → gpt-oss 20B + Qwen 2.5 7B).
- **rsync flow**: this runbook is intended to run on `aiq-gpu` and
  rsync the results back to the analysis host. `60_index_local.sh`
  refreshes the local audit index *on the runbook host* before the
  rsync so the index travels with the run data; the analysis host's
  index gets the same content via `RSYNC_DEST_INDEXES`. After the
  rsync, on the analysis host:
  ```bash
  ./reproduce/open_helm_models_reproducibility/compose.sh
  ./reproduce/open_helm_models_reproducibility/build_summary.sh
  ```
  rebuilds the Case Study 3 numbers including the new entries.
