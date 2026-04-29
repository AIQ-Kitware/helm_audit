#!/usr/bin/env bash
# Run the EEE-only analysis on the demo fixture.
#
# Inputs:
#   tests/fixtures/eee_only_demo/eee_artifacts/   (checked-in fixture)
#
# Outputs:
#   ${OUT_DPATH:-/tmp/eee_only_demo_out}/
#     official_public_index.latest.csv
#     audit_results_index.latest.csv
#     planning/
#     <experiment_name>/core-reports/<packet>/core_metric_report.latest.{txt,json,png}
#     aggregate-summary/all-results/                 (with --build-aggregate-summary)
#       README.latest.txt
#       agreement_curve.latest.{html,jpg}
#       reproducibility_buckets.latest.{html,jpg}
#       sankey_*.html, prioritized_examples.latest/, ...
#
# Override OUT_DPATH to write somewhere else, or BUILD_AGGREGATE=0 to skip the
# cross-packet roll-up (per-packet reports always render).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

OUT_DPATH="${OUT_DPATH:-/tmp/eee_only_demo_out}"
EEE_ROOT="${EEE_ROOT:-tests/fixtures/eee_only_demo/eee_artifacts}"

if [ ! -d "$EEE_ROOT" ]; then
  echo "FAIL: fixture root '$EEE_ROOT' does not exist." >&2
  echo "      Run ./reproduce/eee_only_demo/00_build_fixture.sh first." >&2
  exit 1
fi

BUILD_AGGREGATE="${BUILD_AGGREGATE:-1}"
extra_args=()
if [ "$BUILD_AGGREGATE" != "0" ]; then
  extra_args+=("--build-aggregate-summary")
fi

eval-audit-from-eee \
  --eee-root "$EEE_ROOT" \
  --out-dpath "$OUT_DPATH" \
  --clean \
  "${extra_args[@]}"

echo
echo "Per-packet reports:"
find "$OUT_DPATH" -mindepth 3 -maxdepth 3 -type d -path '*/core-reports/*' -printf '  %p\n' | sort
if [ "$BUILD_AGGREGATE" != "0" ]; then
  echo
  echo "Aggregate summary:"
  echo "  $OUT_DPATH/aggregate-summary/all-results/README.latest.txt"
fi
echo
echo "Quick read (per-packet):"
echo "  $OUT_DPATH/<experiment>/core-reports/<packet>/core_metric_report.latest.txt"
