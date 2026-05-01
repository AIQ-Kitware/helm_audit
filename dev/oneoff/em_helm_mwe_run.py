#!/usr/bin/env python3
"""End-to-end reproducer: does HELM's EntityMatchingScenario emit the
exact same eval-instance ordering we captured in scenario_state.json?

Runs the HELM pipeline up through ``downsample_eval_instances`` for
``entity_matching:dataset=Abt_Buy``, exactly as ``Runner.run_one``
does in [runner.py:240-280]:

    scenario = EntityMatchingScenario("Abt_Buy")
    instances = scenario.get_instances(scenario_output_path)
    instances = with_instance_ids(instances)
    eval_subset = downsample_eval_instances(
        instances, max_eval_instances=1000, eval_splits=["valid", "test"],
    )

Then prints (rank, instance.id, instance.split,
sha256(instance.input.text)[:16]) for the eval portion. Optionally
compares this rank-by-rank against captured scenario_state.json files
from a real HELM run.

Goal: if running this script *in the user's current venv* produces an
ordering identical to the LOCAL scenario_state.json (1000/1000 ranks
matching), and running it in a pandas-2.0.3 venv produces an ordering
identical to the OFFICIAL scenario_state.json, the
pandas-merge-ordering hypothesis is end-to-end validated.

Requires: ``crfm-helm`` installed in the active environment (it
imports HELM's scenario class and runner).

Usage::

    python em_helm_mwe_run.py \\
        --scenario-output-path /tmp/em_helm_cache \\
        [--scenario-state /path/to/scenario_state.json] \\
        [--label local|official]

The ``--scenario-output-path`` is HELM's data cache root; the script
will let HELM download/unzip the deepmatcher Abt-Buy data into
``<path>/data/Abt_Buy/`` if it isn't already there.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _content_sig(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario-output-path", type=Path, required=True,
                        help="Cache dir HELM uses to download deepmatcher data.")
    parser.add_argument("--scenario-state", type=Path, default=None,
                        help="Path to a captured scenario_state.json to "
                             "diff against. If omitted, only the live "
                             "ordering is printed.")
    parser.add_argument("--label", default="captured",
                        help="Label for the comparison file (e.g. 'official' "
                             "or 'local'). Used only in printed output.")
    parser.add_argument("--max-eval-instances", type=int, default=1000)
    parser.add_argument("--eval-splits", nargs="+", default=["valid", "test"])
    parser.add_argument("--max-print", type=int, default=10,
                        help="How many ranks to print verbosely (default 10).")
    args = parser.parse_args()

    # Lazy imports — fail with a useful message if HELM isn't here.
    try:
        import pandas as pd
        import numpy as np
        from helm.benchmark.scenarios.entity_matching_scenario import (
            EntityMatchingScenario,
        )
        from helm.benchmark.scenarios.scenario import with_instance_ids
        from helm.benchmark.runner import downsample_eval_instances
    except ImportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("This script needs crfm-helm installed. "
              "Install with `pip install crfm-helm` (or `crfm-helm[scenarios]` "
              "for full scenario data deps).", file=sys.stderr)
        return 3

    print(f"# pandas={pd.__version__}  numpy={np.__version__}  "
          f"helm scenario module={EntityMatchingScenario.__module__}")
    args.scenario_output_path.mkdir(parents=True, exist_ok=True)

    # Reproduce HELM's run_one pipeline up through the downsample step.
    scenario = EntityMatchingScenario("Abt_Buy")
    instances = scenario.get_instances(str(args.scenario_output_path))
    print(f"# scenario.get_instances returned {len(instances)} instances")
    instances = with_instance_ids(instances)
    sampled = downsample_eval_instances(
        instances, args.max_eval_instances, args.eval_splits,
    )
    # downsample returns train + selected_eval; for the rank comparison
    # we want only the eval portion in the order it was sampled.
    eval_only = [i for i in sampled if i.split in args.eval_splits]
    print(f"# eval_only (post-downsample) = {len(eval_only)}")
    print()

    # Live-side rows: (rank, id, split, content_sig).
    live_rows: list[tuple[int, str, str, str]] = []
    for rank, inst in enumerate(eval_only):
        sig = _content_sig(inst.input.text)
        live_rows.append((rank, inst.id, inst.split, sig))

    # Print first N for a quick eyeball (and so the user can see at a
    # glance which dataset row landed at rank 0).
    print(f"# first {args.max_print} ranks (LIVE from scenario):")
    for rank, iid, split, sig in live_rows[:args.max_print]:
        print(f"{rank:>4d}\t{iid}\t{split}\tsig={sig}")

    # Whole-table digest: one number summarizes the ordering.
    full_digest = hashlib.sha256(
        "\n".join(f"{r}\t{iid}\t{split}\t{sig}" for r, iid, split, sig in live_rows).encode()
    ).hexdigest()
    print(f"# live_full_digest={full_digest}")

    if args.scenario_state is None:
        return 0

    # Compare against captured scenario_state.json.
    print()
    print(f"# comparing against {args.label}: {args.scenario_state}")
    if not args.scenario_state.exists():
        print(f"ERROR: {args.scenario_state} does not exist.", file=sys.stderr)
        return 4

    captured = json.loads(args.scenario_state.read_text())
    cap_rows: list[tuple[int, str, str, str]] = []
    for rank, rs in enumerate(captured.get("request_states", [])):
        inst = rs.get("instance") or {}
        text = (inst.get("input") or {}).get("text", "") or ""
        cap_rows.append((rank, inst.get("id", "?"), inst.get("split", "?"),
                         _content_sig(text)))

    captured_full_digest = hashlib.sha256(
        "\n".join(f"{r}\t{iid}\t{split}\t{sig}" for r, iid, split, sig in cap_rows).encode()
    ).hexdigest()
    print(f"# captured_full_digest={captured_full_digest}")

    n_compare = min(len(live_rows), len(cap_rows))
    n_id_match = sum(1 for a, b in zip(live_rows, cap_rows) if a[1] == b[1])
    n_split_match = sum(1 for a, b in zip(live_rows, cap_rows) if a[2] == b[2])
    n_full_match = sum(1 for a, b in zip(live_rows, cap_rows) if a == b)
    print(f"# rank-by-rank match (over {n_compare} ranks):")
    print(f"#   id match:               {n_id_match} / {n_compare}")
    print(f"#   split match:            {n_split_match} / {n_compare}")
    print(f"#   (id+split+sig) match:   {n_full_match} / {n_compare}")

    if n_full_match == n_compare:
        print(f"# VERDICT: live ordering reproduces {args.label} EXACTLY.")
        return 0

    # Show the first divergence so the user can eyeball it.
    print(f"# first divergence:")
    for i, (a, b) in enumerate(zip(live_rows, cap_rows)):
        if a != b:
            print(f"#   rank={i}")
            print(f"#     LIVE:     id={a[1]}  split={a[2]}  sig={a[3]}")
            print(f"#     {args.label.upper():<9s} id={b[1]}  split={b[2]}  sig={b[3]}")
            break
    print(f"# VERDICT: live ordering does NOT match {args.label} at the rank-by-rank level.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
