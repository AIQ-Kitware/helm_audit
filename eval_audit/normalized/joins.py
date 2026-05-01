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


def _index_by_sample_id(
    instances: Iterable[InstanceRecord],
) -> dict[tuple[str, str | None], InstanceRecord]:
    """Sample-id-keyed index: ``(sample_id, metric_id) -> record``.

    Used as the primary key by :func:`join_instances`. ``sample_id`` is
    HELM-assigned and stable within a scenario × version, so two
    different conversions of the same HELM run dir produce identical
    sets of sample_ids. ``InstanceRecord.join_key`` (which prefers
    ``sample_hash``) is kept untouched for back-compat with any
    direct callers.
    """
    out: dict[tuple[str, str | None], InstanceRecord] = {}
    for rec in instances:
        out.setdefault((rec.sample_id, rec.metric_id), rec)
    return out


def join_instances(
    run_a: NormalizedRun,
    run_b: NormalizedRun,
) -> Iterator[tuple[tuple[str, str | None], InstanceRecord, InstanceRecord]]:
    """Yield ``(join_key, record_a, record_b)`` for instances in both runs.

    Strategy is two-stage to handle real-world data: we observe in our
    audit that two EEE conversions of *the same* HELM run can produce
    differing ``sample_hash`` values for the same instance, because
    the hash sometimes folds in model-deployment-specific prompt
    rendering. ``sample_id`` does not have this drift — HELM assigns
    it per scenario × instance, independent of the model.

    1. **Sample-id-first join.** Match on ``(sample_id, metric_id)``.
       This is the right key for "official vs local reproduction of
       the same HELM run" — which is the audit's primary use case —
       and is robust to ``sample_hash`` drift across conversions.
    2. **Sample-hash fallback.** For records that didn't match by
       sample_id (e.g. a cross-HELM-version comparison where ids drift
       but content is identical), fall back to the hash-keyed index
       built from ``InstanceRecord.join_key``. Any matches found here
       only fire if NEITHER side already matched in stage 1, so we
       never double-count an instance.

    Net effect: same-version joins always succeed via stage 1 (which
    is the case that matters today). Cross-version joins still get
    the hash-based safety net the original design provided.
    """
    # Stage 1: sample-id-keyed indexes (the primary, drift-resistant key)
    a_by_sid = _index_by_sample_id(run_a.instances)
    b_by_sid = _index_by_sample_id(run_b.instances)
    matched_a_ids: set[tuple[str, str | None]] = set()
    matched_b_ids: set[tuple[str, str | None]] = set()
    for key in sorted(set(a_by_sid) & set(b_by_sid),
                      key=lambda k: (str(k[0]), str(k[1]))):
        rec_a, rec_b = a_by_sid[key], b_by_sid[key]
        matched_a_ids.add(key)
        matched_b_ids.add(key)
        yield key, rec_a, rec_b

    # Stage 2: hash-based fallback for records still unmatched on either side.
    a_by_hash = index_instances(run_a.instances)
    b_by_hash = index_instances(run_b.instances)
    for key in sorted(set(a_by_hash) & set(b_by_hash),
                      key=lambda k: (str(k[0]), str(k[1]))):
        rec_a, rec_b = a_by_hash[key], b_by_hash[key]
        a_id_key = (rec_a.sample_id, rec_a.metric_id)
        b_id_key = (rec_b.sample_id, rec_b.metric_id)
        if a_id_key in matched_a_ids or b_id_key in matched_b_ids:
            continue
        matched_a_ids.add(a_id_key)
        matched_b_ids.add(b_id_key)
        yield key, rec_a, rec_b


__all__ = [
    "index_instances",
    "join_instances",
    "join_run_level",
    "joined_metric_means",
]
