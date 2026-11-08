from __future__ import annotations

"""Verified ZIP archive for the complete v6 learning memory.

Each market date is stored as a compact SQLite database plus manifest inside an
atomic ZIP. Live rows are purged only after integrity/count verification and only
when every outcome for that date is complete. ZIP retention is unlimited by
default; space policy can be changed later without reducing captured features.
"""

import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import config
from layer_history import ensure_layer_history_schema
from time_utils import now_madrid, ts_utc_str

TABLES = (
    "layer_signal_history", "layer_signal_outcomes", "layer_feature_history",
    "layer_feature_outcomes", "market_regime_history",
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _schema_sql(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return str(row[0]) if row and row[0] else None


def _copy_rows(src: sqlite3.Connection, dst: sqlite3.Connection, table: str, rows: list[sqlite3.Row]) -> int:
    if not rows:
        return 0
    if not _table_exists(dst, table):
        sql = _schema_sql(src, table)
        if not sql:
            return 0
        dst.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
    src_cols = [str(row[1]) for row in src.execute(f"PRAGMA table_info({table})").fetchall()]
    dst_cols = {str(row[1]) for row in dst.execute(f"PRAGMA table_info({table})").fetchall()}
    for row in src.execute(f"PRAGMA table_info({table})").fetchall():
        name, typ = str(row[1]), str(row[2] or "TEXT")
        if name not in dst_cols:
            dst.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    cols = [c for c in src_cols if c in rows[0].keys()]
    placeholders = ",".join("?" for _ in cols)
    dst.executemany(
        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        [[row[c] for c in cols] for row in rows],
    )
    return len(rows)


def _rows_for_date(conn: sqlite3.Connection, table: str, trade_date: str) -> list[sqlite3.Row]:
    if table == "layer_signal_history":
        return conn.execute("SELECT * FROM layer_signal_history WHERE snapshot_date=?", (trade_date,)).fetchall()
    if table == "layer_feature_history":
        return conn.execute("SELECT * FROM layer_feature_history WHERE snapshot_date=?", (trade_date,)).fetchall()
    if table == "market_regime_history":
        return conn.execute("SELECT * FROM market_regime_history WHERE snapshot_date=?", (trade_date,)).fetchall()
    if table == "layer_signal_outcomes":
        return conn.execute("SELECT * FROM layer_signal_outcomes WHERE signal_date=?", (trade_date,)).fetchall()
    if table == "layer_feature_outcomes":
        return conn.execute("SELECT * FROM layer_feature_outcomes WHERE signal_date=?", (trade_date,)).fetchall()
    return []


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_zip_path(trade_date: str) -> Path:
    return config.LAYER_ARCHIVE_DIR / trade_date[:7] / f"layers_{trade_date}.zip"


def _outcome_watermark(conn: sqlite3.Connection, trade_date: str) -> str:
    """Content fingerprint for outcome progress on one signal date.

    Timestamps are deliberately insufficient: several daemon transitions can
    occur in the same second. Hashing the actual rows guarantees that partial →
    complete, new sessions, target hits and timing changes each create exactly
    one archive revision.
    """
    payload: dict[str, list[dict]] = {}
    for table in ("layer_signal_outcomes", "layer_feature_outcomes"):
        if not _table_exists(conn, table):
            payload[table] = []
            continue
        rows = conn.execute(f"SELECT * FROM {table} WHERE signal_date=? ORDER BY id", (trade_date,)).fetchall()
        payload[table] = [dict(row) for row in rows]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _date_completeness(conn: sqlite3.Connection, trade_date: str) -> dict:
    signals = int(conn.execute("SELECT COUNT(*) FROM layer_signal_history WHERE snapshot_date=?", (trade_date,)).fetchone()[0])
    features = int(conn.execute("SELECT COUNT(*) FROM layer_feature_history WHERE snapshot_date=?", (trade_date,)).fetchone()[0])
    signal_pending = int(conn.execute("SELECT COUNT(*) FROM layer_signal_pending WHERE signal_date=?", (trade_date,)).fetchone()[0])
    feature_pending = int(conn.execute("SELECT COUNT(*) FROM layer_feature_pending WHERE signal_date=?", (trade_date,)).fetchone()[0])
    signal_outcomes = int(conn.execute("SELECT COUNT(*) FROM layer_signal_outcomes WHERE signal_date=? AND complete=1", (trade_date,)).fetchone()[0])
    feature_outcomes = int(conn.execute("SELECT COUNT(*) FROM layer_feature_outcomes WHERE signal_date=? AND complete=1", (trade_date,)).fetchone()[0])
    return {
        "signals_live": signals, "features_live": features,
        "signal_pending": signal_pending, "feature_pending": feature_pending,
        "signal_outcomes_complete": signal_outcomes, "feature_outcomes_complete": feature_outcomes,
        "complete": (signal_pending == 0 or signal_outcomes >= signal_pending) and (feature_pending == 0 or feature_outcomes >= feature_pending),
    }


def archive_layer_date(trade_date: str, *, purge: bool | None = None) -> dict:
    ensure_layer_history_schema()
    purge = bool(config.LAYER_ARCHIVE_DELETE_AFTER_VERIFY if purge is None else purge)
    zip_path = archive_zip_path(trade_date); zip_path.parent.mkdir(parents=True, exist_ok=True)
    src = _connect(config.ALERTS_DB)
    completeness = _date_completeness(src, trade_date)
    outcome_watermark = _outcome_watermark(src, trade_date)
    has_live_or_pending = sum(int(completeness.get(key) or 0) for key in (
        "features_live", "signals_live", "feature_pending", "signal_pending",
        "feature_outcomes_complete", "signal_outcomes_complete",
    )) > 0
    if not has_live_or_pending and not zip_path.exists():
        src.close(); return {"status": "nothing_to_archive", "trade_date": trade_date, "zip": str(zip_path)}

    work_root = config.LAYER_ARCHIVE_DIR / "_work" / trade_date
    shutil.rmtree(work_root, ignore_errors=True); work_root.mkdir(parents=True, exist_ok=True)
    archive_db = work_root / f"layers_{trade_date}.sqlite"
    previous_manifest: dict = {}
    if zip_path.exists():
        try:
            with ZipFile(zip_path) as zf:
                zf.extractall(work_root)
            manifest_path = work_root / "manifest.json"
            if manifest_path.exists():
                previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            shutil.rmtree(work_root, ignore_errors=True); work_root.mkdir(parents=True, exist_ok=True)
            archive_db = work_root / f"layers_{trade_date}.sqlite"; previous_manifest = {}

    live_source_rows = int(completeness.get("signals_live") or 0) + int(completeness.get("features_live") or 0)
    regime_row = int(src.execute("SELECT COUNT(*) FROM market_regime_history WHERE snapshot_date=?", (trade_date,)).fetchone()[0])
    previous_watermark = str(previous_manifest.get("outcome_watermark") or "")
    if previous_manifest and live_source_rows == 0 and regime_row == 0 and outcome_watermark == previous_watermark:
        # The session is already secured and no future session has changed an
        # outcome. Avoid rewriting the same ZIP/revision on every daemon poll.
        revision = int(previous_manifest.get("archive_revision") or 0)
        src.close(); shutil.rmtree(work_root, ignore_errors=True)
        return {
            "status": "up_to_date", "trade_date": trade_date, "zip": str(zip_path),
            "archive_revision": revision, "complete": bool(completeness.get("complete")),
        }

    dst = _connect(archive_db)
    counts: dict[str, int] = {}
    try:
        for table in TABLES:
            counts[table] = _copy_rows(src, dst, table, _rows_for_date(src, table, trade_date))
        dst.commit()
        integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"archive sqlite integrity={integrity}")
        final_counts = {table: int(dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if _table_exists(dst, table) else 0 for table in TABLES}
    finally:
        dst.close()
    manifest = {
        "version": config.BOT_VERSION,
        "trade_date": trade_date,
        "created_at_utc": ts_utc_str(),
        "archive_revision": int(previous_manifest.get("archive_revision") or 0) + 1,
        "purpose": "Point-in-time CORE/graph/GEX/timing/fusion memory for future calibration.",
        "source_counts": completeness,
        "copied_now": counts,
        "final_counts": final_counts,
        "sqlite_integrity": "ok",
        "outcome_watermark": outcome_watermark,
        "retention_policy": {"zip_years": int(config.LAYER_ARCHIVE_KEEP_YEARS), "zero_means_unlimited": True},
    }
    (work_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp = zip_path.with_suffix(".zip.tmp"); tmp.unlink(missing_ok=True)
    with ZipFile(tmp, "w", compression=ZIP_DEFLATED, compresslevel=int(config.LAYER_ARCHIVE_COMPRESSION_LEVEL)) as zf:
        for file in sorted(work_root.rglob("*")):
            if file.is_file(): zf.write(file, file.relative_to(work_root))
    # Verify the exact temporary artifact before replacing the canonical ZIP.
    with ZipFile(tmp) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"corrupt ZIP member: {bad}")
        extracted = Path(tempfile.mkdtemp(prefix="tmad-layer-verify-"))
        try:
            zf.extractall(extracted)
            db_file = next(extracted.glob("*.sqlite"), None)
            if not db_file:
                raise RuntimeError("ZIP missing SQLite")
            check = _connect(db_file)
            try:
                if str(check.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                    raise RuntimeError("ZIP SQLite integrity failed")
                verified_counts = {table: int(check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if _table_exists(check, table) else 0 for table in TABLES}
                if verified_counts != final_counts:
                    raise RuntimeError(f"ZIP row counts mismatch: expected={final_counts} found={verified_counts}")
            finally:
                check.close()
        finally:
            shutil.rmtree(extracted, ignore_errors=True)
    tmp.replace(zip_path); digest = _sha256(zip_path)

    purged = False
    source_purged = False
    outcomes_purged = False
    if purge:
        # Full payloads are now safe in the verified ZIP. Keep only compact pending
        # queues/outcomes until the five-session labels have matured.
        src.execute("DELETE FROM layer_signal_history WHERE snapshot_date=?", (trade_date,))
        src.execute("DELETE FROM layer_feature_history WHERE snapshot_date=?", (trade_date,))
        src.execute("DELETE FROM market_regime_history WHERE snapshot_date=?", (trade_date,))
        source_purged = True
        if completeness["complete"]:
            src.execute("DELETE FROM layer_signal_outcomes WHERE signal_date=?", (trade_date,))
            src.execute("DELETE FROM layer_feature_outcomes WHERE signal_date=?", (trade_date,))
            src.execute("DELETE FROM layer_signal_pending WHERE signal_date=?", (trade_date,))
            src.execute("DELETE FROM layer_feature_pending WHERE signal_date=?", (trade_date,))
            outcomes_purged = True
        purged = source_purged
    src.execute(
        """INSERT INTO layer_archive_catalog(trade_date,zip_path,archive_revision,signal_rows,signal_outcome_rows,
               feature_rows,feature_outcome_rows,regime_rows,sha256,verified,archived_at_utc)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(trade_date) DO UPDATE SET zip_path=excluded.zip_path,archive_revision=excluded.archive_revision,
               signal_rows=excluded.signal_rows,signal_outcome_rows=excluded.signal_outcome_rows,
               feature_rows=excluded.feature_rows,feature_outcome_rows=excluded.feature_outcome_rows,
               regime_rows=excluded.regime_rows,sha256=excluded.sha256,verified=excluded.verified,
               archived_at_utc=excluded.archived_at_utc""",
        (trade_date,str(zip_path),manifest["archive_revision"],final_counts["layer_signal_history"],
         final_counts["layer_signal_outcomes"],final_counts["layer_feature_history"],
         final_counts["layer_feature_outcomes"],final_counts["market_regime_history"],digest,1,ts_utc_str()),
    )
    src.commit(); src.execute("PRAGMA wal_checkpoint(TRUNCATE)"); src.close()
    shutil.rmtree(work_root, ignore_errors=True)
    return {"status": "archived", "trade_date": trade_date, "zip": str(zip_path), "sha256": digest,
            "purged": purged, "source_purged": source_purged, "outcomes_purged": outcomes_purged,
            "complete": bool(completeness["complete"]), "manifest": manifest}


def archive_completed_layer_dates() -> list[dict]:
    """Archive every closed market date; later merge outcomes into the same ZIP.

    The current Madrid date remains live for operational reports. Previous dates
    move to ZIP immediately, independent of whether their five-session labels have
    matured.
    """
    if not getattr(config, "LAYER_ARCHIVE_ENABLED", True):
        return []
    ensure_layer_history_schema(); conn = _connect(config.ALERTS_DB)
    cutoff = now_madrid().date().isoformat()
    rows = conn.execute(
        """SELECT snapshot_date AS d FROM layer_feature_history WHERE snapshot_date<?
           UNION SELECT snapshot_date FROM layer_signal_history WHERE snapshot_date<?
           UNION SELECT signal_date FROM layer_feature_pending WHERE signal_date<?
           UNION SELECT signal_date FROM layer_signal_pending WHERE signal_date<?
           ORDER BY d""", (cutoff, cutoff, cutoff, cutoff),
    ).fetchall()
    dates = [str(row[0]) for row in rows if row[0]]
    conn.close()
    return [archive_layer_date(day) for day in dates]


def _archive_files() -> list[Path]:
    """Return newest layer archives across warm local storage + cold runtime cache.

    GitHub access stays in the control plane. Historical readers only consume
    already-verified ZIPs materialized under 04_RUNTIME/history_cache.
    """
    limit = int(config.LAYER_ARCHIVE_IMPORT_MAX_FILES)
    local = sorted(config.LAYER_ARCHIVE_DIR.glob("????-??/layers_????-??-??.zip"))
    runtime_root = getattr(config, "RUNTIME_ROOT", config.LAYER_ARCHIVE_DIR.parent.parent.parent.parent / "04_RUNTIME")
    cold_root = Path(runtime_root) / "history_cache" / "alerts_layers"
    cold = sorted(cold_root.glob("????-??/layers_????-??-??.zip")) if cold_root.exists() else []

    by_name: dict[str, Path] = {}
    for path in cold:
        by_name[path.name] = path
    for path in local:
        by_name[path.name] = path

    files = sorted(by_name.values(), key=lambda p: p.name)
    return files[-limit:] if limit > 0 else files


def archived_joined_signal_outcomes() -> list[dict]:
    rows: list[dict] = []
    for zip_path in _archive_files():
        try:
            with ZipFile(zip_path) as zf:
                db_name = next((n for n in zf.namelist() if n.endswith(".sqlite")), None)
                if not db_name: continue
                with tempfile.TemporaryDirectory(prefix="tmad-layer-read-") as temp:
                    zf.extract(db_name, temp); conn = _connect(Path(temp) / db_name)
                    try:
                        if _table_exists(conn,"layer_signal_history") and _table_exists(conn,"layer_signal_outcomes"):
                            history_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(layer_signal_history)").fetchall()}
                            def _sig_col(name: str, alias: str | None = None) -> str:
                                target = alias or name
                                return f"s.{name} AS {target}" if name in history_cols else f"NULL AS {target}"
                            projection = ",".join([
                                _sig_col("layer"),
                                _sig_col("snapshot_date"),
                                _sig_col("source_scanner_run_id"),
                                _sig_col("source_gex_run_id"),
                                _sig_col("score"),
                                _sig_col("score", "layer_score"),
                                _sig_col("signal_origin"),
                                _sig_col("state"),
                                _sig_col("actionable"),
                                _sig_col("opportunity_state"),
                                _sig_col("payload_json"),
                                _sig_col("target_pct"),
                            ])
                            rows.extend(dict(r) for r in conn.execute(
                                f"""SELECT {projection},o.* FROM layer_signal_history s
                                    JOIN layer_signal_outcomes o ON o.layer_signal_id=s.id
                                    WHERE o.complete=1"""
                            ).fetchall())
                    finally: conn.close()
        except Exception:
            continue
    return rows


def archived_feature_rows_with_outcomes() -> list[dict]:
    rows: list[dict] = []
    for zip_path in _archive_files():
        try:
            with ZipFile(zip_path) as zf:
                db_name = next((n for n in zf.namelist() if n.endswith(".sqlite")), None)
                if not db_name: continue
                with tempfile.TemporaryDirectory(prefix="tmad-feature-read-") as temp:
                    zf.extract(db_name,temp); conn=_connect(Path(temp)/db_name)
                    try:
                        if _table_exists(conn,"layer_feature_history") and _table_exists(conn,"layer_feature_outcomes"):
                            rows.extend(dict(r) for r in conn.execute(
                                """SELECT f.*,o.entry_price AS outcome_entry_price,o.horizon_sessions,
                                          o.long_5pct_hit,o.short_5pct_hit,o.first_long_5pct_session,
                                          o.first_short_5pct_session,o.max_up_pct,o.max_down_pct,o.close_return_pct,
                                          o.sessions_json,o.complete,o.labeled_at_utc,o.signal_date
                                   FROM layer_feature_history f JOIN layer_feature_outcomes o ON o.layer_feature_id=f.id
                                   WHERE o.complete=1"""
                            ).fetchall())
                    finally: conn.close()
        except Exception:
            continue
    return rows
