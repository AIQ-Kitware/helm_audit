"""Build a virtual experiment from a YAML manifest.

A virtual experiment is a declarative slice over existing audited runs
(plus, in a later iteration, externally-produced EEE artifacts). The
output is written outside the repo at the manifest's ``output.root``,
so derived results never pollute the checked-in tree.

Usage::

    eval-audit-build-virtual-experiment \
        --manifest configs/virtual-experiments/pythia-mmlu-stress.yaml \
        --ensure-local-eee \
        --allow-single-repeat
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from loguru import logger

from eval_audit.infra.logging import rich_link, setup_cli_logging
from eval_audit.virtual import (
    compose_virtual_experiment,
    load_manifest,
    write_synthesized_indexes,
)
from eval_audit.virtual.compose import (
    build_scoped_filter_inventory,
    provenance_payload,
    write_scoped_filter_inventory,
)
from eval_audit.virtual.coverage import (
    compute_coverage,
    write_coverage_artifacts,
)
from eval_audit.workflows import analyze_experiment

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func


def _copy_manifest(manifest_fpath: Path, dest_dpath: Path) -> Path:
    """Snapshot the manifest into the output dir for reproducibility."""
    dest_dpath.mkdir(parents=True, exist_ok=True)
    dest = dest_dpath / "manifest.yaml"
    shutil.copyfile(manifest_fpath, dest)
    return dest


@profile
def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Path to virtual-experiment YAML manifest.")
    parser.add_argument("--ensure-local-eee", action="store_true",
                        help="Convert local HELM runs to EEE on demand if canonical local artifacts are missing.")
    parser.add_argument("--allow-single-repeat", action="store_true",
                        help="Pass through to analyze_experiment so packets with one local component still build.")
    parser.add_argument("--official-eee-root", default=None)
    parser.add_argument("--local-eee-root", default=None)
    parser.add_argument("--compose-only", action="store_true",
                        help="Only synthesize index slices and write provenance; skip analysis. Useful for triage.")
    args = parser.parse_args(argv)

    manifest_fpath = Path(args.manifest).expanduser().resolve()
    manifest = load_manifest(manifest_fpath)
    logger.info(f"Loaded manifest: {rich_link(manifest_fpath)} (name={manifest.name!r})")

    output_root = manifest.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    indexes_dpath = output_root / "indexes"
    analysis_dpath = output_root / "analysis"

    # Snapshot the manifest as it was at compose time.
    saved_manifest = _copy_manifest(manifest_fpath, output_root)
    logger.info(f"Wrote manifest snapshot: {rich_link(saved_manifest)}")

    # Compose: filter sources by scope, stamp virtual experiment_name, etc.
    result = compose_virtual_experiment(manifest)
    logger.info(
        f"Composed virtual experiment '{manifest.name}': "
        f"{len(result.local_rows)} local rows retained "
        f"({result.discarded_local_count} discarded), "
        f"{len(result.official_rows)} official rows retained "
        f"({result.discarded_official_count} discarded), "
        f"{len(result.external_components)} external_eee components"
    )
    if result.external_components:
        materialized = result.external_eee_materialized_counts
        logger.info(
            f"{len(result.external_components)} external_eee components "
            f"materialized: {materialized.get('local', 0)} local, "
            f"{materialized.get('official', 0)} official, "
            f"{materialized.get('discarded', 0)} discarded by manifest scope"
        )

    # Persist synthesized index slices + provenance.
    paths = write_synthesized_indexes(result, indexes_dpath=indexes_dpath)
    logger.info(f"Wrote synthesized audit index: {rich_link(paths['audit_index_fpath'])}")
    logger.info(f"Wrote synthesized official index: {rich_link(paths['official_index_fpath'])}")

    provenance_fpath = output_root / "provenance.json"
    provenance_fpath.write_text(json.dumps(provenance_payload(result), indent=2) + "\n")
    logger.info(f"Wrote provenance: {rich_link(provenance_fpath)}")

    # If any official-public-index source declared a Stage-1 pre_filter,
    # re-stamp the upstream inventory with manifest-scope-aware
    # selection_status and persist it. The publication-side
    # build_reports_summary can then render Sankey A (Universe -> Scope)
    # using this scoped inventory and naturally show the manifest scope
    # as the terminal gate of the funnel.
    #
    # If the manifest has *no* pre_filter, we delete any stale
    # scoped_filter_inventory.json from a previous compose. Otherwise
    # build_summary.sh would pick up the stale file via its existence
    # check and apply yesterday's filtering to today's scope — a
    # silent footgun that already cost us a session of debugging
    # ("why is Falcon excluded from the slim heatmap?").
    scoped_filter_inventory_fpath: Path | None = None
    stale_scoped_inv = output_root / "scoped_filter_inventory.json"
    has_pre_filter = any(
        src.pre_filter is not None for src in manifest.official_sources
    )
    if not has_pre_filter and stale_scoped_inv.is_file():
        stale_scoped_inv.unlink()
        logger.info(
            f"Removed stale scoped_filter_inventory.json from a previous "
            f"compose with a different pre_filter setting: {rich_link(stale_scoped_inv)}"
        )
    for src in manifest.official_sources:
        if src.pre_filter is None:
            continue
        if src.pre_filter.kind != "helm_stage1":
            continue
        inv_path = src.pre_filter.inventory_fpath.expanduser().resolve()
        if not inv_path.is_file():
            logger.warning(
                f"Pre-filter inventory not found at {inv_path}; "
                "Stage-A sankey won't include manifest-scope context."
            )
            continue
        try:
            pre_inventory = json.loads(inv_path.read_text())
        except Exception as ex:
            logger.warning(f"Could not load pre-filter inventory {inv_path}: {ex}")
            continue
        if not isinstance(pre_inventory, list):
            logger.warning(f"Pre-filter inventory at {inv_path} is not a list; skipping.")
            continue
        scoped_inventory = build_scoped_filter_inventory(
            manifest=manifest,
            pre_filter_inventory=pre_inventory,
        )
        scoped_filter_inventory_fpath = output_root / "scoped_filter_inventory.json"
        write_scoped_filter_inventory(scoped_inventory, out_fpath=scoped_filter_inventory_fpath)
        n_selected = sum(1 for r in scoped_inventory if r.get("selection_status") == "selected")
        logger.info(
            f"Wrote scoped filter inventory: {rich_link(scoped_filter_inventory_fpath)} "
            f"({len(scoped_inventory)} rows, {n_selected} in scope)"
        )
        break  # support one pre_filter source for now

    if not result.local_rows:
        logger.warning(
            "No local rows retained after scope+include filters; analysis would be empty. "
            "Skipping analyze_experiment."
        )
        return

    if args.compose_only:
        logger.info("--compose-only set; skipping analyze_experiment.")
        return

    # Drive the existing analysis pipeline against the synthesized slice.
    analyze_argv: list[str] = [
        "--experiment-name", manifest.name,
        "--index-fpath", str(paths["audit_index_fpath"]),
        "--official-index-fpath", str(paths["official_index_fpath"]),
        "--analysis-dpath", str(analysis_dpath),
    ]
    if args.allow_single_repeat:
        analyze_argv.append("--allow-single-repeat")
    if args.ensure_local_eee:
        analyze_argv.append("--ensure-local-eee")
    if args.official_eee_root:
        analyze_argv.extend(["--official-eee-root", str(args.official_eee_root)])
    if args.local_eee_root:
        analyze_argv.extend(["--local-eee-root", str(args.local_eee_root)])

    logger.info(f"Running analyze_experiment over the virtual slice into {rich_link(analysis_dpath)}")
    analyze_experiment.main(analyze_argv)

    # Stage-B coverage funnel: scope (target) -> reproduced -> completed -> analyzed.
    # Computed after analyze_experiment so we can populate the analyzed-stage
    # waist from the per-packet manifests on disk. Stage-A (universe -> scope)
    # is intentionally left null in the JSON for now and will be filled in by
    # the Stage-3 source-pre-filter pass.
    coverage_dpath = output_root / "reports" / "scoped_funnel"
    coverage = compute_coverage(
        name=manifest.name,
        description=manifest.description,
        target_rows=result.official_rows,
        local_rows=result.local_rows,
        analysis_root=analysis_dpath,
    )
    coverage_paths = write_coverage_artifacts(coverage, out_dpath=coverage_dpath)
    logger.info(
        f"Coverage funnel: target={coverage.n_target} "
        f"reproduced(logical)={coverage.n_reproduced_logical} "
        f"completed={coverage.n_completed} analyzed={coverage.n_analyzed}"
    )
    logger.info(f"Wrote coverage funnel summary: {rich_link(coverage_paths['summary_txt'])}")
    logger.info(f"Wrote coverage funnel json: {rich_link(coverage_paths['json'])}")
    if coverage.missing:
        logger.info(
            f"Missing targets: {len(coverage.missing)} (see {rich_link(coverage_paths['missing_csv'])})"
        )


if __name__ == "__main__":
    main()
