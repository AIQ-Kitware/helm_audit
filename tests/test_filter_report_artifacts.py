from __future__ import annotations

from pathlib import Path

from helm_audit.cli.index_historic_helm_runs import (
    build_filter_inventory_rows,
    describe_run_spec,
)
from helm_audit.reports.filter_analysis import (
    emit_filter_analysis_artifacts,
    emit_filter_report_artifacts,
)


def test_describe_run_spec_extracts_dataset_and_setting():
    info = describe_run_spec(
        "entity_matching:dataset=Abt_Buy,model=lmsys/vicuna-7b-v1.3",
        "helm.benchmark.scenarios.entity_matching_scenario.EntityMatchingScenario",
    )
    assert info["benchmark"] == "entity_matching"
    assert info["dataset"] == "dataset=Abt_Buy"
    assert info["scenario"] == "EntityMatchingScenario"
    assert info["setting"].startswith("entity_matching:")


def test_build_filter_inventory_rows_marks_selected_and_excluded():
    complete_rows = [
        {
            "run_spec_name": "boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical",
            "run_dir": "/tmp/selected-run",
            "max_eval_instances": None,
            "model": "lmsys/vicuna-7b-v1.3",
            "scenario_class": "helm.benchmark.scenarios.boolq_scenario.BoolQScenario",
        },
        {
            "run_spec_name": "mmlu:subject=us_foreign_policy,model=openai/text-davinci-003",
            "run_dir": "/tmp/excluded-run",
            "max_eval_instances": None,
            "model": "openai/text-davinci-003",
            "scenario_class": "helm.benchmark.scenarios.mmlu_scenario.MMLUScenario",
        },
    ]
    incomplete_rows = [
        {
            "run_spec_name": "raft:subset=banking_77,model=broken_model",
            "run_dir": "/tmp/incomplete-run",
            "max_eval_instances": None,
            "model": "broken/model",
            "scenario_class": None,
            "benchmark": "raft",
            "dataset": "subset=banking_77",
            "dataset_key": "subset",
            "setting": "raft:subset=banking_77,model=broken_model",
            "scenario": "raft",
            "run_params": {"subset": "banking_77", "model": "broken_model"},
            "selection_status": "excluded",
            "outcome": "excluded",
            "eligible_model": False,
            "failure_reasons": ["structurally-incomplete"],
            "failure_reason_summary": "structurally-incomplete",
            "is_structurally_incomplete": True,
        }
    ]
    model_filter_rows = [
        {
            "model": "lmsys/vicuna-7b-v1.3",
            "n_runs": 1,
            "failure_reasons": [],
            "eligible": True,
        },
        {
            "model": "openai/text-davinci-003",
            "n_runs": 1,
            "failure_reasons": ["no-local-helm-deployment", "not-open-access"],
            "eligible": False,
        },
    ]
    inventory = build_filter_inventory_rows(
        complete_rows=complete_rows,
        incomplete_rows=incomplete_rows,
        model_filter_rows=model_filter_rows,
        chosen_model_names={"lmsys/vicuna-7b-v1.3"},
    )
    statuses = {row["run_spec_name"]: row["selection_status"] for row in inventory}
    assert statuses["boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical"] == "selected"
    assert statuses["mmlu:subject=us_foreign_policy,model=openai/text-davinci-003"] == "excluded"
    excluded_row = next(row for row in inventory if row["model"] == "openai/text-davinci-003")
    assert "no-local-helm-deployment" in excluded_row["failure_reasons"]
    assert excluded_row["considered_for_selection"] is True
    assert "Excluded after consideration" in excluded_row["selection_explanation"]
    selected_row = next(row for row in inventory if row["selection_status"] == "selected")
    assert selected_row["eligible_candidate"] is True
    assert "Selected because" in selected_row["selection_explanation"]


def test_build_filter_inventory_rows_marks_missing_model_metadata_explicitly():
    complete_rows = [
        {
            "run_spec_name": "cub200:model=openai/dalle-2,data_augmentation=canonical",
            "run_dir": "/tmp/missing-model",
            "max_eval_instances": None,
            "model": "openai/dalle-2",
            "scenario_class": "helm.benchmark.scenarios.cub200_scenario.CUB200Scenario",
        },
    ]
    inventory = build_filter_inventory_rows(
        complete_rows=complete_rows,
        incomplete_rows=[],
        model_filter_rows=[
            {
                "model": "openai/dalle-2",
                "n_runs": 1,
                "failure_reasons": ["missing-model-metadata"],
                "failure_reason_details": {
                    "missing-model-metadata": "HELM could not resolve model metadata for this model name."
                },
                "eligible": False,
                "num_parameters": None,
                "access": None,
                "tags": [],
                "has_hf_client": False,
                "size_threshold_params": 10e9,
            }
        ],
        chosen_model_names=set(),
    )
    row = inventory[0]
    assert row["selection_status"] == "excluded"
    assert row["eligible_model"] is False
    assert row["failure_reasons"] == ["missing-model-metadata"]
    assert "missing-model-metadata" in row["failure_reason_summary"]
    assert "HELM could not resolve model metadata" in row["selection_explanation"]


def test_build_filter_inventory_rows_excludes_closed_judge_benchmarks_even_with_eligible_model():
    complete_rows = [
        {
            "run_spec_name": "wildbench:subset=v2,model=openai/gpt-oss-20b",
            "run_dir": "/tmp/wildbench-run",
            "max_eval_instances": None,
            "model": "openai/gpt-oss-20b",
            "scenario_class": "helm.benchmark.scenarios.wildbench_scenario.WildBenchScenario",
        },
    ]
    inventory = build_filter_inventory_rows(
        complete_rows=complete_rows,
        incomplete_rows=[],
        model_filter_rows=[
            {
                "model": "openai/gpt-oss-20b",
                "n_runs": 1,
                "failure_reasons": [],
                "failure_reason_details": {},
                "eligible": True,
                "num_parameters": 20e9,
                "access": "open",
                "tags": ["TEXT_MODEL_TAG"],
                "has_hf_client": True,
                "size_threshold_params": 40e9,
            }
        ],
        chosen_model_names={"openai/gpt-oss-20b"},
    )
    row = inventory[0]
    assert row["selection_status"] == "excluded"
    assert row["eligible_model"] is True
    assert row["eligible_candidate"] is False
    assert row["candidate_pool"] == "eligible-model-out-of-scope"
    assert "requires-closed-judge" in row["failure_reasons"]
    assert "closed-source evaluation dependency" in row["selection_explanation"]


def test_build_filter_inventory_rows_excludes_gated_dataset_benchmarks_even_with_eligible_model():
    complete_rows = [
        {
            "run_spec_name": "gpqa:subset=gpqa_main,use_chain_of_thought=true,use_few_shot=false,model=openai/gpt-oss-20b",
            "run_dir": "/tmp/gpqa-run",
            "max_eval_instances": None,
            "model": "openai/gpt-oss-20b",
            "scenario_class": "helm.benchmark.scenarios.gpqa_scenario.GPQAScenario",
        },
    ]
    inventory = build_filter_inventory_rows(
        complete_rows=complete_rows,
        incomplete_rows=[],
        model_filter_rows=[
            {
                "model": "openai/gpt-oss-20b",
                "n_runs": 1,
                "failure_reasons": [],
                "failure_reason_details": {},
                "eligible": True,
                "num_parameters": 20e9,
                "access": "open",
                "tags": ["TEXT_MODEL_TAG"],
                "has_hf_client": True,
                "size_threshold_params": 40e9,
            }
        ],
        chosen_model_names={"openai/gpt-oss-20b"},
    )
    row = inventory[0]
    assert row["selection_status"] == "excluded"
    assert row["eligible_model"] is True
    assert row["eligible_candidate"] is False
    assert row["candidate_pool"] == "eligible-model-out-of-scope"
    assert "requires-gated-dataset" in row["failure_reasons"]
    assert "gated dataset" in row["selection_explanation"]


def test_emit_filter_report_artifacts_writes_tables(tmp_path: Path):
    inventory_rows = [
        {
            "run_spec_name": "boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical",
            "run_dir": "/tmp/selected-run",
            "max_eval_instances": None,
            "model": "lmsys/vicuna-7b-v1.3",
            "scenario_class": "helm.benchmark.scenarios.boolq_scenario.BoolQScenario",
            "benchmark": "boolq",
            "dataset": "boolq",
            "dataset_key": None,
            "setting": "boolq:data_augmentation=canonical",
            "scenario": "BoolQScenario",
            "run_params": {"model": "lmsys/vicuna-7b-v1.3", "data_augmentation": "canonical"},
            "selection_status": "selected",
            "outcome": "selected",
            "eligible_model": True,
            "failure_reasons": [],
            "failure_reason_summary": "selected",
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "mmlu:subject=us_foreign_policy,model=openai/text-davinci-003",
            "run_dir": "/tmp/excluded-run",
            "max_eval_instances": None,
            "model": "openai/text-davinci-003",
            "scenario_class": "helm.benchmark.scenarios.mmlu_scenario.MMLUScenario",
            "benchmark": "mmlu",
            "dataset": "subject=us_foreign_policy",
            "dataset_key": "subject",
            "setting": "mmlu:subject=us_foreign_policy",
            "scenario": "MMLUScenario",
            "run_params": {"subject": "us_foreign_policy", "model": "openai/text-davinci-003"},
            "selection_status": "excluded",
            "outcome": "excluded",
            "eligible_model": False,
            "failure_reasons": ["no-local-helm-deployment"],
            "failure_reason_summary": "no-local-helm-deployment",
            "is_structurally_incomplete": False,
        },
    ]
    outputs = emit_filter_report_artifacts(
        report_dpath=tmp_path / "reports-filter",
        stamp="20260406T171445Z",
        inventory_rows=inventory_rows,
    )
    assert Path(outputs["summary_txt"]).exists()
    assert Path(outputs["inventory_tsv"]).exists()
    assert Path(outputs["reason_by_model_tsv"]).exists()
    latest_inventory = tmp_path / "reports-filter" / "static" / "tables" / "model_filter_inventory.latest.tsv"
    assert latest_inventory.is_symlink()
    history_inventory = tmp_path / "reports-filter" / ".history" / "20260406" / "20260406T171445Z" / "static" / "tables" / "model_filter_inventory_20260406T171445Z.tsv"
    assert history_inventory.exists()


def test_emit_filter_analysis_artifacts_writes_explanatory_outputs(tmp_path: Path):
    inventory_rows = [
        {
            "run_spec_name": "boolq:model=lmsys/vicuna-7b-v1.3,data_augmentation=canonical",
            "run_dir": "/tmp/selected-run",
            "max_eval_instances": None,
            "model": "lmsys/vicuna-7b-v1.3",
            "scenario_class": "helm.benchmark.scenarios.boolq_scenario.BoolQScenario",
            "benchmark": "boolq",
            "dataset": "boolq",
            "dataset_key": None,
            "setting": "boolq:data_augmentation=canonical",
            "scenario": "BoolQScenario",
            "run_params": {"model": "lmsys/vicuna-7b-v1.3", "data_augmentation": "canonical"},
            "selection_status": "selected",
            "outcome": "selected",
            "considered_for_selection": True,
            "eligible_candidate": True,
            "candidate_pool": "eligible-model",
            "eligible_model": True,
            "failure_reasons": [],
            "failure_reason_summary": "selected",
            "selection_explanation": "Selected because the run was structurally complete and its model passed all eligibility filters.",
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "mmlu:subject=us_foreign_policy,model=openai/text-davinci-003",
            "run_dir": "/tmp/excluded-run",
            "max_eval_instances": None,
            "model": "openai/text-davinci-003",
            "scenario_class": "helm.benchmark.scenarios.mmlu_scenario.MMLUScenario",
            "benchmark": "mmlu",
            "dataset": "subject=us_foreign_policy",
            "dataset_key": "subject",
            "setting": "mmlu:subject=us_foreign_policy",
            "scenario": "MMLUScenario",
            "run_params": {"subject": "us_foreign_policy", "model": "openai/text-davinci-003"},
            "selection_status": "excluded",
            "outcome": "excluded",
            "considered_for_selection": True,
            "eligible_candidate": False,
            "candidate_pool": "complete-run",
            "eligible_model": False,
            "failure_reasons": ["no-local-helm-deployment"],
            "failure_reason_summary": "no-local-helm-deployment",
            "selection_explanation": "Excluded after consideration because the model failed: no-local-helm-deployment.",
            "is_structurally_incomplete": False,
        },
        {
            "run_spec_name": "raft:subset=banking_77,model=broken_model",
            "run_dir": "/tmp/incomplete-run",
            "max_eval_instances": None,
            "model": "broken/model",
            "scenario_class": None,
            "benchmark": "raft",
            "dataset": "subset=banking_77",
            "dataset_key": "subset",
            "setting": "raft:subset=banking_77,model=broken_model",
            "scenario": "raft",
            "run_params": {"subset": "banking_77", "model": "broken_model"},
            "selection_status": "excluded",
            "outcome": "excluded",
            "considered_for_selection": False,
            "eligible_candidate": False,
            "candidate_pool": "structurally-incomplete",
            "eligible_model": False,
            "failure_reasons": ["structurally-incomplete"],
            "failure_reason_summary": "structurally-incomplete",
            "selection_explanation": "Excluded before candidate selection because the run directory was structurally incomplete.",
            "is_structurally_incomplete": True,
        },
    ]
    outputs = emit_filter_analysis_artifacts(
        report_dpath=tmp_path / "reports-filter",
        stamp="20260406T173822Z",
        inventory_rows=inventory_rows,
    )
    assert Path(outputs["summary_json"]).exists()
    assert Path(outputs["summary_txt"]).exists()
    assert Path(outputs["summary_md"]).exists()
    assert Path(outputs["candidate_pool_tsv"]).exists()
    assert Path(outputs["reason_combo_tsv"]).exists()
    assert outputs["hierarchical_filter_sankey"]["json"] is not None
    latest_summary = tmp_path / "reports-filter" / "analysis" / "static" / "filter_candidate_analysis.latest.txt"
    assert latest_summary.is_symlink()
    text = Path(outputs["summary_txt"]).read_text()
    assert "selected_of_all" in text
    assert "Candidate pool funnel:" in text
    assert "Hierarchical gate order:" in text
    assert "Reason combinations:" in text
    assert "Excluded examples:" in text
