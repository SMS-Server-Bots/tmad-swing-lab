from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from return_math import normalize_signal_outcome_row

VERSION = "6.2.67"
ROOT = Path(os.getenv("TMAD_DATA_DIR", Path(__file__).resolve().parents[2] / "03_DATOS")).resolve()
DB = ROOT / "alerts_bot" / "db" / "live" / "tmad_alerts_adaptive.db"


def _connect(*, timeout: float = 20.0, autocommit: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=timeout, isolation_level=None if autocommit else "DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _column(con: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if name not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _storage_metrics(con: sqlite3.Connection) -> dict:
    def p(name: str, default: int = 0) -> int:
        try:
            row = con.execute(f"PRAGMA {name}").fetchone()
            return int(row[0] or 0) if row else default
        except Exception:
            return default
    page_size = p("page_size", 4096)
    page_count = p("page_count")
    freelist = p("freelist_count")
    allocated = max(0, page_size * page_count)
    reclaimable = max(0, page_size * freelist)
    return {
        "file_mb": round(DB.stat().st_size / (1024 * 1024), 1) if DB.exists() else 0.0,
        "allocated_mb": round(allocated / (1024 * 1024), 1),
        "reclaimable_mb": round(reclaimable / (1024 * 1024), 1),
        "reclaimable_pct": round(reclaimable / max(1, allocated) * 100.0, 2),
        "freelist_pages": freelist,
        "auto_vacuum": p("auto_vacuum"),
    }


def migrate_legacy_target_closes(con: sqlite3.Connection) -> int:
    _column(con, "open_trades", "legacy_exit_reason", "TEXT")
    _column(con, "open_trades", "calibration_excluded_reason", "TEXT")
    _column(con, "open_trades", "lifecycle_state", "TEXT NOT NULL DEFAULT 'OPEN'")
    _column(con, "open_trades", "target_touch_count", "INTEGER NOT NULL DEFAULT 0")
    _column(con, "open_trades", "target_touch_at_madrid", "TEXT")
    _column(con, "open_trades", "target_touch_price", "REAL")
    _column(con, "open_trades", "setup_hit", "INTEGER NOT NULL DEFAULT 0")
    _column(con, "open_trades", "setup_hit_at_madrid", "TEXT")
    _column(con, "open_trades", "setup_hit_date", "TEXT")
    _column(con, "open_trades", "setup_hit_price", "REAL")
    _column(con, "open_trades", "setup_hit_return_pct", "REAL")
    _column(con, "open_trades", "setup_hit_sessions", "INTEGER")
    rows = con.execute(
        "SELECT id,exit_reason,exit_return_pct,exit_price,exit_at_madrid,exit_date,sessions_seen "
        "FROM open_trades WHERE status='closed' "
        "AND UPPER(COALESCE(exit_reason,'')) LIKE 'GEX_TARGET_ZONE%'"
    ).fetchall()
    for tid, reason, ret, price, at, date, sessions in rows:
        hit = 1 if abs(float(ret or 0)) >= 5 else 0
        con.execute(
            """UPDATE open_trades SET legacy_exit_reason=?, calibration_excluded_reason='LEGACY_TARGET_TOUCH_CLOSE',
              exit_reason=?, lifecycle_state='CLOSED_LEGACY_TARGET_TOUCH_CLOSE', target_touch_count=CASE WHEN target_touch_count<1 THEN 1 ELSE target_touch_count END,
              target_touch_at_madrid=COALESCE(target_touch_at_madrid,?), target_touch_price=COALESCE(target_touch_price,?),
              setup_hit=CASE WHEN ?=1 THEN 1 ELSE setup_hit END, setup_hit_at_madrid=CASE WHEN ?=1 THEN COALESCE(setup_hit_at_madrid,?) ELSE setup_hit_at_madrid END,
              setup_hit_date=CASE WHEN ?=1 THEN COALESCE(setup_hit_date,?) ELSE setup_hit_date END,
              setup_hit_price=CASE WHEN ?=1 THEN COALESCE(setup_hit_price,?) ELSE setup_hit_price END,
              setup_hit_return_pct=CASE WHEN ?=1 THEN COALESCE(setup_hit_return_pct,?) ELSE setup_hit_return_pct END,
              setup_hit_sessions=CASE WHEN ?=1 THEN COALESCE(setup_hit_sessions,?) ELSE setup_hit_sessions END
              WHERE id=?""",
            (
                reason, "LEGACY_TARGET_TOUCH_CLOSE: " + str(reason), at, price,
                hit, hit, at, hit, date, hit, price, hit, ret, hit, sessions, tid,
            ),
        )
    return len(rows)


def migrate_short_return_math(con: sqlite3.Connection) -> int:
    """Repair persisted SHORT labels produced with inverse-return math.

    Archive rows are normalized at read time elsewhere, so this migration only
    repairs the mutable live SQLite copy. It is safe to run repeatedly.
    """
    tables = {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "layer_signal_outcomes" not in tables:
        return 0

    has_history = "layer_signal_history" in tables
    if has_history:
        rows = con.execute(
            """SELECT o.*,h.target_pct
               FROM layer_signal_outcomes o
               LEFT JOIN layer_signal_history h ON h.id=o.layer_signal_id
               WHERE LOWER(COALESCE(o.direction,''))='short'
                 AND COALESCE(o.sessions_json,o.future_path_json,'')<>''"""
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT o.*,NULL AS target_pct
               FROM layer_signal_outcomes o
               WHERE LOWER(COALESCE(o.direction,''))='short'
                 AND COALESCE(o.sessions_json,o.future_path_json,'')<>''"""
        ).fetchall()

    updated = 0
    for raw in rows:
        item = normalize_signal_outcome_row(dict(raw))
        if item.get("return_math_status") != "SHORT_V2_ENTRY_NOTIONAL":
            continue
        con.execute(
            """UPDATE layer_signal_outcomes
               SET movement_5pct_hit=?,first_5pct_session=?,
                   max_favorable_pct=?,max_adverse_pct=?,target_error_pct=?,
                   close_return_pct=?,resolved_reason=?
               WHERE id=?""",
            (
                item.get("movement_5pct_hit"),
                item.get("first_5pct_session"),
                item.get("max_favorable_pct"),
                item.get("max_adverse_pct"),
                item.get("target_error_pct"),
                item.get("close_return_pct"),
                item.get("resolved_reason"),
                int(item.get("id") or 0),
            ),
        )
        updated += 1

    if "runtime_state" in tables:
        payload = json.dumps(
            {
                "version": 2,
                "mode": "ENTRY_NOTIONAL_SHORT_RETURN",
                "rows_recomputed": updated,
                "formula": "(entry-exit)/entry",
            },
            ensure_ascii=False,
        )
        runtime_state_columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(runtime_state)").fetchall()
        }
        if "updated_at_utc" in runtime_state_columns:
            updated_at_utc = datetime.now(ZoneInfo("UTC")).isoformat()
            con.execute(
                """INSERT INTO runtime_state(state_key,state_value,updated_at_utc)
                   VALUES('historical_short_return_math',?,?)
                   ON CONFLICT(state_key) DO UPDATE SET
                       state_value=excluded.state_value,
                       updated_at_utc=excluded.updated_at_utc""",
                (payload, updated_at_utc),
            )
        else:
            # Compatibility with legacy/test runtime_state schemas. Production
            # v6.2 has updated_at_utc and takes the branch above.
            con.execute(
                """INSERT INTO runtime_state(state_key,state_value)
                   VALUES('historical_short_return_math',?)
                   ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value""",
                (payload,),
            )
    return updated


def _maintenance_allowed() -> bool:
    now = datetime.now(ZoneInfo("Europe/Madrid"))
    return now.weekday() >= 5 or not (12 <= now.hour < 24)


def _run_migration() -> int:
    con = _connect(timeout=30)
    try:
        migrated = migrate_legacy_target_closes(con)
        migrated += migrate_short_return_math(con)
        con.commit()
        return migrated
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()


def _run_optimize() -> dict:
    if not _maintenance_allowed():
        return {"optimized": False, "warning": None, "vacuumed": False, "before": {}, "after": {}}
    con = _connect(timeout=15, autocommit=True)
    try:
        before = _storage_metrics(con)
        con.execute("PRAGMA optimize")
        con.execute("PRAGMA wal_checkpoint(PASSIVE)")
        now = datetime.now(ZoneInfo("Europe/Madrid"))
        weekend = now.weekday() >= 5
        vacuum_enabled = weekend and _env_bool("SQLITE_WEEKEND_VACUUM_ENABLED", True)
        min_free_mb = max(32.0, float(os.getenv("SQLITE_WEEKEND_VACUUM_MIN_FREE_MB", "128") or 128))
        min_free_pct = max(5.0, float(os.getenv("SQLITE_WEEKEND_VACUUM_MIN_FREE_PCT", "20") or 20))
        vacuumed = False
        if (
            vacuum_enabled
            and float(before.get("reclaimable_mb") or 0) >= min_free_mb
            and float(before.get("reclaimable_pct") or 0) >= min_free_pct
        ):
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
            con.execute("PRAGMA optimize")
            vacuumed = True
        after = _storage_metrics(con)
        return {"optimized": True, "warning": None, "vacuumed": vacuumed, "before": before, "after": after}
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return {"optimized": False, "warning": str(exc), "vacuumed": False, "before": {}, "after": {}}
        raise
    finally:
        con.close()


def main() -> dict:
    started = time.perf_counter()
    allowed = _maintenance_allowed()
    db_mb = round(DB.stat().st_size / (1024 * 1024), 1) if DB.exists() else 0.0
    if not DB.exists():
        result = {"migrated": 0, "optimized": False, "pending": False, "warning": "alerts_db_missing", "vacuumed": False}
        print(
            f"[MAINT ALERTS v{VERSION}] status=MISSING elapsed={time.perf_counter()-started:.2f}s "
            f"window={'OFF_HOURS' if allowed else 'MARKET_HOURS'} db={db_mb:.1f}MB reason=alerts_db_missing",
            flush=True,
        )
        return result

    migrated = _run_migration()
    optimize = _run_optimize()
    warning = optimize.get("warning")
    pending = bool(warning)
    result = {
        "migrated": migrated,
        "optimized": bool(optimize.get("optimized")),
        "pending": pending,
        "warning": warning,
        "vacuumed": bool(optimize.get("vacuumed")),
        "storage_before": optimize.get("before") or {},
        "storage_after": optimize.get("after") or {},
    }
    if warning:
        status = "DEFERRED_LOCK"
    elif not allowed:
        status = "MIGRATION_ONLY"
    else:
        status = "OK"
    before = optimize.get("before") or {}
    after = optimize.get("after") or {}
    print(
        f"[MAINT ALERTS v{VERSION}] status={status} elapsed={time.perf_counter()-started:.2f}s "
        f"window={'OFF_HOURS' if allowed else 'MARKET_HOURS'} db={db_mb:.1f}MB "
        f"migration_rows={migrated} optimize={int(bool(optimize.get('optimized')))} "
        f"vacuum={int(bool(optimize.get('vacuumed')))} reclaimable_before={float(before.get('reclaimable_mb') or 0):.1f}MB "
        f"reclaimable_after={float(after.get('reclaimable_mb') or 0):.1f}MB pending={int(pending)} "
        f"warning={warning or 'none'}",
        flush=True,
    )
    return result


if __name__ == "__main__":
    main()
