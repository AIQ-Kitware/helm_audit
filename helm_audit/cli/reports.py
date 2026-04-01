from __future__ import annotations

import argparse

from helm_audit.reports.aggregate import main as aggregate_main
from helm_audit.reports.core_metrics import main as core_main
from helm_audit.reports.pair_report import main as pair_main
from helm_audit.reports.quantiles import main as quantiles_main
from helm_audit.workflows.analyze_experiment import main as experiment_main


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Report-oriented CLI surface.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("pair")
    subparsers.add_parser("core")
    subparsers.add_parser("aggregate")
    subparsers.add_parser("quantiles")
    subparsers.add_parser("experiment")
    args, remaining = parser.parse_known_args(argv)
    if args.command == "pair":
        pair_main(remaining)
    elif args.command == "core":
        core_main(remaining)
    elif args.command == "aggregate":
        aggregate_main(remaining)
    elif args.command == "quantiles":
        quantiles_main(remaining)
    else:
        experiment_main(remaining)


if __name__ == "__main__":
    main()
