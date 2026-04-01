from __future__ import annotations

import argparse

from helm_audit.workflows.run_from_manifest import run_from_manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a kwdagger experiment from a manifest.")
    parser.add_argument("manifest")
    args = parser.parse_args(argv)
    info = run_from_manifest(args.manifest)
    print(info)


if __name__ == "__main__":
    main()
