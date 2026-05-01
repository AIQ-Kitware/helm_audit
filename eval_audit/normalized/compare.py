"""Pairwise comparison core operating on the EEE-normalized representation.

Stage-4 boundary: the agreement/quantile measurements that drive
``reports/core_metrics.py`` move off ``HelmRunDiff`` and onto pure
:class:`NormalizedRun` inputs. The legacy ``HelmRunDiff`` is still used for
the run-spec-semantic diagnosis (which reads ``run_spec.json`` directly via
the raw HELM JSONs cached on the normalized run); only the *measurement*
core moves here.

Why this split is safe:

* ``run_spec.json`` is part of the raw HELM evidence and stays on disk
  read-only. The normalized run's ``Origin.helm_run_path`` plus the cached
  ``raw_helm`` slot give the legacy diagnosis everything it needs without
  depending on HELM-shaped joined-instance tables.
* The measurement core, by contrast, reads per-(sample, metric) data
  that the normalized layer already exposes through :class:`InstanceRecord`.
  Moving it here removes the HELM-specific
  ``JoinedInstanceStatTable`` from the per-pair hot path.
"""

from __future__ import annotations

from typing import Any, Iterable

from eval_audit.helm import metrics as helm_metrics
from eval_audit.normalized.joins import (
    index_instances,
    join_instances,
    joined_metric_means,
)
from eval_audit.normalized.model import InstanceRecord, NormalizedRun

# Zero-overhead in normal runs; line_profiler swaps in a real profiler when
# the LINE_PROFILE env var is set.
try:
    from line_profiler import profile  # type: ignore[import-not-found]
except ImportError:
    def profile(func):  # type: ignore[no-redef]
        return func


# ---------------------------------------------------------------------------
# Run-level + instance-level core rows
# ---------------------------------------------------------------------------

@profile
def run_level_core_rows(
    run_a: NormalizedRun,
    run_b: NormalizedRun,
    *,
    metric_class: str = "core",
) -> list[dict[str, Any]]:
    """Run-level metric-mean rows for the requested metric class.

    ``metric_class`` defaults to ``core`` to match the historical behavior
    of :func:`eval_audit.reports.core_metrics._run_level_core_rows`.
    """
    means_a = joined_metric_means(run_a)
    means_b = joined_metric_means(run_b)
    rows: list[dict[str, Any]] = []
    for key in sorted(set(means_a) & set(means_b)):
        a_val = means_a[key]
        b_val = means_b[key]
        # Preserve filtering by metric class so the output set matches what
        # the legacy core_metrics path produced.
        cls, _ = helm_metrics.classify_metric(key)
        if cls != metric_class:
            continue
        abs_delta = abs(a_val - b_val)
        denom = max(abs(a_val), abs(b_val), 1e-12)
        rows.append({
            "key": key,
            "metric": key,
            "metric_class": cls,
            "a": float(a_val),
            "b": float(b_val),
            "abs_delta": abs_delta,
            "rel_delta": abs_delta / denom,
        })
    return rows


@profile
def instance_level_core_rows(
    run_a: NormalizedRun,
    run_b: NormalizedRun,
    *,
    metric_class: str = "core",
) -> list[dict[str, Any]]:
    """Per-(sample, metric) rows for matching instances and core metrics."""
    rows: list[dict[str, Any]] = []
    classify = helm_metrics.classify_metric
    for key, rec_a, rec_b in join_instances(run_a, run_b):
        # join_instances pairs records by (sample_hash_or_id, metric_id),
        # so rec_a.metric_id == rec_b.metric_id whenever a key materialized.
        # That means a single classify_metric call covers both sides; the
        # previous code did it twice per row × ~1.4M rows = wasted work.
        # The metric_id-is-None branch is also unreachable post-join (the
        # key would have a None component) but kept defensively.
        metric_id = rec_a.metric_id
        if metric_id is None or rec_b.metric_id is None:
            continue
        cls, _ = classify(metric_id)
        if cls != metric_class:
            continue
        a_val = rec_a.score
        b_val = rec_b.score
        abs_delta = abs(a_val - b_val)
        denom = max(abs(a_val), abs(b_val), 1e-12)
        rows.append({
            "key": key,
            "sample_id": rec_a.sample_id,
            "sample_hash": rec_a.sample_hash or rec_b.sample_hash,
            "metric": metric_id,
            "metric_class": cls,
            "a": a_val,
            "b": b_val,
            "abs_delta": abs_delta,
            "rel_delta": abs_delta / denom,
        })
    return rows


# ---------------------------------------------------------------------------
# Single-run instance views (used by report tables and overlay plots)
# ---------------------------------------------------------------------------

def instance_core_score_records(
    run: NormalizedRun,
    *,
    metric_class: str = "core",
) -> list[dict[str, Any]]:
    """Yield ``{run_label, metric, sample_id, value}`` rows for plotting."""
    out: list[dict[str, Any]] = []
    for rec in run.instances:
        if rec.metric_id is None:
            continue
        cls, _ = helm_metrics.classify_metric(rec.metric_id)
        if cls != metric_class:
            continue
        out.append({
            "metric": rec.metric_id,
            "sample_id": rec.sample_id,
            "value": float(rec.score),
        })
    return out


def core_metric_keys(
    run: NormalizedRun,
    *,
    metric_class: str = "core",
) -> set[str]:
    keys: set[str] = set()
    for er in run.evaluation_log.evaluation_results or []:
        cfg = er.metric_config
        key = cfg.metric_id or cfg.metric_name or er.evaluation_name
        cls, _ = helm_metrics.classify_metric(key)
        if cls == metric_class:
            keys.add(key)
    return keys


__all__ = [
    "core_metric_keys",
    "instance_core_score_records",
    "instance_level_core_rows",
    "run_level_core_rows",
]
