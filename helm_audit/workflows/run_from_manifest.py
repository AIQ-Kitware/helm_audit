from __future__ import annotations

from pathlib import Path
from typing import Any

from helm_audit.infra.yaml_io import load_manifest
from helm_audit.integrations.kwdagger_bridge import (
    kwdagger_schedule_argv,
    kwdagger_schedule_command_text,
    prepare_schedule_request,
    run_kwdagger_schedule,
)


def run_from_manifest(
    manifest_fpath: str | Path,
    *,
    run: bool = False,
    root_dpath: str | Path | None = None,
    queue_name: str | None = None,
    devices: str | None = None,
    tmux_workers: int | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_fpath)
    request = prepare_schedule_request(
        manifest_fpath,
        run=run,
        root_dpath=root_dpath,
        queue_name=queue_name,
        devices=devices,
        tmux_workers=tmux_workers,
        backend=backend,
    )
    info: dict[str, Any] = {
        "experiment_name": str(manifest["experiment_name"]),
        "manifest_fpath": str(request.manifest_fpath),
        "mode": "execute" if request.runtime.run else "preview",
        "result_dpath": str(request.runtime.root_dpath),
        "queue_name": request.runtime.queue_name,
        "backend": request.runtime.backend,
        "devices": request.runtime.devices,
        "tmux_workers": request.runtime.tmux_workers,
        "argv": kwdagger_schedule_argv(request),
        "command": kwdagger_schedule_command_text(request),
    }
    if request.runtime.run:
        proc = run_kwdagger_schedule(request)
        info["returncode"] = proc.returncode
    return info
