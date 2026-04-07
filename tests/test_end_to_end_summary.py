from __future__ import annotations

from helm_audit.workflows.build_reports_summary import (
    ATTEMPTED_LABEL,
    FILTER_SELECTION_EXCLUDED_LABEL,
    FILTER_SELECTION_SELECTED_LABEL,
    NOT_ATTEMPTED_LABEL,
    _build_attempted_to_repro_rows,
    _build_end_to_end_funnel_rows,
    _build_filter_to_attempt_rows,
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
