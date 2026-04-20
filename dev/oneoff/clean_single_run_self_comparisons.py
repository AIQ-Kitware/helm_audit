#!/usr/bin/env python3
"""
Remove history artifacts produced by the old --allow-single-repeat behavior
where left_run_a == left_run_b caused a meaningless self-comparison.

Detection criterion:
  report_selection_{stamp}.json has left_run_a == left_run_b
  AND single_run field is absent/None/False
  (new-code runs set single_run=True; old-code runs set neither)

What is deleted per bad stamp:
  - All  *_{stamp}.*  files in the same .history day directory
  - All  instance_samples_kwdagger_repeat_*.txt  in that same day directory
    (those come from the pair_samples call on the self-comparison, timestamps
    differ slightly from the core report stamp so we target by name not stamp)

Run dry-run first (default), then --execute to actually delete.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


STORE_ROOTS = [
    Path('/data/crfm-helm-audit-store/analysis/experiments'),
    Path('/home/joncrall/code/helm_audit/reports/core-run-analysis'),
]
STAMP_RE = re.compile(r'_(\d{8}T\d{6}Z)\.')


def _is_bad_selection(selection_fpath: Path) -> str | None:
    """Return the bad stamp string if this selection file is a self-comparison, else None."""
    try:
        data = json.loads(selection_fpath.read_text())
    except Exception:
        return None
    if data.get('single_run') is True:
        return None  # new-code run, already correct
    left_a = data.get('left_run_a', '')
    left_b = data.get('left_run_b', '')
    if left_a and left_b and left_a == left_b:
        m = STAMP_RE.search(selection_fpath.name)
        return m.group(1) if m else None
    return None


def _files_to_delete(day_dir: Path, stamp: str) -> list[Path]:
    targets = []
    # All stamped artifacts from this run
    for fpath in day_dir.iterdir():
        if fpath.is_file() and f'_{stamp}.' in fpath.name:
            targets.append(fpath)
    # kwdagger_repeat instance sample files (timestamp differs; delete by name pattern)
    for fpath in day_dir.glob('instance_samples_kwdagger_repeat_*.txt'):
        if fpath not in targets:
            targets.append(fpath)
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--execute', action='store_true', help='Actually delete files (default: dry-run).')
    parser.add_argument(
        '--root',
        action='append',
        dest='roots',
        default=[],
        help='Additional search roots (can repeat). Defaults: audit store + repo reports.',
    )
    args = parser.parse_args()

    roots = [Path(r) for r in args.roots] + [r for r in STORE_ROOTS if r.exists()]
    dry_run = not args.execute

    if dry_run:
        print('DRY RUN — pass --execute to actually delete.\n')

    total_files = 0
    total_bytes = 0
    bad_runs = 0

    for root in roots:
        for selection_fpath in sorted(root.rglob('report_selection_*.json')):
            # selection files live in .history/{YYYYMMDD}/
            day_dir = selection_fpath.parent
            if '.history' not in day_dir.parts:
                continue
            stamp = _is_bad_selection(selection_fpath)
            if stamp is None:
                continue

            targets = _files_to_delete(day_dir, stamp)
            if not targets:
                continue

            bad_runs += 1
            size = sum(f.stat().st_size for f in targets if f.exists())
            total_files += len(targets)
            total_bytes += size

            print(f'BAD  {selection_fpath.relative_to(root)}  stamp={stamp}')
            for fpath in sorted(targets):
                tag = 'DELETE' if not dry_run else 'would-delete'
                print(f'  {tag}  {fpath.name}')
                if not dry_run:
                    fpath.unlink(missing_ok=True)
            print()

    size_mb = total_bytes / 1_048_576
    action = 'Would delete' if dry_run else 'Deleted'
    print(f'{action} {total_files} files across {bad_runs} bad run(s)  ({size_mb:.1f} MB)')

    if dry_run and bad_runs > 0:
        print('\nRe-run with --execute to delete.')


if __name__ == '__main__':
    main()
