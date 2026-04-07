from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from helm_audit.infra.api import audit_root


def chrome_candidates() -> list[Path]:
    candidates = [
        audit_root() / ".cache/plotly-chrome/chrome-linux64/chrome",
        Path.home() / ".plotly/chrome/chrome-linux64/chrome",
    ]
    spec = importlib.util.find_spec("choreographer")
    if spec and spec.submodule_search_locations:
        candidates.insert(
            0,
            Path(list(spec.submodule_search_locations)[0]) / "cli/browser_exe/chrome-linux64/chrome",
        )
    return candidates


def configure_plotly_chrome() -> Path | None:
    for cand in chrome_candidates():
        if cand.exists():
            os.environ.setdefault("BROWSER_PATH", str(cand))
            os.environ.setdefault("PLOTLY_CHROME_PATH", str(cand))
            return cand
    return None


def has_plotly_static_dependencies() -> tuple[bool, list[str]]:
    missing: list[str] = []
    try:
        import plotly  # noqa: F401
    except Exception:
        missing.append("plotly")
    try:
        import kaleido  # noqa: F401
    except Exception:
        missing.append("kaleido")
    chrome = configure_plotly_chrome()
    if chrome is None:
        missing.append("chrome")
    return (len(missing) == 0, missing)
