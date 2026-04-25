"""Normalized run model.

Identifies and holds one evaluation run in the EEE-shape representation,
independent of the underlying artifact format (raw HELM, converted EEE, or
future formats). All fields exist to serve the comparison/report layer; raw
artifacts remain on disk for evidence drilldown via :class:`Origin`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # heavy schema import only when type-checking
    from every_eval_ever.eval_types import EvaluationLog
    from every_eval_ever.instance_level_types import InstanceLevelEvaluationLog


class SourceKind(str, enum.Enum):
    """Whether a run is the published reference or a local reproduction."""

    OFFICIAL = "official"
    LOCAL = "local"


class ArtifactFormat(str, enum.Enum):
    """The on-disk shape of the artifacts backing this run.

    * ``helm``: raw HELM JSON tree (``run_spec.json``, ``scenario_state.json``,
      ``stats.json``, ``per_instance_stats.json``). Loaded via in-memory
      conversion to EEE.
    * ``eee``: a converted Every Eval Ever artifact tree (``*.json`` aggregate
      plus ``*_samples.jsonl`` instance-level records).
    """

    HELM = "helm"
    EEE = "eee"


@dataclass(frozen=True)
class Origin:
    """Provenance back to raw evidence.

    ``helm_run_path`` is the canonical raw HELM run directory when one
    exists; for converted EEE artifacts derived from a HELM run it is the
    pre-conversion source path. Reports surface this so a human can drill
    from a derived comparison back to the underlying run directory.
    """

    helm_run_path: Path | None = None
    eee_artifact_path: Path | None = None
    converter_name: str | None = None
    converter_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "helm_run_path": str(self.helm_run_path) if self.helm_run_path else None,
            "eee_artifact_path": str(self.eee_artifact_path) if self.eee_artifact_path else None,
            "converter_name": self.converter_name,
            "converter_version": self.converter_version,
        }


@dataclass(frozen=True)
class NormalizedRunRef:
    """Addressable identity of one normalized run.

    A ref names *what* to load. Loading is performed by
    :func:`helm_audit.normalized.loaders.load_run`, which dispatches on
    ``artifact_format``.
    """

    source_kind: SourceKind
    artifact_format: ArtifactFormat
    artifact_path: Path
    """Where the artifacts to load actually live. For ``HELM`` this is the
    HELM run directory; for ``EEE`` this is the converted artifact directory
    (the one containing the ``<eval_name>/<org>/<model>/`` subtree)."""

    origin: Origin = field(default_factory=Origin)
    """Pointer back to the canonical raw HELM run, when known."""

    component_id: str | None = None
    """Optional planner-assigned component id for traceability."""

    logical_run_key: str | None = None
    """The HELM ``run_spec.name`` (or equivalent) — independent of attempt."""

    display_name: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)
    """Loader-specific options or hints (e.g. ``source_organization_name``
    for in-memory HELM→EEE conversion)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind.value,
            "artifact_format": self.artifact_format.value,
            "artifact_path": str(self.artifact_path),
            "origin": self.origin.to_dict(),
            "component_id": self.component_id,
            "logical_run_key": self.logical_run_key,
            "display_name": self.display_name,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_helm_run(
        cls,
        run_path: str | Path,
        *,
        source_kind: SourceKind,
        component_id: str | None = None,
        logical_run_key: str | None = None,
        display_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "NormalizedRunRef":
        """Build a ref over a raw HELM run directory."""
        run_path = Path(run_path)
        return cls(
            source_kind=source_kind,
            artifact_format=ArtifactFormat.HELM,
            artifact_path=run_path,
            origin=Origin(helm_run_path=run_path),
            component_id=component_id,
            logical_run_key=logical_run_key,
            display_name=display_name,
            extra=dict(extra or {}),
        )

    @classmethod
    def from_eee_artifact(
        cls,
        artifact_path: str | Path,
        *,
        source_kind: SourceKind,
        helm_run_path: str | Path | None = None,
        component_id: str | None = None,
        logical_run_key: str | None = None,
        display_name: str | None = None,
        converter_name: str | None = "every_eval_ever.helm",
        converter_version: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "NormalizedRunRef":
        """Build a ref over a converted EEE artifact directory."""
        artifact_path = Path(artifact_path)
        return cls(
            source_kind=source_kind,
            artifact_format=ArtifactFormat.EEE,
            artifact_path=artifact_path,
            origin=Origin(
                helm_run_path=Path(helm_run_path) if helm_run_path else None,
                eee_artifact_path=artifact_path,
                converter_name=converter_name,
                converter_version=converter_version,
            ),
            component_id=component_id,
            logical_run_key=logical_run_key,
            display_name=display_name,
            extra=dict(extra or {}),
        )


@dataclass(frozen=True)
class InstanceRecord:
    """One instance-level result.

    Wraps an :class:`InstanceLevelEvaluationLog` with optional ``metric_id``
    and ``metric_kind`` shortcut fields so the comparison layer can join by
    ``(sample_id, metric_id)`` without re-reading the embedded EEE record on
    every hot path.
    """

    sample_id: str
    sample_hash: str | None
    metric_id: str | None
    metric_kind: str | None
    score: float
    is_correct: bool | None
    record: "InstanceLevelEvaluationLog"

    @property
    def join_key(self) -> tuple[str, str | None]:
        """Stable join key across runs being compared."""
        # sample_hash is preferred when available because sample_id can drift
        # between HELM versions; fall back to sample_id otherwise.
        return (self.sample_hash or self.sample_id, self.metric_id)


@dataclass(frozen=True)
class NormalizedRun:
    """In-memory normalized view of one evaluation run.

    Holds the EEE aggregate (run-level metric scores) plus per-instance
    records. The :attr:`raw_helm` slot is reserved for legacy code paths
    that still need access to raw HELM JSONs (e.g. ``run_spec.json`` for
    semantic diff) — new code should not depend on it.
    """

    ref: NormalizedRunRef
    evaluation_log: "EvaluationLog"
    instances: list[InstanceRecord] = field(default_factory=list)
    raw_helm: dict[str, Any] | None = None
    """Optional raw HELM JSONs keyed by stem (``run_spec``, ``scenario_state``,
    ``stats``, ``per_instance_stats``). Populated by HELM-format loaders.
    None for pure EEE loads."""

    @property
    def source_kind(self) -> SourceKind:
        return self.ref.source_kind

    @property
    def artifact_format(self) -> ArtifactFormat:
        return self.ref.artifact_format

    @property
    def model_id(self) -> str:
        return self.evaluation_log.model_info.id

    @property
    def evaluation_name_hint(self) -> str | None:
        results = self.evaluation_log.evaluation_results or []
        return results[0].evaluation_name if results else None

    def metrics_by_id(self) -> dict[str, dict[str, Any]]:
        """Map ``metric_id`` (or ``metric_name`` fallback) → run-level score view."""
        out: dict[str, dict[str, Any]] = {}
        for er in self.evaluation_log.evaluation_results or []:
            cfg = er.metric_config
            mid = cfg.metric_id or cfg.metric_name or er.evaluation_name
            out[mid] = {
                "metric_id": cfg.metric_id,
                "metric_name": cfg.metric_name,
                "metric_kind": cfg.metric_kind,
                "evaluation_name": er.evaluation_name,
                "score": er.score_details.score,
                "uncertainty": (
                    er.score_details.uncertainty.model_dump()
                    if er.score_details.uncertainty is not None
                    else None
                ),
            }
        return out
