"""Stage-2 smoke tests for eval_audit.normalized.

These tests exercise the loader contract with the small HELM fixtures shipped
inside ``submodules/every_eval_ever/tests/data/helm/``. They validate that
the normalized layer:

* loads a raw HELM run via :class:`HelmRawLoader`,
* loads a converted EEE artifact tree via :class:`EeeArtifactLoader`,
* preserves the :class:`Origin` back to the raw HELM directory, and
* returns join-ready :class:`InstanceRecord` instances on both code paths.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from eval_audit.normalized import (
    ArtifactFormat,
    NormalizedRunRef,
    SourceKind,
    join_run_level,
    load_run,
)
from eval_audit.normalized.loaders import LoaderError


REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_FIXTURE_ROOT = (
    REPO_ROOT
    / "submodules"
    / "every_eval_ever"
    / "tests"
    / "data"
    / "helm"
)
HELM_FIXTURE_RUN = (
    HELM_FIXTURE_ROOT
    / "mmlu:subject=philosophy,method=multiple_choice_joint,model=openai_gpt2"
)


pytestmark = pytest.mark.skipif(
    not HELM_FIXTURE_RUN.exists(),
    reason=f"HELM fixture not present: {HELM_FIXTURE_RUN}",
)


def _load_helm_fixture() -> "object":
    pytest.importorskip("every_eval_ever")
    pytest.importorskip("helm")
    ref = NormalizedRunRef.from_helm_run(
        HELM_FIXTURE_RUN,
        source_kind=SourceKind.OFFICIAL,
        component_id="official::test::v0.0.0::mmlu_philosophy",
        logical_run_key="mmlu:subject=philosophy,method=multiple_choice_joint,model=openai/gpt2",
        display_name="mmlu_philosophy_gpt2",
        extra={
            "source_organization_name": "test",
            "evaluator_relationship": "third_party",
            "eval_library_name": "HELM",
            "eval_library_version": "v0.0.0",
        },
    )
    return load_run(ref)


def test_helm_raw_loader_produces_normalized_run() -> None:
    run = _load_helm_fixture()
    assert run.artifact_format is ArtifactFormat.HELM
    assert run.source_kind is SourceKind.OFFICIAL
    assert run.ref.origin.helm_run_path == HELM_FIXTURE_RUN
    assert run.ref.origin.converter_name and "every_eval_ever" in run.ref.origin.converter_name

    means = run.metrics_by_id()
    assert means, "EvaluationLog should expose at least one metric"
    sample_score = next(iter(means.values()))["score"]
    assert isinstance(sample_score, float)

    assert run.instances, "per-instance records should be populated"
    assert all(rec.metric_id for rec in run.instances)
    # Scores include both bounded metrics (exact_match) and counters
    # (num_prompt_tokens). Confirm the bounded ones are present.
    bounded = [
        rec for rec in run.instances
        if rec.metric_id in {"exact_match", "quasi_exact_match"}
    ]
    assert bounded, "expected at least one bounded core metric"
    assert all(0.0 <= rec.score <= 1.0 for rec in bounded)

    assert run.raw_helm and "stats" in run.raw_helm and "per_instance_stats" in run.raw_helm


def test_run_level_join_self_match() -> None:
    run = _load_helm_fixture()
    matches = list(join_run_level(run, run))
    assert matches, "self-join must produce matches"
    for _key, a, b in matches:
        assert a == b


def test_loader_rejects_missing_required_files(tmp_path: Path) -> None:
    pytest.importorskip("every_eval_ever")
    pytest.importorskip("helm")
    bare = tmp_path / "incomplete"
    bare.mkdir()
    (bare / "run_spec.json").write_text("{}")
    ref = NormalizedRunRef.from_helm_run(bare, source_kind=SourceKind.OFFICIAL)
    with pytest.raises(LoaderError):
        load_run(ref)


def test_eee_artifact_loader_round_trip(tmp_path: Path) -> None:
    """Convert the HELM fixture to EEE on disk, then re-load via :class:`EeeArtifactLoader`."""
    pytest.importorskip("every_eval_ever")
    pytest.importorskip("helm")
    from every_eval_ever.converters.helm.adapter import HELMAdapter

    out_dir = tmp_path / "eee_out"
    out_dir.mkdir()
    metadata_args = {
        "source_organization_name": "test",
        "evaluator_relationship": "third_party",
        "eval_library_name": "HELM",
        "eval_library_version": "v0.0.0",
        "parent_eval_output_dir": str(out_dir),
    }
    adapter = HELMAdapter()
    aggregates = adapter.transform_from_directory(
        str(HELM_FIXTURE_RUN),
        output_path=str(out_dir / "ignored.jsonl"),
        metadata_args=metadata_args,
    )
    assert aggregates, "HELMAdapter should yield at least one EvaluationLog"

    # The HELM EEE adapter writes the per-instance JSONLs to disk but the
    # aggregate JSON has to be saved manually. Save it next to the JSONL.
    eee_log = aggregates[0]
    detail = eee_log.detailed_evaluation_results
    assert detail is not None and detail.file_path
    aggregate_path = Path(detail.file_path).with_name(
        Path(detail.file_path).stem.removesuffix("_samples") + ".json"
    )
    aggregate_path.write_text(eee_log.model_dump_json(exclude_none=True))

    ref = NormalizedRunRef.from_eee_artifact(
        out_dir,
        source_kind=SourceKind.OFFICIAL,
        helm_run_path=HELM_FIXTURE_RUN,
        component_id="official::test::v0.0.0::mmlu_philosophy",
        logical_run_key="mmlu:subject=philosophy",
        display_name="mmlu_philosophy_gpt2",
    )
    run = load_run(ref)
    assert run.artifact_format is ArtifactFormat.EEE
    assert run.source_kind is SourceKind.OFFICIAL
    assert run.ref.origin.helm_run_path == HELM_FIXTURE_RUN
    assert run.ref.origin.eee_artifact_path is not None
    assert run.metrics_by_id()
    # HELM-origin EEE uses the aggregate from EEE, but per-instance drilldown
    # is normalized from raw HELM so separately converted artifacts keep
    # stable sample ids for official/local joins.
    assert run.instances
    assert all(rec.sample_hash is None for rec in run.instances)
    assert all(rec.metric_id for rec in run.instances)
