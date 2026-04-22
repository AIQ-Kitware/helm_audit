from __future__ import annotations

import json
from pathlib import Path

from helm_audit.infra.logging import rich_link
from helm_audit.reports.core_packet import comparison_sample_latest_name
from helm_audit.workflows import analyze_experiment, build_reports_summary


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_core_report_packet(
    report_dir: Path,
    *,
    experiment_name: str,
    run_entry: str,
    single_run: bool,
) -> None:
    local_a = report_dir / "runs" / "local_a"
    official = report_dir / "runs" / "official"
    local_a.mkdir(parents=True, exist_ok=True)
    official.mkdir(parents=True, exist_ok=True)
    local_b = report_dir / "runs" / "local_b"
    if not single_run:
        local_b.mkdir(parents=True, exist_ok=True)

    components = [
        {
            "component_id": "local-a",
            "run_path": str(local_a),
            "job_path": None,
            "source_kind": "local",
            "tags": ["local"],
            "display_name": "local 1: local_a",
            "attempt_uuid": "attempt-a",
            "attempt_identity": "attempt-a",
            "machine_host": "host-a",
            "experiment_name": experiment_name,
        },
        {
            "component_id": "official-run",
            "run_path": str(official),
            "job_path": None,
            "source_kind": "official",
            "tags": ["official"],
            "display_name": "official: official",
            "attempt_uuid": None,
            "attempt_identity": None,
            "machine_host": None,
            "experiment_name": experiment_name,
        },
    ]
    if not single_run:
        components.insert(
            1,
            {
                "component_id": "local-b",
                "run_path": str(local_b),
                "job_path": None,
                "source_kind": "local",
                "tags": ["local", "repeat"],
                "display_name": "local 2: local_b",
                "attempt_uuid": "attempt-b",
                "attempt_identity": "attempt-b",
                "machine_host": "host-b",
                "experiment_name": experiment_name,
            },
        )

    comparisons = [
        {
            "comparison_id": "official_vs_local",
            "comparison_kind": "official_vs_local",
            "component_ids": ["official-run", "local-a"],
            "enabled": True,
            "reference_component_id": "official-run",
        }
    ]
    if not single_run:
        comparisons.append(
            {
                "comparison_id": "local_repeat",
                "comparison_kind": "local_repeat",
                "component_ids": ["local-a", "local-b"],
                "enabled": True,
                "reference_component_id": "local-a",
            }
        )

    _write_json(
        report_dir / "components_manifest.latest.json",
        {
            "report_dpath": str(report_dir),
            "run_entry": run_entry,
            "experiment_name": experiment_name,
            "components": components,
        },
    )
    _write_json(
        report_dir / "comparisons_manifest.latest.json",
        {
            "report_dpath": str(report_dir),
            "run_entry": run_entry,
            "experiment_name": experiment_name,
            "comparisons": comparisons,
        },
    )

    report_payload = {
        "generated_utc": "20260421T000000Z",
        "run_spec_name": "toy-run",
        "diagnostic_flags": [],
        "run_diagnostics": {
            "local-a": {"empty_completion_rate": 0.2, "output_token_count": {"mean": 4.0}},
            "official-run": {"empty_completion_rate": 0.0, "output_token_count": {"mean": 6.0}},
            **(
                {"local-b": {"empty_completion_rate": 0.1, "output_token_count": {"mean": 5.0}}}
                if not single_run else {}
            ),
        },
        "pairs": [
            {
                "comparison_id": "official_vs_local",
                "comparison_kind": "official_vs_local",
                "core_metrics": ["exact_match"],
                "diagnosis": {"label": "core_metric_drift", "primary_reason_names": ["core_metric_drift"]},
                "run_level": {"overall_quantiles": {"abs_delta": {"p90": 0.2, "max": 0.3}}},
                "instance_level": {
                    "agreement_vs_abs_tol": [
                        {"abs_tol": 0.0, "agree_ratio": 0.4},
                        {"abs_tol": 0.001, "agree_ratio": 0.4},
                        {"abs_tol": 0.05, "agree_ratio": 0.8},
                        {"abs_tol": 0.1, "agree_ratio": 0.9},
                    ],
                    "per_metric_agreement": {},
                },
            },
            *(
                [
                    {
                        "comparison_id": "local_repeat",
                        "comparison_kind": "local_repeat",
                        "core_metrics": ["exact_match"],
                        "diagnosis": {"label": "stable", "primary_reason_names": ["stable"]},
                        "run_level": {"overall_quantiles": {"abs_delta": {"p90": 0.01, "max": 0.02}}},
                        "instance_level": {
                            "agreement_vs_abs_tol": [{"abs_tol": 0.0, "agree_ratio": 1.0}],
                            "per_metric_agreement": {},
                        },
                    }
                ]
                if not single_run else []
            ),
        ],
    }
    _write_json(report_dir / "core_metric_report.latest.json", report_payload)
    _write_json(
        report_dir / "warnings.latest.json",
        {
            "packet_warnings": ["suspicious_case"] if single_run else ["suspicious_case", "repeat_case"],
            "packet_caveats": [],
            "comparisons": [
                {
                    "comparison_id": item["comparison_id"],
                    "enabled": item["enabled"],
                    "disabled_reason": item.get("disabled_reason"),
                    "warnings": item.get("warnings", []),
                    "caveats": item.get("caveats", []),
                }
                for item in comparisons
            ],
        },
    )
    (report_dir / "warnings.latest.txt").write_text("packet_warnings:\n  - suspicious_case\n")


def test_analyze_experiment_summary_uses_packet_manifests_for_single_and_multi_run(tmp_path):
    single_report = tmp_path / "single_report"
    multi_report = tmp_path / "multi_report"
    _write_core_report_packet(single_report, experiment_name="exp-single", run_entry="bench:model=single", single_run=True)
    _write_core_report_packet(multi_report, experiment_name="exp-multi", run_entry="bench:model=multi", single_run=False)

    single_summary = analyze_experiment._summarize_core_report(
        single_report / "core_metric_report.latest.json",
        experiment_name="exp-single",
    )
    multi_summary = analyze_experiment._summarize_core_report(
        multi_report / "core_metric_report.latest.json",
        experiment_name="exp-multi",
    )

    assert single_summary["run_entry"] == "bench:model=single"
    assert single_summary["analysis_single_run"] is True
    assert single_summary["components_manifest"].endswith("components_manifest.latest.json")
    assert single_summary["comparisons_manifest"].endswith("comparisons_manifest.latest.json")
    assert single_summary["warnings_manifest"].endswith("warnings.latest.json")
    assert multi_summary["run_entry"] == "bench:model=multi"
    assert multi_summary["analysis_single_run"] is False
    assert multi_summary["repeat_instance_agree_0"] == 1.0


def test_build_reports_summary_loads_rows_from_packet_manifests_without_selection_sidecar(tmp_path, monkeypatch):
    new_root = tmp_path / "reports" / "core-run-analysis"
    old_root = tmp_path / "compat"
    report_dir = new_root / "experiment-analysis-exp-a" / "core-reports" / "core-metrics-bench-model-a"
    _write_core_report_packet(report_dir, experiment_name="exp-a", run_entry="bench:model=a", single_run=False)

    monkeypatch.setattr(build_reports_summary, "core_run_reports_root", lambda: new_root)
    monkeypatch.setattr(build_reports_summary, "compat_core_run_reports_root", lambda: old_root)

    rows = build_reports_summary._load_all_repro_rows()

    assert len(rows) == 1
    row = rows[0]
    assert row["experiment_name"] == "exp-a"
    assert row["run_entry"] == "bench:model=a"
    assert row["analysis_single_run"] is False
    assert row["official_diagnosis"] == "core_metric_drift"
    assert row["repeat_diagnosis"] == "stable"
    assert row["components_manifest"].endswith("components_manifest.latest.json")
    assert row["comparisons_manifest"].endswith("comparisons_manifest.latest.json")


def test_sample_artifact_lookup_is_derived_from_packet_comparison_ids(tmp_path):
    report_dir = tmp_path / "report"
    _write_core_report_packet(report_dir, experiment_name="exp-a", run_entry="bench:model=a", single_run=False)

    artifact_names = build_reports_summary._prioritized_example_artifact_names(report_dir)

    assert "components_manifest.latest.json" in artifact_names
    assert "comparisons_manifest.latest.json" in artifact_names
    assert "warnings.latest.json" in artifact_names
    assert "warnings.latest.txt" in artifact_names
    assert comparison_sample_latest_name("official_vs_local") in artifact_names
    assert comparison_sample_latest_name("local_repeat") in artifact_names
    assert "instance_samples_official_vs_kwdagger.latest.txt" not in artifact_names


def test_prioritized_breakdown_text_uses_rich_links_for_paths():
    summary = {
        "definitions": {"rank_population": "all analyzed rows"},
        "rows": [
            {
                "priority_rank": 1,
                "dimension": "benchmark",
                "dimension_value": "boolq",
                "bucket_class": "good",
                "n_analyzed": 3,
                "target_bucket_count": 2,
                "target_bucket_share": 2 / 3,
                "selection_reason": "interesting slice",
                "n_attempted": 3,
                "n_completed": 3,
                "dominant_bucket": "high_agreement_0.95+",
                "interesting_flags": [],
                "breakdown_dir": "/tmp/breakdown",
                "breakdown_index_dir": "/tmp/breakdown-index",
                "example_run_entries": ["bench:model=a"],
                "example_report_dirs": ["/tmp/report-a"],
            }
        ],
    }

    lines = build_reports_summary._format_prioritized_breakdown_summary_text(
        scope_title="toy-scope",
        generated_utc="20260421T000000Z",
        summary=summary,
    )
    text = "\n".join(lines)

    assert rich_link("/tmp/breakdown") in text
    assert rich_link("/tmp/breakdown-index") in text
    assert rich_link("/tmp/report-a") in text
