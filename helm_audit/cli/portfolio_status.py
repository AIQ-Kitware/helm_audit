from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from helm_audit.workflows.compare_batch import collect_historic_candidates


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUMMARY_HISTORY_ROOT = REPO_ROOT / "reports" / "aggregate-summary" / "all-results" / ".history"
DEFAULT_HISTORIC_ROOT = Path("/data/crfm-helm-public")


def _latest_run_inventory_csv(summary_history_root: Path) -> Path:
    cands = sorted(summary_history_root.rglob("run_inventory_*.csv"), reverse=True)
    if not cands:
        raise FileNotFoundError(f"No run_inventory_*.csv files found under {summary_history_root}")
    return cands[0]


def _load_rows(run_inventory_csv: Path) -> list[dict[str, str]]:
    with run_inventory_csv.open(newline="") as file:
        return [{k: ("" if v is None else v) for k, v in row.items()} for row in csv.DictReader(file)]


def _is_analyzed(row: dict[str, str]) -> bool:
    return bool((row.get("repro_report_dir") or "").strip())


def _has_scalar_agreement(row: dict[str, str]) -> bool:
    return bool(
        (row.get("official_instance_agree_0") or "").strip()
        or (row.get("mean_rel_metric_mean_agreement") or "").strip()
    )


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


@lru_cache(maxsize=8192)
def _run_entry_has_historic_candidate(historic_root: str, run_entry: str) -> bool:
    return bool(collect_historic_candidates(historic_root, run_entry))


def summarize_rows(
    rows: list[dict[str, str]],
    *,
    experiment_name: str | None,
    top_low_agreement: int,
    classify_backlog: bool,
    historic_root: str | None,
) -> dict[str, Any]:
    scoped_rows = [
        row for row in rows
        if experiment_name is None or row.get("experiment_name") == experiment_name
    ]
    if experiment_name and not scoped_rows:
        raise SystemExit(f"No rows found for experiment_name={experiment_name!r}")

    experiment_rows: list[dict[str, Any]] = []
    by_experiment: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in scoped_rows:
        by_experiment[str(row.get("experiment_name") or "")].append(row)

    for name, exp_rows in sorted(by_experiment.items()):
        analyzed = sum(_is_analyzed(row) for row in exp_rows)
        completed = sum(row.get("lifecycle_stage") == "completed_with_run_artifacts" for row in exp_rows)
        failed = sum(row.get("lifecycle_stage") == "failed_or_incomplete" for row in exp_rows)
        completed_not_analyzed = sum(
            row.get("lifecycle_stage") == "completed_with_run_artifacts" and not _is_analyzed(row)
            for row in exp_rows
        )
        benchmark_counts = Counter((row.get("benchmark") or "unknown") for row in exp_rows)
        host_counts = Counter((row.get("machine_host") or "unknown") for row in exp_rows)
        model_counts = Counter((row.get("model") or "unknown") for row in exp_rows)
        experiment_rows.append(
            {
                "experiment_name": name,
                "total_runs": len(exp_rows),
                "analyzed_runs": analyzed,
                "completed_with_run_artifacts": completed,
                "completed_not_analyzed": completed_not_analyzed,
                "failed_or_incomplete": failed,
                "top_benchmarks": benchmark_counts.most_common(8),
                "hosts": dict(host_counts),
                "top_models": model_counts.most_common(5),
            }
        )

    experiment_rows.sort(
        key=lambda row: (
            row["completed_not_analyzed"],
            row["failed_or_incomplete"],
            row["analyzed_runs"],
            row["total_runs"],
            row["experiment_name"],
        ),
        reverse=True,
    )

    analyzed_rows = [row for row in scoped_rows if _is_analyzed(row)]
    model_evidence_rows: list[dict[str, Any]] = []
    by_model: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in analyzed_rows:
        by_model[str(row.get("model") or "unknown")].append(row)
    for model, model_rows in sorted(by_model.items()):
        bucket_counts = Counter((row.get("official_instance_agree_bucket") or "unknown") for row in model_rows)
        benchmarks = Counter((row.get("benchmark") or "unknown") for row in model_rows)
        model_evidence_rows.append(
            {
                "model": model,
                "analyzed_runs": len(model_rows),
                "agreement_buckets": dict(bucket_counts),
                "top_benchmarks": benchmarks.most_common(8),
            }
        )
    model_evidence_rows.sort(
        key=lambda row: (row["analyzed_runs"], row["model"]),
        reverse=True,
    )

    low_agreement_rows = [
        {
            "experiment_name": row.get("experiment_name"),
            "model": row.get("model"),
            "benchmark": row.get("benchmark"),
            "run_entry": row.get("run_entry"),
            "official_instance_agree_0": _coerce_float(row.get("official_instance_agree_0"), -1.0),
            "official_instance_agree_bucket": row.get("official_instance_agree_bucket"),
            "official_diagnosis": row.get("official_diagnosis"),
        }
        for row in analyzed_rows
        if (row.get("official_instance_agree_bucket") or "") == "low_agreement_0.00+"
    ]
    low_agreement_rows.sort(
        key=lambda row: (
            row["official_instance_agree_0"],
            row["model"] or "",
            row["run_entry"] or "",
        )
    )
    low_agreement_rows = low_agreement_rows[:top_low_agreement]

    failure_reason_counts = Counter(
        (row.get("failure_reason") or "unknown")
        for row in scoped_rows
        if row.get("lifecycle_stage") == "failed_or_incomplete"
    )

    totals = {
        "total_runs": len(scoped_rows),
        "total_experiments": len(by_experiment),
        "analyzed_runs": len(analyzed_rows),
        "completed_with_run_artifacts": sum(
            row.get("lifecycle_stage") == "completed_with_run_artifacts" for row in scoped_rows
        ),
        "completed_not_analyzed": sum(
            row.get("lifecycle_stage") == "completed_with_run_artifacts" and not _is_analyzed(row)
            for row in scoped_rows
        ),
        "failed_or_incomplete": sum(
            row.get("lifecycle_stage") == "failed_or_incomplete" for row in scoped_rows
        ),
    }

    backlog_classification = None
    if classify_backlog and experiment_name is not None:
        historic_root_path = None
        if historic_root:
            cand = Path(historic_root).expanduser().resolve()
            if cand.exists():
                historic_root_path = str(cand)
        rows_with_reports_missing_scalar = [
            row for row in scoped_rows
            if _is_analyzed(row) and not _has_scalar_agreement(row)
        ]
        completed_no_report = [
            row for row in scoped_rows
            if row.get("lifecycle_stage") == "completed_with_run_artifacts" and not _is_analyzed(row)
        ]
        comparable_backlog = []
        no_official_counterpart = []
        unclassified_no_report = []
        for row in completed_no_report:
            run_entry = str(row.get("run_entry") or "")
            if not run_entry or historic_root_path is None:
                unclassified_no_report.append(row)
                continue
            if _run_entry_has_historic_candidate(historic_root_path, run_entry):
                comparable_backlog.append(row)
            else:
                no_official_counterpart.append(row)
        backlog_classification = {
            "historic_root": historic_root_path,
            "rows_with_reports_missing_scalar": len(rows_with_reports_missing_scalar),
            "rows_with_reports_missing_scalar_top_benchmarks": Counter(
                (row.get("benchmark") or "unknown") for row in rows_with_reports_missing_scalar
            ).most_common(8),
            "rows_with_reports_missing_scalar_examples": [
                row.get("run_entry") or ""
                for row in rows_with_reports_missing_scalar[:12]
            ],
            "completed_no_report_total": len(completed_no_report),
            "completed_no_report_comparable_backlog": len(comparable_backlog),
            "completed_no_report_no_official_counterpart": len(no_official_counterpart),
            "completed_no_report_unclassified": len(unclassified_no_report),
            "completed_no_report_no_official_top_benchmarks": Counter(
                (row.get("benchmark") or "unknown") for row in no_official_counterpart
            ).most_common(8),
            "completed_no_report_no_official_top_models": Counter(
                (row.get("model") or "unknown") for row in no_official_counterpart
            ).most_common(8),
            "completed_no_report_comparable_examples": [
                row.get("run_entry") or ""
                for row in comparable_backlog[:12]
            ],
            "completed_no_report_no_official_examples": [
                row.get("run_entry") or ""
                for row in no_official_counterpart[:12]
            ],
        }

    return {
        "experiment_filter": experiment_name,
        "totals": totals,
        "experiment_rows": experiment_rows,
        "model_evidence_rows": model_evidence_rows,
        "failure_reason_counts": dict(
            sorted(failure_reason_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "low_agreement_rows": low_agreement_rows,
        "backlog_classification": backlog_classification,
    }


def _format_report(summary: dict[str, Any], *, top_experiments: int) -> str:
    totals = summary["totals"]
    lines = [
        "Reproduction Portfolio Status",
        "",
        f"experiment_filter={summary['experiment_filter'] or 'all-results'}",
        f"total_runs={totals['total_runs']}",
        f"total_experiments={totals['total_experiments']}",
        f"analyzed_runs={totals['analyzed_runs']}",
        f"completed_with_run_artifacts={totals['completed_with_run_artifacts']}",
        f"completed_not_analyzed={totals['completed_not_analyzed']}",
        f"failed_or_incomplete={totals['failed_or_incomplete']}",
        "",
        "Priority experiment backlog:",
    ]
    for row in summary["experiment_rows"][:top_experiments]:
        lines.append(
            "  - "
            f"{row['experiment_name']}: total={row['total_runs']}, "
            f"analyzed={row['analyzed_runs']}, "
            f"completed_not_analyzed={row['completed_not_analyzed']}, "
            f"failed_or_incomplete={row['failed_or_incomplete']}"
        )
        if row["top_benchmarks"]:
            bench_text = ", ".join(f"{name}={count}" for name, count in row["top_benchmarks"])
            lines.append(f"    top_benchmarks={bench_text}")
        if row["hosts"]:
            host_text = ", ".join(f"{name}={count}" for name, count in sorted(row["hosts"].items()))
            lines.append(f"    hosts={host_text}")

    if summary["model_evidence_rows"]:
        lines.extend(["", "Analyzed model evidence:"])
        for row in summary["model_evidence_rows"]:
            bucket_text = ", ".join(
                f"{name}={count}" for name, count in sorted(row["agreement_buckets"].items())
            )
            lines.append(f"  - {row['model']}: analyzed={row['analyzed_runs']}; buckets={bucket_text}")

    if summary["failure_reason_counts"]:
        lines.extend(["", "Failure reasons:"])
        for reason, count in summary["failure_reason_counts"].items():
            lines.append(f"  - {reason}: {count}")

    if summary["low_agreement_rows"]:
        lines.extend(["", "Lowest-agreement analyzed rows:"])
        for row in summary["low_agreement_rows"]:
            lines.append(
                "  - "
                f"{row['official_instance_agree_0']:.4f} | "
                f"{row['model']} | {row['benchmark']} | {row['run_entry']}"
            )

    backlog = summary.get("backlog_classification")
    if backlog is not None:
        lines.extend(["", "Backlog classification:"])
        lines.append(
            "  - "
            f"reports_missing_scalar={backlog['rows_with_reports_missing_scalar']}, "
            f"completed_no_report_total={backlog['completed_no_report_total']}, "
            f"comparable_backlog={backlog['completed_no_report_comparable_backlog']}, "
            f"no_official_counterpart={backlog['completed_no_report_no_official_counterpart']}, "
            f"unclassified={backlog['completed_no_report_unclassified']}"
        )
        if backlog["historic_root"] is not None:
            lines.append(f"    historic_root={backlog['historic_root']}")
        if backlog["rows_with_reports_missing_scalar_top_benchmarks"]:
            bench_text = ", ".join(
                f"{name}={count}" for name, count in backlog["rows_with_reports_missing_scalar_top_benchmarks"]
            )
            lines.append(f"    reports_missing_scalar_top_benchmarks={bench_text}")
        if backlog["completed_no_report_no_official_top_benchmarks"]:
            bench_text = ", ".join(
                f"{name}={count}" for name, count in backlog["completed_no_report_no_official_top_benchmarks"]
            )
            lines.append(f"    no_official_top_benchmarks={bench_text}")
        if backlog["completed_no_report_no_official_top_models"]:
            model_text = ", ".join(
                f"{name}={count}" for name, count in backlog["completed_no_report_no_official_top_models"]
            )
            lines.append(f"    no_official_top_models={model_text}")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Summarize the current HELM reproduction portfolio.")
    parser.add_argument("--run-inventory-csv", default=None)
    parser.add_argument("--summary-history-root", default=str(DEFAULT_SUMMARY_HISTORY_ROOT))
    parser.add_argument("--experiment-name", default=None)
    parser.add_argument("--top-experiments", type=int, default=12)
    parser.add_argument("--top-low-agreement", type=int, default=12)
    parser.add_argument("--classify-backlog", action="store_true")
    parser.add_argument("--historic-root", default=str(DEFAULT_HISTORIC_ROOT))
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    run_inventory_csv = (
        Path(args.run_inventory_csv).expanduser().resolve()
        if args.run_inventory_csv
        else _latest_run_inventory_csv(Path(args.summary_history_root).expanduser().resolve())
    )
    rows = _load_rows(run_inventory_csv)
    summary = summarize_rows(
        rows,
        experiment_name=args.experiment_name,
        top_low_agreement=args.top_low_agreement,
        classify_backlog=args.classify_backlog,
        historic_root=args.historic_root,
    )
    summary["run_inventory_csv"] = str(run_inventory_csv)
    print(f"run_inventory_csv={run_inventory_csv}")
    print(_format_report(summary, top_experiments=args.top_experiments), end="")

    if args.output_json:
        output_fpath = Path(args.output_json).expanduser().resolve()
        output_fpath.parent.mkdir(parents=True, exist_ok=True)
        output_fpath.write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
