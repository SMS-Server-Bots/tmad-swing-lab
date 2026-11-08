from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

DB_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
SKIP_DIRS = {"archive", "logs", "temp", "tmp", "__pycache__", "backups", "backup"}
RELEVANT_TOKENS = {
    "model", "signal", "position", "outcome", "audit", "learn", "calibrat",
    "research", "alert", "setup", "run", "scanner", "gex", "coord", "state",
    "event", "lifecycle", "prediction", "feature", "candidate", "trade", "market",
    "regime", "fusion", "timing", "shadow", "live", "score", "metric",
}
HEAVY_TOKENS = {"raw", "payload", "strike", "expiration", "snapshot", "history", "feature"}
ORDER_COLUMNS = (
    "id", "run_id", "event_id", "signal_id", "created_at", "updated_at", "timestamp",
    "ts", "datetime", "date", "trade_date", "snapshot_date", "session_date", "generated_at",
)
SECRET_PATTERNS = (
    (re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[^\s\"']+", re.I), "[REDACTED_DISCORD_WEBHOOK]"),
    (re.compile(r"\b(?:gho_|ghp_|github_pat_)[A-Za-z0-9_\-]+\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]+\b"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)\b(?:authorization|token|secret|password|cookie|api[_-]?key)\b\s*[:=]\s*[^,;\s]+"), "[REDACTED_SECRET]"),
)
MAX_STRING_CHARS = 8000
RESEARCH_SIDECAR_MAX_FILE_BYTES = 3 * 1024 * 1024
RESEARCH_SIDECAR_MAX_TOTAL_BYTES = 12 * 1024 * 1024
RESEARCH_SIDECARS = (
    "latest_historical_validation_summary.json",
    "latest_historical_replay_performance.json",
    "latest_entry_eligibility_dual_lane_decision.json",
    "latest_economic_waterfall_calibration.json",
    "latest_economic_architecture_challengers.json",
    "latest_scanner_recall_ranking_calibration.json",
    "latest_scanner_core_interaction_simulation.json",
    "latest_scanner_direction_aware_ranking.json",
    "latest_core_context_precision_optimizer.json",
    "latest_autonomous_research_summary.json",
    "latest_fast_research_cache_status.json",
    "latest_fast_autonomous_research.json",
    "latest_adaptive_research_cycle.json",
    "latest_gen4_failure_research.json",
    "latest_selection_direction_factorial.json",
    "latest_swing_exit_policy_comparison.json",
    "latest_full_universe_calibration.json",
    "latest_scanner_intraday_coverage_audit.json",
    "latest_joint_entry_exit_stress.json",
    "latest_research_ohlc_backfill.json",
    "latest_research_ohlc_gap_triage.json",
    "latest_gex_gate_calibration.json",
    "latest_fusion_gate_calibration.json",
    "latest_momentum_direction_audit.json",
    "latest_momentum_edge_attrition.json",
    "latest_thesis_quality_completion_walkforward.json",
    "latest_prospective_architecture_journal.json",
)



def madrid_now_iso() -> str:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds")
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_string(value: str) -> str:
    text = value
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_STRING_CHARS:
        text = text[:MAX_STRING_CHARS] + f"…[TRUNCATED {len(text) - MAX_STRING_CHARS} chars]"
    return text


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        preview = value[:256].hex()
        suffix = "" if len(value) <= 256 else f"…[TRUNCATED {len(value) - 256} bytes]"
        return f"hex:{preview}{suffix}"
    if isinstance(value, str):
        return sanitize_string(value)
    return sanitize_string(str(value))


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_is_relevant(name: str) -> bool:
    lower = name.lower()
    if lower == "prospective_architecture_journal":
        return True
    return any(token in lower for token in RELEVANT_TOKENS)


def table_row_limit(name: str, default_rows: int) -> int:
    lower = name.lower()
    if any(token in lower for token in HEAVY_TOKENS):
        return min(default_rows, 30)
    return default_rows


def choose_order_column(columns: list[str]) -> str | None:
    by_lower = {c.lower(): c for c in columns}
    for candidate in ORDER_COLUMNS:
        if candidate in by_lower:
            return by_lower[candidate]
    return None


def db_slug(relative_path: Path) -> str:
    raw = relative_path.as_posix().replace("/", "__")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._")
    return slug[:180] or "database"


def write_json(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")
    return path.stat().st_size


def _pragma_int(connection: sqlite3.Connection, name: str, default: int = 0) -> int:
    try:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        return int(row[0] or 0) if row else int(default)
    except Exception:
        return int(default)


def _dbstat_objects(connection: sqlite3.Connection, limit: int = 40) -> tuple[bool, list[dict[str, Any]], str | None]:
    """Return SQLite on-disk object sizes when the optional dbstat virtual table exists."""
    try:
        rows = connection.execute(
            "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages "
            "FROM dbstat GROUP BY name ORDER BY bytes DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return True, [
            {"name": str(row[0]), "bytes": int(row[1] or 0), "pages": int(row[2] or 0)}
            for row in rows
        ], None
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"


def database_storage_metrics(db_path: Path, connection: sqlite3.Connection) -> dict[str, Any]:
    """Read-only physical/allocated/reclaimable SQLite metrics.

    `reclaimable_bytes` is free pages already inside the database file. It is not
    data loss: SQLite may reuse those pages immediately, while VACUUM can return
    them to the filesystem during a separate off-hours maintenance window.
    """
    page_size = _pragma_int(connection, "page_size", 4096)
    page_count = _pragma_int(connection, "page_count")
    freelist_count = _pragma_int(connection, "freelist_count")
    auto_vacuum = _pragma_int(connection, "auto_vacuum")
    allocated_bytes = max(0, page_size * page_count)
    reclaimable_bytes = max(0, page_size * freelist_count)
    estimated_used_bytes = max(0, allocated_bytes - reclaimable_bytes)
    reclaimable_pct = round((reclaimable_bytes / allocated_bytes * 100.0), 3) if allocated_bytes else 0.0
    wal_path = Path(str(db_path) + "-wal")
    shm_path = Path(str(db_path) + "-shm")
    dbstat_available, objects, dbstat_error = _dbstat_objects(connection)
    return {
        "file_bytes": int(db_path.stat().st_size),
        "wal_bytes": int(wal_path.stat().st_size) if wal_path.exists() else 0,
        "shm_bytes": int(shm_path.stat().st_size) if shm_path.exists() else 0,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "allocated_bytes": allocated_bytes,
        "estimated_used_bytes": estimated_used_bytes,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_mb": round(reclaimable_bytes / (1024 * 1024), 3),
        "reclaimable_pct": reclaimable_pct,
        "auto_vacuum": auto_vacuum,
        "auto_vacuum_mode": {0: "NONE", 1: "FULL", 2: "INCREMENTAL"}.get(auto_vacuum, f"UNKNOWN_{auto_vacuum}"),
        "dbstat_available": dbstat_available,
        "dbstat_error": dbstat_error,
        "largest_objects": objects,
    }


def export_database(db_path: Path, data_root: Path, output_root: Path, max_rows: int, remaining_bytes: int) -> tuple[dict, int]:
    relative = db_path.relative_to(data_root)
    catalog: dict[str, Any] = {
        "path": relative.as_posix(),
        "bytes": db_path.stat().st_size,
        "tables": [],
        "errors": [],
    }
    bytes_written = 0
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=8)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=8000")
        catalog["storage"] = database_storage_metrics(db_path, connection)
        objects = connection.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        slug = db_slug(relative)
        for obj in objects:
            name = str(obj["name"])
            obj_type = str(obj["type"])
            table_meta: dict[str, Any] = {"name": name, "type": obj_type, "exported": False}
            try:
                pragma_rows = connection.execute(f"PRAGMA table_info({quote_ident(name)})").fetchall()
                columns = [str(row["name"]) for row in pragma_rows]
                table_meta["columns"] = columns
                table_meta["order_column"] = choose_order_column(columns)
                relevant = table_is_relevant(name)
                table_meta["relevant"] = relevant
                if not relevant or remaining_bytes - bytes_written <= 0:
                    catalog["tables"].append(table_meta)
                    continue

                limit = table_row_limit(name, max_rows)
                order_column = table_meta["order_column"]
                sql = f"SELECT * FROM {quote_ident(name)}"
                if order_column:
                    sql += f" ORDER BY {quote_ident(order_column)} DESC"
                elif obj_type == "table":
                    sql += " ORDER BY rowid DESC"
                sql += " LIMIT ?"
                try:
                    rows = connection.execute(sql, (limit,)).fetchall()
                except sqlite3.OperationalError:
                    rows = connection.execute(f"SELECT * FROM {quote_ident(name)} LIMIT ?", (limit,)).fetchall()

                payload_rows = [
                    {column: json_safe(row[column]) for column in row.keys()}
                    for row in rows
                ]
                payload = {
                    "database": relative.as_posix(),
                    "table": name,
                    "generated_at_madrid": madrid_now_iso(),
                    "order_column": order_column,
                    "newest_first": bool(order_column or obj_type == "table"),
                    "row_limit": limit,
                    "row_count_exported": len(payload_rows),
                    "rows": payload_rows,
                }
                out_path = output_root / "tables" / slug / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}.json"
                estimated = len(json.dumps(payload, ensure_ascii=False))
                if estimated > max(0, remaining_bytes - bytes_written):
                    table_meta["skipped_reason"] = "global_export_size_cap"
                else:
                    size = write_json(out_path, payload)
                    bytes_written += size
                    table_meta.update({"exported": True, "rows": len(payload_rows), "file": out_path.relative_to(output_root).as_posix(), "bytes": size})
            except Exception as exc:
                table_meta["error"] = f"{type(exc).__name__}: {exc}"
                catalog["errors"].append({"table": name, "error": table_meta["error"]})
            catalog["tables"].append(table_meta)
    finally:
        connection.close()
    return catalog, bytes_written


def build_storage_summary(catalogs: list[dict[str, Any]], version: str) -> dict[str, Any]:
    databases: list[dict[str, Any]] = []
    top_objects: list[dict[str, Any]] = []
    for db in catalogs:
        storage = dict(db.get("storage") or {})
        item = {"path": db.get("path"), **storage}
        databases.append(item)
        for obj in storage.get("largest_objects") or []:
            top_objects.append({"database": db.get("path"), **obj})
    top_objects.sort(key=lambda row: int(row.get("bytes") or 0), reverse=True)
    total_file = sum(int(db.get("file_bytes") or 0) for db in databases)
    total_wal = sum(int(db.get("wal_bytes") or 0) for db in databases)
    total_reclaimable = sum(int(db.get("reclaimable_bytes") or 0) for db in databases)
    total_allocated = sum(int(db.get("allocated_bytes") or 0) for db in databases)
    return {
        "schema_version": 1,
        "bot": "tmad-dual-bot",
        "generated_at_madrid": madrid_now_iso(),
        "version": version,
        "scope": "03_DATOS live SQLite files only; read-only measurement",
        "totals": {
            "database_count": len(databases),
            "file_bytes": total_file,
            "wal_bytes": total_wal,
            "allocated_bytes": total_allocated,
            "reclaimable_bytes": total_reclaimable,
            "reclaimable_mb": round(total_reclaimable / (1024 * 1024), 3),
            "reclaimable_pct": round((total_reclaimable / total_allocated * 100.0), 3) if total_allocated else 0.0,
        },
        "databases": databases,
        "largest_objects": top_objects[:80],
        "notes": [
            "freelist/reclaimable pages are reusable by SQLite even before VACUUM.",
            "dbstat object sizes are included only when the SQLite build exposes dbstat.",
            "This file is observability only and never mutates a database.",
        ],
    }


def sanitize_json_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {sanitize_string(str(key)): sanitize_json_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_payload(item) for item in value]
    return json_safe(value)


def _parse_json_or_text(value: str | None) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except Exception:
        return str(value)


def read_historical_replay_progress(data_root: Path) -> dict[str, Any]:
    """Read a tiny replay-status projection directly from Alerts SQLite.

    Read-only and fail-soft: runtime-data publication must never disturb the
    historical replay or Alerts Bot.
    """
    db_path = data_root / "alerts_bot" / "db" / "live" / "tmad_alerts_adaptive.db"
    keys = (
        "historical_replay_status",
        "historical_replay_started_at_madrid",
        "historical_replay_finished_at_madrid",
        "historical_replay_heartbeat_at_madrid",
        "historical_replay_phase",
        "historical_replay_summary",
    )
    result: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "db_exists": db_path.exists(),
        "live_mutation": False,
    }
    if not db_path.exists():
        result["reason"] = "alerts_db_missing"
        return result
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            tables = {str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "runtime_state" not in tables:
                result["reason"] = "runtime_state_missing"
                return result
            placeholders = ",".join("?" for _ in keys)
            rows = con.execute(
                f"SELECT state_key,state_value,updated_at_utc FROM runtime_state "
                f"WHERE state_key IN ({placeholders})",
                keys,
            ).fetchall()
        finally:
            con.close()
        values = {
            str(key): {
                "value": _parse_json_or_text(value),
                "updated_at_utc": updated,
            }
            for key, value, updated in rows
        }
        result.update({
            "status": str((values.get("historical_replay_status") or {}).get("value") or "UNKNOWN").upper(),
            "started_at_madrid": (values.get("historical_replay_started_at_madrid") or {}).get("value"),
            "finished_at_madrid": (values.get("historical_replay_finished_at_madrid") or {}).get("value"),
            "heartbeat_at_madrid": (values.get("historical_replay_heartbeat_at_madrid") or {}).get("value"),
            "phase": (values.get("historical_replay_phase") or {}).get("value"),
            "summary": (values.get("historical_replay_summary") or {}).get("value"),
            "source": "alerts.runtime_state",
            "live_mutation": False,
        })
        return sanitize_json_payload(result)
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return sanitize_json_payload(result)


def publish_research_sidecars(
    data_root: Path,
    output_root: Path,
    *,
    max_file_bytes: int = RESEARCH_SIDECAR_MAX_FILE_BYTES,
    max_total_bytes: int = RESEARCH_SIDECAR_MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Publish a bounded, sanitized projection of strategic replay artifacts."""
    output_source_root = data_root / "alerts_bot" / "output"
    historical_source_root = output_source_root / "historical_validation"
    prospective_source_root = output_source_root / "prospective_architecture"
    destination_root = output_root / "research"
    result: dict[str, Any] = {}
    total_bytes = 0

    for name in RESEARCH_SIDECARS:
        source_root = (
            prospective_source_root
            if name == "latest_prospective_architecture_journal.json"
            else historical_source_root
        )
        source = source_root / name
        if not source.exists():
            result[name] = {"published": False, "status": "MISSING"}
            continue
        try:
            source_bytes = int(source.stat().st_size)
            if source_bytes > max_file_bytes:
                result[name] = {
                    "published": False,
                    "status": "SKIPPED_FILE_TOO_LARGE",
                    "source_bytes": source_bytes,
                    "max_file_bytes": int(max_file_bytes),
                }
                continue
            if total_bytes >= max_total_bytes:
                result[name] = {
                    "published": False,
                    "status": "SKIPPED_TOTAL_CAP",
                    "source_bytes": source_bytes,
                    "max_total_bytes": int(max_total_bytes),
                }
                continue

            payload = json.loads(source.read_text(encoding="utf-8-sig"))
            sanitized = sanitize_json_payload(payload)
            estimated = len(json.dumps(sanitized, ensure_ascii=False).encode("utf-8"))
            if estimated > max_file_bytes:
                result[name] = {
                    "published": False,
                    "status": "SKIPPED_SANITIZED_TOO_LARGE",
                    "source_bytes": source_bytes,
                    "sanitized_estimated_bytes": estimated,
                    "max_file_bytes": int(max_file_bytes),
                }
                continue
            if total_bytes + estimated > max_total_bytes:
                result[name] = {
                    "published": False,
                    "status": "SKIPPED_TOTAL_CAP",
                    "source_bytes": source_bytes,
                    "sanitized_estimated_bytes": estimated,
                    "max_total_bytes": int(max_total_bytes),
                }
                continue

            size = write_json(destination_root / name, sanitized)
            total_bytes += int(size)
            result[name] = {
                "published": True,
                "status": "PUBLISHED",
                "source_bytes": source_bytes,
                "published_bytes": int(size),
                "path": f"research/{name}",
                "source_path": source.relative_to(output_source_root).as_posix(),
            }
        except Exception as exc:
            result[name] = {
                "published": False,
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {exc}",
            }

    return {
        "source_root": output_source_root.as_posix(),
        "source_roots": {
            "historical_validation": historical_source_root.as_posix(),
            "prospective_architecture": prospective_source_root.as_posix(),
        },
        "published_count": sum(1 for item in result.values() if item.get("published")),
        "requested_count": len(RESEARCH_SIDECARS),
        "published_bytes": int(total_bytes),
        "max_file_bytes": int(max_file_bytes),
        "max_total_bytes": int(max_total_bytes),
        "files": result,
    }


def export_runtime_data(production_root: Path, output_root: Path, max_rows: int = 250, max_total_mb: int = 30) -> dict:
    production_root = production_root.resolve()
    data_root = (production_root / "03_DATOS").resolve()
    if not data_root.is_dir():
        raise FileNotFoundError(f"03_DATOS not found: {data_root}")
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    version_path = production_root / "VERSION.txt"
    version = version_path.read_text(encoding="utf-8-sig").strip() if version_path.exists() else "unknown"
    max_total_bytes = max(1, max_total_mb) * 1024 * 1024
    catalogs: list[dict[str, Any]] = []
    total_export_bytes = 0
    skipped_databases: list[str] = []

    databases = sorted(
        p for p in data_root.rglob("*")
        if p.is_file() and p.suffix.lower() in DB_EXTENSIONS
    )
    for db_path in databases:
        relative = db_path.relative_to(data_root)
        if any(part.lower() in SKIP_DIRS for part in relative.parts[:-1]):
            skipped_databases.append(relative.as_posix())
            continue
        if total_export_bytes >= max_total_bytes:
            skipped_databases.append(relative.as_posix())
            continue
        try:
            catalog, written = export_database(
                db_path, data_root, output_root, max_rows, max_total_bytes - total_export_bytes
            )
            total_export_bytes += written
            catalogs.append(catalog)
        except Exception as exc:
            catalogs.append({
                "path": relative.as_posix(),
                "bytes": db_path.stat().st_size,
                "tables": [],
                "errors": [{"database": relative.as_posix(), "error": f"{type(exc).__name__}: {exc}"}],
            })

    exported_tables = sum(1 for db in catalogs for table in db.get("tables", []) if table.get("exported"))
    error_count = sum(len(db.get("errors", [])) for db in catalogs)
    storage = build_storage_summary(catalogs, version)
    disk = shutil.disk_usage(data_root)
    disk_free_pct = round((disk.free / disk.total * 100.0), 3) if disk.total else 0.0
    disk_free_gb = round(disk.free / (1024 ** 3), 3)
    if disk_free_gb < 5.0 or disk_free_pct < 3.0:
        disk_status = "CRITICAL"
    elif disk_free_gb < 15.0 or disk_free_pct < 8.0:
        disk_status = "WARNING"
    else:
        disk_status = "OK"
    storage["drive"] = {
        "path": str(data_root.anchor or data_root),
        "total_bytes": int(disk.total),
        "used_bytes": int(disk.used),
        "free_bytes": int(disk.free),
        "free_gb": disk_free_gb,
        "free_pct": disk_free_pct,
        "status": disk_status,
        "warning_threshold": "free<15GB or free<8%",
        "critical_threshold": "free<5GB or free<3%",
    }

    # Small history-management sidecars are deliberately published to runtime-data.
    # They give QA/ChatGPT visibility of cold history without copying multi-GB DBs.
    history_sidecars: dict[str, Any] = {}
    history_output = data_root / "data_bot" / "output"
    for name in ("legacy_history_catalog.json", "history_storage_status.json"):
        source = history_output / name
        if not source.exists():
            continue
        try:
            payload = json.loads(source.read_text(encoding="utf-8-sig"))
            size = write_json(output_root / name, payload)
            history_sidecars[name] = {"published": True, "bytes": size}
        except Exception as exc:
            history_sidecars[name] = {"published": False, "error": f"{type(exc).__name__}: {exc}"}

    research_sidecars = publish_research_sidecars(data_root, output_root)
    historical_replay = read_historical_replay_progress(data_root)
    write_json(output_root / "historical_replay_progress.json", historical_replay)

    summary = {
        "schema_version": 3,
        "bot": "tmad-dual-bot",
        "generated_at_madrid": madrid_now_iso(),
        "version": version,
        "source": "03_DATOS live/mutable SQLite observability projection",
        "database_count": len(catalogs),
        "database_paths": [db.get("path") for db in catalogs],
        "skipped_database_count": len(skipped_databases),
        "skipped_databases": skipped_databases,
        "exported_table_count": exported_tables,
        "error_count": error_count,
        "max_rows_per_table": max_rows,
        "max_total_mb": max_total_mb,
        "exported_json_bytes": total_export_bytes,
        "storage_file_bytes": storage["totals"]["file_bytes"],
        "storage_wal_bytes": storage["totals"]["wal_bytes"],
        "storage_reclaimable_bytes": storage["totals"]["reclaimable_bytes"],
        "storage_reclaimable_pct": storage["totals"]["reclaimable_pct"],
        "drive_free_bytes": int(disk.free),
        "drive_free_gb": disk_free_gb,
        "drive_free_pct": disk_free_pct,
        "drive_status": disk_status,
        "history_sidecars": history_sidecars,
        "research_sidecars": research_sidecars,
        "research_sidecar_count": int(research_sidecars.get("published_count") or 0),
        "research_sidecar_bytes": int(research_sidecars.get("published_bytes") or 0),
        "historical_replay": historical_replay,
        "notes": [
            "Newest rows only; this is an observability projection, not a backup.",
            "Archive directories and log/temp material are intentionally excluded.",
            "Secrets and very long strings are redacted/truncated before publication.",
            "storage.json exposes physical/allocated/reclaimable SQLite space and host-drive free space read-only.",
            "Selected strategic replay artifacts are published read-only under runtime_data/research with strict file/total size caps.",
            "Full mutable data remains in the tmad-data-latest release backup.",
        ],
    }
    write_json(output_root / "storage.json", storage)
    write_json(output_root / "catalog.json", {"summary": summary, "databases": catalogs})
    write_json(output_root / "status.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Export lightweight TMAD runtime data for GitHub observability.")
    parser.add_argument("production_root")
    parser.add_argument("output_root")
    parser.add_argument("--max-rows", type=int, default=250)
    parser.add_argument("--max-total-mb", type=int, default=30)
    args = parser.parse_args()
    summary = export_runtime_data(
        Path(args.production_root), Path(args.output_root),
        max_rows=max(1, args.max_rows), max_total_mb=max(1, args.max_total_mb),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
