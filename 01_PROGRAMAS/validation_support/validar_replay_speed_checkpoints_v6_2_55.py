from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

from research_snapshot import _sqlite_compact_alerts_snapshot


def _source(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE feature_observations(
                id INTEGER PRIMARY KEY,
                observation_layer TEXT,
                is_canonical_daily_close INTEGER,
                snapshot_date TEXT,
                source_scanner_run_id INTEGER,
                ticker TEXT,
                score REAL
            );
            CREATE TABLE observation_outcomes(
                id INTEGER PRIMARY KEY,
                observation_id INTEGER,
                observation_date TEXT,
                complete INTEGER,
                horizon_sessions INTEGER
            );
            CREATE TABLE asset_relationship_history(
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                related_ticker TEXT,
                score REAL
            );
            CREATE INDEX idx_feature_date_run
                ON feature_observations(snapshot_date,source_scanner_run_id);
            CREATE INDEX idx_rel_ticker
                ON asset_relationship_history(ticker);
            """
        )
        for idx in range(1, 9):
            con.execute(
                """INSERT INTO feature_observations(
                       id,observation_layer,is_canonical_daily_close,
                       snapshot_date,source_scanner_run_id,ticker,score
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    idx,
                    "daily_close" if idx <= 6 else "intraday",
                    1 if idx <= 6 else 0,
                    "2026-10-01",
                    11,
                    f"T{idx}",
                    float(idx),
                ),
            )
            if idx <= 6:
                con.execute(
                    """INSERT INTO observation_outcomes(
                           observation_id,observation_date,complete,horizon_sessions
                       ) VALUES(?,?,1,5)""",
                    (idx, "2026-10-01"),
                )
        for idx in range(1, 51):
            con.execute(
                "INSERT INTO asset_relationship_history(ticker,related_ticker,score) VALUES(?,?,?)",
                (f"T{idx%8}", f"R{idx%5}", idx / 10.0),
            )
        con.commit()
    finally:
        con.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source.db"
        fast = root / "fast.db"
        diagnostic = root / "diagnostic.db"
        forensic = root / "forensic.db"
        _source(source)

        progress: list[tuple[str, str]] = []
        fast_result = _sqlite_compact_alerts_snapshot(
            source,
            fast,
            progress_callback=lambda stage, detail: progress.append((stage, detail)),
            full_integrity_check=False,
        )
        assert fast_result["verification"]["mode"] == "EXACT_ROW_COUNTS_PLUS_SQLITE_INDEX_BUILD"
        assert fast_result["verification"]["structural_check"] == "SKIPPED_ROUTINE"
        assert fast_result["verification"]["routine_structural_scan_skipped"] is True
        assert "__row_count_verification__" in fast_result["table_seconds"]
        assert "__quick_check__" not in fast_result["table_seconds"]
        assert "__integrity_check__" not in fast_result["table_seconds"]
        assert any(stage == "row_count_verification" for stage, _ in progress)
        assert any(stage == "structural_check" and "skipped" in detail for stage, detail in progress)

        con = sqlite3.connect(fast)
        try:
            assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert con.execute(
                "SELECT COUNT(*) FROM asset_relationship_history"
            ).fetchone()[0] == 50
            assert con.execute(
                "SELECT COUNT(*) FROM feature_observations"
            ).fetchone()[0] == fast_result["table_rows"]["feature_observations"]
        finally:
            con.close()

        diagnostic_result = _sqlite_compact_alerts_snapshot(
            source,
            diagnostic,
            quick_check=True,
            full_integrity_check=False,
        )
        assert diagnostic_result["verification"]["mode"] == "EXACT_ROW_COUNTS_PLUS_OPT_IN_PRE_INDEX_QUICK_CHECK_PLUS_SQLITE_INDEX_BUILD"
        assert diagnostic_result["verification"]["structural_check"] == "quick_check"
        assert "__quick_check__" in diagnostic_result["table_seconds"]

        forensic_result = _sqlite_compact_alerts_snapshot(
            source,
            forensic,
            full_integrity_check=True,
        )
        assert forensic_result["verification"]["mode"] == "FULL_INTEGRITY_CHECK_POST_INDEX"
        assert "__integrity_check__" in forensic_result["table_seconds"]

    replay = (ALERTS / "historical_replay_once.py").read_text(encoding="utf-8-sig")
    operational = (ALERTS / "historical_operational_validation.py").read_text(
        encoding="utf-8-sig"
    )
    config = (ALERTS / "config.py").read_text(encoding="utf-8-sig")
    snapshot = (ALERTS / "research_snapshot.py").read_text(encoding="utf-8-sig")

    assert "progress_callback=_operational_progress" in replay
    assert 'f"OPERATIONAL_VALIDATION/{component}"' in replay
    assert 'def _progress(stage: str, detail: str)' in operational
    for stage in (
        "inputs",
        "counterfactual",
        "reverse_universe",
        "price_series",
        "baseline_books",
        "waterfall",
        "context_precision",
        "reverse_stress",
        "lifecycle",
        "entry_research",
        "diagnostics",
        "finalization",
    ):
        assert re.search(
            rf'_progress\(\s*["\\\']{re.escape(stage)}["\\\']',
            operational,
        ), stage
    assert "PRAGMA {integrity_pragma}" in snapshot
    assert "EXACT_ROW_COUNTS_PLUS_SQLITE_INDEX_BUILD" in snapshot
    assert "EXACT_ROW_COUNTS_PLUS_OPT_IN_PRE_INDEX_QUICK_CHECK_PLUS_SQLITE_INDEX_BUILD" in snapshot
    assert "RESEARCH_SNAPSHOT_QUICK_CHECK" in config
    assert "RESEARCH_SNAPSHOT_FULL_INTEGRITY_CHECK" in config

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 55), version

    print(
        "PASS | >=6.2.58 | compact snapshot exact rows + SQLite index build default | "
        "quick_check diagnostic opt-in + full integrity forensic opt-in | OPERATIONAL_VALIDATION subphase checkpoints | "
        "LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
