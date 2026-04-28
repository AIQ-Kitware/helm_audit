"""Stage-4 tests for the EEE-shape comparison core."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_audit.normalized import (
    NormalizedRunRef,
    SourceKind,
    load_run,
)
from eval_audit.normalized import compare as ncompare


REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_FIXTURE_RUN = (
    REPO_ROOT
    / "submodules"
    / "every_eval_ever"
    / "tests"
    / "data"
    / "helm"
    / "mmlu:subject=philosophy,method=multiple_choice_joint,model=openai_gpt2"
)


pytestmark = pytest.mark.skipif(
    not HELM_FIXTURE_RUN.exists(),
    reason=f"HELM fixture not present: {HELM_FIXTURE_RUN}",
)


def _load() -> "object":
    pytest.importorskip("every_eval_ever")
    pytest.importorskip("helm")
    ref = NormalizedRunRef.from_helm_run(HELM_FIXTURE_RUN, source_kind=SourceKind.OFFICIAL)
    return load_run(ref)


def test_run_level_self_pair_zero_delta() -> None:
    nrun = _load()
    rows = ncompare.run_level_core_rows(nrun, nrun)
    assert rows, "expected at least one core run-level row"
    assert all(row["abs_delta"] == 0.0 for row in rows)
    assert all(row["metric_class"] == "core" for row in rows)


def test_instance_level_self_pair_zero_delta() -> None:
    nrun = _load()
    rows = ncompare.instance_level_core_rows(nrun, nrun)
    assert rows, "expected at least one core instance-level row"
    assert all(row["abs_delta"] == 0.0 for row in rows)
    metrics = {row["metric"] for row in rows}
    # The MMLU fixture has multiple core metrics (exact_match family).
    assert any(m.startswith("exact_match") for m in metrics)


def test_core_metric_keys_are_core_classified() -> None:
    nrun = _load()
    keys = ncompare.core_metric_keys(nrun)
    assert keys
    from eval_audit.helm import metrics as hm
    for key in keys:
        cls, _ = hm.classify_metric(key)
        assert cls == "core"
