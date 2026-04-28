from __future__ import annotations

import json

from eval_audit.cli.summarize_experiment_failures import summarize_failures


def test_summarize_failures_classifies_known_failure_patterns(tmp_path):
    helm_root = tmp_path / "experiment" / "helm"

    passed = helm_root / "helm_id_passed"
    passed.mkdir(parents=True)
    (passed / "job_config.json").write_text(json.dumps({"helm.run_entry": "ifeval:model=openai/gpt-oss-20b"}))
    run_dir = passed / "benchmark_output" / "runs" / "demo" / "ifeval:model=openai_gpt-oss-20b"
    run_dir.mkdir(parents=True)
    (run_dir / "run_spec.json").write_text("{}")

    gated = helm_root / "helm_id_gated"
    gated.mkdir(parents=True)
    (gated / "job_config.json").write_text(json.dumps({"helm.run_entry": "gpqa:subset=gpqa_main,model=openai/gpt-oss-20b"}))
    (gated / "helm-run.log").write_text("DatasetNotFoundError: Dataset 'Idavidrein/gpqa' is a gated dataset on the Hub.\n")

    api_key = helm_root / "helm_id_api"
    api_key.mkdir(parents=True)
    (api_key / "job_config.json").write_text(json.dumps({"helm.run_entry": "xstest:model=openai/gpt-oss-20b"}))
    (api_key / "helm-run.log").write_text(
        "AnnotationExecutorError: The api_key client option must be set either by passing api_key to the client or by setting the OPENAI_API_KEY environment variable\n"
    )

    summary = summarize_failures(tmp_path / "experiment")
    assert summary["passed_jobs"] == 1
    assert summary["failed_jobs"] == 2
    assert summary["category_counts"]["gated_dataset"] == 1
    assert summary["category_counts"]["missing_api_key"] == 1

    rows = {row["job_id"]: row for row in summary["rows"]}
    assert rows["helm_id_passed"]["status"] == "passed"
    assert "gated dataset" in rows["helm_id_gated"]["summary"]
    assert "OPENAI_API_KEY" in rows["helm_id_api"]["summary"]


def test_summarize_failures_classifies_null_completion_text(tmp_path):
    helm_root = tmp_path / "experiment" / "helm"
    null_text = helm_root / "helm_id_null_text"
    null_text.mkdir(parents=True)
    (null_text / "job_config.json").write_text(json.dumps({"helm.run_entry": "ifeval:model=openai/gpt-oss-20b"}))
    (null_text / "helm-run.log").write_text(
        "Traceback (most recent call last):\n"
        "  File \"/tmp/demo.py\", line 1, in <module>\n"
        "AttributeError: 'NoneType' object has no attribute 'strip'\n"
    )

    summary = summarize_failures(tmp_path / "experiment")
    assert summary["failed_jobs"] == 1
    assert summary["category_counts"]["null_completion_text"] == 1
    row = summary["rows"][0]
    assert row["category"] == "null_completion_text"
    assert "null text/content" in row["summary"]
