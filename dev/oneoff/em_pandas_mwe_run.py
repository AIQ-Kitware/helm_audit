#!/usr/bin/env python3
"""Minimal reproducer for the entity_matching merge-ordering question.

Runs *only* the pd.merge sequence that HELM's
``EntityMatchingScenario.read_blocked_pairs`` performs. No HELM, no EEE,
no scenario class. Just pandas operating on the deepmatcher CSVs.

Output (stdout): one signature line per merged row, in the order pandas
returned them, plus a final summary. Diffing this output between two
pandas versions tells us whether merge row-order is version-dependent
on the deepmatcher Abt-Buy data.

Per-row signature is sha256(repr(row.to_dict()))[:16] — stable across
pandas versions because dict iteration order is insertion-order in
Python 3.7+ and to_dict() preserves column order.

Usage::

    python em_pandas_mwe_run.py <data_dir> [<split>]

where ``<data_dir>`` is the unpacked Abt-Buy deepmatcher dir
(containing ``tableA.csv``, ``tableB.csv``, ``train.csv``,
``valid.csv``, ``test.csv``). ``<split>`` defaults to ``valid``.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import pandas as pd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data_dir", type=Path,
                        help="Unpacked deepmatcher Abt-Buy dir.")
    parser.add_argument("split", nargs="?", default="valid",
                        choices=["train", "valid", "test"],
                        help="Which split CSV to merge (default: valid).")
    args = parser.parse_args()

    # Library banner so the diff between runs immediately shows the
    # pandas / numpy versions that produced each output.
    import numpy as np
    print(f"# pandas={pd.__version__}  numpy={np.__version__}  "
          f"split={args.split!r}  data_dir={args.data_dir}")

    tableA = pd.read_csv(args.data_dir / "tableA.csv")
    tableB = pd.read_csv(args.data_dir / "tableB.csv")
    labels = pd.read_csv(args.data_dir / f"{args.split}.csv")

    print(f"# n_tableA={len(tableA)}  n_tableB={len(tableB)}  n_labels={len(labels)}")

    # The exact merge sequence from
    # submodules/helm/src/helm/benchmark/scenarios/entity_matching_scenario.py
    # :read_blocked_pairs.
    mergedA = pd.merge(labels, tableA, right_on="id", left_on="ltable_id")
    merged = pd.merge(
        mergedA, tableB, right_on="id", left_on="rtable_id",
        suffixes=("_A", "_B"),
    )

    print(f"# n_merged={len(merged)}")

    # Per-row signature in returned order.
    for i, (_, row) in enumerate(merged.iterrows()):
        # Use repr+to_dict so the signature is stable on any pandas
        # version (vs e.g. row.values which can have dtype-dependent
        # float repr).
        sig = hashlib.sha256(repr(row.to_dict()).encode("utf-8")).hexdigest()[:16]
        # Print ltable_id + rtable_id alongside the signature so a
        # reader can quickly see which row pandas put at each rank
        # without parsing the full dict.
        print(f"{i:>5d}\tltable_id={row['ltable_id']}\trtable_id={row['rtable_id']}\tsig={sig}")

    # Whole-table digest so a single line reveals divergence at a
    # glance even if the per-row dump is long.
    full_text = "\n".join(
        f"{row['ltable_id']},{row['rtable_id']},{row['label']}"
        for _, row in merged.iterrows()
    )
    full_digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    print(f"# full_order_digest={full_digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
