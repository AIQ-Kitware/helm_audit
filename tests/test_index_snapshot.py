"""
Tests for the generic index-snapshot inventory reporter (analyze_index_snapshot).

Covers:
- Required artifacts are emitted (CSV, JSON, TXT, HTML)
- JSON is complete — no top-k truncation, all models/benchmarks included
- TXT may truncate long lists for readability
- entry_kind breakdown CSV is correct
- Graceful degradation when optional columns are absent
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from eval_audit.cli.index_historic_helm_runs import OFFICIAL_INDEX_COLUMNS
from eval_audit.workflows.analyze_index_snapshot import analyze_index_snapshot


# ---------------------------------------------------------------------------
# Artifact contracts
# ---------------------------------------------------------------------------

REQUIRED_CSV_ARTIFACTS = [
    'index_snapshot_by_track.latest.csv',
    'index_snapshot_by_suite_version.latest.csv',
    'index_snapshot_by_model.latest.csv',
    'index_snapshot_by_benchmark.latest.csv',
    'index_snapshot_by_entry_kind.latest.csv',
]

REQUIRED_TXT_JSON_ARTIFACTS = [
    'index_snapshot_summary.latest.txt',
    'index_snapshot_summary.latest.json',
]

REQUIRED_HTML_ARTIFACTS = [
    'index_snapshot_tracks.latest.html',
    'index_snapshot_suite_versions.latest.html',
    'index_snapshot_models.latest.html',
    'index_snapshot_benchmarks.latest.html',
    'index_snapshot_entry_kinds.latest.html',
]

REQUIRED_JPG_ARTIFACTS = [
    'index_snapshot_tracks.latest.jpg',
    'index_snapshot_suite_versions.latest.jpg',
    'index_snapshot_models.latest.jpg',
    'index_snapshot_benchmarks.latest.jpg',
    'index_snapshot_entry_kinds.latest.jpg',
]

REQUIRED_ARTIFACTS = REQUIRED_TXT_JSON_ARTIFACTS + REQUIRED_CSV_ARTIFACTS + REQUIRED_HTML_ARTIFACTS

REMOVED_ARTIFACTS = [
    'index_snapshot_version_drift.latest.csv',
    'index_snapshot_duplicates_by_run_name.latest.csv',
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(run_name: str, suite_version: str = 'v0.2.2',
         run_spec_hash: str | None = 'h0',
         track: str = 'main', model: str | None = None) -> dict:
    return {
        'source_kind': 'official',
        'public_root': '/data',
        'public_track': track,
        'suite_version': suite_version,
        'public_run_dir': f'/data/bo/runs/{suite_version}/{run_name}',
        'run_name': run_name,
        'entry_kind': 'benchmark_run',
        'has_run_spec_json': run_spec_hash is not None,
        'run_spec_fpath': None,
        'run_spec_name': run_name,
        'run_spec_hash': run_spec_hash,
        'model': model or (run_name.split('model=')[1] if 'model=' in run_name else 'test_model'),
        'scenario_class': 'helm.TestScenario',
        'benchmark_group': run_name.split(':')[0],
        'max_eval_instances': None,
        'is_structural_junk': False,
        'index_generated_utc': '2026-01-01T00:00:00Z',
    }


def _junk_row(suite_version: str = 'v0.2.2') -> dict:
    return {
        **_row('groups', suite_version, None),
        'entry_kind': 'structural_non_run',
        'is_structural_junk': True,
        'model': None,
        'scenario_class': None,
        'benchmark_group': None,
        'run_spec_name': None,
    }


def _write_index(rows: list[dict], fpath: Path) -> None:
    df = pd.DataFrame(rows)
    for col in OFFICIAL_INDEX_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df[OFFICIAL_INDEX_COLUMNS].to_csv(fpath, index=False)


def _minimal_row(run_name: str, model: str = 'foo') -> dict:
    """Row with no provenance columns — mimics a non-official index."""
    return {
        'run_name': run_name,
        'model': model,
        'benchmark_group': run_name.split(':')[0],
        'scenario_class': 'TestScenario',
    }


def _run_analysis(rows: list[dict], tmp_path: Path, fname: str = 'idx.csv'):
    index_fpath = tmp_path / fname
    _write_index(rows, index_fpath)
    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)
    return out_dpath, summary


# ---------------------------------------------------------------------------
# Required artifacts
# ---------------------------------------------------------------------------

def test_all_required_artifacts_emitted(tmp_path):
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'hash_aaa'),
        _row('mmlu:model=foo', 'v0.3.0', 'hash_ccc'),
        _row('hellaswag:model=bar', 'v0.2.2', 'hash_ddd'),
        _junk_row(),
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    for artifact in REQUIRED_ARTIFACTS:
        assert (out_dpath / artifact).exists(), f'Missing: {artifact}'


def test_removed_artifacts_not_emitted(tmp_path):
    rows = [_row('boolq:model=foo', 'v0.2.2', 'h1')]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    for artifact in REMOVED_ARTIFACTS:
        assert not (out_dpath / artifact).exists(), f'Should not exist: {artifact}'


# ---------------------------------------------------------------------------
# JSON completeness — no top-k truncation
# ---------------------------------------------------------------------------

def test_json_contains_all_models_not_top_k(tmp_path):
    rows = [
        _row(f'bench_{i}:task=x', 'v0.2.2', f'h{i}', model=f'model_{i:02d}')
        for i in range(15)
    ]
    out_dpath, summary = _run_analysis(rows, tmp_path)
    assert len(summary['counts_by_model']) == 15

    # Verify the JSON file on disk also has all 15
    import json as _json
    data = _json.loads((out_dpath / 'index_snapshot_summary.latest.json').read_text())
    assert len(data['counts_by_model']) == 15


def test_json_contains_all_benchmarks_not_top_k(tmp_path):
    rows = [
        _row(f'bench_{i:02d}:task=x', 'v0.2.2', f'h{i}', model='shared_model')
        for i in range(15)
    ]
    out_dpath, summary = _run_analysis(rows, tmp_path)
    assert len(summary['counts_by_benchmark']) == 15


def test_json_structure_has_required_keys(tmp_path):
    out_dpath, summary = _run_analysis([_row('boolq:model=foo')], tmp_path)
    assert 'row_counts' in summary
    assert 'column_presence' in summary
    assert 'cardinality' in summary
    assert 'counts_by_track' in summary
    assert 'counts_by_suite_version' in summary
    assert 'counts_by_model' in summary
    assert 'counts_by_benchmark' in summary
    assert 'counts_by_entry_kind' in summary
    # Drift keys must not be present
    assert 'n_run_names_in_multiple_versions' not in summary
    assert 'n_run_names_with_hash_drift' not in summary
    assert 'dedup_views' not in summary
    assert 'top_models' not in summary
    assert 'top_benchmarks' not in summary


def test_json_row_counts_correct(tmp_path):
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'h1'),
        _row('mmlu:model=foo', 'v0.3.0', 'h2'),
        _junk_row(),
        {**_row('mystery_dir', 'v0.2.2', None), 'entry_kind': 'unknown'},
    ]
    _, summary = _run_analysis(rows, tmp_path)
    rc = summary['row_counts']
    assert rc['total_rows'] == 4
    assert rc['benchmark_runs'] == 2
    assert rc['structural_non_run'] == 1
    assert rc['unknown_entry'] == 1


def test_json_cardinality_correct(tmp_path):
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'h1'),
        _row('boolq:model=foo', 'v0.3.0', 'h2'),
        _row('mmlu:model=bar', 'v0.2.2', 'h3'),
    ]
    _, summary = _run_analysis(rows, tmp_path)
    card = summary['cardinality']
    assert card['run_names'] == 2       # boolq and mmlu
    assert card['models'] == 2          # foo, bar
    assert card['benchmark_groups'] == 2
    assert card['suite_versions'] == 2  # v0.2.2, v0.3.0
    assert card['tracks'] == 1          # main


# ---------------------------------------------------------------------------
# TXT summary
# ---------------------------------------------------------------------------

def test_txt_shows_top_10_not_all_when_many_models(tmp_path):
    rows = [
        _row(f'bench:task=x', 'v0.2.2', f'h{i}', model=f'model_{i:02d}')
        for i in range(15)
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    txt = (out_dpath / 'index_snapshot_summary.latest.txt').read_text()
    # TXT should mention "Top 10 models"
    assert 'Top 10 models' in txt
    # Should NOT contain more than 10 model lines in the models section
    model_lines = [ln for ln in txt.splitlines() if ln.strip().startswith('model_')]
    assert len(model_lines) <= 10


def test_txt_does_not_mention_drift(tmp_path):
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'h1'),
        _row('boolq:model=foo', 'v0.3.0', 'h2'),
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    txt = (out_dpath / 'index_snapshot_summary.latest.txt').read_text()
    assert 'drift' not in txt.lower()
    assert 'overlap' not in txt.lower()


# ---------------------------------------------------------------------------
# Entry-kind CSV
# ---------------------------------------------------------------------------

def test_entry_kind_csv_correct(tmp_path):
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'h1'),
        _row('mmlu:model=bar', 'v0.2.2', 'h2'),
        _junk_row(),
        {**_row('mystery_dir', 'v0.2.2', None), 'entry_kind': 'unknown'},
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    ek = pd.read_csv(out_dpath / 'index_snapshot_by_entry_kind.latest.csv')
    counts = dict(zip(ek['entry_kind'], ek['total_rows']))
    assert counts['benchmark_run'] == 2
    assert counts['structural_non_run'] == 1
    assert counts['unknown'] == 1


# ---------------------------------------------------------------------------
# HTML figures emitted
# ---------------------------------------------------------------------------

def test_html_artifacts_emitted(tmp_path):
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'h1'),
        _junk_row(),
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    for html in REQUIRED_HTML_ARTIFACTS:
        p = out_dpath / html
        assert p.exists(), f'Missing HTML: {html}'
        assert p.stat().st_size > 0, f'Empty HTML: {html}'


def test_html_artifacts_emitted_when_columns_absent(tmp_path):
    """Even with no suite_version/track, all 5 HTML figures must be written."""
    rows = [_minimal_row('boolq:model=foo'), _minimal_row('mmlu:model=bar')]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'minimal.csv'
    df.to_csv(index_fpath, index=False)
    out_dpath = tmp_path / 'analysis'
    analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)
    for html in REQUIRED_HTML_ARTIFACTS:
        assert (out_dpath / html).exists(), f'Missing HTML: {html}'


# ---------------------------------------------------------------------------
# JPG companions
# ---------------------------------------------------------------------------

def test_jpg_artifacts_emitted(tmp_path, monkeypatch):
    """JPG companions are written when static images are not skipped."""
    monkeypatch.delenv('HELM_AUDIT_SKIP_STATIC_IMAGES', raising=False)
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'h1'),
        _junk_row(),
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    for jpg in REQUIRED_JPG_ARTIFACTS:
        p = out_dpath / jpg
        # JPG may be absent if kaleido/chrome unavailable — that is tolerated.
        # But if it exists it must be non-empty.
        if p.exists():
            assert p.stat().st_size > 0, f'Empty JPG: {jpg}'


def test_jpg_artifacts_skipped_when_env_set(tmp_path, monkeypatch):
    """Setting HELM_AUDIT_SKIP_STATIC_IMAGES=1 must suppress all JPG writes."""
    monkeypatch.setenv('HELM_AUDIT_SKIP_STATIC_IMAGES', '1')
    rows = [_row('boolq:model=foo', 'v0.2.2', 'h1')]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    for jpg in REQUIRED_JPG_ARTIFACTS:
        assert not (out_dpath / jpg).exists(), f'JPG should not exist: {jpg}'


# ---------------------------------------------------------------------------
# No truncation in canonical plots — all rows must appear in HTML
# ---------------------------------------------------------------------------

def test_html_model_plot_not_truncated(tmp_path):
    """The models HTML must contain all model names, not just top 30."""
    rows = [
        _row(f'bench_{i}:task=x', 'v0.2.2', f'h{i}', model=f'model_{i:03d}')
        for i in range(35)
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    html = (out_dpath / 'index_snapshot_models.latest.html').read_text()
    for i in range(35):
        assert f'model_{i:03d}' in html, f'model_{i:03d} missing from HTML'
    assert 'top 30' not in html.lower()


def test_html_benchmark_plot_not_truncated(tmp_path):
    """The benchmarks HTML must contain all benchmark names, not just top 30."""
    rows = [
        _row(f'bench_{i:03d}:task=x', 'v0.2.2', f'h{i}', model='shared_model')
        for i in range(35)
    ]
    out_dpath, _ = _run_analysis(rows, tmp_path)
    html = (out_dpath / 'index_snapshot_benchmarks.latest.html').read_text()
    for i in range(35):
        assert f'bench_{i:03d}' in html, f'bench_{i:03d} missing from HTML'
    assert 'top 30' not in html.lower()


# ---------------------------------------------------------------------------
# Backward compatibility — indexes without a literal run_name column
# ---------------------------------------------------------------------------

def test_analyzer_synthesizes_run_name_from_run_spec_name(tmp_path):
    """A local-style index lacking run_name must still produce nonzero run counts."""
    rows = [
        {'source_kind': 'local', 'run_spec_name': 'boolq:model=foo',
         'model': 'foo', 'benchmark_group': 'boolq', 'scenario_class': 'helm.X'},
        {'source_kind': 'local', 'run_spec_name': 'mmlu:model=bar',
         'model': 'bar', 'benchmark_group': 'mmlu', 'scenario_class': 'helm.X'},
        {'source_kind': 'local', 'run_spec_name': 'boolq:model=bar',
         'model': 'bar', 'benchmark_group': 'boolq', 'scenario_class': 'helm.X'},
    ]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'local_no_run_name.csv'
    df.to_csv(index_fpath, index=False)
    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    assert summary['row_counts']['benchmark_runs'] == 3
    assert summary['cardinality']['run_names'] == 3
    assert summary['cardinality']['models'] == 2
    by_model = {r['model']: r for r in summary['counts_by_model']}
    assert by_model['bar']['total_runs'] == 2
    assert by_model['foo']['total_runs'] == 1


def test_analyzer_synthesizes_run_name_from_logical_run_key(tmp_path):
    """When only logical_run_key is present, analyzer uses it as run_name."""
    rows = [
        {'source_kind': 'local', 'logical_run_key': 'boolq:model=foo',
         'model': 'foo', 'benchmark_group': 'boolq', 'scenario_class': 'helm.X'},
        {'source_kind': 'local', 'logical_run_key': 'mmlu:model=bar',
         'model': 'bar', 'benchmark_group': 'mmlu', 'scenario_class': 'helm.X'},
    ]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'local_logical_only.csv'
    df.to_csv(index_fpath, index=False)
    summary = analyze_index_snapshot(
        index_fpath=index_fpath, out_dpath=tmp_path / 'analysis',
    )
    assert summary['cardinality']['run_names'] == 2


def test_analyzer_synthesizes_run_name_from_run_path_basename(tmp_path):
    """Basename of run_path is used as a fallback for older local indexes."""
    rows = [
        {'source_kind': 'local',
         'run_path': '/results/exp/helm/job-1/benchmark_output/runs/s/boolq:model=foo',
         'model': 'foo', 'benchmark_group': 'boolq', 'scenario_class': 'helm.X'},
        {'source_kind': 'local',
         'run_path': '/results/exp/helm/job-2/benchmark_output/runs/s/mmlu:model=bar',
         'model': 'bar', 'benchmark_group': 'mmlu', 'scenario_class': 'helm.X'},
    ]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'local_only_run_path.csv'
    df.to_csv(index_fpath, index=False)
    summary = analyze_index_snapshot(
        index_fpath=index_fpath, out_dpath=tmp_path / 'analysis',
    )
    assert summary['cardinality']['run_names'] == 2
    by_model = {r['model']: r for r in summary['counts_by_model']}
    assert by_model['foo']['total_runs'] == 1
    assert by_model['bar']['total_runs'] == 1


def test_analyzer_official_index_unaffected_by_run_name_backfill(tmp_path):
    """The explicit run_name in the official index must take precedence."""
    rows = [
        _row('boolq:model=foo', 'v0.2.2', 'h1'),
        _row('mmlu:model=bar', 'v0.2.2', 'h2'),
    ]
    _, summary = _run_analysis(rows, tmp_path)
    assert summary['cardinality']['run_names'] == 2
    by_model = {r['model']: r for r in summary['counts_by_model']}
    assert by_model['foo']['total_runs'] == 1
    assert by_model['bar']['total_runs'] == 1


# ---------------------------------------------------------------------------
# Graceful degradation when optional columns are absent
# ---------------------------------------------------------------------------

def test_degrades_without_suite_version_and_track(tmp_path):
    rows = [
        _minimal_row('boolq:model=foo'),
        _minimal_row('boolq:model=foo'),
        _minimal_row('mmlu:model=bar'),
    ]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'minimal.csv'
    df.to_csv(index_fpath, index=False)
    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    for artifact in REQUIRED_ARTIFACTS:
        assert (out_dpath / artifact).exists(), f'Missing: {artifact}'

    assert summary['column_presence']['has_suite_version'] is False
    assert summary['column_presence']['has_public_track'] is False
    assert summary['cardinality']['tracks'] == 0
    assert summary['cardinality']['suite_versions'] == 0
    assert summary['row_counts']['benchmark_runs'] == 3
    assert summary['cardinality']['run_names'] == 2

    txt = (out_dpath / 'index_snapshot_summary.latest.txt').read_text()
    assert 'no suite_version' in txt


def test_degrades_without_run_spec_hash(tmp_path):
    rows = [
        {**_minimal_row('boolq:model=foo'), 'suite_version': 'v0.2.2', 'public_track': 'main'},
        {**_minimal_row('boolq:model=foo'), 'suite_version': 'v0.3.0', 'public_track': 'main'},
    ]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'idx.csv'
    df.to_csv(index_fpath, index=False)
    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    assert summary['column_presence']['has_run_spec_hash'] is False
    for artifact in REQUIRED_ARTIFACTS:
        assert (out_dpath / artifact).exists(), f'Missing: {artifact}'
