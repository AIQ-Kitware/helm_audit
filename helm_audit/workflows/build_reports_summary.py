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

from helm_audit.infra.api import audit_root, default_index_root, default_store_root
from helm_audit.infra.plotly_env import configure_plotly_chrome
from helm_audit.infra.fs_publish import stamped_history_dir, symlink_to, write_latest_alias
from helm_audit.infra.paths import experiment_analysis_dpath
from helm_audit.infra.report_layout import aggregate_summary_reports_root, compat_core_run_reports_root, core_run_reports_root, portable_repo_root_lines
from helm_audit.utils.numeric import nested_get
from helm_audit.utils.sankey import emit_sankey_artifacts
from helm_audit.utils import sankey_builder

from loguru import logger


DEFAULT_BREAKDOWN_DIMS = [
    "experiment_name",
    "model",
    "benchmark",
    "suite",
    "machine_host",
]


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
    for pair in report.get("pairs", []):
        if pair.get("label") == label:
            return pair
    return {}


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


def _default_filter_inventory_json() -> Path:
    return default_store_root() / "analysis" / "filter_inventory.json"


def _load_filter_inventory_rows(filter_inventory_json: Path | None) -> list[dict[str, Any]]:
    path = filter_inventory_json if filter_inventory_json is not None else _default_filter_inventory_json()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except Exception:
        logger.warning(f"Unable to load filter inventory: {path}")
        return []
    if not isinstance(payload, list):
        logger.warning(f"Filter inventory is not a list: {path}")
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
    decorated = []
    for row in repro_rows_for_entry:
        key = (str(row.get("experiment_name") or ""), str(row.get("run_entry") or ""))
        matching_scope_rows = scope_rows_by_key.get(key, [])
        manifest_ts = max((_coerce_float(item.get("manifest_timestamp")) for item in matching_scope_rows), default=float("-inf"))
        decorated.append((manifest_ts, str(row.get("experiment_name") or ""), row))
    decorated.sort(reverse=True)
    return decorated[0][2]


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
        if "no-hf-deployment" in reasons:
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
        if "no-hf-deployment" in reasons:
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


def _build_filter_to_attempt_root() -> tuple[sankey_builder.Root, list[str], dict[str, list[str]]]:
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
    selection[FILTER_SELECTION_SELECTED_LABEL].group(by="attempt_stage", name="Attempt")

    stage_names = [
        "Structural Gate",
        "Metadata Gate",
        "Open-Weight Gate",
        "Tag Gate",
        "Deployment Gate",
        "Size Gate",
        "Selection",
        "Attempt",
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
        "Attempt": [
            ATTEMPTED_LABEL,
            NOT_ATTEMPTED_LABEL,
        ],
    }
    return root, stage_names, stage_defs


def _build_attempted_to_repro_root() -> tuple[sankey_builder.Root, list[str], dict[str, list[str]]]:
    root = sankey_builder.Root(label="Attempted reproduction runs")
    execution = root.group(by="execution_stage", name="Execution")
    execution["attempted_not_finished"].connect(None)
    execution["attempted_failed_or_incomplete"].connect(None)
    analysis = execution["completed_with_run_artifacts"].group(by="analysis_stage", name="Analysis")
    analysis["completed_not_yet_analyzed"].connect(None)
    analysis["analyzed"].group(by="reproduction_stage", name="Reproduction")
    stage_names = ["Execution", "Analysis", "Reproduction"]
    stage_defs = {
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


def _load_all_repro_rows() -> list[dict[str, Any]]:
    # Scan both the canonical store location and the legacy compat location so
    # experiments that haven't been re-run since the layout migration are still found.
    new_root = core_run_reports_root()
    old_root = compat_core_run_reports_root()
    report_jsons = sorted(
        list(new_root.glob("*/core-reports/*/core_metric_report.latest.json"))
        + list(old_root.glob("experiment-analysis-*/core-reports/*/core_metric_report.latest.json"))
    )
    deduped: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for report_json in report_jsons:
        try:
            report = _load_json(report_json)
        except Exception:
            continue
        selection_fpath = report_json.parent / "report_selection.latest.json"
        selection = _load_json(selection_fpath) if selection_fpath.exists() else {}
        experiment_name = selection.get("experiment_name")
        run_entry = selection.get("run_entry")
        if not experiment_name or not run_entry:
            continue
        official = _find_pair(report, "official_vs_kwdagger") or {}
        repeat = _find_pair(report, "kwdagger_repeat") or {}
        official_diag = official.get("diagnosis", {}) or {}
        repeat_diag = repeat.get("diagnosis", {}) or {}
        official_instance_level = official.get("instance_level") or {}
        official_agree_curve = official_instance_level.get("agreement_vs_abs_tol") or []
        agree_0 = _find_curve_value(official_agree_curve, 0.0)
        row = {
            "experiment_name": experiment_name,
            "run_entry": run_entry,
            "run_spec_name": report.get("run_spec_name"),
            "report_dir": str(report_json.parent),
            "report_json": str(report_json),
            "repeat_diagnosis": repeat_diag.get("label"),
            "repeat_primary_reasons": repeat_diag.get("primary_reason_names") or [],
            "official_diagnosis": official_diag.get("label"),
            "official_primary_reasons": official_diag.get("primary_reason_names") or [],
            "official_instance_agree_0": agree_0,
            "official_instance_agree_bucket": _bucket_agreement(agree_0),
            "official_instance_agree_01": _find_curve_value(official_agree_curve, 0.1),
            "official_runlevel_abs_max": nested_get(official, "run_level", "overall_quantiles", "abs_delta", "max"),
            "official_runlevel_abs_p90": nested_get(official, "run_level", "overall_quantiles", "abs_delta", "p90"),
            "official_instance_agree_001": _find_curve_value(official_agree_curve, 0.001),
            "official_instance_agree_005": _find_curve_value(official_agree_curve, 0.05),
            "core_metrics": official.get("core_metrics") or [],
            "official_runlevel_metric_max_deltas": {
                m["metric"]: nested_get(m, "abs_delta", "max")
                for m in (nested_get(official, "run_level", "by_metric") or [])
            },
            "official_instance_agree_curve": [
                {"abs_tol": pt["abs_tol"], "agree_ratio": pt["agree_ratio"]}
                for pt in official_agree_curve
            ],
            "official_per_metric_agreement": nested_get(official, "instance_level", "per_metric_agreement") or {},
        }
        deduped[(experiment_name, run_entry)] = row
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
    if os.environ.get("HELM_AUDIT_SKIP_PLOTLY", "") not in {"1", "true", "yes"}:
        try:
            configure_plotly_chrome()
            import plotly.express as px

            fig = px.bar(rows, x=x, y=y, color=color, title=title, barmode="stack")
            fig.update_layout(
                xaxis_title=xaxis_title if xaxis_title is not None else x.replace("_", " "),
                yaxis_title=yaxis_title if yaxis_title is not None else y.replace("_", " "),
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                fig.write_image(str(jpg_fpath), scale=2.0)
                jpg_out = str(jpg_fpath)
        except Exception as ex:
            plotly_error = f"unable to write bar HTML/images: {ex!r}"
    else:
        plotly_error = "skipped plotly bar rendering by configuration"
    try:
        import matplotlib.pyplot as plt

        if rows:
            x_values = sorted({str(row.get(x, "")) for row in rows})
            color_values = sorted({str(row.get(color, "")) for row in rows})
            counts = {(str(row.get(x, "")), str(row.get(color, ""))): float(row.get(y, 0) or 0) for row in rows}
            bottoms = [0.0 for _ in x_values]
            fig, ax = plt.subplots(figsize=(12, 6))
            for color_value in color_values:
                vals = [counts.get((xv, color_value), 0.0) for xv in x_values]
                ax.bar(x_values, vals, bottom=bottoms, label=color_value)
                bottoms = [a + b for a, b in zip(bottoms, vals)]
            ax.set_title(title)
            ax.set_xlabel(x.replace("_", " "))
            ax.set_ylabel(y.replace("_", " "))
            ax.tick_params(axis="x", rotation=45)
            ax.legend(fontsize=8)
            fig.tight_layout()
            fig.savefig(png_fpath, dpi=200)
            png_out = str(png_fpath)
            if jpg_out is None:
                fig.savefig(jpg_fpath, dpi=200)
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
        lines.append(row_line("eligible_selected", _cardinality(selected_inv)))

    lines.append(row_line("attempted", _cardinality(enriched_rows)))

    completed_rows = [r for r in enriched_rows if _is_truthy_text(r.get("has_run_spec"))]
    lines.append(row_line("completed", _cardinality(completed_rows)))

    analyzed_rows = [r for r in enriched_rows if r.get("official_instance_agree_0") is not None]
    lines.append(row_line("analyzed", _cardinality(analyzed_rows)))

    lines += [
        "",
        "Columns: runs = total run entries; models/benchmarks/scenarios = unique values;",
        "         mod×bench = unique (model, benchmark) pairs in that subset.",
        "Stages: discovered = all runs seen by Stage 1 filter; eligible_selected = passed all filters;",
        "        attempted = scheduled in this experiment; completed = produced HELM artifacts;",
        "        analyzed = have reproducibility report.",
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
            "",
            "  understand_upstream_filtering:",
            "    1. What runs were excluded at Stage 1 (discovery)? See reports/filtering/ which contains",
            "       sankey_model_filter.latest.html and filter_cardinality_summary.latest.txt.",
            "    2. Read docs/pipeline.md for the full end-to-end workflow (stages 1-6).",
            "",
            "  explore_execution_coverage (read sankeys in order):",
            "    s01: sankey_s01_operational.latest.html — all attempted runs: benchmark → lifecycle → outcome",
            "    s02: sankey_s02_filter_to_attempt.latest.html — eligible run-specs → actually attempted",
            "    s03: sankey_s03_attempted_to_repro.latest.html — attempted runs → reproducibility (exact match)",
            "    s04: sankey_s04_end_to_end.latest.html — full funnel: discovered → reproducible",
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
        "sankey_s02_filter_to_attempt.latest.html",
        "sankey_s03_attempted_to_repro.latest.html",
        "sankey_s04_end_to_end.latest.html",
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
        "sankey_s02_filter_to_attempt.latest.jpg",
        "sankey_s02_filter_to_attempt.latest.txt",
        "sankey_s03_attempted_to_repro.latest.jpg",
        "sankey_s03_attempted_to_repro.latest.txt",
        "sankey_s04_end_to_end.latest.jpg",
        "sankey_s04_end_to_end.latest.txt",
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
    ]:
        src = level_002_static / src_name
        if src.exists() or src.is_symlink():
            write_latest_alias(src, summary_root, src_name)


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
) -> dict[str, str | None]:
    """Line chart: x=abs_tol (log), y=instance agree_ratio, one line per analyzed run."""
    bench_lookup = {
        (str(r.get("experiment_name")), str(r.get("run_entry"))): str(r.get("benchmark") or "unknown")
        for r in enriched_rows
    }
    curve_data: list[dict[str, Any]] = []
    for row in repro_rows:
        key = (str(row.get("experiment_name")), str(row.get("run_entry")))
        bench = bench_lookup.get(key, "unknown")
        curve = row.get("official_instance_agree_curve") or []
        run_label = str(row.get("run_spec_name") or row.get("run_entry") or "unknown")
        for pt in curve:
            curve_data.append({
                "benchmark": bench,
                "run": run_label,
                "abs_tol": pt["abs_tol"],
                "agree_ratio": pt["agree_ratio"],
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
                title=title,
                xaxis={"title": "abs_tol (tolerance on |official - local|)", "type": "log"},
                yaxis={"title": "Fraction of Instances Agreeing", "range": [0, 1.05]},
                legend={"title": "Benchmark"},
                hovermode="closest",
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                fig.write_image(str(jpg_fpath), scale=2.0)
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
                fig.write_image(str(jpg_fpath), scale=2.0)
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
                fig.write_image(str(jpg_fpath), scale=2.0)
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
                xaxis={"title": "Benchmark", "tickangle": -45, "categoryorder": "array", "categoryarray": bench_order},
                yaxis={"title": "Failed Job Count"},
                legend={"title": "Root Cause Category"},
            )
            fig.write_html(str(html_fpath), include_plotlyjs="cdn")
            html_out = str(html_fpath)
            if os.environ.get("HELM_AUDIT_SKIP_STATIC_IMAGES", "") not in {"1", "true", "yes"}:
                fig.write_image(str(jpg_fpath), scale=2.0)
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
    cmd = 'PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" -m helm_audit.workflows.build_reports_summary'
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
    logger.debug(f'Write to 💻: {fpath}')
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

    enriched_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    for row in scope_rows:
        enriched = dict(row)
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
    filter_to_attempt_rows = _build_filter_to_attempt_rows(
        filter_inventory_rows,
        scope_rows,
    )
    attempted_to_repro_exact_rows = _build_attempted_to_repro_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_0",
    )
    attempted_to_repro_tol001_rows = _build_attempted_to_repro_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_001",
    )
    attempted_to_repro_tol010_rows = _build_attempted_to_repro_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_01",
    )
    attempted_to_repro_tol050_rows = _build_attempted_to_repro_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_005",
    )
    end_to_end_exact_rows = _build_end_to_end_funnel_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_0",
    )
    end_to_end_tol001_rows = _build_end_to_end_funnel_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_001",
    )
    end_to_end_tol010_rows = _build_end_to_end_funnel_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_01",
    )
    end_to_end_tol050_rows = _build_end_to_end_funnel_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_005",
    )

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
                    "for completed runs: instance-level agreement bucket at abs_tol=0 (exact match)",
                    "  exact_or_near_exact: >=99.9999% of instances agree exactly",
                    "  high_agreement_0.95+: >=95% of instances agree exactly",
                    "  moderate_agreement_0.80+: >=80% agree exactly",
                    "  low_agreement_0.00+: >0% agree exactly",
                    "  zero_agreement: no instances agree exactly",
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
            title=f"Reproducibility Summary (instance-level, abs_tol=0 exact match): {scope_title}",
            stage_defs={
                "group": ["benchmark family or suite"],
                "repeatability": ["local repeatability diagnosis (run vs its own repeat)"],
                "agreement": [
                    "official-vs-local agreement bucket at abs_tol=0 (exact match only)",
                    "fraction = share of instances where |official_score - local_score| == 0",
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
                "official-vs-local agreement bucket at the abs_tol stated in the title",
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
        filter_to_attempt_root, filter_to_attempt_stage_names, filter_to_attempt_stage_defs = _build_filter_to_attempt_root()
        filter_to_attempt_art = emit_sankey_artifacts(
            rows=filter_to_attempt_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="s02_filter_to_attempt",
            title=f"Filter Funnel to Attempted Runs: {scope_title}",
            stage_defs=filter_to_attempt_stage_defs,
            stage_order=[],
            root=filter_to_attempt_root,
            explicit_stage_names=filter_to_attempt_stage_names,
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        ) if filter_to_attempt_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no filter inventory rows available"}
        attempted_to_repro_root, attempted_to_repro_stage_names, attempted_to_repro_stage_defs = _build_attempted_to_repro_root()
        attempted_to_repro_art = emit_sankey_artifacts(
            rows=attempted_to_repro_exact_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="s03_attempted_to_repro",
            title=f"Attempted Runs to Reproducibility at abs_tol=0: {scope_title}",
            stage_defs=attempted_to_repro_stage_defs,
            stage_order=[],
            root=attempted_to_repro_root,
            explicit_stage_names=attempted_to_repro_stage_names,
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        ) if attempted_to_repro_exact_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no attempted rows available"}
        attempted_to_repro_tol001_art = emit_sankey_artifacts(
            rows=attempted_to_repro_tol001_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="attempted_to_repro_tol001",
            title=f"Attempted Runs to Reproducibility at abs_tol=0.001: {scope_title}",
            stage_defs=attempted_to_repro_stage_defs,
            stage_order=[],
            root=attempted_to_repro_root,
            explicit_stage_names=attempted_to_repro_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if attempted_to_repro_tol001_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no attempted rows available"}
        attempted_to_repro_tol010_art = emit_sankey_artifacts(
            rows=attempted_to_repro_tol010_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="attempted_to_repro_tol010",
            title=f"Attempted Runs to Reproducibility at abs_tol=0.010: {scope_title}",
            stage_defs=attempted_to_repro_stage_defs,
            stage_order=[],
            root=attempted_to_repro_root,
            explicit_stage_names=attempted_to_repro_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if attempted_to_repro_tol010_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no attempted rows available"}
        attempted_to_repro_tol050_art = emit_sankey_artifacts(
            rows=attempted_to_repro_tol050_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="attempted_to_repro_tol050",
            title=f"Attempted Runs to Reproducibility at abs_tol=0.050: {scope_title}",
            stage_defs=attempted_to_repro_stage_defs,
            stage_order=[],
            root=attempted_to_repro_root,
            explicit_stage_names=attempted_to_repro_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if attempted_to_repro_tol050_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no attempted rows available"}
        end_to_end_root, end_to_end_stage_names, end_to_end_stage_defs = _build_end_to_end_funnel_root()
        end_to_end_art = emit_sankey_artifacts(
            rows=end_to_end_exact_rows,
            report_dpath=level_001,
            stamp=generated_utc,
            kind="s04_end_to_end",
            title=f"End-to-End Coverage and Reproducibility at abs_tol=0: {scope_title}",
            stage_defs=end_to_end_stage_defs,
            stage_order=[],
            root=end_to_end_root,
            explicit_stage_names=end_to_end_stage_names,
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
        ) if end_to_end_exact_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no filter inventory rows available"}
        end_to_end_tol001_art = emit_sankey_artifacts(
            rows=end_to_end_tol001_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="end_to_end_tol001",
            title=f"End-to-End Coverage and Reproducibility at abs_tol=0.001: {scope_title}",
            stage_defs=end_to_end_stage_defs,
            stage_order=[],
            root=end_to_end_root,
            explicit_stage_names=end_to_end_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if end_to_end_tol001_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no filter inventory rows available"}
        end_to_end_tol010_art = emit_sankey_artifacts(
            rows=end_to_end_tol010_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="end_to_end_tol010",
            title=f"End-to-End Coverage and Reproducibility at abs_tol=0.010: {scope_title}",
            stage_defs=end_to_end_stage_defs,
            stage_order=[],
            root=end_to_end_root,
            explicit_stage_names=end_to_end_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if end_to_end_tol010_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no filter inventory rows available"}
        end_to_end_tol050_art = emit_sankey_artifacts(
            rows=end_to_end_tol050_rows,
            report_dpath=alt_tol_dpath,
            stamp=generated_utc,
            kind="end_to_end_tol050",
            title=f"End-to-End Coverage and Reproducibility at abs_tol=0.050: {scope_title}",
            stage_defs=end_to_end_stage_defs,
            stage_order=[],
            root=end_to_end_root,
            explicit_stage_names=end_to_end_stage_names,
            machine_dpath=alt_tol_machine,
            interactive_dpath=alt_tol_interactive,
            static_dpath=alt_tol_static,
        ) if end_to_end_tol050_rows else {"json": None, "txt": None, "key_txt": None, "html": None, "jpg": None, "plotly_error": "no filter inventory rows available"}
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

    if include_visuals:
        benchmark_plot = _write_plotly_bar(
            rows=benchmark_status_rows,
            x="group_value",
            y="count",
            color="status_bucket",
            title=f"Benchmark Coverage and Analysis Status (analyzed runs use abs_tol=0): {scope_title}",
            stem=level_001 / f"benchmark_status_{generated_utc}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
            xaxis_title="Benchmark",
            yaxis_title="Job Count",
        )
        repro_bucket_plot = _write_plotly_bar(
            rows=repro_bucket_rows,
            x="official_instance_agree_bucket",
            y="count",
            color="official_instance_agree_bucket",
            title=f"Official vs Local Agreement Buckets (instance-level, abs_tol=0 exact match): {scope_title}",
            stem=level_001 / f"reproducibility_buckets_{generated_utc}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
            xaxis_title="Agreement Bucket (fraction of instances with |official - local| == 0)",
            yaxis_title="Run Count",
        )
        agreement_curve_plot = _write_agreement_curve_plot(
            repro_rows=repro_rows,
            enriched_rows=enriched_rows,
            stem=level_001 / f"agreement_curve_{generated_utc}",
            title=f"Agreement Rate vs Tolerance (instance-level): {scope_title}",
            machine_dpath=level_001_machine,
            interactive_dpath=level_001_interactive,
            static_dpath=level_001_static,
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
        "  - run_inventory.latest.csv: one row per scheduled job with completion, failure, and repro fields",
        "  - reproducibility_rows.latest.csv: analyzed per-run reproducibility cases in this scope",
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
    ]
    for src, root, name in latest_pairs:
        write_latest_alias(src, root, name)

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
        # Check canonical store location first, fall back to legacy compat path.
        analysis_dpath = experiment_analysis_dpath(exp_name)
        if not analysis_dpath.exists():
            analysis_dpath = compat_core_run_reports_root() / f"experiment-analysis-{slugify(exp_name)}"
        if analysis_dpath.exists():
            symlink_to(analysis_dpath, level_002 / "experiment-analysis")

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
        )

    story_index_lines = [
        "Story Index — Canonical Reading Order",
        "======================================",
        f"Generated: {generated_utc}",
        f"Scope: {scope_title}",
        "",
        "Read the five sankeys below in order to follow the full reproducibility story.",
        "",
        "s01 — Executive Operational Summary",
        "  All attempted runs: benchmark group → lifecycle status → outcome/failure reason.",
        "  File: sankey_s01_operational.latest.{html,jpg,txt}",
        "",
        "s02 — Filter Funnel to Attempted Runs",
        "  How many eligible run-specs were actually attempted, and why others were skipped.",
        "  File: sankey_s02_filter_to_attempt.latest.{html,jpg,txt}",
        "",
        "s03 — Attempted Runs to Reproducibility (exact match)",
        "  Attempted runs broken down by local reproducibility at abs_tol=0.",
        "  File: sankey_s03_attempted_to_repro.latest.{html,jpg,txt}",
        "",
        "s04 — End-to-End Coverage and Reproducibility",
        "  Full funnel from all discovered run-specs through to reproducible results.",
        "  File: sankey_s04_end_to_end.latest.{html,jpg,txt}",
        "",
        "s05 — Detailed Reproducibility Breakdown",
        "  Group → local repeatability → official-vs-local agreement → diagnosis.",
        "  File: sankey_s05_reproducibility.latest.{html,jpg,txt}",
        "",
        "Supplementary",
        "  sankey_repro_by_metric: per-metric drift (max |official - local| across runs)",
        "  alt_tolerances/: tolerance sweep variants for s03, s04, s05",
        "  agreement_curve.latest.html: agreement-rate vs tolerance curve",
        "  coverage_matrix.latest.html: model × benchmark reproducibility heat-map",
    ]
    story_index_fpath = level_001 / f"story_index_{generated_utc}.txt"
    _write_text(story_index_lines, story_index_fpath)
    write_latest_alias(story_index_fpath, level_001, "story_index.latest.txt")

    _write_scope_level_aliases(level_001, level_002, summary_root)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-name", default=None)
    parser.add_argument("--index-fpath", default=None)
    parser.add_argument("--index-dpath", default=str(default_index_root()))
    parser.add_argument("--filter-inventory-json", default=None)
    parser.add_argument("--summary-root", default=str(aggregate_summary_reports_root()))
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
    filter_inventory_rows = _load_filter_inventory_rows(filter_inventory_json)
    _raise_fd_limit()  # Note: this probably is not necessary, as fd limits are usually due to a VM issue.
    configure_plotly_chrome()
    all_repro_rows = _load_all_repro_rows()

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
    logger.info(f"Wrote executive summary root: {scope_root}")


if __name__ == "__main__":
    main()
