from __future__ import annotations

import csv
import itertools
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from eval_audit.indexing.schema import (
    component_id_for_local,
    extract_run_spec_fields,
    logical_run_key_for_local,
    logical_run_key_for_official,
    now_utc_iso,
)
from eval_audit.normalized.eee_artifacts import (
    EeeArtifactResolution,
    default_local_eee_root,
    default_official_eee_root,
    resolve_local_eee_artifact,
    resolve_official_eee_artifact,
)
from eval_audit.reports.core_packet import slugify_identifier

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func


PLANNER_VERSION = "core_report_packet_planner.v1"
OFFICIAL_SELECTION_POLICY = "latest_suite_version_per_public_track"


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"none", "nan"}:
        return None
    return text


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return float("-inf")


def _build_attempt_fallback_key(row: dict[str, Any]) -> str:
    parts = {
        "experiment_name": _clean_optional_text(row.get("experiment_name")) or "unknown",
        "job_id": _clean_optional_text(row.get("job_id")) or "unknown",
        "run_entry": _clean_optional_text(row.get("run_entry")) or "unknown",
        "manifest_timestamp": _clean_optional_text(row.get("manifest_timestamp")) or "unknown",
        "machine_host": _clean_optional_text(row.get("machine_host")) or "unknown",
        "run_path": _clean_optional_text(row.get("run_path") or row.get("run_dir")) or "unknown",
    }
    return "fallback::" + "|".join(f"{key}={value}" for key, value in parts.items())


def _attempt_identity(row: dict[str, Any]) -> tuple[str | None, str | None]:
    attempt_uuid = _clean_optional_text(row.get("attempt_uuid"))
    attempt_identity = _clean_optional_text(row.get("attempt_identity")) or attempt_uuid
    if attempt_identity:
        return attempt_uuid, attempt_identity
    fallback = _clean_optional_text(row.get("attempt_fallback_key")) or _build_attempt_fallback_key(row)
    return None, fallback


def _read_run_spec(run_spec_fpath: str | Path | None) -> dict[str, Any]:
    if run_spec_fpath is None:
        return {}
    fpath = Path(run_spec_fpath)
    if not fpath.exists():
        return {}
    try:
        data = json.loads(fpath.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _suite_version_sort_key(value: str | None) -> tuple[Any, ...]:
    text = _clean_optional_text(value) or ""
    parts = re.split(r"(\d+)", text)
    key: list[Any] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((1, int(part)))
        else:
            key.append((0, part))
    return tuple(key)


def _official_fallback_component_id(row: dict[str, Any], logical_run_key: str | None) -> str:
    seed_parts = [
        _clean_optional_text(row.get("public_track")) or "unknown-track",
        _clean_optional_text(row.get("suite_version")) or "unknown-suite-version",
        logical_run_key or _clean_optional_text(row.get("run_name")) or "unknown-run",
        _clean_optional_text(row.get("run_path") or row.get("public_run_dir")) or "unknown-path",
    ]
    return "official::" + "::".join(slugify_identifier(part) for part in seed_parts)


@dataclass(frozen=True)
class NormalizedPlannerComponent:
    component_id: str
    source_kind: str
    logical_run_key: str | None
    run_entry: str | None
    run_path: str | None
    job_path: str | None
    run_spec_fpath: str | None
    run_spec_name: str | None
    model: str | None
    scenario_class: str | None
    benchmark_group: str | None
    model_deployment: str | None
    max_eval_instances: str | None
    suite: str | None
    public_track: str | None
    suite_version: str | None
    experiment_name: str | None
    machine_host: str | None
    attempt_uuid: str | None
    attempt_identity: str | None
    display_name: str
    tags: list[str]
    manifest_timestamp: str | None
    provenance: dict[str, Any]
    extra_metadata: dict[str, Any]
    # Stage-5 normalized-format fields. ``artifact_format`` is one of
    # ``helm`` (raw HELM JSON tree), ``eee`` (converted EEE artifact tree),
    # or any future format registered with eval_audit.normalized.loaders.
    # ``eee_artifact_path`` points at the converted EEE artifact directory
    # when one has been pre-built; reports prefer it when present and fall
    # back to in-memory HELM->EEE conversion via ``run_path`` otherwise.
    artifact_format: str = "helm"
    eee_artifact_path: str | None = None

    def to_manifest_component(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "source_kind": self.source_kind,
            "artifact_format": self.artifact_format,
            "eee_artifact_path": self.eee_artifact_path,
            "run_path": self.run_path,
            "job_path": self.job_path,
            "attempt_uuid": self.attempt_uuid,
            "attempt_identity": self.attempt_identity,
            "display_name": self.display_name,
            "tags": list(self.tags),
            "machine_host": self.machine_host,
            "experiment_name": self.experiment_name,
            "run_entry": self.run_entry,
            "logical_run_key": self.logical_run_key,
            "provenance": self.provenance,
            "model": self.model,
            "scenario_class": self.scenario_class,
            "benchmark_group": self.benchmark_group,
            "model_deployment": self.model_deployment,
            "max_eval_instances": self.max_eval_instances,
            "suite": self.suite,
            "public_track": self.public_track,
            "suite_version": self.suite_version,
            **self.extra_metadata,
        }


@profile
def load_index_rows(index_fpath: str | Path) -> list[dict[str, Any]]:
    with Path(index_fpath).open(newline="") as file:
        return [{k: ("" if v is None else v) for k, v in row.items()} for row in csv.DictReader(file)]


def _row_logical_keys(row: dict[str, Any]) -> set[str]:
    keys = {
        _clean_optional_text(row.get("logical_run_key")),
        _clean_optional_text(row.get("run_entry")),
        _clean_optional_text(row.get("run_name")),
        _clean_optional_text(row.get("run_spec_name")),
    }
    return {key for key in keys if key}


@profile
def _prefilter_index_rows(
    *,
    local_rows: list[dict[str, Any]],
    official_rows: list[dict[str, Any]],
    experiment_name: str | None,
    run_entry: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Narrow raw CSV rows before expensive EEE discovery.

    The planner historically normalized the whole official public index and
    filtered later. EEE discovery adds filesystem checks, so small report
    slices need to apply the cheap CSV-level filters first.
    """
    scoped_local_rows = []
    for row in local_rows:
        if experiment_name is not None and row.get("experiment_name") != experiment_name:
            continue
        if run_entry is not None and run_entry not in _row_logical_keys(row):
            continue
        # Drop rows that point at no executed evidence. The HELM-shaped
        # path supplies a run_path / run_dir; the pure-EEE path supplies
        # eee_artifact_path. Either one is acceptable; only when *both*
        # are missing is the row a scheduled-but-never-completed attempt
        # with nothing to compare.
        has_helm_path = bool(_clean_optional_text(row.get("run_path") or row.get("run_dir")))
        has_eee_path = bool(_clean_optional_text(row.get("eee_artifact_path") or row.get("eee_path")))
        if not has_helm_path and not has_eee_path:
            continue
        scoped_local_rows.append(row)

    if run_entry is not None:
        wanted_keys = {run_entry}
    elif experiment_name is not None:
        wanted_keys = set().union(*(_row_logical_keys(row) for row in scoped_local_rows)) if scoped_local_rows else set()
    else:
        wanted_keys = set()

    if wanted_keys:
        scoped_official_rows = [
            row for row in official_rows
            if _row_logical_keys(row) & wanted_keys
        ]
    elif experiment_name is not None or run_entry is not None:
        scoped_official_rows = []
    else:
        scoped_official_rows = official_rows

    return scoped_local_rows, scoped_official_rows


def _resolve_artifact_format(row: dict[str, Any]) -> tuple[str, str | None, bool]:
    """Determine ``(artifact_format, eee_artifact_path)`` for an index row.

    Index rows that explicitly set a *non-default* ``artifact_format`` win.
    The local audit indexer hardcodes ``artifact_format='helm'`` as a
    placeholder default, so we treat that value as non-explicit — otherwise
    every local row would short-circuit out of the EEE resolver and force a
    re-conversion via :class:`HelmRawLoader`. Only formats other than
    ``helm`` are taken as user intent.
    """
    explicit = _clean_optional_text(row.get("artifact_format"))
    eee_path = _clean_optional_text(row.get("eee_artifact_path") or row.get("eee_path"))
    if explicit and explicit != "helm":
        return explicit, eee_path, True
    if eee_path:
        return "eee", eee_path, False
    return "helm", None, False


def _artifact_resolution_metadata(
    resolution: EeeArtifactResolution | None,
) -> dict[str, Any]:
    return resolution.to_dict() if resolution is not None else {}


def _apply_eee_resolution(
    *,
    row: dict[str, Any],
    source_kind: str,
    artifact_format: str,
    eee_artifact_path: str | None,
    explicit_artifact_format: bool,
    official_eee_root: str | Path | None,
    local_eee_root: str | Path | None,
    ensure_local_eee: bool,
) -> tuple[str, str | None, EeeArtifactResolution | None]:
    """Prefer converted EEE artifacts when discovery finds one.

    An explicit non-EEE artifact format in the index row is respected. Older
    indexes commonly lack these columns, so discovery fills the gap using the
    canonical official sweep root or the local canonical EEE root.
    """
    if artifact_format == "eee" and eee_artifact_path:
        return artifact_format, eee_artifact_path, None
    if explicit_artifact_format and artifact_format != "eee":
        return artifact_format, eee_artifact_path, None
    if source_kind == "official":
        resolution = resolve_official_eee_artifact(
            row,
            official_eee_root=official_eee_root,
        )
    else:
        resolution = resolve_local_eee_artifact(
            row,
            local_eee_root=local_eee_root,
            ensure=ensure_local_eee,
        )
    if resolution.artifact_path is not None:
        return "eee", str(resolution.artifact_path), resolution
    return artifact_format, eee_artifact_path, resolution


@profile
def normalize_local_index_rows(
    rows: list[dict[str, Any]],
    *,
    index_fpath: str | Path,
    local_eee_root: str | Path | None = None,
    ensure_local_eee: bool = False,
) -> list[NormalizedPlannerComponent]:
    index_fpath = str(Path(index_fpath).expanduser().resolve())
    local_eee_root = local_eee_root or default_local_eee_root()
    components: list[NormalizedPlannerComponent] = []
    for row_index, row in enumerate(rows):
        run_path = _clean_optional_text(row.get("run_path") or row.get("run_dir"))
        run_spec_fpath = _clean_optional_text(row.get("run_spec_fpath"))
        spec_fields = extract_run_spec_fields(run_spec_fpath)
        attempt_uuid, attempt_identity = _attempt_identity(row)
        component_id = _clean_optional_text(row.get("component_id")) or component_id_for_local(
            experiment_name=_clean_optional_text(row.get("experiment_name")),
            job_id=_clean_optional_text(row.get("job_id")),
            attempt_identity=attempt_identity,
        )
        logical_run_key = _clean_optional_text(row.get("logical_run_key")) or logical_run_key_for_local(
            run_spec_name=spec_fields.get("run_spec_name"),
            run_entry=_clean_optional_text(row.get("run_entry")),
        )
        tags = ["local"]
        if attempt_uuid:
            tags.append("has_attempt_uuid")
        else:
            tags.append("fallback_attempt_identity")
        artifact_format, eee_artifact_path, explicit_artifact_format = _resolve_artifact_format(row)
        artifact_format, eee_artifact_path, eee_resolution = _apply_eee_resolution(
            row=row,
            source_kind="local",
            artifact_format=artifact_format,
            eee_artifact_path=eee_artifact_path,
            explicit_artifact_format=explicit_artifact_format,
            official_eee_root=None,
            local_eee_root=local_eee_root,
            ensure_local_eee=ensure_local_eee,
        )
        if artifact_format == "eee":
            tags.append("eee_artifact_present")
        elif eee_resolution is not None and eee_resolution.status not in {"root_missing"}:
            tags.append(f"eee_artifact_{eee_resolution.status}")
        components.append(
            NormalizedPlannerComponent(
                component_id=component_id,
                source_kind="local",
                logical_run_key=logical_run_key,
                run_entry=_clean_optional_text(row.get("run_entry")),
                run_path=run_path,
                job_path=_clean_optional_text(row.get("job_dpath")),
                run_spec_fpath=run_spec_fpath,
                run_spec_name=spec_fields.get("run_spec_name") or _clean_optional_text(row.get("run_spec_name")),
                model=spec_fields.get("model") or _clean_optional_text(row.get("model")),
                scenario_class=spec_fields.get("scenario_class") or _clean_optional_text(row.get("scenario_class")),
                benchmark_group=spec_fields.get("benchmark_group") or _clean_optional_text(row.get("benchmark_group")),
                model_deployment=spec_fields.get("model_deployment") or _clean_optional_text(row.get("model_deployment")),
                max_eval_instances=_clean_optional_text(row.get("max_eval_instances")),
                suite=_clean_optional_text(row.get("suite")),
                public_track=None,
                suite_version=None,
                experiment_name=_clean_optional_text(row.get("experiment_name")),
                machine_host=_clean_optional_text(row.get("machine_host")),
                attempt_uuid=attempt_uuid,
                attempt_identity=attempt_identity,
                display_name=f"local: {Path(run_path).name if run_path else component_id}",
                tags=tags,
                manifest_timestamp=_clean_optional_text(row.get("manifest_timestamp")),
                provenance={
                    "source_index_kind": "local",
                    "source_index_fpath": index_fpath,
                    "source_row_index": row_index,
                    "source_component_id": _clean_optional_text(row.get("component_id")),
                    "source_job_id": _clean_optional_text(row.get("job_id")),
                },
                extra_metadata={
                    "attempt_identity_kind": _clean_optional_text(row.get("attempt_identity_kind")) or ("attempt_uuid" if attempt_uuid else "fallback"),
                    "attempt_fallback_key": _clean_optional_text(row.get("attempt_fallback_key")) or (_build_attempt_fallback_key(row) if not attempt_uuid else None),
                    "status": _clean_optional_text(row.get("status")),
                    "eee_artifact_resolution": _artifact_resolution_metadata(eee_resolution),
                },
                artifact_format=artifact_format,
                eee_artifact_path=eee_artifact_path,
            )
        )
    return components


@profile
def normalize_official_index_rows(
    rows: list[dict[str, Any]],
    *,
    index_fpath: str | Path,
    official_eee_root: str | Path | None = None,
) -> list[NormalizedPlannerComponent]:
    index_fpath = str(Path(index_fpath).expanduser().resolve())
    official_eee_root = official_eee_root or default_official_eee_root()
    components: list[NormalizedPlannerComponent] = []
    for row_index, row in enumerate(rows):
        run_path = _clean_optional_text(row.get("run_path") or row.get("public_run_dir"))
        run_spec_fpath = _clean_optional_text(row.get("run_spec_fpath"))
        spec_fields = extract_run_spec_fields(run_spec_fpath)
        logical_run_key = _clean_optional_text(row.get("logical_run_key")) or logical_run_key_for_official(
            run_spec_name=spec_fields.get("run_spec_name"),
            run_name=_clean_optional_text(row.get("run_name")),
        )
        component_id = _clean_optional_text(row.get("component_id")) or _official_fallback_component_id(row, logical_run_key)
        artifact_format, eee_artifact_path, explicit_artifact_format = _resolve_artifact_format(row)
        artifact_format, eee_artifact_path, eee_resolution = _apply_eee_resolution(
            row=row,
            source_kind="official",
            artifact_format=artifact_format,
            eee_artifact_path=eee_artifact_path,
            explicit_artifact_format=explicit_artifact_format,
            official_eee_root=official_eee_root,
            local_eee_root=None,
            ensure_local_eee=False,
        )
        official_tags = ["official", "public_reference_candidate"]
        if artifact_format == "eee":
            official_tags.append("eee_artifact_present")
        elif eee_resolution is not None and eee_resolution.status not in {"root_missing"}:
            official_tags.append(f"eee_artifact_{eee_resolution.status}")
        components.append(
            NormalizedPlannerComponent(
                component_id=component_id,
                source_kind="official",
                logical_run_key=logical_run_key,
                run_entry=None,
                run_path=run_path,
                job_path=None,
                run_spec_fpath=run_spec_fpath,
                run_spec_name=spec_fields.get("run_spec_name") or _clean_optional_text(row.get("run_spec_name")),
                model=spec_fields.get("model") or _clean_optional_text(row.get("model")),
                scenario_class=spec_fields.get("scenario_class") or _clean_optional_text(row.get("scenario_class")),
                benchmark_group=spec_fields.get("benchmark_group") or _clean_optional_text(row.get("benchmark_group")),
                model_deployment=spec_fields.get("model_deployment") or _clean_optional_text(row.get("model_deployment")),
                max_eval_instances=_clean_optional_text(row.get("max_eval_instances")),
                suite=None,
                public_track=_clean_optional_text(row.get("public_track")),
                suite_version=_clean_optional_text(row.get("suite_version")),
                experiment_name=None,
                machine_host=None,
                attempt_uuid=None,
                attempt_identity=component_id,
                display_name=f"official: {Path(run_path).name if run_path else logical_run_key or row_index}",
                tags=official_tags,
                manifest_timestamp=None,
                provenance={
                    "source_index_kind": "official",
                    "source_index_fpath": index_fpath,
                    "source_row_index": row_index,
                    "source_component_id": _clean_optional_text(row.get("component_id")),
                    "source_public_track": _clean_optional_text(row.get("public_track")),
                    "source_suite_version": _clean_optional_text(row.get("suite_version")),
                },
                extra_metadata={
                    "run_name": _clean_optional_text(row.get("run_name")),
                    "eee_artifact_resolution": _artifact_resolution_metadata(eee_resolution),
                },
                artifact_format=artifact_format,
                eee_artifact_path=eee_artifact_path,
            )
        )
    return components


@profile
def normalize_index_rows(
    *,
    local_rows: list[dict[str, Any]],
    official_rows: list[dict[str, Any]],
    local_index_fpath: str | Path,
    official_index_fpath: str | Path,
    official_eee_root: str | Path | None = None,
    local_eee_root: str | Path | None = None,
    ensure_local_eee: bool = False,
) -> list[NormalizedPlannerComponent]:
    return [
        *normalize_local_index_rows(
            local_rows,
            index_fpath=local_index_fpath,
            local_eee_root=local_eee_root,
            ensure_local_eee=ensure_local_eee,
        ),
        *normalize_official_index_rows(
            official_rows,
            index_fpath=official_index_fpath,
            official_eee_root=official_eee_root,
        ),
    ]


def _component_sort_key(component: NormalizedPlannerComponent) -> tuple[Any, ...]:
    if component.source_kind == "local":
        return (
            0,
            -_coerce_float(component.manifest_timestamp),
            component.experiment_name or "",
            component.component_id,
        )
    return (
        1,
        component.public_track or "",
        component.suite_version or "",
        component.component_id,
    )


def _unique_nonempty(values: list[str | None]) -> list[str]:
    seen: list[str] = []
    for value in values:
        text = _clean_optional_text(value)
        if text and text not in seen:
            seen.append(text)
    return seen


def _fact_status(values: list[str | None]) -> tuple[str, list[str]]:
    present = _unique_nonempty(values)
    if not present:
        return "unknown", []
    if len(present) == 1:
        return "yes", present
    return "no", present


def _component_instructions(component: NormalizedPlannerComponent) -> str | None:
    run_spec = _read_run_spec(component.run_spec_fpath)
    adapter = run_spec.get("adapter_spec") or {}
    if isinstance(adapter, dict):
        return _clean_optional_text(adapter.get("instructions"))
    return None


def _component_suite_descriptor(
    component: NormalizedPlannerComponent,
    *,
    all_local: bool = False,
) -> str | None:
    if component.source_kind == "local":
        # Cross-kind comparisons (official_vs_local) deliberately return None
        # for the local side because ``component.suite`` lives in a different
        # namespace than ``public_track::suite_version`` and a string compare
        # would fabricate spurious drift. For local-vs-local comparisons that
        # concern is gone — fall back to ``experiment_name`` so two locals
        # from the same experiment register as ``same_suite_or_track_version=yes``
        # rather than ``unknown``.
        if all_local:
            return component.suite or component.experiment_name
        return component.suite
    parts = [part for part in [component.public_track, component.suite_version] if _clean_optional_text(part)]
    return "::".join(parts) if parts else None


def build_comparability_facts(components: list[NormalizedPlannerComponent]) -> dict[str, Any]:
    all_local = bool(components) and all(c.source_kind == "local" for c in components)
    facts = {
        "same_model": {},
        "same_scenario_class": {},
        "same_benchmark_family": {},
        "same_deployment": {},
        "same_instructions": {},
        "same_max_eval_instances": {},
        "same_suite_or_track_version": {},
    }
    fact_inputs = {
        "same_model": [component.model for component in components],
        "same_scenario_class": [component.scenario_class for component in components],
        "same_benchmark_family": [component.benchmark_group for component in components],
        "same_deployment": [component.model_deployment for component in components],
        "same_instructions": [_component_instructions(component) for component in components],
        "same_max_eval_instances": [component.max_eval_instances for component in components],
        "same_suite_or_track_version": [_component_suite_descriptor(component, all_local=all_local) for component in components],
    }
    for name, values in fact_inputs.items():
        status, present_values = _fact_status(values)
        facts[name] = {
            "status": status,
            "values": present_values,
        }
    return facts


def _comparison_caveats(comparability_facts: dict[str, Any]) -> list[str]:
    caveats: list[str] = []
    for name, fact in comparability_facts.items():
        status = fact.get("status")
        if status == "no":
            caveats.append(f"{name}=no values={fact.get('values')}")
        elif status == "unknown":
            caveats.append(f"{name}=unknown")
    return caveats


def _component_warning_lines(component: NormalizedPlannerComponent) -> list[str]:
    warnings: list[str] = []
    if component.source_kind == "local" and component.attempt_uuid is None:
        warnings.append(f"fallback_local_identity:{component.component_id}")
    if component.run_spec_fpath is None:
        warnings.append(f"missing_run_spec:{component.component_id}")
    if component.model is None:
        warnings.append(f"missing_model_metadata:{component.component_id}")
    if component.scenario_class is None:
        warnings.append(f"missing_scenario_class:{component.component_id}")
    if component.source_kind == "official" and (component.public_track is None or component.suite_version is None):
        warnings.append(f"missing_official_track_or_suite:{component.component_id}")
    return warnings


def _comparability_warning_lines(comparability_facts: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for name, fact in comparability_facts.items():
        status = fact.get("status")
        if status == "no":
            warnings.append(f"comparability_drift:{name}")
        elif status == "unknown":
            warnings.append(f"comparability_unknown:{name}")
    return warnings


def _comparison_payload(
    *,
    comparison_id: str,
    comparison_kind: str,
    components: list[NormalizedPlannerComponent],
    reference_component_id: str | None,
    enabled: bool,
    disabled_reason: str | None = None,
    notes: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    comparability_facts = build_comparability_facts(components)
    warnings = [
        *_comparability_warning_lines(comparability_facts),
        *itertools.chain.from_iterable(_component_warning_lines(component) for component in components),
    ]
    payload = {
        "comparison_id": comparison_id,
        "comparison_kind": comparison_kind,
        "component_ids": [component.component_id for component in components],
        "reference_component_id": reference_component_id,
        "enabled": enabled,
        "disabled_reason": disabled_reason,
        "notes": notes,
        "comparability_facts": comparability_facts,
        "warnings": list(dict.fromkeys(warnings)),
        "caveats": _comparison_caveats(comparability_facts),
    }
    if extra_fields:
        payload.update(extra_fields)
    return payload


def _latest_official_selection(official_components: list[NormalizedPlannerComponent]) -> dict[str, Any]:
    grouped_by_track: dict[str, list[NormalizedPlannerComponent]] = {}
    for component in official_components:
        track = component.public_track or "unknown-track"
        grouped_by_track.setdefault(track, []).append(component)
    retained: list[NormalizedPlannerComponent] = []
    discarded: list[str] = []
    retained_by_track: dict[str, list[str]] = {}
    considered_by_track: dict[str, list[str]] = {}
    warnings: list[str] = []
    for track, components in sorted(grouped_by_track.items()):
        considered_by_track[track] = [component.component_id for component in components]
        latest_suite_version = max(
            (component.suite_version for component in components),
            key=_suite_version_sort_key,
        )
        retained_components = [
            component for component in components
            if component.suite_version == latest_suite_version
        ]
        retained.extend(retained_components)
        retained_by_track[track] = [component.component_id for component in retained_components]
        discarded.extend(
            component.component_id
            for component in components
            if component.suite_version != latest_suite_version
        )
        if len(retained_components) > 1:
            warnings.append(f"multiple_official_candidates_after_latest_per_track:{track}")
    if len(retained_by_track) > 1:
        warnings.append("multiple_official_tracks_after_latest_per_track")
    return {
        "policy_name": OFFICIAL_SELECTION_POLICY,
        "considered_component_ids": [component.component_id for component in official_components],
        "retained_component_ids": [component.component_id for component in retained],
        "discarded_component_ids": discarded,
        "considered_by_track": considered_by_track,
        "retained_by_track": retained_by_track,
        "warnings": warnings,
    }


def _packet_payload(
    *,
    group_key: str,
    experiment_name: str | None,
    run_entry: str | None,
    local_components: list[NormalizedPlannerComponent],
    official_components: list[NormalizedPlannerComponent],
    official_selection: dict[str, Any],
    packet_track: str | None = None,
) -> dict[str, Any]:
    packet_components = [*local_components, *official_components]
    local_reference = local_components[0] if local_components else None
    packet_comparability_facts = build_comparability_facts(packet_components)
    packet_caveats = _comparison_caveats(packet_comparability_facts)
    packet_warnings = [
        *official_selection["warnings"],
        *itertools.chain.from_iterable(_component_warning_lines(component) for component in packet_components),
        *_comparability_warning_lines(packet_comparability_facts),
    ]
    comparisons: list[dict[str, Any]] = []
    if local_components:
        if not official_components:
            for local_component in local_components:
                comparisons.append(
                    _comparison_payload(
                        comparison_id=f"official_vs_local::{local_component.component_id}",
                        comparison_kind="official_vs_local",
                        components=[local_component],
                        reference_component_id=None,
                        enabled=False,
                        disabled_reason="missing_official_component",
                        notes="planner could not find an official component after policy reduction",
                    )
                )
        elif len(official_components) == 1:
            official_reference = official_components[0]
            for local_component in local_components:
                comparisons.append(
                    _comparison_payload(
                        comparison_id=f"official_vs_local::{official_reference.component_id}::{local_component.component_id}",
                        comparison_kind="official_vs_local",
                        components=[official_reference, local_component],
                        reference_component_id=official_reference.component_id,
                        enabled=True,
                        notes="planner first-pass official-vs-local comparison",
                    )
                )
        else:
            candidate_official_ids = [component.component_id for component in official_components]
            for local_component in local_components:
                comparisons.append(
                    _comparison_payload(
                        comparison_id=f"official_vs_local::{local_component.component_id}::disabled",
                        comparison_kind="official_vs_local",
                        components=[*official_components, local_component],
                        reference_component_id=None,
                        enabled=False,
                        disabled_reason="ambiguous_official_candidates_after_latest_per_track",
                        notes="planner retained more than one official candidate after latest-per-track reduction",
                        extra_fields={"candidate_reference_component_ids": candidate_official_ids},
                    )
                )
    # local_repeat comparisons (one local replica vs another, same model
    # + benchmark) are useful for measuring intra-recipe noise but are
    # not what the heatmap-paper analysis cares about. Set
    # EVAL_AUDIT_SKIP_LOCAL_REPEAT={1,true,yes} to skip generating them
    # entirely — saves compute on cells with many replicas (e.g.
    # Pythia-6.9B mmlu has 15 local components, which would generate 14
    # local_repeat pairs per cell). The official_vs_local comparisons
    # are unaffected; reviewers can re-enable replicas by unsetting the
    # env var.
    _skip_local_repeat = os.environ.get(
        "EVAL_AUDIT_SKIP_LOCAL_REPEAT", ""
    ).strip().lower() in {"1", "true", "yes"}
    if not _skip_local_repeat and local_reference is not None:
        for repeat_component in local_components[1:]:
            comparisons.append(
                _comparison_payload(
                    comparison_id=f"local_repeat::{local_reference.component_id}::{repeat_component.component_id}",
                    comparison_kind="local_repeat",
                    components=[local_reference, repeat_component],
                    reference_component_id=local_reference.component_id,
                    enabled=True,
                    notes="planner first-pass local repeat comparison",
                )
            )
    packet_experiment_name = experiment_name
    if packet_experiment_name is None:
        experiment_names = _unique_nonempty([component.experiment_name for component in local_components])
        packet_experiment_name = experiment_names[0] if len(experiment_names) == 1 else None
    packet_id_parts = [packet_experiment_name or "all-experiments", group_key]
    if packet_track is not None:
        packet_id_parts.append(f"track={packet_track}")
    packet_id = slugify_identifier("::".join(packet_id_parts))
    return {
        "packet_id": packet_id,
        "run_entry": run_entry or next((component.run_entry for component in local_components if component.run_entry), None) or group_key,
        "logical_run_key": group_key,
        "experiment_name": packet_experiment_name,
        "components": [component.to_manifest_component() for component in packet_components],
        "comparisons": comparisons,
        "comparability_facts": packet_comparability_facts,
        "official_selection": official_selection,
        "warnings": list(dict.fromkeys([
            *packet_warnings,
            *(["missing_local_component"] if not local_components else []),
            *(["multiple_local_components"] if len(local_components) > 1 else []),
            *(["missing_official_component"] if not official_components else []),
            *(["split_by_public_track"] if packet_track is not None else []),
        ])),
        "caveats": packet_caveats,
        "planner_version": PLANNER_VERSION,
        "selected_public_track": packet_track,
    }


def build_packet_intents(
    components: list[NormalizedPlannerComponent],
    *,
    experiment_name: str | None = None,
    run_entry: str | None = None,
) -> list[dict[str, Any]]:
    filtered = []
    for component in components:
        if experiment_name is not None and component.source_kind == "local" and component.experiment_name != experiment_name:
            continue
        if run_entry is not None and component.logical_run_key != run_entry and component.run_entry != run_entry:
            continue
        filtered.append(component)
    if experiment_name is not None and run_entry is None:
        scoped_local_group_keys = {
            component.logical_run_key or component.run_entry or component.component_id
            for component in filtered
            if component.source_kind == "local"
        }
        filtered = [
            component
            for component in filtered
            if component.source_kind == "local"
            or (component.logical_run_key or component.run_entry or component.component_id) in scoped_local_group_keys
        ]

    grouped: dict[str, list[NormalizedPlannerComponent]] = {}
    for component in filtered:
        key = component.logical_run_key or component.run_entry or component.component_id
        grouped.setdefault(key, []).append(component)

    packets: list[dict[str, Any]] = []
    for group_key, group_components in sorted(grouped.items()):
        sorted_components = sorted(group_components, key=_component_sort_key)
        local_components = [component for component in sorted_components if component.source_kind == "local"]
        official_components_all = [component for component in sorted_components if component.source_kind == "official"]
        official_selection = _latest_official_selection(official_components_all) if official_components_all else {
            "policy_name": OFFICIAL_SELECTION_POLICY,
            "considered_component_ids": [],
            "retained_component_ids": [],
            "discarded_component_ids": [],
            "considered_by_track": {},
            "retained_by_track": {},
            "warnings": [],
        }
        retained_official_ids = set(official_selection["retained_component_ids"])
        official_components = [
            component for component in official_components_all
            if component.component_id in retained_official_ids
        ]
        retained_by_track = official_selection.get("retained_by_track") or {}
        if len(retained_by_track) > 1:
            official_by_id = {
                component.component_id: component
                for component in official_components
            }
            for track, track_component_ids in sorted(retained_by_track.items()):
                track_official_components = [
                    official_by_id[component_id]
                    for component_id in track_component_ids
                    if component_id in official_by_id
                ]
                track_selection = {
                    **official_selection,
                    "selected_public_track": track,
                    "retained_component_ids": track_component_ids,
                    "retained_by_track": {track: track_component_ids},
                    "warnings": list(dict.fromkeys([
                        *official_selection.get("warnings", []),
                        f"render_split_by_public_track:{track}",
                    ])),
                }
                packets.append(
                    _packet_payload(
                        group_key=group_key,
                        experiment_name=experiment_name,
                        run_entry=run_entry,
                        local_components=local_components,
                        official_components=track_official_components,
                        official_selection=track_selection,
                        packet_track=track,
                    )
                )
        else:
            packets.append(
                _packet_payload(
                    group_key=group_key,
                    experiment_name=experiment_name,
                    run_entry=run_entry,
                    local_components=local_components,
                    official_components=official_components,
                    official_selection=official_selection,
                )
            )
    return packets


@profile
def build_planning_artifact(
    *,
    local_index_fpath: str | Path,
    official_index_fpath: str | Path,
    experiment_name: str | None = None,
    run_entry: str | None = None,
    official_eee_root: str | Path | None = None,
    local_eee_root: str | Path | None = None,
    ensure_local_eee: bool = False,
) -> dict[str, Any]:
    local_rows = load_index_rows(local_index_fpath)
    official_rows = load_index_rows(official_index_fpath)
    local_rows, official_rows = _prefilter_index_rows(
        local_rows=local_rows,
        official_rows=official_rows,
        experiment_name=experiment_name,
        run_entry=run_entry,
    )
    official_eee_root = official_eee_root or default_official_eee_root()
    local_eee_root = local_eee_root or default_local_eee_root()
    normalized_components = normalize_index_rows(
        local_rows=local_rows,
        official_rows=official_rows,
        local_index_fpath=local_index_fpath,
        official_index_fpath=official_index_fpath,
        official_eee_root=official_eee_root,
        local_eee_root=local_eee_root,
        ensure_local_eee=ensure_local_eee,
    )
    packets = build_packet_intents(
        normalized_components,
        experiment_name=experiment_name,
        run_entry=run_entry,
    )
    return {
        "generated_utc": now_utc_iso(),
        "planner_version": PLANNER_VERSION,
        "local_index_fpath": str(Path(local_index_fpath).expanduser().resolve()),
        "official_index_fpath": str(Path(official_index_fpath).expanduser().resolve()),
        "official_eee_root": str(Path(official_eee_root).expanduser().resolve()) if official_eee_root else None,
        "local_eee_root": str(Path(local_eee_root).expanduser().resolve()) if local_eee_root else None,
        "ensure_local_eee": ensure_local_eee,
        "experiment_name": experiment_name,
        "run_entry": run_entry,
        "packet_count": len(packets),
        "packets": packets,
    }


def load_planning_artifact(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Planner artifact must decode to a dict: {path}")
    return data


def select_packet_from_artifact(
    artifact: dict[str, Any],
    *,
    packet_id: str | None = None,
    run_entry: str | None = None,
    experiment_name: str | None = None,
) -> dict[str, Any]:
    packets = list(artifact.get("packets") or [])
    if packet_id is not None:
        matches = [packet for packet in packets if packet.get("packet_id") == packet_id]
        if not matches:
            raise KeyError(f"No planner packet matched packet_id={packet_id!r}")
        return matches[0]
    if run_entry is not None:
        packets = [packet for packet in packets if packet.get("run_entry") == run_entry]
    if experiment_name is not None:
        packets = [packet for packet in packets if packet.get("experiment_name") == experiment_name]
    if not packets:
        raise KeyError("No planner packets matched the requested selection")
    if len(packets) != 1:
        packet_ids = [packet.get("packet_id") for packet in packets]
        raise ValueError(
            "Planner selection was ambiguous; "
            f"matched packet_ids={packet_ids}. Pass packet_id explicitly."
        )
    return packets[0]


def comparison_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for packet in artifact.get("packets", []):
        for comparison in packet.get("comparisons", []):
            rows.append(
                {
                    "packet_id": packet.get("packet_id"),
                    "logical_run_key": packet.get("logical_run_key"),
                    "experiment_name": packet.get("experiment_name"),
                    **comparison,
                }
            )
    return rows


def component_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for packet in artifact.get("packets", []):
        for component in packet.get("components", []):
            rows.append(
                {
                    "packet_id": packet.get("packet_id"),
                    "logical_run_key": packet.get("logical_run_key"),
                    "experiment_name": packet.get("experiment_name"),
                    **component,
                }
            )
    return rows


def packet_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for packet in artifact.get("packets", []):
        rows.append(
            {
                "packet_id": packet.get("packet_id"),
                "logical_run_key": packet.get("logical_run_key"),
                "run_entry": packet.get("run_entry"),
                "experiment_name": packet.get("experiment_name"),
                "n_components": len(packet.get("components", [])),
                "n_comparisons": len(packet.get("comparisons", [])),
                "warnings": packet.get("warnings", []),
                "caveats": packet.get("caveats", []),
            }
        )
    return rows


def warning_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for packet in artifact.get("packets", []):
        for warning in packet.get("warnings", []):
            rows.append(
                {
                    "level": "packet",
                    "packet_id": packet.get("packet_id"),
                    "comparison_id": None,
                    "warning": warning,
                }
            )
        for comparison in packet.get("comparisons", []):
            if comparison.get("disabled_reason"):
                rows.append(
                    {
                        "level": "comparison",
                        "packet_id": packet.get("packet_id"),
                        "comparison_id": comparison.get("comparison_id"),
                        "warning": f"disabled:{comparison.get('disabled_reason')}",
                    }
                )
            for warning in comparison.get("warnings", []):
                rows.append(
                    {
                        "level": "comparison",
                        "packet_id": packet.get("packet_id"),
                        "comparison_id": comparison.get("comparison_id"),
                        "warning": warning,
                    }
                )
    return rows


def warning_summary_lines(artifact: dict[str, Any]) -> list[str]:
    lines = [
        "Core Report Packet Planning Warnings",
        "",
        f"generated_utc: {artifact.get('generated_utc')}",
        f"planner_version: {artifact.get('planner_version')}",
        "",
    ]
    for packet in artifact.get("packets", []):
        lines.append(f"packet: {packet.get('packet_id')}")
        packet_warnings = packet.get("warnings", [])
        if packet_warnings:
            lines.append("  packet_warnings:")
            for warning in packet_warnings:
                lines.append(f"    - {warning}")
        official_selection = packet.get("official_selection") or {}
        if official_selection:
            lines.append("  official_selection:")
            lines.append(f"    policy_name: {official_selection.get('policy_name')}")
            lines.append(f"    retained_component_ids: {official_selection.get('retained_component_ids')}")
            lines.append(f"    discarded_component_ids: {official_selection.get('discarded_component_ids')}")
            if official_selection.get("warnings"):
                lines.append(f"    warnings: {official_selection.get('warnings')}")
        lines.append("  comparisons:")
        for comparison in packet.get("comparisons", []):
            lines.append(
                f"    - {comparison.get('comparison_id')} enabled={comparison.get('enabled')} "
                f"disabled_reason={comparison.get('disabled_reason')}"
            )
            if comparison.get("warnings"):
                lines.append(f"      warnings: {comparison.get('warnings')}")
            if comparison.get("caveats"):
                lines.append(f"      caveats: {comparison.get('caveats')}")
        lines.append("")
    return lines


def planning_summary_lines(artifact: dict[str, Any]) -> list[str]:
    lines = [
        "Core Report Packet Planning Summary",
        "",
        f"generated_utc: {artifact.get('generated_utc')}",
        f"planner_version: {artifact.get('planner_version')}",
        f"local_index_fpath: {artifact.get('local_index_fpath')}",
        f"official_index_fpath: {artifact.get('official_index_fpath')}",
        f"official_eee_root: {artifact.get('official_eee_root')}",
        f"local_eee_root: {artifact.get('local_eee_root')}",
        f"ensure_local_eee: {artifact.get('ensure_local_eee')}",
        f"experiment_name: {artifact.get('experiment_name')}",
        f"run_entry: {artifact.get('run_entry')}",
        f"packet_count: {artifact.get('packet_count')}",
        "",
    ]
    for packet in artifact.get("packets", []):
        lines.append(f"packet: {packet['packet_id']}")
        lines.append(f"  logical_run_key: {packet.get('logical_run_key')}")
        lines.append(f"  run_entry: {packet.get('run_entry')}")
        lines.append(f"  experiment_name: {packet.get('experiment_name')}")
        lines.append(f"  warnings: {packet.get('warnings')}")
        lines.append(f"  caveats: {packet.get('caveats')}")
        lines.append(f"  official_selection: {packet.get('official_selection')}")
        lines.append("  components:")
        for component in packet.get("components", []):
            lines.append(
                f"    - {component['component_id']} source_kind={component.get('source_kind')} "
                f"artifact_format={component.get('artifact_format')} "
                f"eee_artifact_path={component.get('eee_artifact_path')} "
                f"tags={component.get('tags')} run_path={component.get('run_path')}"
            )
        lines.append("  comparisons:")
        for comparison in packet.get("comparisons", []):
            lines.append(
                f"    - {comparison['comparison_id']} kind={comparison.get('comparison_kind')} "
                f"component_ids={comparison.get('component_ids')} reference={comparison.get('reference_component_id')} "
                f"enabled={comparison.get('enabled')} disabled_reason={comparison.get('disabled_reason')}"
            )
            lines.append(f"      warnings={comparison.get('warnings')}")
            lines.append(f"      caveats={comparison.get('caveats')}")
        lines.append("  comparability_facts:")
        for fact_name, fact in (packet.get("comparability_facts") or {}).items():
            lines.append(
                f"    - {fact_name}: status={fact.get('status')} values={fact.get('values')}"
            )
        lines.append("")
    return lines
