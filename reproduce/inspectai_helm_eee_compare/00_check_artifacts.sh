#!/usr/bin/env bash
# Verify the three EEE artifact sources this runbook composes are all
# present on disk. No writes.
#
# Sources:
#   1. Public HELM EEE conversion of pythia-6.9b on a single MMLU
#      subject (us_foreign_policy). Has a HELM run_spec.json sidecar.
#   2. Two local audit reproductions of the same scenario
#      (audit-mmlu-usfp-pythia-r{1,2}). Have HELM run_spec.json
#      sidecars from the original HELM run dirs.
#   3. An InspectAI-produced EEE artifact for "MMLU on
#      eleutherai/pythia-6.9b". This one has *no* HELM sidecar — it
#      came from a different harness, scored a different sample set
#      (full MMLU, not us_foreign_policy), with a different metric
#      ("accuracy" vs HELM's "exact_match").
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"

OFFICIAL_DIR="$STORE_ROOT/crfm-helm-public-eee-test/classic/v0.3.0/mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
LOCAL_R1="$STORE_ROOT/eee/local/audit-mmlu-usfp-pythia-r1"
LOCAL_R2="$STORE_ROOT/eee/local/audit-mmlu-usfp-pythia-r2"
INSPECTAI_DIR="$STORE_ROOT/inspectai-eee-results/MMLU-Inspect-EEE"

cd "$ROOT"

errors=0

check_aggregate_dir() {
  local label="$1"
  local dir="$2"
  if [[ ! -d "$dir" ]]; then
    echo "  FAIL: $label missing — $dir" >&2
    errors=$((errors + 1))
    return
  fi
  local count
  count="$(find "$dir" -name '*.json' ! -name 'fixture_manifest.json' ! -name 'provenance.json' ! -name 'status.json' ! -name 'run_spec.json' 2>/dev/null | head -10 | wc -l)"
  if [[ "$count" -lt 1 ]]; then
    echo "  FAIL: no EEE aggregate JSON under $dir" >&2
    errors=$((errors + 1))
  else
    echo "  OK ($count EEE aggregate(s)): $label"
  fi
}

echo "== HELM official (sidecar present) =="
check_aggregate_dir "official mmlu/usfp pythia-6.9b" "$OFFICIAL_DIR/eee_output"
if [[ -f "$OFFICIAL_DIR/run_spec.json" ]]; then
  echo "  OK: run_spec.json present (sidecar will be picked up by the planner)"
else
  echo "  WARN: no run_spec.json sidecar at $OFFICIAL_DIR — comparability facts will be 'unknown' for the HELM-side fields" >&2
fi

echo
echo "== local audit reproductions (sidecars present in HELM run dirs) =="
check_aggregate_dir "audit-mmlu-usfp-pythia-r1" "$LOCAL_R1"
check_aggregate_dir "audit-mmlu-usfp-pythia-r2" "$LOCAL_R2"

echo
echo "== InspectAI-produced EEE (no sidecar; different harness) =="
check_aggregate_dir "inspectai mmlu pythia-6.9b" "$INSPECTAI_DIR"

echo
if [[ "$errors" -gt 0 ]]; then
  echo "FAIL: $errors check(s) failed; cannot continue." >&2
  exit 1
fi
echo "OK: all sources present."
