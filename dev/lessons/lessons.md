# Lessons Learned

Append-only. Each entry: **Lesson**, **Evidence / MWE**, **Applies when**.
Supersede incorrect lessons with a new entry rather than rewriting old ones.

---

## vLLM has no `--enable-reasoning` flag; `--reasoning-parser` alone enables it

- **Lesson:** In current vLLM (verified against `vllm/vllm-openai:v0.19.x`),
  the CLI rejects `--enable-reasoning` with
  `vllm: error: unrecognized arguments: --enable-reasoning`. Reasoning
  extraction is enabled by passing `--reasoning-parser <name>` alone.
  Templates and recipes that pair the two flags will fail to start the
  container.
- **Evidence / MWE:** `submodules/vllm_service/vllm_service/templates/docker-compose.yml.j2`
  (only renders `--reasoning-parser` when `reasoning_enabled` and a parser are
  both set) and `submodules/vllm_service/tests/test_serving_profiles.py::test_no_profile_renders_unsupported_enable_reasoning_flag`,
  which sweeps multiple built-in profiles and forbids `--enable-reasoning`.
- **Applies when:** rendering vLLM command lines for any reasoning-capable
  model (Qwen3.x, etc.) on vLLM 0.19+.

## Docker `/docker-entrypoint-initdb.d` only runs on fresh Postgres volumes

- **Lesson:** A one-shot `postgres-init`-style sidecar based on
  `/docker-entrypoint-initdb.d/*.sql` will silently no-op on any
  already-initialized Postgres data directory. For multi-database setups
  that must work on both new and existing volumes, prefer either (a)
  per-database Postgres containers with their own volumes, or (b) an
  idempotent `psql` bootstrap that runs on every `up` and uses
  `CREATE DATABASE ... IF NOT EXISTS`-style guards (Postgres needs
  `SELECT FROM pg_database` + conditional create — `IF NOT EXISTS` is
  not supported on `CREATE DATABASE` in older Postgres).
- **Evidence / MWE:** `submodules/vllm_service` commit `7901fc3` ("Update
  to two postgress dbs") — replaces the shared-Postgres + `postgres-init`
  bootstrap with two separate `postgres-open-webui` / `postgres-litellm`
  services, exactly because the bootstrap path was unreliable on
  pre-existing volumes.
- **Applies when:** designing Compose/K8s Postgres deployments that may
  be applied on top of existing data directories.

## Service-level metadata is dropped unless added to BOTH catalog normalizers

- **Lesson:** In `submodules/vllm_service/vllm_service/catalog.py`, the
  service shape returned by `_normalize_service_from_profile` and
  `_normalize_legacy_profile` is an explicit allow-list — keys that are
  not listed there get silently dropped before the resolver/renderer ever
  see them. New per-service metadata (e.g. `reasoning`, `tool_calling`,
  `chat_compat`) must be added to **both** functions, not just one. A
  YAML-level field that "looks correct" but is missing from one
  normalizer will be a no-op for whichever profile shape uses it.
- **Evidence / MWE:** the chain of commits adding `reasoning`,
  `tool_calling`, and `chat_compat` to the catalog (`vllm_service/catalog.py`)
  — each one had to touch both normalizer return-dicts; tests caught
  the dropped-field case for `tool_calling` specifically (Qwen profile
  failed to render tool flags despite YAML being correct).
- **Applies when:** adding any new per-service field to vllm_service
  profiles. Search for both `_normalize_service_from_profile` and
  `_normalize_legacy_profile`.

## Cite the specific model card for vLLM serving flags, not the family docs

- **Lesson:** Generic family-level documentation (e.g. "Qwen3-Coder" or
  "Qwen3" generic guides) is **not** a substitute for the specific model
  card when picking vLLM tool-call / reasoning parsers. Different
  variants of the same family pin different parsers. For
  `Qwen/Qwen3.6-35B-A3B` the model card prescribes
  `--reasoning-parser qwen3 --enable-auto-tool-choice
  --tool-call-parser qwen3_coder --language-model-only`; substituting
  `qwen3_xml` (a parser name common in generic Qwen3 examples) is wrong
  for this model.
- **Evidence / MWE:** comment block in
  `submodules/vllm_service/vllm_service/templates/default-profiles.yaml`
  on the Qwen service of `pythia-qwen3.6-mixed-4x96`, plus
  `tests/test_serving_profiles.py::test_mixed_profile_qwen_command_arg_order_matches_model_card`
  which locks the exact parser tokens.
- **Applies when:** choosing or reviewing vLLM `--*-parser` flags for
  any new model. Prefer the specific HF model card over upstream family
  docs.

## SPECULATIVE: LiteLLM proxy flattens chat→completions via prompt-template fields

- **Lesson:** In LiteLLM proxy v1.81.x, a model entry whose upstream is
  `text-completion-openai/<model>` can advertise a chat endpoint to the
  client and have LiteLLM flatten the chat messages into a plain prompt
  by setting `initial_prompt_value` / `roles[X].{pre_message,post_message}`
  / `final_prompt_value` in `litellm_params`. This avoids needing a
  vLLM-side `--chat-template` for base/completions models when the
  client (e.g. InspectAI) only speaks `/v1/chat/completions`.
- **Evidence / MWE:** rendered config and unit tests in
  `submodules/vllm_service/vllm_service/templates/litellm_config.yaml.j2`
  and
  `tests/test_serving_profiles.py::test_pythia_inspect_mmlu_compat_renders_completions_with_litellm_template`.
  **No live MWE yet** — the tests assert YAML shape only. A confirming
  MWE would: (1) run `litellm:v1.81.3-stable` against a stub
  `/v1/completions` server, (2) `POST /v1/chat/completions` with multi-message
  payload, (3) assert the upstream received a single `prompt` string
  whose contents are the message `content`s joined by `\n` with no role
  labels. Until that exists, treat as documentation-backed but not
  empirically confirmed in this repo.
- **Applies when:** adding LiteLLM-only chat-compat shims for other
  base/completions models. Reclassify as confirmed once an MWE under
  `dev/lessons/mwe/litellm-flat-messages/` exists.
