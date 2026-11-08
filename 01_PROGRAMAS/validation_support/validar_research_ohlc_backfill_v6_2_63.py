from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alerts_bot"))

import config
import research_ohlc_backfill as backfill
import research_swing_exit_policy as prices


def _weekdays(first: date, count: int) -> list[date]:
    out = []
    now = first
    while len(out) < count:
        if now.weekday() < 5:
            out.append(now)
        now += timedelta(days=1)
    return out


def main() -> int:
    dates = _weekdays(date(2026, 7, 1), 12)
    ticker = "AAPL"
    bars = [{
        "ticker": ticker, "trade_date": day.isoformat(),
        "open": 100 + i, "high": 101 + i, "low": 99 + i,
        "close": 100.5 + i, "source": backfill.SOURCE,
        "source_symbol": ticker,
    } for i, day in enumerate(dates)]
    utc = ZoneInfo("America/New_York")
    payload = {"chart": {"error": None, "result": [{
        "meta": {"currency": "USD", "instrumentType": "EQUITY", "symbol": ticker},
        "timestamp": [int(datetime(day.year, day.month, day.day, 10, 0, tzinfo=utc).timestamp()) for day in dates],
        "indicators": {"quote": [{
            "open": [b["open"] for b in bars],
            "high": [b["high"] for b in bars],
            "low": [b["low"] for b in bars],
            "close": [b["close"] for b in bars],
        }]},
    }]}}
    raw, err = backfill._parse_chart(payload, ticker, dates[0], dates[-1])
    assert err is None and len(raw) == 12, (err, len(raw))
    assert all(x["source"] == backfill.SOURCE for x in raw)

    split = json.loads(json.dumps(payload))
    split["chart"]["result"][0]["events"] = {"splits": {"171234": {"numerator": 4}}}
    assert backfill._parse_chart(split, ticker, dates[0], dates[-1])[1] == "SPLIT_EVENT_IN_REQUESTED_WINDOW"

    wrong = json.loads(json.dumps(payload))
    wrong["chart"]["result"][0]["meta"]["symbol"] = "MSFT"
    assert backfill._parse_chart(wrong, ticker, dates[0], dates[-1])[1] == "SYMBOL_MISMATCH"

    invalid = json.loads(json.dumps(payload))
    invalid["chart"]["result"][0]["indicators"]["quote"][0]["low"][0] = 999
    assert backfill._parse_chart(invalid, ticker, dates[0], dates[-1])[1] == "BAD_OHLC_GEOMETRY"

    orig_db, orig_dir = config.ALERTS_DB, config.DATA_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.ALERTS_DB = root / "operational.db"
            config.DATA_ROOT = root / "03_DATOS"
            with sqlite3.connect(config.ALERTS_DB) as conn:
                conn.executescript("""
                  CREATE TABLE price_cache (
                    ticker TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
                    close REAL, source TEXT, split_factor REAL);
                """)
                conn.executemany(
                    "INSERT INTO price_cache VALUES(?,?,?,?,?,?,?,?)",
                    [(ticker, x["trade_date"], x["close"], x["close"], x["close"],
                      x["close"], "FEATURE_POINT_IN_TIME_CLOSE", 1.0) for x in bars]
                )
            # Without separate research backfill, OHLC research is fail-closed.
            signal = (dates[0] - timedelta(days=1)).isoformat()
            entry = {"ticker": ticker, "signal_date": signal}
            existing, prev = prices._load_db_price_paths([entry])
            assert prices._future_path(entry, existing[(ticker, signal)], sessions=10)[1] == "INVALID_OR_CLOSE_ONLY_OHLC"

            dbfile = config.DATA_ROOT / "alerts_bot" / "db" / "research_ohlc_backfill.sqlite"
            with backfill._open_db(dbfile) as conn:
                backfill._store(conn, ticker, raw)
                backfill._record(conn, ticker, "OK", "", raw)
                rev = backfill._revision(conn)
                assert len(rev) == 64
            overlay, metrics = prices._load_db_price_paths([entry])
            assert metrics["backfill_complete_paths_applied"] == 1, metrics
            assert prices._future_path(entry, overlay[(ticker, signal)], sessions=10)[1] is None
            # A genuine source-anchor mismatch must quarantine the supplement.
            with sqlite3.connect(config.ALERTS_DB) as conn:
                conn.execute("UPDATE price_cache SET open=500,high=501,low=499,close=500,source='data_live'"
                             " WHERE trade_date=?", (dates[1].isoformat(),))
            _, blocked = prices._load_db_price_paths([entry])
            assert blocked["backfill_price_basis_conflict"] == 1, blocked
    finally:
        config.ALERTS_DB, config.DATA_ROOT = orig_db, orig_dir

    deploy = (ROOT.parent / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8")
    autorun = (ROOT / "alerts_bot" / "auto_joint_stress.py").read_text(encoding="utf-8")
    assert "research_ohlc_backfill.py" in autorun
    assert "BACKFILL_RUNNING" in autorun
    assert "6.2.65" in deploy
    assert "research_ohlc_revision" in autorun
    assert "research_ohlc_revision" in (ROOT / "alerts_bot" / "research_joint_entry_exit_stress.py").read_text()
    print("PASS TMAD 6.2.63: Yahoo OHLC provenance, split and symbol fail-closed; isolated DB overlay; no canonical mutation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
