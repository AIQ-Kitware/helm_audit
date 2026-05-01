#!/usr/bin/env python3
"""Rewrite the source_organization_name tag from helm_audit_local to
eval_audit_local in already-converted local EEE artifacts.

Background: the helm_audit -> eval_audit package rename also flipped
the source-org tag baked into every locally-converted EEE log. New
conversions write "eval_audit_local"; old artifacts on disk still say
"helm_audit_local". This script ports the existing artifacts so
downstream code that groups by source_organization_name sees a
consistent value across old and new runs.

Scope (only these are touched):

  - <root>/**/eee_output/**/*.json
        EEE aggregate JSONs written from a Pydantic dump; the field
        appears as `"source_organization_name": "helm_audit_local"`.
  - <root>/**/reproduce.sh
        per-conversion shell script with `--source_organization_name
        helm_audit_local` baked into the every_eval_ever invocation.

Not touched: status.json, provenance.json, experiment summaries, and
any other archived JSON whose `helm_audit` strings are historical
records (paths, command-lines) rather than the source-org tag.

Default root is /data/crfm-helm-audit-store/eee/local. Pass --root to
scan elsewhere (repeatable).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OLD_TAG = "helm_audit_local"
NEW_TAG = "eval_audit_local"

DEFAULT_ROOTS = ["/data/crfm-helm-audit-store/eee/local"]

JSON_FIELD_RE = re.compile(
    r'("source_organization_name"\s*:\s*)"' + re.escape(OLD_TAG) + r'"'
)
JSON_FIELD_REPL = r'\1"' + NEW_TAG + r'"'


def _atomic_write(fpath: Path, text: str) -> None:
    tmp = fpath.with_suffix(fpath.suffix + ".fixup.tmp")
    tmp.write_text(text)
    tmp.replace(fpath)


def update_json_file(fpath: Path, *, dry_run: bool) -> bool:
    try:
        text = fpath.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    if OLD_TAG not in text:
        return False
    new_text, n = JSON_FIELD_RE.subn(JSON_FIELD_REPL, text)
    if n == 0:
        return False
    if not dry_run:
        _atomic_write(fpath, new_text)
    return True


def update_reproduce_script(fpath: Path, *, dry_run: bool) -> bool:
    try:
        text = fpath.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    if OLD_TAG not in text:
        return False
    new_text = text.replace(
        f"--source_organization_name {OLD_TAG}",
        f"--source_organization_name {NEW_TAG}",
    )
    if new_text == text:
        return False
    if not dry_run:
        _atomic_write(fpath, new_text)
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--root",
        action="append",
        help=f"root dir to scan (repeatable); default {DEFAULT_ROOTS[0]}",
    )
    p.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = p.parse_args()

    roots = [Path(r) for r in (args.root or DEFAULT_ROOTS)]
    n_json = n_sh = 0
    label = "would update" if args.dry_run else "updated"

    for root in roots:
        if not root.is_dir():
            print(f"skip (not a directory): {root}", file=sys.stderr)
            continue
        # JSONs under eee_output/ subtrees only.
        for fpath in root.rglob("eee_output/**/*.json"):
            if update_json_file(fpath, dry_run=args.dry_run):
                n_json += 1
                print(f"json {label}: {fpath}")
        for fpath in root.rglob("reproduce.sh"):
            if update_reproduce_script(fpath, dry_run=args.dry_run):
                n_sh += 1
                print(f"sh   {label}: {fpath}")

    print(f"\nsummary: {n_json} json {label}, {n_sh} reproduce.sh {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
