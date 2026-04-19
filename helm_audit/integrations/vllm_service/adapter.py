from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from helm_audit.infra.paths import audit_store_root, repo_root
from helm_audit.infra.yaml_io import dump_yaml


PRESET_CONFIGS: dict[str, dict[str, Any]] = {
    "gpt_oss_20b_vllm": {
        "profile": "gpt-oss-20b-completions",
        "bundle_name": "gpt_oss_20b_vllm",
        "access_kind": "openai-compatible",
        "model_deployment_name": "litellm/gpt-oss-20b-local",
        "smoke_manifest": {
            "experiment_name": "audit-gpt-oss-20b-vllm-smoke",
            "description": "Smoke-test HELM batch for openai/gpt-oss-20b through the local LiteLLM-backed vLLM service.",
            "run_entries": [
                "ifeval:model=openai/gpt-oss-20b,model_deployment=litellm/gpt-oss-20b-local",
                "bbq:subject=all,method=multiple_choice_joint,max_train_instances=0,model=openai/gpt-oss-20b,model_deployment=litellm/gpt-oss-20b-local",
            ],
            "suite": "audit-gpt-oss-20b-vllm-smoke",
            "max_eval_instances": 5,
        },
        "full_manifest": {
            "experiment_name": "audit-historic-grid-gpt-oss-20b-vllm-trimmed",
            "description": "Targeted in-scope historic-grid extension for openai/gpt-oss-20b using the local LiteLLM-backed vLLM service.",
            "run_entries": [
                "bbq:subject=all,method=multiple_choice_joint,max_train_instances=0,model=openai/gpt-oss-20b,model_deployment=litellm/gpt-oss-20b-local",
                "ifeval:model=openai/gpt-oss-20b,model_deployment=litellm/gpt-oss-20b-local",
                "mmlu_pro:subset=all,use_chain_of_thought=true,use_few_shot=false,model=openai/gpt-oss-20b,model_deployment=litellm/gpt-oss-20b-local",
            ],
            "suite": "audit-historic-grid-gpt-oss-20b-vllm-trimmed",
            "max_eval_instances": 1000,
        },
    },
    "qwen2_72b_vllm": {
        "profile": "qwen2-72b-instruct-tp2-balanced",
        "bundle_name": "qwen2_72b_vllm",
        "backend": "compose",
        "access_kind": "vllm-direct",
        "model_deployment_name": "vllm/qwen2-72b-instruct-local",
        "smoke_manifest": {
            "experiment_name": "audit-qwen2-72b-vllm-smoke",
            "description": "Smoke-test HELM batch for qwen/qwen2-72b-instruct through a local vLLM server.",
            "run_entries": [
                "ewok:domain=agent_properties,model=qwen/qwen2-72b-instruct",
            ],
            "suite": "audit-qwen2-72b-vllm-smoke",
            "max_eval_instances": 5,
        },
        "full_manifest": {
            "experiment_name": "audit-historic-grid-qwen2-72b-vllm",
            "description": "Historic-grid reproduction batch for qwen/qwen2-72b-instruct through a local vLLM server.",
            "run_entries": [
                "ewok:domain=agent_properties,model=qwen/qwen2-72b-instruct",
                "ewok:domain=material_dynamics,model=qwen/qwen2-72b-instruct",
                "ewok:domain=material_properties,model=qwen/qwen2-72b-instruct",
                "ewok:domain=physical_dynamics,model=qwen/qwen2-72b-instruct",
                "ewok:domain=physical_interactions,model=qwen/qwen2-72b-instruct",
                "ewok:domain=physical_relations,model=qwen/qwen2-72b-instruct",
                "ewok:domain=quantitative_properties,model=qwen/qwen2-72b-instruct",
                "ewok:domain=social_interactions,model=qwen/qwen2-72b-instruct",
                "ewok:domain=social_properties,model=qwen/qwen2-72b-instruct",
                "ewok:domain=social_relations,model=qwen/qwen2-72b-instruct",
                "ewok:domain=spatial_relations,model=qwen/qwen2-72b-instruct",
            ],
            "suite": "audit-historic-grid-qwen2-72b-vllm",
            "max_eval_instances": 1000,
        },
    },
    "small_models_kubeai_overnight": {
        "bundle_name": "small_models_kubeai_overnight",
        "backend": "kubeai",
        "profiles": [
            {
                "profile": "qwen2-5-7b-instruct-turbo-default",
                "model_deployment_name": "kubeai/qwen2-5-7b-instruct-turbo-default-local",
                "helm_model_name": "qwen/qwen2.5-7b-instruct-turbo",
                "helm_tokenizer_name": "qwen/qwen2.5-7b-instruct",
            },
            {
                "profile": "vicuna-7b-v1-3-no-chat-template",
                "model_deployment_name": "kubeai/vicuna-7b-v1-3-no-chat-template-local",
                "helm_model_name": "lmsys/vicuna-7b-v1.3",
                "helm_tokenizer_name": "hf-internal-testing/llama-tokenizer",
            },
        ],
        "smoke_manifest": {
            "experiment_name": "audit-small-models-kubeai-smoke",
            "description": "Smoke-test batch for the small KubeAI-served Qwen 2.5 7B and Vicuna 7B profiles.",
            "run_entries": [
                "ifeval:model=qwen/qwen2.5-7b-instruct-turbo,model_deployment=kubeai/qwen2-5-7b-instruct-turbo-default-local",
                "boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical,model_deployment=kubeai/vicuna-7b-v1-3-no-chat-template-local",
            ],
            "suite": "audit-small-models-kubeai-smoke",
            "max_eval_instances": 5,
        },
        "full_manifest": {
            "experiment_name": "audit-small-models-kubeai-overnight",
            "description": "Targeted overnight batch for the KubeAI-served Qwen 2.5 7B and Vicuna 7B profiles.",
            "run_entries": [
                "commonsense:dataset=openbookqa,method=multiple_choice_joint,model=qwen/qwen2.5-7b-instruct-turbo,model_deployment=kubeai/qwen2-5-7b-instruct-turbo-default-local",
                "gsm:model=qwen/qwen2.5-7b-instruct-turbo,stop=none,model_deployment=kubeai/qwen2-5-7b-instruct-turbo-default-local",
                "med_qa:model=qwen/qwen2.5-7b-instruct-turbo,model_deployment=kubeai/qwen2-5-7b-instruct-turbo-default-local",
                "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=qwen/qwen2.5-7b-instruct-turbo,model_deployment=kubeai/qwen2-5-7b-instruct-turbo-default-local",
                "narrative_qa:model=qwen/qwen2.5-7b-instruct-turbo,model_deployment=kubeai/qwen2-5-7b-instruct-turbo-default-local",
                "boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical,model_deployment=kubeai/vicuna-7b-v1-3-no-chat-template-local",
                "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical,model_deployment=kubeai/vicuna-7b-v1-3-no-chat-template-local",
                "narrative_qa:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical,model_deployment=kubeai/vicuna-7b-v1-3-no-chat-template-local",
            ],
            "suite": "audit-small-models-kubeai-overnight",
            "max_eval_instances": 1000,
        },
    },
}


def vllm_service_root() -> Path:
    return repo_root() / "submodules" / "vllm_service"


def _ensure_importable_vllm_service(root: Path | None = None) -> None:
    package_root = str((root or vllm_service_root()).resolve())
    if package_root not in sys.path:
        sys.path.insert(0, package_root)


def _import_vllm_contracts(root: Path | None = None) -> Any:
    _ensure_importable_vllm_service(root)
    return importlib.import_module("vllm_service.contracts")


def load_profile_contract(
    profile: str,
    *,
    backend: str | None = None,
    simulate_hardware: str | None = None,
    vllm_root: Path | None = None,
) -> dict[str, Any]:
    root = (vllm_root or vllm_service_root()).resolve()
    contracts = _import_vllm_contracts(root)
    return contracts.load_profile_contract(
        profile,
        root=root,
        backend=backend,
        simulate_hardware_spec=simulate_hardware,
    )


def _select_service(contract: dict[str, Any]) -> dict[str, Any]:
    services = contract.get("services", [])
    if len(services) != 1:
        raise ValueError("Benchmark integration currently expects a single-service contract")
    return services[0]


def _select_access(service: dict[str, Any], access_kind: str | None) -> dict[str, Any]:
    default = service["access"]["default"]
    if access_kind is None or default["kind"] == access_kind:
        return default
    for candidate in service["access"].get("additional", []):
        if candidate["kind"] == access_kind:
            return candidate
    raise KeyError(f"No access kind {access_kind!r} available for service {service['public_name']}")


def _benchmark_client_class(protocol_mode: str, access_kind: str) -> str:
    if access_kind == "vllm-direct":
        return "helm.clients.vllm_client.VLLMClient" if protocol_mode == "completions" else "helm.clients.vllm_client.VLLMChatClient"
    return (
        "helm.clients.openai_client.OpenAILegacyCompletionsClient"
        if protocol_mode == "completions"
        else "helm.clients.openai_client.OpenAIClient"
    )


def _default_deployment_name(service: dict[str, Any], access_kind: str) -> str:
    prefix = "vllm" if access_kind == "vllm-direct" else "litellm"
    return f"{prefix}/{service['public_name']}-local"


def _resolve_api_key(access: dict[str, Any], *, api_key_value: str | None = None) -> str | None:
    if access["kind"] == "vllm-direct":
        return api_key_value
    if api_key_value is not None:
        return api_key_value
    env_name = access.get("auth_env_name", "")
    env_value = os.environ.get(env_name) if env_name else None
    if env_value:
        return env_value
    if not access.get("auth_required", access.get("auth_placeholder") != "EMPTY"):
        return access.get("auth_placeholder")
    raise ValueError(
        "Selected access mode "
        f"{access['kind']!r} requires credentials via {env_name!r}; "
        "bundle was not written because credentials were missing."
    )


def _model_deployment_entry(
    contract: dict[str, Any],
    *,
    helm_model_name: str | None = None,
    helm_tokenizer_name: str | None = None,
    access_kind: str | None = None,
    model_deployment_name: str | None = None,
    base_url: str | None = None,
    api_key_value: str | None = None,
) -> dict[str, Any]:
    service = _select_service(contract)
    access = _select_access(service, access_kind)
    protocol_mode = service["protocol"]["mode"]
    kind = access["kind"]
    entry = {
        "name": model_deployment_name or _default_deployment_name(service, kind),
        "model_name": helm_model_name or service["model"]["logical_model_name"],
        "tokenizer_name": helm_tokenizer_name or service["model"]["tokenizer_name"],
        "max_sequence_length": int(service["runtime"]["max_model_len"]),
        "client_spec": {
            "class_name": _benchmark_client_class(protocol_mode, kind),
            "args": {
                "base_url": base_url or access["base_url"],
            },
        },
    }
    if kind == "vllm-direct":
        entry["client_spec"]["args"]["vllm_model_name"] = access["request_model_name"]
    else:
        resolved_api_key = _resolve_api_key(access, api_key_value=api_key_value)
        entry["client_spec"]["args"]["api_key"] = resolved_api_key
        entry["client_spec"]["args"]["openai_model_name"] = access["request_model_name"]
    return entry


def _helm_config_paths() -> tuple[Path, Path]:
    helm_root = repo_root() / "submodules" / "helm" / "src" / "helm" / "config"
    return helm_root / "model_metadata.yaml", helm_root / "tokenizer_configs.yaml"


def _assert_helm_aliases_exist(model_name: str, tokenizer_name: str) -> None:
    import yaml

    model_metadata_path, tokenizer_configs_path = _helm_config_paths()
    model_docs = yaml.safe_load(model_metadata_path.read_text(encoding="utf-8")) or {}
    tokenizer_docs = yaml.safe_load(tokenizer_configs_path.read_text(encoding="utf-8")) or {}
    known_models = {item.get("name") for item in model_docs.get("models", []) or []}
    known_tokenizers = {item.get("name") for item in tokenizer_docs.get("tokenizer_configs", []) or []}
    if model_name not in known_models:
        raise ValueError(
            f"HELM model alias missing for {model_name!r}; update the benchmark export override before launching the run."
        )
    if tokenizer_name not in known_tokenizers:
        raise ValueError(
            f"HELM tokenizer alias missing for {tokenizer_name!r}; update the benchmark export override before launching the run."
        )


def _profile_specs(profile: str, preset_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    preset_profiles = preset_cfg.get("profiles")
    if preset_profiles:
        return [dict(item) for item in preset_profiles]
    return [{
        "profile": preset_cfg.get("profile", profile),
        "access_kind": preset_cfg.get("access_kind"),
        "model_deployment_name": preset_cfg.get("model_deployment_name"),
    }]


def _maybe_repo_relative(target: Path) -> str:
    try:
        return str(target.resolve().relative_to(repo_root().resolve()))
    except ValueError:
        return str(target.resolve())


def _manifest_doc(
    *,
    spec: dict[str, Any],
    model_deployments_fpath: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "experiment_name": spec["experiment_name"],
        "description": spec["description"],
        "run_entries": spec["run_entries"],
        "max_eval_instances": spec["max_eval_instances"],
        "suite": spec["suite"],
        "mode": "compute_if_missing",
        "materialize": "symlink",
        "backend": "tmux",
        "devices": 0,
        "tmux_workers": 1,
        "local_path": "prod_env",
        "precomputed_root": None,
        "require_per_instance_stats": True,
        "model_deployments_fpath": model_deployments_fpath,
        "enable_huggingface_models": [],
        "enable_local_huggingface_models": [],
    }


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(data), encoding="utf-8")


def _write_alias(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def materialize_benchmark_bundle(
    *,
    contracts: list[dict[str, Any]],
    output_dir: Path,
    preset: str | None = None,
    profile_specs: list[dict[str, Any]] | None = None,
    access_kind: str | None = None,
    base_url: str | None = None,
    api_key_value: str | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    preset_cfg = PRESET_CONFIGS.get(preset or "", {})
    specs = profile_specs or _profile_specs("", preset_cfg)
    services = [_select_service(contract) for contract in contracts]
    model_entries = []
    selected_accesses = []
    for contract, spec in zip(contracts, specs, strict=True):
        service = _select_service(contract)
        selected_kind = access_kind or spec.get("access_kind") or preset_cfg.get("access_kind")
        model_entries.append(
            _model_deployment_entry(
                contract,
                helm_model_name=spec.get("helm_model_name"),
                helm_tokenizer_name=spec.get("helm_tokenizer_name"),
                access_kind=selected_kind,
                model_deployment_name=spec.get("model_deployment_name"),
                base_url=base_url,
                api_key_value=api_key_value,
            )
        )
        _assert_helm_aliases_exist(model_entries[-1]["model_name"], model_entries[-1]["tokenizer_name"])
        selected_accesses.append(_select_access(service, selected_kind))
    output_dir.mkdir(parents=True, exist_ok=True)
    model_deployments = {"model_deployments": model_entries}
    model_deployments_path = output_dir / "model_deployments.yaml"
    _write_yaml(model_deployments_path, model_deployments)

    model_deployments_fpath = _maybe_repo_relative(model_deployments_path)
    smoke_spec = preset_cfg.get(
        "smoke_manifest",
        {
            "experiment_name": f"{services[0]['public_name']}-smoke",
            "description": f"Machine-local benchmark smoke manifest for {services[0]['public_name']}.",
            "run_entries": [
                f"ifeval:model={service['model']['logical_model_name']},model_deployment={entry['name']}"
                for service, entry in zip(services, model_entries, strict=True)
            ],
            "suite": f"{services[0]['public_name']}-smoke",
            "max_eval_instances": 5,
        },
    )
    full_spec = preset_cfg.get(
        "full_manifest",
        {
            "experiment_name": f"{services[0]['public_name']}-full",
            "description": f"Machine-local benchmark full manifest for {services[0]['public_name']}.",
            "run_entries": [
                f"ifeval:model={service['model']['logical_model_name']},model_deployment={entry['name']}"
                for service, entry in zip(services, model_entries, strict=True)
            ],
            "suite": f"{services[0]['public_name']}-full",
            "max_eval_instances": 1000,
        },
    )
    benchmark_smoke_manifest = _manifest_doc(spec=smoke_spec, model_deployments_fpath=model_deployments_fpath)
    benchmark_full_manifest = _manifest_doc(spec=full_spec, model_deployments_fpath=model_deployments_fpath)
    benchmark_smoke_path = output_dir / "benchmark_smoke_manifest.yaml"
    benchmark_full_path = output_dir / "benchmark_full_manifest.yaml"
    _write_yaml(benchmark_smoke_path, benchmark_smoke_manifest)
    _write_yaml(benchmark_full_path, benchmark_full_manifest)

    smoke_manifest_path = output_dir / "smoke_manifest.yaml"
    full_manifest_path = output_dir / "full_manifest.yaml"
    _write_alias(benchmark_smoke_path, smoke_manifest_path)
    _write_alias(benchmark_full_path, full_manifest_path)

    bundle = {
        "target": "crfm_helm_benchmark",
        "benchmark": {
            "preset": preset,
            "model_deployment_name": model_entries[0]["name"] if len(model_entries) == 1 else None,
            "model_deployment_names": [entry["name"] for entry in model_entries],
            "model_deployments_path": str(model_deployments_path),
            "model_deployments_fpath": model_deployments_fpath,
        },
        "artifacts": {
            "model_deployments": str(model_deployments_path),
            "benchmark_smoke_manifest": str(benchmark_smoke_path),
            "benchmark_full_manifest": str(benchmark_full_path),
        },
    }
    if len(contracts) == 1:
        bundle["profile"] = contracts[0]["profile"]
        bundle["selected_access"] = selected_accesses[0]
    else:
        bundle["profiles"] = [contract["profile"] for contract in contracts]
        bundle["selected_accesses"] = selected_accesses
    bundle_path = output_dir / "bundle.yaml"
    _write_yaml(bundle_path, bundle)
    return {
        "bundle_dir": output_dir,
        "bundle_path": bundle_path,
        "model_deployments_path": model_deployments_path,
        "benchmark_smoke_manifest_path": benchmark_smoke_path,
        "benchmark_full_manifest_path": benchmark_full_path,
        "smoke_manifest_path": smoke_manifest_path,
        "full_manifest_path": full_manifest_path,
        "bundle": bundle,
    }


def export_benchmark_bundle(
    profile: str,
    *,
    preset: str | None = None,
    bundle_root: Path | None = None,
    backend: str | None = None,
    simulate_hardware: str | None = None,
    vllm_root: Path | None = None,
    access_kind: str | None = None,
    base_url: str | None = None,
    api_key_value: str | None = None,
) -> dict[str, Any]:
    preset_cfg = PRESET_CONFIGS.get(preset or "", {})
    effective_backend = backend or preset_cfg.get("backend")
    specs = _profile_specs(profile, preset_cfg)
    contracts = [
        load_profile_contract(
            spec["profile"],
            backend=effective_backend,
            simulate_hardware=simulate_hardware,
            vllm_root=vllm_root,
        )
        for spec in specs
    ]
    if bundle_root is None:
        bundle_name = preset_cfg.get("bundle_name") or specs[0]["profile"].replace("-", "_")
        bundle_root = audit_store_root() / "local-bundles" / bundle_name
    return materialize_benchmark_bundle(
        contracts=contracts,
        output_dir=bundle_root,
        preset=preset,
        profile_specs=specs,
        access_kind=access_kind,
        base_url=base_url,
        api_key_value=api_key_value,
    )
