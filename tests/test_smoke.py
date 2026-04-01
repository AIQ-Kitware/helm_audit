from __future__ import annotations

from pathlib import Path

import pytest

from helm_audit.cli.check_env import main as check_env_main
from helm_audit.cli.compare import main as compare_main
from helm_audit.cli.manifests import main as manifests_main
from helm_audit.cli.reports import main as reports_main
from helm_audit.cli.run import main as run_main
from helm_audit.helm.run_entries import (
    discover_benchmark_output_dirs,
    parse_run_entry_description,
    run_dir_matches_requested,
)
from helm_audit.workflows.compare_batch import aggregate_report


@pytest.mark.parametrize(
    "main",
    [check_env_main, compare_main, manifests_main, reports_main, run_main],
)
def test_cli_help_smoke(main):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_run_entry_helpers_match_canonical_model_tokens():
    bench, tokens = parse_run_entry_description(
        "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,"
        "model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical"
    )
    assert bench == "mmlu"
    assert tokens["subject"] == "us_foreign_policy"
    assert tokens["model"] == "lmsys/vicuna-7b-v1.3"
    assert run_dir_matches_requested(
        "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,"
        "model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical",
        "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,"
        "model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical",
    )


def test_discover_benchmark_output_dirs(tmp_path: Path):
    benchmark_output = tmp_path / "nested" / "benchmark_output"
    benchmark_output.mkdir(parents=True)
    found = list(discover_benchmark_output_dirs([tmp_path]))
    assert found == [benchmark_output]


def test_aggregate_report_counts():
    rows = [
        {
            "status": "compared",
            "diagnosis": {
                "label": "deployment_drift",
                "primary_reason_names": ["deployment_changed"],
                "reasons": [{"name": "deployment_changed"}],
            },
        },
        {"status": "missing"},
    ]
    report = aggregate_report(rows)
    assert report["n_rows"] == 2
    assert report["status_counts"]["compared"] == 1
    assert report["diagnosis_label_counts"]["deployment_drift"] == 1
    assert report["primary_reason_name_counts"]["deployment_changed"] == 1

