from __future__ import annotations

import argparse

from helm_audit.infra.logging import setup_cli_logging
import csv
import datetime as datetime_mod
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from helm_audit.infra.api import audit_root, default_index_root, env_defaults
from helm_audit.infra.fs_publish import safe_unlink, symlink_to, write_latest_alias
from helm_audit.infra.report_layout import core_run_reports_root, portable_repo_root_lines, write_reproduce_script
from helm_audit.reports import core_metrics, pair_samples
from helm_audit.workflows.compare_batch import (
    collect_historic_candidates,
    choose_historic_candidate,
)


def latest_index_csv(index_dpath: Path) -> Path:
    cands = sorted(index_dpath.glob('audit_results_index_*.csv'), reverse=True)
    if not cands:
        raise FileNotFoundError(f'No index csv files found in {index_dpath}')
    return cands[0]


def load_rows(index_fpath: Path) -> list[dict[str, Any]]:
    with index_fpath.open() as file:
        return list(csv.DictReader(file))


def _coerce_float(x):
    try:
        return float(x)
    except Exception:
        return float('-inf')


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or '').strip()
    return text or None


def _build_attempt_fallback_key(row: dict[str, Any]) -> str:
    parts = {
        'experiment_name': _clean_optional_text(row.get('experiment_name')) or 'unknown',
        'job_id': _clean_optional_text(row.get('job_id')) or 'unknown',
        'run_entry': _clean_optional_text(row.get('run_entry')) or 'unknown',
        'manifest_timestamp': _clean_optional_text(row.get('manifest_timestamp')) or 'unknown',
        'machine_host': _clean_optional_text(row.get('machine_host')) or 'unknown',
        'run_dir': _clean_optional_text(row.get('run_dir')) or 'unknown',
    }
    return 'fallback::' + '|'.join(f'{key}={value}' for key, value in parts.items())


def _attempt_ref(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    attempt_uuid = _clean_optional_text(row.get('attempt_uuid'))
    attempt_fallback_key = _clean_optional_text(row.get('attempt_fallback_key')) or _build_attempt_fallback_key(row)
    return {
        'experiment_name': row.get('experiment_name'),
        'job_id': row.get('job_id'),
        'job_dpath': row.get('job_dpath'),
        'run_entry': row.get('run_entry'),
        'run_dir': row.get('run_dir'),
        'machine_host': row.get('machine_host'),
        'manifest_timestamp': row.get('manifest_timestamp'),
        'attempt_uuid': attempt_uuid,
        'attempt_uuid_source': row.get('attempt_uuid_source'),
        'attempt_fallback_key': attempt_fallback_key,
        'attempt_identity': row.get('attempt_identity') or attempt_uuid or attempt_fallback_key,
        'attempt_identity_kind': row.get('attempt_identity_kind') or ('attempt_uuid' if attempt_uuid else 'fallback'),
    }


def matching_rows(
    rows: list[dict[str, Any]],
    run_entry: str,
    experiment_name: str | None = None,
) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if row.get('run_entry') != run_entry:
            continue
        if experiment_name is not None and row.get('experiment_name') != experiment_name:
            continue
        if row.get('status') not in {'computed', 'reused', 'unknown', ''}:
            continue
        if row.get('has_run_spec', '').lower() not in {'true', '1'}:
            continue
        run_dir = row.get('run_dir')
        if not run_dir:
            continue
        out.append(row)
    out.sort(key=lambda r: (_coerce_float(r.get('manifest_timestamp')), r.get('experiment_name', '')), reverse=True)
    return out


def slugify(text: str) -> str:
    return (
        text.replace('/', '-')
        .replace(':', '-')
        .replace(',', '-')
        .replace('=', '-')
        .replace('@', '-')
    )


def _write_latest_selection(report_dpath: Path, selection: dict[str, Any]) -> Path:
    stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime('%Y%m%dT%H%M%SZ')
    history_dpath = report_dpath / '.history' / stamp[:8]
    history_dpath.mkdir(parents=True, exist_ok=True)
    history_fpath = history_dpath / f'report_selection_{stamp}.json'
    history_fpath.write_text(json.dumps(selection, indent=2))
    return write_latest_alias(history_fpath, report_dpath, 'report_selection.latest.json')


def _load_json_if_exists(fpath: Path) -> dict[str, Any] | None:
    if not fpath.exists():
        return None
    try:
        data = json.loads(fpath.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_validated_stored_selection(
    report_dpath: Path,
    *,
    requested_run_entry: str,
    requested_experiment_name: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    selection_fpath = report_dpath / 'report_selection.latest.json'
    selection = _load_json_if_exists(selection_fpath)
    if selection is None:
        return None, [f'missing or unreadable stored selection: {selection_fpath}']
    problems = []
    stored_run_entry = _clean_optional_text(selection.get('run_entry'))
    if stored_run_entry != requested_run_entry:
        problems.append(
            f'stored selection run_entry mismatch: expected {requested_run_entry!r}, found {stored_run_entry!r}'
        )
    if requested_experiment_name is not None:
        stored_experiment_name = _clean_optional_text(selection.get('experiment_name'))
        if stored_experiment_name != requested_experiment_name:
            problems.append(
                'stored selection experiment_name mismatch: '
                f'expected {requested_experiment_name!r}, found {stored_experiment_name!r}'
            )
    if problems:
        return None, problems
    return selection, []


def _resolve_report_local_run_link(report_dpath: Path, link_name: str) -> str | None:
    link_fpath = report_dpath / link_name
    if not (link_fpath.exists() or link_fpath.is_symlink()):
        return None
    try:
        return str(link_fpath.expanduser().resolve())
    except Exception:
        return None


def _existing_run_path(value: Any) -> str | None:
    text = _clean_optional_text(value)
    if not text:
        return None
    try:
        path = Path(text).expanduser().resolve()
    except Exception:
        return None
    if not path.exists():
        return None
    return str(path)


def _format_local_selection_failure(
    *,
    run_entry: str,
    experiment_name: str | None,
    report_dpath: Path,
    local_problems: list[str],
    index_failure: str,
) -> str:
    base = (
        f'Unable to resolve local kwdagger runs for run_entry={run_entry!r}'
        if experiment_name is None else
        f'Unable to resolve local kwdagger runs for run_entry={run_entry!r} within experiment_name={experiment_name!r}'
    )
    details = '; '.join(local_problems) if local_problems else 'no reusable report-local selection was found'
    return (
        f'{base}. Existing report-local selection missing/unusable in {report_dpath}: {details}. '
        f'Current index fallback also failed: {index_failure}'
    )


def _find_kwdagger_job_dpath(run_dpath: str | os.PathLike[str]) -> Path | None:
    current = Path(run_dpath).expanduser().resolve()
    for cand in [current, *current.parents]:
        if (cand / 'job_config.json').exists():
            return cand
    return None


def _write_selected_run_symlinks(report_dpath: Path, selection: dict[str, Any]) -> dict[str, str]:
    created = {}
    run_targets = {
        'kwdagger_a.run': selection['left_run_a'],
        'kwdagger_b.run': selection['left_run_b'],
        'official.run': selection['right_run_a'],
    }
    for name, target in run_targets.items():
        created[name] = str(symlink_to(target, report_dpath / name))

    job_targets = {
        'kwdagger_a.job': _find_kwdagger_job_dpath(selection['left_run_a']),
        'kwdagger_b.job': _find_kwdagger_job_dpath(selection['left_run_b']),
    }
    for name, target in job_targets.items():
        if target is not None:
            created[name] = str(symlink_to(target, report_dpath / name))
    return created


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-entry', required=True)
    parser.add_argument('--index-fpath', default=None)
    parser.add_argument('--index-dpath', default=str(default_index_root()))
    parser.add_argument('--precomputed-root', default=env_defaults()['HELM_PRECOMPUTED_ROOT'])
    parser.add_argument('--report-dpath', default=None)
    parser.add_argument('--left-label', default='kwdagger_repeat')
    parser.add_argument('--right-label', default='official_vs_kwdagger')
    parser.add_argument('--allow-single-repeat', action='store_true')
    parser.add_argument('--experiment-name', default=None)
    args = parser.parse_args(argv)

    report_dpath = Path(args.report_dpath) if args.report_dpath else (
        core_run_reports_root() / 'manual' / f'core-metrics-{slugify(args.run_entry)}'
    )
    report_dpath = report_dpath.expanduser().resolve()
    report_dpath.mkdir(parents=True, exist_ok=True)

    stored_selection, stored_selection_problems = _load_validated_stored_selection(
        report_dpath,
        requested_run_entry=args.run_entry,
        requested_experiment_name=args.experiment_name,
    )
    if stored_selection is not None:
        print(f'Using stored report selection from {report_dpath / "report_selection.latest.json"} as primary replay source')

    local_resolution_problems = list(stored_selection_problems)
    selection_sources: dict[str, str] = {}
    selection_seed = dict(stored_selection or {})

    left_a_row = None
    left_b_row = None
    left_a = _existing_run_path(selection_seed.get('left_run_a'))
    if left_a:
        selection_sources['left_run_a'] = 'stored_selection'
    left_b = _existing_run_path(selection_seed.get('left_run_b'))
    if left_b:
        selection_sources['left_run_b'] = 'stored_selection'
    right_run_a = _existing_run_path(selection_seed.get('right_run_a'))
    if right_run_a:
        selection_sources['right_run_a'] = 'stored_selection'
    single_run = bool(selection_seed.get('single_run'))

    if left_a is None:
        left_a = _resolve_report_local_run_link(report_dpath, 'kwdagger_a.run')
        if left_a:
            selection_sources['left_run_a'] = 'report_symlink'
    if left_b is None:
        left_b = _resolve_report_local_run_link(report_dpath, 'kwdagger_b.run')
        if left_b:
            selection_sources['left_run_b'] = 'report_symlink'
    if right_run_a is None:
        right_run_a = _resolve_report_local_run_link(report_dpath, 'official.run')
        if right_run_a:
            selection_sources['right_run_a'] = 'report_symlink'

    if left_a and left_b:
        try:
            if Path(left_a).resolve() == Path(left_b).resolve():
                single_run = True
        except Exception:
            pass
    if single_run and left_a and not left_b:
        left_b = left_a
        selection_sources['left_run_b'] = selection_sources.get('left_run_a', 'single_run_reuse')
    if single_run and left_b and not left_a:
        left_a = left_b
        selection_sources['left_run_a'] = selection_sources.get('left_run_b', 'single_run_reuse')

    rows: list[dict[str, Any]] | None = None
    matches: list[dict[str, Any]] | None = None
    index_fpath: Path | None = None

    def _ensure_matches() -> list[dict[str, Any]]:
        nonlocal rows, matches, index_fpath
        if matches is not None:
            return matches
        index_fpath = (
            Path(args.index_fpath).expanduser().resolve()
            if args.index_fpath else
            latest_index_csv(Path(args.index_dpath).expanduser().resolve())
        )
        rows = load_rows(index_fpath)
        matches = matching_rows(rows, args.run_entry, experiment_name=args.experiment_name)
        return matches

    need_left_replay = left_a is None or left_b is None or (not single_run and left_a == left_b)
    if need_left_replay:
        match_rows = _ensure_matches()
        if not match_rows:
            if args.experiment_name is not None:
                index_failure = (
                    f'no matching completed rows for run_entry={args.run_entry!r} '
                    f'within experiment_name={args.experiment_name!r}'
                )
            else:
                index_failure = f'no matching completed rows for run_entry={args.run_entry!r}'
            raise SystemExit(
                _format_local_selection_failure(
                    run_entry=args.run_entry,
                    experiment_name=args.experiment_name,
                    report_dpath=report_dpath,
                    local_problems=local_resolution_problems,
                    index_failure=index_failure,
                )
            )
        if len(match_rows) >= 2:
            left_a_row = match_rows[0]
            left_b_row = match_rows[1]
            left_a = str(Path(match_rows[0]['run_dir']).expanduser().resolve())
            left_b = str(Path(match_rows[1]['run_dir']).expanduser().resolve())
            selection_sources['left_run_a'] = 'current_index'
            selection_sources['left_run_b'] = 'current_index'
            single_run = False
        elif args.allow_single_repeat or single_run:
            left_a_row = match_rows[0]
            left_b_row = match_rows[0]
            left_a = str(Path(match_rows[0]['run_dir']).expanduser().resolve())
            left_b = left_a
            selection_sources['left_run_a'] = 'current_index'
            selection_sources['left_run_b'] = 'current_index_reused'
            single_run = True
            print('single_run=True: only one local run found; repeat pair will be skipped')
        else:
            raise SystemExit(
                f'Need at least 2 matching kwdagger runs for run_entry={args.run_entry!r}; '
                f'found {len(match_rows)}. Use --allow-single-repeat to skip the repeat pair.'
            )

    desired_max = None
    preferred_row = left_a_row or left_b_row
    if preferred_row is None and stored_selection is not None:
        for ref in [
            selection_seed.get('left_attempt_a_ref'),
            selection_seed.get('left_attempt_b_ref'),
            *(selection_seed.get('selected_local_attempt_refs') or []),
        ]:
            if isinstance(ref, dict):
                preferred_row = ref
                break
    if preferred_row is None:
        cached_matches = _ensure_matches() if (right_run_a is None or stored_selection is None) else []
        preferred_row = cached_matches[0] if cached_matches else None
    try:
        desired_max = int(preferred_row.get('max_eval_instances')) if preferred_row and preferred_row.get('max_eval_instances') else None
    except Exception:
        desired_max = None

    info = selection_seed.get('historic_info')
    if right_run_a is None:
        historic_candidates = collect_historic_candidates(args.precomputed_root, args.run_entry)
        chosen_historic, info = choose_historic_candidate(historic_candidates, desired_max)
        if chosen_historic is None:
            raise SystemExit(f'No historic HELM candidate found for run_entry={args.run_entry!r}')
        right_run_a = str(Path(chosen_historic['run_dir']).expanduser().resolve())
        selection_sources['right_run_a'] = 'historic_index'
    else:
        info = info or {'reused_from': 'stored_selection_or_report_symlink'}

    if left_a is None or left_b is None or right_run_a is None:
        raise SystemExit(
            f'Failed to resolve rebuild inputs for run_entry={args.run_entry!r}; '
            f'left_run_a={left_a!r} left_run_b={left_b!r} right_run_a={right_run_a!r}'
        )

    effective_index_fpath = index_fpath
    if effective_index_fpath is None:
        stored_index = _clean_optional_text(selection_seed.get('index_fpath'))
        if stored_index:
            effective_index_fpath = Path(stored_index).expanduser()

    print(f'index_fpath={effective_index_fpath}')
    print(f'left_run_a={left_a}')
    print(f'left_run_b={left_b}')
    print(f'right_run_a={right_run_a}')
    print(f'report_dpath={report_dpath}')
    print(f'historic_info={info}')
    selection = {
        **selection_seed,
        'index_fpath': str(effective_index_fpath) if effective_index_fpath is not None else None,
        'run_entry': args.run_entry,
        'left_run_a': str(left_a),
        'left_run_b': str(left_b),
        'right_run_a': str(right_run_a),
        'left_label': args.left_label,
        'right_label': args.right_label,
        'report_dpath': str(report_dpath),
        'experiment_name': args.experiment_name if args.experiment_name is not None else selection_seed.get('experiment_name'),
        'historic_info': info,
        'single_run': single_run,
        'left_attempt_a_ref': selection_seed.get('left_attempt_a_ref') or _attempt_ref(left_a_row),
        'left_attempt_b_ref': selection_seed.get('left_attempt_b_ref') or _attempt_ref(left_b_row),
        'selection_sources': selection_sources,
    }
    if selection_seed.get('selected_local_attempt_refs'):
        selection['selected_local_attempt_refs'] = selection_seed.get('selected_local_attempt_refs')
    else:
        selection['selected_local_attempt_refs'] = [
            ref for ref in [selection.get('left_attempt_a_ref'), selection.get('left_attempt_b_ref')]
            if ref is not None
        ]
    if selection_seed.get('selected_local_attempt_identities'):
        selection['selected_local_attempt_identities'] = selection_seed.get('selected_local_attempt_identities')
    else:
        selection['selected_local_attempt_identities'] = [
            ref['attempt_identity']
            for ref in selection['selected_local_attempt_refs']
            if ref.get('attempt_identity')
        ]
    selection_fpath = _write_latest_selection(report_dpath, selection)
    link_info = _write_selected_run_symlinks(report_dpath, selection)
    selection['selected_run_links'] = link_info
    selection_fpath = _write_latest_selection(report_dpath, selection)
    print(f'selection_fpath={selection_fpath}')
    core_metrics_cmd = [
        '--left-run-a', str(left_a),
        '--left-run-b', str(left_b),
        '--left-label', args.left_label,
        '--right-run-a', str(right_run_a),
        '--right-run-b', str(left_a),
        '--right-label', args.right_label,
        '--report-dpath', str(report_dpath),
    ]
    if single_run:
        core_metrics_cmd.append('--single-run')
    core_metrics.main(core_metrics_cmd)

    if not single_run:
        pair_samples.write_pair_samples(
            run_a=str(left_a),
            run_b=str(left_b),
            label=args.left_label,
            report_dpath=report_dpath,
        )
    pair_samples.write_pair_samples(
        run_a=str(right_run_a),
        run_b=str(left_a),
        label=args.right_label,
        report_dpath=report_dpath,
    )
    cmd_parts = [
        '-m',
        'helm_audit.workflows.rebuild_core_report',
        '--run-entry',
        args.run_entry,
        '--index-fpath',
        str(effective_index_fpath) if effective_index_fpath is not None else '',
        '--precomputed-root',
        str(args.precomputed_root),
        '--report-dpath',
        str(report_dpath),
        '--left-label',
        args.left_label,
        '--right-label',
        args.right_label,
        *( ['--allow-single-repeat'] if args.allow_single_repeat else [] ),
        *( ['--experiment-name', args.experiment_name] if args.experiment_name else [] ),
    ]
    if not effective_index_fpath:
        drop_idx = cmd_parts.index('--index-fpath')
        del cmd_parts[drop_idx: drop_idx + 2]
    reproduce_fpath = write_reproduce_script(report_dpath / 'reproduce.latest.sh', [
        '#!/usr/bin/env bash',
        'set -euo pipefail',
        *portable_repo_root_lines(),
        'cd "$REPO_ROOT"',
        'PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" '
        + ' '.join(shlex.quote(part) for part in cmd_parts)
        + ' "$@"',
    ])
    write_latest_alias(reproduce_fpath, report_dpath, 'reproduce.sh')


if __name__ == '__main__':
    setup_cli_logging()
    main()
