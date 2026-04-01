from __future__ import annotations

import argparse
import shutil

from helm_audit.infra.env import load_env


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate helm_audit environment.")
    parser.add_argument("--require-precomputed-root", action="store_true")
    args = parser.parse_args(argv)

    env = load_env()
    required = {
        "AIQ_MAGNET_ROOT": env.aiq_magnet_root,
        "AUDIT_RESULTS_ROOT": env.audit_results_root,
    }
    if args.require_precomputed_root:
        required["HELM_PRECOMPUTED_ROOT"] = env.helm_precomputed_root
    for key, path in required.items():
        if not path.exists():
            raise SystemExit(f"{key} does not exist: {path}")
    for exe in ["kwdagger", "helm-run", env.aiq_python]:
        if shutil.which(exe) is None:
            raise SystemExit(f"required executable not found: {exe}")
    print("helm_audit environment looks good.")


if __name__ == "__main__":
    main()
