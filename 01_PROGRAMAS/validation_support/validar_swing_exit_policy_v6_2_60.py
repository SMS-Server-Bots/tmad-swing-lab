from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alerts_bot"))

import config
from research_swing_exit_policy import (
    POLICIES, _future_path, _load_db_price_paths, _normalized_entry,
    _valid_ohlc, compare_entries, simulate_exit,
)


def _sessions(n=12, *, start=date(2026, 8, 17)):
    days = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _candles():
    # First five days increase, then profits deteriorate but the initial stop
    # is never violated. The runner should react using confirmed closes only.
    closes = [102, 104, 107, 109, 110, 108, 105, 103, 101, 100]
    return [{
        "trade_date": day, "open": 100.0 if i == 0 else closes[i-1],
        "high": max(100.0 if i == 0 else closes[i-1], price) + .5,
        "low": min(100.0 if i == 0 else closes[i-1], price) - .5,
        "close": float(price), "source": "OBSERVED_OHLC",
    } for i, (day, price) in enumerate(zip(_sessions(11, start=date(2026, 8, 18)), closes))]


def _entry(ticker="AAA", direction="LONG", stop=95.0, target=108.0):
    return {
        "ticker": ticker, "direction": direction, "signal_date": "2026-08-17",
        "entry_price": 100.0, "invalidation_price": stop,
        "target_price": target,
    }


def main():
    entry = _normalized_entry(_entry())
    rows = _candles()
    path, error = _future_path(entry, rows)
    assert error is None and len(path) == 10, (error, len(path))
    a = simulate_exit(entry, path, "TIME_5")
    b = simulate_exit(entry, path, "TIME_10")
    c = simulate_exit(entry, path, "TRAIL_CLOSE_1R")
    d = simulate_exit(entry, path, "TRAIL_CLOSE_2R")
    e = simulate_exit(entry, path, "GEX_MILESTONE_TRAIL")
    assert a["held_sessions"] == 5 and a["gross_return_pct"] == 10, a
    assert b["held_sessions"] == 10 and b["gross_return_pct"] == 0, b
    assert c["held_sessions"] in (7, 8) and c["exit_reason"] == "PRIOR_CLOSE_PEAK_GIVEBACK", c
    assert e["held_sessions"] <= b["held_sessions"], (b, e)
    assert d["entry_price"] == a["entry_price"] and d["exit_date"] >= a["entry_date"]

    view = compare_entries([_entry()], {("AAA", "2026-08-17"): rows})
    assert all(v["sample"] == 1 for v in view["cohorts"].values()), view
    assert all(len(v["arms"]) == len(POLICIES) for k, v in view["cohorts"].items() if k == "COMPLETE_10D"), view
    assert view["cohorts"]["COMPLETE_10D"]["same_entries_and_dates"] is True
    assert view["cohorts"]["COMPLETE_10D"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert all(v["trades"] == 1 for v in view["cohorts"]["COMPLETE_10D"]["arms"].values())

    # Gap-through stop uses open, not an unattainable stop-price fill.
    short = _normalized_entry(_entry("BBB", "SHORT", 105.0, 90))
    short_rows = [{
        "date": _sessions(2, start=date(2026, 8, 18))[0],
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
    }, {
        "date": _sessions(2, start=date(2026, 8, 18))[1],
        "open": 108.0, "high": 109.0, "low": 106.0, "close": 107.0,
    }] + [{
        "date": day, "open": 107, "high": 108, "low": 106, "close": 107,
    } for day in _sessions(12, start=date(2026, 8, 18))[2:10]]
    output = simulate_exit(short, short_rows, "TIME_10")
    assert output["exit_reason"] == "GAP_THROUGH_STOP" and output["exit_price"] == 108, output

    # If target and stop hit same daily bar, the stop takes priority.
    ambiguous = _normalized_entry(_entry("CCC", "LONG", 95.0, 108.0))
    same_bar = [{**rows[0], "open": 100.0, "high": 112.0, "low": 91.0}]
    z = simulate_exit(ambiguous, same_bar, "TIME_5")
    assert z is None  # requires the predeclared five-session observation window
    normalized, err = _future_path(ambiguous, [*same_bar, *rows[1:10]])
    assert err is None and len(normalized) == 10, (err, normalized)
    z = simulate_exit(ambiguous, normalized, "TIME_10")
    assert z["exit_reason"] == "INVALIDATION_STOP" and z["exit_price"] == 95.0, z

    bad = [{**r} for r in rows]
    bad[2].pop("high")
    clean, why = _future_path(entry, bad)
    assert why == "INVALID_OR_CLOSE_ONLY_OHLC" and clean == [], why
    assert _valid_ohlc({**rows[0], "source": "FEATURE_POINT_IN_TIME_CLOSE"}, signal_date=entry["signal_date"]) is None
    short_path, status = _future_path(entry, rows[:5])
    assert status == "INSUFFICIENT_FORWARD_SESSIONS" and len(short_path) == 5

    # Detached loader supports older SQLite databases without split_factor.
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "alerts.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE price_cache(ticker TEXT,trade_date TEXT,open REAL,high REAL,low REAL,close REAL,source TEXT)")
        conn.execute("CREATE TABLE observation_outcomes(ticker TEXT,observation_date TEXT,complete INTEGER,sessions_json TEXT,horizon_sessions INTEGER,id INTEGER)")
        conn.executemany(
            "INSERT INTO price_cache VALUES(?,?,?,?,?,?,?)",
            [("AAA", x["trade_date"], x["open"], x["high"], x["low"], x["close"], x["source"]) for x in rows]
        )
        conn.commit()
        conn.close()
        old = config.ALERTS_DB
        try:
            config.ALERTS_DB = db
            loaded, provenance = _load_db_price_paths([_entry()])
            assert provenance["status"] == "OK" and len(loaded[("AAA", "2026-08-17")]) == 10, provenance
        finally:
            config.ALERTS_DB = old

    print("PASS | TMAD 6.2.60 | frozen entry exit comparison, no lookahead, conservative bars/gaps, data adequacy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
