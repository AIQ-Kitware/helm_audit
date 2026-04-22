from __future__ import annotations

import argparse
import csv
import json
import shlex
from pathlib import Path
from typing import Any

from loguru import logger

from helm_audit.infra.api import default_index_root
from helm_audit.infra.fs_publish import safe_unlink, symlink_to, write_latest_alias
from helm_audit.infra.logging import rich_link, setup_cli_logging
from helm_audit.infra.paths import official_public_index_dpath
from helm_audit.infra.report_layout import (
    core_run_reports_root,
    portable_repo_root_lines,
    write_reproduce_script,
)
from helm_audit.planning.core_report_planner import (
    build_planning_artifact,
    load_planning_artifact,
    select_packet_from_artifact,
)
from helm_audit.reports import core_metrics, pair_samples
from helm_audit.reports.core_packet import (
    cleanup_glob,
    comparison_sample_latest_name,
    component_link_basename,
    load_packet_manifests,
    write_manifest,
    slugify_identifier,
)


def latest_index_csv(index_dpath: Path) -> Path:
    cands = sorted(index_dpath.glob("audit_results_index_*.csv"), reverse=True)
    if not cands:
        raise FileNotFoundError(f"No local index csv files found in {index_dpath}")
    return cands[0]


def load_rows(index_fpath: Path) -> list[dict[str, Any]]:
    with index_fpath.open(newline="") as file:
        return [{k: ("" if v is None else v) for k, v in row.items()} for row in csv.DictReader(file)]


def latest_official_index_csv(index_dpath: Path) -> Path:
    latest_alias = index_dpath / "official_public_index.latest.csv"
    if latest_alias.exists():
        return latest_alias.resolve()
    cands = sorted(index_dpath.glob("official_public_index_*.csv"), reverse=True)
    if not cands:
        raise FileNotFoundError(f"No official public index csv files found in {index_dpath}")
    return cands[0]


def _maybe_latest_local_index_csv(index_dpath: Path) -> str | None:
    try:
        return str(latest_index_csv(index_dpath))
    except Exception:
        return None


def _maybe_latest_official_index_csv(index_dpath: Path) -> str | None:
    try:
        return str(latest_official_index_csv(index_dpath))
    except Exception:
        return None


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower() in {"none", "nan"}:
        return None
    return text


def _existing_run_path(value: Any) -> str | None:
    text = _clean_optional_text(value)
    if not text:
        return None
    try:
        path = Path(text).expanduser().resolve()
    except Exception:
        return None
    if not path.exists():
        return None
    return str(path)


def _existing_report_packet(
    report_dpath: Path,
    *,
    requested_run_entry: str | None,
    requested_experiment_name: str | None,
    requested_packet_id: str | None,
) -> dict[str, Any] | None:
    try:
        _, components_manifest, _, comparisons_manifest = load_packet_manifests(report_dpath=report_dpath)
    except Exception:
        return None
    if requested_packet_id is not None and _clean_optional_text(components_manifest.get("packet_id")) != requested_packet_id:
        return None
    if requested_run_entry is not None and _clean_optional_text(components_manifest.get("run_entry")) != requested_run_entry:
        return None
    if requested_experiment_name is not None and _clean_optional_text(components_manifest.get("experiment_name")) != requested_experiment_name:
        return None
    components = components_manifest.get("components") or []
    if not components:
        return None
    for component in components:
        if _existing_run_path(component.get("run_path")) is None:
            return None
    return {
        "packet_id": components_manifest.get("packet_id"),
        "run_entry": components_manifest.get("run_entry"),
        "logical_run_key": components_manifest.get("logical_run_key"),
        "experiment_name": components_manifest.get("experiment_name"),
        "components": components,
        "comparisons": comparisons_manifest.get("comparisons") or [],
        "comparability_facts": components_manifest.get("comparability_facts") or {},
        "warnings": components_manifest.get("warnings") or [],
        "caveats": components_manifest.get("caveats") or [],
        "official_selection": components_manifest.get("official_selection") or {},
        "planner_version": components_manifest.get("planner_version"),
        "selected_public_track": components_manifest.get("selected_public_track"),
    }


def _render_manifests_from_planned_packet(
    packet: dict[str, Any],
    *,
    report_dpath: Path,
    local_index_fpath: str | None,
    official_index_fpath: str | None,
    planner_artifact_fpath: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    components_manifest = {
        "report_dpath": str(report_dpath),
        "packet_id": packet.get("packet_id"),
        "logical_run_key": packet.get("logical_run_key"),
        "run_entry": packet.get("run_entry"),
        "experiment_name": packet.get("experiment_name"),
        "planner_version": packet.get("planner_version"),
        "selected_public_track": packet.get("selected_public_track"),
        "official_selection": packet.get("official_selection") or {},
        "comparability_facts": packet.get("comparability_facts") or {},
        "warnings": packet.get("warnings") or [],
        "caveats": packet.get("caveats") or [],
        "local_index_fpath": local_index_fpath,
        "official_index_fpath": official_index_fpath,
        "planner_artifact_fpath": planner_artifact_fpath,
        "components": packet.get("components") or [],
    }
    comparisons_manifest = {
        "report_dpath": str(report_dpath),
        "packet_id": packet.get("packet_id"),
        "logical_run_key": packet.get("logical_run_key"),
        "run_entry": packet.get("run_entry"),
        "experiment_name": packet.get("experiment_name"),
        "planner_version": packet.get("planner_version"),
        "selected_public_track": packet.get("selected_public_track"),
        "comparisons": packet.get("comparisons") or [],
    }
    return components_manifest, comparisons_manifest


def _load_selected_packet(
    *,
    report_dpath: Path,
    run_entry: str | None,
    experiment_name: str | None,
    packet_id: str | None,
    planner_artifact_fpath: str | None,
    local_index_fpath: str | None,
    official_index_fpath: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing_packet = _existing_report_packet(
        report_dpath,
        requested_run_entry=run_entry,
        requested_experiment_name=experiment_name,
        requested_packet_id=packet_id,
    )
    if existing_packet is not None and planner_artifact_fpath is None:
        return existing_packet, {
            "planner_artifact_fpath": None,
            "local_index_fpath": local_index_fpath,
            "official_index_fpath": official_index_fpath,
        }
    if planner_artifact_fpath is not None:
        artifact = load_planning_artifact(planner_artifact_fpath)
    else:
        if run_entry is None:
            raise SystemExit("run-entry is required when planner-artifact-fpath is not provided")
        if local_index_fpath is None or official_index_fpath is None:
            raise SystemExit("Both local and official index paths are required to plan a core report")
        artifact = build_planning_artifact(
            local_index_fpath=local_index_fpath,
            official_index_fpath=official_index_fpath,
            experiment_name=experiment_name,
            run_entry=run_entry,
        )
    packet = select_packet_from_artifact(
        artifact,
        packet_id=packet_id,
        run_entry=run_entry,
        experiment_name=experiment_name,
    )
    planner_meta = {
        "planner_artifact_fpath": str(Path(planner_artifact_fpath).expanduser().resolve()) if planner_artifact_fpath else None,
        "local_index_fpath": artifact.get("local_index_fpath") or local_index_fpath,
        "official_index_fpath": artifact.get("official_index_fpath") or official_index_fpath,
    }
    return packet, planner_meta


def _write_component_symlinks(report_dpath: Path, components: list[dict[str, Any]]) -> None:
    components_dir = report_dpath / "components"
    components_dir.mkdir(parents=True, exist_ok=True)
    keep_names: set[str] = set()
    for component in components:
        base = component_link_basename(component["component_id"])
        run_name = f"{base}.run"
        symlink_to(component["run_path"], components_dir / run_name)
        keep_names.add(run_name)
        job_path = component.get("job_path")
        if job_path:
            job_name = f"{base}.job"
            symlink_to(job_path, components_dir / job_name)
            keep_names.add(job_name)
    cleanup_glob(components_dir, "*", keep_names)


def _enabled_comparisons(comparisons_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    comparisons = comparisons_manifest.get("comparisons") or []
    return [comparison for comparison in comparisons if comparison.get("enabled", True)]


def _cleanup_legacy_report_surfaces(report_dpath: Path, enabled_comparison_ids: list[str]) -> None:
    for name in [
        "kwdagger_a.run",
        "kwdagger_b.run",
        "official.run",
        "kwdagger_a.job",
        "kwdagger_b.job",
        "report_selection.latest.json",
        "core_metric_three_run_distributions.latest.png",
    ]:
        safe_unlink(report_dpath / name)
    keep_names = {
        comparison_sample_latest_name(comparison_id)
        for comparison_id in enabled_comparison_ids
    }
    for path in report_dpath.glob("instance_samples_*.latest.txt"):
        if path.name not in keep_names:
            safe_unlink(path)


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-entry", default=None)
    parser.add_argument("--packet-id", default=None)
    parser.add_argument("--index-fpath", default=None)
    parser.add_argument("--index-dpath", default=str(default_index_root()))
    parser.add_argument("--official-index-fpath", default=None)
    parser.add_argument("--official-index-dpath", default=str(official_public_index_dpath()))
    parser.add_argument("--planner-artifact-fpath", default=None)
    parser.add_argument("--report-dpath", default=None)
    parser.add_argument("--allow-single-repeat", action="store_true")
    parser.add_argument("--experiment-name", default=None)
    args = parser.parse_args(argv)

    if args.run_entry is None and args.packet_id is None:
        raise SystemExit("Either --run-entry or --packet-id is required")

    report_dpath = (
        Path(args.report_dpath)
        if args.report_dpath
        else (core_run_reports_root() / "manual" / f"core-metrics-{slugify_identifier(args.packet_id or args.run_entry)}")
    )
    report_dpath = report_dpath.expanduser().resolve()
    report_dpath.mkdir(parents=True, exist_ok=True)

    packet, planner_meta = _load_selected_packet(
        report_dpath=report_dpath,
        run_entry=args.run_entry,
        experiment_name=args.experiment_name,
        packet_id=args.packet_id,
        planner_artifact_fpath=args.planner_artifact_fpath,
        local_index_fpath=(
            str(Path(args.index_fpath).expanduser().resolve())
            if args.index_fpath else
            _maybe_latest_local_index_csv(Path(args.index_dpath).expanduser().resolve())
        ),
        official_index_fpath=(
            str(Path(args.official_index_fpath).expanduser().resolve())
            if args.official_index_fpath else
            _maybe_latest_official_index_csv(Path(args.official_index_dpath).expanduser().resolve())
        ),
    )
    local_index_fpath = planner_meta.get("local_index_fpath")
    official_index_fpath = planner_meta.get("official_index_fpath")
    if packet is not None and planner_meta.get("planner_artifact_fpath") is None and local_index_fpath is None:
        if args.index_fpath:
            local_index_fpath = str(Path(args.index_fpath).expanduser().resolve())
        else:
            local_index_fpath = str(latest_index_csv(Path(args.index_dpath).expanduser().resolve()))
    if packet is not None and planner_meta.get("planner_artifact_fpath") is None and official_index_fpath is None:
        if args.official_index_fpath:
            official_index_fpath = str(Path(args.official_index_fpath).expanduser().resolve())
        else:
            official_index_fpath = str(latest_official_index_csv(Path(args.official_index_dpath).expanduser().resolve()))
    if not args.allow_single_repeat:
        local_components = [
            component for component in (packet.get("components") or [])
            if component.get("source_kind") == "local"
        ]
        if len(local_components) < 2 and not any(
            comparison.get("comparison_kind") == "local_repeat" and comparison.get("enabled", True)
            for comparison in (packet.get("comparisons") or [])
        ):
            logger.info(
                "Rendering a single-run packet without local_repeat because the planner "
                f"declared only {len(local_components)} local component(s)"
            )

    components_manifest, comparisons_manifest = _render_manifests_from_planned_packet(
        packet,
        report_dpath=report_dpath,
        local_index_fpath=planner_meta.get("local_index_fpath"),
        official_index_fpath=planner_meta.get("official_index_fpath"),
        planner_artifact_fpath=planner_meta.get("planner_artifact_fpath"),
    )
    components_fpath = write_manifest(
        report_dpath,
        stem="components_manifest",
        latest_name="components_manifest.latest.json",
        payload=components_manifest,
    )
    comparisons_fpath = write_manifest(
        report_dpath,
        stem="comparisons_manifest",
        latest_name="comparisons_manifest.latest.json",
        payload=comparisons_manifest,
    )

    components = components_manifest.get("components") or []
    enabled_comparisons = _enabled_comparisons(comparisons_manifest)
    _write_component_symlinks(report_dpath, components)
    _cleanup_legacy_report_surfaces(
        report_dpath,
        [str(comparison["comparison_id"]) for comparison in enabled_comparisons if comparison.get("comparison_id")],
    )

    logger.info(f"Rendering core report packet_id={packet.get('packet_id')} into {rich_link(report_dpath)}")
    core_metrics.main(
        [
            "--report-dpath",
            str(report_dpath),
            "--components-manifest",
            str(components_fpath),
            "--comparisons-manifest",
            str(comparisons_fpath),
        ]
    )

    component_lookup = {
        str(component.get("component_id")): component
        for component in components
        if component.get("component_id")
    }
    for comparison in enabled_comparisons:
        component_ids = comparison.get("component_ids") or []
        if len(component_ids) != 2:
            continue
        left_component = component_lookup.get(component_ids[0]) or {}
        right_component = component_lookup.get(component_ids[1]) or {}
        run_a = left_component.get("run_path")
        run_b = right_component.get("run_path")
        if not run_a or not run_b:
            continue
        pair_samples.write_pair_samples(
            run_a=str(run_a),
            run_b=str(run_b),
            label=str(comparison["comparison_id"]),
            report_dpath=report_dpath,
        )

    cmd_parts = [
        "-m",
        "helm_audit.workflows.rebuild_core_report",
        *(["--packet-id", str(packet.get("packet_id"))] if packet.get("packet_id") else []),
        *(["--run-entry", str(packet.get("run_entry"))] if packet.get("run_entry") else []),
        "--report-dpath",
        str(report_dpath),
        "--index-fpath",
        str(planner_meta.get("local_index_fpath") or local_index_fpath),
        "--official-index-fpath",
        str(planner_meta.get("official_index_fpath") or official_index_fpath),
        *(["--planner-artifact-fpath", str(planner_meta["planner_artifact_fpath"])] if planner_meta.get("planner_artifact_fpath") else []),
        *(["--experiment-name", str(packet.get("experiment_name"))] if packet.get("experiment_name") else []),
        *(["--allow-single-repeat"] if args.allow_single_repeat else []),
    ]
    reproduce_fpath = write_reproduce_script(
        report_dpath / "reproduce.latest.sh",
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            *portable_repo_root_lines(),
            'cd "$REPO_ROOT"',
            'PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" '
            + " ".join(shlex.quote(part) for part in cmd_parts)
            + ' "$@"',
        ],
    )
    write_latest_alias(reproduce_fpath, report_dpath, "reproduce.sh")

    logger.info(f"Wrote components manifest: {rich_link(components_fpath)}")
    logger.info(f"Wrote comparisons manifest: {rich_link(comparisons_fpath)}")
    logger.info(f"Wrote reproduce script: {rich_link(reproduce_fpath)}")


if __name__ == "__main__":
    setup_cli_logging()
    main()
