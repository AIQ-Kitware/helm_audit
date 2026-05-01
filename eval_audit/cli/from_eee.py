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

Each packet's report dir gets the standard ``redraw_plots.sh`` /
``reproduce.sh`` siblings so the user can iterate on plot styling
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

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set. See the same shim in build_reports_summary.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func


# ---------------------------------------------------------------------------
# EEE artifact discovery
# ---------------------------------------------------------------------------


@profile
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


_HELM_SIDECAR_FILENAMES = ("run_spec.json",)


def detect_helm_sidecars(artifact_dir: Path) -> dict[str, Any]:
    """Look for HELM-shape sidecar files next to an EEE artifact dir.

    When a HELM run was the upstream of the EEE artifact, the user
    *can* ship the original ``run_spec.json`` alongside ``<uuid>.json``
    and ``<uuid>_samples.jsonl`` — doing so lets the planner populate
    comparability facts (scenario class, deployment, instructions,
    max_eval_instances) instead of collapsing them to ``unknown``.

    Returns ``{"run_spec_fpath": <abs path or None>,
              "max_eval_instances": <str or None>}``. The
    ``max_eval_instances`` field is parsed out of ``run_spec.json``
    because the planner expects it on the index row, not in the
    ``run_spec_fpath`` blob — every other adapter/scenario field flows
    through the planner's existing ``extract_run_spec_fields`` reader.
    """
    run_spec_fpath = artifact_dir / "run_spec.json"
    if not run_spec_fpath.is_file():
        return {"run_spec_fpath": None, "max_eval_instances": None}
    max_eval_instances: str | None = None
    try:
        spec = json.loads(run_spec_fpath.read_text())
        adapter = spec.get("adapter_spec") if isinstance(spec, dict) else None
        if isinstance(adapter, dict):
            mei = adapter.get("max_eval_instances")
            if mei is not None:
                max_eval_instances = str(mei)
    except (OSError, json.JSONDecodeError):
        pass
    return {
        "run_spec_fpath": str(run_spec_fpath),
        "max_eval_instances": max_eval_instances,
    }


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
    sidecars = detect_helm_sidecars(artifact_dir)
    return {
        "artifact_dir": artifact_dir,
        "json_path": json_path,
        "model_id": model_id,
        "benchmark": benchmark,
        "experiment_name": experiment_name,
        "evaluation_id": data.get("evaluation_id"),
        "run_spec_fpath": sidecars["run_spec_fpath"],
        "max_eval_instances": sidecars["max_eval_instances"],
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
        "run_spec_fpath": meta.get("run_spec_fpath"),
        "max_eval_instances": meta.get("max_eval_instances"),
        "model": meta["model_id"],
        "benchmark": meta["benchmark"],
        "public_track": "eee_only_demo",
        "suite_version": "v1",
        "has_run_spec": "True",
    }


def _build_local_index_row(meta: dict[str, Any], *, experiment_override: str | None = None) -> dict[str, Any]:
    logical_run_key = _build_logical_run_key(meta)
    experiment_name = experiment_override or meta["experiment_name"] or "eee_only_local"
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
        "run_spec_fpath": meta.get("run_spec_fpath"),
        "max_eval_instances": meta.get("max_eval_instances"),
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


@profile
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

    (report_dpath / "components_manifest.json").write_text(
        json.dumps(packet["components_manifest"], indent=2) + "\n"
    )
    (report_dpath / "comparisons_manifest.json").write_text(
        json.dumps(packet["comparisons_manifest"], indent=2) + "\n"
    )

    cmd: list[str] = [
        sys.executable, "-m", "eval_audit.reports.core_metrics",
        "--report-dpath", str(report_dpath),
        "--components-manifest", str(report_dpath / "components_manifest.json"),
        "--comparisons-manifest", str(report_dpath / "comparisons_manifest.json"),
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


@profile
def _build_indexes(
    *,
    eee_root: Path,
    out_dir: Path,
    experiment_name: str | None = None,
) -> tuple[Path, Path, list[dict[str, Any]], list[dict[str, Any]]]:
    """Discover artifacts and write the synthesized index CSVs.

    ``experiment_name`` overrides the per-row experiment label that would
    otherwise be derived from the ``local/<experiment>/...`` subdirectory.
    Useful when the user wants every local row grouped under one logical
    experiment regardless of the source layout.

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
        _build_local_index_row(
            _extract_artifact_meta(row, root=local_root),
            experiment_override=experiment_name,
        )
        for row in local_artifacts
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    official_index_fpath = _write_index_csv(
        official_rows, out_dir / "official_public_index.csv"
    )
    local_index_fpath = _write_index_csv(
        local_rows, out_dir / "audit_results_index.csv"
    )
    return local_index_fpath, official_index_fpath, local_rows, official_rows


@profile
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
        default=None,
        help=(
            "Override the logical experiment name on every local index row "
            "(default: derive from the directory immediately below "
            "``local/`` for each artifact, falling back to ``eee_only_local`` "
            "when the layout is too shallow). The experiment name is used in "
            "component IDs and is the parent dir of the per-packet output "
            "tree, so passing this groups everything under one experiment "
            "regardless of source layout."
        ),
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
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of packets to render concurrently. Each packet runs the "
            "core_metrics CLI in its own subprocess, so the OS handles "
            "scheduling — set this to roughly half your physical cores when "
            "joining new-format EEE artifacts (those have 21x the records of "
            "old-format files and saturate a core for several minutes per "
            "packet). Default 1 (serial) preserves the original behavior. "
            "Use 0 to mean ``os.cpu_count() // 2``."
        ),
    )
    args, plot_layout_args = parser.parse_known_args(argv)

    eee_root = Path(args.eee_root).expanduser().resolve()
    out_dir = Path(args.out_dpath).expanduser().resolve()
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)

    local_index_fpath, official_index_fpath, local_rows, official_rows = _build_indexes(
        eee_root=eee_root, out_dir=out_dir, experiment_name=args.experiment_name,
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

    # Resolve --workers. ``0`` means "auto" -> half of cpu_count (rounded up,
    # at least 1) so we leave headroom for the OS, the user's other work,
    # and the per-subprocess pandas/matplotlib spike. Negative values pin
    # to 1 with a warning.
    if args.workers == 0:
        worker_count = max(1, (os.cpu_count() or 2) // 2)
    elif args.workers < 0:
        print(
            f"  WARN: --workers={args.workers} is invalid; using 1 (serial).",
            file=sys.stderr,
        )
        worker_count = 1
    else:
        worker_count = args.workers
    print(f"rendering: {worker_count} worker(s) (--workers={args.workers})")

    rendered: list[Path] = []
    packet_entries = list(_packets_with_manifests(planning_artifact))
    if worker_count <= 1:
        # Original serial path. Preserved verbatim so the behavior of the
        # default invocation does not change.
        for entry in packet_entries:
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
    else:
        # Parallel path. ThreadPoolExecutor (not ProcessPool) because each
        # _render_packet already spawns a core_metrics subprocess; we just
        # need to keep N of them in flight without blocking. Each thread
        # does almost no in-process work, so the GIL is irrelevant.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # We collect any rendering failures and re-raise the first one
        # *after* all in-flight workers finish, so the user gets a
        # complete picture of what did/didn't render rather than a
        # mid-flight crash. ``check=True`` inside ``_render_packet`` will
        # propagate CalledProcessError, which we catch per-future.
        first_failure: tuple[str, BaseException] | None = None
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_to_packet_id = {
                pool.submit(
                    _render_packet,
                    packet=entry,
                    out_root=out_dir,
                    components_manifest_fpath=Path(),
                    comparisons_manifest_fpath=Path(),
                    plot_layout_args=plot_layout_args,
                    render_heavy_plots=args.render_heavy_pairwise_plots,
                ): entry["packet_id"]
                for entry in packet_entries
            }
            for future in as_completed(future_to_packet_id):
                packet_id = future_to_packet_id[future]
                try:
                    report_dpath = future.result()
                except BaseException as exc:  # noqa: BLE001
                    print(f"  FAILED:   {packet_id}: {exc}", file=sys.stderr)
                    if first_failure is None:
                        first_failure = (packet_id, exc)
                else:
                    rendered.append(report_dpath)
                    print(f"  rendered: {report_dpath}  ({len(rendered)}/{len(packet_entries)})")
        if first_failure is not None:
            packet_id, exc = first_failure
            raise RuntimeError(
                f"per-packet rendering failed for {packet_id}; "
                f"{len(packet_entries) - len(rendered)} packet(s) did not "
                f"complete; first failure was: {exc}"
            ) from exc

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
