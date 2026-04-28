from __future__ import annotations

import argparse
import csv
import datetime as datetime_mod
import json
import os
import resource
import shlex
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import kwutil

from eval_audit.infra.api import audit_root, default_index_root, default_store_root
from eval_audit.infra.plotly_env import configure_plotly_chrome
from eval_audit.infra.fs_publish import safe_unlink, stamped_history_dir, symlink_to, write_latest_alias
from eval_audit.infra.logging import rich_link, setup_cli_logging
from eval_audit.infra.paths import experiment_analysis_dpath
from eval_audit.infra.report_layout import (
    aggregate_summary_reports_root,
    experiments_analysis_root,
    legacy_repo_publication_root,
    portable_repo_root_lines,
    publication_experiments_root,
)
from eval_audit.model_registry import local_model_registry_by_name
from eval_audit.reports.core_packet_summary import (
    find_report_pair,
    load_core_report_bundle,
    packet_component_by_source_kind,
    packet_local_reference_component,
    prioritized_example_artifact_names,
    render_path_link,
)
from eval_audit.utils.numeric import nested_get
from eval_audit.utils.sankey import emit_sankey_artifacts
from eval_audit.utils import sankey_builder
from eval_audit.workflows.rebuild_core_report import main as rebuild_core_report_main

from loguru import logger


DEFAULT_BREAKDOWN_DIMS = [
    "experiment_name",
    "model",
    "benchmark",
    "suite",
    "machine_host",
]

CANONICAL_AGREEMENT_TOL = 0.05


def latest_index_csv(index_dpath: Path) -> Path:
    cands = sorted(index_dpath.glob("audit_results_index_*.csv"), reverse=True)
    if not cands:
        raise FileNotFoundError(f"No index csv files found in {index_dpath}")
    return cands[0]


def load_rows(index_fpath: Path) -> list[dict[str, Any]]:
    with index_fpath.open(newline="") as file:
        return [{k: ("" if v is None else v) for k, v in row.items()} for row in csv.DictReader(file)]


def slugify(text: str) -> str:
    return (
        text.replace("/", "-")
        .replace(":", "-")
        .replace(",", "-")
        .replace("=", "-")
        .replace("@", "-")
        .replace(" ", "-")
    )


def _load_json(fpath: Path) -> dict[str, Any]:
    return json.loads(fpath.read_text())


def _write_json(payload: Any, fpath: Path) -> None:
    fpath.write_text(json.dumps(kwutil.Json.ensure_serializable(payload), indent=2))


def _write_text(lines: list[str], fpath: Path) -> None:
    fpath.write_text("\n".join(lines).rstrip() + "\n")


def _find_pair(report: dict[str, Any], label: str) -> dict[str, Any]:
    return find_report_pair(report, label)


def _find_curve_value(rows: list[dict[str, Any]], abs_tol: float) -> float | None:
    for row in rows or []:
        try:
            if float(row.get("abs_tol")) == float(abs_tol):
                return float(row.get("agree_ratio"))
        except Exception:
            pass
    return None


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_truthy_text(value: Any) -> bool:
    return _normalize_text(value) in {"true", "1", "yes"}


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return float("-inf")


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"none", "nan"}:
        return None
    return text


def _preview_values(values: list[str], *, max_items: int = 6) -> list[str]:
    unique = sorted({value for value in values if _clean_optional_text(value)})
    if len(unique) <= max_items:
        return unique
    return unique[:max_items] + [f"... (+{len(unique) - max_items} more)"]


def _build_attempt_fallback_key_from_row(row: dict[str, Any]) -> str:
    parts = {
        "experiment_name": _clean_optional_text(row.get("experiment_name")) or "unknown",
        "job_id": _clean_optional_text(row.get("job_id")) or "unknown",
        "run_entry": _clean_optional_text(row.get("run_entry")) or "unknown",
        "manifest_timestamp": _clean_optional_text(row.get("manifest_timestamp")) or "unknown",
        "machine_host": _clean_optional_text(row.get("machine_host")) or "unknown",
        "run_dir": _clean_optional_text(row.get("run_dir")) or "unknown",
    }
    return "fallback::" + "|".join(f"{key}={value}" for key, value in parts.items())


def _resolve_attempt_identity(row: dict[str, Any]) -> dict[str, str | None]:
    attempt_uuid = _clean_optional_text(row.get("attempt_uuid"))
    attempt_fallback_key = _clean_optional_text(row.get("attempt_fallback_key")) or _build_attempt_fallback_key_from_row(row)
    attempt_identity = _clean_optional_text(row.get("attempt_identity")) or attempt_uuid or attempt_fallback_key
    attempt_identity_kind = _clean_optional_text(row.get("attempt_identity_kind")) or ("attempt_uuid" if attempt_uuid else "fallback")
    return {
        "attempt_uuid": attempt_uuid,
        "attempt_fallback_key": attempt_fallback_key,
        "attempt_identity": attempt_identity,
        "attempt_identity_kind": attempt_identity_kind,
    }


def _storyline_status(expected_local_served: bool, replaces_helm_deployment: str | None) -> str:
    if expected_local_served and replaces_helm_deployment:
        return "on_story"
    if expected_local_served:
        return "off_story"
    return "not_local_story"


def _storyline_reason(expected_local_served: bool, replaces_helm_deployment: str | None) -> str:
    if expected_local_served and replaces_helm_deployment:
        return "expected_local_served=True and replaces_helm_deployment points to a public HELM deployment"
    if expected_local_served:
        return "expected_local_served=True but replaces_helm_deployment is null, so this is a local extension outside the public HELM storyline"
    return "model is not marked as expected_local_served in the checked-in local model registry"


def _filter_inventory_lookup_by_run_entry(filter_inventory_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in filter_inventory_rows:
        run_entry = _clean_optional_text(row.get("run_spec_name"))
        if not run_entry:
            continue
        lookup[run_entry] = row
    return lookup


def _storyline_metadata_for_model(
    *,
    model: str | None,
    registry_lookup: dict[str, Any],
    filter_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reg_entry = registry_lookup.get(model or "")
    if reg_entry is not None:
        expected_local_served = bool(reg_entry.expected_local_served)
        replaces_helm_deployment = reg_entry.replaces_helm_deployment
        local_registry_source = reg_entry.source
        registry_notes = reg_entry.notes or None
    else:
        expected_local_served = _is_truthy_text((filter_row or {}).get("expected_local_served"))
        replaces_helm_deployment = _clean_optional_text((filter_row or {}).get("replaces_helm_deployment"))
        local_registry_source = _clean_optional_text((filter_row or {}).get("local_registry_source"))
        registry_notes = None
    status = _storyline_status(expected_local_served, replaces_helm_deployment)
    return {
        "expected_local_served": expected_local_served,
        "replaces_helm_deployment": replaces_helm_deployment,
        "local_registry_source": local_registry_source,
        "registry_notes": registry_notes,
        "storyline_status": status,
        "storyline_reason": _storyline_reason(expected_local_served, replaces_helm_deployment),
    }


def _run_entry_metadata_lookup(
    filter_inventory_rows: list[dict[str, Any]],
    scope_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in filter_inventory_rows:
        run_entry = _clean_optional_text(row.get("run_spec_name"))
        if not run_entry:
            continue
        info = lookup.setdefault(run_entry, {})
        for src_key, dst_key in [
            ("model", "model"),
            ("benchmark", "benchmark"),
            ("scenario", "scenario"),
            ("suite", "suite"),
            ("dataset", "dataset"),
            ("setting", "setting"),
        ]:
            value = _clean_optional_text(row.get(src_key))
            if value and not info.get(dst_key):
                info[dst_key] = value
    for row in scope_rows:
        run_entry = _clean_optional_text(row.get("run_entry"))
        if not run_entry:
            continue
        info = lookup.setdefault(run_entry, {})
        for src_key, dst_key in [
            ("model", "model"),
            ("benchmark", "benchmark"),
            ("suite", "suite"),
        ]:
            value = _clean_optional_text(row.get(src_key))
            if value and not info.get(dst_key):
                info[dst_key] = value
    return lookup


def _default_filter_inventory_json() -> Path:
    return default_store_root() / "analysis" / "filter_inventory.json"


def _load_filter_inventory_rows(
    filter_inventory_json: Path | None,
    *,
    skip: bool = False,
) -> list[dict[str, Any]]:
    """Load the Stage-1 filter inventory.

    When ``skip`` is True, return an empty list regardless of any explicit
    or default path. Use this for scoped sub-experiments (e.g. virtual
    experiments) where the global Stage-1 filter funnel does not describe
    the report's denominator and would only mislead the reader.
    """
    if skip:
        return []
    path = filter_inventory_json if filter_inventory_json is not None else _default_filter_inventory_json()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except Exception:
        logger.warning(f"Unable to load filter inventory: {rich_link(path)}")
        return []
    if not isinstance(payload, list):
        logger.warning(f"Filter inventory is not a list: {rich_link(path)}")
        return []
    return [row for row in payload if isinstance(row, dict)]


def _bucket_agreement(agree_ratio: float | None) -> str:
    if agree_ratio is None:
        return "not_analyzed"
    if agree_ratio >= 0.999999:
        return "exact_or_near_exact"
    if agree_ratio >= 0.95:
        return "high_agreement_0.95+"
    if agree_ratio >= 0.80:
        return "moderate_agreement_0.80+"
    if agree_ratio > 0.0:
        return "low_agreement_0.00+"
    return "zero_agreement"


FILTER_SELECTION_EXCLUDED_LABEL = "not selected for attempted runs"
FILTER_SELECTION_SELECTED_LABEL = "selected for attempted runs"
ATTEMPTED_LABEL = "attempted run"
NOT_ATTEMPTED_LABEL = "selected but not attempted"


def _primary_filter_reason(row: dict[str, Any]) -> str:
    reasons = [str(r) for r in (row.get("failure_reasons") or []) if str(r)]
    if row.get("selection_status") == "selected":
        return "selected"
    if row.get("is_structurally_incomplete"):
        return "structurally_incomplete"
    if reasons:
        return reasons[0]
    return "excluded_unknown"


def _classify_filter_pool(row: dict[str, Any]) -> str:
    if row.get("is_structurally_incomplete"):
        return "structurally_incomplete"
    return str(row.get("candidate_pool") or "unknown_pool")


def _classify_filter_outcome(row: dict[str, Any]) -> str:
    if row.get("selection_status") == "selected":
        return "selected_for_attempt"
    return f"excluded::{_primary_filter_reason(row)}"


def _group_scope_rows_by_run_entry(scope_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scope_rows:
        run_entry = str(row.get("run_entry") or "").strip()
        if run_entry:
            grouped[run_entry].append(row)
    return grouped


def _group_repro_rows_by_run_entry(repro_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in repro_rows:
        run_entry = str(row.get("run_entry") or "").strip()
        if run_entry:
            grouped[run_entry].append(row)
    return grouped


def _classify_execution_stage(scope_rows_for_entry: list[dict[str, Any]]) -> str:
    if not scope_rows_for_entry:
        return "not_run_in_scope"
    if any(_is_truthy_text(row.get("has_run_spec")) for row in scope_rows_for_entry):
        return "completed_with_run_artifacts"
    statuses = {_normalize_text(row.get("status")) for row in scope_rows_for_entry}
    if statuses & {"running", "queued"}:
        return "attempted_not_finished"
    return "attempted_failed_or_incomplete"


def _classify_analysis_stage(
    row: dict[str, Any],
    scope_rows_for_entry: list[dict[str, Any]],
    repro_rows_for_entry: list[dict[str, Any]],
) -> str:
    if row.get("selection_status") != "selected":
        return "stopped_after_filter"
    execution_stage = _classify_execution_stage(scope_rows_for_entry)
    if execution_stage != "completed_with_run_artifacts":
        return execution_stage
    if repro_rows_for_entry:
        return "analyzed"
    return "completed_not_yet_analyzed"


def _choose_repro_row_for_run_entry(
    repro_rows_for_entry: list[dict[str, Any]],
    scope_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    if not repro_rows_for_entry:
        return None

    def _repro_row_rank(row: dict[str, Any]) -> tuple[float, str, str, str, str]:
        key = (str(row.get("experiment_name") or ""), str(row.get("run_entry") or ""))
        matching_scope_rows = scope_rows_by_key.get(key, [])
        manifest_ts = max(
            (_coerce_float(item.get("manifest_timestamp")) for item in matching_scope_rows),
            default=float("-inf"),
        )
        return (
            manifest_ts,
            str(row.get("experiment_name") or ""),
            str(row.get("packet_id") or ""),
            str(row.get("report_dir") or ""),
            str(row.get("report_json") or ""),
        )

    return max(repro_rows_for_entry, key=_repro_row_rank)


def _classify_reproduction_stage(
    row: dict[str, Any],
    scope_rows_for_entry: list[dict[str, Any]],
    repro_rows_for_entry: list[dict[str, Any]],
    *,
    tol_key: str,
    scope_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]],
) -> str:
    if row.get("selection_status") != "selected":
        return "stopped_after_filter"
    execution_stage = _classify_execution_stage(scope_rows_for_entry)
    if execution_stage != "completed_with_run_artifacts":
        return execution_stage
    repro_row = _choose_repro_row_for_run_entry(repro_rows_for_entry, scope_rows_by_key)
    if repro_row is None:
        return "not_analyzed_yet"
    return _bucket_agreement(repro_row.get(tol_key))


def _build_end_to_end_funnel_rows(
    filter_inventory_rows: list[dict[str, Any]],
    scope_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
    *,
    tol_key: str,
) -> list[dict[str, str]]:
    scope_rows_by_run_entry = _group_scope_rows_by_run_entry(scope_rows)
    repro_rows_by_run_entry = _group_repro_rows_by_run_entry(repro_rows)
    scope_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in scope_rows:
        key = (str(row.get("experiment_name") or ""), str(row.get("run_entry") or ""))
        scope_rows_by_key[key].append(row)

    sankey_rows = []
    for row in filter_inventory_rows:
        run_entry = str(row.get("run_spec_name") or "")
        scope_rows_for_entry = scope_rows_by_run_entry.get(run_entry, [])
        repro_rows_for_entry = repro_rows_by_run_entry.get(run_entry, [])
        reasons = {str(r) for r in (row.get("failure_reasons") or []) if str(r)}
        flow: dict[str, str] = {}
        if row.get("is_structurally_incomplete"):
            flow["structural_gate"] = "excluded: structurally incomplete"
            sankey_rows.append(flow)
            continue
        flow["structural_gate"] = "kept: structurally complete"
        if "missing-model-metadata" in reasons:
            flow["metadata_gate"] = "excluded: missing model metadata"
            sankey_rows.append(flow)
            continue
        flow["metadata_gate"] = "kept: model metadata resolved"
        if "not-open-access" in reasons:
            flow["open_weight_gate"] = "excluded: not open weight"
            sankey_rows.append(flow)
            continue
        flow["open_weight_gate"] = "kept: open weight"
        if ("excluded-tags" in reasons) or ("not-text-like" in reasons):
            flow["tag_gate"] = "excluded: unsuitable text/modality tags"
            sankey_rows.append(flow)
            continue
        flow["tag_gate"] = "kept: suitable text tags"
        if "no-local-helm-deployment" in reasons:
            flow["deployment_gate"] = "excluded: no runnable local deployment"
            sankey_rows.append(flow)
            continue
        flow["deployment_gate"] = "kept: runnable local deployment"
        if "too-large" in reasons:
            flow["size_gate"] = "excluded: exceeds size budget"
            sankey_rows.append(flow)
            continue
        flow["size_gate"] = "kept: within size budget"
        if row.get("selection_status") != "selected":
            flow["selection_gate"] = FILTER_SELECTION_EXCLUDED_LABEL
            sankey_rows.append(flow)
            continue
        flow["selection_gate"] = FILTER_SELECTION_SELECTED_LABEL
        execution_stage = _classify_execution_stage(scope_rows_for_entry)
        flow["execution_stage"] = execution_stage
        if execution_stage != "completed_with_run_artifacts":
            sankey_rows.append(flow)
            continue
        repro_row = _choose_repro_row_for_run_entry(repro_rows_for_entry, scope_rows_by_key)
        if repro_row is None:
            flow["analysis_stage"] = "completed_not_yet_analyzed"
            sankey_rows.append(flow)
            continue
        flow["analysis_stage"] = "analyzed"
        flow["reproduction_stage"] = _bucket_agreement(repro_row.get(tol_key))
        sankey_rows.append(flow)
    return sankey_rows


def _build_filter_to_attempt_rows(
    filter_inventory_rows: list[dict[str, Any]],
    scope_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    scope_rows_by_run_entry = _group_scope_rows_by_run_entry(scope_rows)
    sankey_rows = []
    for row in filter_inventory_rows:
        run_entry = str(row.get("run_spec_name") or "")
        scope_rows_for_entry = scope_rows_by_run_entry.get(run_entry, [])
        reasons = {str(r) for r in (row.get("failure_reasons") or []) if str(r)}
        flow: dict[str, str] = {}
        if row.get("is_structurally_incomplete"):
            flow["structural_gate"] = "excluded: structurally incomplete"
            sankey_rows.append(flow)
            continue
        flow["structural_gate"] = "kept: structurally complete"
        if "missing-model-metadata" in reasons:
            flow["metadata_gate"] = "excluded: missing model metadata"
            sankey_rows.append(flow)
            continue
        flow["metadata_gate"] = "kept: model metadata resolved"
        if "not-open-access" in reasons:
            flow["open_weight_gate"] = "excluded: not open weight"
            sankey_rows.append(flow)
            continue
        flow["open_weight_gate"] = "kept: open weight"
        if ("excluded-tags" in reasons) or ("not-text-like" in reasons):
            flow["tag_gate"] = "excluded: unsuitable text/modality tags"
            sankey_rows.append(flow)
            continue
        flow["tag_gate"] = "kept: suitable text tags"
        if "no-local-helm-deployment" in reasons:
            flow["deployment_gate"] = "excluded: no runnable local deployment"
            sankey_rows.append(flow)
            continue
        flow["deployment_gate"] = "kept: runnable local deployment"
        if "too-large" in reasons:
            flow["size_gate"] = "excluded: exceeds size budget"
            sankey_rows.append(flow)
            continue
        flow["size_gate"] = "kept: within size budget"
        if row.get("selection_status") != "selected":
            flow["selection_gate"] = FILTER_SELECTION_EXCLUDED_LABEL
            sankey_rows.append(flow)
            continue
        flow["selection_gate"] = FILTER_SELECTION_SELECTED_LABEL
        execution_stage = _classify_execution_stage(scope_rows_for_entry)
        flow["attempt_stage"] = ATTEMPTED_LABEL if execution_stage != "not_run_in_scope" else NOT_ATTEMPTED_LABEL
        sankey_rows.append(flow)
    return sankey_rows


def _build_attempted_to_repro_rows(
    filter_inventory_rows: list[dict[str, Any]],
    scope_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
    *,
    tol_key: str,
) -> list[dict[str, str]]:
    scope_rows_by_run_entry = _group_scope_rows_by_run_entry(scope_rows)
    repro_rows_by_run_entry = _group_repro_rows_by_run_entry(repro_rows)
    scope_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in scope_rows:
        key = (str(row.get("experiment_name") or ""), str(row.get("run_entry") or ""))
        scope_rows_by_key[key].append(row)

    sankey_rows = []
    for row in filter_inventory_rows:
        if row.get("selection_status") != "selected":
            continue
        run_entry = str(row.get("run_spec_name") or "")
        scope_rows_for_entry = scope_rows_by_run_entry.get(run_entry, [])
        execution_stage = _classify_execution_stage(scope_rows_for_entry)
        if execution_stage == "not_run_in_scope":
            continue
        repro_rows_for_entry = repro_rows_by_run_entry.get(run_entry, [])
        flow: dict[str, str] = {"execution_stage": execution_stage}
        if execution_stage != "completed_with_run_artifacts":
            sankey_rows.append(flow)
            continue
        repro_row = _choose_repro_row_for_run_entry(repro_rows_for_entry, scope_rows_by_key)
        if repro_row is None:
            flow["analysis_stage"] = "completed_not_yet_analyzed"
            sankey_rows.append(flow)
            continue
        flow["analysis_stage"] = "analyzed"
        flow["reproduction_stage"] = _bucket_agreement(repro_row.get(tol_key))
        sankey_rows.append(flow)
    return sankey_rows


def _build_universe_to_scope_root() -> tuple[sankey_builder.Root, list[str], dict[str, list[str]]]:
    """Stage A: Universe -> Scope.

    Chains the per-source eligibility gates to the selection waist.
    Terminal nodes are ``selected`` (= in scope) and the various
    ``excluded: <reason>`` outcomes. Stage B picks up from ``selected``.
    """
    root = sankey_builder.Root(label="Universe (all discovered runs)")
    structural = root.group(by="structural_gate", name="Structural Gate")
    structural["excluded: structurally incomplete"].connect(None)
    metadata = structural["kept: structurally complete"].group(by="metadata_gate", name="Metadata Gate")
    metadata["excluded: missing model metadata"].connect(None)
    open_weight = metadata["kept: model metadata resolved"].group(by="open_weight_gate", name="Open-Weight Gate")
    open_weight["excluded: not open weight"].connect(None)
    tag = open_weight["kept: open weight"].group(by="tag_gate", name="Tag Gate")
    tag["excluded: unsuitable text/modality tags"].connect(None)
    deployment = tag["kept: suitable text tags"].group(by="deployment_gate", name="Deployment Gate")
    deployment["excluded: no runnable local deployment"].connect(None)
    size = deployment["kept: runnable local deployment"].group(by="size_gate", name="Size Gate")
    size["excluded: exceeds size budget"].connect(None)
    selection = size["kept: within size budget"].connect(
        sankey_builder.Group(name="Selection", by="selection_gate")
    )
    assert isinstance(selection, sankey_builder.Group)
    # Terminal: selected = in scope; excluded = filtered out at selection time.
    # Stage B (sankey_b_scope_to_analyzed) picks up from the selected branch.
    selection[FILTER_SELECTION_EXCLUDED_LABEL].connect(None)
    selection[FILTER_SELECTION_SELECTED_LABEL].connect(None)

    stage_names = [
        "Structural Gate",
        "Metadata Gate",
        "Open-Weight Gate",
        "Tag Gate",
        "Deployment Gate",
        "Size Gate",
        "Selection",
    ]
    stage_defs = {
        "Structural Gate": [
            "excluded: structurally incomplete",
            "kept: structurally complete",
        ],
        "Metadata Gate": [
            "excluded: missing model metadata",
            "kept: model metadata resolved",
        ],
        "Open-Weight Gate": [
            "excluded: not open weight",
            "kept: open weight",
        ],
        "Tag Gate": [
            "excluded: unsuitable text/modality tags",
            "kept: suitable text tags",
        ],
        "Deployment Gate": [
            "excluded: no runnable local deployment",
            "kept: runnable local deployment",
        ],
        "Size Gate": [
            "excluded: exceeds size budget",
            "kept: within size budget",
        ],
        "Selection": [
            FILTER_SELECTION_SELECTED_LABEL,
            FILTER_SELECTION_EXCLUDED_LABEL,
        ],
    }
    return root, stage_names, stage_defs


def _build_filter_to_attempt_root() -> tuple[sankey_builder.Root, list[str], dict[str, list[str]]]:
    # Backwards-compatible alias for callers that still import the old name.
    # The new ``_build_universe_to_scope_root`` is the canonical Stage-A.
    return _build_universe_to_scope_root()


def _build_scope_to_analyzed_root() -> tuple[sankey_builder.Root, list[str], dict[str, list[str]]]:
    """Stage B: Scope -> Attempt -> Execution -> Analysis -> Reproduction.

    Picks up from Stage A's ``selected`` branch. The first stage
    (``Attempt``) splits ``in scope`` into ``attempted`` vs
    ``selected but not attempted`` so the funnel surfaces the gap
    between "we wanted to run this" and "we actually ran this".
    """
    root = sankey_builder.Root(label="Scope (in-scope after Stage-A filtering)")
    attempt = root.group(by="attempt_stage", name="Attempt")
    attempt[NOT_ATTEMPTED_LABEL].connect(None)
    execution = attempt[ATTEMPTED_LABEL].group(by="execution_stage", name="Execution")
    execution["attempted_not_finished"].connect(None)
    execution["attempted_failed_or_incomplete"].connect(None)
    analysis = execution["completed_with_run_artifacts"].group(by="analysis_stage", name="Analysis")
    analysis["completed_not_yet_analyzed"].connect(None)
    analysis["analyzed"].group(by="reproduction_stage", name="Reproduction")
    stage_names = ["Attempt", "Execution", "Analysis", "Reproduction"]
    stage_defs = {
        "Attempt": [
            ATTEMPTED_LABEL,
            NOT_ATTEMPTED_LABEL,
        ],
        "Execution": [
            "attempted_not_finished",
            "attempted_failed_or_incomplete",
            "completed_with_run_artifacts",
        ],
        "Analysis": [
            "completed_not_yet_analyzed",
            "analyzed",
        ],
        "Reproduction": [
            "exact_or_near_exact",
            "high_agreement_0.95+",
            "moderate_agreement_0.80+",
            "low_agreement_0.00+",
            "zero_agreement",
        ],
    }
    return root, stage_names, stage_defs


def _build_attempted_to_repro_root() -> tuple[sankey_builder.Root, list[str], dict[str, list[str]]]:
    # Deprecated alias for callers that still import the old name. The
    # canonical Stage B builder is ``_build_scope_to_analyzed_root``.
    return _build_scope_to_analyzed_root()


def _build_scope_to_analyzed_rows(
    filter_inventory_rows: list[dict[str, Any]],
    scope_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
    *,
    tol_key: str,
) -> list[dict[str, str]]:
    """Stage B rows: in-scope (selected) -> attempt -> execution -> analysis -> reproduction.

    Source population is filter_inventory rows with selection_status=='selected'
    (i.e. the rows that *are* in scope after Stage A). Each row is then
    annotated with whether we attempted, completed, analyzed, and at what
    agreement level it landed.
    """
    scope_rows_by_run_entry = _group_scope_rows_by_run_entry(scope_rows)
    repro_rows_by_run_entry = _group_repro_rows_by_run_entry(repro_rows)
    scope_rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in scope_rows:
        key = (str(row.get("experiment_name") or ""), str(row.get("run_entry") or ""))
        scope_rows_by_key[key].append(row)

    sankey_rows: list[dict[str, str]] = []
    for row in filter_inventory_rows:
        if row.get("selection_status") != "selected":
            continue
        run_entry = str(row.get("run_spec_name") or "")
        scope_rows_for_entry = scope_rows_by_run_entry.get(run_entry, [])
        repro_rows_for_entry = repro_rows_by_run_entry.get(run_entry, [])
        flow: dict[str, str] = {}
        if not scope_rows_for_entry:
            flow["attempt_stage"] = NOT_ATTEMPTED_LABEL
            sankey_rows.append(flow)
            continue
        flow["attempt_stage"] = ATTEMPTED_LABEL
        execution = _classify_execution_stage(scope_rows_for_entry)
        flow["execution_stage"] = execution
        if execution != "completed_with_run_artifacts":
            sankey_rows.append(flow)
            continue
        repro_row = _choose_repro_row_for_run_entry(repro_rows_for_entry, scope_rows_by_key)
        if repro_row is None:
            flow["analysis_stage"] = "completed_not_yet_analyzed"
            sankey_rows.append(flow)
            continue
        flow["analysis_stage"] = "analyzed"
        flow["reproduction_stage"] = _bucket_agreement(repro_row.get(tol_key))
        sankey_rows.append(flow)
    return sankey_rows


def _build_universe_to_scope_rows(
    filter_inventory_rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Stage A rows: pure filter-gate flow ending at the Selection waist.

    Same gate logic as the legacy ``_build_filter_to_attempt_rows`` but
    the row dicts intentionally do *not* carry post-selection keys; the
    Stage A sankey terminates at Selection.
    """
    rows: list[dict[str, str]] = []
    for row in filter_inventory_rows:
        reasons = {str(r) for r in (row.get("failure_reasons") or []) if str(r)}
        flow: dict[str, str] = {}
        if row.get("is_structurally_incomplete"):
            flow["structural_gate"] = "excluded: structurally incomplete"
            rows.append(flow)
            continue
        flow["structural_gate"] = "kept: structurally complete"
        if "missing-model-metadata" in reasons:
            flow["metadata_gate"] = "excluded: missing model metadata"
            rows.append(flow)
            continue
        flow["metadata_gate"] = "kept: model metadata resolved"
        if "not-open-access" in reasons:
            flow["open_weight_gate"] = "excluded: not open weight"
            rows.append(flow)
            continue
        flow["open_weight_gate"] = "kept: open weight"
        if ("excluded-tags" in reasons) or ("not-text-like" in reasons):
            flow["tag_gate"] = "excluded: unsuitable text/modality tags"
            rows.append(flow)
            continue
        flow["tag_gate"] = "kept: suitable text tags"
        if "no-local-helm-deployment" in reasons:
            flow["deployment_gate"] = "excluded: no runnable local deployment"
            rows.append(flow)
            continue
        flow["deployment_gate"] = "kept: runnable local deployment"
        if "too-large" in reasons:
            flow["size_gate"] = "excluded: exceeds size budget"
            rows.append(flow)
            continue
        flow["size_gate"] = "kept: within size budget"
        if row.get("selection_status") != "selected":
            flow["selection_gate"] = FILTER_SELECTION_EXCLUDED_LABEL
        else:
            flow["selection_gate"] = FILTER_SELECTION_SELECTED_LABEL
        rows.append(flow)
    return rows


def _build_end_to_end_funnel_root() -> tuple[sankey_builder.Root, list[str], dict[str, list[str]]]:
    root = sankey_builder.Root(label="All discovered historic HELM runs")
    structural = root.group(by="structural_gate", name="Structural Gate")
    structural["excluded: structurally incomplete"].connect(None)
    metadata = structural["kept: structurally complete"].group(by="metadata_gate", name="Metadata Gate")
    metadata["excluded: missing model metadata"].connect(None)
    open_weight = metadata["kept: model metadata resolved"].group(by="open_weight_gate", name="Open-Weight Gate")
    open_weight["excluded: not open weight"].connect(None)
    tag = open_weight["kept: open weight"].group(by="tag_gate", name="Tag Gate")
    tag["excluded: unsuitable text/modality tags"].connect(None)
    deployment = tag["kept: suitable text tags"].group(by="deployment_gate", name="Deployment Gate")
    deployment["excluded: no runnable local deployment"].connect(None)
    size = deployment["kept: runnable local deployment"].group(by="size_gate", name="Size Gate")
    size["excluded: exceeds size budget"].connect(None)
    selection = size["kept: within size budget"].connect(
        sankey_builder.Group(name="Selection", by="selection_gate")
    )
    assert isinstance(selection, sankey_builder.Group)
    selection[FILTER_SELECTION_EXCLUDED_LABEL].connect(None)
    execution = selection[FILTER_SELECTION_SELECTED_LABEL].group(by="execution_stage", name="Execution")
    execution["not_run_in_scope"].connect(None)
    execution["attempted_not_finished"].connect(None)
    execution["attempted_failed_or_incomplete"].connect(None)
    analysis = execution["completed_with_run_artifacts"].group(by="analysis_stage", name="Analysis")
    analysis["completed_not_yet_analyzed"].connect(None)
    analysis["analyzed"].group(by="reproduction_stage", name="Reproduction")

    stage_names = [
        "Structural Gate",
        "Metadata Gate",
        "Open-Weight Gate",
        "Tag Gate",
        "Deployment Gate",
        "Size Gate",
        "Selection",
        "Execution",
        "Analysis",
        "Reproduction",
    ]
    stage_defs = {
        "Structural Gate": [
            "excluded: structurally incomplete",
            "kept: structurally complete",
        ],
        "Metadata Gate": [
            "excluded: missing model metadata",
            "kept: model metadata resolved",
        ],
        "Open-Weight Gate": [
            "excluded: not open weight",
            "kept: open weight",
        ],
        "Tag Gate": [
            "excluded: unsuitable text/modality tags",
            "kept: suitable text tags",
        ],
        "Deployment Gate": [
            "excluded: no runnable local deployment",
            "kept: runnable local deployment",
        ],
        "Size Gate": [
            "excluded: exceeds size budget",
            "kept: within size budget",
        ],
        "Selection": [
            FILTER_SELECTION_SELECTED_LABEL,
            FILTER_SELECTION_EXCLUDED_LABEL,
        ],
        "Execution": [
            "not_run_in_scope",
            "attempted_not_finished",
            "attempted_failed_or_incomplete",
            "completed_with_run_artifacts",
        ],
        "Analysis": [
            "completed_not_yet_analyzed",
            "analyzed",
        ],
        "Reproduction": [
            "exact_or_near_exact",
            "high_agreement_0.95+",
            "moderate_agreement_0.80+",
            "low_agreement_0.00+",
            "zero_agreement",
        ],
    }
    return root, stage_names, stage_defs


def _read_log_tail(job_dpath: Path, max_chars: int = 40000) -> str:
    log_fpath = job_dpath / "helm-run.log"
    if not log_fpath.exists():
        return ""
    try:
        text = log_fpath.read_text(errors="ignore")
    except Exception:
        return ""
    return text[-max_chars:]


def _classify_failure(job_dpath: Path, row: dict[str, Any]) -> dict[str, Any]:
    log_tail = _read_log_tail(job_dpath)
    text = _normalize_text(log_tail)
    status = _normalize_text(row.get("status"))

    checks: list[tuple[str, str, list[str]]] = [
        (
            "missing_openai_annotation_credentials",
            "run depends on OpenAI-backed annotation but no API key was configured",
            ["openai_api_key", "annotationexecutorerror", "api_key client option must be set"],
        ),
        (
            "missing_math_dataset",
            "required math dataset was not available in the environment",
            ["hendrycks/competition_math", "couldn't find 'hendrycks/competition_math'"],
        ),
        (
            "missing_dataset_or_cached_artifact",
            "required dataset or cached artifact was not available",
            ["filenotfounderror", "couldn't find", "no such file or directory"],
        ),
        (
            "gated_dataset_access",
            "dataset exists but requires gated access credentials or approval",
            ["gated dataset on the hub", "ask for access", "datasetnotfounderror: dataset"],
        ),
        (
            "remote_dataset_download_failure",
            "dataset download failed from a remote source",
            ["failed with exit code 8: wget", "wget https://", "curl: ", "temporary failure in name resolution"],
        ),
        (
            "gpu_memory_or_cuda_failure",
            "job hit a CUDA or GPU-memory related failure",
            ["cuda out of memory", "outofmemoryerror", "cublas", "cuda error"],
        ),
        (
            "process_killed_or_resource_exhausted",
            "process looks to have been killed by the host or scheduler",
            ["killed", "exit code 137", "sigkill"],
        ),
        (
            "network_or_remote_service_failure",
            "remote service or network interaction failed",
            ["connectionerror", "readtimeout", "maxretryerror", "429", "503 service unavailable"],
        ),
        (
            "filesystem_permission_failure",
            "filesystem permissions blocked the run",
            ["permission denied"],
        ),
        (
            "interrupted_run",
            "run was interrupted before completion",
            ["keyboardinterrupt", "cancellederror", "interrupted"],
        ),
    ]

    for label, summary, patterns in checks:
        matched = [pat for pat in patterns if pat in text]
        if matched:
            return {
                "failure_reason": label,
                "failure_summary": summary,
                "failure_confidence": "heuristic_pattern_match",
                "matched_patterns": matched,
                "log_excerpt": log_tail[-2000:] if log_tail else None,
            }

    if status in {"running", "queued"}:
        return {
            "failure_reason": "not_finished_yet",
            "failure_summary": "job appears to be queued or still running",
            "failure_confidence": "status_only",
            "matched_patterns": [],
            "log_excerpt": log_tail[-2000:] if log_tail else None,
        }

    if not log_tail:
        return {
            "failure_reason": "missing_runtime_log",
            "failure_summary": "no runtime log was found for this job",
            "failure_confidence": "missing_evidence",
            "matched_patterns": [],
            "log_excerpt": None,
        }

    if "traceback" not in text and status in {"", "unknown", "computed", "reused"}:
        return {
            "failure_reason": "truncated_or_incomplete_runtime",
            "failure_summary": "job lacks complete run artifacts and the runtime log ends without a clear terminal exception",
            "failure_confidence": "weak_inference",
            "matched_patterns": [],
            "log_excerpt": log_tail[-2000:] if log_tail else None,
        }

    return {
        "failure_reason": "unknown_failure",
        "failure_summary": "no current rule explains this failure; manual drill-down recommended",
        "failure_confidence": "unknown",
        "matched_patterns": [],
        "log_excerpt": log_tail[-2000:] if log_tail else None,
    }


def _raise_fd_limit(target: int = 8192) -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = min(max(soft, target), hard)
        if desired > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    except Exception:
        pass


def _fd_count() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return None


def _load_all_repro_rows(extra_analysis_roots: list[Path] | None = None) -> list[dict[str, Any]]:
    # Scan the canonical store location plus the publication-side and
    # legacy-repo symlink trees so experiments that haven't been re-run
    # since either layout migration are still found.
    #
    # ``extra_analysis_roots`` lets callers point the scan at additional
    # locations that hold the same ``<X>/<something>/core-reports/<packet>/...``
    # shape — virtual experiments, in particular, hold their per-packet
    # reports under their own ``output.root`` and would otherwise be
    # invisible to the aggregate summary.
    canonical_root = experiments_analysis_root()
    publication_root_link_dir = publication_experiments_root()
    legacy_repo_root = legacy_repo_publication_root()
    extra_roots = [Path(p).expanduser().resolve() for p in (extra_analysis_roots or [])]
    report_jsons = sorted(
        list(canonical_root.glob("*/core-reports/*/core_metric_report.latest.json"))
        + list(publication_root_link_dir.glob("experiment-analysis-*/core-reports/*/core_metric_report.latest.json"))
        + list(legacy_repo_root.glob("experiment-analysis-*/core-reports/*/core_metric_report.latest.json"))
        + [
            p
            for root in extra_roots
            for p in root.glob("*/core-reports/*/core_metric_report.latest.json")
        ]
    )
    deduped: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for report_json in report_jsons:
        try:
            bundle = load_core_report_bundle(report_json)
        except Exception:
            continue
        report = bundle["report"]
        packet = bundle["packet"]
        experiment_name = packet["components_manifest"].get("experiment_name")
        run_entry = packet["components_manifest"].get("run_entry")
        if not experiment_name or not run_entry:
            continue
        local_components = [
            component
            for component in packet.get("components", [])
            if component.get("source_kind") == "local"
        ]
        selected_run_dirs = sorted({
            str(component.get("run_path"))
            for component in local_components
            if component.get("run_path")
        })
        official = find_report_pair(report, "official_vs_local") or {}
        repeat = find_report_pair(report, "local_repeat") or {}
        official_diag = official.get("diagnosis", {}) or {}
        repeat_diag = repeat.get("diagnosis", {}) or {}
        official_instance_level = official.get("instance_level") or {}
        official_agree_curve = official_instance_level.get("agreement_vs_abs_tol") or []
        agree_0 = _find_curve_value(official_agree_curve, 0.0)
        agree_005 = _find_curve_value(official_agree_curve, CANONICAL_AGREEMENT_TOL)
        local_reference = packet_local_reference_component(packet)
        official_component = packet_component_by_source_kind(packet, "official_vs_local", "official")
        # Stage-5: surface the artifact_format provenance on every aggregate
        # row so the breakdowns can show whether a given comparison ran
        # against canonical EEE artifacts or in-memory HELM->EEE conversion.
        artifact_formats = sorted({
            (component.get("artifact_format") or "helm")
            for component in packet.get("components", [])
            if component.get("artifact_format") is not None
            or component.get("run_path")
        })
        row = {
            "experiment_name": experiment_name,
            "run_entry": run_entry,
            "packet_id": packet["components_manifest"].get("packet_id"),
            "selected_public_track": packet["components_manifest"].get("selected_public_track"),
            "run_spec_name": report.get("run_spec_name"),
            "report_dir": str(report_json.parent),
            "report_json": str(report_json),
            "components_manifest": str(packet["components_manifest_path"]),
            "comparisons_manifest": str(packet["comparisons_manifest_path"]),
            "warnings_manifest": str(packet["warnings_manifest_path"]),
            "analysis_local_reference_run": _clean_optional_text(local_reference.get("run_path")),
            "analysis_official_run": _clean_optional_text(official_component.get("run_path")),
            "analysis_selected_run_dirs": selected_run_dirs,
            "analysis_selected_attempt_refs": [component.get("selection_ref") for component in local_components if component.get("selection_ref")],
            "analysis_selected_attempt_identities": [component.get("attempt_identity") for component in local_components if component.get("attempt_identity")],
            "analysis_single_run": not bool(repeat),
            "repeat_diagnosis": repeat_diag.get("label"),
            "repeat_primary_reasons": repeat_diag.get("primary_reason_names") or [],
            "official_diagnosis": official_diag.get("label"),
            "official_primary_reasons": official_diag.get("primary_reason_names") or [],
            "official_instance_agree_0": agree_0,
            "official_instance_agree_005": agree_005,
            "official_instance_agree_bucket": _bucket_agreement(agree_005),
            "official_instance_agree_01": _find_curve_value(official_agree_curve, 0.1),
            "official_runlevel_abs_max": nested_get(official, "run_level", "overall_quantiles", "abs_delta", "max"),
            "official_runlevel_abs_p90": nested_get(official, "run_level", "overall_quantiles", "abs_delta", "p90"),
            "official_instance_agree_001": _find_curve_value(official_agree_curve, 0.001),
            "core_metrics": official.get("core_metrics") or [],
            "artifact_formats": artifact_formats,
            "artifact_format": ",".join(artifact_formats) if artifact_formats else "helm",
            "official_runlevel_metric_max_deltas": {
                m["metric"]: nested_get(m, "abs_delta", "max")
                for m in (nested_get(official, "run_level", "by_metric") or [])
            },
            "official_instance_agree_curve": [
                {"abs_tol": pt["abs_tol"], "agree_ratio": pt["agree_ratio"]}
                for pt in official_agree_curve
            ],
            "official_per_metric_agreement": nested_get(official, "instance_level", "per_metric_agreement") or {},
            "packet_warnings": (packet.get("warnings_manifest") or {}).get("packet_warnings", []),
            "packet_caveats": (packet.get("warnings_manifest") or {}).get("packet_caveats", []),
            "comparison_warning_count": sum(
                len(comparison.get("warnings") or [])
                + (1 if comparison.get("disabled_reason") else 0)
                for comparison in ((packet.get("warnings_manifest") or {}).get("comparisons") or [])
            ),
            "report_warning_count": len((packet.get("warnings_manifest") or {}).get("packet_warnings") or []),
            "has_report_warnings": bool(
                (packet.get("warnings_manifest") or {}).get("packet_warnings")
                or report.get("diagnostic_flags")
                or any(
                    comparison.get("warnings") or comparison.get("disabled_reason")
                    for comparison in ((packet.get("warnings_manifest") or {}).get("comparisons") or [])
                )
            ),
        }
        deduped[(experiment_name, run_entry, row["packet_id"] or row["report_dir"])] = row
    return list(deduped.values())


def _write_table_artifacts(
    rows: list[dict[str, Any]],
    stem: Path,
    machine_dpath: Path | None = None,
    static_dpath: Path | None = None,
) -> dict[str, str]:
    if machine_dpath is not None:
        machine_dpath.mkdir(parents=True, exist_ok=True)
        json_fpath = (machine_dpath / stem.name).with_suffix(".json")
    else:
        json_fpath = stem.with_suffix(".json")
    if static_dpath is not None:
        static_dpath.mkdir(parents=True, exist_ok=True)
        csv_fpath = (static_dpath / stem.name).with_suffix(".csv")
        txt_fpath = (static_dpath / stem.name).with_suffix(".txt")
    else:
        csv_fpath = stem.with_suffix(".csv")
        txt_fpath = stem.with_suffix(".txt")
    _write_json(rows, json_fpath)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with csv_fpath.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    if not rows:
        txt_fpath.write_text("(no rows)\n")
    else:
        lines = [", ".join(fieldnames)]
        for row in rows[:200]:
            lines.append(", ".join(str(row.get(key, "")) for key in fieldnames))
        if len(rows) > 200:
            lines.append(f"... ({len(rows) - 200} more rows)")
        txt_fpath.write_text("\n".join(lines) + "\n")
    return {"json": str(json_fpath), "csv": str(csv_fpath), "txt": str(txt_fpath)}


def _write_structured_summary_artifacts(
    *,
    rows: list[dict[str, Any]],
    payload: dict[str, Any],
    txt_lines: list[str],
    stem: Path,
    machine_dpath: Path,
    static_dpath: Path,
) -> dict[str, str]:
    machine_dpath.mkdir(parents=True, exist_ok=True)
    static_dpath.mkdir(parents=True, exist_ok=True)
    json_fpath = (machine_dpath / stem.name).with_suffix(".json")
    csv_fpath = (static_dpath / stem.name).with_suffix(".csv")
    txt_fpath = (static_dpath / stem.name).with_suffix(".txt")
    _write_json(payload, json_fpath)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with csv_fpath.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        else:
            file.write("")
    _write_text(txt_lines, txt_fpath)
    return {"json": str(json_fpath), "csv": str(csv_fpath), "txt": str(txt_fpath)}


def _build_off_story_summary(
    *,
    filter_inventory_rows: list[dict[str, Any]],
    scope_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    registry_lookup = local_model_registry_by_name()
    run_entry_meta = _run_entry_metadata_lookup(filter_inventory_rows, scope_rows)
    model_filter_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    model_scope_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    model_analyzed_run_entries: dict[str, set[str]] = defaultdict(set)
    model_repro_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in filter_inventory_rows:
        model = _clean_optional_text(row.get("model"))
        if model:
            model_filter_rows[model].append(row)
    for row in scope_rows:
        model = _clean_optional_text(row.get("model"))
        if model:
            model_scope_rows[model].append(row)
    for row in repro_rows:
        run_entry = _clean_optional_text(row.get("run_entry"))
        if not run_entry:
            continue
        model = _clean_optional_text((run_entry_meta.get(run_entry) or {}).get("model"))
        if not model:
            continue
        model_analyzed_run_entries[model].add(run_entry)
        model_repro_rows[model].append(row)

    candidate_models = sorted(set(model_filter_rows) | set(model_scope_rows) | set(model_analyzed_run_entries))
    headline_sets: dict[str, dict[str, set[str]]] = {
        "off_story": {
            "models": set(),
            "selected_run_entries": set(),
            "attempted_run_entries": set(),
            "completed_run_entries": set(),
            "analyzed_run_entries": set(),
        },
        "on_story": {
            "models": set(),
            "selected_run_entries": set(),
            "attempted_run_entries": set(),
            "completed_run_entries": set(),
            "analyzed_run_entries": set(),
        },
    }

    off_story_rows: list[dict[str, Any]] = []
    for model in candidate_models:
        filter_rows = model_filter_rows.get(model, [])
        scope_model_rows = model_scope_rows.get(model, [])
        filter_row = filter_rows[0] if filter_rows else None
        story = _storyline_metadata_for_model(model=model, registry_lookup=registry_lookup, filter_row=filter_row)
        status = story["storyline_status"]
        if status not in headline_sets:
            continue

        selected_run_entries = {
            str(row.get("run_spec_name"))
            for row in filter_rows
            if row.get("selection_status") == "selected" and row.get("run_spec_name")
        }
        attempted_run_entries = {
            str(row.get("run_entry"))
            for row in scope_model_rows
            if row.get("run_entry")
        }
        completed_run_entries = {
            str(row.get("run_entry"))
            for row in scope_model_rows
            if row.get("run_entry") and _is_truthy_text(row.get("has_run_spec"))
        }
        analyzed_run_entries = set(model_analyzed_run_entries.get(model, set()))
        context = headline_sets[status]
        context["models"].add(model)
        context["selected_run_entries"].update(selected_run_entries)
        context["attempted_run_entries"].update(attempted_run_entries)
        context["completed_run_entries"].update(completed_run_entries)
        context["analyzed_run_entries"].update(analyzed_run_entries)

        if status != "off_story":
            continue

        off_story_rows.append(
            {
                "model": model,
                "storyline_status": status,
                "why_off_story": story["storyline_reason"],
                "expected_local_served": story["expected_local_served"],
                "replaces_helm_deployment": story["replaces_helm_deployment"],
                "local_registry_source": story["local_registry_source"],
                "registry_notes": story["registry_notes"],
                "n_selected_run_entries": len(selected_run_entries),
                "n_attempted_run_entries": len(attempted_run_entries),
                "n_completed_run_entries": len(completed_run_entries),
                "n_analyzed_run_entries": len(analyzed_run_entries),
                "n_attempt_rows": len(scope_model_rows),
                "n_completed_rows": sum(1 for row in scope_model_rows if _is_truthy_text(row.get("has_run_spec"))),
                "n_analysis_reports": len(model_repro_rows.get(model, [])),
                "selected_run_entries": _preview_values(sorted(selected_run_entries)),
                "attempted_run_entries": _preview_values(sorted(attempted_run_entries)),
                "analyzed_run_entries": _preview_values(sorted(analyzed_run_entries)),
                "attempted_experiment_names": _preview_values([
                    str(row.get("experiment_name")) for row in scope_model_rows if row.get("experiment_name")
                ]),
            }
        )

    off_story_rows.sort(
        key=lambda row: (
            -int(row.get("n_selected_run_entries") or 0),
            -int(row.get("n_attempted_run_entries") or 0),
            str(row.get("model") or ""),
        )
    )
    headline_counts = {
        status: {
            "n_models": len(values["models"]),
            "selected_run_entries": len(values["selected_run_entries"]),
            "attempted_run_entries": len(values["attempted_run_entries"]),
            "completed_run_entries": len(values["completed_run_entries"]),
            "analyzed_run_entries": len(values["analyzed_run_entries"]),
        }
        for status, values in headline_sets.items()
    }
    return {
        "definitions": {
            "off_story": "expected_local_served=True and replaces_helm_deployment is null in eval_audit/model_registry.py",
            "on_story": "expected_local_served=True and replaces_helm_deployment points at a public HELM deployment",
            "count_semantics": "selected counts are Stage 1 selected run_entry values; attempted/completed/analyzed counts are unique run_entry values observed in the current summary scope",
        },
        "headline_counts": headline_counts,
        "rows": off_story_rows,
    }


def _format_off_story_summary_text(
    *,
    scope_title: str,
    generated_utc: str,
    summary: dict[str, Any],
) -> list[str]:
    off_story_counts = summary["headline_counts"].get("off_story", {})
    on_story_counts = summary["headline_counts"].get("on_story", {})
    lines = [
        "Off-Story Local Serving Summary",
        "================================",
        f"Generated: {generated_utc}",
        f"Scope: {scope_title}",
        "",
        "Definitions:",
        f"  off_story: {summary['definitions']['off_story']}",
        f"  on_story:  {summary['definitions']['on_story']}",
        f"  counts:    {summary['definitions']['count_semantics']}",
        "",
        "Headline counts:",
        f"  off_story_models: {off_story_counts.get('n_models', 0)}",
        f"  off_story_selected_run_entries: {off_story_counts.get('selected_run_entries', 0)}",
        f"  off_story_attempted_run_entries: {off_story_counts.get('attempted_run_entries', 0)}",
        f"  off_story_completed_run_entries: {off_story_counts.get('completed_run_entries', 0)}",
        f"  off_story_analyzed_run_entries: {off_story_counts.get('analyzed_run_entries', 0)}",
        "",
        "On-story context:",
        f"  on_story_models: {on_story_counts.get('n_models', 0)}",
        f"  on_story_selected_run_entries: {on_story_counts.get('selected_run_entries', 0)}",
        f"  on_story_attempted_run_entries: {on_story_counts.get('attempted_run_entries', 0)}",
        f"  on_story_completed_run_entries: {on_story_counts.get('completed_run_entries', 0)}",
        f"  on_story_analyzed_run_entries: {on_story_counts.get('analyzed_run_entries', 0)}",
        "",
        "Per off-story model:",
    ]
    rows = summary.get("rows") or []
    if not rows:
        lines.append("  (no off-story models found in this scope)")
        return lines
    header = (
        f"{'model':<32} {'sel':>4} {'att':>4} {'cmp':>4} {'ana':>4} "
        f"{'source':<28} why_off_story"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in rows:
        lines.append(
            f"{str(row.get('model') or ''):<32} "
            f"{int(row.get('n_selected_run_entries') or 0):>4} "
            f"{int(row.get('n_attempted_run_entries') or 0):>4} "
            f"{int(row.get('n_completed_run_entries') or 0):>4} "
            f"{int(row.get('n_analyzed_run_entries') or 0):>4} "
            f"{str(row.get('local_registry_source') or ''):<28} "
            f"{str(row.get('why_off_story') or '')}"
        )
    return lines


def _build_analyzed_attempt_matchers(
    repro_rows: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, set[str]]], set[tuple[str, str]]]:
    explicit_matchers: dict[tuple[str, str], dict[str, set[str]]] = {}
    analyzed_groups: set[tuple[str, str]] = set()
    for row in repro_rows:
        experiment_name = _clean_optional_text(row.get("experiment_name"))
        run_entry = _clean_optional_text(row.get("run_entry"))
        if not experiment_name or not run_entry:
            continue
        group_key = (experiment_name, run_entry)
        analyzed_groups.add(group_key)
        matcher = explicit_matchers.setdefault(
            group_key,
            {
                "run_dirs": set(),
                "attempt_identities": set(),
                "attempt_uuids": set(),
                "attempt_fallback_keys": set(),
            },
        )
        selected_run_dirs = row.get("analysis_selected_run_dirs") or []
        if isinstance(selected_run_dirs, str):
            try:
                selected_run_dirs = json.loads(selected_run_dirs)
            except Exception:
                selected_run_dirs = [selected_run_dirs]
        for run_dir in selected_run_dirs:
            run_dir_text = _clean_optional_text(run_dir)
            if run_dir_text:
                matcher["run_dirs"].add(run_dir_text)
        selected_attempt_refs = row.get("analysis_selected_attempt_refs") or []
        if isinstance(selected_attempt_refs, str):
            try:
                selected_attempt_refs = json.loads(selected_attempt_refs)
            except Exception:
                selected_attempt_refs = []
        for ref in selected_attempt_refs:
            if not isinstance(ref, dict):
                continue
            for src_key, dst_key in [
                ("run_dir", "run_dirs"),
                ("attempt_identity", "attempt_identities"),
                ("attempt_uuid", "attempt_uuids"),
                ("attempt_fallback_key", "attempt_fallback_keys"),
            ]:
                value = _clean_optional_text(ref.get(src_key))
                if value:
                    matcher[dst_key].add(value)
        selected_attempt_identities = row.get("analysis_selected_attempt_identities") or []
        if isinstance(selected_attempt_identities, str):
            try:
                selected_attempt_identities = json.loads(selected_attempt_identities)
            except Exception:
                selected_attempt_identities = [selected_attempt_identities]
        for identity in selected_attempt_identities:
            identity_text = _clean_optional_text(identity)
            if identity_text:
                matcher["attempt_identities"].add(identity_text)
    return explicit_matchers, analyzed_groups


def _analyzed_match_status(
    row: dict[str, Any],
    *,
    explicit_matchers: dict[tuple[str, str], dict[str, set[str]]],
    analyzed_groups: set[tuple[str, str]],
    completed_rows_by_group: dict[tuple[str, str], list[dict[str, Any]]],
) -> str:
    if not _is_truthy_text(row.get("has_run_spec")):
        return "not_completed"
    experiment_name = _clean_optional_text(row.get("experiment_name"))
    run_entry = _clean_optional_text(row.get("run_entry"))
    if not experiment_name or not run_entry:
        return "missing_group_key"
    group_key = (experiment_name, run_entry)
    if group_key not in analyzed_groups:
        return "not_in_analyzed_group"

    matcher = explicit_matchers.get(group_key)
    run_dir = _clean_optional_text(row.get("run_dir"))
    attempt_identity = _clean_optional_text(row.get("attempt_identity"))
    attempt_uuid = _clean_optional_text(row.get("attempt_uuid"))
    attempt_fallback_key = _clean_optional_text(row.get("attempt_fallback_key"))
    if matcher and any(matcher.values()):
        if run_dir and run_dir in matcher["run_dirs"]:
            return "explicit_run_dir"
        if attempt_identity and attempt_identity in matcher["attempt_identities"]:
            return "explicit_attempt_identity"
        if attempt_uuid and attempt_uuid in matcher["attempt_uuids"]:
            return "explicit_attempt_uuid"
        if attempt_fallback_key and attempt_fallback_key in matcher["attempt_fallback_keys"]:
            return "explicit_attempt_fallback_key"
        return "ambiguous_explicit_group_unmatched"

    completed_group_rows = completed_rows_by_group.get(group_key, [])
    if len(completed_group_rows) == 1:
        return "singleton_completed_group_fallback"
    if len(completed_group_rows) > 1:
        return "ambiguous_legacy_group_multi_completed"
    return "analyzed_group_without_completed_rows"


def _build_run_multiplicity_summary(
    *,
    filter_inventory_rows: list[dict[str, Any]],
    scope_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    registry_lookup = local_model_registry_by_name()
    filter_lookup = _filter_inventory_lookup_by_run_entry(filter_inventory_rows)
    run_entry_meta = _run_entry_metadata_lookup(filter_inventory_rows, scope_rows)
    explicit_matchers, analyzed_groups = _build_analyzed_attempt_matchers(repro_rows)
    repro_by_run_entry = _group_repro_rows_by_run_entry(repro_rows)

    grouped_rows = _group_scope_rows_by_run_entry(scope_rows)
    summary_rows: list[dict[str, Any]] = []
    for run_entry, rows in grouped_rows.items():
        resolved_rows = []
        for row in rows:
            resolved = dict(row)
            resolved.update(_resolve_attempt_identity(row))
            resolved_rows.append(resolved)
        resolved_rows.sort(
            key=lambda row: (
                _coerce_float(row.get("manifest_timestamp")),
                str(row.get("experiment_name") or ""),
                str(row.get("job_id") or ""),
            ),
            reverse=True,
        )
        completed_rows = [row for row in resolved_rows if _is_truthy_text(row.get("has_run_spec"))]
        completed_rows_by_group: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in completed_rows:
            group_key = (
                _clean_optional_text(row.get("experiment_name")) or "",
                _clean_optional_text(row.get("run_entry")) or "",
            )
            completed_rows_by_group[group_key].append(row)
        analyzed_statuses = [
            _analyzed_match_status(
                row,
                explicit_matchers=explicit_matchers,
                analyzed_groups=analyzed_groups,
                completed_rows_by_group=completed_rows_by_group,
            )
            for row in resolved_rows
        ]
        analyzed_rows = [
            row for row, status in zip(resolved_rows, analyzed_statuses)
            if status in {
                "explicit_run_dir",
                "explicit_attempt_identity",
                "explicit_attempt_uuid",
                "explicit_attempt_fallback_key",
                "singleton_completed_group_fallback",
            }
        ]
        analyzed_match_status_counts = Counter(analyzed_statuses)
        attempt_ids = [str(row.get("attempt_identity")) for row in resolved_rows if row.get("attempt_identity")]
        attempt_uuids = [str(row.get("attempt_uuid")) for row in resolved_rows if row.get("attempt_uuid")]
        fallback_attempt_ids = [
            str(row.get("attempt_fallback_key"))
            for row in resolved_rows
            if not row.get("attempt_uuid") and row.get("attempt_fallback_key")
        ]
        manifest_timestamps = [str(row.get("manifest_timestamp")) for row in resolved_rows if row.get("manifest_timestamp") not in {None, ""}]
        experiment_names = [str(row.get("experiment_name")) for row in resolved_rows if row.get("experiment_name")]
        machine_hosts = [str(row.get("machine_host")) for row in resolved_rows if row.get("machine_host")]
        process_context_sources = [str(row.get("process_context_source")) for row in resolved_rows if row.get("process_context_source")]
        attempt_uuid_sources = [str(row.get("attempt_uuid_source")) for row in resolved_rows if row.get("attempt_uuid_source")]
        meta = run_entry_meta.get(run_entry, {})
        model = _clean_optional_text(meta.get("model")) or _clean_optional_text(resolved_rows[0].get("model"))
        benchmark = _clean_optional_text(meta.get("benchmark")) or _clean_optional_text(resolved_rows[0].get("benchmark"))
        scenario = _clean_optional_text(meta.get("scenario")) or benchmark or _clean_optional_text(meta.get("suite"))
        story = _storyline_metadata_for_model(
            model=model,
            registry_lookup=registry_lookup,
            filter_row=filter_lookup.get(run_entry),
        )
        summary_rows.append(
            {
                "logical_run_key": run_entry,
                "run_entry": run_entry,
                "model": model,
                "benchmark": benchmark,
                "scenario": scenario,
                "storyline_status": story["storyline_status"],
                "local_registry_source": story["local_registry_source"],
                "replaces_helm_deployment": story["replaces_helm_deployment"],
                "n_rows": len(resolved_rows),
                "n_completed_rows": len(completed_rows),
                "n_analyzed_rows": len(analyzed_rows),
                "n_analysis_reports": len(repro_by_run_entry.get(run_entry, [])),
                "n_experiments": len({item for item in experiment_names if item}),
                "n_machines": len({item for item in machine_hosts if item}),
                "n_manifest_timestamps": len({item for item in manifest_timestamps if item}),
                "n_attempt_ids": len(set(attempt_ids)),
                "n_attempt_uuids": len(set(attempt_uuids)),
                "n_rows_with_attempt_uuid": sum(1 for row in resolved_rows if row.get("attempt_uuid")),
                "n_rows_without_attempt_uuid": sum(1 for row in resolved_rows if not row.get("attempt_uuid")),
                "n_ambiguous_analyzed_candidates": int(
                    analyzed_match_status_counts.get("ambiguous_explicit_group_unmatched", 0)
                    + analyzed_match_status_counts.get("ambiguous_legacy_group_multi_completed", 0)
                ),
                "machine_hosts": _preview_values(machine_hosts, max_items=8),
                "experiment_names": _preview_values(experiment_names, max_items=8),
                "attempt_ids": _preview_values(attempt_ids, max_items=8),
                "attempt_uuids": _preview_values(attempt_uuids, max_items=8),
                "fallback_attempt_ids": _preview_values(fallback_attempt_ids, max_items=6),
                "process_context_sources": _preview_values(process_context_sources, max_items=4),
                "attempt_uuid_sources": _preview_values(attempt_uuid_sources, max_items=4),
                "manifest_timestamps": _preview_values(manifest_timestamps, max_items=6),
                "latest_manifest_timestamp": resolved_rows[0].get("manifest_timestamp"),
                "latest_attempt_identity": resolved_rows[0].get("attempt_identity"),
                "latest_attempt_identity_kind": resolved_rows[0].get("attempt_identity_kind"),
                "latest_attempt_uuid": resolved_rows[0].get("attempt_uuid"),
                "analyzed_match_status_counts": dict(analyzed_match_status_counts),
                "analyzed_match_modes": _preview_values(list(analyzed_match_status_counts.keys()), max_items=6),
                "analysis_report_dirs": _preview_values([
                    str(row.get("report_dir")) for row in repro_by_run_entry.get(run_entry, []) if row.get("report_dir")
                ], max_items=4),
            }
        )
    summary_rows.sort(
        key=lambda row: (
            -int(row.get("n_rows") or 0),
            -int(row.get("n_completed_rows") or 0),
            -int(row.get("n_analyzed_rows") or 0),
            str(row.get("run_entry") or ""),
        )
    )
    headline = {
        "n_logical_runs": len(summary_rows),
        "n_logical_runs_with_multiple_rows": sum(1 for row in summary_rows if int(row.get("n_rows") or 0) > 1),
        "n_logical_runs_with_multiple_completed_rows": sum(1 for row in summary_rows if int(row.get("n_completed_rows") or 0) > 1),
        "n_logical_runs_with_multiple_analyzed_rows": sum(1 for row in summary_rows if int(row.get("n_analyzed_rows") or 0) > 1),
        "n_logical_runs_with_ambiguous_analyzed_matching": sum(1 for row in summary_rows if int(row.get("n_ambiguous_analyzed_candidates") or 0) > 0),
        "n_logical_runs_spanning_multiple_machines": sum(1 for row in summary_rows if int(row.get("n_machines") or 0) > 1),
        "n_logical_runs_spanning_multiple_experiments": sum(1 for row in summary_rows if int(row.get("n_experiments") or 0) > 1),
        "n_logical_runs_with_multiple_manifest_timestamps": sum(1 for row in summary_rows if int(row.get("n_manifest_timestamps") or 0) > 1),
        "n_logical_runs_with_multiple_attempt_ids": sum(1 for row in summary_rows if int(row.get("n_attempt_ids") or 0) > 1),
        "n_logical_runs_with_multiple_attempt_uuids": sum(1 for row in summary_rows if int(row.get("n_attempt_uuids") or 0) > 1),
    }
    return {
        "definitions": {
            "logical_result": "logical_run_key == run_entry; this is the current report-layer identity for a logical result",
            "attempt": "one indexed kwdagger/materialize job row",
            "attempt_uuid": "process_context.properties.uuid when available from process_context.json or embedded adapter_manifest.process_context",
            "attempt_fallback_key": "fallback::experiment_name|job_id|run_entry|manifest_timestamp|machine_host|run_dir when UUID is missing",
            "attempt_identity": "attempt_uuid when present, otherwise attempt_fallback_key",
        "version": "a distinct attempt_identity observed under the same logical_run_key",
        "cross_machine_repeat": "same logical_run_key observed on more than one distinct machine_host",
        "analyzed_row": "a completed indexed row matched to report selection provenance by run_dir or attempt identity when available; otherwise only a singleton completed row in an analyzed legacy (experiment_name, run_entry) group",
    },
        "headline_counts": headline,
        "rows": summary_rows,
    }


def _format_run_multiplicity_summary_text(
    *,
    scope_title: str,
    generated_utc: str,
    summary: dict[str, Any],
) -> list[str]:
    counts = summary["headline_counts"]
    lines = [
        "Run Multiplicity Summary",
        "========================",
        f"Generated: {generated_utc}",
        f"Scope: {scope_title}",
        "",
        "Identity contract:",
        f"  logical_result: {summary['definitions']['logical_result']}",
        f"  attempt: {summary['definitions']['attempt']}",
        f"  attempt_uuid: {summary['definitions']['attempt_uuid']}",
        f"  attempt_fallback_key: {summary['definitions']['attempt_fallback_key']}",
        f"  attempt_identity: {summary['definitions']['attempt_identity']}",
        f"  version: {summary['definitions']['version']}",
        f"  cross_machine_repeat: {summary['definitions']['cross_machine_repeat']}",
        f"  analyzed_row: {summary['definitions']['analyzed_row']}",
        "",
        "Headline counts:",
        f"  n_logical_runs: {counts['n_logical_runs']}",
        f"  n_logical_runs_with_multiple_rows: {counts['n_logical_runs_with_multiple_rows']}",
        f"  n_logical_runs_with_multiple_completed_rows: {counts['n_logical_runs_with_multiple_completed_rows']}",
        f"  n_logical_runs_with_multiple_analyzed_rows: {counts['n_logical_runs_with_multiple_analyzed_rows']}",
        f"  n_logical_runs_with_ambiguous_analyzed_matching: {counts['n_logical_runs_with_ambiguous_analyzed_matching']}",
        f"  n_logical_runs_spanning_multiple_machines: {counts['n_logical_runs_spanning_multiple_machines']}",
        f"  n_logical_runs_spanning_multiple_experiments: {counts['n_logical_runs_spanning_multiple_experiments']}",
        f"  n_logical_runs_with_multiple_manifest_timestamps: {counts['n_logical_runs_with_multiple_manifest_timestamps']}",
        f"  n_logical_runs_with_multiple_attempt_ids: {counts['n_logical_runs_with_multiple_attempt_ids']}",
        f"  n_logical_runs_with_multiple_attempt_uuids: {counts['n_logical_runs_with_multiple_attempt_uuids']}",
        "",
        "Per logical run:",
    ]
    rows = summary.get("rows") or []
    if not rows:
        lines.append("  (no attempted runs found in this scope)")
        return lines
    header = (
        f"{'run_entry':<44} {'rows':>4} {'cmp':>4} {'ana':>4} {'amb':>4} {'exp':>4} "
        f"{'mach':>4} {'ids':>4} {'uuids':>5} latest_manifest"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in rows[:200]:
        lines.append(
            f"{str(row.get('run_entry') or ''):<44} "
            f"{int(row.get('n_rows') or 0):>4} "
            f"{int(row.get('n_completed_rows') or 0):>4} "
            f"{int(row.get('n_analyzed_rows') or 0):>4} "
            f"{int(row.get('n_ambiguous_analyzed_candidates') or 0):>4} "
            f"{int(row.get('n_experiments') or 0):>4} "
            f"{int(row.get('n_machines') or 0):>4} "
            f"{int(row.get('n_attempt_ids') or 0):>4} "
            f"{int(row.get('n_attempt_uuids') or 0):>5} "
            f"{str(row.get('latest_manifest_timestamp') or '')}"
        )
    if len(rows) > 200:
        lines.append(f"... ({len(rows) - 200} more rows)")
    return lines


_TRIAGE_DIMENSION_PRIORITY = {
    "benchmark": 0,
    "model": 1,
    "machine_host": 2,
    "experiment_name": 3,
    "suite": 4,
}

# Section taxonomy.
#
# - score_ge_95 / score_lt_80 : *absolute* threshold sections keyed off the
#                   agreement bucket label. They preserve the publication-
#                   quality narrative ("did we hit / fall below the bar?").
#                   The numeric thresholds are baked into the section name
#                   so a reader doesn't have to look up what "good" or "bad"
#                   meant in the schema.
# - best / mid / worst : *population-quantile* sections that pick rows at
#                   the top / median / bottom of whatever analyzed rows are
#                   in scope. Always populated when there is at least one
#                   analyzed row, so tightly-clustered virtual experiments
#                   still surface their actual range.
# - flagged       : signal-based section (multiplicity, machine spread,
#                   ambiguous matching, off-story, report warnings).
_TRIAGE_BUCKET_CLASS_ORDER = {
    "score_ge_95": 0,
    "best": 1,
    "mid": 2,
    "worst": 3,
    "score_lt_80": 4,
    "flagged": 5,
}

_TRIAGE_ABSOLUTE_BUCKETS = {
    "score_ge_95": ("exact_or_near_exact", "high_agreement_0.95+"),
    "score_lt_80": ("low_agreement_0.00+", "zero_agreement"),
}

# Backwards-compatible export for any external readers that previously
# inspected ``_TRIAGE_BUCKET_LABELS[<class>]``. The moderate-agreement key is
# intentionally gone — moderate rows now flow through the quantile-based
# ``mid`` section, which is not threshold-based.
_TRIAGE_BUCKET_LABELS = dict(_TRIAGE_ABSOLUTE_BUCKETS)


_QUANTILE_BUCKET_TARGETS: dict[str, float] = {
    "best": 1.0,
    "mid": 0.5,
    "worst": 0.0,
}


def _agreement_bucket_class(bucket: str | None) -> str | None:
    """Map an agreement bucket label to its absolute section, or None.

    Only ``score_ge_95`` and ``score_lt_80`` are absolute classifications.
    Moderate / zero-but-nonzero agreement labels return ``None``; quantile
    sections do their own population-relative selection downstream.
    """
    text = _clean_optional_text(bucket)
    if text is None:
        return None
    if text in _TRIAGE_ABSOLUTE_BUCKETS["score_ge_95"]:
        return "score_ge_95"
    if text in _TRIAGE_ABSOLUTE_BUCKETS["score_lt_80"]:
        return "score_lt_80"
    return None


def _safe_ratio(numer: int, denom: int) -> float | None:
    return (numer / denom) if denom else None


def _safe_float(value: Any) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except Exception:
        return None


def _triage_bucket_score(
    *,
    bucket_class: str,
    dimension: str,
    n_analyzed: int,
    target_count: int,
    target_share: float,
    mean_score: float | None,
) -> float:
    dim_priority = _TRIAGE_DIMENSION_PRIORITY.get(dimension, 99)
    dim_bonus = max(0, 500 - (dim_priority * 100))
    coverage_bonus = min(n_analyzed, 12) * 3.0
    target_bonus = min(target_count, 8) * 8.0 + (target_share * 80.0)
    score_bonus = 0.0
    if mean_score is not None:
        if bucket_class == "score_ge_95":
            score_bonus = mean_score * 12.0
        elif bucket_class == "score_lt_80":
            score_bonus = (1.0 - mean_score) * 18.0
    return dim_bonus + coverage_bonus + target_bonus + score_bonus


def _flagged_bucket_score(
    *,
    dimension: str,
    n_analyzed: int,
    has_multiplicity_signal: bool,
    has_machine_spread: bool,
    has_ambiguous_analyzed_matching: bool,
    has_off_story_signal: bool,
    bad_count: int,
) -> float:
    dim_priority = _TRIAGE_DIMENSION_PRIORITY.get(dimension, 99)
    dim_bonus = max(0, 500 - (dim_priority * 100))
    flag_bonus = (
        (18.0 if has_ambiguous_analyzed_matching else 0.0)
        + (14.0 if has_machine_spread else 0.0)
        + (12.0 if has_multiplicity_signal else 0.0)
        + (10.0 if has_off_story_signal else 0.0)
    )
    return dim_bonus + flag_bonus + min(n_analyzed, 10) * 2.0 + min(bad_count, 5) * 4.0


def _example_case_sort_key(row: dict[str, Any], bucket_class: str) -> tuple[float, str]:
    score = _safe_float(row.get("official_instance_agree_005"))
    if score is None:
        score = -1.0
    if bucket_class == "score_ge_95":
        primary = score
    elif bucket_class == "score_lt_80":
        primary = -score
    else:
        # Default for ad-hoc bucket_class strings (e.g. flagged-row fallback);
        # rank by closeness to the moderate band so the example list is stable.
        primary = -abs(score - 0.85)
    return (primary, str(row.get("run_entry") or ""))


def _pick_example_cases(
    *,
    rows: list[dict[str, Any]],
    bucket_class: str,
    max_examples: int = 3,
) -> list[dict[str, Any]]:
    target_rows = [row for row in rows if _agreement_bucket_class(row.get("official_instance_agree_bucket")) == bucket_class]
    candidates = target_rows or rows
    sorted_rows = sorted(candidates, key=lambda row: _example_case_sort_key(row, bucket_class), reverse=True)
    picked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in sorted_rows:
        key = (str(row.get("experiment_name") or ""), str(row.get("run_entry") or ""))
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)
        if len(picked) >= max_examples:
            break
    return picked


def _triage_selection_reason(
    *,
    bucket_class: str,
    dimension: str,
    target_count: int,
    target_share: float,
    n_analyzed: int,
    flags: list[str],
) -> str:
    bucket_label = {
        "score_ge_95": "high-agreement (>=0.95)",
        "score_lt_80": "low-agreement (<0.80)",
        "best": "best-of-population",
        "mid": "median-of-population",
        "worst": "worst-of-population",
        "flagged": "flagged",
    }.get(bucket_class, bucket_class)
    reason = (
        f"{dimension} group is a useful {bucket_label} exemplar: "
        f"{target_count}/{n_analyzed} analyzed rows in the target bucket class"
        f" ({target_share:.0%})"
    )
    if flags:
        reason += "; flags=" + ", ".join(flags)
    return reason


def _coerce_listlike(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            return [value]
        if isinstance(parsed, list):
            return parsed
        return [parsed]
    return [value]


def _selected_attempt_refs_for_repro_row(repro: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for item in _coerce_listlike(repro.get("analysis_selected_attempt_refs")):
        if isinstance(item, dict):
            refs.append(item)
    return refs


def _attempt_ref_matches_row(ref: dict[str, Any], row: dict[str, Any]) -> bool:
    comparisons = [
        ("run_dir", "run_dir"),
        ("attempt_identity", "attempt_identity"),
        ("attempt_uuid", "attempt_uuid"),
        ("attempt_fallback_key", "attempt_fallback_key"),
    ]
    for ref_key, row_key in comparisons:
        ref_value = _clean_optional_text(ref.get(ref_key))
        row_value = _clean_optional_text(row.get(row_key))
        if ref_value and row_value and ref_value == row_value:
            return True
    return False


def _choose_parent_row_for_repro(
    repro: dict[str, Any],
    parent_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not parent_rows:
        return {}
    selected_refs = _selected_attempt_refs_for_repro_row(repro)
    for ref in selected_refs:
        for row in parent_rows:
            if _attempt_ref_matches_row(ref, row):
                return row
    return sorted(
        parent_rows,
        key=lambda row: (
            _coerce_float(row.get("manifest_timestamp")),
            str(row.get("job_id") or ""),
        ),
        reverse=True,
    )[0]


def _analyzed_dimension_values(case_row: dict[str, Any], dimension: str) -> list[str]:
    if dimension == "machine_host":
        hosts = sorted({
            str(host)
            for host in [
                _clean_optional_text(ref.get("machine_host"))
                for ref in (_selected_attempt_refs_for_repro_row(case_row))
            ]
            if host
        })
        if hosts:
            return hosts
    value = _clean_optional_text(case_row.get(dimension))
    return [value or "unknown"]


def _build_prioritized_breakdown_summary(
    *,
    enriched_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
    run_multiplicity_summary: dict[str, Any],
    breakdown_dims: list[str],
    level_002: Path,
) -> dict[str, Any]:
    enriched_lookup: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in enriched_rows:
        experiment_name = _clean_optional_text(row.get("experiment_name"))
        run_entry = _clean_optional_text(row.get("run_entry"))
        if experiment_name and run_entry:
            enriched_lookup[(experiment_name, run_entry)].append(row)
    multiplicity_lookup = {
        str(row.get("logical_run_key") or row.get("run_entry") or ""): row
        for row in (run_multiplicity_summary.get("rows") or [])
        if row.get("logical_run_key") or row.get("run_entry")
    }
    analyzed_case_rows: list[dict[str, Any]] = []
    for repro in repro_rows:
        key = (str(repro.get("experiment_name") or ""), str(repro.get("run_entry") or ""))
        parent_rows = enriched_lookup.get(key, [])
        parent = _choose_parent_row_for_repro(repro, parent_rows)
        logical_run_key = str(parent.get("logical_run_key") or repro.get("run_entry") or "")
        multiplicity = multiplicity_lookup.get(logical_run_key, {})
        selected_attempt_refs = _selected_attempt_refs_for_repro_row(repro)
        selected_machine_hosts = sorted({
            str(host)
            for host in [
                _clean_optional_text(ref.get("machine_host"))
                for ref in selected_attempt_refs
            ]
            if host
        })
        analyzed_case_rows.append(
            {
                **parent,
                **repro,
                "logical_run_key": logical_run_key,
                "selected_attempt_refs": selected_attempt_refs,
                "selected_machine_hosts": selected_machine_hosts,
                "official_instance_agree_bucket": repro.get("official_instance_agree_bucket") or parent.get("official_instance_agree_bucket"),
                "bucket_class": _agreement_bucket_class(repro.get("official_instance_agree_bucket")),
                "has_multiplicity_signal": bool(
                    int(multiplicity.get("n_attempt_ids") or 0) > 1 or int(multiplicity.get("n_rows") or 0) > 1
                ),
                "has_machine_spread": bool(int(multiplicity.get("n_machines") or 0) > 1),
                "has_ambiguous_analyzed_matching": bool(int(multiplicity.get("n_ambiguous_analyzed_candidates") or 0) > 0),
                "has_off_story_signal": str(parent.get("storyline_status") or "") == "off_story",
            }
        )

    attempted_by_dim: dict[str, dict[str, list[dict[str, Any]]]] = {}
    completed_by_dim: dict[str, dict[str, list[dict[str, Any]]]] = {}
    analyzed_by_dim: dict[str, dict[str, list[dict[str, Any]]]] = {}
    dims = [dim for dim in _TRIAGE_DIMENSION_PRIORITY if dim in breakdown_dims]
    for dim in dims:
        attempted_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        completed_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        analyzed_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in enriched_rows:
            value = str(row.get(dim) or "unknown")
            attempted_groups[value].append(row)
            if _is_truthy_text(row.get("has_run_spec")):
                completed_groups[value].append(row)
        for row in analyzed_case_rows:
            for value in _analyzed_dimension_values(row, dim):
                analyzed_groups[str(value or "unknown")].append(row)
        attempted_by_dim[dim] = attempted_groups
        completed_by_dim[dim] = completed_groups
        analyzed_by_dim[dim] = analyzed_groups

    all_group_rows: list[dict[str, Any]] = []
    for dim in dims:
        for value, analyzed_group_rows in analyzed_by_dim[dim].items():
            if not analyzed_group_rows:
                continue
            bucket_counts = Counter(str(row.get("official_instance_agree_bucket") or "unknown") for row in analyzed_group_rows)
            bucket_class_counts = Counter(
                _agreement_bucket_class(row.get("official_instance_agree_bucket")) or "other"
                for row in analyzed_group_rows
            )
            scores = [
                score for score in (_safe_float(row.get("official_instance_agree_005")) for row in analyzed_group_rows)
                if score is not None
            ]
            mean_score = (sum(scores) / len(scores)) if scores else None
            flags = {
                "multiplicity_signal": any(bool(row.get("has_multiplicity_signal")) for row in analyzed_group_rows),
                "machine_spread": any(bool(row.get("has_machine_spread")) for row in analyzed_group_rows),
                "ambiguous_analyzed_matching": any(bool(row.get("has_ambiguous_analyzed_matching")) for row in analyzed_group_rows),
                "off_story_signal": any(bool(row.get("has_off_story_signal")) for row in analyzed_group_rows),
                "report_warnings": any(bool(row.get("has_report_warnings")) for row in analyzed_group_rows),
            }
            dominant_bucket = max(bucket_counts.items(), key=lambda item: (item[1], item[0]))[0]
            dominant_bucket_class = _agreement_bucket_class(dominant_bucket) or "other"
            breakdown_dir = level_002 / "breakdowns" / f"by_{dim}" / slugify(value)
            breakdown_index_dir = level_002 / "breakdowns" / f"by_{dim}"
            all_group_rows.append(
                {
                    "dimension": dim,
                    "dimension_value": value,
                    "dimension_priority": _TRIAGE_DIMENSION_PRIORITY.get(dim, 99),
                    "rank_population": "breakdown groups ranked from analyzed reproducibility rows; attempted/completed counts come from all indexed rows in the same group",
                    "n_attempted": len(attempted_by_dim[dim].get(value, [])),
                    "n_completed": len(completed_by_dim[dim].get(value, [])),
                    "n_analyzed": len(analyzed_group_rows),
                    "machine_host_membership_source": (
                        "selected_attempt_refs.machine_host"
                        if dim == "machine_host" and any(row.get("selected_machine_hosts") for row in analyzed_group_rows)
                        else "coarse_parent_row"
                    ),
                    "bucket_counts": dict(bucket_counts),
                    "bucket_class_counts": dict(bucket_class_counts),
                    "dominant_bucket": dominant_bucket,
                    "dominant_bucket_class": dominant_bucket_class,
                    "mean_official_instance_agree_005": mean_score,
                    "has_multiplicity_signal": flags["multiplicity_signal"],
                    "has_machine_spread": flags["machine_spread"],
                    "has_ambiguous_analyzed_matching": flags["ambiguous_analyzed_matching"],
                    "has_off_story_signal": flags["off_story_signal"],
                    "has_report_warnings": flags["report_warnings"],
                    "breakdown_dir": str(breakdown_dir),
                    "breakdown_index_dir": str(breakdown_index_dir),
                    "rows": analyzed_group_rows,
                }
            )

    def _select_bucket_rows(bucket_class: str, limit: int = 3) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for row in all_group_rows:
            target_count = int(row["bucket_class_counts"].get(bucket_class, 0))
            if target_count <= 0:
                continue
            n_analyzed = int(row["n_analyzed"])
            target_share = float(target_count / n_analyzed) if n_analyzed else 0.0
            example_rows = _pick_example_cases(rows=row["rows"], bucket_class=bucket_class)
            flags = [
                name for name, enabled in [
                    ("multiplicity", row["has_multiplicity_signal"]),
                    ("multi_machine", row["has_machine_spread"]),
                    ("ambiguous_analysis", row["has_ambiguous_analyzed_matching"]),
                    ("off_story", row["has_off_story_signal"]),
                    ("report_warnings", row["has_report_warnings"]),
                ]
                if enabled
            ]
            out = dict(row)
            out.update(
                {
                    "bucket_class": bucket_class,
                    "primary_bucket_class": bucket_class,
                    "target_bucket_count": target_count,
                    "target_bucket_share": target_share,
                    "selection_score": _triage_bucket_score(
                        bucket_class=bucket_class,
                        dimension=str(row["dimension"]),
                        n_analyzed=n_analyzed,
                        target_count=target_count,
                        target_share=target_share,
                        mean_score=_safe_float(row.get("mean_official_instance_agree_005")),
                    ),
                    "example_rows": example_rows,
                    "selection_reason": _triage_selection_reason(
                        bucket_class=bucket_class,
                        dimension=str(row["dimension"]),
                        target_count=target_count,
                        target_share=target_share,
                        n_analyzed=n_analyzed,
                        flags=flags,
                    ),
                    "interesting_flags": flags,
                }
            )
            candidates.append(out)
        candidates.sort(
            key=lambda row: (
                -float(row["selection_score"]),
                int(row["dimension_priority"]),
                -int(row["n_analyzed"]),
                str(row["dimension_value"]),
            )
        )
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in candidates:
            key = (str(row["dimension"]), str(row["dimension_value"]))
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)
            if len(selected) >= limit:
                break
        return selected

    def _select_flagged_rows(limit: int = 5) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for row in all_group_rows:
            flags = [
                name for name, enabled in [
                    ("multiplicity", row["has_multiplicity_signal"]),
                    ("multi_machine", row["has_machine_spread"]),
                    ("ambiguous_analysis", row["has_ambiguous_analyzed_matching"]),
                    ("off_story", row["has_off_story_signal"]),
                    ("report_warnings", row["has_report_warnings"]),
                ]
                if enabled
            ]
            if not flags:
                continue
            bad_count = int(row["bucket_class_counts"].get("score_lt_80", 0))
            out = dict(row)
            out.update(
                {
                    "bucket_class": "flagged",
                    "primary_bucket_class": str(row.get("dominant_bucket_class") or "other"),
                    "target_bucket_count": bad_count,
                    "target_bucket_share": _safe_ratio(bad_count, int(row["n_analyzed"])) or 0.0,
                    "selection_score": _flagged_bucket_score(
                        dimension=str(row["dimension"]),
                        n_analyzed=int(row["n_analyzed"]),
                        has_multiplicity_signal=bool(row["has_multiplicity_signal"]),
                        has_machine_spread=bool(row["has_machine_spread"]),
                        has_ambiguous_analyzed_matching=bool(row["has_ambiguous_analyzed_matching"]),
                        has_off_story_signal=bool(row["has_off_story_signal"]),
                        bad_count=bad_count,
                    ),
                    "example_rows": _pick_example_cases(
                        rows=row["rows"],
                        bucket_class="score_lt_80" if bad_count else str(row.get("dominant_bucket_class") or "flagged"),
                    ),
                    "selection_reason": "interesting investigative flags in an analyzed breakdown group: " + ", ".join(flags),
                    "interesting_flags": flags,
                }
            )
            candidates.append(out)
        candidates.sort(
            key=lambda row: (
                -float(row["selection_score"]),
                int(row["dimension_priority"]),
                -int(row["n_analyzed"]),
                str(row["dimension_value"]),
            )
        )
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in candidates:
            key = (str(row["dimension"]), str(row["dimension_value"]))
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)
            if len(selected) >= limit:
                break
        return selected

    def _select_quantile_section(
        section_name: str,
        *,
        max_examples: int = 3,
    ) -> list[dict[str, Any]]:
        """Build a single synthetic-breakdown entry for a population-quantile section.

        Picks examples at the section's target quantile of the analyzed-row
        population (best=1.0, mid=0.5, worst=0.0), regardless of how those
        rows fall into absolute good/bad buckets. Returns a list to match the
        shape of the absolute-bucket selectors (length 1 or 0).
        """
        target_quantile = _QUANTILE_BUCKET_TARGETS[section_name]
        scored: list[tuple[dict[str, Any], float]] = []
        for case in analyzed_case_rows:
            score = _safe_float(case.get("official_instance_agree_005"))
            if score is None:
                continue
            scored.append((case, score))
        if not scored:
            return []
        scored.sort(key=lambda pair: pair[1])  # ascending: index 0 is worst
        n = len(scored)
        target_idx = round(target_quantile * (n - 1))
        # Sort candidates by distance from target_idx, breaking ties by score
        # in the direction that matches the section semantics so the leading
        # example reads naturally (highest score for "best", lowest for
        # "worst", closest-to-median for "mid").
        if section_name == "best":
            ranked = sorted(range(n), key=lambda i: (-scored[i][1], abs(i - target_idx)))
        elif section_name == "worst":
            ranked = sorted(range(n), key=lambda i: (scored[i][1], abs(i - target_idx)))
        else:  # mid
            ranked = sorted(range(n), key=lambda i: (abs(i - target_idx), scored[i][1]))
        seen_keys: set[tuple[str, str]] = set()
        example_rows: list[dict[str, Any]] = []
        for idx in ranked:
            row = scored[idx][0]
            key = (str(row.get("experiment_name") or ""), str(row.get("run_entry") or ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            example_rows.append(row)
            if len(example_rows) >= max_examples:
                break
        scores = [scored[i][1] for i in range(n)]
        section_label = {
            "best": "highest agreement (top of population)",
            "mid": "median agreement (population p50)",
            "worst": "lowest agreement (bottom of population)",
        }[section_name]
        synthetic_row = {
            "dimension": "agreement_quantile",
            "dimension_priority": _TRIAGE_BUCKET_CLASS_ORDER[section_name],
            "dimension_value": section_name,
            "rank_population": (
                "rows ranked across the analyzed-row population by official_instance_agree_005; "
                "examples picked at the section's target quantile (best=1.0, mid=0.5, worst=0.0)"
            ),
            "n_attempted": n,
            "n_completed": n,
            "n_analyzed": n,
            "target_bucket_count": len(example_rows),
            "target_bucket_share": _safe_ratio(len(example_rows), n) or 0.0,
            "dominant_bucket": None,
            "dominant_bucket_class": None,
            "bucket_counts": {},
            "bucket_class_counts": {},
            "machine_host_membership_source": None,
            "mean_official_instance_agree_005": (sum(scores) / n) if scores else None,
            "has_multiplicity_signal": False,
            "has_machine_spread": False,
            "has_ambiguous_analyzed_matching": False,
            "has_off_story_signal": False,
            "has_report_warnings": False,
            "breakdown_dir": None,
            "breakdown_index_dir": None,
            "rows": [pair[0] for pair in scored],
        }
        synthetic_row.update(
            {
                "bucket_class": section_name,
                "primary_bucket_class": section_name,
                "selection_score": 0.0,
                "example_rows": example_rows,
                "selection_reason": (
                    f"{section_label}: showing {len(example_rows)} example(s) "
                    f"out of {n} analyzed row(s); "
                    f"min={min(scores):.4f} median={scores[n // 2]:.4f} max={max(scores):.4f}"
                ),
                "interesting_flags": [],
            }
        )
        return [synthetic_row]

    selected_by_section = {
        "score_ge_95": _select_bucket_rows("score_ge_95", limit=4),
        "best": _select_quantile_section("best"),
        "mid": _select_quantile_section("mid"),
        "worst": _select_quantile_section("worst"),
        "score_lt_80": _select_bucket_rows("score_lt_80", limit=4),
        "flagged": _select_flagged_rows(limit=6),
    }

    flattened_rows: list[dict[str, Any]] = []
    for section_name in ["score_ge_95", "best", "mid", "worst", "score_lt_80", "flagged"]:
        for idx, row in enumerate(selected_by_section[section_name], start=1):
            example_rows = row.get("example_rows") or []
            flattened_rows.append(
                {
                    "priority_rank": idx,
                    "bucket_class": section_name,
                    "dimension": row["dimension"],
                    "dimension_priority": row["dimension_priority"],
                    "dimension_value": row["dimension_value"],
                    "rank_population": row["rank_population"],
                    "n_attempted": row["n_attempted"],
                    "n_completed": row["n_completed"],
                    "n_analyzed": row["n_analyzed"],
                    "target_bucket_count": row["target_bucket_count"],
                    "target_bucket_share": row["target_bucket_share"],
                    "dominant_bucket": row["dominant_bucket"],
                    "dominant_bucket_class": row["dominant_bucket_class"],
                    "bucket_counts": row["bucket_counts"],
                    "bucket_class_counts": row["bucket_class_counts"],
                    "machine_host_membership_source": row.get("machine_host_membership_source"),
                    "mean_official_instance_agree_005": row["mean_official_instance_agree_005"],
                    "has_multiplicity_signal": row["has_multiplicity_signal"],
                    "has_machine_spread": row["has_machine_spread"],
                    "has_ambiguous_analyzed_matching": row["has_ambiguous_analyzed_matching"],
                    "has_off_story_signal": row["has_off_story_signal"],
                    "interesting_flags": row["interesting_flags"],
                    "breakdown_dir": row["breakdown_dir"],
                    "breakdown_index_dir": row["breakdown_index_dir"],
                    "example_report_dirs": _preview_values([
                        str(item.get("report_dir")) for item in example_rows if item.get("report_dir")
                    ], max_items=3),
                    "example_run_entries": _preview_values([
                        str(item.get("run_entry")) for item in example_rows if item.get("run_entry")
                    ], max_items=3),
                    "example_models": _preview_values([
                        str(item.get("model")) for item in example_rows if item.get("model")
                    ], max_items=3),
                    "selection_reason": row["selection_reason"],
                    "selection_score": row["selection_score"],
                }
            )

    include_values_by_dim: dict[str, set[str]] = defaultdict(set)
    for row in flattened_rows:
        include_values_by_dim[str(row["dimension"])].add(str(row["dimension_value"]))

    def _serialize_example_row(row: dict[str, Any]) -> dict[str, Any]:
        keep = [
            "experiment_name",
            "run_entry",
            "packet_id",
            "report_dir",
            "report_json",
            "warnings_manifest",
            "has_report_warnings",
            "official_instance_agree_bucket",
            "official_instance_agree_005",
            "analysis_single_run",
        ]
        return {key: row.get(key) for key in keep if key in row}

    return {
        "definitions": {
            "rank_population": "breakdown groups ranked from analyzed reproducibility rows; attempted/completed counts are added from all indexed rows in the same group; machine_host membership uses selected attempt provenance when available",
            "section_classes": {
                "score_ge_95": {
                    "kind": "absolute",
                    "agreement_buckets": list(_TRIAGE_ABSOLUTE_BUCKETS["score_ge_95"]),
                    "purpose": "publication-quality threshold (>=0.95 instance-level agreement)",
                },
                "best": {
                    "kind": "quantile",
                    "target_quantile": _QUANTILE_BUCKET_TARGETS["best"],
                    "purpose": "top of the analyzed-row population by official_instance_agree_005, regardless of absolute bucket",
                },
                "mid": {
                    "kind": "quantile",
                    "target_quantile": _QUANTILE_BUCKET_TARGETS["mid"],
                    "purpose": "median of the analyzed-row population by official_instance_agree_005, regardless of absolute bucket",
                },
                "worst": {
                    "kind": "quantile",
                    "target_quantile": _QUANTILE_BUCKET_TARGETS["worst"],
                    "purpose": "bottom of the analyzed-row population by official_instance_agree_005, regardless of absolute bucket",
                },
                "score_lt_80": {
                    "kind": "absolute",
                    "agreement_buckets": list(_TRIAGE_ABSOLUTE_BUCKETS["score_lt_80"]),
                    "purpose": "publication-quality floor (<0.80 instance-level agreement)",
                },
                "flagged": {
                    "kind": "signal",
                    "purpose": "interesting investigative flags regardless of primary bucket",
                },
            },
            "dimension_priority": _TRIAGE_DIMENSION_PRIORITY,
        },
        "selected_by_section": {
            key: [
                {
                    **{
                        k: v for k, v in row.items()
                        if k != "rows" and k != "example_rows"
                    },
                    "example_rows": [
                        _serialize_example_row(example_row)
                        for example_row in (row.get("example_rows") or [])
                    ],
                }
                for row in value
            ]
            for key, value in selected_by_section.items()
        },
        "rows": flattened_rows,
        "include_values_by_dim": {dim: sorted(values) for dim, values in include_values_by_dim.items()},
    }


def _format_prioritized_breakdown_summary_text(
    *,
    scope_title: str,
    generated_utc: str,
    summary: dict[str, Any],
) -> list[str]:
    lines = [
        "Prioritized Breakdown Investigation Checklist",
        "=============================================",
        f"Generated: {generated_utc}",
        f"Scope: {scope_title}",
        "",
        "Population:",
        f"  {summary['definitions']['rank_population']}",
        "",
        "Dimension priority:",
    ]
    for dim, rank in _TRIAGE_DIMENSION_PRIORITY.items():
        lines.append(f"  {rank + 1}. {dim}")

    section_titles = [
        ("score_ge_95", "score_ge_95 — high-agreement breakdowns (absolute threshold, instance agreement >= 0.95)"),
        ("best", "best — best-of-population examples (quantile=1.0, regardless of absolute bucket)"),
        ("mid", "mid — median-of-population examples (quantile=0.5, regardless of absolute bucket)"),
        ("worst", "worst — worst-of-population examples (quantile=0.0, regardless of absolute bucket)"),
        ("score_lt_80", "score_lt_80 — low-agreement breakdowns (absolute threshold, instance agreement < 0.80)"),
        ("flagged", "flagged — special cases worth inspecting (signal-based, regardless of bucket)"),
    ]
    for section_key, section_title in section_titles:
        rows = [row for row in (summary.get("rows") or []) if row.get("bucket_class") == section_key]
        lines.extend(["", section_title, "-" * len(section_title)])
        if not rows:
            lines.append("  (none)")
            continue
        for row in rows:
            lines.append(
                f"[{row['priority_rank']}] {row['dimension']} = {row['dimension_value']} "
                f"({row['bucket_class']}; analyzed={row['n_analyzed']}, target={row['target_bucket_count']}, share={float(row['target_bucket_share'] or 0.0):.0%})"
            )
            lines.append(f"  reason: {row['selection_reason']}")
            lines.append(
                f"  counts: attempted={row['n_attempted']} completed={row['n_completed']} analyzed={row['n_analyzed']} "
                f"dominant_bucket={row['dominant_bucket']}"
            )
            if row["dimension"] == "machine_host" and row.get("machine_host_membership_source"):
                lines.append(f"  machine_host_membership_source: {row['machine_host_membership_source']}")
            flags = row.get("interesting_flags") or []
            if flags:
                lines.append(f"  flags: {', '.join(flags)}")
            lines.append(f"  breakdown_dir: {render_path_link(row['breakdown_dir'])}")
            lines.append(f"  breakdown_index_dir: {render_path_link(row['breakdown_index_dir'])}")
            example_runs = row.get("example_run_entries") or []
            example_reports = row.get("example_report_dirs") or []
            if example_runs:
                lines.append("  example_run_entries:")
                for item in example_runs:
                    lines.append(f"    - {item}")
            if example_reports:
                lines.append("  example_report_dirs:")
                for item in example_reports:
                    lines.append(f"    - {render_path_link(item)}")
    return lines


def _iter_prioritized_example_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for section_rows in (summary.get("selected_by_section") or {}).values():
        for row in section_rows or []:
            for example_row in row.get("example_rows") or []:
                key = (
                    str(example_row.get("experiment_name") or ""),
                    str(example_row.get("run_entry") or ""),
                    str(example_row.get("report_dir") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(example_row)
    return rows


def _prioritized_example_artifact_names(report_dir: Path) -> list[str]:
    try:
        packet = load_core_report_bundle(report_dir / "core_metric_report.latest.json")["packet"]
    except Exception:
        return [
            "core_metric_report.latest.png",
            "core_metric_management_summary.latest.txt",
            "components_manifest.latest.json",
            "comparisons_manifest.latest.json",
            "warnings.latest.json",
            "warnings.latest.txt",
        ]
    return prioritized_example_artifact_names(packet)


def _report_artifact_is_usable(fpath: Path) -> bool:
    return fpath.exists()


def _prioritized_example_missing_artifacts(report_dir: Path) -> list[str]:
    return [
        name for name in _prioritized_example_artifact_names(report_dir)
        if not _report_artifact_is_usable(report_dir / name)
    ]


def _repair_prioritized_example_reports(
    *,
    summary: dict[str, Any],
    index_fpath: Path,
) -> list[dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    for example_row in _iter_prioritized_example_rows(summary):
        report_dir_text = _clean_optional_text(example_row.get("report_dir"))
        run_entry = _clean_optional_text(example_row.get("run_entry"))
        if not report_dir_text or not run_entry:
            continue
        report_dir = Path(report_dir_text).expanduser()
        if not report_dir.exists():
            repairs.append(
                {
                    "report_dir": str(report_dir),
                    "run_entry": run_entry,
                    "status": "missing_report_dir",
                    "missing_artifacts": _prioritized_example_artifact_names(report_dir),
                }
            )
            continue
        missing = _prioritized_example_missing_artifacts(report_dir)
        if not missing:
            repairs.append(
                {
                    "report_dir": str(report_dir),
                    "run_entry": run_entry,
                    "status": "already_ok",
                    "missing_artifacts": [],
                }
            )
            continue
        argv = [
            "--run-entry", run_entry,
            "--report-dpath", str(report_dir),
            "--index-fpath", str(index_fpath),
        ]
        experiment_name = _clean_optional_text(example_row.get("experiment_name"))
        if experiment_name:
            argv.extend(["--experiment-name", experiment_name])
        if bool(example_row.get("analysis_single_run")):
            argv.append("--allow-single-repeat")
        rebuild_core_report_main(argv)
        remaining = _prioritized_example_missing_artifacts(report_dir)
        repairs.append(
            {
                "report_dir": str(report_dir),
                "run_entry": run_entry,
                "status": "repaired" if not remaining else "repair_incomplete",
                "missing_artifacts": remaining,
            }
        )
    return repairs


def _publish_prioritized_examples_tree(
    *,
    level_002: Path,
    generated_utc: str,
    summary: dict[str, Any],
    repair_results: list[dict[str, Any]] | None = None,
) -> Path:
    tree_root = level_002 / f"prioritized_examples_{generated_utc}"
    tree_root.mkdir(parents=True, exist_ok=True)
    repairs_by_dir = {
        str(item.get("report_dir") or ""): item
        for item in (repair_results or [])
        if item.get("report_dir")
    }
    for section_name in ["score_ge_95", "best", "mid", "worst", "score_lt_80", "flagged"]:
        section_dpath = tree_root / section_name
        section_dpath.mkdir(parents=True, exist_ok=True)
        for row in (summary.get("selected_by_section") or {}).get(section_name, []):
            dim = str(row.get("dimension") or "unknown")
            value = str(row.get("dimension_value") or "unknown")
            rank = int(row.get("priority_rank") or 0)
            rec_dpath = section_dpath / f"{rank:02d}-{slugify(dim)}-{slugify(value)}"
            rec_dpath.mkdir(parents=True, exist_ok=True)
            metadata = {
                "bucket_class": section_name,
                "priority_rank": rank,
                "dimension": dim,
                "dimension_value": value,
                "selection_reason": row.get("selection_reason"),
                "breakdown_dir": row.get("breakdown_dir"),
                "breakdown_index_dir": row.get("breakdown_index_dir"),
                "interesting_flags": row.get("interesting_flags") or [],
                "example_report_dirs": [ex.get("report_dir") for ex in (row.get("example_rows") or []) if ex.get("report_dir")],
            }
            _write_json(metadata, rec_dpath / "metadata.json")
            breakdown_dir = _clean_optional_text(row.get("breakdown_dir"))
            if breakdown_dir and Path(breakdown_dir).exists():
                symlink_to(breakdown_dir, rec_dpath / "breakdown_dir")
            breakdown_index_dir = _clean_optional_text(row.get("breakdown_index_dir"))
            if breakdown_index_dir and Path(breakdown_index_dir).exists():
                symlink_to(breakdown_index_dir, rec_dpath / "breakdown_index_dir")
            for ex_idx, example_row in enumerate(row.get("example_rows") or [], start=1):
                run_entry = str(example_row.get("run_entry") or f"example-{ex_idx}")
                ex_dpath = rec_dpath / f"example_{ex_idx:02d}-{slugify(run_entry)}"
                ex_dpath.mkdir(parents=True, exist_ok=True)
                example_report_dir = _clean_optional_text(example_row.get("report_dir"))
                if example_report_dir and Path(example_report_dir).exists():
                    symlink_to(example_report_dir, ex_dpath / "report_dir")
                    for artifact_name in _prioritized_example_artifact_names(Path(example_report_dir)):
                        artifact_fpath = Path(example_report_dir) / artifact_name
                        if artifact_fpath.exists():
                            symlink_to(artifact_fpath, ex_dpath / artifact_name)
                repair_info = repairs_by_dir.get(str(example_report_dir or ""))
                if repair_info is not None:
                    _write_json(repair_info, ex_dpath / "repair_status.json")
    readme_lines = [
        "Prioritized Examples",
        "",
        f"generated_utc: {generated_utc}",
        "This tree is filesystem-first navigation for the prioritized breakdown shortlist.",
        "Each recommendation directory links to the selected breakdown, its parent index, and example report dirs with key latest artifacts.",
    ]
    _write_text(readme_lines, tree_root / "README.txt")
    write_latest_alias(tree_root / "README.txt", tree_root, "README.latest.txt")
    write_latest_alias(tree_root, level_002, "prioritized_examples.latest")
    return tree_root


_AXIS_COUNT_TAGS = {
    "benchmark": "n_benchmarks",
    "model": "n_models",
    "dataset": "n_datasets",
    "scenario": "n_scenarios",
    "official_instance_agree_bucket": "n_buckets",
    "agreement_bucket": "n_buckets",
    "failure_reason": "n_failure_reasons",
    "category": "n_categories",
    "group_value": "n_categories",
}


def _ordered_unique_values(rows: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = str(row.get(key) or "unknown")
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _abbreviate_label(text: str, *, max_chars: int = 24) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max_chars
    return text[: max_chars - 3].rstrip() + "..."


def _bar_count_label(axis_key: str, n_bars: int, *, axis_title: str | None = None) -> str:
    label = axis_title if axis_title is not None else axis_key.replace("_", " ").title()
    count_tag = _AXIS_COUNT_TAGS.get(axis_key, "n_categories")
    return f"{label} ({count_tag}={n_bars})"


def _bar_tickangle(n_bars: int) -> int:
    if n_bars > 50:
        return 90
    if n_bars > 25:
        return 75
    if n_bars > 12:
        return 60
    return -45


def _compact_bar_figure_size(unique_x: list[str]) -> tuple[int, int]:
    longest_label = max((len(value) for value in unique_x), default=0)
    n_bars = max(len(unique_x), 1)
    width = min(max(1100, 36 * n_bars, 14 * longest_label * n_bars), 1600)
    height = min(max(520, 14 * n_bars + 240), 1000)
    return width, height


def _write_plotly_bar(
    *,
    rows: list[dict[str, Any]],
    x: str,
    y: str,
    color: str,
    title: str,
    stem: Path,
    machine_dpath: Path | None = None,
    interactive_dpath: Path | None = None,
    static_dpath: Path | None = None,
    xaxis_title: str | None = None,
    yaxis_title: str | None = None,
    xaxis_count_key: str | None = None,
) -> dict[str, str | None]:
    if machine_dpath is not None:
        machine_dpath.mkdir(parents=True, exist_ok=True)
        json_fpath = (machine_dpath / stem.name).with_suffix(".json")
    else:
        json_fpath = stem.with_suffix(".json")
    _interactive = interactive_dpath if interactive_dpath is not None else stem.parent
    _static = static_dpath if static_dpath is not None else stem.parent
    _interactive.mkdir(parents=True, exist_ok=True)
    _static.mkdir(parents=True, exist_ok=True)
    html_fpath = (_interactive / stem.name).with_suffix(".html")
    jpg_fpath = (_static / stem.name).with_suffix(".jpg")
    png_fpath = (_static / stem.name).with_suffix(".png")
    _write_json(rows, json_fpath)
    html_out = None
    jpg_out = None
    png_out = None
    plotly_error = None
    unique_x = _ordered_unique_values(rows, x)
    color_values = _ordered_unique_values(rows, color)
    count_label = _bar_count_label(xaxis_count_key or x, len(unique_x), axis_title=xaxis_title)
    if os.environ.get("HELM_AUDIT_SKIP_PLOTLY", "") not in {"1", "true", "yes"}:
        try:
            configure_plotly_chrome()
            import plotly.express as px

            fig = px.bar(
                rows,
                x=x,
                y=y,
                color=color,
                title=title,
                barmode="stack",
                category_orders={x: unique_x, color: color_values},
            )
            fig.update_layout(
                xaxis_title=count_label,
                yaxis_title=yaxis_title if yaxis_title is not None else y.replace("_", " "),
            )
            fig.update_xaxes(
                categoryorder="array",
                categoryarray=unique_x,
                tickmode="array",
                tickvals=unique_x,
                ticktext=unique_x,
                tickangle=-45,
                automargin=True,
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                static_width, static_height = _compact_bar_figure_size(unique_x)
                fig.update_layout(width=static_width, height=static_height, margin={"b": min(max(120, 8 * max((len(v) for v in unique_x), default=0)), 220), "t": 80, "l": 70, "r": 30})
                fig.update_xaxes(
                    ticktext=[_abbreviate_label(value) for value in unique_x],
                    tickangle=_bar_tickangle(len(unique_x)),
                    tickfont={"size": 8 if len(unique_x) > 12 else 10},
                )
                fig.write_image(str(jpg_fpath), scale=1.0)
                jpg_out = str(jpg_fpath)
        except Exception as ex:
            plotly_error = f"unable to write bar HTML/images: {ex!r}"
    else:
        plotly_error = "skipped plotly bar rendering by configuration"
    if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
        try:
            import matplotlib.pyplot as plt

            if rows:
                x_values = unique_x
                counts = {(str(row.get(x, "")), str(row.get(color, ""))): float(row.get(y, 0) or 0) for row in rows}
                bottoms = [0.0 for _ in x_values]
                width_px, height_px = _compact_bar_figure_size(unique_x)
                dpi = 120
                fig, ax = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
                positions = list(range(len(x_values)))
                for color_value in color_values:
                    vals = [counts.get((xv, color_value), 0.0) for xv in x_values]
                    ax.bar(positions, vals, bottom=bottoms, label=color_value)
                    bottoms = [a + b for a, b in zip(bottoms, vals)]
                ax.set_title(title)
                ax.set_xlabel(count_label)
                ax.set_ylabel(y.replace("_", " "))
                ax.tick_params(axis="x", rotation=_bar_tickangle(len(x_values)))
                if x_values:
                    ax.set_xticks(positions)
                    ax.set_xticklabels([_abbreviate_label(value) for value in x_values], fontsize=8 if len(x_values) > 12 else 10)
                ax.legend(fontsize=8)
                fig.tight_layout()
                fig.savefig(png_fpath, dpi=dpi)
                png_out = str(png_fpath)
                if jpg_out is None:
                    fig.savefig(jpg_fpath, dpi=dpi)
                    jpg_out = str(jpg_fpath)
                plt.close(fig)
        except Exception:
            pass
    return {
        "json": str(json_fpath),
        "html": html_out,
        "jpg": jpg_out,
        "png": png_out,
        "plotly_error": plotly_error,
    }


def _scope_summary_root(summary_root: Path, scope_slug: str) -> Path:
    return summary_root / scope_slug


def _scope_label(scope_kind: str, scope_value: str | None) -> str:
    if scope_kind == "all_results":
        return "all_results"
    return f"{scope_kind}={scope_value}"


def _scope_slug(scope_kind: str, scope_value: str | None) -> str:
    if scope_kind == "all_results":
        return "all-results"
    return f"{scope_kind}-{slugify(str(scope_value))}"


def _build_breakdown_rows(
    enriched_rows: list[dict[str, Any]],
    *,
    group_key: str,
    repro_keyed: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in enriched_rows:
        group_value = str(row.get(group_key) or "unknown")
        if row.get("completed_with_run_artifacts"):
            repro = repro_keyed.get((str(row.get("experiment_name")), str(row.get("run_entry"))))
            status = "completed_not_yet_analyzed"
            if repro is not None:
                status = f"analyzed::{repro['official_instance_agree_bucket']}"
        else:
            status = f"failed::{row.get('failure_reason') or 'unknown_failure'}"
        counts[(group_value, status)] += 1
    return [
        {"group_value": group_value, "status_bucket": status, "count": count}
        for (group_value, status), count in sorted(counts.items())
    ]


def _build_filter_selection_by_model_rows(filter_inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in filter_inventory_rows:
        model = str(row.get("model") or "unknown")
        selection_status = "selected" if row.get("selection_status") == "selected" else "excluded"
        counts[model][selection_status] += 1

    rows: list[dict[str, Any]] = []
    for model, status_counts in sorted(
        counts.items(),
        key=lambda item: (-(item[1]["selected"] + item[1]["excluded"]), -item[1]["selected"], item[0]),
    ):
        for selection_status in ["excluded", "selected"]:
            count = int(status_counts.get(selection_status, 0))
            if count:
                rows.append(
                    {
                        "model": model,
                        "selection_status": selection_status,
                        "count": count,
                    }
                )
    return rows


def _summarize_by_dimension(
    enriched_rows: list[dict[str, Any]],
    *,
    dimension: str,
    repro_keyed: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    by_value: dict[str, dict[str, Any]] = {}
    for row in enriched_rows:
        value = str(row.get(dimension) or "unknown")
        info = by_value.setdefault(
            value,
            {
                dimension: value,
                "total_jobs": 0,
                "completed_jobs": 0,
                "analyzed_jobs": 0,
                "failed_jobs": 0,
                "failure_reasons": Counter(),
            },
        )
        info["total_jobs"] += 1
        if row.get("completed_with_run_artifacts"):
            info["completed_jobs"] += 1
            key = (str(row.get("experiment_name")), str(row.get("run_entry")))
            if key in repro_keyed:
                info["analyzed_jobs"] += 1
        else:
            info["failed_jobs"] += 1
            info["failure_reasons"][row.get("failure_reason") or "unknown_failure"] += 1
    rows = []
    for value, info in sorted(by_value.items()):
        rows.append(
            {
                dimension: value,
                "total_jobs": info["total_jobs"],
                "completed_jobs": info["completed_jobs"],
                "analyzed_jobs": info["analyzed_jobs"],
                "failed_jobs": info["failed_jobs"],
                "completion_rate": (info["completed_jobs"] / info["total_jobs"]) if info["total_jobs"] else None,
                "top_failure_reason": info["failure_reasons"].most_common(1)[0][0] if info["failure_reasons"] else None,
            }
        )
    return rows


def _cardinality(rows: list[dict[str, Any]], *, model_key: str = "model", bench_key: str = "benchmark", scenario_key: str = "scenario") -> dict[str, int]:
    return {
        "n": len(rows),
        "models": len({r.get(model_key) for r in rows if r.get(model_key)}),
        "benchmarks": len({r.get(bench_key) for r in rows if r.get(bench_key)}),
        "scenarios": len({r.get(scenario_key) for r in rows if r.get(scenario_key)}),
        "model_bench_pairs": len({(r.get(model_key), r.get(bench_key)) for r in rows if r.get(model_key) and r.get(bench_key)}),
    }


def _build_scope_cardinality_lines(
    *,
    filter_inventory_rows: list[dict[str, Any]],
    enriched_rows: list[dict[str, Any]],
    scope_title: str,
    generated_utc: str,
) -> list[str]:
    header = f"{'Stage':<22} {'runs':>6}  {'models':>6}  {'benchmarks':>10}  {'scenarios':>9}  {'mod×bench':>9}"
    sep = "-" * len(header)
    lines = [
        f"Scope Cardinality Summary: {scope_title}",
        f"Generated: {generated_utc}",
        "",
        "Run-spec counts at each stage of the pipeline funnel.",
        "",
        header,
        sep,
    ]

    def row_line(label: str, c: dict[str, int]) -> str:
        return (
            f"{label:<22} {c['n']:>6}  {c['models']:>6}  {c['benchmarks']:>10}"
            f"  {c['scenarios']:>9}  {c['model_bench_pairs']:>9}"
        )

    if filter_inventory_rows:
        all_inv = filter_inventory_rows
        selected_inv = [r for r in filter_inventory_rows if r.get("selection_status") == "selected"]
        lines.append(row_line("discovered", _cardinality(all_inv)))
        lines.append(row_line("selected", _cardinality(selected_inv)))

    lines.append(row_line("attempted", _cardinality(enriched_rows)))

    completed_rows = [r for r in enriched_rows if _is_truthy_text(r.get("has_run_spec"))]
    lines.append(row_line("completed", _cardinality(completed_rows)))

    analyzed_rows = [r for r in enriched_rows if r.get("repro_report_dir") is not None]
    lines.append(row_line("analyzed", _cardinality(analyzed_rows)))

    lines += [
        "",
        "Columns: runs = total run entries; models/benchmarks/scenarios = unique values;",
        "         mod×bench = unique (model, benchmark) pairs in that subset.",
        "Stages: discovered = all runs seen by Stage 1 filter; selected = passed all filters",
        "        and chosen for reproduction; attempted = scheduled in this experiment;",
        "        completed = produced HELM artifacts; analyzed = have reproducibility report.",
        "Note: discovered/selected rows show the global filter universe; other rows are scoped",
        "      to this report's experiment/dimension filter.",
    ]
    return lines


def _build_high_level_readme(
    *,
    scope_title: str,
    generated_utc: str,
    n_total: int,
    n_completed: int,
    n_analyzed: int,
    n_failed: int,
    top_failure_rows: list[dict[str, Any]],
    top_repro_rows: list[dict[str, Any]],
    breakdown_dims: list[str],
) -> list[str]:
    lines = [
        "Executive Summary",
        "",
        f"generated_utc: {generated_utc}",
        f"scope: {scope_title}",
        f"total_jobs: {n_total}",
        f"completed_with_run_artifacts: {n_completed}",
        f"completed_and_analyzed: {n_analyzed}",
        f"failed_or_incomplete: {n_failed}",
        "",
        "key_takeaways:",
        f"  - {n_completed}/{n_total} jobs produced runnable HELM artifacts in this scope.",
        f"  - {n_analyzed} completed jobs in this scope already have reproducibility reports.",
    ]
    if top_failure_rows:
        lines.append("  - dominant failure reasons currently appear to be:")
        for row in top_failure_rows[:5]:
            lines.append(f"    * {row['failure_reason']}: {row['count']}")
    if top_repro_rows:
        lines.append("  - analyzed reproducibility buckets currently are:")
        for row in top_repro_rows[:5]:
            lines.append(f"    * {row['official_instance_agree_bucket']}: {row['count']}")
    lines.extend(
        [
            "",
            "start_here:",
            "  story_index.latest.txt — canonical 5-step reading order for the sankey visualizations",
            "  cardinality_summary.latest.txt — run/model/benchmark counts at each stage of the funnel",
            "  off_story_summary.latest.txt — off-story local-extension models with selected/attempted/completed/analyzed counts",
            "  run_multiplicity_summary.latest.txt — repeated attempts, machine spread, experiment spread, and UUID/fallback identity coverage",
            "  prioritized_breakdowns.latest.txt — shortlist of benchmark/model/machine/experiment breakdowns to inspect next",
            "",
            "  understand_upstream_filtering:",
            "    1. What runs were excluded at Stage 1 (discovery)? See reports/filtering/ which contains",
            "       sankey_model_filter.latest.html and filter_cardinality_summary.latest.txt.",
            "    2. Read docs/pipeline.md for the full end-to-end workflow (stages 1-6).",
            "",
            "  explore_execution_coverage (read sankeys in order):",
            "    s01: sankey_s01_operational.latest.html — all attempted runs: benchmark → lifecycle → outcome",
            "    a:   sankey_a_universe_to_scope.latest.html — Stage A: Universe → Scope (filter funnel)",
            "    b:   sankey_b_scope_to_analyzed.latest.html — Stage B: Scope → Attempt → Execution → Analysis → Reproduction (abs_tol=0)",
            "    s05: sankey_s05_reproducibility.latest.html — detailed group → repeatability → agreement → diagnosis",
            "    sup: sankey_repro_by_metric.latest.html — per-metric drift (run-level max |official - local|)",
            "    sup: filter_selection_by_model.latest.html — selected vs excluded run-specs by model",
            "    sup: benchmark_status.latest.html and coverage_matrix.latest.html",
            "    alt: alt_tolerances/ — tolerance sweep variants (tol001, tol010, tol050) for s03/s04/s05",
            "",
            "  understand_reproducibility:",
            "    1. open agreement_curve.latest.html to see how agreement changes across tolerance thresholds",
            "    2. open agreement_curve_per_metric.latest.html for per-metric agreement curves",
            "    3. open reproducibility_buckets.latest.html to see agreement distribution",
            "    4. for relaxed tolerances, see alt_tolerances/ subdirectory",
            "",
            "  diagnose_failures:",
            "    1. read failure_reasons.latest.txt to see why incomplete jobs failed",
            "    2. open failure_taxonomy.latest.html to see root-cause breakdown (hardware/data/infra)",
            "",
            "  drill_down_by_dimension:",
            "    - follow next_level/ for breakdown tables by benchmark, model, suite, machine, experiment",
            "    - use prioritized_breakdowns.latest.* for a triage-first shortlist with direct breakdown paths",
            "    - use off_story_summary.latest.* and run_multiplicity_summary.latest.* for storyline/attempt identity tables",
            "    - run reproduce.latest.sh to regenerate this report from current data",
            "",
            "default_breakdowns:",
        ]
    )
    for dim in breakdown_dims:
        lines.append(f"  - {dim}")
    return lines


def _write_scope_level_aliases(level_001: Path, level_002: Path, summary_root: Path) -> None:
    write_latest_alias(level_001 / "README.latest.txt", summary_root, "README.latest.txt")
    write_latest_alias(level_001 / "story_index.latest.txt", summary_root, "story_index.latest.txt")
    write_latest_alias(level_001, summary_root, "level_001.latest")
    write_latest_alias(level_002, summary_root, "level_002.latest")
    level_001_interactive = level_001 / "interactive"
    level_001_static = level_001 / "static"
    level_002_static = level_002 / "static"
    for src_name in [
        "sankey_s01_operational.latest.html",
        "sankey_a_universe_to_scope.latest.html",
        "sankey_b_scope_to_analyzed.latest.html",
        "sankey_s05_reproducibility.latest.html",
        "sankey_repro_by_metric.latest.html",
        "benchmark_status.latest.html",
        "reproducibility_buckets.latest.html",
        "agreement_curve.latest.html",
        "agreement_curve_per_metric.latest.html",
        "coverage_matrix.latest.html",
        "failure_taxonomy.latest.html",
        "filter_selection_by_model.latest.html",
    ]:
        src = level_001_interactive / src_name
        if src.exists() or src.is_symlink():
            write_latest_alias(src, summary_root, src_name)
    for src_name in [
        "cardinality_summary.latest.txt",
        "sankey_s01_operational.latest.jpg",
        "sankey_s01_operational.latest.txt",
        "sankey_a_universe_to_scope.latest.jpg",
        "sankey_a_universe_to_scope.latest.txt",
        "sankey_b_scope_to_analyzed.latest.jpg",
        "sankey_b_scope_to_analyzed.latest.txt",
        "sankey_s05_reproducibility.latest.jpg",
        "sankey_s05_reproducibility.latest.txt",
        "sankey_repro_by_metric.latest.jpg",
        "sankey_repro_by_metric.latest.txt",
        "benchmark_status.latest.jpg",
        "reproducibility_buckets.latest.jpg",
        "agreement_curve.latest.jpg",
        "agreement_curve_per_metric.latest.jpg",
        "coverage_matrix.latest.jpg",
        "failure_taxonomy.latest.jpg",
        "filter_selection_by_model.latest.jpg",
        "failure_reasons.latest.txt",
        "failure_runs.latest.csv",
    ]:
        src = level_001_static / src_name
        if src.exists() or src.is_symlink():
            write_latest_alias(src, summary_root, src_name)
    write_latest_alias(level_001 / "reproduce.latest.sh", summary_root, "reproduce.latest.sh")
    write_latest_alias(level_001 / "reproduce.latest.sh", summary_root, "reproduce.sh")
    for src_name in [
        "benchmark_summary.latest.csv",
        "run_inventory.latest.csv",
        "reproducibility_rows.latest.csv",
        "prioritized_breakdowns.latest.csv",
        "off_story_summary.latest.csv",
        "run_multiplicity_summary.latest.csv",
    ]:
        src = level_002_static / src_name
        if src.exists() or src.is_symlink():
            write_latest_alias(src, summary_root, src_name)
    for src_name in [
        "prioritized_breakdowns.latest.txt",
        "prioritized_examples.latest",
        "off_story_summary.latest.txt",
        "run_multiplicity_summary.latest.txt",
    ]:
        src = level_002_static / src_name if src_name.endswith(".txt") else level_002 / src_name
        if src.exists() or src.is_symlink():
            write_latest_alias(src, summary_root, src_name)
    for src_name in [
        "prioritized_breakdowns.latest.json",
        "off_story_summary.latest.json",
        "run_multiplicity_summary.latest.json",
    ]:
        src = level_002 / "machine" / src_name
        if src.exists() or src.is_symlink():
            write_latest_alias(src, summary_root, src_name)

    machine_csv = level_002 / "breakdowns" / "by_machine_host" / "index.latest.csv"
    if machine_csv.exists() or machine_csv.is_symlink():
        write_latest_alias(machine_csv, summary_root, "machine_summary.latest.csv")


def _render_breakdown_scopes(
    *,
    enriched_rows: list[dict[str, Any]],
    all_repro_rows: list[dict[str, Any]],
    filter_inventory_rows: list[dict[str, Any]],
    filter_inventory_json: Path | None,
    index_fpath: Path,
    breakdown_dims: list[str],
    level_002: Path,
    max_items_per_breakdown: int,
    include_values_by_dim: dict[str, list[str]] | None = None,
) -> None:
    breakdowns_root = level_002 / "breakdowns"
    breakdowns_root.mkdir(parents=True, exist_ok=True)
    repro_keyed = {
        (str(row.get("experiment_name")), str(row.get("run_entry"))): row
        for row in all_repro_rows
        if row.get("experiment_name") and row.get("run_entry")
    }
    manifest_rows = []
    for dim in breakdown_dims:
        value_counts = Counter(str(row.get(dim) or "unknown") for row in enriched_rows)
        dim_root = breakdowns_root / f"by_{dim}"
        dim_root.mkdir(parents=True, exist_ok=True)
        top_values = [value for value, _ in value_counts.most_common(max_items_per_breakdown)]
        extra_values = [
            str(value)
            for value in (include_values_by_dim or {}).get(dim, [])
            if str(value) not in top_values
        ]
        top_values.extend(extra_values)
        summary_rows = _summarize_by_dimension(enriched_rows, dimension=dim, repro_keyed=repro_keyed)
        table_artifacts = _write_table_artifacts(summary_rows, dim_root / f"index_{slugify(dim)}")
        for kind in ["json", "csv", "txt"]:
            write_latest_alias(Path(table_artifacts[kind]), dim_root, f"index.latest.{kind}")
        for value in top_values:
            child_rows = [row for row in enriched_rows if str(row.get(dim) or "unknown") == value]
            child_repro = [
                row
                for row in all_repro_rows
                if (str(row.get("experiment_name")), str(row.get("run_entry"))) in {
                    (str(item.get("experiment_name")), str(item.get("run_entry"))) for item in child_rows
                }
            ]
            child_root = dim_root / slugify(value)
            _render_scope_summary(
                scope_kind=dim,
                scope_value=value,
                scope_rows=child_rows,
                repro_rows=child_repro,
                filter_inventory_rows=filter_inventory_rows,
                filter_inventory_json=filter_inventory_json,
                index_fpath=index_fpath,
                summary_root=child_root,
                breakdown_dims=[],
                max_items_per_breakdown=max_items_per_breakdown,
                include_visuals=False,
            )
            manifest_rows.append(
                {
                    "breakdown": dim,
                    "value": value,
                    "n_jobs": len(child_rows),
                    "summary_root": str(child_root),
                }
            )
    manifest_fpath = breakdowns_root / "manifest.json"
    _write_json(manifest_rows, manifest_fpath)
    write_latest_alias(manifest_fpath, breakdowns_root, "manifest.latest.json")


def _bucket_metric_delta(max_delta: float | None) -> str:
    if max_delta is None:
        return "not_available"
    if max_delta == 0.0:
        return "exact_match"
    if max_delta <= 0.001:
        return "tiny_drift_0.001"
    if max_delta <= 0.01:
        return "small_drift_0.01"
    return "large_drift"


def _expand_repro_rows_by_metric(
    repro_rows: list[dict[str, Any]],
    enriched_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched_lookup = {
        (str(r.get("experiment_name")), str(r.get("run_entry"))): r
        for r in enriched_rows
    }
    expanded = []
    for row in repro_rows:
        deltas = row.get("official_runlevel_metric_max_deltas") or {}
        if isinstance(deltas, str):
            try:
                deltas = json.loads(deltas)
            except Exception:
                deltas = {}
        metrics = row.get("core_metrics") or list(deltas.keys())
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except Exception:
                metrics = [metrics] if metrics else []
        key = (str(row.get("experiment_name")), str(row.get("run_entry")))
        parent = enriched_lookup.get(key)
        for metric in (metrics or ["unknown"]):
            max_delta = deltas.get(metric)
            expanded.append({
                "group": str((parent or {}).get("benchmark") or "unknown"),
                "metric": str(metric),
                "drift_bucket": _bucket_metric_delta(max_delta),
            })
    return expanded


def _build_repro_sankey_rows_at_tol(
    repro_rows: list[dict[str, Any]],
    enriched_rows: list[dict[str, Any]],
    agree_field: str,
) -> list[dict[str, Any]]:
    enriched_lookup = {
        (str(r.get("experiment_name")), str(r.get("run_entry"))): r
        for r in enriched_rows
    }
    rows = []
    for row in repro_rows:
        key = (str(row.get("experiment_name")), str(row.get("run_entry")))
        parent = enriched_lookup.get(key)
        agree_val = row.get(agree_field)
        agree = float(agree_val) if agree_val is not None and agree_val != "" else None
        rows.append({
            "group": str((parent or {}).get("benchmark") or "unknown"),
            "repeatability": str(row.get("repeat_diagnosis") or "unknown"),
            "agreement": _bucket_agreement(agree),
            "diagnosis": str(row.get("official_diagnosis") or "unknown"),
        })
    return rows


_FAILURE_CATEGORIES: dict[str, tuple[str, str]] = {
    # failure_reason -> (category_key, category_label)
    "truncated_or_incomplete_runtime": ("hardware_timeout", "Hardware / Compute Timeout"),
    "remote_dataset_download_failure": ("data_access", "Data Access Barrier"),
    "gated_dataset_access": ("data_access", "Data Access Barrier"),
    "missing_dataset_or_cached_artifact": ("data_access", "Data Access Barrier"),
    "missing_math_dataset": ("missing_infrastructure", "Missing Special Infrastructure"),
    "missing_openai_annotation_credentials": ("missing_infrastructure", "Missing Special Infrastructure"),
    "missing_runtime_log": ("unknown", "Unknown / Other"),
    "unknown_failure": ("unknown", "Unknown / Other"),
}
_FAILURE_CATEGORY_ORDER = [
    "hardware_timeout",
    "data_access",
    "missing_infrastructure",
    "unknown",
]
_FAILURE_CATEGORY_LABELS = {
    "hardware_timeout": "Hardware / Compute Timeout",
    "data_access": "Data Access Barrier",
    "missing_infrastructure": "Missing Special Infrastructure",
    "unknown": "Unknown / Other",
}


def _write_agreement_curve_plot(
    repro_rows: list[dict[str, Any]],
    enriched_rows: list[dict[str, Any]],
    stem: Path,
    title: str,
    machine_dpath: Path | None = None,
    interactive_dpath: Path | None = None,
    static_dpath: Path | None = None,
    scope_title: str | None = None,
) -> dict[str, str | None]:
    """Line chart: x=abs_tol (log), y=instance agree_ratio, one line per analyzed run."""
    bench_lookup = {
        (str(r.get("experiment_name")), str(r.get("run_entry"))): str(r.get("benchmark") or "unknown")
        for r in enriched_rows
    }
    meta_lookup = {
        (str(r.get("experiment_name")), str(r.get("run_entry"))): r
        for r in enriched_rows
    }
    def _clean_value(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.lower() in {"unknown", "none", "nan"}:
            return ""
        return text

    def _rowwise_cardinality(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> int:
        values: set[str] = set()
        for row in rows:
            resolved = ""
            for key in keys:
                resolved = _clean_value(row.get(key))
                if resolved:
                    break
            if resolved:
                values.add(resolved)
        return len(values)

    curve_data: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for row in repro_rows:
        key = (str(row.get("experiment_name")), str(row.get("run_entry")))
        bench = bench_lookup.get(key, "unknown")
        curve = row.get("official_instance_agree_curve") or []
        if not curve:
            continue
        curve_rows.append(row)
        run_label = str(row.get("run_spec_name") or row.get("run_entry") or "unknown")
        for pt in curve:
            curve_data.append({
                "benchmark": bench,
                "run": run_label,
                "abs_tol": pt["abs_tol"],
                "agree_ratio": pt["agree_ratio"],
            })
    contributing_rows = [meta_lookup.get((str(row.get("experiment_name")), str(row.get("run_entry")))) for row in curve_rows]
    contributing_rows = [row for row in contributing_rows if row is not None]
    n_runs = len({(str(row.get("experiment_name")), str(row.get("run_entry"))) for row in curve_rows})
    n_models = _rowwise_cardinality(contributing_rows, ("model",))
    n_scenarios = _rowwise_cardinality(contributing_rows, ("scenario", "benchmark", "suite"))
    title_text = title
    if scope_title is not None:
        title_text = (
            "Agreement Rate vs Tolerance (instance-level; "
            f"n_runs={n_runs}, n_models={n_models}, n_scenarios={n_scenarios}): {scope_title}"
        )

    if machine_dpath is not None:
        machine_dpath.mkdir(parents=True, exist_ok=True)
        json_fpath = (machine_dpath / stem.name).with_suffix(".json")
    else:
        json_fpath = stem.with_suffix(".json")
    _interactive = interactive_dpath if interactive_dpath is not None else stem.parent
    _static = static_dpath if static_dpath is not None else stem.parent
    _interactive.mkdir(parents=True, exist_ok=True)
    _static.mkdir(parents=True, exist_ok=True)
    html_fpath = (_interactive / stem.name).with_suffix(".html")
    jpg_fpath = (_static / stem.name).with_suffix(".jpg")
    _write_json(curve_data, json_fpath)

    html_out = None
    jpg_out = None
    plotly_error = None
    if os.environ.get("HELM_AUDIT_SKIP_PLOTLY", "") in {"1", "true", "yes"}:
        plotly_error = "skipped by configuration"
    elif not curve_data:
        plotly_error = "no agreement curve data available"
    else:
        try:
            configure_plotly_chrome()
            import plotly.graph_objects as go

            # Assign a color per benchmark
            benchmarks = sorted(set(d["benchmark"] for d in curve_data))
            palette = [
                "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
            ]
            bench_color = {b: palette[i % len(palette)] for i, b in enumerate(benchmarks)}

            fig = go.Figure()
            seen_benchmarks: set[str] = set()
            for row in repro_rows:
                key = (str(row.get("experiment_name")), str(row.get("run_entry")))
                bench = bench_lookup.get(key, "unknown")
                curve = row.get("official_instance_agree_curve") or []
                if not curve:
                    continue
                run_label = str(row.get("run_spec_name") or row.get("run_entry") or "unknown")
                tols = [max(pt["abs_tol"], 1e-13) for pt in curve]  # avoid log(0)
                ratios = [pt["agree_ratio"] for pt in curve]
                show_legend = bench not in seen_benchmarks
                seen_benchmarks.add(bench)
                fig.add_trace(go.Scatter(
                    x=tols,
                    y=ratios,
                    mode="lines+markers",
                    name=bench,
                    legendgroup=bench,
                    showlegend=show_legend,
                    line={"color": bench_color[bench], "width": 1.5},
                    marker={"size": 5},
                    opacity=0.75,
                    hovertemplate=(
                        f"<b>{bench}</b><br>"
                        "abs_tol=%{x:.2e}<br>"
                        "agree_ratio=%{y:.3f}<br>"
                        f"run={run_label[:60]}<extra></extra>"
                    ),
                ))
            fig.update_layout(
                title=title_text,
                xaxis={"title": "abs_tol (tolerance on |official - local|)", "type": "log"},
                yaxis={"title": "Fraction of Instances Agreeing", "range": [0, 1.05]},
                legend={"title": "Benchmark"},
                hovermode="closest",
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                static_width = min(max(1200, 96 * max(len(benchmarks), 1)), 1800)
                static_height = 880
                fig.update_layout(
                    width=static_width,
                    height=static_height,
                    margin={"t": 100, "b": 180, "l": 70, "r": 50},
                    legend={
                        "title": "Benchmark",
                        "orientation": "h",
                        "x": 0,
                        "xanchor": "left",
                        "y": -0.24,
                        "yanchor": "top",
                        "font": {"size": 9},
                    },
                )
                fig.write_image(str(jpg_fpath), width=static_width, height=static_height, scale=1.0)
                jpg_out = str(jpg_fpath)
        except Exception as ex:
            plotly_error = f"unable to write agreement curve: {ex!r}"

    return {"json": str(json_fpath), "html": html_out, "jpg": jpg_out, "plotly_error": plotly_error}


def _write_per_metric_agreement_plot(
    repro_rows: list[dict[str, Any]],
    enriched_rows: list[dict[str, Any]],
    stem: Path,
    title: str,
    machine_dpath: Path | None = None,
    interactive_dpath: Path | None = None,
    static_dpath: Path | None = None,
) -> dict[str, str | None]:
    """Per-metric agreement curves: one plot per metric showing agreement across all runs."""
    bench_lookup = {
        (str(r.get("experiment_name")), str(r.get("run_entry"))): str(r.get("benchmark") or "unknown")
        for r in enriched_rows
    }

    # Collect per-metric data: metric -> [(abs_tol, agree_ratio, run, benchmark), ...]
    metrics_data: dict[str, list[dict[str, Any]]] = {}
    for row in repro_rows:
        key = (str(row.get("experiment_name")), str(row.get("run_entry")))
        bench = bench_lookup.get(key, "unknown")
        per_metric = row.get("official_per_metric_agreement") or {}
        run_label = str(row.get("run_spec_name") or row.get("run_entry") or "unknown")

        for metric, curve_points in per_metric.items():
            if metric not in metrics_data:
                metrics_data[metric] = []
            for pt in (curve_points or []):
                metrics_data[metric].append({
                    "metric": metric,
                    "benchmark": bench,
                    "run": run_label,
                    "abs_tol": pt.get("abs_tol", 0),
                    "agree_ratio": pt.get("agree_ratio", 0),
                })

    if machine_dpath is not None:
        machine_dpath.mkdir(parents=True, exist_ok=True)
        json_fpath = (machine_dpath / stem.name).with_suffix(".json")
    else:
        json_fpath = stem.with_suffix(".json")
    _interactive = interactive_dpath if interactive_dpath is not None else stem.parent
    _static = static_dpath if static_dpath is not None else stem.parent
    _interactive.mkdir(parents=True, exist_ok=True)
    _static.mkdir(parents=True, exist_ok=True)
    html_fpath = (_interactive / stem.name).with_suffix(".html")
    jpg_fpath = (_static / stem.name).with_suffix(".jpg")
    _write_json(metrics_data, json_fpath)

    html_out = None
    jpg_out = None
    plotly_error = None
    if os.environ.get("HELM_AUDIT_SKIP_PLOTLY", "") in {"1", "true", "yes"}:
        plotly_error = "skipped by configuration"
    elif not metrics_data:
        plotly_error = "no per-metric agreement data available"
    else:
        try:
            configure_plotly_chrome()
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots

            metrics = sorted(metrics_data.keys())
            n_cols = min(3, len(metrics))
            n_rows = (len(metrics) + n_cols - 1) // n_cols

            # Assign a color per benchmark
            all_benchmarks = sorted(set(
                d["benchmark"]
                for metric_pts in metrics_data.values()
                for d in metric_pts
            ))
            palette = [
                "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
            ]
            bench_color = {b: palette[i % len(palette)] for i, b in enumerate(all_benchmarks)}

            fig = make_subplots(
                rows=n_rows,
                cols=n_cols,
                subplot_titles=metrics,
                specs=[[{"secondary_y": False} for _ in range(n_cols)] for _ in range(n_rows)],
            )

            seen_benchmarks: set[str] = set()
            for metric_idx, metric in enumerate(metrics):
                row_idx = (metric_idx // n_cols) + 1
                col_idx = (metric_idx % n_cols) + 1
                # curve_pts = metrics_data[metric]

                for row in repro_rows:
                    key = (str(row.get("experiment_name")), str(row.get("run_entry")))
                    bench = bench_lookup.get(key, "unknown")
                    run_label = str(row.get("run_spec_name") or row.get("run_entry") or "unknown")
                    per_metric = row.get("official_per_metric_agreement") or {}
                    curve = per_metric.get(metric) or []
                    if not curve:
                        continue

                    tols = [max(pt.get("abs_tol", 1e-13), 1e-13) for pt in curve]
                    ratios = [pt.get("agree_ratio", 0) for pt in curve]
                    show_legend = bench not in seen_benchmarks
                    seen_benchmarks.add(bench)

                    fig.add_trace(
                        go.Scatter(
                            x=tols,
                            y=ratios,
                            mode="lines+markers",
                            name=bench,
                            legendgroup=bench,
                            showlegend=show_legend,
                            line={"color": bench_color[bench], "width": 1.5},
                            marker={"size": 4},
                            opacity=0.7,
                            hovertemplate=(
                                f"<b>{bench}</b><br>"
                                "abs_tol=%{x:.2e}<br>"
                                "agree_ratio=%{y:.3f}<br>"
                                f"metric={metric}<br>"
                                f"run={run_label[:50]}<extra></extra>"
                            ),
                        ),
                        row=row_idx,
                        col=col_idx,
                    )

            # Update axes
            for metric_idx, metric in enumerate(metrics):
                row_idx = (metric_idx // n_cols) + 1
                col_idx = (metric_idx % n_cols) + 1
                fig.update_xaxes(title_text="abs_tol", type="log", row=row_idx, col=col_idx)
                fig.update_yaxes(title_text="agreement", range=[0, 1.05], row=row_idx, col=col_idx)

            fig.update_layout(
                title=title,
                height=max(400, 350 * n_rows),
                showlegend=True,
                hovermode="closest",
                legend={"title": "Benchmark"},
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                static_width = min(max(1100, 420 * n_cols), 1600)
                static_height = min(max(520, 320 * n_rows), 1200)
                fig.write_image(str(jpg_fpath), width=static_width, height=static_height, scale=1.0)
                jpg_out = str(jpg_fpath)
        except Exception as ex:
            plotly_error = f"unable to write per-metric agreement: {ex!r}"

    return {"json": str(json_fpath), "html": html_out, "jpg": jpg_out, "plotly_error": plotly_error}


def _write_coverage_matrix_plot(
    enriched_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
    stem: Path,
    title: str,
    machine_dpath: Path | None = None,
    interactive_dpath: Path | None = None,
    static_dpath: Path | None = None,
) -> dict[str, str | None]:
    """Heatmap: rows=model, cols=benchmark, color=best status for that cell."""
    # Status levels (higher = better)
    STATUS_LEVEL = {
        "all_failed": 0,
        "completed_not_analyzed": 1,
        "analyzed_low": 2,
        "analyzed_moderate": 3,
        "analyzed_high": 4,
        "analyzed_exact": 5,
    }
    STATUS_LABEL = {
        0: "all failed",
        1: "completed, not yet analyzed",
        2: "analyzed: low agreement (<80%)",
        3: "analyzed: moderate agreement (80-95%)",
        4: "analyzed: high agreement (95%+)",
        5: "analyzed: exact / near-exact",
    }
    repro_keyed = {
        (str(r.get("experiment_name")), str(r.get("run_entry"))): r
        for r in repro_rows
    }
    # Build best-status per (model, benchmark) cell
    cell_status: dict[tuple[str, str], int] = {}
    cell_counts: dict[tuple[str, str], dict[str, int]] = {}
    for row in enriched_rows:
        model = str(row.get("model") or "unknown")
        bench = str(row.get("benchmark") or "unknown")
        key = (model, bench)
        counts = cell_counts.setdefault(key, {"total": 0, "completed": 0, "analyzed": 0, "failed": 0})
        counts["total"] += 1
        if row.get("completed_with_run_artifacts"):
            counts["completed"] += 1
            rkey = (str(row.get("experiment_name")), str(row.get("run_entry")))
            repro = repro_keyed.get(rkey)
            if repro:
                counts["analyzed"] += 1
                bucket = repro.get("official_instance_agree_bucket") or ""
                if "exact" in bucket:
                    level = STATUS_LEVEL["analyzed_exact"]
                elif "high" in bucket:
                    level = STATUS_LEVEL["analyzed_high"]
                elif "moderate" in bucket:
                    level = STATUS_LEVEL["analyzed_moderate"]
                else:
                    level = STATUS_LEVEL["analyzed_low"]
            else:
                level = STATUS_LEVEL["completed_not_analyzed"]
        else:
            counts["failed"] += 1
            level = STATUS_LEVEL["all_failed"]
        cell_status[key] = max(cell_status.get(key, -1), level)

    models = sorted({m for m, _ in cell_status})
    benchmarks = sorted({b for _, b in cell_status})
    matrix: list[list[int]] = []
    hover_matrix: list[list[str]] = []
    for model in models:
        row_vals = []
        row_hover = []
        for bench in benchmarks:
            key = (model, bench)
            level = cell_status.get(key, -1)
            counts = cell_counts.get(key, {})
            row_vals.append(level)
            if level == -1:
                row_hover.append("not attempted")
            else:
                label = STATUS_LABEL.get(level, "unknown")
                total = counts.get("total", 0)
                completed = counts.get("completed", 0)
                analyzed = counts.get("analyzed", 0)
                row_hover.append(
                    f"{label}<br>total={total} completed={completed} analyzed={analyzed}"
                )
        matrix.append(row_vals)
        hover_matrix.append(row_hover)

    matrix_data = {
        "models": models,
        "benchmarks": benchmarks,
        "matrix": matrix,
        "status_level_meanings": STATUS_LABEL,
    }
    if machine_dpath is not None:
        machine_dpath.mkdir(parents=True, exist_ok=True)
        json_fpath = (machine_dpath / stem.name).with_suffix(".json")
    else:
        json_fpath = stem.with_suffix(".json")
    _interactive = interactive_dpath if interactive_dpath is not None else stem.parent
    _static = static_dpath if static_dpath is not None else stem.parent
    _interactive.mkdir(parents=True, exist_ok=True)
    _static.mkdir(parents=True, exist_ok=True)
    html_fpath = (_interactive / stem.name).with_suffix(".html")
    jpg_fpath = (_static / stem.name).with_suffix(".jpg")
    _write_json(matrix_data, json_fpath)

    html_out = None
    jpg_out = None
    plotly_error = None
    if os.environ.get("HELM_AUDIT_SKIP_PLOTLY", "") in {"1", "true", "yes"}:
        plotly_error = "skipped by configuration"
    elif not models or not benchmarks:
        plotly_error = "no data for coverage matrix"
    else:
        try:
            configure_plotly_chrome()
            import plotly.graph_objects as go

            colorscale = [
                [0.0 / 6, "#f0f0f0"],   # -1 not attempted (grey)
                [1.0 / 6, "#d62728"],    # 0 all_failed (red)
                [2.0 / 6, "#ffdd57"],    # 1 completed not analyzed (yellow)
                [3.0 / 6, "#ff7f0e"],    # 2 analyzed low (orange)
                [4.0 / 6, "#aec7e8"],    # 3 analyzed moderate (light blue)
                [5.0 / 6, "#1f77b4"],    # 4 analyzed high (blue)
                [6.0 / 6, "#2ca02c"],    # 5 analyzed exact (green)
            ]
            fig = go.Figure(go.Heatmap(
                z=matrix,
                x=benchmarks,
                y=models,
                text=hover_matrix,
                hovertemplate="%{y} × %{x}<br>%{text}<extra></extra>",
                colorscale=colorscale,
                zmin=-1,
                zmax=5,
                colorbar={
                    "title": "Status",
                    "tickvals": [-1, 0, 1, 2, 3, 4, 5],
                    "ticktext": [
                        "not attempted",
                        "all failed",
                        "completed (not analyzed)",
                        "analyzed: low agreement",
                        "analyzed: moderate",
                        "analyzed: high",
                        "analyzed: exact/near-exact",
                    ],
                },
            ))
            fig.update_layout(
                title=title,
                xaxis={"title": "Benchmark", "tickangle": -45},
                yaxis={"title": "Model"},
                height=max(400, 60 + 40 * len(models)),
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                static_benchmark_labels = [_abbreviate_label(value) for value in benchmarks]
                static_model_labels = [_abbreviate_label(value) for value in models]
                benchmark_angle = 90 if len(benchmarks) > 40 else 75 if len(benchmarks) > 25 else 60 if len(benchmarks) > 12 else -45
                fig.update_xaxes(
                    tickmode="array",
                    tickvals=benchmarks,
                    ticktext=static_benchmark_labels,
                    tickangle=benchmark_angle,
                    automargin=True,
                )
                fig.update_yaxes(
                    tickmode="array",
                    tickvals=models,
                    ticktext=static_model_labels,
                    automargin=True,
                )
                static_width = min(max(1100, 22 * max(len(benchmarks), 1) + 12 * max((len(label) for label in benchmarks), default=0)), 1800)
                static_height = min(max(520, 18 * max(len(models), 1) + 220), 1200)
                fig.write_image(str(jpg_fpath), width=static_width, height=static_height, scale=1.0)
                jpg_out = str(jpg_fpath)
        except Exception as ex:
            plotly_error = f"unable to write coverage matrix: {ex!r}"

    return {"json": str(json_fpath), "html": html_out, "jpg": jpg_out, "plotly_error": plotly_error}


def _write_failure_taxonomy_plot(
    failed_rows: list[dict[str, Any]],
    stem: Path,
    title: str,
    machine_dpath: Path | None = None,
    interactive_dpath: Path | None = None,
    static_dpath: Path | None = None,
) -> dict[str, str | None]:
    """Stacked bar: x=benchmark, color=failure root-cause category, y=job count."""
    from collections import defaultdict

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in failed_rows:
        bench = str(row.get("benchmark") or "unknown")
        reason = str(row.get("failure_reason") or "unknown_failure")
        cat_key, _ = _FAILURE_CATEGORIES.get(reason, ("unknown", "Unknown / Other"))
        counts[(bench, cat_key)] += 1

    bar_rows: list[dict[str, Any]] = [
        {"benchmark": bench, "category": cat_key, "label": _FAILURE_CATEGORY_LABELS[cat_key], "count": count}
        for (bench, cat_key), count in sorted(counts.items())
    ]
    # Total failures per benchmark for sort order
    bench_totals: dict[str, int] = defaultdict(int)
    for r in bar_rows:
        bench_totals[r["benchmark"]] += r["count"]
    bench_order = sorted(bench_totals, key=lambda b: -bench_totals[b])

    if machine_dpath is not None:
        machine_dpath.mkdir(parents=True, exist_ok=True)
        json_fpath = (machine_dpath / stem.name).with_suffix(".json")
    else:
        json_fpath = stem.with_suffix(".json")
    _interactive = interactive_dpath if interactive_dpath is not None else stem.parent
    _static = static_dpath if static_dpath is not None else stem.parent
    _interactive.mkdir(parents=True, exist_ok=True)
    _static.mkdir(parents=True, exist_ok=True)
    html_fpath = (_interactive / stem.name).with_suffix(".html")
    jpg_fpath = (_static / stem.name).with_suffix(".jpg")
    _write_json(bar_rows, json_fpath)

    html_out = None
    jpg_out = None
    plotly_error = None
    count_label = _bar_count_label("benchmark", len(bench_order), axis_title="Benchmark")
    if os.environ.get("HELM_AUDIT_SKIP_PLOTLY", "") in {"1", "true", "yes"}:
        plotly_error = "skipped by configuration"
    elif not bar_rows:
        plotly_error = "no failure data"
    else:
        try:
            configure_plotly_chrome()
            import plotly.graph_objects as go

            cat_colors = {
                "hardware_timeout": "#d62728",
                "data_access": "#ff7f0e",
                "missing_infrastructure": "#9467bd",
                "unknown": "#7f7f7f",
            }
            fig = go.Figure()
            for cat_key in _FAILURE_CATEGORY_ORDER:
                cat_label = _FAILURE_CATEGORY_LABELS[cat_key]
                y_vals = [
                    sum(r["count"] for r in bar_rows if r["benchmark"] == b and r["category"] == cat_key)
                    for b in bench_order
                ]
                fig.add_trace(go.Bar(
                    name=cat_label,
                    x=bench_order,
                    y=y_vals,
                    marker_color=cat_colors[cat_key],
                    hovertemplate=f"<b>{cat_label}</b><br>benchmark=%{{x}}<br>count=%{{y}}<extra></extra>",
                ))
            fig.update_layout(
                title=title,
                barmode="stack",
                xaxis={"title": count_label, "tickangle": -45, "categoryorder": "array", "categoryarray": bench_order},
                yaxis={"title": "Failed Job Count"},
                legend={"title": "Root Cause Category"},
            )
            fig.update_xaxes(
                tickmode="array",
                tickvals=bench_order,
                ticktext=bench_order,
                tickangle=-45,
                automargin=True,
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                static_width, static_height = _compact_bar_figure_size(bench_order)
                fig.update_layout(width=static_width, height=static_height, margin={"b": min(max(120, 8 * max((len(v) for v in bench_order), default=0)), 220), "t": 80, "l": 70, "r": 30})
                fig.update_xaxes(
                    ticktext=[_abbreviate_label(value) for value in bench_order],
                    tickangle=_bar_tickangle(len(bench_order)),
                    tickfont={"size": 8 if len(bench_order) > 12 else 10},
                )
                fig.write_image(str(jpg_fpath), scale=1.0)
                jpg_out = str(jpg_fpath)
        except Exception as ex:
            plotly_error = f"unable to write failure taxonomy: {ex!r}"

    return {"json": str(json_fpath), "html": html_out, "jpg": jpg_out, "plotly_error": plotly_error}


def _write_reproduce_sh(
    fpath: Path,
    scope_kind: str,
    scope_value: str | None,
    index_path: Path | None = None,
    filter_inventory_json: Path | None = None,
) -> None:
    cmd = 'PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" -m eval_audit.workflows.build_reports_summary'
    if scope_kind not in ("all_results", None) and scope_value:
        cmd += f" --experiment-name {scope_value}"
    if index_path is not None:
        cmd += f" --index-fpath {shlex.quote(str(index_path))}"
    if filter_inventory_json is not None:
        cmd += f" --filter-inventory-json {shlex.quote(str(filter_inventory_json))}"
    lines = [
        "#!/usr/bin/env bash",
        "# Regenerate this summary report from the current index and analysis data.",
        f"# scope: {scope_kind}" + (f" / {scope_value}" if scope_value else ""),
        "set -euo pipefail",
        *portable_repo_root_lines(),
        'cd "$REPO_ROOT"',
        cmd,
    ]
    fpath.write_text("\n".join(lines) + "\n")
    logger.debug(f'Write to 💻: {rich_link(fpath)}')
    fpath.chmod(0o755)


def _render_scope_summary(
    *,
    scope_kind: str,
    scope_value: str | None,
    scope_rows: list[dict[str, Any]],
    repro_rows: list[dict[str, Any]],
    filter_inventory_rows: list[dict[str, Any]],
    filter_inventory_json: Path | None,
    index_fpath: Path,
    summary_root: Path,
    breakdown_dims: list[str],
    max_items_per_breakdown: int,
    include_visuals: bool = True,
) -> None:
    if not scope_rows:
        return

    generated_utc, history_dpath = stamped_history_dir(summary_root)
    version_dpath = history_dpath / generated_utc
    level_001 = version_dpath / "level_001"
    level_002 = version_dpath / "level_002"
    level_001.mkdir(parents=True, exist_ok=True)
    level_002.mkdir(parents=True, exist_ok=True)
    level_001_machine = level_001 / "machine"
    level_001_interactive = level_001 / "interactive"
    level_001_static = level_001 / "static"
    level_002_machine = level_002 / "machine"
    level_002_static = level_002 / "static"
    for d in [level_001_machine, level_001_interactive, level_001_static, level_002_machine, level_002_static]:
        d.mkdir(parents=True, exist_ok=True)

    alt_tol_dpath = level_001 / "alt_tolerances"
    alt_tol_machine = alt_tol_dpath / "machine"
    alt_tol_interactive = alt_tol_dpath / "interactive"
    alt_tol_static = alt_tol_dpath / "static"
    for d in [alt_tol_machine, alt_tol_interactive, alt_tol_static]:
        d.mkdir(parents=True, exist_ok=True)

    repro_keyed = {
        (str(row.get("experiment_name")), str(row.get("run_entry"))): row
        for row in repro_rows
        if row.get("experiment_name") and row.get("run_entry")
    }
    filter_lookup = _filter_inventory_lookup_by_run_entry(filter_inventory_rows)
    registry_lookup = local_model_registry_by_name()

    enriched_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    for row in scope_rows:
        enriched = dict(row)
        enriched["logical_run_key"] = str(row.get("run_entry") or "")
        enriched.update(_resolve_attempt_identity(row))
        filter_row = filter_lookup.get(str(row.get("run_entry") or ""))
        if filter_row is not None:
            for src_key, dst_key in [
                ("scenario", "scenario"),
                ("dataset", "dataset"),
                ("setting", "setting"),
                ("selection_status", "selection_status"),
                ("candidate_pool", "candidate_pool"),
            ]:
                if dst_key not in enriched or not enriched.get(dst_key):
                    enriched[dst_key] = filter_row.get(src_key)
        enriched.update(
            _storyline_metadata_for_model(
                model=_clean_optional_text(enriched.get("model")),
                registry_lookup=registry_lookup,
                filter_row=filter_row,
            )
        )
        completed = _is_truthy_text(row.get("has_run_spec"))
        enriched["completed_with_run_artifacts"] = completed
        enriched["lifecycle_stage"] = "completed_with_run_artifacts" if completed else "failed_or_incomplete"
        key = (str(row.get("experiment_name")), str(row.get("run_entry")))
        repro = repro_keyed.get(key)
        if repro is not None:
            enriched.update(
                {
                    "repro_report_dir": repro.get("report_dir"),
                    "official_instance_agree_0": repro.get("official_instance_agree_0"),
                    "official_instance_agree_bucket": repro.get("official_instance_agree_bucket"),
                    "official_diagnosis": repro.get("official_diagnosis"),
                    "repeat_diagnosis": repro.get("repeat_diagnosis"),
                }
            )
        elif completed:
            enriched["official_instance_agree_bucket"] = "completed_not_yet_analyzed"
        if not completed:
            failure = _classify_failure(Path(str(row.get("job_dpath"))).expanduser(), row)
            enriched.update(failure)
            failed_rows.append(enriched)
        enriched_rows.append(enriched)

    n_total = len(enriched_rows)
    n_completed = sum(1 for row in enriched_rows if row.get("completed_with_run_artifacts"))
    n_failed = n_total - n_completed
    n_analyzed = len(repro_rows)

    failure_counts = Counter(row.get("failure_reason") or "unknown_failure" for row in failed_rows)
    failure_reason_rows = [
        {"failure_reason": reason, "count": count, "share_of_failed": (count / n_failed) if n_failed else None}
        for reason, count in failure_counts.most_common()
    ]
    repro_bucket_counts = Counter(row.get("official_instance_agree_bucket") or "not_analyzed" for row in repro_rows)
    repro_bucket_rows = [
        {
            "official_instance_agree_bucket": bucket,
            "count": count,
            "share_of_analyzed": (count / n_analyzed) if n_analyzed else None,
        }
        for bucket, count in repro_bucket_counts.most_common()
    ]
    filter_selection_by_model_rows = _build_filter_selection_by_model_rows(filter_inventory_rows)

    benchmark_status_rows = _build_breakdown_rows(enriched_rows, group_key="benchmark", repro_keyed=repro_keyed)
    benchmark_summary = _summarize_by_dimension(enriched_rows, dimension="benchmark", repro_keyed=repro_keyed)
    run_inventory = enriched_rows
    repro_inventory = repro_rows
    off_story_summary = _build_off_story_summary(
        filter_inventory_rows=filter_inventory_rows,
        scope_rows=scope_rows,
        repro_rows=repro_rows,
    )
    run_multiplicity_summary = _build_run_multiplicity_summary(
        filter_inventory_rows=filter_inventory_rows,
        scope_rows=scope_rows,
        repro_rows=repro_rows,
    )
    prioritized_breakdowns_summary = _build_prioritized_breakdown_summary(
        enriched_rows=enriched_rows,
        repro_rows=repro_rows,
        run_multiplicity_summary=run_multiplicity_summary,
        breakdown_dims=breakdown_dims,
        level_002=level_002,
    )
    if breakdown_dims:
        _render_breakdown_scopes(
            enriched_rows=enriched_rows,
            all_repro_rows=repro_rows,
            filter_inventory_rows=filter_inventory_rows,
            filter_inventory_json=filter_inventory_json,
            index_fpath=index_fpath,
            breakdown_dims=breakdown_dims,
            level_002=level_002,
            max_items_per_breakdown=max_items_per_breakdown,
            include_values_by_dim=prioritized_breakdowns_summary.get("include_values_by_dim"),
        )

    operational_sankey_rows = []
    for row in enriched_rows:
        if row.get("completed_with_run_artifacts"):
            outcome = str(row.get("official_instance_agree_bucket") or "completed_not_yet_analyzed")
        else:
            outcome = str(row.get("failure_reason") or "unknown_failure")
        operational_sankey_rows.append(
            {
                "group": str(row.get("benchmark") or "unknown"),
                "lifecycle": str(row.get("lifecycle_stage") or "unknown"),
                "outcome": outcome,
            }
        )

    repro_sankey_rows = []
    for row in repro_rows:
        parent = next(
            (
                item for item in enriched_rows
                if str(item.get("experiment_name")) == str(row.get("experiment_name"))
                and str(item.get("run_entry")) == str(row.get("run_entry"))
            ),
            None,
        )
        repro_sankey_rows.append(
            {
                "group": str((parent or {}).get("benchmark") or "unknown"),
                "repeatability": str(row.get("repeat_diagnosis") or "unknown"),
                "agreement": str(row.get("official_instance_agree_bucket") or "not_analyzed"),
                "diagnosis": str(row.get("official_diagnosis") or "unknown"),
            }
        )

    repro_tol001_rows = _build_repro_sankey_rows_at_tol(repro_rows, enriched_rows, "official_instance_agree_001")
    repro_tol010_rows = _build_repro_sankey_rows_at_tol(repro_rows, enriched_rows, "official_instance_agree_01")
    repro_tol050_rows = _build_repro_sankey_rows_at_tol(repro_rows, enriched_rows, "official_instance_agree_005")
    metric_sankey_rows = _expand_repro_rows_by_metric(repro_rows, enriched_rows)
    # Stage A — Universe -> Scope: pure filter-funnel ending at the
    # selection waist. No tolerance variant (Stage A is independent of
    # reproduction agreement). Replaces the legacy ``filter_to_attempt``
    # row-builder which also reached into post-selection territory.
    universe_to_scope_rows = _build_universe_to_scope_rows(filter_inventory_rows)

    # Stage B — Scope -> Attempt -> Execution -> Analysis -> Reproduction.
    # Source population is filter_inventory rows with selection_status='selected'
    # (the in-scope set). Tolerance variants drive the reproduction-stage
    # waist at different abs_tol values.
    scope_to_analyzed_exact_rows = _build_scope_to_analyzed_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_0",
    )
    scope_to_analyzed_tol001_rows = _build_scope_to_analyzed_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_001",
    )
    scope_to_analyzed_tol010_rows = _build_scope_to_analyzed_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_01",
    )
    scope_to_analyzed_tol050_rows = _build_scope_to_analyzed_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_005",
    )
    # The legacy combined Universe->Reproducible sankey (s04) is intentionally
    # dropped: Stage A and Stage B together carry the same information without
    # the eight-stage cramping that made the combined view unreadable. Anyone
    # reading both sankeys side-by-side recovers the full chain.

    scope_title = _scope_label(scope_kind, scope_value)
    if include_visuals:
        operational_art = emit_sankey_artifacts(
            rows=operational_sankey_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="s01_operational",
            title=f"Executive Operational Summary: {scope_title}",
            stage_defs={
                "group": ["benchmark family or suite"],
                "lifecycle": ["whether the run produced runnable artifacts"],
                "outcome": [
                    "for failed/incomplete runs: failure reason",
                    f"for completed runs: instance-level agreement bucket at abs_tol={CANONICAL_AGREEMENT_TOL:g}",
                    f"  exact_or_near_exact: >=99.9999% of instances agree within abs_tol={CANONICAL_AGREEMENT_TOL:g}",
                    f"  high_agreement_0.95+: >=95% of instances agree within abs_tol={CANONICAL_AGREEMENT_TOL:g}",
                    f"  moderate_agreement_0.80+: >=80% agree within abs_tol={CANONICAL_AGREEMENT_TOL:g}",
                    f"  low_agreement_0.00+: >0% agree within abs_tol={CANONICAL_AGREEMENT_TOL:g}",
                    f"  zero_agreement: no instances agree within abs_tol={CANONICAL_AGREEMENT_TOL:g}",
                ],
            },
            stage_order=[("group", "group"), ("lifecycle", "lifecycle"), ("outcome", "outcome")],
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        )
        repro_art = emit_sankey_artifacts(
            rows=repro_sankey_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="s05_reproducibility",
            title=f"Reproducibility Summary (instance-level, abs_tol={CANONICAL_AGREEMENT_TOL:g} canonical): {scope_title}",
            stage_defs={
                "group": ["benchmark family or suite"],
                "repeatability": ["local repeatability diagnosis (run vs its own repeat)"],
                "agreement": [
                    f"official-vs-local agreement bucket at abs_tol={CANONICAL_AGREEMENT_TOL:g} (canonical)",
                    f"fraction = share of instances where |official_score - local_score| <= {CANONICAL_AGREEMENT_TOL:g}",
                    "  exact_or_near_exact: fraction >= 0.999999",
                    "  high_agreement_0.95+: fraction >= 0.95",
                    "  moderate_agreement_0.80+: fraction >= 0.80",
                    "  low_agreement_0.00+: fraction > 0.0",
                    "  zero_agreement: fraction == 0.0",
                ],
                "diagnosis": ["top-level diagnosis from official-vs-local comparison"],
            },
            stage_order=[
                ("group", "group"),
                ("repeatability", "repeatability"),
                ("agreement", "agreement"),
                ("diagnosis", "diagnosis"),
            ],
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        )
        _repro_stage_order = [
            ("group", "group"),
            ("repeatability", "repeatability"),
            ("agreement", "agreement"),
            ("diagnosis", "diagnosis"),
        ]
        _repro_stage_defs = {
            "group": ["benchmark family or suite"],
            "repeatability": ["local repeatability diagnosis (run vs its own repeat)"],
            "agreement": [
                f"official-vs-local agreement bucket at the abs_tol stated in the title (canonical abs_tol={CANONICAL_AGREEMENT_TOL:g})",
                "fraction = share of instances where |official_score - local_score| <= abs_tol",
                "  exact_or_near_exact: fraction >= 0.999999",
                "  high_agreement_0.95+: fraction >= 0.95",
                "  moderate_agreement_0.80+: fraction >= 0.80",
                "  low_agreement_0.00+: fraction > 0.0",
                "  zero_agreement: fraction == 0.0",
            ],
            "diagnosis": ["top-level diagnosis from official-vs-local comparison"],
        }
        repro_tol001_art = emit_sankey_artifacts(
            rows=repro_tol001_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="repro_tol001",
            title=f"Reproducibility at abs_tol=0.001: {scope_title}",
            stage_defs=_repro_stage_defs,
            stage_order=_repro_stage_order,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        )
        repro_tol010_art = emit_sankey_artifacts(
            rows=repro_tol010_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="repro_tol010",
            title=f"Reproducibility at abs_tol=0.010: {scope_title}",
            stage_defs=_repro_stage_defs,
            stage_order=_repro_stage_order,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        )
        repro_tol050_art = emit_sankey_artifacts(
            rows=repro_tol050_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="repro_tol050",
            title=f"Reproducibility at abs_tol=0.050: {scope_title}",
            stage_defs=_repro_stage_defs,
            stage_order=_repro_stage_order,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        )
        repro_metric_art = emit_sankey_artifacts(
            rows=metric_sankey_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="repro_by_metric",
            title=f"Per-Metric Reproducibility Drift (run-level max |official - local|): {scope_title}",
            stage_defs={
                "group": ["benchmark family or suite"],
                "metric": ["core metric name (e.g. exact_match, f1_score, rouge_l)"],
                "drift_bucket": [
                    "signal: max absolute delta between official and local score across all runs",
                    "  exact_match:      max |official - local| == 0.0  (bit-perfect agreement)",
                    "  tiny_drift_0.001: max |official - local| <= 0.001",
                    "  small_drift_0.01: max |official - local| <= 0.01",
                    "  large_drift:      max |official - local|  > 0.01",
                    "  not_available:    metric not present in run-level data",
                ],
            },
            stage_order=[("group", "group"), ("metric", "metric"), ("drift_bucket", "drift_bucket")],
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        )
        # Stage A — Universe -> Scope (no tolerance variant; tolerance is a
        # post-selection concept)
        a_root, a_stage_names, a_stage_defs = _build_universe_to_scope_root()
        empty_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no filter inventory rows available"}
        universe_to_scope_art = emit_sankey_artifacts(
            rows=universe_to_scope_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="a_universe_to_scope",
            title=f"Stage A — Universe → Scope (filter funnel): {scope_title}",
            stage_defs=a_stage_defs,
            stage_order=[],
            root=a_root,
            explicit_stage_names=a_stage_names,
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        ) if universe_to_scope_rows else dict(empty_art)

        # Stage B — Scope -> Attempt -> Execution -> Analysis -> Reproduction
        b_root, b_stage_names, b_stage_defs = _build_scope_to_analyzed_root()
        empty_b_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no in-scope rows available"}
        scope_to_analyzed_art = emit_sankey_artifacts(
            rows=scope_to_analyzed_exact_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="b_scope_to_analyzed",
            title=f"Stage B — Scope → Analyzed at abs_tol=0: {scope_title}",
            stage_defs=b_stage_defs,
            stage_order=[],
            root=b_root,
            explicit_stage_names=b_stage_names,
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        ) if scope_to_analyzed_exact_rows else dict(empty_b_art)
        scope_to_analyzed_tol001_art = emit_sankey_artifacts(
            rows=scope_to_analyzed_tol001_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="b_scope_to_analyzed_tol001",
            title=f"Stage B — Scope → Analyzed at abs_tol=0.001: {scope_title}",
            stage_defs=b_stage_defs,
            stage_order=[],
            root=b_root,
            explicit_stage_names=b_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if scope_to_analyzed_tol001_rows else dict(empty_b_art)
        scope_to_analyzed_tol010_art = emit_sankey_artifacts(
            rows=scope_to_analyzed_tol010_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="b_scope_to_analyzed_tol010",
            title=f"Stage B — Scope → Analyzed at abs_tol=0.010: {scope_title}",
            stage_defs=b_stage_defs,
            stage_order=[],
            root=b_root,
            explicit_stage_names=b_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if scope_to_analyzed_tol010_rows else dict(empty_b_art)
        scope_to_analyzed_tol050_art = emit_sankey_artifacts(
            rows=scope_to_analyzed_tol050_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="b_scope_to_analyzed_tol050",
            title=f"Stage B — Scope → Analyzed at abs_tol=0.050: {scope_title}",
            stage_defs=b_stage_defs,
            stage_order=[],
            root=b_root,
            explicit_stage_names=b_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if scope_to_analyzed_tol050_rows else dict(empty_b_art)
        # Backwards-compatible aliases for the old variable names so the
        # downstream manifest schema (and any callers reading it) keeps
        # working until they migrate to the new keys.
        filter_to_attempt_art = universe_to_scope_art
        attempted_to_repro_art = scope_to_analyzed_art
        attempted_to_repro_tol001_art = scope_to_analyzed_tol001_art
        attempted_to_repro_tol010_art = scope_to_analyzed_tol010_art
        attempted_to_repro_tol050_art = scope_to_analyzed_tol050_art
        # Combined Universe->Reproducible sankey is dropped (see comment above).
        end_to_end_art = dict(empty_art)
        end_to_end_tol001_art = dict(empty_art)
        end_to_end_tol010_art = dict(empty_art)
        end_to_end_tol050_art = dict(empty_art)
    else:
        operational_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        repro_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        repro_tol001_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        repro_tol010_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        repro_tol050_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        repro_metric_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        filter_to_attempt_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        attempted_to_repro_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        attempted_to_repro_tol001_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        attempted_to_repro_tol010_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        attempted_to_repro_tol050_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        end_to_end_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        end_to_end_tol001_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        end_to_end_tol010_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}
        end_to_end_tol050_art = {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": None}

    failure_table = _write_table_artifacts(failed_rows, level_001 / f"failure_runs_{generated_utc}", machine_dpath=level_001_machine, static_dpath=level_001_static)
    failure_reason_table = _write_table_artifacts(failure_reason_rows, level_001 / f"failure_reasons_{generated_utc}", machine_dpath=level_001_machine, static_dpath=level_001_static)
    benchmark_table = _write_table_artifacts(benchmark_summary, level_002 / f"benchmark_summary_{generated_utc}", machine_dpath=level_002_machine, static_dpath=level_002_static)
    run_inventory_table = _write_table_artifacts(run_inventory, level_002 / f"run_inventory_{generated_utc}", machine_dpath=level_002_machine, static_dpath=level_002_static)
    repro_table = _write_table_artifacts(repro_inventory, level_002 / f"reproducibility_rows_{generated_utc}", machine_dpath=level_002_machine, static_dpath=level_002_static)
    off_story_table = _write_structured_summary_artifacts(
        rows=off_story_summary["rows"],
        payload={
            "generated_utc": generated_utc,
            "scope_title": scope_title,
            **off_story_summary,
        },
        txt_lines=_format_off_story_summary_text(
            scope_title=scope_title,
            generated_utc=generated_utc,
            summary=off_story_summary,
        ),
        stem=level_002 / f"off_story_summary_{generated_utc}",
        machine_dpath=level_002_machine,
        static_dpath=level_002_static,
    )
    run_multiplicity_table = _write_structured_summary_artifacts(
        rows=run_multiplicity_summary["rows"],
        payload={
            "generated_utc": generated_utc,
            "scope_title": scope_title,
            **run_multiplicity_summary,
        },
        txt_lines=_format_run_multiplicity_summary_text(
            scope_title=scope_title,
            generated_utc=generated_utc,
            summary=run_multiplicity_summary,
        ),
        stem=level_002 / f"run_multiplicity_summary_{generated_utc}",
        machine_dpath=level_002_machine,
        static_dpath=level_002_static,
    )
    prioritized_breakdowns_table = _write_structured_summary_artifacts(
        rows=prioritized_breakdowns_summary["rows"],
        payload={
            "generated_utc": generated_utc,
            "scope_title": scope_title,
            **prioritized_breakdowns_summary,
        },
        txt_lines=_format_prioritized_breakdown_summary_text(
            scope_title=scope_title,
            generated_utc=generated_utc,
            summary=prioritized_breakdowns_summary,
        ),
        stem=level_002 / f"prioritized_breakdowns_{generated_utc}",
        machine_dpath=level_002_machine,
        static_dpath=level_002_static,
    )
    prioritized_example_repairs = _repair_prioritized_example_reports(
        summary=prioritized_breakdowns_summary,
        index_fpath=index_fpath,
    )
    prioritized_examples_tree = _publish_prioritized_examples_tree(
        level_002=level_002,
        generated_utc=generated_utc,
        summary=prioritized_breakdowns_summary,
        repair_results=prioritized_example_repairs,
    )

    if include_visuals:
        benchmark_plot = _write_plotly_bar(
            rows=benchmark_status_rows,
            x="group_value",
            y="count",
            color="status_bucket",
            title=f"Benchmark Coverage and Analysis Status (analyzed runs use abs_tol={CANONICAL_AGREEMENT_TOL:g}): {scope_title}",
            stem=level_001 / f"benchmark_status_{generated_utc}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
            xaxis_title="Benchmark",
            xaxis_count_key="benchmark",
            yaxis_title="Job Count",
        )
        repro_bucket_plot = _write_plotly_bar(
            rows=repro_bucket_rows,
            x="official_instance_agree_bucket",
            y="count",
            color="official_instance_agree_bucket",
            title=f"Official vs Local Agreement Buckets (instance-level, abs_tol={CANONICAL_AGREEMENT_TOL:g} canonical): {scope_title}",
            stem=level_001 / f"reproducibility_buckets_{generated_utc}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
            xaxis_title="Agreement Bucket",
            xaxis_count_key="official_instance_agree_bucket",
            yaxis_title="Run Count",
        )
        agreement_curve_plot = _write_agreement_curve_plot(
            repro_rows=repro_rows,
            enriched_rows=enriched_rows,
            stem=level_001 / f"agreement_curve_{generated_utc}",
            title="Agreement Rate vs Tolerance (instance-level)",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
            scope_title=scope_title,
        )
        per_metric_agreement_plot = _write_per_metric_agreement_plot(
            repro_rows=repro_rows,
            enriched_rows=enriched_rows,
            stem=level_001 / f"agreement_curve_per_metric_{generated_utc}",
            title=f"Agreement Rate vs Tolerance (per-metric): {scope_title}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        )
        coverage_matrix_plot = _write_coverage_matrix_plot(
            enriched_rows=enriched_rows,
            repro_rows=repro_rows,
            stem=level_001 / f"coverage_matrix_{generated_utc}",
            title=f"Model × Benchmark Coverage and Reproducibility Status: {scope_title}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        )
        failure_taxonomy_plot = _write_failure_taxonomy_plot(
            failed_rows=failed_rows,
            stem=level_001 / f"failure_taxonomy_{generated_utc}",
            title=f"Why Jobs Failed: Root Cause Taxonomy by Benchmark: {scope_title}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        )
        filter_selection_by_model_plot = _write_plotly_bar(
            rows=filter_selection_by_model_rows,
            x="model",
            y="count",
            color="selection_status",
            title=f"Selected vs Excluded Run Specs by Model: {scope_title}",
            stem=level_001 / f"filter_selection_by_model_{generated_utc}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
            xaxis_title="Model",
            xaxis_count_key="model",
            yaxis_title="Run Spec Count",
        )
    else:
        benchmark_plot = {"json": None, "html": None, "jpg": None, "png": None, "plotly_error": None}
        repro_bucket_plot = {"json": None, "html": None, "jpg": None, "png": None, "plotly_error": None}
        agreement_curve_plot = {"json": None, "html": None, "jpg": None, "plotly_error": None}
        per_metric_agreement_plot = {"json": None, "html": None, "jpg": None, "plotly_error": None}
        coverage_matrix_plot = {"json": None, "html": None, "jpg": None, "plotly_error": None}
        failure_taxonomy_plot = {"json": None, "html": None, "jpg": None, "plotly_error": None}
        filter_selection_by_model_plot = {"json": None, "html": None, "jpg": None, "png": None, "plotly_error": None}

    level_001_readme = _build_high_level_readme(
        scope_title=scope_title,
        generated_utc=generated_utc,
        n_total=n_total,
        n_completed=n_completed,
        n_analyzed=n_analyzed,
        n_failed=n_failed,
        top_failure_rows=failure_reason_rows,
        top_repro_rows=repro_bucket_rows,
        breakdown_dims=breakdown_dims,
    )
    _write_text(level_001_readme, level_001 / f"README_{generated_utc}.txt")

    cardinality_lines = _build_scope_cardinality_lines(
        filter_inventory_rows=filter_inventory_rows,
        enriched_rows=enriched_rows,
        scope_title=scope_title,
        generated_utc=generated_utc,
    )
    cardinality_fpath = level_001_static / f"cardinality_summary_{generated_utc}.txt"
    _write_text(cardinality_lines, cardinality_fpath)
    write_latest_alias(cardinality_fpath, level_001_static, "cardinality_summary.latest.txt")
    write_latest_alias(cardinality_fpath, level_001, "cardinality_summary.latest.txt")

    level_002_lines = [
        "Drilldown Summary",
        "",
        f"generated_utc: {generated_utc}",
        f"scope: {scope_title}",
        "",
        "contents:",
        "  - benchmark_summary.latest.csv: benchmark-level counts and top failure reason",
        "  - run_inventory.latest.csv: one row per scheduled job with completion, failure, repro, and attempt identity/provenance fields",
        "  - reproducibility_rows.latest.csv: analyzed per-run reproducibility cases in this scope",
        "  - prioritized_breakdowns.latest.{txt,csv,json}: ranked triage shortlist of breakdowns and example cases to inspect next",
        "  - prioritized_examples.latest/: filesystem-first symlink tree for the shortlisted breakdowns and example report artifacts",
        "  - off_story_summary.latest.{txt,csv,json}: off-story local extensions plus on-story context counts",
        "  - run_multiplicity_summary.latest.{txt,csv,json}: logical-run multiplicity, attempt identity, machine spread, and experiment spread",
    ]
    if breakdown_dims:
        level_002_lines.append("  - breakdowns/: reusable summaries for additional cuts of the same data")
    _write_text(level_002_lines, level_002 / f"README_{generated_utc}.txt")

    latest_pairs = [
        (level_001 / f"README_{generated_utc}.txt", level_001, "README.latest.txt"),
        (level_002 / f"README_{generated_utc}.txt", level_002, "README.latest.txt"),
        (Path(failure_table["json"]), level_001_machine, "failure_runs.latest.json"),
        (Path(failure_table["csv"]), level_001_static, "failure_runs.latest.csv"),
        (Path(failure_table["txt"]), level_001_static, "failure_runs.latest.txt"),
        (Path(failure_reason_table["json"]), level_001_machine, "failure_reasons.latest.json"),
        (Path(failure_reason_table["csv"]), level_001_static, "failure_reasons.latest.csv"),
        (Path(failure_reason_table["txt"]), level_001_static, "failure_reasons.latest.txt"),
        (Path(benchmark_table["json"]), level_002_machine, "benchmark_summary.latest.json"),
        (Path(benchmark_table["csv"]), level_002_static, "benchmark_summary.latest.csv"),
        (Path(benchmark_table["txt"]), level_002_static, "benchmark_summary.latest.txt"),
        (Path(run_inventory_table["json"]), level_002_machine, "run_inventory.latest.json"),
        (Path(run_inventory_table["csv"]), level_002_static, "run_inventory.latest.csv"),
        (Path(run_inventory_table["txt"]), level_002_static, "run_inventory.latest.txt"),
        (Path(repro_table["json"]), level_002_machine, "reproducibility_rows.latest.json"),
        (Path(repro_table["csv"]), level_002_static, "reproducibility_rows.latest.csv"),
        (Path(repro_table["txt"]), level_002_static, "reproducibility_rows.latest.txt"),
        (Path(prioritized_breakdowns_table["json"]), level_002_machine, "prioritized_breakdowns.latest.json"),
        (Path(prioritized_breakdowns_table["csv"]), level_002_static, "prioritized_breakdowns.latest.csv"),
        (Path(prioritized_breakdowns_table["txt"]), level_002_static, "prioritized_breakdowns.latest.txt"),
        (Path(off_story_table["json"]), level_002_machine, "off_story_summary.latest.json"),
        (Path(off_story_table["csv"]), level_002_static, "off_story_summary.latest.csv"),
        (Path(off_story_table["txt"]), level_002_static, "off_story_summary.latest.txt"),
        (Path(run_multiplicity_table["json"]), level_002_machine, "run_multiplicity_summary.latest.json"),
        (Path(run_multiplicity_table["csv"]), level_002_static, "run_multiplicity_summary.latest.csv"),
        (Path(run_multiplicity_table["txt"]), level_002_static, "run_multiplicity_summary.latest.txt"),
    ]
    for src, root, name in latest_pairs:
        write_latest_alias(src, root, name)
    write_latest_alias(prioritized_examples_tree, level_002, "prioritized_examples.latest")

    if include_visuals:
        for base_name, artifact in [
            ("benchmark_status", benchmark_plot),
            ("reproducibility_buckets", repro_bucket_plot),
            ("agreement_curve", agreement_curve_plot),
            ("coverage_matrix", coverage_matrix_plot),
            ("failure_taxonomy", failure_taxonomy_plot),
            ("filter_selection_by_model", filter_selection_by_model_plot),
        ]:
            write_latest_alias(Path(artifact["json"]), level_001_machine, f"{base_name}.latest.json")
            if artifact.get("html"):
                write_latest_alias(Path(str(artifact["html"])), level_001_interactive, f"{base_name}.latest.html")
            if artifact.get("png"):
                write_latest_alias(Path(str(artifact["png"])), level_001_static, f"{base_name}.latest.png")
            if artifact.get("jpg"):
                write_latest_alias(Path(str(artifact["jpg"])), level_001_static, f"{base_name}.latest.jpg")

    manifest = {
        "generated_utc": generated_utc,
        "scope_kind": scope_kind,
        "scope_value": scope_value,
        "scope_title": scope_title,
        "summary_root": str(summary_root),
        "version_dpath": str(version_dpath),
        "level_001": str(level_001),
        "level_002": str(level_002),
        "n_total": n_total,
        "n_completed": n_completed,
        "n_failed": n_failed,
        "n_analyzed": n_analyzed,
        "breakdown_dims": breakdown_dims,
        "operational_sankey": operational_art,
        "filter_to_attempt_sankey": filter_to_attempt_art,
        "attempted_to_repro_sankey": attempted_to_repro_art,
        "attempted_to_repro_sankey_tol001": attempted_to_repro_tol001_art,
        "attempted_to_repro_sankey_tol010": attempted_to_repro_tol010_art,
        "attempted_to_repro_sankey_tol050": attempted_to_repro_tol050_art,
        "end_to_end_sankey": end_to_end_art,
        "end_to_end_sankey_tol001": end_to_end_tol001_art,
        "end_to_end_sankey_tol010": end_to_end_tol010_art,
        "end_to_end_sankey_tol050": end_to_end_tol050_art,
        "reproducibility_sankey": repro_art,
        "reproducibility_sankey_tol001": repro_tol001_art,
        "reproducibility_sankey_tol010": repro_tol010_art,
        "reproducibility_sankey_tol050": repro_tol050_art,
        "reproducibility_sankey_by_metric": repro_metric_art,
        "benchmark_plot": benchmark_plot,
        "repro_bucket_plot": repro_bucket_plot,
        "agreement_curve_plot": agreement_curve_plot,
        "coverage_matrix_plot": coverage_matrix_plot,
        "failure_taxonomy_plot": failure_taxonomy_plot,
        "filter_selection_by_model_plot": filter_selection_by_model_plot,
        "prioritized_breakdowns": prioritized_breakdowns_table,
        "prioritized_examples": {
            "tree_root": str(prioritized_examples_tree),
            "repairs": prioritized_example_repairs,
        },
        "off_story_summary": off_story_table,
        "run_multiplicity_summary": run_multiplicity_table,
        "identity_contract": run_multiplicity_summary.get("definitions"),
    }
    manifest_fpath = level_001_machine / f"summary_manifest_{generated_utc}.json"
    _write_json(manifest, manifest_fpath)
    write_latest_alias(manifest_fpath, level_001_machine, "summary_manifest.latest.json")

    reproduce_sh_fpath = level_001 / f"reproduce_{generated_utc}.sh"
    _write_reproduce_sh(
        reproduce_sh_fpath,
        scope_kind,
        scope_value,
        index_path=index_fpath,
        filter_inventory_json=filter_inventory_json,
    )
    write_latest_alias(reproduce_sh_fpath, level_001, "reproduce.latest.sh")
    write_latest_alias(reproduce_sh_fpath, level_001, "reproduce.sh")

    symlink_to(level_002, level_001 / "next_level")
    symlink_to(level_001, level_002 / "up_level")
    experiment_names = {str(row.get("experiment_name")) for row in enriched_rows if row.get("experiment_name")}
    if len(experiment_names) == 1:
        exp_name = next(iter(experiment_names))
        # Resolve the experiment-analysis target by checking the canonical
        # store location first, then the parameterized publication-side
        # symlink directory, then the in-repo legacy location. Anyone of
        # them may hold the live reference depending on when this experiment
        # was last analyzed.
        candidates = [
            experiment_analysis_dpath(exp_name),
            publication_experiments_root() / f"experiment-analysis-{slugify(exp_name)}",
            legacy_repo_publication_root() / f"experiment-analysis-{slugify(exp_name)}",
        ]
        analysis_dpath = next((c for c in candidates if c.exists()), None)
        if analysis_dpath is not None:
            symlink_to(analysis_dpath, level_002 / "experiment-analysis")

    story_index_lines = [
        "Story Index — Canonical Reading Order",
        "======================================",
        f"Generated: {generated_utc}",
        f"Scope: {scope_title}",
        "",
        "The reproducibility story has two stages plus an executive summary",
        "and a detail view. Read in order:",
        "",
        "s01 — Executive Operational Summary",
        "  All attempted runs: benchmark group → lifecycle status → outcome/failure reason.",
        "  File: sankey_s01_operational.latest.{html,jpg,txt}",
        "",
        "Stage A — Universe → Scope (filter funnel)",
        "  How the source universe gets narrowed to the in-scope set. Every filter gate",
        "  (structural, model metadata, open-weight, tag/modality, deployment, size,",
        "  selection) is a stage; terminal nodes are 'selected' (in scope) or",
        "  'excluded: <reason>'. This is the context-establishment view.",
        "  File: sankey_a_universe_to_scope.latest.{html,jpg,txt}",
        "",
        "Stage B — Scope → Attempt → Execution → Analysis → Reproduction",
        "  Of the in-scope rows, how many we attempted, completed, analyzed, and at",
        "  what agreement bucket they landed (abs_tol=0). This is the coverage view.",
        "  File: sankey_b_scope_to_analyzed.latest.{html,jpg,txt}",
        "  Tolerance variants live under alt_tolerances/ as",
        "  sankey_b_scope_to_analyzed_tol{001,010,050}.",
        "",
        "s05 — Detailed Reproducibility Breakdown",
        "  Group → local repeatability → official-vs-local agreement → diagnosis.",
        "  File: sankey_s05_reproducibility.latest.{html,jpg,txt}",
        "",
        "Supplementary",
        "  prioritized_breakdowns.latest.txt: triage-first shortlist with direct paths",
        "  prioritized_examples.latest/: filesystem-first symlink tree for shortlisted examples",
        "  off_story_summary.latest.txt: off-story local extensions with stage counts",
        "  run_multiplicity_summary.latest.txt: logical-result identity, repeats, machines",
        "  sankey_repro_by_metric: per-metric drift (max |official - local| across runs)",
        "  alt_tolerances/: tolerance sweep variants for Stage B and s05",
        "  agreement_curve.latest.html: agreement-rate vs tolerance curve",
        "  coverage_matrix.latest.html: model × benchmark reproducibility heat-map",
    ]
    story_index_fpath = level_001 / f"story_index_{generated_utc}.txt"
    _write_text(story_index_lines, story_index_fpath)
    write_latest_alias(story_index_fpath, level_001, "story_index.latest.txt")

    _write_scope_level_aliases(level_001, level_002, summary_root)

    # Always sweep legacy s02/s03/s04 aliases so a re-run after the
    # rename-refactor doesn't surface stale-named sankeys alongside the
    # new a/b ones.
    _cleanup_legacy_sankey_aliases(summary_root)

    if not filter_inventory_rows:
        # No filter inventory was loaded for this scope (e.g. virtual
        # experiments where the global Stage-1 funnel does not describe
        # the report's denominator). Remove any stale ``latest`` aliases
        # for the filter-side artifacts so a reader doesn't see a
        # misleading "selected vs excluded by model" plot or a
        # ``discovered -> attempted`` sankey rooted in a universe that
        # doesn't apply to this scope. Timestamped history files in
        # ``.history/`` are left alone — only the surfaced aliases are
        # cleaned up.
        _cleanup_filter_artifact_aliases(summary_root)


_FILTER_ARTIFACT_ALIAS_NAMES = (
    "filter_selection_by_model.latest.json",
    "filter_selection_by_model.latest.html",
    "filter_selection_by_model.latest.jpg",
    "filter_selection_by_model.latest.png",
    # Stage-A funnel (new) — only meaningful when filter inventory is loaded.
    "sankey_a_universe_to_scope.latest.html",
    "sankey_a_universe_to_scope.latest.jpg",
    "sankey_a_universe_to_scope.latest.txt",
    "sankey_a_universe_to_scope.latest.json",
    # Legacy filter sankeys (s02 / s04) — kept here so historic builds get
    # their stale aliases cleaned up when re-run with --no-filter-inventory.
    "sankey_s02_filter_to_attempt.latest.html",
    "sankey_s02_filter_to_attempt.latest.jpg",
    "sankey_s02_filter_to_attempt.latest.txt",
    "sankey_s02_filter_to_attempt.latest.json",
    "sankey_s04_end_to_end.latest.html",
    "sankey_s04_end_to_end.latest.jpg",
    "sankey_s04_end_to_end.latest.txt",
    "sankey_s04_end_to_end.latest.json",
)


_LEGACY_SANKEY_ALIAS_NAMES = (
    "sankey_s02_filter_to_attempt.latest.html",
    "sankey_s02_filter_to_attempt.latest.jpg",
    "sankey_s02_filter_to_attempt.latest.txt",
    "sankey_s02_filter_to_attempt.latest.json",
    "sankey_s03_attempted_to_repro.latest.html",
    "sankey_s03_attempted_to_repro.latest.jpg",
    "sankey_s03_attempted_to_repro.latest.txt",
    "sankey_s03_attempted_to_repro.latest.json",
    "sankey_s04_end_to_end.latest.html",
    "sankey_s04_end_to_end.latest.jpg",
    "sankey_s04_end_to_end.latest.txt",
    "sankey_s04_end_to_end.latest.json",
)


def _cleanup_legacy_sankey_aliases(scope_root: Path) -> None:
    """Unlink legacy s02/s03/s04 sankey aliases after the rename to a/b.

    Stage 2 of the funnel-decomposition refactor renamed:
      sankey_s02_filter_to_attempt → sankey_a_universe_to_scope
      sankey_s03_attempted_to_repro → sankey_b_scope_to_analyzed
      sankey_s04_end_to_end → dropped (decomposable into a + b)
    """
    target_names = set(_LEGACY_SANKEY_ALIAS_NAMES)
    for path in scope_root.rglob("*"):
        if path.name in target_names:
            safe_unlink(path)


def _cleanup_filter_artifact_aliases(scope_root: Path) -> None:
    """Unlink any latest alias that surfaces a filter-funnel artifact.

    Used when a scope has no filter inventory; without this, latest
    aliases from a previous run (when one was loaded) still surface a
    misleading filter funnel for the current scope.
    """
    target_names = set(_FILTER_ARTIFACT_ALIAS_NAMES)
    for path in scope_root.rglob("*"):
        if path.name in target_names:
            safe_unlink(path)


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-name", default=None)
    parser.add_argument("--index-fpath", default=None)
    parser.add_argument("--index-dpath", default=str(default_index_root()))
    parser.add_argument("--filter-inventory-json", default=None)
    parser.add_argument(
        "--no-filter-inventory",
        action="store_true",
        help=(
            "Skip loading the Stage-1 filter inventory entirely (overrides "
            "both --filter-inventory-json and the default fallback at "
            "<audit_store>/analysis/filter_inventory.json). Use this for "
            "scoped sub-experiments — e.g. virtual experiments — where the "
            "global filter funnel doesn't describe the report's denominator "
            "and would only mislead the reader. Filter sankeys, the model "
            "selection plot, and the discovered/selected cardinality lines "
            "all naturally drop out when the inventory is empty."
        ),
    )
    parser.add_argument("--summary-root", default=str(aggregate_summary_reports_root()))
    parser.add_argument(
        "--analysis-root",
        action="append",
        default=[],
        help=(
            "Extra directory to scan for per-packet core-report JSONs. "
            "Repeatable. Used for virtual experiments whose analysis lives "
            "under a custom output.root and would otherwise be invisible "
            "to the canonical/publication/legacy scan. Each root is globbed "
            "as <root>/*/core-reports/*/core_metric_report.latest.json."
        ),
    )
    parser.add_argument(
        "--breakdown-dims",
        nargs="*",
        default=DEFAULT_BREAKDOWN_DIMS,
    )
    parser.add_argument("--max-items-per-breakdown", type=int, default=12)
    args = parser.parse_args(argv)

    index_fpath = (
        Path(args.index_fpath).expanduser().resolve()
        if args.index_fpath
        else latest_index_csv(Path(args.index_dpath).expanduser().resolve())
    )
    filter_inventory_json = (
        Path(args.filter_inventory_json).expanduser().resolve()
        if args.filter_inventory_json
        else None
    )
    rows = load_rows(index_fpath)
    filter_inventory_rows = _load_filter_inventory_rows(
        filter_inventory_json,
        skip=args.no_filter_inventory,
    )
    _raise_fd_limit()  # Note: this probably is not necessary, as fd limits are usually due to a VM issue.
    configure_plotly_chrome()
    all_repro_rows = _load_all_repro_rows(extra_analysis_roots=args.analysis_root)

    if args.experiment_name:
        scope_kind = "experiment_name"
        scope_value = args.experiment_name
        scope_rows = [row for row in rows if row.get("experiment_name") == args.experiment_name]
        if not scope_rows:
            raise SystemExit(f"No rows found for experiment_name={args.experiment_name!r}")
        repro_rows = [row for row in all_repro_rows if row.get("experiment_name") == args.experiment_name]
    else:
        scope_kind = "all_results"
        scope_value = None
        scope_rows = rows
        repro_rows = all_repro_rows

    scope_root = _scope_summary_root(
        Path(args.summary_root).expanduser().resolve(),
        _scope_slug(scope_kind, scope_value),
    )
    filter_inventory_path_for_repro = (
        filter_inventory_json
        if filter_inventory_json is not None
        else (_default_filter_inventory_json() if _default_filter_inventory_json().exists() else None)
    )
    _render_scope_summary(
        scope_kind=scope_kind,
        scope_value=scope_value,
        scope_rows=scope_rows,
        repro_rows=repro_rows,
        filter_inventory_rows=filter_inventory_rows,
        filter_inventory_json=filter_inventory_path_for_repro,
        index_fpath=index_fpath,
        summary_root=scope_root,
        breakdown_dims=list(args.breakdown_dims),
        max_items_per_breakdown=args.max_items_per_breakdown,
    )
    logger.info(f"Wrote executive summary root: {rich_link(scope_root)}")


if __name__ == "__main__":
    setup_cli_logging()
    main()
