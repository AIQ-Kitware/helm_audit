#!/usr/bin/env python3
"""
EEE sweep: convert every official public HELM run through every_eval_ever convert helm.

Usage:
    python dev/poc/eee-audit/sweep.py [--workers N] [--limit N] [--suite SUITE]

Output root: /data/crfm-helm-audit-store/crfm-helm-public-eee-test/
DB index:    /data/crfm-helm-audit-store/crfm-helm-public-eee-test/sweep_index.db
Summary:     /data/crfm-helm-audit-store/crfm-helm-public-eee-test/summary.json
Log:         /data/crfm-helm-audit-store/crfm-helm-public-eee-test/results.jsonl

Each run's converted output and per-run status.json land under:
    {OUTPUT_ROOT}/{suite}/{version}/{run_name}/

The SQLite index (sweep_index.db) is the source of truth for skip decisions.
It is populated with all discovered runs (including file sizes) before any
conversion starts, so you can query it to plan exclusions:

    sqlite3 sweep_index.db "
        SELECT suite, exception_class, count(*) n
        FROM runs WHERE status='fail'
        GROUP BY suite, exception_class ORDER BY n DESC"

    sqlite3 sweep_index.db "
        SELECT run_name, scenario_state_mb FROM runs
        WHERE scenario_state_mb > 100 ORDER BY scenario_state_mb DESC LIMIT 20"
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PUBLIC_ROOT = Path(os.environ.get("HELM_PRECOMPUTED_ROOT", "/data/crfm-helm-public"))
OUTPUT_ROOT = Path(
    os.environ.get(
        "EEE_SWEEP_OUTPUT",
        "/data/crfm-helm-audit-store/crfm-helm-public-eee-test",
    )
)
EEE_CMD = os.environ.get("EEE_CMD", "every_eval_ever")

REQUIRED_FILES = {
    "run_spec.json",
    "scenario_state.json",
    "scenario.json",
    "per_instance_stats.json",
}

# Statuses that count as "done" for skip purposes by default.
# Pass --skip-statuses to override (e.g. also skip 'fail' to stop retrying).
DEFAULT_SKIP_STATUSES = {"ok", "skipped_large"}


# ---------------------------------------------------------------------------
# Run enumeration
# ---------------------------------------------------------------------------
def _is_valid_run_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    names = {p.name for p in path.iterdir()}
    return REQUIRED_FILES.issubset(names)


def enumerate_runs(public_root: Path, suite_filter: str | None = None):
    """
    Yield (suite, version, run_name, run_path) for every valid HELM run dir.

    Directory layout: {public_root}/{suite}/benchmark_output/runs/{version}/{run_name}
    """
    for suite_dir in sorted(public_root.iterdir()):
        if not suite_dir.is_dir():
            continue
        if suite_filter and suite_dir.name != suite_filter:
            continue
        bo_dir = suite_dir / "benchmark_output" / "runs"
        if not bo_dir.is_dir():
            continue
        for version_dir in sorted(bo_dir.iterdir()):
            if not version_dir.is_dir():
                continue
            for run_dir in sorted(version_dir.iterdir()):
                if _is_valid_run_dir(run_dir):
                    yield (suite_dir.name, version_dir.name, run_dir.name, run_dir)


# ---------------------------------------------------------------------------
# SQLite manifest
# ---------------------------------------------------------------------------
_CREATE_RUNS_TABLE = """
    CREATE TABLE IF NOT EXISTS runs (
        suite               TEXT NOT NULL,
        version             TEXT NOT NULL,
        run_name            TEXT NOT NULL,
        run_path            TEXT NOT NULL,
        scenario_state_mb   REAL,
        status              TEXT,
        exception_class     TEXT,
        failure_snippet     TEXT,
        returncode          INTEGER,
        attempt_count       INTEGER NOT NULL DEFAULT 0,
        updated_at          TEXT,
        PRIMARY KEY (suite, version, run_name)
    )
"""


def _configure_con(con: sqlite3.Connection) -> None:
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # FULL flushes WAL pages to disk before each commit, preventing the
    # B-tree corruption that NORMAL can leave behind after a hard kill (-9).
    con.execute("PRAGMA synchronous=FULL")


def _salvage_and_rebuild(db_path: Path) -> None:
    """
    Read all recoverable rows from a corrupt DB, back it up, and write a clean
    replacement.  Called automatically by open_db when integrity_check fails.
    """
    print(f"[DB] Corrupt DB detected at {db_path} — attempting recovery...")
    try:
        src = sqlite3.connect(str(db_path))
        src.row_factory = sqlite3.Row
        rows = src.execute("SELECT * FROM runs").fetchall()
        src.close()
        print(f"[DB] Salvaged {len(rows)} rows from corrupt DB")
    except Exception as exc:
        print(f"[DB] Could not read corrupt DB: {exc}. Starting empty.")
        rows = []

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = db_path.with_suffix(f".corrupt.{ts}.bak")
    db_path.rename(bak)
    print(f"[DB] Backed up corrupt file to {bak}")

    dst = sqlite3.connect(str(db_path))
    _configure_con(dst)
    dst.execute(_CREATE_RUNS_TABLE)
    if rows:
        dst.executemany(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [tuple(r) for r in rows],
        )
    dst.commit()
    dst.close()
    print(f"[DB] Rebuilt DB with {len(rows)} rows — integrity OK")


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists():
        try:
            probe = sqlite3.connect(str(db_path))
            result = probe.execute("PRAGMA quick_check").fetchone()[0]
            probe.close()
            if result != "ok":
                _salvage_and_rebuild(db_path)
        except Exception:
            _salvage_and_rebuild(db_path)

    con = sqlite3.connect(str(db_path), check_same_thread=False)
    _configure_con(con)
    con.execute(_CREATE_RUNS_TABLE)
    con.commit()
    return con


def populate_manifest(
    con: sqlite3.Connection,
    public_root: Path,
    suite_filter: str | None = None,
) -> int:
    """
    Scan all valid run dirs and upsert them into the DB.

    For each run, records the scenario_state.json file size in MB (cheap
    stat() call).  Rows that already exist are left untouched except that a
    NULL scenario_state_mb is filled in if we can measure it now.

    Returns the total number of runs discovered.
    """
    count = 0
    batch = []
    for suite, version, run_name, run_path in enumerate_runs(public_root, suite_filter):
        ss_path = run_path / "scenario_state.json"
        mb: float | None = None
        if ss_path.exists():
            try:
                mb = ss_path.stat().st_size / (1024 * 1024)
            except OSError:
                pass
        batch.append((suite, version, run_name, str(run_path), mb))
        count += 1
        if len(batch) >= 500:
            _upsert_batch(con, batch)
            batch.clear()
    if batch:
        _upsert_batch(con, batch)
    return count


def _upsert_batch(con: sqlite3.Connection, batch: list):
    con.executemany("""
        INSERT INTO runs (suite, version, run_name, run_path, scenario_state_mb)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (suite, version, run_name) DO UPDATE SET
            run_path = excluded.run_path,
            scenario_state_mb = COALESCE(runs.scenario_state_mb, excluded.scenario_state_mb)
    """, batch)
    con.commit()


def import_status_json_files(con: sqlite3.Connection, output_root: Path) -> int:
    """
    One-time backward-compat import: read existing per-run status.json files
    into the DB for runs that have no DB status yet.  Safe to call every time
    (the INSERT ... WHERE status IS NULL clause is a no-op once imported).
    """
    imported = 0
    for sf in output_root.rglob("status.json"):
        try:
            d = json.loads(sf.read_text())
        except Exception:
            continue
        suite = d.get("suite")
        version = d.get("version")
        run_name = d.get("run_name")
        status = d.get("status")
        if not (suite and version and run_name and status):
            continue
        cur = con.execute(
            "SELECT status FROM runs WHERE suite=? AND version=? AND run_name=?",
            (suite, version, run_name),
        ).fetchone()
        if cur is None or cur["status"] is None:
            con.execute("""
                UPDATE runs SET
                    status          = ?,
                    exception_class = ?,
                    failure_snippet = ?,
                    returncode      = ?,
                    attempt_count   = MAX(attempt_count, 1),
                    updated_at      = ?
                WHERE suite=? AND version=? AND run_name=?
            """, (
                status,
                d.get("exception_class"),
                d.get("failure_snippet"),
                d.get("returncode"),
                d.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                suite, version, run_name,
            ))
            imported += 1
    con.commit()
    return imported


def update_run_status(con: sqlite3.Connection, result: dict) -> None:
    con.execute("""
        UPDATE runs SET
            status          = ?,
            exception_class = ?,
            failure_snippet = ?,
            returncode      = ?,
            attempt_count   = attempt_count + 1,
            updated_at      = ?
        WHERE suite=? AND version=? AND run_name=?
    """, (
        result["status"],
        result.get("exception_class"),
        result.get("failure_snippet"),
        result.get("returncode"),
        datetime.now(timezone.utc).isoformat(),
        result["suite"],
        result["version"],
        result["run_name"],
    ))
    con.commit()


def get_run_status(
    con: sqlite3.Connection, suite: str, version: str, run_name: str
) -> str | None:
    row = con.execute(
        "SELECT status FROM runs WHERE suite=? AND version=? AND run_name=?",
        (suite, version, run_name),
    ).fetchone()
    return row["status"] if row else None


def print_db_summary(con: sqlite3.Connection) -> None:
    """Thin wrapper kept for --index-only; delegates to print_report."""
    print_report(con)


def print_failures(
    con: sqlite3.Connection,
    *,
    cls_filter: str | None = None,
    limit_per_class: int = 20,
) -> None:
    """Dump per-failure details. Read-only.

    With ``cls_filter=None``, walks every failure exception_class and prints
    up to ``limit_per_class`` rows from each. With a class name, only that
    class is shown. Useful for triaging stale failures before deciding how
    to retry them (``--retry-class``).
    """
    if cls_filter is None:
        rows = con.execute(
            """
            SELECT exception_class, COUNT(*) AS n
            FROM runs
            WHERE status IN ('fail','timeout','error')
            GROUP BY exception_class
            ORDER BY n DESC
            """
        ).fetchall()
        classes = [(r["exception_class"] or "UnknownError", r["n"]) for r in rows]
    else:
        n = con.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN ('fail','timeout','error') "
            "AND COALESCE(exception_class,'UnknownError') = ?",
            (cls_filter,),
        ).fetchone()[0]
        classes = [(cls_filter, n)]

    if not classes:
        print("No failures recorded in the DB.")
        return

    for cls, total in classes:
        print()
        print("=" * 72)
        print(f"  exception_class = {cls}   (total: {total})")
        print("=" * 72)
        rows = con.execute(
            """
            SELECT suite, version, run_name, run_path,
                   scenario_state_mb, returncode, attempt_count, updated_at,
                   failure_snippet
            FROM runs
            WHERE status IN ('fail','timeout','error')
              AND COALESCE(exception_class,'UnknownError') = ?
            ORDER BY suite, version, run_name
            LIMIT ?
            """,
            (cls, limit_per_class),
        ).fetchall()
        for r in rows:
            mb = f"{r['scenario_state_mb']:.1f}MB" if r["scenario_state_mb"] is not None else "size?"
            print(
                f"\n[{r['suite']}/{r['version']}] {r['run_name']}\n"
                f"  path: {r['run_path']}\n"
                f"  size: {mb}  rc: {r['returncode']}  "
                f"attempts: {r['attempt_count']}  updated: {r['updated_at']}"
            )
            snippet = (r["failure_snippet"] or "").rstrip()
            if snippet:
                for line in snippet.splitlines()[-8:]:
                    print(f"    {line}")
            else:
                print("    (no snippet recorded)")
        if total > len(rows):
            print(f"\n  ...and {total - len(rows)} more (raise --show-failures-limit to see)")


def print_report(con: sqlite3.Connection) -> None:
    """Print a full human-readable conversion report from the DB."""
    W = 72  # report width

    def rule(char="─"):
        print(char * W)

    def section(title):
        print()
        print(f"  {title}")
        rule()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rule("═")
    print(f"  EEE HELM CONVERSION REPORT   ({now})")
    rule("═")

    # ── Overall totals ────────────────────────────────────────────────────────
    totals = con.execute("""
        SELECT
            COUNT(*)                        AS discovered,
            SUM(status = 'ok')              AS ok,
            SUM(status = 'fail')            AS fail,
            SUM(status = 'timeout')         AS timeout,
            SUM(status = 'error')           AS error,
            SUM(status = 'skipped_large')   AS skip_large,
            SUM(status IS NULL)             AS pending,
            SUM(attempt_count > 0)          AS attempted
        FROM runs
    """).fetchone()

    n_total     = totals["discovered"] or 0
    n_ok        = totals["ok"] or 0
    n_fail      = totals["fail"] or 0
    n_timeout   = totals["timeout"] or 0
    n_error     = totals["error"] or 0
    n_skip_lg   = totals["skip_large"] or 0
    n_pending   = totals["pending"] or 0
    n_attempted = totals["attempted"] or 0
    n_bad       = n_fail + n_timeout + n_error

    def pct(n, d):
        return f"{100*n/d:5.1f}%" if d else "  n/a "

    section("OVERALL")
    print(f"  {'Total runs discovered':<36s}: {n_total:>7,}")
    print(f"  {'Attempted (at least once)':<36s}: {n_attempted:>7,}  {pct(n_attempted, n_total)} of total")
    print(f"  {'Pending (never attempted)':<36s}: {n_pending:>7,}  {pct(n_pending, n_total)} of total")
    print()
    print(f"  {'Succeeded (ok)':<36s}: {n_ok:>7,}  {pct(n_ok, n_total)} of total  |  {pct(n_ok, n_attempted)} of attempted")
    print(f"  {'Failed (any error)':<36s}: {n_bad:>7,}  {pct(n_bad, n_total)} of total  |  {pct(n_bad, n_attempted)} of attempted")
    print(f"    {'└─ non-zero exit':<34s}: {n_fail:>7,}")
    print(f"    {'└─ timeout':<34s}: {n_timeout:>7,}")
    print(f"    {'└─ runner error':<34s}: {n_error:>7,}")
    print(f"  {'Skipped (too large)':<36s}: {n_skip_lg:>7,}")

    # ── Per-suite breakdown ───────────────────────────────────────────────────
    section("PER-SUITE BREAKDOWN")
    suite_rows = con.execute("""
        SELECT suite,
               COUNT(*)                                        AS total,
               SUM(status = 'ok')                             AS ok,
               SUM(status IN ('fail','timeout','error'))       AS bad,
               SUM(status = 'skipped_large')                  AS skip_lg,
               SUM(status IS NULL)                            AS pending,
               ROUND(AVG(COALESCE(scenario_state_mb,0)),1)    AS avg_mb,
               ROUND(MAX(COALESCE(scenario_state_mb,0)),1)    AS max_mb
        FROM runs
        GROUP BY suite
        ORDER BY suite
    """).fetchall()

    hdr = (f"  {'suite':<26s} {'total':>6} {'ok':>6} {'ok%':>6} "
           f"{'fail':>5} {'skip_lg':>7} {'pending':>7} {'avg_mb':>7} {'max_mb':>7}")
    print(hdr)
    print("  " + "─" * (W - 2))
    for r in suite_rows:
        t  = r["total"] or 0
        ok = r["ok"] or 0
        print(
            f"  {r['suite']:<26s} {t:6d} {ok:6d} {pct(ok,t):>6} "
            f"{r['bad'] or 0:5d} {r['skip_lg'] or 0:7d} {r['pending'] or 0:7d} "
            f"{r['avg_mb'] or 0:7.1f} {r['max_mb'] or 0:7.1f}"
        )

    # ── Failure modes ─────────────────────────────────────────────────────────
    fail_rows = con.execute("""
        SELECT
            exception_class,
            COUNT(*)                            AS n,
            GROUP_CONCAT(DISTINCT suite)        AS suites
        FROM runs
        WHERE status IN ('fail','timeout','error')
        GROUP BY exception_class
        ORDER BY n DESC
    """).fetchall()

    if fail_rows:
        section("FAILURE MODES")
        hdr2 = f"  {'exception_class':<45s} {'count':>6}  suites"
        print(hdr2)
        print("  " + "─" * (W - 2))
        for r in fail_rows:
            ec     = (r["exception_class"] or "unknown")[:44]
            suites = (r["suites"] or "")
            # wrap suite list if long
            suite_str = suites if len(suites) <= 20 else suites[:17] + "..."
            print(f"  {ec:<45s} {r['n']:6d}  {suite_str}")

        # One representative snippet per failure class
        print()
        print("  Representative failure snippets:")
        for r in fail_rows:
            ec = r["exception_class"] or "unknown"
            example = con.execute("""
                SELECT suite, version, run_name, failure_snippet
                FROM runs
                WHERE status IN ('fail','timeout','error')
                  AND (exception_class = ? OR (exception_class IS NULL AND ? = 'unknown'))
                LIMIT 1
            """, (ec, ec)).fetchone()
            if not example or not example["failure_snippet"]:
                continue
            print()
            print(f"  [{ec}]  {example['suite']}/{example['version']}/{example['run_name'][:40]}")
            for line in (example["failure_snippet"] or "").splitlines()[-5:]:
                print(f"    {line}")

    # ── File-size distribution ────────────────────────────────────────────────
    section("SCENARIO_STATE.JSON SIZE DISTRIBUTION")
    thresholds = [1, 8, 32, 64, 128, 256, 512]
    prev = 0
    size_rows = []

    def _bucket_counts(lo, hi=None):
        if hi is not None:
            r = con.execute(
                "SELECT COUNT(*), SUM(status='ok') FROM runs"
                " WHERE scenario_state_mb >= ? AND scenario_state_mb < ?",
                (lo, hi),
            ).fetchone()
        else:
            r = con.execute(
                "SELECT COUNT(*), SUM(status='ok') FROM runs WHERE scenario_state_mb >= ?",
                (lo,),
            ).fetchone()
        return r[0] or 0, r[1] or 0

    for t in thresholds:
        cnt, ok = _bucket_counts(prev, t)
        size_rows.append((f"{prev}–{t} MB", cnt, ok))
        prev = t
    cnt_over, ok_over = _bucket_counts(thresholds[-1])
    r_null = con.execute(
        "SELECT COUNT(*), SUM(status='ok') FROM runs WHERE scenario_state_mb IS NULL"
    ).fetchone()
    size_rows.append((f"≥ {thresholds[-1]} MB", cnt_over, ok_over))
    size_rows.append(("size unknown", r_null[0] or 0, r_null[1] or 0))

    bar_scale = max(r[1] for r in size_rows) or 1
    for label, cnt, ok in size_rows:
        bar_w = int(30 * cnt / bar_scale)
        ok_pct = f"{100*ok/cnt:5.1f}%" if cnt else "   n/a"
        bar = "█" * bar_w
        print(f"  {label:>12s}  {cnt:6,}  {ok:6,} ok  {ok_pct}  {bar}")

    # ── Retry candidates ──────────────────────────────────────────────────────
    retry_rows = con.execute("""
        SELECT suite, exception_class, COUNT(*) n
        FROM runs
        WHERE status IN ('fail','timeout','error')
          AND exception_class != 'FileNotFoundError'
        GROUP BY suite, exception_class
        ORDER BY n DESC
        LIMIT 15
    """).fetchall()

    if retry_rows:
        section("RETRY CANDIDATES  (failures excluding expected media-asset errors)")
        hdr3 = f"  {'suite':<26s} {'exception_class':<40s} {'count':>6}"
        print(hdr3)
        print("  " + "─" * (W - 2))
        for r in retry_rows:
            print(f"  {r['suite']:<26s} {(r['exception_class'] or 'unknown'):<40s} {r['n']:6d}")

    print()


# ---------------------------------------------------------------------------
# Per-run output path (kept for detailed stderr/stdout artifacts)
# ---------------------------------------------------------------------------
def output_subdir(suite: str, version: str, run_name: str) -> Path:
    return OUTPUT_ROOT / suite / version / run_name


def status_file(suite: str, version: str, run_name: str) -> Path:
    return output_subdir(suite, version, run_name) / "status.json"


# ---------------------------------------------------------------------------
# Conversion worker (runs in a subprocess worker — no DB access here)
# ---------------------------------------------------------------------------
def convert_one(args_tuple):
    """
    Run every_eval_ever convert helm on a single HELM run directory.
    Returns a dict with status info.
    """
    suite, version, run_name, run_path, timeout_s, max_mb = args_tuple

    # Pre-flight: skip runs whose scenario_state.json exceeds the size cap.
    scenario_state_path = Path(run_path) / "scenario_state.json"
    if max_mb is not None and scenario_state_path.exists():
        size_mb = scenario_state_path.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            result = {
                "suite": suite,
                "version": version,
                "run_name": run_name,
                "run_path": str(run_path),
                "status": "skipped_large",
                "exception_class": "FileTooLarge",
                "failure_snippet": f"scenario_state.json is {size_mb:.0f} MB > limit {max_mb} MB",
                "scenario_state_mb": round(size_mb, 1),
            }
            sf = status_file(suite, version, run_name)
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text(json.dumps(result, indent=2))
            return result

    out_dir = output_subdir(suite, version, run_name)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        EEE_CMD,
        "convert",
        "helm",
        "--log_path",
        str(run_path),
        "--output_dir",
        str(out_dir / "eee_output"),
        "--source_organization_name",
        "CRFM",
        "--evaluator_relationship",
        "third_party",
        "--eval_library_name",
        "HELM",
        "--eval_library_version",
        "unknown",
    ]

    result = {
        "suite": suite,
        "version": version,
        "run_name": run_name,
        "run_path": str(run_path),
        "out_dir": str(out_dir),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cmd": cmd,
    }

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout[-4000:] if proc.stdout else ""
        result["stderr"] = proc.stderr[-12000:] if proc.stderr else ""

        if proc.returncode == 0:
            result["status"] = "ok"
        else:
            result["status"] = "fail"
            if proc.returncode < 0:
                sig = -proc.returncode
                exc_class = f"Signal_{sig}" if sig != 9 else "SIGKILL_OOM"
                result["exception_class"] = exc_class
                result["failure_snippet"] = f"Process killed by signal {sig}"
            else:
                exc_class = _extract_exception_class(proc.stderr)
                result["exception_class"] = exc_class
                result["failure_snippet"] = _extract_failure_snippet(proc.stderr)
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["exception_class"] = "TimeoutExpired"
        result["failure_snippet"] = f"Process timed out after {timeout_s}s"
        result["returncode"] = -1
        result["stdout"] = ""
        result["stderr"] = ""
    except Exception:
        result["status"] = "error"
        exc_class = type(sys.exc_info()[1]).__name__
        result["exception_class"] = exc_class
        result["failure_snippet"] = traceback.format_exc()[-2000:]
        result["returncode"] = -1
        result["stdout"] = ""
        result["stderr"] = ""

    # Write per-run status.json (for detailed stderr/stdout inspection)
    sf = status_file(suite, version, run_name)
    sf.write_text(json.dumps(result, indent=2))

    return result


def _extract_exception_class(stderr: str) -> str:
    """Extract the outermost exception class from a Python traceback."""
    lines = stderr.strip().splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("File ", " ", "~", "^", ".")):
            continue
        if "Error" in stripped or "Exception" in stripped:
            if ":" in stripped:
                exc_part = stripped.split(":")[0].strip()
                return exc_part.split(".")[-1]
            if stripped.split() and (
                "Error" in stripped.split()[0] or "Exception" in stripped.split()[0]
            ):
                return stripped.split()[0]
    return "UnknownError"


def _extract_failure_snippet(stderr: str) -> str:
    lines = stderr.strip().splitlines()
    return "\n".join(lines[-10:])


# ---------------------------------------------------------------------------
# Summary writer (reads from DB for full-corpus view)
# ---------------------------------------------------------------------------
def write_summary(con: sqlite3.Connection, summary_path: Path) -> dict:
    rows = con.execute("""
        SELECT status, exception_class, run_path, failure_snippet
        FROM runs
    """).fetchall()

    n_ok = n_fail = n_timeout = n_error = n_skip_large = n_pending = 0
    exc_counter: Counter = Counter()
    exc_examples: dict[str, list] = defaultdict(list)
    failed_paths = []

    for r in rows:
        st = r["status"]
        if st == "ok":
            n_ok += 1
        elif st == "fail":
            n_fail += 1
        elif st == "timeout":
            n_timeout += 1
        elif st == "error":
            n_error += 1
        elif st == "skipped_large":
            n_skip_large += 1
        else:
            n_pending += 1

        if st in ("fail", "timeout", "error"):
            ec = r["exception_class"] or "UnknownError"
            exc_counter[ec] += 1
            failed_paths.append(r["run_path"])
            if len(exc_examples[ec]) < 3:
                exc_examples[ec].append({
                    "run_path": r["run_path"],
                    "failure_snippet": r["failure_snippet"] or "",
                })

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "public_root": str(PUBLIC_ROOT),
        "output_root": str(OUTPUT_ROOT),
        "totals": {
            "discovered": len(rows),
            "succeeded": n_ok,
            "failed": n_fail + n_timeout + n_error,
            "skipped_too_large": n_skip_large,
            "pending": n_pending,
        },
        "failure_breakdown": {ec: cnt for ec, cnt in exc_counter.most_common()},
        "failure_examples": dict(exc_examples),
        "failed_run_paths": failed_paths,
    }
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------
def _effective_skip(
    status: str | None,
    scenario_state_mb: float | None,
    skip_statuses: set[str],
    max_mb: float | None,
) -> bool:
    """
    Return True if this run should be skipped given the current configuration.

    For most statuses a simple set-membership check is sufficient.  For
    'skipped_large' we also verify that the stored file size still exceeds the
    *current* threshold — if the user raised or removed the threshold the run
    should be retried, not silently skipped.

    If the stored size is unknown (None) and the status is 'skipped_large' we
    conservatively keep skipping; a future --index-only pass will populate the
    size and unblock it on the next real run.
    """
    if status not in skip_statuses:
        return False
    if status == "skipped_large":
        if max_mb is None:
            return False  # threshold removed entirely — retry everything
        if scenario_state_mb is None:
            return True   # size unknown; trust the recorded status for now
        return scenario_state_mb > max_mb
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Sweep all official public HELM runs through every_eval_ever convert helm."
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap number of runs to process (for testing)")
    parser.add_argument("--suite", default=None,
                        help="Only process this benchmark suite")
    parser.add_argument("--no-skip", action="store_true",
                        help="Re-run everything, ignoring existing statuses")
    parser.add_argument(
        "--skip-statuses",
        default=",".join(sorted(DEFAULT_SKIP_STATUSES)),
        help="Comma-separated list of statuses to treat as done and skip. "
             f"Default: '{','.join(sorted(DEFAULT_SKIP_STATUSES))}'. "
             "Checked against both the per-run status.json (filesystem) and the "
             "DB cache. Add 'fail' to stop retrying failures, e.g. "
             "--skip-statuses ok,skipped_large,fail",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="Do not use the SQLite DB at all — filesystem status.json only. "
             "The DB is a stats cache; this flag makes that explicit.",
    )
    parser.add_argument("--timeout", type=int, default=300,
                        help="Per-run subprocess timeout in seconds (default: 300)")
    parser.add_argument(
        "--max-scenario-state-mb",
        type=float, default=512.0, dest="max_mb",
        help="Skip runs whose scenario_state.json exceeds this MB (default: 512). "
             "Use 0 to disable.",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="PATTERN",
        help="Glob pattern against '{suite}/{version}/{run_name}'. May repeat.",
    )
    parser.add_argument(
        "--index-only", action="store_true",
        help="Populate the DB manifest and print status table, then exit without running.",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print a conversion report from the existing DB and exit. "
             "Does not scan for new runs or run any conversions.",
    )
    parser.add_argument(
        "--db", default=None,
        help="Path to SQLite DB (default: OUTPUT_ROOT/sweep_index.db)",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Restrict the candidate set to runs whose recorded status is in "
             "{fail,timeout,error}. Implies the candidate set excludes 'ok', "
             "'skipped_large', and pending rows. Combine with --suite, "
             "--exclude, and --retry-class to narrow further.",
    )
    parser.add_argument(
        "--retry-class", default=None, metavar="CLASS",
        help="When set, restrict to rows with the given exception_class "
             "(e.g. SIGKILL_OOM, UnknownError, TypeError, FileNotFoundError). "
             "Implies --retry-failed.",
    )
    parser.add_argument(
        "--show-failures", default=None, metavar="CLASS", const="ALL", nargs="?",
        help="Print details (run path, snippet, version, size) of failed rows "
             "and exit. With no argument, shows all classes; pass a class "
             "name to filter. Read-only.",
    )
    parser.add_argument(
        "--show-failures-limit", type=int, default=20,
        help="Max number of failures to print per class for --show-failures.",
    )
    args = parser.parse_args()

    if args.max_mb == 0:
        args.max_mb = None

    skip_statuses: set[str] = set()
    if not args.no_skip:
        skip_statuses = {s.strip() for s in args.skip_statuses.split(",") if s.strip()}

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    db_path = Path(args.db) if args.db else OUTPUT_ROOT / "sweep_index.db"
    results_jsonl = OUTPUT_ROOT / "results.jsonl"
    summary_path = OUTPUT_ROOT / "summary.json"

    # --report: read-only, no scanning, no conversions
    if args.report:
        con = open_db(db_path)
        print_report(con)
        con.close()
        return

    # --show-failures: read-only failure dump
    if args.show_failures is not None:
        con = open_db(db_path)
        print_failures(
            con,
            cls_filter=None if args.show_failures == "ALL" else args.show_failures,
            limit_per_class=args.show_failures_limit,
        )
        con.close()
        return

    # --retry-class implies --retry-failed.
    if args.retry_class:
        args.retry_failed = True

    print(f"Public HELM root : {PUBLIC_ROOT}")
    print(f"Output root      : {OUTPUT_ROOT}")
    print(f"DB index         : {'(disabled --no-db)' if args.no_db else db_path}")
    print(f"Workers          : {args.workers}")
    print(f"Timeout          : {args.timeout}s")
    print(f"Max scenario MB  : {args.max_mb if args.max_mb else 'unlimited'}")
    print(f"Skip statuses    : {sorted(skip_statuses)}")

    con: sqlite3.Connection | None = None
    if not args.no_db:
        con = open_db(db_path)
        # Always re-scan the source tree so new runs appear in the DB.
        print("Scanning public HELM root for run directories...")
        n_discovered = populate_manifest(con, PUBLIC_ROOT, suite_filter=args.suite)
        print(f"Manifest: {n_discovered} runs discovered (suite filter: {args.suite or 'all'})")
        # Import any pre-existing per-run status.json files into the DB.
        n_imported = import_status_json_files(con, OUTPUT_ROOT)
        if n_imported:
            print(f"Imported {n_imported} existing status.json results into DB")

    if args.index_only:
        if con is None:
            print("--index-only requires the DB (remove --no-db)")
            return
        print()
        print_db_summary(con)
        con.close()
        return

    # Build the candidate list from the DB (or by re-enumerating the filesystem
    # if --no-db was passed, in which case con is None and we skip DB entirely).
    if con is not None:
        query = "SELECT suite, version, run_name, run_path, status, scenario_state_mb FROM runs"
        where_parts: list[str] = []
        params: list = []
        if args.suite:
            where_parts.append("suite = ?")
            params.append(args.suite)
        if args.retry_failed:
            # Restrict to recorded failures. Pending and successful rows are
            # skipped at the SQL layer so the candidate set is exactly the
            # failures we want to re-attempt.
            where_parts.append("status IN ('fail','timeout','error')")
        if args.retry_class:
            # Match against the stored exception_class, treating NULL as the
            # 'UnknownError' bucket (consistent with print_failures + report).
            where_parts.append("COALESCE(exception_class,'UnknownError') = ?")
            params.append(args.retry_class)
        if where_parts:
            query += " WHERE " + " AND ".join(where_parts)
        query += " ORDER BY suite, version, run_name"
        all_runs = [
            (r["suite"], r["version"], r["run_name"], r["run_path"],
             r["status"], r["scenario_state_mb"])
            for r in con.execute(query, params).fetchall()
        ]
        if args.retry_failed or args.retry_class:
            print(
                "Retry filter: status in (fail,timeout,error)"
                + (f", exception_class={args.retry_class}" if args.retry_class else "")
                + f"  → {len(all_runs)} candidate rows from DB"
            )
            # When retrying recorded failures we explicitly want to re-attempt
            # them, so an existing on-disk status.json marked as 'fail' must
            # not skip the row. Drop those statuses from skip_statuses too.
            skip_statuses = skip_statuses - {"fail", "timeout", "error"}
    else:
        if args.retry_failed or args.retry_class:
            print("--retry-failed/--retry-class require the DB (do not pass --no-db)")
            return
        all_runs = [
            (suite, version, run_name, str(run_path), None, None)
            for suite, version, run_name, run_path in enumerate_runs(PUBLIC_ROOT, args.suite)
        ]

    to_run = []
    n_skipped = 0
    for suite, version, run_name, run_path, db_status, db_mb in all_runs:
        run_key = f"{suite}/{version}/{run_name}"

        # Glob exclusions
        if args.exclude and any(fnmatch.fnmatch(run_key, pat) for pat in args.exclude):
            n_skipped += 1
            continue

        if skip_statuses:
            # Primary: filesystem status.json (the original skip mechanism).
            sf = status_file(suite, version, run_name)
            if sf.exists():
                try:
                    d = json.loads(sf.read_text())
                    fs_status = d.get("status")
                    fs_mb = d.get("scenario_state_mb")
                except Exception:
                    fs_status = fs_mb = None
            else:
                fs_status = fs_mb = None

            if _effective_skip(fs_status, fs_mb, skip_statuses, args.max_mb):
                n_skipped += 1
                continue

            # Secondary: DB cache (allows skipping runs marked via SQL without
            # needing a status.json on disk, e.g. manually inserted rows).
            if _effective_skip(db_status, db_mb, skip_statuses, args.max_mb):
                n_skipped += 1
                continue

        to_run.append((suite, version, run_name, Path(run_path)))

    print(f"Skipping {n_skipped} runs (status in skip set or excluded)")
    print(f"Running  {len(to_run)} runs")

    if args.limit:
        to_run = to_run[: args.limit]
        print(f"Capped at {len(to_run)} runs (--limit)")

    if to_run:
        with open(results_jsonl, "a") as log_f:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(convert_one, (*t, args.timeout, args.max_mb)): t
                    for t in to_run
                }
                done_count = 0
                for future in as_completed(futures):
                    done_count += 1
                    try:
                        result = future.result()
                    except Exception:
                        run_tuple = futures[future]
                        suite, version, run_name, run_path = run_tuple
                        result = {
                            "suite": suite,
                            "version": version,
                            "run_name": run_name,
                            "run_path": str(run_path),
                            "status": "error",
                            "exception_class": "FutureError",
                            "failure_snippet": traceback.format_exc()[-1000:],
                        }

                    if con is not None:
                        update_run_status(con, result)
                    log_f.write(json.dumps(result) + "\n")
                    log_f.flush()

                    status = result["status"]
                    mark = "OK" if status == "ok" else f"FAIL({result.get('exception_class', status)})"
                    print(
                        f"[{done_count}/{len(to_run)}] {mark}"
                        f" {result['suite']}/{result['version']}/{result['run_name'][:60]}"
                    )

    if con is not None:
        summary = write_summary(con, summary_path)
        print()
        print("=" * 70)
        print("SWEEP COMPLETE")
        print(f"  Discovered : {summary['totals']['discovered']}")
        print(f"  Succeeded  : {summary['totals']['succeeded']}")
        print(f"  Failed     : {summary['totals']['failed']}")
        print(f"  Pending    : {summary['totals']['pending']}")
        print(f"  Skip large : {summary['totals']['skipped_too_large']}")
        if summary["failure_breakdown"]:
            print()
            print("Failure breakdown:")
            for exc, cnt in summary["failure_breakdown"].items():
                print(f"  {exc:50s} {cnt:6d}")
        print()
        print(f"DB index : {db_path}")
        print(f"Summary  : {summary_path}")
    print(f"JSONL log: {results_jsonl}")

    if con is not None:
        con.close()


if __name__ == "__main__":
    main()
