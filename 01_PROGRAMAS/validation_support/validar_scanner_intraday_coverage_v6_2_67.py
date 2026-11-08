from __future__ import annotations
"""Offline evidence: all-run Scanner coverage is distinct from a daily anchor."""
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "alerts_bot"))
import config
from scanner_intraday_coverage_audit import (
    _choose_representative_run, _label, audit_rows, load_observed_rows, run_if_stale,
)


def test_pure_audit():
    assert _choose_representative_run({10: 4, 20: 4}) == 20
    assert _choose_representative_run({31: 4, 32: 2}) == 31
    assert _choose_representative_run({0: 10, 50: 4}) is None
    assert _label({"complete_5d": True, "mfe_long_pct": 8., "mae_long_pct": -1.}) == "LONG_HIT"
    assert _label({"complete_5d": True, "mfe_long_pct": 1., "mae_long_pct": -7.}) == "SHORT_HIT"
    assert _label({"complete_5d": True, "mfe_long_pct": 8., "mae_long_pct": -7.}) == "BOTH_SIDES_HIT"
    assert _label({"complete_5d": False, "mfe_long_pct": 9., "mae_long_pct": -9.}) == "UNLABELLED"
    sample = [
        {"date": "2026-09-01", "run_id": 10, "ticker": "A", "complete_5d": True,
         "mfe_long_pct": 7., "mae_long_pct": -2., "observed_at_madrid": "2026-09-01T16:00:00"},
        *({"date": "2026-09-01", "run_id": 10, "ticker": v} for v in ("B", "C", "D")),
        *({"date": "2026-09-01", "run_id": 20, "ticker": v} for v in ("B", "C", "D", "E")),
        *({"date": "2026-09-02", "run_id": 31, "ticker": v} for v in ("F", "G", "H", "I")),
        *({"date": "2026-09-02", "run_id": 32, "ticker": v} for v in ("J", "K")),
        {"date": "2026-09-03", "run_id": 50, "ticker": "L", "complete_5d": True,
         "mfe_long_pct": 2., "mae_long_pct": -8.},
        *({"date": "2026-09-03", "run_id": 50, "ticker": v} for v in ("M", "N", "O")),
    ]
    stats = {
        "2026-09-01": {10: 4, 20: 4},
        "2026-09-02": {31: 4, 32: 2},
        "2026-09-03": {0: 10, 50: 4},
    }
    result = audit_rows(sample, run_stats=stats)
    assert result["dates"] == 3
    assert result["dates_without_representative_run"] == 1
    assert result["total_observed_ticker_dates"] == 15
    assert result["representative_ticker_dates"] == 8
    assert result["exclusive_ticker_dates_in_other_runs"] == 7
    assert result["unique_excluded_ticker_dates_with_complete_h5_labels"] == 2
    assert result["unique_excluded_ticker_dates_with_h5_5pct_move"] == 2
    assert result["cohorts"]["other_runs"]["observations"] == 10
    assert result["daily"][0]["representative_run_id"] == 20
    assert result["daily"][1]["representative_run_id"] == 31
    assert result["daily"][2]["representative_run_id"] is None
    assert result["no_entry_promotion"] and result["not_frozen_operational_snapshot"]


def test_readonly_db_join():
    # A tiny SQLite DB confirms one outcome per exact observation_id, not a
    # same-day ticker join which would leak future prices across Scanner runs.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "read_only_test.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript("""
                CREATE TABLE feature_observations (
                    id INTEGER PRIMARY KEY, snapshot_date TEXT, source_scanner_run_id INTEGER,
                    ticker TEXT, observed_at_madrid TEXT, observation_layer TEXT,
                    is_canonical_daily_close INTEGER
                );
                CREATE TABLE observation_outcomes (
                    id INTEGER PRIMARY KEY, observation_id INTEGER, observation_date TEXT,
                    complete INTEGER, horizon_sessions INTEGER,
                    mfe_long_pct REAL, mae_long_pct REAL
                );
            """)
            dates = ["2026-09-01", "2026-09-02"]
            records = []
            oid = 0
            for day, run, tickers in [
                (dates[0], 1, ("A", "B", "C", "D")),
                (dates[0], 2, ("B", "C", "D", "E")),
                (dates[1], 3, ("F", "G", "H", "I")),
                (dates[1], 4, ("J", "K")),
            ]:
                for ticker in tickers:
                    oid += 1
                    records.append((oid, day, run, ticker, day + "T16:00:00", "intraday", 0))
            conn.executemany(
                "INSERT INTO feature_observations VALUES (?,?,?,?,?,?,?)", records
            )
            # Mature date via A, F; the other run has no labels.
            conn.executemany("INSERT INTO observation_outcomes VALUES (?,?,?,?,?,?,?)", [
                (1, 1, dates[0], 1, 5, 7., -2.),
                (2, 9, dates[1], 1, 5, 1., -1.),
            ])
            conn.commit()
        finally:
            conn.close()
        original = config.ALERTS_DB
        config.ALERTS_DB = db_path
        try:
            rows, wm, stats = load_observed_rows()
            assert len(rows) == 14
            assert wm["max_observation_id"] == 14
            assert stats[dates[0]] == {1: 4, 2: 4}
            assert stats[dates[1]] == {3: 4, 4: 2}
            result = audit_rows(rows, run_stats=stats)
            assert result["exclusive_ticker_dates_in_other_runs"] == 3
            assert result["unique_excluded_ticker_dates_with_h5_5pct_move"] == 1
            assert result["unique_excluded_ticker_dates_with_complete_h5_labels"] == 1
            assert not result["no_live_mutation"] is False
        finally:
            config.ALERTS_DB = original


def main():
    test_pure_audit()
    test_readonly_db_join()
    replay = (Path(__file__).resolve().parents[1] / "alerts_bot" / "historical_replay_once.py").read_text(encoding="utf-8-sig")
    assert "def _refresh_readonly_scanner_intraday_audit()" in replay
    assert 'reused["scanner_intraday_audit"] = _refresh_readonly_scanner_intraday_audit()' in replay
    assert "                _refresh_readonly_scanner_intraday_audit()" in replay
    audit_source = (Path(__file__).resolve().parents[1] / "alerts_bot" / "scanner_intraday_coverage_audit.py").read_text(encoding="utf-8-sig")
    assert audit_source.count("connect(config.ALERTS_DB, readonly=True, live=True)") == 2, "Must bypass compact snapshot to audit full raw source read-only"
    export = (Path(__file__).resolve().parents[1] / "observability" / "runtime_data_export.py").read_text(encoding="utf-8-sig")
    assert '"latest_scanner_intraday_coverage_audit.json"' in export
    print("PASS | 6.2.67 | Scanner all-run read-only coverage, representative anchor, H5 per-observation labels, no empty-date loss")


if __name__ == "__main__":
    main()
