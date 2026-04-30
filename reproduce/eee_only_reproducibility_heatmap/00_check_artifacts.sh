#!/usr/bin/env bash
# Check which (model, benchmark) pairs have both official and local EEE coverage.
# Prints a coverage table and exits non-zero if any required artifact is missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORE_ROOT="${AUDIT_STORE_ROOT:-/data/crfm-helm-audit-store}"
OFFICIAL_V24="$STORE_ROOT/crfm-helm-public-eee-test/classic/v0.2.4"
OFFICIAL_V30="$STORE_ROOT/crfm-helm-public-eee-test/classic/v0.3.0"
LOCAL_ROOT="$STORE_ROOT/eee/local"
LOCAL_EXP="open-helm-models-reproducibility"

# ---------------------------------------------------------------------------
# Official run dirs: version | model_slug | bench_family | run_dir_name
# ---------------------------------------------------------------------------
declare -a OFFICIAL_ENTRIES
OFFICIAL_ENTRIES=(
  # pythia-2.8b-v0 (v0.2.4 — only boolq + civil_comments available publicly)
  "v0.2.4|eleutherai_pythia-2.8b-v0|boolq|boolq:model=eleutherai_pythia-2.8b-v0,data_augmentation=canonical"
  "v0.2.4|eleutherai_pythia-2.8b-v0|civil_comments|civil_comments:demographic=all,model=eleutherai_pythia-2.8b-v0,data_augmentation=canonical"
  # pythia-6.9b (v0.3.0 — all 14 benchmarks)
  "v0.3.0|eleutherai_pythia-6.9b|boolq|boolq:model=eleutherai_pythia-6.9b,data_augmentation=canonical"
  "v0.3.0|eleutherai_pythia-6.9b|civil_comments|civil_comments:demographic=all,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
  "v0.3.0|eleutherai_pythia-6.9b|entity_data_imputation|entity_data_imputation:dataset=Buy,model=eleutherai_pythia-6.9b"
  "v0.3.0|eleutherai_pythia-6.9b|entity_matching|entity_matching:dataset=Abt_Buy,model=eleutherai_pythia-6.9b"
  "v0.3.0|eleutherai_pythia-6.9b|gsm|gsm:model=eleutherai_pythia-6.9b"
  "v0.3.0|eleutherai_pythia-6.9b|imdb|imdb:model=eleutherai_pythia-6.9b,data_augmentation=canonical"
  "v0.3.0|eleutherai_pythia-6.9b|lsat_qa|lsat_qa:task=all,method=multiple_choice_joint,model=eleutherai_pythia-6.9b"
  "v0.3.0|eleutherai_pythia-6.9b|mmlu|mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
  "v0.3.0|eleutherai_pythia-6.9b|narrativeqa|narrative_qa:model=eleutherai_pythia-6.9b,data_augmentation=canonical"
  "v0.3.0|eleutherai_pythia-6.9b|quac|quac:model=eleutherai_pythia-6.9b,data_augmentation=canonical"
  "v0.3.0|eleutherai_pythia-6.9b|synthetic_reasoning|synthetic_reasoning:mode=variable_substitution,model=eleutherai_pythia-6.9b"
  "v0.3.0|eleutherai_pythia-6.9b|sythetic_reasoning_natural|synthetic_reasoning_natural:difficulty=easy,model=eleutherai_pythia-6.9b"
  "v0.3.0|eleutherai_pythia-6.9b|truthful_qa|truthful_qa:task=mc_single,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
  "v0.3.0|eleutherai_pythia-6.9b|wikifact|wikifact:k=5,subject=place_of_birth,model=eleutherai_pythia-6.9b"
  # vicuna-7b-v1.3 (v0.3.0 — all 14 benchmarks)
  "v0.3.0|lmsys_vicuna-7b-v1.3|boolq|boolq:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"
  "v0.3.0|lmsys_vicuna-7b-v1.3|civil_comments|civil_comments:demographic=all,model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"
  "v0.3.0|lmsys_vicuna-7b-v1.3|entity_data_imputation|entity_data_imputation:dataset=Buy,model=lmsys_vicuna-7b-v1.3"
  "v0.3.0|lmsys_vicuna-7b-v1.3|entity_matching|entity_matching:dataset=Abt_Buy,model=lmsys_vicuna-7b-v1.3"
  "v0.3.0|lmsys_vicuna-7b-v1.3|gsm|gsm:model=lmsys_vicuna-7b-v1.3"
  "v0.3.0|lmsys_vicuna-7b-v1.3|imdb|imdb:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"
  "v0.3.0|lmsys_vicuna-7b-v1.3|lsat_qa|lsat_qa:task=all,method=multiple_choice_joint,model=lmsys_vicuna-7b-v1.3"
  "v0.3.0|lmsys_vicuna-7b-v1.3|mmlu|mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"
  "v0.3.0|lmsys_vicuna-7b-v1.3|narrativeqa|narrative_qa:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"
  "v0.3.0|lmsys_vicuna-7b-v1.3|quac|quac:model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"
  "v0.3.0|lmsys_vicuna-7b-v1.3|synthetic_reasoning|synthetic_reasoning:mode=variable_substitution,model=lmsys_vicuna-7b-v1.3"
  "v0.3.0|lmsys_vicuna-7b-v1.3|sythetic_reasoning_natural|synthetic_reasoning_natural:difficulty=easy,model=lmsys_vicuna-7b-v1.3"
  "v0.3.0|lmsys_vicuna-7b-v1.3|truthful_qa|truthful_qa:task=mc_single,method=multiple_choice_joint,model=lmsys_vicuna-7b-v1.3,data_augmentation=canonical"
  "v0.3.0|lmsys_vicuna-7b-v1.3|wikifact|wikifact:k=5,subject=place_of_birth,model=lmsys_vicuna-7b-v1.3"
)

# Local slug filters: bench_family -> glob fragment to filter run-spec slug paths
# Empty string means no filter (unique sub-benchmark for that family).
declare -A LOCAL_SLUG_FILTER
LOCAL_SLUG_FILTER=(
  [boolq]=""
  [civil_comments]="demographic-all"
  [entity_data_imputation]="Buy"
  [entity_matching]="Abt_Buy"
  [gsm]=""
  [imdb]=""
  [lsat_qa]=""
  [mmlu]="us_foreign_policy"
  [narrativeqa]=""
  [quac]=""
  [synthetic_reasoning]="variable_substitution"
  [sythetic_reasoning_natural]="difficulty-easy"
  [truthful_qa]=""
  [wikifact]="place_of_birth"
)

echo "Coverage check: official + local EEE artifacts"
echo "Store root: $STORE_ROOT"
echo

n_ok=0
n_miss_official=0
n_miss_local=0

for entry in "${OFFICIAL_ENTRIES[@]}"; do
  IFS='|' read -r version model_slug bench_family run_dir_name <<< "$entry"

  # official check
  case "$version" in
    v0.2.4) official_root="$OFFICIAL_V24" ;;
    v0.3.0) official_root="$OFFICIAL_V30" ;;
    *) official_root="" ;;
  esac
  official_dir="$official_root/$run_dir_name"
  if [ -d "$official_dir" ]; then
    n_off=$(find "$official_dir/eee_output" -name "*.json" \
      ! -name "*_samples.jsonl" ! -name "status.json" ! -name "provenance.json" \
      2>/dev/null | wc -l)
    if [ "$n_off" -gt 0 ]; then
      off_status="OK($n_off)"
    else
      off_status="EMPTY"
      ((n_miss_official++)) || true
    fi
  else
    off_status="MISSING"
    ((n_miss_official++)) || true
  fi

  # local check
  slug_filter="${LOCAL_SLUG_FILTER[$bench_family]:-}"
  if [ -n "$slug_filter" ]; then
    local_path_pattern="*${slug_filter}*/eee_output/${bench_family}/${model_slug//_/\/}/*.json"
  else
    local_path_pattern="*/eee_output/${bench_family}/${model_slug//_/\/}/*.json"
  fi
  # model_slug -> dev/model (eleutherai_pythia-6.9b -> eleutherai/pythia-6.9b)
  # Use python for the / replacement since bash can't replace only first _
  dev_model=$(python3 -c "
s = '$model_slug'
# Replace first underscore with /
idx = s.index('_')
print(s[:idx] + '/' + s[idx+1:])
" 2>/dev/null || echo "${model_slug/_//}")

  if [ -n "$slug_filter" ]; then
    local_path_pattern="*${slug_filter}*/eee_output/${bench_family}/${dev_model}/*.json"
  else
    local_path_pattern="*/eee_output/${bench_family}/${dev_model}/*.json"
  fi
  n_loc=$(find "$LOCAL_ROOT/$LOCAL_EXP" -path "$local_path_pattern" \
    ! -name "*_samples.jsonl" 2>/dev/null | wc -l)
  if [ "$n_loc" -gt 0 ]; then
    loc_status="OK($n_loc)"
  else
    loc_status="MISSING"
    ((n_miss_local++)) || true
  fi

  if [[ "$off_status" == OK* ]] && [[ "$loc_status" == OK* ]]; then
    status="BOTH"
    ((n_ok++)) || true
  elif [[ "$off_status" == OK* ]]; then
    status="OFF_ONLY"
  elif [[ "$loc_status" == OK* ]]; then
    status="LOC_ONLY"
  else
    status="NEITHER"
  fi

  printf "  %-20s %-26s %-10s %-10s %s\n" \
    "$bench_family" "$model_slug" "$off_status" "$loc_status" "$status"
done

echo
echo "Summary: $n_ok pairs with both official+local coverage"
echo "         $n_miss_official pairs missing official, $n_miss_local pairs missing local"

if [ $((n_miss_official + n_miss_local)) -gt 0 ]; then
  echo
  echo "NOTE: Missing artifacts will appear as gray 'N/A' cells in the heatmap."
  echo "      Run 10_link_tree.sh which skips absent entries with a warning."
fi
