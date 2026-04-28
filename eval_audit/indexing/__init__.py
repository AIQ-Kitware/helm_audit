"""Shared indexing helpers — normalization and schema for HELM index rows.

Discovery is deliberately NOT shared: the official/public scanner and the
local/audited scanner walk different filesystem layouts and must stay apart.
Only row-level normalization and schema helpers live here.
"""
from eval_audit.indexing.schema import (
    COMMON_COMPONENT_COLUMNS,
    KNOWN_STRUCTURAL_JUNK_NAMES,
    LOCAL_COMPONENT_COLUMNS,
    OFFICIAL_COMPONENT_COLUMNS,
    benchmark_group_from_run_name,
    classify_run_entry,
    compute_run_spec_hash,
    component_id_for_official,
    component_id_for_local,
    extract_run_spec_fields,
    logical_run_key_for_official,
    logical_run_key_for_local,
    normalize_for_hash,
    now_utc_iso,
)

__all__ = [
    "COMMON_COMPONENT_COLUMNS",
    "KNOWN_STRUCTURAL_JUNK_NAMES",
    "LOCAL_COMPONENT_COLUMNS",
    "OFFICIAL_COMPONENT_COLUMNS",
    "benchmark_group_from_run_name",
    "classify_run_entry",
    "compute_run_spec_hash",
    "component_id_for_official",
    "component_id_for_local",
    "extract_run_spec_fields",
    "logical_run_key_for_official",
    "logical_run_key_for_local",
    "normalize_for_hash",
    "now_utc_iso",
]
