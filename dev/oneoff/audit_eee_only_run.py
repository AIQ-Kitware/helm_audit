#!/usr/bin/env python3
"""
Run a command with a Python audit hook installed in every child Python
process, recording every ``open`` / ``os.open`` event to per-PID JSONL
logs. After the command exits, classify the recorded paths and emit a
PASS/FAIL verdict on whether any HELM run-dir JSON was touched.

Why this exists
---------------
The paper claim under audit (Case Study 3) is:

    "EEE's per-instance schema is sufficient for reproducibility
    analysis at multiple granularities."

[`eval_audit/normalized/loaders.py`](../../eval_audit/normalized/loaders.py)
contains a silent HELM fallback in ``EeeArtifactLoader.load`` that
reads ``per_instance_stats.json`` / ``scenario_state.json`` from the
HELM run dir when one is on disk and overwrites the EEE-derived
instances. ``EVAL_AUDIT_EEE_STRICT=1`` disables it. This wrapper
provides the corresponding *evidence* — a runtime trace of every file
opened during the analysis. If no ``run_spec.json`` /
``scenario.json`` / ``scenario_state.json`` / ``stats.json`` /
``per_instance_stats.json`` was opened, and no path under
``benchmark_output/runs/`` was opened, the run was honestly EEE-only.

Threat model
------------
- Catches: any file that Python opens via ``open()`` /
  ``pathlib.Path.open()`` / ``json.load(file)`` /
  ``Path.read_text/read_bytes`` / etc., and any ``os.open()`` call
  Python makes.
- Misses: file reads from C extensions that bypass the Python audit
  framework (e.g., a C extension calling ``fopen()`` directly without
  routing through CPython's IO layer). For belt-and-suspenders against
  this, use ``strace -fe trace=openat`` on the same command and grep
  the trace for the same patterns.

Mechanism
---------
1. Write a ``sitecustomize.py`` to a temp dir; that module is imported
   automatically at every Python startup if its directory is on
   ``sys.path``.
2. Prepend that temp dir to ``PYTHONPATH`` so every child Python
   process inherits the hook (including ``eval-audit-from-eee``'s
   per-packet ``core_metrics`` subprocesses and the aggregate-summary
   subprocess).
3. The hook writes one JSONL record per ``open`` / ``os.open`` event
   to ``$EVAL_AUDIT_AUDIT_LOG_DIR/audit_<pid>.jsonl``.
4. After the wrapped command exits, walk the log dir, classify each
   recorded path, and write ``audit_eee_only_run.report.json`` plus a
   stdout summary. Exit nonzero if any HELM-shaped path was touched.

Usage
-----
    # Wrapper around 20_run.sh (the analysis step). Make sure the
    # EEE-only env-var stack is exported in the parent shell first
    # (see paper_draft/2026-05-01_session_log.md §"Env-var stack").
    python3 dev/oneoff/audit_eee_only_run.py \\
        --log-dir /data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim/audit \\
        --report-out /data/crfm-helm-audit-store/eee-only-reproducibility-heatmap-paper-slim/audit/audit_eee_only_run.report.json \\
        -- bash reproduce/eee_only_reproducibility_heatmap/20_run.sh

Exit codes
----------
- 0: wrapped command exited 0 AND no HELM-shaped open was recorded.
- 1: wrapped command exited nonzero (its exit code is preserved).
- 2: wrapped command exited 0 but at least one HELM-shaped open was
     recorded — the analysis cheated and the paper claim is invalid
     for this run.
- 3: wrapper itself failed (bad arguments, can't write log dir, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


# Embedded child-side hook. Kept as a string so the wrapper can drop it
# into a temp directory at runtime without polluting the repo's import
# tree. The hook itself avoids re-entry via a thread-local flag and
# never raises out of audit-event delivery.
SITECUSTOMIZE_TEMPLATE = r'''
"""Audit hook installed by dev/oneoff/audit_eee_only_run.py.

Activated only when EVAL_AUDIT_AUDIT_LOG_DIR is set. Writes one JSONL
record per `open` / `os.open` event to
    $EVAL_AUDIT_AUDIT_LOG_DIR/audit_<pid>.jsonl
The wrapper aggregates these after the command exits.
"""
import json as _json
import os as _os
import sys as _sys
import threading as _threading

_LOG_DIR = _os.environ.get("EVAL_AUDIT_AUDIT_LOG_DIR", "")
if _LOG_DIR:
    _LOG_PATH = _os.path.join(_LOG_DIR, "audit_%d.jsonl" % _os.getpid())
    _LOCK = _threading.Lock()
    _LOCAL = _threading.local()
    _FH = [None]  # list-cell so the closure can mutate it

    def _audit_eee_only_hook(event, args):
        # Only inspect the two events that correspond to opening files.
        # Ignore everything else (import, exec, compile, etc.).
        if event != "open" and event != "os.open":
            return
        # Re-entry guard: opening the log file itself fires `open`. The
        # guard makes that nested event a no-op so we don't recurse.
        if getattr(_LOCAL, "in_hook", False):
            return
        _LOCAL.in_hook = True
        try:
            if _FH[0] is None:
                with _LOCK:
                    if _FH[0] is None:
                        try:
                            _os.makedirs(_LOG_DIR, exist_ok=True)
                            _FH[0] = open(_LOG_PATH, "a", buffering=1)
                        except Exception:
                            return
            try:
                path = args[0] if args else ""
                if isinstance(path, (bytes, bytearray)):
                    path = path.decode("utf-8", "replace")
                elif not isinstance(path, str):
                    path = str(path)
            except Exception:
                return
            try:
                _FH[0].write(_json.dumps(
                    {"event": event, "path": path, "pid": _os.getpid()}
                ) + "\n")
            except Exception:
                pass
        finally:
            _LOCAL.in_hook = False

    try:
        _sys.addaudithook(_audit_eee_only_hook)
    except Exception:
        pass
'''


# HELM run-dir signatures. Any open whose basename matches one of these
# names, OR whose path contains the HELM benchmark_output/runs subtree,
# counts as a HELM-shaped read.
HELM_FILENAMES = frozenset({
    "run_spec.json",
    "scenario.json",
    "scenario_state.json",
    "stats.json",
    "per_instance_stats.json",
})

HELM_PATH_SUBSTRINGS = (
    "/benchmark_output/runs/",
)


def classify(path: str) -> str | None:
    """Return a flag label if ``path`` looks HELM-shaped, else None."""
    base = os.path.basename(path)
    if base in HELM_FILENAMES:
        return f"helm_filename:{base}"
    for sub in HELM_PATH_SUBSTRINGS:
        if sub in path:
            return f"helm_path_substring:{sub}"
    return None


def aggregate(log_dir: Path) -> dict:
    """Walk the log dir, parse every JSONL line, classify, summarize."""
    log_files = sorted(log_dir.glob("audit_*.jsonl"))
    total_records = 0
    flagged: list[dict] = []
    by_basename: dict[str, int] = {}
    seen_paths: set[str] = set()
    for log_file in log_files:
        try:
            with log_file.open("r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    total_records += 1
                    path = rec.get("path", "")
                    if not isinstance(path, str):
                        continue
                    seen_paths.add(path)
                    label = classify(path)
                    if label:
                        flagged.append({
                            "path": path,
                            "event": rec.get("event"),
                            "pid": rec.get("pid"),
                            "label": label,
                        })
                    base = os.path.basename(path)
                    by_basename[base] = by_basename.get(base, 0) + 1
        except Exception as e:
            print(f"  WARN: could not parse {log_file}: {e}", file=sys.stderr)

    by_basename_top = sorted(
        by_basename.items(), key=lambda kv: kv[1], reverse=True
    )[:30]
    return {
        "n_log_files": len(log_files),
        "n_total_open_records": total_records,
        "n_unique_paths": len(seen_paths),
        "n_flagged_opens": len(flagged),
        "flagged_opens": flagged,
        "top_basenames": by_basename_top,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a command under a file-open audit hook and emit a "
            "PASS/FAIL verdict on whether any HELM run-dir JSON was "
            "touched. See module docstring for full mechanism."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--log-dir", required=True,
        help=(
            "Directory for per-PID JSONL audit logs. Will be created. "
            "The directory is preserved on exit so the raw evidence is "
            "available for re-aggregation."
        ),
    )
    parser.add_argument(
        "--report-out", default=None,
        help=(
            "Path for the structured JSON report. Default: "
            "<log-dir>/audit_eee_only_run.report.json."
        ),
    )
    parser.add_argument(
        "--require-strict-flag", action="store_true",
        help=(
            "Refuse to run unless EVAL_AUDIT_EEE_STRICT is set to a "
            "truthy value. Use this for the canonical paper run so the "
            "audit cannot accidentally be done with the silent HELM "
            "fallback enabled."
        ),
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="The command to run (separate from wrapper args with --).",
    )
    args = parser.parse_args()

    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("ERROR: no command given (use -- before the command).", file=sys.stderr)
        return 3

    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(
        args.report_out or (log_dir / "audit_eee_only_run.report.json")
    ).resolve()

    if args.require_strict_flag:
        v = os.environ.get("EVAL_AUDIT_EEE_STRICT", "").strip().lower()
        if v not in {"1", "true", "yes"}:
            print(
                "ERROR: --require-strict-flag is set but "
                "EVAL_AUDIT_EEE_STRICT is not truthy "
                f"(got {v!r}). Refusing to produce a paper-validity "
                "report when the silent HELM fallback could be active.",
                file=sys.stderr,
            )
            return 3

    # Drop the audit-hook sitecustomize into a temp dir and prepend it
    # to PYTHONPATH so every child Python process auto-imports it.
    sitedir = tempfile.mkdtemp(prefix="audit_eee_only_sc_")
    sitecustomize_path = Path(sitedir) / "sitecustomize.py"
    sitecustomize_path.write_text(SITECUSTOMIZE_TEMPLATE)

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        sitedir + (os.pathsep + existing_pp if existing_pp else "")
    )
    env["EVAL_AUDIT_AUDIT_LOG_DIR"] = str(log_dir)

    print(f"[audit-eee-only] log dir:           {log_dir}", file=sys.stderr)
    print(f"[audit-eee-only] sitecustomize:     {sitecustomize_path}", file=sys.stderr)
    print(f"[audit-eee-only] EEE strict flag:   "
          f"{env.get('EVAL_AUDIT_EEE_STRICT', '<unset>')}", file=sys.stderr)
    print(f"[audit-eee-only] running:           {' '.join(cmd)}", file=sys.stderr)

    try:
        proc = subprocess.run(cmd, env=env)
    except FileNotFoundError as e:
        print(f"ERROR: command not found: {e}", file=sys.stderr)
        return 3

    print(f"[audit-eee-only] command exit code: {proc.returncode}", file=sys.stderr)

    summary = aggregate(log_dir)
    summary["command"] = cmd
    summary["command_exit_code"] = proc.returncode
    summary["env_eee_strict"] = os.environ.get("EVAL_AUDIT_EEE_STRICT", "")
    summary["env_skip_helm_diagnosis"] = os.environ.get(
        "EVAL_AUDIT_SKIP_HELM_DIAGNOSIS", ""
    )
    summary["env_trust_eee_schema"] = os.environ.get(
        "EVAL_AUDIT_TRUST_EEE_SCHEMA", ""
    )
    report_path.write_text(json.dumps(summary, indent=2) + "\n")

    print()
    print(f"[audit-eee-only] report:            {report_path}")
    print(f"[audit-eee-only] log files:         {summary['n_log_files']}")
    print(f"[audit-eee-only] open records:      {summary['n_total_open_records']}")
    print(f"[audit-eee-only] unique paths:      {summary['n_unique_paths']}")
    print(f"[audit-eee-only] flagged opens:     {summary['n_flagged_opens']}")

    if summary["n_flagged_opens"]:
        print()
        print("[audit-eee-only] VERDICT: FAIL — analysis read HELM-shaped files.")
        print("[audit-eee-only] First 20 flagged paths:")
        for f in summary["flagged_opens"][:20]:
            print(f"  {f['label']}\t{f['path']}")
        if proc.returncode == 0:
            return 2
        return 1

    if proc.returncode != 0:
        print()
        print("[audit-eee-only] VERDICT: command failed; no HELM-shaped opens "
              "recorded but the wrapped command itself returned nonzero.")
        return 1

    print()
    print("[audit-eee-only] VERDICT: PASS — no HELM-shaped opens recorded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
