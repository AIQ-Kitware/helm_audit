from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from helm_audit.reports import core_metrics


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def test_core_metrics_single_run_uses_manifests_and_writes_comparability_block(tmp_path, monkeypatch):
    report_dpath = tmp_path / "report"
    report_dpath.mkdir()
    local_run = tmp_path / "runs" / "local-run"
    official_run = tmp_path / "runs" / "official-run"
    for run_dpath in [local_run, official_run]:
        run_dpath.mkdir(parents=True)
        (run_dpath / "run_spec.json").write_text(
            json.dumps(
                {
                    "name": "toy-run",
                    "adapter_spec": {
                        "model": "toy-model",
                        "model_deployment": "toy-deploy",
                        "max_eval_instances": 100,
                    },
                    "scenario_spec": {"class_name": "helm.benchmark.scenarios.toy.ToyScenario"},
                }
            )
        )

    components_manifest = {
        "report_dpath": str(report_dpath),
        "run_entry": "toy:model=x",
        "experiment_name": "toy-exp",
        "components": [
            {
                "component_id": "local-attempt-a",
                "run_path": str(local_run),
                "job_path": None,
                "source_kind": "local",
                "tags": ["local"],
                "display_name": "local 1: local-run",
                "attempt_uuid": "attempt-a",
                "attempt_identity": "attempt-a",
                "machine_host": "host-a",
                "experiment_name": "toy-exp",
                "max_eval_instances": 100,
            },
            {
                "component_id": "official-toy-run",
                "run_path": str(official_run),
                "job_path": None,
                "source_kind": "official",
                "tags": ["official"],
                "display_name": "official: official-run",
                "attempt_uuid": None,
                "attempt_identity": None,
                "machine_host": None,
                "experiment_name": "toy-exp",
                "max_eval_instances": 100,
            },
        ],
    }
    comparisons_manifest = {
        "report_dpath": str(report_dpath),
        "run_entry": "toy:model=x",
        "experiment_name": "toy-exp",
        "comparisons": [
            {
                "comparison_id": "official_vs_local",
                "comparison_kind": "official_vs_local",
                "component_ids": ["official-toy-run", "local-attempt-a"],
                "enabled": True,
                "reference_component_id": "official-toy-run",
                "notes": None,
                "caveats": None,
            }
        ],
    }
    components_fpath = report_dpath / "components_manifest.latest.json"
    comparisons_fpath = report_dpath / "comparisons_manifest.latest.json"
    _write_manifest(components_fpath, components_manifest)
    _write_manifest(comparisons_fpath, comparisons_manifest)

    thresholds = [0.0, 1e-3, 1e-2, 0.1, 1.0]
    right_pair = {
        "label": "official_vs_local",
        "core_metrics": ["exact_match"],
        "diagnosis": {"label": "core_metric_drift", "primary_reason_names": ["core_metric_drift"]},
        "run_level": {
            "n_rows": 1,
            "overall_quantiles": {"abs_delta": {"min": 0.2, "p50": 0.2, "p90": 0.2, "p95": 0.2, "p99": 0.2, "max": 0.2}},
            "by_metric": [{"metric": "exact_match", "count": 1, "abs_delta": {"p50": 0.2, "p90": 0.2, "p95": 0.2, "p99": 0.2, "max": 0.2}}],
            "agreement_vs_abs_tol": [{"abs_tol": t, "agree_ratio": 0.0 if t == 0.0 else 1.0, "matched": 1, "count": 1} for t in thresholds],
        },
        "instance_level": {
            "n_rows": 2,
            "overall_quantiles": {"abs_delta": {"min": 0.0, "p50": 0.25, "p90": 0.5, "p95": 0.5, "p99": 0.5, "max": 0.5}},
            "by_metric": [{"metric": "exact_match", "count": 2, "abs_delta": {"p50": 0.25, "p90": 0.5, "p95": 0.5, "p99": 0.5, "max": 0.5}}],
            "agreement_vs_abs_tol": [{"abs_tol": t, "agree_ratio": 0.5 if t < 0.1 else 1.0, "matched": 1, "count": 2} for t in thresholds],
            "per_metric_agreement": {
                "exact_match": [{"abs_tol": t, "agree_ratio": 0.5 if t < 0.1 else 1.0} for t in thresholds]
            },
        },
        "_instance_rows": [
            {"metric": "exact_match", "a": 1.0, "b": 0.5, "abs_delta": 0.5, "rel_delta": 0.5},
            {"metric": "exact_match", "a": 0.0, "b": 0.0, "abs_delta": 0.0, "rel_delta": 0.0},
        ],
    }

    monkeypatch.setattr(core_metrics, "_infer_run_spec_name", lambda *args: "toy-run")
    monkeypatch.setattr(core_metrics, "_build_pair", lambda *args, **kwargs: dict(right_pair))
    monkeypatch.setattr(
        core_metrics,
        "_run_diagnostics",
        lambda run_path: {
            "n_request_states": 2,
            "n_with_completions": 2,
            "empty_completion_count": 0,
            "empty_completion_rate": 0.0,
            "output_token_count": {"mean": 3.0, "p50": 3.0, "p90": 3.0, "max": 3.0},
            "stats_means": {"num_output_tokens": {"test": 3.0}, "finish_reason_unknown": {"test": 0.0}},
        },
    )
    monkeypatch.setattr(
        core_metrics,
        "_single_run_instance_core_rows",
        lambda run_path, label: pd.DataFrame(
            [
                {"run": label, "metric": "exact_match", "value": 1.0 if "local" in label else 0.5},
                {"run": label, "metric": "exact_match", "value": 0.0},
            ]
        ),
    )
    monkeypatch.setattr(
        core_metrics,
        "_single_run_core_stat_index",
        lambda run_path: {"exact_match": SimpleNamespace(metric="exact_match", mean=1.0 if "local" in run_path else 0.5)},
    )

    core_metrics.main(
        [
            "--report-dpath", str(report_dpath),
            "--components-manifest", str(components_fpath),
            "--comparisons-manifest", str(comparisons_fpath),
        ]
    )

    for name in [
        "core_metric_report.latest.png",
        "core_metric_management_summary.latest.txt",
        "core_metric_distributions.latest.png",
        "core_metric_overlay_distributions.latest.png",
        "core_metric_ecdfs.latest.png",
        "core_metric_per_metric_agreement.latest.png",
        "core_runlevel_table.latest.csv",
    ]:
        assert (report_dpath / name).exists(), name
    assert not (report_dpath / "core_metric_three_run_distributions.latest.png").exists()

    text = (report_dpath / "core_metric_management_summary.latest.txt").read_text()
    assert f"report_dpath: {report_dpath}" in text
    assert f"components_manifest: {components_fpath}" in text
    assert f"comparisons_manifest: {comparisons_fpath}" in text
    assert "selected_components:" in text
    assert "comparisons:" in text
    assert "comparability:" in text


def test_diagnostic_flags_use_manifest_semantics_not_component_order():
    components = [
        {
            "component_id": "local-attempt-a",
            "source_kind": "local",
            "tags": ["local"],
        },
        {
            "component_id": "official-run",
            "source_kind": "official",
            "tags": ["official"],
        },
    ]
    comparisons = [
        {
            "comparison_id": "official_vs_local",
            "comparison_kind": "official_vs_local",
            "component_ids": ["local-attempt-a", "official-run"],
            "enabled": True,
            "reference_component_id": "official-run",
        }
    ]
    run_diagnostics = {
        "local-attempt-a": {
            "empty_completion_rate": 0.3,
            "output_token_count": {"mean": 3.0},
        },
        "official-run": {
            "empty_completion_rate": 0.0,
            "output_token_count": {"mean": 3.0},
        },
    }

    flags = core_metrics._diagnostic_flags(run_diagnostics, components, comparisons)

    assert "official_vs_local:empty_completion_pathology" in flags
