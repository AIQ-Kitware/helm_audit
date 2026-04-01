from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator


def parse_run_entry_description(desc: str) -> tuple[str, dict[str, object]]:
    if ":" not in desc:
        raise ValueError(
            "Run entry description must contain ':' separating benchmark and parameters"
        )
    from helm.common.object_spec import parse_object_spec

    spec = parse_object_spec(desc)
    return spec.class_name, spec.args


def parse_run_name_to_kv(run_name: str) -> tuple[str, dict[str, object]]:
    if ":" not in run_name:
        return "", {}
    bench, rest = run_name.split(":", 1)
    bench = bench.strip()
    kv: dict[str, object] = {}
    rest = rest.strip()
    if rest:
        for part in rest.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                kv[k.strip()] = v.strip()
            else:
                kv[part] = True
    return bench, kv


def canonicalize_kv(kv: dict[str, object]) -> dict[str, object]:
    kv = dict(kv)
    model = kv.get("model", None)
    if isinstance(model, str):
        kv["model"] = model.replace("/", "_")
    return kv


def run_dir_matches_requested(run_dir_name: str, requested_desc: str) -> bool:
    req_bench, req_kv = parse_run_name_to_kv(requested_desc)
    cand_bench, cand_kv = parse_run_name_to_kv(run_dir_name)
    if req_bench != cand_bench:
        return False

    req_kv = canonicalize_kv(req_kv)
    cand_kv = canonicalize_kv(cand_kv)
    for k, v in req_kv.items():
        if k not in cand_kv:
            return False
        if cand_kv[k] != v:
            return False
    return True


def discover_benchmark_output_dirs(
    roots: Iterable[os.PathLike[str] | str],
) -> Iterator[Path]:
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        if root.name == "benchmark_output" and root.is_dir():
            yield root
            continue

        for dirpath, dirnames, _filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            prunable = {".git", "__pycache__", ".venv", "venv", "node_modules"}
            dirnames[:] = [d for d in dirnames if d not in prunable]
            if "benchmark_output" in dirnames:
                bo = Path(dirpath) / "benchmark_output"
                if bo.is_dir():
                    yield bo
                dirnames[:] = [d for d in dirnames if d != "benchmark_output"]
