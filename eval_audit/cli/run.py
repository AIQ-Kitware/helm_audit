from __future__ import annotations

import argparse
import json

from eval_audit.infra.logging import setup_cli_logging
from eval_audit.workflows.run_from_manifest import run_from_manifest


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser(
        description="Preview or execute a kwdagger experiment from a manifest."
    )
    parser.add_argument("manifest")
    parser.add_argument(
        "--run",
        type=int,
        choices=[0, 1],
        default=0,
        help="Use 0 to preview generated kwdagger argv, 1 to execute it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for --run=0.",
    )
    parser.add_argument("--root-dpath", default=None)
    parser.add_argument("--queue-name", default=None)
    parser.add_argument("--devices", default=None)
    parser.add_argument("--tmux-workers", type=int, default=None)
    parser.add_argument("--backend", default=None)
    args = parser.parse_args(argv)
    info = run_from_manifest(
        args.manifest,
        run=bool(0 if args.dry_run else args.run),
        root_dpath=args.root_dpath,
        queue_name=args.queue_name,
        devices=args.devices,
        tmux_workers=args.tmux_workers,
        backend=args.backend,
    )
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
