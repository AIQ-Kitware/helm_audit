"""Virtual-experiment YAML manifest schema and loader.

The manifest declares:
  - a name + description for the composed slice,
  - a scope (model / benchmark patterns) that filters rows from each source,
  - one or more sources (audit_index, official_public_index, external_eee),
  - an output root (must be outside the repo so derived results don't pollute
    the checked-in tree).

Schema (version 1, illustrative)::

    schema_version: 1
    name: pythia-mmlu-stress
    description: Pythia models on MMLU; existing local + official rows.
    scope:
      models: ["eleutherai/pythia-*"]      # kwutil.MultiPattern
      benchmarks: ["mmlu"]
    sources:
      - kind: audit_index
        fpath: /data/.../audit_results_index.latest.csv
        include_experiments: [audit-mmlu-usfp-pythia-r1, audit-historic-grid]
      - kind: official_public_index
        fpath: /data/.../official_public_index.latest.csv
      - kind: external_eee
        components:
          - id: inspectai-pythia-mmlu-2026-04
            eee_artifact_path: /path/to/.../eee_output
            run_entry: mmlu:subject=us_foreign_policy,...,model=eleutherai/pythia-6.9b,...
            display_name: "inspectai pythia 6.9b mmlu"
            provenance: {tool: inspect-ai, version: 0.3.x}
    output:
      root: /data/crfm-helm-audit-store/virtual-experiments/pythia-mmlu-stress
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_SCHEMA_VERSIONS = (1,)


class ManifestError(ValueError):
    """Raised when a manifest is structurally invalid."""


@dataclasses.dataclass
class ScopeFilter:
    """Multi-pattern filters applied to every source's rows.

    An empty list for a field means "no filter on this dimension".
    Patterns are kwutil.MultiPattern strings: a literal token, a
    ``regex:...`` form, or a list of either.
    """
    models: list[str] = dataclasses.field(default_factory=list)
    benchmarks: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AuditIndexSource:
    """A slice of the local audit-results index.

    Filters in order: ``include_experiments`` (exact match against
    ``experiment_name``) then the manifest-level ``scope``.
    """
    fpath: Path
    include_experiments: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class HelmStage1PreFilter:
    """A reference to a pre-existing HELM Stage-1 filter inventory.

    The inventory describes per-run eligibility decisions (structural
    completeness, metadata resolution, open-weight gating, tag/modality,
    deployment availability, size, and selection). When attached to an
    ``official_public_index`` source, the virtual-experiment composer
    re-stamps the inventory's ``selection_status`` so a row is
    ``selected`` iff it both passed the original Stage-1 filter AND
    matches the manifest scope. The resulting scope-aware inventory
    drives Sankey A (Universe -> Scope) inside build_reports_summary.
    """
    kind: str
    inventory_fpath: Path


@dataclasses.dataclass
class OfficialPublicIndexSource:
    """A slice of the official public-HELM index, scope-filtered.

    Optional ``pre_filter`` references an upstream eligibility decision
    (e.g. the HELM Stage-1 filter) that the composer re-stamps with
    manifest scope so the publication's Stage-A sankey reflects the
    full ``Universe -> source eligibility -> manifest scope -> selected``
    chain.
    """
    fpath: Path
    pre_filter: HelmStage1PreFilter | None = None


@dataclasses.dataclass
class ExternalEeeComponent:
    """An externally-produced EEE artifact (e.g. Inspect AI output).

    ``run_entry`` is the logical comparison key the planner already uses
    to group rows; matching ``run_entry`` to existing local/official rows
    is how the external component participates in a packet.
    """
    id: str
    eee_artifact_path: Path
    run_entry: str
    display_name: str
    provenance: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ExternalEeeSource:
    """Group of external EEE components.

    The compose step records these in the provenance file but does not
    yet plumb them into the planner. A future iteration will add an
    ``external`` source_kind alongside ``local`` and ``official``.
    """
    components: list[ExternalEeeComponent] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class VirtualExperimentManifest:
    """Top-level manifest: declarative slice over existing audit data."""
    name: str
    description: str
    scope: ScopeFilter
    audit_sources: list[AuditIndexSource]
    official_sources: list[OfficialPublicIndexSource]
    external_eee_sources: list[ExternalEeeSource]
    output_root: Path
    schema_version: int = 1


def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ManifestError(f"missing required key '{key}' in {where}")
    return d[key]


def _coerce_str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ManifestError(f"{where} must be a list of strings; got {value!r}")
        return list(value)
    raise ManifestError(f"{where} must be a string or list of strings; got {type(value).__name__}")


def _parse_scope(raw: Any) -> ScopeFilter:
    if raw is None:
        return ScopeFilter()
    if not isinstance(raw, dict):
        raise ManifestError(f"'scope' must be a mapping; got {type(raw).__name__}")
    return ScopeFilter(
        models=_coerce_str_list(raw.get("models"), "scope.models"),
        benchmarks=_coerce_str_list(raw.get("benchmarks"), "scope.benchmarks"),
    )


def _parse_external_components(raw: Any) -> list[ExternalEeeComponent]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ManifestError(f"external_eee.components must be a list; got {type(raw).__name__}")
    out: list[ExternalEeeComponent] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ManifestError(f"external_eee.components[{i}] must be a mapping")
        out.append(ExternalEeeComponent(
            id=str(_require(item, "id", f"external_eee.components[{i}]")),
            eee_artifact_path=Path(_require(item, "eee_artifact_path", f"external_eee.components[{i}]")).expanduser(),
            run_entry=str(_require(item, "run_entry", f"external_eee.components[{i}]")),
            display_name=str(_require(item, "display_name", f"external_eee.components[{i}]")),
            provenance=dict(item.get("provenance") or {}),
        ))
    return out


def _parse_sources(raw: Any) -> tuple[list[AuditIndexSource], list[OfficialPublicIndexSource], list[ExternalEeeSource]]:
    if raw is None:
        return [], [], []
    if not isinstance(raw, list):
        raise ManifestError(f"'sources' must be a list; got {type(raw).__name__}")
    audit: list[AuditIndexSource] = []
    official: list[OfficialPublicIndexSource] = []
    external: list[ExternalEeeSource] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ManifestError(f"sources[{i}] must be a mapping")
        kind = _require(item, "kind", f"sources[{i}]")
        if kind == "audit_index":
            audit.append(AuditIndexSource(
                fpath=Path(_require(item, "fpath", f"sources[{i}]")).expanduser(),
                include_experiments=_coerce_str_list(item.get("include_experiments"), f"sources[{i}].include_experiments"),
            ))
        elif kind == "official_public_index":
            pre_filter_raw = item.get("pre_filter")
            pre_filter: HelmStage1PreFilter | None = None
            if pre_filter_raw is not None:
                if not isinstance(pre_filter_raw, dict):
                    raise ManifestError(f"sources[{i}].pre_filter must be a mapping")
                pf_kind = _require(pre_filter_raw, "kind", f"sources[{i}].pre_filter")
                if pf_kind != "helm_stage1":
                    raise ManifestError(
                        f"sources[{i}].pre_filter.kind={pf_kind!r} is not yet supported "
                        "(supported: 'helm_stage1')"
                    )
                pre_filter = HelmStage1PreFilter(
                    kind=str(pf_kind),
                    inventory_fpath=Path(_require(
                        pre_filter_raw, "inventory_fpath", f"sources[{i}].pre_filter"
                    )).expanduser(),
                )
            official.append(OfficialPublicIndexSource(
                fpath=Path(_require(item, "fpath", f"sources[{i}]")).expanduser(),
                pre_filter=pre_filter,
            ))
        elif kind == "external_eee":
            external.append(ExternalEeeSource(
                components=_parse_external_components(item.get("components")),
            ))
        else:
            raise ManifestError(
                f"sources[{i}].kind={kind!r} is not one of "
                "{'audit_index', 'official_public_index', 'external_eee'}"
            )
    return audit, official, external


def parse_manifest(data: dict[str, Any]) -> VirtualExperimentManifest:
    """Validate and parse a YAML-loaded manifest dict into the dataclass."""
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a mapping")
    schema_version = data.get("schema_version", 1)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError(f"unsupported schema_version {schema_version}; supported: {SUPPORTED_SCHEMA_VERSIONS}")
    name = str(_require(data, "name", "manifest"))
    description = str(data.get("description", ""))
    output = data.get("output") or {}
    if not isinstance(output, dict):
        raise ManifestError("'output' must be a mapping")
    output_root = Path(_require(output, "root", "output")).expanduser()
    scope = _parse_scope(data.get("scope"))
    audit_sources, official_sources, external_eee_sources = _parse_sources(data.get("sources"))
    return VirtualExperimentManifest(
        schema_version=schema_version,
        name=name,
        description=description,
        scope=scope,
        audit_sources=audit_sources,
        official_sources=official_sources,
        external_eee_sources=external_eee_sources,
        output_root=output_root,
    )


def load_manifest(fpath: str | Path) -> VirtualExperimentManifest:
    """Load and validate a virtual-experiment manifest from disk."""
    path = Path(fpath).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    data = yaml.safe_load(path.read_text())
    return parse_manifest(data)
