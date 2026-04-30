from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval_audit.infra.api import audit_root
from eval_audit.infra.yaml_io import dump_yaml, load_manifest
from eval_audit.infra.paths import experiment_result_dpath


def _detect_virtualenv_cmd() -> str | None:
    """Return a shell command that activates the venv eval-audit-run is
    running in, or ``None`` if no venv is detected.

    Why this matters: kwdagger spawns each job in a fresh shell (tmux
    pane, slurm job, or serial subprocess). The shell loads the user's
    rc files and then runs the command. Whether ``.venv`` is activated
    in that shell depends on whether the user's dotfiles auto-activate
    it — most don't. Without an explicit activation step, the job's
    ``python`` may resolve to a pyenv shim, ``/usr/bin/python3``, or a
    different uv-managed interpreter than the one running this CLI.
    The symptoms range from silent "wrong package version" mismatches
    to outright ``ModuleNotFoundError``.

    We use the venv's ``bin/activate`` script when available since it
    sets ``VIRTUAL_ENV`` and prepends the right PATH the same way an
    interactive ``source .venv/bin/activate`` does. Fall back to
    ``None`` when nothing looks like a venv — better to inherit the
    parent's environment than to inject a broken activation command.
    """
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        # Not running under an activated venv. Try to derive from
        # ``sys.prefix`` — uv-run / pipx style invocations don't set
        # VIRTUAL_ENV but ``sys.prefix`` still points at the env root.
        if sys.prefix != sys.base_prefix:
            venv = sys.prefix
    if not venv:
        return None
    activate = Path(venv) / "bin" / "activate"
    if not activate.is_file():
        return None
    return f"source {shlex.quote(str(activate))}"


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
    argv = [
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
        # Tee per-node stdout/stderr to info_dpath/status/<pathid>.logs so
        # cmd_queue's failure surfacing has content to display when a job
        # crashes before helm-run starts (i.e. before
        # materialize_helm.run_helm captures cmd_stdout.txt/cmd_stderr.txt).
        "--log=True",
        "--monitor=tmux",
    ]
    # Auto-activate the running venv inside each spawned job. See
    # _detect_virtualenv_cmd for rationale. This is purely additive —
    # if no venv is detected, the spawned jobs inherit the parent's
    # environment as before.
    venv_cmd = _detect_virtualenv_cmd()
    if venv_cmd:
        argv.append(f"--virtualenv_cmd={venv_cmd}")
    return argv


def kwdagger_schedule_command_text(request: KWDaggerScheduleRequest) -> str:
    return shlex.join(kwdagger_schedule_argv(request))


def run_kwdagger_schedule(request: KWDaggerScheduleRequest) -> subprocess.CompletedProcess[str]:
    request.runtime.root_dpath.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        kwdagger_schedule_argv(request),
        check=True,
        text=True,
    )
