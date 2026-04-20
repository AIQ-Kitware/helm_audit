from __future__ import annotations

from pathlib import Path

from helm_audit.infra.paths import audit_store_root, reports_root
from loguru import logger


def filtering_reports_root() -> Path:
    return reports_root() / "filtering"


def core_run_reports_root() -> Path:
    """Canonical root for per-experiment analysis outputs (in the audit store)."""
    return audit_store_root() / "analysis" / "experiments"


def compat_core_run_reports_root() -> Path:
    """Legacy location; used only to publish backward-compat symlinks."""
    return reports_root() / "core-run-analysis"


def aggregate_summary_reports_root() -> Path:
    return reports_root() / "aggregate-summary"


def portable_repo_root_lines() -> list[str]:
    return [
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'REPO_ROOT="$SCRIPT_DIR"',
        'while [[ ! -f "$REPO_ROOT/pyproject.toml" || ! -d "$REPO_ROOT/helm_audit" ]]; do',
        '  NEXT="$(dirname "$REPO_ROOT")"',
        '  if [[ "$NEXT" == "$REPO_ROOT" ]]; then',
        '    echo "Could not locate helm_audit repo root from $SCRIPT_DIR" >&2',
        '    exit 1',
        '  fi',
        '  REPO_ROOT="$NEXT"',
        'done',
        'PYTHON_BIN="${PYTHON_BIN:-python}"',
    ]


def write_reproduce_script(script_fpath: Path, lines: list[str]) -> Path:
    script_fpath.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    script_fpath.write_text(text)
    script_fpath.chmod(0o755)
    logger.debug(f'Write to: {script_fpath}')
    return script_fpath
