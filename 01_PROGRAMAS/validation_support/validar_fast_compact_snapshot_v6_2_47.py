from __future__ import annotations

from pathlib import Path
import importlib.util
import sqlite3
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def _load():
    sys.path.insert(0,str(ALERTS))
    path=ALERTS/"research_snapshot.py"
    spec=importlib.util.spec_from_file_location("research_snapshot_v647",path)
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_source(path: Path) -> None:
    con=sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE feature_observations(
            id INTEGER PRIMARY KEY,
            snapshot_date TEXT NOT NULL,
            source_scanner_run_id INTEGER NOT NULL,
            observation_layer TEXT,
            is_canonical_daily_close INTEGER DEFAULT 0,
            features_json TEXT,
            unique_key TEXT UNIQUE
        );
        CREATE INDEX idx_feature_run
        ON feature_observations(snapshot_date,source_scanner_run_id);

        CREATE TABLE observation_outcomes(
            id INTEGER PRIMARY KEY,
            observation_id INTEGER NOT NULL,
            observation_date TEXT NOT NULL,
            horizon_sessions INTEGER NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_outcome_obs ON observation_outcomes(observation_id);

        CREATE TABLE runtime_state(
            state_key TEXT PRIMARY KEY,
            state_value TEXT
        );
        CREATE TABLE model_versions(
            id INTEGER PRIMARY KEY,
            model_key TEXT,
            status TEXT
        );
        """)

        rows=[
            *[(i,"2026-09-01",10,"intraday",0,'{"x":1}',f"r10-{i}") for i in range(1,11)],
            *[(100+i,"2026-09-01",11,"intraday",0,'{"x":2}',f"r11-{i}") for i in range(1,7)],
            (201,"2026-09-02",20,"daily_close",1,'{"daily":1}',"daily"),
            (202,"2026-09-03",30,"core_history",0,'{"core":1}',"core"),
            (203,"2026-09-04",40,"intraday",0,'{"noise":1}',"noise"),
            (204,"2026-09-05",50,"intraday",0,'{"labeled":1}',"labeled"),
        ]
        con.executemany("INSERT INTO feature_observations VALUES(?,?,?,?,?,?,?)",rows)
        con.executemany(
            "INSERT INTO observation_outcomes VALUES(?,?,?,?,?)",
            [
                (1,1,"2026-09-01",5,1),
                (2,204,"2026-09-05",2,1),
            ],
        )
        con.execute("INSERT INTO runtime_state VALUES('k','v')")
        con.execute("INSERT INTO model_versions VALUES(7,'m7','active')")
        con.commit()
    finally:
        con.close()


def _legacy_expected_ids(module, source: Path) -> set[int]:
    con=sqlite3.connect(source)
    try:
        query,policy=module._compact_feature_query(con)
        assert policy=="RESEARCH_RELEVANT_FEATURE_ROWS_V2"
        return {int(row[0]) for row in con.execute(
            f"SELECT id FROM ({query}) ORDER BY id"
        ).fetchall()}
    finally:
        con.close()


def main()->int:
    m=_load()
    with tempfile.TemporaryDirectory(prefix="tmad-v647-") as td:
        root=Path(td)
        src=root/"source.db"
        dst=root/"snapshot.db"
        _build_source(src)
        expected=_legacy_expected_ids(m,src)

        # The new fast path must never fall back to Python row marshaling.
        def _forbidden(*args,**kwargs):
            raise AssertionError("_copy_query_rows must not be used by v6.2.47 Alerts snapshot")
        m._copy_query_rows=_forbidden

        wrote_after_t0={"done":False}
        def progress(stage: str, detail: str)->None:
            # The source transaction has already been anchored before table-copy
            # callbacks begin. A concurrent WAL write must not leak into T0.
            if stage=="feature_observations" and detail=="running" and not wrote_after_t0["done"]:
                writer=sqlite3.connect(src,timeout=30)
                try:
                    writer.execute(
                        "INSERT INTO feature_observations VALUES(?,?,?,?,?,?,?)",
                        (999,"2026-09-06",60,"daily_close",1,'{"after_t0":1}',"after-t0"),
                    )
                    writer.commit()
                    wrote_after_t0["done"]=True
                finally:
                    writer.close()

        meta=m._sqlite_compact_alerts_snapshot(src,dst,progress_callback=progress)
        assert wrote_after_t0["done"] is True
        assert meta["mode"]=="SQLITE_COMPACT_RESEARCH_COPY_V2_ATTACH_BULK"
        assert meta["feature_policy"]=="RESEARCH_RELEVANT_FEATURE_ROWS_V2"
        assert meta["feature_diagnostics"]["chosen_runs"]==1
        assert meta["feature_diagnostics"]["outcome_ids"]==2
        assert meta["table_rows"]["runtime_state"]==1
        assert meta["table_rows"]["model_versions"]==1
        assert "feature_observations" in meta["table_seconds"]
        assert "__indexes__" in meta["table_seconds"]
        # >=6.2.58 routine replay verification is exact persisted row counts
        # plus SQLite index reconstruction. Whole-file structural scans are
        # diagnostic/forensic opt-ins because the snapshot is disposable.
        assert "__row_count_verification__" in meta["table_seconds"]
        assert "__quick_check__" not in meta["table_seconds"]
        assert "__integrity_check__" not in meta["table_seconds"]
        assert meta["verification"]["mode"]=="EXACT_ROW_COUNTS_PLUS_SQLITE_INDEX_BUILD"
        assert meta["verification"]["routine_structural_scan_skipped"] is True

        snap=sqlite3.connect(dst)
        try:
            actual={int(r[0]) for r in snap.execute(
                "SELECT id FROM feature_observations ORDER BY id"
            ).fetchall()}
            assert actual==expected,(sorted(actual),sorted(expected))
            assert 999 not in actual
            assert snap.execute(
                "SELECT state_value FROM runtime_state WHERE state_key='k'"
            ).fetchone()[0]=="v"
            assert snap.execute(
                "SELECT model_key FROM model_versions WHERE id=7"
            ).fetchone()[0]=="m7"
            indexes={r[1] for r in snap.execute(
                "PRAGMA index_list(feature_observations)"
            ).fetchall()}
            assert "idx_feature_run" in indexes
            assert str(snap.execute("PRAGMA integrity_check").fetchone()[0]).lower()=="ok"
        finally:
            snap.close()

        # The live DB did advance after T0; only the frozen snapshot excludes it.
        live=sqlite3.connect(src)
        try:
            assert live.execute(
                "SELECT COUNT(*) FROM feature_observations WHERE id=999"
            ).fetchone()[0]==1
        finally:
            live.close()

    source=(ALERTS/"research_snapshot.py").read_text(encoding="utf-8-sig")
    assert "ATTACH DATABASE ? AS srcdb" in source
    assert "INSERT INTO main.{qtable} SELECT * FROM srcdb.{qtable}" in source
    assert "CREATE TEMP TABLE _tmad_chosen_runs" in source
    assert "CREATE TEMP TABLE _tmad_outcome_ids" in source
    assert "RESEARCH_RELEVANT_FEATURE_ROWS_V2" in source
    assert "SQLITE_COMPACT_RESEARCH_COPY_V2_ATTACH_BULK" in source
    assert 'f"sqlite_alerts/{component}"' in source

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,47),version

    print(
        "PASS | >=6.2.47 | Alerts T0 bulk copy matches legacy row policy exactly, "
        "preserves schema/indexes and excludes concurrent post-T0 WAL writes"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
