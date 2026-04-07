from __future__ import annotations

from pathlib import Path
from typing import Any

from helm_audit.infra.env import env_defaults, load_env
from helm_audit.infra.paths import (
    audit_store_root,
    experiment_report_dpath as _experiment_report_dpath,
    experiment_result_dpath as _experiment_result_dpath,
    generated_indexes_root,
    generated_manifests_root,
    reports_root,
    run_details_fpath,
    run_specs_fpath,
)
from helm_audit.infra.yaml_io import dump_yaml, load_manifest


def audit_root() -> Path:
    return load_env().repo_root


def default_report_root() -> Path:
    return reports_root()


def default_store_root() -> Path:
    return audit_store_root()


def default_manifest_root() -> Path:
    return generated_manifests_root()


def default_index_root() -> Path:
    return generated_indexes_root()


def repo_run_specs_fpath() -> Path:
    return run_specs_fpath()


def repo_run_details_fpath() -> Path:
    return run_details_fpath()


def experiment_result_dpath(manifest: dict[str, Any]) -> Path:
    return _experiment_result_dpath(str(manifest["experiment_name"]))


def experiment_report_dpath(manifest: dict[str, Any]) -> Path:
    return _experiment_report_dpath(str(manifest["experiment_name"]))
