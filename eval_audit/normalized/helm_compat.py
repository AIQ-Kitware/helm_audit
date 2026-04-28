"""Compatibility adapters that let legacy HELM-shape consumers use NormalizedRun.

Stage-3 scope: the existing :class:`eval_audit.helm.diff.HelmRunDiff` and
:class:`eval_audit.helm.analysis.HelmRunAnalysis` consume objects that look
like ``compat.helm_outputs.HelmRun`` — i.e. they expose ``.json.run_spec()``,
``.json.scenario()``, ``.json.scenario_state()``, ``.json.stats()``,
``.json.per_instance_stats()`` and a ``.path``. This module provides a tiny
adapter so a :class:`NormalizedRun` (which now sits at the boundary between
on-disk artifacts and comparison logic) can be passed straight into
``HelmRunDiff`` while the larger refactor is in flight.

The adapter prefers the in-memory raw HELM JSONs cached on the
:class:`NormalizedRun`, and falls back to a lazy filesystem read via
``Origin.helm_run_path`` when needed. Calling code that holds an EEE-only
run with no HELM origin gets a clear error rather than silent fallthrough.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval_audit.normalized.model import (
    ArtifactFormat,
    NormalizedRun,
    NormalizedRunRef,
    SourceKind,
)


class _NormalizedJsonView:
    """Mimics ``compat.helm_outputs._JsonRunView`` for legacy consumers."""

    _KEYS = ("run_spec", "scenario", "scenario_state", "stats", "per_instance_stats")

    def __init__(self, run: NormalizedRun):
        self._run = run

    def _load(self, name: str) -> Any:
        if name not in self._KEYS:
            raise KeyError(f"Unknown HELM JSON view {name!r}")
        cached = (self._run.raw_helm or {}).get(name)
        if cached is not None:
            return cached
        helm_path = self._run.ref.origin.helm_run_path
        if helm_path is None:
            raise FileNotFoundError(
                f"NormalizedRun has no raw HELM origin to satisfy {name}.json; "
                "load via HelmRawLoader or expose the HELM run via Origin.helm_run_path."
            )
        return json.loads(Path(helm_path, f"{name}.json").read_text())

    def run_spec(self) -> dict[str, Any]:
        return self._load("run_spec")

    def scenario(self) -> dict[str, Any]:
        return self._load("scenario")

    def scenario_state(self) -> dict[str, Any]:
        return self._load("scenario_state")

    def stats(self) -> list[dict[str, Any]]:
        return self._load("stats")

    def per_instance_stats(self) -> list[dict[str, Any]]:
        return self._load("per_instance_stats")


class HelmRunView:
    """Adapter that exposes a :class:`NormalizedRun` as a HELM-shape reader.

    The legacy comparison core reads ``.path``, ``.name``, ``.json.X()`` and
    (less commonly) ``.msgspec.X()``. We map all of these onto the
    in-memory cache, falling back to disk via the run's :class:`Origin`.
    """

    def __init__(self, run: NormalizedRun):
        self.normalized = run
        helm_path = run.ref.origin.helm_run_path
        # ``.path`` is used for naming and per-run diagnostics across the
        # legacy code; if the normalized run has no HELM origin, the
        # artifact_path is the next-best human anchor.
        self.path = Path(helm_path) if helm_path else Path(run.ref.artifact_path)
        self.name = self.path.name
        self.json = _NormalizedJsonView(run)
        # Most call sites only touch ``.json``; alias ``.msgspec`` to it so
        # legacy paths that happened to use the alternative reader still
        # resolve. Both views ultimately serve the same JSON dicts.
        self.msgspec = self.json


def helm_view(run: NormalizedRun) -> HelmRunView:
    """Wrap a :class:`NormalizedRun` in a HELM-compatible reader."""
    return HelmRunView(run)


def helm_view_from_path(
    run_path: str | Path,
    *,
    source_kind: SourceKind | str = SourceKind.OFFICIAL,
    component_id: str | None = None,
    logical_run_key: str | None = None,
    display_name: str | None = None,
) -> HelmRunView:
    """Cheap legacy bridge: HELM run dir → HELM-shape view via the normalized layer.

    Routes through :class:`NormalizedRunRef` and an :class:`Origin` so the
    legacy comparison core sees the *same* identity surface as the new EEE
    code paths, without paying for the in-memory HELM→EEE conversion. This
    is the Stage-3 seam: no comparison logic moves, but every legacy reader
    construction now flows through the normalized boundary.

    Stage 4 replaces the comparison core itself; this helper goes away then.
    """
    if not isinstance(source_kind, SourceKind):
        source_kind = SourceKind(str(source_kind))
    run_path = Path(run_path)
    ref = NormalizedRunRef.from_helm_run(
        run_path,
        source_kind=source_kind,
        component_id=component_id,
        logical_run_key=logical_run_key,
        display_name=display_name,
    )
    # Note: NormalizedRun normally requires an EvaluationLog; for the
    # raw-only legacy bridge we deliberately leave that slot None and
    # populate ``raw_helm`` lazily through _NormalizedJsonView's filesystem
    # fallback (Origin.helm_run_path). This avoids running the EEE
    # converter just to read run_spec.json. The bridge exists only while
    # the comparison core is still HELM-shaped.
    run = NormalizedRun.__new__(NormalizedRun)
    object.__setattr__(run, "ref", ref)
    object.__setattr__(run, "evaluation_log", None)
    object.__setattr__(run, "instances", [])
    object.__setattr__(run, "raw_helm", None)
    return HelmRunView(run)


__all__ = ["HelmRunView", "helm_view", "helm_view_from_path"]
