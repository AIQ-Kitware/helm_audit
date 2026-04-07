from __future__ import annotations

from pathlib import Path

from helm_audit.infra.api import default_index_root, default_manifest_root
from helm_audit.infra.env import env_defaults
from helm_audit.infra.paths import run_details_fpath, run_specs_fpath


def test_store_root_drives_generated_paths(monkeypatch, tmp_path: Path):
    store_root = tmp_path / "audit-store"
    monkeypatch.setenv("AUDIT_STORE_ROOT", str(store_root))

    assert run_specs_fpath() == store_root / "configs" / "run_specs.yaml"
    assert run_details_fpath() == store_root / "configs" / "run_details.yaml"
    assert default_manifest_root() == store_root / "configs" / "manifests"
    assert default_index_root() == store_root / "indexes"
    assert env_defaults()["AUDIT_STORE_ROOT"] == str(store_root)
