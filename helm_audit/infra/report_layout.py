from __future__ import annotations

from pathlib import Path

from helm_audit.infra.paths import reports_root
from loguru import logger


def filtering_reports_root() -> Path:
    return reports_root() / "filtering"


def core_run_reports_root() -> Path:
    return reports_root() / "core-run-analysis"


def aggregate_summary_reports_root() -> Path:
    return reports_root() / "aggregate-summary"


def write_reproduce_script(script_fpath: Path, lines: list[str]) -> Path:
    script_fpath.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    script_fpath.write_text(text)
    script_fpath.chmod(0o755)
    logger.debug(f'Write to: {script_fpath}')
    return script_fpath
