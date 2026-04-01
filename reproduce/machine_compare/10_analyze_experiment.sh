#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
EXPERIMENT_NAME="${1:?experiment name required}"
helm-audit-analyze-experiment --experiment-name "$EXPERIMENT_NAME" --allow-single-repeat
