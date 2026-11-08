from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_coordination_schema(db_path: Path) -> None:
    with closing(_connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS coordination_state (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                generation INTEGER NOT NULL DEFAULT 0,
                semantic_hash TEXT NOT NULL DEFAULT '',
                bot_version TEXT,
                bootstrap_id TEXT,
                bootstrap_complete INTEGER NOT NULL DEFAULT 0,
                core_ready INTEGER NOT NULL DEFAULT 0,
                universe_tickers INTEGER NOT NULL DEFAULT 0,
                eligible_tickers INTEGER NOT NULL DEFAULT 0,
                excluded_tickers INTEGER NOT NULL DEFAULT 0,
                pending_tickers INTEGER NOT NULL DEFAULT 0,
                remaining_tickers INTEGER NOT NULL DEFAULT 0,
                paired_trading_dates INTEGER NOT NULL DEFAULT 0,
                coverage_end_date TEXT,
                manifest_json TEXT NOT NULL DEFAULT '{}',
                reason TEXT,
                data_pid INTEGER,
                updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coordination_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                reason TEXT,
                semantic_hash TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_coord_events_generation
                ON coordination_events(generation);
            CREATE TABLE IF NOT EXISTS coordination_acks (
                consumer TEXT PRIMARY KEY,
                generation INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'none',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coordination_heartbeats (
                bot_name TEXT PRIMARY KEY,
                pid INTEGER,
                status TEXT NOT NULL,
                generation_seen INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                updated_at_utc TEXT NOT NULL
            );
            """
        )
        conn.commit()


def _stable_value(value: Any) -> Any:
    """Remove volatile timestamps/cache metadata before generation hashing."""
    volatile = {
        "generated_at", "generated_at_utc", "generated_at_madrid", "updated_at",
        "updated_at_utc", "updated_at_madrid", "bootstrap_started_at",
        "cache_updated_at", "archive_checked_at",
    }
    if isinstance(value, dict):
        return {
            str(k): _stable_value(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if str(k) not in volatile and not str(k).startswith("_")
        }
    if isinstance(value, list):
        return [_stable_value(v) for v in value]
    return value


def _semantic_hash(manifest: dict) -> str:
    raw = json.dumps(_stable_value(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_json(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def publish_data_state(
    db_path: Path,
    manifest: dict,
    *,
    reason: str = "manifest_update",
    backup_json_path: Path | None = None,
    force_generation: bool = False,
) -> dict:
    ensure_coordination_schema(db_path)
    now = _utc_now()
    semantic_hash = _semantic_hash(manifest)
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    with closing(_connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT generation, semantic_hash FROM coordination_state WHERE singleton_id=1").fetchone()
        old_generation = int(row["generation"] if row else 0)
        changed = bool(force_generation or not row or str(row["semantic_hash"] or "") != semantic_hash)
        generation = old_generation + 1 if changed else old_generation
        values = {
            "generation": generation,
            "semantic_hash": semantic_hash,
            "bot_version": str(manifest.get("bot_version") or ""),
            "bootstrap_id": str(manifest.get("bootstrap_id") or ""),
            "bootstrap_complete": int(bool(manifest.get("bootstrap_complete"))),
            "core_ready": int(bool(manifest.get("core_history_ready"))),
            "universe_tickers": int(manifest.get("core_history_universe_tickers") or 0),
            "eligible_tickers": int(manifest.get("core_history_complete_tickers") or 0),
            "excluded_tickers": int(manifest.get("core_history_excluded_tickers") or 0),
            "pending_tickers": int(manifest.get("core_history_pending_tickers") or manifest.get("core_history_remaining_tickers") or 0),
            "remaining_tickers": int(manifest.get("core_history_remaining_tickers") or manifest.get("core_history_pending_tickers") or 0),
            "paired_trading_dates": int(manifest.get("paired_trading_dates") or 0),
            "coverage_end_date": str(manifest.get("coverage_end_date") or ""),
            "manifest_json": manifest_json,
            "reason": reason,
            "data_pid": os.getpid(),
            "updated_at_utc": now,
        }
        conn.execute(
            """INSERT INTO coordination_state(
                   singleton_id,generation,semantic_hash,bot_version,bootstrap_id,
                   bootstrap_complete,core_ready,universe_tickers,eligible_tickers,
                   excluded_tickers,pending_tickers,remaining_tickers,paired_trading_dates,
                   coverage_end_date,manifest_json,reason,data_pid,updated_at_utc
               ) VALUES(1,:generation,:semantic_hash,:bot_version,:bootstrap_id,
                   :bootstrap_complete,:core_ready,:universe_tickers,:eligible_tickers,
                   :excluded_tickers,:pending_tickers,:remaining_tickers,:paired_trading_dates,
                   :coverage_end_date,:manifest_json,:reason,:data_pid,:updated_at_utc)
               ON CONFLICT(singleton_id) DO UPDATE SET
                   generation=excluded.generation, semantic_hash=excluded.semantic_hash,
                   bot_version=excluded.bot_version, bootstrap_id=excluded.bootstrap_id,
                   bootstrap_complete=excluded.bootstrap_complete, core_ready=excluded.core_ready,
                   universe_tickers=excluded.universe_tickers, eligible_tickers=excluded.eligible_tickers,
                   excluded_tickers=excluded.excluded_tickers, pending_tickers=excluded.pending_tickers,
                   remaining_tickers=excluded.remaining_tickers, paired_trading_dates=excluded.paired_trading_dates,
                   coverage_end_date=excluded.coverage_end_date, manifest_json=excluded.manifest_json,
                   reason=excluded.reason, data_pid=excluded.data_pid, updated_at_utc=excluded.updated_at_utc""",
            values,
        )
        if changed:
            conn.execute(
                "INSERT INTO coordination_events(generation,event_type,reason,semantic_hash,payload_json,created_at_utc) VALUES(?,?,?,?,?,?)",
                (generation, "data_state_published", reason, semantic_hash, manifest_json, now),
            )
        conn.commit()
    state = {**values, "changed": changed}
    _atomic_json(backup_json_path, state)
    return state


def read_data_state(db_path: Path) -> dict:
    if not db_path.exists():
        return {}
    ensure_coordination_schema(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM coordination_state WHERE singleton_id=1").fetchone()
    if not row:
        return {}
    out = dict(row)
    try:
        out["manifest"] = json.loads(out.get("manifest_json") or "{}")
    except Exception:
        out["manifest"] = {}
    for key in ("bootstrap_complete", "core_ready"):
        out[key] = bool(out.get(key))
    return out


def acknowledge_generation(
    db_path: Path,
    consumer: str,
    generation: int,
    status: str,
    *,
    message: str = "",
    details: dict | None = None,
    attempt_count: int | None = None,
) -> dict:
    ensure_coordination_schema(db_path)
    now = _utc_now()
    with closing(_connect(db_path)) as conn:
        current = conn.execute("SELECT attempt_count FROM coordination_acks WHERE consumer=?", (consumer,)).fetchone()
        attempts = int(attempt_count if attempt_count is not None else ((current[0] if current else 0) + 1))
        conn.execute(
            """INSERT INTO coordination_acks(consumer,generation,status,attempt_count,message,details_json,updated_at_utc)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(consumer) DO UPDATE SET generation=excluded.generation,status=excluded.status,
                   attempt_count=excluded.attempt_count,message=excluded.message,
                   details_json=excluded.details_json,updated_at_utc=excluded.updated_at_utc""",
            (consumer, int(generation), str(status), attempts, str(message), json.dumps(details or {}, ensure_ascii=False), now),
        )
        conn.commit()
    return {"consumer": consumer, "generation": int(generation), "status": status, "attempt_count": attempts, "updated_at_utc": now}


def read_ack(db_path: Path, consumer: str) -> dict:
    if not db_path.exists():
        return {}
    ensure_coordination_schema(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM coordination_acks WHERE consumer=?", (consumer,)).fetchone()
    return dict(row) if row else {}


def write_heartbeat(
    db_path: Path,
    bot_name: str,
    status: str,
    *,
    generation_seen: int = 0,
    details: dict | None = None,
) -> dict:
    ensure_coordination_schema(db_path)
    now = _utc_now()
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO coordination_heartbeats(bot_name,pid,status,generation_seen,details_json,updated_at_utc)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(bot_name) DO UPDATE SET pid=excluded.pid,status=excluded.status,
                   generation_seen=excluded.generation_seen,details_json=excluded.details_json,
                   updated_at_utc=excluded.updated_at_utc""",
            (str(bot_name), os.getpid(), str(status), int(generation_seen), json.dumps(details or {}, ensure_ascii=False), now),
        )
        conn.commit()
    return {"bot_name": bot_name, "status": status, "generation_seen": int(generation_seen), "updated_at_utc": now}


def read_heartbeat(db_path: Path, bot_name: str) -> dict:
    if not db_path.exists():
        return {}
    ensure_coordination_schema(db_path)
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM coordination_heartbeats WHERE bot_name=?", (bot_name,)).fetchone()
    return dict(row) if row else {}


def heartbeat_age_seconds(heartbeat: dict) -> float | None:
    raw = str(heartbeat.get("updated_at_utc") or "")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None
