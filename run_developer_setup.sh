#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3.10}"
VENV_PYTHON="$VENV_DIR/bin/python"

echo "[helm_audit] repo root: $REPO_ROOT"

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "error: required command not found: $1" >&2
        exit 1
    fi
}

need_cmd git
need_cmd uv

echo "[helm_audit] syncing submodules"
git submodule sync --recursive
git submodule update --init --recursive

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "[helm_audit] creating virtual environment at $VENV_DIR"
    uv venv "$VENV_DIR" --python "$PYTHON_BIN"
else
    echo "[helm_audit] reusing existing virtual environment at $VENV_DIR"
fi

echo "[helm_audit] upgrading packaging tools"
uv pip install --python "$VENV_PYTHON" --upgrade pip setuptools wheel

echo "[helm_audit] installing root package in editable mode"
uv pip install --python "$VENV_PYTHON" -e .

install_editable_pkg() {
    local pkg_path="$1"

    if [[ ! -d "$pkg_path" ]]; then
        echo "[helm_audit] skipping missing path: $pkg_path"
        return 0
    fi

    if [[ -f "$pkg_path/pyproject.toml" || -f "$pkg_path/setup.py" || -f "$pkg_path/setup.cfg" ]]; then
        echo "[helm_audit] installing editable package: $pkg_path"
        uv pip install --python "$VENV_PYTHON" -e "$pkg_path"
    else
        echo "[helm_audit] skipping non-python submodule: $pkg_path"
    fi
}

if [[ -f .gitmodules ]]; then
    echo "[helm_audit] discovering submodules from .gitmodules"

    declare -A seen_paths=()

    while IFS= read -r sm_path; do
        [[ -n "${seen_paths[$sm_path]:-}" ]] && continue
        seen_paths["$sm_path"]=1
        install_editable_pkg "$sm_path"
    done < <(git config --file .gitmodules --get-regexp path | awk '{print $2}')
else
    echo "[helm_audit] no .gitmodules found"
fi

echo
echo "[helm_audit] setup complete"
echo "venv python: $VENV_PYTHON"
echo "activate with:"
echo "  source $VENV_DIR/bin/activate"
echo
echo "sanity check:"
echo "  $VENV_PYTHON -m helm_audit.cli.check_env"
