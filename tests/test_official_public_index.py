"""
Tests for the official/public HELM index functionality.

Covers:
1. public_track and suite_version are captured correctly in rows
2. Structural junk entries (groups/confs/logs) are marked as non-run
3. run_spec_hash is stable for identical specs regardless of key order
4. The analysis tool runs from a single official index CSV and produces
   all required summary artifacts
5. Duplicates across versions are surfaced in duplicates report
6. Run-name groups with multiple distinct run_spec_hash values appear in
   the version-drift report
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from helm_audit.cli.index_historic_helm_runs import (
    KNOWN_STRUCTURAL_JUNK_NAMES,
    OFFICIAL_INDEX_COLUMNS,
    _classify_run_entry,
    _compute_run_spec_hash,
    _normalize_for_hash,
    _scan_benchmark_output_dir,
)
from helm_audit.workflows.analyze_index_snapshot import analyze_index_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_spec(run_name: str, model: str = 'test/model') -> dict:
    return {
        'name': run_name,
        'adapter_spec': {'model': model},
        'scenario_spec': {'class_name': 'helm.TestScenario'},
    }


def _base_row(run_name: str, suite_version: str, run_spec_hash: str | None) -> dict:
    """Build a minimal official-index row for use in analysis tests."""
    return {
        'source_kind': 'official',
        'public_root': '/data',
        'public_track': 'main',
        'suite_version': suite_version,
        'public_run_dir': f'/data/bo/runs/{suite_version}/{run_name}',
        'run_name': run_name,
        'entry_kind': 'benchmark_run',
        'has_run_spec_json': run_spec_hash is not None,
        'run_spec_fpath': None,
        'run_spec_name': run_name,
        'run_spec_hash': run_spec_hash,
        'model': run_name.split('model=')[1] if 'model=' in run_name else 'test_model',
        'scenario_class': 'helm.TestScenario',
        'benchmark_group': run_name.split(':')[0],
        'max_eval_instances': None,
        'is_structural_junk': False,
        'index_generated_utc': '2026-01-01T00:00:00Z',
    }


def _write_index(rows: list[dict], fpath: Path) -> None:
    """Write a list of row dicts to a CSV with the canonical column order."""
    df = pd.DataFrame(rows)
    for col in OFFICIAL_INDEX_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df[OFFICIAL_INDEX_COLUMNS].to_csv(fpath, index=False)


# ---------------------------------------------------------------------------
# Part 1 — _normalize_for_hash and _compute_run_spec_hash
# ---------------------------------------------------------------------------

def test_normalize_for_hash_sorts_dict_keys():
    obj = {'z': 1, 'a': 2, 'nested': {'y': 'yes', 'b': 'no'}}
    result = _normalize_for_hash(obj)
    assert list(result.keys()) == ['a', 'nested', 'z']
    assert list(result['nested'].keys()) == ['b', 'y']


def test_normalize_for_hash_handles_lists():
    obj = [{'b': 2, 'a': 1}, {'d': 4, 'c': 3}]
    result = _normalize_for_hash(obj)
    assert result[0] == {'a': 1, 'b': 2}
    assert result[1] == {'c': 3, 'd': 4}


def test_run_spec_hash_stable_across_key_orderings(tmp_path):
    spec = _make_run_spec('boolq:model=foo')
    spec_reordered = {k: spec[k] for k in reversed(list(spec.keys()))}

    fpath1 = tmp_path / 'a.json'
    fpath2 = tmp_path / 'b.json'
    fpath1.write_text(json.dumps(spec))
    fpath2.write_text(json.dumps(spec_reordered))

    h1 = _compute_run_spec_hash(fpath1)
    h2 = _compute_run_spec_hash(fpath2)
    assert h1 is not None
    assert h1 == h2


def test_run_spec_hash_differs_for_different_content(tmp_path):
    spec_a = _make_run_spec('boolq:model=foo', model='foo')
    spec_b = _make_run_spec('boolq:model=bar', model='bar')

    fa = tmp_path / 'a.json'
    fb = tmp_path / 'b.json'
    fa.write_text(json.dumps(spec_a))
    fb.write_text(json.dumps(spec_b))

    assert _compute_run_spec_hash(fa) != _compute_run_spec_hash(fb)


def test_run_spec_hash_returns_none_for_missing_file(tmp_path):
    assert _compute_run_spec_hash(tmp_path / 'nonexistent.json') is None


def test_run_spec_hash_returns_none_for_invalid_json(tmp_path):
    bad = tmp_path / 'bad.json'
    bad.write_text('not valid json!!!')
    assert _compute_run_spec_hash(bad) is None


# ---------------------------------------------------------------------------
# Part 2 — _classify_run_entry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name', sorted(KNOWN_STRUCTURAL_JUNK_NAMES))
def test_classify_known_junk(name):
    kind, is_junk = _classify_run_entry(name)
    assert kind == 'structural_non_run'
    assert is_junk is True


def test_classify_benchmark_run():
    kind, is_junk = _classify_run_entry('boolq:model=foo,data_augmentation=canonical')
    assert kind == 'benchmark_run'
    assert is_junk is False


def test_classify_unknown_entry():
    kind, is_junk = _classify_run_entry('some_mystery_dir')
    assert kind == 'unknown'
    assert is_junk is False


# ---------------------------------------------------------------------------
# Part 3 — _scan_benchmark_output_dir
# ---------------------------------------------------------------------------

def _build_fake_bo(
    tmp_path: Path,
    track: str,
    suite_version: str,
    run_names: list[str],
    junk_names: list[str] | None = None,
    write_run_specs: bool = False,
) -> Path:
    bo_dir = tmp_path / track / 'benchmark_output'
    suite_dir = bo_dir / 'runs' / suite_version
    for run_name in run_names:
        entry_dir = suite_dir / run_name
        entry_dir.mkdir(parents=True, exist_ok=True)
        if write_run_specs:
            spec = _make_run_spec(run_name)
            (entry_dir / 'run_spec.json').write_text(json.dumps(spec))
    for junk in (junk_names or []):
        (suite_dir / junk).mkdir(parents=True, exist_ok=True)
    return bo_dir


def test_scan_captures_public_track_and_suite_version(tmp_path):
    bo_dir = _build_fake_bo(
        tmp_path, 'thaiexam', 'v1.1.0',
        run_names=['thai_exam:exam=tpat1,model=foo'],
    )
    rows = _scan_benchmark_output_dir(
        bo_dir=bo_dir,
        public_root=str(tmp_path),
        public_track='thaiexam',
        suite_pattern='*',
        index_generated_utc='2026-01-01T00:00:00Z',
    )
    assert len(rows) == 1
    r = rows[0]
    assert r['public_track'] == 'thaiexam'
    assert r['suite_version'] == 'v1.1.0'
    assert r['run_name'] == 'thai_exam:exam=tpat1,model=foo'
    assert r['entry_kind'] == 'benchmark_run'
    assert r['is_structural_junk'] is False
    assert r['source_kind'] == 'official'
    assert r['index_generated_utc'] == '2026-01-01T00:00:00Z'


def test_scan_marks_structural_junk(tmp_path):
    bo_dir = _build_fake_bo(
        tmp_path, 'classic', 'v0.2.2',
        run_names=['boolq:model=foo'],
        junk_names=['groups', 'confs', 'logs'],
    )
    rows = _scan_benchmark_output_dir(
        bo_dir=bo_dir,
        public_root=str(tmp_path),
        public_track='main',
        suite_pattern='*',
        index_generated_utc='t',
    )
    by_name = {r['run_name']: r for r in rows}

    for junk in ['groups', 'confs', 'logs']:
        assert by_name[junk]['entry_kind'] == 'structural_non_run', junk
        assert by_name[junk]['is_structural_junk'] is True, junk

    assert by_name['boolq:model=foo']['entry_kind'] == 'benchmark_run'
    assert by_name['boolq:model=foo']['is_structural_junk'] is False


def test_scan_suite_pattern_filtering(tmp_path):
    bo_dir = _build_fake_bo(tmp_path, 'c', 'v0.2.2', ['run_a:model=foo'])
    _build_fake_bo(tmp_path, 'c', 'v0.3.0', ['run_b:model=foo'])
    # re-build: both suites under same bo_dir
    bo_dir = tmp_path / 'c' / 'benchmark_output'
    for sv, rn in [('v0.2.2', 'run_a:model=foo'), ('v0.3.0', 'run_b:model=foo')]:
        d = bo_dir / 'runs' / sv / rn
        d.mkdir(parents=True, exist_ok=True)

    rows_all = _scan_benchmark_output_dir(bo_dir, str(tmp_path), 'c', '*', '')
    rows_v022 = _scan_benchmark_output_dir(bo_dir, str(tmp_path), 'c', 'v0.2.2', '')

    assert {r['suite_version'] for r in rows_all} == {'v0.2.2', 'v0.3.0'}
    assert {r['suite_version'] for r in rows_v022} == {'v0.2.2'}


def test_scan_reads_run_spec_and_computes_hash(tmp_path):
    bo_dir = _build_fake_bo(
        tmp_path, 'c', 'v0.2.2',
        run_names=['boolq:model=foo'],
        write_run_specs=True,
    )
    rows = _scan_benchmark_output_dir(bo_dir, str(tmp_path), 'main', '*', '')
    r = rows[0]
    assert r['has_run_spec_json'] is True
    assert r['run_spec_hash'] is not None
    assert r['model'] == 'test/model'
    assert r['scenario_class'] == 'helm.TestScenario'
    assert r['run_spec_name'] == 'boolq:model=foo'


def test_scan_empty_runs_dir(tmp_path):
    bo_dir = tmp_path / 'benchmark_output'
    (bo_dir / 'runs').mkdir(parents=True)
    rows = _scan_benchmark_output_dir(bo_dir, str(tmp_path), 'main', '*', '')
    assert rows == []


def test_scan_missing_runs_dir(tmp_path):
    bo_dir = tmp_path / 'benchmark_output'
    bo_dir.mkdir(parents=True)
    rows = _scan_benchmark_output_dir(bo_dir, str(tmp_path), 'main', '*', '')
    assert rows == []


# ---------------------------------------------------------------------------
# Part 4 — analyze_index_snapshot: required artifacts and correctness
# ---------------------------------------------------------------------------

REQUIRED_ARTIFACTS = [
    'index_snapshot_summary.latest.txt',
    'index_snapshot_summary.latest.json',
    'index_snapshot_by_track.latest.csv',
    'index_snapshot_by_suite_version.latest.csv',
    'index_snapshot_by_model.latest.csv',
    'index_snapshot_by_benchmark.latest.csv',
    'index_snapshot_duplicates_by_run_name.latest.csv',
    'index_snapshot_version_drift.latest.csv',
]


def _make_analysis_index(tmp_path: Path) -> Path:
    """Build a small but representative official index CSV for analysis tests."""
    rows = [
        # boolq:model=foo appears in v0.2.2 AND v0.3.0 with DIFFERENT hashes (drift)
        _base_row('boolq:model=foo', 'v0.2.2', 'hash_aaa'),
        _base_row('boolq:model=foo', 'v0.3.0', 'hash_bbb'),
        # mmlu:model=foo appears in v0.2.2 AND v0.3.0 with SAME hash (no drift)
        _base_row('mmlu:model=foo', 'v0.2.2', 'hash_ccc'),
        _base_row('mmlu:model=foo', 'v0.3.0', 'hash_ccc'),
        # hellaswag: only in one version, different model
        _base_row('hellaswag:model=bar', 'v0.2.2', 'hash_ddd'),
        # structural junk
        {
            **_base_row('groups', 'v0.2.2', None),
            'entry_kind': 'structural_non_run',
            'is_structural_junk': True,
            'model': None,
            'scenario_class': None,
            'benchmark_group': None,
            'run_spec_name': None,
        },
    ]
    fpath = tmp_path / 'official_public_index.latest.csv'
    _write_index(rows, fpath)
    return fpath


def test_analysis_produces_all_required_artifacts(tmp_path):
    index_fpath = _make_analysis_index(tmp_path)
    out_dpath = tmp_path / 'analysis'
    analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)
    for artifact in REQUIRED_ARTIFACTS:
        assert (out_dpath / artifact).exists(), f'Missing: {artifact}'


def test_analysis_summary_counts(tmp_path):
    index_fpath = _make_analysis_index(tmp_path)
    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    assert summary['total_rows'] == 6
    assert summary['n_benchmark_runs'] == 5
    assert summary['n_structural_non_run'] == 1
    assert summary['distinct_run_names'] == 3   # boolq, mmlu, hellaswag
    assert summary['distinct_models'] == 2       # foo, bar


# ---------------------------------------------------------------------------
# Part 5 — duplicates across versions are surfaced
# ---------------------------------------------------------------------------

def test_duplicates_report_multi_version_runs(tmp_path):
    rows = [
        _base_row('boolq:model=foo', 'v0.2.2', 'h1'),
        _base_row('boolq:model=foo', 'v0.2.3', 'h1'),
        _base_row('boolq:model=foo', 'v0.3.0', 'h1'),
        _base_row('mmlu:model=foo', 'v0.2.2', 'h2'),  # only one version
    ]
    index_fpath = tmp_path / 'idx.csv'
    _write_index(rows, index_fpath)

    out_dpath = tmp_path / 'analysis'
    analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    dups = pd.read_csv(out_dpath / 'index_snapshot_duplicates_by_run_name.latest.csv')
    assert len(dups) == 1
    row = dups.iloc[0]
    assert row['run_name'] == 'boolq:model=foo'
    assert int(row['n_occurrences']) == 3
    assert int(row['n_suite_versions']) == 3


def test_duplicates_report_excludes_single_occurrence_runs(tmp_path):
    rows = [_base_row('boolq:model=foo', 'v0.2.2', 'h1')]
    index_fpath = tmp_path / 'idx.csv'
    _write_index(rows, index_fpath)

    out_dpath = tmp_path / 'analysis'
    analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    dups = pd.read_csv(out_dpath / 'index_snapshot_duplicates_by_run_name.latest.csv')
    assert len(dups) == 0


# ---------------------------------------------------------------------------
# Part 6 — version drift (multiple distinct run_spec_hash values)
# ---------------------------------------------------------------------------

def test_version_drift_detects_hash_differences(tmp_path):
    rows = [
        _base_row('boolq:model=foo', 'v0.2.2', 'aaa'),  # different hashes → drift
        _base_row('boolq:model=foo', 'v0.3.0', 'bbb'),
        _base_row('mmlu:model=foo', 'v0.2.2', 'ccc'),   # same hash → no drift
        _base_row('mmlu:model=foo', 'v0.3.0', 'ccc'),
    ]
    index_fpath = tmp_path / 'idx.csv'
    _write_index(rows, index_fpath)

    out_dpath = tmp_path / 'analysis'
    analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    drift = pd.read_csv(out_dpath / 'index_snapshot_version_drift.latest.csv')
    assert len(drift) == 1
    assert drift.iloc[0]['run_name'] == 'boolq:model=foo'
    assert int(drift.iloc[0]['n_distinct_hashes']) == 2


def test_version_drift_empty_when_no_drift(tmp_path):
    rows = [
        _base_row('boolq:model=foo', 'v0.2.2', 'same_hash'),
        _base_row('boolq:model=foo', 'v0.3.0', 'same_hash'),
    ]
    index_fpath = tmp_path / 'idx.csv'
    _write_index(rows, index_fpath)

    out_dpath = tmp_path / 'analysis'
    analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    drift = pd.read_csv(out_dpath / 'index_snapshot_version_drift.latest.csv')
    assert len(drift) == 0


def test_version_drift_ignores_rows_without_hash(tmp_path):
    rows = [
        _base_row('boolq:model=foo', 'v0.2.2', None),  # no hash available
        _base_row('boolq:model=foo', 'v0.3.0', None),
    ]
    index_fpath = tmp_path / 'idx.csv'
    _write_index(rows, index_fpath)

    out_dpath = tmp_path / 'analysis'
    analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    drift = pd.read_csv(out_dpath / 'index_snapshot_version_drift.latest.csv')
    assert len(drift) == 0


# ---------------------------------------------------------------------------
# Part 7 — dedup views in summary
# ---------------------------------------------------------------------------

def test_dedup_views_in_summary(tmp_path):
    rows = [
        _base_row('boolq:model=foo', 'v0.2.2', 'h1'),
        _base_row('boolq:model=foo', 'v0.3.0', 'h2'),  # same name, different hash
        _base_row('mmlu:model=foo', 'v0.2.2', 'h3'),
    ]
    index_fpath = tmp_path / 'idx.csv'
    _write_index(rows, index_fpath)

    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    dv = summary['dedup_views']
    assert dv['raw_benchmark_run_rows'] == 3
    assert dv['distinct_run_name'] == 2          # boolq and mmlu
    assert dv['distinct_run_name_x_track'] == 2  # both in 'main' track
    assert dv['distinct_run_spec_hash'] == 3     # h1, h2, h3 are all distinct


# ---------------------------------------------------------------------------
# Part 8 — graceful degradation when optional columns are absent
# ---------------------------------------------------------------------------

def _minimal_row(run_name: str, model: str = 'foo') -> dict:
    """Minimal row with only run_name and model — no provenance columns."""
    return {
        'run_name': run_name,
        'model': model,
        'benchmark_group': run_name.split(':')[0],
        'scenario_class': 'TestScenario',
    }


def test_analysis_degrades_without_suite_version_and_track(tmp_path):
    """Index with no suite_version or public_track should still produce all 8 artifacts."""
    rows = [
        _minimal_row('boolq:model=foo'),
        _minimal_row('boolq:model=foo'),  # duplicate run_name
        _minimal_row('mmlu:model=bar'),
    ]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'minimal.csv'
    df.to_csv(index_fpath, index=False)

    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    for artifact in REQUIRED_ARTIFACTS:
        assert (out_dpath / artifact).exists(), f'Missing: {artifact}'

    assert summary['has_suite_version'] is False
    assert summary['has_public_track'] is False
    assert summary['n_tracks'] == 0
    assert summary['n_suite_versions'] == 0
    assert summary['distinct_run_names'] == 2
    assert summary['n_benchmark_runs'] == 3

    # Drift report should be empty (no run_spec_hash column)
    drift = pd.read_csv(out_dpath / 'index_snapshot_version_drift.latest.csv')
    assert len(drift) == 0

    # Summary text should mention absent columns
    txt = (out_dpath / 'index_snapshot_summary.latest.txt').read_text()
    assert 'no suite_version' in txt
    assert 'no public_track' in txt or 'no run_spec_hash' in txt


def test_analysis_degrades_without_run_spec_hash(tmp_path):
    """Index with suite_version/track but no run_spec_hash: drift report is empty."""
    rows = [
        {**_minimal_row('boolq:model=foo'), 'suite_version': 'v0.2.2', 'public_track': 'main'},
        {**_minimal_row('boolq:model=foo'), 'suite_version': 'v0.3.0', 'public_track': 'main'},
    ]
    df = pd.DataFrame(rows)
    index_fpath = tmp_path / 'idx.csv'
    df.to_csv(index_fpath, index=False)

    out_dpath = tmp_path / 'analysis'
    summary = analyze_index_snapshot(index_fpath=index_fpath, out_dpath=out_dpath)

    assert summary['has_run_spec_hash'] is False
    assert summary['n_run_names_with_hash_drift'] == 0

    drift = pd.read_csv(out_dpath / 'index_snapshot_version_drift.latest.csv')
    assert len(drift) == 0

    # But duplicate detection still works from suite_version
    dups = pd.read_csv(out_dpath / 'index_snapshot_duplicates_by_run_name.latest.csv')
    assert len(dups) == 1
    assert int(dups.iloc[0]['n_suite_versions']) == 2
