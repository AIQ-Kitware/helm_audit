from __future__ import annotations

import argparse
import importlib.util
import shutil

from eval_audit.infra.env import load_env
from eval_audit.infra.logging import setup_cli_logging
from eval_audit.infra.plotly_env import has_plotly_static_dependencies


def require_module(modname: str) -> None:
    if importlib.util.find_spec(modname) is None:
        raise SystemExit(f"required python module not found: {modname}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate eval_audit environment.")
    parser.add_argument("--require-precomputed-root", action="store_true")
    parser.add_argument("--require-plotly-static", action="store_true")
    parser.add_argument(
        "--plotly-static-only",
        action="store_true",
        help="Only validate plotly/kaleido/chrome static rendering dependencies.",
    )
    args = parser.parse_args(argv)
    setup_cli_logging()

    if args.plotly_static_only:
        ok, missing = has_plotly_static_dependencies()
        if not ok:
            raise SystemExit(
                "plotly static rendering is not ready; missing: "
                + ", ".join(missing)
            )
        print("plotly static rendering looks good.")
        return

    env = load_env()
    required = {
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
    require_module("magnet")
    require_module("helm")
    if args.require_plotly_static:
        ok, missing = has_plotly_static_dependencies()
        if not ok:
            raise SystemExit(
                "plotly static rendering is not ready; missing: "
                + ", ".join(missing)
            )
    print("eval_audit environment looks good.")


if __name__ == "__main__":
    main()
