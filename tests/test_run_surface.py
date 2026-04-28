from __future__ import annotations

from pathlib import Path

import pytest

from eval_audit.integrations.kwdagger_bridge import (
    kwdagger_schedule_argv,
    prepare_schedule_request,
)
from eval_audit.infra.paths import repo_root
from eval_audit.manifests import builders as manifest_builders
from eval_audit.workflows import run_from_manifest as run_workflow


def _write_manifest(tmp_path: Path) -> Path:
    manifest_fpath = tmp_path / "manifest.yaml"
    manifest_fpath.write_text(
        "\n".join(
            [
                "experiment_name: demo-exp",
                "description: demo",
                "run_entries:",
                "  - boolq:model=openai/gpt2,data_augmentation=canonical",
                "suite: audit-smoke",
                "max_eval_instances: 10",
                "devices: 2,3",
                "tmux_workers: 4",
                "backend: tmux",
            ]
        )
        + "\n"
    )
    return manifest_fpath


def test_kwdagger_argv_differs_between_preview_and_execute(tmp_path: Path):
    manifest_fpath = _write_manifest(tmp_path)
    preview = prepare_schedule_request(manifest_fpath, run=False)
    execute = prepare_schedule_request(manifest_fpath, run=True)
    preview_argv = kwdagger_schedule_argv(preview)
    execute_argv = kwdagger_schedule_argv(execute)
    assert "--run=0" in preview_argv
    assert "--run=1" in execute_argv
    assert preview_argv[:-1] == execute_argv[:-1]


def test_run_from_manifest_preview_does_not_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest_fpath = _write_manifest(tmp_path)
    called = {"count": 0}

    def _unexpected_call(request):
        called["count"] += 1
        raise AssertionError("preview should not execute kwdagger")

    monkeypatch.setattr(run_workflow, "run_kwdagger_schedule", _unexpected_call)
    info = run_workflow.run_from_manifest(manifest_fpath, run=False, root_dpath=tmp_path / "results")
    assert info["mode"] == "preview"
    assert "--run=0" in info["argv"]
    assert "kwdagger schedule" in info["command"]
    assert called["count"] == 0


def test_run_from_manifest_execute_calls_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest_fpath = _write_manifest(tmp_path)
    called = {"count": 0}

    class _Proc:
        returncode = 0

    def _fake_run(request):
        called["count"] += 1
        assert request.runtime.run is True
        return _Proc()

    monkeypatch.setattr(run_workflow, "run_kwdagger_schedule", _fake_run)
    info = run_workflow.run_from_manifest(manifest_fpath, run=True, root_dpath=tmp_path / "results")
    assert info["mode"] == "execute"
    assert info["returncode"] == 0
    assert "--run=1" in info["argv"]
    assert called["count"] == 1


def test_choose_model_override_for_qwen_and_vicuna():
    qwen_entry = "gsm:model=qwen/qwen2.5-7b-instruct-turbo,stop=none"
    vicuna_entry = "boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical"
    assert manifest_builders._choose_model_override([qwen_entry], False) == (
        "configs/debug/repro_model_overrides.yaml"
    )
    assert manifest_builders._choose_model_override([vicuna_entry], False) == (
        "configs/debug/repro_model_overrides.yaml"
    )


def test_kwdagger_manifest_propagates_model_override(tmp_path: Path):
    manifest_fpath = tmp_path / "manifest.yaml"
    manifest_fpath.write_text(
        "\n".join(
            [
                "experiment_name: demo-exp",
                "description: demo",
                "run_entries:",
                "  - gsm:model=qwen/qwen2.5-7b-instruct-turbo,stop=none",
                "suite: audit-smoke",
                "max_eval_instances: 10",
                "devices: 2,3",
                "tmux_workers: 4",
                "backend: tmux",
                "model_deployments_fpath: configs/debug/repro_model_overrides.yaml",
            ]
        )
        + "\n"
    )
    request = prepare_schedule_request(manifest_fpath, run=False)
    params_text = request.params_text
    assert "helm.model_deployments_fpath" in params_text
    assert str((repo_root() / "configs/debug/repro_model_overrides.yaml").resolve()) in params_text


def test_qwen35_vllm_manifest_propagates_local_override_and_local_path():
    manifest_fpath = repo_root() / "configs/qwen35_vllm_smoke_manifest.yaml"
    request = prepare_schedule_request(manifest_fpath, run=False)
    params_text = request.params_text
    assert "qwen/qwen3.5-9b" in params_text
    assert "helm.local_path" in params_text
    assert "prod_env" in params_text
    assert "helm.model_deployments_fpath" in params_text
    assert str(
        (repo_root() / "configs/local_models/qwen35_9b_vllm/model_deployments.yaml").resolve()
    ) in params_text
