from __future__ import annotations

import argparse

from helm_audit.workflows import analyze_experiment, build_reports_summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Analyze multiple experiments in one Python process so shared caches are reused."
    )
    parser.add_argument(
        "--experiment-name",
        dest="experiment_names",
        action="append",
        required=True,
        help="Experiment name to analyze. Repeat this flag to analyze multiple experiments.",
    )
    parser.add_argument("--index-fpath", required=True)
    parser.add_argument("--allow-single-repeat", action="store_true")
    parser.add_argument("--build-summary", action="store_true")
    parser.add_argument("--filter-inventory-json", default=None)
    args = parser.parse_args(argv)

    for experiment_name in args.experiment_names:
        print(f"BEGIN {experiment_name}", flush=True)
        cmd = [
            "--experiment-name",
            experiment_name,
            "--index-fpath",
            args.index_fpath,
        ]
        if args.allow_single_repeat:
            cmd.append("--allow-single-repeat")
        analyze_experiment.main(cmd)
        print(f"END {experiment_name}", flush=True)

    if args.build_summary:
        print("BEGIN build_reports_summary", flush=True)
        cmd = ["--index-fpath", args.index_fpath]
        if args.filter_inventory_json:
            cmd.extend(["--filter-inventory-json", args.filter_inventory_json])
        build_reports_summary.main(cmd)
        print("END build_reports_summary", flush=True)


if __name__ == "__main__":
    main()
