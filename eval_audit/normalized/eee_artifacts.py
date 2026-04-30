"""Discovery and lightweight conversion helpers for EEE artifact trees.

The normalized report path should consume explicit artifact references, not
guess at load time.  This module is the small filesystem bridge that maps raw
HELM run rows to converted Every Eval Ever (EEE) artifact directories while
keeping raw HELM paths as provenance.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import safer

from eval_audit.helm.hashers import stable_hash36
from eval_audit.infra.paths import audit_store_root, repo_root
from eval_audit.normalized.loaders import _eee_converter_name, _eee_converter_version


def _atomic_write_text(fpath: Path, content: str) -> None:
    """Atomic write via :mod:`safer` so concurrent readers never see a partial file."""
    fpath.parent.mkdir(parents=True, exist_ok=True)
    with safer.open(str(fpath), "w") as fh:
        fh.write(content)


def default_official_eee_root() -> Path:
    """Default root for prebuilt official HELM->EEE conversions."""
    return audit_store_root() / "crfm-helm-public-eee-test"


def default_local_eee_root() -> Path:
    """Default root for local HELM->EEE conversions generated on demand."""
    return audit_store_root() / "eee" / "local"


def default_helm_raw_cache_root() -> Path:
    """Default root for the content-addressed HELM->EEE cache used by ``HelmRawLoader``.

    Keyed by ``sha256(resolved_run_path)`` so any caller that hands a HELM
    run directory to the loader hits the same cache regardless of which
    experiment / planner identity it traces back to. The planner's
    experiment-keyed cache (:func:`local_eee_parent_for_row`) and this
    content-addressed cache live side by side; either can satisfy a load.
    """
    return audit_store_root() / "eee" / "by-run-path"


def helm_raw_cache_parent(run_path: str | Path) -> Path:
    """Deterministic cache parent for one HELM run directory."""
    resolved = str(Path(run_path).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return default_helm_raw_cache_root() / digest


def convert_helm_run_to_cached_eee(
    run_path: str | Path,
    *,
    source_kind: str = "local",
    source_organization_name: str = "eval_audit_helm_raw",
    eval_library_name: str = "HELM",
    eval_library_version: str = "unknown",
    evaluator_relationship: str = "third_party",
) -> EeeArtifactResolution:
    """Convert a HELM run directory to EEE under the content-addressed cache.

    Atomic on a per-file basis via :func:`_atomic_write_text`, and idempotent:
    if another process has already populated the cache the existing artifact
    is returned without re-running the converter.
    """
    run_path = Path(run_path).expanduser().resolve()
    if not run_path.is_dir():
        return EeeArtifactResolution(
            artifact_path=None,
            status="missing_run_path",
            source="helm_raw_cached_conversion",
            message=f"run path does not exist: {run_path}",
        )

    parent = helm_raw_cache_parent(run_path)
    artifact_path = parent / "eee_output"
    status_path = parent / "status.json"
    provenance_path = parent / "provenance.json"
    if _artifact_has_aggregate(artifact_path):
        return EeeArtifactResolution(
            artifact_path=artifact_path.resolve(),
            status="found",
            source="helm_raw_cached_conversion",
            status_path=status_path.resolve() if status_path.exists() else None,
            provenance_path=provenance_path.resolve() if provenance_path.exists() else None,
        )
    parent.mkdir(parents=True, exist_ok=True)
    artifact_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).isoformat()
    eval_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, str(run_path)))
    metadata_args = {
        "source_organization_name": source_organization_name,
        "evaluator_relationship": evaluator_relationship,
        "eval_library_name": eval_library_name,
        "eval_library_version": eval_library_version,
        "parent_eval_output_dir": str(artifact_path),
        "file_uuid": eval_uuid,
    }
    status: dict[str, Any] = {
        "source_kind": source_kind,
        "status": "started",
        "timestamp": timestamp,
        "run_path": str(run_path),
        "out_dir": str(parent),
        "eee_artifact_path": str(artifact_path),
        "converter_name": _eee_converter_name(),
        "converter_version": _eee_converter_version(),
    }

    try:
        from every_eval_ever.converters.helm.adapter import HELMAdapter

        adapter = HELMAdapter()
        logs = adapter.transform_from_directory(
            str(run_path),
            output_path=str(artifact_path / "helm_output"),
            metadata_args=metadata_args,
        )
        aggregate_paths: list[str] = []
        for log in logs:
            aggregate_path = _aggregate_path_for_log(log, artifact_path, eval_uuid)
            _atomic_write_text(
                aggregate_path,
                log.model_dump_json(exclude_none=True, indent=2) + "\n",
            )
            aggregate_paths.append(str(aggregate_path))
        status.update(
            {
                "status": "ok",
                "returncode": 0,
                "n_evaluation_logs": len(logs),
                "aggregate_paths": aggregate_paths,
            }
        )
        _atomic_write_text(provenance_path, json.dumps({**status}, indent=2, default=str) + "\n")
        _atomic_write_text(status_path, json.dumps(status, indent=2, default=str) + "\n")
        if not _artifact_has_aggregate(artifact_path):
            return EeeArtifactResolution(
                artifact_path=None,
                status="conversion_empty",
                source="helm_raw_cached_conversion",
                status_path=status_path.resolve(),
                provenance_path=provenance_path.resolve(),
                message="converter completed but wrote no aggregate JSON",
                generated=True,
            )
        return EeeArtifactResolution(
            artifact_path=artifact_path.resolve(),
            status="generated",
            source="helm_raw_cached_conversion",
            status_path=status_path.resolve(),
            provenance_path=provenance_path.resolve(),
            generated=True,
        )
    except Exception as exc:
        status.update(
            {
                "status": "fail",
                "exception_class": type(exc).__name__,
                "failure_snippet": traceback.format_exc()[-4000:],
                "returncode": -1,
            }
        )
        _atomic_write_text(status_path, json.dumps(status, indent=2, default=str) + "\n")
        return EeeArtifactResolution(
            artifact_path=None,
            status="conversion_failed",
            source="helm_raw_cached_conversion",
            status_path=status_path.resolve(),
            provenance_path=provenance_path.resolve() if provenance_path.exists() else None,
            message=f"{type(exc).__name__}: {exc}",
            generated=True,
        )


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"none", "nan"}:
        return None
    return text


def _resolve_existing_dir(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    return path if path.is_dir() else None


def _artifact_has_aggregate(artifact_path: Path) -> bool:
    if not artifact_path.is_dir():
        return False
    for path in artifact_path.rglob("*.json"):
        if path.name in {"status.json", "provenance.json"}:
            continue
        if path.name.endswith("_samples.json"):
            continue
        return True
    return False


def _slugify(text: str, *, max_len: int = 96) -> str:
    slug = (
        str(text)
        .replace("/", "-")
        .replace(":", "-")
        .replace(",", "-")
        .replace("=", "-")
        .replace("@", "-")
        .replace(" ", "-")
    ).strip("-")
    return (slug[:max_len].rstrip("-") or "run")


@dataclass(frozen=True)
class EeeArtifactResolution:
    """Result of mapping or generating a converted EEE artifact."""

    artifact_path: Path | None
    status: str
    source: str | None = None
    status_path: Path | None = None
    provenance_path: Path | None = None
    message: str | None = None
    generated: bool = False

    @property
    def found(self) -> bool:
        return self.artifact_path is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_path": str(self.artifact_path) if self.artifact_path else None,
            "status": self.status,
            "source": self.source,
            "status_path": str(self.status_path) if self.status_path else None,
            "provenance_path": str(self.provenance_path) if self.provenance_path else None,
            "message": self.message,
            "generated": self.generated,
        }


def _explicit_eee_resolution(row: dict[str, Any]) -> EeeArtifactResolution | None:
    eee_path = _clean_optional_text(row.get("eee_artifact_path") or row.get("eee_path"))
    if not eee_path:
        return None
    path = _resolve_existing_dir(eee_path)
    if path is None:
        return EeeArtifactResolution(
            artifact_path=None,
            status="missing_explicit_path",
            source="index_row",
            message=f"explicit EEE path is not a directory: {eee_path}",
        )
    if not _artifact_has_aggregate(path):
        return EeeArtifactResolution(
            artifact_path=None,
            status="empty_explicit_path",
            source="index_row",
            message=f"explicit EEE path contains no aggregate JSON: {path}",
        )
    return EeeArtifactResolution(
        artifact_path=path,
        status="found",
        source="index_row",
    )


@lru_cache(maxsize=8)
def _official_sweep_results_by_run_path(root_text: str) -> dict[str, Path]:
    root = Path(root_text)
    results_fpath = root / "results.jsonl"
    out: dict[str, Path] = {}
    if not results_fpath.exists():
        return out
    try:
        lines = results_fpath.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") != "ok":
            continue
        run_path = _clean_optional_text(row.get("run_path"))
        out_dir = _clean_optional_text(row.get("out_dir"))
        if not run_path or not out_dir:
            continue
        artifact_path = Path(out_dir).expanduser() / "eee_output"
        out[str(Path(run_path).expanduser().resolve())] = artifact_path.resolve()
    return out


def resolve_official_eee_artifact(
    row: dict[str, Any],
    *,
    official_eee_root: str | Path | None = None,
) -> EeeArtifactResolution:
    """Resolve an official/public index row to a prebuilt EEE artifact."""

    explicit = _explicit_eee_resolution(row)
    if explicit is not None:
        return explicit

    root = _resolve_existing_dir(official_eee_root or default_official_eee_root())
    if root is None:
        return EeeArtifactResolution(
            artifact_path=None,
            status="root_missing",
            source="official_eee_root",
            message=f"official EEE root does not exist: {official_eee_root or default_official_eee_root()}",
        )

    track = _clean_optional_text(row.get("public_track"))
    version = _clean_optional_text(row.get("suite_version"))
    run_name = _clean_optional_text(row.get("run_name") or row.get("logical_run_key"))
    if track and version and run_name:
        candidate = root.joinpath(*track.split("/"), version, run_name)
        status_path = candidate / "status.json"
        artifact_path = candidate / "eee_output"
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text()).get("status")
            except Exception:
                status = None
            if status == "ok" and _artifact_has_aggregate(artifact_path):
                return EeeArtifactResolution(
                    artifact_path=artifact_path.resolve(),
                    status="found",
                    source="official_sweep_layout",
                    status_path=status_path.resolve(),
                )
        elif _artifact_has_aggregate(artifact_path):
            return EeeArtifactResolution(
                artifact_path=artifact_path.resolve(),
                status="found",
                source="official_sweep_layout",
            )

    run_path = _clean_optional_text(row.get("run_path") or row.get("public_run_dir"))
    if run_path:
        resolved_run_path = str(Path(run_path).expanduser().resolve())
        artifact_path = _official_sweep_results_by_run_path(str(root)).get(resolved_run_path)
        if artifact_path is not None and _artifact_has_aggregate(artifact_path):
            return EeeArtifactResolution(
                artifact_path=artifact_path,
                status="found",
                source="official_results_jsonl",
            )

    return EeeArtifactResolution(
        artifact_path=None,
        status="missing",
        source="official_sweep",
        message="no successful official EEE conversion matched this index row",
    )


def local_eee_parent_for_row(
    row: dict[str, Any],
    *,
    local_eee_root: str | Path | None = None,
) -> Path:
    """Return the deterministic parent dir for one local converted artifact."""

    root = Path(local_eee_root or default_local_eee_root()).expanduser().resolve()
    # Prefer ``source_experiment_name`` when present so virtual-experiment
    # composes (which restamp ``experiment_name`` to the virtual name) hit
    # the same cache key as the original audit run that produced the data.
    experiment = _slugify(
        _clean_optional_text(row.get("source_experiment_name"))
        or _clean_optional_text(row.get("experiment_name"))
        or "unknown-experiment"
    )
    job_id = _slugify(_clean_optional_text(row.get("job_id")) or "unknown-job")
    run_name = (
        _clean_optional_text(row.get("run_name"))
        or _clean_optional_text(row.get("logical_run_key"))
        or Path(_clean_optional_text(row.get("run_path") or row.get("run_dir")) or "run").name
    )
    digest = stable_hash36(
        {
            "run_path": _clean_optional_text(row.get("run_path") or row.get("run_dir")),
            "component_id": _clean_optional_text(row.get("component_id")),
            "attempt_identity": _clean_optional_text(row.get("attempt_identity")),
        }
    )[:12]
    return root / experiment / job_id / f"{_slugify(run_name)}--{digest}"


def resolve_local_eee_artifact(
    row: dict[str, Any],
    *,
    local_eee_root: str | Path | None = None,
    ensure: bool = False,
) -> EeeArtifactResolution:
    """Resolve or optionally generate the EEE artifact for a local run row."""

    explicit = _explicit_eee_resolution(row)
    if explicit is not None:
        return explicit

    parent = local_eee_parent_for_row(row, local_eee_root=local_eee_root)
    artifact_path = parent / "eee_output"
    status_path = parent / "status.json"
    provenance_path = parent / "provenance.json"
    if _artifact_has_aggregate(artifact_path):
        return EeeArtifactResolution(
            artifact_path=artifact_path.resolve(),
            status="found",
            source="local_canonical_layout",
            status_path=status_path.resolve() if status_path.exists() else None,
            provenance_path=provenance_path.resolve() if provenance_path.exists() else None,
        )
    if not ensure:
        return EeeArtifactResolution(
            artifact_path=None,
            status="missing",
            source="local_canonical_layout",
            status_path=status_path.resolve() if status_path.exists() else None,
            provenance_path=provenance_path.resolve() if provenance_path.exists() else None,
        )
    return convert_local_helm_run_to_eee(row, local_eee_root=local_eee_root)


def _write_local_reproduce_script(parent: Path, run_path: Path, artifact_path: Path) -> Path:
    script_fpath = parent / "reproduce.sh"
    cmd = [
        os.environ.get("EEE_CMD", "every_eval_ever"),
        "convert",
        "helm",
        "--log_path",
        str(run_path),
        "--output_dir",
        str(artifact_path),
        "--source_organization_name",
        "eval_audit_local",
        "--evaluator_relationship",
        "third_party",
        "--eval_library_name",
        "HELM",
        "--eval_library_version",
        "unknown",
    ]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {shlex.quote(str(repo_root()))}",
        "PYTHONPATH=\"${PYTHONPATH:-$PWD}\" " + " ".join(shlex.quote(part) for part in cmd),
    ]
    _atomic_write_text(script_fpath, "\n".join(lines) + "\n")
    script_fpath.chmod(0o755)
    return script_fpath


def _aggregate_path_for_log(log: Any, artifact_path: Path, eval_uuid: str) -> Path:
    detail = getattr(log, "detailed_evaluation_results", None)
    detail_path = Path(detail.file_path) if detail is not None and detail.file_path else None
    if detail_path is not None:
        stem = detail_path.stem.removesuffix("_samples")
        return detail_path.with_name(stem + ".json")

    dataset = "unknown"
    results = getattr(log, "evaluation_results", None) or []
    if results and getattr(results[0], "source_data", None):
        dataset = results[0].source_data.dataset_name or "unknown"
    model_id = getattr(getattr(log, "model_info", None), "id", None) or "unknown"
    if "/" in model_id:
        developer, model_name = model_id.split("/", 1)
    else:
        developer, model_name = "unknown", model_id
    out_dir = artifact_path / dataset / developer / model_name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{eval_uuid}.json"


def convert_local_helm_run_to_eee(
    row: dict[str, Any],
    *,
    local_eee_root: str | Path | None = None,
) -> EeeArtifactResolution:
    """Convert one local HELM run directory into the canonical local EEE tree."""

    run_path_text = _clean_optional_text(row.get("run_path") or row.get("run_dir"))
    if not run_path_text:
        return EeeArtifactResolution(
            artifact_path=None,
            status="missing_run_path",
            source="local_conversion",
        )
    run_path = Path(run_path_text).expanduser().resolve()
    if not run_path.is_dir():
        return EeeArtifactResolution(
            artifact_path=None,
            status="missing_run_path",
            source="local_conversion",
            message=f"run path does not exist: {run_path}",
        )

    parent = local_eee_parent_for_row(row, local_eee_root=local_eee_root)
    artifact_path = parent / "eee_output"
    status_path = parent / "status.json"
    provenance_path = parent / "provenance.json"
    parent.mkdir(parents=True, exist_ok=True)
    artifact_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).isoformat()
    eval_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, str(run_path)))
    metadata_args = {
        "source_organization_name": "eval_audit_local",
        "evaluator_relationship": "third_party",
        "eval_library_name": "HELM",
        "eval_library_version": "unknown",
        "parent_eval_output_dir": str(artifact_path),
        "file_uuid": eval_uuid,
    }
    status: dict[str, Any] = {
        "source_kind": "local",
        "status": "started",
        "timestamp": timestamp,
        "run_path": str(run_path),
        "out_dir": str(parent),
        "eee_artifact_path": str(artifact_path),
        "component_id": _clean_optional_text(row.get("component_id")),
        "experiment_name": _clean_optional_text(row.get("experiment_name")),
        "job_id": _clean_optional_text(row.get("job_id")),
        "attempt_identity": _clean_optional_text(row.get("attempt_identity")),
        "converter_name": _eee_converter_name(),
        "converter_version": _eee_converter_version(),
    }

    try:
        from every_eval_ever.converters.helm.adapter import HELMAdapter

        adapter = HELMAdapter()
        logs = adapter.transform_from_directory(
            str(run_path),
            output_path=str(artifact_path / "helm_output"),
            metadata_args=metadata_args,
        )
        aggregate_paths: list[str] = []
        for log in logs:
            aggregate_path = _aggregate_path_for_log(log, artifact_path, eval_uuid)
            _atomic_write_text(
                aggregate_path,
                log.model_dump_json(exclude_none=True, indent=2) + "\n",
            )
            aggregate_paths.append(str(aggregate_path))
        status.update(
            {
                "status": "ok",
                "returncode": 0,
                "n_evaluation_logs": len(logs),
                "aggregate_paths": aggregate_paths,
            }
        )
        provenance = {
            **status,
            "index_row": dict(row),
            "reproduce_script": str(_write_local_reproduce_script(parent, run_path, artifact_path)),
        }
        _atomic_write_text(provenance_path, json.dumps(provenance, indent=2, default=str) + "\n")
        _atomic_write_text(status_path, json.dumps(status, indent=2, default=str) + "\n")
        if not _artifact_has_aggregate(artifact_path):
            return EeeArtifactResolution(
                artifact_path=None,
                status="conversion_empty",
                source="local_conversion",
                status_path=status_path.resolve(),
                provenance_path=provenance_path.resolve(),
                message="converter completed but wrote no aggregate JSON",
                generated=True,
            )
        return EeeArtifactResolution(
            artifact_path=artifact_path.resolve(),
            status="generated",
            source="local_conversion",
            status_path=status_path.resolve(),
            provenance_path=provenance_path.resolve(),
            generated=True,
        )
    except Exception as exc:
        status.update(
            {
                "status": "fail",
                "exception_class": type(exc).__name__,
                "failure_snippet": traceback.format_exc()[-4000:],
                "returncode": -1,
            }
        )
        _atomic_write_text(status_path, json.dumps(status, indent=2, default=str) + "\n")
        return EeeArtifactResolution(
            artifact_path=None,
            status="conversion_failed",
            source="local_conversion",
            status_path=status_path.resolve(),
            provenance_path=provenance_path.resolve() if provenance_path.exists() else None,
            message=f"{type(exc).__name__}: {exc}",
            generated=True,
        )


__all__ = [
    "EeeArtifactResolution",
    "convert_helm_run_to_cached_eee",
    "convert_local_helm_run_to_eee",
    "default_helm_raw_cache_root",
    "default_local_eee_root",
    "default_official_eee_root",
    "helm_raw_cache_parent",
    "local_eee_parent_for_row",
    "resolve_local_eee_artifact",
    "resolve_official_eee_artifact",
]
