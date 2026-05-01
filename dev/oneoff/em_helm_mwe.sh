#!/usr/bin/env bash
# End-to-end validation: run HELM's EntityMatchingScenario through
# downsample_eval_instances and compare its ordering to captured
# scenario_state.json files from the public store and the audit run.
#
# This composes the merge-ordering MWE (em_pandas_mwe.sh) with full
# HELM in the loop. Two passes:
#
#   1. CURRENT venv (whatever pandas + crfm-helm the user has active)
#      → expected to match the LOCAL scenario_state.json from
#        audit-historic-grid (since that's what produced it).
#
#   2. v0.3.0-era venv (crfm-helm==0.3.0, pandas==2.0.3, numpy==1.23.5)
#      → expected to match the OFFICIAL v0.3.0 scenario_state.json.
#
# If both passes match their respective captured scenario_states
# rank-by-rank, the chain is fully validated end-to-end:
#
#   pandas merge ordering → with_instance_ids → downsample seed=0
#                                              → scenario_state.json
#
# Run on toothbrush/aiq-gpu (NOT aivm-2404). Idempotent: re-running
# skips already-built venvs.
#
# Usage (with default scenario_state paths)::
#
#     bash dev/oneoff/em_helm_mwe.sh
#
# Override paths via env vars OFFICIAL_SS / LOCAL_SS.

set -euo pipefail

OFFICIAL_SS="${OFFICIAL_SS:-/data/crfm-helm-public/classic/benchmark_output/runs/v0.3.0/entity_matching:dataset=Abt_Buy,model=eleutherai_pythia-6.9b/scenario_state.json}"
LOCAL_SS="${LOCAL_SS:-/data/crfm-helm-audit/audit-historic-grid/helm/helm_id_jvz3gj5vj7b8/benchmark_output/runs/audit-historic-grid/entity_matching:dataset=Abt_Buy,model=eleutherai_pythia-6.9b/scenario_state.json}"

OUT_DIR="${OUT_DIR:-$PWD/em_helm_mwe_out}"
SCENARIO_CACHE="$OUT_DIR/scenario_cache"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_PY="$SCRIPT_DIR/em_helm_mwe_run.py"

if [[ ! -f "$RUN_PY" ]]; then
    echo "ERROR: $RUN_PY not found." >&2
    exit 1
fi

mkdir -p "$OUT_DIR" "$SCENARIO_CACHE"

# Sanity check: scenario_state.json paths exist.
for label in OFFICIAL LOCAL; do
    var="${label}_SS"
    path="${!var}"
    if [[ ! -f "$path" ]]; then
        echo "WARN: $label scenario_state.json not found at $path" >&2
        echo "      Pass an alternate path via $var=... env var if you" >&2
        echo "      want this side compared. Will skip the comparison." >&2
    fi
done
echo

# ---------------------------------------------------------------------
# Pass 1: current venv. The user runs this script from their existing
# crfm-helm venv (the one that produced the LOCAL scenario_state).
# We run em_helm_mwe_run.py directly with the current python.
# ---------------------------------------------------------------------
echo "=========================================="
echo "[em-helm-mwe] Pass 1: current venv"
echo "=========================================="
python -c "import helm; print(f'# helm import path: {helm.__file__}')" \
    || { echo "ERROR: crfm-helm not importable in current venv." >&2; exit 1; }

# Run live → compare to LOCAL.
LIVE_LOCAL_OUT="$OUT_DIR/live-current-vs-local.txt"
echo "[em-helm-mwe] live (current venv)  vs  LOCAL scenario_state"
if [[ -f "$LOCAL_SS" ]]; then
    # Mismatch verdicts return exit 1 from the python script; that's a
    # valid finding here, not a script error. Don't abort the orchestrator.
    set +e
    python "$RUN_PY" \
        --scenario-output-path "$SCENARIO_CACHE" \
        --scenario-state "$LOCAL_SS" \
        --label local \
        | tee "$LIVE_LOCAL_OUT"
    set -e
else
    echo "  skipped (LOCAL_SS missing)"
fi
echo

# Run live → compare to OFFICIAL (almost certainly will NOT match in
# the current venv, because pandas is too new — but capture the diff
# anyway as evidence of the divergence).
LIVE_OFFICIAL_OUT="$OUT_DIR/live-current-vs-official.txt"
echo "[em-helm-mwe] live (current venv)  vs  OFFICIAL scenario_state"
if [[ -f "$OFFICIAL_SS" ]]; then
    set +e
    python "$RUN_PY" \
        --scenario-output-path "$SCENARIO_CACHE" \
        --scenario-state "$OFFICIAL_SS" \
        --label official \
        | tee "$LIVE_OFFICIAL_OUT"
    set -e
else
    echo "  skipped (OFFICIAL_SS missing)"
fi
echo

# ---------------------------------------------------------------------
# Pass 2: v0.3.0-era venv. Build a fresh venv with crfm-helm==0.3.0
# pinned to pandas 2.0.3 + numpy 1.23.5, then run the same script.
# ---------------------------------------------------------------------
echo "=========================================="
echo "[em-helm-mwe] Pass 2: v0.3.0-era venv"
echo "=========================================="
HELM_OLD_VENV="$OUT_DIR/venv-helm-0.3.0"
# HELM v0.3.0 setup.cfg: python_requires = >=3.8,<3.11.
# pyext 0.7 (transitive dep) uses inspect.getargspec which was removed
# in Python 3.11, so 3.11+ definitely fails. 3.10 is the most recent
# supported.
HELM_OLD_PY="3.10"

have_uv=0
if command -v uv >/dev/null 2>&1; then have_uv=1; fi

# Reuse only if the venv exists AND has helm importable; otherwise nuke
# and rebuild. (A previous run with an unsupported Python may have
# created the venv but failed to install crfm-helm.)
HELM_OLD_VENV_OK=0
if [[ -d "$HELM_OLD_VENV" ]] \
        && "$HELM_OLD_VENV/bin/python" -c "import helm" 2>/dev/null; then
    HELM_OLD_VENV_OK=1
fi
if [[ $HELM_OLD_VENV_OK -eq 1 ]]; then
    echo "[em-helm-mwe] reusing $HELM_OLD_VENV"
else
    if [[ -d "$HELM_OLD_VENV" ]]; then
        echo "[em-helm-mwe] $HELM_OLD_VENV exists but lacks importable helm"
        echo "[em-helm-mwe] removing and rebuilding"
        rm -rf "$HELM_OLD_VENV"
    fi
    if [[ $have_uv -eq 1 ]]; then
        uv venv "$HELM_OLD_VENV" --python "$HELM_OLD_PY"
        VIRTUAL_ENV="$HELM_OLD_VENV" uv pip install \
            "crfm-helm==0.3.0" "pandas==2.0.3" "numpy==1.23.5" \
            || { echo "[em-helm-mwe] install failed for v0.3.0 venv"; exit 2; }
    else
        python3 -m venv "$HELM_OLD_VENV"
        "$HELM_OLD_VENV/bin/pip" install --upgrade pip
        "$HELM_OLD_VENV/bin/pip" install "crfm-helm==0.3.0" "pandas==2.0.3" "numpy==1.23.5" \
            || { echo "[em-helm-mwe] install failed for v0.3.0 venv"; exit 2; }
    fi
fi

# Use a SEPARATE scenario cache so the v0.3.0 venv re-downloads (or
# re-uses if compatible) without contaminating Pass 1.
SCENARIO_CACHE_OLD="$OUT_DIR/scenario_cache_old"
mkdir -p "$SCENARIO_CACHE_OLD"

LIVE_OLD_OFFICIAL_OUT="$OUT_DIR/live-helm0.3.0-vs-official.txt"
echo "[em-helm-mwe] live (v0.3.0 venv)  vs  OFFICIAL scenario_state"
if [[ -f "$OFFICIAL_SS" ]]; then
    set +e
    "$HELM_OLD_VENV/bin/python" "$RUN_PY" \
        --scenario-output-path "$SCENARIO_CACHE_OLD" \
        --scenario-state "$OFFICIAL_SS" \
        --label official \
        | tee "$LIVE_OLD_OFFICIAL_OUT"
    set -e
else
    echo "  skipped (OFFICIAL_SS missing)"
fi
echo

LIVE_OLD_LOCAL_OUT="$OUT_DIR/live-helm0.3.0-vs-local.txt"
echo "[em-helm-mwe] live (v0.3.0 venv)  vs  LOCAL scenario_state"
if [[ -f "$LOCAL_SS" ]]; then
    set +e
    "$HELM_OLD_VENV/bin/python" "$RUN_PY" \
        --scenario-output-path "$SCENARIO_CACHE_OLD" \
        --scenario-state "$LOCAL_SS" \
        --label local \
        | tee "$LIVE_OLD_LOCAL_OUT"
    set -e
else
    echo "  skipped (LOCAL_SS missing)"
fi
echo

# ---------------------------------------------------------------------
# Verdict.
# ---------------------------------------------------------------------
echo "=========================================="
echo "[em-helm-mwe] verdict"
echo "=========================================="
verdict_for() {
    local f="$1"; local label="$2"
    if [[ ! -f "$f" ]]; then
        echo "  $label: skipped"
        return
    fi
    local v
    v=$(grep -E "^# VERDICT" "$f" | head -1)
    if [[ -z "$v" ]]; then
        v="(no verdict line; check $f)"
    fi
    echo "  $label: $v"
}
verdict_for "$LIVE_LOCAL_OUT"          "current venv  vs  LOCAL    "
verdict_for "$LIVE_OFFICIAL_OUT"       "current venv  vs  OFFICIAL "
verdict_for "$LIVE_OLD_OFFICIAL_OUT"   "v0.3.0  venv  vs  OFFICIAL "
verdict_for "$LIVE_OLD_LOCAL_OUT"      "v0.3.0  venv  vs  LOCAL    "
echo
echo "Full per-pass output files:"
ls -1 "$OUT_DIR"/live-*.txt 2>/dev/null | sed 's/^/  /'
