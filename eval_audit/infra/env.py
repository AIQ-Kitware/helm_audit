from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AuditEnv:
    repo_root: Path
    aiq_python: str
    helm_precomputed_root: Path
    audit_results_root: Path
    audit_store_root: Path
    publication_root: Path
    default_max_eval_instances: int
    default_tmux_workers: int


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_env() -> AuditEnv:
    audit_store = Path(
        os.environ.get("AUDIT_STORE_ROOT", "/data/crfm-helm-audit-store")
    ).expanduser().resolve()
    # ADR 3: there is a folder named ``reports/`` that holds the publication
    # surface. Its location is parameterized — historically the repo's
    # ``reports/`` directory, but defaulting to the audit store so derived
    # outputs do not pollute the checked-in tree. Override with
    # ``HELM_AUDIT_PUBLICATION_ROOT`` if you want it elsewhere (e.g. point it
    # back at <repo>/reports/ for the legacy layout).
    publication_root_env = os.environ.get("HELM_AUDIT_PUBLICATION_ROOT")
    publication_root = (
        Path(publication_root_env).expanduser().resolve()
        if publication_root_env
        else audit_store / "reports"
    )
    return AuditEnv(
        repo_root=repo_root(),
        aiq_python=os.environ.get("AIQ_PYTHON", "python"),
        helm_precomputed_root=Path(
            os.environ.get("HELM_PRECOMPUTED_ROOT", "/data/crfm-helm-public")
        ).expanduser().resolve(),
        audit_results_root=Path(
            os.environ.get("AUDIT_RESULTS_ROOT", "/data/crfm-helm-audit")
        ).expanduser().resolve(),
        audit_store_root=audit_store,
        publication_root=publication_root,
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
        "AIQ_PYTHON": env.aiq_python,
        "HELM_PRECOMPUTED_ROOT": str(env.helm_precomputed_root),
        "AUDIT_RESULTS_ROOT": str(env.audit_results_root),
        "AUDIT_STORE_ROOT": str(env.audit_store_root),
        "HELM_AUDIT_PUBLICATION_ROOT": str(env.publication_root),
        "AUDIT_DEFAULT_MAX_EVAL_INSTANCES": str(env.default_max_eval_instances),
        "AUDIT_DEFAULT_TMUX_WORKERS": str(env.default_tmux_workers),
    }
