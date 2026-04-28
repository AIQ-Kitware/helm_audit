Below is the implementation brief I would hand to a code agent.

## Goal

Run HELM benchmarks against **`qwen/qwen3.5-9b`** through a **local vLLM OpenAI-compatible server** on **`http://localhost:8000/v1`**, without changing HELM’s Python client code. Upstream HELM already documents local model registration through `prod_env/*.yaml`, already ships `VLLMClient` and `VLLMChatClient`, and already distinguishes that chat models should use `VLLMChatClient`. In the upstream snapshot I inspected, `qwen/qwen3.5-9b` is already present in HELM’s tokenizer and model-metadata configs. ([CRFM HELM][1])   

The local audit wrapper you already have is also sufficient on the scheduling side: your `kwdagger` bridge already accepts `local_path` and `model_deployments_fpath`, and your project notes state that `materialize_helm_run.py` now stages a provided deployment file into `<local_path>/model_deployments.yaml` before invoking `helm-run`.  

## High-confidence conclusion

**Minimal-path answer:** if your HELM mirror is current enough to already contain upstream’s `qwen/qwen3.5-9b` tokenizer and model-metadata entries, then **no HELM code change is needed**. The only required local plugin file is a **`model_deployments.yaml`** entry pointing that logical HELM model at your local vLLM server. ([CRFM HELM][1])   

**Fallback:** if your mirror does **not** yet contain those upstream `qwen/qwen3.5-9b` metadata/tokenizer entries, that is a **mirror sync gap**, not a missing HELM feature. In that case, either sync those two upstream config entries into the mirror, or add equivalent local `model_metadata.yaml` and `tokenizer_configs.yaml`. Your current audit wrapper only automates copying `model_deployments.yaml`, so a fully out-of-tree fallback would require a small wrapper enhancement to also stage those two extra local config files.  

## Important implementation choices

Use **`helm.clients.vllm_client.VLLMChatClient`**, not `VLLMClient`. HELM’s own docs say to use `VLLMClient` for non-chat models and `VLLMChatClient` for chat models, and Qwen3.5 9B is an instruction-following/chat-style text model. HELM’s vLLM client code also explicitly supports `base_url` and `vllm_model_name`. ([CRFM HELM][1])  

Start the vLLM server on **`localhost:8000`** and point HELM to **`http://localhost:8000/v1`**. vLLM’s docs show that base URL, and their server implements both Completions and Chat APIs. ([vLLM][2])

Do **not** launch vLLM with a non-`EMPTY` API key unless you also account for HELM’s hardcoded behavior. HELM’s `VLLMClient`/`VLLMChatClient` path sets the OpenAI client key to **`"EMPTY"`**, so the safest server launch is either **no API key at all** or `--api-key EMPTY`.  ([vLLM][2])

## File layout to add

Create a small local config bundle, for example:

```text
configs/local_models/qwen35_9b_vllm/
  model_deployments.yaml
  manifest.smoke.yaml
  start_vllm.sh
  validate_vllm.py
```

You can put these somewhere else if you prefer. The important thing is that `manifest.smoke.yaml` references an **absolute** `model_deployments_fpath`.

---

## 1) `model_deployments.yaml`

Create this file:

```yaml
model_deployments:
  - name: vllm/qwen3.5-9b-local
    model_name: qwen/qwen3.5-9b
    tokenizer_name: qwen/qwen3.5-9b
    max_sequence_length: 32768
    client_spec:
      class_name: "helm.clients.vllm_client.VLLMChatClient"
      args:
        base_url: "http://localhost:8000/v1"
        vllm_model_name: "Qwen/Qwen3.5-9B"
```

Notes for the agent:

* Keep `model_name` as the logical HELM model: `qwen/qwen3.5-9b`.
* Keep `tokenizer_name` aligned to the same HELM tokenizer entry.
* `vllm_model_name` should match the model string the server is actually serving.
* `max_sequence_length` should match your server launch constraints. `32768` is a reasonable placeholder, but the agent should lower or raise it to match actual GPU capacity and vLLM launch settings.
* This is the only local plugin file needed in the no-core-change path. ([CRFM HELM][1]) 

---

## 2) `start_vllm.sh`

Create this file:

```bash
#!/usr/bin/env bash
set -euo pipefail

MODEL_NAME="${MODEL_NAME:-Qwen/Qwen3.5-9B}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# IMPORTANT:
# HELM's VLLM client sends api_key="EMPTY".
# Easiest path is to omit --api-key entirely.
# If you insist on auth, use: --api-key EMPTY

exec vllm serve "${MODEL_NAME}" \
  --host "${HOST}" \
  --port "${PORT}"
```

If you need tensor parallelism or memory controls, extend it, for example:

```bash
exec vllm serve "${MODEL_NAME}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --tensor-parallel-size "${TP_SIZE:-1}" \
  --max-model-len "${MAX_MODEL_LEN:-32768}" \
  --gpu-memory-utilization "${GPU_MEM_UTIL:-0.9}"
```

vLLM’s docs show `vllm serve ...` and the client base URL `http://localhost:8000/v1`. They also note that chat requests require a usable chat template; if the model lacks one, launch with `--chat-template`. If chat requests fail with a template-related error, add `--chat-template /path/to/template.jinja`. ([vLLM][2])

---

## 3) `validate_vllm.py`

Create a smoke-test client:

```python
from openai import OpenAI

def main() -> None:
    client = OpenAI(
        base_url="http://localhost:8000/v1",
        api_key="EMPTY",
    )
    resp = client.chat.completions.create(
        model="Qwen/Qwen3.5-9B",
        messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        max_tokens=8,
        temperature=0.0,
    )
    text = resp.choices[0].message.content
    print(text)

if __name__ == "__main__":
    main()
```

Success criterion: this returns a normal chat completion without transport or chat-template errors. vLLM documents this exact OpenAI-compatible pattern and URL. ([vLLM][2])

---

## 4) `manifest.smoke.yaml`

This is the cleanest path through your existing `eval_audit` / `kwdagger` bridge:

```yaml
experiment_name: audit-qwen35-9b-vllm-smoke

run_entries:
  - "mmlu:subject=anatomy,method=multiple_choice_joint,model=qwen/qwen3.5-9b"

max_eval_instances: 5
precomputed_root: null
suite: audit-qwen35-9b-vllm-smoke
require_per_instance_stats: true
mode: compute_if_missing
materialize: symlink
local_path: prod_env

# Use an absolute path here
model_deployments_fpath: "/ABS/PATH/TO/configs/local_models/qwen35_9b_vllm/model_deployments.yaml"

devices: "0"
tmux_workers: 1
backend: tmux
```

Why this works:

* your manifest-to-kwdagger bridge already forwards `local_path`;
* it also forwards `model_deployments_fpath`;
* your wrapper notes say the deployment file gets copied into `<local_path>/model_deployments.yaml` before `helm-run`.  

---

## Direct manual HELM smoke test

Before involving `eval_audit`, the agent should also validate the HELM side directly in a throwaway directory:

```bash
mkdir -p /tmp/helm-qwen35-smoke/prod_env
cp configs/local_models/qwen35_9b_vllm/model_deployments.yaml /tmp/helm-qwen35-smoke/prod_env/model_deployments.yaml
cd /tmp/helm-qwen35-smoke

helm-run \
  --run-entry "mmlu:subject=anatomy,method=multiple_choice_joint,model=qwen/qwen3.5-9b" \
  --suite audit-qwen35-direct \
  --max-eval-instances 5 \
  --disable-cache
```

HELM’s docs explicitly recommend local model registration through `prod_env/*.yaml` and recommend a small `helm-run` smoke test with `--disable-cache` after adding a model. ([CRFM HELM][1])

If that succeeds, then run the audit wrapper path.

---

## Run through `eval_audit`

Given your current bridge, the agent can schedule the smoke manifest directly. Your bridge builds a kwdagger matrix containing `helm.run_entry`, `helm.max_eval_instances`, `helm.local_path`, and, when present, `helm.model_deployments_fpath`. 

Example:

```bash
python -m eval_audit.integrations.kwdagger_bridge \
  # or whatever entrypoint you use to schedule manifests
```

Or, if you already have a manifest runner:

```bash
eval-audit-run \
  --experiment_name audit-qwen35-9b-vllm-smoke \
  --manifests_dpath /ABS/PATH/TO/manifests \
  --max_jobs 1 \
  --run 1
```

Your pipeline docs describe this manifest → kwdagger → per-run job directory flow, and the produced run directory should include `run_spec.json`, `scenario_state.json`, `stats.json`, `per_instance_stats.json`, and logs. 

---

## What the agent should verify after a successful run

The agent should inspect the produced `run_spec.json` and confirm:

* `adapter_spec.model == "qwen/qwen3.5-9b"`
* `adapter_spec.model_deployment` resolves to your local deployment
* the run directory contains `stats.json` and `per_instance_stats.json`

This is worth doing because your project’s recent analysis already identified `adapter_spec.model_deployment` as one of the main remaining drift points in apples-to-apples reproductions, so it is important to make the deployment explicit and verify it landed as intended. 

---

## Optional fallback files if the mirror is stale

Only do this if the HELM mirror does **not** already contain `qwen/qwen3.5-9b` in built-in tokenizer and model metadata.

### `model_metadata.yaml`

```yaml
models:
  - name: qwen/qwen3.5-9b
    display_name: Qwen3.5 9B
    description: Local Qwen3.5 9B served through vLLM.
    creator_organization_name: Qwen
    access: open
    num_parameters: 10000000000
    release_date: 2026-03-02
    tags: [TEXT_MODEL_TAG, LIMITED_FUNCTIONALITY_TEXT_MODEL_TAG, INSTRUCTION_FOLLOWING_MODEL_TAG]
```

### `tokenizer_configs.yaml`

```yaml
tokenizer_configs:
  - name: qwen/qwen3.5-9b
    tokenizer_spec:
      class_name: "helm.tokenizers.huggingface_tokenizer.HuggingFaceTokenizer"
    end_of_text_token: "<|im_end|>"
    prefix_token: ""
```

These are only fallback copies of what upstream main already has in the snapshot I inspected.  

### If you need these fallback files

Your current wrapper only advertises automatic staging for `model_deployments_fpath`, not the metadata/tokenizer files. So the agent has two choices:

1. patch the HELM mirror checkout directly, or
2. extend your wrapper with two more optional manifest fields:

   * `model_metadata_fpath`
   * `tokenizer_configs_fpath`

The implementation should mirror the existing `model_deployments_fpath` behavior: create `<local_path>` if needed, then copy those files into:

* `<local_path>/model_metadata.yaml`
* `<local_path>/tokenizer_configs.yaml`

That is a small wrapper enhancement, not a HELM feature change. 

---

## One repo-local thing that may still need changing

If you want `qwen/qwen3.5-9b` to flow through your **automated discovery/filtering** stage, your current `index_historic_helm_runs.py` logic is probably too narrow. It currently considers a model locally eligible only if it already has a Hugging Face client deployment or appears in `KNOWN_HF_OVERRIDES`, and that override set currently lists three older Qwen models, not `qwen/qwen3.5-9b`. 

So the agent should treat this as optional, depending on workflow:

* **If you are manually specifying run entries or manifests:** no change needed.
* **If you want automated selection to include qwen3.5-9b:** update that filter logic.

The cleanest patch is to stop hardcoding “HF-only” as the only acceptable local path. Rename the notion from something like `has_local_hf_path` to `has_local_supported_path`, and allow either:

* built-in HuggingFace client deployments, or
* a manually declared local-override allowlist that includes `qwen/qwen3.5-9b` for vLLM.

---

## Recommended implementation order for the agent

1. Check whether the HELM mirror already contains `qwen/qwen3.5-9b` in built-in metadata/tokenizer config.
2. Add `configs/local_models/qwen35_9b_vllm/model_deployments.yaml`.
3. Launch vLLM on `127.0.0.1:8000` without a non-`EMPTY` API key.
4. Run `validate_vllm.py`.
5. Run the direct HELM smoke test in a temp dir with `prod_env/model_deployments.yaml`.
6. Add the audit manifest and run it through your existing wrapper.
7. Inspect the produced `run_spec.json` to confirm local deployment resolution.
8. Only if automated filtering matters, patch `index_historic_helm_runs.py` to recognize `qwen/qwen3.5-9b` as locally runnable through your vLLM route.   

## Final bottom line

For **upstream HELM main**, the feature gap is already closed: HELM already has the vLLM client classes and already knows about `qwen/qwen3.5-9b` as a model/tokenizer. The implementation you need is a **local deployment registration** plus a **local vLLM server**, and your current `eval_audit` wrapper already has the manifest plumbing to inject that deployment file per job. ([CRFM HELM][1])     

If you want, I can turn this into a literal patch plan with filename-by-filename diffs.

[1]: https://crfm-helm.readthedocs.io/en/latest/adding_new_models/ "Adding New Models - CRFM HELM"
[2]: https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html "OpenAI-Compatible Server - vLLM"
