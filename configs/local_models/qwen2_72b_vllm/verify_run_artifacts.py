from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_FILES = (
    "run_spec.json",
    "stats.json",
    "per_instance_stats.json",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that a HELM run directory used the expected local Qwen2-72B deployment."
    )
    parser.add_argument("run_dir")
    parser.add_argument("--expect-model", default="qwen/qwen2-72b-instruct")
    parser.add_argument("--expect-deployment", default="vllm/qwen2-72b-instruct-local")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir).expanduser().resolve()
    missing = [name for name in REQUIRED_FILES if not (run_dir / name).exists()]
    if missing:
        raise SystemExit(f"Missing expected artifacts in {run_dir}: {', '.join(missing)}")

    run_spec = json.loads((run_dir / "run_spec.json").read_text())
    adapter_spec = run_spec.get("adapter_spec", {})
    model = adapter_spec.get("model")
    deployment = adapter_spec.get("model_deployment")
    if model != args.expect_model:
        raise SystemExit(f"Expected adapter_spec.model={args.expect_model!r}, found {model!r}")
    if deployment != args.expect_deployment:
        raise SystemExit(
            f"Expected adapter_spec.model_deployment={args.expect_deployment!r}, found {deployment!r}"
        )

    summary = {
        "run_dir": str(run_dir),
        "model": model,
        "model_deployment": deployment,
        "artifacts_verified": list(REQUIRED_FILES),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
