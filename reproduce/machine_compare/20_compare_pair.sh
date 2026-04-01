#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
RUN_A="${1:?run a required}"
RUN_B="${2:?run b required}"
REPORT_DPATH="${3:-reports/pairwise}"
helm-audit-compare-pair --run-a "$RUN_A" --run-b "$RUN_B" --report-dpath "$REPORT_DPATH"
