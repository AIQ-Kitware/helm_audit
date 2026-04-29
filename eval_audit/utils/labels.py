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
  readers can resolve any alias back to its full label. Optionally
  accepts a ``color_map`` mapping ``long_label -> matplotlib color``
  so each row can echo the color the corresponding line/marker has in
  the parent plot.

Use both together: build the alias map, plot with the aliases, and emit
the sidecar legend so the alias mapping is recoverable. Both functions
are stable across runs given the same inputs.
"""
from __future__ import annotations

import datetime as datetime_mod
import textwrap
from pathlib import Path
from typing import Any

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
    color_map: dict[str, Any] | None = None,
    label_wrap_chars: int = 110,
    fig_w_inches: float = 16.0,
) -> tuple[Path | None, Path | None]:
    """Render a sidecar legend mapping short aliases back to full labels.

    Emits two artifacts next to the main plot:

    * ``<out_name>_label_legend.latest.txt`` — plain-text mapping
      (``<short>  <full>`` per row) for easy grep/diff.
    * ``<out_name>_label_legend.latest.png`` — image with the same
      mapping. Long labels wrap at ``label_wrap_chars``; figure height
      auto-grows to fit. If ``color_map`` is provided
      (``long_label -> matplotlib color``) the row text uses that color,
      so a reader can match a row to the corresponding line/marker in
      the parent plot.

    Returns ``(png_path, txt_path)``; either may be ``None`` if the
    alias map is empty.
    """
    if not alias_map:
        return None, None
    if stamp is None:
        stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime("%Y%m%dT%H%M%SZ")
    items = sorted(alias_map.items(), key=lambda kv: kv[1])
    color_map = color_map or {}

    # ---- text sidecar (unchanged shape) ----
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

    # ---- image sidecar ----
    # Pre-wrap labels so each entry knows how many lines it occupies; we
    # use these counts to size the figure height so nothing clips.
    wrapped_rows: list[tuple[str, str, list[str], Any]] = []
    for long_label, short_alias in items:
        wrapped = textwrap.wrap(long_label, width=label_wrap_chars) or [long_label]
        color = color_map.get(long_label)
        wrapped_rows.append((short_alias, long_label, wrapped, color))

    body_lines = sum(len(wrapped) for _, _, wrapped, _ in wrapped_rows)
    inter_row_gaps = max(0, len(wrapped_rows) - 1)
    # Figure layout (figure-fraction coords, top-down). Leave room for the
    # title at the top and a single header row beneath it.
    line_h_in = 0.22
    title_h_in = 0.45
    header_h_in = 0.30
    bottom_pad_in = 0.30
    inter_row_h_in = 0.10
    fig_h = (
        title_h_in
        + header_h_in
        + body_lines * line_h_in
        + inter_row_gaps * inter_row_h_in
        + bottom_pad_in
    )
    fig_h = max(1.8, fig_h)
    fig = plt.figure(figsize=(fig_w_inches, fig_h))

    def _y(top_offset_in: float) -> float:
        return 1.0 - (top_offset_in / fig_h)

    # Title.
    fig.text(0.012, _y(0.30), title, fontsize=12, fontweight="bold", va="top")

    # Column header. label_x is wide enough to clear the longest header
    # word ("short alias", 11 chars at fontsize 10) plus a single-space
    # gutter; body rows use shorter alias strings so they don't bump.
    header_y_in = title_h_in + 0.10
    alias_x = 0.012
    label_x = 0.090
    fig.text(alias_x, _y(header_y_in), "short alias", fontsize=10, family="monospace", fontweight="bold", va="top")
    fig.text(label_x, _y(header_y_in), "full label", fontsize=10, family="monospace", fontweight="bold", va="top")

    # Separator rule under the header.
    sep_y_in = header_y_in + 0.20
    fig.text(
        alias_x,
        _y(sep_y_in),
        "─" * 12,
        fontsize=8,
        family="monospace",
        va="top",
    )
    fig.text(
        label_x,
        _y(sep_y_in),
        "─" * label_wrap_chars,
        fontsize=8,
        family="monospace",
        va="top",
    )

    # Body rows.
    cursor_in = title_h_in + header_h_in + 0.10
    for short_alias, _, wrapped, color in wrapped_rows:
        text_color = color if color is not None else "black"
        fig.text(
            alias_x,
            _y(cursor_in),
            short_alias,
            fontsize=9,
            family="monospace",
            color=text_color,
            va="top",
        )
        for line in wrapped:
            fig.text(
                label_x,
                _y(cursor_in),
                line,
                fontsize=9,
                family="monospace",
                color=text_color,
                va="top",
            )
            cursor_in += line_h_in
        cursor_in += inter_row_h_in

    png_fpath = fig_dpath / f"{out_name}_label_legend.latest.png"
    suffix = png_fpath.suffix.lstrip(".") or "png"
    with safer.open(png_fpath, "wb", make_parents=True) as fp:
        fig.savefig(fp, format=suffix, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return png_fpath, txt_fpath


__all__ = ["short_alias_map", "emit_label_legend_artifacts"]
