"""End-to-end test for EEE-only virtual experiments.

Composes the checked-in EEE demo fixture as a virtual experiment via
``eval-audit-build-virtual-experiment`` (using
``configs/virtual-experiments/eee-only-demo.yaml``) and asserts the
synthesized index slices, the per-packet reports, and the aggregate
summary all match the engineered drift map.

Marked ``slow`` because the full pipeline (compose → analyze →
aggregate summary) takes a few seconds. Run with ``pytest --run-slow``.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "eee_only_demo" / "eee_artifacts"


def _write_manifest(tmp_path: Path, *, output_root: Path) -> Path:
    """Synthesize an EEE-only manifest using the checked-in demo fixture.

    Mirrors ``configs/virtual-experiments/eee-only-demo.yaml`` but with an
    output root inside the test's tmp_path so we don't pollute /tmp.
    """
    fpath = tmp_path / "manifest.yaml"
    fpath.write_text(textwrap.dedent(f"""\
        schema_version: 1
        name: eee-only-demo-virtual-test
        description: EEE-only demo virtual experiment used by the pytest suite.

        scope:
          models:
            - "regex:toy/m[0-9]+-.*"
          benchmarks:
            - "arc_easy"
            - "imdb"
            - "truthful_qa"

        sources:
          - kind: eee_root
            root: {FIXTURE_ROOT}
            side: both

        output:
          root: {output_root}
        """))
    return fpath


@pytest.fixture(scope="module")
def virtual_experiment_output(tmp_path_factory) -> Path:
    """Build the virtual experiment once per session; reuse across tests."""
    if not FIXTURE_ROOT.exists():
        pytest.skip(f"EEE demo fixture missing: {FIXTURE_ROOT}")
    tmp_path = tmp_path_factory.mktemp("virtual_eee_only")
    output_root = tmp_path / "output"
    manifest_fpath = _write_manifest(tmp_path, output_root=output_root)
    cmd = [
        sys.executable, "-m", "eval_audit.cli.build_virtual_experiment",
        "--manifest", str(manifest_fpath),
        "--allow-single-repeat",
    ]
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)
    return output_root


def test_synthesized_indexes_present(virtual_experiment_output: Path) -> None:
    """compose materializes both indexes from the EEE tree."""
    audit = virtual_experiment_output / "indexes" / "audit_results_index.csv"
    official = virtual_experiment_output / "indexes" / "official_public_index.csv"
    assert audit.is_file()
    assert official.is_file()
    audit_rows = list(csv.DictReader(audit.open()))
    official_rows = list(csv.DictReader(official.open()))
    assert len(audit_rows) == 10  # 9 single-attempt + 1 repeat for arc_easy m1-small
    assert len(official_rows) == 9


def test_synthesized_rows_carry_eee_artifact_format(virtual_experiment_output: Path) -> None:
    """Every synthesized row must announce ``artifact_format=eee``; planner
    integration depends on this distinction.
    """
    audit = virtual_experiment_output / "indexes" / "audit_results_index.csv"
    official = virtual_experiment_output / "indexes" / "official_public_index.csv"
    for fpath in (audit, official):
        for row in csv.DictReader(fpath.open()):
            assert row["artifact_format"] == "eee", row
            assert row["eee_artifact_path"], row


def test_local_rows_stamped_with_virtual_experiment_name(virtual_experiment_output: Path) -> None:
    """Compose stamps every local row with the virtual experiment's name so
    the planner's --experiment-name filter selects exactly this slice.
    """
    audit = virtual_experiment_output / "indexes" / "audit_results_index.csv"
    rows = list(csv.DictReader(audit.open()))
    for row in rows:
        assert row["experiment_name"] == "eee-only-demo-virtual-test"
        # The original (subdir-derived) experiment name is preserved.
        assert row["source_experiment_name"] in {"eee_only_local", "primary", "repeat"}


def test_provenance_records_eee_root_counts(virtual_experiment_output: Path) -> None:
    """provenance.json should expose the per-source counts from the EEE root."""
    payload = json.loads((virtual_experiment_output / "provenance.json").read_text())
    eee_roots = payload.get("eee_root_sources") or []
    assert len(eee_roots) == 1
    src = eee_roots[0]
    assert src["side"] == "both"
    assert src["official_seen"] == 9 and src["official_retained"] == 9
    assert src["local_seen"] == 10 and src["local_retained"] == 10


def test_per_packet_reports_built(virtual_experiment_output: Path) -> None:
    """analyze_experiment should land 9 per-packet core_metric reports."""
    reports_dpath = virtual_experiment_output / "analysis" / "core-reports"
    assert reports_dpath.is_dir()
    packets = sorted(p.name for p in reports_dpath.iterdir() if p.is_dir())
    assert len(packets) == 9
    for packet in packets:
        for name in ("core_metric_report.latest.json", "core_metric_report.latest.txt"):
            assert (reports_dpath / packet / name).is_file(), (packet, name)


def test_aggregate_summary_buckets_match_drift_map(virtual_experiment_output: Path) -> None:
    """Build the aggregate summary on top of the virtual experiment and
    assert the engineered drift counts (6 exact / 2 low / 1 zero) come
    through unchanged.
    """
    summary_root = virtual_experiment_output / "aggregate-summary"
    cmd = [
        sys.executable, "-m", "eval_audit.workflows.build_reports_summary",
        "--no-filter-inventory",
        "--no-canonical-scan",
        "--analysis-root", str(virtual_experiment_output),
        "--index-fpath", str(virtual_experiment_output / "indexes" / "audit_results_index.csv"),
        "--summary-root", str(summary_root),
        "--experiment-name", "eee-only-demo-virtual-test",
    ]
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)

    rows_csv = summary_root / "experiment_name-eee-only-demo-virtual-test" / "reproducibility_rows.latest.csv"
    assert rows_csv.is_file()
    bucket_counts: dict[str, int] = {}
    for row in csv.DictReader(rows_csv.open()):
        bucket = row.get("official_instance_agree_bucket", "")
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    assert bucket_counts.get("exact_or_near_exact", 0) == 6, bucket_counts
    assert bucket_counts.get("low_agreement_0.00+", 0) == 2, bucket_counts
    assert bucket_counts.get("zero_agreement", 0) == 1, bucket_counts


def test_external_eee_component_is_consumed(tmp_path_factory) -> None:
    """external_eee components are now materialized as planner-visible rows
    (not provenance-only). Composes a manifest with one external_eee
    component pointing at a fixture artifact and asserts the synthesized
    audit index contains a row for it on the local side.
    """
    if not FIXTURE_ROOT.exists():
        pytest.skip("fixture missing")
    tmp_path = tmp_path_factory.mktemp("external_eee_consumed")
    output_root = tmp_path / "output"
    artifact_dir = FIXTURE_ROOT / "official" / "imdb" / "toy" / "m1-small"
    manifest_fpath = tmp_path / "manifest.yaml"
    manifest_fpath.write_text(textwrap.dedent(f"""\
        schema_version: 1
        name: external-eee-consumed-test
        description: external_eee + scope filter coverage.
        scope:
          models: ["regex:toy/.*"]
          benchmarks: ["imdb"]
        sources:
          - kind: external_eee
            components:
              - id: synthetic-1
                eee_artifact_path: {artifact_dir}
                run_entry: "imdb:model=toy/m1-small"
                display_name: "synthetic external m1"
                side: local
        output:
          root: {output_root}
        """))
    cmd = [
        sys.executable, "-m", "eval_audit.cli.build_virtual_experiment",
        "--manifest", str(manifest_fpath),
        "--compose-only",
    ]
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)

    audit_rows = list(csv.DictReader(
        (output_root / "indexes" / "audit_results_index.csv").open()
    ))
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["external_eee_component_id"] == "synthetic-1"
    assert row["logical_run_key"] == "imdb:model=toy/m1-small"
    assert row["artifact_format"] == "eee"
    assert row["experiment_name"] == "external-eee-consumed-test"

    payload = json.loads((output_root / "provenance.json").read_text())
    assert payload["external_eee_materialized"] == {"local": 1, "official": 0, "discarded": 0}
