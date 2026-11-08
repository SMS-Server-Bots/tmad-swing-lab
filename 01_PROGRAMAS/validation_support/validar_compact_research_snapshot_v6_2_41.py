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
    spec=importlib.util.spec_from_file_location("research_snapshot_v641",path)
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_source(path: Path) -> None:
    con=sqlite3.connect(path)
    try:
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
        CREATE INDEX idx_feature_run ON feature_observations(snapshot_date,source_scanner_run_id);

        CREATE TABLE observation_outcomes(
            id INTEGER PRIMARY KEY,
            observation_id INTEGER NOT NULL,
            observation_date TEXT NOT NULL,
            horizon_sessions INTEGER NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE runtime_state(
            state_key TEXT PRIMARY KEY,
            state_value TEXT
        );
        """)

        rows=[
            # Mature date 2026-09-01:
            # run 10 has 10 rows, run 11 has 6 rows => both >=50%; latest 11 wins.
            *[(i,"2026-09-01",10,"intraday",0,'{"x":1}',f"r10-{i}") for i in range(1,11)],
            *[(100+i,"2026-09-01",11,"intraday",0,'{"x":2}',f"r11-{i}") for i in range(1,7)],
            # daily/core rows must always survive regardless of run selection.
            (201,"2026-09-02",20,"daily_close",1,'{"daily":1}',"daily"),
            (202,"2026-09-03",30,"core_history",0,'{"core":1}',"core"),
            # unlabeled ephemeral intraday must disappear.
            (203,"2026-09-04",40,"intraday",0,'{"noise":1}',"noise"),
            # outcome-linked intraday survives even outside chosen run.
            (204,"2026-09-05",50,"intraday",0,'{"labeled":1}',"labeled"),
        ]
        con.executemany(
            "INSERT INTO feature_observations VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        con.executemany(
            "INSERT INTO observation_outcomes VALUES(?,?,?,?,?)",
            [
                # Makes 2026-09-01 a mature H5 date.
                (1,1,"2026-09-01",5,1),
                # Keeps observation 204 by direct outcome reference.
                (2,204,"2026-09-05",2,1),
            ],
        )
        con.execute("INSERT INTO runtime_state VALUES('k','v')")
        con.commit()
    finally:
        con.close()


def main()->int:
    m=_load()
    with tempfile.TemporaryDirectory(prefix="tmad-v641-") as td:
        root=Path(td)
        src=root/"source.db"
        dst=root/"snapshot.db"
        _build_source(src)

        meta=m._sqlite_compact_alerts_snapshot(src,dst)
        version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
        version_tuple=tuple(int(x) for x in version.split("."))
        if version_tuple>=(6,2,47):
            assert meta["mode"]=="SQLITE_COMPACT_RESEARCH_COPY_V2_ATTACH_BULK"
        else:
            assert meta["mode"]=="SQLITE_COMPACT_RESEARCH_COPY_V1"
        assert meta["feature_policy"]=="RESEARCH_RELEVANT_FEATURE_ROWS_V2"
        assert meta["table_rows"]["runtime_state"]==1

        con=sqlite3.connect(dst)
        try:
            ids={int(r[0]) for r in con.execute(
                "SELECT id FROM feature_observations ORDER BY id"
            ).fetchall()}
            # Selected mature operational run 11 survives in full.
            for obs_id in range(101,107):
                assert obs_id in ids
            # Older eligible run 10 is excluded except id=1, which is
            # independently preserved because an outcome references it.
            assert 1 in ids
            for obs_id in range(2,11):
                assert obs_id not in ids
            assert 201 in ids
            assert 202 in ids
            assert 204 in ids
            assert 203 not in ids

            integrity=con.execute("PRAGMA integrity_check").fetchone()[0]
            assert str(integrity).lower()=="ok"
            idx={r[1] for r in con.execute("PRAGMA index_list(feature_observations)").fetchall()}
            assert "idx_feature_run" in idx
            assert con.execute("SELECT state_value FROM runtime_state WHERE state_key='k'").fetchone()[0]=="v"
        finally:
            con.close()

    source=(ALERTS/"research_snapshot.py").read_text(encoding="utf-8-sig")
    if version_tuple>=(6,2,47):
        assert "_sqlite_compact_alerts_snapshot(" in source
        assert "progress_callback=lambda component, detail" in source
    else:
        assert "_sqlite_compact_alerts_snapshot(source, destination)" in source
    assert 'if label == "alerts"' in source
    if version_tuple>=(6,2,47):
        assert "backup = _sqlite_backup(source, destination)" in source
        assert 'if label == "alerts":' in source
    else:
        assert "else _sqlite_backup(source, destination)" in source
    assert "RESEARCH_RELEVANT_FEATURE_ROWS_V2" in source
    assert "observation_layer IN ('daily_close','core_history')" in source
    assert "rc.n*2>=mc.max_n" in source
    if version_tuple>=(6,2,47):
        assert "SQLITE_COMPACT_RESEARCH_COPY_V2_ATTACH_BULK" in source
    else:
        assert "SQLITE_COMPACT_RESEARCH_COPY_V1" in source

    assert version_tuple>=(6,2,41),version

    print(
        "PASS | >=6.2.41 | compact Alerts T0 preserves outcomes + daily/core + "
        "exact mature operational runs | ephemeral unlabeled intraday excluded"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
