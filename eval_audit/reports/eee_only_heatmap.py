"""EEE-only reproducibility heatmap.

Reads ``core_metric_report.json`` files produced by ``eval-audit-from-eee``,
groups by ``(model, benchmark_family)`` using the ``logical_run_key`` stored
in each report's component list, micro-averages the instance-level
official_vs_local agreement fraction at a given ``abs_tol``, and renders a
model × benchmark heatmap.

Each cell value is:

    agree_ratio = sum(matched) / sum(count)

across all ``official_vs_local`` pairs in all packets for that
(model, benchmark_family) combination.  Missing cells (no official or no
local artifact) are shown as gray "N/A".

CLI::

    python -m eval_audit.reports.eee_only_heatmap \\
        --analysis-root <from_eee_out_dir> \\
        --out-dir <output_dir> \\
        [--abs-tol 1e-9] [--title "Reproducibility Heatmap"]
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Display label tables
# ---------------------------------------------------------------------------

_MODEL_DISPLAY: dict[str, str] = {
    "eleutherai/pythia-2.8b-v0": "Pythia-2.8B",
    "eleutherai/pythia-6.9b": "Pythia-6.9B",
    "lmsys/vicuna-7b-v1.3": "Vicuna-7B-v1.3",
}

_BENCHMARK_DISPLAY: dict[str, str] = {
    "boolq": "BoolQ",
    "civil_comments": "CivilComments",
    "entity_data_imputation": "Entity-DataImputation",
    "entity_matching": "Entity-Matching",
    "gsm": "GSM",
    "imdb": "IMDB",
    "lsat_qa": "LSAT-QA",
    "mmlu": "MMLU",
    "narrativeqa": "NarrativeQA",
    "quac": "QuAC",
    "synthetic_reasoning": "SyntheticReasoning",
    "sythetic_reasoning_natural": "SyntheticReasoning-Natural",
    "truthful_qa": "TruthfulQA",
    "wikifact": "WikiFact",
}

# Canonical display order (rows top-to-bottom in the heatmap)
_BENCHMARK_ORDER: list[str] = [
    "boolq",
    "civil_comments",
    "entity_data_imputation",
    "entity_matching",
    "gsm",
    "imdb",
    "lsat_qa",
    "mmlu",
    "narrativeqa",
    "quac",
    "synthetic_reasoning",
    "sythetic_reasoning_natural",
    "truthful_qa",
    "wikifact",
]

_MODEL_ORDER: list[str] = [
    "eleutherai/pythia-2.8b-v0",
    "eleutherai/pythia-6.9b",
    "lmsys/vicuna-7b-v1.3",
]


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


def _benchmark_family(logical_run_key: str) -> str:
    """Extract the top-level benchmark name from a logical_run_key.

    ``mmlu:model=eleutherai/pythia-6.9b`` → ``mmlu``
    ``civil_comments:model=...`` → ``civil_comments``
    """
    if ":model=" in logical_run_key:
        bench_part, _, _ = logical_run_key.partition(":model=")
    elif ":" in logical_run_key:
        bench_part = logical_run_key.split(":")[0]
    else:
        bench_part = logical_run_key
    return bench_part.strip()


def _model_from_component(component: dict[str, Any]) -> str | None:
    """Pull the model id from a planner component dict."""
    # First try the explicit 'model' field (set by the planner)
    m = (component.get("model") or "").strip()
    if m:
        return m
    # Fallback: parse from logical_run_key
    lrk = (component.get("logical_run_key") or "").strip()
    if ":model=" in lrk:
        _, _, model_part = lrk.partition(":model=")
        return model_part.strip() or None
    return None


def _collect_cells(
    analysis_root: Path,
    abs_tol: float,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Walk core_metric_report.json files and accumulate per-cell data.

    Returns a dict mapping ``(model_id, benchmark_family)`` to::

        {
            "matched": int,            # instances agreeing within abs_tol
            "count": int,               # total paired instances
            "agree_ratio": float | None,
            "n_pairs_with_data": int,   # official_vs_local pairs whose
                                        # instance_level.n_rows > 0
            "n_pairs_total": int,       # all official_vs_local pairs we saw,
                                        # including ones with 0 instance rows
            "n_packets": int,           # number of distinct packet json files
                                        # that targeted this (model, bench)
            "status": str,              # "present" / "join_failed" / "missing"
                                        # (missing == cell absent from result)
        }

    The three statuses are the heatmap's main job: distinguishing cells
    where we just don't have artifacts (``missing``) from cells where we
    do but the instance-level join produced no rows (``join_failed`` —
    investigate the converter / instance-id scheme), from cells with
    real numbers (``present``).
    """
    cells: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "matched": 0,
            "count": 0,
            "n_pairs_with_data": 0,
            "n_pairs_total": 0,
            "n_packets": 0,
        }
    )

    report_paths = sorted(analysis_root.rglob("core_metric_report.json"))
    if not report_paths:
        return {}

    for rp in report_paths:
        try:
            report = json.loads(rp.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        # Extract (model, benchmark) from any component's fields
        model_id: str | None = None
        benchmark: str | None = None
        for comp in (report.get("components") or []):
            lrk = (comp.get("logical_run_key") or "").strip()
            if not lrk:
                continue
            m = _model_from_component(comp)
            if m:
                model_id = m
            b = _benchmark_family(lrk)
            if b:
                benchmark = b
            if model_id and benchmark:
                break

        if not model_id or not benchmark:
            continue

        key = (model_id, benchmark)
        # Track that a packet for this cell exists, regardless of
        # whether its pairs produced any instance-level rows.
        cells[key]["n_packets"] += 1

        # Accumulate instance-level agreement from official_vs_local pairs
        for pair in (report.get("pairs") or []):
            if pair.get("comparison_kind") != "official_vs_local":
                continue
            cells[key]["n_pairs_total"] += 1

            il = pair.get("instance_level") or {}
            avs = il.get("agreement_vs_abs_tol") or []
            if not avs:
                # Pair was disabled or never executed — no rows.
                continue

            # Find the row matching our target abs_tol (exact or nearest)
            best_row = _find_tol_row(avs, abs_tol)
            if best_row is None:
                continue
            if best_row.get("count", 0) == 0:
                # Pair ran but the official↔local instance join produced
                # zero overlapping records. This is the "join_failed"
                # signal; we count the pair but don't accumulate matches.
                continue

            cells[key]["matched"] += best_row["matched"]
            cells[key]["count"] += best_row["count"]
            cells[key]["n_pairs_with_data"] += 1

    # Compute final agree_ratio + status
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for key, cell in cells.items():
        if cell["count"] > 0:
            ratio: float | None = cell["matched"] / cell["count"]
            status = "present"
        else:
            ratio = None
            # We saw a packet for this cell but the join produced no
            # instance-level rows. That's join_failed (distinct from
            # the cell being absent entirely, which we represent as
            # missing-from-the-result-dict).
            status = "join_failed"
        result[key] = {
            "matched": cell["matched"],
            "count": cell["count"],
            "agree_ratio": ratio,
            "n_pairs_with_data": cell["n_pairs_with_data"],
            "n_pairs_total": cell["n_pairs_total"],
            "n_packets": cell["n_packets"],
            "status": status,
        }
    return result


def _find_tol_row(
    avs: list[dict[str, Any]],
    target: float,
) -> dict[str, Any] | None:
    """Return the avs row whose abs_tol is closest to ``target``."""
    if not avs:
        return None
    best: dict[str, Any] | None = None
    best_dist = math.inf
    for row in avs:
        t = row.get("abs_tol")
        if t is None:
            continue
        dist = abs(float(t) - target)
        if dist < best_dist:
            best_dist = dist
            best = row
    return best


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------


def _render_text_table(
    cells: dict[tuple[str, str], dict[str, Any]],
    models: list[str],
    benchmarks: list[str],
    abs_tol: float,
) -> str:
    """Render a fixed-width table with three cell states::

        0.987    -> present (number is the agree_ratio at abs_tol)
        join0/3  -> join_failed: 0 of 3 official_vs_local pairs joined
                    (data exists on both sides but instance-id join
                    produced 0 overlapping rows; investigate the
                    converter / id scheme)
        --       -> missing: no packet exists for this (model, bench)
    """
    lines: list[str] = [
        f"Reproducibility heatmap (abs_tol={abs_tol})",
        f"Instance-level agree_ratio: fraction of pairs within ±{abs_tol}",
        "",
        "Cell legend:",
        "  0.987    instance-level agree_ratio at the chosen abs_tol",
        "  join0/N  packet exists; 0 of N official_vs_local pairs joined",
        "  --       no packet for this (model, benchmark)",
        "",
    ]
    col_w = 14
    bench_w = 26
    header = f"{'Benchmark':<{bench_w}}" + "".join(
        f"{_MODEL_DISPLAY.get(m, m)[:col_w]:>{col_w}}" for m in models
    )
    lines.append(header)
    lines.append("-" * len(header))
    for bench in benchmarks:
        row = f"{_BENCHMARK_DISPLAY.get(bench, bench):<{bench_w}}"
        for m in models:
            cell = cells.get((m, bench))
            if cell is None:
                row += f"{'--':>{col_w}}"
            elif cell.get("status") == "present":
                row += f"{cell['agree_ratio']:>{col_w}.3f}"
            else:
                # join_failed
                marker = f"join0/{cell.get('n_pairs_total', 0)}"
                row += f"{marker:>{col_w}}"
        lines.append(row)
    lines.append("")
    # Coverage summary: how many cells in each state.
    n_present = sum(1 for c in cells.values() if c.get("status") == "present")
    n_join_failed = sum(1 for c in cells.values() if c.get("status") == "join_failed")
    n_total = len(models) * len(benchmarks)
    n_missing = n_total - n_present - n_join_failed
    lines.append(
        f"Coverage: {n_present} present / {n_join_failed} join_failed / "
        f"{n_missing} missing  (of {n_total} cells)"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Heatmap rendering
# ---------------------------------------------------------------------------


def _render_heatmap(
    cells: dict[tuple[str, str], dict[str, Any]],
    models: list[str],
    benchmarks: list[str],
    abs_tol: float,
    title: str,
    out_dir: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    import numpy as np

    n_bench = len(benchmarks)
    n_models = len(models)

    fig_w = max(6.0, 2.2 * n_models + 2.0)
    fig_h = max(5.0, 0.5 * n_bench + 1.5)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # Colormap: RdYlGn for agreement (red=0, green=1)
    cmap = plt.get_cmap("RdYlGn")
    cmap_norm = mcolors.Normalize(vmin=0.0, vmax=1.0)
    cmap_scalar = plt.cm.ScalarMappable(norm=cmap_norm, cmap=cmap)

    # Background defaults to the "missing" color so any cell we don't
    # explicitly draw shows as missing.
    _MISSING_COLOR = "#bdbdbd"
    _JOIN_FAILED_COLOR = "#fff4d6"  # light amber — not red (it's not bad
                                     # data, just unavailable for analysis
                                     # until the converter mismatch is fixed)
    ax.set_facecolor(_MISSING_COLOR)

    # Draw each cell explicitly so the three statuses get distinct visuals.
    for i, bench in enumerate(benchmarks):
        for j, model in enumerate(models):
            cell = cells.get((model, bench))
            if cell is not None and cell.get("status") == "present":
                # Real value: colored by agree_ratio
                val = cell["agree_ratio"]
                rect = plt.Rectangle(
                    (j - 0.5, i - 0.5), 1, 1,
                    facecolor=cmap(cmap_norm(val)),
                    edgecolor="white", linewidth=0.5,
                )
                ax.add_patch(rect)
                text_color = (
                    "black" if 0.3 < val < 0.8
                    else "white" if val <= 0.3
                    else "black"
                )
                ax.text(
                    j, i,
                    f"{val:.3f}",
                    ha="center", va="center",
                    fontsize=8, color=text_color, fontweight="bold",
                )
            elif cell is not None and cell.get("status") == "join_failed":
                # Light amber + diagonal hatching → "data exists but no
                # join". Distinct from missing (solid gray) so a quick
                # glance tells you which gap is fixable.
                rect = plt.Rectangle(
                    (j - 0.5, i - 0.5), 1, 1,
                    facecolor=_JOIN_FAILED_COLOR,
                    edgecolor="white", linewidth=0.5,
                    hatch="////",
                )
                ax.add_patch(rect)
                n_total = cell.get("n_pairs_total", 0)
                ax.text(
                    j, i,
                    f"join 0/{n_total}",
                    ha="center", va="center",
                    fontsize=7, color="#7a4f00", fontweight="bold",
                )
            else:
                # Missing: solid darker gray + dash. Drawn explicitly so
                # the cell border visually delimits it from the
                # background of the same color.
                rect = plt.Rectangle(
                    (j - 0.5, i - 0.5), 1, 1,
                    facecolor=_MISSING_COLOR,
                    edgecolor="white", linewidth=0.5,
                )
                ax.add_patch(rect)
                ax.text(
                    j, i, "—",
                    ha="center", va="center",
                    fontsize=10, color="#606060",
                )

    # Axis labels
    ax.set_xticks(range(n_models))
    ax.set_xticklabels(
        [_MODEL_DISPLAY.get(m, m) for m in models],
        fontsize=9, ha="right", rotation=25,
    )
    ax.set_yticks(range(n_bench))
    ax.set_yticklabels(
        [_BENCHMARK_DISPLAY.get(b, b) for b in benchmarks],
        fontsize=8,
    )
    ax.set_xlim(-0.5, n_models - 0.5)
    ax.set_ylim(-0.5, n_bench - 0.5)
    ax.invert_yaxis()

    # Colorbar for the present-status colormap
    cbar = fig.colorbar(cmap_scalar, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("agree_ratio", fontsize=8)
    cbar.ax.tick_params(labelsize=7)

    # Three-state legend just below the title.
    from matplotlib.patches import Patch
    legend_handles = [
        Patch(facecolor=cmap(cmap_norm(0.5)), edgecolor="white",
              label="present (agree_ratio shown)"),
        Patch(facecolor=_JOIN_FAILED_COLOR, edgecolor="white",
              hatch="////", label="join_failed (data, but 0 instances joined)"),
        Patch(facecolor=_MISSING_COLOR, edgecolor="white",
              label="missing (no packet for this cell)"),
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper center", bbox_to_anchor=(0.5, -0.08),
        ncol=3, fontsize=7, frameon=False,
    )

    ax.set_title(
        f"{title}\n"
        f"instance-level agree_ratio at abs_tol={abs_tol}",
        fontsize=9, pad=8,
    )

    plt.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "reproducibility_heatmap.png"
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Heatmap saved: {png_path}")


# ---------------------------------------------------------------------------
# JSON summary
# ---------------------------------------------------------------------------


def _save_cell_data(
    cells: dict[tuple[str, str], dict[str, Any]],
    models: list[str],
    benchmarks: list[str],
    abs_tol: float,
    out_dir: Path,
) -> None:
    rows = []
    for bench in benchmarks:
        for model in models:
            cell = cells.get((model, bench))
            if cell is None:
                rows.append(
                    {
                        "model": model,
                        "benchmark": bench,
                        "abs_tol": abs_tol,
                        "status": "missing",
                        "agree_ratio": None,
                        "matched": None,
                        "count": None,
                        "n_pairs_with_data": 0,
                        "n_pairs_total": 0,
                        "n_packets": 0,
                    }
                )
            else:
                rows.append(
                    {
                        "model": model,
                        "benchmark": bench,
                        "abs_tol": abs_tol,
                        "status": cell.get("status", "unknown"),
                        "agree_ratio": cell["agree_ratio"],
                        "matched": cell["matched"],
                        "count": cell["count"],
                        "n_pairs_with_data": cell.get("n_pairs_with_data", 0),
                        "n_pairs_total": cell.get("n_pairs_total", 0),
                        "n_packets": cell.get("n_packets", 0),
                    }
                )
    out_path = out_dir / "cell_data.json"
    out_path.write_text(json.dumps({"abs_tol": abs_tol, "cells": rows}, indent=2) + "\n")
    print(f"Cell data saved: {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--analysis-root",
        required=True,
        help="Root of the eval-audit-from-eee output (contains core_metric_report.json files).",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Directory to write heatmap outputs into.",
    )
    parser.add_argument(
        "--abs-tol",
        type=float,
        default=1e-9,
        help="Agreement threshold (default: 1e-9, between exact-match and 10-pico).",
    )
    parser.add_argument(
        "--title",
        default="EEE-only reproducibility heatmap",
        help="Figure title.",
    )
    args = parser.parse_args(argv)

    analysis_root = Path(args.analysis_root).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    abs_tol: float = args.abs_tol
    title: str = args.title

    if not analysis_root.exists():
        raise SystemExit(f"FAIL: analysis-root does not exist: {analysis_root}")

    print(f"Collecting cell data from {analysis_root} (abs_tol={abs_tol}) ...")
    cells = _collect_cells(analysis_root, abs_tol)
    print(f"  found {len(cells)} (model, benchmark) cells with data")

    # Determine which models / benchmarks appear in the data
    found_models = {m for (m, _) in cells}
    found_benchmarks = {b for (_, b) in cells}

    models = [m for m in _MODEL_ORDER if m in found_models]
    # Include any extra models not in the canonical order
    models += sorted(found_models - set(_MODEL_ORDER))

    benchmarks = [b for b in _BENCHMARK_ORDER if b in found_benchmarks]
    benchmarks += sorted(found_benchmarks - set(_BENCHMARK_ORDER))

    if not models or not benchmarks:
        raise SystemExit(
            "FAIL: no cell data found. "
            "Check that 20_run.sh completed and produced core_metric_report.json files "
            f"under {analysis_root}."
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    # Text table
    text = _render_text_table(cells, models, benchmarks, abs_tol)
    txt_path = out_dir / "reproducibility_heatmap.txt"
    txt_path.write_text(text)
    print(text)
    print(f"Text table saved: {txt_path}")

    # JSON cell data
    _save_cell_data(cells, models, benchmarks, abs_tol, out_dir)

    # Heatmap PNG
    try:
        _render_heatmap(cells, models, benchmarks, abs_tol, title, out_dir)
    except ImportError as exc:
        print(f"WARNING: matplotlib not available ({exc}); skipping PNG output.")


if __name__ == "__main__":
    main()
