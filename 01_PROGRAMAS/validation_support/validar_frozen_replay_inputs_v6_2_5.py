from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

from research_snapshot import (
    compact_snapshot_summary,
    create_research_snapshot,
    cleanup_research_snapshot,
    frozen_research_snapshot,
    research_db_path,
    research_input_path,
)
import db as tmad_db


def make_db(path: Path, *, source_archive: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE feature_observations(
            id INTEGER PRIMARY KEY,
            snapshot_date TEXT,
            source_archive TEXT
        );
        CREATE TABLE layer_archive_catalog(
            trade_date TEXT PRIMARY KEY,
            zip_path TEXT,
            verified INTEGER
        );
        CREATE TABLE runtime_state(
            state_key TEXT PRIMARY KEY,
            state_value TEXT,
            updated_at_utc TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO feature_observations(id,snapshot_date,source_archive) VALUES(1,'2026-09-01',?)",
        (source_archive,),
    )
    conn.execute("INSERT INTO runtime_state(state_key,state_value) VALUES('entry_mode','shadow')")
    conn.commit()
    conn.close()


def row_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM feature_observations").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tmad-freeze-test-") as tmp:
        base = Path(tmp)
        runtime = base / "04_RUNTIME"
        output = base / "03_DATOS" / "alerts_bot" / "output"
        layer = base / "03_DATOS" / "alerts_bot" / "db" / "archive" / "layers"
        data_archive = base / "03_DATOS" / "data_bot" / "db" / "archive"
        alerts = base / "03_DATOS" / "alerts_bot" / "db" / "live" / "alerts.db"
        scanner = base / "03_DATOS" / "data_bot" / "db" / "live" / "scanner.db"
        gex = base / "03_DATOS" / "data_bot" / "db" / "live" / "gex.db"

        weekly = data_archive / "weekly" / "history.zip"
        weekly.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(weekly, "w") as zf:
            zf.writestr("dummy.txt", "frozen")

        make_db(alerts, source_archive="weekly/history.zip")
        for path in (scanner, gex):
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO t VALUES(1)")
            conn.commit()
            conn.close()

        layer_zip = layer / "2026-09" / "layers_2026-09-01.zip"
        layer_zip.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(layer_zip, "w") as zf:
            zf.writestr("layer.sqlite", b"placeholder")
        conn = sqlite3.connect(alerts)
        conn.execute(
            "INSERT INTO layer_archive_catalog(trade_date,zip_path,verified) VALUES(?,?,1)",
            ("2026-09-01", str(layer_zip)),
        )
        conn.commit()
        conn.close()

        eod = output / "alpha_pnl_attribution" / "eod_selection_journal.jsonl"
        swing = output / "swing_v2" / "swing_v2_prospective_journal.jsonl"
        eod.parent.mkdir(parents=True, exist_ok=True)
        swing.parent.mkdir(parents=True, exist_ok=True)
        eod.write_text('{"key":"before"}\\n', encoding="utf-8")
        swing.write_text('{"snapshot_date":"before"}\\n', encoding="utf-8")

        cfg = SimpleNamespace(
            RUNTIME_ROOT=runtime,
            OUTPUT_DIR=output,
            ALERTS_DB=alerts,
            SCANNER_DB=scanner,
            GEX_DB=gex,
            LAYER_ARCHIVE_DIR=layer,
            DATA_ARCHIVE_DIR=data_archive,
            DATA_INTRADAY_ARCHIVE_DIR=data_archive / "intraday",
        )

        snapshot = create_research_snapshot(cfg)
        try:
            summary = compact_snapshot_summary(snapshot)
            assert summary["sqlite_backups"]["alerts"]["exists"] is True
            assert summary["layer_archives"] == 1
            assert summary["data_archives"] == 1

            # Production advances after T0.
            conn = sqlite3.connect(alerts)
            conn.execute(
                "INSERT INTO feature_observations(id,snapshot_date,source_archive) VALUES(2,'2026-09-02',?)",
                ("weekly/history.zip",),
            )
            conn.commit()
            conn.close()
            with eod.open("a", encoding="utf-8") as handle:
                handle.write('{"key":"after"}\\n')
            newer_layer = layer / "2026-09" / "layers_2026-09-02.zip"
            with ZipFile(newer_layer, "w") as zf:
                zf.writestr("layer.sqlite", b"new")

            # Snapshot keeps T0, while live has moved.
            frozen_alerts = Path(snapshot["db_map"][str(alerts.resolve()).lower()] if str(alerts.resolve()).lower() in snapshot["db_map"] else research_db_path(alerts))
            # activate via public context below for path-resolution assertions.
        finally:
            cleanup_research_snapshot(snapshot)

        tmad_db.config.ALERTS_DB = alerts
        with frozen_research_snapshot(cfg) as active:
            assert row_count(research_db_path(alerts)) == 2  # fresh T0 for this second snapshot
            conn = tmad_db.connect(alerts)
            try:
                assert int(conn.execute("SELECT COUNT(*) FROM feature_observations").fetchone()[0]) == 2
            finally:
                conn.close()
            frozen_eod = research_input_path(eod)
            assert frozen_eod.read_text(encoding="utf-8").count("\\n") == 2
            assert len(list(Path(cfg.LAYER_ARCHIVE_DIR).glob("????-??/layers_*.zip"))) == 1
            # Production can move again and research view stays fixed.
            conn = sqlite3.connect(alerts)
            conn.execute(
                "INSERT INTO feature_observations(id,snapshot_date,source_archive) VALUES(3,'2026-09-03',?)",
                ("weekly/history.zip",),
            )
            conn.commit()
            conn.close()
            with eod.open("a", encoding="utf-8") as handle:
                handle.write('{"key":"later"}\\n')
            assert row_count(research_db_path(alerts)) == 2
            conn = tmad_db.connect(alerts)
            try:
                assert int(conn.execute("SELECT COUNT(*) FROM feature_observations").fetchone()[0]) == 2
            finally:
                conn.close()
            conn = tmad_db.connect(alerts, live=True)
            try:
                assert int(conn.execute("SELECT COUNT(*) FROM feature_observations").fetchone()[0]) == 3
            finally:
                conn.close()
            tmad_db.set_runtime_state("frozen_replay_probe", "LIVE_OUTPUT")
            live_conn = sqlite3.connect(alerts)
            try:
                assert live_conn.execute(
                    "SELECT state_value FROM runtime_state WHERE state_key='frozen_replay_probe'"
                ).fetchone()[0] == "LIVE_OUTPUT"
            finally:
                live_conn.close()
            frozen_conn = tmad_db.connect(alerts)
            try:
                assert frozen_conn.execute(
                    "SELECT 1 FROM runtime_state WHERE state_key='frozen_replay_probe'"
                ).fetchone() is None
            finally:
                frozen_conn.close()
            assert research_input_path(eod).read_text(encoding="utf-8").count("\\n") == 2
            assert compact_snapshot_summary(active)["production_paused"] is False

        assert cfg.LAYER_ARCHIVE_DIR == layer
        assert row_count(alerts) == 3

    replay = (ALERTS / "historical_replay_once.py").read_text(encoding="utf-8")
    assert "frozen_research_snapshot" in replay
    assert '"VALID_FROZEN_INPUTS"' in replay
    assert '"live_inputs_advanced_during_replay"' in replay
    assert "frozen_operational_input_drift" in replay
    assert "frozen_attribution_input_drift" in replay

    alpha = (ALERTS / "alpha_pnl_attribution.py").read_text(encoding="utf-8")
    assert "research_input_path" in alpha
    assert "frozen_replay_no_journal_mutation" in alpha

    db_text = (ALERTS / "db.py").read_text(encoding="utf-8")
    assert "def connect(path: Path, readonly: bool = False, *, live: bool = False)" in db_text
    assert "connect(config.ALERTS_DB, live=True)" in db_text

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(map(int, version.split("."))) >= (6, 2, 5), version
    print("PASS | v6.2.5 | historical replay uses immutable T0 inputs while production continues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())