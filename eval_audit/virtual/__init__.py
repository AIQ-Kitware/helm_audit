"""Virtual experiments: declarative slices over existing indexed runs.

A *virtual experiment* is a YAML-defined composition that pulls rows
from existing local audit + public HELM indexes, augments them with
externally-produced EEE artifacts (e.g. Inspect AI) or whole EEE
trees, and feeds the combined slice through the existing planner /
report pipeline. Output lands outside the repo, under an explicit
``output.root`` declared by the manifest.

Source kinds:

- ``audit_index`` — slice of a local audit-results CSV (HELM-driven).
- ``official_public_index`` — slice of an official public-HELM CSV.
- ``eee_root`` — walk an ``official/``+``local/`` EEE tree (the same
  shape ``eval-audit-from-eee`` consumes) and synthesize index rows.
- ``external_eee`` — cherry-pick one or more individual EEE artifacts
  by absolute path with explicit ``run_entry`` pinning.
"""

from eval_audit.virtual.manifest import (
    AuditIndexSource,
    EeeRootSource,
    ExternalEeeComponent,
    ExternalEeeSource,
    HelmStage1PreFilter,
    OfficialPublicIndexSource,
    ScopeFilter,
    VirtualExperimentManifest,
    load_manifest,
)
from eval_audit.virtual.compose import (
    ComposeResult,
    compose_virtual_experiment,
    write_synthesized_indexes,
)

__all__ = [
    "AuditIndexSource",
    "ComposeResult",
    "EeeRootSource",
    "ExternalEeeComponent",
    "ExternalEeeSource",
    "HelmStage1PreFilter",
    "OfficialPublicIndexSource",
    "ScopeFilter",
    "VirtualExperimentManifest",
    "compose_virtual_experiment",
    "load_manifest",
    "write_synthesized_indexes",
]
