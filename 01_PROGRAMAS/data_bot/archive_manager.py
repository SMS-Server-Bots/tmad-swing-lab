from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import config
from config import (
    ARCHIVE_DIR,
    SCANNER_DB,
    GEX_DB,
    RAW_DB,
    META_DB,
    ARCHIVE_ENABLED,
    EXPORT_MONTHLY_CSV,
    EXPORT_WEEKLY_CSV,
    EXPORT_WEEKLY_ZIP,
    ARCHIVE_PREVIOUS_WEEK,
    CLEAN_INVALID_ARCHIVE_ROWS,
    DELETE_FINALIZED_WEEK_FOLDER_AFTER_ZIP,
)
from db_core import connect, ensure_all_schemas
from time_utils import now_madrid


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


def _verify_period_zip(zip_path: Path, expected_inventory: dict[str, dict]) -> None:
    with ZipFile(zip_path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP corrupt member: {bad}")
        with tempfile.TemporaryDirectory(prefix="tmad-week-verify-") as temp:
            archive.extractall(temp)
            found = _sqlite_inventory(Path(temp))
            if found != expected_inventory:
                raise RuntimeError(f"Weekly ZIP SQLite inventory mismatch: expected={expected_inventory} found={found}")
            manifests = list(Path(temp).rglob("archive_manifest.json"))
            if not manifests:
                raise RuntimeError("Weekly ZIP missing archive_manifest.json")
            saved = json.loads(manifests[0].read_text(encoding="utf-8"))
            if saved.get("sqlite_inventory") != expected_inventory:
                raise RuntimeError("Weekly ZIP manifest inventory mismatch")


def month_bounds(month_key: str) -> tuple[str, str]:
    year, month = [int(x) for x in month_key.split("-")]
    if month == 12:
        return f"{year}-12-01", f"{year + 1}-01-01"
    return f"{year}-{month:02d}-01", f"{year}-{month + 1:02d}-01"


def iso_week_key(value: date | datetime | None = None) -> str:
    value = value or now_madrid()
    d = value.date() if isinstance(value, datetime) else value
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week_key: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", week_key)
    if not match:
        raise ValueError(f"Semana ISO inválida: {week_key}")
    year, week = int(match.group(1)), int(match.group(2))
    start = date.fromisocalendar(year, week, 1)
    end = start + timedelta(days=7)
    return start.isoformat(), end.isoformat()


def _table_exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_columns(conn, table: str) -> dict[str, str]:
    cols: dict[str, str] = {}
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        name = row["name"] if hasattr(row, "keys") else row[1]
        typ = row["type"] if hasattr(row, "keys") else row[2]
        cols[name] = typ or "TEXT"
    return cols


def copy_table_range(src_db: Path, dst_db: Path, table: str, date_col: str, start: str, end: str):
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = connect(src_db)
    dst = connect(dst_db)
    try:
        schema_row = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not schema_row:
            return
        if not _table_exists(dst, table):
            schema_sql = re.sub(r"^CREATE TABLE\s+", "CREATE TABLE IF NOT EXISTS ", schema_row["sql"], flags=re.IGNORECASE)
            dst.execute(schema_sql)
        else:
            src_cols = _table_columns(src, table)
            dst_cols = _table_columns(dst, table)
            for col, typ in src_cols.items():
                if col not in dst_cols:
                    dst.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ or 'TEXT'}")
        rows = src.execute(
            f"SELECT * FROM {table} WHERE substr({date_col},1,10) >= ? AND substr({date_col},1,10) < ?",
            (start, end),
        ).fetchall()
        if rows:
            dst_cols = set(_table_columns(dst, table))
            cols = [c for c in rows[0].keys() if c in dst_cols]
            sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
            dst.executemany(sql, [[row[c] for c in cols] for row in rows])
        dst.commit()
    finally:
        src.close()
        dst.close()


def export_table_csv(db_path: Path, table: str, out_csv: Path):
    if not db_path.exists():
        return
    conn = connect(db_path)
    try:
        if not _table_exists(conn, table):
            return
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()
    if not rows:
        return
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        cols = rows[0].keys()
        writer.writerow(cols)
        for row in rows:
            writer.writerow([row[col] for col in cols])


def cleanup_archive(scanner_archive: Path, gex_archive: Path, raw_archive: Path | None = None, meta_archive: Path | None = None):
    if not CLEAN_INVALID_ARCHIVE_ROWS:
        return
    scanner_success_ids: set[int] = set()
    gex_success_ids: set[int] = set()

    if scanner_archive.exists():
        conn = connect(scanner_archive)
        try:
            scanner_success_ids = {int(r[0]) for r in conn.execute("SELECT id FROM scanner_runs WHERE status='success'").fetchall()}
            for table, col in [
                ("scanner_snapshots", "scanner_run_id"),
                ("scanner_snapshot_deltas", "scanner_run_id"),
                ("ticker_detail_zones", "scanner_run_id"),
                ("ticker_detail_history", "scanner_run_id"),
                ("ticker_price_history", "scanner_run_id"),
            ]:
                if _table_exists(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE {col} NOT IN (SELECT id FROM scanner_runs WHERE status='success')")
            if _table_exists(conn, "ticker_daily_history"):
                conn.execute("DELETE FROM ticker_daily_history WHERE last_run_id IS NOT NULL AND last_run_id NOT IN (SELECT id FROM scanner_runs WHERE status='success')")
            conn.execute("DELETE FROM scanner_runs WHERE COALESCE(status,'') <> 'success'")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()

    if gex_archive.exists():
        conn = connect(gex_archive)
        try:
            gex_success_ids = {int(r[0]) for r in conn.execute("SELECT id FROM gex_runs WHERE status='success'").fetchall()}
            for table in ["gex_levels", "gex_summary", "gex_expirations", "gex_expiry_buckets", "gex_failures"]:
                if _table_exists(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE gex_run_id NOT IN (SELECT id FROM gex_runs WHERE status='success')")
            conn.execute("DELETE FROM gex_runs WHERE COALESCE(status,'') <> 'success'")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()

    if raw_archive and raw_archive.exists():
        conn = connect(raw_archive)
        try:
            if scanner_success_ids:
                ph = ",".join(["?"] * len(scanner_success_ids))
                conn.execute(f"DELETE FROM raw_api_payloads WHERE run_type='scanner' AND run_id IS NOT NULL AND run_id NOT IN ({ph})", list(scanner_success_ids))
            if gex_success_ids:
                ph = ",".join(["?"] * len(gex_success_ids))
                conn.execute(f"DELETE FROM raw_api_payloads WHERE run_type='gex' AND run_id IS NOT NULL AND run_id NOT IN ({ph})", list(gex_success_ids))
            conn.commit()
        finally:
            conn.close()

    if meta_archive and meta_archive.exists():
        conn = connect(meta_archive)
        try:
            if scanner_success_ids:
                ph = ",".join(["?"] * len(scanner_success_ids))
                conn.execute(f"DELETE FROM extra_fields WHERE run_type='scanner' AND run_id IS NOT NULL AND run_id NOT IN ({ph})", list(scanner_success_ids))
            if gex_success_ids:
                ph = ",".join(["?"] * len(gex_success_ids))
                conn.execute(f"DELETE FROM extra_fields WHERE run_type='gex' AND run_id IS NOT NULL AND run_id NOT IN ({ph})", list(gex_success_ids))
            conn.commit()
        finally:
            conn.close()




def cleanup_weekly_to_canonical_daily(scanner_archive: Path, gex_archive: Path, raw_archive: Path | None = None, meta_archive: Path | None = None):
    """Weekly archives are the 30-close calibration source, not hourly storage.

    Hourly runs are archived separately in daily intraday ZIPs. Keeping only deep
    runs here prevents a selective run from being mistaken for a daily close by
    the Alerts Bot or the history coverage manifest.
    """
    scanner_keep: set[int] = set()
    gex_keep: set[int] = set()
    scanner_drop: set[int] = set()
    gex_drop: set[int] = set()
    if scanner_archive.exists():
        conn = connect(scanner_archive)
        try:
            rows = conn.execute("SELECT id,session_type FROM scanner_runs").fetchall() if _table_exists(conn, "scanner_runs") else []
            for row in rows:
                rid = int(row[0]); session = str(row[1] or "").lower()
                (scanner_keep if "deep" in session else scanner_drop).add(rid)
            if scanner_drop:
                ph = ",".join(["?"] * len(scanner_drop)); ids = list(scanner_drop)
                for table, col in [("scanner_snapshots","scanner_run_id"),("scanner_snapshot_deltas","scanner_run_id"),("ticker_detail_zones","scanner_run_id"),("ticker_detail_history","scanner_run_id"),("ticker_price_history","scanner_run_id"),("data_quality_events","scanner_run_id")]:
                    if _table_exists(conn, table): conn.execute(f"DELETE FROM {table} WHERE {col} IN ({ph})", ids)
                conn.execute(f"DELETE FROM scanner_runs WHERE id IN ({ph})", ids)
            conn.commit(); conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()
    if gex_archive.exists():
        conn = connect(gex_archive)
        try:
            rows = conn.execute("SELECT id,session_type FROM gex_runs").fetchall() if _table_exists(conn, "gex_runs") else []
            for row in rows:
                rid = int(row[0]); session = str(row[1] or "").lower()
                (gex_keep if "deep" in session else gex_drop).add(rid)
            if gex_drop:
                ph = ",".join(["?"] * len(gex_drop)); ids = list(gex_drop)
                for table in ["gex_summary","gex_levels","gex_expirations","gex_expiry_buckets","gex_failures"]:
                    if _table_exists(conn, table): conn.execute(f"DELETE FROM {table} WHERE gex_run_id IN ({ph})", ids)
                conn.execute(f"DELETE FROM gex_runs WHERE id IN ({ph})", ids)
            conn.commit(); conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()
    if raw_archive and raw_archive.exists():
        conn = connect(raw_archive)
        try:
            if scanner_drop:
                ph = ",".join(["?"] * len(scanner_drop)); conn.execute(f"DELETE FROM raw_api_payloads WHERE run_type='scanner' AND run_id IN ({ph})", list(scanner_drop))
            if gex_drop:
                ph = ",".join(["?"] * len(gex_drop)); conn.execute(f"DELETE FROM raw_api_payloads WHERE run_type='gex' AND run_id IN ({ph})", list(gex_drop))
            conn.commit()
        finally:
            conn.close()
    if meta_archive and meta_archive.exists():
        conn = connect(meta_archive)
        try:
            if scanner_drop:
                ph = ",".join(["?"] * len(scanner_drop)); conn.execute(f"DELETE FROM extra_fields WHERE run_type='scanner' AND run_id IN ({ph})", list(scanner_drop))
            if gex_drop:
                ph = ",".join(["?"] * len(gex_drop)); conn.execute(f"DELETE FROM extra_fields WHERE run_type='gex' AND run_id IN ({ph})", list(gex_drop))
            conn.commit()
        finally:
            conn.close()


def _archive_period(period_key: str, start: str, end: str, weekly: bool, finalize: bool = False):
    ensure_all_schemas()
    period_dir = ARCHIVE_DIR / period_key
    scanner_archive = period_dir / f"scanner_{period_key}.sqlite"
    gex_archive = period_dir / f"gex_{period_key}.sqlite"
    raw_archive = period_dir / f"raw_{period_key}.sqlite"
    meta_archive = period_dir / f"meta_{period_key}.sqlite"

    scanner_tables = [
        ("scanner_runs", "captured_at_madrid"),
        ("scanner_snapshots", "captured_at_madrid"),
        ("ticker_daily_history", "updated_at_madrid"),
        ("ticker_detail_zones", "captured_at_madrid"),
        ("ticker_detail_history", "captured_at_madrid"),
        ("ticker_price_history", "captured_at_madrid"),
        ("ticker_ohlc_daily", "price_date"),
        ("ticker_signal_daily", "history_date"),
        ("score_runs", "captured_at_madrid"),
        ("score_top_longs", "captured_at_madrid"),
        ("score_backtest_metrics", "captured_at_madrid"),
        ("score_backtest_equity_curve", "curve_date"),
        ("score_backtest_monte_carlo", "curve_date"),
        ("score_backtest_bootstrap", "created_at_utc"),
        ("score_basket_history_summary", "captured_at_madrid"),
        ("score_basket_history_days", "basket_date"),
        ("score_basket_history_positions", "basket_date"),
        ("watchlist_runs", "captured_at_madrid"),
        ("watchlists", "captured_at_madrid"),
        ("watchlist_sections", "created_at_utc"),
        ("watchlist_dashboard_rows", "captured_at_madrid"),
        ("watchlist_sparklines", "created_at_utc"),
        ("data_quality_events", "event_at_madrid"),
    ]
    gex_tables = [
        ("gex_runs", "captured_at_madrid"),
        ("gex_summary", "captured_at_madrid"),
        ("gex_levels", "captured_at_madrid"),
        ("gex_expirations", "captured_at_madrid"),
        ("gex_expiry_buckets", "captured_at_madrid"),
        ("gex_failures", "captured_at_madrid"),
    ]
    for table, col in scanner_tables:
        copy_table_range(SCANNER_DB, scanner_archive, table, col, start, end)
    for table, col in gex_tables:
        copy_table_range(GEX_DB, gex_archive, table, col, start, end)
    copy_table_range(RAW_DB, raw_archive, "raw_api_payloads", "captured_at_madrid", start, end)
    copy_table_range(META_DB, meta_archive, "extra_fields", "captured_at_madrid", start, end)
    copy_table_range(META_DB, meta_archive, "schema_change_events", "event_at_madrid", start, end)

    cleanup_archive(scanner_archive, gex_archive, raw_archive, meta_archive)
    if weekly:
        cleanup_weekly_to_canonical_daily(scanner_archive, gex_archive, raw_archive, meta_archive)

    export_csv = (EXPORT_WEEKLY_CSV and finalize) if weekly else EXPORT_MONTHLY_CSV
    if export_csv:
        csv_dir = period_dir / "csv"
        for table in ["scanner_snapshots", "ticker_daily_history", "ticker_ohlc_daily", "ticker_signal_daily", "watchlist_dashboard_rows"]:
            export_table_csv(scanner_archive, table, csv_dir / f"{table}_{period_key}.csv")
        for table in ["gex_summary", "gex_levels", "gex_expirations", "gex_expiry_buckets", "gex_failures"]:
            export_table_csv(gex_archive, table, csv_dir / f"{table}_{period_key}.csv")

    if weekly and finalize:
        marker = period_dir / "FINALIZED.ok"
        marker.write_text(
            f"period={period_key}\nstart={start}\nend_exclusive={end}\nfinalized_at={now_madrid().isoformat()}\n",
            encoding="utf-8",
        )
        if EXPORT_WEEKLY_ZIP:
            zip_path = ARCHIVE_DIR / f"{period_key}.zip"
            tmp_path = zip_path.with_suffix(".zip.tmp")
            inventory = _sqlite_inventory(period_dir)
            archive_manifest = {
                "version": config.BOT_VERSION, "period": period_key, "start": start, "end_exclusive": end,
                "created_at_madrid": now_madrid().isoformat(), "sqlite_inventory": inventory,
                "policy": "Delete source folder only after ZIP + SQLite + row-count verification.",
            }
            (period_dir / "archive_manifest.json").write_text(
                json.dumps(archive_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp_path.unlink(missing_ok=True)
            with ZipFile(tmp_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
                for file in period_dir.rglob("*"):
                    if file.is_file():
                        archive.write(file, file.relative_to(period_dir.parent))
            _verify_period_zip(tmp_path, inventory)
            tmp_path.replace(zip_path)
            if DELETE_FINALIZED_WEEK_FOLDER_AFTER_ZIP:
                shutil.rmtree(period_dir, ignore_errors=True)
                return zip_path
    return period_dir


def archive_week(week_key: str | None = None, finalize: bool = False):
    if not ARCHIVE_ENABLED:
        return None
    week_key = week_key or iso_week_key()
    period_dir = ARCHIVE_DIR / week_key
    marker = period_dir / "FINALIZED.ok"
    zip_path = ARCHIVE_DIR / f"{week_key}.zip"
    if finalize and EXPORT_WEEKLY_ZIP and zip_path.exists():
        return zip_path
    if finalize and marker.exists() and not EXPORT_WEEKLY_ZIP:
        return period_dir
    start, end = week_bounds(week_key)
    return _archive_period(week_key, start, end, weekly=True, finalize=finalize)


def archive_recent_weeks():
    if not ARCHIVE_ENABLED:
        return []
    now = now_madrid()
    out = [archive_week(iso_week_key(now), finalize=False)]
    if ARCHIVE_PREVIOUS_WEEK:
        out.append(archive_week(iso_week_key(now - timedelta(days=7)), finalize=True))
    return out


def archive_month(month_key: str):
    """Compatibilidad con v1; la v2 usa archive_recent_weeks()."""
    if not ARCHIVE_ENABLED:
        return None
    start, end = month_bounds(month_key)
    return _archive_period(month_key, start, end, weekly=False, finalize=True)
