from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import config
from db_core import connect
from time_utils import market_date_from_madrid_iso, now_madrid, ts_madrid_str


def _sqlite_inventory(root: Path) -> dict[str, dict]:
    inventory: dict[str, dict] = {}
    for db_path in sorted(root.rglob("*.sqlite")):
        conn = sqlite3.connect(db_path)
        try:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise RuntimeError(f"SQLite integrity failed for {db_path.name}: {integrity}")
            tables = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()]
            counts = {table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in tables}
        finally:
            conn.close()
        inventory[db_path.name] = {"integrity": "ok", "tables": counts}
    return inventory


def _verify_intraday_zip(zip_path: Path, expected_inventory: dict[str, dict]) -> None:
    with ZipFile(zip_path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP corrupt member: {bad}")
        with tempfile.TemporaryDirectory(prefix="tmad-intraday-verify-") as temp:
            archive.extractall(temp)
            root = Path(temp)
            found = _sqlite_inventory(root)
            if found != expected_inventory:
                raise RuntimeError(f"Intraday ZIP SQLite inventory mismatch: expected={expected_inventory} found={found}")
            manifest_file = root / "manifest.json"
            if not manifest_file.exists():
                candidates = list(root.rglob("manifest.json"))
                manifest_file = candidates[0] if candidates else manifest_file
            if not manifest_file.exists():
                raise RuntimeError("Intraday ZIP missing manifest.json")
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            if manifest.get("sqlite_inventory") != expected_inventory:
                raise RuntimeError("Intraday ZIP manifest inventory mismatch")


def is_canonical_daily_session(session_type: str | None) -> bool:
    value = str(session_type or "").strip().lower()
    return "deep" in value or value in {"postmarket", "daily_close", "canonical_close"}


def current_market_date() -> str:
    return market_date_from_madrid_iso(ts_madrid_str())


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _table_columns(conn, table: str) -> dict[str, str]:
    return {str(row[1]): (str(row[2]) or "TEXT") for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_table(src, dst, table: str) -> bool:
    schema = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not schema or not schema[0]:
        return False
    if not _table_exists(dst, table):
        sql = re.sub(r"^CREATE TABLE\s+", "CREATE TABLE IF NOT EXISTS ", str(schema[0]), flags=re.I)
        dst.execute(sql)
    else:
        src_cols = _table_columns(src, table)
        dst_cols = _table_columns(dst, table)
        for name, ddl in src_cols.items():
            if name not in dst_cols:
                dst.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    return True


def _copy_where(src_db: Path, dst_db: Path, table: str, where: str, params: tuple) -> int:
    src = connect(src_db)
    dst = connect(dst_db)
    try:
        if not _ensure_table(src, dst, table):
            return 0
        rows = src.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
        if not rows:
            return 0
        cols = list(rows[0].keys())
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
        dst.executemany(sql, [[row[col] for col in cols] for row in rows])
        dst.commit()
        return len(rows)
    finally:
        src.close()
        dst.close()


def _run_ids(db_path: Path, table: str, trade_date: str, daily: bool) -> list[int]:
    conn = connect(db_path)
    try:
        op = "LIKE" if daily else "NOT LIKE"
        rows = conn.execute(
            f"SELECT id FROM {table} WHERE snapshot_date=? AND status='success' AND LOWER(COALESCE(session_type,'')) {op} '%deep%' ORDER BY id",
            (trade_date,),
        ).fetchall()
        return [int(row[0]) for row in rows]
    finally:
        conn.close()


def _ids_where(column: str, ids: list[int]) -> tuple[str, tuple]:
    if not ids:
        return "1=0", ()
    placeholders = ",".join(["?"] * len(ids))
    return f"{column} IN ({placeholders})", tuple(ids)


def _copy_intraday_databases(trade_date: str, work_dir: Path) -> dict:
    scanner_ids = _run_ids(config.SCANNER_DB, "scanner_runs", trade_date, daily=False)
    gex_ids = _run_ids(config.GEX_DB, "gex_runs", trade_date, daily=False)
    counts: dict[str, int] = {}

    scanner_dst = work_dir / f"scanner_intraday_{trade_date}.sqlite"
    gex_dst = work_dir / f"gex_intraday_{trade_date}.sqlite"
    raw_dst = work_dir / f"raw_intraday_{trade_date}.sqlite"
    meta_dst = work_dir / f"meta_intraday_{trade_date}.sqlite"

    scanner_where, scanner_params = _ids_where("id", scanner_ids)
    counts["scanner_runs"] = _copy_where(config.SCANNER_DB, scanner_dst, "scanner_runs", scanner_where, scanner_params)
    child_where, child_params = _ids_where("scanner_run_id", scanner_ids)
    for table in [
        "scanner_snapshots",
        "scanner_snapshot_deltas",
        "ticker_detail_zones",
        "ticker_detail_history",
        "ticker_price_history",
        "data_quality_events",
    ]:
        counts[table] = _copy_where(config.SCANNER_DB, scanner_dst, table, child_where, child_params)

    gex_where, gex_params = _ids_where("id", gex_ids)
    counts["gex_runs"] = _copy_where(config.GEX_DB, gex_dst, "gex_runs", gex_where, gex_params)
    gex_child_where, gex_child_params = _ids_where("gex_run_id", gex_ids)
    for table in ["gex_summary", "gex_levels", "gex_expirations", "gex_expiry_buckets", "gex_failures"]:
        counts[table] = _copy_where(config.GEX_DB, gex_dst, table, gex_child_where, gex_child_params)

    # Paired GEX endpoint payloads are recorded under the scanner run id. Separate
    # GEX jobs, when enabled, use the GEX run id. Preserve both families.
    raw_clauses: list[str] = []
    raw_params: list[int] = []
    if scanner_ids:
        ph = ",".join(["?"] * len(scanner_ids))
        raw_clauses.append(f"(run_type='scanner' AND run_id IN ({ph}))")
        raw_params.extend(scanner_ids)
    if gex_ids:
        ph = ",".join(["?"] * len(gex_ids))
        raw_clauses.append(f"(run_type='gex' AND run_id IN ({ph}))")
        raw_params.extend(gex_ids)
    raw_where = " OR ".join(raw_clauses) if raw_clauses else "1=0"
    counts["raw_api_payloads"] = _copy_where(
        config.RAW_DB, raw_dst, "raw_api_payloads", raw_where, tuple(raw_params)
    )
    counts["extra_fields"] = _copy_where(
        config.META_DB, meta_dst, "extra_fields", raw_where, tuple(raw_params)
    )

    return {
        "trade_date": trade_date,
        "scanner_run_ids": scanner_ids,
        "gex_run_ids": gex_ids,
        "counts": counts,
        "created_at_madrid": now_madrid().isoformat(),
        "purpose": "Ephemeral hourly layer archived for future intraday entry/exit calibration.",
        "calibration_policy": {
            "daily_primary_window": 30,
            "intraday_used_live": True,
            "intraday_future_training": True,
        },
    }


def _delete_ids(conn, table: str, column: str, ids: list[int]) -> int:
    if not ids or not _table_exists(conn, table):
        return 0
    ph = ",".join(["?"] * len(ids))
    cur = conn.execute(f"DELETE FROM {table} WHERE {column} IN ({ph})", ids)
    return max(0, int(cur.rowcount or 0))


def purge_intraday_from_live(scanner_ids: list[int], gex_ids: list[int]) -> dict[str, int]:
    deleted: dict[str, int] = {}
    conn = connect(config.SCANNER_DB)
    try:
        for table in [
            "scanner_snapshots",
            "scanner_snapshot_deltas",
            "ticker_detail_zones",
            "ticker_detail_history",
            "ticker_price_history",
            "data_quality_events",
        ]:
            deleted[table] = _delete_ids(conn, table, "scanner_run_id", scanner_ids)
        deleted["scanner_runs"] = _delete_ids(conn, "scanner_runs", "id", scanner_ids)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        conn.close()

    conn = connect(config.GEX_DB)
    try:
        for table in ["gex_summary", "gex_levels", "gex_expirations", "gex_expiry_buckets", "gex_failures"]:
            deleted[table] = _delete_ids(conn, table, "gex_run_id", gex_ids)
        deleted["gex_runs"] = _delete_ids(conn, "gex_runs", "id", gex_ids)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        conn.close()

    for db_path, table in [(config.RAW_DB, "raw_api_payloads"), (config.META_DB, "extra_fields")]:
        conn = connect(db_path)
        try:
            total = 0
            if scanner_ids:
                ph = ",".join(["?"] * len(scanner_ids))
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE run_type='scanner' AND run_id IN ({ph})", scanner_ids
                )
                total += max(0, int(cur.rowcount or 0))
            if gex_ids:
                ph = ",".join(["?"] * len(gex_ids))
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE run_type='gex' AND run_id IN ({ph})", gex_ids
                )
                total += max(0, int(cur.rowcount or 0))
            deleted[table] = total
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()
    return deleted


def intraday_zip_path(trade_date: str) -> Path:
    month = trade_date[:7]
    return config.INTRADAY_ARCHIVE_DIR / month / f"intraday_{trade_date}.zip"


def archive_intraday_date(trade_date: str, delete_after: bool | None = None) -> dict:
    """Archive/merge every non-deep run for one market date, then purge live.

    The ZIP is mergeable: if a late/recovery hourly run appears after the first
    archive pass, it is added atomically instead of being discarded merely
    because the ZIP already exists.
    """
    delete_after = config.INTRADAY_DELETE_AFTER_ZIP if delete_after is None else bool(delete_after)
    zip_path = intraday_zip_path(trade_date)
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    scanner_ids = _run_ids(config.SCANNER_DB, "scanner_runs", trade_date, daily=False)
    gex_ids = _run_ids(config.GEX_DB, "gex_runs", trade_date, daily=False)
    if not scanner_ids and not gex_ids:
        return {"status": "nothing_to_archive", "trade_date": trade_date, "zip": str(zip_path)}

    work_dir = config.INTRADAY_ARCHIVE_DIR / "_work" / trade_date
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    previous_manifest: dict = {}
    if zip_path.exists():
        try:
            with ZipFile(zip_path) as existing:
                existing.extractall(work_dir)
            manifest_path = work_dir / "manifest.json"
            if manifest_path.exists():
                previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt/incomplete archive is never trusted. The current live
            # rows are rebuilt into a fresh atomic ZIP.
            shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            previous_manifest = {}

    manifest = _copy_intraday_databases(trade_date, work_dir)
    manifest["scanner_run_ids"] = sorted({
        *[int(x) for x in previous_manifest.get("scanner_run_ids", []) if str(x).isdigit()],
        *scanner_ids,
    })
    manifest["gex_run_ids"] = sorted({
        *[int(x) for x in previous_manifest.get("gex_run_ids", []) if str(x).isdigit()],
        *gex_ids,
    })
    manifest["archive_revision"] = int(previous_manifest.get("archive_revision") or 0) + 1
    manifest["merged_existing_zip"] = bool(previous_manifest)
    manifest["sqlite_inventory"] = _sqlite_inventory(work_dir)
    manifest["verification_policy"] = "Purge live rows only after ZIP + SQLite + row-count verification."
    (work_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp = zip_path.with_suffix(".zip.tmp")
    tmp.unlink(missing_ok=True)
    with ZipFile(tmp, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(work_dir.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(work_dir))
    _verify_intraday_zip(tmp, manifest["sqlite_inventory"])
    tmp.replace(zip_path)
    shutil.rmtree(work_dir, ignore_errors=True)

    deleted = purge_intraday_from_live(scanner_ids, gex_ids) if delete_after else {}
    return {
        "status": "archived",
        "trade_date": trade_date,
        "zip": str(zip_path),
        "scanner_runs": len(scanner_ids),
        "gex_runs": len(gex_ids),
        "deleted": deleted,
        "manifest": manifest,
    }


def archive_pending_intraday(before_date: str | None = None) -> list[dict]:
    before_date = before_date or current_market_date()
    conn = connect(config.SCANNER_DB)
    try:
        rows = conn.execute(
            """SELECT DISTINCT snapshot_date FROM scanner_runs
               WHERE status='success' AND snapshot_date < ?
                 AND LOWER(COALESCE(session_type,'')) NOT LIKE '%deep%'
               ORDER BY snapshot_date""",
            (before_date,),
        ).fetchall()
    finally:
        conn.close()
    return [archive_intraday_date(str(row[0])) for row in rows]
