from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval_audit.infra.api import audit_root
from eval_audit.infra.yaml_io import dump_yaml, load_manifest
from eval_audit.infra.paths import experiment_result_dpath


@dataclass(frozen=True)
class KWDaggerRuntime:
    queue_name: str
    root_dpath: Path
    devices: str
    tmux_workers: int
    backend: str
    run: bool
    skip_existing: bool = True


@dataclass(frozen=True)
class KWDaggerScheduleRequest:
    manifest_fpath: Path
    manifest: dict[str, Any]
    runtime: KWDaggerRuntime
    params_text: str


def _resolve_manifest_override_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = audit_root() / path
    return str(path.resolve())


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


def prepare_schedule_request(
    manifest_fpath: str | Path,
    *,
    run: bool = False,
    root_dpath: str | Path | None = None,
    queue_name: str | None = None,
    devices: str | None = None,
    tmux_workers: int | None = None,
    backend: str | None = None,
) -> KWDaggerScheduleRequest:
    manifest_path = Path(manifest_fpath).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    manifest = dict(manifest)
    manifest["model_deployments_fpath"] = _resolve_manifest_override_path(
        manifest.get("model_deployments_fpath", None)
    )
    experiment_name = str(manifest["experiment_name"])
    runtime_queue_name = (queue_name or f"audit-{experiment_name}").translate(
        str.maketrans({c: "-" for c in " !@#$%^&*()+={}[]|\\:;\"'<>,?/~`"})
    )
    params = build_schedule_params(manifest)
    runtime = KWDaggerRuntime(
        queue_name=runtime_queue_name,
        root_dpath=(
            Path(root_dpath).expanduser().resolve()
            if root_dpath is not None
            else experiment_result_dpath(experiment_name)
        ),
        devices=str(devices if devices is not None else manifest.get("devices", "0,1")),
        tmux_workers=int(
            tmux_workers
            if tmux_workers is not None
            else manifest.get("tmux_workers", 2)
        ),
        backend=str(backend if backend is not None else manifest.get("backend", "tmux")),
        run=bool(run),
    )
    return KWDaggerScheduleRequest(
        manifest_fpath=manifest_path,
        manifest=manifest,
        runtime=runtime,
        params_text=dump_yaml(params),
    )


def kwdagger_schedule_argv(request: KWDaggerScheduleRequest) -> list[str]:
    # FIXME(kwdagger): kwdagger currently makes this integration awkward because
    # --params may be either inline YAML text or a YAML file path.
    return [
        "kwdagger",
        "schedule",
        f"--queue_name={request.runtime.queue_name}",
        f"--params={request.params_text}",
        f"--devices={request.runtime.devices}",
        f"--tmux_workers={request.runtime.tmux_workers}",
        f"--root_dpath={request.runtime.root_dpath}",
        f"--backend={request.runtime.backend}",
        f"--skip_existing={1 if request.runtime.skip_existing else 0}",
        f"--run={1 if request.runtime.run else 0}",
    ]


def kwdagger_schedule_command_text(request: KWDaggerScheduleRequest) -> str:
    return shlex.join(kwdagger_schedule_argv(request))


def run_kwdagger_schedule(request: KWDaggerScheduleRequest) -> subprocess.CompletedProcess[str]:
    request.runtime.root_dpath.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        kwdagger_schedule_argv(request),
        check=True,
        text=True,
    )
