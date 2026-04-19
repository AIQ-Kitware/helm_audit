from __future__ import annotations

import sys
from pathlib import Path

import pytest
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
        api_key_value="explicit-test-key",
    )
    chat = export_benchmark_bundle(
        "gpt-oss-20b-chat",
        bundle_root=tmp_path / "gpt-oss-chat",
        simulate_hardware="1x96",
        vllm_root=vllm_root,
        api_key_value="explicit-test-key",
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
        api_key_value="explicit-test-key",
    )
    smoke = yaml.safe_load(result["benchmark_smoke_manifest_path"].read_text())
    assert smoke["model_deployments_fpath"] == str((bundle_root / "model_deployments.yaml").resolve())


def test_export_bundle_fails_fast_when_openai_auth_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    with pytest.raises(ValueError, match="LITELLM_MASTER_KEY"):
        export_benchmark_bundle(
            "gpt-oss-20b-completions",
            preset="gpt_oss_20b_vllm",
            bundle_root=tmp_path / "missing-auth",
            simulate_hardware="1x96",
            vllm_root=vllm_root,
        )
    assert not (tmp_path / "missing-auth" / "bundle.yaml").exists()


def test_export_bundle_uses_env_auth_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "env-test-key")
    result = export_benchmark_bundle(
        "gpt-oss-20b-completions",
        preset="gpt_oss_20b_vllm",
        bundle_root=tmp_path / "env-auth",
        simulate_hardware="1x96",
        vllm_root=vllm_root,
    )
    deployment = yaml.safe_load(result["model_deployments_path"].read_text())["model_deployments"][0]
    assert deployment["client_spec"]["args"]["api_key"] == "env-test-key"


def test_export_bundle_uses_explicit_auth_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    result = export_benchmark_bundle(
        "gpt-oss-20b-completions",
        preset="gpt_oss_20b_vllm",
        bundle_root=tmp_path / "explicit-auth",
        simulate_hardware="1x96",
        vllm_root=vllm_root,
        api_key_value="explicit-test-key",
    )
    deployment = yaml.safe_load(result["model_deployments_path"].read_text())["model_deployments"][0]
    assert deployment["client_spec"]["args"]["api_key"] == "explicit-test-key"


def test_qwen_direct_vllm_export_does_not_require_litellm_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    result = export_benchmark_bundle(
        "qwen2-72b-instruct-tp2-balanced",
        preset="qwen2_72b_vllm",
        bundle_root=tmp_path / "qwen-direct",
        simulate_hardware="2x96",
        vllm_root=vllm_root,
    )
    deployment = yaml.safe_load(result["model_deployments_path"].read_text())["model_deployments"][0]
    assert "api_key" not in deployment["client_spec"]["args"]


def test_export_bundle_supports_multi_model_kubeai_overnight_preset(tmp_path: Path) -> None:
    vllm_root = _make_vllm_root(tmp_path)
    result = export_benchmark_bundle(
        "",
        preset="small_models_kubeai_overnight",
        bundle_root=tmp_path / "small-models-kubeai",
        vllm_root=vllm_root,
    )
    deployments = yaml.safe_load(result["model_deployments_path"].read_text())["model_deployments"]
    assert [item["name"] for item in deployments] == [
        "kubeai/qwen2-5-7b-instruct-turbo-default-local",
        "kubeai/vicuna-7b-v1-3-no-chat-template-local",
    ]
    assert deployments[0]["model_name"] == "qwen/qwen2.5-7b-instruct-turbo"
    assert deployments[0]["tokenizer_name"] == "qwen/qwen2.5-7b-instruct"
    assert deployments[0]["tokenizer_name"] != "qwen/qwen2.5-7b-instruct-turbo"
    assert deployments[0]["client_spec"]["class_name"].endswith("OpenAIClient")
    assert deployments[0]["client_spec"]["args"]["base_url"] == "http://127.0.0.1:8000/openai/v1"
    assert deployments[0]["client_spec"]["args"]["openai_model_name"] == "qwen2-5-7b-instruct-turbo-default"
    assert deployments[1]["model_name"] == "lmsys/vicuna-7b-v1.3"
    assert deployments[1]["tokenizer_name"] == "hf-internal-testing/llama-tokenizer"
    assert deployments[1]["tokenizer_name"] != "lmsys/vicuna-7b-v1.3"
    assert deployments[1]["client_spec"]["class_name"].endswith("OpenAILegacyCompletionsClient")
    assert deployments[1]["client_spec"]["args"]["openai_model_name"] == "vicuna-7b-v1-3-no-chat-template"

    bundle = yaml.safe_load(result["bundle_path"].read_text())
    assert [item["public_name"] for item in bundle["profiles"]] == [
        "qwen2-5-7b-instruct-turbo-default",
        "vicuna-7b-v1-3-no-chat-template",
    ]

    overnight = yaml.safe_load(result["benchmark_full_manifest_path"].read_text())
    assert overnight["experiment_name"] == "audit-small-models-kubeai-overnight"
    assert any("model_deployment=kubeai/qwen2-5-7b-instruct-turbo-default-local" in entry for entry in overnight["run_entries"])
    assert any("model_deployment=kubeai/vicuna-7b-v1-3-no-chat-template-local" in entry for entry in overnight["run_entries"])
