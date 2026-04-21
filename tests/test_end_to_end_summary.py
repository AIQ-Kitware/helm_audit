from __future__ import annotations

from helm_audit.workflows.build_reports_summary import (
    ATTEMPTED_LABEL,
    FILTER_SELECTION_EXCLUDED_LABEL,
    FILTER_SELECTION_SELECTED_LABEL,
    NOT_ATTEMPTED_LABEL,
    _build_off_story_summary,
    _build_prioritized_breakdown_summary,
    _build_attempted_to_repro_rows,
    _build_end_to_end_funnel_rows,
    _build_filter_to_attempt_rows,
    _build_filter_selection_by_model_rows,
    _build_run_multiplicity_summary,
    _publish_prioritized_examples_tree,
    _repair_prioritized_example_reports,
)


def test_end_to_end_funnel_rows_cover_excluded_unrun_and_analyzed_cases():
    filter_inventory_rows = [
        {
            "run_spec_name": "bench:model=a",
            "selection_status": "excluded",
            "candidate_pool": "complete-run",
            "failure_reasons": ["too-large"],
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "bench:model=b",
            "selection_status": "selected",
            "candidate_pool": "complete-run",
            "failure_reasons": [],
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "bench:model=c",
            "selection_status": "selected",
            "candidate_pool": "complete-run",
            "failure_reasons": [],
            "is_structurally_incomplete": False,
        },
    ]
    scope_rows = [
        {
            "experiment_name": "demo-exp",
            "run_entry": "bench:model=c",
            "has_run_spec": "True",
            "status": "computed",
            "manifest_timestamp": "10",
        }
    ]
    repro_rows = [
        {
            "experiment_name": "demo-exp",
            "run_entry": "bench:model=c",
            "official_instance_agree_0": 1.0,
            "official_instance_agree_001": 1.0,
            "official_instance_agree_01": 1.0,
            "official_instance_agree_005": 1.0,
        }
    ]

    rows = _build_end_to_end_funnel_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_0",
    )
    excluded = next(row for row in rows if row.get("size_gate") == "excluded: exceeds size budget")
    assert "execution_stage" not in excluded
    assert "analysis_stage" not in excluded
    assert "reproduction_stage" not in excluded

    selected_rows = [row for row in rows if row.get("selection_gate") == FILTER_SELECTION_SELECTED_LABEL]
    unrun = next(row for row in selected_rows if row["execution_stage"] == "not_run_in_scope")
    analyzed = next(row for row in selected_rows if row["execution_stage"] == "completed_with_run_artifacts")

    assert "analysis_stage" not in unrun
    assert "reproduction_stage" not in unrun

    assert analyzed["analysis_stage"] == "analyzed"
    assert analyzed["reproduction_stage"] == "exact_or_near_exact"


def test_filter_to_attempt_rows_split_selected_and_attempted_states():
    filter_inventory_rows = [
        {
            "run_spec_name": "bench:model=a",
            "selection_status": "excluded",
            "candidate_pool": "complete-run",
            "failure_reasons": [],
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "bench:model=b",
            "selection_status": "selected",
            "candidate_pool": "eligible-model",
            "failure_reasons": [],
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "bench:model=c",
            "selection_status": "selected",
            "candidate_pool": "eligible-model",
            "failure_reasons": [],
            "is_structurally_incomplete": False,
        },
    ]
    scope_rows = [
        {
            "experiment_name": "demo-exp",
            "run_entry": "bench:model=c",
            "has_run_spec": "True",
            "status": "computed",
            "manifest_timestamp": "10",
        }
    ]

    rows = _build_filter_to_attempt_rows(filter_inventory_rows, scope_rows)
    excluded = next(row for row in rows if row.get("selection_gate") == FILTER_SELECTION_EXCLUDED_LABEL)
    assert "attempt_stage" not in excluded

    selected_rows = [row for row in rows if row.get("selection_gate") == FILTER_SELECTION_SELECTED_LABEL]
    assert {row["attempt_stage"] for row in selected_rows} == {ATTEMPTED_LABEL, NOT_ATTEMPTED_LABEL}


def test_filter_to_attempt_rows_surface_missing_model_metadata_explicitly():
    filter_inventory_rows = [
        {
            "run_spec_name": "cub200:model=openai/dalle-2",
            "selection_status": "excluded",
            "candidate_pool": "complete-run",
            "failure_reasons": ["missing-model-metadata"],
            "is_structurally_incomplete": False,
        },
    ]
    rows = _build_filter_to_attempt_rows(filter_inventory_rows, [])
    assert rows == [
        {
            "structural_gate": "kept: structurally complete",
            "metadata_gate": "excluded: missing model metadata",
        }
    ]


def test_attempted_to_repro_rows_start_from_attempted_runs_only():
    filter_inventory_rows = [
        {
            "run_spec_name": "bench:model=b",
            "selection_status": "selected",
            "candidate_pool": "eligible-model",
            "failure_reasons": [],
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "bench:model=c",
            "selection_status": "selected",
            "candidate_pool": "eligible-model",
            "failure_reasons": [],
            "is_structurally_incomplete": False,
        },
    ]
    scope_rows = [
        {
            "experiment_name": "demo-exp",
            "run_entry": "bench:model=c",
            "has_run_spec": "True",
            "status": "computed",
            "manifest_timestamp": "10",
        }
    ]
    repro_rows = [
        {
            "experiment_name": "demo-exp",
            "run_entry": "bench:model=c",
            "official_instance_agree_0": 1.0,
            "official_instance_agree_001": 1.0,
            "official_instance_agree_01": 1.0,
            "official_instance_agree_005": 1.0,
        }
    ]

    rows = _build_attempted_to_repro_rows(
        filter_inventory_rows,
        scope_rows,
        repro_rows,
        tol_key="official_instance_agree_0",
    )
    assert len(rows) == 1
    assert rows[0]["execution_stage"] == "completed_with_run_artifacts"
    assert rows[0]["analysis_stage"] == "analyzed"
    assert rows[0]["reproduction_stage"] == "exact_or_near_exact"


def test_filter_selection_by_model_rows_separate_selected_and_excluded_counts():
    rows = _build_filter_selection_by_model_rows(
        [
            {"model": "model-a", "selection_status": "selected"},
            {"model": "model-a", "selection_status": "excluded"},
            {"model": "model-a", "selection_status": "excluded"},
            {"model": "model-b", "selection_status": "selected"},
            {"model": "model-b", "selection_status": "selected"},
            {"model": "model-c", "selection_status": "excluded"},
        ]
    )

    assert rows == [
        {"model": "model-a", "selection_status": "excluded", "count": 2},
        {"model": "model-a", "selection_status": "selected", "count": 1},
        {"model": "model-b", "selection_status": "selected", "count": 2},
        {"model": "model-c", "selection_status": "excluded", "count": 1},
    ]


def test_off_story_summary_surfaces_stage_counts_and_registry_provenance():
    filter_inventory_rows = [
        {
            "run_spec_name": "bbh:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "bbh",
            "scenario": "bbh",
            "selection_status": "selected",
            "expected_local_served": True,
            "replaces_helm_deployment": None,
            "local_registry_source": "preset:gpt_oss_20b_vllm",
        },
        {
            "run_spec_name": "mmlu:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "mmlu",
            "scenario": "mmlu",
            "selection_status": "selected",
            "expected_local_served": True,
            "replaces_helm_deployment": None,
            "local_registry_source": "preset:gpt_oss_20b_vllm",
        },
        {
            "run_spec_name": "bbh:model=qwen/qwen2.5-7b-instruct-turbo",
            "model": "qwen/qwen2.5-7b-instruct-turbo",
            "benchmark": "bbh",
            "scenario": "bbh",
            "selection_status": "selected",
            "expected_local_served": True,
            "replaces_helm_deployment": "qwen/qwen2.5-7b-instruct-turbo",
            "local_registry_source": "preset:small_models_kubeai_overnight",
        },
    ]
    scope_rows = [
        {
            "experiment_name": "demo-exp",
            "run_entry": "bbh:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "bbh",
            "has_run_spec": "True",
        },
        {
            "experiment_name": "demo-exp",
            "run_entry": "mmlu:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "mmlu",
            "has_run_spec": "False",
        },
        {
            "experiment_name": "demo-exp",
            "run_entry": "bbh:model=qwen/qwen2.5-7b-instruct-turbo",
            "model": "qwen/qwen2.5-7b-instruct-turbo",
            "benchmark": "bbh",
            "has_run_spec": "True",
        },
    ]
    repro_rows = [
        {
            "experiment_name": "demo-exp",
            "run_entry": "bbh:model=openai/gpt-oss-20b",
        }
    ]

    summary = _build_off_story_summary(
        filter_inventory_rows=filter_inventory_rows,
        scope_rows=scope_rows,
        repro_rows=repro_rows,
    )

    assert summary["headline_counts"]["off_story"] == {
        "n_models": 1,
        "selected_run_entries": 2,
        "attempted_run_entries": 2,
        "completed_run_entries": 1,
        "analyzed_run_entries": 1,
    }
    assert summary["headline_counts"]["on_story"]["n_models"] == 1
    assert len(summary["rows"]) == 1
    row = summary["rows"][0]
    assert row["model"] == "openai/gpt-oss-20b"
    assert row["local_registry_source"] == "preset:gpt_oss_20b_vllm"
    assert row["replaces_helm_deployment"] is None
    assert row["n_selected_run_entries"] == 2
    assert row["n_attempted_run_entries"] == 2
    assert row["n_completed_run_entries"] == 1
    assert row["n_analyzed_run_entries"] == 1


def test_run_multiplicity_summary_tracks_attempt_identity_and_analysis_selection():
    filter_inventory_rows = [
        {
            "run_spec_name": "mmlu:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "mmlu",
            "scenario": "mmlu",
            "selection_status": "selected",
            "expected_local_served": True,
            "replaces_helm_deployment": None,
            "local_registry_source": "preset:gpt_oss_20b_vllm",
        }
    ]
    scope_rows = [
        {
            "experiment_name": "exp-a",
            "job_id": "job-1",
            "run_entry": "mmlu:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "mmlu",
            "machine_host": "host-a",
            "manifest_timestamp": "10",
            "has_run_spec": "True",
            "run_dir": "/runs/a1",
            "attempt_uuid": "uuid-a",
            "attempt_identity": "uuid-a",
            "attempt_identity_kind": "attempt_uuid",
            "attempt_fallback_key": "fallback::job-1",
        },
        {
            "experiment_name": "exp-a",
            "job_id": "job-2",
            "run_entry": "mmlu:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "mmlu",
            "machine_host": "host-b",
            "manifest_timestamp": "20",
            "has_run_spec": "True",
            "run_dir": "/runs/a2",
            "attempt_uuid": "",
            "attempt_identity": "",
            "attempt_identity_kind": "",
            "attempt_fallback_key": "fallback::job-2",
        },
        {
            "experiment_name": "exp-b",
            "job_id": "job-3",
            "run_entry": "mmlu:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "mmlu",
            "machine_host": "host-a",
            "manifest_timestamp": "30",
            "has_run_spec": "False",
            "run_dir": "/runs/a3",
            "attempt_uuid": "uuid-c",
            "attempt_identity": "uuid-c",
            "attempt_identity_kind": "attempt_uuid",
            "attempt_fallback_key": "fallback::job-3",
        },
    ]
    repro_rows = [
        {
            "experiment_name": "exp-a",
            "run_entry": "mmlu:model=openai/gpt-oss-20b",
            "analysis_selected_run_dirs": ["/runs/a1", "/runs/a2"],
            "report_dir": "/reports/r1",
        }
    ]

    summary = _build_run_multiplicity_summary(
        filter_inventory_rows=filter_inventory_rows,
        scope_rows=scope_rows,
        repro_rows=repro_rows,
    )

    assert summary["headline_counts"] == {
        "n_logical_runs": 1,
        "n_logical_runs_with_multiple_rows": 1,
        "n_logical_runs_with_multiple_completed_rows": 1,
        "n_logical_runs_with_multiple_analyzed_rows": 1,
        "n_logical_runs_with_ambiguous_analyzed_matching": 0,
        "n_logical_runs_spanning_multiple_machines": 1,
        "n_logical_runs_spanning_multiple_experiments": 1,
        "n_logical_runs_with_multiple_manifest_timestamps": 1,
        "n_logical_runs_with_multiple_attempt_ids": 1,
        "n_logical_runs_with_multiple_attempt_uuids": 1,
    }
    row = summary["rows"][0]
    assert row["logical_run_key"] == "mmlu:model=openai/gpt-oss-20b"
    assert row["n_rows"] == 3
    assert row["n_completed_rows"] == 2
    assert row["n_analyzed_rows"] == 2
    assert row["n_experiments"] == 2
    assert row["n_machines"] == 2
    assert row["n_attempt_ids"] == 3
    assert row["n_attempt_uuids"] == 2
    assert row["n_rows_without_attempt_uuid"] == 1
    assert row["latest_attempt_identity"] == "uuid-c"
    assert "fallback::job-2" in row["fallback_attempt_ids"]


def test_run_multiplicity_summary_marks_legacy_multi_completed_groups_ambiguous():
    filter_inventory_rows = [
        {
            "run_spec_name": "gsm8k:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "gsm8k",
            "scenario": "gsm8k",
            "selection_status": "selected",
            "expected_local_served": True,
            "replaces_helm_deployment": None,
            "local_registry_source": "preset:gpt_oss_20b_vllm",
        }
    ]
    scope_rows = [
        {
            "experiment_name": "legacy-exp",
            "job_id": "job-1",
            "run_entry": "gsm8k:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "gsm8k",
            "machine_host": "host-a",
            "manifest_timestamp": "10",
            "has_run_spec": "True",
            "run_dir": "/runs/g1",
            "attempt_fallback_key": "fallback::job-1",
        },
        {
            "experiment_name": "legacy-exp",
            "job_id": "job-2",
            "run_entry": "gsm8k:model=openai/gpt-oss-20b",
            "model": "openai/gpt-oss-20b",
            "benchmark": "gsm8k",
            "machine_host": "host-b",
            "manifest_timestamp": "20",
            "has_run_spec": "True",
            "run_dir": "/runs/g2",
            "attempt_fallback_key": "fallback::job-2",
        },
    ]
    repro_rows = [
        {
            "experiment_name": "legacy-exp",
            "run_entry": "gsm8k:model=openai/gpt-oss-20b",
            "report_dir": "/reports/legacy",
        }
    ]

    summary = _build_run_multiplicity_summary(
        filter_inventory_rows=filter_inventory_rows,
        scope_rows=scope_rows,
        repro_rows=repro_rows,
    )

    assert summary["headline_counts"]["n_logical_runs_with_ambiguous_analyzed_matching"] == 1
    row = summary["rows"][0]
    assert row["n_completed_rows"] == 2
    assert row["n_analyzed_rows"] == 0
    assert row["n_ambiguous_analyzed_candidates"] == 2
    assert row["analyzed_match_status_counts"]["ambiguous_legacy_group_multi_completed"] == 2


def test_prioritized_breakdown_summary_ranks_and_points_to_actionable_paths(tmp_path):
    level_002 = tmp_path / "level_002"
    enriched_rows = [
        {
            "experiment_name": "exp-good",
            "run_entry": "bench_good:model=model-a",
            "benchmark": "bench_good",
            "model": "model-a",
            "machine_host": "host-a",
            "suite": "suite-1",
            "has_run_spec": "True",
            "storyline_status": "on_story",
            "logical_run_key": "bench_good:model=model-a",
            "repro_report_dir": "/reports/good",
        },
        {
            "experiment_name": "exp-mid",
            "run_entry": "bench_mid:model=model-a",
            "benchmark": "bench_mid",
            "model": "model-a",
            "machine_host": "host-b",
            "suite": "suite-1",
            "has_run_spec": "True",
            "storyline_status": "on_story",
            "logical_run_key": "bench_mid:model=model-a",
            "repro_report_dir": "/reports/mid",
        },
        {
            "experiment_name": "exp-bad",
            "run_entry": "bench_bad:model=model-b",
            "benchmark": "bench_bad",
            "model": "model-b",
            "machine_host": "host-c",
            "suite": "suite-2",
            "has_run_spec": "True",
            "storyline_status": "off_story",
            "logical_run_key": "bench_bad:model=model-b",
            "repro_report_dir": "/reports/bad",
        },
    ]
    repro_rows = [
        {
            "experiment_name": "exp-good",
            "run_entry": "bench_good:model=model-a",
            "report_dir": "/reports/good",
            "official_instance_agree_bucket": "exact_or_near_exact",
            "official_instance_agree_005": 0.99,
        },
        {
            "experiment_name": "exp-mid",
            "run_entry": "bench_mid:model=model-a",
            "report_dir": "/reports/mid",
            "official_instance_agree_bucket": "moderate_agreement_0.80+",
            "official_instance_agree_005": 0.84,
        },
        {
            "experiment_name": "exp-bad",
            "run_entry": "bench_bad:model=model-b",
            "report_dir": "/reports/bad",
            "official_instance_agree_bucket": "low_agreement_0.00+",
            "official_instance_agree_005": 0.22,
        },
    ]
    run_multiplicity_summary = {
        "rows": [
            {
                "logical_run_key": "bench_good:model=model-a",
                "n_attempt_ids": 1,
                "n_rows": 1,
                "n_machines": 1,
                "n_ambiguous_analyzed_candidates": 0,
            },
            {
                "logical_run_key": "bench_mid:model=model-a",
                "n_attempt_ids": 2,
                "n_rows": 2,
                "n_machines": 1,
                "n_ambiguous_analyzed_candidates": 0,
            },
            {
                "logical_run_key": "bench_bad:model=model-b",
                "n_attempt_ids": 2,
                "n_rows": 2,
                "n_machines": 2,
                "n_ambiguous_analyzed_candidates": 1,
            },
        ]
    }

    summary = _build_prioritized_breakdown_summary(
        enriched_rows=enriched_rows,
        repro_rows=repro_rows,
        run_multiplicity_summary=run_multiplicity_summary,
        breakdown_dims=["benchmark", "model", "machine_host", "experiment_name", "suite"],
        level_002=level_002,
    )

    good_rows = [row for row in summary["rows"] if row["bucket_class"] == "good"]
    mid_rows = [row for row in summary["rows"] if row["bucket_class"] == "mid"]
    bad_rows = [row for row in summary["rows"] if row["bucket_class"] == "bad"]
    flagged_rows = [row for row in summary["rows"] if row["bucket_class"] == "flagged"]

    assert good_rows[0]["dimension"] == "benchmark"
    assert good_rows[0]["dimension_value"] == "bench_good"
    assert good_rows[0]["example_report_dirs"] == ["/reports/good"]
    assert good_rows[0]["breakdown_dir"].endswith("/level_002/breakdowns/by_benchmark/bench_good")
    assert "bench_good" in summary["include_values_by_dim"]["benchmark"]

    assert mid_rows[0]["dimension"] == "benchmark"
    assert mid_rows[0]["dimension_value"] == "bench_mid"
    assert mid_rows[0]["has_multiplicity_signal"] is True

    assert bad_rows[0]["dimension"] == "benchmark"
    assert bad_rows[0]["dimension_value"] == "bench_bad"
    assert bad_rows[0]["has_machine_spread"] is True
    assert bad_rows[0]["has_ambiguous_analyzed_matching"] is True
    assert bad_rows[0]["has_off_story_signal"] is True

    assert flagged_rows
    assert any("ambiguous_analysis" in row["interesting_flags"] for row in flagged_rows)


def test_prioritized_breakdown_summary_uses_selected_attempt_machine_host_for_analyzed_grouping(tmp_path):
    level_002 = tmp_path / "level_002"
    enriched_rows = [
        {
            "experiment_name": "exp-multi",
            "run_entry": "bench_multi:model=model-x",
            "benchmark": "bench_multi",
            "model": "model-x",
            "machine_host": "host-a",
            "suite": "suite-1",
            "job_id": "job-a",
            "run_dir": "/runs/selected",
            "attempt_identity": "uuid-selected",
            "attempt_uuid": "uuid-selected",
            "attempt_fallback_key": "fallback::selected",
            "manifest_timestamp": "10",
            "has_run_spec": "True",
            "storyline_status": "on_story",
            "logical_run_key": "bench_multi:model=model-x",
            "repro_report_dir": "/reports/multi",
        },
        {
            "experiment_name": "exp-multi",
            "run_entry": "bench_multi:model=model-x",
            "benchmark": "bench_multi",
            "model": "model-x",
            "machine_host": "host-b",
            "suite": "suite-1",
            "job_id": "job-b",
            "run_dir": "/runs/unselected",
            "attempt_identity": "uuid-unselected",
            "attempt_uuid": "uuid-unselected",
            "attempt_fallback_key": "fallback::unselected",
            "manifest_timestamp": "20",
            "has_run_spec": "True",
            "storyline_status": "on_story",
            "logical_run_key": "bench_multi:model=model-x",
            "repro_report_dir": "/reports/multi",
        },
    ]
    repro_rows = [
        {
            "experiment_name": "exp-multi",
            "run_entry": "bench_multi:model=model-x",
            "report_dir": "/reports/multi",
            "official_instance_agree_bucket": "high_agreement_0.95+",
            "official_instance_agree_005": 0.97,
            "analysis_selected_attempt_refs": [
                {
                    "run_dir": "/runs/selected",
                    "attempt_identity": "uuid-selected",
                    "attempt_uuid": "uuid-selected",
                    "attempt_fallback_key": "fallback::selected",
                    "machine_host": "host-a",
                    "experiment_name": "exp-multi",
                }
            ],
            "analysis_selected_attempt_identities": ["uuid-selected"],
        }
    ]
    run_multiplicity_summary = {
        "rows": [
            {
                "logical_run_key": "bench_multi:model=model-x",
                "n_attempt_ids": 2,
                "n_rows": 2,
                "n_machines": 2,
                "n_ambiguous_analyzed_candidates": 0,
            }
        ]
    }

    summary = _build_prioritized_breakdown_summary(
        enriched_rows=enriched_rows,
        repro_rows=repro_rows,
        run_multiplicity_summary=run_multiplicity_summary,
        breakdown_dims=["benchmark", "model", "machine_host", "experiment_name", "suite"],
        level_002=level_002,
    )

    machine_good_rows = [
        row for row in summary["rows"]
        if row["bucket_class"] == "good" and row["dimension"] == "machine_host"
    ]
    assert machine_good_rows
    assert any(row["dimension_value"] == "host-a" for row in machine_good_rows)
    assert all(row["dimension_value"] != "host-b" for row in machine_good_rows)
    row = next(row for row in machine_good_rows if row["dimension_value"] == "host-a")
    assert row["machine_host_membership_source"] == "selected_attempt_refs.machine_host"
    assert row["example_report_dirs"] == ["/reports/multi"]


def test_prioritized_example_symlink_tree_is_created_and_points_to_real_targets(tmp_path):
    level_002 = tmp_path / "level_002"
    breakdown_dir = level_002 / "breakdowns" / "by_benchmark" / "bench-good"
    breakdown_index_dir = level_002 / "breakdowns" / "by_benchmark"
    breakdown_dir.mkdir(parents=True)
    breakdown_index_dir.mkdir(parents=True, exist_ok=True)
    report_dir = tmp_path / "reports" / "good"
    report_dir.mkdir(parents=True)
    for name in [
        "core_metric_report.latest.png",
        "core_metric_management_summary.latest.txt",
        "instance_samples_official_vs_kwdagger.latest.txt",
        "report_selection.latest.json",
    ]:
        (report_dir / name).write_text(name)

    summary = {
        "selected_by_section": {
            "good": [
                {
                    "priority_rank": 1,
                    "dimension": "benchmark",
                    "dimension_value": "bench-good",
                    "selection_reason": "good exemplar",
                    "breakdown_dir": str(breakdown_dir),
                    "breakdown_index_dir": str(breakdown_index_dir),
                    "interesting_flags": [],
                    "example_rows": [
                        {
                            "experiment_name": "exp-good",
                            "run_entry": "bench-good:model=a",
                            "report_dir": str(report_dir),
                        }
                    ],
                }
            ],
            "mid": [],
            "bad": [],
            "flagged": [],
        }
    }

    tree_root = _publish_prioritized_examples_tree(
        level_002=level_002,
        generated_utc="20260421T154116Z",
        summary=summary,
        repair_results=[],
    )

    assert (level_002 / "prioritized_examples.latest").is_symlink()
    rec_dir = tree_root / "good" / "01-benchmark-bench-good"
    example_dir = rec_dir / "example_01-bench-good-model-a"
    assert rec_dir.exists()
    assert example_dir.exists()
    assert (rec_dir / "breakdown_dir").resolve() == breakdown_dir.resolve()
    assert (rec_dir / "breakdown_index_dir").resolve() == breakdown_index_dir.resolve()
    assert (example_dir / "report_dir").resolve() == report_dir.resolve()
    assert (example_dir / "core_metric_report.latest.png").resolve() == (report_dir / "core_metric_report.latest.png").resolve()


def test_prioritized_example_repairs_missing_latest_artifacts(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports" / "needs-repair"
    report_dir.mkdir(parents=True)
    summary = {
        "selected_by_section": {
            "good": [
                {
                    "priority_rank": 1,
                    "dimension": "benchmark",
                    "dimension_value": "bench-repair",
                    "selection_reason": "repair me",
                    "breakdown_dir": str(tmp_path / "breakdowns" / "bench-repair"),
                    "breakdown_index_dir": str(tmp_path / "breakdowns"),
                    "example_rows": [
                        {
                            "experiment_name": "exp-repair",
                            "run_entry": "bench-repair:model=a",
                            "report_dir": str(report_dir),
                            "analysis_single_run": True,
                        }
                    ],
                }
            ],
            "mid": [],
            "bad": [],
            "flagged": [],
        }
    }

    calls = []

    def _fake_rebuild(argv):
        calls.append(argv)
        for name in [
            "core_metric_report.latest.png",
            "core_metric_management_summary.latest.txt",
            "instance_samples_official_vs_kwdagger.latest.txt",
        ]:
            (report_dir / name).write_text("repaired")

    monkeypatch.setattr(
        "helm_audit.workflows.build_reports_summary.rebuild_core_report_main",
        _fake_rebuild,
    )

    repairs = _repair_prioritized_example_reports(
        summary=summary,
        index_fpath=tmp_path / "index.csv",
    )

    assert len(calls) == 1
    assert "--allow-single-repeat" in calls[0]
    assert repairs[0]["status"] == "repaired"
    assert repairs[0]["missing_artifacts"] == []
