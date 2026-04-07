from __future__ import annotations

import datetime as datetime_mod
import os
from pathlib import Path
from loguru import logger


def safe_unlink(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def write_latest_alias(src: Path, latest_root: Path, latest_name: str) -> Path:
    latest_fpath = latest_root / latest_name
    safe_unlink(latest_fpath)
    rel_src = os.path.relpath(src, start=latest_fpath.parent)
    logger.debug(f'Write link 🔗: {latest_fpath}')
    os.symlink(rel_src, latest_fpath)
    return latest_fpath


def symlink_to(target: str | os.PathLike[str], link_path: Path) -> Path:
    target = Path(target).expanduser().resolve()
    link_path.parent.mkdir(parents=True, exist_ok=True)
    safe_unlink(link_path)
    rel_src = os.path.relpath(target, start=link_path.parent)
    os.symlink(rel_src, link_path)
    logger.debug(f'Write link 🔗: {link_path}')
    return link_path


def stamped_history_dir(root: Path, stamp: str | None = None) -> tuple[str, Path]:
    stamp = stamp or datetime_mod.datetime.now(datetime_mod.UTC).strftime("%Y%m%dT%H%M%SZ")
    history_dpath = root / ".history" / stamp[:8]
    history_dpath.mkdir(parents=True, exist_ok=True)
    return stamp, history_dpath


def history_publish_root(report_root: Path, visible_root: Path, stamp: str) -> Path:
    report_root = report_root.expanduser().resolve()
    visible_root = visible_root.expanduser().resolve()
    try:
        rel_visible = visible_root.relative_to(report_root)
    except ValueError:
        rel_visible = Path(visible_root.name)
    history_root = report_root / ".history" / stamp[:8] / stamp / rel_visible
    history_root.mkdir(parents=True, exist_ok=True)
    return history_root
