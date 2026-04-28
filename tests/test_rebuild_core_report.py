from __future__ import annotations

import csv
import json
from pathlib import Path

from eval_audit.planning.core_report_planner import (
    build_planning_artifact,
    _comparability_warning_lines,
)
from eval_audit.reports.core_packet import component_link_basename, comparison_sample_latest_name
from eval_audit.workflows import rebuild_core_report
from eval_audit.workflows.rebuild_core_report import (
    _should_auto_render_heavy_pairwise_plots,
)


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


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _make_run_spec(run_name: str, *, deployment: str, instructions: str) -> dict:
    return {
        "name": run_name,
        "adapter_spec": {
            "model": "meta/llama-3-8b",
            "model_deployment": deployment,
            "max_eval_instances": 100,
            "instructions": instructions,
        },
        "scenario_spec": {"class_name": "helm.BoolQScenario"},
    }


def _write_index_inputs(tmp_path: Path, *, single_run: bool) -> tuple[Path, Path]:
    official_root = tmp_path / "official"
    local_root = tmp_path / "local"
    official_run = official_root / "benchmark_output" / "runs" / "v1" / "bench:model=test"
    local_a = local_root / "exp" / "helm" / "job-a" / "benchmark_output" / "runs" / "suite" / "bench:model=test"
    local_b = local_root / "exp" / "helm" / "job-b" / "benchmark_output" / "runs" / "suite" / "bench:model=test"
    for path in [official_run, local_a, local_b]:
        path.mkdir(parents=True, exist_ok=True)
    # Use empty instructions for all runs so the fixture represents routine
    # deployment-only drift (expected in every local-vs-official comparison).
    # Tests that need instructions drift build their own packets explicitly.
    _write_json(official_run / "run_spec.json", _make_run_spec("bench:model=test", deployment="hf/meta-llama-3-8b", instructions=""))
    _write_json(local_a / "run_spec.json", _make_run_spec("bench:model=test", deployment="local/meta-llama-3-8b", instructions=""))
    _write_json(local_b / "run_spec.json", _make_run_spec("bench:model=test", deployment="local/meta-llama-3-8b", instructions=""))
    for job_root in [local_a.parents[3], local_b.parents[3]]:
        (job_root / "job_config.json").write_text("{}\n")

    local_rows = [
        {
            "component_id": "local::exp::job-a::uuid-a",
            "logical_run_key": "bench:model=test",
            "experiment_name": "exp",
            "job_id": "job-a",
            "job_dpath": str(local_a.parents[3]),
            "run_path": str(local_a),
            "run_spec_fpath": str(local_a / "run_spec.json"),
            "run_spec_name": "bench:model=test",
            "model": "meta/llama-3-8b",
            "model_deployment": "local/meta-llama-3-8b",
            "scenario_class": "helm.BoolQScenario",
            "benchmark_group": "bench",
            "max_eval_instances": "100",
            "status": "computed",
            "manifest_timestamp": "20",
            "run_entry": "bench:model=test",
            "suite": "suite",
            "attempt_uuid": "uuid-a",
            "attempt_identity": "uuid-a",
            "attempt_identity_kind": "attempt_uuid",
            "attempt_fallback_key": "fallback::job-a",
            "machine_host": "host-a",
            "has_run_spec": "true",
        }
    ]
    if not single_run:
        local_rows.append(
            {
                "component_id": "",
                "logical_run_key": "bench:model=test",
                "experiment_name": "exp",
                "job_id": "job-b",
                "job_dpath": str(local_b.parents[3]),
                "run_path": str(local_b),
                "run_spec_fpath": str(local_b / "run_spec.json"),
                "run_spec_name": "bench:model=test",
                "model": "meta/llama-3-8b",
                "model_deployment": "local/meta-llama-3-8b",
                "scenario_class": "helm.BoolQScenario",
                "benchmark_group": "bench",
                "max_eval_instances": "100",
                "status": "computed",
                "manifest_timestamp": "10",
                "run_entry": "bench:model=test",
                "suite": "suite",
                "attempt_uuid": "",
                "attempt_identity": "",
                "attempt_identity_kind": "",
                "attempt_fallback_key": "fallback::job-b",
                "machine_host": "host-b",
                "has_run_spec": "true",
            }
        )
    local_index = tmp_path / "local_index.csv"
    official_index = tmp_path / "official_index.csv"
    _write_csv(local_index, local_rows)
    _write_csv(
        official_index,
        [
            {
                "component_id": "official::main::v1::bench:model=test",
                "logical_run_key": "bench:model=test",
                "run_path": str(official_run),
                "public_run_dir": str(official_run),
                "run_name": "bench:model=test",
                "run_spec_fpath": str(official_run / "run_spec.json"),
                "run_spec_name": "bench:model=test",
                "model": "meta/llama-3-8b",
                "model_deployment": "hf/meta-llama-3-8b",
                "scenario_class": "helm.BoolQScenario",
                "benchmark_group": "bench",
                "max_eval_instances": "100",
                "public_track": "main",
                "suite_version": "v1",
            }
        ],
    )
    return local_index, official_index


def _write_planner_artifact(tmp_path: Path, *, single_run: bool) -> tuple[Path, dict]:
    local_index, official_index = _write_index_inputs(tmp_path, single_run=single_run)
    artifact = build_planning_artifact(
        local_index_fpath=local_index,
        official_index_fpath=official_index,
        experiment_name="exp",
        run_entry="bench:model=test",
    )
    planner_fpath = tmp_path / "comparison_intents.latest.json"
    planner_fpath.write_text(json.dumps(artifact, indent=2) + "\n")
    return planner_fpath, artifact


def test_single_run_core_report_uses_planner_packet_and_cleans_repeat_artifacts(tmp_path, monkeypatch):
    planner_fpath, artifact = _write_planner_artifact(tmp_path, single_run=True)
    packet = artifact["packets"][0]
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    stale_components_dir = report_dir / "components"
    stale_components_dir.mkdir()
    (stale_components_dir / "old-repeat.run").symlink_to(tmp_path / "local")
    (report_dir / "kwdagger_b.run").symlink_to(tmp_path / "local")
    (report_dir / comparison_sample_latest_name("local_repeat")).write_text("stale\n")
    (report_dir / "instance_samples_official_vs_kwdagger.latest.txt").write_text("stale\n")
    (report_dir / "core_metric_three_run_distributions.latest.png").write_text("stale\n")

    core_metric_calls: list[list[str]] = []
    pair_sample_calls: list[dict] = []
    monkeypatch.setattr(rebuild_core_report.core_metrics, "main", lambda argv: core_metric_calls.append(argv))
    monkeypatch.setattr(
        rebuild_core_report.pair_samples,
        "write_pair_samples",
        lambda **kwargs: pair_sample_calls.append(kwargs),
    )

    rebuild_core_report.main(
        [
            "--planner-artifact-fpath", str(planner_fpath),
            "--packet-id", str(packet["packet_id"]),
            "--report-dpath", str(report_dir),
        ]
    )

    components_manifest = _read_json(report_dir / "components_manifest.latest.json")
    comparisons_manifest = _read_json(report_dir / "comparisons_manifest.latest.json")
    assert components_manifest["packet_id"] == packet["packet_id"]
    assert components_manifest["warnings"] == packet["warnings"]
    assert comparisons_manifest["comparisons"] == packet["comparisons"]
    assert [comparison["comparison_kind"] for comparison in comparisons_manifest["comparisons"]] == ["official_vs_local"]
    assert not (report_dir / comparison_sample_latest_name("local_repeat")).exists()
    assert not (report_dir / "instance_samples_official_vs_kwdagger.latest.txt").exists()
    assert not (report_dir / "core_metric_three_run_distributions.latest.png").exists()
    assert not (report_dir / "kwdagger_b.run").exists()
    assert len(core_metric_calls) == 1
    assert len(pair_sample_calls) == 1
    assert pair_sample_calls[0]["label"].startswith("official_vs_local::")

    # Canonical heavy artifacts are NOT auto-rendered; --render-heavy-pairwise-plots absent
    assert "--render-heavy-pairwise-plots" not in core_metric_calls[0]

    # render_heavy_pairwise_plots.latest.sh is written
    render_script = report_dir / "render_heavy_pairwise_plots.latest.sh"
    assert render_script.exists(), "render script must be written"
    script_text = render_script.read_text()
    assert "--render-heavy-pairwise-plots" in script_text
    assert "eval_audit.reports.core_metrics" in script_text
    assert "components_manifest.latest.json" in script_text
    assert "comparisons_manifest.latest.json" in script_text
    assert "--plots-only" not in script_text, "render script must NOT skip non-plot writes"

    # redraw_plots.latest.sh is written for narrow plot-styling iteration
    redraw_script = report_dir / "redraw_plots.latest.sh"
    assert redraw_script.exists(), "redraw_plots.sh must be written"
    redraw_text = redraw_script.read_text()
    assert "--plots-only" in redraw_text, "redraw_plots.sh must use --plots-only"
    assert "--render-heavy-pairwise-plots" in redraw_text
    assert "components_manifest.latest.json" in redraw_text
    assert "comparisons_manifest.latest.json" in redraw_text


def test_component_link_basename_is_bounded_for_verbose_fallback_ids():
    component_id = "local::exp::job::fallback::" + ("very-long-part|" * 40)
    basename = component_link_basename(component_id)
    assert len(basename) < 100
    assert basename == component_link_basename(component_id)


def test_multi_run_core_report_renders_only_declared_planner_comparisons(tmp_path, monkeypatch):
    planner_fpath, artifact = _write_planner_artifact(tmp_path, single_run=False)
    packet = artifact["packets"][0]
    report_dir = tmp_path / "report"
    report_dir.mkdir()

    core_metric_calls: list[list[str]] = []
    pair_sample_calls: list[dict] = []
    monkeypatch.setattr(rebuild_core_report.core_metrics, "main", lambda argv: core_metric_calls.append(argv))
    monkeypatch.setattr(
        rebuild_core_report.pair_samples,
        "write_pair_samples",
        lambda **kwargs: pair_sample_calls.append(kwargs),
    )

    rebuild_core_report.main(
        [
            "--planner-artifact-fpath", str(planner_fpath),
            "--packet-id", str(packet["packet_id"]),
            "--report-dpath", str(report_dir),
        ]
    )

    components_manifest = _read_json(report_dir / "components_manifest.latest.json")
    comparisons_manifest = _read_json(report_dir / "comparisons_manifest.latest.json")
    local_components = [
        component for component in components_manifest["components"]
        if component["source_kind"] == "local"
    ]
    assert len(local_components) == 2
    assert {comparison["comparison_kind"] for comparison in comparisons_manifest["comparisons"]} == {"official_vs_local", "local_repeat"}
    assert len(pair_sample_calls) == 3
    assert {call["label"].split("::", 1)[0] for call in pair_sample_calls} == {"official_vs_local", "local_repeat"}
    assert len(core_metric_calls) == 1

    # Heavy pairwise interactives not auto-rendered by default
    assert "--render-heavy-pairwise-plots" not in core_metric_calls[0]

    # render_heavy_pairwise_plots.latest.sh written and references canonical manifests
    render_script = report_dir / "render_heavy_pairwise_plots.latest.sh"
    assert render_script.exists(), "render script must be written"
    script_text = render_script.read_text()
    assert "--render-heavy-pairwise-plots" in script_text
    assert "eval_audit.reports.core_metrics" in script_text
    assert "components_manifest.latest.json" in script_text
    assert "comparisons_manifest.latest.json" in script_text
    assert "--plots-only" not in script_text

    # redraw_plots.latest.sh emitted for narrow plot-styling iteration
    redraw_script = report_dir / "redraw_plots.latest.sh"
    assert redraw_script.exists()
    redraw_text = redraw_script.read_text()
    assert "--plots-only" in redraw_text


def test_auto_render_policy_deployment_and_suite_drift_alone_do_not_trigger(tmp_path):
    """Deployment and suite-version drift are expected in every local-vs-official comparison."""
    packet = {
        "packet_id": "some-packet",
        "run_entry": "bench:model=test",
        "warnings": [
            "comparability_drift:same_deployment",
            "comparability_drift:same_suite_or_track_version",
        ],
    }
    comparisons = [
        {
            "comparison_kind": "official_vs_local",
            "enabled": True,
            "warnings": [
                "comparability_drift:same_deployment",
                "comparability_drift:same_suite_or_track_version",
            ],
        },
    ]
    assert not _should_auto_render_heavy_pairwise_plots(packet, comparisons, tmp_path)


def test_auto_render_policy_instructions_drift_triggers(tmp_path):
    """Instructions drift is unusual; uses correct planner name same_instructions."""
    packet = {
        "packet_id": "some-packet",
        "warnings": [
            "comparability_drift:same_deployment",
            "comparability_drift:same_instructions",
        ],
    }
    comparisons = [{"comparison_kind": "official_vs_local", "enabled": True, "warnings": []}]
    assert _should_auto_render_heavy_pairwise_plots(packet, comparisons, tmp_path)


def test_auto_render_policy_model_drift_triggers(tmp_path):
    """Model drift triggers; uses correct planner name same_model (not same_base_model)."""
    packet = {
        "packet_id": "some-packet",
        "warnings": ["comparability_drift:same_model"],
    }
    assert _should_auto_render_heavy_pairwise_plots(packet, [], tmp_path)


def test_auto_render_policy_unexpected_drift_in_comparison_triggers(tmp_path):
    """Unexpected drift carried on a comparison (not the packet) also triggers."""
    packet = {"packet_id": "some-packet", "warnings": ["comparability_drift:same_deployment"]}
    comparisons = [
        {
            "comparison_kind": "official_vs_local",
            "enabled": True,
            "warnings": ["comparability_drift:same_max_eval_instances"],
        }
    ]
    assert _should_auto_render_heavy_pairwise_plots(packet, comparisons, tmp_path)


def test_auto_render_policy_disabled_comparison_warnings_ignored(tmp_path):
    """Warnings on disabled comparisons do not trigger heavy rendering."""
    packet = {"packet_id": "some-packet", "warnings": []}
    comparisons = [
        {
            "comparison_kind": "official_vs_local",
            "enabled": False,
            "warnings": ["comparability_drift:same_instructions"],
        }
    ]
    assert not _should_auto_render_heavy_pairwise_plots(packet, comparisons, tmp_path)


def test_auto_render_policy_interface_takes_full_packet_not_kind(tmp_path):
    """Policy hook signature includes packet, comparisons, and report_dpath — not comparison_kind."""
    import inspect
    sig = inspect.signature(_should_auto_render_heavy_pairwise_plots)
    param_names = list(sig.parameters)
    assert "packet" in param_names
    assert "comparisons" in param_names
    assert "report_dpath" in param_names
    assert "comparison_kind" not in param_names


def test_trigger_prefixes_match_real_planner_warning_names(tmp_path):
    """Selection rule is tested against actual warning names emitted by _comparability_warning_lines.

    This test uses the real planner machinery so that if fact names change in the
    planner, this test will fail and catch the mismatch before production does.
    """
    # Build comparability facts the same way the planner does, with unexpected drift
    unexpected_drift_facts = {
        "same_model": {"status": "no", "values": ["model-a", "model-b"]},
        "same_instructions": {"status": "no", "values": ["instr-a", "instr-b"]},
        "same_scenario_class": {"status": "yes", "values": ["SomeScenario"]},
        "same_benchmark_family": {"status": "yes", "values": ["bench"]},
        "same_max_eval_instances": {"status": "yes", "values": [100]},
        "same_deployment": {"status": "no", "values": ["deploy-a", "deploy-b"]},
        "same_suite_or_track_version": {"status": "no", "values": ["suite", "main::v1"]},
    }
    # Use the real planner function to emit the actual warning strings
    real_warnings = _comparability_warning_lines(unexpected_drift_facts)

    # Verify the warnings include the names we expect (guards against planner renames)
    assert "comparability_drift:same_model" in real_warnings
    assert "comparability_drift:same_instructions" in real_warnings
    assert "comparability_drift:same_deployment" in real_warnings

    # The selection function should trigger on model/instructions drift
    packet_with_unexpected = {"warnings": real_warnings}
    assert _should_auto_render_heavy_pairwise_plots(packet_with_unexpected, [], tmp_path)

    # Deployment + suite drift only → should NOT trigger
    deployment_only_facts = {
        "same_model": {"status": "yes", "values": ["model-a"]},
        "same_instructions": {"status": "unknown", "values": []},
        "same_scenario_class": {"status": "yes", "values": ["SomeScenario"]},
        "same_benchmark_family": {"status": "yes", "values": ["bench"]},
        "same_max_eval_instances": {"status": "yes", "values": [100]},
        "same_deployment": {"status": "no", "values": ["deploy-a", "deploy-b"]},
        "same_suite_or_track_version": {"status": "no", "values": ["suite", "main::v1"]},
    }
    deployment_only_warnings = _comparability_warning_lines(deployment_only_facts)
    packet_deploy_only = {"warnings": deployment_only_warnings}
    assert not _should_auto_render_heavy_pairwise_plots(packet_deploy_only, [], tmp_path)
