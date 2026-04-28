from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from eval_audit.workflows import index_results


def _write_json(fpath: Path, data: dict) -> None:
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(json.dumps(data))


def _make_run_spec(run_name: str, model: str = 'meta/llama-3-8b',
                   scenario_class: str = 'helm.TestScenario',
                   deployment: str = 'local/meta-llama-3-8b') -> dict:
    return {
        'name': run_name,
        'adapter_spec': {
            'model': model,
            'model_deployment': deployment,
        },
        'scenario_spec': {'class_name': scenario_class},
        'metric_specs': [{'class_name': 'helm.BasicMetric'}],
    }


def _build_fake_job(
    tmp_path: Path,
    *,
    run_entry: str,
    run_name: str,
    model: str = 'meta/llama-3-8b',
    scenario_class: str = 'helm.TestScenario',
    deployment: str = 'local/meta-llama-3-8b',
    uuid: str | None = 'uuid-xyz',
    write_run_spec: bool = True,
    write_stats: bool = True,
) -> Path:
    job_dpath = tmp_path / 'demo-exp' / 'helm' / 'job-1'
    job_config_fpath = job_dpath / 'job_config.json'
    _write_json(
        job_config_fpath,
        {
            'helm.run_entry': run_entry,
            'helm.suite': 'demo-suite',
            'helm.max_eval_instances': 100,
        },
    )
    process_context = {
        'properties': {
            'uuid': uuid,
            'machine': {
                'host': 'host-a',
                'user': 'alice',
                'os_name': 'Linux',
                'arch': 'x86_64',
                'py_version': '3.11.9',
            },
            'start_timestamp': '2026-01-01T00:00:00+00:00',
            'stop_timestamp': '2026-01-01T00:00:02+00:00',
            'duration': '0:00:02',
            'extra': {
                'env': {'CUDA_VISIBLE_DEVICES': '0'},
                'nvidia_smi': {'gpus': []},
            },
        },
    }
    if uuid is None:
        process_context['properties'].pop('uuid')
    _write_json(
        job_dpath / 'adapter_manifest.json',
        {
            'status': 'computed',
            'timestamp': 123.4,
            'out_dpath': str(job_dpath),
            'process_context_fpath': str(job_dpath / 'process_context.json'),
            'process_context': process_context,
        },
    )
    if write_run_spec:
        run_dir = job_dpath / 'benchmark_output' / 'runs' / 'demo-suite' / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(run_dir / 'run_spec.json',
                    _make_run_spec(run_name, model=model,
                                   scenario_class=scenario_class,
                                   deployment=deployment))
        if write_stats:
            _write_json(run_dir / 'stats.json', {})
    return job_config_fpath


def test_row_for_job_uses_embedded_process_context_uuid_when_json_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    job_dpath = tmp_path / "demo-exp" / "helm" / "job-123"
    job_config_fpath = job_dpath / "job_config.json"
    _write_json(
        job_config_fpath,
        {
            "helm.run_entry": "mmlu:model=openai/gpt-oss-20b",
            "helm.suite": "demo-suite",
            "helm.max_eval_instances": 100,
        },
    )
    _write_json(
        job_dpath / "adapter_manifest.json",
        {
            "status": "computed",
            "timestamp": 123.4,
            "out_dpath": str(job_dpath),
            "process_context_fpath": str(job_dpath / "process_context.json"),
            "process_context": {
                "properties": {
                    "uuid": "uuid-123",
                    "machine": {
                        "host": "host-a",
                        "user": "alice",
                        "os_name": "Linux",
                        "arch": "x86_64",
                        "py_version": "3.11.9",
                    },
                    "start_timestamp": "2026-01-01T00:00:00+00:00",
                    "stop_timestamp": "2026-01-01T00:00:02+00:00",
                    "duration": "0:00:02",
                    "extra": {
                        "env": {"CUDA_VISIBLE_DEVICES": "0"},
                        "nvidia_smi": {"gpus": [{"name": "A100", "memory_total_mb": 40960}]},
                    },
                }
            },
        },
    )
    monkeypatch.setattr(index_results, "_first_run_dir", lambda _job_dpath: None)

    row = index_results._row_for_job(job_config_fpath, fallback_host=None)

    assert row["attempt_uuid"] == "uuid-123"
    assert row["attempt_identity"] == "uuid-123"
    assert row["attempt_identity_kind"] == "attempt_uuid"
    assert row["attempt_uuid_source"] == "process_context.properties.uuid"
    assert row["process_context_source"] == "adapter_manifest.process_context"
    assert row["materialize_out_dpath"] == str(job_dpath)
    assert row["process_context_fpath"] == str(job_dpath / "process_context.json")
    assert row["process_start_timestamp"] == "2026-01-01T00:00:00+00:00"
    assert row["process_stop_timestamp"] == "2026-01-01T00:00:02+00:00"
    assert row["machine_host"] == "host-a"
    assert row["attempt_fallback_key"].startswith("fallback::")


def test_row_has_normalized_component_fields(tmp_path, monkeypatch):
    """Local row must include component-style fields aligned with official schema."""
    run_name = 'boolq:model=meta_llama-3-8b'
    job_config_fpath = _build_fake_job(
        tmp_path,
        run_entry=run_name,
        run_name=run_name,
        model='meta/llama-3-8b',
        scenario_class='helm.BoolQScenario',
        deployment='local/meta-llama-3-8b',
    )
    run_dir = job_config_fpath.parent / 'benchmark_output' / 'runs' / 'demo-suite' / run_name
    monkeypatch.setattr(index_results, '_first_run_dir', lambda _job_dpath: run_dir)

    row = index_results._row_for_job(
        job_config_fpath, fallback_host=None,
        index_generated_utc='2026-04-21T00:00:00Z',
    )

    assert row['source_kind'] == 'local'
    assert row['component_id'].startswith('local::demo-exp::')
    assert row['logical_run_key'] == run_name
    assert row['run_path'] == str(run_dir)
    assert row['run_spec_fpath'] == str(run_dir / 'run_spec.json')
    assert row['run_spec_name'] == run_name
    assert row['model'] == 'meta/llama-3-8b'
    assert row['model_deployment'] == 'local/meta-llama-3-8b'
    assert row['scenario_class'] == 'helm.BoolQScenario'
    assert row['benchmark_group'] == 'boolq'
    assert row['max_eval_instances'] == 100
    assert row['index_generated_utc'] == '2026-04-21T00:00:00Z'
    # Legacy field retained for back-compat.
    assert row['run_dir'] == str(run_dir)


def test_row_run_name_prefers_run_spec_json_name(tmp_path, monkeypatch):
    """run_name must be sourced from run_spec.json["name"] when available."""
    spec_name = 'mmlu:subject=anatomy,model=meta/llama-3-8b'
    # Deliberately give the run directory a *different* name from the spec name
    # to prove we prefer the spec.
    run_dir_basename = 'mmlu_subject_anatomy_renamed'
    job_dpath = tmp_path / 'demo-exp' / 'helm' / 'job-1'
    job_config_fpath = job_dpath / 'job_config.json'
    _write_json(job_config_fpath, {
        'helm.run_entry': spec_name,
        'helm.suite': 'demo-suite',
    })
    _write_json(job_dpath / 'adapter_manifest.json', {'status': 'computed'})
    run_dir = job_dpath / 'benchmark_output' / 'runs' / 'demo-suite' / run_dir_basename
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / 'run_spec.json', _make_run_spec(spec_name))

    monkeypatch.setattr(index_results, '_first_run_dir', lambda _j: run_dir)
    row = index_results._row_for_job(job_config_fpath, fallback_host=None)

    assert row['run_name'] == spec_name, 'run_name must come from run_spec.json["name"]'
    assert row['run_spec_name'] == spec_name
    assert Path(row['run_path']).name == run_dir_basename


def test_row_run_name_falls_back_to_basename_when_no_spec(tmp_path, monkeypatch):
    """When run_spec.json is absent, run_name falls back to run_dir basename."""
    run_dir_basename = 'boolq:model=meta_llama-3-8b'
    job_dpath = tmp_path / 'demo-exp' / 'helm' / 'job-1'
    job_config_fpath = job_dpath / 'job_config.json'
    _write_json(job_config_fpath, {
        'helm.run_entry': 'boolq:model=meta/llama-3-8b',
        'helm.suite': 'demo-suite',
    })
    _write_json(job_dpath / 'adapter_manifest.json', {'status': 'failed'})
    run_dir = job_dpath / 'benchmark_output' / 'runs' / 'demo-suite' / run_dir_basename
    run_dir.mkdir(parents=True, exist_ok=True)
    # No run_spec.json written.

    monkeypatch.setattr(index_results, '_first_run_dir', lambda _j: run_dir)
    row = index_results._row_for_job(job_config_fpath, fallback_host=None)

    assert row['run_spec_name'] is None
    assert row['run_name'] == run_dir_basename


def test_row_run_name_final_fallback_is_logical_or_entry(tmp_path, monkeypatch):
    """With no run_dir and no spec, run_name falls back to logical_run_key / run_entry."""
    job_dpath = tmp_path / 'demo-exp' / 'helm' / 'job-1'
    job_config_fpath = job_dpath / 'job_config.json'
    _write_json(job_config_fpath, {
        'helm.run_entry': 'boolq:model=meta/llama-3-8b',
        'helm.suite': 'demo-suite',
    })
    _write_json(job_dpath / 'adapter_manifest.json', {'status': 'failed'})

    monkeypatch.setattr(index_results, '_first_run_dir', lambda _j: None)
    row = index_results._row_for_job(job_config_fpath, fallback_host=None)

    assert row['run_path'] is None
    assert row['run_name'] == 'boolq:model=meta/llama-3-8b'


def test_row_run_spec_hash_is_stable_and_present(tmp_path, monkeypatch):
    """run_spec_hash must be deterministic and come from the shared helper."""
    run_name = 'mmlu:model=meta_llama-3-8b'
    job_config_fpath = _build_fake_job(
        tmp_path, run_entry=run_name, run_name=run_name,
    )
    run_dir = job_config_fpath.parent / 'benchmark_output' / 'runs' / 'demo-suite' / run_name
    monkeypatch.setattr(index_results, '_first_run_dir', lambda _job_dpath: run_dir)

    row_a = index_results._row_for_job(job_config_fpath, fallback_host=None)
    row_b = index_results._row_for_job(job_config_fpath, fallback_host=None)

    assert row_a['run_spec_hash'] is not None
    assert len(row_a['run_spec_hash']) == 64  # sha256 hex
    assert row_a['run_spec_hash'] == row_b['run_spec_hash']

    # Shared helper must produce the same hash from the raw run_spec.json.
    from eval_audit.indexing.schema import compute_run_spec_hash
    shared = compute_run_spec_hash(run_dir / 'run_spec.json')
    assert row_a['run_spec_hash'] == shared


def test_row_attempt_identity_preserved_and_first_class(tmp_path, monkeypatch):
    """attempt_uuid / attempt_identity remain first-class on normalized rows."""
    run_name = 'boolq:model=meta_llama-3-8b'
    job_config_fpath = _build_fake_job(
        tmp_path, run_entry=run_name, run_name=run_name, uuid='uuid-abc',
    )
    run_dir = job_config_fpath.parent / 'benchmark_output' / 'runs' / 'demo-suite' / run_name
    monkeypatch.setattr(index_results, '_first_run_dir', lambda _job_dpath: run_dir)

    row = index_results._row_for_job(job_config_fpath, fallback_host=None)

    assert row['attempt_uuid'] == 'uuid-abc'
    assert row['attempt_uuid_source'] == 'process_context.properties.uuid'
    assert row['attempt_identity'] == 'uuid-abc'
    assert row['attempt_identity_kind'] == 'attempt_uuid'
    # component_id must be derived from attempt_identity so it disambiguates retries.
    assert 'uuid-abc' in row['component_id']


def test_combined_component_index_is_normalized_union(tmp_path, monkeypatch):
    """Combined index contains both official and local source_kind rows, no grouping."""
    # Build an official-style CSV with two rows.
    official_fpath = tmp_path / 'official.csv'
    official_rows = [
        {
            'source_kind': 'official',
            'component_id': 'official::main::v0.2.2::boolq:model=foo',
            'logical_run_key': 'boolq:model=foo',
            'run_path': '/data/bo/runs/v0.2.2/boolq:model=foo',
            'run_spec_fpath': '/data/bo/runs/v0.2.2/boolq:model=foo/run_spec.json',
            'run_spec_name': 'boolq:model=foo',
            'run_spec_hash': 'deadbeef',
            'model': 'foo',
            'model_deployment': 'hf/foo',
            'scenario_class': 'helm.BoolQScenario',
            'benchmark_group': 'boolq',
            'max_eval_instances': None,
            'index_generated_utc': '2026-01-01T00:00:00Z',
        },
    ]
    pd.DataFrame(official_rows).to_csv(official_fpath, index=False)

    # Build one local-style row via the normalized row builder.
    run_name = 'boolq:model=meta_llama-3-8b'
    job_config_fpath = _build_fake_job(
        tmp_path, run_entry=run_name, run_name=run_name,
    )
    run_dir = job_config_fpath.parent / 'benchmark_output' / 'runs' / 'demo-suite' / run_name
    monkeypatch.setattr(index_results, '_first_run_dir', lambda _job_dpath: run_dir)
    local_row = index_results._row_for_job(
        job_config_fpath, fallback_host=None,
        index_generated_utc='2026-04-21T00:00:00Z',
    )

    combined_fpath = tmp_path / 'combined.csv'
    index_results.write_combined_component_index(
        official_index_fpath=official_fpath,
        local_rows=[local_row],
        out_fpath=combined_fpath,
    )
    df = pd.read_csv(combined_fpath)

    assert set(df['source_kind']) == {'official', 'local'}
    assert len(df) == 2
    # The union must carry the shared component columns and nothing grouped.
    from eval_audit.indexing.schema import COMMON_COMPONENT_COLUMNS
    for col in COMMON_COMPONENT_COLUMNS:
        assert col in df.columns, f'Missing common column: {col}'


def test_row_for_job_falls_back_when_process_context_uuid_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    job_dpath = tmp_path / "demo-exp" / "job-999"
    job_config_fpath = job_dpath / "job_config.json"
    _write_json(
        job_config_fpath,
        {
            "helm.run_entry": "bbh:model=openai/gpt-oss-20b",
            "helm.suite": "demo-suite",
        },
    )
    _write_json(
        job_dpath / "adapter_manifest.json",
        {
            "status": "failed",
            "timestamp": 55.0,
        },
    )
    monkeypatch.setattr(index_results, "_first_run_dir", lambda _job_dpath: None)

    row = index_results._row_for_job(job_config_fpath, fallback_host="fallback-host")

    assert row["attempt_uuid"] is None
    assert row["attempt_uuid_source"] == "missing"
    assert row["attempt_identity_kind"] == "fallback"
    assert row["attempt_identity"] == row["attempt_fallback_key"]
    assert row["process_context_source"] == "missing"
    assert row["machine_host"] == "fallback-host"
    assert "job_id=job-999" in row["attempt_fallback_key"]
