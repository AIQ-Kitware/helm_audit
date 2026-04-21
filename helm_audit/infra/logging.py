from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import traceback as tb

from loguru import logger
from rich.console import Console
from rich.text import Text


def _coerce_link_target(target: str | Path) -> str:
    text = str(target)
    parsed = urlparse(text)
    if parsed.scheme:
        return text
    return Path(text).expanduser().resolve().as_uri()


def rich_link(target: str | Path, label: str | None = None) -> str:
    display = label if label is not None else str(target)
    return f"[link={_coerce_link_target(target)}]{display}[/link]"


_LEVEL_STYLES = {
    "TRACE": "dim blue",
    "DEBUG": "cyan",
    "INFO": "bold",
    "SUCCESS": "bold green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}


def _make_rich_sink(console: Console):
    def sink(message):
        record = message.record

        level_name = record["level"].name
        level_style = _LEVEL_STYLES.get(level_name, "white")

        line = Text()
        line.append(record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], style="dim")
        line.append(" | ", style="dim")
        line.append(f"{level_name:<8}", style=level_style)
        line.append(" | ", style="dim")
        line.append(record["name"], style="cyan")
        line.append(":", style="dim")
        line.append(record["function"], style="cyan")
        line.append(":", style="dim")
        line.append(str(record["line"]), style="cyan")
        line.append(" - ", style="dim")
        line += Text.from_markup(record["message"])

        console.print(line)

        if record["exception"] is not None:
            exc = record["exception"]
            rendered = "".join(
                tb.format_exception(exc.type, exc.value, exc.traceback)
            )
            console.print(rendered, end="")

    return sink


def setup_cli_logging(*, level: str = "DEBUG", console: Console | None = None) -> Console:
    console = console or Console(stderr=True)

    logger.remove()
    logger.add(
        _make_rich_sink(console),
        level=level,
        backtrace=True,
        diagnose=False,
    )
    return console
