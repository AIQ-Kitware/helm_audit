from __future__ import annotations

from pathlib import Path

from helm_audit.infra.yaml_io import load_manifest
from helm_audit.integrations.kwdagger_bridge import (
    prepare_schedule_request,
    run_kwdagger_schedule,
)


def run_from_manifest(manifest_fpath: str | Path) -> dict[str, str]:
    manifest = load_manifest(manifest_fpath)
    request = prepare_schedule_request(manifest_fpath)
    run_kwdagger_schedule(request)
    return {
        "experiment_name": str(manifest["experiment_name"]),
        "result_dpath": str(request.result_dpath),
        "queue_name": request.queue_name,
    }
