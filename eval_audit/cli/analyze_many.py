from __future__ import annotations

import argparse
import csv
import time
import datetime as dt
from pathlib import Path
from typing import Any

from loguru import logger

from eval_audit.infra.logging import rich_link, setup_cli_logging
from eval_audit.workflows import analyze_experiment, build_reports_summary


def _discover_experiment_names(index_fpath: Path) -> list[str]:
    with index_fpath.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    names = sorted({r["experiment_name"] for r in rows if r.get("experiment_name")})
    return names


def _hms(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _progress_line(idx: int, total: int, name: str, *, tag: str, elapsed: float | None = None, eta: float | None = None) -> str:
    counter = f"[{idx}/{total}]"
    parts = [f"[{_ts()}]", counter, f"{tag:<5}", name]
    trailer_parts = []
    if elapsed is not None:
        trailer_parts.append(_hms(elapsed))
    if eta is not None:
        trailer_parts.append(f"eta ~{_hms(eta)}")
    if trailer_parts:
        parts.append(f"({' | '.join(trailer_parts)})")
    return "  ".join(parts)


def _print_summary_table(results: list[dict[str, Any]], total_elapsed: float) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box

        console = Console()
        table = Table(box=box.SIMPLE_HEAVY, show_footer=False)
        table.add_column("#", style="dim", justify="right", width=4)
        table.add_column("Experiment", min_width=40)
        table.add_column("Status", justify="center", width=8)
        table.add_column("Elapsed", justify="right", width=8)

        ok_count = 0
        for i, r in enumerate(results, 1):
            status_text = "[green]OK[/green]" if r["ok"] else "[red]FAIL[/red]"
            table.add_row(str(i), r["name"], status_text, _hms(r["elapsed"]))
            if r["ok"]:
                ok_count += 1

        n = len(results)
        console.print(table)
        console.print(
            f"  [bold]{n}[/bold] experiments  "
            f"[green]{ok_count} OK[/green]  "
            f"[red]{n - ok_count} failed[/red]  "
            f"total elapsed: [bold]{_hms(total_elapsed)}[/bold]"
        )
    except Exception:
        # Fallback if rich is unavailable or output is not a tty
        print("\nSummary:")
        for r in results:
            status = "OK  " if r["ok"] else "FAIL"
            print(f"  {status}  {_hms(r['elapsed']):>6}  {r['name']}")
        ok_count = sum(r["ok"] for r in results)
        print(f"\n{len(results)} experiments, {ok_count} OK, {len(results) - ok_count} failed, "
              f"total: {_hms(total_elapsed)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze multiple experiments in one Python process so shared caches are reused. "
            "Pass --all-from-index to automatically discover every experiment name in the index."
        )
    )
    parser.add_argument(
        "--experiment-name",
        dest="experiment_names",
        action="append",
        default=[],
        help="Experiment name to analyze. Repeat to analyze multiple. Omit when using --all-from-index.",
    )
    parser.add_argument(
        "--all-from-index",
        action="store_true",
        help="Discover and analyze every experiment name present in --index-fpath.",
    )
    parser.add_argument("--index-fpath", required=True)
    parser.add_argument("--official-index-fpath", default=None)
    parser.add_argument("--allow-single-repeat", action="store_true")
    parser.add_argument("--build-summary", action="store_true")
    parser.add_argument("--filter-inventory-json", default=None)
    parser.add_argument("--official-eee-root", default=None)
    parser.add_argument("--local-eee-root", default=None)
    parser.add_argument(
        "--ensure-local-eee",
        action="store_true",
        help="Convert local HELM runs to EEE on demand when canonical local artifacts are missing.",
    )
    args = parser.parse_args(argv)
    setup_cli_logging()

    index_fpath = Path(args.index_fpath).expanduser().resolve()

    if args.all_from_index:
        experiment_names = _discover_experiment_names(index_fpath)
        logger.info(f"[{_ts()}]  Discovered {len(experiment_names)} experiment(s) from {rich_link(index_fpath)}")
    else:
        experiment_names = args.experiment_names

    if not experiment_names:
        parser.error("Provide at least one --experiment-name or use --all-from-index.")

    total = len(experiment_names)
    results: list[dict[str, Any]] = []
    batch_start = time.monotonic()
    elapsed_times: list[float] = []

    for idx, experiment_name in enumerate(experiment_names, 1):
        eta: float | None = None
        if elapsed_times:
            avg = sum(elapsed_times) / len(elapsed_times)
            remaining = total - idx + 1
            eta = avg * remaining

        print(_progress_line(idx, total, experiment_name, tag="START", eta=eta), flush=True)

        cmd = [
            "--experiment-name", experiment_name,
            "--index-fpath", str(index_fpath),
        ]
        if args.allow_single_repeat:
            cmd.append("--allow-single-repeat")
        if args.official_index_fpath:
            cmd.extend(["--official-index-fpath", str(args.official_index_fpath)])
        if args.official_eee_root:
            cmd.extend(["--official-eee-root", str(args.official_eee_root)])
        if args.local_eee_root:
            cmd.extend(["--local-eee-root", str(args.local_eee_root)])
        if args.ensure_local_eee:
            cmd.append("--ensure-local-eee")

        t0 = time.monotonic()
        ok = True
        try:
            analyze_experiment.main(cmd)
        except (Exception, SystemExit) as ex:
            ok = False
            elapsed = time.monotonic() - t0
            print(_progress_line(idx, total, experiment_name, tag="ERROR", elapsed=elapsed), flush=True)
            print(f"         └─ {ex}", flush=True)
        else:
            elapsed = time.monotonic() - t0
            elapsed_times.append(elapsed)
            avg = sum(elapsed_times) / len(elapsed_times)
            done = idx
            remaining = total - done
            running_eta = avg * remaining if remaining > 0 else None
            print(_progress_line(idx, total, experiment_name, tag="OK", elapsed=elapsed, eta=running_eta), flush=True)

        results.append({"name": experiment_name, "ok": ok, "elapsed": time.monotonic() - t0})

    total_elapsed = time.monotonic() - batch_start
    print(flush=True)
    _print_summary_table(results, total_elapsed)

    if args.build_summary:
        print(f"\n[{_ts()}]  BEGIN build_reports_summary", flush=True)
        t0 = time.monotonic()
        cmd = ["--index-fpath", str(index_fpath)]
        if args.filter_inventory_json:
            cmd.extend(["--filter-inventory-json", args.filter_inventory_json])
        build_reports_summary.main(cmd)
        print(f"[{_ts()}]  END build_reports_summary ({_hms(time.monotonic() - t0)})", flush=True)


if __name__ == "__main__":
    main()
