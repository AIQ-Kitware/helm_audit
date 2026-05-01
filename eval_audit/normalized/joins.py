"""Generic join helpers for normalized runs.

Stage-2 scope: provide the join primitives used by the Stage-3+
comparison core, but no comparison logic of their own. Code outside the
normalized package should rely on these helpers rather than re-implementing
the same joins ad hoc.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from eval_audit.normalized.model import InstanceRecord, NormalizedRun


def joined_metric_means(
    run: NormalizedRun,
) -> dict[str, float]:
    """Run-level metric means keyed by stable metric handle.

    Uses ``metric_id`` when populated, otherwise ``metric_name``, otherwise
    ``evaluation_name`` so every result lands somewhere addressable.
    """
    out: dict[str, float] = {}
    for er in run.evaluation_log.evaluation_results or []:
        cfg = er.metric_config
        key = cfg.metric_id or cfg.metric_name or er.evaluation_name
        try:
            out[key] = float(er.score_details.score)
        except (TypeError, ValueError):
            continue
    return out


def join_run_level(
    run_a: NormalizedRun,
    run_b: NormalizedRun,
) -> Iterator[tuple[str, float, float]]:
    """Yield ``(metric_key, score_a, score_b)`` for metrics present in both runs."""
    means_a = joined_metric_means(run_a)
    means_b = joined_metric_means(run_b)
    for key in sorted(set(means_a) & set(means_b)):
        yield key, means_a[key], means_b[key]


def index_instances(
    instances: Iterable[InstanceRecord],
) -> dict[tuple[str, str | None], InstanceRecord]:
    """Index instances by stable join key.

    When the same join key appears multiple times (e.g. perturbations not yet
    represented in the EEE schema), the first record wins. The matching
    behavior should be reviewed at Stage 4 if perturbed comparisons become a
    requirement; today we treat one record per (sample, metric) as the rule.
    """
    out: dict[tuple[str, str | None], InstanceRecord] = {}
    for rec in instances:
        out.setdefault(rec.join_key, rec)
    return out


def join_instances(
    run_a: NormalizedRun,
    run_b: NormalizedRun,
) -> Iterator[tuple[tuple[str, str | None], InstanceRecord, InstanceRecord]]:
    """Yield ``(join_key, record_a, record_b)`` for instances in both runs."""
    idx_a = index_instances(run_a.instances)
    idx_b = index_instances(run_b.instances)
    for key in sorted(set(idx_a) & set(idx_b), key=lambda k: (str(k[0]), str(k[1]))):
        yield key, idx_a[key], idx_b[key]


__all__ = [
    "index_instances",
    "join_instances",
    "join_run_level",
    "joined_metric_means",
]
