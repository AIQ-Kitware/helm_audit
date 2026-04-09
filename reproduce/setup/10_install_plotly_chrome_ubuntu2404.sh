#!/usr/bin/env bash
set -euo pipefail

# Install Google Chrome for Plotly/Kaleido static image export on a headless
# Ubuntu 24.04 VM. This keeps Chrome in the repo-local cache path already used
# by helm_audit's Plotly helpers.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CACHE_ROOT="${REPO_ROOT}/.cache/plotly-chrome"

echo "repo_root=${REPO_ROOT}"
echo "cache_root=${CACHE_ROOT}"

if ! command -v python >/dev/null 2>&1; then
    echo "python is required" >&2
    exit 1
fi

PLOTLY_GET_CHROME_CMD=()
if command -v plotly_get_chrome >/dev/null 2>&1; then
    PLOTLY_GET_CHROME_CMD=(plotly_get_chrome)
elif command -v uv >/dev/null 2>&1; then
    PLOTLY_GET_CHROME_CMD=(uv run --project "${REPO_ROOT}" plotly_get_chrome)
else
    echo "plotly_get_chrome is required. Install python deps first, e.g.:" >&2
    echo "  cd ${REPO_ROOT}" >&2
    echo "  uv pip install -e ." >&2
    exit 1
fi

mkdir -p "${CACHE_ROOT}"
"${PLOTLY_GET_CHROME_CMD[@]}" -y --path "${CACHE_ROOT}"

CHROME_BIN="${CACHE_ROOT}/chrome-linux64/chrome"
if [[ ! -x "${CHROME_BIN}" ]]; then
    echo "expected chrome binary missing: ${CHROME_BIN}" >&2
    exit 1
fi

echo "installed_chrome=${CHROME_BIN}"
echo
echo "Verify with:"
echo "  PYTHONPATH=${REPO_ROOT} python -m helm_audit.cli.check_env --plotly-static-only"
