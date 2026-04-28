"""Shared schema/normalization helpers for HELM index rows.

Both the canonical official/public index and the canonical local/audited index
produce component-style rows.  They diverge in their discovery (filesystem
layout) and their provenance fields, but they share:

- run-spec normalization + stable hashing
- run-spec field extraction (name / model / scenario / benchmark group)
- run-entry classification (benchmark_run / structural_non_run / unknown)
- canonical column ordering for the two flavours plus a common subset

Discovery logic is intentionally NOT shared.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


#: Directory names that appear under ``benchmark_output/runs/<suite>/`` but are
#: NOT benchmark run directories (HELM emits ``groups/``, ``confs/``, ``logs/``).
KNOWN_STRUCTURAL_JUNK_NAMES: frozenset[str] = frozenset({
    'groups', 'confs', 'logs', '__pycache__',
})


#: Columns that should exist on both official and local component rows so that
#: a future combined index can be a simple normalized union.
COMMON_COMPONENT_COLUMNS: list[str] = [
    'source_kind',
    'artifact_format',
    'eee_artifact_path',
    'component_id',
    'logical_run_key',
    'run_path',
    'run_spec_fpath',
    'run_spec_name',
    'run_spec_hash',
    'model',
    'model_deployment',
    'scenario_class',
    'benchmark_group',
    'max_eval_instances',
    'index_generated_utc',
]


#: Canonical column order for the official/public index CSV.
OFFICIAL_COMPONENT_COLUMNS: list[str] = [
    'source_kind',
    'artifact_format',
    'eee_artifact_path',
    'component_id',
    'logical_run_key',
    'public_root',
    'public_track',
    'suite_version',
    'public_run_dir',
    'run_path',
    'run_name',
    'entry_kind',
    'has_run_spec_json',
    'run_spec_fpath',
    'run_spec_name',
    'run_spec_hash',
    'model',
    'model_deployment',
    'scenario_class',
    'benchmark_group',
    'max_eval_instances',
    'is_structural_junk',
    'index_generated_utc',
]


#: Canonical column order for the local/audited index CSV.  Common component
#: fields come first, local-only provenance (attempt identity, process context,
#: machine info) after.
LOCAL_COMPONENT_COLUMNS: list[str] = [
    # --- component-row fields (schema aligned with OFFICIAL_COMPONENT_COLUMNS) ---
    'source_kind',
    'artifact_format',
    'eee_artifact_path',
    'component_id',
    'logical_run_key',
    'experiment_name',
    'job_id',
    'job_dpath',
    'run_path',
    'run_name',
    'run_spec_fpath',
    'run_spec_name',
    'run_spec_hash',
    'model',
    'model_deployment',
    'scenario_class',
    'benchmark_group',
    'max_eval_instances',
    'index_generated_utc',
    # --- status / run metadata ---
    'status',
    'manifest_timestamp',
    'run_entry',
    'benchmark',
    'method',
    'suite',
    'has_run_dir',
    'has_run_spec',
    'has_stats',
    'has_per_instance_stats',
    'metric_class_names',
    # --- attempt identity (first-class) ---
    'attempt_uuid',
    'attempt_uuid_source',
    'attempt_identity',
    'attempt_identity_kind',
    'attempt_fallback_key',
    # --- machine / process context ---
    'machine_host',
    'machine_user',
    'machine_os',
    'machine_arch',
    'python_version',
    'cuda_visible_devices',
    'gpu_count',
    'gpu_names',
    'gpu_memory_total_mb',
    'provenance_source',
    'process_context_source',
    'adapter_manifest_fpath',
    'process_context_fpath',
    'materialize_out_dpath',
    'process_start_timestamp',
    'process_stop_timestamp',
    'process_duration',
]


def now_utc_iso() -> str:
    """Return the current UTC time as an ISO-8601 second-resolution string."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def normalize_for_hash(obj: Any) -> Any:
    """Recursively sort dict keys so the hash is stable regardless of insertion order."""
    if isinstance(obj, dict):
        return {k: normalize_for_hash(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [normalize_for_hash(v) for v in obj]
    return obj


def compute_run_spec_hash(run_spec_fpath: Path | str | None) -> str | None:
    """SHA-256 hex digest of a normalised ``run_spec.json``, or None on error.

    Stable regardless of dict key insertion order.
    """
    if run_spec_fpath is None:
        return None
    fpath = Path(run_spec_fpath)
    try:
        content = json.loads(fpath.read_text(encoding='utf-8'))
        normalised = json.dumps(
            normalize_for_hash(content),
            ensure_ascii=False,
            separators=(',', ':'),
        )
        return hashlib.sha256(normalised.encode('utf-8')).hexdigest()
    except Exception:
        return None


def extract_run_spec_fields(run_spec_fpath: Path | str | None) -> dict[str, Any]:
    """Extract common fields from a run_spec.json, tolerant of absence / errors.

    Returns a dict with keys:
      ``run_spec_name``, ``model``, ``model_deployment``, ``scenario_class``,
      ``benchmark_group``, ``run_spec_hash``, ``has_run_spec_json``.
    All string-valued fields may be ``None`` when unavailable.
    """
    out: dict[str, Any] = {
        'run_spec_name': None,
        'model': None,
        'model_deployment': None,
        'scenario_class': None,
        'benchmark_group': None,
        'run_spec_hash': None,
        'has_run_spec_json': False,
    }
    if run_spec_fpath is None:
        return out
    fpath = Path(run_spec_fpath)
    if not fpath.exists():
        return out
    out['has_run_spec_json'] = True
    out['run_spec_hash'] = compute_run_spec_hash(fpath)
    try:
        spec = json.loads(fpath.read_text(encoding='utf-8'))
    except Exception:
        return out
    if not isinstance(spec, dict):
        return out
    adapter = spec.get('adapter_spec') or {}
    scenario = spec.get('scenario_spec') or {}
    run_spec_name = spec.get('name')
    out['run_spec_name'] = run_spec_name
    out['model'] = adapter.get('model') if isinstance(adapter, dict) else None
    out['model_deployment'] = (
        adapter.get('model_deployment') if isinstance(adapter, dict) else None
    )
    out['scenario_class'] = (
        scenario.get('class_name') if isinstance(scenario, dict) else None
    )
    out['benchmark_group'] = benchmark_group_from_run_name(run_spec_name)
    return out


def benchmark_group_from_run_name(run_name: str | None) -> str | None:
    """Return the leading benchmark-group token of a HELM run name, or None."""
    if not run_name or ':' not in run_name:
        return None
    return run_name.split(':', 1)[0]


def classify_run_entry(entry_name: str) -> tuple[str, bool]:
    """
    Classify a directory name found under a HELM suite directory.

    Returns:
        (entry_kind, is_structural_junk)
        ``entry_kind`` is one of ``'benchmark_run'``, ``'structural_non_run'``,
        ``'unknown'``.
    """
    if entry_name in KNOWN_STRUCTURAL_JUNK_NAMES:
        return 'structural_non_run', True
    if ':' in entry_name:
        return 'benchmark_run', False
    return 'unknown', False


# ---------------------------------------------------------------------------
# Component identity
# ---------------------------------------------------------------------------

def component_id_for_official(
    *,
    public_track: str | None,
    suite_version: str | None,
    run_name: str | None,
) -> str:
    """Stable per-component id for an official/public index row.

    Shape: ``official::<track>::<suite_version>::<run_name>``.
    """
    return 'official::{}::{}::{}'.format(
        public_track or 'unknown',
        suite_version or 'unknown',
        run_name or 'unknown',
    )


def component_id_for_local(
    *,
    experiment_name: str | None,
    job_id: str | None,
    attempt_identity: str | None,
) -> str:
    """Stable per-component id for a local/audited index row.

    Shape: ``local::<experiment>::<job_id>::<attempt_identity>``.
    Attempt identity is preferred as the disambiguator; the experiment+job
    prefix keeps the id readable and scoped.
    """
    return 'local::{}::{}::{}'.format(
        experiment_name or 'unknown',
        job_id or 'unknown',
        attempt_identity or 'unknown',
    )


def logical_run_key_for_official(
    *,
    run_spec_name: str | None,
    run_name: str | None,
) -> str | None:
    """Logical key identifying what was being run, independent of attempt.

    Prefers the parsed ``run_spec.name`` over the directory name.  Returns
    ``None`` only if neither is available.
    """
    return run_spec_name or run_name


def logical_run_key_for_local(
    *,
    run_spec_name: str | None,
    run_entry: str | None,
) -> str | None:
    """Logical key identifying what was being run, independent of attempt.

    Prefers the parsed ``run_spec.name``; falls back to the ``helm.run_entry``
    declared in the job config.
    """
    return run_spec_name or run_entry
