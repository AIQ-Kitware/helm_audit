from __future__ import annotations

from pathlib import Path
from typing import Any

import kwutil


def load_yaml(path: str | Path) -> Any:
    return kwutil.Yaml.load(Path(path))


def load_manifest(path: str | Path) -> dict[str, Any]:
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise TypeError("Manifest must decode to a dictionary")
    return data


def dump_yaml(data: Any) -> str:
    return kwutil.Yaml.dumps(data)
