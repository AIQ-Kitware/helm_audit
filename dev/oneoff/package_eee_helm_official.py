#!/usr/bin/env python3
"""Package EEE-converted official HELM artifacts into a zipfile for sharing.

Suites supported (mapped to the local EEE store layout):

    helm_mmlu      → /data/crfm-helm-audit-store/crfm-helm-public-eee-test/mmlu/v1.13.0/
    helm_classic   → /data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.{2.2,2.3,2.4,3.0,4.0}/
    helm_air_bench → /data/crfm-helm-audit-store/crfm-helm-public-eee-test/speech/v1.0.0/air_bench_chat*

Only the EEE outputs are included: per-run aggregate ``<uuid>.json`` and
the per-instance ``<uuid>_samples.jsonl`` siblings. Converter sidecars
(``status.json``, ``provenance.json``, sweep logs, sqlite indexes)
are excluded — the recipient gets the data, not our internal
bookkeeping.

Default mode is **dry-run**: walk the tree and report file counts +
uncompressed sizes per suite, then exit without writing the zip.
Pass ``--write`` to actually create the zip. There's also a
``--max-uncompressed-gb`` guard (default 50 GB) so accidentally
zipping the 1.5 TB classic suite fails fast with an explanation.

Usage::

    # See what would be included
    python dev/oneoff/package_eee_helm_official.py \\
        --suite helm_mmlu --suite helm_air_bench

    # Actually write the zip
    python dev/oneoff/package_eee_helm_official.py \\
        --suite helm_mmlu --suite helm_air_bench \\
        --out /tmp/helm_eee_share.zip --write

    # If you really want classic (huge), raise the cap explicitly
    python dev/oneoff/package_eee_helm_official.py \\
        --suite helm_classic --out /tmp/helm_classic_eee.zip \\
        --max-uncompressed-gb 2000 --write
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


# Mapping suite-name → list of (root_dir, optional_run_spec_prefix_filter).
# ``run_spec_prefix`` constrains which immediate-child run-spec dirs of
# ``root`` are included (used for air_bench, which lives mixed in with
# the broader speech suite at v1.0.0). Empty string = no filter.
_SUITE_ROOTS: dict[str, list[tuple[str, str]]] = {
    "helm_mmlu": [
        ("/data/crfm-helm-audit-store/crfm-helm-public-eee-test/mmlu/v1.13.0", ""),
    ],
    "helm_classic": [
        ("/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.2.2", ""),
        ("/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.2.3", ""),
        ("/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.2.4", ""),
        ("/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.3.0", ""),
        ("/data/crfm-helm-audit-store/crfm-helm-public-eee-test/classic/v0.4.0", ""),
    ],
    "helm_air_bench": [
        # AIR-Bench lives in the speech suite at v1.0.0 alongside the
        # other speech benchmarks; filter by the run-spec prefix.
        ("/data/crfm-helm-audit-store/crfm-helm-public-eee-test/speech/v1.0.0", "air_bench"),
    ],
}


@dataclass
class SuitePlan:
    """Files to include for one suite, with sizes for the dry-run report."""
    suite: str
    files: list[tuple[Path, str]] = field(default_factory=list)  # (src, arcname)
    n_run_dirs: int = 0
    uncompressed_bytes: int = 0


def _human(n: int) -> str:
    """Pretty byte count."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} {units[-1]}"


def _collect_eee_files(run_dir: Path) -> list[Path]:
    """Return the *.json (excluding sidecars) + *_samples.jsonl files under
    a run-spec dir's eee_output/. Excludes converter bookkeeping."""
    eee_root = run_dir / "eee_output"
    if not eee_root.is_dir():
        return []
    out: list[Path] = []
    SIDECARS = {"status.json", "provenance.json", "fixture_manifest.json"}
    for path in eee_root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name in SIDECARS:
            continue
        if name.endswith(".json") or name.endswith(".jsonl"):
            out.append(path)
    return out


def _plan_suite(suite: str, roots: list[tuple[str, str]]) -> SuitePlan:
    """Walk every root for this suite and build a SuitePlan."""
    plan = SuitePlan(suite=suite)
    for root_text, prefix_filter in roots:
        root = Path(root_text)
        if not root.is_dir():
            print(f"  WARN: {suite}: root does not exist: {root}", file=sys.stderr)
            continue
        # Each immediate child of root is one run-spec dir
        # (e.g., "mmlu:subject=abstract_algebra,...").
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if prefix_filter and not entry.name.startswith(prefix_filter):
                continue
            files = _collect_eee_files(entry)
            if not files:
                continue
            plan.n_run_dirs += 1
            for f in files:
                # Arcname: <suite>/<version>/<run_spec>/eee_output/<...>
                # Stripping the leading absolute path keeps the zip portable.
                # We anchor on the suite root's PARENT so the version dir is
                # the first path segment under the suite, e.g.
                #   helm_mmlu/v1.13.0/mmlu:subject=...,model=.../eee_output/...
                rel_to_root = f.relative_to(root.parent)
                arcname = f"{suite}/{rel_to_root.as_posix()}"
                plan.files.append((f, arcname))
                plan.uncompressed_bytes += f.stat().st_size
    return plan


def _write_zip(out_fpath: Path, plans: list[SuitePlan], force: bool) -> None:
    if out_fpath.exists():
        if not force:
            print(f"FAIL: {out_fpath} exists; pass --force to overwrite", file=sys.stderr)
            sys.exit(2)
        out_fpath.unlink()
    out_fpath.parent.mkdir(parents=True, exist_ok=True)
    total_files = sum(len(p.files) for p in plans)
    total_bytes = sum(p.uncompressed_bytes for p in plans)
    written = 0
    written_bytes = 0
    last_log = time.time()
    print(f"Writing {total_files} files ({_human(total_bytes)} uncompressed) to {out_fpath} ...")
    # ZIP_DEFLATED gives ~10x on JSON; ZIP_LZMA is denser but ~5x slower.
    # JSON compresses well so deflate is the right default.
    with zipfile.ZipFile(out_fpath, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # Per-suite manifest at the top of each suite tree, listing the
        # included run-spec dirs. Recipient can sanity-check coverage.
        for plan in plans:
            run_dirs = sorted({Path(arc).parent.parent.parent.as_posix()
                               for _src, arc in plan.files})
            manifest_lines = [
                f"# {plan.suite} EEE artifact manifest",
                f"# generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
                f"# n_run_dirs: {plan.n_run_dirs}",
                f"# n_files: {len(plan.files)}",
                f"# uncompressed_bytes: {plan.uncompressed_bytes}",
                "",
                *run_dirs,
                "",
            ]
            zf.writestr(f"{plan.suite}/MANIFEST.txt", "\n".join(manifest_lines))
            for src, arcname in plan.files:
                zf.write(src, arcname)
                written += 1
                written_bytes += src.stat().st_size
                # Log once per ~5s so we don't spam stdout but the user
                # can tell whether large suites are progressing.
                if time.time() - last_log > 5:
                    pct = 100.0 * written / max(total_files, 1)
                    print(
                        f"  {written}/{total_files} files "
                        f"({_human(written_bytes)} of {_human(total_bytes)}, "
                        f"{pct:.1f}%)"
                    )
                    last_log = time.time()
    final_size = out_fpath.stat().st_size
    ratio = total_bytes / max(final_size, 1)
    print(
        f"\nDONE. Wrote {total_files} files. "
        f"Zip: {_human(final_size)} (compression {ratio:.1f}x from {_human(total_bytes)} uncompressed)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=sorted(_SUITE_ROOTS.keys()),
        required=True,
        help="Suite to include. Pass multiple times to include several "
        "in one zip (one MANIFEST.txt per suite at the suite root).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("./helm_eee_share.zip"),
        help="Output zipfile path (default: ./helm_eee_share.zip).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually write the zip. Without this, runs in dry-run mode "
        "(reports what would be included, then exits).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output zip if it already exists.",
    )
    parser.add_argument(
        "--max-uncompressed-gb",
        type=float,
        default=50.0,
        help=(
            "Refuse to write the zip if the total uncompressed size of "
            "selected files exceeds this many GB (default: 50). The "
            "classic suite is ~1.5 TB; pass a larger value to proceed."
        ),
    )
    args = parser.parse_args()

    print(f"Planning {len(args.suite)} suite(s) ...")
    plans: list[SuitePlan] = []
    for suite in args.suite:
        roots = _SUITE_ROOTS[suite]
        plan = _plan_suite(suite, roots)
        plans.append(plan)
        # Show roots so the user can verify the right paths were walked.
        for root_text, prefix_filter in roots:
            extra = f" (filter: starts with {prefix_filter!r})" if prefix_filter else ""
            print(f"  {suite}: {root_text}{extra}")
        print(
            f"    {plan.n_run_dirs} run-spec dirs, {len(plan.files)} files, "
            f"{_human(plan.uncompressed_bytes)} uncompressed"
        )

    total_bytes = sum(p.uncompressed_bytes for p in plans)
    total_files = sum(len(p.files) for p in plans)
    print()
    print(f"Total: {total_files} files, {_human(total_bytes)} uncompressed")

    cap_bytes = int(args.max_uncompressed_gb * (1024 ** 3))
    if total_bytes > cap_bytes:
        msg = (
            f"FAIL: total uncompressed size ({_human(total_bytes)}) exceeds "
            f"--max-uncompressed-gb={args.max_uncompressed_gb}. Pass a larger "
            "value if this is intentional. Note that classic alone is ~1.5 TB."
        )
        print(msg, file=sys.stderr)
        sys.exit(3)

    if not args.write:
        print()
        print("Dry-run only. Pass --write to actually create the zip at:")
        print(f"  {args.out.resolve()}")
        return

    _write_zip(args.out, plans, force=args.force)


if __name__ == "__main__":
    main()
