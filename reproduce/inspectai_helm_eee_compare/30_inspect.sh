#!/usr/bin/env bash
# Print the comparability story for the cross-harness packet so the
# user can see what the planner can and can't conclude. This is the
# "did the system catch the cross-harness mismatch?" diagnostic.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
OUT_ROOT="${OUT_ROOT:-$STORE_ROOT/inspectai-helm-eee-compare}"
FROM_EEE_OUT="${FROM_EEE_OUT:-$OUT_ROOT/from_eee_out}"

cd "$ROOT"

# There's only one packet in this scope (mmlu × pythia-6.9b), so just
# walk every core-report we find.
for report_json in $(find "$FROM_EEE_OUT" -name 'core_metric_report.json' 2>/dev/null); do
  pkt="$(basename "$(dirname "$report_json")")"
  echo "================================================================="
  echo "packet: $pkt"
  echo "================================================================="
  python3 <<PY
import json
from pathlib import Path
report = json.loads(Path("$report_json").read_text())
print(f'pairs: {len(report.get("pairs") or [])}')
print()
for i, p in enumerate(report.get("pairs") or [], 1):
    print(f'--- pair {i}: kind={p.get("comparison_kind")} ---')
    print(f'  components: {p.get("component_ids")}')
    facts = p.get("comparability_facts") or {}
    for fname in [
        "same_model",
        "same_scenario_class",
        "same_benchmark_family",
        "same_deployment",
        "same_instructions",
        "same_max_eval_instances",
        "same_suite_or_track_version",
    ]:
        f = facts.get(fname) or {}
        status = f.get("status", "?")
        values = f.get("values") or []
        print(f'    {fname:<32} {status:<8} values={values}')
    print(f'  warnings: {p.get("warnings")}')
    rl_curve = (p.get("run_level") or {}).get("agreement_vs_abs_tol") or []
    a0 = next((row.get("agree_ratio") for row in rl_curve if row.get("abs_tol") == 0.0), None)
    il_curve = (p.get("instance_level") or {}).get("agreement_vs_abs_tol") or []
    ia0 = next((row.get("agree_ratio") for row in il_curve if row.get("abs_tol") == 0.0), None)
    print(f'  agree@0:  run-level={a0!r}  instance-level={ia0!r}')
    print()
PY
done

echo
echo "================================================================="
echo "Cross-harness comparability check (the real question):"
echo "================================================================="
echo
echo "The planner today asks: 'do these run_specs agree on adapter"
echo "spec, scenario class, deployment, instructions, max_eval_instances,"
echo "suite/track?'  When one side is InspectAI (no run_spec sidecar),"
echo "those collapse to 'unknown'. The planner does NOT today inspect"
echo "EEE-native fields that *are* present on both sides:"
echo
echo "  - source_data.dataset_name      (mmlu vs mmlu — match by name only)"
echo "  - source_data.samples_number    (HELM 1000 / subject vs InspectAI 13937)"
echo "  - metric_config.evaluation_description  (HELM exact_match vs Inspect accuracy)"
echo "  - eval_library.name             (helm vs inspect_ai)"
echo "  - generation_config.additional_details  (5-shot config, prompt template, …)"
echo
echo "Below: side-by-side EEE fields from each component, so you can"
echo "see which dimensions actually agree."
echo

# Pull the raw EEE aggregates and print the differentiating fields.
ARTIFACTS_ROOT="${OUT_TREE:-$OUT_ROOT/eee_artifacts}"
python3 <<PY
import json
from pathlib import Path
root = Path("$ARTIFACTS_ROOT")
print(f"{'side':<40} {'samples_n':>10} {'metric_desc':>28} {'eval_lib':>10}  {'dataset':<10}  evaluation_name")
print('-' * 140)
for kind in ('official', 'local'):
    for j in sorted(root.glob(f'{kind}/**/*.json')):
        if j.name in ('run_spec.json',):
            continue
        try:
            d = json.loads(j.read_text())
        except Exception:
            continue
        if 'evaluation_results' not in d or 'model_info' not in d:
            continue
        side = j.relative_to(root).parts[1] if kind == 'local' else 'official'
        side = f'{kind}/{side}' if kind == 'local' else 'official'
        er = (d.get('evaluation_results') or [{}])[0]
        sd = er.get('source_data') or {}
        mc = er.get('metric_config') or {}
        lib = (d.get('eval_library') or {}).get('name') or '(unset)'
        print(f"{side:<40} {str(sd.get('samples_number', '?')):>10} {mc.get('evaluation_description', '?'):>28} {lib:>10}  {sd.get('dataset_name', '?'):<10}  {er.get('evaluation_name', '?')}")
PY

echo
echo "If those last four columns differ, the comparison the planner"
echo "produces above is mixing apples and oranges. The agreement number"
echo "is not interpretable as 'do the two evals reproduce each other'"
echo "in that case — it's measuring the difference between two evals"
echo "that were never running the same thing."
