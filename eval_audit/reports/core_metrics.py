from __future__ import annotations

import argparse
from dataclasses import dataclass

from loguru import logger

from eval_audit.infra.logging import rich_link, setup_cli_logging
import datetime as datetime_mod
import json
import os
import shutil
import statistics
import warnings
from pathlib import Path
from typing import Any

import kwutil
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from eval_audit.helm.diff import HelmRunDiff
from eval_audit.helm import metrics as helm_metrics
from eval_audit.helm.hashers import stable_hash36
from eval_audit.indexing.schema import extract_run_spec_fields
import safer

from eval_audit.infra.fs_publish import link_alias, safe_unlink, write_text_atomic
from eval_audit.normalized import (
    NormalizedRun,
    NormalizedRunRef,
    SourceKind,
    load_run,
)
from eval_audit.normalized import compare as ncompare
from eval_audit.normalized.helm_compat import helm_view
from eval_audit.reports.paper_labels import load_paper_label_manager
from eval_audit.utils.labels import emit_label_legend_artifacts, short_alias_map
from eval_audit.reports.core_packet import load_packet_manifests
from eval_audit.utils.numeric import quantile as _quantile

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func


MetricDomain = tuple[float, float]
_PLOT_TARGETS = {
    'all',
    'core_metric_report',
    'core_metric_distributions',
    'core_metric_overlay_distributions',
    'core_metric_ecdfs',
    'core_metric_per_metric_agreement',
}


def _wants_plot(plot_target: str, plot_name: str) -> bool:
    return plot_target == 'all' or plot_target == plot_name


@dataclass(frozen=True)
class PlotLayout:
    """Matplotlib layout knobs for crowded report figures."""

    # Multiplicative scale applied to every Matplotlib figure size before
    # layout. Increase this when labels/titles are too crowded for the canvas.
    figure_scale: float = 1.5
    # Figure-coordinate y position for the figure-level title. Values near
    # 1.0 place the suptitle at the top edge; larger values move it upward.
    suptitle_y: float | None = 0.995
    # Minimum vertical padding around axes decorations, in inches, for
    # Matplotlib's constrained-layout engine.
    constrained_h_pad: float | None = 0.02
    # Minimum vertical space between subplot groups, as a fraction of the
    # average subplot height, for constrained layout.
    constrained_hspace: float | None = 0.05
    # Minimum horizontal padding around axes decorations, in inches, for
    # Matplotlib's constrained-layout engine.
    constrained_w_pad: float | None = 0.08
    # Minimum horizontal space between subplot groups, as a fraction of the
    # average subplot width, for constrained layout.
    constrained_wspace: float | None = 0.05
    # Manual subplot margin for grids that use fig.subplots_adjust. Values are
    # figure fractions in Matplotlib's [0, 1] coordinate system.
    subplot_left: float | None = None
    # Manual subplot right edge for fig.subplots_adjust, as a figure fraction.
    subplot_right: float | None = None
    # Manual subplot bottom margin for fig.subplots_adjust, as a figure fraction.
    subplot_bottom: float | None = None
    # Manual subplot top edge for fig.subplots_adjust, as a figure fraction.
    subplot_top: float | None = None


def _coalesce(value: float | None, default: float | None) -> float | None:
    return default if value is None else value


def _plot_layout_from_cli(args: argparse.Namespace) -> PlotLayout:
    default = PlotLayout()
    return PlotLayout(
        figure_scale=_coalesce(args.plot_figure_scale, default.figure_scale),
        suptitle_y=_coalesce(args.plot_suptitle_y, default.suptitle_y),
        constrained_h_pad=_coalesce(args.plot_constrained_h_pad, default.constrained_h_pad),
        constrained_hspace=_coalesce(args.plot_constrained_hspace, default.constrained_hspace),
        constrained_w_pad=_coalesce(args.plot_constrained_w_pad, default.constrained_w_pad),
        constrained_wspace=_coalesce(args.plot_constrained_wspace, default.constrained_wspace),
        subplot_left=_coalesce(args.plot_subplot_left, default.subplot_left),
        subplot_right=_coalesce(args.plot_subplot_right, default.subplot_right),
        subplot_bottom=_coalesce(args.plot_subplot_bottom, default.subplot_bottom),
        subplot_top=_coalesce(args.plot_subplot_top, default.subplot_top),
    )


def _scaled_figsize(width: float, height: float, plot_layout: PlotLayout | None = None) -> tuple[float, float]:
    scale = (plot_layout or PlotLayout()).figure_scale
    if scale <= 0:
        scale = 1.0
    return (width * scale, height * scale)


def _apply_matplotlib_style() -> None:
    """Apply the eval_audit matplotlib/seaborn theme.

    Every plotting function that creates a Figure should call this before
    ``plt.subplots`` so the whitegrid background, talk-context font sizes,
    and seaborn palette are consistent across the report. (Plotly plots
    are styled separately; this helper is matplotlib-only.)"""
    sns.set_theme(style='whitegrid', context='talk')


def _palette_color_map(labels: list[str]) -> dict[str, Any]:
    """Map each unique label to its seaborn-palette color in plot order.

    seaborn's ``hue`` semantic assigns colors from the active palette to
    unique values in *sorted* order; this helper mirrors that so a sidecar
    label legend can echo the matching color for each pair/run/etc."""
    unique = sorted(set(labels))
    palette = sns.color_palette(n_colors=max(len(unique), 1))
    return {label: palette[i % len(palette)] for i, label in enumerate(unique)}


def _apply_plot_layout(fig: plt.Figure, plot_layout: PlotLayout | None) -> PlotLayout:
    layout = plot_layout or PlotLayout()
    pad_kwargs = {
        key: value
        for key, value in {
            'h_pad': layout.constrained_h_pad,
            'hspace': layout.constrained_hspace,
            'w_pad': layout.constrained_w_pad,
            'wspace': layout.constrained_wspace,
        }.items()
        if value is not None
    }
    if pad_kwargs:
        layout_engine = fig.get_layout_engine() if hasattr(fig, 'get_layout_engine') else None
        if layout_engine is not None and hasattr(layout_engine, 'set'):
            layout_engine.set(**pad_kwargs)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', PendingDeprecationWarning)
                fig.set_constrained_layout_pads(**pad_kwargs)
    return layout


def _set_suptitle(
    fig: plt.Figure,
    text: str,
    *,
    fontsize: float,
    plot_layout: PlotLayout | None = None,
) -> None:
    layout = _apply_plot_layout(fig, plot_layout)
    kwargs: dict[str, Any] = {'fontsize': fontsize}
    if layout.suptitle_y is not None:
        kwargs['y'] = layout.suptitle_y
    fig.suptitle(text, **kwargs)


def _subplot_adjust_kwargs(
    fig: plt.Figure,
    layout: PlotLayout,
    *,
    top: float = 0.92,
    bottom: float = 0.04,
) -> dict[str, float]:
    """Translate layout knobs into stable manual subplot spacing.

    ``top`` and ``bottom`` are per-plot defaults (not layout-level) for the
    fraction of the figure used by axes; pass a more generous ``bottom``
    when the plot has a labelled x-axis that would otherwise clip on
    short figures. ``layout.subplot_top`` / ``layout.subplot_bottom``
    explicitly override these per-plot defaults when provided.
    """
    fig_w, fig_h = fig.get_size_inches()
    kwargs = {
        'top': layout.subplot_top if layout.subplot_top is not None else top,
        'bottom': layout.subplot_bottom if layout.subplot_bottom is not None else bottom,
    }
    if layout.constrained_h_pad is not None and fig_h > 0:
        vpad = min(0.20, max(0.0, layout.constrained_h_pad / fig_h))
        if layout.subplot_bottom is None:
            kwargs['bottom'] = max(kwargs['bottom'], vpad)
        if layout.subplot_top is None:
            kwargs['top'] = min(kwargs['top'], 1.0 - vpad)
    if layout.constrained_w_pad is not None and fig_w > 0:
        hpad = min(0.20, max(0.0, layout.constrained_w_pad / fig_w))
        if layout.subplot_left is None:
            kwargs['left'] = max(0.04, hpad)
        if layout.subplot_right is None:
            kwargs['right'] = min(0.98, 1.0 - hpad)
    if layout.subplot_left is not None:
        kwargs['left'] = layout.subplot_left
    if layout.subplot_right is not None:
        kwargs['right'] = layout.subplot_right
    if layout.constrained_hspace is not None:
        kwargs['hspace'] = layout.constrained_hspace
    if layout.constrained_wspace is not None:
        kwargs['wspace'] = layout.constrained_wspace
    return kwargs


def _load_json(fpath: Path) -> Any:
    return json.loads(fpath.read_text())


def _load_optional_cross_machine_pair(report_dpath: Path) -> dict[str, Any] | None:
    pair_fpath = report_dpath / 'cross-machine-aiq-gpu' / 'pair_report.json'
    if not pair_fpath.exists():
        return None
    data = _load_json(pair_fpath)
    display = data.get('display_labels', {}) or {}
    label_a = (
        display.get('label_a')
        or ((data.get('inputs') or {}).get('label_a'))
        or 'aiq-gpu'
    )
    label_b = (
        display.get('label_b')
        or ((data.get('inputs') or {}).get('label_b'))
        or 'other-machine'
    )
    highlights = data.get('tolerance_highlights', {}) or {}
    distance = data.get('distance_summary', {}) or {}
    strict = data.get('strict_summary', {}) or {}
    diagnosis = (strict.get('diagnosis') or {})
    return {
        'label': f'{label_a}_vs_{label_b}',
        'diagnosis': diagnosis,
        'run_level': {
            'agreement_vs_abs_tol': highlights.get('run_level', []) or [],
            'overall_quantiles': (distance.get('run_level') or {}).get('overall', {}) or {},
        },
        'instance_level': {
            'agreement_vs_abs_tol': highlights.get('instance_level', []) or [],
            'overall_quantiles': (distance.get('instance_level') or {}).get('overall', {}) or {},
        },
    }


def _collect_stat_means(stats: list[dict[str, Any]], metric_name: str) -> dict[str, float]:
    found = {}
    for row in stats:
        name = row.get('name')
        if not isinstance(name, dict):
            continue
        if name.get('name') != metric_name:
            continue
        split = name.get('split')
        found[str(split)] = row.get('mean')
    return found


_EMPTY_RUN_DIAGNOSTICS: dict[str, Any] = {
    'n_request_states': 0,
    'n_with_completions': 0,
    'empty_completion_count': 0,
    'empty_completion_rate': None,
    'output_token_count': {'mean': None, 'p50': None, 'p90': None, 'max': None},
    'stats_means': {},
}


def _run_diagnostics(run_path: str | None) -> dict[str, Any]:
    """HELM run-dir diagnostics; gracefully skipped for EEE-only components.

    The diagnostics summary (empty-completion rate, prompt/completion token
    counts) is computed from raw HELM ``scenario_state.json`` + ``stats.json``.
    For pure-EEE components we don't have those files; return shape-correct
    zeros instead of crashing so the per-pair report can render the
    instance-level core-metric numbers (which are all the comparison core
    actually consumes anyway)."""
    if not run_path:
        return dict(_EMPTY_RUN_DIAGNOSTICS)
    run_path = str(Path(run_path).expanduser().resolve())
    run_dpath = Path(run_path)
    if not run_dpath.is_dir():
        return dict(_EMPTY_RUN_DIAGNOSTICS)
    scenario_state = _load_json(run_dpath / 'scenario_state.json')
    stats = _load_json(run_dpath / 'stats.json')
    reqs = scenario_state.get('request_states', [])

    token_counts = []
    empty_completion_count = 0
    nonempty_completion_count = 0
    completion_count = 0
    for rs in reqs:
        comps = (rs.get('result') or {}).get('completions') or []
        if not comps:
            continue
        completion_count += 1
        c0 = comps[0] or {}
        text = c0.get('text', '')
        toklist = c0.get('tokens') or []
        token_counts.append(len(toklist))
        if text == '':
            empty_completion_count += 1
        else:
            nonempty_completion_count += 1

    mean_tokens = statistics.mean(token_counts) if token_counts else None
    return {
        'run_path': run_path,
        'run_name': run_dpath.name,
        'n_request_states': len(reqs),
        'n_with_completions': completion_count,
        'empty_completion_count': empty_completion_count,
        'nonempty_completion_count': nonempty_completion_count,
        'empty_completion_rate': (
            empty_completion_count / completion_count if completion_count else None
        ),
        'output_token_count': {
            'mean': mean_tokens,
            'p50': _quantile(token_counts, 0.5),
            'p90': _quantile(token_counts, 0.9),
            'max': _quantile(token_counts, 1.0),
        },
        'stats_means': {
            'num_output_tokens': _collect_stat_means(stats, 'num_output_tokens'),
            'num_completion_tokens': _collect_stat_means(stats, 'num_completion_tokens'),
            'finish_reason_unknown': _collect_stat_means(stats, 'finish_reason_unknown'),
        },
    }


def _diagnostic_flags(
    run_diagnostics: dict[str, dict[str, Any]],
    components: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
) -> list[str]:
    flags = []
    for label, diag in run_diagnostics.items():
        rate = diag.get('empty_completion_rate')
        mean_tokens = (diag.get('output_token_count') or {}).get('mean')
        if rate is not None and rate > 0.1:
            flags.append(f'{label}:high_empty_completion_rate')
        if mean_tokens is not None and mean_tokens < 1.0:
            flags.append(f'{label}:near_zero_mean_output_tokens')
    component_lookup = {component['component_id']: component for component in components}
    official_vs_local = next(
        (
            comparison
            for comparison in comparisons
            if comparison.get('comparison_kind') == 'official_vs_local' and comparison.get('enabled', True)
        ),
        None,
    )
    if official_vs_local is not None:
        component_ids = official_vs_local.get('component_ids') or []
        comparison_components = [
            component_lookup.get(component_id, {})
            for component_id in component_ids
        ]
        reference_component = component_lookup.get(
            official_vs_local.get('reference_component_id'),
            {},
        )
        official_component = next(
            (component for component in comparison_components if component.get('source_kind') == 'official'),
            None,
        )
        local_component = next(
            (component for component in comparison_components if component.get('source_kind') == 'local'),
            None,
        )
        if reference_component and reference_component.get('source_kind') == 'official':
            official_component = reference_component
        elif reference_component and reference_component.get('source_kind') == 'local':
            local_component = reference_component
        official_diag = (
            run_diagnostics.get(official_component.get('component_id'), {})
            if official_component is not None else {}
        )
        local_diag = (
            run_diagnostics.get(local_component.get('component_id'), {})
            if local_component is not None else {}
        )
        official_rate = official_diag.get('empty_completion_rate')
        local_rate = local_diag.get('empty_completion_rate')
        if (
            official_component is not None
            and local_component is not None
            and official_rate is not None
            and local_rate is not None
            and official_rate < 0.01
            and local_rate > 0.1
        ):
            flags.append(
                f"{official_vs_local['comparison_id']}:empty_completion_pathology"
            )
    return flags


@profile
def _group_quantiles(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute every reported quantile of ``abs_delta`` in a single pass.

    Previously called the pure-python ``_quantile`` helper six times, and
    each call did its own internal sort — so we sorted the same vector
    seven times. ``np.quantile`` does one sort and answers every q at
    once.
    """
    n = len(rows)
    if n == 0:
        return {
            'count': 0,
            'abs_delta': {
                'min': None, 'p50': None, 'p90': None,
                'p95': None, 'p99': None, 'max': None,
            },
        }
    arr = np.fromiter(
        (float(r['abs_delta']) for r in rows),
        dtype=np.float64,
        count=n,
    )
    # method='linear' matches the existing _quantile helper's
    # interpolation rule, so existing report numbers don't shift.
    qs = np.quantile(arr, [0.0, 0.5, 0.9, 0.95, 0.99, 1.0], method='linear')
    return {
        'count': n,
        'abs_delta': {
            'min': float(qs[0]),
            'p50': float(qs[1]),
            'p90': float(qs[2]),
            'p95': float(qs[3]),
            'p99': float(qs[4]),
            'max': float(qs[5]),
        },
    }


@profile
def _metric_quantiles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_metric.setdefault(str(row['metric']), []).append(row)
    out = []
    for metric, items in sorted(by_metric.items()):
        info = _group_quantiles(items)
        info['metric'] = metric
        out.append(info)
    return out


_BINARY_CORE_METRICS = {
    'exact_match',
    'prefix_exact_match',
    'quasi_exact_match',
    'quasi_prefix_exact_match',
    'exact_match@5',
    'prefix_exact_match@5',
    'quasi_exact_match@5',
    'quasi_prefix_exact_match@5',
}
_BOUNDED_OVERLAP_CORE_METRICS = {'bleu_1', 'bleu_4', 'f1_score', 'rouge_l'}


def _metric_descriptor(metric: str) -> dict[str, str]:
    if metric in _BINARY_CORE_METRICS:
        return {
            'kind': 'binary',
            'range': '0 to 1',
            'direction': 'higher is better',
        }
    if metric in _BOUNDED_OVERLAP_CORE_METRICS:
        return {
            'kind': 'bounded overlap score',
            'range': '0 to 1',
            'direction': 'higher is better',
        }
    return {
        'kind': 'score',
        'range': 'metric-dependent',
        'direction': 'higher is better unless documented otherwise',
    }


def _metric_domain(metric: str) -> MetricDomain | None:
    if metric in _BINARY_CORE_METRICS or metric in _BOUNDED_OVERLAP_CORE_METRICS:
        return (0.0, 1.0)
    return None


def _common_metric_domain(metrics: list[str] | set[str]) -> MetricDomain | None:
    if not metrics:
        return None
    domains = {_metric_domain(str(metric)) for metric in metrics}
    if None in domains or len(domains) != 1:
        return None
    return next(iter(domains))


def _pair_metric_domain(*pairs: dict[str, Any]) -> MetricDomain | None:
    metrics: set[str] = set()
    for pair in pairs:
        if not pair:
            continue
        pair_metrics = pair.get('core_metrics')
        if not pair_metrics:
            return None
        metrics.update(str(metric) for metric in pair_metrics)
    return _common_metric_domain(metrics)


def _apply_xlim_hint(ax, domain: MetricDomain | None, values) -> None:
    if domain is None:
        return
    observed = [float(value) for value in values if value is not None and pd.notna(value)]
    if not observed:
        return
    lower, upper = domain
    if min(observed) < lower or max(observed) > upper:
        return
    ax.set_xlim(lower, upper)


def _apply_abs_delta_ylim_hint(ax, domain: MetricDomain | None, values) -> None:
    if domain is None:
        return
    observed = [float(value) for value in values if value is not None and pd.notna(value)]
    if not observed or min(observed) < 0:
        return
    lower, upper = domain
    span = upper - lower
    if span <= 0 or max(observed) > span:
        return
    ax.set_ylim(0.0, span)


def _should_treat_as_discrete(values) -> bool:
    values = [float(v) for v in values if v is not None]
    unique_values = sorted(set(values))
    if not unique_values:
        return False
    return len(unique_values) <= 8 and all(v in {0.0, 1.0} for v in unique_values)


@profile
def _agreement_curve(rows: list[dict[str, Any]], thresholds: list[float]) -> list[dict[str, Any]]:
    """Count abs_delta-≤-threshold for each threshold via a single sort + searchsorted.

    Previously did ``sum(v <= t for v in vals)`` inside a per-threshold
    Python loop — O(N × K) Python comparisons per call. With ~7000 rows
    × 13 thresholds × ~1400 calls per heatmap run it accounted for ~10s
    of pure interpreter loop overhead.

    np.searchsorted on a sorted array does each threshold's count in
    O(log N) (binary search). For "≤ t" we use side='right': the
    rightmost insertion point equals the number of values ≤ t.
    """
    if not rows:
        return []
    n = len(rows)
    arr = np.fromiter(
        (float(r['abs_delta']) for r in rows),
        dtype=np.float64,
        count=n,
    )
    arr.sort()
    thresh_arr = np.asarray(thresholds, dtype=np.float64)
    counts = np.searchsorted(arr, thresh_arr, side='right')
    return [
        {
            'abs_tol': float(t),
            'agree_ratio': int(c) / n,
            'matched': int(c),
            'count': n,
        }
        for t, c in zip(thresh_arr, counts)
    ]


def _infer_run_spec_name(*run_paths: str) -> str:
    names = [Path(p).name for p in run_paths if p]
    names = [n for n in names if n]
    if not names:
        return 'unknown_run_spec'
    unique = sorted(set(names))
    if len(unique) == 1:
        return unique[0]
    return unique[0]


@profile
def _load_normalized(
    run_path: str | Path,
    source_kind: SourceKind = SourceKind.OFFICIAL,
    *,
    artifact_format: str = "helm",
    eee_artifact_path: str | Path | None = None,
    component_id: str | None = None,
    logical_run_key: str | None = None,
) -> NormalizedRun:
    """Load a run as a :class:`NormalizedRun` honoring the manifest format.

    When the planner has tagged a component as ``artifact_format='eee'`` and
    pointed ``eee_artifact_path`` at a converted EEE artifact directory, the
    EEE loader is used and the raw HELM run becomes evidence-only. Otherwise
    we fall back to the in-memory HELM->EEE conversion against ``run_path``.
    """
    if artifact_format == "eee" and eee_artifact_path:
        ref = NormalizedRunRef.from_eee_artifact(
            eee_artifact_path,
            source_kind=source_kind,
            helm_run_path=run_path,
            component_id=component_id,
            logical_run_key=logical_run_key,
        )
    else:
        ref = NormalizedRunRef.from_helm_run(
            run_path,
            source_kind=source_kind,
            component_id=component_id,
            logical_run_key=logical_run_key,
        )
    return load_run(ref)


def _component_source_kind(component: dict[str, Any] | None) -> SourceKind:
    raw = (component or {}).get("source_kind") or "official"
    try:
        return SourceKind(str(raw))
    except ValueError:
        return SourceKind.OFFICIAL


@profile
def _load_component_run(
    component: dict[str, Any],
    *,
    cache: dict[str, NormalizedRun] | None = None,
) -> NormalizedRun:
    """Load a component into a NormalizedRun, optionally memoizing.

    When the same component_id appears in multiple comparisons within
    a single packet (e.g. one official paired against N local replicas
    plus N-1 local_repeat comparisons share the local components),
    the loader was previously called once per pair — meaning the
    official artifact got loaded N times and each local artifact got
    loaded ~2x. Each load parses the EEE samples.jsonl from disk
    (105k records for new-format civil_comments, etc.), which is
    measurable wall-clock per call.

    Pass a ``cache`` dict to memoize across calls. The cache is keyed
    on ``component_id``; passing ``None`` preserves the original
    no-cache behavior (used by call sites that don't have a packet-
    scoped lifetime).
    """
    component_id = component.get("component_id")
    if cache is not None and component_id and component_id in cache:
        return cache[component_id]
    run = _load_normalized(
        component["run_path"],
        source_kind=_component_source_kind(component),
        artifact_format=str(component.get("artifact_format") or "helm"),
        eee_artifact_path=component.get("eee_artifact_path"),
        component_id=component_id,
        logical_run_key=component.get("logical_run_key"),
    )
    if cache is not None and component_id:
        cache[component_id] = run
    return run


@profile
def _build_pair(
    run_a: str,
    run_b: str,
    label: str,
    thresholds: list[float],
    *,
    component_a: dict[str, Any] | None = None,
    component_b: dict[str, Any] | None = None,
    component_cache: dict[str, NormalizedRun] | None = None,
    skip_diagnosis: bool = False,
) -> dict[str, Any]:
    # Stage-4 + Stage-5: the per-metric measurement core operates on the
    # EEE-normalized representation. When the planner has tagged a
    # component as artifact_format='eee', the EEE loader is used directly;
    # otherwise we fall back to in-memory HELM->EEE conversion. The legacy
    # HelmRunDiff is still used for the run-spec-semantic diagnosis (which
    # reads run_spec.json from the raw HELM JSONs cached on the run).
    #
    # ``skip_diagnosis=True`` (driven by --skip-diagnosis or
    # EVAL_AUDIT_SKIP_HELM_DIAGNOSIS=1) bypasses HelmRunDiff entirely. The
    # diagnosis labels (recipe_clean / deployment_drift / ...) need
    # run_spec.json which is a HELM artifact; for the EEE-only paper
    # validity claim the heatmap's NUMBERS must come from EEE alone, and
    # the diagnosis is auxiliary metadata, not load-bearing for the
    # core agreement-ratio comparisons. Skipping it also drops ~57s/packet
    # of wasted compute (summary_dict(level=20) computes far more than
    # the diagnosis we actually consume).
    if component_a is not None:
        nrun_a = _load_component_run(component_a, cache=component_cache)
    else:
        nrun_a = _load_normalized(run_a, source_kind=SourceKind.OFFICIAL)
    if component_b is not None:
        nrun_b = _load_component_run(component_b, cache=component_cache)
    else:
        nrun_b = _load_normalized(run_b, source_kind=SourceKind.LOCAL)
    if skip_diagnosis:
        diagnosis: dict[str, Any] = {}
    else:
        diff = HelmRunDiff(
            helm_view(nrun_a),
            helm_view(nrun_b),
            a_name=f'{label}:A',
            b_name=f'{label}:B',
        )
        diagnosis = diff.summary_dict(level=20).get('diagnosis', {})
    run_rows = ncompare.run_level_core_rows(nrun_a, nrun_b)
    inst_rows = ncompare.instance_level_core_rows(nrun_a, nrun_b)

    # Calculate per-metric agreement curves for instance level
    per_metric_curves = {}
    if inst_rows:
        by_metric = {}
        for row in inst_rows:
            metric = str(row.get('metric', 'unknown'))
            if metric not in by_metric:
                by_metric[metric] = []
            by_metric[metric].append(row)
        for metric, metric_rows in by_metric.items():
            per_metric_curves[metric] = _agreement_curve(metric_rows, thresholds)

    return {
        'label': label,
        'inputs': {
            'run_a': str(Path(run_a).expanduser().resolve()),
            'run_b': str(Path(run_b).expanduser().resolve()),
        },
        'diagnosis': diagnosis,
        'core_metrics': sorted({str(r['metric']) for r in inst_rows}),
        'run_level': {
            'n_rows': len(run_rows),
            'overall_quantiles': _group_quantiles(run_rows),
            'by_metric': _metric_quantiles(run_rows),
            'agreement_vs_abs_tol': _agreement_curve(run_rows, thresholds),
        },
        'instance_level': {
            'n_rows': len(inst_rows),
            'overall_quantiles': _group_quantiles(inst_rows),
            'by_metric': _metric_quantiles(inst_rows),
            'agreement_vs_abs_tol': _agreement_curve(inst_rows, thresholds),
            'per_metric_agreement': per_metric_curves,
        },
        '_instance_rows': inst_rows,
    }


@profile
def _agreement_curve_rows(*pairs: dict[str, Any], level_key: str) -> list[dict[str, Any]]:
    rows = []
    for pair in pairs:
        if not pair:
            continue
        for row in pair[level_key]['agreement_vs_abs_tol']:
            rows.append({
                'pair': pair['label'],
                'abs_tol': float(row['abs_tol']),
                'agree_ratio': float(row['agree_ratio']),
            })
    return rows


@profile
def _plot_distribution(
    ax,
    *pairs: dict[str, Any],
    level_key: str,
    alias_map: dict[str, str] | None = None,
) -> None:
    rows = pd.DataFrame(_agreement_curve_rows(*pairs, level_key=level_key))
    if rows.empty or 'abs_tol' not in rows.columns or 'agree_ratio' not in rows.columns:
        ax.text(0.5, 0.5, 'No comparable core-metric rows', ha='center', va='center', transform=ax.transAxes)
        ax.set_axis_off()
        return
    if alias_map:
        rows = rows.assign(pair=rows['pair'].map(alias_map).fillna(rows['pair']))
    sns.lineplot(
        ax=ax,
        data=rows,
        x='abs_tol',
        y='agree_ratio',
        hue='pair',
        style='pair',
        markers=True,
        dashes=False,
        linewidth=2,
    )
    ax.set_xscale('symlog', linthresh=1e-12)
    ax.set_ylim(0, 1.02)
    _apply_xlim_hint(ax, _pair_metric_domain(*pairs), rows['abs_tol'].tolist())
    ax.set_xlabel('Absolute Tolerance Threshold for Core Metric Difference')
    ax.set_ylabel('Fraction of Core Metric Comparisons in Agreement')
    ax.tick_params(axis='x', rotation=28)
    ax.legend(title='')


@profile
def _per_metric_agreement_curves(*pairs: dict[str, Any], level_key: str, thresholds: list[float]) -> dict[str, list[dict[str, Any]]]:
    """Calculate per-metric agreement curves from pair instance rows."""
    curves = {}
    for pair in pairs:
        if not pair:
            continue
        instance_rows = pair.get('_instance_rows', [])
        if level_key == 'instance_level':
            rows = instance_rows
        else:
            continue

        by_metric = {}
        for row in rows:
            metric = str(row.get('metric', 'unknown'))
            if metric not in by_metric:
                by_metric[metric] = []
            by_metric[metric].append(row)

        for metric, metric_rows in by_metric.items():
            if metric not in curves:
                curves[metric] = []
            agreement = _agreement_curve(metric_rows, thresholds)
            for agreement_row in agreement:
                curves[metric].append({
                    'pair': pair['label'],
                    'metric': metric,
                    'abs_tol': float(agreement_row['abs_tol']),
                    'agree_ratio': float(agreement_row['agree_ratio']),
                })
    return curves


@profile
def _plot_per_metric_agreement(
    fig_dpath: Path,
    stamp: str,
    *pairs: dict[str, Any],
    level_key: str = 'instance_level',
    thresholds: list[float] | None = None,
    plot_layout: PlotLayout | None = None,
) -> Path | None:
    """Create per-metric agreement curve plots."""
    if thresholds is None:
        thresholds = [1e-12, 1e-9, 1e-6, 1e-3, 1e-2, 0.1, 0.25, 0.5, 1.0]

    curves = _per_metric_agreement_curves(*pairs, level_key=level_key, thresholds=thresholds)
    if not curves:
        return None

    metrics = sorted(curves.keys())
    n_cols = min(3, len(metrics))
    n_rows = (len(metrics) + n_cols - 1) // n_cols

    # Pair labels (legend hue) are full comparison ids, ~100+ chars; the
    # legend overflows the axes when the labels are long. Alias each pair
    # to a short slug for the legend; sidecar artifacts emitted below
    # preserve the long labels.
    pair_labels = sorted({row['pair'] for metric_rows in curves.values() for row in metric_rows})
    alias_map = short_alias_map(pair_labels)

    _apply_matplotlib_style()
    fig, axes = plt.subplots(
        n_rows,
        n_cols,
        figsize=_scaled_figsize(6.5 * n_cols, 4.8 * n_rows, plot_layout),
        constrained_layout=False,
    )
    if len(metrics) == 1:
        axes = [[axes]]
    elif n_rows == 1:
        axes = [axes]
    elif n_cols == 1:
        axes = [[ax] for ax in axes]

    for idx, metric in enumerate(metrics):
        row_idx = idx // n_cols
        col_idx = idx % n_cols
        ax = axes[row_idx][col_idx]

        metric_data = curves[metric]
        df = pd.DataFrame(metric_data)
        if not df.empty:
            df = df.assign(pair=df['pair'].map(alias_map).fillna(df['pair']))
            sns.lineplot(
                ax=ax,
                data=df,
                x='abs_tol',
                y='agree_ratio',
                hue='pair',
                style='pair',
                markers=True,
                dashes=False,
                linewidth=2,
            )
            ax.set_xscale('symlog', linthresh=1e-12)
            ax.set_ylim(0, 1.02)
            _apply_xlim_hint(ax, _metric_domain(metric), df['abs_tol'].tolist())
            ax.set_xlabel('Abs Tolerance', fontsize=9)
            ax.set_ylabel('Agreement Ratio', fontsize=9)
            ax.tick_params(axis='x', rotation=28, labelsize=8)
            ax.tick_params(axis='y', labelsize=8)
            ax.set_title(metric, fontsize=10)
            ax.legend(title='', fontsize=8)

    for idx in range(len(metrics), n_rows * n_cols):
        row_idx = idx // n_cols
        col_idx = idx % n_cols
        fig.delaxes(axes[row_idx][col_idx])

    layout = plot_layout or PlotLayout()
    fig.suptitle(
        'Per-Metric Agreement vs Absolute Tolerance\n'
        'Legend uses short pair aliases; see the sidecar legend artifact for the full labels.',
        fontsize=14,
        y=layout.suptitle_y if layout.suptitle_y is not None else 0.995,
    )
    # Multi-row grid: each row has its own xlabel + tick labels, which were
    # overlapping the row-below title at the layout default hspace=0.05.
    # Bump hspace / wspace to give every facet breathing room, and a touch
    # of bottom margin so the bottom row's xlabel doesn't clip.
    adjust_kwargs = _subplot_adjust_kwargs(fig, layout, top=0.92, bottom=0.07)
    adjust_kwargs['hspace'] = max(adjust_kwargs.get('hspace', 0.40), 0.40)
    adjust_kwargs['wspace'] = max(adjust_kwargs.get('wspace', 0.18), 0.18)
    fig.subplots_adjust(**adjust_kwargs)
    fig_fpath = fig_dpath / f'core_metric_per_metric_agreement.png'
    _atomic_savefig(fig, fig_fpath, dpi=180)
    plt.close(fig)
    emit_label_legend_artifacts(
        alias_map,
        fig_dpath=fig_dpath,
        out_name='core_metric_per_metric_agreement',
        title='Per-Metric Agreement — short alias → full pair label',
        stamp=stamp,
        color_map=_palette_color_map(pair_labels),
    )
    return fig_fpath


@profile
def _plot_quantiles(ax, pair_a: dict[str, Any], pair_b: dict[str, Any], level_key: str, title: str) -> None:
    labels = ['p50', 'p90', 'p95', 'p99', 'max']
    x = list(range(len(labels)))
    a_vals = [pair_a[level_key]['overall_quantiles']['abs_delta'][k] for k in labels]
    b_vals = [pair_b[level_key]['overall_quantiles']['abs_delta'][k] for k in labels]
    ax.plot(x, a_vals, marker='o', label=pair_a['label'])
    ax.plot(x, b_vals, marker='o', label=pair_b['label'])
    ax.set_xticks(x, labels)
    ax.set_yscale('symlog', linthresh=1e-12)
    _apply_abs_delta_ylim_hint(ax, _pair_metric_domain(pair_a, pair_b), a_vals + b_vals)
    ax.set_title(title)
    ax.set_xlabel('Quantile')
    ax.set_ylabel('Absolute Difference in Core Metric Value')
    ax.legend(title='')


@profile
def _distribution_rows(pair: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for row in pair.get('_instance_rows', []):
        rows.append({
            'pair': pair['label'],
            'metric': row['metric'],
            'side': 'A',
            'value': float(row['a']),
        })
        rows.append({
            'pair': pair['label'],
            'metric': row['metric'],
            'side': 'B',
            'value': float(row['b']),
        })
    return pd.DataFrame(rows)


@profile
def _plot_metric_distributions(fig_dpath: Path, stamp: str, left: dict[str, Any], right: dict[str, Any], run_spec_name: str) -> Path | None:
    return _plot_pair_metric_distributions(fig_dpath, stamp, [left, right], run_spec_name)


@profile
def _plot_pair_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    pairs: list[dict[str, Any]],
    run_spec_name: str,
    *,
    plot_layout: PlotLayout | None = None,
) -> Path | None:
    pairs = [pair for pair in pairs if pair]
    if not pairs:
        return None
    df = pd.concat([
        _distribution_rows(pair)
        for pair in pairs
    ], ignore_index=True)
    if df.empty or 'metric' not in df.columns:
        return None
    metrics = sorted(df['metric'].dropna().unique().tolist())
    if not metrics:
        return None
    pair_order = [pair['label'] for pair in pairs]
    # Pair labels are full comparison ids that splice the official component,
    # the local component, attempt UUIDs, etc., and are routinely 100+ chars
    # long — they crush the per-axis title. Alias each to a short slug for
    # the title; emit the full mapping as a sidecar legend artifact below.
    alias_map = short_alias_map(pair_order)
    layout = plot_layout or PlotLayout()
    # Pad the per-row height so 1-row × N-col grids leave room for a
    # multi-line suptitle without colliding with the axis-level titles.
    row_height = 4.2 + (1.6 if len(pair_order) == 1 else 0.0)
    _apply_matplotlib_style()
    fig, axes = plt.subplots(
        len(pair_order),
        len(metrics),
        figsize=_scaled_figsize(5.2 * len(metrics), row_height * len(pair_order), plot_layout),
        constrained_layout=False,
    )
    if len(pair_order) == 1 and len(metrics) == 1:
        axes = [[axes]]
    elif len(pair_order) == 1:
        axes = [axes]
    elif len(metrics) == 1:
        axes = [[ax] for ax in axes]
    for row_idx, pair_label in enumerate(pair_order):
        for col_idx, metric in enumerate(metrics):
            ax = axes[row_idx][col_idx]
            sub = df[(df['pair'] == pair_label) & (df['metric'] == metric)]
            discrete = _should_treat_as_discrete(sub['value'].tolist())
            sns.histplot(
                data=sub,
                x='value',
                hue='side',
                stat='probability',
                common_norm=False,
                discrete=discrete,
                multiple='dodge',
                shrink=0.8,
                bins=None if discrete else 20,
                ax=ax,
            )
            ax.set_title(f'{alias_map[pair_label]}  {metric}', fontsize=10)
            ax.set_xlabel('Core metric value')
            ax.set_ylabel('Probability')
            legend = ax.get_legend()
            if legend is not None:
                legend.set_title('')
    fig.suptitle(
        'Core Metric Score Distributions Within Each Comparison Pair — '
        f'{run_spec_name}  (per-axis titles: <pair-alias>  <metric>; '
        'see sidecar legend for full pair labels)',
        fontsize=12,
        y=layout.suptitle_y if layout.suptitle_y is not None else 0.995,
    )
    adjust_kwargs = _subplot_adjust_kwargs(fig, layout, top=0.86, bottom=0.13)
    # Multi-column grid: each column has its own y-axis label which crowds
    # the plot to its left at the layout default wspace=0.05. Bump wspace
    # so y-axis labels and tick labels have breathing room.
    adjust_kwargs['wspace'] = max(adjust_kwargs.get('wspace', 0.30), 0.30)
    fig.subplots_adjust(**adjust_kwargs)
    out_fpath = fig_dpath / f'core_metric_distributions.png'
    _atomic_savefig(fig, out_fpath, dpi=180)
    plt.close(fig)
    emit_label_legend_artifacts(
        alias_map,
        fig_dpath=fig_dpath,
        out_name='core_metric_distributions',
        title='Core Metric Distributions — short alias → full pair label',
        stamp=stamp,
    )
    return out_fpath


# short_alias_map / emit_label_legend_artifacts live in
# eval_audit.utils.labels so the same hash-and-sidecar pattern stays
# consistent everywhere a long identifier would crush a plot legend or
# axis title.


@profile
def _plot_run_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    run_specs: list[tuple[str, str] | tuple[str, str, dict[str, Any] | None]],
    run_spec_name: str,
    *,
    out_name: str = 'core_metric_overlay_distributions',
    title: str = 'Overlay of Per-Instance Core Metric Score Distributions by Run',
    subtitle: str = 'This shows the raw score distributions for each core metric across the selected runs.',
    ecdf: bool = False,
    plot_layout: PlotLayout | None = None,
) -> dict[str, Path] | None:
    normalized_run_specs = _normalize_plot_run_specs(run_specs)
    frames = [
        _single_run_instance_core_rows(
            run_path,
            label,
            component=component,
        )
        for run_path, label, component in normalized_run_specs
    ]
    df = pd.concat(frames, ignore_index=True)
    if df.empty or 'metric' not in df.columns:
        return None
    metrics = sorted(df['metric'].dropna().unique().tolist())
    if not metrics:
        return None
    # Alias every legend label to a short, unique slug. The full labels
    # (component display_names) routinely run 80–120 chars and crush the plot
    # legend; the sidecar legend artifacts emitted below preserve the long
    # labels so readers can resolve the aliases.
    long_labels = sorted({label for _, label, _ in normalized_run_specs})
    alias_map = short_alias_map(long_labels)
    df = df.assign(run=df['run'].map(alias_map).fillna(df['run']))
    _apply_matplotlib_style()
    layout = plot_layout or PlotLayout()
    # Reserve a fixed inch allocation at the top of the figure for the
    # 4-line fontsize-15 suptitle; computed in inches and converted to a
    # figure-fraction below so the suptitle never crashes the first axis
    # title regardless of how many metric rows we plot.
    suptitle_band_in = 1.6
    fig_h_in = 3.2 * len(metrics) + suptitle_band_in
    fig, axes = plt.subplots(
        len(metrics),
        1,
        figsize=_scaled_figsize(10, fig_h_in, plot_layout),
        constrained_layout=False,
    )
    if len(metrics) == 1:
        axes = [axes]
    for ax, metric in zip(axes, metrics):
        sub = df[df['metric'] == metric].copy()
        if ecdf:
            sns.ecdfplot(
                data=sub,
                x='value',
                hue='run',
                ax=ax,
            )
            desc = _metric_descriptor(metric)
            ax.set_title(
                f"{metric} ECDF ({desc['kind']}, {desc['range']}, {desc['direction']})"
            )
            ax.set_ylabel('Cumulative fraction of instances')
        else:
            discrete = _should_treat_as_discrete(sub['value'].tolist())
            sns.histplot(
                data=sub,
                x='value',
                hue='run',
                stat='probability',
                common_norm=False,
                element='step',
                fill=False,
                multiple='layer',
                discrete=discrete,
                bins=None if discrete else 20,
                ax=ax,
            )
            desc = _metric_descriptor(metric)
            ax.set_title(
                f"{metric} ({desc['kind']}, {desc['range']}, {desc['direction']})"
            )
            ax.set_ylabel('Probability')
        ax.set_xlabel('Instance-level metric value')
        legend = ax.get_legend()
        if legend is not None:
            legend.set_title('')
    _set_suptitle(
        fig,
        f'{title}\n'
        f'Run Spec: {run_spec_name}\n'
        f'{subtitle}\n'
        f'Legend uses short aliases; see the sidecar legend artifact for the full labels.',
        fontsize=15,
        plot_layout=plot_layout,
    )
    # Pin top= so the reserved suptitle band is honored regardless of how
    # many metric rows are stacked below it. Bump hspace so the row-below
    # title doesn't crowd the row-above tick labels.
    actual_fig_h = fig.get_size_inches()[1]
    top_fraction = max(0.5, 1.0 - (suptitle_band_in / actual_fig_h))
    adjust_kwargs = _subplot_adjust_kwargs(fig, layout, top=top_fraction, bottom=0.05)
    adjust_kwargs['hspace'] = max(adjust_kwargs.get('hspace', 0.35), 0.35)
    fig.subplots_adjust(**adjust_kwargs)
    out_fpath = fig_dpath / f'{out_name}.png'
    _atomic_savefig(fig, out_fpath, dpi=180)
    plt.close(fig)
    legend_png_fpath, legend_txt_fpath = emit_label_legend_artifacts(
        alias_map,
        fig_dpath=fig_dpath,
        out_name=out_name,
        title=f"{title} — short alias → full label",
        stamp=stamp,
        color_map=_palette_color_map(long_labels),
    )
    artifacts: dict[str, Path] = {'plot': out_fpath}
    if legend_png_fpath is not None:
        artifacts['legend_png'] = legend_png_fpath
    if legend_txt_fpath is not None:
        artifacts['legend_txt'] = legend_txt_fpath
    return artifacts


def _single_run_instance_core_rows(
    run_path: str,
    label: str,
    *,
    component: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Per-(sample, core-metric) score rows for a single run.

    Stage-4: reads from the normalized layer's :class:`InstanceRecord`
    instead of HELM's joined per-instance stats table.
    """
    nrun = _load_component_run(component) if component is not None else _load_normalized(run_path)
    rows = [
        {"run": label, **rec}
        for rec in ncompare.instance_core_score_records(nrun)
    ]
    return pd.DataFrame(rows)


def _normalize_plot_run_specs(
    run_specs: list[tuple[str, str] | tuple[str, str, dict[str, Any] | None]],
) -> list[tuple[str, str, dict[str, Any] | None]]:
    normalized = []
    for item in run_specs:
        if len(item) == 2:
            run_path, label = item
            component = None
        elif len(item) == 3:
            run_path, label, component = item
        else:
            raise ValueError(f'Expected 2- or 3-tuples in run_specs, got {item!r}')
        normalized.append((run_path, label, component))
    return normalized


@profile
def _plot_three_run_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    kwdagger_a_run: str,
    kwdagger_b_run: str,
    official_run: str,
    run_spec_name: str,
    *,
    plot_layout: PlotLayout | None = None,
) -> Path | None:
    df = pd.concat([
        _single_run_instance_core_rows(kwdagger_a_run, 'kwdagger A'),
        _single_run_instance_core_rows(kwdagger_b_run, 'kwdagger B'),
        _single_run_instance_core_rows(official_run, 'official'),
    ], ignore_index=True)
    if df.empty or 'metric' not in df.columns:
        return None
    metrics = sorted(df['metric'].dropna().unique().tolist())
    if not metrics:
        return None
    run_order = ['kwdagger A', 'kwdagger B', 'official']
    _apply_matplotlib_style()
    fig, axes = plt.subplots(
        len(metrics),
        len(run_order),
        figsize=_scaled_figsize(5.0 * len(run_order), 3.2 * len(metrics), plot_layout),
        constrained_layout=True,
    )
    if len(metrics) == 1 and len(run_order) == 1:
        axes = [[axes]]
    elif len(metrics) == 1:
        axes = [axes]
    elif len(run_order) == 1:
        axes = [[ax] for ax in axes]
    for row_idx, metric in enumerate(metrics):
        for col_idx, run_label in enumerate(run_order):
            ax = axes[row_idx][col_idx]
            sub = df[(df['metric'] == metric) & (df['run'] == run_label)]
            discrete = _should_treat_as_discrete(sub['value'].tolist())
            sns.histplot(
                data=sub,
                x='value',
                stat='probability',
                discrete=discrete,
                shrink=0.8,
                bins=None if discrete else 20,
                ax=ax,
                color='#4C72B0',
            )
            if row_idx == 0:
                ax.set_title(run_label)
            ax.set_xlabel('Core metric value')
            ax.set_ylabel(metric if col_idx == 0 else '')
    _set_suptitle(
        fig,
        'Per-Run Instance-Level Core Metric Score Distributions\n'
        f'Run Spec: {run_spec_name}\n'
        'Columns are kwdagger repeat A, kwdagger repeat B, and the official HELM run.',
        fontsize=16,
        plot_layout=plot_layout,
    )
    out_fpath = fig_dpath / f'core_metric_three_run_distributions.png'
    _atomic_savefig(fig, out_fpath, dpi=180)
    plt.close(fig)
    return out_fpath


@profile
def _plot_overlay_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    kwdagger_a_run: str,
    kwdagger_b_run: str,
    official_run: str,
    run_spec_name: str,
    *,
    plot_layout: PlotLayout | None = None,
) -> dict[str, Path] | None:
    return _plot_run_metric_distributions(
        fig_dpath,
        stamp,
        [
            (kwdagger_a_run, 'kwdagger A'),
            (kwdagger_b_run, 'kwdagger B'),
            (official_run, 'official'),
        ],
        run_spec_name,
        out_name='core_metric_overlay_distributions',
        title='Overlay of Per-Instance Core Metric Score Distributions by Run',
        subtitle='This shows the raw score distributions for each core metric across kwdagger repeats and the official HELM run.',
        plot_layout=plot_layout,
    )


@profile
def _plot_overlay_metric_ecdfs(
    fig_dpath: Path,
    stamp: str,
    kwdagger_a_run: str,
    kwdagger_b_run: str,
    official_run: str,
    run_spec_name: str,
    *,
    plot_layout: PlotLayout | None = None,
) -> dict[str, Path] | None:
    return _plot_run_metric_distributions(
        fig_dpath,
        stamp,
        [
            (kwdagger_a_run, 'kwdagger A'),
            (kwdagger_b_run, 'kwdagger B'),
            (official_run, 'official'),
        ],
        run_spec_name,
        out_name='core_metric_ecdfs',
        title='ECDF of Per-Instance Core Metric Scores by Run',
        subtitle='This often communicates sparse or zero-heavy metric distributions more clearly than histograms.',
        ecdf=True,
        plot_layout=plot_layout,
    )


class _SimpleStatRow:
    """Minimal row used by run-level table writers.

    Replaces the ``StatMeta`` records the legacy
    :class:`HelmRunAnalysis.stat_index` produced. Only the fields actually
    consumed by the table writers (``metric`` and ``mean``) are exposed.
    """

    __slots__ = ("metric", "mean")

    def __init__(self, metric: str, mean: float) -> None:
        self.metric = metric
        self.mean = mean


def _single_run_core_stat_index(
    run_path: str,
    *,
    component: dict[str, Any] | None = None,
    component_cache: dict[str, NormalizedRun] | None = None,
) -> dict[str, _SimpleStatRow]:
    """Run-level core metric means keyed by stable metric handle.

    Stage-4: backed by ``ncompare.joined_metric_means`` over a normalized
    run instead of ``HelmRunAnalysis.stat_index``.

    ``component_cache`` is the per-packet NormalizedRun memo populated by
    ``_build_pair``. Threading it here avoids re-loading every official +
    local artifact from disk a second time when the runlevel-table
    writer asks for the per-run core stats — those artifacts were
    already parsed for the agreement-curve computation.
    """
    nrun = (
        _load_component_run(component, cache=component_cache)
        if component is not None
        else _load_normalized(run_path)
    )
    out: dict[str, _SimpleStatRow] = {}
    for key in ncompare.core_metric_keys(nrun):
        means = {
            (er.metric_config.metric_id or er.metric_config.metric_name or er.evaluation_name): er.score_details.score
            for er in nrun.evaluation_log.evaluation_results or []
        }
        if key in means:
            out[key] = _SimpleStatRow(metric=key, mean=float(means[key]))
    return out


@profile
def _write_three_run_runlevel_table(
    out_dpath: Path,
    stamp: str,
    kwdagger_a_run: str,
    kwdagger_b_run: str,
    official_run: str,
) -> tuple[Path, Path | None]:
    idx_a = _single_run_core_stat_index(kwdagger_a_run)
    idx_b = _single_run_core_stat_index(kwdagger_b_run)
    idx_o = _single_run_core_stat_index(official_run)
    keys = sorted(set(idx_a) & set(idx_b) & set(idx_o))
    rows = []
    for key in keys:
        a = idx_a[key]
        b = idx_b[key]
        o = idx_o[key]
        rows.append({
            'stat_key': key,
            'metric': a.metric,
            'kwdagger_a': a.mean,
            'kwdagger_b': b.mean,
            'official': o.mean,
            'delta_official_vs_kwdagger_a': None if a.mean is None or o.mean is None else abs(o.mean - a.mean),
            'delta_kwdagger_a_vs_kwdagger_b': None if a.mean is None or b.mean is None else abs(a.mean - b.mean),
        })
    table = pd.DataFrame(rows)
    csv_fpath = out_dpath / f'core_runlevel_table.csv'
    md_fpath = out_dpath / f'core_runlevel_table.md'
    table.to_csv(csv_fpath, index=False)
    try:
        write_text_atomic(md_fpath, table.to_markdown(index=False) + '\n')
    except ImportError:
        md_fpath = None
    return csv_fpath, md_fpath


@profile
def _write_two_run_runlevel_table(
    out_dpath: Path,
    stamp: str,
    kwdagger_run: str,
    official_run: str,
) -> tuple[Path, Path | None]:
    idx_kw = _single_run_core_stat_index(kwdagger_run)
    idx_off = _single_run_core_stat_index(official_run)
    keys = sorted(set(idx_kw) & set(idx_off))
    rows = []
    for key in keys:
        kw = idx_kw[key]
        off = idx_off[key]
        rows.append({
            'stat_key': key,
            'metric': kw.metric,
            'kwdagger': kw.mean,
            'official': off.mean,
            'delta_official_vs_kwdagger': None if kw.mean is None or off.mean is None else abs(off.mean - kw.mean),
        })
    table = pd.DataFrame(rows)
    csv_fpath = out_dpath / f'core_runlevel_table.csv'
    md_fpath = out_dpath / f'core_runlevel_table.md'
    table.to_csv(csv_fpath, index=False)
    try:
        write_text_atomic(md_fpath, table.to_markdown(index=False) + '\n')
    except ImportError:
        md_fpath = None
    return csv_fpath, md_fpath


@profile
def _plot_single_pair_summary(
    fig_dpath: Path,
    stamp: str,
    pair: dict[str, Any],
    run_spec_name: str,
    *,
    plot_layout: PlotLayout | None = None,
) -> Path:
    _apply_matplotlib_style()
    layout = plot_layout or PlotLayout()
    # The full pair label (a spliced comparison id; ~150-200 chars on real
    # packets) crushes the suptitle and the right-pane legend. Alias it for
    # display; emit the alias->full mapping as a sidecar.
    alias_map = short_alias_map([pair['label']])
    pair_alias = alias_map[pair['label']]
    fig, axes = plt.subplots(
        1,
        2,
        figsize=_scaled_figsize(18, 7.5, plot_layout),
        constrained_layout=False,
    )
    quantiles = pair['instance_level']['overall_quantiles']['abs_delta']
    labels = ['p50', 'p90', 'p95', 'p99', 'max']
    abs_delta_values = [quantiles[k] for k in labels]
    axes[0].plot(range(len(labels)), abs_delta_values, marker='o', color='#4C72B0')
    axes[0].set_xticks(range(len(labels)), labels)
    axes[0].set_yscale('symlog', linthresh=1e-12)
    _apply_abs_delta_ylim_hint(axes[0], _pair_metric_domain(pair), abs_delta_values)
    axes[0].set_title('Official vs Local Instance-Level Delta Quantiles')
    axes[0].set_xlabel('Quantile')
    axes[0].set_ylabel('Absolute Difference in Core Metric Value')
    _plot_distribution(axes[1], pair, level_key='instance_level', alias_map=alias_map)
    axes[1].set_title('Official vs Local Agreement vs Tolerance')
    _set_suptitle(
        fig,
        'Core Metric Agreement and Difference Summary\n'
        f'Run Spec: {run_spec_name}\n'
        f'Pair: {pair_alias}  (full label in sidecar legend artifact)\n'
        f'Instance-level N: {pair["instance_level"]["n_rows"]}',
        fontsize=15,
        plot_layout=plot_layout,
    )
    # Roomier left/right margins so y-axis labels and the right-edge ticks
    # don't clip; wider wspace so the two panes don't crowd each other.
    adjust_kwargs = _subplot_adjust_kwargs(fig, layout, top=0.78, bottom=0.10)
    adjust_kwargs.setdefault('left', 0.06)
    adjust_kwargs['left'] = max(adjust_kwargs.get('left', 0.06), 0.06)
    adjust_kwargs['right'] = min(adjust_kwargs.get('right', 0.97), 0.97)
    adjust_kwargs['wspace'] = max(adjust_kwargs.get('wspace', 0.25), 0.25)
    fig.subplots_adjust(**adjust_kwargs)
    fig_fpath = fig_dpath / f'core_metric_report.png'
    _atomic_savefig(fig, fig_fpath, dpi=180)
    plt.close(fig)
    emit_label_legend_artifacts(
        alias_map,
        fig_dpath=fig_dpath,
        out_name='core_metric_report',
        title='Core Metric Report — short alias → full pair label',
        stamp=stamp,
        color_map=_palette_color_map([pair['label']]),
    )
    return fig_fpath


def _strip_private(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_private(v)
            for k, v in obj.items()
            if not str(k).startswith('_')
        }
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


def _find_pair(report: dict[str, Any], comparison_kind: str) -> dict[str, Any] | None:
    return next(
        (pair for pair in report.get('pairs', []) if pair.get('comparison_kind') == comparison_kind),
        None,
    )


def _load_run_spec_json(component: dict[str, Any]) -> dict[str, Any] | None:
    """Read raw HELM run_spec.json off the component's run_path.

    Returns ``None`` for pure-EEE components (no HELM run_path on disk),
    for components whose run_path is missing, and for unparseable files."""
    run_path = component.get('run_path')
    if not run_path:
        return None
    run_spec_fpath = Path(run_path) / 'run_spec.json'
    if not run_spec_fpath.exists():
        return None
    try:
        data = json.loads(run_spec_fpath.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _component_spec_metadata(component: dict[str, Any]) -> dict[str, Any]:
    run_spec = _load_run_spec_json(component) or {}
    run_path = component.get('run_path')
    fields = extract_run_spec_fields(Path(run_path) / 'run_spec.json' if run_path else None)
    adapter = run_spec.get('adapter_spec') or {}
    return {
        'base_model': fields.get('model'),
        'scenario_class': fields.get('scenario_class'),
        'deployment': fields.get('model_deployment'),
        'adapter_instructions': (
            adapter.get('instructions')
            if isinstance(adapter, dict) else None
        ),
        'max_eval_instances': (
            adapter.get('max_eval_instances')
            if isinstance(adapter, dict) and adapter.get('max_eval_instances') is not None
            else component.get('max_eval_instances')
        ),
    }


def _same_value_fact(values: list[Any]) -> dict[str, Any]:
    present = [value for value in values if value not in {None, ''}]
    unique = []
    for value in present:
        if value not in unique:
            unique.append(value)
    if not present:
        return {'status': 'unknown', 'values': []}
    if len(unique) == 1:
        return {'status': 'yes', 'values': unique}
    return {'status': 'no', 'values': unique}


@profile
def _comparability_summary(components: list[dict[str, Any]]) -> dict[str, Any]:
    metadata_by_component = {
        component['component_id']: _component_spec_metadata(component)
        for component in components
    }
    facts = {
        'same_base_model': _same_value_fact([meta.get('base_model') for meta in metadata_by_component.values()]),
        'same_scenario_class': _same_value_fact([meta.get('scenario_class') for meta in metadata_by_component.values()]),
        'same_deployment': _same_value_fact([meta.get('deployment') for meta in metadata_by_component.values()]),
        'same_adapter_instructions': _same_value_fact([meta.get('adapter_instructions') for meta in metadata_by_component.values()]),
        'same_max_eval_instances': _same_value_fact([meta.get('max_eval_instances') for meta in metadata_by_component.values()]),
    }
    return {
        'component_metadata': metadata_by_component,
        'facts': facts,
    }


@profile
def _warnings_payload(report: dict[str, Any]) -> dict[str, Any]:
    comparisons = report.get("comparisons") or []
    return {
        "report_dpath": report.get("report_dpath"),
        "packet_id": report.get("packet_id"),
        "run_entry": report.get("run_entry"),
        "planner_version": report.get("planner_version"),
        "packet_warnings": report.get("packet_warnings") or [],
        "packet_caveats": report.get("packet_caveats") or [],
        "official_selection": report.get("official_selection") or {},
        "diagnostic_flags": report.get("diagnostic_flags") or [],
        "comparisons": [
            {
                "comparison_id": comparison.get("comparison_id"),
                "comparison_kind": comparison.get("comparison_kind"),
                "enabled": comparison.get("enabled"),
                "disabled_reason": comparison.get("disabled_reason"),
                "warnings": comparison.get("warnings") or [],
                "caveats": comparison.get("caveats") or [],
                "comparability_facts": comparison.get("comparability_facts") or {},
            }
            for comparison in comparisons
        ],
    }


def _warning_summary_lines(report: dict[str, Any]) -> list[str]:
    warnings_payload = _warnings_payload(report)
    lines = [
        "Core Metric Report Warnings",
        "",
        f"report_dpath: {report.get('report_dpath')}",
        f"packet_id: {report.get('packet_id')}",
        f"run_entry: {report.get('run_entry')}",
        f"planner_version: {report.get('planner_version')}",
        f"diagnostic_flags: {report.get('diagnostic_flags') or []}",
        "",
    ]
    packet_warnings = warnings_payload.get("packet_warnings") or []
    packet_caveats = warnings_payload.get("packet_caveats") or []
    if packet_warnings:
        lines.append("packet_warnings:")
        for item in packet_warnings:
            lines.append(f"  - {item}")
    if packet_caveats:
        lines.append("packet_caveats:")
        for item in packet_caveats:
            lines.append(f"  - {item}")
    official_selection = warnings_payload.get("official_selection") or {}
    if official_selection:
        lines.append("official_selection:")
        lines.append(f"  policy_name: {official_selection.get('policy_name')}")
        lines.append(f"  selected_public_track: {official_selection.get('selected_public_track')}")
        lines.append(f"  retained_component_ids: {official_selection.get('retained_component_ids')}")
        lines.append(f"  discarded_component_ids: {official_selection.get('discarded_component_ids')}")
        if official_selection.get("warnings"):
            lines.append(f"  warnings: {official_selection.get('warnings')}")
    lines.append("comparisons:")
    for comparison in warnings_payload.get("comparisons") or []:
        lines.append(
            f"  - {comparison.get('comparison_id')} enabled={comparison.get('enabled')} "
            f"disabled_reason={comparison.get('disabled_reason')}"
        )
        if comparison.get("warnings"):
            lines.append(f"    warnings: {comparison.get('warnings')}")
        if comparison.get("caveats"):
            lines.append(f"    caveats: {comparison.get('caveats')}")
    return lines


@profile
def _write_comparison_runlevel_table(
    out_dpath: Path,
    stamp: str,
    comparisons: list[dict[str, Any]],
    component_lookup: dict[str, dict[str, Any]],
    *,
    component_cache: dict[str, NormalizedRun] | None = None,
) -> tuple[Path, Path | None]:
    rows = []
    for comparison in comparisons:
        component_ids = comparison.get('component_ids') or []
        if len(component_ids) != 2:
            continue
        left_component = component_lookup[component_ids[0]]
        right_component = component_lookup[component_ids[1]]
        idx_left = _single_run_core_stat_index(
            left_component['run_path'],
            component=left_component,
            component_cache=component_cache,
        )
        idx_right = _single_run_core_stat_index(
            right_component['run_path'],
            component=right_component,
            component_cache=component_cache,
        )
        for key in sorted(set(idx_left) & set(idx_right)):
            left = idx_left[key]
            right = idx_right[key]
            rows.append({
                'comparison_id': comparison['comparison_id'],
                'comparison_kind': comparison.get('comparison_kind'),
                'left_component_id': left_component['component_id'],
                'left_display_name': left_component['display_name'],
                'right_component_id': right_component['component_id'],
                'right_display_name': right_component['display_name'],
                'stat_key': key,
                'metric': left.metric,
                'left_mean': left.mean,
                'right_mean': right.mean,
                'abs_delta': None if left.mean is None or right.mean is None else abs(left.mean - right.mean),
            })
    table = pd.DataFrame(rows)
    csv_fpath = out_dpath / f'core_runlevel_table.csv'
    md_fpath = out_dpath / f'core_runlevel_table.md'
    table.to_csv(csv_fpath, index=False)
    try:
        write_text_atomic(md_fpath, table.to_markdown(index=False) + '\n')
    except ImportError:
        md_fpath = None
    return csv_fpath, md_fpath


@profile
def _write_text(report: dict[str, Any], out_fpath: Path) -> None:
    pairs = report['pairs']
    local_repeat = _find_pair(report, 'local_repeat')
    official_vs_local = _find_pair(report, 'official_vs_local') or (pairs[-1] if pairs else {})
    lines = []
    lines.append('Core Metric Report')
    lines.append('')
    lines.append(f"generated_utc: {report['generated_utc']}")
    lines.append(f"run_spec_name: {report['run_spec_name']}")
    lines.append(f"report_dpath: {report['report_dpath']}")
    lines.append(f"components_manifest: {report['components_manifest_path']}")
    lines.append(f"comparisons_manifest: {report['comparisons_manifest_path']}")
    lines.append(f"single_run_mode: {str(report.get('single_run_mode', False)).lower()}")
    lines.append(f"diagnostic_flags: {report.get('diagnostic_flags', [])}")
    lines.append('')
    lines.append('warnings_and_caveats:')
    lines.append(f"  packet_warnings: {report.get('packet_warnings', [])}")
    lines.append(f"  packet_caveats: {report.get('packet_caveats', [])}")
    lines.append(f"  warnings_manifest: {report.get('warnings_manifest_path')}")
    lines.append('')
    lines.append('selected_components:')
    for component in report.get('components', []):
        lines.append(
            f"  - {component['component_id']}: tags={component.get('tags', [])} "
            f"artifact_format={component.get('artifact_format')} "
            f"eee_artifact_path={component.get('eee_artifact_path')} "
            f"run_path={component.get('run_path')}"
        )
    lines.append('')
    lines.append('comparisons:')
    for comparison in report.get('comparisons', []):
        lines.append(
            f"  - {comparison['comparison_id']}: kind={comparison.get('comparison_kind')} "
            f"enabled={comparison.get('enabled')} component_ids={comparison.get('component_ids')}"
        )
        lines.append(f"    disabled_reason: {comparison.get('disabled_reason')}")
        lines.append(f"    warnings: {comparison.get('warnings', [])}")
        lines.append(f"    caveats: {comparison.get('caveats', [])}")
    lines.append('')
    lines.append('comparability:')
    for fact_name, fact in (report.get('comparability') or {}).get('facts', {}).items():
        lines.append(f"  {fact_name}: {fact.get('status')} values={fact.get('values')}")
    lines.append('')
    lines.append('core_metrics:')
    ref_pair = local_repeat or official_vs_local
    for metric in ref_pair.get('core_metrics', []):
        lines.append(f'  - {metric}')
    lines.append('')
    lines.append('run_diagnostics:')
    for label, diag in report.get('run_diagnostics', {}).items():
        lines.append(f'  {label}:')
        lines.append(f"    n_request_states: {diag.get('n_request_states')}")
        lines.append(f"    n_with_completions: {diag.get('n_with_completions')}")
        lines.append(f"    empty_completion_count: {diag.get('empty_completion_count')}")
        lines.append(f"    empty_completion_rate: {diag.get('empty_completion_rate')}")
        lines.append(f"    output_token_count: {json.dumps(diag.get('output_token_count'))}")
        lines.append(f"    stats_means: {json.dumps(diag.get('stats_means'))}")
    lines.append('')
    for pair in report['pairs']:
        lines.append(f"pair: {pair['comparison_id']}")
        lines.append(f"  comparison_kind: {pair.get('comparison_kind')}")
        lines.append(f"  diagnosis: {pair['diagnosis'].get('label')}")
        lines.append(f"  primary_reason_names: {pair['diagnosis'].get('primary_reason_names')}")
        lines.append(f"  run_level_n: {pair['run_level']['n_rows']}")
        lines.append(f"  instance_level_n: {pair['instance_level']['n_rows']}")
        lines.append(f"  run_level_quantiles: {json.dumps(pair['run_level']['overall_quantiles']['abs_delta'])}")
        lines.append(f"  instance_level_quantiles: {json.dumps(pair['instance_level']['overall_quantiles']['abs_delta'])}")
        lines.append('  by_metric:')
        for row in pair['instance_level']['by_metric']:
            lines.append(
                f"    - metric={row['metric']} count={row['count']} "
                f"p50={row['abs_delta']['p50']} p90={row['abs_delta']['p90']} "
                f"p95={row['abs_delta']['p95']} p99={row['abs_delta']['p99']} "
                f"max={row['abs_delta']['max']}"
            )
        lines.append('  agreement_vs_abs_tol:')
        for row in pair['instance_level']['agreement_vs_abs_tol']:
            lines.append(
                f"    - abs_tol={row['abs_tol']} agree_ratio={row['agree_ratio']}"
            )
        lines.append('')
    write_text_atomic(out_fpath, '\n'.join(lines) + '\n')


def _find_curve_value(rows: list[dict[str, Any]], abs_tol: float) -> float | None:
    for row in rows:
        if float(row.get('abs_tol', float('nan'))) == float(abs_tol):
            return row.get('agree_ratio')
    return None


@profile
def _write_management_summary(report: dict[str, Any], out_fpath: Path) -> None:
    pairs = report['pairs']
    local_repeat = _find_pair(report, 'local_repeat')
    official_vs_local = _find_pair(report, 'official_vs_local') or (pairs[-1] if pairs else {})
    ref_pair = local_repeat or official_vs_local
    lines = []
    lines.append('Core Metric Executive Summary')
    lines.append('')
    lines.append(f"generated_utc: {report['generated_utc']}")
    lines.append(f"run_spec_name: {report['run_spec_name']}")
    lines.append(f"report_dpath: {report['report_dpath']}")
    lines.append(f"components_manifest: {report['components_manifest_path']}")
    lines.append(f"comparisons_manifest: {report['comparisons_manifest_path']}")
    lines.append(f"single_run_mode: {str(report.get('single_run_mode', False)).lower()}")
    lines.append(f"core_metrics: {', '.join(ref_pair.get('core_metrics', []))}")
    lines.append(f"diagnostic_flags: {report.get('diagnostic_flags', [])}")
    lines.append('')
    lines.append('warnings_and_caveats:')
    lines.append(f"  packet_warnings: {report.get('packet_warnings', [])}")
    lines.append(f"  packet_caveats: {report.get('packet_caveats', [])}")
    lines.append(f"  warnings_manifest: {report.get('warnings_manifest_path')}")
    lines.append('')
    lines.append('selected_components:')
    for component in report.get('components', []):
        lines.append(
            f"  - {component['component_id']}: tags={component.get('tags', [])} "
            f"artifact_format={component.get('artifact_format')} "
            f"eee_artifact_path={component.get('eee_artifact_path')} "
            f"run_path={component.get('run_path')}"
        )
    lines.append('')
    lines.append('comparisons:')
    for comparison in report.get('comparisons', []):
        lines.append(
            f"  - {comparison['comparison_id']}: kind={comparison.get('comparison_kind')} "
            f"enabled={comparison.get('enabled')} component_ids={comparison.get('component_ids')}"
        )
        lines.append(f"    disabled_reason: {comparison.get('disabled_reason')}")
        lines.append(f"    warnings: {comparison.get('warnings', [])}")
        lines.append(f"    caveats: {comparison.get('caveats', [])}")
    lines.append('')
    lines.append('comparability:')
    for fact_name, fact in (report.get('comparability') or {}).get('facts', {}).items():
        lines.append(f"  {fact_name}: {fact.get('status')} values={fact.get('values')}")
    lines.append('')
    lines.append('on_demand_heavy_pairwise_plots: render_heavy_pairwise_plots.sh (in this directory)')
    lines.append('  (histogram/ECDF distributions and per-metric agreement PNG plots; not rendered by default)')
    lines.append('')
    lines.append('metric_descriptions:')
    for metric in ref_pair.get('core_metrics', []):
        desc = _metric_descriptor(metric)
        lines.append(
            f"  - {metric}: {desc['kind']}; {desc['range']}; {desc['direction']}"
        )
    lines.append('')
    lines.append('run_diagnostics:')
    for label, diag in report.get('run_diagnostics', {}).items():
        lines.append(f'  {label}:')
        lines.append(f"    n_request_states: {diag.get('n_request_states')}")
        lines.append(f"    n_with_completions: {diag.get('n_with_completions')}")
        lines.append(f"    empty_completion_count: {diag.get('empty_completion_count')}")
        lines.append(f"    empty_completion_rate: {diag.get('empty_completion_rate')}")
        lines.append(f"    mean_output_tokens_from_state: {(diag.get('output_token_count') or {}).get('mean')}")
        lines.append(f"    p90_output_tokens_from_state: {(diag.get('output_token_count') or {}).get('p90')}")
        lines.append(f"    num_output_tokens_from_stats: {(diag.get('stats_means') or {}).get('num_output_tokens')}")
        lines.append(f"    finish_reason_unknown_from_stats: {(diag.get('stats_means') or {}).get('finish_reason_unknown')}")
    lines.append('')
    if local_repeat is not None:
        lines.append(f"{local_repeat['comparison_id']}:")
        lines.append(f"  diagnosis: {local_repeat['diagnosis'].get('label')}")
        lines.append(f"  run-level N: {local_repeat['run_level']['n_rows']}")
        lines.append(f"  instance-level N: {local_repeat['instance_level']['n_rows']}")
        lines.append(
            f"  instance agreement at abs_tol=0.0: {_find_curve_value(local_repeat['instance_level']['agreement_vs_abs_tol'], 0.0)}"
        )
        lines.append(
            f"  run-level abs delta max: {local_repeat['run_level']['overall_quantiles']['abs_delta']['max']}"
        )
        lines.append(
            f"  instance-level abs delta max: {local_repeat['instance_level']['overall_quantiles']['abs_delta']['max']}"
        )
        lines.append('')
    else:
        lines.append('local_repeat: not_computed')
        lines.append('')
    lines.append(f"{official_vs_local['comparison_id']}:")
    lines.append(f"  diagnosis: {official_vs_local['diagnosis'].get('label')}")
    lines.append(f"  run-level N: {official_vs_local['run_level']['n_rows']}")
    lines.append(f"  instance-level N: {official_vs_local['instance_level']['n_rows']}")
    for tol in [0.0, 1e-3, 1e-2, 1e-1, 2.5e-1, 5e-1, 1.0]:
        lines.append(
            f"  instance agreement at abs_tol={tol}: "
            f"{_find_curve_value(official_vs_local['instance_level']['agreement_vs_abs_tol'], tol)}"
        )
    lines.append(
        f"  run-level abs delta p90/max: "
        f"{official_vs_local['run_level']['overall_quantiles']['abs_delta']['p90']} / "
        f"{official_vs_local['run_level']['overall_quantiles']['abs_delta']['max']}"
    )
    lines.append(
        f"  instance-level abs delta p99/max: "
        f"{official_vs_local['instance_level']['overall_quantiles']['abs_delta']['p99']} / "
        f"{official_vs_local['instance_level']['overall_quantiles']['abs_delta']['max']}"
    )
    write_text_atomic(out_fpath, '\n'.join(lines) + '\n')


def _write_latest_alias(src: Path | None, latest_root: Path, latest_name: str) -> Path | None:
    """Tolerates ``src is None``. After the simplification (2026-04-28b)
    the canonical artifact is written directly to ``<root>/<name>.<ext>``,
    so callers passing ``src`` already at that target make this a no-op.
    The fallback is :func:`link_alias` for cross-tree navigation aliases."""
    if src is None:
        return None
    target = latest_root / latest_name
    if Path(src) == target:
        return target
    return link_alias(src, latest_root, latest_name)


def _atomic_savefig(fig, fpath: Path, **kwargs) -> Path:
    """matplotlib ``fig.savefig`` writing to ``fpath`` atomically via safer.
    Format inferred from the file suffix (defaults to png)."""
    fpath = Path(fpath)
    suffix = fpath.suffix.lstrip('.') or 'png'
    with safer.open(fpath, 'wb', make_parents=True) as fp:
        fig.savefig(fp, format=suffix, **kwargs)
    return fpath


@profile
def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-dpath', required=True)
    parser.add_argument('--components-manifest', default=None)
    parser.add_argument('--comparisons-manifest', default=None)
    parser.add_argument(
        '--render-heavy-pairwise-plots',
        action='store_true',
        default=False,
        help=(
            'Also render heavy per-pair PNG plots (histograms, ECDFs, per-metric agreement curves). '
            'Off by default; run render_heavy_pairwise_plots.sh in the report directory instead.'
        ),
    )
    parser.add_argument(
        '--plots-only',
        action='store_true',
        default=False,
        help=(
            'Skip rewriting the JSON/text/management/warnings/runlevel-table report artifacts; '
            'only redraw figures and update plot latest aliases. Intended for fast iteration on '
            'plot styling: edit core_metrics.py and rerun redraw_plots.sh in the report directory.'
        ),
    )
    parser.add_argument(
        '--plot_figure_scale',
        type=float,
        default=None,
        help=(
            'Optional multiplicative scale for Matplotlib figure sizes. '
            'Increase when labels or titles are too crowded for the canvas.'
        ),
    )
    parser.add_argument(
        '--plot-figure-scale',
        dest='plot_figure_scale',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_target',
        choices=sorted(_PLOT_TARGETS),
        default='all',
        help=(
            'When redrawing plots, render only this plot family. '
            'Use all to refresh every plot artifact.'
        ),
    )
    parser.add_argument(
        '--plot-target',
        dest='plot_target',
        choices=sorted(_PLOT_TARGETS),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_suptitle_y',
        type=float,
        default=None,
        help=(
            'Optional Matplotlib figure-coordinate y position for figure suptitles. '
            'Increase above the default when subplot titles overlap a multi-line suptitle.'
        ),
    )
    parser.add_argument(
        '--plot-suptitle-y',
        dest='plot_suptitle_y',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_constrained_h_pad',
        type=float,
        default=None,
        help=(
            'Optional constrained-layout vertical padding in inches. '
            'Useful for adding space between suptitles, subplot titles, and axes.'
        ),
    )
    parser.add_argument(
        '--plot-constrained-h-pad',
        dest='plot_constrained_h_pad',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_constrained_hspace',
        type=float,
        default=None,
        help=(
            'Optional constrained-layout vertical spacing between subplot groups. '
            'Use with --plot_constrained_h_pad when crowded figures still overlap.'
        ),
    )
    parser.add_argument(
        '--plot-constrained-hspace',
        dest='plot_constrained_hspace',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_constrained_w_pad',
        type=float,
        default=None,
        help=(
            'Optional constrained-layout horizontal padding in inches. '
            'Useful when y-axis labels, legends, or side-by-side panels crowd each other.'
        ),
    )
    parser.add_argument(
        '--plot-constrained-w-pad',
        dest='plot_constrained_w_pad',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_constrained_wspace',
        type=float,
        default=None,
        help=(
            'Optional constrained-layout horizontal spacing between subplot groups. '
            'Use with --plot_constrained_w_pad when side-by-side panels are too tight.'
        ),
    )
    parser.add_argument(
        '--plot-constrained-wspace',
        dest='plot_constrained_wspace',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_subplot_left',
        type=float,
        default=None,
        help='Optional manual left margin for fig.subplots_adjust, as a figure fraction.',
    )
    parser.add_argument(
        '--plot-subplot-left',
        dest='plot_subplot_left',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_subplot_right',
        type=float,
        default=None,
        help='Optional manual right edge for fig.subplots_adjust, as a figure fraction.',
    )
    parser.add_argument(
        '--plot-subplot-right',
        dest='plot_subplot_right',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_subplot_bottom',
        type=float,
        default=None,
        help='Optional manual bottom margin for fig.subplots_adjust, as a figure fraction.',
    )
    parser.add_argument(
        '--plot-subplot-bottom',
        dest='plot_subplot_bottom',
        type=float,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--plot_subplot_top',
        type=float,
        default=None,
        help='Optional manual top edge for fig.subplots_adjust, as a figure fraction.',
    )
    parser.add_argument(
        '--plot-subplot-top',
        dest='plot_subplot_top',
        type=float,
        help=argparse.SUPPRESS,
    )
    # The diagnosis labels (recipe_clean / deployment_drift /
    # comparability_unknown / ...) live on top of the agreement-ratio
    # numbers and are derived from HELM ``run_spec.json``. For the EEE-only
    # paper validity claim, the heatmap's numerical content must come from
    # EEE alone; --skip-diagnosis bypasses the HelmRunDiff branch in
    # _build_pair so no run_spec.json is consulted, the auxiliary
    # diagnosis dict comes back empty, and the agreement numbers are
    # untouched. Default reads EVAL_AUDIT_SKIP_HELM_DIAGNOSIS={1,true,yes}
    # so wrappers can flip this for an entire pipeline invocation without
    # threading the flag through every CLI hop.
    _skip_diag_default = os.environ.get(
        'EVAL_AUDIT_SKIP_HELM_DIAGNOSIS', ''
    ).strip().lower() in {'1', 'true', 'yes'}
    parser.add_argument(
        '--skip-diagnosis',
        action='store_true',
        default=_skip_diag_default,
        help=(
            'Skip the HELM-derived diagnosis labels (recipe_clean / '
            'deployment_drift / etc). Use for the EEE-only paper path '
            'where run_spec.json must not be consulted. The heatmap '
            'numerical content is unaffected; only the auxiliary '
            'diagnosis dict in core_metric_report.json becomes empty. '
            'Also reads EVAL_AUDIT_SKIP_HELM_DIAGNOSIS={1,true,yes} as '
            'the default.'
        ),
    )
    # Distinct from HELM_AUDIT_SKIP_PLOTLY (which only affects Plotly /
    # Chromium PNG exports in build_reports_summary). All the heavyweight
    # per-pair figures in core_metrics are matplotlib — they were not
    # gated by the SKIP_PLOTLY env var and that's been a foot-gun for
    # iteration. --no-plots / EVAL_AUDIT_NO_PLOTS={1,true,yes} skips
    # every matplotlib plot block in core_metrics.main: the 2x2 summary
    # panel, the per-pair distribution figures, the overlay/ECDF runs,
    # and the per-metric agreement figures. JSON / TXT / management
    # summaries / runlevel tables still write — only static figures are
    # skipped.
    _no_plots_default = os.environ.get(
        'EVAL_AUDIT_NO_PLOTS', ''
    ).strip().lower() in {'1', 'true', 'yes'}
    parser.add_argument(
        '--no-plots',
        action='store_true',
        default=_no_plots_default,
        help=(
            'Skip every matplotlib figure in core_metrics.main (summary '
            'panel, pair distributions, overlays, ECDFs, per-metric '
            'agreement). JSON/TXT/runlevel-table outputs are unaffected. '
            'Distinct from HELM_AUDIT_SKIP_PLOTLY which only gates the '
            'Plotly/Chromium PNG exports in build_reports_summary. '
            'Also reads EVAL_AUDIT_NO_PLOTS={1,true,yes} as the default.'
        ),
    )
    args = parser.parse_args(argv)
    plot_layout = _plot_layout_from_cli(args)
    plot_target = args.plot_target

    thresholds = [0.0, 1e-12, 1e-9, 1e-6, 1e-4, 1e-3, 1e-2, 2e-2, 5e-2, 1e-1, 2.5e-1, 5e-1, 1.0]
    report_dpath = Path(args.report_dpath).expanduser().resolve()
    report_dpath.mkdir(parents=True, exist_ok=True)
    stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime('%Y%m%dT%H%M%SZ')
    # History layer retired 2026-04-28: write stamped intermediates next to
    # the visible *.* targets and let write_latest_alias rename them
    # in place. No .history/ subdir is created.
    history_dpath = report_dpath
    (
        components_manifest_fpath,
        components_manifest,
        comparisons_manifest_fpath,
        comparisons_manifest,
    ) = load_packet_manifests(
        report_dpath=report_dpath,
        components_manifest=args.components_manifest,
        comparisons_manifest=args.comparisons_manifest,
    )
    components = components_manifest.get('components') or []
    all_comparisons = comparisons_manifest.get('comparisons') or []
    comparisons = [comparison for comparison in all_comparisons if comparison.get('enabled', True)]
    component_lookup = {component['component_id']: component for component in components}
    run_spec_name = _infer_run_spec_name(*(component['run_path'] for component in components))

    # Memoize NormalizedRun loads across the per-pair loop. A typical
    # packet has one official component reused across N official_vs_local
    # pairs and ~N local components each appearing twice (once in
    # official_vs_local, once as the reference or repeat in
    # local_repeat). Without caching the official artifact gets parsed
    # ~N times. The cache is intentionally local to this packet
    # invocation so memory doesn't accumulate when from_eee renders
    # many packets in sequence (or in parallel via subprocess.run).
    component_cache: dict[str, NormalizedRun] = {}

    pairs = []
    for comparison in comparisons:
        component_ids = comparison.get('component_ids') or []
        if len(component_ids) != 2:
            continue
        component_a = component_lookup[component_ids[0]]
        component_b = component_lookup[component_ids[1]]
        # Pure-EEE components don't carry a HELM run_path; fall back to the
        # eee_artifact_path or component_id so _build_pair has a non-None
        # display anchor without also needing run_spec.json on disk.
        run_a = (
            component_a.get('run_path')
            or component_a.get('eee_artifact_path')
            or component_a['component_id']
        )
        run_b = (
            component_b.get('run_path')
            or component_b.get('eee_artifact_path')
            or component_b['component_id']
        )
        pair = _build_pair(
            run_a,
            run_b,
            str(comparison['comparison_id']),
            thresholds,
            component_a=component_a,
            component_b=component_b,
            component_cache=component_cache,
            skip_diagnosis=args.skip_diagnosis,
        )
        pair['artifact_formats'] = {
            component_ids[0]: component_a.get('artifact_format') or 'helm',
            component_ids[1]: component_b.get('artifact_format') or 'helm',
        }
        pair['comparison_id'] = comparison['comparison_id']
        pair['comparison_kind'] = comparison.get('comparison_kind')
        pair['component_ids'] = component_ids
        pair['reference_component_id'] = comparison.get('reference_component_id')
        pair['comparability_facts'] = comparison.get('comparability_facts') or {}
        pair['warnings'] = comparison.get('warnings') or []
        pair['caveats'] = comparison.get('caveats') or []
        pair['label'] = comparison['comparison_id']
        pairs.append(pair)

    run_diagnostics = {
        component['component_id']: _run_diagnostics(component['run_path'])
        for component in components
    }
    single_run_mode = not any(
        comparison.get('comparison_kind') == 'local_repeat'
        for comparison in comparisons
    )
    component_comparability = _comparability_summary(components)
    comparability = {
        'facts': components_manifest.get('comparability_facts') or component_comparability.get('facts', {}),
        'component_metadata': component_comparability.get('component_metadata', {}),
    }

    report = {
        'generated_utc': stamp,
        'run_spec_name': run_spec_name,
        'report_dpath': str(report_dpath),
        'packet_id': components_manifest.get('packet_id'),
        'run_entry': components_manifest.get('run_entry'),
        'planner_version': components_manifest.get('planner_version'),
        'components_manifest_path': str(components_manifest_fpath),
        'comparisons_manifest_path': str(comparisons_manifest_fpath),
        'warnings_manifest_path': str(report_dpath / 'warnings.json'),
        'thresholds': thresholds,
        'components': components,
        'comparisons': all_comparisons,
        'pairs': pairs,
        'run_diagnostics': run_diagnostics,
        'diagnostic_flags': _diagnostic_flags(run_diagnostics, components, comparisons),
        'single_run_mode': single_run_mode,
        'comparability': comparability,
        'packet_warnings': components_manifest.get('warnings') or [],
        'packet_caveats': components_manifest.get('caveats') or [],
        'official_selection': components_manifest.get('official_selection') or {},
    }

    json_fpath = history_dpath / f'core_metric_report.json'
    txt_fpath = history_dpath / f'core_metric_report.txt'
    mgmt_fpath = history_dpath / f'core_metric_management_summary.txt'
    warnings_json_fpath = history_dpath / f'warnings.json'
    warnings_txt_fpath = history_dpath / f'warnings.txt'
    official_vs_local = _find_pair(report, 'official_vs_local') or (pairs[-1] if pairs else None)
    local_repeat = _find_pair(report, 'local_repeat')

    if official_vs_local is None:
        raise SystemExit('No enabled comparisons were available to render a core metric report')

    # --no-plots is the master kill-switch: when set, no matplotlib
    # block runs regardless of plots_only / render_heavy_pairwise_plots
    # / plot_target.
    render_core_metric_report = (
        (not args.no_plots)
        and ((not args.plots_only) or _wants_plot(plot_target, 'core_metric_report'))
    )
    if render_core_metric_report and len(pairs) == 1:
        fig_fpath = _plot_single_pair_summary(
            history_dpath,
            stamp,
            official_vs_local,
            run_spec_name,
            plot_layout=plot_layout,
        )
    elif render_core_metric_report:
        fig_fpath = history_dpath / f'core_metric_report.png'
        extra_pair = _load_optional_cross_machine_pair(report_dpath)
        paper_labels = load_paper_label_manager(style='paper_short')
        all_pairs = pairs + ([extra_pair] if extra_pair is not None else [])
        # Alias every pair label so the legend in the bottom row stays
        # readable; emit the alias->full mapping as a sidecar artifact.
        pair_alias_map = short_alias_map([p['label'] for p in all_pairs])
        pair_line = 'Pairs: ' + ' vs '.join(
            pair_alias_map.get(pair['label'], pair_alias_map.get(pair.get('comparison_id', ''), pair.get('comparison_id', '')))
            for pair in pairs
        )
        if extra_pair is not None:
            pair_line += f' + {pair_alias_map[extra_pair["label"]]}'
        pair_line += '  (full labels in sidecar legend artifact)'
        pair_line = paper_labels.relabel_text(pair_line)
        _apply_matplotlib_style()
        layout = plot_layout or PlotLayout()
        fig, axes = plt.subplots(
            2,
            2,
            figsize=_scaled_figsize(24, 14.5, plot_layout),
            constrained_layout=False,
        )
        _plot_quantiles(
            axes[0, 0],
            local_repeat or official_vs_local,
            official_vs_local,
            'run_level',
            'Run-Level Delta Quantiles'
        )
        _plot_quantiles(
            axes[0, 1],
            local_repeat or official_vs_local,
            official_vs_local,
            'instance_level',
            'Instance-Level Delta Quantiles'
        )
        _plot_distribution(axes[1, 0], *all_pairs, level_key='run_level', alias_map=pair_alias_map)
        axes[1, 0].set_title('Run-Level Agreement vs Tolerance', fontsize=11)
        _plot_distribution(axes[1, 1], *all_pairs, level_key='instance_level', alias_map=pair_alias_map)
        axes[1, 1].set_title('Instance-Level Agreement vs Tolerance', fontsize=11)
        axes[0, 0].title.set_fontsize(11)
        axes[0, 1].title.set_fontsize(11)
        _set_suptitle(
            fig,
            'Core Metric Agreement and Difference Summary\n'
            f'Run Spec: {run_spec_name}\n'
            f'{pair_line}',
            fontsize=15,
            plot_layout=plot_layout,
        )
        adjust_kwargs = _subplot_adjust_kwargs(fig, layout, top=0.82, bottom=0.07)
        adjust_kwargs['left'] = max(adjust_kwargs.get('left', 0.06), 0.06)
        adjust_kwargs['right'] = min(adjust_kwargs.get('right', 0.98), 0.98)
        adjust_kwargs['wspace'] = max(adjust_kwargs.get('wspace', 0.22), 0.22)
        fig.subplots_adjust(**adjust_kwargs)
        _atomic_savefig(fig, fig_fpath, dpi=180)
        plt.close(fig)
        emit_label_legend_artifacts(
            pair_alias_map,
            fig_dpath=report_dpath,
            out_name='core_metric_report',
            title='Core Metric Report — short alias → full pair label',
            stamp=stamp,
            color_map=_palette_color_map([p['label'] for p in all_pairs]),
        )
    else:
        fig_fpath = None

    render_pairwise = args.render_heavy_pairwise_plots and not args.no_plots
    if render_pairwise and _wants_plot(plot_target, 'core_metric_distributions'):
        dist_fig_fpath = _plot_pair_metric_distributions(
            history_dpath,
            stamp,
            pairs,
            run_spec_name,
            plot_layout=plot_layout,
        )
    else:
        dist_fig_fpath = None
    if render_pairwise and (
        _wants_plot(plot_target, 'core_metric_overlay_distributions')
        or _wants_plot(plot_target, 'core_metric_ecdfs')
    ):
        run_specs = [
            (component['run_path'], component['display_name'], component)
            for component in components
        ]
    else:
        run_specs = []
    if render_pairwise and _wants_plot(plot_target, 'core_metric_overlay_distributions'):
        overlay_dist_artifacts = _plot_run_metric_distributions(
            history_dpath,
            stamp,
            run_specs,
            run_spec_name,
            out_name='core_metric_overlay_distributions',
            title='Overlay of Per-Instance Core Metric Score Distributions by Component',
            subtitle='Each series comes from a selected report component declared in the components manifest.',
            plot_layout=plot_layout,
        )
    else:
        overlay_dist_artifacts = None
    if render_pairwise and _wants_plot(plot_target, 'core_metric_ecdfs'):
        ecdf_artifacts = _plot_run_metric_distributions(
            history_dpath,
            stamp,
            run_specs,
            run_spec_name,
            out_name='core_metric_ecdfs',
            title='ECDF of Per-Instance Core Metric Scores by Component',
            subtitle='Each series comes from a selected report component declared in the components manifest.',
            ecdf=True,
            plot_layout=plot_layout,
        )
    else:
        ecdf_artifacts = None
    if render_pairwise and _wants_plot(plot_target, 'core_metric_per_metric_agreement'):
        per_metric_agree_fpath = _plot_per_metric_agreement(
            history_dpath,
            stamp,
            *pairs,
            level_key='instance_level',
            thresholds=thresholds,
            plot_layout=plot_layout,
        )
    else:
        per_metric_agree_fpath = None
    overlay_dist_fpath = (overlay_dist_artifacts or {}).get('plot') if overlay_dist_artifacts else None
    overlay_dist_legend_png = (overlay_dist_artifacts or {}).get('legend_png') if overlay_dist_artifacts else None
    overlay_dist_legend_txt = (overlay_dist_artifacts or {}).get('legend_txt') if overlay_dist_artifacts else None
    ecdf_fig_fpath = (ecdf_artifacts or {}).get('plot') if ecdf_artifacts else None
    ecdf_legend_png = (ecdf_artifacts or {}).get('legend_png') if ecdf_artifacts else None
    ecdf_legend_txt = (ecdf_artifacts or {}).get('legend_txt') if ecdf_artifacts else None
    plots_only = args.plots_only
    if not plots_only:
        runlevel_csv_fpath, runlevel_md_fpath = _write_comparison_runlevel_table(
            history_dpath,
            stamp,
            comparisons,
            component_lookup,
            component_cache=component_cache,
        )
        report = kwutil.Json.ensure_serializable(_strip_private(report))
        write_text_atomic(json_fpath, json.dumps(report, indent=2))
        _write_text(report, txt_fpath)
        _write_management_summary(report, mgmt_fpath)
        write_text_atomic(warnings_json_fpath, json.dumps(_warnings_payload(report), indent=2) + '\n')
        write_text_atomic(warnings_txt_fpath, '\n'.join(_warning_summary_lines(report)) + '\n')

    # Build the latest alias map. In plots_only mode we only refresh plot
    # aliases — the JSON/text/management/warnings/runlevel artifacts and their
    # latest aliases are intentionally left untouched so the existing canonical
    # report stays consistent while we iterate on plot styling.
    plot_latest_map: dict[Path, str] = {}
    if fig_fpath is not None:
        plot_latest_map[fig_fpath] = 'core_metric_report.png'
    if dist_fig_fpath is not None:
        plot_latest_map[dist_fig_fpath] = 'core_metric_distributions.png'
    if overlay_dist_fpath is not None:
        plot_latest_map[overlay_dist_fpath] = 'core_metric_overlay_distributions.png'
    if overlay_dist_legend_png is not None:
        plot_latest_map[overlay_dist_legend_png] = 'core_metric_overlay_distributions_label_legend.png'
    if overlay_dist_legend_txt is not None:
        plot_latest_map[overlay_dist_legend_txt] = 'core_metric_overlay_distributions_label_legend.txt'
    if ecdf_fig_fpath is not None:
        plot_latest_map[ecdf_fig_fpath] = 'core_metric_ecdfs.png'
    if ecdf_legend_png is not None:
        plot_latest_map[ecdf_legend_png] = 'core_metric_ecdfs_label_legend.png'
    if ecdf_legend_txt is not None:
        plot_latest_map[ecdf_legend_txt] = 'core_metric_ecdfs_label_legend.txt'
    if per_metric_agree_fpath is not None:
        plot_latest_map[per_metric_agree_fpath] = 'core_metric_per_metric_agreement.png'

    if plots_only:
        latest_map = plot_latest_map
    else:
        latest_map = {
            json_fpath: 'core_metric_report.json',
            txt_fpath: 'core_metric_report.txt',
            mgmt_fpath: 'core_metric_management_summary.txt',
            warnings_json_fpath: 'warnings.json',
            warnings_txt_fpath: 'warnings.txt',
            runlevel_csv_fpath: 'core_runlevel_table.csv',
            **plot_latest_map,
        }
        if runlevel_md_fpath is not None:
            latest_map[runlevel_md_fpath] = 'core_runlevel_table.md'
    for src, latest_name in latest_map.items():
        _write_latest_alias(src, report_dpath, latest_name)
    if not plots_only:
        # Stale-alias cleanup is for the canonical (full) write path. In
        # plots_only mode the other artifacts (JSON/text/runlevel/...) are
        # deliberately not in latest_map, so blanket cleanup would erase them.
        known_latest_names = {
            'core_metric_report.json',
            'core_metric_report.txt',
            'core_metric_management_summary.txt',
            'warnings.json',
            'warnings.txt',
            'core_metric_report.png',
            'core_metric_distributions.png',
            'core_metric_three_run_distributions.png',
            'core_metric_overlay_distributions.png',
            'core_metric_overlay_distributions_label_legend.png',
            'core_metric_overlay_distributions_label_legend.txt',
            'core_metric_ecdfs.png',
            'core_metric_ecdfs_label_legend.png',
            'core_metric_ecdfs_label_legend.txt',
            'core_metric_per_metric_agreement.png',
            'core_runlevel_table.csv',
            'core_runlevel_table.md',
        }
        for latest_name in known_latest_names - set(latest_map.values()):
            safe_unlink(report_dpath / latest_name)

    if not plots_only:
        logger.info(f'Wrote core metric report: {rich_link(json_fpath)}')
        logger.info(f'Wrote core metric text: {rich_link(txt_fpath)}')
        logger.info(f'Wrote core metric management summary: {rich_link(mgmt_fpath)}')
        logger.info(f'Wrote core metric warnings json: {rich_link(warnings_json_fpath)}')
        logger.info(f'Wrote core metric warnings text: {rich_link(warnings_txt_fpath)}')
    if fig_fpath is not None:
        logger.info(f'Wrote core metric plot: {rich_link(fig_fpath)}')
    if dist_fig_fpath is not None:
        logger.info(f'Wrote core metric distributions: {rich_link(dist_fig_fpath)}')
    if overlay_dist_fpath is not None:
        logger.info(f'Wrote core metric overlay distributions: {rich_link(overlay_dist_fpath)}')
    if ecdf_fig_fpath is not None:
        logger.info(f'Wrote core metric ecdfs: {rich_link(ecdf_fig_fpath)}')
    if per_metric_agree_fpath is not None:
        logger.info(f'Wrote per-metric agreement curves: {rich_link(per_metric_agree_fpath)}')
    if not plots_only:
        logger.info(f'Wrote core run-level table csv: {rich_link(runlevel_csv_fpath)}')
        if runlevel_md_fpath is not None:
            logger.info(f'Wrote core run-level table md: {rich_link(runlevel_md_fpath)}')


if __name__ == '__main__':
    setup_cli_logging()
    main()
