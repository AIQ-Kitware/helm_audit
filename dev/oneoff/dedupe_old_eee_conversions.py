#!/usr/bin/env python3
"""
Delete superseded EEE aggregate JSONs (and their ``_samples.jsonl``
siblings) so each HELM run dir keeps exactly one — the newest by
``retrieved_timestamp``.

Why this exists
---------------
The public ``crfm-helm-public-eee-test/`` store has been
re-converted multiple times as ``every_eval_ever convert helm``
matured. The older runs left their output files behind alongside
the new ones, so a typical artifact directory now contains:

    <eee_output_root>/<bench>/<dev>/<model>/
        80d21175-...json              # old conversion (1 record/sample,
        80d21175-..._samples.jsonl    #   evaluation_result_id=None,
                                      #   collapsed metrics)
        a1b2c3d4-...json              # mid-period conversion
        a1b2c3d4-..._samples.jsonl
        eabfbb59-...json              # newest conversion (per-metric
        eabfbb59-..._samples.jsonl    #   records, full schema)

The ``EeeArtifactLoader`` picks the newest by
``retrieved_timestamp`` at load time, so the old files are
*usually* invisible. They become a problem when downstream tooling
(our heatmap link tree builder; see
``reproduce/eee_only_reproducibility_heatmap/10_link_tree.sh``)
ends up pointing at one of the *older* files instead of the newest
— silently producing schema-mismatched joins that drop all rows.

Cleanup is the right fix here: keep the newest aggregate per dir,
delete the rest. The newer-format files have everything the older
ones did and more, so nothing is lost. Disk usage drops too — the
older single-record-per-sample files coexist with new files that
have ~21x as many records, but together they're roughly 22x the
needed volume.

Default behavior
----------------
**Dry-run.** This script defaults to ``--dry-run`` and only prints
what it *would* delete. To actually delete, pass ``--apply``.
There is no recovery — the deletions go straight to ``unlink()``,
not the trash. Always inspect the dry-run output first.

Heuristics for "which file is newest"
-------------------------------------
Read each ``<uuid>.json``'s top-level ``retrieved_timestamp``
field and pick the one with the maximum value. Tie-break on
filename for determinism.

If a candidate file fails to parse as JSON, it is **kept** (not
deleted) and a warning is printed; we won't touch broken data.

Targets
-------
By default the script walks ``crfm-helm-public-eee-test/`` and
processes every ``eee_output/.../<bench>/<dev>/<model>/`` directory
that has more than one aggregate JSON. The same logic applies to
``eee/local/``, but local artifacts are usually one-per-dir already
so passing ``--root <local-root>`` is a no-op in practice — but
still safe to run.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Rich is a transitive dep of helm_audit (used throughout the eval_audit
# package). Importing it here lets us render clickable file links in the
# dry-run output, which is the whole point of this oneoff being usable
# for spot-checking before --apply. If rich isn't available for some
# reason we degrade to plain ``print``.
try:
    from rich import print as _rich_print
    _HAVE_RICH = True
except ImportError:  # pragma: no cover - rich is always installed in this repo
    _rich_print = print  # type: ignore[assignment]
    _HAVE_RICH = False


def _link(path: Path, *, label: str | None = None) -> str:
    """Return rich-markup linking to ``path`` so the terminal can open it.

    Mirrors ``eval_audit.infra.logging.rich_link`` but inlined so this
    oneoff doesn't pull in package internals. The link target is the
    absolute resolved path as a ``file://`` URI so terminals that
    honor OSC 8 (kitty, iTerm2, modern Wezterm) make it clickable.
    The displayed label defaults to the unresolved input — usually the
    short filename — so output stays compact.
    """
    display = label if label is not None else str(path)
    if not _HAVE_RICH:
        return display
    target = path.expanduser().resolve().as_uri()
    return f"[link={target}]{display}[/link]"


# Files that are NEVER candidates for deletion. The store sometimes
# contains bookkeeping JSONs alongside aggregates; we don't touch them.
_NEVER_DELETE_NAMES = frozenset({
    "status.json",
    "provenance.json",
    "fixture_manifest.json",
})


# ---------------------------------------------------------------------------
# Paper scope: which (model, benchmark) cells we actually care about right now
#
# The cleanup is intentionally narrow to start: it's the set of models and
# benchmark families the in-progress paper / heatmap depends on. Anything
# outside this scope is left alone for a later, broader sweep. Pass
# --paper-scope to apply the filter; pass --all-suites to override.
#
# Slug forms match how HELM names its run directories on disk:
#     <bench>:<sub-args>,model=<model_slug>,...
# We match by *substring* on the run-dir name (which is also the directory
# whose ancestor walk reaches our aggregate JSONs).
# ---------------------------------------------------------------------------

_PAPER_MODEL_SLUGS: tuple[str, ...] = (
    "eleutherai_pythia-2.8b-v0",
    "eleutherai_pythia-6.9b",
    "lmsys_vicuna-7b-v1.3",
    "qwen_qwen2.5-7b-instruct-turbo",
    "openai_gpt-oss-20b",
)

_PAPER_BENCHMARK_PREFIXES: tuple[str, ...] = (
    # Pythia / Vicuna heatmap (14 families)
    "boolq",
    "civil_comments",
    "entity_data_imputation",
    "entity_matching",
    "gsm",
    "imdb",
    "lsat_qa",
    "mmlu",   # also covers mmlu:subject=... ; mmlu_pro is a *different* prefix below
    "narrative_qa",
    "narrativeqa",  # belt-and-suspenders for the EEE typo'd name
    "quac",
    "synthetic_reasoning",
    "synthetic_reasoning_natural",
    "sythetic_reasoning_natural",  # the well-known dataset-name typo we preserve
    "truthful_qa",
    "wikifact",
    # Qwen lite v1.9.0 add-ons (already partially covered above)
    "commonsense",
    "legalbench",
    "math",
    "med_qa",
    "natural_qa",
    "wmt_14",
    # gpt-oss capabilities/safety
    "ifeval",
    "mmlu_pro",
    "bbq",
)


def _path_matches_paper_scope(run_dir_name: str) -> bool:
    """Return True if ``run_dir_name`` (e.g.
    ``mmlu:subject=us_foreign_policy,...,model=eleutherai_pythia-6.9b,...``)
    targets a paper-scope (model, benchmark) combination.
    """
    # Run dir names start with the benchmark followed by ':'; we
    # match the prefix up to the first ':' or ',' (whichever ends the
    # benchmark name). For benchmarks whose canonical name *contains*
    # a comma (e.g. ``legal_support,method=...``) we still match the
    # literal benchmark prefix because we check ``startswith``.
    name = run_dir_name
    bench_ok = any(
        name == prefix
        or name.startswith(f"{prefix}:")
        or name.startswith(f"{prefix},")
        for prefix in _PAPER_BENCHMARK_PREFIXES
    )
    if not bench_ok:
        return False
    # Model match: slug appears anywhere after the benchmark prefix.
    model_ok = any(slug in name for slug in _PAPER_MODEL_SLUGS)
    return model_ok


@dataclass(frozen=True)
class AggregateCandidate:
    """One ``<uuid>.json`` file with its parsed timestamp + sibling samples path."""

    aggregate: Path
    samples: Path | None
    retrieved_timestamp: float | None
    parse_error: str | None = None

    @property
    def total_bytes(self) -> int:
        size = self.aggregate.stat().st_size if self.aggregate.exists() else 0
        if self.samples is not None and self.samples.exists():
            size += self.samples.stat().st_size
        return size


@dataclass
class DirGroup:
    """All aggregate candidates that live in one EEE output dir."""

    dir_path: Path
    candidates: list[AggregateCandidate] = field(default_factory=list)


def _is_aggregate_json(p: Path) -> bool:
    """A non-bookkeeping ``.json`` that isn't a samples file."""
    if p.suffix != ".json":
        return False
    if p.name in _NEVER_DELETE_NAMES:
        return False
    if p.name.endswith("_samples.jsonl"):
        return False
    return True


def _eee_output_root(aggregate: Path) -> Path | None:
    """Walk up from ``aggregate`` and return the ``eee_output`` ancestor
    if one exists, else None.

    Older every_eval_ever converters had a bug where the aggregate
    JSON and its ``_samples.jsonl`` could be written into *sibling*
    subtrees of ``eee_output`` (the aggregate under ``unknown/...``
    while samples landed under the actual benchmark name). To find
    the samples reliably we have to widen the search to the whole
    ``eee_output`` subtree, not just ``aggregate.parent``.
    """
    for ancestor in aggregate.parents:
        if ancestor.name == "eee_output":
            return ancestor
    return None


def _samples_sibling(aggregate: Path) -> Path | None:
    """Locate the ``<stem>_samples.jsonl`` for ``aggregate``.

    Tries (in order):
      1. Same directory — the modern layout.
      2. Anywhere within the same ``eee_output`` subtree, matching by
         filename. Catches the older converter that split aggregate
         and samples into ``eee_output/unknown/.../`` and
         ``eee_output/<bench>/.../`` respectively.
    Returns the first match it finds, or None.
    """
    expected_name = aggregate.stem + "_samples.jsonl"
    same_dir = aggregate.with_name(expected_name)
    if same_dir.is_file():
        return same_dir
    eee_root = _eee_output_root(aggregate)
    if eee_root is None:
        return None
    for candidate in eee_root.rglob(expected_name):
        if candidate.is_file():
            return candidate
    return None


def _load_candidate(aggregate: Path) -> AggregateCandidate:
    """Read ``aggregate``'s ``retrieved_timestamp`` (best-effort)."""
    try:
        data = json.loads(aggregate.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return AggregateCandidate(
            aggregate=aggregate,
            samples=_samples_sibling(aggregate),
            retrieved_timestamp=None,
            parse_error=str(exc),
        )
    ts_raw = data.get("retrieved_timestamp")
    try:
        ts = float(ts_raw) if ts_raw is not None else None
    except (TypeError, ValueError):
        ts = None
    return AggregateCandidate(
        aggregate=aggregate,
        samples=_samples_sibling(aggregate),
        retrieved_timestamp=ts,
    )


def _find_helm_run_dir_name(eee_aggregate_dir: Path, root: Path) -> str | None:
    """For an aggregate JSON living at
    ``<root>/<...>/<helm_run_dir>/eee_output/<bench>/<dev>/<model>/``
    return ``<helm_run_dir>``'s name, i.e. the HELM run-spec slug
    (``mmlu:subject=...,model=...``).

    We walk up from ``eee_aggregate_dir`` looking for an ``eee_output``
    component; the directory immediately above ``eee_output`` is the
    HELM run dir whose name we use to match against scope filters.
    Returns None if the layout doesn't match.
    """
    for ancestor in eee_aggregate_dir.parents:
        if ancestor == root:
            return None
        if ancestor.name == "eee_output":
            return ancestor.parent.name
    return None


def find_groups(
    root: Path,
    *,
    paper_scope: bool = False,
) -> Iterable[DirGroup]:
    """Yield every directory under ``root`` that contains 2+ aggregate JSONs.

    Single-aggregate dirs are skipped — there's nothing to clean up.

    When ``paper_scope`` is True, also skip groups whose enclosing HELM
    run dir name doesn't match a paper-scope (model, benchmark) pair.
    """
    by_dir: dict[Path, list[Path]] = defaultdict(list)
    for p in root.rglob("*.json"):
        if not _is_aggregate_json(p):
            continue
        by_dir[p.parent].append(p)
    for dir_path in sorted(by_dir):
        aggregates = sorted(by_dir[dir_path])
        if len(aggregates) < 2:
            continue
        if paper_scope:
            run_dir_name = _find_helm_run_dir_name(dir_path, root)
            if run_dir_name is None or not _path_matches_paper_scope(run_dir_name):
                continue
        candidates = [_load_candidate(a) for a in aggregates]
        yield DirGroup(dir_path=dir_path, candidates=candidates)


def select_keeper(group: DirGroup) -> tuple[AggregateCandidate, list[AggregateCandidate]]:
    """Return ``(keeper, to_delete)`` for one directory group.

    Rules:
      1. If any candidate had a parse error, refuse to delete *anything*
         in this group — keep all of them and surface a warning. Bad
         data shouldn't get cascade-deleted on top of itself.
      2. Otherwise, the keeper is the candidate with the maximum
         ``retrieved_timestamp``. Ties broken by aggregate path
         (lexicographic ascending) for determinism.
      3. Candidates with ``retrieved_timestamp == None`` are demoted
         below all timestamped ones — they're old enough that the
         field wasn't yet emitted.
    """
    if any(c.parse_error for c in group.candidates):
        return group.candidates[0], []  # signal handled by the caller

    def sort_key(c: AggregateCandidate) -> tuple[int, float, str]:
        # First component: 1 if timestamped, 0 if not (sort timestamped first)
        # Second: timestamp itself
        # Third: path string (tie-break)
        has_ts = 1 if c.retrieved_timestamp is not None else 0
        ts = c.retrieved_timestamp if c.retrieved_timestamp is not None else 0.0
        return (has_ts, ts, str(c.aggregate))

    ranked = sorted(group.candidates, key=sort_key, reverse=True)
    keeper = ranked[0]
    to_delete = ranked[1:]
    return keeper, to_delete


def _format_ts(ts: float | None) -> str:
    if ts is None:
        return "(no ts)"
    return f"{int(ts)}"


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f}PB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default="/data/crfm-helm-audit-store/crfm-helm-public-eee-test",
        help="Root to walk. Anything below containing aggregate <uuid>.json "
             "files will be inspected. Default is the public CRFM EEE store.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the superseded files. Without this flag the "
             "script runs in dry-run mode and only prints what it WOULD do.",
    )
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--paper-scope",
        action="store_true",
        default=True,
        help="Restrict cleanup to (model, benchmark) combinations the "
             "paper / heatmap depends on. Default. See _PAPER_MODEL_SLUGS "
             "and _PAPER_BENCHMARK_PREFIXES at the top of this file.",
    )
    scope_group.add_argument(
        "--all-suites",
        dest="paper_scope",
        action="store_false",
        help="Do not filter by paper scope. Touch every duplicate "
             "aggregate under --root. Use only when you've already "
             "validated paper-scope behavior.",
    )
    parser.add_argument(
        "--include-no-timestamp-only",
        action="store_true",
        help="Only consider directories where ALL files lack "
             "retrieved_timestamp as candidates for deletion. Useful for a "
             "very conservative first pass — it never picks a winner among "
             "equally-old files, so this flag effectively becomes a no-op "
             "and is mostly here for symmetry with the analysis surface.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after processing this many groups. Useful for spot-checks.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-group output; only print the final summary.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"FAIL: --root {root} is not a directory", file=sys.stderr)
        return 2

    if args.apply:
        mode_markup = "[bold white on red] APPLY [/bold white on red] [red]deletions WILL HAPPEN[/red]"
    else:
        mode_markup = "[bold black on yellow] DRY RUN [/bold black on yellow] [yellow]no files will be touched[/yellow]"
    scope = "paper-scope only" if args.paper_scope else "ALL suites"
    _rich_print(f"Mode:  {mode_markup}")
    _rich_print(f"Root:  [cyan]{_link(root)}[/cyan]")
    _rich_print(f"Scope: [magenta]{scope}[/magenta]")
    if args.paper_scope:
        _rich_print(f"  [dim]models:[/dim] {', '.join(_PAPER_MODEL_SLUGS)}")
        _rich_print(f"  [dim]bench prefixes:[/dim] {', '.join(_PAPER_BENCHMARK_PREFIXES)}")
    print()

    n_groups_seen = 0
    n_groups_with_warnings = 0
    n_files_to_delete = 0
    n_bytes_to_delete = 0
    n_files_actually_deleted = 0
    warnings: list[str] = []

    for group in find_groups(root, paper_scope=args.paper_scope):
        if args.limit is not None and n_groups_seen >= args.limit:
            break
        n_groups_seen += 1

        # Refuse the whole group if any aggregate failed to parse.
        if any(c.parse_error for c in group.candidates):
            n_groups_with_warnings += 1
            warning = (
                f"SKIP {group.dir_path}: "
                f"{sum(1 for c in group.candidates if c.parse_error)} of "
                f"{len(group.candidates)} aggregates failed to parse — "
                f"keeping all to avoid cascading bad data"
            )
            warnings.append(warning)
            if not args.quiet:
                _rich_print(
                    f"[yellow]SKIP[/yellow] {_link(group.dir_path)}: "
                    f"{sum(1 for c in group.candidates if c.parse_error)} of "
                    f"{len(group.candidates)} aggregates failed to parse — "
                    f"[dim]keeping all to avoid cascading bad data[/dim]"
                )
            continue

        keeper, to_delete = select_keeper(group)
        if not to_delete:
            continue

        if args.include_no_timestamp_only and any(
            c.retrieved_timestamp is not None for c in group.candidates
        ):
            continue

        rel = group.dir_path.relative_to(root)
        if not args.quiet:
            def _format_files(cand: AggregateCandidate) -> str:
                """Render the aggregate + (optional) samples sibling as
                two side-by-side clickable links. Same formatting for
                KEEP and DELETE so the eye can pattern-match what was
                kept against what was dropped without having to count
                columns.
                """
                parts = [_link(cand.aggregate, label=cand.aggregate.name)]
                if cand.samples is not None:
                    parts.append(_link(cand.samples, label=cand.samples.name))
                return " + ".join(parts)

            # Header: clickable bold-cyan group dir.
            _rich_print(f"[bold cyan]{_link(group.dir_path, label=str(rel))}[/bold cyan]")
            # KEEP: green + same +samples notation as DELETE so the user
            # can verify visually that the keeper retains its samples
            # sibling.
            _rich_print(
                f"  [bold green]KEEP  [/bold green] "
                f"{_format_files(keeper)}  "
                f"[dim]ts={_format_ts(keeper.retrieved_timestamp)}  "
                f"size={_human_bytes(keeper.total_bytes)}[/dim]"
            )
            for cand in to_delete:
                _rich_print(
                    f"  [bold red]DELETE[/bold red] "
                    f"{_format_files(cand)}  "
                    f"[dim]ts={_format_ts(cand.retrieved_timestamp)}  "
                    f"size={_human_bytes(cand.total_bytes)}[/dim]"
                )

        for cand in to_delete:
            n_files_to_delete += 1 + (1 if cand.samples is not None else 0)
            n_bytes_to_delete += cand.total_bytes
            if args.apply:
                try:
                    cand.aggregate.unlink()
                    n_files_actually_deleted += 1
                    if cand.samples is not None and cand.samples.exists():
                        cand.samples.unlink()
                        n_files_actually_deleted += 1
                except OSError as exc:
                    msg = f"FAIL: could not delete {cand.aggregate}: {exc}"
                    warnings.append(msg)
                    print(msg, file=sys.stderr)

    print()
    _rich_print("[dim]" + "-" * 60 + "[/dim]")
    _rich_print(f"Groups inspected:        [cyan]{n_groups_seen}[/cyan]")
    skipped_style = "yellow" if n_groups_with_warnings else "dim"
    _rich_print(f"Groups skipped (warn):   [{skipped_style}]{n_groups_with_warnings}[/{skipped_style}]")
    _rich_print(f"Files queued for delete: [bold red]{n_files_to_delete}[/bold red]")
    _rich_print(f"Bytes queued for delete: [bold red]{_human_bytes(n_bytes_to_delete)}[/bold red]")
    if args.apply:
        ok_style = "bold green" if n_files_actually_deleted == n_files_to_delete else "yellow"
        _rich_print(f"Files actually deleted:  [{ok_style}]{n_files_actually_deleted}[/{ok_style}]")
    else:
        print()
        _rich_print(
            "[bold black on yellow] DRY RUN [/bold black on yellow] "
            "[yellow]This was a dry run. To actually delete, re-run with "
            "[bold]--apply[/bold].[/yellow]"
        )
    if warnings:
        print()
        _rich_print(f"[yellow]Warnings ({len(warnings)}):[/yellow]")
        for w in warnings[:10]:
            _rich_print(f"  [yellow]- {w}[/yellow]")
        if len(warnings) > 10:
            _rich_print(f"  [dim]... and {len(warnings) - 10} more (suppressed).[/dim]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
