from __future__ import annotations

import json
from pathlib import Path

from helm_audit.workflows import index_results


def _write_json(fpath: Path, data: dict) -> None:
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(json.dumps(data))


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
