from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import scriptconfig as scfg


ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"DatasetNotFoundError: Dataset '([^']+)' is a gated dataset",
        "gated_dataset",
        "gated dataset: {0}",
    ),
    (
        r"The api_key client option must be set .* OPENAI_API_KEY environment variable",
        "missing_api_key",
        "missing OPENAI_API_KEY for annotator / judge",
    ),
    (
        r"Either prompt or prompt_embeds must be provided and non-empty",
        "empty_prompt_completions_mismatch",
        "legacy completions client received an empty prompt for a chat-style request",
    ),
    (
        r"Unknown model or no default model deployment found for model ([^\s\"']+)",
        "missing_model_deployment",
        "no default model deployment for model: {0}",
    ),
]


def _find_job_dirs(result_dpath: Path) -> list[Path]:
    helm_root = result_dpath / "helm" if (result_dpath / "helm").is_dir() else result_dpath
    return sorted(p for p in helm_root.glob("helm_id_*") if p.is_dir())


def _extract_run_entry(job_dir: Path) -> str:
    job_config_fpath = job_dir / "job_config.json"
    if not job_config_fpath.exists():
        return job_dir.name
    try:
        payload = json.loads(job_config_fpath.read_text())
    except Exception:
        return job_dir.name
    return payload.get("helm.run_entry", job_dir.name)


def _has_success_artifacts(job_dir: Path) -> bool:
    return any((job_dir / "benchmark_output" / "runs").glob("**/run_spec.json"))


def _read_log_text(job_dir: Path) -> str:
    chunks: list[str] = []
    for name in ["helm-run.log", "helm-run.debug.log"]:
        fpath = job_dir / name
        if fpath.exists():
            try:
                chunks.append(fpath.read_text())
            except Exception:
                chunks.append("")
    return "\n".join(chunks)


def _extract_error_summary(log_text: str) -> tuple[str, str]:
    for pattern, category, template in ERROR_PATTERNS:
        match = re.search(pattern, log_text, flags=re.MULTILINE | re.DOTALL)
        if match:
            groups = match.groups()
            summary = template.format(*groups) if groups else template
            return category, summary

    error_lines = [
        line.strip()
        for line in log_text.splitlines()
        if any(token in line for token in ["Error:", "Exception:", "RunnerError:", "ExecutorError:", "AnnotationExecutorError:"])
    ]
    if error_lines:
        return "uncategorized_error", error_lines[-1]

    return "opaque_failure", "no actionable traceback captured in helm-run logs"


def summarize_failures(result_dpath: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for job_dir in _find_job_dirs(result_dpath):
        run_entry = _extract_run_entry(job_dir)
        succeeded = _has_success_artifacts(job_dir)
        if succeeded:
            rows.append(
                {
                    "job_id": job_dir.name,
                    "run_entry": run_entry,
                    "status": "passed",
                    "category": "passed",
                    "summary": "run_spec.json present",
                    "log_fpath": str(job_dir / "helm-run.log"),
                }
            )
            continue

        log_text = _read_log_text(job_dir)
        category, summary = _extract_error_summary(log_text)
        rows.append(
            {
                "job_id": job_dir.name,
                "run_entry": run_entry,
                "status": "failed",
                "category": category,
                "summary": summary,
                "log_fpath": str(job_dir / "helm-run.log"),
            }
        )

    category_counts: dict[str, int] = {}
    for row in rows:
        category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1
    category_counts = dict(sorted(category_counts.items(), key=lambda item: (-item[1], item[0])))

    return {
        "result_dpath": str(result_dpath),
        "total_jobs": len(rows),
        "passed_jobs": sum(1 for row in rows if row["status"] == "passed"),
        "failed_jobs": sum(1 for row in rows if row["status"] == "failed"),
        "category_counts": category_counts,
        "rows": rows,
    }


def _format_text_report(summary: dict[str, Any], *, include_passed: bool = False) -> str:
    lines = [
        "Experiment Failure Summary",
        "",
        f"result_dpath={summary['result_dpath']}",
        f"total_jobs={summary['total_jobs']}",
        f"passed_jobs={summary['passed_jobs']}",
        f"failed_jobs={summary['failed_jobs']}",
        "",
        "Failure categories:",
    ]
    for category, count in summary["category_counts"].items():
        lines.append(f"  {category}: {count}")

    lines.extend(["", "Job details:"])
    for row in summary["rows"]:
        if row["status"] == "passed" and not include_passed:
            continue
        lines.extend(
            [
                f"  - {row['run_entry']}",
                f"    status={row['status']}",
                f"    category={row['category']}",
                f"    summary={row['summary']}",
                f"    log={row['log_fpath']}",
            ]
        )
    return "\n".join(lines) + "\n"


class SummarizeExperimentFailuresConfig(scfg.DataConfig):
    result_dpath = scfg.Value(position=1, help="Experiment result root or its nested helm/ directory.")
    output_json = scfg.Value(None, help="Optional path to write the structured summary as JSON.")
    include_passed = scfg.Value(False, isflag=True, help="Include passed jobs in the text output.")

    @classmethod
    def main(cls, argv=None, **kwargs):
        config = cls.cli(argv=argv, data=kwargs, strict=True)
        result_dpath = Path(config.result_dpath).expanduser().resolve()
        summary = summarize_failures(result_dpath)
        print(_format_text_report(summary, include_passed=bool(config.include_passed)), end="")
        if config.output_json:
            output_fpath = Path(config.output_json).expanduser().resolve()
            output_fpath.parent.mkdir(parents=True, exist_ok=True)
            output_fpath.write_text(json.dumps(summary, indent=2) + "\n")


__cli__ = SummarizeExperimentFailuresConfig
main = __cli__.main


if __name__ == "__main__":
    main()
