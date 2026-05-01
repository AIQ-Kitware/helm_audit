# llama2_70b_helm_audit — local LLaMA-2-70B reproduction for Case Study 3

This runbook brings up the new
[`pythia-llama2-70b-mixed-4x96`](../../submodules/vllm_service/vllm_service/templates/default-profiles.yaml)
serving profile and re-runs the public HELM Classic v0.3.0 LLaMA-2-70B
benchmarks locally so the EEE-only reproducibility heatmap (Case Study
3) can include LLaMA-2-70B as a fourth open-weight model.

## Why a separate profile from `pythia-qwen25-gptoss-mixed-4x96`

LLaMA-2-70B at fp16 weighs ~140 GB and needs `tp=2` across two 96 GB
GPUs. That evicts the gpt-oss-20b service that occupies GPU 0 in the
qwen25-gptoss profile. Public HELM ran LLaMA-2-70B at fp16, so using
tp=2 (rather than INT4/AWQ on a single GPU) keeps the local recipe
matched to the public recipe — no quantization confound in the
reproducibility comparison. The qwen25-gptoss profile is left alone so
gpt-oss work can resume by switching back.

## Hardware assumption

A 4×96 GB GPU host (same target as
`pythia-qwen3.6-mixed-4x96` / `pythia-qwen25-gptoss-mixed-4x96`).

| GPU | Service | Memory | Protocol |
|-----|---------|--------|----------|
| 0+1 | llama2-70b (tp=2) | ~140 GiB total | completions (chat_compat) |
| 2 | pythia-69b | ~16 GiB | completions (chat_compat) |
| 3 | pythia-28b | ~8 GiB | completions (chat_compat) |

The Pythia GPU pinning is identical to the qwen25-gptoss and
qwen3.6-mixed profiles, so a host already running those two Pythia
containers can be switched to this profile without recreating them.

## Workflow

This runbook is structurally identical to
[`reproduce/finish_qwen25_gptoss/`](../finish_qwen25_gptoss/README.md);
clone its step scripts and change two things:

1. `10_start_service.sh`: set `VLLM_PROFILE=pythia-llama2-70b-mixed-4x96`
2. `05_write_bundle.sh`: set the model_deployment for LLaMA-2-70B
   to `meta/llama-2-70b` and select the heatmap's 14 benchmarks
   (boolq, civil_comments, entity_data_imputation, entity_matching,
   gsm, imdb, lsat_qa, mmlu, narrative_qa, quac, synthetic_reasoning,
   synthetic_reasoning_natural, truthful_qa, wikifact).

Then the rest of the qwen25-gptoss step sequence (`00_check_env.sh`,
`02_warmup_data.sh`, `15_validate_server.sh`, `16_curl_test_bundle.sh`,
`20_preview_smoke.sh`, `30_run_smoke.sh`, `40_preview_full.sh`,
`50_run_full.sh`, `60_index_local.sh`, `70_rsync_back.sh`) applies
unchanged.

## Recipe-match notes

- **Context**: 4096 tokens (LLaMA-2 base context, matches public HELM v0.3.0).
- **Quantization**: none (fp16 weights, matches public HELM).
- **Tokenizer**: stock LLaMA-2 from `meta-llama/Llama-2-70b-hf`.
- **Chat template**: none. LLaMA-2-70B is a base model in HELM v0.3.0;
  the profile uses `protocol_mode: completions` with the LiteLLM-only
  `chat_compat: flat_messages` shim so chat-shaped clients can still
  call the service without affecting the prompt seen by vLLM.

## Validating the profile is recognized (any host)

```bash
python3 -c "
import yaml
d = yaml.safe_load(open('submodules/vllm_service/vllm_service/templates/default-profiles.yaml'))
print('pythia-llama2-70b-mixed-4x96' in d['profiles'])
print('helm-llama-2-70b' in d['profiles'])
print('helm-llama-2-13b' in d['profiles'])
"
```

All three should print `True`. The HELM `model_deployments.yaml`
emitted by `eval-audit-make-bundle` then routes
`meta/llama-2-70b` through the LiteLLM router at the local vLLM
service.

## After the run

The standard post-run flow rebuilds the Case Study 3 numbers:

```bash
./reproduce/open_helm_models_reproducibility/compose.sh
./reproduce/open_helm_models_reproducibility/build_summary.sh
./reproduce/eee_only_reproducibility_heatmap/30_heatmap.sh   # PER_METRIC=1 if desired
```

LLaMA-2-13B (~28 GB at fp16) does not need this profile — it fits on
a single GPU and runs through HELM's HuggingFace backend the same way
the existing Pythia-6.9B / Vicuna-7B-v1.3 locals do. Same for
Falcon-7B (~16 GB). Add their run-spec entries to
`configs/virtual-experiments/open-helm-models-reproducibility.yaml`
and re-run the local sweep on any single-GPU host.
