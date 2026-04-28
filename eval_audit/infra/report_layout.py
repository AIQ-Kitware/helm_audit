from __future__ import annotations

from pathlib import Path

from eval_audit.infra.paths import (
    audit_store_root,
    publication_root,
    repo_reports_root,
)
from eval_audit.infra.logging import rich_link
from loguru import logger


# ----------------------------------------------------------------------
# Publication surface (ADR 3): one folder named ``reports/`` whose
# location is parameterized via ``publication_root()``. All publication
# helpers below derive from that single root so a deployment can move
# the surface (e.g. out of the repo) by setting one env var.
# ----------------------------------------------------------------------


def filtering_reports_root() -> Path:
    """``<publication_root>/filtering`` — Stage-1 filter visualizations."""
    return publication_root() / "filtering"


def aggregate_summary_reports_root() -> Path:
    """``<publication_root>/aggregate-summary`` — story-arc sankeys etc."""
    return publication_root() / "aggregate-summary"


def publication_experiments_root() -> Path:
    """``<publication_root>/core-run-analysis`` — the publication-side
    symlink directory that points at canonical analysis outputs.

    Was previously named ``compat_core_run_reports_root`` and hard-coded
    to ``<repo>/reports/core-run-analysis``. Now follows the configured
    publication root so virtual experiments (and any future re-route of
    the publication surface) compose cleanly.
    """
    return publication_root() / "core-run-analysis"


# ----------------------------------------------------------------------
# Canonical analysis storage. NOT a publication path — lives in the
# audit store regardless of where the publication surface points.
# ----------------------------------------------------------------------


def experiments_analysis_root() -> Path:
    """``<audit_store>/analysis/experiments`` — canonical per-experiment
    analysis outputs (manifests, packets, core reports). This is the
    canonical *storage* location, never the publication surface.
    """
    return audit_store_root() / "analysis" / "experiments"


# ----------------------------------------------------------------------
# Legacy aliases. Kept for migration code that still needs to reach the
# pre-move locations. New code must not introduce calls to these.
# ----------------------------------------------------------------------


def core_run_reports_root() -> Path:
    """Deprecated alias for :func:`experiments_analysis_root`.

    The old name conflated "where reports live" with "where canonical
    analysis lives". Kept for backward compatibility while call sites
    migrate.
    """
    return experiments_analysis_root()


def legacy_repo_publication_root() -> Path:
    """The pre-parameterization in-repo ``<repo>/reports/core-run-analysis``.

    Used only by :mod:`eval_audit.workflows.analyze_experiment` to migrate
    legacy in-repo analysis directories to the canonical store path.
    """
    return repo_reports_root() / "core-run-analysis"


def compat_core_run_reports_root() -> Path:
    """Deprecated alias for :func:`legacy_repo_publication_root`.

    Some code still refers to this name when scanning for legacy data.
    Returns the in-repo legacy path, not the new publication root.
    """
    return legacy_repo_publication_root()


# ----------------------------------------------------------------------
# Reproduce-script helpers
# ----------------------------------------------------------------------


def portable_repo_root_lines(repo_root_fallback: Path | None = None) -> list[str]:
    """Bash lines that resolve ``REPO_ROOT`` for a generated reproduce script.

    Walks up from the script's directory looking for ``pyproject.toml`` +
    ``eval_audit/``. If the script lives outside the repo (the new
    publication-root location does), the walk-up will fail, so we bake
    in an absolute fallback at script-generation time. ``REPO_ROOT`` env
    var, if set, always wins.
    """
    if repo_root_fallback is None:
        # Late import to avoid a cycle with paths.py.
        from eval_audit.infra.paths import repo_root as _repo_root
        repo_root_fallback = _repo_root()
    fallback_repr = str(repo_root_fallback).replace('"', '\\"')
    return [
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'if [[ -n "${REPO_ROOT:-}" && -f "$REPO_ROOT/pyproject.toml" && -d "$REPO_ROOT/eval_audit" ]]; then',
        '  :',
        'else',
        '  REPO_ROOT="$SCRIPT_DIR"',
        '  while [[ ! -f "$REPO_ROOT/pyproject.toml" || ! -d "$REPO_ROOT/eval_audit" ]]; do',
        '    NEXT="$(dirname "$REPO_ROOT")"',
        '    if [[ "$NEXT" == "$REPO_ROOT" ]]; then',
        f'      REPO_ROOT="{fallback_repr}"',
        '      break',
        '    fi',
        '    REPO_ROOT="$NEXT"',
        '  done',
        'fi',
        'if [[ ! -f "$REPO_ROOT/pyproject.toml" || ! -d "$REPO_ROOT/eval_audit" ]]; then',
        '  echo "Could not locate eval_audit repo root from $SCRIPT_DIR (fallback ' f'{fallback_repr}' ' also missing); set REPO_ROOT=" >&2',
        '  exit 1',
        'fi',
        'PYTHON_BIN="${PYTHON_BIN:-python}"',
    ]


def write_reproduce_script(script_fpath: Path, lines: list[str]) -> Path:
    script_fpath.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    script_fpath.write_text(text)
    script_fpath.chmod(0o755)
    logger.debug(f'Write to: {rich_link(script_fpath)}')
    return script_fpath
