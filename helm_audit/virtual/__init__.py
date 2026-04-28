"""Virtual experiments: declarative slices over existing indexed runs.

A *virtual experiment* is a YAML-defined composition that pulls rows from
existing local audit + public HELM indexes, optionally augments them with
externally-produced EEE artifacts (e.g. Inspect AI), and feeds the
combined slice through the existing planner / report pipeline. Output
lands outside the repo, under an explicit ``output.root`` declared by
the manifest.

The first iteration accepts ``external_eee`` components in the schema but
does not yet plumb them into the planner; they are recorded in the
provenance file so a later pass can wire them in without changing the
manifest format.
"""

from helm_audit.virtual.manifest import (
    AuditIndexSource,
    ExternalEeeComponent,
    ExternalEeeSource,
    HelmStage1PreFilter,
    OfficialPublicIndexSource,
    ScopeFilter,
    VirtualExperimentManifest,
    load_manifest,
)
from helm_audit.virtual.compose import (
    ComposeResult,
    compose_virtual_experiment,
    write_synthesized_indexes,
)

__all__ = [
    "AuditIndexSource",
    "ComposeResult",
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
