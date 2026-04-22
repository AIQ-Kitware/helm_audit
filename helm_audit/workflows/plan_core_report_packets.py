from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from loguru import logger

from helm_audit.infra.fs_publish import stamped_history_dir, write_latest_alias
from helm_audit.infra.logging import rich_link, setup_cli_logging
from helm_audit.planning.core_report_planner import (
    build_planning_artifact,
    comparison_rows,
    component_rows,
    packet_rows,
    planning_summary_lines,
    warning_rows,
    warning_summary_lines,
)


def _write_json(payload: Any, fpath: Path) -> None:
    fpath.write_text(json.dumps(payload, indent=2) + "\n")


def _write_text(lines: list[str], fpath: Path) -> None:
    fpath.write_text("\n".join(lines).rstrip() + "\n")


def _write_csv(rows: list[dict[str, Any]], fpath: Path) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()}) if rows else []
    with fpath.open("w", newline="") as file:
        if not fieldnames:
            file.write("")
            return
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-index-fpath", required=True)
    parser.add_argument("--official-index-fpath", required=True)
    parser.add_argument("--experiment-name", default=None)
    parser.add_argument("--run-entry", default=None)
    parser.add_argument("--out-dpath", required=True)
    args = parser.parse_args(argv)

    out_dpath = Path(args.out_dpath).expanduser().resolve()
    out_dpath.mkdir(parents=True, exist_ok=True)
    stamp, history_dpath = stamped_history_dir(out_dpath)

    artifact = build_planning_artifact(
        local_index_fpath=args.local_index_fpath,
        official_index_fpath=args.official_index_fpath,
        experiment_name=args.experiment_name,
        run_entry=args.run_entry,
    )

    json_fpath = history_dpath / f"comparison_intents_{stamp}.json"
    txt_fpath = history_dpath / f"comparison_intents_{stamp}.txt"
    packet_csv_fpath = history_dpath / f"comparison_intent_packets_{stamp}.csv"
    component_csv_fpath = history_dpath / f"comparison_intent_components_{stamp}.csv"
    comparison_csv_fpath = history_dpath / f"comparison_intent_comparisons_{stamp}.csv"
    warnings_json_fpath = history_dpath / f"comparison_intent_warnings_{stamp}.json"
    warnings_txt_fpath = history_dpath / f"comparison_intent_warnings_{stamp}.txt"

    _write_json(artifact, json_fpath)
    _write_text(planning_summary_lines(artifact), txt_fpath)
    _write_csv(packet_rows(artifact), packet_csv_fpath)
    _write_csv(component_rows(artifact), component_csv_fpath)
    _write_csv(comparison_rows(artifact), comparison_csv_fpath)
    _write_json({"warnings": warning_rows(artifact)}, warnings_json_fpath)
    _write_text(warning_summary_lines(artifact), warnings_txt_fpath)

    write_latest_alias(json_fpath, out_dpath, "comparison_intents.latest.json")
    write_latest_alias(txt_fpath, out_dpath, "comparison_intents.latest.txt")
    write_latest_alias(packet_csv_fpath, out_dpath, "comparison_intent_packets.latest.csv")
    write_latest_alias(component_csv_fpath, out_dpath, "comparison_intent_components.latest.csv")
    write_latest_alias(comparison_csv_fpath, out_dpath, "comparison_intent_comparisons.latest.csv")
    write_latest_alias(warnings_json_fpath, out_dpath, "warnings.latest.json")
    write_latest_alias(warnings_txt_fpath, out_dpath, "warnings.latest.txt")

    logger.info(f"Wrote comparison intents json: {rich_link(json_fpath)}")
    logger.info(f"Wrote comparison intents text: {rich_link(txt_fpath)}")
    logger.info(f"Wrote comparison intent packets csv: {rich_link(packet_csv_fpath)}")
    logger.info(f"Wrote comparison intent components csv: {rich_link(component_csv_fpath)}")
    logger.info(f"Wrote comparison intent comparisons csv: {rich_link(comparison_csv_fpath)}")
    logger.info(f"Wrote planner warnings json: {rich_link(warnings_json_fpath)}")
    logger.info(f"Wrote planner warnings text: {rich_link(warnings_txt_fpath)}")


if __name__ == "__main__":
    setup_cli_logging()
    main()
