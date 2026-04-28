from __future__ import annotations

from pathlib import Path

from eval_audit.infra.env import load_env


def repo_root() -> Path:
    return load_env().repo_root


def publication_root() -> Path:
    """Where the publication surface (a folder named ``reports/``) lives.

    ADR 3: there is *one* publication surface, named ``reports/``. Its
    location is parameterized. The default points at
    ``<audit_store>/reports/`` so derived outputs do not pollute the
    checked-in repository. Override with ``HELM_AUDIT_PUBLICATION_ROOT`` to
    relocate (e.g. set it to ``<repo>/reports`` for the legacy layout).
    """
    return load_env().publication_root


def reports_root() -> Path:
    """Backwards-compatible alias for :func:`publication_root`.

    Older code referred to the publication surface as ``reports_root``.
    The function now returns whatever the configured publication root is,
    not a hard-coded ``<repo>/reports``. New call sites should prefer
    :func:`publication_root`.
    """
    return publication_root()


def repo_reports_root() -> Path:
    """Legacy in-repo ``reports/`` directory — for migration code only.

    This is the historical path under the source tree. It is preserved
    so the legacy-dir migration in :mod:`eval_audit.workflows.analyze_experiment`
    can still locate pre-move directories. New code must not write here.
    """
    return repo_root() / "reports"


def audit_store_root() -> Path:
    return load_env().audit_store_root


def configs_root() -> Path:
    return repo_root() / "configs"


def generated_configs_root() -> Path:
    return audit_store_root() / "configs"


def generated_manifests_root() -> Path:
    return generated_configs_root() / "manifests"


def generated_indexes_root() -> Path:
    return audit_store_root() / "indexes"


def run_specs_fpath() -> Path:
    return generated_configs_root() / "run_specs.yaml"


def run_details_fpath() -> Path:
    return generated_configs_root() / "run_details.yaml"


def experiment_result_dpath(experiment_name: str) -> Path:
    return load_env().audit_results_root / experiment_name


def experiment_report_dpath(experiment_name: str) -> Path:
    return reports_root() / experiment_name


def experiment_analysis_dpath(experiment_name: str) -> Path:
    """Canonical per-experiment analysis root inside the audit store."""
    return audit_store_root() / "analysis" / "experiments" / experiment_name


def official_public_index_dpath() -> Path:
    """Directory where official/public index artifacts are stored."""
    return generated_indexes_root()


def index_snapshot_analysis_dpath() -> Path:
    """Default output directory for index-snapshot analysis artifacts."""
    return audit_store_root() / "analysis" / "index-snapshot"


def official_public_analysis_dpath() -> Path:
    """Backwards-compat alias for index_snapshot_analysis_dpath()."""
    return index_snapshot_analysis_dpath()


def paper_label_config_fpath() -> Path:
    return configs_root() / "paper_label_mappings.yaml"
