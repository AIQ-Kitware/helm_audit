from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


def _latest_csv(index_root: Path, stem: str) -> Path:
    latest_alias = index_root / f"{stem}.latest.csv"
    if latest_alias.exists():
        return latest_alias.resolve()
    candidates = sorted(index_root.glob(f"{stem}_*.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No {stem}_*.csv found in {index_root}")
    return candidates[0].resolve()


def _normalize_text_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([""] * len(df), index=df.index)
    return df[col].fillna("").astype(str).str.strip()


def _truthy_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([False] * len(df), index=df.index)
    return (
        df[col]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.lower()
        .isin({"1", "true", "yes"})
    )


def _suite_version_key(value: Any) -> tuple[int, ...]:
    text = str(value or "")
    nums = re.findall(r"\d+", text)
    return tuple(int(n) for n in nums) if nums else (0,)


def _normalize_official_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "run_path" not in df.columns and "run_dir" in df.columns:
        df["run_path"] = df["run_dir"]
    elif "run_path" in df.columns and "run_dir" in df.columns:
        df["run_path"] = df["run_path"].fillna(df["run_dir"])
    if "source_kind" not in df.columns:
        df["source_kind"] = "official"
    return df


def load_local_index(index_root: str | Path) -> tuple[Path, pd.DataFrame]:
    index_root = Path(index_root).expanduser().resolve()
    fpath = _latest_csv(index_root, "audit_results_index")
    df = pd.read_csv(fpath, low_memory=False)
    return fpath, df


def load_official_index_or_details(
    index_root: str | Path,
    repo_root: str | Path,
) -> tuple[Path, pd.DataFrame]:
    index_root = Path(index_root).expanduser().resolve()
    repo_root = Path(repo_root).expanduser().resolve()

    try:
        fpath = _latest_csv(index_root, "official_public_index")
        df = pd.read_csv(fpath, low_memory=False)
        return fpath, _normalize_official_df(df)
    except FileNotFoundError:
        pass

    fpath = repo_root / "run_details.yaml"
    rows = yaml.safe_load(fpath.read_text())
    df = pd.DataFrame(rows)
    return fpath, _normalize_official_df(df)


def target_mmlu_run_spec(subject: str, model: str = "eleutherai/pythia-6.9b") -> str:
    return (
        f"mmlu:subject={subject},"
        f"method=multiple_choice_joint,"
        f"model={model},"
        f"data_augmentation=canonical"
    )


def alternate_model_spellings(model: str) -> set[str]:
    alts = {model}
    if "/" in model:
        alts.add(model.replace("/", "_"))
    if "_" in model:
        # only fix the first underscore back to slash if it looks like org_model
        parts = model.split("_", 1)
        if len(parts) == 2:
            alts.add(parts[0] + "/" + parts[1])
    return alts


def alternate_target_spellings(subject: str, model: str) -> set[str]:
    base = target_mmlu_run_spec(subject, model)
    targets = {base}
    for model_alt in alternate_model_spellings(model):
        targets.add(
            f"mmlu:subject={subject},"
            f"method=multiple_choice_joint,"
            f"model={model_alt},"
            f"data_augmentation=canonical"
        )
    return targets


def score_local_candidates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    status = _normalize_text_col(df, "status")
    df["_status_rank"] = status.map({
        "computed": 4,
        "reused": 3,
        "complete": 3,
        "unknown": 1,
        "": 0,
    }).fillna(0)

    df["_has_run_path"] = _normalize_text_col(df, "run_path").ne("")
    df["_has_run_spec"] = _truthy_col(df, "has_run_spec")
    df["_has_stats"] = _truthy_col(df, "has_stats")
    df["_has_per_instance_stats"] = _truthy_col(df, "has_per_instance_stats")

    df["_artifact_rank"] = (
        df["_has_run_path"].astype(int) * 1
        + df["_has_run_spec"].astype(int) * 2
        + df["_has_stats"].astype(int) * 4
        + df["_has_per_instance_stats"].astype(int) * 8
    )

    if "manifest_timestamp" not in df.columns:
        df["manifest_timestamp"] = None

    return df.sort_values(
        ["_artifact_rank", "_status_rank", "manifest_timestamp"],
        ascending=[False, False, False],
        na_position="last",
    )


def find_local_pythia_mmlu_candidates(
    subject: str = "us_foreign_policy",
    *,
    index_root: str | Path = "/data/crfm-helm-audit-store/indexes",
    model: str = "eleutherai/pythia-6.9b",
) -> tuple[Path, pd.DataFrame]:
    local_index_fpath, local_df = load_local_index(index_root)
    target_spellings = alternate_target_spellings(subject, model)

    mask = pd.Series([False] * len(local_df), index=local_df.index)
    for col in ["run_entry", "logical_run_key", "run_spec_name", "run_name"]:
        if col in local_df.columns:
            mask |= _normalize_text_col(local_df, col).isin(target_spellings)

    hits = local_df[mask].copy()
    if hits.empty:
        raise RuntimeError(
            f"No local candidates found for any of: {sorted(target_spellings)} "
            f"in {local_index_fpath}"
        )

    hits = score_local_candidates(hits)
    return local_index_fpath, hits


def find_official_pythia_mmlu_path(
    subject: str = "us_foreign_policy",
    *,
    index_root: str | Path = "/data/crfm-helm-audit-store/indexes",
    repo_root: str | Path = "/home/joncrall/code/helm_audit",
    model: str = "eleutherai/pythia-6.9b",
) -> tuple[Path, dict]:
    official_source_fpath, official_df = load_official_index_or_details(index_root, repo_root)
    target_spellings = alternate_target_spellings(subject, model)

    mask = _normalize_text_col(official_df, "run_spec_name").isin(target_spellings)
    if "source_kind" in official_df.columns:
        mask &= _normalize_text_col(official_df, "source_kind").eq("official")

    hits = official_df[mask].copy()
    if hits.empty:
        raise RuntimeError(
            f"No official candidates found for any of: {sorted(target_spellings)} "
            f"in {official_source_fpath}"
        )

    if "suite_version" in hits.columns:
        hits = hits.assign(
            _suite_key=hits["suite_version"].map(_suite_version_key)
        ).sort_values("_suite_key", ascending=False)

    row = hits.iloc[0].to_dict()
    return official_source_fpath, row


def find_poc_paths(
    subject: str = "us_foreign_policy",
    *,
    index_root: str | Path = "/data/crfm-helm-audit-store/indexes",
    repo_root: str | Path = "/home/joncrall/code/helm_audit",
    model: str = "eleutherai/pythia-6.9b",
) -> dict[str, Any]:
    local_index_fpath, local_hits = find_local_pythia_mmlu_candidates(
        subject=subject, index_root=index_root, model=model
    )
    official_source_fpath, official_row = find_official_pythia_mmlu_path(
        subject=subject, index_root=index_root, repo_root=repo_root, model=model
    )

    cols = [
        "experiment_name",
        "job_id",
        "status",
        "run_path",
        "run_spec_name",
        "logical_run_key",
        "run_entry",
        "has_run_spec",
        "has_stats",
        "has_per_instance_stats",
        "manifest_timestamp",
    ]
    cols = [c for c in cols if c in local_hits.columns]

    best_local = local_hits.iloc[0].to_dict()

    return {
        "local_index_fpath": str(local_index_fpath),
        "official_source_fpath": str(official_source_fpath),
        "official_run_path": official_row.get("run_path") or official_row.get("run_dir"),
        "best_local_run_path": best_local.get("run_path"),
        "best_local_experiment_name": best_local.get("experiment_name"),
        "best_local_job_id": best_local.get("job_id"),
        "all_local_candidates": local_hits[cols].to_dict(orient="records"),
    }


if __name__ == "__main__":
    info = find_poc_paths()
    print(json.dumps(info, indent=2))
