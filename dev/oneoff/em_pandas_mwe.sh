#!/usr/bin/env bash
# MWE: does pd.merge(labels, tableA, ...) + pd.merge(..., tableB, ...)
# produce different row orderings across (pandas, numpy) combos on the
# deepmatcher Abt-Buy data?
#
# Reproducer for the entity_matching official↔local divergence we
# observed in the slim heatmap (see
# paper_draft/2026-05-01_session_log.md). Symptom: same
# np.random.seed(0) downsample picks the same indices on both sides,
# but the content at each index differs — implying the upstream merged
# DataFrame is in a different row order between two HELM runs of the
# same scenario.
#
# Endpoints:
#   - "v0.3.0 era":  numpy~=1.23.3 was the HELM v0.3.0 pin.
#                    Pandas was unpinned but contemporaneously 2.0.x.
#   - User's aiq-gpu (Mar 2026): pandas 2.3.3, numpy 2.4.4 (numpy 2.x).
#
# Numpy made a major-version transition (1→2) between those endpoints.
# pandas merge uses numpy internals (argsort, hashing) and could
# produce different output ordering on the same inputs across that
# boundary.
#
# Run on a host with internet access and enough disk for ~5 minimal
# venvs. Do NOT run on aivm-2404 (FD limit). Idempotent: re-running
# skips cached venvs / outputs.
#
# Usage::
#
#     bash dev/oneoff/em_pandas_mwe.sh
#
# Outputs land under ./em_pandas_mwe_out/ in the CWD.

set -euo pipefail

# (pandas_version, numpy_version) pairs to compare. The first bracket
# is HELM v0.3.0's pin; the last is the user's actual aiq-gpu env. The
# middle three step through the pandas2/numpy1 → pandas2/numpy2
# transition to localize where ordering diverges (if it does).
COMBOS=(
  "2.0.3:1.23.5"   # close to HELM v0.3.0 era (numpy~=1.23.3)
  "2.0.3:1.26.4"   # numpy 1 latest, pandas 2.0
  "2.2.3:1.26.4"   # last pandas to support numpy 1 cleanly
  "2.2.3:2.0.0"    # first numpy 2 cross
  "2.3.3:2.4.4"    # user's actual aiq-gpu env
)

# Deepmatcher Abt-Buy URL (per HELM scenario source). Per upstream
# author: dataset has not been modified since 2018.
DEEPMATCHER_URL="http://pages.cs.wisc.edu/~anhai/data1/deepmatcher_data/Textual/Abt-Buy/abt_buy_exp_data.zip"

OUT_DIR="${OUT_DIR:-$PWD/em_pandas_mwe_out}"
EXTRACT_DIR="$OUT_DIR/data"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_PY="$SCRIPT_DIR/em_pandas_mwe_run.py"

if [[ ! -f "$RUN_PY" ]]; then
    echo "ERROR: $RUN_PY not found." >&2
    exit 1
fi

mkdir -p "$EXTRACT_DIR"

# 1. Fetch + unpack the deepmatcher ZIP (idempotent).
# Layout-tolerant: the zip may put CSVs at root, in an "exp_data/"
# subdir, or in some other vendor-specific subdir. We locate
# tableA.csv after extraction and treat its parent as DATA_DIR.
ZIP_PATH="$EXTRACT_DIR/abt_buy_exp_data.zip"
if [[ ! -f "$ZIP_PATH" ]]; then
    echo "[em-mwe] downloading $DEEPMATCHER_URL"
    curl -fsSL -o "$ZIP_PATH" "$DEEPMATCHER_URL"
fi

# Find tableA.csv anywhere in EXTRACT_DIR; if missing, extract.
TABLEA="$(find "$EXTRACT_DIR" -name 'tableA.csv' -type f -print -quit 2>/dev/null || true)"
if [[ -z "$TABLEA" ]]; then
    echo "[em-mwe] extracting $ZIP_PATH into $EXTRACT_DIR"
    unzip -o -q "$ZIP_PATH" -d "$EXTRACT_DIR"
    TABLEA="$(find "$EXTRACT_DIR" -name 'tableA.csv' -type f -print -quit 2>/dev/null || true)"
fi

if [[ -z "$TABLEA" ]]; then
    echo "ERROR: tableA.csv not found anywhere under $EXTRACT_DIR after extract." >&2
    echo "       Zip contents:" >&2
    unzip -l "$ZIP_PATH" >&2 | head -30
    exit 1
fi

DATA_DIR="$(dirname "$TABLEA")"
echo "[em-mwe] DATA_DIR=$DATA_DIR"

# Bytes-confirmation: record the same dataset went into every venv.
echo "[em-mwe] data digests:"
for f in tableA.csv tableB.csv train.csv valid.csv test.csv; do
    if [[ -f "$DATA_DIR/$f" ]]; then
        sha=$(sha256sum "$DATA_DIR/$f" | awk '{print $1}')
        sz=$(stat -c '%s' "$DATA_DIR/$f")
        echo "  $f  sha256=${sha:0:16}  size=$sz"
    fi
done
echo

# Choose a venv tool. uv preferred (fast); fall back to python -m venv + pip.
have_uv=0
if command -v uv >/dev/null 2>&1; then
    have_uv=1
fi

# Combo label: replace : with _ for filename safety.
combo_label() { echo "${1//:/_}"; }

# 2. Build one venv per (pandas, numpy) combo, install minimal deps, run
# the merge script, capture stdout. If a combo's pip install fails
# (incompatible versions), record that and continue.
for combo in "${COMBOS[@]}"; do
    pv="${combo%%:*}"
    nv="${combo##*:}"
    label=$(combo_label "$combo")
    VENV="$OUT_DIR/venv-$label"
    OUT="$OUT_DIR/out-$label.txt"
    echo "=========================================="
    echo "[em-mwe] combo=$combo  (pandas==$pv  numpy==$nv)"
    echo "=========================================="
    if [[ -f "$OUT" ]]; then
        echo "[em-mwe] $OUT already exists — skipping (rm to re-run)"
        continue
    fi
    if [[ ! -d "$VENV" ]]; then
        if [[ $have_uv -eq 1 ]]; then
            if ! uv venv "$VENV" --python 3.11 --quiet; then
                echo "[em-mwe] uv venv failed for $combo" | tee "$OUT"
                continue
            fi
            if ! VIRTUAL_ENV="$VENV" uv pip install --quiet \
                    "pandas==$pv" "numpy==$nv" 2>&1; then
                echo "[em-mwe] uv pip install failed for $combo (incompatible versions?)" | tee "$OUT"
                continue
            fi
        else
            python3 -m venv "$VENV" || { echo "[em-mwe] venv creation failed" | tee "$OUT"; continue; }
            "$VENV/bin/pip" install --quiet --upgrade pip
            if ! "$VENV/bin/pip" install --quiet "pandas==$pv" "numpy==$nv" 2>&1; then
                echo "[em-mwe] pip install failed for $combo" | tee "$OUT"
                continue
            fi
        fi
    fi
    if ! "$VENV/bin/python" "$RUN_PY" "$DATA_DIR" valid > "$OUT" 2>&1; then
        echo "[em-mwe] run script failed for $combo (see $OUT)"
        continue
    fi
    head -2 "$OUT"
    grep -E "^# full_order_digest=" "$OUT" || true
    echo
done

# 3. Compare full_order_digest across all combos.
echo "=========================================="
echo "[em-mwe] full_order_digest summary"
echo "=========================================="
DIGEST_FILE="$OUT_DIR/digest_summary.txt"
: > "$DIGEST_FILE"
for combo in "${COMBOS[@]}"; do
    label=$(combo_label "$combo")
    OUT="$OUT_DIR/out-$label.txt"
    if [[ -f "$OUT" ]]; then
        d=$(grep -E "^# full_order_digest=" "$OUT" | head -1 | sed 's/^# full_order_digest=//')
        if [[ -z "$d" ]]; then
            d="<no-digest:install-or-run-failed>"
        fi
        echo "$combo  full_order_digest=$d" | tee -a "$DIGEST_FILE"
    fi
done

# Count distinct digests (ignore failed runs).
n_distinct=$(awk '{print $2}' "$DIGEST_FILE" | grep -v "no-digest" | sort -u | wc -l)
echo
if [[ "$n_distinct" == "1" ]]; then
    echo "[em-mwe] VERDICT: all combos produced the SAME row ordering."
    echo "                 Pandas merge is stable across these"
    echo "                 (pandas, numpy) pairs on this dataset; the"
    echo "                 entity_matching divergence is NOT here."
    echo "                 Look upstream — e.g. read_csv parsing,"
    echo "                 train-split groupby/apply, or numpy rng"
    echo "                 advancement before downsample."
elif [[ "$n_distinct" -gt 1 ]]; then
    echo "[em-mwe] VERDICT: $n_distinct distinct full_order_digests."
    echo "                 Merge ordering IS combo-dependent on this"
    echo "                 dataset. Smoking gun."
    echo
    echo "[em-mwe] Pairwise diffs:"
    for ((i=0; i<${#COMBOS[@]}; i++)); do
        for ((j=i+1; j<${#COMBOS[@]}; j++)); do
            a="${COMBOS[$i]}"; b="${COMBOS[$j]}"
            A="$OUT_DIR/out-$(combo_label "$a").txt"
            B="$OUT_DIR/out-$(combo_label "$b").txt"
            if [[ -f "$A" && -f "$B" ]]; then
                first_diff=$(diff <(grep -v "^#" "$A") <(grep -v "^#" "$B") \
                              | grep -E "^[<>]" | head -1 || true)
                if [[ -z "$first_diff" ]]; then
                    echo "  $a  vs  $b : identical"
                else
                    echo "  $a  vs  $b : DIFFER"
                    echo "    first divergence: $first_diff"
                fi
            fi
        done
    done
else
    echo "[em-mwe] VERDICT: unable to evaluate — every combo failed to install/run."
    echo "                 Check $OUT_DIR/out-*.txt for error messages."
fi
