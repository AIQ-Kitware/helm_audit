from __future__ import annotations

import argparse

from helm_audit.infra.logging import setup_cli_logging
import datetime as datetime_mod
import json
import os
import shutil
import statistics
from pathlib import Path
from typing import Any

import kwutil
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

from helm_audit.compat.helm_outputs import HelmRun
from helm_audit.helm.analysis import HelmRunAnalysis
from helm_audit.helm.diff import HelmRunDiff
from helm_audit.helm import metrics as helm_metrics
from helm_audit.indexing.schema import extract_run_spec_fields
from helm_audit.infra.fs_publish import safe_unlink
from helm_audit.reports.paper_labels import load_paper_label_manager
from helm_audit.reports.core_packet import load_packet_manifests
from helm_audit.utils.numeric import safe_float as _safe_float, quantile as _quantile


def _run_level_core_rows(diff: HelmRunDiff) -> list[dict[str, Any]]:
    idx_a = diff.a.stat_index(drop_zero_count=True, require_mean=True, short_hash=diff.short_hash)
    idx_b = diff.b.stat_index(drop_zero_count=True, require_mean=True, short_hash=diff.short_hash)
    rows = []
    for k in set(idx_a) & set(idx_b):
        a = idx_a[k]
        b = idx_b[k]
        if a.mean is None or b.mean is None:
            continue
        if a.metric_class != 'core':
            continue
        abs_delta = abs(a.mean - b.mean)
        denom = max(abs(a.mean), abs(b.mean), 1e-12)
        rel_delta = abs_delta / denom
        rows.append({
            'key': k,
            'metric': a.metric,
            'metric_class': a.metric_class,
            'a': float(a.mean),
            'b': float(b.mean),
            'abs_delta': abs_delta,
            'rel_delta': rel_delta,
        })
    return rows


def _load_json(fpath: Path) -> Any:
    return json.loads(fpath.read_text())


def _load_optional_cross_machine_pair(report_dpath: Path) -> dict[str, Any] | None:
    pair_fpath = report_dpath / 'cross-machine-aiq-gpu' / 'pair_report.latest.json'
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


def _run_diagnostics(run_path: str) -> dict[str, Any]:
    run_path = str(Path(run_path).expanduser().resolve())
    run_dpath = Path(run_path)
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
        if len(component_ids) == 2:
            left_component = component_lookup.get(component_ids[0], {})
            right_component = component_lookup.get(component_ids[1], {})
            left_diag = run_diagnostics.get(component_ids[0], {})
            right_diag = run_diagnostics.get(component_ids[1], {})
            left_rate = left_diag.get('empty_completion_rate')
            right_rate = right_diag.get('empty_completion_rate')
            if (
                left_component.get('source_kind') == 'official'
                and left_rate is not None
                and right_rate is not None
                and left_rate < 0.01
                and right_rate > 0.1
            ):
                flags.append(
                    f"{official_vs_local['comparison_id']}:empty_completion_pathology"
                )
    return flags


def _iter_joined_rows(joined, row_by_key):
    if row_by_key is not None:
        return row_by_key.values()
    if isinstance(joined, dict):
        return joined.values()
    if hasattr(joined, '__iter__'):
        return joined
    return []


def _row_key(row: Any) -> Any:
    return (
        getattr(row, 'key', None)
        or getattr(row, 'stat_key', None)
        or getattr(row, 'row_key', None)
        or row
    )


def _instance_level_core_rows(diff: HelmRunDiff) -> list[dict[str, Any]]:
    joined_a = diff.a.joined_instance_stat_table(assert_assumptions=False, short_hash=diff.short_hash)
    joined_b = diff.b.joined_instance_stat_table(assert_assumptions=False, short_hash=diff.short_hash)
    map_a = getattr(joined_a, 'row_by_key', None)
    map_b = getattr(joined_b, 'row_by_key', None)
    if map_a is None:
        map_a = {_row_key(r): r for r in _iter_joined_rows(joined_a, map_a)}
    if map_b is None:
        map_b = {_row_key(r): r for r in _iter_joined_rows(joined_b, map_b)}

    rows = []
    for k in set(map_a) & set(map_b):
        ra = map_a[k]
        rb = map_b[k]
        sa = getattr(ra, 'stat', None) if hasattr(ra, 'stat') else (ra.get('stat') if isinstance(ra, dict) else None)
        sb = getattr(rb, 'stat', None) if hasattr(rb, 'stat') else (rb.get('stat') if isinstance(rb, dict) else None)
        ma = _safe_float((sa or {}).get('mean') if isinstance(sa, dict) else getattr(sa, 'mean', None))
        mb = _safe_float((sb or {}).get('mean') if isinstance(sb, dict) else getattr(sb, 'mean', None))
        ca = int((sa or {}).get('count', 0) or 0) if isinstance(sa, dict) else int(getattr(sa, 'count', 0) or 0)
        cb = int((sb or {}).get('count', 0) or 0) if isinstance(sb, dict) else int(getattr(sb, 'count', 0) or 0)
        if ma is None or mb is None or ca == 0 or cb == 0:
            continue
        name_obj = (sa or {}).get('name') if isinstance(sa, dict) else getattr(sa, 'name_obj', None)
        metric = name_obj.get('name') if isinstance(name_obj, dict) else getattr(sa, 'metric', None)
        metric_class, _ = helm_metrics.classify_metric(metric)
        if metric_class != 'core':
            continue
        abs_delta = abs(ma - mb)
        denom = max(abs(ma), abs(mb), 1e-12)
        rel_delta = abs_delta / denom
        rows.append({
            'key': k,
            'metric': metric,
            'metric_class': metric_class,
            'a': ma,
            'b': mb,
            'abs_delta': abs_delta,
            'rel_delta': rel_delta,
        })
    return rows


def _group_quantiles(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(float(r['abs_delta']) for r in rows)
    return {
        'count': len(values),
        'abs_delta': {
            'min': _quantile(values, 0.0),
            'p50': _quantile(values, 0.5),
            'p90': _quantile(values, 0.9),
            'p95': _quantile(values, 0.95),
            'p99': _quantile(values, 0.99),
            'max': _quantile(values, 1.0),
        },
    }


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


def _metric_descriptor(metric: str) -> dict[str, str]:
    if metric in {
        'exact_match',
        'prefix_exact_match',
        'quasi_exact_match',
        'quasi_prefix_exact_match',
        'exact_match@5',
        'prefix_exact_match@5',
        'quasi_exact_match@5',
        'quasi_prefix_exact_match@5',
    }:
        return {
            'kind': 'binary',
            'range': '0 to 1',
            'direction': 'higher is better',
        }
    if metric in {'bleu_1', 'bleu_4', 'f1_score', 'rouge_l'}:
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


def _should_treat_as_discrete(values) -> bool:
    values = [float(v) for v in values if v is not None]
    unique_values = sorted(set(values))
    if not unique_values:
        return False
    return len(unique_values) <= 8 and all(v in {0.0, 1.0} for v in unique_values)


def _agreement_curve(rows: list[dict[str, Any]], thresholds: list[float]) -> list[dict[str, Any]]:
    if not rows:
        return []
    vals = [float(r['abs_delta']) for r in rows]
    out = []
    for t in thresholds:
        matched = sum(v <= t for v in vals)
        out.append({
            'abs_tol': t,
            'agree_ratio': matched / len(vals),
            'matched': matched,
            'count': len(vals),
        })
    return out


def _infer_run_spec_name(*run_paths: str) -> str:
    names = [Path(p).name for p in run_paths if p]
    names = [n for n in names if n]
    if not names:
        return 'unknown_run_spec'
    unique = sorted(set(names))
    if len(unique) == 1:
        return unique[0]
    return unique[0]


def _build_pair(run_a: str, run_b: str, label: str, thresholds: list[float]) -> dict[str, Any]:
    diff = HelmRunDiff(HelmRun.coerce(run_a), HelmRun.coerce(run_b), a_name=f'{label}:A', b_name=f'{label}:B')
    run_rows = _run_level_core_rows(diff)
    inst_rows = _instance_level_core_rows(diff)

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
        'diagnosis': diff.summary_dict(level=20).get('diagnosis', {}),
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


def _plot_distribution(ax, *pairs: dict[str, Any], level_key: str) -> None:
    rows = pd.DataFrame(_agreement_curve_rows(*pairs, level_key=level_key))
    if rows.empty or 'abs_tol' not in rows.columns or 'agree_ratio' not in rows.columns:
        ax.text(0.5, 0.5, 'No comparable core-metric rows', ha='center', va='center', transform=ax.transAxes)
        ax.set_axis_off()
        return
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
    ax.set_xlabel('Absolute Tolerance Threshold for Core Metric Difference')
    ax.set_ylabel('Fraction of Core Metric Comparisons in Agreement')
    ax.tick_params(axis='x', rotation=28)
    ax.legend(title='')


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


def _plot_per_metric_agreement(fig_dpath: Path, stamp: str, *pairs: dict[str, Any], level_key: str = 'instance_level', thresholds: list[float] | None = None) -> Path | None:
    """Create per-metric agreement curve plots."""
    if thresholds is None:
        thresholds = [1e-12, 1e-9, 1e-6, 1e-3, 1e-2, 0.1, 0.25, 0.5, 1.0]

    curves = _per_metric_agreement_curves(*pairs, level_key=level_key, thresholds=thresholds)
    if not curves:
        return None

    metrics = sorted(curves.keys())
    n_cols = min(3, len(metrics))
    n_rows = (len(metrics) + n_cols - 1) // n_cols

    fig, axes = plt.subplots(n_rows, n_cols, figsize=(6.5 * n_cols, 4.5 * n_rows), constrained_layout=True)
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

    fig.suptitle('Per-Metric Agreement vs Absolute Tolerance', fontsize=14)
    fig_fpath = fig_dpath / f'core_metric_per_metric_agreement_{stamp}.png'
    fig.savefig(fig_fpath, dpi=180)
    plt.close(fig)
    return fig_fpath


def _plot_quantiles(ax, pair_a: dict[str, Any], pair_b: dict[str, Any], level_key: str, title: str) -> None:
    labels = ['p50', 'p90', 'p95', 'p99', 'max']
    x = list(range(len(labels)))
    a_vals = [pair_a[level_key]['overall_quantiles']['abs_delta'][k] for k in labels]
    b_vals = [pair_b[level_key]['overall_quantiles']['abs_delta'][k] for k in labels]
    ax.plot(x, a_vals, marker='o', label=pair_a['label'])
    ax.plot(x, b_vals, marker='o', label=pair_b['label'])
    ax.set_xticks(x, labels)
    ax.set_yscale('symlog', linthresh=1e-12)
    ax.set_title(title)
    ax.set_xlabel('Quantile')
    ax.set_ylabel('Absolute Difference in Core Metric Value')
    ax.legend(title='')


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


def _plot_metric_distributions(fig_dpath: Path, stamp: str, left: dict[str, Any], right: dict[str, Any], run_spec_name: str) -> Path | None:
    return _plot_pair_metric_distributions(fig_dpath, stamp, [left, right], run_spec_name)


def _plot_pair_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    pairs: list[dict[str, Any]],
    run_spec_name: str,
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
    fig, axes = plt.subplots(
        len(pair_order),
        len(metrics),
        figsize=(5.2 * len(metrics), 4.2 * len(pair_order)),
        constrained_layout=True,
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
            ax.set_title(f'{pair_label}\n{metric}')
            ax.set_xlabel('Core metric value')
            ax.set_ylabel('Probability')
            legend = ax.get_legend()
            if legend is not None:
                legend.set_title('')
    fig.suptitle(
        'Core Metric Score Distributions Within Each Comparison Pair\n'
        f'Run Spec: {run_spec_name}\n'
        'Each panel shows the per-instance score distribution for side A vs side B.',
        fontsize=16,
    )
    out_fpath = fig_dpath / f'core_metric_distributions_{stamp}.png'
    fig.savefig(out_fpath, dpi=180)
    plt.close(fig)
    return out_fpath


def _plot_run_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    run_specs: list[tuple[str, str]],
    run_spec_name: str,
    *,
    out_name: str = 'core_metric_overlay_distributions',
    title: str = 'Overlay of Per-Instance Core Metric Score Distributions by Run',
    subtitle: str = 'This shows the raw score distributions for each core metric across the selected runs.',
    ecdf: bool = False,
) -> Path | None:
    frames = [
        _single_run_instance_core_rows(run_path, label)
        for run_path, label in run_specs
    ]
    df = pd.concat(frames, ignore_index=True)
    if df.empty or 'metric' not in df.columns:
        return None
    metrics = sorted(df['metric'].dropna().unique().tolist())
    if not metrics:
        return None
    fig, axes = plt.subplots(
        len(metrics),
        1,
        figsize=(10, 3.2 * len(metrics)),
        constrained_layout=True,
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
    fig.suptitle(
        f'{title}\n'
        f'Run Spec: {run_spec_name}\n'
        f'{subtitle}',
        fontsize=16,
    )
    out_fpath = fig_dpath / f'{out_name}_{stamp}.png'
    fig.savefig(out_fpath, dpi=180)
    plt.close(fig)
    return out_fpath


def _single_run_instance_core_rows(run_path: str, label: str) -> pd.DataFrame:
    ana = HelmRunAnalysis(HelmRun.coerce(run_path), name=label)
    joined = ana.joined_instance_stat_table(assert_assumptions=False)
    row_by_key = getattr(joined, 'row_by_key', None) or {}
    rows = []
    for row in row_by_key.values():
        stat = row.stat
        mean = _safe_float(stat.get('mean'))
        count = int(stat.get('count', 0) or 0)
        if mean is None or count == 0:
            continue
        name_obj = stat.get('name', {})
        metric = name_obj.get('name')
        metric_class, _ = helm_metrics.classify_metric(metric)
        if metric_class != 'core':
            continue
        rows.append({
            'run': label,
            'metric': metric,
            'value': float(mean),
        })
    return pd.DataFrame(rows)


def _plot_three_run_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    kwdagger_a_run: str,
    kwdagger_b_run: str,
    official_run: str,
    run_spec_name: str,
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
    fig, axes = plt.subplots(
        len(metrics),
        len(run_order),
        figsize=(5.0 * len(run_order), 3.2 * len(metrics)),
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
    fig.suptitle(
        'Per-Run Instance-Level Core Metric Score Distributions\n'
        f'Run Spec: {run_spec_name}\n'
        'Columns are kwdagger repeat A, kwdagger repeat B, and the official HELM run.',
        fontsize=16,
    )
    out_fpath = fig_dpath / f'core_metric_three_run_distributions_{stamp}.png'
    fig.savefig(out_fpath, dpi=180)
    plt.close(fig)
    return out_fpath


def _plot_overlay_metric_distributions(
    fig_dpath: Path,
    stamp: str,
    kwdagger_a_run: str,
    kwdagger_b_run: str,
    official_run: str,
    run_spec_name: str,
) -> Path | None:
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
    )


def _plot_overlay_metric_ecdfs(
    fig_dpath: Path,
    stamp: str,
    kwdagger_a_run: str,
    kwdagger_b_run: str,
    official_run: str,
    run_spec_name: str,
) -> Path | None:
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
    )


def _single_run_core_stat_index(run_path: str) -> dict[str, Any]:
    ana = HelmRunAnalysis(HelmRun.coerce(run_path))
    idx = ana.stat_index(drop_zero_count=True, require_mean=True)
    return {k: v for k, v in idx.items() if v.metric_class == 'core'}


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
    csv_fpath = out_dpath / f'core_runlevel_table_{stamp}.csv'
    md_fpath = out_dpath / f'core_runlevel_table_{stamp}.md'
    table.to_csv(csv_fpath, index=False)
    try:
        md_fpath.write_text(table.to_markdown(index=False) + '\n')
    except ImportError:
        md_fpath = None
    return csv_fpath, md_fpath


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
    csv_fpath = out_dpath / f'core_runlevel_table_{stamp}.csv'
    md_fpath = out_dpath / f'core_runlevel_table_{stamp}.md'
    table.to_csv(csv_fpath, index=False)
    try:
        md_fpath.write_text(table.to_markdown(index=False) + '\n')
    except ImportError:
        md_fpath = None
    return csv_fpath, md_fpath


def _plot_single_pair_summary(
    fig_dpath: Path,
    stamp: str,
    pair: dict[str, Any],
    run_spec_name: str,
) -> Path:
    sns.set_theme(style='whitegrid', context='talk')
    fig, axes = plt.subplots(1, 2, figsize=(18, 7.5), constrained_layout=True)
    quantiles = pair['instance_level']['overall_quantiles']['abs_delta']
    labels = ['p50', 'p90', 'p95', 'p99', 'max']
    axes[0].plot(range(len(labels)), [quantiles[k] for k in labels], marker='o', color='#4C72B0')
    axes[0].set_xticks(range(len(labels)), labels)
    axes[0].set_yscale('symlog', linthresh=1e-12)
    axes[0].set_title('Official vs Local Instance-Level Delta Quantiles')
    axes[0].set_xlabel('Quantile')
    axes[0].set_ylabel('Absolute Difference in Core Metric Value')
    _plot_distribution(axes[1], pair, level_key='instance_level')
    axes[1].set_title('Official vs Local Agreement vs Tolerance')
    fig.suptitle(
        'Core Metric Agreement and Difference Summary\n'
        f'Run Spec: {run_spec_name}\n'
        f'Pair: {pair["label"]}\n'
        f'Instance-level N: {pair["instance_level"]["n_rows"]}',
        fontsize=15,
    )
    fig_fpath = fig_dpath / f'core_metric_report_{stamp}.png'
    fig.savefig(fig_fpath, dpi=180)
    plt.close(fig)
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
    run_spec_fpath = Path(component['run_path']) / 'run_spec.json'
    if not run_spec_fpath.exists():
        return None
    try:
        data = json.loads(run_spec_fpath.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _component_spec_metadata(component: dict[str, Any]) -> dict[str, Any]:
    run_spec = _load_run_spec_json(component) or {}
    fields = extract_run_spec_fields(Path(component['run_path']) / 'run_spec.json')
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


def _write_comparison_runlevel_table(
    out_dpath: Path,
    stamp: str,
    comparisons: list[dict[str, Any]],
    component_lookup: dict[str, dict[str, Any]],
) -> tuple[Path, Path | None]:
    rows = []
    for comparison in comparisons:
        component_ids = comparison.get('component_ids') or []
        if len(component_ids) != 2:
            continue
        left_component = component_lookup[component_ids[0]]
        right_component = component_lookup[component_ids[1]]
        idx_left = _single_run_core_stat_index(left_component['run_path'])
        idx_right = _single_run_core_stat_index(right_component['run_path'])
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
    csv_fpath = out_dpath / f'core_runlevel_table_{stamp}.csv'
    md_fpath = out_dpath / f'core_runlevel_table_{stamp}.md'
    table.to_csv(csv_fpath, index=False)
    try:
        md_fpath.write_text(table.to_markdown(index=False) + '\n')
    except ImportError:
        md_fpath = None
    return csv_fpath, md_fpath


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
    lines.append('selected_components:')
    for component in report.get('components', []):
        lines.append(
            f"  - {component['component_id']}: tags={component.get('tags', [])} "
            f"run_path={component.get('run_path')}"
        )
    lines.append('')
    lines.append('comparisons:')
    for comparison in report.get('comparisons', []):
        lines.append(
            f"  - {comparison['comparison_id']}: kind={comparison.get('comparison_kind')} "
            f"enabled={comparison.get('enabled')} component_ids={comparison.get('component_ids')}"
        )
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
    out_fpath.write_text('\n'.join(lines) + '\n')


def _find_curve_value(rows: list[dict[str, Any]], abs_tol: float) -> float | None:
    for row in rows:
        if float(row.get('abs_tol', float('nan'))) == float(abs_tol):
            return row.get('agree_ratio')
    return None


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
    lines.append('selected_components:')
    for component in report.get('components', []):
        lines.append(
            f"  - {component['component_id']}: tags={component.get('tags', [])} "
            f"run_path={component.get('run_path')}"
        )
    lines.append('')
    lines.append('comparisons:')
    for comparison in report.get('comparisons', []):
        lines.append(
            f"  - {comparison['comparison_id']}: kind={comparison.get('comparison_kind')} "
            f"enabled={comparison.get('enabled')} component_ids={comparison.get('component_ids')}"
        )
    lines.append('')
    lines.append('comparability:')
    for fact_name, fact in (report.get('comparability') or {}).get('facts', {}).items():
        lines.append(f"  {fact_name}: {fact.get('status')} values={fact.get('values')}")
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
    out_fpath.write_text('\n'.join(lines) + '\n')


def _write_latest_alias(src: Path | None, latest_root: Path, latest_name: str) -> Path | None:
    if src is None:
        return None
    latest_fpath = latest_root / latest_name
    if latest_fpath.exists() or latest_fpath.is_symlink():
        latest_fpath.unlink()
    rel_src = os.path.relpath(src, start=latest_fpath.parent)
    os.symlink(rel_src, latest_fpath)
    return latest_fpath


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-dpath', required=True)
    parser.add_argument('--components-manifest', default=None)
    parser.add_argument('--comparisons-manifest', default=None)
    args = parser.parse_args(argv)

    thresholds = [0.0, 1e-12, 1e-9, 1e-6, 1e-4, 1e-3, 1e-2, 2e-2, 5e-2, 1e-1, 2.5e-1, 5e-1, 1.0]
    report_dpath = Path(args.report_dpath).expanduser().resolve()
    report_dpath.mkdir(parents=True, exist_ok=True)
    stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime('%Y%m%dT%H%M%SZ')
    history_dpath = report_dpath / '.history' / stamp[:8]
    history_dpath.mkdir(parents=True, exist_ok=True)
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
    comparisons = [comparison for comparison in (comparisons_manifest.get('comparisons') or []) if comparison.get('enabled', True)]
    component_lookup = {component['component_id']: component for component in components}
    run_spec_name = _infer_run_spec_name(*(component['run_path'] for component in components))

    pairs = []
    for comparison in comparisons:
        component_ids = comparison.get('component_ids') or []
        if len(component_ids) != 2:
            continue
        run_a = component_lookup[component_ids[0]]['run_path']
        run_b = component_lookup[component_ids[1]]['run_path']
        pair = _build_pair(run_a, run_b, str(comparison['comparison_id']), thresholds)
        pair['comparison_id'] = comparison['comparison_id']
        pair['comparison_kind'] = comparison.get('comparison_kind')
        pair['component_ids'] = component_ids
        pair['reference_component_id'] = comparison.get('reference_component_id')
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
    comparability = _comparability_summary(components)

    report = {
        'generated_utc': stamp,
        'run_spec_name': run_spec_name,
        'report_dpath': str(report_dpath),
        'components_manifest_path': str(components_manifest_fpath),
        'comparisons_manifest_path': str(comparisons_manifest_fpath),
        'thresholds': thresholds,
        'components': components,
        'comparisons': comparisons,
        'pairs': pairs,
        'run_diagnostics': run_diagnostics,
        'diagnostic_flags': _diagnostic_flags(run_diagnostics, components, comparisons),
        'single_run_mode': single_run_mode,
        'comparability': comparability,
    }

    json_fpath = history_dpath / f'core_metric_report_{stamp}.json'
    txt_fpath = history_dpath / f'core_metric_report_{stamp}.txt'
    mgmt_fpath = history_dpath / f'core_metric_management_summary_{stamp}.txt'
    official_vs_local = _find_pair(report, 'official_vs_local') or (pairs[-1] if pairs else None)
    local_repeat = _find_pair(report, 'local_repeat')

    if official_vs_local is None:
        raise SystemExit('No enabled comparisons were available to render a core metric report')

    if len(pairs) == 1:
        fig_fpath = _plot_single_pair_summary(history_dpath, stamp, official_vs_local, run_spec_name)
    else:
        fig_fpath = history_dpath / f'core_metric_report_{stamp}.png'
        extra_pair = _load_optional_cross_machine_pair(report_dpath)
        paper_labels = load_paper_label_manager(style='paper_short')
        extra_label = extra_pair['label'] if extra_pair is not None else None
        pair_line = 'Pairs: ' + ' vs '.join(pair['comparison_id'] for pair in pairs)
        if extra_label is not None:
            pair_line += f' + {extra_label}'
        pair_line = paper_labels.relabel_text(pair_line)
        sns.set_theme(style='whitegrid', context='talk')
        fig, axes = plt.subplots(2, 2, figsize=(24, 14.5), constrained_layout=True)
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
        _plot_distribution(axes[1, 0], *(pairs + ([extra_pair] if extra_pair is not None else [])), level_key='run_level')
        axes[1, 0].set_title('Run-Level Agreement vs Tolerance', fontsize=11)
        _plot_distribution(axes[1, 1], *(pairs + ([extra_pair] if extra_pair is not None else [])), level_key='instance_level')
        axes[1, 1].set_title('Instance-Level Agreement vs Tolerance', fontsize=11)
        axes[0, 0].title.set_fontsize(11)
        axes[0, 1].title.set_fontsize(11)
        fig.suptitle(
            'Core Metric Agreement and Difference Summary\n'
            f'Run Spec: {run_spec_name}\n'
            f'{pair_line}',
            fontsize=15,
        )
        fig.savefig(fig_fpath, dpi=180)
        plt.close(fig)

    dist_fig_fpath = _plot_pair_metric_distributions(history_dpath, stamp, pairs, run_spec_name)
    run_specs = [(component['run_path'], component['display_name']) for component in components]
    overlay_dist_fpath = _plot_run_metric_distributions(
        history_dpath,
        stamp,
        run_specs,
        run_spec_name,
        out_name='core_metric_overlay_distributions',
        title='Overlay of Per-Instance Core Metric Score Distributions by Component',
        subtitle='Each series comes from a selected report component declared in the components manifest.',
    )
    ecdf_fig_fpath = _plot_run_metric_distributions(
        history_dpath,
        stamp,
        run_specs,
        run_spec_name,
        out_name='core_metric_ecdfs',
        title='ECDF of Per-Instance Core Metric Scores by Component',
        subtitle='Each series comes from a selected report component declared in the components manifest.',
        ecdf=True,
    )
    per_metric_agree_fpath = _plot_per_metric_agreement(
        history_dpath,
        stamp,
        *pairs,
        level_key='instance_level',
        thresholds=thresholds,
    )
    runlevel_csv_fpath, runlevel_md_fpath = _write_comparison_runlevel_table(
        history_dpath,
        stamp,
        comparisons,
        component_lookup,
    )

    report = kwutil.Json.ensure_serializable(_strip_private(report))
    json_fpath.write_text(json.dumps(report, indent=2))
    _write_text(report, txt_fpath)
    _write_management_summary(report, mgmt_fpath)

    latest_map = {
        json_fpath: 'core_metric_report.latest.json',
        txt_fpath: 'core_metric_report.latest.txt',
        mgmt_fpath: 'core_metric_management_summary.latest.txt',
        fig_fpath: 'core_metric_report.latest.png',
        runlevel_csv_fpath: 'core_runlevel_table.latest.csv',
    }
    if dist_fig_fpath is not None:
        latest_map[dist_fig_fpath] = 'core_metric_distributions.latest.png'
    if overlay_dist_fpath is not None:
        latest_map[overlay_dist_fpath] = 'core_metric_overlay_distributions.latest.png'
    if ecdf_fig_fpath is not None:
        latest_map[ecdf_fig_fpath] = 'core_metric_ecdfs.latest.png'
    if runlevel_md_fpath is not None:
        latest_map[runlevel_md_fpath] = 'core_runlevel_table.latest.md'
    if per_metric_agree_fpath is not None:
        latest_map[per_metric_agree_fpath] = 'core_metric_per_metric_agreement.latest.png'
    for src, latest_name in latest_map.items():
        _write_latest_alias(src, report_dpath, latest_name)
    known_latest_names = {
        'core_metric_report.latest.json',
        'core_metric_report.latest.txt',
        'core_metric_management_summary.latest.txt',
        'core_metric_report.latest.png',
        'core_metric_distributions.latest.png',
        'core_metric_three_run_distributions.latest.png',
        'core_metric_overlay_distributions.latest.png',
        'core_metric_ecdfs.latest.png',
        'core_metric_per_metric_agreement.latest.png',
        'core_runlevel_table.latest.csv',
        'core_runlevel_table.latest.md',
    }
    for latest_name in known_latest_names - set(latest_map.values()):
        safe_unlink(report_dpath / latest_name)

    print(f'Wrote core metric report: {json_fpath}')
    print(f'Wrote core metric text: {txt_fpath}')
    print(f'Wrote core metric management summary: {mgmt_fpath}')
    print(f'Wrote core metric plot: {fig_fpath}')
    if dist_fig_fpath is not None:
        print(f'Wrote core metric distributions: {dist_fig_fpath}')
    if overlay_dist_fpath is not None:
        print(f'Wrote core metric overlay distributions: {overlay_dist_fpath}')
    if ecdf_fig_fpath is not None:
        print(f'Wrote core metric ecdfs: {ecdf_fig_fpath}')
    if per_metric_agree_fpath is not None:
        print(f'Wrote per-metric agreement curves: {per_metric_agree_fpath}')
    print(f'Wrote core run-level table csv: {runlevel_csv_fpath}')
    print(f'Wrote core run-level table md: {runlevel_md_fpath}')


if __name__ == '__main__':
    setup_cli_logging()
    main()
