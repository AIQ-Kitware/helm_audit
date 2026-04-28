from __future__ import annotations

import argparse
import datetime as datetime_mod
import json
from collections import Counter
from pathlib import Path
from typing import Any

import kwutil
import pandas as pd

from eval_audit.compat.helm_outputs import HelmOutputs
from eval_audit.indexing.schema import (
    benchmark_group_from_run_name,
    component_id_for_local,
    extract_run_spec_fields,
    logical_run_key_for_local,
    now_utc_iso,
)
from eval_audit.infra.api import default_index_root, env_defaults
from eval_audit.infra.fs_publish import write_latest_alias
from eval_audit.infra.logging import rich_link, setup_cli_logging
from eval_audit.helm.run_entries import parse_run_entry_description

from loguru import logger


def _safe_json_load(fpath: Path) -> dict[str, Any]:
    if not fpath.exists():
        return {}
    try:
        return json.loads(fpath.read_text())
    except Exception:
        return {}


def _first_run_dir(job_dpath: Path) -> Path | None:
    bo = job_dpath / 'benchmark_output'
    if not bo.exists():
        return None
    try:
        outputs = HelmOutputs.coerce(bo)
    except Exception:
        return None
    runs = []
    for suite in outputs.suites(pattern='*'):
        runs.extend(list(suite.runs()))
    if not runs:
        return None
    return Path(runs[0].path)


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _build_attempt_fallback_key(
    *,
    experiment_name: str | None,
    job_id: str | None,
    run_entry: str | None,
    manifest_timestamp: Any,
    machine_host: str | None,
    run_dir: str | None,
) -> str:
    parts = {
        "experiment_name": _clean_optional_text(experiment_name) or "unknown",
        "job_id": _clean_optional_text(job_id) or "unknown",
        "run_entry": _clean_optional_text(run_entry) or "unknown",
        "manifest_timestamp": _clean_optional_text(manifest_timestamp) or "unknown",
        "machine_host": _clean_optional_text(machine_host) or "unknown",
        "run_dir": _clean_optional_text(run_dir) or "unknown",
    }
    return "fallback::" + "|".join(f"{key}={value}" for key, value in parts.items())


def _process_context_info(process_context: dict[str, Any], fallback_host: str | None) -> dict[str, Any]:
    props = process_context.get('properties', {}) if isinstance(process_context, dict) else {}
    machine = props.get('machine', {}) if isinstance(props.get('machine', {}), dict) else {}
    extra = props.get('extra', {}) if isinstance(props.get('extra', {}), dict) else {}
    env = extra.get('env', {}) if isinstance(extra.get('env', {}), dict) else {}
    nvidia_smi = extra.get('nvidia_smi', {}) if isinstance(extra.get('nvidia_smi', {}), dict) else {}
    gpus = nvidia_smi.get('gpus', []) if isinstance(nvidia_smi.get('gpus', []), list) else []

    host = machine.get('host')
    provenance = 'recorded'
    if not host:
        host = fallback_host
        provenance = 'fallback' if fallback_host else 'unknown'

    return {
        'machine_host': host,
        'machine_user': machine.get('user'),
        'machine_os': machine.get('os_name'),
        'machine_arch': machine.get('arch'),
        'python_version': machine.get('py_version'),
        'cuda_visible_devices': env.get('CUDA_VISIBLE_DEVICES'),
        'gpu_count': len(gpus),
        'gpu_names': [g.get('name') for g in gpus if isinstance(g, dict)],
        'gpu_memory_total_mb': [g.get('memory_total_mb') for g in gpus if isinstance(g, dict)],
        'provenance_source': provenance,
    }


def _process_context_provenance(job_dpath: Path, adapter_manifest: dict[str, Any], process_context: dict[str, Any]) -> dict[str, Any]:
    process_context_json_fpath = job_dpath / 'process_context.json'
    manifest_process_context_fpath = _clean_optional_text(adapter_manifest.get('process_context_fpath'))
    process_context_fpath = (
        str(process_context_json_fpath)
        if process_context_json_fpath.exists() else
        manifest_process_context_fpath
    )
    if process_context_json_fpath.exists():
        process_context_source = 'process_context.json'
    elif process_context:
        process_context_source = 'adapter_manifest.process_context'
    else:
        process_context_source = 'missing'

    props = process_context.get('properties', {}) if isinstance(process_context, dict) else {}
    attempt_uuid = _clean_optional_text(props.get('uuid'))

    return {
        'adapter_manifest_fpath': str(job_dpath / 'adapter_manifest.json') if (job_dpath / 'adapter_manifest.json').exists() else None,
        'process_context_fpath': process_context_fpath,
        'process_context_source': process_context_source,
        'materialize_out_dpath': _clean_optional_text(adapter_manifest.get('out_dpath')),
        'process_start_timestamp': _clean_optional_text(props.get('start_timestamp')),
        'process_stop_timestamp': _clean_optional_text(props.get('stop_timestamp')),
        'process_duration': _clean_optional_text(props.get('duration')),
        'attempt_uuid': attempt_uuid,
        'attempt_uuid_source': 'process_context.properties.uuid' if attempt_uuid else 'missing',
    }


def _row_for_job(
    job_config_fpath: Path,
    fallback_host: str | None,
    index_generated_utc: str | None = None,
) -> dict[str, Any]:
    job_dpath = job_config_fpath.parent
    adapter_manifest = _safe_json_load(job_dpath / 'adapter_manifest.json')
    process_context = _safe_json_load(job_dpath / 'process_context.json')
    if not process_context:
        process_context = adapter_manifest.get('process_context', {}) if isinstance(adapter_manifest, dict) else {}
    run_dir = _first_run_dir(job_dpath)

    job_config = _safe_json_load(job_config_fpath)
    run_entry = job_config.get('helm.run_entry')
    benchmark = None
    run_entry_model = None
    method = None
    if run_entry:
        try:
            benchmark, tokens = parse_run_entry_description(run_entry)
            run_entry_model = tokens.get('model')
            method = tokens.get('method')
        except Exception:
            benchmark = None

    # Normalized run-spec fields via the shared extractor (stable hash + names).
    run_spec_fpath = (run_dir / 'run_spec.json') if run_dir else None
    spec_fields = extract_run_spec_fields(run_spec_fpath)

    # Prefer the authoritative adapter_spec.model from the run_spec; fall back to
    # the model token parsed out of helm.run_entry when the spec isn't present.
    model = spec_fields['model'] or run_entry_model
    scenario_class = spec_fields['scenario_class']
    benchmark_group = spec_fields['benchmark_group'] or benchmark_group_from_run_name(run_entry)
    model_deployment = spec_fields['model_deployment']

    context_info = _process_context_info(process_context, fallback_host)
    process_info = _process_context_provenance(job_dpath, adapter_manifest, process_context)
    metric_specs: list[Any] = []
    if run_spec_fpath and run_spec_fpath.exists():
        raw_spec = _safe_json_load(run_spec_fpath)
        if isinstance(raw_spec, dict):
            raw_specs_list = raw_spec.get('metric_specs', [])
            if isinstance(raw_specs_list, list):
                metric_specs = raw_specs_list
    if model_deployment is None and isinstance(adapter_manifest, dict):
        # Legacy fallback: adapter_manifest sometimes carries a top-level adapter_spec.
        am_adapter = adapter_manifest.get('adapter_spec', {})
        if isinstance(am_adapter, dict):
            model_deployment = am_adapter.get('model_deployment')

    experiment_name = job_dpath.parent.parent.name if job_dpath.parent.name == 'helm' else job_dpath.parent.name
    run_dir_text = str(run_dir) if run_dir else None
    attempt_fallback_key = _build_attempt_fallback_key(
        experiment_name=experiment_name,
        job_id=job_dpath.name,
        run_entry=run_entry,
        manifest_timestamp=adapter_manifest.get('timestamp'),
        machine_host=context_info.get('machine_host'),
        run_dir=run_dir_text,
    )
    attempt_identity = process_info['attempt_uuid'] or attempt_fallback_key

    logical_run_key = logical_run_key_for_local(
        run_spec_name=spec_fields['run_spec_name'],
        run_entry=run_entry,
    )
    component_id = component_id_for_local(
        experiment_name=experiment_name,
        job_id=job_dpath.name,
        attempt_identity=attempt_identity,
    )

    # Canonical run_name — prefer the authoritative run_spec.json["name"]; fall
    # back through directory-basename and then weaker logical keys.  Keeping
    # this explicit means analyzers don't have to synthesize it downstream.
    run_name = spec_fields['run_spec_name']
    if run_name is None and run_dir is not None:
        run_name = run_dir.name
    if run_name is None and run_dir_text:
        run_name = Path(run_dir_text).name
    if run_name is None:
        run_name = logical_run_key or run_entry

    row = {
        # --- normalized component-row fields (aligned with official index) ---
        'source_kind': 'local',
        'artifact_format': 'helm',
        'eee_artifact_path': None,
        'component_id': component_id,
        'logical_run_key': logical_run_key,
        'experiment_name': experiment_name,
        'job_id': job_dpath.name,
        'job_dpath': str(job_dpath),
        'run_path': run_dir_text,
        'run_name': run_name,
        'run_spec_fpath': str(run_spec_fpath) if (run_spec_fpath and run_spec_fpath.exists()) else None,
        'run_spec_name': spec_fields['run_spec_name'],
        'run_spec_hash': spec_fields['run_spec_hash'],
        'model': model,
        'model_deployment': model_deployment,
        'scenario_class': scenario_class,
        'benchmark_group': benchmark_group,
        'max_eval_instances': job_config.get('helm.max_eval_instances'),
        'index_generated_utc': index_generated_utc,
        # --- status / run metadata ---
        'status': adapter_manifest.get('status'),
        'manifest_timestamp': adapter_manifest.get('timestamp'),
        'run_entry': run_entry,
        'benchmark': benchmark,
        'method': method,
        'suite': job_config.get('helm.suite'),
        # Legacy alias preserved so existing consumers continue to work.
        'run_dir': run_dir_text,
        'has_run_dir': bool(run_dir and run_dir.exists()),
        'has_run_spec': bool(run_dir and (run_dir / 'run_spec.json').exists()),
        'has_stats': bool(run_dir and (run_dir / 'stats.json').exists()),
        'has_per_instance_stats': bool(run_dir and (run_dir / 'per_instance_stats.json').exists()),
        'metric_class_names': [m.get('class_name') for m in metric_specs if isinstance(m, dict)],
        # --- attempt identity (first-class) ---
        'attempt_fallback_key': attempt_fallback_key,
        'attempt_identity': attempt_identity,
        'attempt_identity_kind': 'attempt_uuid' if process_info['attempt_uuid'] else 'fallback',
    }
    row.update(context_info)
    row.update(process_info)
    return row


def _write_summary(rows: list[dict[str, Any]], out_fpath: Path) -> None:
    benchmark_counts = Counter(row.get('benchmark') or 'unknown' for row in rows)
    model_counts = Counter(row.get('model') or 'unknown' for row in rows)
    host_counts = Counter(row.get('machine_host') or 'unknown' for row in rows)
    status_counts = Counter(row.get('status') or 'unknown' for row in rows)

    lines = []
    lines.append('Audit Results Index Summary')
    lines.append('')
    lines.append(f'n_rows: {len(rows)}')
    lines.append('')
    lines.append('status_counts:')
    for key, val in sorted(status_counts.items()):
        lines.append(f'  {key}: {val}')
    lines.append('')
    lines.append('machine_host_counts:')
    for key, val in sorted(host_counts.items()):
        lines.append(f'  {key}: {val}')
    lines.append('')
    lines.append('benchmark_counts:')
    for key, val in sorted(benchmark_counts.items()):
        lines.append(f'  {key}: {val}')
    lines.append('')
    lines.append('model_counts:')
    for key, val in sorted(model_counts.items()):
        lines.append(f'  {key}: {val}')
    logger.debug(f'Write to: {rich_link(out_fpath)}')
    out_fpath.write_text('\n'.join(lines) + '\n')


def write_combined_component_index(
    *,
    official_index_fpath: Path,
    local_rows: list[dict[str, Any]],
    out_fpath: Path,
) -> Path:
    """Emit a derived normalized union of official and local component rows.

    This is a dumb, derived artifact: rows from both sides are coerced to a
    shared column set with the ``source_kind`` column distinguishing them.  No
    grouping or comparison logic is performed here — that remains the
    responsibility of downstream grouping/comparison tooling.
    """
    from eval_audit.indexing.schema import COMMON_COMPONENT_COLUMNS

    official_df = pd.read_csv(official_index_fpath, low_memory=False)
    local_df = pd.DataFrame(local_rows)

    for col in COMMON_COMPONENT_COLUMNS:
        if col not in official_df.columns:
            official_df[col] = None
        if col not in local_df.columns:
            local_df[col] = None

    combined = pd.concat(
        [
            official_df[COMMON_COMPONENT_COLUMNS],
            local_df[COMMON_COMPONENT_COLUMNS],
        ],
        axis=0,
        ignore_index=True,
    )
    out_fpath.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out_fpath, index=False)
    return out_fpath


def main(argv: list[str] | None = None) -> None:
    setup_cli_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--results-root', default=env_defaults()['AUDIT_RESULTS_ROOT'])
    parser.add_argument('--report-dpath', default=str(default_index_root()))
    parser.add_argument('--fallback-host', default=None)
    parser.add_argument(
        '--combined-with-official',
        default=None,
        help=(
            'Optional path to an official/public index CSV. When provided, also '
            'emit a derived combined index CSV that is a normalized union of '
            'official and local component rows (no grouping, purely a union).'
        ),
    )
    args = parser.parse_args(argv)
    logger.debug('Start index results')

    results_root = Path(args.results_root).expanduser().resolve()
    report_dpath = Path(args.report_dpath).expanduser().resolve()
    report_dpath.mkdir(parents=True, exist_ok=True)
    stamp = datetime_mod.datetime.now(datetime_mod.UTC).strftime('%Y%m%dT%H%M%SZ')
    index_generated_utc = now_utc_iso()

    rows = []
    logger.debug(f'Globbing {rich_link(results_root)}')
    for job_config_fpath in sorted(results_root.rglob('job_config.json')):
        try:
            rows.append(_row_for_job(
                job_config_fpath, args.fallback_host,
                index_generated_utc=index_generated_utc,
            ))
        except Exception as ex:
            rows.append({
                'source_kind': 'local',
                'job_dpath': str(job_config_fpath.parent),
                'status': 'index_error',
                'error': repr(ex),
                'machine_host': args.fallback_host,
                'provenance_source': 'fallback' if args.fallback_host else 'unknown',
                'index_generated_utc': index_generated_utc,
            })

    jsonl_fpath = report_dpath / f'audit_results_index_{stamp}.jsonl'
    csv_fpath = report_dpath / f'audit_results_index_{stamp}.csv'
    summary_fpath = report_dpath / f'audit_results_index_{stamp}.txt'
    logger.debug(f'Writing to: {rich_link(jsonl_fpath)}')
    with jsonl_fpath.open('w') as file:
        for row in rows:
            file.write(json.dumps(kwutil.Json.ensure_serializable(row)) + '\n')

    table = pd.DataFrame(rows)
    if not table.empty:
        # Component-style fields first (aligned with the official index), then
        # legacy operational columns, then any remaining fields.
        preferred = [
            'source_kind', 'artifact_format', 'eee_artifact_path',
            'component_id', 'logical_run_key',
            'experiment_name', 'job_id', 'status',
            'run_spec_name', 'run_spec_hash', 'benchmark_group',
            'benchmark', 'model', 'model_deployment', 'method',
            'attempt_identity_kind', 'attempt_uuid', 'attempt_identity',
            'manifest_timestamp', 'process_start_timestamp', 'process_stop_timestamp',
            'max_eval_instances', 'machine_host', 'gpu_count', 'gpu_names',
            'cuda_visible_devices', 'provenance_source', 'process_context_source',
            'run_name', 'run_path', 'run_dir', 'run_spec_fpath',
            'materialize_out_dpath', 'adapter_manifest_fpath',
            'process_context_fpath', 'index_generated_utc',
        ]
        cols = [c for c in preferred if c in table.columns] + [c for c in table.columns if c not in preferred]
        table = table[cols]
    table.to_csv(csv_fpath, index=False)
    _write_summary(rows, summary_fpath)

    write_latest_alias(jsonl_fpath, report_dpath, 'audit_results_index.latest.jsonl')
    write_latest_alias(csv_fpath, report_dpath, 'audit_results_index.latest.csv')
    write_latest_alias(summary_fpath, report_dpath, 'audit_results_index.latest.txt')

    logger.info(f'Wrote jsonl index: {rich_link(jsonl_fpath)}')
    logger.info(f'Wrote csv index: {rich_link(csv_fpath)}')
    logger.info(f'Wrote summary: {rich_link(summary_fpath)}')
    logger.info(f'Latest alias: {rich_link(report_dpath / "audit_results_index.latest.csv")}')

    if args.combined_with_official:
        official_fpath = Path(args.combined_with_official).expanduser().resolve()
        combined_fpath = report_dpath / f'combined_component_index_{stamp}.csv'
        write_combined_component_index(
            official_index_fpath=official_fpath,
            local_rows=rows,
            out_fpath=combined_fpath,
        )
        write_latest_alias(combined_fpath, report_dpath, 'combined_component_index.latest.csv')
        logger.info(f'Wrote combined index: {rich_link(combined_fpath)}')


if __name__ == '__main__':
    setup_cli_logging()
    main()
