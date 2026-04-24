#!/usr/bin/env bash
set -euo pipefail

RUN_GS="gs://crfm-helm-public/classic/benchmark_output/runs/v0.3.0/mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"
RUN_REL="classic/benchmark_output/runs/v0.3.0/mmlu:subject=us_foreign_policy,method=multiple_choice_joint,model=eleutherai_pythia-6.9b,data_augmentation=canonical"

TMPDIR="$(mktemp -d -t eee-helm-mwe-XXXXXX)"
SRCROOT="$TMPDIR/src"
OUTROOT="$TMPDIR/out"
mkdir -p "$SRCROOT" "$OUTROOT"

echo "TMPDIR=$TMPDIR"

download_with_public_http() {
  python - "$SRCROOT" "$RUN_REL" <<'PY'
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

dst_root = Path(sys.argv[1])
prefix = sys.argv[2]
bucket = "crfm-helm-public"

def list_objects(bucket: str, prefix: str):
    token = None
    while True:
        params = {"prefix": prefix + "/"}
        if token:
            params["pageToken"] = token
        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{urllib.parse.quote(bucket, safe='')}/o?"
            f"{urllib.parse.urlencode(params)}"
        )
        with urllib.request.urlopen(url) as resp:
            data = json.load(resp)
        for item in data.get("items", []):
            yield item["name"]
        token = data.get("nextPageToken")
        if not token:
            break

objects = list(list_objects(bucket, prefix))
if not objects:
    raise RuntimeError(f"No public objects found for prefix={prefix!r}")

for name in objects:
    rel = Path(name)
    dst = dst_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw_url = f"https://storage.googleapis.com/{bucket}/{urllib.parse.quote(name, safe='/')}"
    print(f"download {raw_url} -> {dst}")
    with urllib.request.urlopen(raw_url) as resp, dst.open("wb") as f:
        f.write(resp.read())

print(str(dst_root / prefix))
PY
}

if command -v gsutil >/dev/null 2>&1; then
  echo "Using gsutil to fetch public folder"
  gsutil -m cp -r "$RUN_GS" "$SRCROOT/"
  RUN_DIR="$SRCROOT/$(basename "$RUN_GS")"
else
  echo "gsutil not found; using public HTTP fallback"
  RUN_DIR="$(download_with_public_http)"
fi

echo "RUN_DIR=$RUN_DIR"
test -d "$RUN_DIR"

echo
echo "== Inspect downloaded run_spec =="
python - "$RUN_DIR" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
data = json.loads((run_dir / "run_spec.json").read_text())
adapter = data.get("adapter_spec", {})
print("run_dir =", run_dir)
print("model =", adapter.get("model"))
print("model_deployment =", repr(adapter.get("model_deployment")))
assert adapter.get("model") == "eleutherai/pythia-6.9b"
assert adapter.get("model_deployment") is None
PY

echo
echo "== Run every_eval_ever conversion and expect current failure =="
set +e
every_eval_ever convert helm \
  --log-path "$RUN_DIR" \
  --output-dir "$OUTROOT/converted" \
  --source-organization-name "CRFM" \
  --evaluator-relationship third_party \
  --eval-library-name "HELM" \
  --eval-library-version "v0.3.0" \
  >"$TMPDIR/stdout.txt" 2>"$TMPDIR/stderr.txt"
status=$?
set -e

echo "--- stdout ---"
cat "$TMPDIR/stdout.txt" || true
echo "--- stderr ---"
cat "$TMPDIR/stderr.txt" || true

if [[ "$status" -eq 0 ]]; then
  echo "ERROR: expected conversion to fail, but it succeeded"
  exit 1
fi

grep -q "ModelDeploymentNotFoundError" "$TMPDIR/stderr.txt" || {
  echo "ERROR: conversion failed, but not with the expected ModelDeploymentNotFoundError"
  exit 1
}

echo
echo "Reproduced expected failure in temp dir:"
echo "  $TMPDIR"
