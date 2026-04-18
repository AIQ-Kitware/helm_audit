from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adapter import export_benchmark_bundle, load_profile_contract


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="helm_audit integration layer for consuming vllm_service serving profiles."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("describe-contract")
    s.add_argument("profile")
    s.add_argument("--backend", default=None)
    s.add_argument("--simulate-hardware", default=None)
    s.add_argument("--vllm-root", default=None)
    s.set_defaults(cmd_name="describe-contract")

    s = sub.add_parser("export-benchmark-bundle")
    s.add_argument("profile", nargs="?", default=None)
    s.add_argument("--preset", default=None)
    s.add_argument("--bundle-root", default=None)
    s.add_argument("--backend", default=None)
    s.add_argument("--simulate-hardware", default=None)
    s.add_argument("--vllm-root", default=None)
    s.add_argument("--access-kind", default=None)
    s.add_argument("--base-url", default=None)
    s.add_argument("--api-key-value", default=None)
    s.set_defaults(cmd_name="export-benchmark-bundle")

    args = parser.parse_args(argv)
    if args.cmd_name == "describe-contract":
        data = load_profile_contract(
            args.profile,
            backend=args.backend,
            simulate_hardware=args.simulate_hardware,
            vllm_root=Path(args.vllm_root) if args.vllm_root else None,
        )
        print(json.dumps(data, indent=2))
        return

    if args.profile is None and args.preset is None:
        raise SystemExit("Either a profile or a preset is required")
    result = export_benchmark_bundle(
        args.profile or "",
        preset=args.preset,
        bundle_root=Path(args.bundle_root) if args.bundle_root else None,
        backend=args.backend,
        simulate_hardware=args.simulate_hardware,
        vllm_root=Path(args.vllm_root) if args.vllm_root else None,
        access_kind=args.access_kind,
        base_url=args.base_url,
        api_key_value=args.api_key_value,
    )
    print(json.dumps({
        "bundle_dir": str(result["bundle_dir"]),
        "bundle_path": str(result["bundle_path"]),
        "model_deployments_path": str(result["model_deployments_path"]),
        "benchmark_smoke_manifest_path": str(result["benchmark_smoke_manifest_path"]),
        "benchmark_full_manifest_path": str(result["benchmark_full_manifest_path"]),
    }, indent=2))


if __name__ == "__main__":
    main()
