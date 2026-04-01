from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from helm_audit.infra.yaml_io import dump_yaml, load_manifest
from helm_audit.infra.paths import experiment_result_dpath


@dataclass(frozen=True)
class KWDaggerScheduleRequest:
    manifest_fpath: Path
    manifest: dict[str, Any]
    result_dpath: Path
    queue_name: str
    devices: str
    tmux_workers: int
    backend: str
    params_text: str


def build_schedule_params(manifest: dict[str, Any]) -> dict[str, Any]:
    matrix = {
        "helm.run_entry": list(manifest["run_entries"]),
        "helm.max_eval_instances": [manifest["max_eval_instances"]],
        "helm.precomputed_root": manifest.get("precomputed_root", None),
        "helm.suite": [manifest.get("suite", "audit-smoke")],
        "helm.require_per_instance_stats": [
            manifest.get("require_per_instance_stats", True)
        ],
        "helm.mode": [manifest.get("mode", "compute_if_missing")],
        "helm.materialize": [manifest.get("materialize", "symlink")],
        "helm.local_path": [manifest.get("local_path", "prod_env")],
    }
    model_deployments_fpath = manifest.get("model_deployments_fpath", None)
    if model_deployments_fpath is not None:
        matrix["helm.model_deployments_fpath"] = [model_deployments_fpath]
    enable_hf = manifest.get("enable_huggingface_models", [])
    if enable_hf:
        matrix["helm.enable_huggingface_models"] = [json.dumps(enable_hf)]
    enable_local_hf = manifest.get("enable_local_huggingface_models", [])
    if enable_local_hf:
        matrix["helm.enable_local_huggingface_models"] = [json.dumps(enable_local_hf)]
    return {
        "pipeline": "magnet.backends.helm.pipeline.helm_single_run_pipeline()",
        "matrix": matrix,
    }


def prepare_schedule_request(manifest_fpath: str | Path) -> KWDaggerScheduleRequest:
    manifest_path = Path(manifest_fpath).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    experiment_name = str(manifest["experiment_name"])
    queue_name = f"audit-{experiment_name}".translate(
        str.maketrans({c: "-" for c in " !@#$%^&*()+={}[]|\\:;\"'<>,?/~`"})
    )
    params = build_schedule_params(manifest)
    return KWDaggerScheduleRequest(
        manifest_fpath=manifest_path,
        manifest=manifest,
        result_dpath=experiment_result_dpath(experiment_name),
        queue_name=queue_name,
        devices=str(manifest.get("devices", "0,1")),
        tmux_workers=int(manifest.get("tmux_workers", 2)),
        backend=str(manifest.get("backend", "tmux")),
        params_text=dump_yaml(params),
    )


def kwdagger_schedule_argv(request: KWDaggerScheduleRequest) -> list[str]:
    # FIXME(kwdagger): kwdagger currently makes this integration awkward because
    # --params may be either inline YAML text or a YAML file path.
    return [
        "kwdagger",
        "schedule",
        f"--queue_name={request.queue_name}",
        f"--params={request.params_text}",
        f"--devices={request.devices}",
        f"--tmux_workers={request.tmux_workers}",
        f"--root_dpath={request.result_dpath}",
        f"--backend={request.backend}",
        "--skip_existing=1",
        "--run=1",
    ]


def run_kwdagger_schedule(request: KWDaggerScheduleRequest) -> subprocess.CompletedProcess[str]:
    request.result_dpath.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        kwdagger_schedule_argv(request),
        check=True,
        text=True,
    )
