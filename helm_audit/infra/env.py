from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditEnv:
    repo_root: Path
    aiq_magnet_root: Path
    aiq_python: str
    helm_precomputed_root: Path
    audit_results_root: Path
    audit_store_root: Path
    default_max_eval_instances: int
    default_tmux_workers: int


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_env() -> AuditEnv:
    return AuditEnv(
        repo_root=repo_root(),
        aiq_magnet_root=Path(
            os.environ.get("AIQ_MAGNET_ROOT", str(Path.home() / "code" / "aiq-magnet"))
        ).expanduser().resolve(),
        aiq_python=os.environ.get("AIQ_PYTHON", "python"),
        helm_precomputed_root=Path(
            os.environ.get("HELM_PRECOMPUTED_ROOT", "/data/crfm-helm-public")
        ).expanduser().resolve(),
        audit_results_root=Path(
            os.environ.get("AUDIT_RESULTS_ROOT", "/data/crfm-helm-audit")
        ).expanduser().resolve(),
        audit_store_root=Path(
            os.environ.get("AUDIT_STORE_ROOT", "/data/crfm-helm-audit-store")
        ).expanduser().resolve(),
        default_max_eval_instances=int(
            os.environ.get("AUDIT_DEFAULT_MAX_EVAL_INSTANCES", "100")
        ),
        default_tmux_workers=int(
            os.environ.get("AUDIT_DEFAULT_TMUX_WORKERS", "2")
        ),
    )


def env_defaults() -> dict[str, str]:
    env = load_env()
    return {
        "AIQ_MAGNET_ROOT": str(env.aiq_magnet_root),
        "AIQ_PYTHON": env.aiq_python,
        "HELM_PRECOMPUTED_ROOT": str(env.helm_precomputed_root),
        "AUDIT_RESULTS_ROOT": str(env.audit_results_root),
        "AUDIT_STORE_ROOT": str(env.audit_store_root),
        "AUDIT_DEFAULT_MAX_EVAL_INSTANCES": str(env.default_max_eval_instances),
        "AUDIT_DEFAULT_TMUX_WORKERS": str(env.default_tmux_workers),
    }
