"""Backwards-compat shim — use helm_audit.workflows.analyze_index_snapshot instead."""
from helm_audit.workflows.analyze_index_snapshot import (  # noqa: F401
    AnalyzeIndexSnapshotConfig as AnalyzeOfficialIndexConfig,
    analyze_index_snapshot as analyze_official_index,
)
