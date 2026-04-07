from __future__ import annotations

from pathlib import Path

from helm_audit.infra.env import load_env


def repo_root() -> Path:
    return load_env().repo_root


def reports_root() -> Path:
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


def paper_label_config_fpath() -> Path:
    return configs_root() / "paper_label_mappings.yaml"
