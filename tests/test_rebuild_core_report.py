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
    ]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(key, "")) for key in headers))
    fpath.write_text("\n".join(lines) + "\n")


def _write_selection(report_dir: Path, payload: dict) -> None:
    (report_dir / "report_selection.latest.json").write_text(json.dumps(payload, indent=2))


def test_stored_report_selection_rebuilds_without_current_index_matches(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    left_a = tmp_path / "runs" / "left_a"
    left_b = tmp_path / "runs" / "left_b"
    right = tmp_path / "runs" / "official"
    for dpath in [left_a, left_b, right]:
        dpath.mkdir(parents=True)

    selection = {
        "run_entry": "bench:model=a",
        "experiment_name": "exp-old",
        "left_run_a": str(left_a),
        "left_run_b": str(left_b),
        "right_run_a": str(right),
        "single_run": False,
        "selected_local_attempt_refs": [
            {"attempt_identity": "attempt-a", "run_dir": str(left_a)},
            {"attempt_identity": "attempt-b", "run_dir": str(left_b)},
        ],
        "selected_local_attempt_identities": ["attempt-a", "attempt-b"],
    }
    _write_selection(report_dir, selection)

    index_fpath = tmp_path / "index.csv"
    _write_index_csv(index_fpath, [])

    calls: dict[str, list] = {"core_metrics": [], "pair_samples": []}

    monkeypatch.setattr(rebuild_core_report.core_metrics, "main", lambda argv: calls["core_metrics"].append(argv))
    monkeypatch.setattr(
        rebuild_core_report.pair_samples,
        "write_pair_samples",
        lambda **kwargs: calls["pair_samples"].append(kwargs),
    )

    rebuild_core_report.main(
        [
            "--run-entry", "bench:model=a",
            "--experiment-name", "exp-old",
            "--report-dpath", str(report_dir),
            "--index-fpath", str(index_fpath),
        ]
    )

    assert len(calls["core_metrics"]) == 1
    argv = calls["core_metrics"][0]
    assert str(left_a) in argv
    assert str(left_b) in argv
    assert str(right) in argv

    refreshed = json.loads((report_dir / "report_selection.latest.json").read_text())
    assert refreshed["selected_local_attempt_identities"] == ["attempt-a", "attempt-b"]
    assert Path(report_dir / "kwdagger_a.run").resolve() == left_a.resolve()
    assert Path(report_dir / "kwdagger_b.run").resolve() == left_b.resolve()
    assert Path(report_dir / "official.run").resolve() == right.resolve()


def test_stored_single_run_report_rebuilds_without_allow_single_repeat(tmp_path, monkeypatch):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    left_a = tmp_path / "runs" / "left_a"
    right = tmp_path / "runs" / "official"
    for dpath in [left_a, right]:
        dpath.mkdir(parents=True)

    _write_selection(
        report_dir,
        {
            "run_entry": "bench:model=single",
            "experiment_name": "exp-single",
            "left_run_a": str(left_a),
            "left_run_b": str(left_a),
            "right_run_a": str(right),
            "single_run": True,
        },
    )

    index_fpath = tmp_path / "index.csv"
    _write_index_csv(index_fpath, [])

    calls: dict[str, list] = {"core_metrics": [], "pair_samples": []}
    monkeypatch.setattr(rebuild_core_report.core_metrics, "main", lambda argv: calls["core_metrics"].append(argv))
    monkeypatch.setattr(
        rebuild_core_report.pair_samples,
        "write_pair_samples",
        lambda **kwargs: calls["pair_samples"].append(kwargs),
    )

    rebuild_core_report.main(
        [
            "--run-entry", "bench:model=single",
            "--experiment-name", "exp-single",
            "--report-dpath", str(report_dir),
            "--index-fpath", str(index_fpath),
        ]
    )

    argv = calls["core_metrics"][0]
    assert "--single-run" in argv
    assert argv[argv.index("--left-run-a") + 1] == str(left_a)
    assert argv[argv.index("--left-run-b") + 1] == str(left_a)
    assert len(calls["pair_samples"]) == 1
    assert calls["pair_samples"][0]["label"] == "official_vs_kwdagger"
