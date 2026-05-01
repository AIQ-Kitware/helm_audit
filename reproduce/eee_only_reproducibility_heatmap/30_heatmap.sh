#!/usr/bin/env bash
# Generate the 3-model × 14-benchmark reproducibility heatmap.
# Reads the core_metric_report.json files written by 20_run.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
OUT_ROOT="${OUT_ROOT:-$STORE_ROOT/eee-only-reproducibility-heatmap}"
FROM_EEE_OUT="${FROM_EEE_OUT:-$OUT_ROOT/from_eee_out}"
HEATMAP_OUT="${HEATMAP_OUT:-$OUT_ROOT/heatmap}"
ABS_TOL="${ABS_TOL:-1e-9}"

cd "$ROOT"

# Count packets the heatmap can read. Use ``wc -l`` instead of
# ``grep -q .`` so the count is visible and so a ``set -o pipefail``
# interaction can't squelch a working find.
n_reports=$(find "$FROM_EEE_OUT" -name "core_metric_report.json" 2>/dev/null | wc -l)
if [ "$n_reports" -eq 0 ]; then
  echo "FAIL: no core_metric_report.json files under" >&2
  echo "      $FROM_EEE_OUT" >&2
  echo >&2
  if [ -d "$FROM_EEE_OUT" ]; then
    echo "      Path exists. Top-level entries:" >&2
    ls "$FROM_EEE_OUT" 2>&1 | sed 's/^/        /' >&2
    echo >&2
    echo "      Looking one level deeper for any *.json (in case the layout changed):" >&2
    find "$FROM_EEE_OUT" -maxdepth 4 -name '*.json' -printf '        %p\n' 2>&1 \
      | head -10 >&2
  else
    echo "      Path does not exist. Run ./20_run.sh first or set FROM_EEE_OUT" >&2
    echo "      to override (current: \$FROM_EEE_OUT=\"$FROM_EEE_OUT\")." >&2
  fi
  exit 1
fi
echo "Found $n_reports core_metric_report.json files."

echo "Generating reproducibility heatmap ..."
echo "  analysis root: $FROM_EEE_OUT"
echo "  output dir:    $HEATMAP_OUT"
echo "  abs_tol:       $ABS_TOL"
echo

# Per-metric drill-down (off by default — produces a tall heatmap with
# one row per (benchmark, metric) so the eye can spot which scoring
# metric is dragging a benchmark's overall agree_ratio. Set
# PER_METRIC=1 to enable, plus INCLUDE_BOOKKEEPING=1 to keep the
# always-1.0 token-count / finish_reason metrics if you want them.)
extra_args=()
if [ "${PER_METRIC:-0}" = "1" ]; then
  extra_args+=("--per-metric")
fi
if [ "${INCLUDE_BOOKKEEPING:-0}" = "1" ]; then
  extra_args+=("--include-bookkeeping")
fi

python3 -m eval_audit.reports.eee_only_heatmap \
  --analysis-root "$FROM_EEE_OUT" \
  --out-dir "$HEATMAP_OUT" \
  --abs-tol "$ABS_TOL" \
  "${extra_args[@]}"

echo
echo "Outputs:"
find "$HEATMAP_OUT" -type f | sort | sed 's/^/  /'
