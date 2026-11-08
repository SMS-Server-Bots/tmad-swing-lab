from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_contains(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8-sig")
    for needle in needles:
        assert needle in text, f"missing {needle!r} in {path}"


def test_runtime_storage_export(tmp: Path) -> None:
    production = tmp / "prod"
    live = production / "03_DATOS" / "data_bot" / "db" / "live"
    live.mkdir(parents=True)
    (production / "VERSION.txt").write_text("6.1.41\n", encoding="utf-8")
    db = live / "storage_test.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE signal_events(id INTEGER PRIMARY KEY, payload TEXT)")
    con.executemany("INSERT INTO signal_events(payload) VALUES(?)", [("x" * 1000,) for _ in range(200)])
    con.commit()
    con.execute("DELETE FROM signal_events WHERE id <= 120")
    con.commit()
    con.close()

    exporter = load_module("tmad_runtime_data_export_v641", ROOT / "01_PROGRAMAS" / "observability" / "runtime_data_export.py")
    out = tmp / "runtime_data"
    summary = exporter.export_runtime_data(production, out, max_rows=20, max_total_mb=5)
    assert int(summary["schema_version"]) >= 2
    assert (out / "storage.json").is_file()
    storage = json.loads((out / "storage.json").read_text(encoding="utf-8"))
    assert storage["totals"]["database_count"] == 1
    item = storage["databases"][0]
    for key in ("file_bytes", "page_size", "page_count", "freelist_count", "reclaimable_bytes", "reclaimable_pct", "auto_vacuum_mode"):
        assert key in item, key
    assert int(item["file_bytes"]) > 0


def test_extra_field_compaction(tmp: Path) -> None:
    today_date = datetime.now(ZoneInfo("Europe/Madrid")).date()
    previous_date = today_date - timedelta(days=1)
    today = today_date.isoformat()
    previous = previous_date.isoformat()
    data_root = tmp / "data"
    live = data_root / "data_bot" / "db" / "live"
    live.mkdir(parents=True)
    db = live / "tmad_meta_live.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE extra_fields(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        endpoint_type TEXT NOT NULL,
        ticker TEXT,
        run_type TEXT,
        run_id INTEGER,
        field_path TEXT NOT NULL,
        field_value TEXT,
        value_type TEXT,
        payload_hash TEXT,
        unique_key TEXT UNIQUE
    )""")
    rows = [
        (f"{previous}T10:00:00Z", f"{previous}T12:00:00+02:00", previous, "gex", "AAA", "gex", 1, "data.foo", "1", "int", "h1", "k1"),
        (f"{previous}T11:00:00Z", f"{previous}T13:00:00+02:00", previous, "gex", "BBB", "gex", 2, "data.foo", "2", "int", "h2", "k2"),
        (f"{today}T10:00:00Z", f"{today}T12:00:00+02:00", today, "gex", "CCC", "gex", 3, "data.foo", "3", "int", "h3", "k3"),
    ]
    con.executemany("INSERT INTO extra_fields(captured_at_utc,captured_at_madrid,snapshot_date,endpoint_type,ticker,run_type,run_id,field_path,field_value,value_type,payload_hash,unique_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit(); con.close()

    old = os.environ.get("TMAD_DATA_DIR")
    os.environ["TMAD_DATA_DIR"] = str(data_root)
    try:
        maintenance = load_module("tmad_data_maintenance_v641", ROOT / "01_PROGRAMAS" / "data_bot" / "maintenance_v620.py")
        result = maintenance.compact_extra_fields(today)
    finally:
        if old is None:
            os.environ.pop("TMAD_DATA_DIR", None)
        else:
            os.environ["TMAD_DATA_DIR"] = old
    assert result["rows_compacted"] == 2, result
    assert result["daily_groups"] == 1, result
    con = sqlite3.connect(db)
    remaining = int(con.execute("SELECT COUNT(*) FROM extra_fields").fetchone()[0])
    summary = con.execute("SELECT observations,distinct_tickers FROM extra_field_daily_summary WHERE snapshot_date=?", (previous,)).fetchone()
    con.close()
    assert remaining == 1
    assert summary == (2, 2), summary


def test_wiring_and_safety() -> None:
    # 01_PROGRAMAS exists in both control (_2) and production and must always be checked.
    assert_contains(
        ROOT / "01_PROGRAMAS" / "data_bot" / "maintenance_v620.py",
        "extra_field_daily_summary",
        "EXTRA_FIELD_SUMMARY_RETENTION_DAYS",
        "SQLITE_WEEKEND_VACUUM_ENABLED",
        "SQLITE_WEEKEND_VACUUM_MIN_FREE_MB",
        "SQLITE_WEEKEND_VACUUM_MIN_FREE_PCT",
    )
    assert_contains(
        ROOT / "01_PROGRAMAS" / "alerts_bot" / "maintenance_v620.py",
        "SQLITE_WEEKEND_VACUUM_ENABLED",
        "reclaimable_before",
        "weekend",
    )

    # Root observability/install scripts are control-only by deployment design.
    # Validate them when any is present, but never make production POST-checks
    # depend on files that DESPLEGAR_TMAD.ps1 intentionally does not mirror.
    control_only = [
        ROOT / "SYNC_RUNTIME_LOGS_TMAD.ps1",
        ROOT / "RUN_TMAD_STORAGE_MAINTENANCE.ps1",
        ROOT / "INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1",
        ROOT / ".github" / "workflows" / "sync-tmad-version.yml",
    ]
    if any(path.exists() for path in control_only):
        assert all(path.is_file() for path in control_only), "partial control-only storage infrastructure"
        assert_contains(
            control_only[0],
            'schema_version = 2',
            '$classification = "CURRENT"',
            '"HISTORICAL"',
            'alerts_error = $alertsError',
            'data_error = $dataError',
        )
        assert_contains(
            control_only[1],
            "$madridNow.Hour -lt 3",
            "$madridNow.Hour -ge 6",
            "storage_maintenance_last_success.txt",
        )
        assert_contains(
            control_only[2],
            "Trumppetta_TMAD_Storage_Maintenance",
            "RUN_TMAD_STORAGE_MAINTENANCE.ps1",
        )
        assert_contains(
            control_only[3],
            "Create exact version tag",
            "validate-tmad.sh",
        )
        assert_contains(
            ROOT / ".github" / "scripts" / "validate-tmad.sh",
            "validar_v6_1_41.py",
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tmad_v641_") as td:
        tmp = Path(td)
        test_runtime_storage_export(tmp / "export")
        test_extra_field_compaction(tmp / "compact")
    test_wiring_and_safety()
    print("PASS | v6.1.41+ | storage observability + dual-layout safe maintenance + error freshness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
