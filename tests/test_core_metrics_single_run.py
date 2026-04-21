from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from helm_audit.reports import core_metrics


def test_core_metrics_single_run_still_emits_meaningful_artifacts(tmp_path, monkeypatch):
    report_dpath = tmp_path / "report"

    thresholds = [0.0, 1e-3, 1e-2, 0.1, 1.0]
    right_pair = {
        "label": "official_vs_kwdagger",
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
    monkeypatch.setattr(core_metrics, "_build_pair", lambda *args, **kwargs: right_pair)
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
                {"run": label, "metric": "exact_match", "value": 1.0 if "official" not in label else 0.5},
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
            "--left-run-a", "local-run",
            "--left-run-b", "local-run",
            "--left-label", "kwdagger_repeat",
            "--right-run-a", "official-run",
            "--right-run-b", "local-run",
            "--right-label", "official_vs_kwdagger",
            "--report-dpath", str(report_dpath),
            "--single-run",
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
