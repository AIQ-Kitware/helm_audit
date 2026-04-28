from __future__ import annotations

import datetime as datetime_mod
import os
from pathlib import Path
from loguru import logger
from eval_audit.infra.logging import rich_link


def safe_unlink(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def write_latest_alias(src: Path, latest_root: Path, latest_name: str) -> Path:
    """Place ``src`` at ``latest_root/latest_name``.

    Behaviour depends on whether the call is collapsing a stamped intermediate
    onto the visible ``*.latest.*`` name (rename) or wiring up a navigation
    alias (symlink). Rule:

    * **Rename** when all of: ``src`` is a regular file, ``src.parent ==
      latest_root``, ``latest_name`` contains ``.latest.``, and ``src.name``
      does not. This is the "stamped intermediate -> visible name" case
      after the history retirement (2026-04-28): callers write
      ``foo_<stamp>.txt`` next to the intended ``foo.latest.txt`` and we
      promote it in place. Net result: a single regular file at
      ``latest_root/latest_name`` with no stamped residue.
    * **Symlink** in every other case. Covers
      - cross-tree navigation aliases
        (``summary_root/README.latest.txt`` -> ``level_001/README.latest.txt``);
      - directory aliases (``summary_root/level_001.latest`` ->
        ``summary_root/level_001/``);
      - inverse same-dir aliases where the source is the canonical
        ``*.latest.*`` artifact and the alias is a plain shortcut
        (``reproduce.sh`` -> ``reproduce.latest.sh``,
        ``render_heavy_pairwise_plots.sh`` -> ``render_heavy_pairwise_plots.latest.sh``).
    """
    latest_fpath = latest_root / latest_name
    if src == latest_fpath:
        return latest_fpath
    safe_unlink(latest_fpath)
    same_dir = src.parent == latest_root
    is_regular_file = src.is_file() and not src.is_symlink()
    rename_eligible = (
        same_dir
        and is_regular_file
        and ".latest." in latest_name
        and ".latest." not in src.name
    )
    if rename_eligible:
        src.rename(latest_fpath)
        logger.debug(f'Move 📦: {rich_link(latest_fpath)}')
        return latest_fpath
    rel_src = os.path.relpath(src, start=latest_fpath.parent)
    os.symlink(rel_src, latest_fpath)
    logger.debug(f'Write link 🔗: {rich_link(latest_fpath)}')
    return latest_fpath


def symlink_to(target: str | os.PathLike[str], link_path: Path) -> Path:
    target = Path(target).expanduser().resolve()
    link_path.parent.mkdir(parents=True, exist_ok=True)
    safe_unlink(link_path)
    rel_src = os.path.relpath(target, start=link_path.parent)
    os.symlink(rel_src, link_path)
    logger.debug(f'Write link 🔗: {rich_link(link_path)}')
    return link_path


def stamped_history_dir(root: Path, stamp: str | None = None) -> tuple[str, Path]:
    """Return ``(stamp, root)``. The history layer was retired on 2026-04-28;
    callers still receive a unique stamp string (used as a filename infix on
    the intermediate file) and the same ``root`` to write into. The
    intermediate file is then promoted to the visible ``*.latest.*`` name by
    :func:`write_latest_alias` (rename, not symlink), so no ``.history/``
    directory or stamped residue is left on disk."""
    stamp = stamp or datetime_mod.datetime.now(datetime_mod.UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return stamp, root


def history_publish_root(report_root: Path, visible_root: Path, stamp: str) -> Path:
    """Return ``visible_root`` (creating it if missing).

    Originally this returned a deep ``report_root/.history/<date>/<stamp>/<rel>``
    path so plot-generating code could write to a stamped tree and then
    symlink ``*.latest.*`` aliases at the visible level. After the history
    retirement on 2026-04-28 the returned path is just the visible root;
    callers continue to use a stamped filename infix and the alias step
    promotes the file in place via :func:`write_latest_alias`.
    """
    visible_root = Path(visible_root).expanduser().resolve()
    visible_root.mkdir(parents=True, exist_ok=True)
    return visible_root
