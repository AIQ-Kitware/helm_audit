"""eval_audit.normalized — the EEE-centered normalized layer.

This package is the boundary between raw evaluation artifacts (HELM JSON, EEE
JSON, future formats) and the comparison/report core. Code outside this
package should treat runs as :class:`NormalizedRun` instances, never as raw
HELM directories.

Design (see ``dev/analysis/eee_refactor_stage1_map.md``):

* :class:`SourceKind` distinguishes ``official`` (public reference) from
  ``local`` (locally executed audit) runs.
* :class:`ArtifactFormat` distinguishes ``helm`` (raw HELM JSON tree) from
  ``eee`` (a converted Every Eval Ever artifact tree).
* :class:`NormalizedRunRef` is the addressable identity of one run: where
  the artifacts live, what kind of source they are, what format, and a
  stable origin path back to the underlying HELM run when applicable.
* :class:`NormalizedRun` is the loaded, in-memory view of one run, holding
  the EEE-shape :class:`EvaluationLog` aggregate plus the list of
  :class:`InstanceLevelEvaluationLog` records for that run. A :class:`Origin`
  sub-record preserves the path back to the raw HELM artifacts (for
  evidence drilldown).
* :class:`Loader` is the abstract loader interface. Concrete loaders live
  in :mod:`eval_audit.normalized.loaders` and are registered against an
  ``ArtifactFormat`` value.

Stage 2 only introduces these abstractions and validates them with small
targeted smoke tests. Stages 3+ migrate call sites onto them.
"""

from __future__ import annotations

from eval_audit.normalized.model import (
    ArtifactFormat,
    InstanceRecord,
    NormalizedRun,
    NormalizedRunRef,
    Origin,
    SourceKind,
)
from eval_audit.normalized.loaders import (
    Loader,
    LoaderError,
    get_loader,
    load_run,
    register_loader,
)
from eval_audit.normalized.joins import (
    index_instances,
    join_instances,
    join_run_level,
    joined_metric_means,
)

__all__ = [
    "ArtifactFormat",
    "InstanceRecord",
    "Loader",
    "LoaderError",
    "NormalizedRun",
    "NormalizedRunRef",
    "Origin",
    "SourceKind",
    "get_loader",
    "index_instances",
    "join_instances",
    "join_run_level",
    "joined_metric_means",
    "load_run",
    "register_loader",
]
