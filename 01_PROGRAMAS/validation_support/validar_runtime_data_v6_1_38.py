from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROGRAMS = HERE.parent
REPO = PROGRAMS.parent
EXPORTER = PROGRAMS / "observability" / "runtime_data_export.py"
LEGACY_QUALITY = PROGRAMS / "validar_quality_v6_1_37.py"

assert EXPORTER.is_file(), "runtime_data_export.py missing"
spec = importlib.util.spec_from_file_location("tmad_runtime_data_export", EXPORTER)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix="tmad_runtime_data_validation_") as tmp:
    root = Path(tmp)
    production = root / "production"
    data_root = production / "03_DATOS" / "alerts_bot" / "db"
    data_root.mkdir(parents=True)
    (production / "VERSION.txt").write_text("6.1.38\n", encoding="utf-8")
    db_path = data_root / "observability_test.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE model_registry (id INTEGER PRIMARY KEY, created_at TEXT, score REAL, token TEXT)")
        conn.execute("CREATE TABLE gex_raw_payloads (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("CREATE TABLE irrelevant_table (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO model_registry(created_at,score,token) VALUES(?,?,?)",
            ("2026-08-10T00:00:00+02:00", 91.5, "ghp_abcdefghijklmnopqrstuvwxyz123456"),
        )
        conn.execute("INSERT INTO gex_raw_payloads(payload) VALUES(?)", ("x" * 12000,))
        conn.execute("INSERT INTO irrelevant_table(value) VALUES('do-not-export')")
        conn.commit()
    finally:
        conn.close()

    out = root / "runtime_data"
    summary = module.export_runtime_data(production, out, max_rows=5, max_total_mb=5)
    assert summary["version"] == "6.1.38"
    assert summary["database_count"] == 1
    assert summary["exported_table_count"] >= 2
    assert (out / "status.json").is_file()
    assert (out / "catalog.json").is_file()

    catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    tables = catalog["databases"][0]["tables"]
    by_name = {row["name"]: row for row in tables}
    assert by_name["model_registry"]["exported"] is True
    assert by_name["gex_raw_payloads"]["exported"] is True
    assert by_name["irrelevant_table"]["exported"] is False

    model_file = out / by_name["model_registry"]["file"]
    model_text = model_file.read_text(encoding="utf-8")
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in model_text
    assert "[REDACTED_GITHUB_TOKEN]" in model_text

    gex_file = out / by_name["gex_raw_payloads"]["file"]
    gex_payload = json.loads(gex_file.read_text(encoding="utf-8"))
    assert gex_payload["row_limit"] <= 30
    assert "TRUNCATED" in gex_payload["rows"][0]["payload"]

# v6.1.37 regression must be safe in production, where control-only root scripts
# intentionally do not exist.
legacy = LEGACY_QUALITY.read_text(encoding="utf-8-sig")
assert "if sync_path.exists() or watch_path.exists():" in legacy
assert "production POST-check" in legacy

# These assertions apply only in the control repo (_2). Production intentionally
# receives 01_PROGRAMAS + selected root launchers, not the observability sync tools.
sync_data = REPO / "SYNC_RUNTIME_DATA_TMAD.ps1"
watch_logs = REPO / "WATCH_RUNTIME_LOGS_TMAD.ps1"
if sync_data.exists() or watch_logs.exists():
    assert sync_data.is_file(), "SYNC_RUNTIME_DATA_TMAD.ps1 missing from control repo"
    assert watch_logs.is_file(), "WATCH_RUNTIME_LOGS_TMAD.ps1 missing from control repo"
    sync_text = sync_data.read_text(encoding="utf-8-sig")
    watch_text = watch_logs.read_text(encoding="utf-8-sig")
    assert '$Branch = "runtime-data"' in sync_text
    assert 'git push --force origin "HEAD:$Branch"' in sync_text
    assert 'runtime_data_export.py' in sync_text
    assert "MaxRowsPerTable" in sync_text and "MaxTotalMb" in sync_text
    assert "SYNC_RUNTIME_DATA_TMAD.ps1" in watch_text
    assert "DataMinSyncIntervalSeconds = 300" in watch_text
    assert "Publish-RuntimeData" in watch_text

print("PASS | v6.1.38 runtime-data + dual-layout validation")
