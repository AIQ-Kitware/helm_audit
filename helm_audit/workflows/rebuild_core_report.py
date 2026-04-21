from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
from pathlib import Path
from typing import Any

from helm_audit.infra.api import default_index_root, env_defaults
from helm_audit.infra.fs_publish import safe_unlink, symlink_to, write_latest_alias
from helm_audit.infra.logging import setup_cli_logging
from helm_audit.infra.report_layout import (
    core_run_reports_root,
    portable_repo_root_lines,
    write_reproduce_script,
)
from helm_audit.reports import core_metrics, pair_samples
from helm_audit.reports.core_packet import (
    cleanup_glob,
    component_link_basename,
    load_packet_manifests,
    slugify_identifier,
    write_manifest,
)
from helm_audit.workflows.compare_batch import (
    choose_historic_candidate,
    collect_historic_candidates,
)


def latest_index_csv(index_dpath: Path) -> Path:
    cands = sorted(index_dpath.glob("audit_results_index_*.csv"), reverse=True)
    if not cands:
        raise FileNotFoundError(f"No index csv files found in {index_dpath}")
    return cands[0]


def load_rows(index_fpath: Path) -> list[dict[str, Any]]:
    with index_fpath.open() as file:
        return list(csv.DictReader(file))


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return float("-inf")


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _build_attempt_fallback_key(row: dict[str, Any]) -> str:
    parts = {
        "experiment_name": _clean_optional_text(row.get("experiment_name")) or "unknown",
        "job_id": _clean_optional_text(row.get("job_id")) or "unknown",
        "run_entry": _clean_optional_text(row.get("run_entry")) or "unknown",
        "manifest_timestamp": _clean_optional_text(row.get("manifest_timestamp")) or "unknown",
        "machine_host": _clean_optional_text(row.get("machine_host")) or "unknown",
        "run_dir": _clean_optional_text(row.get("run_dir")) or "unknown",
    }
    return "fallback::" + "|".join(f"{key}={value}" for key, value in parts.items())


def _attempt_ref(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    attempt_uuid = _clean_optional_text(row.get("attempt_uuid"))
    attempt_fallback_key = _clean_optional_text(row.get("attempt_fallback_key")) or _build_attempt_fallback_key(row)
    return {
        "experiment_name": row.get("experiment_name"),
        "job_id": row.get("job_id"),
        "job_path": row.get("job_dpath"),
        "run_entry": row.get("run_entry"),
        "run_path": row.get("run_dir"),
        "machine_host": row.get("machine_host"),
        "manifest_timestamp": row.get("manifest_timestamp"),
        "attempt_uuid": attempt_uuid,
        "attempt_uuid_source": row.get("attempt_uuid_source"),
        "attempt_fallback_key": attempt_fallback_key,
        "attempt_identity": row.get("attempt_identity") or attempt_uuid or attempt_fallback_key,
        "attempt_identity_kind": row.get("attempt_identity_kind") or ("attempt_uuid" if attempt_uuid else "fallback"),
        "max_eval_instances": row.get("max_eval_instances"),
    }


def matching_rows(
    rows: list[dict[str, Any]],
    run_entry: str,
    experiment_name: str | None = None,
) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if row.get("run_entry") != run_entry:
            continue
        if experiment_name is not None and row.get("experiment_name") != experiment_name:
            continue
        if row.get("status") not in {"computed", "reused", "unknown", ""}:
            continue
        if row.get("has_run_spec", "").lower() not in {"true", "1"}:
            continue
        run_dir = row.get("run_dir")
        if not run_dir:
            continue
        out.append(row)
    out.sort(
        key=lambda row: (_coerce_float(row.get("manifest_timestamp")), row.get("experiment_name", "")),
        reverse=True,
    )
    return out


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


def _find_kwdagger_job_dpath(run_dpath: str | os.PathLike[str]) -> Path | None:
    current = Path(run_dpath).expanduser().resolve()
    for cand in [current, *current.parents]:
        if (cand / "job_config.json").exists():
            return cand
    return None


def _existing_component_packet(
    report_dpath: Path,
    *,
    requested_run_entry: str,
    requested_experiment_name: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        _, components_manifest, _, comparisons_manifest = load_packet_manifests(report_dpath=report_dpath)
    except Exception:
        return None, None
    if _clean_optional_text(components_manifest.get("run_entry")) != requested_run_entry:
        return None, None
    if requested_experiment_name is not None:
        if _clean_optional_text(components_manifest.get("experiment_name")) != requested_experiment_name:
            return None, None
    components = components_manifest.get("components") or []
    if not components:
        return None, None
    for component in components:
        if _existing_run_path(component.get("run_path")) is None:
            return None, None
    return components_manifest, comparisons_manifest


def _component_seed(source_kind: str, run_path: str, *, attempt_identity: str | None = None) -> str:
    if attempt_identity:
        return f"{source_kind}-{attempt_identity}"
    return f"{source_kind}-{Path(run_path).name}"


def _allocate_component_id(used_ids: set[str], source_kind: str, run_path: str, *, attempt_identity: str | None = None) -> str:
    base = slugify_identifier(_component_seed(source_kind, run_path, attempt_identity=attempt_identity))
    candidate = base
    suffix = 2
    while candidate in used_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _component_display_name(source_kind: str, index: int, run_path: str) -> str:
    base = Path(run_path).name
    if source_kind == "official":
        return f"official: {base}"
    return f"local {index}: {base}"


def _build_local_component(row: dict[str, Any], index: int, used_ids: set[str], *, is_repeat: bool) -> dict[str, Any]:
    run_path = str(Path(row["run_dir"]).expanduser().resolve())
    attempt_ref = _attempt_ref(row)
    tags = ["local"]
    if is_repeat:
        tags.append("repeat")
    component_id = _allocate_component_id(
        used_ids,
        "local",
        run_path,
        attempt_identity=_clean_optional_text((attempt_ref or {}).get("attempt_identity")),
    )
    return {
        "component_id": component_id,
        "run_path": run_path,
        "job_path": str(_find_kwdagger_job_dpath(run_path)) if _find_kwdagger_job_dpath(run_path) is not None else None,
        "source_kind": "local",
        "tags": tags,
        "display_name": _component_display_name("local", index, run_path),
        "attempt_uuid": (attempt_ref or {}).get("attempt_uuid"),
        "attempt_identity": (attempt_ref or {}).get("attempt_identity"),
        "machine_host": row.get("machine_host"),
        "experiment_name": row.get("experiment_name"),
        "run_entry": row.get("run_entry"),
        "max_eval_instances": row.get("max_eval_instances"),
        "selection_ref": attempt_ref,
    }


def _build_official_component(
    run_entry: str,
    run_path: str,
    used_ids: set[str],
    *,
    info: dict[str, Any] | None,
    experiment_name: str | None,
) -> dict[str, Any]:
    run_path = str(Path(run_path).expanduser().resolve())
    component_id = _allocate_component_id(used_ids, "official", run_path)
    tags = ["official"]
    return {
        "component_id": component_id,
        "run_path": run_path,
        "job_path": None,
        "source_kind": "official",
        "tags": tags,
        "display_name": _component_display_name("official", 1, run_path),
        "attempt_uuid": None,
        "attempt_identity": None,
        "machine_host": None,
        "experiment_name": experiment_name,
        "run_entry": run_entry,
        "max_eval_instances": (info or {}).get("chosen_requested_max_eval_instances"),
        "historic_info": info,
    }


def _build_comparisons(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    local_components = [component for component in components if component.get("source_kind") == "local"]
    official_components = [component for component in components if component.get("source_kind") == "official"]
    comparisons: list[dict[str, Any]] = []
    if official_components and local_components:
        comparisons.append(
            {
                "comparison_id": "official_vs_local",
                "comparison_kind": "official_vs_local",
                "component_ids": [official_components[0]["component_id"], local_components[0]["component_id"]],
                "enabled": True,
                "reference_component_id": official_components[0]["component_id"],
                "notes": None,
                "caveats": None,
            }
        )
    if len(local_components) >= 2:
        comparisons.append(
            {
                "comparison_id": "local_repeat",
                "comparison_kind": "local_repeat",
                "component_ids": [local_components[0]["component_id"], local_components[1]["component_id"]],
                "enabled": True,
                "reference_component_id": local_components[0]["component_id"],
                "notes": None,
                "caveats": None,
            }
        )
    return comparisons


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


def _cleanup_legacy_report_surfaces(report_dpath: Path, comparison_ids: list[str]) -> None:
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
    keep_names = {f"instance_samples_{slugify_identifier(comparison_id)}.latest.txt" for comparison_id in comparison_ids}
    for path in report_dpath.glob("instance_samples_*.latest.txt"):
        if path.name not in keep_names:
            safe_unlink(path)


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-entry", required=True)
    parser.add_argument("--index-fpath", default=None)
    parser.add_argument("--index-dpath", default=str(default_index_root()))
    parser.add_argument("--precomputed-root", default=env_defaults()["HELM_PRECOMPUTED_ROOT"])
    parser.add_argument("--report-dpath", default=None)
    parser.add_argument("--allow-single-repeat", action="store_true")
    parser.add_argument("--experiment-name", default=None)
    args = parser.parse_args(argv)

    report_dpath = (
        Path(args.report_dpath)
        if args.report_dpath
        else (core_run_reports_root() / "manual" / f"core-metrics-{slugify_identifier(args.run_entry)}")
    )
    report_dpath = report_dpath.expanduser().resolve()
    report_dpath.mkdir(parents=True, exist_ok=True)

    components_manifest = None
    comparisons_manifest = None
    existing_components_manifest, existing_comparisons_manifest = _existing_component_packet(
        report_dpath,
        requested_run_entry=args.run_entry,
        requested_experiment_name=args.experiment_name,
    )
    if existing_components_manifest is not None and existing_comparisons_manifest is not None:
        components_manifest = existing_components_manifest
        comparisons_manifest = existing_comparisons_manifest
    else:
        index_fpath = (
            Path(args.index_fpath).expanduser().resolve()
            if args.index_fpath
            else latest_index_csv(Path(args.index_dpath).expanduser().resolve())
        )
        rows = load_rows(index_fpath)
        match_rows = matching_rows(rows, args.run_entry, experiment_name=args.experiment_name)
        if not match_rows:
            if args.experiment_name is not None:
                raise SystemExit(
                    f"No matching local runs for run_entry={args.run_entry!r} "
                    f"within experiment_name={args.experiment_name!r}"
                )
            raise SystemExit(f"No matching local runs for run_entry={args.run_entry!r}")
        if len(match_rows) < 2 and not args.allow_single_repeat:
            raise SystemExit(
                f"Need at least 2 matching local runs for run_entry={args.run_entry!r}; "
                f"found {len(match_rows)}. Use --allow-single-repeat to produce a single-run core report."
            )

        selected_local_rows = match_rows[:2] if len(match_rows) >= 2 else match_rows[:1]
        preferred_row = selected_local_rows[0]
        try:
            desired_max = int(preferred_row.get("max_eval_instances")) if preferred_row.get("max_eval_instances") else None
        except Exception:
            desired_max = None

        historic_candidates = collect_historic_candidates(args.precomputed_root, args.run_entry)
        chosen_historic, historic_info = choose_historic_candidate(historic_candidates, desired_max)
        if chosen_historic is None:
            raise SystemExit(f"No historic HELM candidate found for run_entry={args.run_entry!r}")

        used_ids: set[str] = set()
        components: list[dict[str, Any]] = []
        for index, row in enumerate(selected_local_rows, start=1):
            components.append(
                _build_local_component(
                    row,
                    index,
                    used_ids,
                    is_repeat=(index > 1),
                )
            )
        components.append(
            _build_official_component(
                args.run_entry,
                chosen_historic["run_dir"],
                used_ids,
                info=historic_info,
                experiment_name=args.experiment_name,
            )
        )

        comparisons = _build_comparisons(components)
        components_manifest = {
            "report_dpath": str(report_dpath),
            "run_entry": args.run_entry,
            "experiment_name": args.experiment_name,
            "index_fpath": str(index_fpath),
            "components": components,
        }
        comparisons_manifest = {
            "report_dpath": str(report_dpath),
            "run_entry": args.run_entry,
            "experiment_name": args.experiment_name,
            "comparisons": comparisons,
        }

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
    comparisons = [comparison for comparison in (comparisons_manifest.get("comparisons") or []) if comparison.get("enabled", True)]
    _write_component_symlinks(report_dpath, components)
    _cleanup_legacy_report_surfaces(report_dpath, [comparison["comparison_id"] for comparison in comparisons])

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

    for comparison in comparisons:
        component_ids = comparison.get("component_ids") or []
        component_lookup = {component["component_id"]: component for component in components}
        if len(component_ids) != 2:
            continue
        run_a = component_lookup[component_ids[0]]["run_path"]
        run_b = component_lookup[component_ids[1]]["run_path"]
        pair_samples.write_pair_samples(
            run_a=str(run_a),
            run_b=str(run_b),
            label=str(comparison["comparison_id"]),
            report_dpath=report_dpath,
        )

    cmd_parts = [
        "-m",
        "helm_audit.workflows.rebuild_core_report",
        "--run-entry",
        args.run_entry,
        "--precomputed-root",
        str(args.precomputed_root),
        "--report-dpath",
        str(report_dpath),
        *(["--index-fpath", components_manifest.get("index_fpath")] if components_manifest.get("index_fpath") else []),
        *(["--allow-single-repeat"] if args.allow_single_repeat else []),
        *(["--experiment-name", args.experiment_name] if args.experiment_name else []),
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


if __name__ == "__main__":
    setup_cli_logging()
    main()
