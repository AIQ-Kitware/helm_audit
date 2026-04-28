"""
Inventory-reporting tool for a single HELM index CSV.

Describes what is in the index — row counts, cardinality, and distributions
across tracks, suite versions, models, benchmark groups, and entry kinds.

This stage is intentionally neutral.  It does not judge, interpret drift,
or make deduplication decisions.

Usage:

    python -m eval_audit.workflows.analyze_index_snapshot \\
        --index_fpath /data/my-store/indexes/my_index.latest.csv \\
        --out_dpath   /data/my-store/analysis/index-snapshot/
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import scriptconfig as scfg
from loguru import logger

from eval_audit.infra.logging import setup_cli_logging
from eval_audit.infra.paths import index_snapshot_analysis_dpath
from eval_audit.infra.plotly_env import configure_plotly_chrome


class AnalyzeIndexSnapshotConfig(scfg.DataConfig):
    index_fpath = scfg.Value(
        None,
        help='Path to the index CSV to analyze.',
        position=1,
    )
    out_dpath = scfg.Value(
        str(index_snapshot_analysis_dpath()),
        help='Directory where analysis artifacts will be written.',
    )

    @classmethod
    def main(cls, argv=None, **kwargs):
        """
        Example:
            >>> # xdoctest: +SKIP
            >>> from eval_audit.workflows.analyze_index_snapshot import *  # NOQA
            >>> argv = False
            >>> cls = AnalyzeIndexSnapshotConfig
            >>> cls.main(argv=argv)
        """
        setup_cli_logging()
        config = cls.cli(argv=argv, data=kwargs, verbose='auto')
        if not config.index_fpath:
            raise SystemExit('--index_fpath is required')
        index_fpath = Path(config.index_fpath).expanduser().resolve()
        out_dpath = Path(config.out_dpath).expanduser().resolve()
        analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)


def analyze_index_snapshot(index_fpath: Path, out_dpath: Path) -> dict:
    """
    Inventory-report an index CSV and emit tabular, JSON, TXT, and HTML artifacts.

    Optional provenance columns (``suite_version``, ``public_track``,
    ``run_spec_hash``, ``entry_kind``) are injected as null/default columns
    when absent so that all downstream groupby calls succeed.  Breakdowns on
    absent columns produce empty tables and trivially-empty figures.

    The returned dict mirrors ``index_snapshot_summary.latest.json`` exactly —
    it contains complete (non-truncated) arrays.  Only the TXT summary
    truncates long lists for human readability.

    Args:
        index_fpath: Path to the index CSV.
        out_dpath: Directory where analysis artifacts will be written.

    Returns:
        Summary dict (same content as ``index_snapshot_summary.latest.json``).
    """
    logger.info('Loading index snapshot from {}', index_fpath)
    df = pd.read_csv(index_fpath, low_memory=False)
    out_dpath.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Normalise optional provenance columns — note which were present
    # before we fill in defaults so we can signal absence in output.
    # ------------------------------------------------------------------
    _original_cols = set(df.columns)
    for col in ('suite_version', 'public_track', 'run_spec_hash'):
        if col not in df.columns:
            df[col] = None
    if 'entry_kind' not in df.columns:
        df['entry_kind'] = 'benchmark_run'
    for col in ('model', 'scenario_class', 'benchmark_group', 'run_name'):
        if col not in df.columns:
            df[col] = None

    has_suite_version = 'suite_version' in _original_cols and df['suite_version'].notna().any()
    has_public_track = 'public_track' in _original_cols and df['public_track'].notna().any()
    has_run_spec_hash = 'run_spec_hash' in _original_cols and df['run_spec_hash'].notna().any()

    # ------------------------------------------------------------------
    # Backfill a canonical run_name so older indexes (e.g. local indexes
    # emitted before `run_name` was a first-class field) still produce
    # non-zero run counts.  Preference order mirrors the local builder:
    # run_name → run_spec_name → logical_run_key → run_entry → basename of
    # run_path / run_dir.
    # ------------------------------------------------------------------
    df['run_name'] = _coerce_run_name_series(df)

    # ------------------------------------------------------------------
    # Partition rows
    # ------------------------------------------------------------------
    df_runs = df[df['entry_kind'] == 'benchmark_run'].copy()
    n_benchmark_runs = len(df_runs)
    n_structural_non_run = int((df['entry_kind'] == 'structural_non_run').sum())
    n_unknown_entry = int((df['entry_kind'] == 'unknown').sum())
    total_rows = len(df)

    # ------------------------------------------------------------------
    # Cardinality (benchmark runs only for run/model/benchmark counts)
    # ------------------------------------------------------------------
    tracks = sorted(df['public_track'].dropna().unique().tolist())
    suite_versions = sorted(df['suite_version'].dropna().unique().tolist())
    distinct_run_names = int(df_runs['run_name'].dropna().nunique())
    distinct_models = int(df_runs['model'].dropna().nunique())
    distinct_benchmarks = int(df_runs['benchmark_group'].dropna().nunique())
    distinct_scenario_classes = int(df_runs['scenario_class'].dropna().nunique())

    # ------------------------------------------------------------------
    # Grouped breakdowns
    # ------------------------------------------------------------------
    by_track = _agg_by_group(df, df_runs, 'public_track')
    by_suite = _agg_by_group(df, df_runs, 'suite_version')
    if has_suite_version:
        by_suite = by_suite.sort_values('suite_version')

    by_model = (
        df_runs.groupby('model', dropna=False)
        .agg(
            total_runs=('run_name', 'count'),
            distinct_run_names=('run_name', 'nunique'),
            distinct_benchmarks=('benchmark_group', 'nunique'),
            distinct_suite_versions=('suite_version', 'nunique'),
        )
        .reset_index()
        .sort_values('total_runs', ascending=False)
    )

    by_benchmark = (
        df_runs.groupby('benchmark_group', dropna=False)
        .agg(
            total_runs=('run_name', 'count'),
            distinct_run_names=('run_name', 'nunique'),
            distinct_models=('model', 'nunique'),
            distinct_suite_versions=('suite_version', 'nunique'),
        )
        .reset_index()
        .sort_values('total_runs', ascending=False)
    )

    by_entry_kind = (
        df.groupby('entry_kind', dropna=False)
        .size()
        .reset_index(name='total_rows')
        .sort_values('total_rows', ascending=False)
    )

    # ------------------------------------------------------------------
    # Build summary dict — complete, no top-k truncation
    # ------------------------------------------------------------------
    summary: dict = {
        'index_fpath': str(index_fpath),
        'row_counts': {
            'total_rows': total_rows,
            'benchmark_runs': n_benchmark_runs,
            'structural_non_run': n_structural_non_run,
            'unknown_entry': n_unknown_entry,
        },
        'column_presence': {
            'has_suite_version': has_suite_version,
            'has_public_track': has_public_track,
            'has_run_spec_hash': has_run_spec_hash,
        },
        'cardinality': {
            'tracks': len(tracks),
            'suite_versions': len(suite_versions),
            'run_names': distinct_run_names,
            'models': distinct_models,
            'benchmark_groups': distinct_benchmarks,
            'scenario_classes': distinct_scenario_classes,
        },
        'counts_by_track': by_track.to_dict(orient='records'),
        'counts_by_suite_version': by_suite.to_dict(orient='records'),
        'counts_by_model': by_model.to_dict(orient='records'),
        'counts_by_benchmark': by_benchmark.to_dict(orient='records'),
        'counts_by_entry_kind': by_entry_kind.to_dict(orient='records'),
    }

    # ------------------------------------------------------------------
    # Write artifacts
    # ------------------------------------------------------------------
    def _write_txt(text: str, name: str) -> Path:
        p = out_dpath / name
        p.write_text(text, encoding='utf-8')
        logger.success('Wrote {}', p)
        return p

    def _write_json(obj: dict, name: str) -> Path:
        p = out_dpath / name
        p.write_text(
            json.dumps(obj, indent=2, default=str, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        logger.success('Wrote {}', p)
        return p

    def _write_csv(df_out: pd.DataFrame, name: str) -> Path:
        p = out_dpath / name
        df_out.to_csv(p, index=False)
        logger.success('Wrote {}', p)
        return p

    # TXT — human-readable, top-10 truncation allowed
    summary_text = _format_summary_text(
        summary, df, tracks, suite_versions, by_model, by_benchmark,
    )
    _write_txt(summary_text, 'index_snapshot_summary.latest.txt')
    _write_json(summary, 'index_snapshot_summary.latest.json')

    _write_csv(by_track, 'index_snapshot_by_track.latest.csv')
    _write_csv(by_suite, 'index_snapshot_by_suite_version.latest.csv')
    _write_csv(by_model, 'index_snapshot_by_model.latest.csv')
    _write_csv(by_benchmark, 'index_snapshot_by_benchmark.latest.csv')
    _write_csv(by_entry_kind, 'index_snapshot_by_entry_kind.latest.csv')

    # HTML + JPG figures
    _write_plotly_figure_bundle(
        by_track, y_col='public_track', x_col='total_rows',
        title='HELM Index Snapshot — Rows by Track',
        html_fpath=out_dpath / 'index_snapshot_tracks.latest.html',
        jpg_fpath=out_dpath / 'index_snapshot_tracks.latest.jpg',
    )
    _write_plotly_figure_bundle(
        by_suite, y_col='suite_version', x_col='total_rows',
        title='HELM Index Snapshot — Rows by Suite Version',
        html_fpath=out_dpath / 'index_snapshot_suite_versions.latest.html',
        jpg_fpath=out_dpath / 'index_snapshot_suite_versions.latest.jpg',
    )
    _write_plotly_figure_bundle(
        by_model, y_col='model', x_col='total_runs',
        title='HELM Index Snapshot — Runs by Model',
        html_fpath=out_dpath / 'index_snapshot_models.latest.html',
        jpg_fpath=out_dpath / 'index_snapshot_models.latest.jpg',
    )
    _write_plotly_figure_bundle(
        by_benchmark, y_col='benchmark_group', x_col='total_runs',
        title='HELM Index Snapshot — Runs by Benchmark Group',
        html_fpath=out_dpath / 'index_snapshot_benchmarks.latest.html',
        jpg_fpath=out_dpath / 'index_snapshot_benchmarks.latest.jpg',
    )
    _write_plotly_figure_bundle(
        by_entry_kind, y_col='entry_kind', x_col='total_rows',
        title='HELM Index Snapshot — Rows by Entry Kind',
        html_fpath=out_dpath / 'index_snapshot_entry_kinds.latest.html',
        jpg_fpath=out_dpath / 'index_snapshot_entry_kinds.latest.jpg',
    )

    print(summary_text)
    logger.success('Analysis complete — artifacts written to {}', out_dpath)
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_run_name_series(df: pd.DataFrame) -> pd.Series:
    """Return a canonical run_name column, filling gaps from weaker sources.

    Preference order per row:
      1. ``run_name``
      2. ``run_spec_name``
      3. ``logical_run_key``
      4. ``run_entry``
      5. basename of ``run_path``
      6. basename of ``run_dir``
    """
    def _basename_of(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([None] * len(df), index=df.index)
        return df[col].map(
            lambda v: Path(v).name if isinstance(v, str) and v else None
        )

    out = df['run_name'].copy() if 'run_name' in df.columns else pd.Series(
        [None] * len(df), index=df.index,
    )
    for col in ('run_spec_name', 'logical_run_key', 'run_entry'):
        if col in df.columns:
            out = out.where(out.notna(), df[col])
    out = out.where(out.notna(), _basename_of('run_path'))
    out = out.where(out.notna(), _basename_of('run_dir'))
    return out


def _agg_by_group(
    df: pd.DataFrame,
    df_runs: pd.DataFrame,
    group_col: str,
) -> pd.DataFrame:
    """Per-group breakdown combining all-rows totals and benchmark-run sub-counts."""
    total = df.groupby(group_col, dropna=False).size().rename('total_rows')
    runs = df_runs.groupby(group_col, dropna=False).size().rename('benchmark_runs')
    non_run = (
        df[df['entry_kind'] != 'benchmark_run']
        .groupby(group_col, dropna=False)
        .size()
        .rename('non_run_entries')
    )
    distinct_runs = (
        df_runs.groupby(group_col, dropna=False)['run_name']
        .nunique()
        .rename('distinct_run_names')
    )
    other_col = 'suite_version' if group_col == 'public_track' else 'public_track'
    cross = (
        df_runs.groupby(group_col, dropna=False)[other_col]
        .nunique()
        .rename(f'distinct_{other_col}s')
    )
    models = (
        df_runs.groupby(group_col, dropna=False)['model']
        .nunique()
        .rename('distinct_models')
    )
    result = (
        pd.concat([total, runs, non_run, distinct_runs, cross, models], axis=1)
        .fillna(0)
        .reset_index()
    )
    int_cols = [c for c in result.columns if c != group_col]
    result[int_cols] = result[int_cols].astype(int)
    return result


def _write_plotly_figure_bundle(
    df: pd.DataFrame,
    y_col: str,
    x_col: str,
    title: str,
    html_fpath: Path,
    jpg_fpath: Path,
) -> None:
    """Write a horizontal bar chart as both HTML and JPG."""
    import plotly.express as px

    df_plot = df[[y_col, x_col]].dropna(subset=[y_col])
    fig = px.bar(df_plot, x=x_col, y=y_col, orientation='h', title=title)
    if not df_plot.empty:
        fig.update_layout(yaxis={'categoryorder': 'total ascending'})

    fig.write_html(str(html_fpath), include_plotlyjs='cdn')
    logger.success('Wrote {}', html_fpath)

    skip_static = os.environ.get('HELM_AUDIT_SKIP_STATIC_IMAGES', '') in {'1', 'true', 'yes'}
    if not skip_static:
        configure_plotly_chrome()
        try:
            fig.write_image(str(jpg_fpath), scale=3.0)
            logger.success('Wrote {}', jpg_fpath)
        except Exception as ex:
            logger.warning('unable to write JPG {}: {!r}', jpg_fpath, ex)


def _absent(flag: bool) -> str:
    return '' if flag else ' (column absent)'


def _format_summary_text(
    summary: dict,
    df: pd.DataFrame,
    tracks: list[str],
    suite_versions: list[str],
    by_model: pd.DataFrame,
    by_benchmark: pd.DataFrame,
) -> str:
    rc = summary['row_counts']
    card = summary['cardinality']
    cp = summary['column_presence']
    has_sv = cp['has_suite_version']
    has_tr = cp['has_public_track']

    lines = [
        '=' * 70,
        'HELM INDEX SNAPSHOT — INVENTORY SUMMARY',
        '=' * 70,
        f"Index file: {summary['index_fpath']}",
        '',
        '--- Row counts ---',
        f"  Total rows:                {rc['total_rows']:>8,}",
        f"  Benchmark runs:            {rc['benchmark_runs']:>8,}",
        f"  Structural non-run:        {rc['structural_non_run']:>8,}",
        f"  Unknown entry kind:        {rc['unknown_entry']:>8,}",
        '',
        f"--- Public tracks{_absent(has_tr)} ---",
        f"  Number of tracks:          {card['tracks']:>8}",
    ]
    for t in tracks:
        n = int((df['public_track'] == t).sum())
        lines.append(f'    {t}: {n:,}')
    if not has_tr:
        lines.append('    (no public_track column in this index)')
    lines += [
        '',
        f"--- Suite versions{_absent(has_sv)} ---",
        f"  Number of suite versions:  {card['suite_versions']:>8}",
    ]
    for sv in suite_versions:
        n = int((df['suite_version'] == sv).sum())
        lines.append(f'    {sv}: {n:,}')
    if not has_sv:
        lines.append('    (no suite_version column in this index)')
    lines += [
        '',
        '--- Cardinality (benchmark runs only) ---',
        f"  Distinct run names:        {card['run_names']:>8,}",
        f"  Distinct models:           {card['models']:>8,}",
        f"  Distinct benchmark groups: {card['benchmark_groups']:>8,}",
        f"  Distinct scenario classes: {card['scenario_classes']:>8,}",
        '',
        '--- Top 10 models by run count ---',
    ]
    for _, row in by_model.head(10).iterrows():
        lines.append(f"  {row['model']}: {int(row['total_runs']):,}")
    lines += [
        '',
        '--- Top 10 benchmarks by run count ---',
    ]
    for _, row in by_benchmark.head(10).iterrows():
        lines.append(f"  {row['benchmark_group']}: {int(row['total_runs']):,}")
    lines.append('=' * 70)
    return '\n'.join(lines) + '\n'


# Thin alias kept for any callers from the previous module name.
analyze_official_index = analyze_index_snapshot


__cli__ = AnalyzeIndexSnapshotConfig

main = __cli__.main

if __name__ == '__main__':
    __cli__.main()
