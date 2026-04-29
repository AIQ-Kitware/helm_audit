"""Shared label-aliasing helpers for plot/report artifacts.

Long labels — full HELM run-spec names with model + adapter qualifiers,
comparison ids that splice multiple component ids together, public-track
identifiers laced with suite versions — routinely run 80–200 chars and
crush plot legends, axis titles, and sankey node names. Two helpers in
this module keep the actual plot small and shunt the long form into a
sidecar legend artifact:

* :func:`short_alias_map` builds a deterministic short alias for each
  unique long label (``c<hash6>`` by default; hash-len auto-extends if
  the default 6 chars happens to collide).

* :func:`emit_label_legend_artifacts` writes a plain-text + matplotlib
  table sidecar named ``<out_name>_label_legend.latest.{txt,png}`` so
  readers can resolve any alias back to its full label.

Use both together: build the alias map, plot with the aliases, and emit
the sidecar legend so the alias mapping is recoverable. Both functions
are stable across runs given the same inputs.
"""
from __future__ import annotations

import datetime as datetime_mod
import os
from pathlib import Path

import matplotlib.pyplot as plt
import safer

from eval_audit.helm.hashers import stable_hash36
from eval_audit.infra.fs_publish import write_text_atomic


def short_alias_map(
    labels: list[str],
    *,
    prefix: str = "c",
    min_hash_len: int = 6,
) -> dict[str, str]:
    """Build a deterministic short-alias map for plot/legend labels.

    The default alias is ``<prefix><hash>`` where ``<hash>`` is the first
    ``min_hash_len`` chars of ``stable_hash36(label)``. If two distinct
    labels collide on those chars, the hash length is extended uniformly
    for every label until all aliases are distinct, so the alias surface
    stays consistent within one figure.

    Determinism: same input set → same output mapping (independent of
    label order or count). Uniqueness: no two distinct long labels point
    at the same short alias.
    """
    unique = sorted(set(labels))
    if not unique:
        return {}
    for hash_len in range(min_hash_len, 33):
        candidate = {label: f"{prefix}{stable_hash36(label)[:hash_len]}" for label in unique}
        if len(set(candidate.values())) == len(candidate):
            return candidate
    # Pathological fall-through (sha256 base36 collisions are astronomically rare);
    # disambiguate by appending the index of the offending label.
    return {label: f"{prefix}{stable_hash36(label)}_{i}" for i, label in enumerate(unique)}


def emit_label_legend_artifacts(
    alias_map: dict[str, str],
    *,
    fig_dpath: Path,
    out_name: str,
    title: str,
    stamp: str | None = None,
) -> tuple[Path | None, Path | None]:
    """Render a sidecar legend mapping short aliases back to full labels.

    Emits two artifacts next to the main plot:

    * ``<out_name>_label_legend.latest.txt`` — plain text mapping (one row
      per ``alias <-> full label`` pair) for easy grep/diff.
    * ``<out_name>_label_legend.latest.png`` — text-only matplotlib figure
      with the same mapping rendered as a table, suitable for embedding
      next to the main plot.

    Returns ``(png_path, txt_path)``; either may be ``None`` if the alias
    map is empty.
    """
    if not alias_map:
        return None, None
    if stamp is None:
        stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime("%Y%m%dT%H%M%SZ")
    items = sorted(alias_map.items(), key=lambda kv: kv[1])

    txt_fpath = fig_dpath / f"{out_name}_label_legend.latest.txt"
    txt_lines = [
        f"{title}",
        f"Generated: {stamp}",
        "",
        f"{'short':<12s}  full",
        f"{'-' * 12}  {'-' * 60}",
    ]
    for long_label, short_alias in items:
        txt_lines.append(f"{short_alias:<12s}  {long_label}")
    write_text_atomic(txt_fpath, "\n".join(txt_lines) + "\n")

    n_rows = len(items)
    fig_h = max(1.6, 0.32 * n_rows + 1.2)
    fig, ax = plt.subplots(figsize=(12, fig_h), constrained_layout=True)
    ax.axis("off")
    table_rows = [[short, long_label] for long_label, short in items]
    table = ax.table(
        cellText=table_rows,
        colLabels=["short alias", "full label"],
        cellLoc="left",
        colLoc="left",
        loc="upper left",
        colWidths=[0.10, 0.90],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1.0, 1.25)
    ax.set_title(title, fontsize=12, loc="left")
    png_fpath = fig_dpath / f"{out_name}_label_legend.latest.png"
    suffix = png_fpath.suffix.lstrip(".") or "png"
    with safer.open(png_fpath, "wb", make_parents=True) as fp:
        fig.savefig(fp, format=suffix, dpi=180)
    plt.close(fig)
    return png_fpath, txt_fpath


__all__ = ["short_alias_map", "emit_label_legend_artifacts"]
