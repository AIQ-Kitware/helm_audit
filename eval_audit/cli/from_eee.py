"""eval-audit-from-eee: build a comparison report from a directory of EEE artifacts.

Tutorial-grade entry point for the EEE-only path. Inputs:

* a root directory containing ``official/`` and ``local/`` subtrees, each
  containing one or more EEE artifact directories of the shape produced by
  ``every_eval_ever convert helm`` (i.e.,
  ``<root>/<dataset>/<dev>/<model>/<uuid>.json`` plus a sibling
  ``<uuid>_samples.jsonl``).

* an output directory.

The CLI:

  1. Walks both subtrees and builds in-memory index rows (no HELM
     metadata, no run_spec.json, no audit_results_index.csv on disk).
     The aggregate JSON of each EEE artifact provides the model id and
     the dataset name; the directory name above ``<uuid>.json`` gives the
     experiment name (for ``local/<experiment>/...``).

  2. Writes the synthesized indexes as CSVs alongside the output report
     so the rest of the pipeline (which is index-driven) can consume
     them unchanged.

  3. Calls ``core_report_planner.build_planning_artifact`` to pair up
     official and local runs by logical key, then runs ``rebuild_core``
     on each packet to render per-pair core-metric reports + comparability
     facts.

Each packet's report dir gets the standard ``redraw_plots.latest.sh`` /
``reproduce.latest.sh`` siblings so the user can iterate on plot styling
without re-running the analysis.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid as uuidlib
from pathlib import Path
from typing import Any, Iterable

from eval_audit.infra.fs_publish import write_text_atomic
from eval_audit.infra.logging import setup_cli_logging
from eval_audit.planning.core_report_planner import build_planning_artifact
from eval_audit.workflows.plan_core_report_packets import write_planning_outputs


# ---------------------------------------------------------------------------
# EEE artifact discovery
# ---------------------------------------------------------------------------


def _discover_eee_artifacts(root: Path) -> list[dict[str, Any]]:
    """Walk ``root`` for EEE aggregate files and return one row per artifact dir.

    An "artifact dir" is the directory containing a ``<uuid>.json`` and the
    sibling ``<uuid>_samples.jsonl``. Multiple artifacts in the same dir are
    returned as separate rows.
    """
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for json_path in sorted(root.rglob("*.json")):
        if json_path.name in {
            "fixture_manifest.json",
            "provenance.json",
            "status.json",
        }:
            continue
        try:
            data = json.loads(json_path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        # EEE EvaluationLog has these top-level keys; cheap structural check
        # avoids importing every_eval_ever just to discover.
        if "evaluation_results" not in data or "model_info" not in data:
            continue
        rows.append({
            "json_path": json_path,
            "data": data,
        })
    return rows


def _extract_artifact_meta(row: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """From a discovered artifact, pull model / benchmark / experiment fields."""
    data = row["data"]
    json_path: Path = row["json_path"]
    artifact_dir = json_path.parent

    model_info = data.get("model_info") or {}
    model_id = (model_info.get("id") or model_info.get("name") or "").strip()
    eval_results = data.get("evaluation_results") or []
    if eval_results:
        first = eval_results[0]
        source_data = first.get("source_data") or {}
        benchmark = (
            source_data.get("dataset_name")
            or first.get("evaluation_name")
            or "unknown"
        )
    else:
        benchmark = "unknown"

    # Experiment name = the path component just below "local/" (if present),
    # so the user can group local attempts however they like
    # (local/<experiment>/<benchmark>/<dev>/<model>/...).
    rel = artifact_dir.relative_to(root)
    experiment_name: str | None = None
    if len(rel.parts) > 4:
        experiment_name = rel.parts[0]
    return {
        "artifact_dir": artifact_dir,
        "json_path": json_path,
        "model_id": model_id,
        "benchmark": benchmark,
        "experiment_name": experiment_name,
        "evaluation_id": data.get("evaluation_id"),
    }


def _stable_short_hash(*parts: str) -> str:
    return uuidlib.uuid5(uuidlib.NAMESPACE_URL, "::".join(parts)).hex[:12]


def _build_logical_run_key(meta: dict[str, Any]) -> str:
    """``<benchmark>:model=<model_id>`` — the comparison identity."""
    return f"{meta['benchmark']}:model={meta['model_id']}"


def _build_official_index_row(meta: dict[str, Any]) -> dict[str, Any]:
    logical_run_key = _build_logical_run_key(meta)
    component_id = (
        f"official::eee_only::{meta['model_id']}::{meta['benchmark']}::"
        f"{_stable_short_hash(str(meta['artifact_dir']))}"
    )
    return {
        "source_kind": "official",
        "artifact_format": "eee",
        "eee_artifact_path": str(meta["artifact_dir"]),
        "component_id": component_id,
        "logical_run_key": logical_run_key,
        "run_name": logical_run_key,
        "run_spec_name": logical_run_key,
        "model": meta["model_id"],
        "benchmark": meta["benchmark"],
        "public_track": "eee_only_demo",
        "suite_version": "v1",
        "has_run_spec": "True",
    }


def _build_local_index_row(meta: dict[str, Any]) -> dict[str, Any]:
    logical_run_key = _build_logical_run_key(meta)
    experiment_name = meta["experiment_name"] or "eee_only_local"
    artifact_short = _stable_short_hash(str(meta["artifact_dir"]))
    job_id = f"job_{artifact_short}"
    component_id = (
        f"local::{experiment_name}::{job_id}::{meta.get('evaluation_id') or artifact_short}"
    )
    return {
        "source_kind": "local",
        "artifact_format": "eee",
        "eee_artifact_path": str(meta["artifact_dir"]),
        "component_id": component_id,
        "logical_run_key": logical_run_key,
        "run_entry": logical_run_key,
        "run_spec_name": logical_run_key,
        "model": meta["model_id"],
        "benchmark": meta["benchmark"],
        "experiment_name": experiment_name,
        "job_id": job_id,
        "attempt_uuid": meta.get("evaluation_id") or artifact_short,
        "attempt_identity": meta.get("evaluation_id") or artifact_short,
        "attempt_identity_kind": "eee_evaluation_id",
        "machine_host": "eee_only_demo",
        "status": "computed",
        "has_run_spec": "True",
    }


# ---------------------------------------------------------------------------
# Index synthesis
# ---------------------------------------------------------------------------


def _write_index_csv(rows: list[dict[str, Any]], fpath: Path) -> Path:
    """Write rows to a CSV with stable header ordering.

    The header is the union of all row keys in sorted order; missing keys
    are written as empty strings. This avoids a hard-coded schema while
    still producing a CSV that the planner's ``csv.DictReader`` can read.
    """
    if not rows:
        write_text_atomic(fpath, "")
        return fpath
    fieldnames = sorted({k for r in rows for k in r.keys()})
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in fieldnames})
    write_text_atomic(fpath, buf.getvalue())
    return fpath


# ---------------------------------------------------------------------------
# Per-packet rebuild via the analyze_experiment workflow
# ---------------------------------------------------------------------------


def _render_packet(
    *,
    packet: dict[str, Any],
    out_root: Path,
    components_manifest_fpath: Path,
    comparisons_manifest_fpath: Path,
    plot_layout_args: list[str],
    render_heavy_plots: bool,
) -> Path:
    """Run rebuild_core_report on a single planner packet.

    The packet manifests are pre-written next to the output dir; this just
    invokes core_metrics so the per-pair plots + comparability facts are
    rendered.

    Output layout mirrors the canonical
    ``<root>/<experiment_name>/core-reports/<packet>/...`` shape so that
    ``eval-audit-build-summary --analysis-root <out_root>`` can pick the
    reports up via its standard glob without bespoke wiring.
    """
    packet_id = packet["packet_id"]
    experiment_name = (
        packet["components_manifest"].get("experiment_name") or "eee_only"
    )
    report_dpath = out_root / experiment_name / "core-reports" / packet_id
    report_dpath.mkdir(parents=True, exist_ok=True)

    (report_dpath / "components_manifest.latest.json").write_text(
        json.dumps(packet["components_manifest"], indent=2) + "\n"
    )
    (report_dpath / "comparisons_manifest.latest.json").write_text(
        json.dumps(packet["comparisons_manifest"], indent=2) + "\n"
    )

    cmd: list[str] = [
        sys.executable, "-m", "eval_audit.reports.core_metrics",
        "--report-dpath", str(report_dpath),
        "--components-manifest", str(report_dpath / "components_manifest.latest.json"),
        "--comparisons-manifest", str(report_dpath / "comparisons_manifest.latest.json"),
    ]
    if render_heavy_plots:
        cmd.append("--render-heavy-pairwise-plots")
    cmd += plot_layout_args
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + (
        os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else ""
    )
    subprocess.run(cmd, check=True, env=env)
    return report_dpath


def _packets_with_manifests(planning_artifact: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield per-packet dicts ready for ``_render_packet``.

    ``build_planning_artifact`` returns a single artifact dict containing all
    packets together; ``rebuild_core_report`` expects per-packet
    ``components_manifest`` + ``comparisons_manifest`` (one each per pair).
    """
    for packet in planning_artifact.get("packets", []):
        components = packet.get("components") or []
        comparisons = packet.get("comparisons") or []
        components_manifest = {
            "report_dpath": "",  # filled in by core_metrics
            "packet_id": packet.get("packet_id"),
            "run_entry": packet.get("run_entry"),
            "experiment_name": packet.get("experiment_name"),
            "planner_version": planning_artifact.get("planner_version"),
            "selected_public_track": packet.get("selected_public_track"),
            "warnings": packet.get("warnings") or [],
            "caveats": packet.get("caveats") or [],
            "comparability_facts": packet.get("comparability_facts") or {},
            "official_selection": packet.get("official_selection") or {},
            "components": components,
        }
        comparisons_manifest = {
            "report_dpath": "",
            "run_entry": packet.get("run_entry"),
            "experiment_name": packet.get("experiment_name"),
            "comparisons": comparisons,
        }
        yield {
            "packet_id": packet.get("packet_id"),
            "components_manifest": components_manifest,
            "comparisons_manifest": comparisons_manifest,
        }


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _build_indexes(
    *,
    eee_root: Path,
    out_dir: Path,
) -> tuple[Path, Path, list[dict[str, Any]], list[dict[str, Any]]]:
    """Discover artifacts and write the synthesized index CSVs.

    Returns ``(local_index_fpath, official_index_fpath, local_rows, official_rows)``.
    """
    official_root = eee_root / "official"
    local_root = eee_root / "local"

    official_artifacts = _discover_eee_artifacts(official_root)
    local_artifacts = _discover_eee_artifacts(local_root)

    if not official_artifacts and not local_artifacts:
        raise SystemExit(
            f"FAIL: no EEE artifacts found under {eee_root}. Expected layout:\n"
            f"  {eee_root}/official/<dataset>/<dev>/<model>/<uuid>.json\n"
            f"  {eee_root}/local/<experiment>/<dataset>/<dev>/<model>/<uuid>.json"
        )

    official_rows = [
        _build_official_index_row(_extract_artifact_meta(row, root=official_root))
        for row in official_artifacts
    ]
    local_rows = [
        _build_local_index_row(_extract_artifact_meta(row, root=local_root))
        for row in local_artifacts
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    official_index_fpath = _write_index_csv(
        official_rows, out_dir / "official_public_index.latest.csv"
    )
    local_index_fpath = _write_index_csv(
        local_rows, out_dir / "audit_results_index.latest.csv"
    )
    return local_index_fpath, official_index_fpath, local_rows, official_rows


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--eee-root",
        required=True,
        help="Root of the EEE artifact tree; expects official/ and local/ subdirs.",
    )
    parser.add_argument(
        "--out-dpath",
        required=True,
        help="Output directory for the synthesized indexes + per-packet reports.",
    )
    parser.add_argument(
        "--experiment-name",
        default="eee_only_demo",
        help="Logical name for this comparison run; used in component ids and "
             "as the planner's experiment_name filter.",
    )
    parser.add_argument(
        "--render-heavy-pairwise-plots",
        action="store_true",
        default=False,
        help="Render per-pair distribution + per-metric agreement PNGs (slow).",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="Remove --out-dpath before building.",
    )
    parser.add_argument(
        "--build-aggregate-summary",
        action="store_true",
        default=False,
        help=(
            "After per-packet reports finish, run eval-audit-build-summary "
            "against the per-experiment subtrees produced under --out-dpath "
            "to generate a cross-packet aggregate report (agreement curves, "
            "per-metric breakdowns, README). The Stage-1 filter inventory is "
            "skipped automatically since EEE-only inputs have no Stage-1 "
            "filter sankey to fold in."
        ),
    )
    args, plot_layout_args = parser.parse_known_args(argv)

    eee_root = Path(args.eee_root).expanduser().resolve()
    out_dir = Path(args.out_dpath).expanduser().resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)

    local_index_fpath, official_index_fpath, local_rows, official_rows = _build_indexes(
        eee_root=eee_root, out_dir=out_dir
    )
    print(
        f"discovered: {len(official_rows)} official + {len(local_rows)} local artifacts under {eee_root}"
    )
    print(f"  official_index: {official_index_fpath}")
    print(f"  local_index:    {local_index_fpath}")

    planning_artifact = build_planning_artifact(
        local_index_fpath=local_index_fpath,
        official_index_fpath=official_index_fpath,
        experiment_name=None,  # don't filter by experiment_name; demo includes all
        run_entry=None,
    )

    planning_dpath = out_dir / "planning"
    write_planning_outputs(artifact=planning_artifact, out_dpath=planning_dpath)
    print(f"  planning_dir:   {planning_dpath}")

    n_packets = planning_artifact.get("packet_count", 0)
    n_pairs = sum(
        len(packet.get("comparisons") or [])
        for packet in planning_artifact.get("packets", [])
    )
    print(f"planner: {n_packets} packets, {n_pairs} pairwise comparisons")

    rendered = []
    for entry in _packets_with_manifests(planning_artifact):
        report_dpath = _render_packet(
            packet=entry,
            out_root=out_dir,
            components_manifest_fpath=Path(),  # constructed inline
            comparisons_manifest_fpath=Path(),
            plot_layout_args=plot_layout_args,
            render_heavy_plots=args.render_heavy_pairwise_plots,
        )
        rendered.append(report_dpath)
        print(f"  rendered: {report_dpath}")

    print(f"\nDONE: {len(rendered)} per-pair core-metric reports under {out_dir}/<experiment>/core-reports/")

    if args.build_aggregate_summary:
        summary_root = out_dir / "aggregate-summary"
        summary_cmd = [
            sys.executable, "-m", "eval_audit.workflows.build_reports_summary",
            "--no-filter-inventory",
            "--no-canonical-scan",
            "--analysis-root", str(out_dir),
            "--index-fpath", str(local_index_fpath),
            "--summary-root", str(summary_root),
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + (
            os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else ""
        )
        print(f"\nBuilding aggregate summary under {summary_root}/ ...")
        subprocess.run(summary_cmd, check=True, env=env)
        print(f"DONE: aggregate summary at {summary_root}/all-results/")


if __name__ == "__main__":
    main()
