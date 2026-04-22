from __future__ import annotations

import csv
import json
from pathlib import Path

from helm_audit.planning import core_report_planner
from helm_audit.workflows import plan_core_report_packets


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _make_run_spec(
    run_name: str,
    *,
    model: str = "meta/llama-3-8b",
    deployment: str = "local/meta-llama-3-8b",
    scenario_class: str = "helm.BoolQScenario",
    instructions: str | None = None,
) -> dict:
    adapter = {
        "model": model,
        "model_deployment": deployment,
        "max_eval_instances": 100,
    }
    if instructions is not None:
        adapter["instructions"] = instructions
    return {
        "name": run_name,
        "adapter_spec": adapter,
        "scenario_spec": {"class_name": scenario_class},
    }


def _setup_index_inputs(
    tmp_path: Path,
    *,
    include_second_official_same_track: bool = False,
    include_second_official_other_track: bool = False,
    official_component_id: str | None = "official::main::v1::boolq:model=meta/llama-3-8b",
) -> tuple[Path, Path]:
    official_root = tmp_path / "official"
    local_root = tmp_path / "local"
    official_run = official_root / "benchmark_output" / "runs" / "v1" / "boolq:model=meta/llama-3-8b"
    local_run_a = local_root / "exp-a" / "helm" / "job-a" / "benchmark_output" / "runs" / "demo-suite" / "boolq:model=meta/llama-3-8b"
    local_run_b = local_root / "exp-a" / "helm" / "job-b" / "benchmark_output" / "runs" / "demo-suite" / "boolq:model=meta/llama-3-8b"
    for path in [official_run, local_run_a, local_run_b]:
        path.mkdir(parents=True, exist_ok=True)

    _write_json(official_run / "run_spec.json", _make_run_spec("boolq:model=meta/llama-3-8b", deployment="hf/meta-llama-3-8b", instructions="official prompt"))
    _write_json(
        local_run_a / "run_spec.json",
        _make_run_spec(
            "boolq:model=meta/llama-3-8b",
            deployment="local/meta-llama-3-8b",
            instructions="local prompt",
        ),
    )
    _write_json(
        local_run_b / "run_spec.json",
        _make_run_spec(
            "boolq:model=meta/llama-3-8b",
            deployment="local/meta-llama-3-8b",
            instructions="local prompt",
        ),
    )

    local_index = tmp_path / "local_index.csv"
    official_index = tmp_path / "official_index.csv"
    _write_csv(
        local_index,
        [
            {
                "component_id": "local::exp-a::job-a::uuid-a",
                "source_kind": "local",
                "logical_run_key": "boolq:model=meta/llama-3-8b",
                "experiment_name": "exp-a",
                "job_id": "job-a",
                "job_dpath": str(local_run_a.parents[3]),
                "run_path": str(local_run_a),
                "run_spec_fpath": str(local_run_a / "run_spec.json"),
                "run_spec_name": "boolq:model=meta/llama-3-8b",
                "model": "meta/llama-3-8b",
                "model_deployment": "local/meta-llama-3-8b",
                "scenario_class": "helm.BoolQScenario",
                "benchmark_group": "boolq",
                "max_eval_instances": "100",
                "status": "computed",
                "manifest_timestamp": "20",
                "run_entry": "boolq:model=meta/llama-3-8b",
                "suite": "demo-suite",
                "attempt_uuid": "uuid-a",
                "attempt_identity": "uuid-a",
                "attempt_identity_kind": "attempt_uuid",
                "attempt_fallback_key": "fallback::job-a",
                "machine_host": "host-a",
            },
            {
                "component_id": "",
                "source_kind": "local",
                "logical_run_key": "boolq:model=meta/llama-3-8b",
                "experiment_name": "exp-a",
                "job_id": "job-b",
                "job_dpath": str(local_run_b.parents[3]),
                "run_path": str(local_run_b),
                "run_spec_fpath": str(local_run_b / "run_spec.json"),
                "run_spec_name": "boolq:model=meta/llama-3-8b",
                "model": "meta/llama-3-8b",
                "model_deployment": "local/meta-llama-3-8b",
                "scenario_class": "helm.BoolQScenario",
                "benchmark_group": "boolq",
                "max_eval_instances": "100",
                "status": "computed",
                "manifest_timestamp": "10",
                "run_entry": "boolq:model=meta/llama-3-8b",
                "suite": "demo-suite",
                "attempt_uuid": "",
                "attempt_identity": "",
                "attempt_identity_kind": "",
                "attempt_fallback_key": "fallback::job-b",
                "machine_host": "host-b",
            },
        ],
    )
    official_rows = [
        {
            "component_id": official_component_id or "",
            "source_kind": "official",
            "logical_run_key": "boolq:model=meta/llama-3-8b",
            "run_path": str(official_run),
            "public_run_dir": str(official_run),
            "run_name": "boolq:model=meta/llama-3-8b",
            "run_spec_fpath": str(official_run / "run_spec.json"),
            "run_spec_name": "boolq:model=meta/llama-3-8b",
            "model": "meta/llama-3-8b",
            "model_deployment": "hf/meta-llama-3-8b",
            "scenario_class": "helm.BoolQScenario",
            "benchmark_group": "boolq",
            "max_eval_instances": "100",
            "public_track": "main",
            "suite_version": "v1",
        }
    ]
    if include_second_official_same_track:
        official_run_v2 = official_root / "benchmark_output" / "runs" / "v2" / "boolq:model=meta/llama-3-8b"
        official_run_v2.mkdir(parents=True, exist_ok=True)
        _write_json(official_run_v2 / "run_spec.json", _make_run_spec("boolq:model=meta/llama-3-8b", deployment="hf/meta-llama-3-8b", instructions="official prompt"))
        official_rows.append(
            {
                "component_id": "official::main::v2::boolq:model=meta/llama-3-8b",
                "source_kind": "official",
                "logical_run_key": "boolq:model=meta/llama-3-8b",
                "run_path": str(official_run_v2),
                "public_run_dir": str(official_run_v2),
                "run_name": "boolq:model=meta/llama-3-8b",
                "run_spec_fpath": str(official_run_v2 / "run_spec.json"),
                "run_spec_name": "boolq:model=meta/llama-3-8b",
                "model": "meta/llama-3-8b",
                "model_deployment": "hf/meta-llama-3-8b",
                "scenario_class": "helm.BoolQScenario",
                "benchmark_group": "boolq",
                "max_eval_instances": "100",
                "public_track": "main",
                "suite_version": "v2",
            }
        )
    if include_second_official_other_track:
        official_run_alt = official_root / "benchmark_output" / "runs" / "v9" / "boolq:model=meta/llama-3-8b-alt"
        official_run_alt.mkdir(parents=True, exist_ok=True)
        _write_json(official_run_alt / "run_spec.json", _make_run_spec("boolq:model=meta/llama-3-8b", deployment="hf/meta-llama-3-8b-alt", instructions="official prompt alt"))
        official_rows.append(
            {
                "component_id": "official::alt::v9::boolq:model=meta/llama-3-8b",
                "source_kind": "official",
                "logical_run_key": "boolq:model=meta/llama-3-8b",
                "run_path": str(official_run_alt),
                "public_run_dir": str(official_run_alt),
                "run_name": "boolq:model=meta/llama-3-8b",
                "run_spec_fpath": str(official_run_alt / "run_spec.json"),
                "run_spec_name": "boolq:model=meta/llama-3-8b",
                "model": "meta/llama-3-8b",
                "model_deployment": "hf/meta-llama-3-8b-alt",
                "scenario_class": "helm.BoolQScenario",
                "benchmark_group": "boolq",
                "max_eval_instances": "100",
                "public_track": "alt",
                "suite_version": "v9",
            }
        )
    _write_csv(official_index, official_rows)
    return local_index, official_index


def test_planner_emits_packet_intent_for_one_official_one_local_case(tmp_path):
    local_index, official_index = _setup_index_inputs(tmp_path)
    artifact = core_report_planner.build_planning_artifact(
        local_index_fpath=local_index,
        official_index_fpath=official_index,
        experiment_name="exp-a",
        run_entry="boolq:model=meta/llama-3-8b",
    )

    assert artifact["packet_count"] == 1
    packet = artifact["packets"][0]
    assert packet["run_entry"] == "boolq:model=meta/llama-3-8b"
    assert {component["source_kind"] for component in packet["components"]} == {"local", "official"}
    assert any(comparison["comparison_kind"] == "official_vs_local" for comparison in packet["comparisons"])
    assert not any("kwdagger" in component["component_id"] for component in packet["components"])


def test_planner_emits_local_repeat_when_multiple_local_components_exist(tmp_path):
    local_index, official_index = _setup_index_inputs(tmp_path)
    artifact = core_report_planner.build_planning_artifact(
        local_index_fpath=local_index,
        official_index_fpath=official_index,
        experiment_name="exp-a",
        run_entry="boolq:model=meta/llama-3-8b",
    )

    comparison_kinds = [comparison["comparison_kind"] for comparison in artifact["packets"][0]["comparisons"]]
    assert "local_repeat" in comparison_kinds


def test_planner_preserves_stable_local_component_identity_using_uuid_or_fallback(tmp_path):
    local_index, official_index = _setup_index_inputs(tmp_path)
    artifact = core_report_planner.build_planning_artifact(
        local_index_fpath=local_index,
        official_index_fpath=official_index,
        experiment_name="exp-a",
        run_entry="boolq:model=meta/llama-3-8b",
    )

    local_components = [
        component
        for component in artifact["packets"][0]["components"]
        if component["source_kind"] == "local"
    ]
    component_by_id = {component["component_id"]: component for component in local_components}
    assert "local::exp-a::job-a::uuid-a" in component_by_id
    fallback_component = next(component for component in local_components if component["attempt_uuid"] is None)
    assert fallback_component["attempt_identity"] == "fallback::job-b"


def test_comparison_specific_caveats_do_not_leak_official_drift_into_local_repeat(tmp_path):
    local_index, official_index = _setup_index_inputs(tmp_path)
    artifact = core_report_planner.build_planning_artifact(
        local_index_fpath=local_index,
        official_index_fpath=official_index,
        experiment_name="exp-a",
        run_entry="boolq:model=meta/llama-3-8b",
    )

    packet = artifact["packets"][0]
    official_vs_local = next(item for item in packet["comparisons"] if item["comparison_kind"] == "official_vs_local" and item["enabled"])
    local_repeat = next(item for item in packet["comparisons"] if item["comparison_kind"] == "local_repeat")
    assert official_vs_local["comparability_facts"]["same_deployment"]["status"] == "no"
    assert official_vs_local["comparability_facts"]["same_instructions"]["status"] == "no"
    assert local_repeat["comparability_facts"]["same_deployment"]["status"] == "yes"
    assert local_repeat["comparability_facts"]["same_instructions"]["status"] == "yes"
    assert not any("same_deployment=no" in item for item in local_repeat["caveats"])


def test_latest_suite_version_per_official_track_is_default_selection_policy(tmp_path):
    local_index, official_index = _setup_index_inputs(tmp_path, include_second_official_same_track=True)
    artifact = core_report_planner.build_planning_artifact(
        local_index_fpath=local_index,
        official_index_fpath=official_index,
        experiment_name="exp-a",
        run_entry="boolq:model=meta/llama-3-8b",
    )

    packet = artifact["packets"][0]
    retained = packet["official_selection"]["retained_component_ids"]
    assert retained == ["official::main::v2::boolq:model=meta/llama-3-8b"]
    assert "official::main::v1::boolq:model=meta/llama-3-8b" in packet["official_selection"]["discarded_component_ids"]


def test_multi_track_official_ambiguity_does_not_silently_auto_pick_reference(tmp_path):
    local_index, official_index = _setup_index_inputs(
        tmp_path,
        include_second_official_same_track=True,
        include_second_official_other_track=True,
    )
    artifact = core_report_planner.build_planning_artifact(
        local_index_fpath=local_index,
        official_index_fpath=official_index,
        experiment_name="exp-a",
        run_entry="boolq:model=meta/llama-3-8b",
    )

    packet = artifact["packets"][0]
    disabled = [
        comparison for comparison in packet["comparisons"]
        if comparison["comparison_kind"] == "official_vs_local"
    ]
    assert disabled
    assert all(comparison["enabled"] is False for comparison in disabled)
    assert all(comparison["disabled_reason"] == "ambiguous_official_candidates_after_latest_per_track" for comparison in disabled)
    assert "multiple_official_tracks_after_latest_per_track" in packet["warnings"]
    assert all(comparison["candidate_reference_component_ids"] for comparison in disabled)


def test_official_fallback_identity_is_stable_and_not_row_index_based(tmp_path):
    local_index, official_index = _setup_index_inputs(tmp_path, official_component_id=None)
    rows = core_report_planner.load_index_rows(official_index)
    normalized_a = core_report_planner.normalize_official_index_rows(rows, index_fpath=official_index)
    normalized_b = core_report_planner.normalize_official_index_rows(list(reversed(rows)), index_fpath=official_index)

    assert normalized_a[0].component_id == normalized_b[0].component_id
    assert "::0" not in normalized_a[0].component_id


def test_warnings_emitted_for_suspicious_conditions_and_written_as_artifacts(tmp_path):
    local_index, official_index = _setup_index_inputs(
        tmp_path,
        include_second_official_same_track=True,
        include_second_official_other_track=True,
    )
    out_dpath = tmp_path / "planned"

    plan_core_report_packets.main(
        [
            "--local-index-fpath", str(local_index),
            "--official-index-fpath", str(official_index),
            "--experiment-name", "exp-a",
            "--run-entry", "boolq:model=meta/llama-3-8b",
            "--out-dpath", str(out_dpath),
        ]
    )

    warnings_payload = json.loads((out_dpath / "warnings.latest.json").read_text())
    warnings_text = (out_dpath / "warnings.latest.txt").read_text()

    warning_values = [row["warning"] for row in warnings_payload["warnings"]]
    assert any("multiple_official_tracks_after_latest_per_track" in item for item in warning_values)
    assert any("disabled:ambiguous_official_candidates_after_latest_per_track" in item for item in warning_values)
    assert any("fallback_local_identity:" in item for item in warning_values)
    assert "packet_warnings:" in warnings_text
    assert "disabled_reason=ambiguous_official_candidates_after_latest_per_track" in warnings_text


def test_planner_outputs_are_human_inspectable_and_declared(tmp_path):
    local_index, official_index = _setup_index_inputs(tmp_path)
    out_dpath = tmp_path / "planned"

    plan_core_report_packets.main(
        [
            "--local-index-fpath", str(local_index),
            "--official-index-fpath", str(official_index),
            "--experiment-name", "exp-a",
            "--run-entry", "boolq:model=meta/llama-3-8b",
            "--out-dpath", str(out_dpath),
        ]
    )

    artifact = json.loads((out_dpath / "comparison_intents.latest.json").read_text())
    summary_text = (out_dpath / "comparison_intents.latest.txt").read_text()
    components_csv = (out_dpath / "comparison_intent_components.latest.csv").read_text()
    comparisons_csv = (out_dpath / "comparison_intent_comparisons.latest.csv").read_text()

    assert artifact["packet_count"] == 1
    assert "components:" in summary_text
    assert "comparisons:" in summary_text
    assert "comparability_facts:" in summary_text
    assert "component_id" in components_csv
    assert "comparison_kind" in comparisons_csv
