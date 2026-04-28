"""
Checked-in registry of models expected to be locally servable.

Each entry documents whether we have a local serving recipe for a model and
whether it participates in the main public HELM reproducibility storyline.

Sources: PRESET_CONFIGS in integrations/vllm_service/adapter.py and
KNOWN_HF_OVERRIDES in cli/index_historic_helm_runs.py.

Semantics:
  replaces_helm_deployment non-null  → main public HELM storyline model; we are
      reproducing public HELM runs for this model via a local serving recipe.
  replaces_helm_deployment null      → local/off-story extension; not a public
      HELM open-weight model in the main storyline.

TODO: Add runtime verification that vllm_service profiles can actually switch to
the relevant profile and serve the listed models on a target machine.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LocalModelEntry:
    model: str
    expected_local_served: bool
    replaces_helm_deployment: str | None
    source: str
    notes: str = field(default="")

    @property
    def is_on_story(self) -> bool:
        """True iff this model reproduces a public HELM deployment (main storyline)."""
        return self.replaces_helm_deployment is not None


LOCAL_MODEL_REGISTRY: list[LocalModelEntry] = [
    # --- On-story: public HELM open-weight models with local serving recipes ---
    LocalModelEntry(
        model="qwen/qwen2.5-7b-instruct-turbo",
        expected_local_served=True,
        replaces_helm_deployment="qwen/qwen2.5-7b-instruct-turbo",
        source="preset:small_models_kubeai_overnight",
    ),
    LocalModelEntry(
        model="lmsys/vicuna-7b-v1.3",
        expected_local_served=True,
        replaces_helm_deployment="lmsys/vicuna-7b-v1.3",
        source="preset:small_models_kubeai_overnight",
    ),
    LocalModelEntry(
        model="qwen/qwen2-72b-instruct",
        expected_local_served=True,
        replaces_helm_deployment="qwen/qwen2-72b-instruct",
        source="preset:qwen2_72b_vllm",
    ),
    LocalModelEntry(
        model="qwen/qwen2.5-72b-instruct-turbo",
        expected_local_served=True,
        replaces_helm_deployment="qwen/qwen2.5-72b-instruct-turbo",
        source="known_hf_overrides",
        notes="In KNOWN_HF_OVERRIDES so already passes Stage 1 deployment check.",
    ),
    # --- Off-story: local extensions not in the public HELM storyline ---
    LocalModelEntry(
        model="openai/gpt-oss-20b",
        expected_local_served=True,
        replaces_helm_deployment=None,
        source="preset:gpt_oss_20b_vllm",
        notes="Off-story local extension served via LiteLLM/vLLM; not a public HELM open-weight model.",
    ),
]


def local_model_registry_by_name() -> dict[str, LocalModelEntry]:
    return {entry.model: entry for entry in LOCAL_MODEL_REGISTRY}
