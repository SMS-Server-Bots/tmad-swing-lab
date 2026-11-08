from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import config
from db import (
    archive_imported,
    insert_feature_observation,
    mark_archive_imported,
    upsert_price_cache_rows,
)
from rules import build_features, feature_observation
from time_utils import now_madrid

SOURCE_KEY = "legacy-old2-scanner"
STATUS_FILE = config.OUTPUT_DIR / "legacy_history_import.json"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _rowdict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _date10(value) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else ""


def _latest_runs(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    if table not in _tables(conn):
        return {}
    cols = _columns(conn, table)
    if "id" not in cols:
        return {}
    date_col = next((c for c in ("snapshot_date", "captured_at_madrid", "created_at_madrid", "created_at_utc") if c in cols), None)
    captured_col = next((c for c in ("captured_at_madrid", "created_at_madrid", "created_at_utc") if c in cols), date_col)
    if not date_col:
        return {}
    sql = f'SELECT * FROM "{table}"'
    if "status" in cols:
        sql += " WHERE LOWER(COALESCE(status,'')) IN ('success','ok','complete','completed')"
    conn.row_factory = sqlite3.Row
    best: dict[str, tuple[tuple[int, str, int], dict]] = {}
    for row in conn.execute(sql):
        item = _rowdict(row)
        day = _date10(item.get(date_col))
        if not day:
            continue
        session = str(item.get("session_type") or "").lower()
        captured = str(item.get(captured_col) or item.get(date_col) or "")
        rank = (1 if "deep" in session else 0, captured, int(item.get("id") or 0))
        if day not in best or rank > best[day][0]:
            best[day] = (rank, item)
    return {day: item for day, (_rank, item) in best.items()}


def _scanner_daily(db: Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        tables = _tables(conn)
        if "scanner_snapshots" not in tables:
            return out
        cols = _columns(conn, "scanner_snapshots")
        ticker_col = "ticker" if "ticker" in cols else ("symbol" if "symbol" in cols else None)
        run_fk = "scanner_run_id" if "scanner_run_id" in cols else ("run_id" if "run_id" in cols else None)
        if not ticker_col:
            return out
        runs = _latest_runs(conn, "scanner_runs")
        if runs and run_fk:
            for day, run in runs.items():
                for row in conn.execute(f'SELECT * FROM scanner_snapshots WHERE "{run_fk}"=?', (run["id"],)):
                    item = _rowdict(row)
                    ticker = str(item.get(ticker_col) or "").upper().strip()
                    if not ticker:
                        continue
                    item["ticker"] = ticker
                    item["snapshot_date"] = day
                    item["captured_at_madrid"] = str(item.get("captured_at_madrid") or run.get("captured_at_madrid") or day)
                    item["session_type"] = "legacy_deep"
                    key = (day, ticker)
                    prev = out.get(key)
                    if not prev or str(item["captured_at_madrid"]) > str(prev.get("captured_at_madrid") or ""):
                        out[key] = item
        return out
    finally:
        conn.close()


def _gex_daily(db: Path) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], dict[str, dict]]]:
    summary: dict[tuple[str, str], dict] = {}
    buckets: dict[tuple[str, str], dict[str, dict]] = {}
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        tables = _tables(conn)
        if "gex_summary" not in tables:
            return summary, buckets
        cols = _columns(conn, "gex_summary")
        ticker_col = "ticker" if "ticker" in cols else ("symbol" if "symbol" in cols else None)
        run_fk = "gex_run_id" if "gex_run_id" in cols else ("run_id" if "run_id" in cols else None)
        if not ticker_col:
            return summary, buckets
        runs = _latest_runs(conn, "gex_runs")
        if runs and run_fk:
            for day, run in runs.items():
                run_id = run["id"]
                for row in conn.execute(f'SELECT * FROM gex_summary WHERE "{run_fk}"=?', (run_id,)):
                    item = _rowdict(row)
                    ticker = str(item.get(ticker_col) or "").upper().strip()
                    if not ticker:
                        continue
                    item["ticker"] = ticker
                    item["snapshot_date"] = day
                    item["captured_at_madrid"] = str(item.get("captured_at_madrid") or run.get("captured_at_madrid") or day)
                    item["session_type"] = "legacy_deep"
                    summary[(day, ticker)] = item
                if "gex_expiry_buckets" in tables:
                    bcols = _columns(conn, "gex_expiry_buckets")
                    b_run = "gex_run_id" if "gex_run_id" in bcols else ("run_id" if "run_id" in bcols else None)
                    if b_run:
                        for row in conn.execute(f'SELECT * FROM gex_expiry_buckets WHERE "{b_run}"=?', (run_id,)):
                            item = _rowdict(row)
                            ticker = str(item.get("ticker") or "").upper().strip()
                            bucket = str(item.get("expiry_bucket") or "")
                            if ticker and bucket:
                                buckets.setdefault((day, ticker), {})[bucket] = item
        return summary, buckets
    finally:
        conn.close()


def import_legacy_history(legacy_root: Path) -> dict:
    legacy_root = legacy_root.resolve()
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if archive_imported(SOURCE_KEY):
        previous = {}
        try:
            if STATUS_FILE.exists():
                previous = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
        imported_meta = {}
        try:
            conn = sqlite3.connect(config.ALERTS_DB)
            row = conn.execute(
                "SELECT imported_at_utc, feature_rows, price_rows, note FROM archive_imports WHERE archive_name=?",
                (SOURCE_KEY,),
            ).fetchone()
            date_row = conn.execute(
                "SELECT COUNT(DISTINCT snapshot_date) FROM feature_observations WHERE source_archive=?",
                (SOURCE_KEY,),
            ).fetchone()
            conn.close()
            if row:
                imported_meta = {
                    "imported_at_utc": row[0],
                    "features_imported": int(row[1] or 0),
                    "price_rows_imported": int(row[2] or 0),
                    "note": row[3] or "",
                    "date_count": int((date_row or [0])[0] or 0),
                }
        except Exception:
            imported_meta = {}
        status = {
            **previous,
            **imported_meta,
            "status": "already_imported",
            "source": SOURCE_KEY,
            "legacy_root": str(legacy_root),
        }
        STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return status
    if not legacy_root.is_dir():
        raise FileNotFoundError(f"Legacy history not found: {legacy_root}")

    scanner: dict[tuple[str, str], dict] = {}
    gex: dict[tuple[str, str], dict] = {}
    buckets: dict[tuple[str, str], dict[str, dict]] = {}
    sources = []

    for db in sorted(legacy_root.rglob("*")):
        if not db.is_file() or db.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            continue
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
            tables = _tables(conn)
            conn.close()
            used = False
            if "scanner_snapshots" in tables:
                for key, row in _scanner_daily(db).items():
                    prev = scanner.get(key)
                    if not prev or str(row.get("captured_at_madrid") or "") > str(prev.get("captured_at_madrid") or ""):
                        scanner[key] = row
                used = True
            if "gex_summary" in tables:
                s, b = _gex_daily(db)
                for key, row in s.items():
                    prev = gex.get(key)
                    if not prev or str(row.get("captured_at_madrid") or "") > str(prev.get("captured_at_madrid") or ""):
                        gex[key] = row
                        buckets[key] = b.get(key, {})
                used = True
            if used:
                sources.append(str(db.relative_to(legacy_root)).replace("\\", "/"))
        except Exception as exc:
            sources.append(f"ERROR:{db.name}:{type(exc).__name__}:{exc}")

    inserted = 0
    prices = []
    dates = sorted({day for day, _ticker in scanner})
    for (day, ticker), snapshot in sorted(scanner.items()):
        gex_row = gex.get((day, ticker))
        features = build_features(snapshot, gex_row, buckets.get((day, ticker), {}), [])
        features["source_archive"] = SOURCE_KEY
        features["observation_layer"] = "daily_close"
        features["is_canonical_daily_close"] = 1
        try:
            row = feature_observation(snapshot, gex_row, features)
        except Exception:
            continue
        row["source_archive"] = SOURCE_KEY
        row["observation_layer"] = "daily_close"
        row["is_canonical_daily_close"] = 1
        if insert_feature_observation(row):
            inserted += 1

        price = snapshot.get("current_price")
        if price is None:
            price = snapshot.get("price")
        if price is None:
            price = snapshot.get("close")
        if price is not None:
            prices.append({
                "ticker": ticker,
                "trade_date": day,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            })

    price_rows = upsert_price_cache_rows(prices, SOURCE_KEY) if prices else 0
    mark_archive_imported(SOURCE_KEY, inserted, price_rows, "old_2 legacy Scanner -> canonical daily_close")
    status = {
        "status": "ok",
        "source": SOURCE_KEY,
        "legacy_root": str(legacy_root),
        "dates": dates,
        "date_count": len(dates),
        "scanner_daily_rows": len(scanner),
        "gex_daily_rows": len(gex),
        "features_imported": inserted,
        "price_rows_imported": price_rows,
        "source_databases": sources,
        "finished_at_madrid": now_madrid().isoformat(),
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return status


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--legacy-root",
        default=r"C:\Users\Administrator\Desktop\old_2\old Bot Scanner_data",
    )
    args = parser.parse_args()
    result = import_legacy_history(Path(args.legacy_root))
    print(json.dumps(result, ensure_ascii=False))
