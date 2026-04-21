from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from loguru import logger
from rich.console import Console


def _coerce_link_target(target: str | Path) -> str:
    text = str(target)
    parsed = urlparse(text)
    if parsed.scheme:
        return text
    return Path(text).expanduser().resolve().as_uri()


def rich_link(target: str | Path, label: str | None = None) -> str:
    """Format a Rich hyperlink for a filesystem path or URL."""
    display = label if label is not None else str(target)
    return f"[link={_coerce_link_target(target)}]{display}[/link]"


def setup_cli_logging(*, level: str = "DEBUG", console: Console | None = None) -> Console:
    """Configure Loguru to render through Rich for CLI entry points."""
    console = console or Console(stderr=True)

    logger.remove()
    logger.add(
        lambda message: console.print(message, end=""),
        level=level,
        format="{message}",
    )
    return console
