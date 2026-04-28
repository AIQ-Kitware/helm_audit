"""Apply scope filters to source indexes and synthesize a virtual slice.

The compose step:
  1. Loads each ``audit_index`` source, applies ``include_experiments``
     (exact match), then the manifest scope (kwutil.MultiPattern over
     the model/benchmark columns).
  2. Loads each ``official_public_index`` source, applies the same scope
     to the model + benchmark fields (parsed from the run_name when not
     directly populated on the row).
  3. Stamps every retained local row with ``experiment_name = <virtual_name>``
     so the existing planner --experiment-name filter selects exactly the
     synthesized slice. The original experiment name is preserved in
     ``source_experiment_name`` for provenance.
  4. Records external_eee components in the provenance file but does not
     yet plumb them into the planner (a later pass will add an
     ``external`` source_kind alongside ``local``/``official``).
  5. When an ``official_public_index`` source declares a ``pre_filter``,
     re-stamps the upstream filter inventory with manifest-scope-aware
     ``selection_status`` and writes a scoped inventory file the
     publication-side ``build_reports_summary`` can use to render
     Sankey A (Universe -> Scope) for this virtual experiment.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Iterable

import kwutil

from helm_audit.virtual.manifest import (
    ExternalEeeComponent,
    ScopeFilter,
    VirtualExperimentManifest,
)


# Parses ``benchmark:...,model=<value>,...`` from a run_name. Used as a
# fallback when the official index does not directly populate ``benchmark``.
_BENCHMARK_PREFIX_RE = re.compile(r"^([^:]+):")
_MODEL_KEY_RE = re.compile(r"(?:^|,)model=([^,]+)")


def _parse_benchmark_from_run_name(run_name: str | None) -> str | None:
    if not run_name:
        return None
    match = _BENCHMARK_PREFIX_RE.match(run_name)
    return match.group(1) if match else None


def _parse_model_from_run_name(run_name: str | None) -> str | None:
    if not run_name:
        return None
    match = _MODEL_KEY_RE.search(run_name)
    if not match:
        return None
    # Public HELM uses underscore-separated org_model in run_name; our
    # scope filters typically use the canonical "org/model" form, so
    # restore the slash before matching.
    return match.group(1).replace("_", "/", 1)


def _row_attrs(row: dict[str, Any], *, source_kind: str) -> tuple[str | None, str | None]:
    """Return (model, benchmark) from a row, falling back to run_name parsing."""
    model = (row.get("model") or "").strip() or None
    benchmark = (row.get("benchmark") or "").strip() or None
    if not benchmark:
        # Official rows don't carry ``benchmark`` directly; derive from run_name.
        benchmark = _parse_benchmark_from_run_name(row.get("run_name") or row.get("logical_run_key"))
    if not model:
        model = _parse_model_from_run_name(row.get("run_name") or row.get("logical_run_key"))
    return model, benchmark


def _coerce_pattern(values: list[str]):
    """Build a kwutil MultiPattern, or None if no patterns declared."""
    if not values:
        return None
    return kwutil.MultiPattern.coerce(values)


def _scope_match(row: dict[str, Any], scope: ScopeFilter, *, source_kind: str) -> bool:
    """Return True if this row passes the manifest scope filter."""
    model, benchmark = _row_attrs(row, source_kind=source_kind)
    model_pat = _coerce_pattern(scope.models)
    if model_pat is not None:
        if not model or not model_pat.match(model):
            return False
    bench_pat = _coerce_pattern(scope.benchmarks)
    if bench_pat is not None:
        if not benchmark or not bench_pat.match(benchmark):
            return False
    return True


def _load_index_rows(fpath: Path) -> list[dict[str, Any]]:
    if not fpath.is_file():
        raise FileNotFoundError(f"index not found: {fpath}")
    with fpath.open(newline="") as fh:
        return list(csv.DictReader(fh))


@dataclasses.dataclass
class ComposeResult:
    """Outcome of composing a virtual experiment from declared sources."""
    manifest: VirtualExperimentManifest
    local_rows: list[dict[str, Any]]
    official_rows: list[dict[str, Any]]
    external_components: list[ExternalEeeComponent]
    discarded_local_count: int
    discarded_official_count: int
    per_source_local_counts: list[dict[str, Any]]
    per_source_official_counts: list[dict[str, Any]]


def compose_virtual_experiment(manifest: VirtualExperimentManifest) -> ComposeResult:
    """Apply scope + include filters to each declared source and synthesize the slice.

    Local rows have their ``experiment_name`` stamped to the virtual name so
    the existing planner accepts the synthesized index unchanged. The
    original experiment name is preserved on each row as
    ``source_experiment_name``.
    """
    local_rows: list[dict[str, Any]] = []
    discarded_local = 0
    per_source_local: list[dict[str, Any]] = []
    for src in manifest.audit_sources:
        rows = _load_index_rows(src.fpath)
        retained = 0
        for row in rows:
            if src.include_experiments and row.get("experiment_name") not in src.include_experiments:
                discarded_local += 1
                continue
            if not _scope_match(row, manifest.scope, source_kind="local"):
                discarded_local += 1
                continue
            stamped = dict(row)
            stamped["source_experiment_name"] = stamped.get("experiment_name")
            stamped["source_index_fpath"] = str(src.fpath)
            stamped["experiment_name"] = manifest.name
            local_rows.append(stamped)
            retained += 1
        per_source_local.append({
            "fpath": str(src.fpath),
            "include_experiments": list(src.include_experiments),
            "rows_seen": len(rows),
            "rows_retained": retained,
        })

    official_rows: list[dict[str, Any]] = []
    discarded_official = 0
    per_source_official: list[dict[str, Any]] = []
    for src in manifest.official_sources:
        rows = _load_index_rows(src.fpath)
        retained = 0
        for row in rows:
            if not _scope_match(row, manifest.scope, source_kind="official"):
                discarded_official += 1
                continue
            stamped = dict(row)
            stamped["source_index_fpath"] = str(src.fpath)
            official_rows.append(stamped)
            retained += 1
        per_source_official.append({
            "fpath": str(src.fpath),
            "rows_seen": len(rows),
            "rows_retained": retained,
        })

    external_components: list[ExternalEeeComponent] = []
    for src in manifest.external_eee_sources:
        external_components.extend(src.components)

    return ComposeResult(
        manifest=manifest,
        local_rows=local_rows,
        official_rows=official_rows,
        external_components=external_components,
        discarded_local_count=discarded_local,
        discarded_official_count=discarded_official,
        per_source_local_counts=per_source_local,
        per_source_official_counts=per_source_official,
    )


def _write_csv(rows: list[dict[str, Any]], fpath: Path) -> None:
    """Write rows to CSV, taking the union of keys for fieldnames."""
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()}) if rows else []
    with fpath.open("w", newline="") as fh:
        if not fieldnames:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_synthesized_indexes(
    result: ComposeResult,
    *,
    indexes_dpath: Path,
) -> dict[str, Path]:
    """Materialize the synthesized local + official index slices on disk.

    Returns a mapping with keys ``audit_index_fpath`` and
    ``official_index_fpath`` pointing at the written CSVs (suitable for
    feeding to ``analyze_experiment.main`` directly).
    """
    indexes_dpath.mkdir(parents=True, exist_ok=True)
    audit_fpath = indexes_dpath / "audit_results_index.csv"
    official_fpath = indexes_dpath / "official_public_index.csv"
    _write_csv(result.local_rows, audit_fpath)
    _write_csv(result.official_rows, official_fpath)
    return {
        "audit_index_fpath": audit_fpath,
        "official_index_fpath": official_fpath,
    }


def _external_component_to_row(component: ExternalEeeComponent) -> dict[str, Any]:
    """Provenance-only row for an external EEE component (not consumed yet)."""
    return {
        "id": component.id,
        "eee_artifact_path": str(component.eee_artifact_path),
        "run_entry": component.run_entry,
        "display_name": component.display_name,
        "provenance": dict(component.provenance),
    }


def build_scoped_filter_inventory(
    *,
    manifest: VirtualExperimentManifest,
    pre_filter_inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-stamp a Stage-1 filter inventory with manifest-scope-aware status.

    A row is ``selected`` iff it both passed the upstream pre-filter AND
    matches the manifest scope (``ScopeFilter`` MultiPattern over the
    row's ``model`` / ``benchmark``). Rows that fail the manifest scope
    keep their original ``failure_reasons`` augmented with
    ``excluded-by-manifest-scope`` and have ``selection_status`` set to
    something other than ``selected`` so Stage-A sankeys render the
    manifest scope as the terminal gate of the funnel.

    Output preserves the original schema so any downstream consumer
    (notably ``build_reports_summary``) can read it as a drop-in
    replacement.
    """
    out: list[dict[str, Any]] = []
    for row in pre_filter_inventory:
        new_row = dict(row)
        original_status = new_row.get("selection_status")
        in_scope = _scope_match(new_row, manifest.scope, source_kind="pre_filter")
        if not in_scope:
            # Augment failure_reasons rather than replace them — preserve
            # the upstream gate signal so the sankey still shows "structural"
            # / "deployment" / etc. exclusions for rows excluded earlier.
            reasons = list(new_row.get("failure_reasons") or [])
            if "excluded-by-manifest-scope" not in reasons:
                reasons.append("excluded-by-manifest-scope")
            new_row["failure_reasons"] = reasons
            new_row["selection_status"] = "excluded"
        else:
            # In scope. ``selected`` only if the upstream filter also passed.
            if original_status != "selected":
                pass  # leave the upstream excluded status intact
            else:
                new_row["selection_status"] = "selected"
        out.append(new_row)
    return out


def write_scoped_filter_inventory(
    inventory: list[dict[str, Any]],
    *,
    out_fpath: Path,
) -> Path:
    out_fpath.parent.mkdir(parents=True, exist_ok=True)
    out_fpath.write_text(json.dumps(inventory, indent=2) + "\n")
    return out_fpath


def provenance_payload(result: ComposeResult) -> dict[str, Any]:
    """Build the provenance.json payload describing what was composed."""
    manifest = result.manifest
    return {
        "schema_version": manifest.schema_version,
        "name": manifest.name,
        "description": manifest.description,
        "scope": {
            "models": list(manifest.scope.models),
            "benchmarks": list(manifest.scope.benchmarks),
        },
        "output_root": str(manifest.output_root),
        "audit_sources": result.per_source_local_counts,
        "official_sources": result.per_source_official_counts,
        "external_eee_components": [
            _external_component_to_row(c) for c in result.external_components
        ],
        "totals": {
            "local_rows_retained": len(result.local_rows),
            "official_rows_retained": len(result.official_rows),
            "local_rows_discarded": result.discarded_local_count,
            "official_rows_discarded": result.discarded_official_count,
            "external_components": len(result.external_components),
        },
        "notes": (
            "external_eee components are recorded for provenance only; "
            "the planner does not yet consume them. The next iteration "
            "will plumb them in as a third source_kind."
            if result.external_components else
            "no external_eee components declared in this manifest."
        ),
    }
