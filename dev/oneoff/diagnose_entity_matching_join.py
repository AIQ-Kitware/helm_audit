#!/usr/bin/env python3
"""Gather evidence on the entity_matching official↔local hash divergence.

Symptom (per the slim heatmap dated 2026-05-01)
------------------------------------------------
For every (model, scenario=Abt_Buy) pair we have, official-side
``sample_hash`` values and local-side ``sample_hash`` values have
**zero overlap** despite ``sample_id`` overlap being 1000/1000:

    id2221 on official → "panasonic silver dect 6.0 cordless telephone"
    id2221 on local    → "samsung 19' black flat panel series 6 lcd hdtv"

Same id, completely different examples.

Upstream context (HELM source, not our code)
--------------------------------------------
[`entity_matching_scenario_fixed_random_state.py`](../../submodules/helm/src/helm/benchmark/scenarios/entity_matching_scenario_fixed_random_state.py)
documents the issue from HELM's side:

    Unfortunately, the previous official HELM runs did not initialize
    the numpy random state to zero, so future runs must use the same
    random states in order to sample the same test instances to
    reproduce the official HELM runs.

The fix is a JSON file of canned numpy random states (one per dataset),
fetched at scenario-construction time. The official-side EEE artifact
in our public store predates the fix; the local-side is from our audit
runs, which use whichever HELM version was installed at run time.

This script does not draw conclusions. It gathers evidence so we can.

What it asks
------------
Q1. Are the two sides showing the *same* test examples (id-relabel /
    shuffle) or *different* test examples (disjoint or partial-overlap
    selection)?
    → Hash ``input.raw`` content on each side; compute set overlap.

Q2. If shared content exists, what is the (official_id, local_id)
    permutation? Identity, deterministic shift, arbitrary?
    → Tabulate ``(off_id, loc_id)`` for the first N shared contents.

Q3. What is ``sample_hash`` actually a function of? Just ``input.raw``,
    or does it include other fields?
    → Look for content-identical records with different hashes.

Q4. For the same ``sample_id`` on both sides, does the **fewshot
    prefix** differ, or only the test pair?
    → Strip the test "Product A is..." line and compare.

Q5. Is there any HELM run-spec metadata available — sidecar
    ``run_spec.json``, scenario class, HELM version, RNG seed?
    → Walk artifact dirs and look.

Cross-model consistency
-----------------------
For id2221 across all 3 official-side conversions (Pythia / Vicuna /
Falcon), is the content the same? If yes, id2221 is a stable label
within official conversions; the divergence is purely
official-vs-local. If no, every conversion shuffles independently.

Output
------
Prints a structured text report to stdout, plus writes a JSON copy to
``--out`` if specified. Read-only; no mutations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
from collections import Counter, defaultdict


OUT_ROOT_DEFAULT = pathlib.Path(
    "/data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim"
)
BENCH = "entity_matching"
MODELS = [
    # (display, dirname org, dirname model)
    ("eleutherai/pythia-6.9b", "eleutherai", "pythia-6.9b"),
    ("lmsys/vicuna-7b-v1.3", "lmsys", "vicuna-7b-v1.3"),
    ("tiiuae/falcon-7b", "tiiuae", "falcon-7b"),
]


def _samples_dir(out_root: pathlib.Path, side: str, org: str, model: str) -> pathlib.Path:
    if side == "official":
        return out_root / "eee_artifacts" / "official" / BENCH / org / model
    return (
        out_root / "eee_artifacts" / "local" / "open-helm-models-reproducibility"
        / BENCH / org / model
    )


def _load_records(d: pathlib.Path) -> list[dict] | None:
    """Read every *_samples.jsonl in dir; return list of records (or None)."""
    files = sorted(d.glob("*_samples.jsonl"))
    if not files:
        return None
    rows: list[dict] = []
    for f in files:
        with f.open("rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    return rows


def _input_raw(rec: dict) -> str:
    """Extract the prompt string the model saw."""
    inp = rec.get("input")
    if isinstance(inp, dict):
        return inp.get("raw", "") or ""
    return str(inp or "")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _aggregate_path(d: pathlib.Path) -> pathlib.Path | None:
    """The EEE aggregate JSON (the *.json that's NOT *_samples.jsonl)."""
    for f in sorted(d.glob("*.json")):
        if not f.name.endswith("_samples.jsonl") and "_samples" not in f.stem:
            return f
    return None


def _resolve_path(p: pathlib.Path) -> pathlib.Path:
    """Follow symlinks to the real file. Useful since the heatmap link
    tree symlinks into the public store and the audit store."""
    return pathlib.Path(os.path.realpath(p))


def _split_test_pair(text: str) -> tuple[str, str]:
    """Naive split: prefix (everything up to the FINAL 'Product A is')
    vs suffix (the test pair). Used to compare fewshot stability while
    isolating the test example."""
    marker = "Product A is"
    idx = text.rfind(marker)
    if idx < 0:
        return (text, "")
    return (text[:idx], text[idx:])


def _scrape_helm_metadata(rec: dict) -> dict:
    """Look for HELM-shaped run-spec metadata embedded in the EEE record."""
    found = {}
    for k in ("metadata",):
        v = rec.get(k)
        if isinstance(v, dict):
            for sub_k in (
                "helm_version", "helm_run_spec", "scenario_class",
                "model_deployment", "max_eval_instances",
                "instructions", "run_spec_name", "random_seed",
            ):
                if sub_k in v:
                    found[sub_k] = v[sub_k]
    return found


def diagnose(out_root: pathlib.Path) -> dict:
    """Run all checks; return a structured report."""
    report: dict = {
        "out_root": str(out_root),
        "benchmark": BENCH,
        "per_model": {},
        "cross_model_official_id_consistency": {},
        "helm_source_evidence": _helm_source_evidence(),
    }

    # First pass: load both sides for each model.
    loaded: dict[str, dict] = {}
    for display, org, model in MODELS:
        off_dir = _samples_dir(out_root, "official", org, model)
        loc_dir = _samples_dir(out_root, "local", org, model)
        off = _load_records(off_dir) if off_dir.exists() else None
        loc = _load_records(loc_dir) if loc_dir.exists() else None
        loaded[display] = {
            "off_dir": off_dir, "loc_dir": loc_dir,
            "off": off, "loc": loc,
            "off_aggregate_path": (
                _resolve_path(_aggregate_path(off_dir))
                if off_dir.exists() and _aggregate_path(off_dir) else None
            ),
            "loc_aggregate_path": (
                _resolve_path(_aggregate_path(loc_dir))
                if loc_dir.exists() and _aggregate_path(loc_dir) else None
            ),
        }

    # Per-model analysis.
    for display, org, model in MODELS:
        d = loaded[display]
        off, loc = d["off"], d["loc"]
        per: dict = {
            "off_dir": str(d["off_dir"]),
            "loc_dir": str(d["loc_dir"]),
            "off_aggregate_real_path": str(d["off_aggregate_path"]) if d["off_aggregate_path"] else None,
            "loc_aggregate_real_path": str(d["loc_aggregate_path"]) if d["loc_aggregate_path"] else None,
        }
        if not off or not loc:
            per["status"] = "missing_one_side"
            report["per_model"][display] = per
            continue

        per["n_off_records"] = len(off)
        per["n_loc_records"] = len(loc)

        # Q1. Content overlap.
        off_by_content: dict[str, list[dict]] = defaultdict(list)
        loc_by_content: dict[str, list[dict]] = defaultdict(list)
        for r in off:
            off_by_content[_content_hash(_input_raw(r))].append(r)
        for r in loc:
            loc_by_content[_content_hash(_input_raw(r))].append(r)
        shared = set(off_by_content) & set(loc_by_content)
        only_off = set(off_by_content) - set(loc_by_content)
        only_loc = set(loc_by_content) - set(off_by_content)
        per["content_overlap"] = {
            "n_unique_off": len(off_by_content),
            "n_unique_loc": len(loc_by_content),
            "n_shared": len(shared),
            "n_only_off": len(only_off),
            "n_only_loc": len(only_loc),
        }

        # Q2. Permutation (off_id ↔ loc_id) for shared content. Pick
        # one record per content hash (any will do — content
        # determines the prompt; sample_id is the variable).
        permutation_pairs: list[tuple[str, str, str]] = []
        for ch in sorted(shared)[:50]:
            off_rec = off_by_content[ch][0]
            loc_rec = loc_by_content[ch][0]
            permutation_pairs.append((
                ch,
                str(off_rec.get("sample_id")),
                str(loc_rec.get("sample_id")),
            ))
        # Identity check across ALL shared content (not just first 50).
        identity_count = 0
        for ch in shared:
            off_id = str(off_by_content[ch][0].get("sample_id"))
            loc_id = str(loc_by_content[ch][0].get("sample_id"))
            if off_id == loc_id:
                identity_count += 1
        per["permutation"] = {
            "n_identity_off_id_eq_loc_id": identity_count,
            "n_shared_total": len(shared),
            "first_50_pairs": permutation_pairs,
        }

        # Q3. sample_hash function: for shared content, compare hashes.
        # If content is the same but hashes differ → hash depends on
        # something other than input.raw.
        same_content_diff_hash = 0
        same_content_same_hash = 0
        for ch in shared:
            off_h = off_by_content[ch][0].get("sample_hash")
            loc_h = loc_by_content[ch][0].get("sample_hash")
            if off_h and loc_h:
                if off_h == loc_h:
                    same_content_same_hash += 1
                else:
                    same_content_diff_hash += 1
        per["sample_hash_function"] = {
            "shared_content_same_hash": same_content_same_hash,
            "shared_content_diff_hash": same_content_diff_hash,
            "interpretation_note": (
                "If diff_hash > 0, sample_hash depends on more than "
                "input.raw (e.g., reference labels, sample_id, "
                "tokenization, or position-in-list). If diff_hash == 0 "
                "and shared content exists, sample_hash IS purely "
                "content-derived — the join failure is data divergence."
            ),
        }

        # Q4. Fewshot-vs-test-pair stability for SAME sample_id.
        # Build by-id indexes; for ids present on both sides, compare
        # the prefix (everything before the final "Product A is").
        off_by_id = {r.get("sample_id"): r for r in off}
        loc_by_id = {r.get("sample_id"): r for r in loc}
        shared_ids = sorted(set(off_by_id) & set(loc_by_id))[:25]
        prefix_diffs: list[dict] = []
        n_same_prefix_diff_test = 0
        n_diff_prefix = 0
        for sid in shared_ids:
            off_text = _input_raw(off_by_id[sid])
            loc_text = _input_raw(loc_by_id[sid])
            off_pre, off_test = _split_test_pair(off_text)
            loc_pre, loc_test = _split_test_pair(loc_text)
            entry = {
                "sample_id": sid,
                "prefix_equal": off_pre == loc_pre,
                "test_pair_equal": off_test == loc_test,
                "off_test_pair_excerpt": off_test[:120],
                "loc_test_pair_excerpt": loc_test[:120],
            }
            prefix_diffs.append(entry)
            if off_pre == loc_pre and off_test != loc_test:
                n_same_prefix_diff_test += 1
            elif off_pre != loc_pre:
                n_diff_prefix += 1
        per["prompt_stability"] = {
            "n_shared_ids_examined": len(shared_ids),
            "n_same_prefix_diff_test": n_same_prefix_diff_test,
            "n_diff_prefix": n_diff_prefix,
            "first_5_examples": prefix_diffs[:5],
            "interpretation_note": (
                "n_same_prefix_diff_test > 0 → fewshot pool is stable, "
                "test pair selection is non-deterministic (HELM RNG "
                "seed difference, narrowly). "
                "n_diff_prefix > 0 → the WHOLE prompt is shuffled "
                "(fewshot pool selection is also non-deterministic)."
            ),
        }

        # Q5. HELM-shaped metadata in EEE records.
        if off:
            per["off_first_record_metadata_scrape"] = _scrape_helm_metadata(off[0])
        if loc:
            per["loc_first_record_metadata_scrape"] = _scrape_helm_metadata(loc[0])

        # Q5b. Sidecar run_spec.json next to either aggregate file?
        for label, agg_path in (
            ("off_sidecar_run_spec_json", d["off_aggregate_path"]),
            ("loc_sidecar_run_spec_json", d["loc_aggregate_path"]),
        ):
            sidecar = None
            if agg_path:
                cand = agg_path.parent / "run_spec.json"
                if cand.exists():
                    try:
                        sidecar = json.loads(cand.read_text())
                    except Exception as e:
                        sidecar = {"_parse_error": str(e)}
            per[label] = sidecar

        report["per_model"][display] = per

    # Cross-model official-side: does id2221 mean the same thing
    # across all 3 official conversions? If yes, official ids are
    # stable within their corpus and divergence is purely
    # official-vs-local. If no, every conversion shuffles.
    sample_ids_to_check = ["id2221", "id2857", "id3000", "id1500"]
    for sid in sample_ids_to_check:
        per_model_content_hash = {}
        for display, _, _ in MODELS:
            off = loaded[display]["off"]
            if not off:
                continue
            for r in off:
                if r.get("sample_id") == sid:
                    per_model_content_hash[display] = {
                        "content_hash": _content_hash(_input_raw(r)),
                        "input_excerpt": _input_raw(r)[:160],
                        "sample_hash": r.get("sample_hash"),
                    }
                    break
        report["cross_model_official_id_consistency"][sid] = per_model_content_hash

    return report


def _helm_source_evidence() -> dict:
    """Read the HELM source-side files that document the upstream RNG fix."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    fixed = repo_root / "submodules/helm/src/helm/benchmark/scenarios/entity_matching_scenario_fixed_random_state.py"
    main = repo_root / "submodules/helm/src/helm/benchmark/scenarios/entity_matching_scenario.py"
    out: dict = {
        "exists_fixed_random_state_module": fixed.exists(),
        "exists_main_scenario_module": main.exists(),
    }
    if fixed.exists():
        text = fixed.read_text()
        # Extract the docstring that admits the original non-determinism.
        marker = '"""'
        chunks = text.split(marker)
        if len(chunks) >= 3:
            out["fixed_random_state_module_docstring"] = chunks[1].strip()
        # Extract the FIXED_RANDOM_SEED_URL constant.
        for line in text.splitlines():
            if "_FIXED_RANDOM_SEED_URL" in line and "=" in line:
                out["fixed_random_seed_url_line"] = line.strip()
                break
    if main.exists():
        text = main.read_text()
        for line in text.splitlines():
            if "set_fixed_random_state_for_dataset" in line:
                out.setdefault("main_scenario_seed_setter_calls", []).append(line.strip())
    return out


def render_report(rep: dict) -> str:
    """Render the structured report as readable text."""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("entity_matching official↔local hash divergence — evidence dump")
    lines.append("=" * 78)
    lines.append(f"out_root:  {rep['out_root']}")
    lines.append(f"benchmark: {rep['benchmark']}")
    lines.append("")

    # HELM source evidence
    lines.append("--- HELM source evidence ---")
    he = rep.get("helm_source_evidence", {})
    for k, v in he.items():
        if isinstance(v, list):
            lines.append(f"  {k}:")
            for x in v:
                lines.append(f"    - {x}")
        else:
            lines.append(f"  {k}: {v}")
    lines.append("")

    # Per-model evidence
    for display, per in rep["per_model"].items():
        lines.append("=" * 78)
        lines.append(f"model: {display}")
        lines.append("=" * 78)
        if per.get("status") == "missing_one_side":
            lines.append(f"  off_dir: {per['off_dir']}")
            lines.append(f"  loc_dir: {per['loc_dir']}")
            lines.append(f"  status:  one side missing; skipping deep checks")
            lines.append("")
            continue
        lines.append(f"  off records: {per['n_off_records']}")
        lines.append(f"  loc records: {per['n_loc_records']}")
        lines.append(f"  off real path: {per.get('off_aggregate_real_path')}")
        lines.append(f"  loc real path: {per.get('loc_aggregate_real_path')}")
        lines.append("")
        co = per["content_overlap"]
        lines.append(f"  Q1. content (input.raw) overlap")
        lines.append(f"      unique on off:   {co['n_unique_off']}")
        lines.append(f"      unique on loc:   {co['n_unique_loc']}")
        lines.append(f"      shared:          {co['n_shared']}")
        lines.append(f"      only-off:        {co['n_only_off']}")
        lines.append(f"      only-loc:        {co['n_only_loc']}")
        lines.append("")
        pe = per["permutation"]
        lines.append(f"  Q2. id permutation (for shared content)")
        lines.append(f"      shared total:                       {pe['n_shared_total']}")
        lines.append(f"      where off sample_id == loc sample_id: {pe['n_identity_off_id_eq_loc_id']}")
        lines.append(f"      first 10 (off_id → loc_id) pairs:")
        for ch, off_id, loc_id in pe["first_50_pairs"][:10]:
            lines.append(f"        {off_id:>10s} → {loc_id:<10s}  (content #{ch[:8]})")
        lines.append("")
        sh = per["sample_hash_function"]
        lines.append(f"  Q3. sample_hash function (shared content)")
        lines.append(f"      same content + same hash:  {sh['shared_content_same_hash']}")
        lines.append(f"      same content + diff hash:  {sh['shared_content_diff_hash']}")
        lines.append(f"      → {sh['interpretation_note']}")
        lines.append("")
        ps = per["prompt_stability"]
        lines.append(f"  Q4. fewshot vs test-pair stability (same sample_id)")
        lines.append(f"      examined ids:               {ps['n_shared_ids_examined']}")
        lines.append(f"      same prefix, diff test pair: {ps['n_same_prefix_diff_test']}")
        lines.append(f"      different prefix:            {ps['n_diff_prefix']}")
        lines.append(f"      → {ps['interpretation_note']}")
        lines.append("      first example:")
        if ps["first_5_examples"]:
            ex = ps["first_5_examples"][0]
            lines.append(f"        sample_id:        {ex['sample_id']}")
            lines.append(f"        prefix_equal:     {ex['prefix_equal']}")
            lines.append(f"        test_pair_equal:  {ex['test_pair_equal']}")
            lines.append(f"        off test excerpt: {ex['off_test_pair_excerpt']!r}")
            lines.append(f"        loc test excerpt: {ex['loc_test_pair_excerpt']!r}")
        lines.append("")
        lines.append(f"  Q5. HELM-shaped metadata in EEE records")
        lines.append(f"      off scrape: {per.get('off_first_record_metadata_scrape')}")
        lines.append(f"      loc scrape: {per.get('loc_first_record_metadata_scrape')}")
        lines.append(f"      off sidecar run_spec.json: {per.get('off_sidecar_run_spec_json')}")
        lines.append(f"      loc sidecar run_spec.json: {per.get('loc_sidecar_run_spec_json')}")
        lines.append("")

    # Cross-model official-side consistency
    lines.append("=" * 78)
    lines.append("cross-model official-side id consistency")
    lines.append("=" * 78)
    cmc = rep.get("cross_model_official_id_consistency", {})
    for sid, per_model in cmc.items():
        lines.append(f"  {sid}:")
        for model, info in per_model.items():
            lines.append(f"    {model}")
            lines.append(f"      content_hash: {info['content_hash']}")
            lines.append(f"      excerpt:      {info['input_excerpt']!r}")
            lines.append(f"      sample_hash:  {info['sample_hash']}")
        # Are all content hashes equal across models?
        chs = {info["content_hash"] for info in per_model.values()}
        lines.append(f"    distinct content hashes across {len(per_model)} models: {len(chs)}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-root", type=pathlib.Path, default=OUT_ROOT_DEFAULT,
                        help=f"Heatmap OUT_ROOT (default: {OUT_ROOT_DEFAULT})")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="Optional path to dump structured JSON in addition to printing.")
    args = parser.parse_args()

    rep = diagnose(args.out_root)
    print(render_report(rep))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rep, indent=2, default=str) + "\n")
        print(f"\nWrote structured report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
