from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

import config
from gex_negative_cache import clear_success as gex_cache_clear_success
from gex_negative_cache import record_failure as gex_cache_record_failure
from gex_negative_cache import should_skip as gex_cache_should_skip
from time_utils import market_date_from_madrid_iso, market_session_date_str, ts_madrid_str
from tmad_client import fetch_stock_gex
from transforms import build_scanner_snapshot, summarize_gex_payload
from utils import ticker_from_row, to_float

RISK_REFRESH_FILE = config.OUTPUT_DIR / "risk_refresh_latest.json"


def _open_trade_tickers() -> list[str]:
    db = Path(config.ALERTS_DB_PATH)
    if not db.exists():
        return []
    conn = sqlite3.connect(db, timeout=10)
    try:
        rows = conn.execute(
            """SELECT DISTINCT UPPER(ot.ticker)
               FROM open_trades ot
               JOIN alert_events ae ON ae.id=ot.alert_event_id
               WHERE ot.status='open'
               ORDER BY UPPER(ot.ticker)"""
        ).fetchall()
        return [str(row[0]).upper() for row in rows if row and row[0]]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _write_atomic(payload: dict) -> None:
    RISK_REFRESH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = RISK_REFRESH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(RISK_REFRESH_FILE)


def build_partial_risk_refresh(
    *,
    scanner_run_id: int,
    session_type: str,
    rows: list[dict],
    integrity: dict,
    client,
) -> dict:
    """Refresh only open positions when the global Scanner cannot be handed off.

    This path deliberately does not persist Scanner/GEX research rows, publish a
    Data generation, run FUSION, or create entries. It only writes a bounded
    sidecar consumed by Alerts lifecycle for positions already open.
    """
    generated_at = ts_madrid_str()
    by_ticker: dict[str, dict] = {}
    for row in rows:
        ticker = str(ticker_from_row(row) or "").upper()
        if ticker and ticker not in by_ticker:
            by_ticker[ticker] = row

    open_tickers = _open_trade_tickers()
    items: list[dict] = []
    missing: list[str] = []
    for ticker in open_tickers:
        scanner_row = by_ticker.get(ticker)
        if scanner_row is None:
            missing.append(ticker)
            continue
        gex_payload = None
        gex_summary: dict = {}
        expirations: list[dict] = []
        expiry_buckets: dict[str, dict] = {}
        gex_status = "unavailable"
        gex_error = ""
        cached_skip, cached_entry = gex_cache_should_skip(ticker, scanner_row)
        if cached_skip:
            gex_status = "unavailable_cached"
            gex_error = f"negative_cache_{(cached_entry or {}).get('status_code') or 'unknown'}"
        else:
            try:
                gex_payload, status = fetch_stock_gex(client, ticker)
                gex_cache_clear_success(ticker)
                current_price = to_float(scanner_row.get("currentPrice"))
                summary, _levels, raw_expirations, raw_buckets = summarize_gex_payload(
                    gex_payload,
                    current_price=current_price,
                    ticker=ticker,
                    gex_run_id=-int(scanner_run_id),
                )
                captured = ts_madrid_str()
                gex_summary = {
                    "gex_run_id": -int(scanner_run_id),
                    "captured_at_madrid": captured,
                    "snapshot_date": market_date_from_madrid_iso(captured),
                    "session_type": f"{session_type}_risk_refresh",
                    "ticker": ticker,
                    "current_price": current_price,
                    "change_percent": to_float(scanner_row.get("changePercent")),
                    **summary,
                }
                expirations = [
                    {
                        "gex_run_id": -int(scanner_run_id),
                        "captured_at_madrid": captured,
                        "snapshot_date": market_date_from_madrid_iso(captured),
                        "session_type": f"{session_type}_risk_refresh",
                        "ticker": ticker,
                        **item,
                    }
                    for item in raw_expirations
                ]
                expiry_buckets = {
                    str(item.get("expiry_bucket") or ""): {
                        "gex_run_id": -int(scanner_run_id),
                        "captured_at_madrid": captured,
                        "snapshot_date": market_date_from_madrid_iso(captured),
                        "session_type": f"{session_type}_risk_refresh",
                        "ticker": ticker,
                        **item,
                    }
                    for item in raw_buckets
                    if str(item.get("expiry_bucket") or "")
                }
                gex_status = "ok"
            except Exception as exc:
                status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None else None
                gex_cache_record_failure(ticker, status_code, market_session_date_str())
                gex_status = "unavailable" if status_code == 404 else "error"
                gex_error = f"{type(exc).__name__}:{str(exc)[:180]}"

        statuses = {
            "detail_status": "not_requested_risk_refresh",
            "intraday_status": "not_requested_risk_refresh",
            "gex_status": gex_status,
            "error_message": gex_error or None,
        }
        snapshot = build_scanner_snapshot(
            int(scanner_run_id),
            f"{session_type}_risk_refresh",
            ticker,
            scanner_row,
            None,
            None,
            gex_summary,
            {"scanner": None, "gex": None},
            statuses,
        )
        items.append({
            "ticker": ticker,
            "snapshot": snapshot,
            "gex": gex_summary if gex_status == "ok" else None,
            "expiry_buckets": expiry_buckets,
            "expirations": expirations,
            "gex_status": gex_status,
            "gex_error": gex_error,
        })

    payload = {
        "schema_version": 1,
        "kind": "POSITION_RISK_ONLY",
        "generated_at_madrid": generated_at,
        "scanner_run_id": int(scanner_run_id),
        "session_type": str(session_type),
        "scanner_status": "partial",
        "scanner_unique": int(integrity.get("unique_tickers") or len(by_ticker)),
        "scanner_expected": int(integrity.get("expected") or len(by_ticker)),
        "scanner_shortfall": int(integrity.get("shortfall") or 0),
        "open_tickers": open_tickers,
        "missing_open_tickers": missing,
        "items": items,
        "entries_allowed": False,
        "research_allowed": False,
        "learning_allowed": False,
    }
    _write_atomic(payload)
    return payload
