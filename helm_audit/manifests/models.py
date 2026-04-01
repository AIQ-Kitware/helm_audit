from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ManifestSpec:
    experiment_name: str
    description: str
    run_entries: list[str]
    suite: str
    max_eval_instances: int
    backend: str = "tmux"
    mode: str = "compute_if_missing"
    materialize: str = "symlink"
    devices: str = "0,1"
    tmux_workers: int = 2
    local_path: str = "prod_env"
    precomputed_root: str | None = None
    require_per_instance_stats: bool = True
    model_deployments_fpath: str | None = None
    enable_huggingface_models: list[str] = field(default_factory=list)
    enable_local_huggingface_models: list[str] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
