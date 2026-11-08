from __future__ import annotations

"""TMAD 6.2.65 offline residual history and validation-gating audit."""
from datetime import date, timedelta
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alerts_bot"))
import auto_joint_stress as auto
import research_ohlc_gap_triage as triage


def sample(start: date, n: int = 15):
    rows = []
    day = start
    while len(rows) < n:
        if day.weekday() < 5:
            p = 100.0 + len(rows) / 10.0
            rows.append({
                "trade_date": day.isoformat(), "open": p, "high": p + 1,
                "low": p - 1, "close": p,
                "split_factor": 1.0, "source": triage.ALT_SOURCE,
            })
        day += timedelta(days=1)
    return rows


def main():
    assert triage._alt_symbol("AAPL") == "aapl.us"
    assert triage._alt_symbol("^SPX") is None
    assert triage._classify("SPLIT_EVENT_IN_REQUESTED_WINDOW", "") == "CORPORATE_ACTION_OR_PRICE_DISCONTINUITY"
    assert triage._classify("EXISTING_TRUSTED_CLOSE_CONFLICT", "") == "PRICE_BASIS_CONFLICT"
    assert triage._fetch_stooq("AAPL", date(2026, 6, 2), date(2026, 6, 30), "")[1] == "ALT_KEY_NOT_CONFIGURED"

    signal = "2026-06-01"
    dates = [signal]
    bars = sample(date(2026, 6, 2), 15)
    original = {("AAPL", signal): bars[:2]}
    def independent(ticker, begin, end, key):
        assert ticker == "AAPL" and key == "TEST"
        return [row for row in bars if begin.isoformat() <= row["trade_date"] <= end.isoformat()], None
    review = triage._audit_alt("AAPL", dates, original, "TEST", independent, date(2026, 10, 11))
    assert review["complete_alternate_paths"] == 1, review
    assert review["cross_source_overlap_sessions"] >= 2, review
    assert review["status"] == "MANUAL_REVIEW_CANDIDATE", review
    unknown = triage._audit_alt("AAPL", dates, {("AAPL", signal): []}, "TEST", independent, date(2026, 10, 11))
    assert unknown["status"] == "UNVERIFIED_PRICE_BASIS", unknown
    biased = {("AAPL", signal): [{**row, "close": row["close"] * 2,
                                  "open": row["open"] * 2,
                                  "low": row["low"] * 2,
                                  "high": row["high"] * 2} for row in bars[:2]]}
    mismatch = triage._audit_alt("AAPL", dates, biased, "TEST", independent, date(2026, 10, 11))
    assert mismatch["status"] == "ALTERNATE_PRICE_BASIS_CONFLICT", mismatch

    stress = {
        "generated_at_madrid": "2026-10-11T23:11:35+02:00",
        "methodology": {"research_ohlc_revision": "rev123"},
        "train_frozen_shortlist": [{"id": "TRAIN_LOCKED_1"}],
        "validation": {
            lane: {"specs": {"TRAIN_LOCKED_1": {
                "evaluable": n, "dates": d, "net_avg_pct": avg,
                "net_profit_factor": pf, "explicit_sector_etf_trades": 0,
            }}}
            for lane, n, d, avg, pf in [
                ("ROLLING_LATEST", 15, 7, 2.12, 1.83),
                ("FIXED_BENCHMARK", 10, 3, 3.55, 4.72),
            ]
        },
    }
    r = triage._readiness(stress, "rev123")
    assert r["train_frozen_shortlist_size"] == 1
    assert r["joint_report_revision_matches"] is True
    assert r["candidates"][0]["eligible_to_promote"] is False
    assert "ROLLING_LATEST_TRADES_BELOW_20" in r["candidates"][0]["issues"]
    assert "FIXED_BENCHMARK_DATES_BELOW_8" in r["candidates"][0]["issues"]
    assert "PRICE_REVISION_MISMATCH" in triage._readiness(stress, "other")["candidates"][0]["issues"]

    old_required, old_loader = triage._required_keys, triage._load_db_price_paths
    old_db, old_stress, old_backfill = triage.RESEARCH_DB, triage.STRESS_REPORT, triage.BACKFILL_REPORT
    old_source = auto.SOURCE
    try:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            triage.RESEARCH_DB = root / "not-existing-research.db"
            triage._required_keys = lambda: (
                {"AAPL": dates}, {
                    "signal_keys": 1, "cache": {
                        "ROLLING_LATEST": {"fingerprint": "R"},
                        "FIXED_BENCHMARK": {"fingerprint": "F"},
                    },
                },
            )
            triage._load_db_price_paths = lambda rows: (original, {"status": "OK"})
            triage.STRESS_REPORT = root / "stress.json"
            triage.STRESS_REPORT.write_text(json.dumps(stress), encoding="utf-8")
            triage.BACKFILL_REPORT = root / "backfill.json"
            triage.BACKFILL_REPORT.write_text(json.dumps({"outcomes": {"revision": "rev123"}}), encoding="utf-8")
            triage_no_key = triage.run(max_secondary_tickers=0, api_key="")
            assert triage_no_key["residual"]["missing_10session_signal_keys"] == 1, triage_no_key
            assert triage_no_key["residual"]["affected_tickers"] == 1, triage_no_key
            assert triage_no_key["secondary_source"]["status"] == "NO_KEY_DIAGNOSTIC_ONLY"
            assert triage_no_key["economic_validation"]["train_frozen_shortlist_size"] == 1

            with_alternate = triage.run(max_secondary_tickers=1, pause_seconds=0.0,
                                         alternate_fetcher=independent, api_key="TEST")
            assert with_alternate["secondary_source"]["cases"]["AAPL"]["status"] == "MANUAL_REVIEW_CANDIDATE"
            assert with_alternate["canonical_price_mutation"] is False
            assert with_alternate["research_ohlc_mutation"] is False
            assert with_alternate["economic_validation"]["final_test"] == "LOCKED"

            auto.SOURCE = root
            pair = {"ROLLING_LATEST": "R", "FIXED_BENCHMARK": "F"}
            last = {"gaps": {"keys": 88}, "outcomes": {
                "gaps_after_keys": 88, "revision": "abc",
                "newly_unattempted_queue_remaining": 0,
            }, "required": {"cache": {
                "ROLLING_LATEST": {"fingerprint": "R"},
                "FIXED_BENCHMARK": {"fingerprint": "F"},
            }}}
            (root / "latest_research_ohlc_backfill.json").write_text(json.dumps(last))
            assert auto._backfill_saturated(pair) is True
            last["outcomes"]["gaps_after_keys"] = 40
            (root / "latest_research_ohlc_backfill.json").write_text(json.dumps(last))
            assert auto._backfill_saturated(pair) is False
            last["outcomes"]["gaps_after_keys"] = 88
            last["required"]["cache"]["FIXED_BENCHMARK"]["fingerprint"] = "OLD"
            (root / "latest_research_ohlc_backfill.json").write_text(json.dumps(last))
            assert auto._backfill_saturated(pair) is False
    finally:
        triage._required_keys, triage._load_db_price_paths = old_required, old_loader
        triage.RESEARCH_DB, triage.STRESS_REPORT, triage.BACKFILL_REPORT = old_db, old_stress, old_backfill
        auto.SOURCE = old_source

    script = (ROOT / "alerts_bot" / "auto_joint_stress.py").read_text(encoding="utf-8")
    exporter = (ROOT / "observability" / "runtime_data_export.py").read_text(encoding="utf-8")
    deploy = (ROOT.parent / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8")
    assert "research_ohlc_gap_triage.py" in script
    assert "latest_research_ohlc_gap_triage.json" in exporter
    assert "if ($version -eq '6.2.65' -and -not $NoStart)" in deploy
    assert "Replay FULL omitido en 6.2.65" in deploy
    print("PASS TMAD 6.2.65: failed OHLC triage, opt-in secondary basis audit, evidence gates, no automatic history or LIVE mutations")


if __name__ == "__main__":
    main()
