from __future__ import annotations

import json
from pathlib import Path

from helm_audit.workflows import rebuild_core_report


def _write_index_csv(fpath: Path, rows: list[dict[str, str]]) -> None:
    headers = [
        "run_entry",
        "experiment_name",
        "status",
        "has_run_spec",
        "run_dir",
        "manifest_timestamp",
        "max_eval_instances",
        "machine_host",
        "attempt_uuid",
        "attempt_identity",
        "job_dpath",
    ]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(key, "")) for key in headers))
    fpath.write_text("\n".join(lines) + "\n")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _make_local_row(run_entry: str, experiment_name: str, run_dir: Path, *, manifest_timestamp: str, attempt_uuid: str) -> dict[str, str]:
    job_dpath = run_dir.parent / f"{run_dir.name}_job"
    job_dpath.mkdir(parents=True, exist_ok=True)
    (job_dpath / "job_config.json").write_text("{}\n")
    return {
        "run_entry": run_entry,
        "experiment_name": experiment_name,
        "status": "computed",
        "has_run_spec": "true",
        "run_dir": str(run_dir),
        "manifest_timestamp": manifest_timestamp,
        "max_eval_instances": "100",
        "machine_host": "machine-a",
        "attempt_uuid": attempt_uuid,
        "attempt_identity": attempt_uuid,
        "job_dpath": str(job_dpath),
    }


def _make_historic_choice(run_dir: Path) -> tuple[dict[str, str], dict[str, int]]:
    return {"run_dir": str(run_dir)}, {"chosen_requested_max_eval_instances": 100}


def test_single_run_core_report_writes_manifests_and_cleans_repeat_artifacts(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    local_run = tmp_path / "runs" / "local_a"
    official_run = tmp_path / "runs" / "official"
    local_run.mkdir(parents=True)
    official_run.mkdir(parents=True)

    stale_components_dir = report_dir / "components"
    stale_components_dir.mkdir()
    (stale_components_dir / "old-repeat.run").symlink_to(local_run)
    (report_dir / "kwdagger_b.run").symlink_to(local_run)
    (report_dir / "instance_samples_local_repeat.latest.txt").write_text("stale\n")
    (report_dir / "instance_samples_official_vs_kwdagger.latest.txt").write_text("stale\n")
    (report_dir / "core_metric_three_run_distributions.latest.png").write_text("stale\n")

    index_fpath = tmp_path / "index.csv"
    _write_index_csv(
        index_fpath,
        [
            _make_local_row(
                "bench:model=single",
                "exp-single",
                local_run,
                manifest_timestamp="10",
                attempt_uuid="attempt-a",
            ),
        ],
    )

    core_metric_calls: list[list[str]] = []
    pair_sample_calls: list[dict] = []
    monkeypatch.setattr(rebuild_core_report.core_metrics, "main", lambda argv: core_metric_calls.append(argv))
    monkeypatch.setattr(
        rebuild_core_report.pair_samples,
        "write_pair_samples",
        lambda **kwargs: pair_sample_calls.append(kwargs),
    )
    monkeypatch.setattr(rebuild_core_report, "collect_historic_candidates", lambda *args, **kwargs: [{"run_dir": str(official_run)}])
    monkeypatch.setattr(rebuild_core_report, "choose_historic_candidate", lambda *args, **kwargs: _make_historic_choice(official_run))

    rebuild_core_report.main(
        [
            "--run-entry", "bench:model=single",
            "--experiment-name", "exp-single",
            "--report-dpath", str(report_dir),
            "--index-fpath", str(index_fpath),
            "--allow-single-repeat",
        ]
    )

    components_manifest = _read_json(report_dir / "components_manifest.latest.json")
    comparisons_manifest = _read_json(report_dir / "comparisons_manifest.latest.json")
    components = components_manifest["components"]
    comparisons = comparisons_manifest["comparisons"]

    assert len(components) == 2
    assert {component["source_kind"] for component in components} == {"local", "official"}
    assert [comparison["comparison_kind"] for comparison in comparisons] == ["official_vs_local"]
    assert all(component["component_id"] != "kwdagger_b" for component in components)
    assert all("reference" not in component["tags"] for component in components)
    assert comparisons[0]["reference_component_id"] in comparisons[0]["component_ids"]

    component_links = sorted(path.name for path in (report_dir / "components").glob("*.run"))
    assert len(component_links) == 2
    assert not (report_dir / "kwdagger_b.run").exists()
    assert not (report_dir / "instance_samples_local_repeat.latest.txt").exists()
    assert not (report_dir / "instance_samples_official_vs_kwdagger.latest.txt").exists()
    assert not (report_dir / "core_metric_three_run_distributions.latest.png").exists()

    assert len(core_metric_calls) == 1
    assert "--components-manifest" in core_metric_calls[0]
    assert "--comparisons-manifest" in core_metric_calls[0]
    assert len(pair_sample_calls) == 1
    assert pair_sample_calls[0]["label"] == "official_vs_local"


def test_multi_run_core_report_writes_multiple_local_components_and_both_comparisons(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    local_a = tmp_path / "runs" / "local_a"
    local_b = tmp_path / "runs" / "local_b"
    official_run = tmp_path / "runs" / "official"
    for dpath in [local_a, local_b, official_run]:
        dpath.mkdir(parents=True)

    index_fpath = tmp_path / "index.csv"
    _write_index_csv(
        index_fpath,
        [
            _make_local_row(
                "bench:model=multi",
                "exp-multi",
                local_a,
                manifest_timestamp="20",
                attempt_uuid="attempt-a",
            ),
            _make_local_row(
                "bench:model=multi",
                "exp-multi",
                local_b,
                manifest_timestamp="10",
                attempt_uuid="attempt-b",
            ),
        ],
    )

    core_metric_calls: list[list[str]] = []
    pair_sample_calls: list[dict] = []
    monkeypatch.setattr(rebuild_core_report.core_metrics, "main", lambda argv: core_metric_calls.append(argv))
    monkeypatch.setattr(
        rebuild_core_report.pair_samples,
        "write_pair_samples",
        lambda **kwargs: pair_sample_calls.append(kwargs),
    )
    monkeypatch.setattr(rebuild_core_report, "collect_historic_candidates", lambda *args, **kwargs: [{"run_dir": str(official_run)}])
    monkeypatch.setattr(rebuild_core_report, "choose_historic_candidate", lambda *args, **kwargs: _make_historic_choice(official_run))

    rebuild_core_report.main(
        [
            "--run-entry", "bench:model=multi",
            "--experiment-name", "exp-multi",
            "--report-dpath", str(report_dir),
            "--index-fpath", str(index_fpath),
        ]
    )

    components_manifest = _read_json(report_dir / "components_manifest.latest.json")
    comparisons_manifest = _read_json(report_dir / "comparisons_manifest.latest.json")
    components = components_manifest["components"]
    comparisons = comparisons_manifest["comparisons"]

    local_components = [component for component in components if component["source_kind"] == "local"]
    assert len(local_components) == 2
    assert any("repeat" in component["tags"] for component in local_components)
    assert all("reference" not in component["tags"] for component in components)
    assert {comparison["comparison_kind"] for comparison in comparisons} == {"official_vs_local", "local_repeat"}

    comparison_ids = {comparison["comparison_id"]: comparison for comparison in comparisons}
    official_vs_local = comparison_ids["official_vs_local"]
    local_repeat = comparison_ids["local_repeat"]
    assert official_vs_local["reference_component_id"] in official_vs_local["component_ids"]
    assert local_repeat["reference_component_id"] in local_repeat["component_ids"]

    assert len(pair_sample_calls) == 2
    assert {call["label"] for call in pair_sample_calls} == {"official_vs_local", "local_repeat"}
    assert len(core_metric_calls) == 1
