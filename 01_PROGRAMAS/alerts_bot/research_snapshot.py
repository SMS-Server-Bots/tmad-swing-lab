from __future__ import annotations

"""Immutable input snapshots for long-running historical research.

The live Data/Alerts processes keep writing while a historical replay can take
hours. This module creates a point-in-time research view without stopping
production:

* SQLite inputs are copied with SQLite's online backup API.
* immutable archive ZIPs are hard-linked when possible (copy fallback).
* append-only research journals are copied, never hard-linked.
* only research readers are redirected to the snapshot; runtime state writes and
  report outputs continue to target production.

The snapshot is deleted when the replay finishes or fails, so no unbounded
04_RUNTIME growth is introduced.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Callable, Iterator

_ACTIVE: dict[str, Any] | None = None


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def research_snapshot_active() -> bool:
    return _ACTIVE is not None


def research_db_path(path: Path | str) -> Path:
    source = Path(path)
    if not _ACTIVE:
        return source
    mapped = (_ACTIVE.get("db_map") or {}).get(_norm(source))
    return Path(mapped) if mapped else source


def research_input_path(path: Path | str) -> Path:
    source = Path(path)
    if not _ACTIVE:
        return source
    mapped = (_ACTIVE.get("file_map") or {}).get(_norm(source))
    return Path(mapped) if mapped else source



def _copy_query_rows(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    query: str,
    *,
    batch_size: int = 2000,
) -> int:
    cols = [str(row[1]) for row in src.execute(f"PRAGMA table_info({table})").fetchall()]
    if not cols:
        return 0
    placeholders = ",".join("?" for _ in cols)
    insert_sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    cur = src.execute(query)
    copied = 0
    while True:
        batch = cur.fetchmany(max(100, int(batch_size)))
        if not batch:
            break
        dst.executemany(insert_sql, batch)
        copied += len(batch)
    return copied


def _compact_feature_query(src: sqlite3.Connection) -> tuple[str, str]:
    cols = {str(row[1]) for row in src.execute("PRAGMA table_info(feature_observations)").fetchall()}
    predicates: list[str] = []
    if "observation_layer" in cols:
        predicates.append("observation_layer IN ('daily_close','core_history')")
    if "is_canonical_daily_close" in cols:
        predicates.append("COALESCE(is_canonical_daily_close,0)=1")
    has_outcomes = (
        "id" in cols
        and _table_exists(src, "observation_outcomes")
        and "observation_id" in {
            str(row[1])
            for row in src.execute("PRAGMA table_info(observation_outcomes)").fetchall()
        }
    )
    if has_outcomes:
        predicates.append(
            "id IN (SELECT DISTINCT observation_id FROM observation_outcomes "
            "WHERE observation_id IS NOT NULL)"
        )

    # Preserve the exact operational-run selection universe used by
    # _counterfactual_current_policy_inputs(): for each mature H5 date, keep
    # every row belonging to the latest run whose cross-section is >=50% of the
    # daily maximum and contains at least four rows. This protects semantics
    # even if that selected run is not explicitly tagged daily_close.
    if (
        has_outcomes
        and "snapshot_date" in cols
        and "source_scanner_run_id" in cols
    ):
        predicates.append(
            """EXISTS (
                WITH mature_dates AS (
                    SELECT DISTINCT observation_date AS d
                    FROM observation_outcomes
                    WHERE complete=1 AND horizon_sessions=5
                ),
                run_counts AS (
                    SELECT snapshot_date,source_scanner_run_id,COUNT(*) AS n
                    FROM feature_observations
                    WHERE snapshot_date IN (SELECT d FROM mature_dates)
                    GROUP BY snapshot_date,source_scanner_run_id
                ),
                max_counts AS (
                    SELECT snapshot_date,MAX(n) AS max_n
                    FROM run_counts GROUP BY snapshot_date
                ),
                chosen AS (
                    SELECT rc.snapshot_date,MAX(rc.source_scanner_run_id) AS run_id
                    FROM run_counts rc
                    JOIN max_counts mc ON mc.snapshot_date=rc.snapshot_date
                    WHERE rc.n>=4 AND rc.n*2>=mc.max_n
                    GROUP BY rc.snapshot_date
                )
                SELECT 1 FROM chosen c
                WHERE c.snapshot_date=feature_observations.snapshot_date
                  AND c.run_id=feature_observations.source_scanner_run_id
            )"""
        )

    if not predicates:
        return "SELECT * FROM feature_observations", "FALLBACK_FULL_COPY"
    return (
        "SELECT * FROM feature_observations WHERE " + " OR ".join(f"({p})" for p in predicates),
        "RESEARCH_RELEVANT_FEATURE_ROWS_V2",
    )


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _attached_table_columns(conn: sqlite3.Connection, schema: str, table: str) -> set[str]:
    rows = conn.execute(
        f"PRAGMA {_quote_ident(schema)}.table_info({_quote_ident(table)})"
    ).fetchall()
    return {str(row[1]) for row in rows}


def _attached_table_exists(conn: sqlite3.Connection, schema: str, table: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {_quote_ident(schema)}.sqlite_master "
        "WHERE type='table' AND name=?",
        (str(table),),
    ).fetchone()
    return row is not None


def _bulk_insert_feature_observations(
    conn: sqlite3.Connection,
    *,
    source_schema: str = "srcdb",
    destination_schema: str = "main",
) -> tuple[int, str, dict]:
    """Copy the exact v2 research-relevant feature universe inside SQLite.

    The old implementation evaluated the same policy through a Python
    fetchmany/executemany bridge. This version materializes the two small lookup
    sets once in TEMP and lets SQLite execute one INSERT..SELECT, preserving the
    identical row policy while avoiding Python marshaling for multi-GB tables.
    """
    feature_cols = _attached_table_columns(conn, source_schema, "feature_observations")
    predicates: list[str] = []
    diagnostics = {
        "outcome_ids": 0,
        "chosen_runs": 0,
        "policy": "FALLBACK_FULL_COPY",
    }

    if "observation_layer" in feature_cols:
        predicates.append("f.observation_layer IN ('daily_close','core_history')")
    if "is_canonical_daily_close" in feature_cols:
        predicates.append("COALESCE(f.is_canonical_daily_close,0)=1")

    outcome_cols: set[str] = set()
    has_outcomes = (
        "id" in feature_cols
        and _attached_table_exists(conn, source_schema, "observation_outcomes")
    )
    if has_outcomes:
        outcome_cols = _attached_table_columns(conn, source_schema, "observation_outcomes")
        has_outcomes = "observation_id" in outcome_cols

    if has_outcomes:
        conn.execute(
            """CREATE TEMP TABLE _tmad_outcome_ids(
                   observation_id INTEGER PRIMARY KEY
               ) WITHOUT ROWID"""
        )
        conn.execute(
            f"""INSERT OR IGNORE INTO temp._tmad_outcome_ids(observation_id)
                SELECT observation_id
                FROM {_quote_ident(source_schema)}.observation_outcomes
                WHERE observation_id IS NOT NULL"""
        )
        diagnostics["outcome_ids"] = int(
            conn.execute("SELECT COUNT(*) FROM temp._tmad_outcome_ids").fetchone()[0] or 0
        )
        predicates.append(
            "EXISTS (SELECT 1 FROM temp._tmad_outcome_ids oi "
            "WHERE oi.observation_id=f.id)"
        )

    # Exact operational-run universe from RESEARCH_RELEVANT_FEATURE_ROWS_V2.
    # Materialize chosen (date,run) pairs once instead of running the correlated
    # CTE for every feature_observations row.
    if (
        has_outcomes
        and "observation_date" in outcome_cols
        and "complete" in outcome_cols
        and "horizon_sessions" in outcome_cols
        and "snapshot_date" in feature_cols
        and "source_scanner_run_id" in feature_cols
    ):
        conn.execute(
            """CREATE TEMP TABLE _tmad_chosen_runs(
                   snapshot_date TEXT NOT NULL,
                   run_id INTEGER NOT NULL,
                   PRIMARY KEY(snapshot_date,run_id)
               ) WITHOUT ROWID"""
        )
        conn.execute(
            f"""INSERT OR IGNORE INTO temp._tmad_chosen_runs(snapshot_date,run_id)
                WITH mature_dates AS (
                    SELECT DISTINCT observation_date AS d
                    FROM {_quote_ident(source_schema)}.observation_outcomes
                    WHERE complete=1 AND horizon_sessions=5
                ),
                run_counts AS (
                    SELECT snapshot_date,source_scanner_run_id,COUNT(*) AS n
                    FROM {_quote_ident(source_schema)}.feature_observations
                    WHERE snapshot_date IN (SELECT d FROM mature_dates)
                    GROUP BY snapshot_date,source_scanner_run_id
                ),
                max_counts AS (
                    SELECT snapshot_date,MAX(n) AS max_n
                    FROM run_counts
                    GROUP BY snapshot_date
                )
                SELECT rc.snapshot_date,MAX(rc.source_scanner_run_id) AS run_id
                FROM run_counts rc
                JOIN max_counts mc ON mc.snapshot_date=rc.snapshot_date
                WHERE rc.n>=4 AND rc.n*2>=mc.max_n
                GROUP BY rc.snapshot_date"""
        )
        diagnostics["chosen_runs"] = int(
            conn.execute("SELECT COUNT(*) FROM temp._tmad_chosen_runs").fetchone()[0] or 0
        )
        predicates.append(
            "EXISTS (SELECT 1 FROM temp._tmad_chosen_runs cr "
            "WHERE cr.snapshot_date=f.snapshot_date "
            "AND cr.run_id=f.source_scanner_run_id)"
        )

    src = f"{_quote_ident(source_schema)}.feature_observations"
    dst = f"{_quote_ident(destination_schema)}.feature_observations"
    if predicates:
        where = " WHERE " + " OR ".join(f"({item})" for item in predicates)
        policy = "RESEARCH_RELEVANT_FEATURE_ROWS_V2"
    else:
        where = ""
        policy = "FALLBACK_FULL_COPY"
    conn.execute(f"INSERT INTO {dst} SELECT f.* FROM {src} AS f{where}")
    copied = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    diagnostics["policy"] = policy

    conn.execute("DROP TABLE IF EXISTS temp._tmad_chosen_runs")
    conn.execute("DROP TABLE IF EXISTS temp._tmad_outcome_ids")
    return copied, policy, diagnostics


def _sqlite_compact_alerts_snapshot(
    source: Path,
    destination: Path,
    progress_callback: Callable | None = None,
    *,
    quick_check: bool = False,
    full_integrity_check: bool = False,
) -> dict:
    """Create the immutable compact Alerts snapshot with SQLite-native bulk copy.

    All user tables and explicit indexes are preserved. feature_observations
    keeps the exact RESEARCH_RELEVANT_FEATURE_ROWS_V2 universe used since 6.2.41.
    The physical copy is now ATTACH + INSERT..SELECT inside one SQLite
    transaction, eliminating multi-GB Python row marshaling.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    source_bytes = int(source.stat().st_size)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    dst = sqlite3.connect(destination, uri=True, timeout=120)
    table_rows: dict[str, int] = {}
    table_seconds: dict[str, float] = {}
    feature_policy = "NOT_APPLICABLE"
    feature_diagnostics: dict[str, Any] = {}

    def _progress(stage: str, detail: str) -> None:
        if not callable(progress_callback):
            return
        try:
            progress_callback(stage, detail)
        except Exception:
            pass

    try:
        dst.execute("PRAGMA busy_timeout=120000")
        dst.execute("PRAGMA journal_mode=OFF")
        dst.execute("PRAGMA synchronous=OFF")
        dst.execute("PRAGMA temp_store=MEMORY")
        dst.execute("PRAGMA foreign_keys=OFF")
        dst.execute("ATTACH DATABASE ? AS srcdb", (source_uri,))
        dst.execute("BEGIN")

        # Anchor the source WAL read view before copying any table so all source
        # reads belong to one immutable transaction while production continues.
        dst.execute("SELECT COUNT(*) FROM srcdb.sqlite_master").fetchone()

        tables = dst.execute(
            """SELECT name,sql FROM srcdb.sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        ).fetchall()
        for table, create_sql in tables:
            if not create_sql:
                continue
            table_name = str(table)
            table_started = time.perf_counter()
            _progress(table_name, "running")
            dst.execute(str(create_sql))
            if table_name == "feature_observations":
                copied, feature_policy, feature_diagnostics = _bulk_insert_feature_observations(
                    dst,
                    source_schema="srcdb",
                    destination_schema="main",
                )
            else:
                qtable = _quote_ident(table_name)
                dst.execute(
                    f"INSERT INTO main.{qtable} SELECT * FROM srcdb.{qtable}"
                )
                copied = int(dst.execute("SELECT changes()").fetchone()[0] or 0)
            table_rows[table_name] = copied
            elapsed = time.perf_counter() - table_started
            table_seconds[table_name] = round(elapsed, 3)
            _progress(table_name, f"done rows={copied} seconds={elapsed:.3f}")

        # Verify table cardinality before building explicit indexes. The snapshot
        # is disposable research state and the indexes are created by SQLite from
        # these freshly verified table rows. Running quick_check *after* building
        # every explicit index made verification scale with multi-GB index trees
        # and duplicated work already performed by CREATE INDEX.
        row_verify_started = time.perf_counter()
        _progress("row_count_verification", "running")
        row_count_mismatches: dict[str, dict[str, int]] = {}
        for table_name, expected_rows in table_rows.items():
            qtable = _quote_ident(table_name)
            actual_rows = int(
                dst.execute(f"SELECT COUNT(*) FROM main.{qtable}").fetchone()[0] or 0
            )
            if actual_rows != int(expected_rows):
                row_count_mismatches[table_name] = {
                    "expected": int(expected_rows),
                    "actual": actual_rows,
                }
        table_seconds["__row_count_verification__"] = round(
            time.perf_counter() - row_verify_started, 3
        )
        _progress(
            "row_count_verification",
            f"done mismatches={len(row_count_mismatches)} "
            f"seconds={table_seconds['__row_count_verification__']}",
        )
        if row_count_mismatches:
            raise RuntimeError(
                "compact Alerts snapshot row-count verification failed: "
                + json.dumps(row_count_mismatches, sort_keys=True)
            )

        # Routine FULL deliberately skips PRAGMA quick_check. This database was
        # just materialized by SQLite in the current process; every copied table
        # has already been read successfully and its persisted cardinality is
        # verified above. A full-file quick_check on a multi-GB disposable
        # snapshot duplicates that scan and became an hour-scale bottleneck.
        # Operators can still opt into pre-index quick_check or the stronger
        # post-index integrity_check for diagnostics/forensics.
        integrity_pragma = "integrity_check" if full_integrity_check else "quick_check"
        integrity = "skipped"
        if bool(quick_check) and not full_integrity_check:
            integrity_started = time.perf_counter()
            _progress(integrity_pragma, "running pre_index=true opt_in=true")
            integrity = str(
                dst.execute(f"PRAGMA {integrity_pragma}").fetchone()[0]
            ).lower()
            integrity_key = f"__{integrity_pragma}__"
            table_seconds[integrity_key] = round(
                time.perf_counter() - integrity_started, 3
            )
            _progress(
                integrity_pragma,
                f"done status={integrity} pre_index=true opt_in=true "
                f"seconds={table_seconds[integrity_key]}",
            )
            if integrity != "ok":
                raise RuntimeError(
                    f"compact Alerts snapshot {integrity_pragma} failed: {integrity}"
                )
        elif not full_integrity_check:
            _progress(
                "structural_check",
                "skipped routine_quick_check=false; exact row counts + SQLite index build are authoritative",
            )

        index_started = time.perf_counter()
        _progress("indexes", "running")
        indexes = dst.execute(
            """SELECT name,sql FROM srcdb.sqlite_master
               WHERE type='index' AND sql IS NOT NULL
               ORDER BY name"""
        ).fetchall()
        indexes_created = 0
        indexes_skipped = 0
        index_failures: list[str] = []
        for index_name, index_sql in indexes:
            try:
                dst.execute(str(index_sql))
                indexes_created += 1
            except sqlite3.OperationalError:
                # A legacy index can refer to a column omitted by an older schema.
                # The table data remains authoritative for read-only research.
                indexes_skipped += 1
                index_failures.append(str(index_name))
                continue
        table_seconds["__indexes__"] = round(time.perf_counter() - index_started, 3)
        _progress(
            "indexes",
            f"done created={indexes_created} skipped={indexes_skipped} "
            f"seconds={table_seconds['__indexes__']}",
        )

        dst.commit()

        if full_integrity_check:
            integrity_started = time.perf_counter()
            _progress(integrity_pragma, "running post_index=true")
            integrity = str(
                dst.execute(f"PRAGMA {integrity_pragma}").fetchone()[0]
            ).lower()
            integrity_key = f"__{integrity_pragma}__"
            table_seconds[integrity_key] = round(
                time.perf_counter() - integrity_started, 3
            )
            _progress(
                integrity_pragma,
                f"done status={integrity} post_index=true "
                f"seconds={table_seconds[integrity_key]}",
            )
            if integrity != "ok":
                raise RuntimeError(
                    f"compact Alerts snapshot {integrity_pragma} failed: {integrity}"
                )
        dst.execute("DETACH DATABASE srcdb")
    except Exception:
        try:
            dst.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            dst.close()
        except Exception:
            pass

    snapshot_bytes = int(destination.stat().st_size)
    return {
        "source": str(source),
        "snapshot": str(destination),
        "source_bytes": source_bytes,
        "bytes": snapshot_bytes,
        "saved_bytes": max(0, source_bytes - snapshot_bytes),
        "saved_pct": round(max(0.0, 100.0 * (source_bytes - snapshot_bytes) / source_bytes), 3) if source_bytes else 0.0,
        "seconds": round(time.perf_counter() - started, 3),
        "mode": "SQLITE_COMPACT_RESEARCH_COPY_V2_ATTACH_BULK",
        "feature_policy": feature_policy,
        "feature_diagnostics": feature_diagnostics,
        "verification": {
            "mode": (
                "FULL_INTEGRITY_CHECK_POST_INDEX"
                if full_integrity_check
                else (
                    "EXACT_ROW_COUNTS_PLUS_OPT_IN_PRE_INDEX_QUICK_CHECK_PLUS_SQLITE_INDEX_BUILD"
                    if quick_check
                    else "EXACT_ROW_COUNTS_PLUS_SQLITE_INDEX_BUILD"
                )
            ),
            "quick_check": bool(quick_check and not full_integrity_check),
            "full_integrity_check": bool(full_integrity_check),
            "row_count_mismatches": 0,
            "structural_check": (
                "integrity_check"
                if full_integrity_check
                else ("quick_check" if quick_check else "SKIPPED_ROUTINE")
            ),
            "structural_check_scope": (
                "POST_INDEX"
                if full_integrity_check
                else ("TABLE_PAYLOAD_PRE_EXPLICIT_INDEX" if quick_check else "NOT_RUN")
            ),
            "routine_structural_scan_skipped": bool(not quick_check and not full_integrity_check),
            "explicit_indexes_created": int(indexes_created),
            "explicit_indexes_skipped": int(indexes_skipped),
            "explicit_index_failures": list(index_failures),
            "explicit_indexes_built_by_sqlite_from_verified_rows": True,
        },
        "table_rows": table_rows,
        "table_seconds": table_seconds,
    }


def _sqlite_backup(source: Path, destination: Path) -> dict:
    """Capture one immutable SQLite view without incremental backup restarts.

    The source databases run in WAL mode and production keeps writing while the
    replay runs. A paged backup can repeatedly revisit changed pages on a large
    live database. Using one backup step pins the source read view for the whole
    copy. Destination durability is intentionally disabled because the snapshot
    is disposable research state: a crash simply discards and rebuilds it.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    src = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro",
        uri=True,
        timeout=120,
    )
    dst = sqlite3.connect(destination, timeout=120)
    try:
        src.execute("PRAGMA query_only=ON")
        dst.execute("PRAGMA journal_mode=OFF")
        dst.execute("PRAGMA synchronous=OFF")
        dst.execute("PRAGMA temp_store=MEMORY")
        src.backup(dst, pages=-1, sleep=0.0)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return {
        "source": str(source),
        "snapshot": str(destination),
        "bytes": int(destination.stat().st_size),
        "seconds": round(time.perf_counter() - started, 3),
        "mode": "SQLITE_BACKUP_ONE_SHOT",
    }


def _copy_journal(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return {"source": str(source), "snapshot": str(destination), "exists": False, "bytes": 0}
    shutil.copy2(source, destination)
    return {
        "source": str(source),
        "snapshot": str(destination),
        "exists": True,
        "bytes": int(destination.stat().st_size),
    }


def _link_immutable(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
        return True
    except Exception:
        shutil.copy2(source, destination)
        return False


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _snapshot_layer_archives(alerts_snapshot: Path, live_root: Path, frozen_root: Path) -> tuple[list[dict], dict[str, str]]:
    refs: list[tuple[str, str]] = []
    conn = sqlite3.connect(alerts_snapshot)
    try:
        if _table_exists(conn, "layer_archive_catalog"):
            cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(layer_archive_catalog)").fetchall()}
            verified_where = "WHERE verified=1" if "verified" in cols else ""
            rows = conn.execute(
                f"SELECT trade_date,zip_path FROM layer_archive_catalog {verified_where} ORDER BY trade_date"
            ).fetchall()
            refs = [(str(row[0] or ""), str(row[1] or "")) for row in rows]
    finally:
        conn.close()

    # Older schemas may not expose a catalog. Fall back to the archive set that
    # existed at snapshot time.
    if not refs and live_root.exists():
        refs = [
            (path.stem.replace("layers_", ""), str(path))
            for path in sorted(live_root.glob("????-??/layers_????-??-??.zip"))
        ]

    manifest: list[dict] = []
    mapping: dict[str, str] = {}
    for trade_date, raw_path in refs:
        source = Path(raw_path)
        if not source.is_absolute():
            source = live_root / source
        if not source.is_file():
            # Catalogs normally store absolute paths, but old rows can only
            # carry the filename. Recover from the canonical layout.
            candidate = live_root / trade_date[:7] / f"layers_{trade_date}.zip"
            if candidate.is_file():
                source = candidate
        if not source.is_file():
            manifest.append({"trade_date": trade_date, "source": str(source), "status": "MISSING"})
            continue
        destination = frozen_root / trade_date[:7] / f"layers_{trade_date}.zip"
        linked = _link_immutable(source, destination)
        mapping[_norm(source)] = str(destination)
        manifest.append({
            "trade_date": trade_date,
            "source": str(source),
            "snapshot": str(destination),
            "bytes": int(destination.stat().st_size),
            "mode": "HARDLINK" if linked else "COPY",
        })
    return manifest, mapping


def _source_archive_refs(alerts_snapshot: Path) -> list[str]:
    conn = sqlite3.connect(alerts_snapshot)
    try:
        if not _table_exists(conn, "feature_observations"):
            return []
        rows = conn.execute(
            """SELECT DISTINCT source_archive
               FROM feature_observations
               WHERE source_archive IS NOT NULL AND TRIM(source_archive)<>''"""
        ).fetchall()
        return sorted({str(row[0]).strip() for row in rows if str(row[0] or "").strip()})
    finally:
        conn.close()


def _snapshot_data_archives(alerts_snapshot: Path, live_root: Path, frozen_root: Path) -> tuple[list[dict], dict[str, str]]:
    manifest: list[dict] = []
    mapping: dict[str, str] = {}
    for archive_name in _source_archive_refs(alerts_snapshot):
        raw = Path(archive_name)
        source = raw if raw.is_absolute() else live_root / raw
        if not source.is_file():
            manifest.append({"archive_name": archive_name, "source": str(source), "status": "MISSING"})
            continue

        try:
            relative = source.resolve().relative_to(live_root.resolve())
            destination = frozen_root / relative
        except Exception:
            token = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
            destination = frozen_root / "_external" / f"{token}_{source.name}"

        linked = _link_immutable(source, destination)
        mapping[_norm(source)] = str(destination)
        manifest.append({
            "archive_name": archive_name,
            "source": str(source),
            "snapshot": str(destination),
            "bytes": int(destination.stat().st_size),
            "mode": "HARDLINK" if linked else "COPY",
        })
    return manifest, mapping


def _prune_stale_snapshots(root: Path, max_age_hours: int = 36) -> None:
    if not root.exists():
        return
    cutoff = time.time() - max(1, int(max_age_hours)) * 3600
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except Exception:
            pass


def create_research_snapshot(config_module, progress_callback: Callable | None = None) -> dict:
    snapshots_root = Path(config_module.RUNTIME_ROOT) / "research_snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    _prune_stale_snapshots(snapshots_root)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = f"{stamp}_{os.getpid()}"
    root = snapshots_root / snapshot_id
    db_dir = root / "db"
    journals_dir = root / "journals"
    layer_dir = root / "layer_archives"
    data_archive_dir = root / "data_archives"
    root.mkdir(parents=True, exist_ok=True)

    def _progress(stage: str, detail: str) -> None:
        if not callable(progress_callback):
            return
        try:
            progress_callback(stage, detail)
        except Exception:
            pass

    db_map: dict[str, str] = {}
    db_manifest: dict[str, dict] = {}
    db_specs = {
        "alerts": Path(config_module.ALERTS_DB),
        "scanner": Path(config_module.SCANNER_DB),
        "gex": Path(config_module.GEX_DB),
    }
    for label, source in db_specs.items():
        if not source.is_file():
            db_manifest[label] = {"source": str(source), "exists": False}
            continue
        destination = db_dir / f"{label}.db"
        source_mb = round(source.stat().st_size / (1024 * 1024), 1)
        _progress(f"sqlite_{label}", f"running source_mb={source_mb}")
        if label == "alerts":
            backup = _sqlite_compact_alerts_snapshot(
                source,
                destination,
                progress_callback=lambda component, detail: _progress(
                    f"sqlite_alerts/{component}",
                    detail,
                ),
                quick_check=bool(
                    getattr(
                        config_module,
                        "RESEARCH_SNAPSHOT_QUICK_CHECK",
                        False,
                    )
                ),
                full_integrity_check=bool(
                    getattr(
                        config_module,
                        "RESEARCH_SNAPSHOT_FULL_INTEGRITY_CHECK",
                        False,
                    )
                ),
            )
        else:
            backup = _sqlite_backup(source, destination)
        db_manifest[label] = {"exists": True, **backup}
        db_map[_norm(source)] = str(destination)
        _progress(
            f"sqlite_{label}",
            f"done seconds={backup.get('seconds')} snapshot_mb={round(int(backup.get('bytes') or 0)/(1024*1024),1)}",
        )

    alerts_snapshot = Path(db_map.get(_norm(config_module.ALERTS_DB), ""))
    if not alerts_snapshot.is_file():
        raise RuntimeError("No se pudo crear snapshot SQLite de ALERTS_DB.")

    file_map: dict[str, str] = {}
    journal_manifest: dict[str, dict] = {}
    journal_specs = {
        "eod_options": Path(config_module.OUTPUT_DIR) / "alpha_pnl_attribution" / "eod_selection_journal.jsonl",
        "swing_v2_prospective": Path(config_module.OUTPUT_DIR) / "swing_v2" / "swing_v2_prospective_journal.jsonl",
        "economic_benchmark_v1": Path(config_module.RUNTIME_ROOT) / "research_benchmarks" / "economic_benchmark_v1.json",
        "economic_feature_observation_benchmark_v1": Path(config_module.RUNTIME_ROOT) / "research_benchmarks" / "economic_feature_observation_benchmark_v1.json",
    }
    for label, source in journal_specs.items():
        destination = journals_dir / source.name
        journal_manifest[label] = _copy_journal(source, destination)
        # Always freeze the path mapping, even when the journal does not exist
        # at T0. Otherwise production can create the live journal mid-replay and
        # research_input_path() would suddenly resolve to that mutable live file,
        # making the supposedly frozen attribution basis drift internally.
        file_map[_norm(source)] = str(destination)

    _progress("archives", "capturing immutable archive links")
    layer_manifest, layer_map = _snapshot_layer_archives(
        alerts_snapshot,
        Path(config_module.LAYER_ARCHIVE_DIR),
        layer_dir,
    )
    file_map.update(layer_map)

    data_manifest, data_map = _snapshot_data_archives(
        alerts_snapshot,
        Path(config_module.DATA_ARCHIVE_DIR),
        data_archive_dir,
    )
    file_map.update(data_map)
    _progress(
        "archives",
        f"done layer_archives={len(layer_manifest)} data_archives={len(data_manifest)}",
    )

    snapshot = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "db_map": db_map,
        "file_map": file_map,
        "db_manifest": db_manifest,
        "journal_manifest": journal_manifest,
        "layer_archive_dir": str(layer_dir),
        "data_archive_dir": str(data_archive_dir),
        "layer_archives": layer_manifest,
        "data_archives": data_manifest,
    }
    (root / "manifest.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return snapshot


def compact_snapshot_summary(snapshot: dict) -> dict:
    db_manifest = snapshot.get("db_manifest") or {}
    journal_manifest = snapshot.get("journal_manifest") or {}
    return {
        "schema_version": snapshot.get("schema_version"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "created_at_utc": snapshot.get("created_at_utc"),
        "sqlite_backups": {
            key: {
                "exists": bool((value or {}).get("exists")),
                "bytes": int((value or {}).get("bytes") or 0),
                "seconds": (value or {}).get("seconds"),
            }
            for key, value in db_manifest.items()
        },
        "journals": {
            key: {
                "exists": bool((value or {}).get("exists")),
                "bytes": int((value or {}).get("bytes") or 0),
            }
            for key, value in journal_manifest.items()
        },
        "layer_archives": len(snapshot.get("layer_archives") or []),
        "data_archives": len(snapshot.get("data_archives") or []),
        "storage_policy": "ALERTS_COMPACT_T0 + DATA_SQLITE_BACKUP + IMMUTABLE_ARCHIVE_HARDLINK + JOURNAL_COPY",
        "production_paused": False,
    }


def cleanup_research_snapshot(snapshot: dict) -> None:
    root = Path(str(snapshot.get("root") or ""))
    if root and root.exists():
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def frozen_research_snapshot(config_module, progress_callback: Callable | None = None) -> Iterator[dict]:
    global _ACTIVE
    if _ACTIVE is not None:
        raise RuntimeError("Ya hay un research snapshot activo en este proceso.")

    snapshot = create_research_snapshot(config_module, progress_callback=progress_callback)
    old_dirs = {
        "DATA_ARCHIVE_DIR": Path(config_module.DATA_ARCHIVE_DIR),
        "DATA_INTRADAY_ARCHIVE_DIR": Path(config_module.DATA_INTRADAY_ARCHIVE_DIR),
        "LAYER_ARCHIVE_DIR": Path(config_module.LAYER_ARCHIVE_DIR),
    }
    previous = _ACTIVE
    _ACTIVE = snapshot
    try:
        # Archive readers enumerate these directories dynamically. Point them to
        # the immutable set captured at T0. SQLite and journal readers use the
        # maps above so runtime-state writes and output artifacts stay LIVE.
        config_module.DATA_ARCHIVE_DIR = Path(snapshot["data_archive_dir"])
        config_module.DATA_INTRADAY_ARCHIVE_DIR = Path(snapshot["data_archive_dir"]) / "intraday"
        config_module.LAYER_ARCHIVE_DIR = Path(snapshot["layer_archive_dir"])
        yield snapshot
    finally:
        config_module.DATA_ARCHIVE_DIR = old_dirs["DATA_ARCHIVE_DIR"]
        config_module.DATA_INTRADAY_ARCHIVE_DIR = old_dirs["DATA_INTRADAY_ARCHIVE_DIR"]
        config_module.LAYER_ARCHIVE_DIR = old_dirs["LAYER_ARCHIVE_DIR"]
        _ACTIVE = previous
        cleanup_research_snapshot(snapshot)
