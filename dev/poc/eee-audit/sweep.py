#!/usr/bin/env python3
"""
EEE sweep: convert every official public HELM run through every_eval_ever convert helm.

Usage:
    python dev/poc/eee-audit/sweep.py [--workers N] [--limit N] [--suite SUITE]

Output root: /data/crfm-helm-audit-store/crfm-helm-public-eee-test/
Summary:     /data/crfm-helm-audit-store/crfm-helm-public-eee-test/summary.json
Log:         /data/crfm-helm-audit-store/crfm-helm-public-eee-test/results.jsonl

Each run's converted output and per-run status.json land under:
    {OUTPUT_ROOT}/{suite}/{version}/{run_name}/

Re-runnable: runs with an existing status.json containing {"status": "ok"} are skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PUBLIC_ROOT = Path(os.environ.get("HELM_PRECOMPUTED_ROOT", "/data/crfm-helm-public"))
OUTPUT_ROOT = Path(
    os.environ.get(
        "EEE_SWEEP_OUTPUT",
        "/data/crfm-helm-audit-store/crfm-helm-public-eee-test",
    )
)
EEE_CMD = os.environ.get("EEE_CMD", "every_eval_ever")

REQUIRED_FILES = {
    "run_spec.json",
    "scenario_state.json",
    "scenario.json",
    "per_instance_stats.json",
}


# ---------------------------------------------------------------------------
# Run enumeration
# ---------------------------------------------------------------------------
def _is_valid_run_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    names = {p.name for p in path.iterdir()}
    return REQUIRED_FILES.issubset(names)


def enumerate_runs(public_root: Path, suite_filter: str | None = None):
    """
    Yield (suite, version, run_name, run_path) for every valid HELM run dir.

    Directory layout: {public_root}/{suite}/benchmark_output/runs/{version}/{run_name}
    """
    for suite_dir in sorted(public_root.iterdir()):
        if not suite_dir.is_dir():
            continue
        if suite_filter and suite_dir.name != suite_filter:
            continue
        bo_dir = suite_dir / "benchmark_output" / "runs"
        if not bo_dir.is_dir():
            continue
        for version_dir in sorted(bo_dir.iterdir()):
            if not version_dir.is_dir():
                continue
            for run_dir in sorted(version_dir.iterdir()):
                if _is_valid_run_dir(run_dir):
                    yield (suite_dir.name, version_dir.name, run_dir.name, run_dir)


# ---------------------------------------------------------------------------
# Per-run output path
# ---------------------------------------------------------------------------
def output_subdir(suite: str, version: str, run_name: str) -> Path:
    return OUTPUT_ROOT / suite / version / run_name


def status_file(suite: str, version: str, run_name: str) -> Path:
    return output_subdir(suite, version, run_name) / "status.json"


def already_done(suite: str, version: str, run_name: str) -> bool:
    sf = status_file(suite, version, run_name)
    if not sf.exists():
        return False
    try:
        data = json.loads(sf.read_text())
        return data.get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Conversion worker
# ---------------------------------------------------------------------------
def convert_one(args_tuple):
    """
    Run every_eval_ever convert helm on a single HELM run directory.
    Returns a dict with status info.
    """
    suite, version, run_name, run_path = args_tuple
    out_dir = output_subdir(suite, version, run_name)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        EEE_CMD,
        "convert",
        "helm",
        "--log_path",
        str(run_path),
        "--output_dir",
        str(out_dir / "eee_output"),
        "--source_organization_name",
        "CRFM",
        "--evaluator_relationship",
        "third_party",
        "--eval_library_name",
        "HELM",
        "--eval_library_version",
        "unknown",
    ]

    result = {
        "suite": suite,
        "version": version,
        "run_name": run_name,
        "run_path": str(run_path),
        "out_dir": str(out_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cmd": cmd,
    }

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout[-4000:] if proc.stdout else ""
        result["stderr"] = proc.stderr[-4000:] if proc.stderr else ""

        if proc.returncode == 0:
            result["status"] = "ok"
        else:
            result["status"] = "fail"
            # Extract the exception class from stderr for grouping
            exc_class = _extract_exception_class(proc.stderr)
            result["exception_class"] = exc_class
            result["failure_snippet"] = _extract_failure_snippet(proc.stderr)
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["exception_class"] = "TimeoutExpired"
        result["failure_snippet"] = "Process timed out after 120s"
        result["returncode"] = -1
        result["stdout"] = ""
        result["stderr"] = ""
    except Exception:
        result["status"] = "error"
        tb = traceback.format_exc()
        exc_class = type(sys.exc_info()[1]).__name__
        result["exception_class"] = exc_class
        result["failure_snippet"] = tb[-2000:]
        result["returncode"] = -1
        result["stdout"] = ""
        result["stderr"] = ""

    # Write per-run status
    sf = status_file(suite, version, run_name)
    sf.write_text(json.dumps(result, indent=2))

    return result


def _extract_exception_class(stderr: str) -> str:
    """Extract the last exception class name from a Python traceback."""
    lines = stderr.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if "Error" in line or "Exception" in line or "Warning" in line:
            # e.g. "helm.benchmark.model_deployment_registry.ModelDeploymentNotFoundError: ..."
            if ":" in line:
                exc_part = line.split(":")[0].strip()
                return exc_part.split(".")[-1]
            return line.split()[0] if line.split() else "UnknownError"
    return "UnknownError"


def _extract_failure_snippet(stderr: str) -> str:
    """Return last ~10 lines of stderr as a concise failure snippet."""
    lines = stderr.strip().splitlines()
    return "\n".join(lines[-10:])


# ---------------------------------------------------------------------------
# Summary writer
# ---------------------------------------------------------------------------
def write_summary(results: list[dict], summary_path: Path):
    total = len(results)
    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    n_timeout = sum(1 for r in results if r["status"] == "timeout")
    n_error = sum(1 for r in results if r["status"] == "error")
    n_skipped = sum(1 for r in results if r.get("status") == "skipped")

    failures = [r for r in results if r["status"] in ("fail", "timeout", "error")]

    exc_counter: Counter = Counter()
    exc_examples: dict[str, list] = defaultdict(list)
    for r in failures:
        ec = r.get("exception_class", "UnknownError")
        exc_counter[ec] += 1
        if len(exc_examples[ec]) < 3:
            exc_examples[ec].append(
                {
                    "run_path": r["run_path"],
                    "failure_snippet": r.get("failure_snippet", ""),
                }
            )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "public_root": str(PUBLIC_ROOT),
        "output_root": str(OUTPUT_ROOT),
        "totals": {
            "attempted": total - n_skipped,
            "succeeded": n_ok,
            "failed": n_fail + n_timeout + n_error,
            "skipped_existing": n_skipped,
        },
        "failure_breakdown": {
            ec: cnt for ec, cnt in exc_counter.most_common()
        },
        "failure_examples": {
            ec: examples for ec, examples in exc_examples.items()
        },
        "failed_run_paths": [r["run_path"] for r in failures],
    }

    summary_path.write_text(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Sweep all official public HELM runs through every_eval_ever convert helm."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel worker processes (default: 4)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of runs to process (for quick testing)",
    )
    parser.add_argument(
        "--suite",
        default=None,
        help="Only process runs from this benchmark suite (e.g. classic, mmlu, lite)",
    )
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Re-run even runs that already have a successful status.json",
    )
    args = parser.parse_args()

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    results_jsonl = OUTPUT_ROOT / "results.jsonl"
    summary_path = OUTPUT_ROOT / "summary.json"

    print(f"Public HELM root : {PUBLIC_ROOT}")
    print(f"Output root      : {OUTPUT_ROOT}")
    print(f"Workers          : {args.workers}")

    # Collect all runs
    all_runs = list(enumerate_runs(PUBLIC_ROOT, suite_filter=args.suite))
    print(f"Total valid run dirs found: {len(all_runs)}")

    if args.limit:
        all_runs = all_runs[: args.limit]
        print(f"Limiting to {len(all_runs)} runs (--limit)")

    # Split into skip vs to-do
    to_run = []
    skipped = []
    for run_tuple in all_runs:
        suite, version, run_name, run_path = run_tuple
        if not args.no_skip and already_done(suite, version, run_name):
            skipped.append(
                {
                    "suite": suite,
                    "version": version,
                    "run_name": run_name,
                    "run_path": str(run_path),
                    "status": "skipped",
                }
            )
        else:
            to_run.append(run_tuple)

    print(f"Skipping {len(skipped)} already-succeeded runs")
    print(f"Running  {len(to_run)} runs")

    all_results = list(skipped)

    if to_run:
        # Open JSONL log in append mode
        with open(results_jsonl, "a") as log_f:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(convert_one, t): t for t in to_run}
                done_count = 0
                for future in as_completed(futures):
                    done_count += 1
                    try:
                        result = future.result()
                    except Exception:
                        run_tuple = futures[future]
                        suite, version, run_name, run_path = run_tuple
                        result = {
                            "suite": suite,
                            "version": version,
                            "run_name": run_name,
                            "run_path": str(run_path),
                            "status": "error",
                            "exception_class": "FutureError",
                            "failure_snippet": traceback.format_exc()[-1000:],
                        }

                    all_results.append(result)
                    log_f.write(json.dumps(result) + "\n")
                    log_f.flush()

                    status = result["status"]
                    mark = "OK" if status == "ok" else f"FAIL({result.get('exception_class',status)})"
                    print(
                        f"[{done_count}/{len(to_run)}] {mark} {result['suite']}/{result['version']}/{result['run_name'][:60]}"
                    )

    # Write summary
    summary = write_summary(all_results, summary_path)
    print()
    print("=" * 70)
    print(f"SWEEP COMPLETE")
    print(f"  Attempted : {summary['totals']['attempted']}")
    print(f"  Succeeded : {summary['totals']['succeeded']}")
    print(f"  Failed    : {summary['totals']['failed']}")
    print(f"  Skipped   : {summary['totals']['skipped_existing']}")
    print()
    if summary["failure_breakdown"]:
        print("Failure breakdown:")
        for exc, cnt in summary["failure_breakdown"].items():
            print(f"  {exc:50s} {cnt:6d}")
    print()
    print(f"Summary  : {summary_path}")
    print(f"JSONL log: {results_jsonl}")


if __name__ == "__main__":
    main()
