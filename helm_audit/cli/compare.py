from __future__ import annotations

import argparse

from helm_audit.reports.pair_report import main as compare_pair_main
from helm_audit.workflows.compare_batch import main as compare_batch_main


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Compare runs or batches.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("pair")
    subparsers.add_parser("batch")
    args, remaining = parser.parse_known_args(argv)
    if args.command == "pair":
        compare_pair_main(remaining)
    else:
        compare_batch_main(remaining)


if __name__ == "__main__":
    main()
