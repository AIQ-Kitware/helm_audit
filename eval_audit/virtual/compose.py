"""Apply scope filters to source indexes and synthesize a virtual slice.

The compose step:
  1. Loads each ``audit_index`` source, applies ``include_experiments``
     (exact match), then the manifest scope (kwutil.MultiPattern over
     the model/benchmark columns).
  2. Loads each ``official_public_index`` source, applies the same scope
     to the model + benchmark fields (parsed from the run_name when not
     directly populated on the row).
  3. Walks each ``eee_root`` source's tree (``official/`` + ``local/``
     subdirs), synthesizes index rows from the EEE aggregate JSONs, and
     applies the manifest scope. EEE rows interleave with HELM rows in
     the synthesized indexes; the planner already accepts mixed
     ``artifact_format=helm`` / ``artifact_format=eee`` rows.
  4. Materializes each ``external_eee`` component into a row on the
     side it declares (``local`` by default, or ``official``).
  5. Stamps every retained local row with ``experiment_name = <virtual_name>``
     so the existing planner --experiment-name filter selects exactly the
     synthesized slice. The original experiment name is preserved in
     ``source_experiment_name`` for provenance.
  6. When an ``official_public_index`` source declares a ``pre_filter``,
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

from eval_audit.cli.from_eee import (
    _build_local_index_row,
    _build_official_index_row,
    _discover_eee_artifacts,
    _extract_artifact_meta,
    detect_helm_sidecars,
)
from eval_audit.virtual.manifest import (
    EeeRootSource,
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
    per_source_eee_root_counts: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    external_eee_materialized_counts: dict[str, int] = dataclasses.field(default_factory=dict)


def _eee_rows_from_root(
    src: EeeRootSource,
    *,
    virtual_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Walk an EEE tree and synthesize official + local index rows.

    Honors ``side`` ("both" / "official" / "local") to skip half the
    tree when the user wants to use the same root for only one side.
    Each EEE artifact is run through the same row-builders
    ``eval-audit-from-eee`` uses, so the resulting rows look exactly
    like rows from a real EEE-only run; the planner already accepts
    them via the ``artifact_format=eee`` path.

    The caller is responsible for applying the manifest's ``scope``
    filter; this function returns *raw* (pre-scope) rows so the per-
    source bookkeeping in compose can record discard counts uniformly
    with the HELM-driven sources.
    """
    official_rows: list[dict[str, Any]] = []
    local_rows: list[dict[str, Any]] = []
    counts = {"official_seen": 0, "local_seen": 0}

    if src.side in {"both", "official"}:
        official_root = src.root / "official"
        for art in _discover_eee_artifacts(official_root):
            meta = _extract_artifact_meta(art, root=official_root)
            official_rows.append(_build_official_index_row(meta))
            counts["official_seen"] += 1

    if src.side in {"both", "local"}:
        local_root = src.root / "local"
        for art in _discover_eee_artifacts(local_root):
            meta = _extract_artifact_meta(art, root=local_root)
            # Build the row with its natural subdir-derived experiment name
            # (or the per-source override if the manifest declares one).
            # The compose loop will then re-stamp ``experiment_name`` to
            # the virtual experiment's name and preserve the original in
            # ``source_experiment_name``, exactly like ``audit_index``
            # rows do — keeping the stamping policy in one place.
            local_rows.append(
                _build_local_index_row(meta, experiment_override=src.experiment_name)
            )
            counts["local_seen"] += 1

    return official_rows, local_rows, counts


def _row_from_external_eee_component(
    component: ExternalEeeComponent,
    *,
    virtual_name: str,
) -> dict[str, Any]:
    """Synthesize an index row from a cherry-picked external EEE artifact.

    Loads the EEE aggregate to extract ``model_id`` and ``benchmark``,
    then builds a row in the same shape ``from_eee`` produces. The
    component's ``run_entry`` from the manifest **overrides** the
    derived ``logical_run_key`` so the user can pin an external
    artifact to a specific comparison even when its EEE metadata would
    have placed it elsewhere.
    """
    artifact_path = component.eee_artifact_path
    if artifact_path.is_file():
        json_path = artifact_path
    elif artifact_path.is_dir():
        candidates = sorted(
            f for f in artifact_path.glob("*.json")
            if f.name not in {"fixture_manifest.json", "provenance.json", "status.json", "run_spec.json"}
        )
        if not candidates:
            raise ManifestComposeError(
                f"external_eee.components[{component.id}]: no EEE aggregate JSON in {artifact_path}"
            )
        if len(candidates) > 1:
            listing = ", ".join(c.name for c in candidates)
            raise ManifestComposeError(
                f"external_eee.components[{component.id}]: multiple EEE aggregates in {artifact_path}: {listing}"
            )
        json_path = candidates[0]
    else:
        raise ManifestComposeError(
            f"external_eee.components[{component.id}]: path does not exist: {artifact_path}"
        )

    try:
        data = json.loads(json_path.read_text())
    except json.JSONDecodeError as exc:
        raise ManifestComposeError(
            f"external_eee.components[{component.id}]: cannot parse {json_path}: {exc}"
        )
    if not isinstance(data, dict) or "evaluation_results" not in data or "model_info" not in data:
        raise ManifestComposeError(
            f"external_eee.components[{component.id}]: {json_path} is not an EEE aggregate"
        )
    model_info = data.get("model_info") or {}
    model_id = (model_info.get("id") or model_info.get("name") or "").strip()
    eval_results = data.get("evaluation_results") or []
    if eval_results:
        first = eval_results[0]
        source_data = first.get("source_data") or {}
        benchmark = (
            source_data.get("dataset_name")
            or first.get("evaluation_name")
            or "unknown"
        )
    else:
        benchmark = "unknown"
    sidecars = detect_helm_sidecars(json_path.parent)
    meta = {
        "artifact_dir": json_path.parent,
        "json_path": json_path,
        "model_id": model_id,
        "benchmark": benchmark,
        "experiment_name": None,
        "evaluation_id": data.get("evaluation_id"),
        "run_spec_fpath": sidecars["run_spec_fpath"],
        "max_eval_instances": sidecars["max_eval_instances"],
    }
    if component.side == "official":
        row = _build_official_index_row(meta)
    else:
        row = _build_local_index_row(meta, experiment_override=virtual_name)
    # Pin the manifest's run_entry/logical_run_key so the planner groups
    # this component the way the manifest declares, even if the EEE
    # metadata would have produced a different key.
    row["logical_run_key"] = component.run_entry
    row["run_entry"] = component.run_entry
    row["run_spec_name"] = component.run_entry
    if component.side != "official":
        row["display_name"] = component.display_name
    row["external_eee_component_id"] = component.id
    return row


class ManifestComposeError(ValueError):
    """Raised when an EEE source resolves to something the composer can't use."""


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

    # eee_root sources: walk an EEE tree and synthesize official + local rows.
    per_source_eee_root: list[dict[str, Any]] = []
    for src in manifest.eee_root_sources:
        raw_official, raw_local, counts = _eee_rows_from_root(
            src, virtual_name=manifest.name
        )
        official_retained = 0
        for row in raw_official:
            if not _scope_match(row, manifest.scope, source_kind="official"):
                discarded_official += 1
                continue
            stamped = dict(row)
            stamped["source_index_fpath"] = f"eee_root:{src.root}"
            official_rows.append(stamped)
            official_retained += 1
        local_retained = 0
        for row in raw_local:
            if not _scope_match(row, manifest.scope, source_kind="local"):
                discarded_local += 1
                continue
            stamped = dict(row)
            stamped["source_experiment_name"] = stamped.get("experiment_name")
            stamped["source_index_fpath"] = f"eee_root:{src.root}"
            stamped["experiment_name"] = manifest.name
            local_rows.append(stamped)
            local_retained += 1
        per_source_eee_root.append({
            "root": str(src.root),
            "side": src.side,
            "experiment_name": src.experiment_name,
            "official_seen": counts["official_seen"],
            "local_seen": counts["local_seen"],
            "official_retained": official_retained,
            "local_retained": local_retained,
        })

    # external_eee components: each becomes one row on the side it declares.
    materialized_counts = {"local": 0, "official": 0, "discarded": 0}
    for component in external_components:
        row = _row_from_external_eee_component(component, virtual_name=manifest.name)
        if not _scope_match(row, manifest.scope, source_kind=("official" if component.side == "official" else "local")):
            materialized_counts["discarded"] += 1
            continue
        if component.side == "official":
            row["source_index_fpath"] = f"external_eee:{component.id}"
            official_rows.append(row)
            materialized_counts["official"] += 1
        else:
            row["source_experiment_name"] = row.get("experiment_name")
            row["source_index_fpath"] = f"external_eee:{component.id}"
            row["experiment_name"] = manifest.name
            local_rows.append(row)
            materialized_counts["local"] += 1

    return ComposeResult(
        manifest=manifest,
        local_rows=local_rows,
        official_rows=official_rows,
        external_components=external_components,
        discarded_local_count=discarded_local,
        discarded_official_count=discarded_official,
        per_source_local_counts=per_source_local,
        per_source_official_counts=per_source_official,
        per_source_eee_root_counts=per_source_eee_root,
        external_eee_materialized_counts=materialized_counts,
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
        "eee_root_sources": result.per_source_eee_root_counts,
        "external_eee_components": [
            _external_component_to_row(c) for c in result.external_components
        ],
        "external_eee_materialized": result.external_eee_materialized_counts,
        "totals": {
            "local_rows_retained": len(result.local_rows),
            "official_rows_retained": len(result.official_rows),
            "local_rows_discarded": result.discarded_local_count,
            "official_rows_discarded": result.discarded_official_count,
            "external_components": len(result.external_components),
        },
    }
