from __future__ import annotations

import argparse

from loguru import logger

from eval_audit.infra.logging import rich_link, setup_cli_logging
import datetime as datetime_mod
import glob
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from eval_audit.infra.api import default_report_root
from eval_audit.reports.core_packet_summary import (
    find_report_pair,
    load_core_report_packet,
    packet_component_by_source_kind,
    packet_local_reference_component,
)
from eval_audit.utils.numeric import nested_get


def _load_json(fpath: Path) -> dict[str, Any]:
    return json.loads(fpath.read_text())


def _find_pair(report: dict[str, Any], label: str) -> dict[str, Any]:
    return find_report_pair(report, label)


def _find_curve_value(rows: list[dict[str, Any]], abs_tol: float) -> float | None:
    for row in rows or []:
        try:
            if float(row.get('abs_tol')) == float(abs_tol):
                return float(row.get('agree_ratio'))
        except Exception:
            pass
    return None


def _assessment_label(repeat_agree_0: float | None, official_agree_01: float | None) -> str:
    if repeat_agree_0 is None or official_agree_01 is None:
        return 'unknown'
    if repeat_agree_0 >= 0.99 and official_agree_01 >= 0.95:
        return 'close_match'
    if repeat_agree_0 >= 0.99 and official_agree_01 >= 0.75:
        return 'moderate_drift'
    if repeat_agree_0 >= 0.99:
        return 'strong_drift'
    return 'unstable_local_repeat'


def _slugify(text: str) -> str:
    return (
        text.replace('/', '-')
        .replace(':', '-')
        .replace(',', '-')
        .replace('=', '-')
        .replace('@', '-')
        .replace(' ', '-')
    )


def _write_latest_alias(src: Path, latest_root: Path, latest_name: str) -> None:
    latest_fpath = latest_root / latest_name
    if latest_fpath.exists() or latest_fpath.is_symlink():
        latest_fpath.unlink()
    rel_src = os.path.relpath(src, start=latest_root)
    os.symlink(rel_src, latest_fpath)


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-root', default=str(default_report_root()))
    parser.add_argument('--out-dpath', default=None)
    args = parser.parse_args(argv)

    report_root = Path(args.report_root).expanduser().resolve()
    out_dpath = Path(args.out_dpath).expanduser().resolve() if args.out_dpath else (report_root / 'overall-reproducibility')
    out_dpath.mkdir(parents=True, exist_ok=True)

    report_paths = sorted(glob.glob(str(report_root / 'core-metrics-*' / 'core_metric_report.latest.json')))
    rows = []
    for p in report_paths:
        fpath = Path(p)
        report = _load_json(fpath)
        packet = load_core_report_packet(fpath.parent)
        repeat = _find_pair(report, 'local_repeat')
        official = _find_pair(report, 'official_vs_local')
        repeat_agree_0 = _find_curve_value(repeat.get('instance_level', {}).get('agreement_vs_abs_tol', []), 0.0)
        official_agree_0 = _find_curve_value(official.get('instance_level', {}).get('agreement_vs_abs_tol', []), 0.0)
        official_agree_01 = _find_curve_value(official.get('instance_level', {}).get('agreement_vs_abs_tol', []), 0.1)
        official_agree_025 = _find_curve_value(official.get('instance_level', {}).get('agreement_vs_abs_tol', []), 0.25)
        official_agree_05 = _find_curve_value(official.get('instance_level', {}).get('agreement_vs_abs_tol', []), 0.5)
        local_component = packet_local_reference_component(packet)
        official_component = packet_component_by_source_kind(packet, 'official_vs_local', 'official')
        rows.append({
            'report_dir': str(fpath.parent),
            'report_json': str(fpath),
            'run_spec_name': report.get('run_spec_name'),
            'generated_utc': report.get('generated_utc'),
            'components_manifest': str(packet['components_manifest_path']),
            'comparisons_manifest': str(packet['comparisons_manifest_path']),
            'warnings_manifest': str(packet['warnings_manifest_path']),
            'n_core_metrics': len((official.get('core_metrics') or [])),
            'diagnostic_flags': report.get('diagnostic_flags', []),
            'packet_warnings': (packet.get('warnings_manifest') or {}).get('packet_warnings', []),
            'local_reference_empty_completion_rate': nested_get(report, 'run_diagnostics', local_component.get('component_id'), 'empty_completion_rate'),
            'local_reference_mean_output_tokens': nested_get(report, 'run_diagnostics', local_component.get('component_id'), 'output_token_count', 'mean'),
            'official_empty_completion_rate': nested_get(report, 'run_diagnostics', official_component.get('component_id'), 'empty_completion_rate'),
            'official_mean_output_tokens': nested_get(report, 'run_diagnostics', official_component.get('component_id'), 'output_token_count', 'mean'),
            'repeat_instance_agree_0': repeat_agree_0,
            'official_instance_agree_0': official_agree_0,
            'official_instance_agree_01': official_agree_01,
            'official_instance_agree_025': official_agree_025,
            'official_instance_agree_05': official_agree_05,
            'official_runlevel_p90': nested_get(official, 'run_level', 'overall_quantiles', 'abs_delta', 'p90'),
            'official_runlevel_max': nested_get(official, 'run_level', 'overall_quantiles', 'abs_delta', 'max'),
            'assessment_label': _assessment_label(repeat_agree_0, official_agree_01),
        })

    table = pd.DataFrame(rows).sort_values(['assessment_label', 'run_spec_name'], na_position='last')
    stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime('%Y%m%dT%H%M%SZ')
    history_dpath = out_dpath / '.history' / stamp[:8]
    history_dpath.mkdir(parents=True, exist_ok=True)

    json_fpath = history_dpath / f'overall_reproducibility_summary_{stamp}.json'
    csv_fpath = history_dpath / f'overall_reproducibility_summary_{stamp}.csv'
    txt_fpath = history_dpath / f'overall_reproducibility_summary_{stamp}.txt'
    md_fpath = history_dpath / f'overall_reproducibility_summary_{stamp}.md'

    summary = {
        'generated_utc': stamp,
        'n_reports': len(rows),
        'assessment_counts': dict(Counter(row['assessment_label'] for row in rows)),
        'run_specs': rows,
    }
    json_fpath.write_text(json.dumps(summary, indent=2))
    table.to_csv(csv_fpath, index=False)
    md_fpath.write_text(table.to_markdown(index=False) + '\n')

    lines = []
    lines.append('Overall Reproducibility Assessment')
    lines.append('')
    lines.append(f'generated_utc: {stamp}')
    lines.append(f'n_reports: {len(rows)}')
    lines.append('')
    lines.append('assessment_counts:')
    for key, val in sorted(summary['assessment_counts'].items()):
        lines.append(f'  {key}: {val}')
    lines.append('')
    lines.append('per_run_spec:')
    for row in rows:
        lines.append(f"  - run_spec_name: {row['run_spec_name']}")
        lines.append(f"    report_dir: {row['report_dir']}")
        lines.append(f"    warnings_manifest: {row['warnings_manifest']}")
        lines.append(f"    packet_warnings: {row['packet_warnings']}")
        lines.append(f"    assessment_label: {row['assessment_label']}")
        lines.append(f"    diagnostic_flags: {row['diagnostic_flags']}")
        lines.append(f"    local_reference_empty_completion_rate: {row['local_reference_empty_completion_rate']}")
        lines.append(f"    local_reference_mean_output_tokens: {row['local_reference_mean_output_tokens']}")
        lines.append(f"    official_empty_completion_rate: {row['official_empty_completion_rate']}")
        lines.append(f"    official_mean_output_tokens: {row['official_mean_output_tokens']}")
        lines.append(f"    repeat_instance_agree_0: {row['repeat_instance_agree_0']}")
        lines.append(f"    official_instance_agree_0: {row['official_instance_agree_0']}")
        lines.append(f"    official_instance_agree_01: {row['official_instance_agree_01']}")
        lines.append(f"    official_instance_agree_025: {row['official_instance_agree_025']}")
        lines.append(f"    official_instance_agree_05: {row['official_instance_agree_05']}")
        lines.append(f"    official_runlevel_p90: {row['official_runlevel_p90']}")
        lines.append(f"    official_runlevel_max: {row['official_runlevel_max']}")
    txt_fpath.write_text('\n'.join(lines) + '\n')

    for src, latest_name in [
        (json_fpath, 'overall_reproducibility_summary.latest.json'),
        (csv_fpath, 'overall_reproducibility_summary.latest.csv'),
        (txt_fpath, 'overall_reproducibility_summary.latest.txt'),
        (md_fpath, 'overall_reproducibility_summary.latest.md'),
    ]:
        _write_latest_alias(src, out_dpath, latest_name)

    logger.info(f'Wrote summary json: {rich_link(json_fpath)}')
    logger.info(f'Wrote summary csv: {rich_link(csv_fpath)}')
    logger.info(f'Wrote summary md: {rich_link(md_fpath)}')
    logger.info(f'Wrote summary txt: {rich_link(txt_fpath)}')


if __name__ == '__main__':
    setup_cli_logging()
    main()
