from __future__ import annotations

import argparse
import datetime as datetime_mod
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import kwutil

from helm_audit.cli.index_historic_helm_runs import CLOSED_JUDGE_REQUIRED_REASON
from helm_audit.infra.api import audit_root
from helm_audit.infra.fs_publish import history_publish_root, write_latest_alias
from helm_audit.infra.report_layout import filtering_reports_root, portable_repo_root_lines, write_reproduce_script
from helm_audit.infra.plotly_env import configure_plotly_chrome
from helm_audit.utils.sankey import emit_sankey_artifacts
from loguru import logger



UNCLASSIFIED_EXCLUSION = 'unclassified-exclusion'


def _title_with_n(title: str, n: int) -> str:
    return f'{title} n={n}'


_AXIS_COUNT_TAGS = {
    'model': 'n_models',
    'benchmark': 'n_benchmarks',
    'dataset': 'n_datasets',
    'scenario': 'n_scenarios',
    'failure_reason': 'n_failure_reasons',
    'candidate_pool': 'n_candidate_pools',
    'reason_combo': 'n_reason_combos',
}


def _bar_count_label(axis_key: str, n_bars: int, *, axis_title: str | None = None) -> str:
    label = axis_title if axis_title is not None else axis_key.replace('_', ' ').title()
    count_tag = _AXIS_COUNT_TAGS.get(axis_key, 'n_categories')
    return f'{label} ({count_tag}={n_bars}, n_bars={n_bars})'


def _bar_axis_values(rows: list[dict[str, Any]], x: str) -> list[str]:
    unique_x = []
    seen = set()
    for row in rows:
        value = str(row.get(x) or 'unknown')
        if value not in seen:
            seen.add(value)
            unique_x.append(value)
    return unique_x


def _abbreviate_label(text: str, *, max_chars: int = 24) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return '.' * max_chars
    return text[: max_chars - 3].rstrip() + '...'


def _bar_chart_layout(rows: list[dict[str, Any]], x: str, *, compact: bool = False) -> dict[str, Any]:
    unique_x = _bar_axis_values(rows, x)
    longest_label = max((len(value) for value in unique_x), default=0)
    if compact:
        n_bars = max(len(unique_x), 1)
        height = min(max(520, 14 * n_bars + 240), 1000)
        width = min(max(1100, 36 * n_bars, 14 * longest_label * n_bars), 1600)
        return {
            'width': width,
            'height': height,
            'margin': {'b': min(max(120, 8 * longest_label), 220), 't': 80, 'l': 70, 'r': 30},
        }
    height = max(650, 24 * max(len(unique_x), 1) + 260)
    width = max(1400, 120 * max(len(unique_x), 1), 18 * longest_label * max(len(unique_x), 1))
    max_width = int(height * 2.5)
    width = min(width, max_width)
    return {
        'width': width,
        'height': height,
        'margin': {'b': max(180, 12 * longest_label)},
    }


def _bar_chart_xaxis_update(
    rows: list[dict[str, Any]],
    *,
    x: str,
    xaxis_title: str | None,
    compact: bool,
) -> dict[str, Any]:
    unique_x = _bar_axis_values(rows, x)
    n_bars = len(unique_x)
    title_text = _bar_count_label(x, n_bars, axis_title=xaxis_title)
    if not compact:
        return {
            'title_text': title_text,
            'tickangle': -45,
            'automargin': True,
        }
    if n_bars > 50:
        tickangle = 90
        tickfont_size = 8
    elif n_bars > 25:
        tickangle = 75
        tickfont_size = 8
    elif n_bars > 12:
        tickangle = 60
        tickfont_size = 9
    else:
        tickangle = -45
        tickfont_size = 10
    return {
        'title_text': title_text,
        'tickmode': 'array',
        'tickvals': unique_x,
        'ticktext': [_abbreviate_label(value) for value in unique_x],
        'tickangle': tickangle,
        'tickfont': {'size': tickfont_size},
        'automargin': True,
    }


def summarize_inventory(inventory_rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_rows = len(inventory_rows)
    selected_rows = [row for row in inventory_rows if row.get('selection_status') == 'selected']
    excluded_rows = [row for row in inventory_rows if row.get('selection_status') != 'selected']
    considered_rows = [row for row in inventory_rows if row.get('considered_for_selection')]
    eligible_rows = [row for row in inventory_rows if row.get('eligible_candidate')]
    structurally_incomplete = [row for row in inventory_rows if row.get('is_structurally_incomplete')]
    unique_selected_models = sorted({row.get('model') for row in selected_rows if row.get('model')})

    exclusion_counts: dict[str, int] = {}
    for row in excluded_rows:
        reasons = row.get('failure_reasons', []) or []
        if not reasons:
            reasons = [UNCLASSIFIED_EXCLUSION]
        for reason in reasons:
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
    exclusion_counts = dict(sorted(exclusion_counts.items(), key=lambda item: (-item[1], item[0])))

    def frac(num: int, den: int) -> float | None:
        return None if den == 0 else num / den

    return {
        'total_discovered_runs': total_rows,
        'considered_runs': len(considered_rows),
        'eligible_runs': len(eligible_rows),
        'selected_runs': len(selected_rows),
        'excluded_runs': len(excluded_rows),
        'structurally_incomplete_runs': len(structurally_incomplete),
        'selected_models': len(unique_selected_models),
        'selected_model_names': unique_selected_models,
        'fraction_considered_of_all': frac(len(considered_rows), total_rows),
        'fraction_eligible_of_all': frac(len(eligible_rows), total_rows),
        'fraction_selected_of_all': frac(len(selected_rows), total_rows),
        'fraction_selected_of_considered': frac(len(selected_rows), len(considered_rows)),
        'fraction_selected_of_eligible': frac(len(selected_rows), len(eligible_rows)),
        'exclusion_reason_counts': exclusion_counts,
    }


def make_count_table(
    inventory_rows: list[dict[str, Any]],
    *,
    facet_key: str,
) -> list[dict[str, Any]]:
    facet_values = sorted({row.get(facet_key) or 'unknown' for row in inventory_rows})
    out = []
    for facet in facet_values:
        rows = [row for row in inventory_rows if (row.get(facet_key) or 'unknown') == facet]
        total_runs = len(rows)
        considered_runs = sum(1 for row in rows if row.get('considered_for_selection'))
        eligible_runs = sum(1 for row in rows if row.get('eligible_candidate'))
        selected_runs = sum(1 for row in rows if row.get('selection_status') == 'selected')
        excluded_runs = total_runs - selected_runs
        reasons: dict[str, int] = {}
        for row in rows:
            row_reasons = row.get('failure_reasons', []) or []
            if row.get('selection_status') != 'selected' and not row_reasons:
                row_reasons = [UNCLASSIFIED_EXCLUSION]
            for reason in row_reasons:
                reasons[reason] = reasons.get(reason, 0) + 1
        top_reason = None
        top_reason_count = 0
        if reasons:
            top_reason = min(
                [(-count, reason) for reason, count in reasons.items()]
            )[1]
            top_reason_count = reasons[top_reason]
        out.append({
            facet_key: facet,
            'total_runs': total_runs,
            'considered_runs': considered_runs,
            'eligible_runs': eligible_runs,
            'selected_runs': selected_runs,
            'excluded_runs': excluded_runs,
            'fraction_considered_of_all': None if total_runs == 0 else considered_runs / total_runs,
            'fraction_selected_of_all': None if total_runs == 0 else selected_runs / total_runs,
            'fraction_selected_of_considered': None if considered_runs == 0 else selected_runs / considered_runs,
            'fraction_selected_of_eligible': None if eligible_runs == 0 else selected_runs / eligible_runs,
            'top_exclusion_reason': top_reason,
            'top_exclusion_reason_count': top_reason_count,
        })
    out.sort(key=lambda row: (-row['total_runs'], -row['selected_runs'], str(row[facet_key])))
    return out


def make_reason_breakout_table(inventory_rows: list[dict[str, Any]], facet_key: str) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for row in inventory_rows:
        facet_value = row.get(facet_key) or 'unknown'
        reasons = row.get('failure_reasons', []) or []
        if row.get('selection_status') != 'selected' and not reasons:
            reasons = [UNCLASSIFIED_EXCLUSION]
        for reason in reasons:
            counts[(facet_value, reason)] = counts.get((facet_value, reason), 0) + 1
    rows = [
        {facet_key: facet, 'failure_reason': reason, 'run_count': count}
        for (facet, reason), count in counts.items()
    ]
    rows.sort(key=lambda row: (-row['run_count'], str(row[facet_key]), row['failure_reason']))
    return rows


def make_open_access_exclusion_reason_table(inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    open_access_rows = [
        row for row in inventory_rows
        if row.get('model_access') == 'open' and row.get('selection_status') != 'selected'
    ]
    counts: dict[str, int] = {}
    for row in open_access_rows:
        reasons = row.get('failure_reasons', []) or []
        if not reasons:
            reasons = [UNCLASSIFIED_EXCLUSION]
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    rows = [{'failure_reason': reason, 'run_count': count} for reason, count in counts.items()]
    rows.sort(key=lambda row: (-row['run_count'], row['failure_reason']))
    return rows


def make_reason_combo_breakout_table(
    inventory_rows: list[dict[str, Any]],
    facet_key: str,
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for row in inventory_rows:
        facet_value = row.get(facet_key) or 'unknown'
        reasons = row.get('failure_reasons', []) or []
        if row.get('selection_status') != 'selected' and not reasons:
            reasons = [UNCLASSIFIED_EXCLUSION]
        combo = 'selected' if row.get('selection_status') == 'selected' else '|'.join(sorted({str(reason) for reason in reasons}))
        counts[(facet_value, combo)] = counts.get((facet_value, combo), 0) + 1
    rows = [
        {facet_key: facet, 'reason_combo': combo, 'run_count': count}
        for (facet, combo), count in counts.items()
    ]
    rows.sort(key=lambda row: (-row['run_count'], str(row[facet_key]), row['reason_combo']))
    return rows


def make_open_access_exclusion_reason_by_model_table(
    inventory_rows: list[dict[str, Any]],
    *,
    excluded_reasons: set[str] | None = None,
) -> list[dict[str, Any]]:
    open_access_rows = [
        row for row in inventory_rows
        if row.get('model_access') == 'open' and row.get('selection_status') != 'selected'
    ]
    if excluded_reasons:
        open_access_rows = [
            row for row in open_access_rows
            if not (set(row.get('failure_reasons', []) or []) & excluded_reasons)
        ]
    return make_reason_combo_breakout_table(open_access_rows, 'model')


def make_reason_combo_table(inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    examples: dict[str, dict[str, Any]] = {}
    for row in inventory_rows:
        reasons = row.get('failure_reasons', []) or []
        if row.get('selection_status') != 'selected' and not reasons:
            reasons = [UNCLASSIFIED_EXCLUSION]
        combo = 'selected' if row.get('selection_status') == 'selected' else '|'.join(sorted(reasons))
        counts[combo] = counts.get(combo, 0) + 1
        examples.setdefault(combo, {
            'example_run_spec_name': row.get('run_spec_name'),
            'example_model': row.get('model'),
            'example_benchmark': row.get('benchmark'),
        })
    rows = []
    for combo, count in counts.items():
        rows.append({
            'reason_combo': combo,
            'run_count': count,
            **examples[combo],
        })
    rows.sort(key=lambda row: (-row['run_count'], row['reason_combo']))
    return rows


def make_candidate_pool_table(inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pools = sorted({row.get('candidate_pool') or 'unknown' for row in inventory_rows})
    total = len(inventory_rows)
    rows = []
    for pool in pools:
        pool_rows = [row for row in inventory_rows if (row.get('candidate_pool') or 'unknown') == pool]
        selected_runs = sum(1 for row in pool_rows if row.get('selection_status') == 'selected')
        excluded_runs = len(pool_rows) - selected_runs
        rows.append({
            'candidate_pool': pool,
            'run_count': len(pool_rows),
            'selected_runs': selected_runs,
            'excluded_runs': excluded_runs,
            'fraction_of_all_runs': None if total == 0 else len(pool_rows) / total,
        })
    rows.sort(key=lambda row: (-row['run_count'], row['candidate_pool']))
    return rows


def make_selection_path_table(inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table: dict[tuple[str, str], int] = {}
    total = len(inventory_rows)
    for row in inventory_rows:
        key = ((row.get('candidate_pool') or 'unknown'), row.get('selection_status') or 'unknown')
        table[key] = table.get(key, 0) + 1
    rows = []
    for (pool, status), count in sorted(table.items(), key=lambda item: (-item[1], item[0][0], item[0][1])):
        rows.append({
            'candidate_pool': pool,
            'selection_status': status,
            'run_count': count,
            'fraction_of_all_runs': None if total == 0 else count / total,
        })
    return rows


def make_pair_table(
    inventory_rows: list[dict[str, Any]],
    left_key: str,
    right_key: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for row in inventory_rows:
        left = row.get(left_key) or 'unknown'
        right = row.get(right_key) or 'unknown'
        bucket = counts.setdefault((left, right), {
            'total_runs': 0,
            'selected_runs': 0,
            'considered_runs': 0,
            'eligible_runs': 0,
        })
        bucket['total_runs'] += 1
        bucket['selected_runs'] += int(row.get('selection_status') == 'selected')
        bucket['considered_runs'] += int(bool(row.get('considered_for_selection')))
        bucket['eligible_runs'] += int(bool(row.get('eligible_candidate')))
    rows = []
    for (left, right), vals in counts.items():
        rows.append({
            left_key: left,
            right_key: right,
            **vals,
            'excluded_runs': vals['total_runs'] - vals['selected_runs'],
            'fraction_selected_of_all': None if vals['total_runs'] == 0 else vals['selected_runs'] / vals['total_runs'],
            'fraction_selected_of_considered': None if vals['considered_runs'] == 0 else vals['selected_runs'] / vals['considered_runs'],
        })
    rows.sort(key=lambda row: (-row['total_runs'], -row['selected_runs'], str(row[left_key]), str(row[right_key])))
    return rows[:limit]


def make_reason_examples_table(inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    examples: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in inventory_rows:
        reasons = row.get('failure_reasons', []) or []
        if row.get('selection_status') != 'selected' and not reasons:
            reasons = [UNCLASSIFIED_EXCLUSION]
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
            examples.setdefault(reason, {
                'failure_reason': reason,
                'run_spec_name': row.get('run_spec_name'),
                'model': row.get('model'),
                'benchmark': row.get('benchmark'),
                'dataset': row.get('dataset'),
                'scenario': row.get('scenario'),
                'selection_explanation': row.get('selection_explanation'),
            })
    rows = []
    for reason, payload in examples.items():
        rows.append({
            'failure_reason': reason,
            'run_count': counts.get(reason, 0),
            **payload,
        })
    rows.sort(key=lambda row: (-row['run_count'], row['failure_reason']))
    return rows


def classify_hierarchical_filter_stages(row: dict[str, Any]) -> dict[str, str]:
    if row.get('is_structurally_incomplete'):
        return {
            'structural_stage': 'excluded: structurally incomplete',
            'access_stage': 'stopped before access check',
            'tag_stage': 'stopped before tag check',
            'deployment_stage': 'stopped before deployment check',
            'size_stage': 'stopped before size check',
            'judge_stage': 'stopped before judge check',
            'outcome_stage': 'excluded before candidate pool',
        }

    reasons = set(row.get('failure_reasons', []) or [])
    access_ok = 'not-open-access' not in reasons
    tag_ok = ('excluded-tags' not in reasons) and ('not-text-like' not in reasons)
    deployment_ok = 'no-local-helm-deployment' not in reasons
    size_ok = 'too-large' not in reasons
    judge_ok = CLOSED_JUDGE_REQUIRED_REASON not in reasons
    selected = row.get('selection_status') == 'selected'

    if not access_ok:
        return {
            'structural_stage': 'passed structural completeness',
            'access_stage': 'excluded: not open weight',
            'tag_stage': 'stopped after access exclusion',
            'deployment_stage': 'stopped after access exclusion',
            'size_stage': 'stopped after access exclusion',
            'judge_stage': 'stopped after access exclusion',
            'outcome_stage': 'excluded at open-weight gate',
        }
    if not tag_ok:
        return {
            'structural_stage': 'passed structural completeness',
            'access_stage': 'kept: open weight',
            'tag_stage': 'excluded: unsuitable text/modality tags',
            'deployment_stage': 'stopped after tag exclusion',
            'size_stage': 'stopped after tag exclusion',
            'judge_stage': 'stopped after tag exclusion',
            'outcome_stage': 'excluded at tag gate',
        }
    if not deployment_ok:
        return {
            'structural_stage': 'passed structural completeness',
            'access_stage': 'kept: open weight',
            'tag_stage': 'kept: suitable text tags',
            'deployment_stage': 'excluded: no runnable local deployment',
            'size_stage': 'stopped after deployment exclusion',
            'judge_stage': 'stopped after deployment exclusion',
            'outcome_stage': 'excluded at deployment gate',
        }
    if not size_ok:
        size_text = row.get('failure_reason_details', {}).get('too-large', '')
        short_label = 'excluded: exceeds size budget'
        if size_text:
            short_label = (
                f"excluded: {format_size_label(row.get('model_num_parameters'), row.get('size_threshold_params'))}"
            )
        return {
            'structural_stage': 'passed structural completeness',
            'access_stage': 'kept: open weight',
            'tag_stage': 'kept: suitable text tags',
            'deployment_stage': 'kept: runnable local deployment',
            'size_stage': short_label,
            'judge_stage': 'stopped after size exclusion',
            'outcome_stage': 'excluded at size gate',
        }
    if not judge_ok:
        return {
            'structural_stage': 'passed structural completeness',
            'access_stage': 'kept: open weight',
            'tag_stage': 'kept: suitable text tags',
            'deployment_stage': 'kept: runnable local deployment',
            'size_stage': 'kept: within size budget',
            'judge_stage': 'excluded: requires closed-source judge',
            'outcome_stage': 'excluded at judge gate',
        }
    if not selected:
        return {
            'structural_stage': 'passed structural completeness',
            'access_stage': 'kept: open weight',
            'tag_stage': 'kept: suitable text tags',
            'deployment_stage': 'kept: runnable local deployment',
            'size_stage': 'kept: within size budget',
            'judge_stage': 'kept: no closed-source judge dependency',
            'outcome_stage': 'excluded after explicit gates (unclassified)',
        }
    return {
        'structural_stage': 'passed structural completeness',
        'access_stage': 'kept: open weight',
        'tag_stage': 'kept: suitable text tags',
        'deployment_stage': 'kept: runnable local deployment',
        'size_stage': 'kept: within size budget',
        'judge_stage': 'kept: no closed-source judge dependency',
        'outcome_stage': 'selected for reproduction',
    }


def build_hierarchical_sankey_rows(inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [classify_hierarchical_filter_stages(row) for row in inventory_rows]


def format_size_label(num_params: Any, threshold: Any) -> str:
    def _fmt(val: Any) -> str:
        if val is None:
            return 'unknown'
        val = float(val)
        if val >= 1e9:
            return f'{val / 1e9:.1f}B'
        if val >= 1e6:
            return f'{val / 1e6:.1f}M'
        return str(int(val))
    return f"too large ({_fmt(num_params)} > {_fmt(threshold)})"


def build_hierarchical_sankey_key(summary: dict[str, Any]) -> dict[str, list[str]]:
    return {
        'Structural Gate': [
            f"excluded: structurally incomplete ({summary['structurally_incomplete_runs']} runs)",
            'passed structural completeness: run had enough HELM files to enter model filtering',
        ],
        'Open-Weight Gate': [
            'excluded: not open weight: HELM access is not "open"',
            'kept: open weight: passes the open-access requirement',
            'stopped before access check: eliminated at an earlier gate',
        ],
        'Tag Gate': [
            'excluded: unsuitable text/modality tags: model is not in the text-only reproducibility target',
            'kept: suitable text tags: passes the text-like and excluded-tag checks',
            'stopped after access exclusion: excluded earlier, so no tag decision was needed',
        ],
        'Deployment Gate': [
            'excluded: no runnable local deployment: no HuggingFace/local deployment path available',
            'kept: runnable local deployment: model has a local deployment path or explicit override',
            'stopped after tag exclusion: excluded earlier, so no deployment decision was needed',
        ],
        'Size Gate': [
            'excluded: ... exceeds the local reproduction budget: parameter count is above the configured threshold',
            'kept: within size budget: passes the size budget gate',
            'stopped after deployment exclusion: excluded earlier, so no size decision was needed',
        ],
        'Judge Gate': [
            'excluded: requires closed-source judge: benchmark depends on a proprietary / credentialed judge or annotator',
            'kept: no closed-source judge dependency: benchmark stays within the current open-model reproduction scope',
            'stopped before judge check / stopped after access exclusion / stopped after tag exclusion / stopped after deployment exclusion / stopped after size exclusion',
        ],
        'Outcome': [
            'selected for reproduction: run survives every gate and is included in the output run list',
            'excluded before candidate pool / at open-weight gate / at tag gate / at deployment gate / at size gate / at judge gate / after explicit gates (unclassified)',
        ],
    }


def make_decision_examples(inventory_rows: list[dict[str, Any]], limit: int = 30) -> dict[str, list[dict[str, Any]]]:
    selected = []
    excluded = []
    for row in inventory_rows:
        payload = {
            'run_spec_name': row.get('run_spec_name'),
            'model': row.get('model'),
            'benchmark': row.get('benchmark'),
            'dataset': row.get('dataset'),
            'scenario': row.get('scenario'),
            'selection_status': row.get('selection_status'),
            'failure_reasons': row.get('failure_reasons'),
            'selection_explanation': row.get('selection_explanation'),
        }
        if row.get('selection_status') == 'selected':
            if len(selected) < limit:
                selected.append(payload)
        else:
            if len(excluded) < limit:
                excluded.append(payload)
    return {'selected_examples': selected, 'excluded_examples': excluded}


def to_tsv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '\n'
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    lines = ['\t'.join(columns)]
    for row in rows:
        parts = []
        for col in columns:
            value = row.get(col, '')
            if isinstance(value, (list, dict)):
                value = json.dumps(value, sort_keys=True)
            parts.append(str(value))
        lines.append('\t'.join(parts))
    return '\n'.join(lines) + '\n'


def to_markdown(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '(no rows)\n'
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    str_rows = []
    for row in rows:
        str_rows.append([str(row.get(col, '')) for col in columns])
    widths = []
    for idx, col in enumerate(columns):
        widths.append(max(len(col), *(len(r[idx]) for r in str_rows)))

    def fmt(cells: list[str]) -> str:
        return '| ' + ' | '.join(cell.ljust(widths[idx]) for idx, cell in enumerate(cells)) + ' |'

    lines = [
        fmt(columns),
        '| ' + ' | '.join('-' * width for width in widths) + ' |',
    ]
    for row in str_rows:
        lines.append(fmt(row))
    return '\n'.join(lines) + '\n'


def _write_stamped_text(report_root: Path, root: Path, stem: str, stamp: str, suffix: str, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    history_root = history_publish_root(report_root, root, stamp)
    fpath = history_root / f'{stem}_{stamp}{suffix}'
    logger.debug(f'Write to: {fpath}')
    fpath.write_text(text)
    write_latest_alias(fpath, root, f'{stem}.latest{suffix}')
    return fpath


def _write_stamped_json(report_root: Path, root: Path, stem: str, stamp: str, payload: Any) -> Path:
    text = json.dumps(kwutil.Json.ensure_serializable(payload), indent=2, ensure_ascii=False, default=str) + '\n'
    return _write_stamped_text(report_root, root, stem, stamp, '.json', text)


def _write_stamped_table(report_root: Path, root: Path, stem: str, stamp: str, rows: list[dict[str, Any]]) -> Path:
    return _write_stamped_text(report_root, root, stem, stamp, '.tsv', to_tsv(rows))


def _shell_quote(parts: list[str]) -> str:
    return ' '.join(shlex.quote(part) for part in parts)


def write_filter_rebuild_script(report_dpath: Path, *, inventory_json: Path | None = None) -> Path:
    _ = inventory_json
    cmd = [
        '"${PYTHON_BIN}"',
        '-m',
        'helm_audit.cli.reports',
        'filter',
        '--report-dpath',
        '"${REPORT_DPATH}"',
        '--inventory-json',
        '"${REPORT_DPATH}/machine/model_filter_inventory.latest.json"',
    ]
    script = write_reproduce_script(report_dpath / 'rebuild_analysis.latest.sh', [
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        *portable_repo_root_lines(),
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'REPORT_DPATH="$SCRIPT_DIR"',
        'cd "$REPO_ROOT"',
        f'PYTHONPATH="$REPO_ROOT" {" ".join(cmd)} "$@"',
    ])
    write_latest_alias(script, report_dpath, 'rebuild_analysis.sh')
    return script


def write_filter_reproduce_script(report_dpath: Path, *, source_command: str | None = None) -> Path:
    lines = [
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        *portable_repo_root_lines(),
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        'REPORT_DPATH="$SCRIPT_DIR"',
        'cd "$REPO_ROOT"',
    ]
    if source_command:
        lines.extend([
            '',
            '# Re-run Stage 1 discovery/filtering and then rebuild the report bundle.',
            source_command,
        ])
    else:
        lines.extend([
            '',
            '# Rebuild the filter report bundle from the latest saved inventory.',
            'PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" -m helm_audit.cli.reports filter --report-dpath "$REPORT_DPATH" "$@"',
        ])
    script = write_reproduce_script(report_dpath / 'reproduce.latest.sh', lines)
    write_latest_alias(script, report_dpath, 'reproduce.sh')
    return script


def build_filter_cardinality_text(inventory_rows: list[dict[str, Any]]) -> str:
    def _card(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {
            'n': len(rows),
            'models': len({r.get('model') for r in rows if r.get('model')}),
            'benchmarks': len({r.get('benchmark') for r in rows if r.get('benchmark')}),
            'scenarios': len({r.get('scenario') for r in rows if r.get('scenario')}),
            'model_bench_pairs': len({(r.get('model'), r.get('benchmark')) for r in rows if r.get('model') and r.get('benchmark')}),
        }

    all_rows = inventory_rows
    considered_rows = [r for r in inventory_rows if r.get('considered_for_selection')]
    eligible_rows = [r for r in inventory_rows if r.get('eligible_candidate')]
    selected_rows = [r for r in inventory_rows if r.get('selection_status') == 'selected']

    header = f"{'Stage':<22} {'runs':>6}  {'models':>6}  {'benchmarks':>10}  {'scenarios':>9}  {'mod×bench':>9}"
    sep = '-' * len(header)

    def row_line(label: str, c: dict[str, int]) -> str:
        return (
            f"{label:<22} {c['n']:>6}  {c['models']:>6}  {c['benchmarks']:>10}"
            f"  {c['scenarios']:>9}  {c['model_bench_pairs']:>9}"
        )

    lines = [
        'Filter Stage Cardinality Summary',
        '================================',
        '',
        'Run-spec counts at each stage of the Stage 1 filter funnel.',
        '',
        header,
        sep,
        row_line('all_discovered', _card(all_rows)),
        row_line('considered', _card(considered_rows)),
        row_line('eligible', _card(eligible_rows)),
        row_line('selected', _card(selected_rows)),
        '',
        'Columns: runs = total run entries; models/benchmarks/scenarios = unique values;',
        '         mod×bench = unique (model, benchmark) pairs.',
        'Stages: all_discovered = every run seen; considered = passed initial checks;',
        '        eligible = passed all criteria; selected = chosen for reproduction.',
    ]
    return '\n'.join(lines) + '\n'


def build_local_serving_recovery_text(inventory_rows: list[dict[str, Any]]) -> str:
    """
    Partition models excluded by no-local-helm-deployment into:
      on-story  — public HELM model with a checked-in local serving recipe
      off-story — local extension not in the public HELM storyline
      no-plan   — not in the model registry; no known local serving path
    """
    NO_LOCAL = 'no-local-helm-deployment'
    deployment_excluded = [
        r for r in inventory_rows
        if NO_LOCAL in (r.get('failure_reasons') or [])
    ]
    seen: set[str] = set()
    model_rows: list[dict[str, Any]] = []
    for r in deployment_excluded:
        m = str(r.get('model') or 'unknown')
        if m not in seen:
            seen.add(m)
            model_rows.append(r)
    model_rows.sort(key=lambda r: str(r.get('model') or ''))

    on_story = [r for r in model_rows if r.get('replaces_helm_deployment') is not None]
    off_story = [r for r in model_rows if r.get('replaces_helm_deployment') is None and r.get('expected_local_served')]
    no_plan = [r for r in model_rows if not r.get('expected_local_served')]

    def _table(rows: list[dict[str, Any]]) -> list[str]:
        if not rows:
            return ['  (none)']
        out = []
        for r in rows:
            m = str(r.get('model') or 'unknown')
            src = str(r.get('local_registry_source') or '')
            repl = r.get('replaces_helm_deployment')
            suffix = f'  replaces={repl}' if repl else ''
            src_str = f'  source={src}' if src else ''
            out.append(f'  {m:<48}{src_str}{suffix}')
        return out

    lines: list[str] = [
        'Local Serving Recovery Summary',
        '==============================',
        '',
        'Models excluded by no-local-helm-deployment, by local serving plan.',
        '',
        f'  on-story  (public HELM model, local recipe exists): {len(on_story)}',
        f'  off-story (local extension, not in public HELM):    {len(off_story)}',
        f'  no-plan   (not in helm_audit model registry):       {len(no_plan)}',
        '',
    ]
    if on_story:
        lines += ['On-story models (in main reproducibility storyline):']
        lines += _table(on_story)
        lines += ['']
    if off_story:
        lines += ['Off-story models (local extensions, not in public HELM storyline):']
        lines += _table(off_story)
        lines += ['']
    if no_plan:
        lines += ['No local serving plan (not in helm_audit/model_registry.py):']
        lines += _table(no_plan)
        lines += ['']
    lines += [
        'Notes:',
        '  no-local-helm-deployment = Stage 1 automatic filter found no default local',
        '  HELM deployment path for this model. On-story models have a recipe in',
        '  helm_audit/model_registry.py and are run via a separate serving bundle.',
        '  TODO: Add runtime verification that vllm_service profiles can serve these.',
    ]
    return '\n'.join(lines) + '\n'


def build_filter_report_text(
    *,
    summary: dict[str, Any],
    by_model_rows: list[dict[str, Any]],
    by_dataset_rows: list[dict[str, Any]],
    by_scenario_rows: list[dict[str, Any]],
    reason_by_model_rows: list[dict[str, Any]],
    open_access_exclusion_reason_rows: list[dict[str, Any]],
    open_access_exclusion_reason_by_model_rows: list[dict[str, Any]],
    open_access_text_exclusion_reason_by_model_rows: list[dict[str, Any]],
    open_access_text_size_exclusion_reason_by_model_rows: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]],
) -> str:
    lines = [
        'Model Selection Filter Report',
        '',
        'Headline:',
        f"  total_discovered_runs={summary['total_discovered_runs']}",
        f"  considered_runs={summary['considered_runs']}",
        f"  eligible_runs={summary['eligible_runs']}",
        f"  selected_runs={summary['selected_runs']}",
        f"  excluded_runs={summary['excluded_runs']}",
        f"  structurally_incomplete_runs={summary['structurally_incomplete_runs']}",
        f"  selected_models={summary['selected_models']}",
        '',
        'Exclusion reasons:',
    ]
    for reason, count in summary['exclusion_reason_counts'].items():
        lines.append(f'  {reason}: {count}')

    def add_table_preview(title: str, rows: list[dict[str, Any]], key: str) -> None:
        lines.append('')
        lines.append(title)
        for row in rows[:12]:
            lines.append(
                f"  {row[key]}: total={row['total_runs']} selected={row['selected_runs']} excluded={row['excluded_runs']}"
            )

    add_table_preview('Selected / excluded by model (top rows):', by_model_rows, 'model')
    add_table_preview('Selected / excluded by dataset slice (top rows):', by_dataset_rows, 'dataset')
    add_table_preview('Selected / excluded by scenario (top rows):', by_scenario_rows, 'scenario')

    lines.append('')
    lines.append('Top exclusion-reason / model pairs:')
    for row in reason_by_model_rows[:15]:
        lines.append(f"  {row['model']} :: {row['failure_reason']} -> {row['run_count']}")

    lines.append('')
    lines.append('Open-access exclusion reasons:')
    for row in open_access_exclusion_reason_rows[:15]:
        lines.append(f"  {row['failure_reason']}: {row['run_count']}")

    lines.append('')
    lines.append('Open-access exclusion reason combinations by model:')
    for row in open_access_exclusion_reason_by_model_rows[:15]:
        lines.append(f"  {row['model']} :: {row['reason_combo']} -> {row['run_count']}")

    lines.append('')
    lines.append('Open-access, text-compatible exclusion reason combinations by model:')
    for row in open_access_text_exclusion_reason_by_model_rows[:15]:
        lines.append(f"  {row['model']} :: {row['reason_combo']} -> {row['run_count']}")

    lines.append('')
    lines.append('Open-access, text-compatible, size-ok exclusion reason combinations by model:')
    for row in open_access_text_size_exclusion_reason_by_model_rows[:15]:
        lines.append(f"  {row['model']} :: {row['reason_combo']} -> {row['run_count']}")

    lines.append('')
    lines.append('Selected run specs (first 25):')
    for row in selected_rows[:25]:
        lines.append(f"  {row['run_spec_name']}")
    lines.append('')
    lines.append('See the adjacent TSV/JSON artifacts for the full inventory and regroupings.')
    return '\n'.join(lines) + '\n'


def _load_inventory_json(report_dpath: Path, inventory_json: Path | None = None) -> list[dict[str, Any]]:
    if inventory_json is not None:
        payload = json.loads(inventory_json.read_text())
        return payload
    latest = report_dpath / 'machine' / 'model_filter_inventory.latest.json'
    if latest.exists():
        return json.loads(latest.read_text())
    candidates = sorted((report_dpath / 'machine').glob('model_filter_inventory_*.json'), reverse=True)
    if candidates:
        return json.loads(candidates[0].read_text())
    raise FileNotFoundError(
        f'No filter inventory JSON found under {report_dpath}. '
        'Re-run Stage 1 with the updated index_historic_helm_runs flow so it emits '
        'machine/model_filter_inventory.latest.json, or pass --inventory-json explicitly.'
    )


def build_filter_reason_sankey_rows(inventory_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in inventory_rows:
        reasons = row.get('failure_reasons', []) or []
        if row.get('selection_status') == 'selected':
            rows.append({'filter_reason': 'selected', 'outcome': 'selected'})
            continue
        if row.get('is_structurally_incomplete'):
            rows.append({'filter_reason': 'structurally-incomplete', 'outcome': 'excluded'})
            continue
        if not reasons:
            reasons = [UNCLASSIFIED_EXCLUSION]
        for reason in reasons:
            rows.append({'filter_reason': reason, 'outcome': 'excluded'})
    return rows


def emit_filter_report_artifacts(
    *,
    report_dpath: Path,
    stamp: str,
    inventory_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    interactive_dpath = report_dpath / 'interactive'
    static_dpath = report_dpath / 'static'
    machine_dpath = report_dpath / 'machine'
    tables_dpath = static_dpath / 'tables'
    figures_dpath = static_dpath / 'figures'
    for dpath in [interactive_dpath, static_dpath, machine_dpath, tables_dpath, figures_dpath]:
        dpath.mkdir(parents=True, exist_ok=True)

    summary = summarize_inventory(inventory_rows)
    selected_rows = [row for row in inventory_rows if row.get('selection_status') == 'selected']
    excluded_rows = [row for row in inventory_rows if row.get('selection_status') != 'selected']
    by_model_rows = make_count_table(inventory_rows, facet_key='model')
    by_dataset_rows = make_count_table(inventory_rows, facet_key='dataset')
    by_scenario_rows = make_count_table(inventory_rows, facet_key='scenario')
    by_benchmark_rows = make_count_table(inventory_rows, facet_key='benchmark')
    reason_by_model_rows = make_reason_breakout_table(inventory_rows, 'model')
    reason_by_dataset_rows = make_reason_breakout_table(inventory_rows, 'dataset')
    reason_by_scenario_rows = make_reason_breakout_table(inventory_rows, 'scenario')
    reason_by_benchmark_rows = make_reason_breakout_table(inventory_rows, 'benchmark')
    open_access_exclusion_reason_rows = make_open_access_exclusion_reason_table(inventory_rows)
    open_access_exclusion_reason_by_model_rows = make_open_access_exclusion_reason_by_model_table(inventory_rows)
    open_access_text_exclusion_reason_by_model_rows = make_open_access_exclusion_reason_by_model_table(
        inventory_rows,
        excluded_reasons={'not-text-like', 'excluded-tags'},
    )
    open_access_text_size_exclusion_reason_by_model_rows = make_open_access_exclusion_reason_by_model_table(
        inventory_rows,
        excluded_reasons={'not-text-like', 'excluded-tags', 'too-large'},
    )
    summary_txt = build_filter_report_text(
        summary=summary,
        by_model_rows=by_model_rows,
        by_dataset_rows=by_dataset_rows,
        by_scenario_rows=by_scenario_rows,
        reason_by_model_rows=reason_by_model_rows,
        open_access_exclusion_reason_rows=open_access_exclusion_reason_rows,
        open_access_exclusion_reason_by_model_rows=open_access_exclusion_reason_by_model_rows,
        open_access_text_exclusion_reason_by_model_rows=open_access_text_exclusion_reason_by_model_rows,
        open_access_text_size_exclusion_reason_by_model_rows=open_access_text_size_exclusion_reason_by_model_rows,
        selected_rows=selected_rows,
    )
    selected_run_specs_txt = '\n'.join(row['run_spec_name'] for row in selected_rows) + '\n'
    cardinality_txt = build_filter_cardinality_text(inventory_rows)
    local_serving_txt = build_local_serving_recovery_text(inventory_rows)

    outputs = {
        'summary_json': str(_write_stamped_json(report_dpath, machine_dpath, 'model_filter_summary', stamp, {'summary': summary})),
        'inventory_json': str(_write_stamped_json(report_dpath, machine_dpath, 'model_filter_inventory', stamp, inventory_rows)),
        'summary_txt': str(_write_stamped_text(report_dpath, static_dpath, 'model_filter_report', stamp, '.txt', summary_txt)),
        'filter_cardinality_txt': str(_write_stamped_text(report_dpath, static_dpath, 'filter_cardinality_summary', stamp, '.txt', cardinality_txt)),
        'local_serving_txt': str(_write_stamped_text(report_dpath, static_dpath, 'filter_local_serving_summary', stamp, '.txt', local_serving_txt)),
        'selected_run_specs_txt': str(_write_stamped_text(report_dpath, static_dpath, 'model_filter_selected_run_specs', stamp, '.txt', selected_run_specs_txt)),
        'inventory_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_inventory', stamp, inventory_rows)),
        'selected_runs_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_selected_runs', stamp, selected_rows)),
        'excluded_runs_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_runs', stamp, excluded_rows)),
        'by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_counts_by_model', stamp, by_model_rows)),
        'by_dataset_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_counts_by_dataset', stamp, by_dataset_rows)),
        'by_scenario_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_counts_by_scenario', stamp, by_scenario_rows)),
        'by_benchmark_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_counts_by_benchmark', stamp, by_benchmark_rows)),
        'reason_by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_by_model', stamp, reason_by_model_rows)),
        'reason_by_dataset_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_by_dataset', stamp, reason_by_dataset_rows)),
        'reason_by_scenario_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_by_scenario', stamp, reason_by_scenario_rows)),
        'reason_by_benchmark_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_by_benchmark', stamp, reason_by_benchmark_rows)),
        'open_access_reason_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_open_access_only', stamp, open_access_exclusion_reason_rows)),
        'open_access_reason_by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_open_access_only_by_model', stamp, open_access_exclusion_reason_by_model_rows)),
        'open_access_text_reason_by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_open_access_text_only_by_model', stamp, open_access_text_exclusion_reason_by_model_rows)),
        'open_access_text_size_reason_by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'model_filter_excluded_reason_open_access_text_size_only_by_model', stamp, open_access_text_size_exclusion_reason_by_model_rows)),
    }
    outputs['flat_filter_sankey'] = emit_sankey_artifacts(
        rows=build_filter_reason_sankey_rows(inventory_rows),
        report_dpath=report_dpath,
        stamp=stamp,
        kind='model_filter',
        title=_title_with_n('Run Selection Filter: Which HELM Runs Were Included', len(inventory_rows)),
        stage_defs={
            'filter_reason': [
                'selected: model passed all eligibility criteria and had complete run data',
                'structurally-incomplete: run directory missing required files',
                'not-text-like: model has no text-compatible tags',
                'excluded-tags: model tagged as a modality or category we exclude',
                'too-large: model exceeds the local reproduction size budget',
                'not-open-access: model access is not open in the HELM registry',
                'no-local-helm-deployment: no default local HELM deployment path known to Stage 1 filter',
                f'{CLOSED_JUDGE_REQUIRED_REASON}: benchmark requires a proprietary / credentialed judge or annotator',
                f'{UNCLASSIFIED_EXCLUSION}: no current rule classified this exclusion',
            ],
            'outcome': [
                'selected: run was included in the reproduction list',
                'excluded: run was excluded from the reproduction list',
            ],
        },
        stage_order=[('filter_reason', 'Exclusion Criterion'), ('outcome', 'Outcome')],
        machine_dpath=machine_dpath,
        interactive_dpath=interactive_dpath,
        static_dpath=static_dpath,
    )
    write_latest_alias(Path(outputs['filter_cardinality_txt']), report_dpath, 'filter_cardinality_summary.latest.txt')
    write_latest_alias(Path(outputs['local_serving_txt']), report_dpath, 'filter_local_serving_summary.latest.txt')
    return outputs


def build_analysis_text(
    summary: dict[str, Any],
    by_model_rows: list[dict[str, Any]],
    by_dataset_rows: list[dict[str, Any]],
    by_scenario_rows: list[dict[str, Any]],
    candidate_pool_rows: list[dict[str, Any]],
    selection_path_rows: list[dict[str, Any]],
    reason_combo_rows: list[dict[str, Any]],
    pair_model_scenario_rows: list[dict[str, Any]],
    pair_model_benchmark_rows: list[dict[str, Any]],
    reason_example_rows: list[dict[str, Any]],
    examples: dict[str, list[dict[str, Any]]],
) -> str:
    lines = [
        'Filter Candidate Analysis',
        '',
        'Coverage:',
        f"  discovered_runs={summary['total_discovered_runs']}",
        f"  considered_runs={summary['considered_runs']}",
        f"  eligible_runs={summary['eligible_runs']}",
        f"  selected_runs={summary['selected_runs']}",
        f"  excluded_runs={summary['excluded_runs']}",
        f"  structurally_incomplete_runs={summary['structurally_incomplete_runs']}",
        '',
        'Fractions:',
        f"  selected_of_all={summary['fraction_selected_of_all']}",
        f"  selected_of_considered={summary['fraction_selected_of_considered']}",
        f"  selected_of_eligible={summary['fraction_selected_of_eligible']}",
        f"  considered_of_all={summary['fraction_considered_of_all']}",
        f"  eligible_of_all={summary['fraction_eligible_of_all']}",
        '',
        'Denominators:',
        '  discovered_runs: every run directory seen during Stage 1, including structurally incomplete directories when discoverable.',
        '  considered_runs: structurally complete runs that reached the model eligibility decision.',
        '  eligible_runs: considered runs whose model passed all eligibility filters.',
        '  selected_runs: eligible runs retained for reproduction output.',
        '',
        'Candidate pool funnel:',
    ]
    for row in candidate_pool_rows:
        lines.append(
            f"  {row['candidate_pool']}: runs={row['run_count']} selected={row['selected_runs']} excluded={row['excluded_runs']} fraction_of_all={row['fraction_of_all_runs']}"
        )

    lines.extend([
        '',
        'Selection paths:',
    ])
    for row in selection_path_rows:
        lines.append(
            f"  {row['candidate_pool']} -> {row['selection_status']}: runs={row['run_count']} fraction_of_all={row['fraction_of_all_runs']}"
        )

    lines.extend([
        '',
        'Hierarchical gate order:',
        '  all discovered runs -> structural completeness -> open weight -> suitable text tags -> runnable local deployment -> size budget -> no closed-source judge dependency -> selected subset',
        '  This gate order makes the full-corpus denominator visible while also showing the fairer open-weight and runnable subsets at intermediate steps.',
        '',
        'Suggested plots:',
        '  - selected/excluded by model',
        '  - selected/excluded by benchmark',
        '  - selected/excluded by dataset',
        '  - exclusion reasons by model',
        '  - open-access exclusion reasons by model',
        '  - open-access, text-compatible exclusion reasons by model',
        '  - open-access, text-compatible, size-OK exclusion reasons by model',
        '  - top reason combinations',
        '  - selected/excluded by candidate pool',
    ])

    lines.extend([
        '',
        'Why runs were not chosen:',
    ])
    for reason, count in summary['exclusion_reason_counts'].items():
        lines.append(f'  {reason}: {count}')

    lines.extend([
        '',
        'Reason combinations:',
    ])
    for row in reason_combo_rows[:20]:
        lines.append(
            f"  {row['reason_combo']}: runs={row['run_count']} example={row['example_run_spec_name']}"
        )

    def add_section(title: str, rows: list[dict[str, Any]], key: str) -> None:
        lines.append('')
        lines.append(title)
        for row in rows[:15]:
            lines.append(
                f"  {row[key]}: total={row['total_runs']} considered={row['considered_runs']} eligible={row['eligible_runs']} selected={row['selected_runs']} excluded={row['excluded_runs']} selected_of_all={row['fraction_selected_of_all']} selected_of_considered={row['fraction_selected_of_considered']} top_exclusion_reason={row['top_exclusion_reason']}"
            )

    add_section('Coverage by model:', by_model_rows, 'model')
    add_section('Coverage by dataset slice:', by_dataset_rows, 'dataset')
    add_section('Coverage by scenario:', by_scenario_rows, 'scenario')

    lines.append('')
    lines.append('Top model x scenario cohorts:')
    for row in pair_model_scenario_rows[:20]:
        lines.append(
            f"  {row['model']} x {row['scenario']}: total={row['total_runs']} selected={row['selected_runs']} considered={row['considered_runs']} eligible={row['eligible_runs']} selected_of_all={row['fraction_selected_of_all']}"
        )

    lines.append('')
    lines.append('Top model x benchmark cohorts:')
    for row in pair_model_benchmark_rows[:20]:
        lines.append(
            f"  {row['model']} x {row['benchmark']}: total={row['total_runs']} selected={row['selected_runs']} considered={row['considered_runs']} eligible={row['eligible_runs']} selected_of_all={row['fraction_selected_of_all']}"
        )

    lines.append('')
    lines.append('Representative examples by exclusion reason:')
    for row in reason_example_rows[:20]:
        lines.append(
            f"  {row['failure_reason']}: {row['run_spec_name']} :: {row['selection_explanation']}"
        )

    lines.append('')
    lines.append('Selected examples:')
    for row in examples['selected_examples'][:20]:
        lines.append(f"  {row['run_spec_name']} :: {row['selection_explanation']}")

    lines.append('')
    lines.append('Excluded examples:')
    for row in examples['excluded_examples'][:20]:
        lines.append(f"  {row['run_spec_name']} :: {row['selection_explanation']}")

    lines.append('')
    lines.append('Use the adjacent TSV/JSON artifacts to inspect the full candidate set and facet-specific fractions.')
    return '\n'.join(lines) + '\n'


def _emit_bar_chart(
    rows: list[dict[str, Any]],
    *,
    report_dpath: Path,
    x: str,
    y: str,
    title: str,
    stem: str,
    stamp: str,
    interactive_dpath: Path,
    static_dpath: Path,
    xaxis_title: str | None = None,
) -> dict[str, str | None]:
    if not rows:
        return {'html': None, 'png': None, 'plotly_error': None}
    html_root = history_publish_root(report_dpath, interactive_dpath, stamp)
    png_root = history_publish_root(report_dpath, static_dpath, stamp)
    html_fpath = html_root / f'{stem}_{stamp}.html'
    png_fpath = png_root / f'{stem}_{stamp}.png'
    html_out = None
    png_out = None
    plotly_error = None
    try:
        import plotly.express as px

        configure_plotly_chrome()
        n_bars = len({str(row.get(x) or 'unknown') for row in rows})
        if n_bars > 75:
            logger.warning(f'Chart {stem!r} is rendering {n_bars} bars; rendering the full chart anyway.')
        fig = px.bar(pd.DataFrame(rows), x=x, y=y, title=title)
        fig.update_layout(**_bar_chart_layout(rows, x))
        fig.update_xaxes(**_bar_chart_xaxis_update(rows, x=x, xaxis_title=xaxis_title, compact=False))
        fig.write_html(str(html_fpath), include_plotlyjs='cdn')
        logger.debug(f'Write to 📝: {html_fpath}')
        write_latest_alias(html_fpath, interactive_dpath, f'{stem}.latest.html')
        html_out = str(html_fpath)
        try:
            fig.update_layout(**_bar_chart_layout(rows, x, compact=True))
            fig.update_xaxes(**_bar_chart_xaxis_update(rows, x=x, xaxis_title=xaxis_title, compact=True))
            fig.write_image(str(png_fpath), scale=1.0)
            logger.debug(f'Write 🖼: {png_fpath}')
            write_latest_alias(png_fpath, static_dpath, f'{stem}.latest.png')
            png_out = str(png_fpath)
        except Exception as ex:
            plotly_error = f'unable to write PNG: {ex!r}'
            logger.warning(plotly_error)
    except Exception as ex:
        plotly_error = f'unable to write chart: {ex!r}'
        logger.warning(plotly_error)
    return {'html': html_out, 'png': png_out, 'plotly_error': plotly_error}


def _emit_stacked_bar_chart(
    rows: list[dict[str, Any]],
    *,
    report_dpath: Path,
    x: str,
    y: str,
    color: str,
    title: str,
    stem: str,
    stamp: str,
    interactive_dpath: Path,
    static_dpath: Path,
    xaxis_title: str | None = None,
    yaxis_title: str | None = None,
    color_order: list[str] | None = None,
    n_facets_shown: int | None = None,
    n_facets_total: int | None = None,
) -> dict[str, str | None]:
    if not rows:
        return {'html': None, 'png': None, 'plotly_error': None}
    html_root = history_publish_root(report_dpath, interactive_dpath, stamp)
    png_root = history_publish_root(report_dpath, static_dpath, stamp)
    html_fpath = html_root / f'{stem}_{stamp}.html'
    png_fpath = png_root / f'{stem}_{stamp}.png'
    html_out = None
    png_out = None
    plotly_error = None
    try:
        import plotly.express as px

        configure_plotly_chrome()
        category_orders = {}
        if color_order is not None:
            category_orders[color] = color_order
        n_bars = len({str(row.get(x) or 'unknown') for row in rows})
        if n_bars > 75:
            logger.warning(f'Chart {stem!r} is rendering {n_bars} bars; rendering the full chart anyway.')
        fig = px.bar(
            pd.DataFrame(rows),
            x=x,
            y=y,
            color=color,
            title=title,
            barmode='stack',
            category_orders=category_orders,
        )
        fig.update_layout(
            yaxis_title=yaxis_title if yaxis_title is not None else y.replace('_', ' '),
            **_bar_chart_layout(rows, x),
        )
        fig.update_xaxes(**_bar_chart_xaxis_update(rows, x=x, xaxis_title=xaxis_title, compact=False))
        fig.write_html(str(html_fpath), include_plotlyjs='cdn')
        logger.debug(f'Write to 📝: {html_fpath}')
        write_latest_alias(html_fpath, interactive_dpath, f'{stem}.latest.html')
        html_out = str(html_fpath)
        try:
            fig.update_layout(**_bar_chart_layout(rows, x, compact=True))
            fig.update_xaxes(**_bar_chart_xaxis_update(rows, x=x, xaxis_title=xaxis_title, compact=True))
            fig.write_image(str(png_fpath), scale=1.0)
            logger.debug(f'Write 🖼: {png_fpath}')
            write_latest_alias(png_fpath, static_dpath, f'{stem}.latest.png')
            png_out = str(png_fpath)
        except Exception as ex:
            plotly_error = f'unable to write PNG: {ex!r}'
            logger.warning(plotly_error)
    except Exception as ex:
        plotly_error = f'unable to write chart: {ex!r}'
        logger.warning(plotly_error)
    return {'html': html_out, 'png': png_out, 'plotly_error': plotly_error}


def _make_selected_excluded_rows(
    inventory_rows: list[dict[str, Any]],
    facet_key: str,
    *,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """
    Returns (plot_rows, n_facets_shown, n_facets_total).

    If `limit` is provided, slices at the facet level (top `limit` facets by
    total then selected count) before expanding to per-status rows, so the slice
    boundary never cuts a facet in half and selected facets are never crowded
    out by excluded-only facets. If `limit` is `None`, all facets are included.
    """
    counts: dict[str, dict[str, int]] = {}
    for row in inventory_rows:
        facet = str(row.get(facet_key) or 'unknown')
        status = 'selected' if row.get('selection_status') == 'selected' else 'excluded'
        bucket = counts.setdefault(facet, {'selected': 0, 'excluded': 0})
        bucket[status] += 1
    sorted_facets = sorted(
        counts.items(),
        key=lambda item: (-sum(item[1].values()), -item[1]['selected'], str(item[0])),
    )
    n_facets_total = len(sorted_facets)
    top_facets = sorted_facets if limit is None else sorted_facets[:limit]
    n_facets_shown = len(top_facets)
    rows = []
    for facet, bucket in top_facets:
        if bucket['selected'] > 0:
            rows.append({facet_key: facet, 'selection_status': 'selected', 'count': bucket['selected']})
        if bucket['excluded'] > 0:
            rows.append({facet_key: facet, 'selection_status': 'excluded', 'count': bucket['excluded']})
    return rows, n_facets_shown, n_facets_total


def emit_filter_analysis_artifacts(
    *,
    report_dpath: Path,
    stamp: str,
    inventory_rows: list[dict[str, Any]],
    chosen_model_rows: list[dict[str, Any]] | None = None,
    model_filter_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    analysis_dpath = report_dpath / 'analysis'
    machine_dpath = analysis_dpath / 'machine'
    static_dpath = analysis_dpath / 'static'
    interactive_dpath = analysis_dpath / 'interactive'
    tables_dpath = static_dpath / 'tables'
    figures_dpath = static_dpath / 'figures'
    for dpath in [analysis_dpath, machine_dpath, static_dpath, interactive_dpath, tables_dpath, figures_dpath]:
        dpath.mkdir(parents=True, exist_ok=True)

    summary = summarize_inventory(inventory_rows)
    by_model_rows = make_count_table(inventory_rows, facet_key='model')
    by_dataset_rows = make_count_table(inventory_rows, facet_key='dataset')
    by_scenario_rows = make_count_table(inventory_rows, facet_key='scenario')
    by_benchmark_rows = make_count_table(inventory_rows, facet_key='benchmark')
    candidate_pool_rows = make_candidate_pool_table(inventory_rows)
    selection_path_rows = make_selection_path_table(inventory_rows)
    selected_excluded_by_model_rows, n_model_facets_shown, n_model_facets_total = _make_selected_excluded_rows(inventory_rows, 'model')
    selected_excluded_by_benchmark_rows, n_benchmark_facets_shown, n_benchmark_facets_total = _make_selected_excluded_rows(inventory_rows, 'benchmark')
    selected_excluded_by_dataset_rows, n_dataset_facets_shown, n_dataset_facets_total = _make_selected_excluded_rows(inventory_rows, 'dataset')
    selected_excluded_by_scenario_rows, n_scenario_facets_shown, n_scenario_facets_total = _make_selected_excluded_rows(inventory_rows, 'scenario')
    reasons_by_model = make_reason_breakout_table(inventory_rows, 'model')
    reasons_by_dataset = make_reason_breakout_table(inventory_rows, 'dataset')
    reasons_by_scenario = make_reason_breakout_table(inventory_rows, 'scenario')
    reasons_by_benchmark = make_reason_breakout_table(inventory_rows, 'benchmark')
    open_access_exclusion_reason_rows = make_open_access_exclusion_reason_table(inventory_rows)
    open_access_exclusion_reason_by_model_rows = make_open_access_exclusion_reason_by_model_table(inventory_rows)
    open_access_text_exclusion_reason_by_model_rows = make_open_access_exclusion_reason_by_model_table(
        inventory_rows,
        excluded_reasons={'not-text-like', 'excluded-tags'},
    )
    open_access_text_size_exclusion_reason_by_model_rows = make_open_access_exclusion_reason_by_model_table(
        inventory_rows,
        excluded_reasons={'not-text-like', 'excluded-tags', 'too-large'},
    )
    reason_combo_rows = make_reason_combo_table(inventory_rows)
    pair_model_scenario_rows = make_pair_table(inventory_rows, 'model', 'scenario')
    pair_model_benchmark_rows = make_pair_table(inventory_rows, 'model', 'benchmark')
    pair_benchmark_dataset_rows = make_pair_table(inventory_rows, 'benchmark', 'dataset')
    reason_example_rows = make_reason_examples_table(inventory_rows)
    hierarchical_sankey_rows = build_hierarchical_sankey_rows(inventory_rows)
    examples = make_decision_examples(inventory_rows)
    analysis_text = build_analysis_text(
        summary,
        by_model_rows,
        by_dataset_rows,
        by_scenario_rows,
        candidate_pool_rows,
        selection_path_rows,
        reason_combo_rows,
        pair_model_scenario_rows,
        pair_model_benchmark_rows,
        reason_example_rows,
        examples,
    )
    analysis_md = '\n'.join([
        '# Filter Candidate Analysis',
        '',
        '## Summary',
        '',
        to_markdown([{k: v for k, v in summary.items() if k != 'selected_model_names' and k != 'exclusion_reason_counts'}]),
        '',
        '## Candidate Pool',
        '',
        to_markdown(candidate_pool_rows),
        '',
        '## Selection Paths',
        '',
        to_markdown(selection_path_rows),
        '',
        '## Coverage By Model',
        '',
        to_markdown(by_model_rows[:30]),
        '',
        '## Coverage By Dataset',
        '',
        to_markdown(by_dataset_rows[:30]),
        '',
        '## Coverage By Scenario',
        '',
        to_markdown(by_scenario_rows[:30]),
        '',
        '## Reason Combinations',
        '',
        to_markdown(reason_combo_rows[:30]),
        '',
        '## Model x Scenario Cohorts',
        '',
        to_markdown(pair_model_scenario_rows[:30]),
        '',
        '## Model x Benchmark Cohorts',
        '',
        to_markdown(pair_model_benchmark_rows[:30]),
        '',
        '## Representative Exclusions',
        '',
        to_markdown(reason_example_rows[:30]),
    ]) + '\n'

    summary_payload = {
        'generated_utc': stamp,
        'summary': summary,
        'chosen_model_rows': chosen_model_rows or [],
        'model_filter_rows': model_filter_rows or [],
        'decision_examples': examples,
        'hierarchical_sankey_rows': hierarchical_sankey_rows,
        'candidate_pool_rows': candidate_pool_rows,
        'selection_path_rows': selection_path_rows,
        'reason_combo_rows': reason_combo_rows,
        'pair_model_scenario_rows': pair_model_scenario_rows,
        'pair_model_benchmark_rows': pair_model_benchmark_rows,
        'pair_benchmark_dataset_rows': pair_benchmark_dataset_rows,
        'reason_example_rows': reason_example_rows,
        'selected_excluded_by_model_rows': selected_excluded_by_model_rows,
        'selected_excluded_by_benchmark_rows': selected_excluded_by_benchmark_rows,
        'selected_excluded_by_dataset_rows': selected_excluded_by_dataset_rows,
        'selected_excluded_by_scenario_rows': selected_excluded_by_scenario_rows,
    }

    outputs = {
        'summary_json': str(_write_stamped_json(report_dpath, machine_dpath, 'filter_candidate_analysis', stamp, summary_payload)),
        'summary_txt': str(_write_stamped_text(report_dpath, static_dpath, 'filter_candidate_analysis', stamp, '.txt', analysis_text)),
        'summary_md': str(_write_stamped_text(report_dpath, static_dpath, 'filter_candidate_analysis', stamp, '.md', analysis_md)),
        'by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_coverage_by_model', stamp, by_model_rows)),
        'by_dataset_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_coverage_by_dataset', stamp, by_dataset_rows)),
        'by_scenario_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_coverage_by_scenario', stamp, by_scenario_rows)),
        'by_benchmark_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_coverage_by_benchmark', stamp, by_benchmark_rows)),
        'candidate_pool_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_pool', stamp, candidate_pool_rows)),
        'selection_path_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_selection_paths', stamp, selection_path_rows)),
        'reason_by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_reasons_by_model', stamp, reasons_by_model)),
        'reason_by_dataset_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_reasons_by_dataset', stamp, reasons_by_dataset)),
        'reason_by_scenario_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_reasons_by_scenario', stamp, reasons_by_scenario)),
        'reason_by_benchmark_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_reasons_by_benchmark', stamp, reasons_by_benchmark)),
        'reason_combo_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_reason_combinations', stamp, reason_combo_rows)),
        'open_access_reason_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_open_access_exclusion_reasons', stamp, open_access_exclusion_reason_rows)),
        'open_access_reason_by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_open_access_exclusion_reasons_by_model', stamp, open_access_exclusion_reason_by_model_rows)),
        'model_scenario_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_model_by_scenario', stamp, pair_model_scenario_rows)),
        'model_benchmark_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_model_by_benchmark', stamp, pair_model_benchmark_rows)),
        'benchmark_dataset_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_benchmark_by_dataset', stamp, pair_benchmark_dataset_rows)),
        'reason_examples_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_reason_examples', stamp, reason_example_rows)),
        'sel_excl_by_model_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_selection_by_model', stamp, selected_excluded_by_model_rows)),
        'sel_excl_by_benchmark_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_selection_by_benchmark', stamp, selected_excluded_by_benchmark_rows)),
        'sel_excl_by_dataset_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_selection_by_dataset', stamp, selected_excluded_by_dataset_rows)),
        'sel_excl_by_scenario_tsv': str(_write_stamped_table(report_dpath, tables_dpath, 'filter_candidate_selection_by_scenario', stamp, selected_excluded_by_scenario_rows)),
    }

    outputs['selected_fraction_by_model_chart'] = _emit_bar_chart(
        by_model_rows,
        report_dpath=report_dpath,
        x='model',
        y='fraction_selected_of_all',
        title='Selected Fraction of Candidate Runs by Model',
        stem='filter_candidate_fraction_selected_by_model',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
    )
    outputs['selected_fraction_by_dataset_chart'] = _emit_bar_chart(
        by_dataset_rows,
        report_dpath=report_dpath,
        x='dataset',
        y='fraction_selected_of_all',
        title='Selected Fraction of Candidate Runs by Dataset Slice',
        stem='filter_candidate_fraction_selected_by_dataset',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
    )
    outputs['open_access_exclusion_reason_chart'] = _emit_bar_chart(
        open_access_exclusion_reason_rows,
        report_dpath=report_dpath,
        x='failure_reason',
        y='run_count',
        title='Excluded Runs by Reason for Open-Access Models',
        stem='filter_candidate_open_access_exclusion_reasons',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
    )
    outputs['open_access_exclusion_reason_by_model_chart'] = _emit_stacked_bar_chart(
        open_access_exclusion_reason_by_model_rows,
        report_dpath=report_dpath,
        x='model',
        y='run_count',
        color='reason_combo',
        title='Open-Access Excluded Runs by Reason Combination and Model',
        stem='filter_candidate_open_access_exclusion_reasons_by_model',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Model',
        yaxis_title='Excluded Run Count',
    )
    outputs['open_access_text_exclusion_reason_by_model_chart'] = _emit_stacked_bar_chart(
        open_access_text_exclusion_reason_by_model_rows,
        report_dpath=report_dpath,
        x='model',
        y='run_count',
        color='reason_combo',
        title='Open-Access, Text-Compatible Excluded Runs by Reason Combination and Model',
        stem='filter_candidate_open_access_text_exclusion_reasons_by_model',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Model',
        yaxis_title='Excluded Run Count',
    )
    outputs['open_access_text_size_exclusion_reason_by_model_chart'] = _emit_stacked_bar_chart(
        open_access_text_size_exclusion_reason_by_model_rows,
        report_dpath=report_dpath,
        x='model',
        y='run_count',
        color='reason_combo',
        title='Open-Access, Text-Compatible, Size-OK Excluded Runs by Reason Combination and Model',
        stem='filter_candidate_open_access_text_size_exclusion_reasons_by_model',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Model',
        yaxis_title='Excluded Run Count',
    )
    outputs['selected_vs_excluded_by_model_chart'] = _emit_stacked_bar_chart(
        selected_excluded_by_model_rows,
        report_dpath=report_dpath,
        x='model',
        y='count',
        color='selection_status',
        title='Selected vs Excluded Run Specs by Model',
        stem='filter_candidate_selection_by_model',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Model',
        yaxis_title='Run Spec Count',
        color_order=['selected', 'excluded'],
        n_facets_shown=n_model_facets_shown,
        n_facets_total=n_model_facets_total,
    )
    outputs['selected_vs_excluded_by_benchmark_chart'] = _emit_stacked_bar_chart(
        selected_excluded_by_benchmark_rows,
        report_dpath=report_dpath,
        x='benchmark',
        y='count',
        color='selection_status',
        title='Selected vs Excluded Run Specs by Benchmark',
        stem='filter_candidate_selection_by_benchmark',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Benchmark',
        yaxis_title='Run Spec Count',
        color_order=['selected', 'excluded'],
        n_facets_shown=n_benchmark_facets_shown,
        n_facets_total=n_benchmark_facets_total,
    )
    outputs['selected_vs_excluded_by_dataset_chart'] = _emit_stacked_bar_chart(
        selected_excluded_by_dataset_rows,
        report_dpath=report_dpath,
        x='dataset',
        y='count',
        color='selection_status',
        title='Selected vs Excluded Run Specs by Dataset Slice',
        stem='filter_candidate_selection_by_dataset',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Dataset Slice',
        yaxis_title='Run Spec Count',
        color_order=['selected', 'excluded'],
        n_facets_shown=n_dataset_facets_shown,
        n_facets_total=n_dataset_facets_total,
    )
    outputs['selected_vs_excluded_by_scenario_chart'] = _emit_stacked_bar_chart(
        selected_excluded_by_scenario_rows,
        report_dpath=report_dpath,
        x='scenario',
        y='count',
        color='selection_status',
        title='Selected vs Excluded Run Specs by Scenario',
        stem='filter_candidate_selection_by_scenario',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Scenario',
        yaxis_title='Run Spec Count',
        color_order=['selected', 'excluded'],
        n_facets_shown=n_scenario_facets_shown,
        n_facets_total=n_scenario_facets_total,
    )
    outputs['exclusion_reason_chart'] = _emit_bar_chart(
        [{'failure_reason': reason, 'run_count': count} for reason, count in summary['exclusion_reason_counts'].items()],
        report_dpath=report_dpath,
        x='failure_reason',
        y='run_count',
        title='Excluded Runs by Reason',
        stem='filter_candidate_exclusion_reasons',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
    )
    outputs['reason_by_model_chart'] = _emit_stacked_bar_chart(
        reasons_by_model,
        report_dpath=report_dpath,
        x='model',
        y='run_count',
        color='failure_reason',
        title='Exclusion Reasons by Model',
        stem='filter_candidate_exclusion_reasons_by_model',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Model',
        yaxis_title='Excluded Run Count',
    )
    outputs['candidate_pool_chart'] = _emit_stacked_bar_chart(
        [
            {
                'candidate_pool': row['candidate_pool'],
                'selection_status': 'selected',
                'count': row['selected_runs'],
            }
            for row in candidate_pool_rows
            if row['selected_runs']
        ] + [
            {
                'candidate_pool': row['candidate_pool'],
                'selection_status': 'excluded',
                'count': row['excluded_runs'],
            }
            for row in candidate_pool_rows
            if row['excluded_runs']
        ],
        report_dpath=report_dpath,
        x='candidate_pool',
        y='count',
        color='selection_status',
        title='Selected vs Excluded Run Specs by Candidate Pool',
        stem='filter_candidate_selection_by_candidate_pool',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
        xaxis_title='Candidate Pool',
        yaxis_title='Run Spec Count',
        color_order=['selected', 'excluded'],
    )
    outputs['reason_combo_chart'] = _emit_bar_chart(
        reason_combo_rows,
        report_dpath=report_dpath,
        x='reason_combo',
        y='run_count',
        title='Filter Reason Combinations',
        stem='filter_candidate_reason_combinations',
        stamp=stamp,
        interactive_dpath=interactive_dpath,
        static_dpath=figures_dpath,
    )
    outputs['hierarchical_filter_sankey'] = emit_sankey_artifacts(
        rows=hierarchical_sankey_rows,
        report_dpath=analysis_dpath,
        stamp=stamp,
        kind='hierarchical_filter_path',
        title=_title_with_n('Hierarchical Filter Path: From All HELM Runs to the Reproduced Subset', len(hierarchical_sankey_rows)),
        stage_defs=build_hierarchical_sankey_key(summary),
        stage_order=[
            ('structural_stage', 'Structural Gate'),
            ('access_stage', 'Open-Weight Gate'),
            ('tag_stage', 'Tag Gate'),
            ('deployment_stage', 'Deployment Gate'),
            ('size_stage', 'Size Gate'),
            ('judge_stage', 'Judge Gate'),
            ('outcome_stage', 'Outcome'),
        ],
        machine_dpath=machine_dpath,
        interactive_dpath=interactive_dpath,
        static_dpath=static_dpath,
    )
    return outputs


def emit_filter_report_bundle(
    *,
    report_dpath: Path,
    stamp: str,
    inventory_rows: list[dict[str, Any]],
    source_command: str | None = None,
) -> dict[str, Any]:
    report_outputs = emit_filter_report_artifacts(
        report_dpath=report_dpath,
        stamp=stamp,
        inventory_rows=inventory_rows,
    )
    analysis_outputs = emit_filter_analysis_artifacts(
        report_dpath=report_dpath,
        stamp=stamp,
        inventory_rows=inventory_rows,
    )
    write_filter_rebuild_script(
        report_dpath,
        inventory_json=Path(report_outputs['inventory_json']),
    )
    write_filter_reproduce_script(
        report_dpath,
        source_command=source_command,
    )
    return {
        'report': report_outputs,
        'analysis': analysis_outputs,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Analyze a saved Stage 1 filter inventory.')
    parser.add_argument('--report-dpath', default=str(filtering_reports_root()))
    parser.add_argument('--inventory-json', default=None)
    args = parser.parse_args(argv)

    report_dpath = Path(args.report_dpath).expanduser().resolve()
    inventory_json = Path(args.inventory_json).expanduser().resolve() if args.inventory_json else None
    try:
        inventory_rows = _load_inventory_json(report_dpath, inventory_json)
    except FileNotFoundError as ex:
        raise SystemExit(str(ex))
    stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime('%Y%m%dT%H%M%SZ')
    outputs = emit_filter_report_bundle(
        report_dpath=report_dpath,
        stamp=stamp,
        inventory_rows=inventory_rows,
    )
    logger.info(f"Wrote filter inventory/report bundle: {outputs['report']['inventory_json']}")
    logger.info(f"Wrote filter candidate analysis: {outputs['analysis']['summary_json']}")


if __name__ == '__main__':
    main()
