from __future__ import annotations

"""TMAD 6.2.65: no false OHLC completion or endless replay on cached data."""

from datetime import date, timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alerts_bot"))
import research_ohlc_backfill as lab


def _bars(ticker: str, start: date, end: date, limit: int = 100) -> list[dict]:
    output = []
    index = 0
    day = start
    while day <= end and len(output) < limit:
        if day.weekday() < 5:
            close = 100 + index * 0.1
            output.append({
                "ticker": ticker, "trade_date": day.isoformat(),
                "open": close, "high": close + 1, "low": close - 1,
                "close": close, "source": lab.SOURCE,
                "source_symbol": ticker, "split_factor": 1.0,
            })
            index += 1
        day += timedelta(days=1)
    return output


def main() -> None:
    days = ["2025-01-02", "2026-06-01"]
    windows = lab._request_windows(days, date(2026, 10, 10))
    assert len(windows) == 2, windows
    assert all((b - a).days <= lab.MAX_WINDOW_CALENDAR_DAYS for a, b in windows)
    assert windows[0][0] == date(2025, 1, 3)
    assert lab._coverage(days, _bars("AAPL", *windows[0]))[0] == 1
    late_only = _bars("AAPL", *windows[1])
    assert lab._coverage(days, late_only)[0] == 1
    # The 2025 signal MUST NOT be given ten bars beginning in June 2026.
    _, late_errors = lab._coverage(days, late_only)
    assert late_errors["STALE_ENTRY_TO_FIRST_BAR"] == 1, late_errors
    assert lab._coverage(days, _bars("AAPL", *windows[0]) + late_only)[0] == 2
    assert lab._coverage(days, _bars("AAPL", *windows[0], limit=4))[0] == 0

    old_open, old_loader = lab._open_db, lab._load_db_price_paths
    old_required, old_status = lab._required_keys, lab.STATUS_FILE
    try:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "isolated_research.sqlite"
            lab._open_db = lambda: old_open(db)
            lab.STATUS_FILE = root / "latest_research_ohlc_backfill.json"
            lab._required_keys = lambda: (
                {"AAPL": days},
                {"signal_keys": 2, "tickers": 1, "cache": {}, "final_test_locked": True},
            )

            def read_local(entries, **kw):
                result = {}
                with old_open(db) as conn:
                    for item in entries:
                        ticker, day = item["ticker"], item["signal_date"]
                        rows = conn.execute(
                            "SELECT trade_date,open,high,low,close,source,split_factor "
                            "FROM research_ohlc WHERE ticker=? AND trade_date>? ORDER BY trade_date",
                            (ticker, day),
                        ).fetchall()
                        result[(ticker, day)] = [
                            dict(zip(["trade_date","open","high","low","close","source","split_factor"], row))
                            for row in rows
                        ]
                return result, {"status": "OK"}
            lab._load_db_price_paths = read_local
            # Old v6.2.63 could mark a request OK despite missing forward
            # sessions. The new scanner must retry that explicitly.
            with old_open(db) as conn:
                lab._record(conn, "AAPL", "OK", "", [])
            calls = []
            def fake_fetch(ticker, begin, finish):
                calls.append((ticker, begin, finish))
                return _bars(ticker, begin, finish), None

            report = lab.run(max_tickers=1, pause_seconds=0.0, fetcher=fake_fetch)
            assert report["prior_ok_but_gapped"] == 1, report
            assert report["attempted"] == 1
            assert len(calls) == 2, calls
            assert report["outcomes"]["gaps_after_keys"] == 0, report
            assert report["outcomes"]["gaps_after_tickers"] == 0, report
            assert report["outcomes"]["coverage_status"] == "COMPLETE", report
            assert report["outcomes"]["batch_statuses"]["OK"] == 1
            assert len(report["outcomes"]["revision"]) == 64
            second = lab.run(max_tickers=1, pause_seconds=0.0, fetcher=fake_fetch)
            assert second["attempted"] == 0 and second["gaps"]["keys"] == 0, second
            assert len(calls) == 2, calls

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "partial_research.sqlite"
            lab._open_db = lambda: old_open(db)
            lab.STATUS_FILE = root / "latest_research_ohlc_backfill.json"
            lab._required_keys = lambda: (
                {"AAPL": days},
                {"signal_keys": 2, "tickers": 1, "cache": {}, "final_test_locked": True},
            )
            def limited(ticker, begin, finish):
                return _bars(ticker, begin, finish, limit=4), None
            partial = lab.run(max_tickers=1, pause_seconds=0.0, fetcher=limited)
            assert partial["outcomes"]["gaps_after_keys"] == 2, partial
            assert partial["outcomes"]["batch_statuses"]["PARTIAL"] == 1, partial
            assert partial["outcomes"]["coverage_status"] == "INCOMPLETE", partial

    finally:
        lab._open_db, lab._load_db_price_paths = old_open, old_loader
        lab._required_keys, lab.STATUS_FILE = old_required, old_status

    autorun = (ROOT / "alerts_bot" / "auto_joint_stress.py").read_text(encoding="utf-8")
    deploy = (ROOT.parent / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8")
    assert "BACKFILL_BATCH_COMPLETE" in autorun
    assert '"--max-tickers", "300"' in autorun
    assert "if ($version -eq '6.2.65' -and -not $NoStart)" in deploy
    assert "Replay FULL omitido en 6.2.65" in deploy
    print("PASS TMAD 6.2.65: multiwindow historical OHLC; old false OK; verified coverage; bounded batch runner")


if __name__ == "__main__":
    main()
