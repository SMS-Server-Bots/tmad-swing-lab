from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from datetime import date, timedelta
from zipfile import ZIP_DEFLATED, ZipFile

import config
from db_core import connect
from time_utils import now_madrid



def _archive_connect(path: Path) -> sqlite3.Connection:
    """Open the portable archive database without WAL sidecar files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.row_factory = sqlite3.Row
    return conn


def profile_zip_path(trade_date: str) -> Path:
    return config.GEX_PROFILE_ARCHIVE_DIR / trade_date[:7] / f"gex_profiles_{trade_date}.zip"


def _table_exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _ensure_event_table(src, dst) -> None:
    schema = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='gex_profile_events'").fetchone()
    if not schema or not schema[0]:
        raise RuntimeError("gex_profile_events schema missing")
    if not _table_exists(dst, "gex_profile_events"):
        dst.execute(str(schema[0]).replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
        dst.execute("CREATE INDEX IF NOT EXISTS idx_profile_archive_ticker_time ON gex_profile_events(ticker,captured_at_madrid)")


def _copy_events(trade_date: str, archive_db: Path) -> tuple[list[int], int]:
    src = connect(config.GEX_DB)
    dst = _archive_connect(archive_db)
    try:
        _ensure_event_table(src, dst)
        rows = src.execute(
            "SELECT * FROM gex_profile_events WHERE snapshot_date=? ORDER BY captured_at_madrid,id",
            (trade_date,),
        ).fetchall()
        if not rows:
            return [], 0
        cols = list(rows[0].keys())
        sql = f"INSERT OR REPLACE INTO gex_profile_events ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
        dst.executemany(sql, [[row[col] for col in cols] for row in rows])
        dst.commit()
        # DELETE journal mode guarantees the ZIP contains a self-contained DB.
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return [int(row["id"]) for row in rows], len(rows)
    finally:
        src.close()
        dst.close()


def _archive_counts(archive_db: Path, trade_date: str) -> dict:
    conn = _archive_connect(archive_db)
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS events,COUNT(DISTINCT ticker) AS tickers,
                      SUM(CASE WHEN event_kind='baseline' THEN 1 ELSE 0 END) AS baselines,
                      SUM(CASE WHEN event_kind='delta' THEN 1 ELSE 0 END) AS deltas,
                      SUM(CASE WHEN material_change=1 THEN 1 ELSE 0 END) AS material
               FROM gex_profile_events WHERE snapshot_date=?""",
            (trade_date,),
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}
    finally:
        conn.close()


def _verify_zip(zip_path: Path, trade_date: str, minimum_events: int) -> dict:
    """Verify container integrity and query the archived SQLite before live purge."""
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        raise RuntimeError("GEX profile ZIP missing or empty")
    db_name = f"gex_profiles_{trade_date}.sqlite"
    with ZipFile(zip_path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"Corrupt GEX profile ZIP member: {bad}")
        names = set(archive.namelist())
        if db_name not in names or "manifest.json" not in names:
            raise RuntimeError("GEX profile ZIP incomplete")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        manifest_events = int(manifest.get("counts", {}).get("events") or 0)
        if manifest_events < int(minimum_events):
            raise RuntimeError("GEX profile ZIP manifest count mismatch")
        with tempfile.TemporaryDirectory(prefix="tmad_gex_verify_") as temp_dir:
            extracted = Path(temp_dir) / db_name
            extracted.write_bytes(archive.read(db_name))
            conn = sqlite3.connect(f"file:{extracted.as_posix()}?mode=ro", uri=True)
            try:
                actual_events = int(conn.execute(
                    "SELECT COUNT(*) FROM gex_profile_events WHERE snapshot_date=?",
                    (trade_date,),
                ).fetchone()[0])
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                conn.close()
        if integrity.lower() != "ok":
            raise RuntimeError(f"Archived GEX SQLite integrity check failed: {integrity}")
        if actual_events != manifest_events or actual_events < int(minimum_events):
            raise RuntimeError(
                f"GEX profile ZIP SQLite count mismatch: actual={actual_events}, manifest={manifest_events}"
            )
        manifest["verified_sqlite_events"] = actual_events
        return manifest


def _purge_event_ids(event_ids: list[int]) -> int:
    if not event_ids:
        return 0
    conn = connect(config.GEX_DB)
    try:
        ph = ",".join(["?"] * len(event_ids))
        cur = conn.execute(f"DELETE FROM gex_profile_events WHERE id IN ({ph})", event_ids)
        deleted = max(0, int(cur.rowcount or 0))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted
    finally:
        conn.close()


def purge_old_gex_profile_archives() -> int:
    years = int(config.GEX_PROFILE_ARCHIVE_YEARS_TO_KEEP)
    # v6 default: zero means unlimited. Storage policy may change later without
    # throwing away the learning memory collected today.
    if years <= 0:
        return 0
    cutoff = now_madrid().date() - timedelta(days=366 * years)
    deleted = 0
    for path in config.GEX_PROFILE_ARCHIVE_DIR.rglob("gex_profiles_????-??-??.zip"):
        try:
            trade_date = date.fromisoformat(path.stem.replace("gex_profiles_", ""))
            if trade_date < cutoff:
                path.unlink(missing_ok=True)
                deleted += 1
        except Exception:
            continue
    for folder in sorted(config.GEX_PROFILE_ARCHIVE_DIR.glob("????-??")):
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except Exception:
            pass
    return deleted


def archive_gex_profile_date(trade_date: str, delete_after: bool | None = None) -> dict:
    """Merge one market day's baseline+deltas into an atomic, verified ZIP."""
    if not config.GEX_PROFILE_ARCHIVE_ENABLED:
        return {"status": "disabled", "trade_date": trade_date}
    delete_after = config.GEX_PROFILE_DELETE_AFTER_ZIP if delete_after is None else bool(delete_after)
    zip_path = profile_zip_path(trade_date)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = config.GEX_PROFILE_ARCHIVE_DIR / "_work" / trade_date
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    archive_db = work_dir / f"gex_profiles_{trade_date}.sqlite"
    previous_revision = 0
    if zip_path.exists():
        try:
            with ZipFile(zip_path) as existing:
                existing.extractall(work_dir)
            manifest_path = work_dir / "manifest.json"
            if manifest_path.exists():
                previous_revision = int(json.loads(manifest_path.read_text(encoding="utf-8")).get("archive_revision") or 0)
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            archive_db = work_dir / f"gex_profiles_{trade_date}.sqlite"
            previous_revision = 0

    event_ids, copied = _copy_events(trade_date, archive_db)
    if copied == 0 and not zip_path.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
        return {"status": "nothing_to_archive", "trade_date": trade_date, "zip": str(zip_path)}

    counts = _archive_counts(archive_db, trade_date)
    manifest = {
        "version": config.BOT_VERSION,
        "trade_date": trade_date,
        "created_at_madrid": now_madrid().isoformat(),
        "archive_revision": previous_revision + 1,
        "counts": counts,
        "format": "SQLite baseline+delta chain",
        "reconstruction": "Apply each ticker baseline, then later delta events ordered by captured_at_madrid/id.",
        "compression": {"event_payload": "zlib", "container": "zip-deflate"},
    }
    (work_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp = zip_path.with_suffix(".zip.tmp")
    tmp.unlink(missing_ok=True)
    with ZipFile(tmp, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(archive_db, archive_db.name)
        archive.write(work_dir / "manifest.json", "manifest.json")
    tmp.replace(zip_path)
    verified = _verify_zip(zip_path, trade_date, counts["events"])
    deleted = _purge_event_ids(event_ids) if delete_after else 0
    old_archives_deleted = purge_old_gex_profile_archives()
    shutil.rmtree(work_dir, ignore_errors=True)
    return {
        "status": "archived",
        "trade_date": trade_date,
        "zip": str(zip_path),
        "copied_events": copied,
        "deleted_live_events": deleted,
        "counts": verified.get("counts", {}),
        "verified_sqlite_events": verified.get("verified_sqlite_events"),
        "archive_revision": verified.get("archive_revision"),
        "old_archives_deleted": old_archives_deleted,
    }


def archive_pending_gex_profiles(before_date: str) -> list[dict]:
    if not config.GEX_PROFILE_ARCHIVE_ENABLED:
        return []
    conn = connect(config.GEX_DB)
    try:
        rows = conn.execute(
            "SELECT DISTINCT snapshot_date FROM gex_profile_events WHERE snapshot_date < ? ORDER BY snapshot_date",
            (before_date,),
        ).fetchall()
    finally:
        conn.close()
    return [archive_gex_profile_date(str(row[0])) for row in rows]
