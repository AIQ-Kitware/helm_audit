from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from helm_audit.infra.fs_publish import safe_unlink, stamped_history_dir, write_latest_alias


def slugify_identifier(text: str) -> str:
    return (
        str(text)
        .replace("/", "-")
        .replace(":", "-")
        .replace(",", "-")
        .replace("=", "-")
        .replace("@", "-")
        .replace(" ", "-")
    )


def write_manifest(
    report_dpath: Path,
    *,
    stem: str,
    latest_name: str,
    payload: dict[str, Any],
) -> Path:
    stamp, history_dpath = stamped_history_dir(report_dpath)
    out_fpath = history_dpath / f"{stem}_{stamp}.json"
    out_fpath.write_text(json.dumps(payload, indent=2) + "\n")
    write_latest_alias(out_fpath, report_dpath, latest_name)
    return out_fpath


def load_manifest(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must decode to a dict: {path}")
    return data


def load_packet_manifests(
    *,
    report_dpath: str | Path,
    components_manifest: str | Path | None = None,
    comparisons_manifest: str | Path | None = None,
) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
    report_dpath = Path(report_dpath).expanduser().resolve()
    components_fpath = (
        Path(components_manifest).expanduser().resolve()
        if components_manifest is not None
        else (report_dpath / "components_manifest.latest.json").resolve()
    )
    comparisons_fpath = (
        Path(comparisons_manifest).expanduser().resolve()
        if comparisons_manifest is not None
        else (report_dpath / "comparisons_manifest.latest.json").resolve()
    )
    return (
        components_fpath,
        load_manifest(components_fpath),
        comparisons_fpath,
        load_manifest(comparisons_fpath),
    )


def component_link_basename(component_id: str) -> str:
    return slugify_identifier(component_id)


def cleanup_glob(root: Path, pattern: str, keep_names: set[str]) -> None:
    if not root.exists():
        return
    for path in root.glob(pattern):
        if path.name not in keep_names:
            safe_unlink(path)
