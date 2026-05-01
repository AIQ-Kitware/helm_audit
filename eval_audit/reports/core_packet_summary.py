from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval_audit.reports.core_packet import comparison_sample_latest_name, load_packet_manifests


def load_core_report_packet(report_dpath: str | Path) -> dict[str, Any]:
    report_dpath = Path(report_dpath).expanduser().resolve()
    (
        components_fpath,
        components_manifest,
        comparisons_fpath,
        comparisons_manifest,
    ) = load_packet_manifests(report_dpath=report_dpath)
    components = components_manifest.get("components") or []
    comparisons = [
        comparison
        for comparison in (comparisons_manifest.get("comparisons") or [])
        if comparison.get("enabled", True)
    ]
    component_lookup = {
        str(component.get("component_id")): component
        for component in components
        if component.get("component_id")
    }
    warnings_manifest_path = report_dpath / "warnings.json"
    warnings_manifest = {}
    if warnings_manifest_path.exists():
        try:
            warnings_manifest = json.loads(warnings_manifest_path.read_text())
        except Exception:
            warnings_manifest = {}
    return {
        "report_dpath": report_dpath,
        "components_manifest_path": components_fpath,
        "comparisons_manifest_path": comparisons_fpath,
        "warnings_manifest_path": warnings_manifest_path,
        "warnings_manifest": warnings_manifest,
        "components_manifest": components_manifest,
        "comparisons_manifest": comparisons_manifest,
        "components": components,
        "comparisons": comparisons,
        "component_lookup": component_lookup,
    }


def load_core_report_bundle(report_json: str | Path) -> dict[str, Any]:
    report_json = Path(report_json).expanduser()
    report = json.loads(report_json.read_text())
    packet = load_core_report_packet(report_json.parent)
    return {
        "report_json_path": report_json.resolve(),
        "report": report,
        "packet": packet,
    }


def find_report_pair(report: dict[str, Any], comparison_kind: str) -> dict[str, Any]:
    for pair in report.get("pairs", []):
        if pair.get("comparison_kind") == comparison_kind:
            return pair
    return {}


def find_packet_comparison(packet: dict[str, Any], comparison_kind: str) -> dict[str, Any]:
    for comparison in packet.get("comparisons", []):
        if comparison.get("comparison_kind") == comparison_kind:
            return comparison
    return {}


def packet_component(packet: dict[str, Any], component_id: str | None) -> dict[str, Any]:
    if not component_id:
        return {}
    return packet.get("component_lookup", {}).get(component_id, {})


def packet_reference_component(packet: dict[str, Any], comparison_kind: str) -> dict[str, Any]:
    comparison = find_packet_comparison(packet, comparison_kind)
    return packet_component(packet, comparison.get("reference_component_id"))


def packet_component_by_source_kind(
    packet: dict[str, Any],
    comparison_kind: str,
    source_kind: str,
) -> dict[str, Any]:
    comparison = find_packet_comparison(packet, comparison_kind)
    component_lookup = packet.get("component_lookup", {})
    for component_id in comparison.get("component_ids") or []:
        component = component_lookup.get(component_id, {})
        if component.get("source_kind") == source_kind:
            return component
    return {}


def packet_local_reference_component(packet: dict[str, Any]) -> dict[str, Any]:
    comparison = find_packet_comparison(packet, "local_repeat")
    reference_component = packet_component(packet, comparison.get("reference_component_id"))
    if reference_component:
        return reference_component
    return packet_component_by_source_kind(packet, "official_vs_local", "local")


def packet_sample_artifact_name(comparison_id: str) -> str:
    return comparison_sample_latest_name(comparison_id)


def packet_sample_artifact_names(packet: dict[str, Any]) -> list[str]:
    """Per-pair sample artifact filenames for *enabled* comparisons in a packet.

    Disabled comparisons (e.g. ones the planner marked
    ``enabled: false`` for "ambiguous official candidates", "missing
    local component", etc.) never produce a per-pair sample artifact —
    the renderer skips them. Including their would-be filename here was
    a long-standing bug: ``_repair_prioritized_example_reports`` saw
    them as "missing" and triggered a full ``rebuild_core_report_main``
    re-render on every aggregate-summary run, which then produced the
    same set of artifacts (still skipping the disabled comparisons),
    so the next run repeated the cycle.

    Filter by ``enabled`` (defaulting to True for back-compat with
    older packet schemas that didn't carry the flag) so the required-
    artifact set matches what the renderer actually emits.
    """
    return [
        packet_sample_artifact_name(str(comparison.get("comparison_id")))
        for comparison in packet.get("comparisons", [])
        if comparison.get("comparison_id")
        and comparison.get("enabled", True)
    ]


def prioritized_example_artifact_names(packet: dict[str, Any]) -> list[str]:
    return [
        "core_metric_report.png",
        "core_metric_management_summary.txt",
        "components_manifest.json",
        "comparisons_manifest.json",
        "warnings.json",
        "warnings.txt",
        *packet_sample_artifact_names(packet),
    ]


def render_path_link(path: str | Path | None, *, label: str | None = None) -> str:
    """Render a path for plain-text report surfaces (txt/md files).

    Report text artifacts are read in plain terminals, editors, and grep
    output — none of which render Rich markup. Emit the path as a plain
    string so operators see a usable path instead of literal markup.
    """
    if path is None:
        return "None"
    return label if label is not None else str(path)
