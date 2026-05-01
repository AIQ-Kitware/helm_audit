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
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import safer
from loguru import logger

from eval_audit.infra.fs_publish import write_text_atomic
from eval_audit.infra.logging import rich_link, setup_cli_logging
from eval_audit.infra.report_layout import (
    portable_repo_root_lines,
    write_reproduce_script,
)

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func

# ---------------------------------------------------------------------------
# Display label tables
# ---------------------------------------------------------------------------

_MODEL_DISPLAY: dict[str, str] = {
    "eleutherai/pythia-6.9b": "Pythia-6.9B",
    "lmsys/vicuna-7b-v1.3": "Vicuna-7B-v1.3",
    "tiiuae/falcon-7b": "Falcon-7B",
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
    "eleutherai/pythia-6.9b",
    "lmsys/vicuna-7b-v1.3",
    "tiiuae/falcon-7b",
]


# Bookkeeping metrics: HELM emits these per-instance fields with
# every run, but they're deterministic counts/labels (input length,
# token counts, finish reason, etc.) that are uniformly reproducible
# and don't carry information about the *model's* score agreement.
# Filtered out of the per-metric heatmap by default so the picture
# focuses on actual scoring metrics where reproducibility variation
# lives. Override with ``--include-bookkeeping``.
_BOOKKEEPING_METRICS: frozenset[str] = frozenset({
    "batch_size",
    "finish_reason_endoftext",
    "finish_reason_length",
    "finish_reason_stop",
    "finish_reason_unknown",
    "inference_runtime",
    "logprob",
    "max_prob",
    "num_bytes",
    "num_completion_tokens",
    "num_output_tokens",
    "num_perplexity_tokens",
    "num_prompt_tokens",
    "num_references",
    "num_train_instances",
    "num_train_trials",
    "prompt_truncated",
    # tokenization metrics also noise-free for reproducibility purposes
    "training_co2_cost",
    "training_energy_cost",
})


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


@profile
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
            "n_joined_pairs": int,      # sum of instance_level.n_joined_pairs
                                        # across all official_vs_local pairs.
                                        # Pre-classifier-filter join count
                                        # used to discriminate join_failed vs
                                        # no_core_metrics.
            "n_packets": int,           # number of distinct packet json files
                                        # that targeted this (model, bench)
            "status": str,              # "present" / "join_failed" /
                                        # "no_core_metrics" / "missing"
                                        # (missing == cell absent from result)
        }

    The four statuses distinguish:

    * ``present`` — data joined and at least one core metric scored.
    * ``join_failed`` — ``n_joined_pairs == 0``: sample_hashes never
      overlapped between official and local. **Upstream data problem**;
      investigate converter / scenario / dataset version / HELM RNG.
    * ``no_core_metrics`` — ``n_joined_pairs > 0`` but ``count == 0``:
      data joined fine, but every row was filtered by ``classify_metric``
      because no metric in the run had a prefix in
      :data:`eval_audit.helm.metrics.METRIC_PREFIXES.CORE_PREFIXES`.
      **Analyzer-side gap**: register the missing metric family.
    * ``missing`` — cell absent from the result dict (no packet at all).
    """
    cells: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "matched": 0,
            "count": 0,
            "n_pairs_with_data": 0,
            "n_pairs_total": 0,
            "n_joined_pairs": 0,
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
            # Pre-classifier-filter join count. Older reports without
            # this field default to 0; the resulting status defaults to
            # the conservative join_failed case (no upgrade to
            # no_core_metrics without explicit evidence). Re-render the
            # packet to populate this field.
            cells[key]["n_joined_pairs"] += int(il.get("n_joined_pairs", 0))

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
                # zero overlapping records (or the classifier filtered
                # everything out). The cell-level status code below
                # disambiguates these via n_joined_pairs.
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
        elif cell["n_joined_pairs"] > 0:
            ratio = None
            # Sample_hashes overlapped between official and local, but
            # every row was filtered by classify_metric. Means
            # eval_audit.helm.metrics.CORE_PREFIXES is missing a
            # metric family used by this benchmark.
            status = "no_core_metrics"
        else:
            ratio = None
            # No overlap at the join key level — sample_hashes (or
            # sample_ids in the fallback) never matched. Real upstream
            # data problem.
            status = "join_failed"
        result[key] = {
            "matched": cell["matched"],
            "count": cell["count"],
            "agree_ratio": ratio,
            "n_pairs_with_data": cell["n_pairs_with_data"],
            "n_pairs_total": cell["n_pairs_total"],
            "n_joined_pairs": cell["n_joined_pairs"],
            "n_packets": cell["n_packets"],
            "status": status,
        }
    return result


@profile
def _collect_cells_per_metric(
    analysis_root: Path,
    abs_tol: float,
    *,
    include_bookkeeping: bool = False,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Like :func:`_collect_cells` but split by metric.

    Returns a dict keyed on ``(model_id, benchmark_family, metric_name)``.
    Each per-pair report's ``instance_level.per_metric_agreement`` provides
    the per-metric breakdown — the same shape as ``agreement_vs_abs_tol``
    but one curve per metric. We micro-average ``matched`` / ``count``
    across all ``official_vs_local`` pairs that contributed to that
    (model, benchmark, metric) cell.

    ``include_bookkeeping=False`` (default) drops metrics in
    :data:`_BOOKKEEPING_METRICS` — counts/labels that are
    deterministic by construction and uniformly reproducible, so they
    don't tell us anything about the model's score-level reproducibility.
    Set to True to include them (e.g. to verify that bookkeeping really
    is uniform).
    """
    cells: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
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

        # Same model/benchmark resolution as the parent function.
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

        for pair in (report.get("pairs") or []):
            if pair.get("comparison_kind") != "official_vs_local":
                continue
            il = pair.get("instance_level") or {}
            per_metric = il.get("per_metric_agreement") or {}
            if not per_metric:
                # Pair has no per-metric breakdown — likely an empty
                # join. Don't count it; the (model, benchmark) overall
                # heatmap captures the "packet exists but join failed"
                # signal already.
                continue
            for metric, avs in per_metric.items():
                if not avs:
                    continue
                if not include_bookkeeping and metric in _BOOKKEEPING_METRICS:
                    continue
                key = (model_id, benchmark, metric)
                cells[key]["n_pairs_total"] += 1
                best_row = _find_tol_row(avs, abs_tol)
                if best_row is None or best_row.get("count", 0) == 0:
                    continue
                cells[key]["matched"] += best_row["matched"]
                cells[key]["count"] += best_row["count"]
                cells[key]["n_pairs_with_data"] += 1

    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, cell in cells.items():
        if cell["count"] > 0:
            ratio: float | None = cell["matched"] / cell["count"]
            status = "present"
        else:
            ratio = None
            status = "join_failed"
        result[key] = {
            "matched": cell["matched"],
            "count": cell["count"],
            "agree_ratio": ratio,
            "n_pairs_with_data": cell["n_pairs_with_data"],
            "n_pairs_total": cell["n_pairs_total"],
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
    """Render a fixed-width table with four cell states::

        0.987    -> present (number is the agree_ratio at abs_tol)
        join0/3  -> join_failed: sample_hashes never overlapped between
                    official and local. Upstream data problem.
        nocore   -> no_core_metrics: data joined but every row was
                    filtered by classify_metric. Analyzer-side gap;
                    register the missing metric family in
                    eval_audit/helm/metrics.py:CORE_PREFIXES.
        --       -> missing: no packet exists for this (model, bench)
    """
    lines: list[str] = [
        f"Reproducibility heatmap (abs_tol={abs_tol})",
        f"Instance-level agree_ratio: fraction of pairs within ±{abs_tol}",
        "",
        "Cell legend:",
        "  0.987    instance-level agree_ratio at the chosen abs_tol",
        "  join0/N  no hash overlap (upstream data problem)",
        "  nocore   joined but no recognized core metrics (analyzer gap)",
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
            else:
                status = cell.get("status")
                if status == "present":
                    row += f"{cell['agree_ratio']:>{col_w}.3f}"
                elif status == "no_core_metrics":
                    row += f"{'nocore':>{col_w}}"
                else:
                    marker = f"join0/{cell.get('n_pairs_total', 0)}"
                    row += f"{marker:>{col_w}}"
        lines.append(row)
    lines.append("")
    # Coverage summary: how many cells in each state.
    n_present = sum(1 for c in cells.values() if c.get("status") == "present")
    n_join_failed = sum(1 for c in cells.values() if c.get("status") == "join_failed")
    n_no_core = sum(1 for c in cells.values() if c.get("status") == "no_core_metrics")
    n_total = len(models) * len(benchmarks)
    n_missing = n_total - n_present - n_join_failed - n_no_core
    lines.append(
        f"Coverage: {n_present} present / {n_join_failed} join_failed / "
        f"{n_no_core} no_core_metrics / {n_missing} missing  "
        f"(of {n_total} cells)"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Heatmap rendering
# ---------------------------------------------------------------------------


def _atomic_savefig(fig, fpath: Path, **kwargs) -> Path:
    """``fig.savefig`` to ``fpath`` atomically via safer (parent dirs auto-
    created). Format is inferred from the suffix; defaults to png."""
    fpath = Path(fpath)
    suffix = fpath.suffix.lstrip(".") or "png"
    with safer.open(fpath, "wb", make_parents=True) as fp:
        fig.savefig(fp, format=suffix, **kwargs)
    return fpath


@profile
def _render_heatmap(
    cells: dict[tuple[str, str], dict[str, Any]],
    models: list[str],
    benchmarks: list[str],
    abs_tol: float,
    title: str,
    out_dir: Path,
    *,
    out_filename: str = "reproducibility_heatmap.png",
    subtitle: str | None = None,
) -> Path:
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

    # Colormap: RdYlGn for agreement (red=low, green=high). The default
    # range is tightened to [0.7, 1.0] because for the slim-paper heatmap
    # every present cell sits in [0.788, 1.000] — using vmin=0 wastes
    # most of the gradient on values that never occur and leaves the
    # actual data clustered in the green band where small differences
    # are imperceptible. Override per-render via env vars
    # EVAL_AUDIT_HEATMAP_VMIN / EVAL_AUDIT_HEATMAP_VMAX.
    cmap_vmin = float(os.environ.get("EVAL_AUDIT_HEATMAP_VMIN", "0.7"))
    cmap_vmax = float(os.environ.get("EVAL_AUDIT_HEATMAP_VMAX", "1.0"))
    cmap = plt.get_cmap("RdYlGn")
    cmap_norm = mcolors.Normalize(vmin=cmap_vmin, vmax=cmap_vmax)
    cmap_scalar = plt.cm.ScalarMappable(norm=cmap_norm, cmap=cmap)

    # Background defaults to the "missing" color so any cell we don't
    # explicitly draw shows as missing.
    _MISSING_COLOR = "#bdbdbd"
    _JOIN_FAILED_COLOR = "#fff4d6"  # light amber — not red (it's not bad
                                     # data, just unavailable for analysis
                                     # until the converter mismatch is fixed)
    _NO_CORE_METRICS_COLOR = "#e1bee7"  # light purple — distinct from amber
                                         # so a reviewer can tell at a glance
                                         # that the failure is analyzer-side
                                         # (missing metric registration), not
                                         # an upstream data problem.
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
                # Pick text color based on the *normalized* position
                # (0..1) within the cmap range so the choice tracks the
                # actual cell color regardless of vmin/vmax.
                norm_val = float(cmap_norm(val))
                text_color = "white" if (norm_val < 0.2 or norm_val > 0.85) else "black"
                ax.text(
                    j, i,
                    f"{val:.3f}",
                    ha="center", va="center",
                    fontsize=8, color=text_color, fontweight="bold",
                )
            elif cell is not None and cell.get("status") == "join_failed":
                # Light amber + diagonal hatching → "no hash overlap"
                # (upstream data problem). Distinct from missing
                # (solid gray) so a quick glance tells you which gap
                # is fixable.
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
            elif cell is not None and cell.get("status") == "no_core_metrics":
                # Light purple + dotted hatching → "joined but no
                # recognized core metrics". Analyzer-side gap; the fix
                # is to extend CORE_PREFIXES, not to investigate the
                # data.
                rect = plt.Rectangle(
                    (j - 0.5, i - 0.5), 1, 1,
                    facecolor=_NO_CORE_METRICS_COLOR,
                    edgecolor="white", linewidth=0.5,
                    hatch="....",
                )
                ax.add_patch(rect)
                ax.text(
                    j, i,
                    "no core",
                    ha="center", va="center",
                    fontsize=7, color="#4a148c", fontweight="bold",
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

    # Build the legend dynamically: only include status entries that
    # actually appear in this heatmap. For the slim-paper render, that
    # typically means just `present` + `join_failed`, with
    # `no_core_metrics` and `missing` omitted (they exist as fallbacks
    # for other heatmaps but aren't relevant here).
    from matplotlib.patches import Patch
    statuses_present = {c.get("status") for c in cells.values() if c}
    has_missing_in_grid = any(
        cells.get((m, b)) is None
        for m in models for b in benchmarks
    )
    # Use a high-end swatch for `present` so it visually matches what
    # readers see on the actual cells (deep green, not the gradient
    # midpoint which lands in yellow with a tightened vmin).
    legend_handles = [
        Patch(facecolor=cmap(cmap_norm(cmap_vmax)), edgecolor="white",
              label="present (agree_ratio shown)"),
    ]
    if "join_failed" in statuses_present:
        legend_handles.append(
            Patch(facecolor=_JOIN_FAILED_COLOR, edgecolor="white",
                  hatch="////",
                  label="join_failed (no hash overlap; upstream)")
        )
    if "no_core_metrics" in statuses_present:
        legend_handles.append(
            Patch(facecolor=_NO_CORE_METRICS_COLOR, edgecolor="white",
                  hatch="....",
                  label="no_core_metrics (joined; classifier gap)")
        )
    if has_missing_in_grid:
        legend_handles.append(
            Patch(facecolor=_MISSING_COLOR, edgecolor="white",
                  label="missing (no packet for this cell)")
        )
    ax.legend(
        handles=legend_handles,
        loc="upper center", bbox_to_anchor=(0.5, -0.08),
        ncol=min(len(legend_handles), 3), fontsize=7, frameon=False,
    )

    sub = subtitle if subtitle is not None else (
        f"instance-level agree_ratio at abs_tol={abs_tol}"
    )
    ax.set_title(
        f"{title}\n{sub}",
        fontsize=9, pad=8,
    )

    plt.tight_layout()
    png_path = out_dir / out_filename
    _atomic_savefig(fig, png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info(f"Wrote heatmap: {rich_link(png_path)}")
    return png_path


# ---------------------------------------------------------------------------
# Per-metric heatmaps (one figure per metric)
# ---------------------------------------------------------------------------


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_part(name: str) -> str:
    """Sanitize a metric name for use in a filename. Replaces any run of
    non ``[A-Za-z0-9._-]`` characters with a single underscore so things
    like ``exact_match@5`` become ``exact_match_5``."""
    cleaned = _FILENAME_SAFE_RE.sub("_", name).strip("_")
    return cleaned or "metric"


@profile
def _render_per_metric_heatmaps(
    cells: dict[tuple[str, str, str], dict[str, Any]],
    models: list[str],
    benchmarks: list[str],
    metrics_in_order: list[str],
    abs_tol: float,
    title: str,
    out_dir: Path,
) -> list[Path]:
    """Emit one ``model × benchmark`` heatmap per metric.

    Each plot has the same shape as the main heatmap (rows = benchmarks
    in canonical order, columns = models), so the eye can flip between
    metrics without re-learning the layout. Plots land in
    ``<out_dir>/reproducibility_heatmap_per_metric/<metric>.png``.

    Cells where the metric isn't present for a (model, benchmark) pair
    render as the standard "missing" gray — the per-metric coverage is
    naturally sparse (e.g. ``exact_match@5`` only on retrieval-style
    benchmarks) and the gray makes that visible.
    """
    sub_dir = out_dir / "reproducibility_heatmap_per_metric"
    sub_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for metric in metrics_in_order:
        # Filter to (model, benchmark) cells for this one metric.
        per_metric_cells: dict[tuple[str, str], dict[str, Any]] = {
            (m, b): cell
            for (m, b, met), cell in cells.items()
            if met == metric
        }
        if not per_metric_cells:
            continue
        # Drop benchmarks that don't use this metric — otherwise every
        # plot shows a wall of gray "missing" rows for benchmarks that
        # never report it (e.g. bleu_1 only applies to NarrativeQA, so
        # the BoolQ/MMLU/IMDB/... rows are pure noise on that plot).
        benchmarks_for_metric = [
            b for b in benchmarks
            if any((m, b) in per_metric_cells for m in models)
        ]
        if not benchmarks_for_metric:
            continue
        png_path = _render_heatmap(
            per_metric_cells,
            models,
            benchmarks_for_metric,
            abs_tol,
            f"{title} — metric: {metric}",
            sub_dir,
            out_filename=f"{_safe_filename_part(metric)}.png",
            subtitle=(
                f"instance-level agree_ratio at abs_tol={abs_tol} "
                f"(metric: {metric})"
            ),
        )
        written.append(png_path)
    return written


def _render_per_metric_text_table(
    cells: dict[tuple[str, str, str], dict[str, Any]],
    models: list[str],
    rows_in_order: list[tuple[str, str]],
    abs_tol: float,
) -> str:
    """Plain-text equivalent of the per-metric heatmap. Useful for
    grepping ("which metric is the WikiFact 0.92 floor?") and for
    pasting into commit messages / paper drafts.
    """
    lines: list[str] = [
        f"Per-metric reproducibility heatmap (abs_tol={abs_tol})",
        f"Instance-level agree_ratio per (benchmark, metric)",
        "",
        "Cell legend:",
        "  0.987    instance-level agree_ratio at the chosen abs_tol",
        "  join0/N  packet exists; 0 of N official_vs_local pairs joined",
        "  --       this metric not present for that (model, benchmark)",
        "",
    ]
    col_w = 14
    label_w = 48
    header = f"{'Benchmark / metric':<{label_w}}" + "".join(
        f"{_MODEL_DISPLAY.get(m, m)[:col_w]:>{col_w}}" for m in models
    )
    lines.append(header)
    lines.append("-" * len(header))
    prev_bench = None
    for bench, metric in rows_in_order:
        # Group separator
        if prev_bench is not None and bench != prev_bench:
            lines.append("")
        prev_bench = bench
        label = f"{_BENCHMARK_DISPLAY.get(bench, bench)}: {metric}"
        row = f"{label[:label_w]:<{label_w}}"
        for m in models:
            cell = cells.get((m, bench, metric))
            if cell is None:
                row += f"{'--':>{col_w}}"
            elif cell.get("status") == "present":
                row += f"{cell['agree_ratio']:>{col_w}.3f}"
            else:
                marker = f"join0/{cell.get('n_pairs_total', 0)}"
                row += f"{marker:>{col_w}}"
        lines.append(row)

    n_present = sum(1 for c in cells.values() if c.get("status") == "present")
    n_join_failed = sum(1 for c in cells.values() if c.get("status") == "join_failed")
    lines.append("")
    lines.append(
        f"Coverage: {n_present} present / {n_join_failed} join_failed "
        f"(of {len(cells)} (model, benchmark, metric) cells with data)"
    )
    lines.append("")
    return "\n".join(lines)


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
                        "n_joined_pairs": 0,
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
                        "n_joined_pairs": cell.get("n_joined_pairs", 0),
                        "n_packets": cell.get("n_packets", 0),
                    }
                )
    out_path = out_dir / "cell_data.json"
    write_text_atomic(
        out_path,
        json.dumps({"abs_tol": abs_tol, "cells": rows}, indent=2) + "\n",
    )
    logger.info(f"Wrote cell data: {rich_link(out_path)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@profile
def _write_redraw_plots_script(
    out_dir: Path,
    analysis_root: Path,
    abs_tol: float,
    title: str,
    per_metric: bool,
    include_bookkeeping: bool,
) -> Path:
    """Drop a self-contained ``redraw_plots.sh`` next to the heatmap outputs.

    The script re-invokes ``python -m eval_audit.reports.eee_only_heatmap``
    with the same arguments that produced the current outputs, so an
    iteration loop on plot styling (color scale, legend, layout) is
    just: edit ``eval_audit/reports/eee_only_heatmap.py`` and rerun
    ``bash redraw_plots.sh`` from the heatmap output dir.

    Captures the colormap env vars
    (``EVAL_AUDIT_HEATMAP_VMIN`` / ``EVAL_AUDIT_HEATMAP_VMAX``) at
    generation time so re-renders use the same color scale unless the
    user explicitly overrides via the same env vars.
    """
    import shlex

    # Mirror the invocation actually used.
    cmd_parts = [
        "-m", "eval_audit.reports.eee_only_heatmap",
        "--analysis-root", str(analysis_root),
        # Resolve out-dir at script-run time so this script is portable
        # across moves/copies of the heatmap dir (the script lives next
        # to its own outputs).
        "--out-dir", '"$SCRIPT_DIR"',
        "--abs-tol", str(abs_tol),
        "--title", title,
    ]
    if per_metric:
        cmd_parts.append("--per-metric")
    if include_bookkeeping:
        cmd_parts.append("--include-bookkeeping")

    # Quote every fixed arg; the "$SCRIPT_DIR" placeholder must remain
    # unquoted so the shell expands it.
    quoted_parts: list[str] = []
    for part in cmd_parts:
        if part == '"$SCRIPT_DIR"':
            quoted_parts.append(part)
        else:
            quoted_parts.append(shlex.quote(part))
    cmd_str = " ".join(quoted_parts)

    # Capture colormap env vars so the regenerated PNG matches the
    # original styling unless the user explicitly overrides them. The
    # vars hold short numeric strings like "0.7" / "1.0"; the
    # ``${VAR:-default}`` indirection means a value already in the
    # environment at run time still wins.
    captured_env_lines: list[str] = []
    for var in ("EVAL_AUDIT_HEATMAP_VMIN", "EVAL_AUDIT_HEATMAP_VMAX"):
        v = os.environ.get(var)
        if v is not None:
            quoted = shlex.quote(v)
            captured_env_lines.append(
                f'export {var}="${{{var}:-$(echo {quoted})}}"'
            )

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# Regenerate the heatmap PNG + per-metric drill-down PNGs from",
        "# the per-packet core_metric_report.json files this output was",
        "# computed from. Use this when iterating on plot styling: edit",
        "# eval_audit/reports/eee_only_heatmap.py and rerun.",
        "#",
        "# Output dir is resolved as the directory this script lives in,",
        "# so the script remains valid if you copy/move the heatmap dir.",
        "# Override REPO_ROOT to point at a different eval_audit checkout.",
        "# Override EVAL_AUDIT_HEATMAP_VMIN / EVAL_AUDIT_HEATMAP_VMAX to",
        "# adjust the color scale (defaults captured from generation).",
        *portable_repo_root_lines(),
        *captured_env_lines,
        'cd "$REPO_ROOT"',
        f'PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" {cmd_str} "$@"',
    ]
    return write_reproduce_script(out_dir / "redraw_plots.sh", lines)


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
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
    parser.add_argument(
        "--per-metric",
        action="store_true",
        default=False,
        help=(
            "Also emit a per-(benchmark, metric) heatmap. Drills down "
            "from the one-number-per-cell view to show which scoring "
            "metric is responsible for a benchmark's agree_ratio. The "
            "regular benchmark-level heatmap is still written."
        ),
    )
    parser.add_argument(
        "--include-bookkeeping",
        action="store_true",
        default=False,
        help=(
            "Include bookkeeping metrics (token counts, finish_reason, "
            "inference_runtime, etc.) in the per-metric heatmap. Default "
            "off because these are deterministic and uniformly "
            "reproducible — they bury the interesting score-level "
            "metrics under a sea of 1.0 cells."
        ),
    )
    args = parser.parse_args(argv)

    analysis_root = Path(args.analysis_root).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    abs_tol: float = args.abs_tol
    title: str = args.title

    if not analysis_root.exists():
        raise SystemExit(f"FAIL: analysis-root does not exist: {analysis_root}")

    logger.info(
        f"Collecting cell data from {rich_link(analysis_root)} "
        f"(abs_tol={abs_tol}) ..."
    )
    cells = _collect_cells(analysis_root, abs_tol)
    logger.info(f"  found {len(cells)} (model, benchmark) cells with data")

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

    # Drop a redraw_plots.sh next to the outputs so iterating on plot
    # styling is "edit code → rerun this script". Written before the
    # rest of the renders so even a partial render leaves a regen
    # script behind.
    redraw_path = _write_redraw_plots_script(
        out_dir=out_dir,
        analysis_root=analysis_root,
        abs_tol=abs_tol,
        title=title,
        per_metric=args.per_metric,
        include_bookkeeping=args.include_bookkeeping,
    )
    logger.info(f"Wrote regen script: {rich_link(redraw_path)}")

    # Text table
    text = _render_text_table(cells, models, benchmarks, abs_tol)
    txt_path = out_dir / "reproducibility_heatmap.txt"
    write_text_atomic(txt_path, text)
    print(text)
    logger.info(f"Wrote text table: {rich_link(txt_path)}")

    # JSON cell data
    _save_cell_data(cells, models, benchmarks, abs_tol, out_dir)

    # Heatmap PNG
    try:
        _render_heatmap(cells, models, benchmarks, abs_tol, title, out_dir)
    except ImportError as exc:
        logger.warning(
            f"matplotlib not available ({exc}); skipping PNG output."
        )

    # Optional per-metric drill-down: one figure per metric, each shaped
    # like the main heatmap (rows = benchmarks, columns = models). The
    # text table and JSON sidecar still list everything in one document
    # so downstream scripts can grep/sort without walking the subdir.
    if args.per_metric:
        logger.info(
            f"Collecting per-(model, benchmark, metric) cells "
            f"(abs_tol={abs_tol}, include_bookkeeping={args.include_bookkeeping}) ..."
        )
        per_metric_cells = _collect_cells_per_metric(
            analysis_root, abs_tol,
            include_bookkeeping=args.include_bookkeeping,
        )
        logger.info(f"  found {len(per_metric_cells)} cells")

        # Row order for the combined text/JSON: walk benchmarks in
        # canonical order, within each benchmark sort metrics alphabetically.
        rows_in_order: list[tuple[str, str]] = []
        for bench in benchmarks:
            metrics_for_bench = sorted({
                metric for (_m, b, metric) in per_metric_cells if b == bench
            })
            rows_in_order.extend((bench, metric) for metric in metrics_for_bench)

        # Plot order: alphabetical by metric name. One figure per metric,
        # so cross-metric comparison is "open the next file" not "scroll
        # the same figure."
        metrics_in_order = sorted({
            metric for (_m, _b, metric) in per_metric_cells
        })

        if not rows_in_order:
            logger.warning("no per-metric cells found; skipping per-metric output.")
        else:
            text_pm = _render_per_metric_text_table(
                per_metric_cells, models, rows_in_order, abs_tol,
            )
            txt_pm = out_dir / "reproducibility_heatmap_per_metric.txt"
            write_text_atomic(txt_pm, text_pm)
            print(text_pm)
            logger.info(f"Wrote per-metric text table: {rich_link(txt_pm)}")

            # Per-metric JSON sidecar — flat list of (model, benchmark,
            # metric, agree_ratio, status, ...) so downstream scripts can
            # filter/sort without re-walking the per-pair reports.
            json_pm = out_dir / "cell_data_per_metric.json"
            pm_rows = [
                {
                    "model": m,
                    "benchmark": b,
                    "metric": metric,
                    "abs_tol": abs_tol,
                    **per_metric_cells[(m, b, metric)],
                }
                for (b, metric) in rows_in_order
                for m in models
                if (m, b, metric) in per_metric_cells
            ]
            write_text_atomic(
                json_pm,
                json.dumps({"abs_tol": abs_tol, "cells": pm_rows}, indent=2) + "\n",
            )
            logger.info(f"Wrote per-metric cell data: {rich_link(json_pm)}")

            try:
                written = _render_per_metric_heatmaps(
                    per_metric_cells, models, benchmarks, metrics_in_order,
                    abs_tol, title, out_dir,
                )
                logger.info(
                    f"Wrote {len(written)} per-metric heatmap(s) under "
                    f"{rich_link(out_dir / 'reproducibility_heatmap_per_metric')}"
                )
            except ImportError as exc:
                logger.warning(
                    f"matplotlib not available ({exc}); "
                    "skipping per-metric PNG output."
                )


if __name__ == "__main__":
    main()
