from __future__ import annotations

import argparse

from helm_audit.manifests.builders import main as historic_main
from helm_audit.manifests.presets import main as preset_main


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build manifests.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preset")
    subparsers.add_parser("historic")
    args, remaining = parser.parse_known_args(argv)
    if args.command == "preset":
        preset_main(remaining)
    else:
        historic_main(remaining)


if __name__ == "__main__":
    main()
