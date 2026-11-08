from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

from research_snapshot import _sqlite_compact_alerts_snapshot


def _build(path: Path) -> None:
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
                ticker TEXT
            );
            CREATE TABLE observation_outcomes(
                id INTEGER PRIMARY KEY,
                observation_id INTEGER,
                observation_date TEXT,
                complete INTEGER,
                horizon_sessions INTEGER
            );
            CREATE TABLE relation_rows(
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                related TEXT
            );
            CREATE INDEX idx_feature_date_run
                ON feature_observations(snapshot_date,source_scanner_run_id);
            CREATE INDEX idx_relation_ticker ON relation_rows(ticker);
            """
        )
        for idx in range(1, 21):
            con.execute(
                "INSERT INTO feature_observations VALUES(?,?,?,?,?,?)",
                (idx, "daily_close", 1, "2026-10-01", 77, f"T{idx}"),
            )
            con.execute(
                "INSERT INTO observation_outcomes(observation_id,observation_date,complete,horizon_sessions) VALUES(?,?,1,5)",
                (idx, "2026-10-01"),
            )
        for idx in range(1, 201):
            con.execute(
                "INSERT INTO relation_rows(ticker,related) VALUES(?,?)",
                (f"T{idx%20}", f"R{idx%11}"),
            )
        con.commit()
    finally:
        con.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tmad-v6258-") as tmp:
        root = Path(tmp)
        src = root / "source.db"
        routine = root / "routine.db"
        diagnostic = root / "diagnostic.db"
        forensic = root / "forensic.db"
        _build(src)

        progress: list[tuple[str, str]] = []
        normal = _sqlite_compact_alerts_snapshot(
            src,
            routine,
            progress_callback=lambda stage, detail: progress.append((stage, detail)),
        )
        assert normal["verification"]["mode"] == "EXACT_ROW_COUNTS_PLUS_SQLITE_INDEX_BUILD"
        assert normal["verification"]["routine_structural_scan_skipped"] is True
        assert normal["verification"]["structural_check"] == "SKIPPED_ROUTINE"
        assert "__row_count_verification__" in normal["table_seconds"]
        assert "__quick_check__" not in normal["table_seconds"]
        assert "__integrity_check__" not in normal["table_seconds"]
        assert any(
            stage == "structural_check" and "skipped" in detail
            for stage, detail in progress
        )
        assert normal["verification"]["explicit_indexes_created"] >= 2
        assert normal["verification"]["explicit_indexes_built_by_sqlite_from_verified_rows"] is True

        diag = _sqlite_compact_alerts_snapshot(
            src,
            diagnostic,
            quick_check=True,
        )
        assert diag["verification"]["mode"] == "EXACT_ROW_COUNTS_PLUS_OPT_IN_PRE_INDEX_QUICK_CHECK_PLUS_SQLITE_INDEX_BUILD"
        assert diag["verification"]["quick_check"] is True
        assert "__quick_check__" in diag["table_seconds"]

        full = _sqlite_compact_alerts_snapshot(
            src,
            forensic,
            full_integrity_check=True,
        )
        assert full["verification"]["mode"] == "FULL_INTEGRITY_CHECK_POST_INDEX"
        assert full["verification"]["full_integrity_check"] is True
        assert "__integrity_check__" in full["table_seconds"]

        for path in (routine, diagnostic, forensic):
            con = sqlite3.connect(path)
            try:
                assert con.execute("SELECT COUNT(*) FROM relation_rows").fetchone()[0] == 200
                assert str(con.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok"
            finally:
                con.close()

    config_text = (ALERTS / "config.py").read_text(encoding="utf-8-sig")
    snapshot_text = (ALERTS / "research_snapshot.py").read_text(encoding="utf-8-sig")
    assert 'RESEARCH_SNAPSHOT_QUICK_CHECK = _bool(' in config_text
    assert '"RESEARCH_SNAPSHOT_QUICK_CHECK", False' in config_text
    assert '"RESEARCH_SNAPSHOT_FULL_INTEGRITY_CHECK", False' in config_text
    assert "routine_quick_check=false" in snapshot_text
    assert "EXACT_ROW_COUNTS_PLUS_SQLITE_INDEX_BUILD" in snapshot_text

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 58), version

    print(
        "PASS | >=6.2.58 | routine snapshot skips multi-GB structural rescan; "
        "exact rows + SQLite index build remain mandatory; quick/integrity checks are opt-in"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
