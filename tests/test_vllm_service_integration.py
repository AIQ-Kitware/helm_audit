from __future__ import annotations

import sys
from pathlib import Path

import yaml

from helm_audit.integrations.vllm_service.adapter import (
    export_benchmark_bundle,
    load_profile_contract,
)


def _import_vllm_config():
    submodule_root = Path(__file__).resolve().parents[1] / "submodules" / "vllm_service"
    if str(submodule_root) not in sys.path:
        sys.path.insert(0, str(submodule_root))
    from vllm_service.config import initial_config, save_yaml

    return initial_config, save_yaml


def _make_vllm_root(tmp_path: Path, *, backend: str = "compose") -> Path:
    initial_config, save_yaml = _import_vllm_config()
    root = tmp_path / "vllm_service_root"
    root.mkdir()
    cfg = initial_config()
    cfg["backend"] = backend
    cfg["state"] = {
        "hf_cache": "state/hf-cache",
        "open_webui": "state/open-webui",
        "postgres": "state/postgres",
        "runtime": "state/runtime",
    }
    save_yaml(root / "config.yaml", cfg)
    save_yaml(root / "models.yaml", {"models": {}, "profiles": {}})
    return root


def test_load_profile_contract_from_vllm_service(tmp_path: Path) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    contract = load_profile_contract(
        "qwen2-72b-instruct-tp2-balanced",
        simulate_hardware="2x96",
        vllm_root=vllm_root,
    )
    assert contract["kind"] == "serving-profile-contract"
    assert contract["profile"]["public_name"] == "qwen2-72b-instruct-tp2-balanced"
    assert contract["services"][0]["model"]["logical_model_name"] == "qwen/qwen2-72b-instruct"


def test_export_bundle_distinguishes_gpt_oss_chat_vs_completions(tmp_path: Path) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    completions = export_benchmark_bundle(
        "gpt-oss-20b-completions",
        bundle_root=tmp_path / "gpt-oss-completions",
        simulate_hardware="1x96",
        vllm_root=vllm_root,
    )
    chat = export_benchmark_bundle(
        "gpt-oss-20b-chat",
        bundle_root=tmp_path / "gpt-oss-chat",
        simulate_hardware="1x96",
        vllm_root=vllm_root,
    )
    completions_doc = yaml.safe_load(completions["model_deployments_path"].read_text())["model_deployments"][0]
    chat_doc = yaml.safe_load(chat["model_deployments_path"].read_text())["model_deployments"][0]
    assert completions_doc["client_spec"]["class_name"].endswith("OpenAILegacyCompletionsClient")
    assert chat_doc["client_spec"]["class_name"].endswith("OpenAIClient")


def test_export_bundle_uses_qwen_direct_vllm_convention(tmp_path: Path) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    result = export_benchmark_bundle(
        "qwen2-72b-instruct-tp2-balanced",
        preset="qwen2_72b_vllm",
        bundle_root=tmp_path / "qwen-bundle",
        simulate_hardware="2x96",
        vllm_root=vllm_root,
    )
    deployment = yaml.safe_load(result["model_deployments_path"].read_text())["model_deployments"][0]
    assert deployment["name"] == "vllm/qwen2-72b-instruct-local"
    assert deployment["client_spec"]["class_name"].endswith("VLLMChatClient")
    assert deployment["client_spec"]["args"]["vllm_model_name"] == "Qwen/Qwen2-72B-Instruct"


def test_machine_local_bundle_uses_absolute_model_deployments_path(tmp_path: Path) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    bundle_root = tmp_path / "machine-local-bundle"
    result = export_benchmark_bundle(
        "gpt-oss-20b-completions",
        preset="gpt_oss_20b_vllm",
        bundle_root=bundle_root,
        simulate_hardware="1x96",
        vllm_root=vllm_root,
    )
    smoke = yaml.safe_load(result["benchmark_smoke_manifest_path"].read_text())
    assert smoke["model_deployments_fpath"] == str((bundle_root / "model_deployments.yaml").resolve())
