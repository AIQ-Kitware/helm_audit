"""
Tests for the official/public HELM index scan functions.

Covers:
1. run_spec_hash is stable for identical specs regardless of key order
2. Structural junk entries (groups/confs/logs) are classified correctly
3. _scan_benchmark_output_dir captures public_track and suite_version
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_audit.cli.index_historic_helm_runs import (
    KNOWN_STRUCTURAL_JUNK_NAMES,
    _classify_run_entry,
    _compute_run_spec_hash,
    _normalize_for_hash,
    _scan_benchmark_output_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_spec(run_name: str, model: str = 'test/model') -> dict:
    return {
        'name': run_name,
        'adapter_spec': {'model': model},
        'scenario_spec': {'class_name': 'helm.TestScenario'},
    }


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
            (entry_dir / 'run_spec.json').write_text(json.dumps(_make_run_spec(run_name)))
    for junk in (junk_names or []):
        (suite_dir / junk).mkdir(parents=True, exist_ok=True)
    return bo_dir


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
    fa = tmp_path / 'a.json'
    fb = tmp_path / 'b.json'
    fa.write_text(json.dumps(_make_run_spec('boolq:model=foo', model='foo')))
    fb.write_text(json.dumps(_make_run_spec('boolq:model=bar', model='bar')))
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
    bo_dir = tmp_path / 'c' / 'benchmark_output'
    for sv, rn in [('v0.2.2', 'run_a:model=foo'), ('v0.3.0', 'run_b:model=foo')]:
        (bo_dir / 'runs' / sv / rn).mkdir(parents=True, exist_ok=True)

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
    assert _scan_benchmark_output_dir(bo_dir, str(tmp_path), 'main', '*', '') == []


def test_scan_missing_runs_dir(tmp_path):
    bo_dir = tmp_path / 'benchmark_output'
    bo_dir.mkdir(parents=True)
    assert _scan_benchmark_output_dir(bo_dir, str(tmp_path), 'main', '*', '') == []
