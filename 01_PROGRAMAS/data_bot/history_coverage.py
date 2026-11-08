from __future__ import annotations

import csv
import io
import json
import hashlib
import math
import re
import sqlite3
import tempfile
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import config
from archive_manager import archive_week, iso_week_key
from time_utils import now_madrid
from coordination_bus import publish_data_state

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_RECOVERED_BOOTSTRAP_ID: str | None = None
_RECOVERED_BOOTSTRAP_STARTED_AT: str | None = None


def _previous_bootstrap_metadata() -> tuple[str | None, str | None]:
    """Read the last valid identity so intraday refreshes cannot erase it."""
    path = config.HISTORY_COVERAGE_FILE
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if str(payload.get("bot_version") or "") != str(config.BOT_VERSION):
        return None, None
    bootstrap_id = str(payload.get("bootstrap_id") or "").strip() or None
    started_at = str(payload.get("bootstrap_started_at_madrid") or "").strip() or None
    return bootstrap_id, started_at


def _resolve_bootstrap_metadata(
    bootstrap_complete: bool,
    bootstrap_id: str | None,
    bootstrap_started_at: str | None,
) -> tuple[str | None, str | None]:
    """Keep a stable bootstrap identity for the whole Data process generation.

    Startup supplies an explicit id. Subsequent archive/coverage refreshes used to
    call ``ensure_history_archives`` without it and silently replaced the id with
    null, making Alerts reject an otherwise complete CORE manifest. If an old or
    manually repaired installation has no id, create one once and then persist it.
    """
    requested_id = str(bootstrap_id or "").strip() or None
    requested_started = str(bootstrap_started_at or "").strip() or None
    if requested_id:
        return requested_id, requested_started

    previous_id, previous_started = _previous_bootstrap_metadata()
    if previous_id:
        return previous_id, requested_started or previous_started

    if not bootstrap_complete:
        return None, requested_started

    global _RECOVERED_BOOTSTRAP_ID, _RECOVERED_BOOTSTRAP_STARTED_AT
    if not _RECOVERED_BOOTSTRAP_ID:
        recovered_now = now_madrid()
        stamp = recovered_now.strftime("%Y%m%dT%H%M%S%z")
        _RECOVERED_BOOTSTRAP_ID = f"data-recovered-{stamp}-{uuid.uuid4().hex[:8]}"
        _RECOVERED_BOOTSTRAP_STARTED_AT = recovered_now.isoformat()
    return _RECOVERED_BOOTSTRAP_ID, requested_started or _RECOVERED_BOOTSTRAP_STARTED_AT


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _distinct_dates(db_path: Path, table: str, date_col: str, success_filter: str | None = None) -> set[str]:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        if not _table_exists(conn, table):
            return set()
        sql = f"SELECT DISTINCT substr({date_col},1,10) FROM {table}"
        if success_filter:
            sql += f" WHERE {success_filter}"
        return {
            str(row[0])
            for row in conn.execute(sql).fetchall()
            if row and row[0] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row[0]))
        }
    finally:
        conn.close()


def _live_date_sets() -> tuple[set[str], set[str], set[str]]:
    daily_filter = "status='success' AND LOWER(COALESCE(session_type,'')) LIKE '%deep%'"
    scanner = _distinct_dates(config.SCANNER_DB, "scanner_runs", "snapshot_date", daily_filter)
    gex = _distinct_dates(config.GEX_DB, "gex_runs", "snapshot_date", daily_filter)
    return scanner, gex, scanner & gex


def _week_key_from_iso(day: str) -> str:
    parsed = date.fromisoformat(day)
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def _csv_dates_from_zip(archive: ZipFile, table_token: str, daily_only: bool = True) -> set[str]:
    member = next(
        (
            name
            for name in archive.namelist()
            if f"/csv/{table_token}_" in name and name.lower().endswith(".csv")
        ),
        None,
    )
    if not member:
        return set()
    out: set[str] = set()
    with archive.open(member) as raw:
        wrapper = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        for row in csv.DictReader(wrapper):
            if daily_only and "deep" not in str(row.get("session_type") or "").lower():
                continue
            value = str(row.get("snapshot_date") or row.get("captured_at_madrid") or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                out.add(value)
    return out


def _sqlite_dates_from_zip(archive: ZipFile, filename_token: str, table: str, date_col: str, daily_only: bool = True) -> set[str]:
    member = next(
        (
            name
            for name in archive.namelist()
            if filename_token in Path(name).name and name.lower().endswith((".sqlite", ".db"))
        ),
        None,
    )
    if not member:
        return set()
    with tempfile.TemporaryDirectory(prefix="tmad_zip_dates_") as tmp:
        db_path = Path(tmp) / Path(member).name
        db_path.write_bytes(archive.read(member))
        success_filter = "status='success' AND LOWER(COALESCE(session_type,'')) LIKE '%deep%'" if daily_only else None
        return _distinct_dates(db_path, table, date_col, success_filter)


def paired_dates_from_zip(zip_path: Path) -> set[str]:
    try:
        with ZipFile(zip_path) as archive:
            scanner = _csv_dates_from_zip(archive, "scanner_snapshots", daily_only=True)
            gex = _csv_dates_from_zip(archive, "gex_summary", daily_only=True)
            if not scanner:
                scanner = _sqlite_dates_from_zip(archive, "scanner_", "scanner_runs", "snapshot_date", daily_only=True)
            if not gex:
                gex = _sqlite_dates_from_zip(archive, "gex_", "gex_runs", "snapshot_date", daily_only=True)
            return scanner & gex
    except Exception:
        return set()


def _load_zip_cache() -> dict:
    path = config.HISTORY_COVERAGE_CACHE_FILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_zip_cache(payload: dict) -> None:
    path = config.HISTORY_COVERAGE_CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _zip_date_map() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if not config.ARCHIVE_DIR.exists():
        return out
    cache = _load_zip_cache()
    updated: dict = {}
    for path in sorted(config.ARCHIVE_DIR.glob("????-W??.zip")):
        stat = path.stat()
        cached = cache.get(path.name) if isinstance(cache.get(path.name), dict) else {}
        signature = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
        if cached.get("size") == signature["size"] and cached.get("mtime_ns") == signature["mtime_ns"]:
            dates = {str(x) for x in cached.get("paired_dates", [])}
        else:
            dates = paired_dates_from_zip(path)
        out[path.name] = dates
        updated[path.name] = {**signature, "paired_dates": sorted(dates)}
    _write_zip_cache(updated)
    return out


def _existing_archive_week_dirs() -> set[str]:
    out: set[str] = set()
    if not config.ARCHIVE_DIR.exists():
        return out
    for path in config.ARCHIVE_DIR.iterdir():
        if path.is_dir() and _WEEK_RE.fullmatch(path.name):
            out.add(path.name)
    return out


def _archive_zip_needs_refresh(zip_path: Path, expected_dates: set[str]) -> bool:
    if not zip_path.exists():
        return True
    if not expected_dates:
        return False
    actual = paired_dates_from_zip(zip_path)
    return not expected_dates.issubset(actual)


def generate_missing_weekly_archives() -> dict:
    """Create weekly archives for every week represented by real local runs.

    Past weeks are finalized to ZIP. The current week remains an incremental folder.
    Existing finalized ZIPs are rebuilt only when local DBs contain paired dates that
    the ZIP does not yet contain. No historical snapshots are fabricated.
    """
    scanner_dates, gex_dates, paired_live = _live_date_sets()
    all_live_dates = scanner_dates | gex_dates
    current_week = iso_week_key()
    weeks = {_week_key_from_iso(day) for day in all_live_dates}
    weeks |= _existing_archive_week_dirs()
    generated: list[str] = []
    refreshed: list[str] = []
    current_updated = False
    errors: list[str] = []

    for week_key in sorted(weeks):
        try:
            if week_key == current_week:
                archive_week(week_key, finalize=False)
                current_updated = True
                continue
            expected = {day for day in paired_live if _week_key_from_iso(day) == week_key}
            zip_path = config.ARCHIVE_DIR / f"{week_key}.zip"
            needs_refresh = _archive_zip_needs_refresh(zip_path, expected)
            if needs_refresh and zip_path.exists() and config.HISTORY_REBUILD_INCOMPLETE_ZIPS:
                zip_path.unlink(missing_ok=True)
                refreshed.append(zip_path.name)
            existed = zip_path.exists()
            result = archive_week(week_key, finalize=True)
            if result and not existed and Path(result).exists():
                generated.append(Path(result).name)
        except Exception as exc:
            errors.append(f"{week_key}: {exc}")

    return {
        "live_scanner_dates": len(scanner_dates),
        "live_gex_dates": len(gex_dates),
        "live_paired_dates": len(paired_live),
        "weeks_considered": sorted(weeks),
        "generated_zips": generated,
        "refreshed_zips": refreshed,
        "current_week_updated": current_updated,
        "errors": errors,
    }




def _current_scanner_universe(conn: sqlite3.Connection) -> dict:
    """Return the complete universe from the latest successful Scanner run."""
    row = conn.execute(
        """SELECT id,snapshot_date,session_type,COALESCE(base_unique_tickers,0) AS declared
           FROM scanner_runs
           WHERE status='success'
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return {"run_id": 0, "snapshot_date": None, "session_type": None, "tickers": []}
    run_id = int(row[0] or 0)
    tickers = [
        str(item[0]).upper()
        for item in conn.execute(
            """SELECT ticker FROM scanner_snapshots
               WHERE scanner_run_id=? AND ticker IS NOT NULL
               GROUP BY ticker ORDER BY MIN(rowid)""",
            (run_id,),
        ).fetchall()
        if str(item[0] or "").strip()
    ]
    return {
        "run_id": run_id,
        "snapshot_date": str(row[1] or ""),
        "session_type": str(row[2] or ""),
        "declared_tickers": int(row[3] or 0),
        "tickers": tickers,
    }


CORE_COMPLETION_POLICY = "per_ticker_latest_sessions_v1"


def _core_ticker_session_counts(
    conn: sqlite3.Connection,
    tickers: list[str],
) -> dict[str, int]:
    """Count valid TMAD history sessions independently for every Scanner ticker.

    CORE calibration requires 30 real sessions per ticker, but it must not require
    every request batch to end on the exact same market date. TMAD detail calls can
    roll across a close, so strict common-date intersection falsely excludes valid
    symbols even when each one contains the requested 30 observations.
    """
    counts: dict[str, int] = {}
    if not tickers:
        return counts
    for start in range(0, len(tickers), 350):
        chunk = tickers[start:start + 350]
        ticker_ph = ",".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""SELECT s.ticker,COUNT(DISTINCT s.history_date) AS n
                FROM ticker_signal_daily s
                LEFT JOIN ticker_ohlc_daily p
                  ON p.ticker=s.ticker AND p.price_date=s.history_date
                WHERE s.ticker IN ({ticker_ph})
                  AND COALESCE(s.close_price,p.close,p.price) IS NOT NULL
                GROUP BY s.ticker""",
            chunk,
        ).fetchall()
        for ticker, n in rows:
            counts[str(ticker).upper()] = int(n or 0)
    return counts


def _core_complete_tickers(
    conn: sqlite3.Connection,
    tickers: list[str],
    target: int,
) -> tuple[int, list[str], dict[str, int]]:
    counts = _core_ticker_session_counts(conn, tickers)
    missing = [ticker for ticker in tickers if int(counts.get(ticker, 0)) < int(target)]
    return len(tickers) - len(missing), missing, counts


def _core_latest_session_union(
    conn: sqlite3.Connection, tickers: list[str], target: int
) -> tuple[list[str], dict[str, int]]:
    """Union of each eligible ticker's latest target sessions.

    The union can be target+1 dates when API batches straddle a close. That is
    expected and must reconcile identically in Data and Alerts.
    """
    by_ticker: dict[str, list[str]] = {ticker: [] for ticker in tickers}
    if not tickers:
        return [], {}
    for start in range(0, len(tickers), 350):
        chunk = tickers[start:start + 350]
        ph = ",".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""SELECT s.ticker,s.history_date
                FROM ticker_signal_daily s
                LEFT JOIN ticker_ohlc_daily p
                  ON p.ticker=s.ticker AND p.price_date=s.history_date
                WHERE s.ticker IN ({ph})
                  AND COALESCE(s.close_price,p.close,p.price) IS NOT NULL
                GROUP BY s.ticker,s.history_date
                ORDER BY s.ticker,s.history_date""",
            chunk,
        ).fetchall()
        for ticker, day in rows:
            by_ticker.setdefault(str(ticker).upper(), []).append(str(day))
    date_counts: dict[str, int] = {}
    for ticker in tickers:
        days = sorted(set(by_ticker.get(ticker) or []))[-int(target):]
        if len(days) < int(target):
            continue
        for day in days:
            date_counts[day] = date_counts.get(day, 0) + 1
    return sorted(date_counts), date_counts


def _core_window_fingerprint(
    universe_fingerprint: str, universe_date: str | None, dates: list[str]
) -> str:
    if not universe_fingerprint:
        return ""
    latest = str(dates[-1] if dates else universe_date or "")
    token = f"{CORE_COMPLETION_POLICY}|{universe_fingerprint}|{universe_date or ''}|{latest}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _core_resolution_state(
    conn: sqlite3.Connection,
    universe_fingerprint: str,
    universe_date: str | None,
    dates: list[str],
    missing_tickers: list[str],
) -> dict:
    """Resolve current-window exclusions without hiding Scanner membership.

    Every Scanner ticker must be attempted. A ticker that deterministically cannot
    provide 30 valid ticker sessions is recorded as excluded for that exact
    universe+window and retried automatically when either changes.
    """
    window_fingerprint = _core_window_fingerprint(universe_fingerprint, universe_date, dates)
    excluded: list[str] = []
    reasons: dict[str, str] = {}
    if window_fingerprint and _table_exists(conn, "core_bootstrap_ticker_state"):
        rows = conn.execute(
            """SELECT ticker,exclusion_reason FROM core_bootstrap_ticker_state
               WHERE universe_fingerprint=? AND window_fingerprint=? AND status='excluded'""",
            (universe_fingerprint, window_fingerprint),
        ).fetchall()
        missing_set = set(missing_tickers)
        for ticker, reason in rows:
            ticker = str(ticker or "").upper()
            if ticker in missing_set:
                excluded.append(ticker)
                reasons[ticker] = str(reason or "excluded_current_window")
    excluded = sorted(set(excluded))
    excluded_set = set(excluded)
    pending = [ticker for ticker in missing_tickers if ticker not in excluded_set]
    return {
        "window_fingerprint": window_fingerprint,
        "excluded_tickers_list": excluded,
        "excluded_ticker_reasons": reasons,
        "pending_tickers_list": pending,
        "excluded_fingerprint": hashlib.sha256(
            "\n".join(excluded).encode("utf-8")
        ).hexdigest() if excluded else "",
    }


def core_history_coverage() -> dict:
    """Coverage of TMAD's compact /api/stock/{ticker} daily history.

    The newest sufficiently represented dates form the shared PIT calendar.
    Older batches are refreshed when that rolling window advances; a signal close
    may anchor a date even when priceHistory rolls at a slightly different time.
    """
    target = max(1, int(config.CORE_HISTORY_TARGET_SESSIONS))
    empty = {
        "ready": False, "dates": [], "date_count": 0, "target": target,
        "start_date": None, "end_date": None, "rows": 0, "tickers": 0,
        "quality_threshold": int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE),
        "date_row_counts": {},
        "universe_run_id": 0, "universe_date": None, "universe_fingerprint": "",
        "universe_tickers": 0, "complete_tickers": 0, "remaining_tickers": 0,
        "universe_complete": False, "universe_completion_ratio": 0.0,
        "resolved_tickers": 0, "excluded_tickers": 0, "pending_tickers": 0,
        "excluded_fingerprint": "", "window_fingerprint": "",
        "excluded_ticker_sample": [], "pending_ticker_sample": [],
        "missing_ticker_sample": [],
    }
    if not config.SCANNER_DB.exists():
        return empty
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    try:
        if not (_table_exists(conn, "ticker_signal_daily") and _table_exists(conn, "ticker_ohlc_daily")):
            return empty
        universe = _current_scanner_universe(conn)
        universe_tickers = list(universe.get("tickers") or [])
        universe_fingerprint = hashlib.sha256(
            "\n".join(sorted(universe_tickers)).encode("utf-8")
        ).hexdigest() if universe_tickers else ""
        universe_base = {
            "universe_run_id": int(universe.get("run_id") or 0),
            "universe_date": universe.get("snapshot_date"),
            "universe_fingerprint": universe_fingerprint,
            "universe_tickers": len(universe_tickers),
        }
        empty_universe_meta = {
            **universe_base,
            "complete_tickers": 0,
            "remaining_tickers": len(universe_tickers),
            "universe_complete": False,
            "universe_completion_ratio": 0.0,
            "resolved_tickers": 0,
            "excluded_tickers": 0,
            "pending_tickers": len(universe_tickers),
            "excluded_fingerprint": "",
            "window_fingerprint": "",
            "excluded_ticker_sample": [],
            "pending_ticker_sample": universe_tickers[:50],
            "missing_ticker_sample": universe_tickers[:50],
        }
        rows = conn.execute(
            """SELECT s.history_date,COUNT(DISTINCT s.ticker) AS n
               FROM ticker_signal_daily s
               LEFT JOIN ticker_ohlc_daily p
                 ON p.ticker=s.ticker AND p.price_date=s.history_date
               WHERE s.close_price IS NOT NULL OR p.close IS NOT NULL OR p.price IS NOT NULL
               GROUP BY s.history_date ORDER BY s.history_date"""
        ).fetchall()
        if not rows:
            return {**empty, **empty_universe_meta}
        maximum = max(int(row[1] or 0) for row in rows)
        threshold = max(
            int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE),
            int(math.ceil(maximum * float(config.CORE_HISTORY_MIN_COVERAGE_RATIO))),
        )
        # Reporting calendar only. Eligibility is per ticker, not a strict
        # intersection of dates across request batches.
        calendar_threshold = max(1, min(int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE), maximum))
        qualifying = [(str(row[0]), int(row[1] or 0)) for row in rows if int(row[1] or 0) >= calendar_threshold]
        selected = qualifying[-target:]
        dates = [day for day, _ in selected]
        if not dates:
            return {**empty, **empty_universe_meta, "quality_threshold": threshold}
        complete_tickers, missing_tickers, ticker_session_counts = _core_complete_tickers(
            conn, universe_tickers, target
        )
        eligible_tickers = [ticker for ticker in universe_tickers if ticker not in set(missing_tickers)]
        materialized_dates, materialized_date_counts = _core_latest_session_union(
            conn, eligible_tickers, target
        )
        if materialized_dates:
            dates = materialized_dates
        resolution = _core_resolution_state(
            conn, universe_fingerprint, universe.get("snapshot_date"), dates, missing_tickers
        )
        excluded_tickers_list = list(resolution.get("excluded_tickers_list") or [])
        pending_tickers_list = list(resolution.get("pending_tickers_list") or [])
        resolved_tickers = int(complete_tickers) + len(excluded_tickers_list)
        eligible_ratio = complete_tickers / max(len(universe_tickers), 1)
        minimum_eligible_ratio = max(0.80, min(1.0, float(config.CORE_HISTORY_MIN_ELIGIBLE_UNIVERSE_RATIO)))
        universe_complete = bool(
            len(dates) >= target
            and universe_tickers
            and not pending_tickers_list
            and complete_tickers >= int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE)
            and eligible_ratio >= minimum_eligible_ratio
        )
        universe_meta = {
            **universe_base,
            "complete_tickers": int(complete_tickers),
            "resolved_tickers": int(resolved_tickers),
            "excluded_tickers": len(excluded_tickers_list),
            "pending_tickers": len(pending_tickers_list),
            "remaining_tickers": len(pending_tickers_list),
            "universe_complete": universe_complete,
            "universe_completion_ratio": round(eligible_ratio, 6),
            "universe_resolved_ratio": round(resolved_tickers / max(len(universe_tickers), 1), 6),
            "minimum_eligible_ratio": minimum_eligible_ratio,
            "window_fingerprint": str(resolution.get("window_fingerprint") or ""),
            "excluded_fingerprint": str(resolution.get("excluded_fingerprint") or ""),
            "excluded_ticker_sample": excluded_tickers_list[:50],
            "excluded_ticker_reasons": {
                ticker: str((resolution.get("excluded_ticker_reasons") or {}).get(ticker) or "excluded_current_window")
                for ticker in excluded_tickers_list[:200]
            },
            "pending_ticker_sample": pending_tickers_list[:50],
            "missing_ticker_sample": missing_tickers[:50],
        }
        capped_rows = sum(min(int(ticker_session_counts.get(ticker, 0)), target) for ticker in universe_tickers)
        return {
            "ready": bool(len(dates) >= target and universe_complete),
            "dates": dates,
            "date_count": len(dates),
            "target": target,
            "start_date": dates[0],
            "end_date": dates[-1],
            "rows": int(capped_rows),
            "tickers": int(complete_tickers),
            "quality_threshold": threshold,
            "calendar_threshold": calendar_threshold,
            "date_row_counts": materialized_date_counts or {day: count for day, count in selected},
            **universe_meta,
            "universe_quality": (
                "COMPLETE_CURRENT_SCANNER" if universe_complete and not excluded_tickers_list
                else ("RESOLVED_WITH_AUDITED_EXCLUSIONS" if universe_complete else "PARTIAL_CURRENT_SCANNER")
            ),
            "source": "TMAD_DETAIL_30D",
            "completion_policy": CORE_COMPLETION_POLICY,
        }
    finally:
        conn.close()



def build_history_coverage(generation: dict | None = None, bootstrap_complete: bool = True, bootstrap_id: str | None = None, bootstrap_started_at: str | None = None) -> dict:
    scanner_live, gex_live, paired_live = _live_date_sets()
    zip_map = _zip_date_map()
    archive_dates: set[str] = set()
    contributing_zips: list[str] = []
    for name, dates in zip_map.items():
        if dates:
            archive_dates |= dates
            contributing_zips.append(name)
    paired_all = archive_dates | paired_live
    ordered = sorted(paired_all)
    start = ordered[0] if ordered else None
    end = ordered[-1] if ordered else None
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 if start and end else 0
    target_days = max(1, int(config.HISTORY_TARGET_DAYS))
    target_sessions = max(1, int(config.DAILY_CLOSE_WINDOW_SESSIONS))
    minimum_sessions = target_sessions
    ready = len(ordered) >= target_sessions
    missing_span = max(0, target_sessions - len(ordered))
    core = core_history_coverage()
    if core.get("ready") and ready:
        status = "core_and_full_ready"
    elif core.get("ready"):
        status = "core_ready_full_collecting"
    else:
        status = "collecting"
    payload = {
        "schema_version": 3,
        "bot_version": config.BOT_VERSION,
        "generated_at_madrid": now_madrid().isoformat(),
        "bootstrap_complete": bool(bootstrap_complete),
        "bootstrap_id": bootstrap_id,
        "bootstrap_started_at_madrid": bootstrap_started_at,
        "status": status,
        "ready": ready,
        "target_calendar_days": target_days,
        "target_daily_closes": target_sessions,
        "minimum_trading_dates": minimum_sessions,
        "coverage_start_date": start,
        "coverage_end_date": end,
        "coverage_calendar_days": span,
        "paired_trading_dates": len(ordered),
        "missing_daily_closes": missing_span,
        "missing_calendar_span_days": max(0, target_days - span),
        "live_paired_dates": len(paired_live),
        "archive_paired_dates": len(archive_dates),
        "contributing_zip_count": len(contributing_zips),
        "contributing_zips": contributing_zips,
        "paired_dates": ordered,
        "core_history_ready": bool(core.get("ready")),
        "core_history_dates": int(core.get("date_count") or 0),
        "core_history_target_sessions": int(core.get("target") or config.CORE_HISTORY_TARGET_SESSIONS),
        "core_history_start_date": core.get("start_date"),
        "core_history_end_date": core.get("end_date"),
        "core_history_rows": int(core.get("rows") or 0),
        "core_history_tickers": int(core.get("tickers") or 0),
        "core_history_universe_run_id": int(core.get("universe_run_id") or 0),
        "core_history_universe_date": core.get("universe_date"),
        "core_history_universe_tickers": int(core.get("universe_tickers") or 0),
        "core_history_universe_fingerprint": str(core.get("universe_fingerprint") or ""),
        "core_history_complete_tickers": int(core.get("complete_tickers") or 0),
        "core_history_resolved_tickers": int(core.get("resolved_tickers") or 0),
        "core_history_excluded_tickers": int(core.get("excluded_tickers") or 0),
        "core_history_pending_tickers": int(core.get("pending_tickers") or core.get("remaining_tickers") or 0),
        "core_history_remaining_tickers": int(core.get("remaining_tickers") or 0),
        "core_history_window_fingerprint": str(core.get("window_fingerprint") or ""),
        "core_history_excluded_fingerprint": str(core.get("excluded_fingerprint") or ""),
        "core_history_excluded_ticker_sample": core.get("excluded_ticker_sample") or [],
        "core_history_pending_ticker_sample": core.get("pending_ticker_sample") or [],
        "core_history_universe_complete": bool(core.get("universe_complete")),
        "core_history_universe_completion_ratio": float(core.get("universe_completion_ratio") or 0.0),
        "core_history_missing_ticker_sample": core.get("missing_ticker_sample") or [],
        "core_history_quality_threshold": int(core.get("quality_threshold") or 0),
        "core_history_date_row_counts": core.get("date_row_counts") or {},
        "core_history_source": core.get("source"),
        "core_history_universe_quality": core.get("universe_quality"),
        "note": (
            "CORE TMAD y FULL DEEP listos."
            if ready and core.get("ready")
            else (
                "CORE TMAD listo para calibración inicial; FULL DEEP e intradía siguen acumulándose."
                if core.get("ready")
                else "Se seguirá completando CORE TMAD y acumulando FULL DEEP; la capa horaria no sustituye al cierre."
            )
        ),
        "generation": generation or {},
    }
    return payload


def write_history_coverage(payload: dict) -> Path:
    path = config.HISTORY_COVERAGE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    # Shared transactional handoff. A semantic change increments generation;
    # volatile timestamps alone do not wake the Alerts Bot.
    try:
        publish_data_state(
            config.COORDINATION_DB,
            payload,
            reason=("bootstrap_complete" if payload.get("bootstrap_complete") else "bootstrap_in_progress"),
            backup_json_path=config.COORDINATION_STATE_FILE,
        )
    except Exception as exc:
        # Coordination is an acceleration/recovery layer, never a hard dependency.
        # Alerts can still use the atomic history_coverage.json fallback.
        error_path = config.OUTPUT_DIR / "coordination_publish_error.log"
        error_path.write_text(f"{datetime.now().isoformat()} | {exc}\n", encoding="utf-8")
    return path


def mark_history_bootstrap_started(bootstrap_id: str, started_at_madrid: str) -> dict:
    """Overwrite any stale completed manifest before startup work begins.

    Alerts Bot waits for this same bootstrap id to finish instead of consuming a
    completed manifest left by a previous process.
    """
    payload = build_history_coverage(
        generation={"stage": "startup_in_progress"},
        bootstrap_complete=False,
        bootstrap_id=bootstrap_id,
        bootstrap_started_at=started_at_madrid,
    )
    write_history_coverage(payload)
    return payload


def ensure_history_archives(
    bootstrap_complete: bool = True,
    bootstrap_id: str | None = None,
    bootstrap_started_at: str | None = None,
) -> dict:
    resolved_id, resolved_started_at = _resolve_bootstrap_metadata(
        bootstrap_complete, bootstrap_id, bootstrap_started_at
    )
    generation = generate_missing_weekly_archives() if config.HISTORY_AUTO_GENERATE_ARCHIVES else {}
    payload = build_history_coverage(
        generation,
        bootstrap_complete=bootstrap_complete,
        bootstrap_id=resolved_id,
        bootstrap_started_at=resolved_started_at,
    )
    write_history_coverage(payload)
    return payload
