from __future__ import annotations

import csv
import json
from pathlib import Path

from helm_audit.planning.core_report_planner import build_planning_artifact
from helm_audit.reports.core_packet import comparison_sample_latest_name
from helm_audit.workflows import rebuild_core_report
from helm_audit.workflows.rebuild_core_report import (
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
    _write_json(official_run / "run_spec.json", _make_run_spec("bench:model=test", deployment="hf/meta-llama-3-8b", instructions="official"))
    _write_json(local_a / "run_spec.json", _make_run_spec("bench:model=test", deployment="local/meta-llama-3-8b", instructions="local"))
    _write_json(local_b / "run_spec.json", _make_run_spec("bench:model=test", deployment="local/meta-llama-3-8b", instructions="local"))
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
    assert "helm_audit.reports.core_metrics" in script_text
    assert "components_manifest.latest.json" in script_text
    assert "comparisons_manifest.latest.json" in script_text


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
    assert "helm_audit.reports.core_metrics" in script_text
    assert "components_manifest.latest.json" in script_text
    assert "comparisons_manifest.latest.json" in script_text


def test_auto_render_policy_is_conservative_by_default(tmp_path):
    """_should_auto_render_heavy_pairwise_plots returns False for any normal packet."""
    packet = {"packet_id": "some-packet", "run_entry": "bench:model=test"}
    comparisons = [
        {"comparison_kind": "official_vs_local", "enabled": True},
        {"comparison_kind": "local_repeat", "enabled": True},
    ]
    assert not _should_auto_render_heavy_pairwise_plots(packet, comparisons, tmp_path)


def test_auto_render_policy_accepts_packet_and_report_dpath(tmp_path):
    """Policy hook operates on full packet metadata and report_dpath, not comparison_kind alone.

    This test verifies the interface: the function accepts packet, comparisons, and
    report_dpath so that extensions can key on packet_id, diagnostic flags, or any
    other report metadata — not just comparison_kind.
    """
    import inspect
    sig = inspect.signature(_should_auto_render_heavy_pairwise_plots)
    param_names = list(sig.parameters)
    assert "packet" in param_names
    assert "comparisons" in param_names
    assert "report_dpath" in param_names
    # No comparison_kind shortcut; the function gets the full comparison list
    assert "comparison_kind" not in param_names


def test_auto_render_policy_can_be_overridden_via_monkeypatch(tmp_path, monkeypatch):
    """The policy function is the single extension point; patching it enables heavy rendering."""
    monkeypatch.setattr(
        rebuild_core_report,
        "_should_auto_render_heavy_pairwise_plots",
        lambda packet, comparisons, report_dpath: True,
    )
    packet = {"packet_id": "interesting-packet"}
    comparisons = [{"comparison_kind": "official_vs_local", "enabled": True}]
    assert rebuild_core_report._should_auto_render_heavy_pairwise_plots(
        packet, comparisons, tmp_path
    )
