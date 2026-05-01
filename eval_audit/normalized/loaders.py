"""Loader registry and concrete loaders for the normalized layer.

Loaders convert artifacts on disk into :class:`NormalizedRun` instances.
They are registered against an :class:`ArtifactFormat`, and dispatched by
:func:`load_run`.

Two loaders ship in Stage 2:

* :class:`EeeArtifactLoader` reads converted EEE artifact directories
  produced by ``every_eval_ever convert helm`` (or another converter that
  emits the same shape).
* :class:`HelmRawLoader` reads raw HELM run directories and converts to EEE
  in-memory using the ``every_eval_ever.converters.helm.HELMAdapter``.

Both loaders preserve the :class:`Origin` so downstream reports can drill
back to the canonical raw evidence.
"""

from __future__ import annotations

import abc
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any, Callable

from eval_audit.normalized.model import (
    ArtifactFormat,
    InstanceRecord,
    NormalizedRun,
    NormalizedRunRef,
    Origin,
)

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func

# Concrete loaders are registered by ArtifactFormat.
_REGISTRY: dict[ArtifactFormat, "Loader"] = {}


class LoaderError(RuntimeError):
    """Raised when a loader cannot produce a normalized run."""


class Loader(abc.ABC):
    """Abstract loader: ref → :class:`NormalizedRun`."""

    artifact_format: ArtifactFormat

    @abc.abstractmethod
    def load(self, ref: NormalizedRunRef) -> NormalizedRun:
        ...


def register_loader(loader: Loader) -> None:
    _REGISTRY[loader.artifact_format] = loader


def get_loader(artifact_format: ArtifactFormat) -> Loader:
    try:
        return _REGISTRY[artifact_format]
    except KeyError as exc:
        raise LoaderError(
            f"No loader registered for artifact_format={artifact_format!r}"
        ) from exc


@profile
def load_run(ref: NormalizedRunRef) -> NormalizedRun:
    """Load a normalized run, dispatching on ``ref.artifact_format``."""
    return get_loader(ref.artifact_format).load(ref)


# ---------------------------------------------------------------------------
# EEE artifact loader
# ---------------------------------------------------------------------------

class EeeArtifactLoader(Loader):
    """Read a converted EEE artifact directory.

    Layout produced by ``every_eval_ever convert helm``:

    .. code-block::

        <artifact_path>/
          <evaluation_name>/<org>/<model>/
            <uuid>.json              # EvaluationLog (aggregate / run-level)
            <uuid>_samples.jsonl     # InstanceLevelEvaluationLog records

    A single artifact_path may contain multiple ``<uuid>.json`` aggregates if
    the converter discovered multiple HELM runs under the same source dir.
    The loader merges them into one :class:`NormalizedRun` keyed by
    ``ref.logical_run_key`` when present; otherwise it picks the most-recent
    aggregate by ``retrieved_timestamp``.
    """

    artifact_format = ArtifactFormat.EEE

    @profile
    def load(self, ref: NormalizedRunRef) -> NormalizedRun:
        from every_eval_ever.eval_types import EvaluationLog
        from every_eval_ever.instance_level_types import InstanceLevelEvaluationLog

        if ref.artifact_format is not ArtifactFormat.EEE:
            raise LoaderError(f"EeeArtifactLoader cannot load {ref.artifact_format!r}")

        artifact_path = Path(ref.artifact_path)
        if not artifact_path.exists():
            raise LoaderError(f"EEE artifact path does not exist: {artifact_path}")

        aggregate_paths = sorted(
            p for p in artifact_path.rglob("*.json") if not p.name.endswith("_samples.json")
        )
        # Filter out the *_samples.jsonl we may have caught and any
        # non-EvaluationLog files (provenance.json, status.json, etc.).
        aggregate_paths = [
            p for p in aggregate_paths
            if p.name not in {"provenance.json", "status.json"}
        ]
        if not aggregate_paths:
            raise LoaderError(
                f"No EEE aggregate JSON files found under {artifact_path}"
            )

        candidates: list[tuple[EvaluationLog, Path]] = []
        for p in aggregate_paths:
            try:
                log = EvaluationLog.model_validate_json(p.read_text())
            except Exception:
                continue
            candidates.append((log, p))

        if not candidates:
            raise LoaderError(
                f"None of the JSON files under {artifact_path} parsed as EvaluationLog"
            )

        if ref.logical_run_key:
            named = [
                (log, p) for (log, p) in candidates
                if any(
                    er.evaluation_name == ref.logical_run_key
                    or ref.logical_run_key.startswith(er.evaluation_name + ":")
                    for er in (log.evaluation_results or [])
                )
            ]
            if named:
                candidates = named

        # Pick the newest by retrieved_timestamp when multiple candidates remain.
        candidates.sort(
            key=lambda lp: float(lp[0].retrieved_timestamp or 0),
            reverse=True,
        )
        chosen_log, chosen_path = candidates[0]

        # Locate the matching samples.jsonl, if any.
        samples_path = chosen_path.with_name(chosen_path.stem + "_samples.jsonl")
        instances: list[InstanceRecord] = []
        if samples_path.exists():
            for line in samples_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = InstanceLevelEvaluationLog.model_validate_json(line)
                except Exception:
                    continue
                instances.append(_instance_record_from_eee(rec))

        # HELM-origin EEE artifacts use the EEE aggregate as the run-level
        # source, but report drilldown still needs stable HELM sample ids.
        # Older conversions lacked metric ids; newer conversions may carry
        # metric rows with sample hashes that do not join across separately
        # converted public/local artifacts. Use raw HELM per_instance_stats
        # whenever provenance is available so official/local instance joins
        # remain comparable and drill back to the original evidence.
        #
        # *** PAPER VALIDITY GUARD ***
        # The block below silently overwrites the EEE-derived instances with
        # HELM-derived instances when the HELM run dir is also on disk. For
        # the EEE-only paper claim, this fallback must be off so the
        # analysis honestly uses *only* EEE data. Set
        # EVAL_AUDIT_EEE_STRICT={1,true,yes} (or pass the flag at the entry
        # point that propagates here) to skip the HELM fallback. With it
        # set, instance joins that depend on stable HELM sample ids
        # gracefully fail to ``join_failed`` cells in the heatmap — which
        # is the *honest* EEE-only signal. See
        # docs/eee-only-hard-split-todo.md for the full architectural fix
        # (lifting the recipe facts into the EEE schema so the fallback
        # is never needed).
        _eee_strict = os.environ.get(
            "EVAL_AUDIT_EEE_STRICT", ""
        ).strip().lower() in {"1", "true", "yes"}
        if not _eee_strict and ref.origin.helm_run_path is not None:
            raw_instances = _instances_from_raw_helm(ref.origin.helm_run_path, chosen_log)
            if raw_instances:
                instances = raw_instances

        # Augment ref.origin with the actual chosen artifact path.
        new_origin = Origin(
            helm_run_path=ref.origin.helm_run_path,
            eee_artifact_path=chosen_path,
            converter_name=ref.origin.converter_name or _eee_converter_name(),
            converter_version=ref.origin.converter_version or _eee_converter_version(),
        )
        new_ref = NormalizedRunRef(
            source_kind=ref.source_kind,
            artifact_format=ref.artifact_format,
            artifact_path=ref.artifact_path,
            origin=new_origin,
            component_id=ref.component_id,
            logical_run_key=ref.logical_run_key,
            display_name=ref.display_name,
            extra=ref.extra,
        )
        return NormalizedRun(
            ref=new_ref,
            evaluation_log=chosen_log,
            instances=instances,
            raw_helm=None,
        )


@profile
def _instance_record_from_eee(rec) -> InstanceRecord:
    """Project an InstanceLevelEvaluationLog into the comparison-friendly shape."""
    return InstanceRecord(
        sample_id=rec.sample_id,
        sample_hash=rec.sample_hash,
        # EEE per-instance records currently carry one score per (sample,
        # metric) via ``evaluation_result_id``. When present we use it as the
        # metric handle; otherwise we use ``evaluation_name`` so each sample
        # is at least tagged with a stable per-eval identifier.
        metric_id=rec.evaluation_result_id or rec.evaluation_name,
        metric_kind=None,
        score=float(rec.evaluation.score),
        is_correct=rec.evaluation.is_correct,
        record=rec,
    )


# ---------------------------------------------------------------------------
# Raw-HELM loader (in-memory conversion)
# ---------------------------------------------------------------------------

class HelmRawLoader(Loader):
    """Load a raw HELM run directory by converting to EEE in-memory.

    This loader is the fallback for runs we have not yet (or cannot) convert
    to canonical EEE artifacts on disk. It uses
    :class:`every_eval_ever.converters.helm.adapter.HELMAdapter` directly so
    no subprocess or filesystem write is required.

    Raw HELM JSONs (``run_spec``, ``scenario_state``, ``stats``,
    ``per_instance_stats``) are also exposed via :attr:`NormalizedRun.raw_helm`
    so any legacy comparison code that still needs them during migration can
    reach them without re-reading the disk.
    """

    artifact_format = ArtifactFormat.HELM

    REQUIRED_FILES = (
        "run_spec.json",
        "scenario_state.json",
        "stats.json",
        "per_instance_stats.json",
    )

    @profile
    def load(self, ref: NormalizedRunRef) -> NormalizedRun:
        if ref.artifact_format is not ArtifactFormat.HELM:
            raise LoaderError(f"HelmRawLoader cannot load {ref.artifact_format!r}")

        run_path = Path(ref.artifact_path)
        if not run_path.is_dir():
            raise LoaderError(f"HELM run path is not a directory: {run_path}")
        missing = [n for n in self.REQUIRED_FILES if not (run_path / n).exists()]
        if missing:
            raise LoaderError(
                f"HELM run {run_path} is missing required files: {missing}"
            )

        # Delegate to the content-addressed cache. On hit, read directly via
        # :class:`EeeArtifactLoader`; on miss, run the HELM->EEE conversion
        # once into the cache (atomic per file via :mod:`safer`) and load the
        # cached artifact. This replaces the historical "convert into a
        # /tmp dir and discard" pattern that re-ran the converter on every
        # call.
        from eval_audit.normalized.eee_artifacts import (
            _artifact_has_aggregate,
            convert_helm_run_to_cached_eee,
            helm_raw_cache_parent,
        )

        cache_parent = helm_raw_cache_parent(run_path)
        cache_artifact = cache_parent / "eee_output"
        if not _artifact_has_aggregate(cache_artifact):
            resolution = convert_helm_run_to_cached_eee(
                run_path,
                source_kind=ref.source_kind.value if hasattr(ref.source_kind, "value") else str(ref.source_kind),
                source_organization_name=ref.extra.get("source_organization_name", "eval_audit_helm_raw"),
                eval_library_name=ref.extra.get("eval_library_name", "HELM"),
                eval_library_version=ref.extra.get("eval_library_version", "unknown"),
                evaluator_relationship=ref.extra.get("evaluator_relationship", "third_party"),
            )
            if resolution.artifact_path is None:
                raise LoaderError(
                    f"HELM->EEE conversion failed for {run_path}: "
                    f"status={resolution.status} message={resolution.message}"
                )
            cache_artifact = resolution.artifact_path

        eee_ref = NormalizedRunRef(
            source_kind=ref.source_kind,
            artifact_format=ArtifactFormat.EEE,
            artifact_path=cache_artifact,
            origin=Origin(
                helm_run_path=run_path,
                eee_artifact_path=cache_artifact,
                converter_name=_eee_converter_name(),
                converter_version=_eee_converter_version(),
            ),
            component_id=ref.component_id,
            logical_run_key=ref.logical_run_key,
            display_name=ref.display_name,
            extra=ref.extra,
        )
        run = get_loader(ArtifactFormat.EEE).load(eee_ref)
        # Preserve the original ref's HELM artifact path / format so callers
        # that introspect ``run.ref`` see the same identity they passed in.
        new_ref = NormalizedRunRef(
            source_kind=ref.source_kind,
            artifact_format=ref.artifact_format,
            artifact_path=ref.artifact_path,
            origin=Origin(
                helm_run_path=run_path,
                eee_artifact_path=cache_artifact,
                converter_name=run.ref.origin.converter_name,
                converter_version=run.ref.origin.converter_version,
            ),
            component_id=ref.component_id,
            logical_run_key=ref.logical_run_key,
            display_name=ref.display_name,
            extra=ref.extra,
        )
        raw_helm = _read_raw_helm_jsons(run_path)
        return NormalizedRun(
            ref=new_ref,
            evaluation_log=run.evaluation_log,
            instances=run.instances,
            raw_helm=raw_helm,
        )


@profile
def _read_raw_helm_jsons(run_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for stem in ("run_spec", "scenario_state", "stats", "per_instance_stats", "scenario"):
        fpath = run_path / f"{stem}.json"
        if fpath.exists():
            try:
                out[stem] = json.loads(fpath.read_text())
            except Exception:
                out[stem] = None
    return out


@profile
def _instances_from_raw_helm(run_path: Path, evaluation_log) -> list[InstanceRecord]:
    """Lift HELM ``per_instance_stats.json`` rows into :class:`InstanceRecord`.

    One :class:`InstanceRecord` per (instance_id, metric) so per-metric
    agreement curves stay computable. The ``record`` slot is populated with
    a minimal :class:`InstanceLevelEvaluationLog` so the comparison layer can
    reach the EEE schema if it needs to (input.raw, etc.).
    """
    from every_eval_ever.instance_level_types import (
        AnswerAttributionItem,
        Evaluation,
        Input,
        InstanceLevelEvaluationLog,
        InteractionType,
        Output,
    )

    per_instance_path = run_path / "per_instance_stats.json"
    scenario_state_path = run_path / "scenario_state.json"
    if not per_instance_path.exists():
        return []
    try:
        per_instance = json.loads(per_instance_path.read_text())
    except Exception:
        return []
    request_states_by_id: dict[str, dict[str, Any]] = {}
    if scenario_state_path.exists():
        try:
            scenario_state = json.loads(scenario_state_path.read_text())
        except Exception:
            scenario_state = {}
        for rs in scenario_state.get("request_states") or []:
            inst = rs.get("instance") or {}
            iid = inst.get("id")
            if iid is None:
                continue
            # Keep first occurrence per id to avoid stomping perturbed variants
            request_states_by_id.setdefault(str(iid), rs)

    eval_id = evaluation_log.evaluation_id
    model_id = evaluation_log.model_info.id
    eval_name = (
        evaluation_log.evaluation_results[0].evaluation_name
        if evaluation_log.evaluation_results
        else "unknown"
    )

    records: list[InstanceRecord] = []
    for bundle in per_instance:
        iid = bundle.get("instance_id")
        if iid is None:
            continue
        rs = request_states_by_id.get(str(iid), {})
        inst = rs.get("instance") or {}
        prompt = (rs.get("request") or {}).get("prompt") or inst.get("input", {}).get("text", "")
        completions = (rs.get("result") or {}).get("completions") or []
        completion_texts = [c.get("text", "") for c in completions]
        refs = [
            r.get("output", {}).get("text", "")
            for r in inst.get("references") or []
            if "correct" in (r.get("tags") or [])
        ]

        for stat in bundle.get("stats") or []:
            name_obj = stat.get("name") or {}
            metric_name = name_obj.get("name")
            if metric_name is None:
                continue
            mean = stat.get("mean")
            if mean is None:
                continue
            try:
                score = float(mean)
            except (TypeError, ValueError):
                continue
            try:
                rec = InstanceLevelEvaluationLog(
                    schema_version="0.2.2",
                    evaluation_id=eval_id,
                    model_id=model_id,
                    evaluation_name=eval_name,
                    evaluation_result_id=metric_name,
                    sample_id=str(iid),
                    sample_hash=None,
                    interaction_type=InteractionType.single_turn,
                    input=Input(raw=prompt or "", reference=refs),
                    output=Output(raw=completion_texts or [""]),
                    answer_attribution=[
                        AnswerAttributionItem(
                            turn_idx=0,
                            source="output.raw",
                            extracted_value=(completion_texts[0] if completion_texts else ""),
                            extraction_method="raw",
                            is_terminal=True,
                        )
                    ],
                    evaluation=Evaluation(score=score, is_correct=score > 0.5),
                )
            except Exception:
                # If the EEE schema rejects this record (e.g. empty refs +
                # validator), fall back to skipping the per-instance schema
                # construction; the score still flows through InstanceRecord.
                rec = None
            records.append(
                InstanceRecord(
                    sample_id=str(iid),
                    sample_hash=None,
                    metric_id=metric_name,
                    metric_kind=None,
                    score=score,
                    is_correct=score > 0.5,
                    record=rec,
                )
            )
    return records


def _eee_converter_name() -> str:
    return "every_eval_ever.converters.helm"


def _eee_converter_version() -> str | None:
    try:
        return importlib.metadata.version("every_eval_ever")
    except Exception:
        return None


# Register defaults at import time so callers can use ``load_run`` directly.
register_loader(EeeArtifactLoader())
register_loader(HelmRawLoader())


__all__ = [
    "EeeArtifactLoader",
    "HelmRawLoader",
    "Loader",
    "LoaderError",
    "get_loader",
    "load_run",
    "register_loader",
]
