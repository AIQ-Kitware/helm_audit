"""Backwards-compat shim — use eval_audit.workflows.analyze_index_snapshot instead."""
from eval_audit.workflows.analyze_index_snapshot import (  # noqa: F401
    AnalyzeIndexSnapshotConfig as AnalyzeOfficialIndexConfig,
    analyze_index_snapshot as analyze_official_index,
)
