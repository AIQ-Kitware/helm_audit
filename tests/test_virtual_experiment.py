"""Tests for the virtual-experiment manifest + compose layer.

These exercise the YAML loader, scope matching with kwutil MultiPattern,
and the in-memory compose result. End-to-end CLI execution is covered by
the live pythia-mmlu run rather than a synthetic test, since the heavy
analyze_experiment pipeline already has its own coverage.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from eval_audit.virtual import (
    AuditIndexSource,
    OfficialPublicIndexSource,
    ScopeFilter,
    VirtualExperimentManifest,
    compose_virtual_experiment,
    load_manifest,
    write_synthesized_indexes,
)
from eval_audit.virtual.compose import provenance_payload
from eval_audit.virtual.manifest import (
    ExternalEeeComponent,
    ExternalEeeSource,
    ManifestError,
    parse_manifest,
)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({k for row in rows for k in row.keys()}) if rows else []
    with path.open("w", newline="") as fh:
        if not fieldnames:
            return
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _local_row(experiment, model, benchmark, run_entry, run_path="/some/run"):
    return {
        "experiment_name": experiment,
        "model": model,
        "benchmark": benchmark,
        "run_entry": run_entry,
        "run_path": run_path,
        "run_dir": run_path,
        "logical_run_key": run_entry,
    }


def _official_row(run_name, suite_version, run_path="/public/run", model=None):
    # Official rows do not directly carry ``benchmark`` — it must be parsed
    # from run_name. Model column is populated at index time from run_spec.
    return {
        "run_name": run_name,
        "logical_run_key": run_name,
        "public_track": "classic",
        "suite_version": suite_version,
        "run_path": run_path,
        "model": model or "",
    }


def test_load_manifest_round_trip(tmp_path):
    manifest_data = {
        "schema_version": 1,
        "name": "pythia-mmlu-stress",
        "description": "test",
        "scope": {
            "models": ["regex:eleutherai/pythia-.*"],
            "benchmarks": ["mmlu"],
        },
        "sources": [
            {"kind": "audit_index", "fpath": "/tmp/audit.csv", "include_experiments": ["a", "b"]},
            {"kind": "official_public_index", "fpath": "/tmp/official.csv"},
            {"kind": "external_eee", "components": []},
        ],
        "output": {"root": "/tmp/out"},
    }
    fpath = tmp_path / "manifest.yaml"
    fpath.write_text(yaml.safe_dump(manifest_data))
    manifest = load_manifest(fpath)
    assert manifest.name == "pythia-mmlu-stress"
    assert manifest.scope.models == ["regex:eleutherai/pythia-.*"]
    assert manifest.scope.benchmarks == ["mmlu"]
    assert len(manifest.audit_sources) == 1
    assert manifest.audit_sources[0].include_experiments == ["a", "b"]
    assert len(manifest.official_sources) == 1
    assert len(manifest.external_eee_sources) == 1
    assert manifest.external_eee_sources[0].components == []
    assert manifest.output_root == Path("/tmp/out")


def test_load_manifest_rejects_unknown_source_kind():
    with pytest.raises(ManifestError, match="not one of"):
        parse_manifest({
            "name": "x", "description": "",
            "output": {"root": "/tmp/out"},
            "sources": [{"kind": "mystery", "fpath": "/tmp/q.csv"}],
        })


def test_load_manifest_rejects_unsupported_schema_version():
    with pytest.raises(ManifestError, match="unsupported schema_version"):
        parse_manifest({
            "schema_version": 99,
            "name": "x", "description": "",
            "output": {"root": "/tmp/out"},
        })


def test_load_manifest_requires_output_root():
    with pytest.raises(ManifestError, match="missing required key 'root'"):
        parse_manifest({
            "name": "x", "description": "",
            "output": {},
        })


def test_load_manifest_parses_external_eee_components():
    parsed = parse_manifest({
        "name": "x", "description": "",
        "output": {"root": "/tmp/out"},
        "sources": [
            {"kind": "external_eee", "components": [
                {
                    "id": "inspectai-1",
                    "eee_artifact_path": "/tmp/eee_output",
                    "run_entry": "mmlu:model=foo,data_augmentation=canonical",
                    "display_name": "inspectai",
                    "provenance": {"tool": "inspect-ai"},
                },
            ]},
        ],
    })
    assert len(parsed.external_eee_sources) == 1
    components = parsed.external_eee_sources[0].components
    assert len(components) == 1
    assert components[0].id == "inspectai-1"
    assert components[0].provenance == {"tool": "inspect-ai"}


def test_compose_filters_by_scope_and_include_experiments(tmp_path):
    audit_csv = tmp_path / "audit.csv"
    official_csv = tmp_path / "official.csv"
    _write_csv(audit_csv, [
        # In scope (pythia + mmlu) and in include_experiments
        _local_row("audit-mmlu-usfp-pythia-r1", "eleutherai/pythia-6.9b", "mmlu",
                   "mmlu:model=eleutherai/pythia-6.9b"),
        # Different model — out by scope
        _local_row("audit-mmlu-usfp-pythia-r1", "openai/gpt-4", "mmlu",
                   "mmlu:model=openai/gpt-4"),
        # Different benchmark — out by scope
        _local_row("audit-mmlu-usfp-pythia-r1", "eleutherai/pythia-6.9b", "boolq",
                   "boolq:model=eleutherai/pythia-6.9b"),
        # Right model+benchmark but excluded experiment
        _local_row("audit-other", "eleutherai/pythia-6.9b", "mmlu",
                   "mmlu:model=eleutherai/pythia-6.9b"),
    ])
    _write_csv(official_csv, [
        # mmlu pythia-6.9b — passes
        _official_row("mmlu:subject=us_foreign_policy,model=eleutherai_pythia-6.9b,data_augmentation=canonical",
                      "v0.3.0", model="eleutherai/pythia-6.9b"),
        # boolq — out by scope
        _official_row("boolq:model=eleutherai_pythia-6.9b,data_augmentation=canonical",
                      "v0.3.0", model="eleutherai/pythia-6.9b"),
        # mmlu but model gpt-4 — out by scope
        _official_row("mmlu:subject=algebra,model=openai_gpt-4,data_augmentation=canonical",
                      "v0.3.0", model="openai/gpt-4"),
    ])
    manifest = VirtualExperimentManifest(
        name="virt", description="",
        scope=ScopeFilter(
            models=["regex:eleutherai/pythia-.*"],
            benchmarks=["mmlu"],
        ),
        audit_sources=[AuditIndexSource(
            fpath=audit_csv,
            include_experiments=["audit-mmlu-usfp-pythia-r1"],
        )],
        official_sources=[OfficialPublicIndexSource(fpath=official_csv)],
        external_eee_sources=[],
        output_root=tmp_path / "out",
    )
    result = compose_virtual_experiment(manifest)
    assert len(result.local_rows) == 1, "scope + include_experiments should leave exactly one local row"
    assert result.local_rows[0]["model"] == "eleutherai/pythia-6.9b"
    assert result.local_rows[0]["benchmark"] == "mmlu"
    assert result.local_rows[0]["experiment_name"] == "virt", "virtual name must be stamped"
    assert result.local_rows[0]["source_experiment_name"] == "audit-mmlu-usfp-pythia-r1", \
        "original experiment must be preserved as provenance"
    assert len(result.official_rows) == 1
    assert "pythia-6.9b" in result.official_rows[0]["run_name"]
    assert result.discarded_local_count == 3
    assert result.discarded_official_count == 2


def test_compose_falls_back_to_run_name_for_official_benchmark(tmp_path):
    """Official rows have no ``benchmark`` column; benchmark must be parsed from run_name."""
    official_csv = tmp_path / "official.csv"
    _write_csv(official_csv, [
        # No benchmark column populated; only run_name
        _official_row("mmlu:subject=algebra,model=eleutherai_pythia-6.9b,data_augmentation=canonical",
                      "v0.3.0", model="eleutherai/pythia-6.9b"),
    ])
    audit_csv = tmp_path / "audit.csv"
    _write_csv(audit_csv, [])  # no local rows
    manifest = VirtualExperimentManifest(
        name="virt", description="",
        scope=ScopeFilter(models=["regex:eleutherai/pythia-.*"], benchmarks=["mmlu"]),
        audit_sources=[AuditIndexSource(fpath=audit_csv)],
        official_sources=[OfficialPublicIndexSource(fpath=official_csv)],
        external_eee_sources=[],
        output_root=tmp_path / "out",
    )
    result = compose_virtual_experiment(manifest)
    assert len(result.official_rows) == 1, \
        "benchmark parsed from run_name should let the row pass the scope filter"


def test_write_synthesized_indexes_round_trips(tmp_path):
    audit_csv = tmp_path / "audit.csv"
    _write_csv(audit_csv, [
        _local_row("e1", "m1", "b1", "b1:model=m1"),
        _local_row("e1", "m1", "b1", "b1:model=m1"),
    ])
    manifest = VirtualExperimentManifest(
        name="virt", description="",
        scope=ScopeFilter(),
        audit_sources=[AuditIndexSource(fpath=audit_csv)],
        official_sources=[],
        external_eee_sources=[],
        output_root=tmp_path / "out",
    )
    result = compose_virtual_experiment(manifest)
    paths = write_synthesized_indexes(result, indexes_dpath=tmp_path / "out" / "indexes")
    assert paths["audit_index_fpath"].is_file()
    rows_back = list(csv.DictReader(paths["audit_index_fpath"].open()))
    assert len(rows_back) == 2
    assert all(r["experiment_name"] == "virt" for r in rows_back)
    assert all(r["source_experiment_name"] == "e1" for r in rows_back)


def test_pre_filter_helm_stage1_parsing_and_scoped_inventory():
    """An ``official_public_index`` source can declare a ``helm_stage1``
    pre_filter; the composer then re-stamps the upstream inventory so a
    row is ``selected`` iff it both passed Stage-1 AND matches manifest
    scope."""
    from eval_audit.virtual.compose import build_scoped_filter_inventory
    from eval_audit.virtual.manifest import HelmStage1PreFilter

    parsed = parse_manifest({
        "name": "x", "description": "",
        "output": {"root": "/tmp/out"},
        "scope": {"models": ["regex:eleutherai/pythia-.*"], "benchmarks": ["mmlu"]},
        "sources": [
            {
                "kind": "official_public_index",
                "fpath": "/tmp/official.csv",
                "pre_filter": {
                    "kind": "helm_stage1",
                    "inventory_fpath": "/tmp/inv.json",
                },
            },
        ],
    })
    assert len(parsed.official_sources) == 1
    pf = parsed.official_sources[0].pre_filter
    assert isinstance(pf, HelmStage1PreFilter)
    assert pf.kind == "helm_stage1"
    assert pf.inventory_fpath == Path("/tmp/inv.json")

    # An inventory with three rows: one selected+in-scope, one
    # selected+out-of-scope, one excluded+in-scope.
    pre_inv = [
        {
            "run_spec_name": "mmlu:subject=foo,model=eleutherai_pythia-6.9b",
            "model": "eleutherai/pythia-6.9b",
            "benchmark": "mmlu",
            "selection_status": "selected",
            "failure_reasons": [],
        },
        {
            "run_spec_name": "boolq:model=eleutherai_pythia-6.9b",
            "model": "eleutherai/pythia-6.9b",
            "benchmark": "boolq",
            "selection_status": "selected",
            "failure_reasons": [],
        },
        {
            "run_spec_name": "mmlu:subject=foo,model=anthropic_claude",
            "model": "anthropic/claude",
            "benchmark": "mmlu",
            "selection_status": "excluded",
            "failure_reasons": ["not-open-access"],
        },
    ]
    out = build_scoped_filter_inventory(manifest=parsed, pre_filter_inventory=pre_inv)
    by_name = {r["run_spec_name"]: r for r in out}
    # In scope (mmlu+pythia) AND originally selected -> still selected
    assert by_name["mmlu:subject=foo,model=eleutherai_pythia-6.9b"]["selection_status"] == "selected"
    # Originally selected but out of scope (wrong benchmark) -> excluded
    assert by_name["boolq:model=eleutherai_pythia-6.9b"]["selection_status"] == "excluded"
    assert "excluded-by-manifest-scope" in by_name["boolq:model=eleutherai_pythia-6.9b"]["failure_reasons"]
    # Originally excluded for an upstream reason; in scope but stays excluded
    out_of_scope_excluded = by_name["mmlu:subject=foo,model=anthropic_claude"]
    # ``anthropic/claude`` doesn't match scope so it's also tagged.
    assert out_of_scope_excluded["selection_status"] == "excluded"


def test_provenance_payload_records_external_components_without_consuming_them():
    component = ExternalEeeComponent(
        id="inspectai-1",
        eee_artifact_path=Path("/tmp/eee_output"),
        run_entry="mmlu:model=foo",
        display_name="inspectai",
        provenance={"tool": "inspect-ai"},
    )
    manifest = VirtualExperimentManifest(
        name="virt", description="",
        scope=ScopeFilter(),
        audit_sources=[],
        official_sources=[],
        external_eee_sources=[ExternalEeeSource(components=[component])],
        output_root=Path("/tmp/out"),
    )
    result = compose_virtual_experiment(manifest)
    payload = provenance_payload(result)
    assert payload["totals"]["external_components"] == 1
    assert payload["external_eee_components"][0]["id"] == "inspectai-1"
    # And the loud note about non-consumption is there so the user knows
    # the components are recorded but inert in this iteration.
    assert "external_eee components are recorded" in payload["notes"].lower()
