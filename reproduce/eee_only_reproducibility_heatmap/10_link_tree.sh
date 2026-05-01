#!/usr/bin/env bash
# Build the official/local symlink tree for the 3×14 reproducibility heatmap.
#
# Delegates all filesystem operations to a Python helper to avoid bash
# file-descriptor accumulation issues in long scripts.
#
# Output layout (the shape ``eval-audit-from-eee`` expects):
#
#   $OUT_TREE/
#     official/
#       <bench_family>/<dev>/<model>/<uuid>.{json,_samples.jsonl}
#     local/
#       open-helm-models-reproducibility/
#         <bench_family>/<dev>/<model>/<uuid>.{json,_samples.jsonl}
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
OUT_ROOT="${OUT_ROOT:-$STORE_ROOT/eee-only-reproducibility-heatmap}"
OUT_TREE="${OUT_TREE:-$OUT_ROOT/eee_artifacts}"

# ``open-helm-models-reproducibility`` is a composite EEE folder that pools
# artifacts from many origin experiments, including secondary reproductions
# from namek and yardrat. For the paper we only want runs from aiq-gpu, so
# blocklist origin experiments whose name we know corresponds to a
# secondary host. Comma-separated; substring match against the origin
# experiment recorded in each artifact's status.json ``run_path``.
EXCLUDE_ORIGIN_EXPERIMENTS="${EXCLUDE_ORIGIN_EXPERIMENTS:-audit-namek-subset,audit-yardrat-subset}"

cd "$ROOT"

python3 - "$STORE_ROOT" "$OUT_TREE" "$EXCLUDE_ORIGIN_EXPERIMENTS" <<'PYEOF'
"""Build the official/local EEE symlink tree for the reproducibility heatmap."""
import json
import sys
import os
from pathlib import Path

store_root = Path(sys.argv[1])
out_tree = Path(sys.argv[2])
EXCLUDE_ORIGIN_EXPERIMENTS = tuple(
    x.strip() for x in sys.argv[3].split(",") if x.strip()
)


def _origin_experiment_for_local_artifact(aggregate_path):
    """Return the origin experiment name (third path segment of the
    audit run_path), or None if the layout doesn't expose it.

    Each local EEE artifact has a sibling ``status.json`` written by
    the helm_audit conversion pipeline. Its ``run_path`` field looks
    like::

        /data/crfm-helm-audit/<origin-experiment>/helm/<helm_id>/
        benchmark_output/runs/<suite>/<run_dir>/

    We're after ``<origin-experiment>``. Used to blocklist runs that
    came from secondary hosts (namek, yardrat) when assembling the
    paper-scope heatmap.
    """
    # status.json is at the dir 3 levels above the aggregate JSON:
    #   <helm_id>/<run_slug>/eee_output/<bench>/<dev>/<model>/<uuid>.json
    #          ^                                                ^
    #          status.json lives here              we're here
    candidate = aggregate_path.parents[4] / "status.json"
    if not candidate.is_file():
        return None
    try:
        d = json.loads(candidate.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rp = (d.get("run_path") or "").strip("/").split("/")
    # ['data', 'crfm-helm-audit', '<exp>', 'helm', ...]
    if len(rp) >= 3 and rp[1] == "crfm-helm-audit":
        return rp[2]
    return None


def _is_excluded_origin(origin_exp):
    if origin_exp is None:
        return False
    return any(blocked in origin_exp for blocked in EXCLUDE_ORIGIN_EXPERIMENTS)

OFFICIAL_V24 = store_root / "crfm-helm-public-eee-test/classic/v0.2.4"
OFFICIAL_V30 = store_root / "crfm-helm-public-eee-test/classic/v0.3.0"
LOCAL_ROOT = store_root / "eee/local"
LOCAL_EXP = "open-helm-models-reproducibility"

# Each entry: (official_version, dev, model_name, bench_family,
#              official_run_dir_name, local_slug_filter)
# local_slug_filter is a substring that must appear in the run-spec slug path
# segment when there are multiple sub-benchmarks; "" means no filter.
ENTRIES = [
    # pythia-2.8b-v0 (v0.2.4 — only boolq + civil_comments available publicly)
    ("v0.2.4", "eleutherai", "pythia-2.8b-v0", "boolq",
     "boolq:model=eleutherai_pythia-2.8b-v0,data_augmentation=canonical", ""),
    ("v0.2.4", "eleutherai", "pythia-2.8b-v0", "civil_comments",
     "civil_comments:demographic=all,model=eleutherai_pythia-2.8b-v0,data_augmentation=canonical",
     "demographic-all"),
    # pythia-6.9b (v0.3.0 — all 14 benchmarks)
    ("v0.3.0", "eleutherai", "pythia-6.9b", "boolq",
     "boolq:model=eleutherai_pythia-6.9b,data_augmentation=canonical", ""),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "civil_comments",
     "civil_comments:demographic=all,model=eleutherai_pythia-6.9b,data_augmentation=canonical",
     "demographic-all"),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "entity_data_imputation",
     "entity_data_imputation:dataset=Buy,model=eleutherai_pythia-6.9b", "Buy"),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "entity_matching",
     "entity_matching:dataset=Abt_Buy,model=eleutherai_pythia-6.9b", "Abt_Buy"),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "gsm",
     "gsm:model=eleutherai_pythia-6.9b", ""),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "imdb",
     "imdb:model=eleutherai_pythia-6.9b,data_augmentation=canonical", ""),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "lsat_qa",
     "lsat_qa:task=all,method=multiple_choice_joint,model=eleutherai_pythia-6.9b", ""),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "mmlu",
     "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,"
     "model=eleutherai_pythia-6.9b,data_augmentation=canonical",
     "us_foreign_policy"),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "narrativeqa",
     "narrative_qa:model=eleutherai_pythia-6.9b,data_augmentation=canonical", ""),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "quac",
     "quac:model=eleutherai_pythia-6.9b,data_augmentation=canonical", ""),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "synthetic_reasoning",
     "synthetic_reasoning:mode=variable_substitution,model=eleutherai_pythia-6.9b",
     "variable_substitution"),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "sythetic_reasoning_natural",
     "synthetic_reasoning_natural:difficulty=easy,model=eleutherai_pythia-6.9b",
     "difficulty-easy"),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "truthful_qa",
     "truthful_qa:task=mc_single,method=multiple_choice_joint,"
     "model=eleutherai_pythia-6.9b,data_augmentation=canonical", ""),
    ("v0.3.0", "eleutherai", "pythia-6.9b", "wikifact",
     "wikifact:k=5,subject=place_of_birth,model=eleutherai_pythia-6.9b",
     "place_of_birth"),
    # vicuna-7b-v1.3 (v0.3.0 — all 14 benchmarks)
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "boolq",
     "boolq:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical", ""),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "civil_comments",
     "civil_comments:demographic=all,model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical",
     "demographic-all"),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "entity_data_imputation",
     "entity_data_imputation:dataset=Buy,model=lmsys_vicuna-7b-v1.3", "Buy"),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "entity_matching",
     "entity_matching:dataset=Abt_Buy,model=lmsys_vicuna-7b-v1.3", "Abt_Buy"),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "gsm",
     "gsm:model=lmsys_vicuna-7b-v1.3", ""),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "imdb",
     "imdb:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical", ""),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "lsat_qa",
     "lsat_qa:task=all,method=multiple_choice_joint,model=lmsys_vicuna-7b-v1.3",
     ""),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "mmlu",
     "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,"
     "model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical",
     "us_foreign_policy"),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "narrativeqa",
     "narrative_qa:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical", ""),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "quac",
     "quac:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical", ""),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "synthetic_reasoning",
     "synthetic_reasoning:mode=variable_substitution,model=lmsys_vicuna-7b-v1.3",
     "variable_substitution"),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "sythetic_reasoning_natural",
     "synthetic_reasoning_natural:difficulty=easy,model=lmsys_vicuna-7b-v1.3",
     "difficulty-easy"),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "truthful_qa",
     "truthful_qa:task=mc_single,method=multiple_choice_joint,"
     "model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical", ""),
    ("v0.3.0", "lmsys", "vicuna-7b-v1.3", "wikifact",
     "wikifact:k=5,subject=place_of_birth,model=lmsys_vicuna-7b-v1.3",
     "place_of_birth"),
    # tiiuae/falcon-7b (v0.3.0 — all 14 benchmarks, fp16 HF backend)
    ("v0.3.0", "tiiuae", "falcon-7b", "boolq",
     "boolq:model=tiiuae_falcon-7b,data_augmentation=canonical", ""),
    ("v0.3.0", "tiiuae", "falcon-7b", "civil_comments",
     "civil_comments:demographic=all,model=tiiuae_falcon-7b,data_augmentation=canonical",
     "demographic-all"),
    ("v0.3.0", "tiiuae", "falcon-7b", "entity_data_imputation",
     "entity_data_imputation:dataset=Buy,model=tiiuae_falcon-7b", "Buy"),
    ("v0.3.0", "tiiuae", "falcon-7b", "entity_matching",
     "entity_matching:dataset=Abt_Buy,model=tiiuae_falcon-7b", "Abt_Buy"),
    ("v0.3.0", "tiiuae", "falcon-7b", "gsm",
     "gsm:model=tiiuae_falcon-7b", ""),
    ("v0.3.0", "tiiuae", "falcon-7b", "imdb",
     "imdb:model=tiiuae_falcon-7b,data_augmentation=canonical", ""),
    ("v0.3.0", "tiiuae", "falcon-7b", "lsat_qa",
     "lsat_qa:task=all,method=multiple_choice_joint,model=tiiuae_falcon-7b", ""),
    ("v0.3.0", "tiiuae", "falcon-7b", "mmlu",
     "mmlu:subject=us_foreign_policy,method=multiple_choice_joint,"
     "model=tiiuae_falcon-7b,data_augmentation=canonical",
     "us_foreign_policy"),
    ("v0.3.0", "tiiuae", "falcon-7b", "narrativeqa",
     "narrative_qa:model=tiiuae_falcon-7b,data_augmentation=canonical", ""),
    ("v0.3.0", "tiiuae", "falcon-7b", "quac",
     "quac:model=tiiuae_falcon-7b,data_augmentation=canonical", ""),
    ("v0.3.0", "tiiuae", "falcon-7b", "synthetic_reasoning",
     "synthetic_reasoning:mode=variable_substitution,model=tiiuae_falcon-7b",
     "variable_substitution"),
    ("v0.3.0", "tiiuae", "falcon-7b", "sythetic_reasoning_natural",
     "synthetic_reasoning_natural:difficulty=easy,model=tiiuae_falcon-7b",
     "difficulty-easy"),
    ("v0.3.0", "tiiuae", "falcon-7b", "truthful_qa",
     "truthful_qa:task=mc_single,method=multiple_choice_joint,"
     "model=tiiuae_falcon-7b,data_augmentation=canonical", ""),
    ("v0.3.0", "tiiuae", "falcon-7b", "wikifact",
     "wikifact:k=5,subject=place_of_birth,model=tiiuae_falcon-7b",
     "place_of_birth"),
]

VERSION_ROOTS = {"v0.2.4": OFFICIAL_V24, "v0.3.0": OFFICIAL_V30}


def _find_eee_jsons(directory: Path, slug_filter: str = "") -> list[Path]:
    """Return EEE aggregate JSONs under directory/eee_output/, **newest first**.

    Key detail: the same physical run directory frequently contains
    multiple ``<uuid>.json`` aggregates from successive re-conversions
    (e.g., the public CRFM EEE store has a mix of old-format files
    where ``evaluation_result_id`` is None and newer-format files
    where each metric gets its own per-sample record). The sort key
    is the aggregate's ``retrieved_timestamp`` (descending) so the
    caller picking ``[0]`` always gets the most recent — and therefore
    most-likely-newest-schema — conversion. Sorting alphabetically by
    UUID would pick a random one and silently drop the join when an
    old-format file happened to sort first.
    """
    eee_out = directory / "eee_output"
    if not eee_out.is_dir():
        return []
    results: list[tuple[float, Path]] = []
    for p in eee_out.rglob("*.json"):
        if p.name in {"status.json", "provenance.json"}:
            continue
        if p.name.endswith("_samples.jsonl"):
            continue
        try:
            ts_raw = json.loads(p.read_text()).get("retrieved_timestamp")
            ts = float(ts_raw) if ts_raw is not None else 0.0
        except (OSError, ValueError, json.JSONDecodeError):
            ts = 0.0
        results.append((ts, p))
    if slug_filter:
        results = [(t, p) for (t, p) in results if slug_filter in str(p)]
    # Sort by timestamp descending; ties broken by path for determinism.
    results.sort(key=lambda tp: (-tp[0], str(tp[1])))
    return [p for (_t, p) in results]


def _symlink_force(src: Path, dst: Path) -> None:
    """Create dst as a symlink to src, replacing any existing symlink."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    os.symlink(src, dst)


print(f"Cleaning {out_tree} ...")
import shutil
if out_tree.exists():
    shutil.rmtree(out_tree)
out_tree.mkdir(parents=True)

prev_section = None
for (version, dev, model_name, bench_family, official_run_dir, local_slug) in ENTRIES:
    section = f"{dev}/{model_name}"
    if section != prev_section:
        print(f"\n== {dev}/{model_name} ({version}) ==")
        prev_section = section

    official_root = VERSION_ROOTS[version]
    official_dir = official_root / official_run_dir

    # ---- official: link ONE (alphabetically first) uuid.json ----
    official_jsons = _find_eee_jsons(official_dir)
    if not official_jsons:
        print(f"  WARN(off): no aggregate JSON for {bench_family}/{dev}/{model_name}")
        continue
    src_agg = official_jsons[0]
    uuid = src_agg.stem
    src_samples = src_agg.with_name(uuid + "_samples.jsonl")
    dst_off_dir = out_tree / "official" / bench_family / dev / model_name
    _symlink_force(src_agg, dst_off_dir / f"{uuid}.json")
    if src_samples.exists():
        _symlink_force(src_samples, dst_off_dir / f"{uuid}_samples.jsonl")
    print(f"  official: {bench_family}/{dev}/{model_name}/{uuid}  ({version})")

    # ---- local: link ALL matching artifacts ----
    # Walk the local exp looking for artifacts under eee_output/<bench>/<dev>/<model>/
    dst_loc_dir = out_tree / "local" / LOCAL_EXP / bench_family / dev / model_name
    n_local = 0
    n_skipped_origin = 0
    local_exp_root = LOCAL_ROOT / LOCAL_EXP
    if local_exp_root.is_dir():
        for dirpath, _dirnames, filenames in os.walk(local_exp_root):
            dp = Path(dirpath)
            # Must be under .../eee_output/<bench>/<dev>/<model>/ exactly
            rel = dp.relative_to(local_exp_root)
            parts = rel.parts
            # Structure: <helm_id>/<run_slug>/eee_output/<bench>/<dev>/<model>
            # So we need exactly: parts[-4] == 'eee_output', parts[-3] == bench,
            #                     parts[-2] == dev, parts[-1] == model_name
            if (len(parts) >= 4
                    and parts[-4] == "eee_output"
                    and parts[-3] == bench_family
                    and parts[-2] == dev
                    and parts[-1] == model_name):
                # Apply slug filter against the run_slug segment (parts[-5])
                if local_slug and len(parts) >= 5 and local_slug not in parts[-5]:
                    continue
                for fname in sorted(filenames):
                    if fname.endswith(".json") and not fname.endswith("_samples.jsonl"):
                        if fname in {"status.json", "provenance.json"}:
                            continue
                        src_f = dp / fname
                        # Origin-experiment blocklist. The artifact's
                        # status.json sits four directories above its
                        # aggregate JSON; reading run_path tells us which
                        # audit experiment (and therefore which host)
                        # produced it. Skip secondary hosts entirely.
                        origin_exp = _origin_experiment_for_local_artifact(src_f)
                        if _is_excluded_origin(origin_exp):
                            n_skipped_origin += 1
                            continue
                        u = fname[:-5]  # strip .json
                        src_s = dp / f"{u}_samples.jsonl"
                        _symlink_force(src_f, dst_loc_dir / f"{u}.json")
                        if src_s.exists():
                            _symlink_force(src_s, dst_loc_dir / f"{u}_samples.jsonl")
                        n_local += 1

    if n_local == 0:
        print(f"  WARN(loc): no local artifacts for {bench_family}/{dev}/{model_name}"
              f" (filter='{local_slug}') — cell will be N/A")
    else:
        suffix = ""
        if n_skipped_origin:
            suffix = f"  [skipped {n_skipped_origin} from excluded origins]"
        print(f"  local:    {bench_family}/{dev}/{model_name}  ({n_local} artifacts){suffix}")

print(f"\nTree ready at: {out_tree}")
print("Next: ./20_run.sh")
PYEOF
