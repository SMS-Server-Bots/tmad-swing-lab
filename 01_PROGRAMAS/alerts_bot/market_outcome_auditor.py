from __future__ import annotations

"""Prospective full-universe validation for TMAD.

The auditor answers both sides of model quality:

* precision: did signals emitted by CORE/FUSION/ENTER reach the expected move?
* coverage/recall: of every mature +/-5% movement in the stored universe, how
  many were visible to CORE, Research, GEX/Fusion and ENTER before the hit?

Only point-in-time layer history and already-mature future outcomes are used.
No current or future feature value is introduced into a historical decision.
"""

import csv
import hashlib
import json
import math
import sqlite3
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZipFile

import config
from return_math import directional_return_pct
from db import (
    canonical_materialization_reconciliation,
    get_active_model,
    get_runtime_state,
    set_runtime_state,
)
from discord_presentation import build_group
from discord_utils import log_info, send_daily
from layer_archive import archived_feature_rows_with_outcomes, archived_joined_signal_outcomes
from historical_evidence import history_maturity_snapshot
from archive_deep_inventory import weekly_deep_inventory
from model_metrics import normalized_oos_metrics
from daily_reverse_alpha import build_daily_reverse_alpha
from economic_evidence import build_opportunity_ledger, build_daily_alpha_scorecard, evaluate_eod_alpha
from time_utils import ts_madrid_str, ts_utc_str
from research_snapshot import research_db_path, research_input_path


BAD_GEX_INTENTS = {
    "", "NONE", "UNKNOWN", "UNCLEAR", "NEUTRAL", "HEDGING", "STABILIZING",
    "NO_CONFIRMADO", "NOT_CONFIRMED", "DIVERGENT", "CONFLICT",
}
RESEARCH_STATES = {"DISCOVERED", "EARLY", "CAPPED", "READY", "RETEST", "ENTER"}
READY_STATES = {"READY", "RETEST", "ENTER"}
TIMING_READY_STATES = {"READY", "RETEST", "ENTER"}


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _pct(num: int | float, den: int | float) -> float | None:
    return round(float(num) / float(den) * 100.0, 3) if den else None


def _avg(values: Iterable[Any]) -> float | None:
    clean = [float(x) for x in values if _f(x) is not None]
    return round(statistics.mean(clean), 4) if clean else None


def _median(values: Iterable[Any]) -> float | None:
    clean = [float(x) for x in values if _f(x) is not None]
    return round(statistics.median(clean), 4) if clean else None


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _fmt_pct(value: Any) -> str:
    number = _f(value)
    return "n/a" if number is None else f"{number:.1f}%"


def _fmt_num(value: Any, digits: int = 1) -> str:
    number = _f(value)
    return "n/a" if number is None else f"{number:.{digits}f}"


def _fmt_quality_hit(metric: dict) -> str:
    if not bool((metric or {}).get("evaluable")):
        sample = int((metric or {}).get("sample") or 0)
        minimum = int(config.MARKET_OUTCOME_AUDITOR_MIN_SIGNAL_SAMPLE)
        return f"ACCUMULATING({sample}/{minimum})"
    return _fmt_pct((metric or {}).get("hit_rate"))


def _connect(timeout: float = 15.0) -> sqlite3.Connection:
    conn = sqlite3.connect(research_db_path(config.ALERTS_DB), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def ensure_market_outcome_audit_schema() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_outcome_audit_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_key TEXT NOT NULL UNIQUE,
                created_at_utc TEXT NOT NULL,
                created_at_madrid TEXT NOT NULL,
                window_start_date TEXT,
                window_end_date TEXT,
                horizon_sessions INTEGER NOT NULL,
                active_model_id INTEGER,
                active_model_key TEXT,
                source_feature_rows INTEGER NOT NULL DEFAULT 0,
                source_feature_dates INTEGER NOT NULL DEFAULT 0,
                movement_events INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                diagnostics_json TEXT NOT NULL,
                context TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_market_audit_created
                ON market_outcome_audit_runs(created_at_utc);

            CREATE TABLE IF NOT EXISTS market_outcome_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_run_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                hit_date TEXT,
                entry_price REAL,
                threshold_price REAL,
                move_pct REAL,
                first_hit_session INTEGER,
                core_detected INTEGER NOT NULL DEFAULT 0,
                research_reached INTEGER NOT NULL DEFAULT 0,
                gex_confirmed INTEGER NOT NULL DEFAULT 0,
                timing_ready INTEGER NOT NULL DEFAULT 0,
                fusion_ready INTEGER NOT NULL DEFAULT 0,
                enter_generated INTEGER NOT NULL DEFAULT 0,
                core_score REAL,
                core_probability_pct REAL,
                core_rank INTEGER,
                momentum_rank INTEGER,
                atr_rank INTEGER,
                flow_rank INTEGER,
                lead_sessions INTEGER,
                pre_move_pct REAL,
                classification TEXT NOT NULL,
                sector TEXT,
                industry TEXT,
                details_json TEXT NOT NULL,
                unique_key TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_market_audit_events_run
                ON market_outcome_audit_events(audit_run_id,classification);
            CREATE INDEX IF NOT EXISTS idx_market_audit_events_ticker
                ON market_outcome_audit_events(ticker,hit_date);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _source_fingerprint(active_model_id: int) -> str:
    conn = _connect()
    try:
        live_features = live_feature_dates = live_signals = 0
        full_observation_outcomes = full_observation_outcome_max_id = 0
        live_end = ""
        archived_features = archived_dates = 0
        archived_end = ""
        if _table_exists(conn, "layer_feature_outcomes"):
            row = conn.execute(
                """SELECT COUNT(*) rows,COUNT(DISTINCT signal_date) dates,MAX(signal_date) end_date
                   FROM layer_feature_outcomes WHERE complete=1"""
            ).fetchone()
            live_features = _i(row["rows"]); live_feature_dates = _i(row["dates"]); live_end = str(row["end_date"] or "")
        if _table_exists(conn, "layer_signal_outcomes"):
            live_signals = _i(conn.execute(
                "SELECT COUNT(*) FROM layer_signal_outcomes WHERE complete=1"
            ).fetchone()[0])
        if _table_exists(conn, "observation_outcomes"):
            row = conn.execute(
                "SELECT COUNT(*) AS rows,COALESCE(MAX(id),0) AS max_id FROM observation_outcomes WHERE complete=1"
            ).fetchone()
            full_observation_outcomes = _i(row["rows"])
            full_observation_outcome_max_id = _i(row["max_id"])
        if _table_exists(conn, "layer_archive_catalog"):
            row = conn.execute(
                """SELECT COALESCE(SUM(feature_outcome_rows),0) rows,COUNT(*) dates,MAX(trade_date) end_date
                   FROM layer_archive_catalog WHERE verified=1"""
            ).fetchone()
            archived_features = _i(row["rows"]); archived_dates = _i(row["dates"]); archived_end = str(row["end_date"] or "")
    finally:
        conn.close()
    raw = "|".join(map(str, [
        config.BOT_VERSION, active_model_id, live_features, live_feature_dates, live_end,
        live_signals, full_observation_outcomes, full_observation_outcome_max_id,
        archived_features, archived_dates, archived_end,
        int(config.MARKET_OUTCOME_AUDITOR_HORIZON_SESSIONS),
        float(config.MARKET_OUTCOME_AUDITOR_TARGET_PCT),
    ]))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _live_feature_rows() -> list[dict]:
    conn = _connect()
    try:
        if not (_table_exists(conn, "layer_feature_history") and _table_exists(conn, "layer_feature_outcomes")):
            return []
        return [dict(r) for r in conn.execute(
            """SELECT f.*,o.entry_price AS outcome_entry_price,o.horizon_sessions,
                      o.long_5pct_hit,o.short_5pct_hit,o.first_long_5pct_session,
                      o.first_short_5pct_session,o.max_up_pct,o.max_down_pct,
                      o.close_return_pct,o.sessions_json,o.complete,o.labeled_at_utc
               FROM layer_feature_history f
               JOIN layer_feature_outcomes o ON o.layer_feature_id=f.id
               WHERE o.complete=1"""
        ).fetchall()]
    finally:
        conn.close()


def _all_feature_rows() -> list[dict]:
    rows = list(archived_feature_rows_with_outcomes()) + _live_feature_rows()
    dedup: dict[tuple, dict] = {}
    for row in rows:
        key = (
            _i(row.get("source_scanner_run_id")),
            str(row.get("snapshot_date") or row.get("signal_date") or ""),
            str(row.get("ticker") or "").upper(),
        )
        if key[1] and key[2]:
            dedup[key] = row
    return list(dedup.values())


def _live_signal_rows() -> list[dict]:
    conn = _connect()
    try:
        if not (_table_exists(conn, "layer_signal_history") and _table_exists(conn, "layer_signal_outcomes")):
            return []
        return [dict(r) for r in conn.execute(
            """SELECT s.*,o.entry_price AS outcome_entry_price,o.max_horizon_sessions,
                      o.movement_5pct_hit,o.target_zone_hit,o.first_5pct_session,
                      o.first_target_session,o.max_favorable_pct,o.max_adverse_pct,
                      o.target_error_pct,o.sessions_json AS outcome_sessions_json,
                      o.close_return_pct,o.resolved_reason,o.complete
               FROM layer_signal_history s
               JOIN layer_signal_outcomes o ON o.layer_signal_id=s.id
               WHERE o.complete=1"""
        ).fetchall()]
    finally:
        conn.close()


def _normalize_signal(row: dict) -> dict:
    payload = _json(row.get("payload_json"))
    out = dict(payload)
    for key in (
        "layer", "snapshot_date", "signal_date", "ticker", "direction", "score", "state",
        "actionable", "signal_origin", "source_scanner_run_id", "source_gex_run_id",
        "target_source", "target_pct", "gex_intent", "expiry_regime", "wall_asymmetry",
        "movement_5pct_hit", "target_zone_hit", "first_5pct_session", "first_target_session",
        "max_favorable_pct", "max_adverse_pct", "close_return_pct", "model_version_id",
    ):
        if row.get(key) not in (None, ""):
            out[key] = row.get(key)
    out["snapshot_date"] = str(out.get("snapshot_date") or out.get("signal_date") or "")
    out["ticker"] = str(out.get("ticker") or "").upper()
    out["direction"] = str(out.get("direction") or "").lower()
    out["layer"] = str(out.get("layer") or "").upper()
    out["state"] = str(out.get("opportunity_state") or out.get("state") or "WATCH").upper()
    out["score"] = _f(out.get("score"), 0.0) or 0.0
    out["actionable"] = bool(_i(out.get("actionable"))) or out["state"] == "ENTER"
    out["payload"] = payload
    return out


def _all_signal_rows() -> list[dict]:
    rows = list(archived_joined_signal_outcomes()) + _live_signal_rows()
    dedup: dict[tuple, dict] = {}
    for raw in rows:
        row = _normalize_signal(raw)
        key = (
            row.get("snapshot_date"), row.get("ticker"), row.get("layer"),
            row.get("direction"), _i(row.get("source_scanner_run_id")),
        )
        if key[0] and key[1] and key[2]:
            old = dedup.get(key)
            if old is None or float(row.get("score") or 0) >= float(old.get("score") or 0):
                dedup[key] = row
    return list(dedup.values())


def _eod_selection_journal_rows() -> list[dict]:
    path = research_input_path(config.OUTPUT_DIR / "alpha_pnl_attribution" / "eod_selection_journal.jsonl")
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        return []
    return rows


def _alert_index() -> set[tuple[str, str, str]]:
    conn = _connect()
    try:
        if not _table_exists(conn, "alert_events"):
            return set()
        return {
            (str(r[0] or ""), str(r[1] or "").upper(), str(r[2] or "").lower())
            for r in conn.execute(
                """SELECT snapshot_date,ticker,direction FROM alert_events
                   WHERE status NOT IN ('ignored','archived')"""
            ).fetchall()
        }
    finally:
        conn.close()


def _price_series(tickers: set[str]) -> dict[str, list[dict]]:
    if not tickers:
        return {}
    conn = _connect()
    result: dict[str, list[dict]] = defaultdict(list)
    try:
        if not _table_exists(conn, "price_cache"):
            return {}
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(price_cache)").fetchall()
        }
        extra = ",".join([
            "adjusted_close" if "adjusted_close" in cols else "NULL AS adjusted_close",
            "split_factor" if "split_factor" in cols else "NULL AS split_factor",
            "adjustment_json" if "adjustment_json" in cols else "NULL AS adjustment_json",
            "source" if "source" in cols else "NULL AS source",
        ])
        values = sorted(tickers)
        for start in range(0, len(values), 400):
            batch = values[start:start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""SELECT ticker,trade_date,open,high,low,close,{extra}
                    FROM price_cache
                    WHERE ticker IN ({placeholders}) ORDER BY ticker,trade_date""",
                batch,
            ).fetchall()
            for row in rows:
                result[str(row["ticker"]).upper()].append(dict(row))
    finally:
        conn.close()
    return dict(result)


def _rows_to_outcome_price_series(rows: list[dict]) -> dict[str, list[dict]]:
    """Convert mature observation_outcomes sessions_json into daily OHLC series."""
    result: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        raw = row.get("sessions_json")
        try:
            sessions = json.loads(raw or "[]") if not isinstance(raw, list) else raw
        except Exception:
            sessions = []
        if not isinstance(sessions, list):
            continue
        for session in sessions:
            if not isinstance(session, dict):
                continue
            trade_date = str(session.get("trade_date") or session.get("date") or "")[:10]
            close = _f(session.get("close"))
            if not trade_date or close is None or close <= 0:
                continue
            result[ticker][trade_date] = {
                "ticker": ticker,
                "trade_date": trade_date,
                "open": _f(session.get("open"), close),
                "high": _f(session.get("high"), close),
                "low": _f(session.get("low"), close),
                "close": close,
                "source": "observation_outcomes",
            }
    return {
        ticker: [days[key] for key in sorted(days)]
        for ticker, days in result.items()
    }


def _merge_price_series(
    primary: dict[str, list[dict]],
    supplement: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Merge daily price series, preserving price_cache when both sources overlap."""
    merged: dict[str, dict[str, dict]] = defaultdict(dict)
    for ticker, rows in supplement.items():
        for row in rows:
            day = str(row.get("trade_date") or "")[:10]
            if day:
                merged[str(ticker).upper()][day] = dict(row)
    for ticker, rows in primary.items():
        for row in rows:
            day = str(row.get("trade_date") or "")[:10]
            if day:
                merged[str(ticker).upper()][day] = dict(row)
    return {
        ticker: [days[key] for key in sorted(days)]
        for ticker, days in merged.items()
    }


def _observation_outcome_price_series(
    canonical: dict[tuple[str, str], dict],
) -> tuple[dict[str, list[dict]], dict]:
    """Load the longest mature outcome path for each canonical FULL-DEEP observation.

    Exact observation_id lookups reuse already-labelled future OHLC paths and avoid
    depending on price_cache coverage for the Reverse Alpha denominator.
    """
    observation_ids = sorted({
        _i(row.get("id"))
        for row in canonical.values()
        if _i(row.get("id")) > 0
    })
    if not observation_ids:
        return {}, {"requested_observations": 0, "matched_observations": 0, "session_rows": 0}

    conn = _connect()
    selected: dict[int, dict] = {}
    try:
        if not _table_exists(conn, "observation_outcomes"):
            return {}, {"requested_observations": len(observation_ids), "matched_observations": 0, "session_rows": 0}
        for start in range(0, len(observation_ids), 400):
            batch = observation_ids[start:start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""SELECT observation_id,ticker,observation_date,horizon_sessions,sessions_json,id
                    FROM observation_outcomes
                    WHERE complete=1 AND sessions_json IS NOT NULL AND sessions_json<>''
                      AND observation_id IN ({placeholders})
                    ORDER BY observation_id,horizon_sessions DESC,id DESC""",
                batch,
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                oid = _i(row.get("observation_id"))
                if oid > 0 and oid not in selected:
                    selected[oid] = row
    finally:
        conn.close()

    series = _rows_to_outcome_price_series(list(selected.values()))
    session_rows = sum(len(rows) for rows in series.values())
    return series, {
        "requested_observations": len(observation_ids),
        "matched_observations": len(selected),
        "matched_pct": round(100.0 * len(selected) / len(observation_ids), 3) if observation_ids else None,
        "tickers": len(series),
        "session_rows": session_rows,
        "source": "mature_observation_outcomes_sessions_json",
    }


def _feature_payload(row: dict) -> dict:
    payload = _json(row.get("payload_json"))
    features = payload.get("features")
    return features if isinstance(features, dict) else payload


def _canonical_daily_rows(rows: list[dict]) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], list[dict]]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        date = str(row.get("snapshot_date") or row.get("signal_date") or "")
        ticker = str(row.get("ticker") or "").upper()
        if date and ticker:
            grouped[(date, ticker)].append(row)

    canonical: dict[tuple[str, str], dict] = {}
    for key, items in grouped.items():
        def priority(item: dict) -> tuple[int, int]:
            layer = str(item.get("observation_layer") or "").lower()
            daily = 2 if layer == "daily_close" else (1 if "deep" in layer else 0)
            return daily, _i(item.get("source_scanner_run_id"))
        canonical[key] = max(items, key=priority)
    return canonical, grouped


def _full_deep_reverse_canonical(max_dates: int) -> tuple[dict[tuple[str, str], dict], dict[str, dict], dict]:
    """Build the reverse-test universe from canonical FULL-DEEP observations.

    This is read-only SHADOW evidence.  A date is labelled FULL_SCANNER_DEEP only
    when the Data Bot deep-run inventory reconciles to the materialised canonical
    daily observations. Older/unreconciled dates remain explicitly subset-only.
    """
    conn = _connect()
    try:
        if not (_table_exists(conn, "feature_observations")):
            return {}, {}, {"status": "NO_FEATURE_OBSERVATIONS", "universe_scope": "OBSERVED_FEATURE_SUBSET"}

        date_rows = conn.execute(
            """SELECT DISTINCT snapshot_date
               FROM feature_observations
               WHERE observation_layer='daily_close' AND is_canonical_daily_close=1
                 AND snapshot_date IS NOT NULL AND snapshot_date<>''
               ORDER BY snapshot_date DESC LIMIT ?""",
            (max(1, int(max_dates or 20)),),
        ).fetchall()
        dates = sorted(str(row[0]) for row in date_rows if row[0])
        if not dates:
            return {}, {}, {"status": "NO_CANONICAL_DEEP_DATES", "universe_scope": "OBSERVED_FEATURE_SUBSET"}

        placeholders = ",".join("?" for _ in dates)
        rows = [dict(row) for row in conn.execute(
            f"""SELECT id,snapshot_date,source_scanner_run_id,ticker,current_price,
                       features_json,observation_layer,is_canonical_daily_close,
                       session_type,has_gex,source_archive,feature_tier,universe_quality
                FROM feature_observations
                WHERE observation_layer='daily_close' AND is_canonical_daily_close=1
                  AND snapshot_date IN ({placeholders})
                ORDER BY snapshot_date,source_scanner_run_id,id""",
            dates,
        ).fetchall()]
    finally:
        conn.close()

    normalized: list[dict] = []
    for row in rows:
        item = dict(row)
        item["outcome_entry_price"] = item.get("current_price")
        item["price"] = item.get("current_price")
        item["payload_json"] = item.get("features_json")
        normalized.append(item)
    canonical, _groups = _canonical_daily_rows(normalized)

    counts: dict[str, int] = Counter(date for date, _ticker in canonical)
    source_archives_by_date: dict[str, set[str]] = defaultdict(set)
    for row in normalized:
        date = str(row.get("snapshot_date") or "")
        source_archive = str(row.get("source_archive") or "").strip()
        if date and source_archive:
            source_archives_by_date[date].add(source_archive)

    cutoff = dates[0]
    try:
        reconciliation = canonical_materialization_reconciliation(
            cutoff, limit=max(500, len(dates) * 8)
        )
    except Exception as exc:
        reconciliation = {
            "expected_dates": 0,
            "expected_rows": 0,
            "actual_rows": 0,
            "missing_rows": 0,
            "details": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    reconciled = {
        str(row.get("snapshot_date") or ""): row
        for row in (reconciliation.get("details") or [])
        if row.get("snapshot_date")
    }
    archive_names = {
        name
        for values in source_archives_by_date.values()
        for name in values
    }
    archive_inventory = weekly_deep_inventory(
        config.DATA_ARCHIVE_DIR,
        archive_names=archive_names,
        dates=set(dates),
    )

    metadata: dict[str, dict] = {}
    for date in dates:
        available = int(counts.get(date, 0))
        live_rec = reconciled.get(date) or {}
        archive_rec = archive_inventory.get(date) or {}
        rec = live_rec if int(live_rec.get("expected_rows") or 0) > 0 else archive_rec
        expected = int(rec.get("expected_rows") or 0)
        complete = bool(expected > 0 and available >= expected)
        inventory_source = (
            "live_reconciliation"
            if int(live_rec.get("expected_rows") or 0) > 0
            else "weekly_archive"
            if int(archive_rec.get("expected_rows") or 0) > 0
            else "unknown"
        )
        metadata[date] = {
            "universe_scope": "FULL_SCANNER_DEEP" if complete else "OBSERVED_FEATURE_SUBSET",
            "expected_universe": expected or None,
            "available_universe": available,
            "coverage_pct": round(100.0 * available / expected, 3) if expected else None,
            "source_scanner_run_id": rec.get("run_id"),
            "materialization_complete": complete,
            "inventory_source": inventory_source,
            "source_archive": archive_rec.get("source_archive"),
        }

    scopes = {row["universe_scope"] for row in metadata.values()}
    overall_scope = (
        "FULL_SCANNER_DEEP"
        if scopes == {"FULL_SCANNER_DEEP"}
        else "MIXED_FULL_DEEP_AND_OBSERVED_SUBSET"
        if "FULL_SCANNER_DEEP" in scopes
        else "OBSERVED_FEATURE_SUBSET"
    )
    bridge = {
        "status": "OK" if canonical else "EMPTY",
        "universe_scope": overall_scope,
        "dates": len(dates),
        "canonical_rows": len(canonical),
        "full_deep_dates": sum(1 for row in metadata.values() if row["universe_scope"] == "FULL_SCANNER_DEEP"),
        "subset_dates": sum(1 for row in metadata.values() if row["universe_scope"] != "FULL_SCANNER_DEEP"),
        "reconciliation": {
            key: reconciliation.get(key)
            for key in ("expected_dates", "expected_rows", "actual_rows", "missing_rows", "incomplete_runs", "error")
            if key in reconciliation
        },
        "archive_inventory": {
            "archives_referenced": len(archive_names),
            "dates_reconciled": len(archive_inventory),
            "full_deep_dates_from_archive": sum(
                1 for row in metadata.values()
                if row.get("inventory_source") == "weekly_archive"
                and row.get("universe_scope") == "FULL_SCANNER_DEEP"
            ),
        },
        "policy": {
            "read_only": True,
            "live_mutation": False,
            "strategic_use_requires_full_scanner_deep": True,
        },
    }
    return canonical, metadata, bridge


def _rank_maps(canonical: dict[tuple[str, str], dict]) -> tuple[dict, dict[str, int]]:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for (date, _ticker), row in canonical.items():
        by_date[date].append(row)
    ranks: dict[tuple[str, str, str, str], int] = {}
    universe_sizes: dict[str, int] = {}
    for date, rows in by_date.items():
        universe_sizes[date] = len(rows)
        for direction in ("long", "short"):
            core = [r for r in rows if str(r.get("core_direction") or "").lower() == direction and _f(r.get("core_score")) is not None]
            core.sort(key=lambda r: float(r.get("core_score") or 0), reverse=True)
            for rank, row in enumerate(core, 1):
                ranks[(date, direction, str(row.get("ticker") or "").upper(), "core")] = rank

            momentum_rows = []
            atr_rows = []
            flow_rows = []
            for row in rows:
                features = _feature_payload(row)
                momentum = _f(features.get("momentum_5d_pct"))
                atr = _f(features.get("atr_pct"))
                flow_key = "call_accumulation_score" if direction == "long" else "put_accumulation_score"
                flow = _f(features.get(flow_key))
                ticker = str(row.get("ticker") or "").upper()
                if momentum is not None:
                    momentum_rows.append((momentum, ticker))
                if atr is not None:
                    atr_rows.append((atr, ticker))
                if flow is not None:
                    flow_rows.append((flow, ticker))
            momentum_rows.sort(key=lambda x: x[0], reverse=(direction == "long"))
            atr_rows.sort(key=lambda x: x[0], reverse=True)
            flow_rows.sort(key=lambda x: x[0], reverse=True)
            for rank, (_value, ticker) in enumerate(momentum_rows, 1):
                ranks[(date, direction, ticker, "momentum")] = rank
            for rank, (_value, ticker) in enumerate(atr_rows, 1):
                ranks[(date, direction, ticker, "atr")] = rank
            for rank, (_value, ticker) in enumerate(flow_rows, 1):
                ranks[(date, direction, ticker, "flow")] = rank
    return ranks, universe_sizes


def _future_row(series: dict[str, list[dict]], ticker: str, signal_date: str, session: int) -> dict | None:
    future = [row for row in series.get(ticker, []) if str(row.get("trade_date") or "") > signal_date]
    return future[session - 1] if session > 0 and len(future) >= session else None


def _movement_candidates(canonical: dict[tuple[str, str], dict], series: dict[str, list[dict]]) -> list[dict]:
    candidates: list[dict] = []
    for (signal_date, ticker), row in canonical.items():
        entry = _f(row.get("outcome_entry_price"), _f(row.get("price")))
        if not entry or entry <= 0:
            continue
        sector = row.get("sector"); industry = row.get("industry")
        for direction, hit_field, session_field, move_field in (
            ("long", "long_5pct_hit", "first_long_5pct_session", "max_up_pct"),
            ("short", "short_5pct_hit", "first_short_5pct_session", "max_down_pct"),
        ):
            if not bool(_i(row.get(hit_field))):
                continue
            first = _i(row.get(session_field))
            hit_row = _future_row(series, ticker, signal_date, first)
            hit_date = str((hit_row or {}).get("trade_date") or f"{signal_date}+{first}")
            move = abs(_f(row.get(move_field), 0.0) or 0.0)
            threshold = entry * (1.0 + float(config.MARKET_OUTCOME_AUDITOR_TARGET_PCT) / 100.0) if direction == "long" else entry * (1.0 - float(config.MARKET_OUTCOME_AUDITOR_TARGET_PCT) / 100.0)
            first_open = _f((hit_row or {}).get("open"))
            gap_pct = None
            if first_open and entry:
                gap_pct = float(directional_return_pct(direction, entry, first_open) or 0.0)
            candidates.append({
                "ticker": ticker, "direction": direction, "signal_date": signal_date,
                "hit_date": hit_date, "entry_price": entry, "threshold_price": threshold,
                "move_pct": move, "first_hit_session": first, "gap_pct": gap_pct,
                "sector": sector, "industry": industry, "row": row,
            })
    return candidates


def _signals_index(rows: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    out: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        date = str(row.get("snapshot_date") or "")
        ticker = str(row.get("ticker") or "").upper()
        direction = str(row.get("direction") or "").lower()
        if date and ticker and direction in {"long", "short"}:
            out[(date, ticker, direction)].append(row)
    return out


def _gex_is_confirmed(signal: dict) -> bool:
    intent = str(signal.get("gex_intent") or (_json(signal.get("payload_json")).get("gex_intent")) or "").upper()
    expiry = str(signal.get("expiry_status") or signal.get("expiry_regime") or "").upper()
    return intent not in BAD_GEX_INTENTS and "STALE" not in expiry and "EXPIRED" not in expiry


def _best_score(signals: list[dict], layer: str) -> float | None:
    values = [_f(s.get("score")) for s in signals if str(s.get("layer") or "").upper() == layer]
    clean = [x for x in values if x is not None]
    return max(clean) if clean else None


def _cluster_and_classify(
    candidates: list[dict], feature_groups: dict[tuple[str, str], list[dict]],
    signal_index: dict[tuple[str, str, str], list[dict]], ranks: dict,
    universe_sizes: dict[str, int], alerts: set[tuple[str, str, str]],
) -> list[dict]:
    clusters: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for item in candidates:
        clusters[(item["ticker"], item["direction"], item["hit_date"])].append(item)

    events: list[dict] = []
    for (ticker, direction, hit_date), source_rows in clusters.items():
        source_rows.sort(key=lambda x: (x["signal_date"], x["first_hit_session"]))
        earliest = source_rows[0]
        date_details: list[dict] = []
        for source in source_rows:
            date = source["signal_date"]
            sigs = signal_index.get((date, ticker, direction), [])
            feature_rows = feature_groups.get((date, ticker), [])
            core_sigs = [s for s in sigs if str(s.get("layer") or "").upper() == "CORE"]
            gex_sigs = [s for s in sigs if str(s.get("layer") or "").upper() == "GEX_DAILY"]
            timing_sigs = [s for s in sigs if str(s.get("layer") or "").upper() == "GEX_INTRADAY"]
            fusion_sigs = [s for s in sigs if str(s.get("layer") or "").upper() == "FUSION"]

            fallback_core = [r for r in feature_rows if str(r.get("core_direction") or "").lower() == direction and _f(r.get("core_score")) is not None]
            fallback_gex = [r for r in feature_rows if str(r.get("gex_direction") or "").lower() == direction]
            core_detected = bool(core_sigs or fallback_core)
            opposite = any(
                str(r.get("core_direction") or "").lower() in {"long", "short"}
                and str(r.get("core_direction") or "").lower() != direction
                for r in feature_rows
            ) or any(
                str(s.get("layer") or "").upper() == "CORE" and str(s.get("direction") or "").lower() != direction
                for s in sigs
            )
            core_score_values = [
                *[_f(s.get("score")) for s in core_sigs],
                *[_f(r.get("core_score")) for r in fallback_core],
            ]
            core_score = max([v for v in core_score_values if v is not None], default=None)
            probability_values = [
                *[_f(s.get("probability_pct") or s.get("core_probability_pct")) for s in core_sigs],
                *[_f(r.get("core_probability_pct")) for r in fallback_core],
            ]
            probability = max([v for v in probability_values if v is not None], default=None)
            research = any(str(s.get("state") or "").upper() in RESEARCH_STATES for s in sigs)
            gex_confirmed = any(_gex_is_confirmed(s) for s in gex_sigs) or any(
                str(r.get("gex_intent") or "").upper() not in BAD_GEX_INTENTS for r in fallback_gex
            )
            timing_ready = any(str(s.get("state") or "").upper() in TIMING_READY_STATES for s in timing_sigs)
            fusion_ready = any(
                bool(s.get("actionable")) or str(s.get("state") or "").upper() in READY_STATES
                for s in fusion_sigs
            )
            enter = (date, ticker, direction) in alerts
            blockers: list[str] = []
            for signal in fusion_sigs:
                value = signal.get("blockers") or signal.get("payload", {}).get("blockers") or []
                if isinstance(value, str):
                    blockers.append(value)
                elif isinstance(value, list):
                    blockers.extend(str(x) for x in value)
            date_details.append({
                **source,
                "core_detected": core_detected, "opposite_detected": opposite,
                "core_score": core_score, "core_probability_pct": probability,
                "research_reached": research, "gex_confirmed": gex_confirmed,
                "timing_ready": timing_ready, "fusion_ready": fusion_ready,
                "enter_generated": enter, "blockers": blockers,
                "core_rank": ranks.get((date, direction, ticker, "core")),
                "momentum_rank": ranks.get((date, direction, ticker, "momentum")),
                "atr_rank": ranks.get((date, direction, ticker, "atr")),
                "flow_rank": ranks.get((date, direction, ticker, "flow")),
                "universe_size": universe_sizes.get(date),
                "signals": sigs,
            })

        detected_rows = [x for x in date_details if x["core_detected"]]
        detection = detected_rows[0] if detected_rows else earliest | {
            "core_detected": False, "opposite_detected": any(x["opposite_detected"] for x in date_details),
            "core_score": None, "core_probability_pct": None, "research_reached": False,
            "gex_confirmed": False, "timing_ready": False, "fusion_ready": False,
            "enter_generated": False, "blockers": [], "core_rank": None,
            "momentum_rank": ranks.get((earliest["signal_date"], direction, ticker, "momentum")),
            "atr_rank": ranks.get((earliest["signal_date"], direction, ticker, "atr")),
            "flow_rank": ranks.get((earliest["signal_date"], direction, ticker, "flow")),
            "universe_size": universe_sizes.get(earliest["signal_date"]), "signals": [],
        }

        # Later stages may be reached after the first CORE detection but before the hit.
        core_detected = bool(detected_rows)
        research = any(x["research_reached"] for x in date_details)
        gex_confirmed = any(x["gex_confirmed"] for x in date_details)
        timing_ready = any(x["timing_ready"] for x in date_details)
        fusion_ready = any(x["fusion_ready"] for x in date_details)
        enter = any(x["enter_generated"] for x in date_details)
        lead = _i(detection.get("first_hit_session")) if core_detected else None
        earliest_entry = _f(earliest.get("entry_price"))
        detect_entry = _f(detection.get("entry_price"))
        pre_move = None
        if earliest_entry and detect_entry:
            pre_move = ((detect_entry / earliest_entry - 1.0) * 100.0) if direction == "long" else ((earliest_entry / detect_entry - 1.0) * 100.0)
        gap = (_f(earliest.get("gap_pct"), 0.0) or 0.0) >= float(config.MARKET_OUTCOME_AUDITOR_TARGET_PCT)
        late = bool(
            core_detected and (
                (lead is not None and lead <= int(config.MARKET_OUTCOME_AUDITOR_LATE_LEAD_SESSIONS))
                or ((_f(pre_move, 0.0) or 0.0) >= float(config.MARKET_OUTCOME_AUDITOR_LATE_PREMOVE_PCT))
            )
        )
        opposite = any(x["opposite_detected"] for x in date_details)

        if enter:
            classification = "ANTICIPATED_ENTER"
        elif gap and not core_detected:
            classification = "GAP_EVENT"
        elif not core_detected and opposite:
            classification = "WRONG_DIRECTION"
        elif not core_detected:
            classification = "NO_CORE_DIRECTION"
        elif late:
            classification = "DETECTED_LATE"
        elif not research:
            classification = "BELOW_RESEARCH_GATE"
        elif not gex_confirmed:
            classification = "GEX_NOT_CONFIRMED"
        elif not timing_ready:
            classification = "TIMING_NOT_CONFIRMED"
        elif not fusion_ready:
            classification = "FUSION_GATE"
        else:
            classification = "ENTER_GATE_OR_PORTFOLIO"

        blocker_counts = Counter(
            b.strip() for detail in date_details for b in detail.get("blockers", []) if str(b).strip()
        )
        blocker = blocker_counts.most_common(1)[0][0] if blocker_counts else ""
        events.append({
            "ticker": ticker, "direction": direction, "signal_date": earliest["signal_date"],
            "hit_date": hit_date, "entry_price": earliest["entry_price"],
            "threshold_price": earliest["threshold_price"],
            "move_pct": max(float(x.get("move_pct") or 0.0) for x in source_rows),
            "first_hit_session": earliest["first_hit_session"],
            "core_detected": core_detected, "research_reached": research,
            "gex_confirmed": gex_confirmed, "timing_ready": timing_ready,
            "fusion_ready": fusion_ready, "enter_generated": enter,
            "core_score": detection.get("core_score"),
            "core_probability_pct": detection.get("core_probability_pct"),
            "core_rank": detection.get("core_rank"),
            "momentum_rank": detection.get("momentum_rank"),
            "atr_rank": detection.get("atr_rank"), "flow_rank": detection.get("flow_rank"),
            "lead_sessions": lead, "pre_move_pct": pre_move,
            "classification": classification, "sector": earliest.get("sector"),
            "industry": earliest.get("industry"), "blocker": blocker,
            "universe_size": detection.get("universe_size"),
            "source_dates": [x["signal_date"] for x in date_details],
            "details": date_details,
        })
    return sorted(events, key=lambda x: (x["hit_date"], x["ticker"], x["direction"]))


def _dedup_signal_quality(rows: list[dict]) -> list[dict]:
    dedup: dict[tuple, dict] = {}
    for row in rows:
        if str(row.get("direction") or "") not in {"long", "short"}:
            continue
        key = (row.get("snapshot_date"), row.get("ticker"), row.get("layer"), row.get("direction"))
        old = dedup.get(key)
        if old is None or float(row.get("score") or 0) >= float(old.get("score") or 0):
            dedup[key] = row
    return list(dedup.values())


def _signal_quality(rows: list[dict], alerts: set[tuple[str, str, str]]) -> dict:
    rows = _dedup_signal_quality(rows)
    by_layer: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_layer[str(row.get("layer") or "").upper()].append(row)

    def metrics(items: list[dict]) -> dict:
        min_sample = int(config.MARKET_OUTCOME_AUDITOR_MIN_SIGNAL_SAMPLE)
        if not items:
            return {
                "sample": 0, "evaluable": False, "status": "ACCUMULATING",
                "hit_rate": None, "raw_hit_rate": None,
                "avg_mfe": None, "avg_mae": None, "avg_lead": None,
                "remaining_to_evaluable": min_sample,
            }
        raw_hit = _pct(sum(bool(_i(x.get("movement_5pct_hit"))) for x in items), len(items))
        evaluable = len(items) >= min_sample
        return {
            "sample": len(items),
            "evaluable": evaluable,
            "status": "EVALUABLE" if evaluable else "ACCUMULATING",
            # A small sample is persisted as raw evidence, but it is not exposed as
            # a conclusive precision estimate or compared against OOS.
            "hit_rate": raw_hit if evaluable else None,
            "raw_hit_rate": raw_hit,
            "avg_mfe": _avg(x.get("max_favorable_pct") for x in items),
            "avg_mae": _avg(x.get("max_adverse_pct") for x in items),
            "avg_lead": _avg(x.get("first_5pct_session") for x in items if _i(x.get("movement_5pct_hit"))),
            "remaining_to_evaluable": max(0, min_sample - len(items)),
        }

    core = by_layer.get("CORE", [])
    research = [x for x in core if str(x.get("state") or "").upper() in RESEARCH_STATES]
    fusion = [x for x in by_layer.get("FUSION", []) if bool(x.get("actionable")) or str(x.get("state") or "").upper() in READY_STATES]
    enter_items = [
        x for x in fusion
        if (str(x.get("snapshot_date") or ""), str(x.get("ticker") or "").upper(), str(x.get("direction") or "").lower()) in alerts
    ]
    probability_bins: dict[str, list[int]] = defaultdict(list)
    for row in core:
        p = _f(row.get("probability_pct") or row.get("core_probability_pct"))
        if p is None:
            continue
        if p < 50: label = "<50"
        elif p < 60: label = "50-59"
        elif p < 70: label = "60-69"
        elif p < 80: label = "70-79"
        elif p < 90: label = "80-89"
        else: label = "90+"
        probability_bins[label].append(1 if _i(row.get("movement_5pct_hit")) else 0)
    return {
        "core": metrics(core), "research": metrics(research),
        "fusion_ready": metrics(fusion), "enter": metrics(enter_items),
        "probability_calibration": {
            label: {"sample": len(values), "observed_hit_rate": _pct(sum(values), len(values))}
            for label, values in probability_bins.items()
        },
    }


def _gex_value(rows: list[dict]) -> dict:
    rows = _dedup_signal_quality(rows)
    index: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    core_rows: list[dict] = []
    for row in rows:
        key = (str(row.get("snapshot_date") or ""), str(row.get("ticker") or "").upper(), str(row.get("direction") or "").lower())
        index[key].append(row)
        if str(row.get("layer") or "").upper() == "CORE":
            core_rows.append(row)
    confirmed: list[dict] = []
    blocked: list[dict] = []
    for core in core_rows:
        key = (str(core.get("snapshot_date") or ""), str(core.get("ticker") or "").upper(), str(core.get("direction") or "").lower())
        gex = [x for x in index.get(key, []) if str(x.get("layer") or "").upper() == "GEX_DAILY"]
        (confirmed if any(_gex_is_confirmed(x) for x in gex) else blocked).append(core)

    def raw_hit_rate(items: list[dict]) -> float | None:
        return _pct(sum(bool(_i(x.get("movement_5pct_hit"))) for x in items), len(items))

    min_sample = int(config.MARKET_OUTCOME_AUDITOR_MIN_SIGNAL_SAMPLE)
    base_raw = raw_hit_rate(core_rows); confirmed_raw = raw_hit_rate(confirmed)
    blocked_hits = sum(bool(_i(x.get("movement_5pct_hit"))) for x in blocked)
    base_evaluable = len(core_rows) >= min_sample
    confirmed_evaluable = len(confirmed) >= min_sample
    blocked_evaluable = len(blocked) >= min_sample
    base_rate = base_raw if base_evaluable else None
    confirmed_rate = confirmed_raw if confirmed_evaluable else None
    blocking_raw = _pct(len(blocked) - blocked_hits, len(blocked))
    return {
        "status": "EVALUABLE" if base_evaluable else "ACCUMULATING",
        "min_signal_sample": min_sample,
        "core_sample": len(core_rows), "core_evaluable": base_evaluable,
        "core_hit_rate": base_rate, "core_raw_hit_rate": base_raw,
        "confirmed_sample": len(confirmed), "confirmed_evaluable": confirmed_evaluable,
        "confirmed_hit_rate": confirmed_rate, "confirmed_raw_hit_rate": confirmed_raw,
        "lift_pp": round(float(confirmed_rate) - float(base_rate), 3) if confirmed_rate is not None and base_rate is not None else None,
        "blocked_sample": len(blocked), "blocked_evaluable": blocked_evaluable,
        "blocked_correct": len(blocked) - blocked_hits,
        "good_opportunities_blocked": blocked_hits,
        "blocking_precision": blocking_raw if blocked_evaluable else None,
        "blocking_raw_precision": blocking_raw,
    }


def _recall(events: list[dict], field: str, k: int) -> float | None:
    return _pct(sum(1 for event in events if _i(event.get(field), 10**9) <= k), len(events))


def _benchmarks(events: list[dict], universe_sizes: dict[str, int]) -> dict:
    top_ks = list(config.MARKET_OUTCOME_AUDITOR_TOP_K)
    result: dict[str, dict[str, float | None]] = {}
    for k in top_ks:
        random_values = [min(1.0, k / max(1, _i(e.get("universe_size"), universe_sizes.get(e.get("signal_date"), 1)))) for e in events]
        result[str(k)] = {
            "core_recall": _recall(events, "core_rank", k),
            "momentum_recall": _recall(events, "momentum_rank", k),
            "atr_recall": _recall(events, "atr_rank", k),
            "flow_recall": _recall(events, "flow_rank", k),
            "random_expected": round(statistics.mean(random_values) * 100.0, 3) if random_values else None,
        }
    return result


def _expansion_proxy(events: list[dict], canonical: dict[tuple[str, str], dict]) -> dict:
    by_date_direction: dict[tuple[str, str], set[str]] = defaultdict(set)
    by_date_universe: dict[str, int] = Counter(date for date, _ticker in canonical)
    for event in events:
        by_date_direction[(event["signal_date"], event["direction"])].add(event["ticker"])

    conn = _connect()
    regimes: dict[str, dict] = {}
    try:
        if _table_exists(conn, "market_regime_history"):
            for row in conn.execute("SELECT * FROM market_regime_history ORDER BY source_scanner_run_id"):
                regimes[str(row["snapshot_date"] or "")] = dict(row)
    finally:
        conn.close()

    actual = anticipated = 0
    details = []
    for (date, direction), tickers in sorted(by_date_direction.items()):
        universe = max(1, by_date_universe.get(date, 0))
        threshold = max(
            int(config.MARKET_OUTCOME_AUDITOR_EXPANSION_MIN_TICKERS),
            int(math.ceil(universe * float(config.MARKET_OUTCOME_AUDITOR_EXPANSION_MIN_RATIO))),
        )
        if len(tickers) < threshold:
            continue
        actual += 1
        regime = regimes.get(date) or {}
        signal = bool(_i(regime.get("close_all_shorts_signal"))) if direction == "long" else bool(_i(regime.get("close_all_longs_signal")))
        anticipated += 1 if signal else 0
        details.append({"date": date, "direction": direction, "tickers": len(tickers), "threshold": threshold, "anticipated": signal})
    return {"events": actual, "anticipated": anticipated, "recall": _pct(anticipated, actual), "details": details[-20:]}


def _drift(events: list[dict], signal_quality: dict, active_model: dict) -> dict:
    dates = sorted({str(e.get("signal_date") or "") for e in events if e.get("signal_date")})
    rolling_dates = set(dates[-int(config.MARKET_OUTCOME_AUDITOR_ROLLING_DATES):])
    recent = [e for e in events if e.get("signal_date") in rolling_dates]
    previous = [e for e in events if e.get("signal_date") not in rolling_dates]
    recent_cov = _pct(sum(bool(e.get("core_detected")) for e in recent), len(recent))
    previous_cov = _pct(sum(bool(e.get("core_detected")) for e in previous), len(previous))
    oos = normalized_oos_metrics((active_model or {}).get("metrics") or {})
    oos_hit = _f(oos.get("target_hit_rate") or oos.get("win_rate"))
    core_quality = signal_quality.get("core") or {}
    live_hit = _f(core_quality.get("hit_rate")) if bool(core_quality.get("evaluable")) else None
    warnings: list[str] = []
    statistically_evaluable = (
        len(recent) >= int(config.MARKET_OUTCOME_AUDITOR_MIN_EVENTS)
        and bool(core_quality.get("evaluable"))
    )
    if statistically_evaluable:
        if previous_cov is not None and recent_cov is not None and recent_cov < previous_cov - float(config.MARKET_OUTCOME_AUDITOR_DRIFT_COVERAGE_DROP_PP):
            warnings.append(f"cobertura reciente {recent_cov:.1f}% vs previa {previous_cov:.1f}%")
        if oos_hit is not None and live_hit is not None and live_hit < oos_hit - float(config.MARKET_OUTCOME_AUDITOR_DRIFT_PRECISION_DROP_PP):
            warnings.append(f"precisión prospectiva {live_hit:.1f}% vs OOS {oos_hit:.1f}%")
    status = "DRIFT" if warnings else ("STABLE" if statistically_evaluable else "ACCUMULATING")
    return {
        "status": status, "evaluable": statistically_evaluable,
        "recent_dates": len(rolling_dates), "recent_events": len(recent),
        "recent_coverage": recent_cov if len(recent) >= int(config.MARKET_OUTCOME_AUDITOR_MIN_EVENTS) else None,
        "raw_recent_coverage": recent_cov,
        "previous_coverage": previous_cov if statistically_evaluable else None,
        "raw_previous_coverage": previous_cov,
        "oos_hit_rate": oos_hit if statistically_evaluable else None,
        "reference_oos_hit_rate": oos_hit,
        "prospective_hit_rate": live_hit, "warnings": warnings,
        "reason": None if statistically_evaluable else (
            f"minimum evidence not reached: movements {len(recent)}/{int(config.MARKET_OUTCOME_AUDITOR_MIN_EVENTS)}, "
            f"CORE signals {int(core_quality.get('sample') or 0)}/{int(config.MARKET_OUTCOME_AUDITOR_MIN_SIGNAL_SAMPLE)}"
        ),
    }


def _aggregate_metrics(events: list[dict], signal_rows: list[dict], alerts: set[tuple[str, str, str]], universe_sizes: dict[str, int], active_model: dict, canonical: dict) -> tuple[dict, dict]:
    total = len(events)
    classifications = Counter(str(e.get("classification") or "UNKNOWN") for e in events)
    sectors = Counter(str(e.get("sector") or "UNKNOWN") for e in events if not e.get("enter_generated"))
    funnel = {
        "movements": total,
        "core_detected": sum(bool(e.get("core_detected")) for e in events),
        "research_reached": sum(bool(e.get("research_reached")) for e in events),
        "gex_confirmed": sum(bool(e.get("gex_confirmed")) for e in events),
        "timing_ready": sum(bool(e.get("timing_ready")) for e in events),
        "fusion_ready": sum(bool(e.get("fusion_ready")) for e in events),
        "enter_generated": sum(bool(e.get("enter_generated")) for e in events),
    }
    funnel.update({f"{key}_pct": _pct(value, total) for key, value in list(funnel.items()) if key != "movements"})
    lead_values = [e.get("lead_sessions") for e in events if e.get("core_detected")]
    pre_move_values = [e.get("pre_move_pct") for e in events if e.get("core_detected")]
    anticipation = {
        "median_lead_sessions": _median(lead_values), "avg_lead_sessions": _avg(lead_values),
        "median_pre_move_pct": _median(pre_move_values),
        "late_events": classifications.get("DETECTED_LATE", 0),
        "late_pct": _pct(classifications.get("DETECTED_LATE", 0), total),
    }
    signal_quality = _signal_quality(signal_rows, alerts)
    gex = _gex_value(signal_rows)
    benchmarks = _benchmarks(events, universe_sizes)
    expansion = _expansion_proxy(events, canonical)
    drift = _drift(events, signal_quality, active_model)
    metrics = {
        "funnel": funnel, "anticipation": anticipation, "signal_quality": signal_quality,
        "gex_value": gex, "benchmarks": benchmarks, "expansion": expansion, "drift": drift,
    }
    diagnostics = {
        "classifications": dict(classifications), "missed_sectors": dict(sectors.most_common(10)),
        "top_misses": sorted(
            [e for e in events if not e.get("enter_generated")],
            key=lambda e: (float(e.get("move_pct") or 0), -_i(e.get("core_rank"), 99999)), reverse=True,
        )[: int(config.MARKET_OUTCOME_AUDITOR_MAX_MISSES_CONSOLE)],
    }
    return metrics, diagnostics


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_artifacts(payload: dict, events: list[dict]) -> dict[str, str]:
    out_dir = config.MARKET_OUTCOME_AUDITOR_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    latest_json = out_dir / "latest_market_outcome_audit.json"
    latest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    history = out_dir / "market_outcome_audit_history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    event_rows = []
    for event in events:
        event_rows.append({k: v for k, v in event.items() if k not in {"details"}})
    fields = [
        "ticker", "direction", "signal_date", "hit_date", "entry_price", "threshold_price",
        "move_pct", "first_hit_session", "core_detected", "research_reached", "gex_confirmed",
        "timing_ready", "fusion_ready", "enter_generated", "core_score", "core_probability_pct",
        "core_rank", "momentum_rank", "atr_rank", "flow_rank", "lead_sessions", "pre_move_pct",
        "classification", "sector", "industry", "blocker", "universe_size",
    ]
    events_csv = out_dir / "latest_movement_events.csv"
    misses_csv = out_dir / "latest_missed_movements.csv"
    _write_csv(events_csv, event_rows, fields)
    _write_csv(misses_csv, [x for x in event_rows if not x.get("enter_generated")], fields)

    ledger_json = out_dir / "latest_opportunity_ledger.json"
    ledger_payload = (payload.get("diagnostics") or {}).get("opportunity_ledger") or []
    ledger_json.write_text(json.dumps(ledger_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    scorecard_json = out_dir / "latest_daily_alpha_scorecard.json"
    scorecard_payload = (payload.get("metrics") or {}).get("daily_alpha_scorecard") or {}
    scorecard_json.write_text(json.dumps(scorecard_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {
        "json": str(latest_json), "events_csv": str(events_csv),
        "misses_csv": str(misses_csv), "history": str(history),
        "opportunity_ledger_json": str(ledger_json),
        "daily_alpha_scorecard_json": str(scorecard_json),
    }


def _persist(audit_key: str, payload: dict, events: list[dict], context: str) -> int:
    ensure_market_outcome_audit_schema()
    conn = _connect()
    try:
        metrics = payload.get("metrics") or {}; diagnostics = payload.get("diagnostics") or {}
        row = conn.execute("SELECT id FROM market_outcome_audit_runs WHERE audit_key=?", (audit_key,)).fetchone()
        if row:
            audit_id = int(row[0])
            conn.execute(
                """UPDATE market_outcome_audit_runs SET created_at_utc=?,created_at_madrid=?,window_start_date=?,
                         window_end_date=?,horizon_sessions=?,active_model_id=?,active_model_key=?,source_feature_rows=?,
                         source_feature_dates=?,movement_events=?,status=?,metrics_json=?,diagnostics_json=?,context=? WHERE id=?""",
                (payload["created_at_utc"], payload["created_at_madrid"], payload.get("window_start_date"),
                 payload.get("window_end_date"), payload["horizon_sessions"], payload.get("active_model_id"),
                 payload.get("active_model_key"), payload.get("source_feature_rows", 0), payload.get("source_feature_dates", 0),
                 len(events), payload.get("status"), json.dumps(metrics, ensure_ascii=False),
                 json.dumps(diagnostics, ensure_ascii=False, default=str), context, audit_id),
            )
            conn.execute("DELETE FROM market_outcome_audit_events WHERE audit_run_id=?", (audit_id,))
        else:
            cur = conn.execute(
                """INSERT INTO market_outcome_audit_runs(
                       audit_key,created_at_utc,created_at_madrid,window_start_date,window_end_date,horizon_sessions,
                       active_model_id,active_model_key,source_feature_rows,source_feature_dates,movement_events,status,
                       metrics_json,diagnostics_json,context) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (audit_key, payload["created_at_utc"], payload["created_at_madrid"], payload.get("window_start_date"),
                 payload.get("window_end_date"), payload["horizon_sessions"], payload.get("active_model_id"),
                 payload.get("active_model_key"), payload.get("source_feature_rows", 0), payload.get("source_feature_dates", 0),
                 len(events), payload.get("status"), json.dumps(metrics, ensure_ascii=False),
                 json.dumps(diagnostics, ensure_ascii=False, default=str), context),
            )
            audit_id = int(cur.lastrowid)
        for event in events:
            unique_key = f"{audit_key}|{event['ticker']}|{event['direction']}|{event['hit_date']}"
            conn.execute(
                """INSERT OR REPLACE INTO market_outcome_audit_events(
                       audit_run_id,ticker,direction,signal_date,hit_date,entry_price,threshold_price,move_pct,
                       first_hit_session,core_detected,research_reached,gex_confirmed,timing_ready,fusion_ready,
                       enter_generated,core_score,core_probability_pct,core_rank,momentum_rank,atr_rank,flow_rank,
                       lead_sessions,pre_move_pct,classification,sector,industry,details_json,unique_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (audit_id,event["ticker"],event["direction"],event["signal_date"],event.get("hit_date"),
                 event.get("entry_price"),event.get("threshold_price"),event.get("move_pct"),event.get("first_hit_session"),
                 1 if event.get("core_detected") else 0,1 if event.get("research_reached") else 0,
                 1 if event.get("gex_confirmed") else 0,1 if event.get("timing_ready") else 0,
                 1 if event.get("fusion_ready") else 0,1 if event.get("enter_generated") else 0,
                 event.get("core_score"),event.get("core_probability_pct"),event.get("core_rank"),
                 event.get("momentum_rank"),event.get("atr_rank"),event.get("flow_rank"),event.get("lead_sessions"),
                 event.get("pre_move_pct"),event.get("classification"),event.get("sector"),event.get("industry"),
                 json.dumps({"blocker": event.get("blocker"), "source_dates": event.get("source_dates")}, ensure_ascii=False),
                 unique_key),
            )
        conn.commit()
        return audit_id
    finally:
        conn.close()


def _console_report(payload: dict) -> None:
    metrics = payload.get("metrics") or {}; funnel = metrics.get("funnel") or {}
    quality = metrics.get("signal_quality") or {}; gex = metrics.get("gex_value") or {}
    anticipation = metrics.get("anticipation") or {}; drift = metrics.get("drift") or {}
    expansion = metrics.get("expansion") or {}
    diagnostics = payload.get("diagnostics") or {}; benchmarks = metrics.get("benchmarks") or {}
    min_events = int(config.MARKET_OUTCOME_AUDITOR_MIN_EVENTS)
    log_info(
        "📊 [AUDIT STATUS] "
        f"id={payload.get('audit_run_id') or 'pending'} status={payload.get('status') or 'UNKNOWN'} "
        f"context={payload.get('context') or 'n/a'} | "
        f"window={payload.get('window_start_date') or 'n/a'}→{payload.get('window_end_date') or 'n/a'} "
        f"horizon={payload.get('horizon_sessions')} target=±{float(config.MARKET_OUTCOME_AUDITOR_TARGET_PCT):.1f}% | "
        f"mature_dates={payload.get('source_feature_dates',0)} feature_rows={payload.get('source_feature_rows',0)} "
        f"events={funnel.get('movements',0)}/{min_events}_ready",
        send_discord=False,
    )
    log_info(
        "📊 [AUDIT MODEL/SOURCE] "
        f"model=#{payload.get('active_model_id') or 0} key={payload.get('active_model_key') or 'sin_modelo'} | "
        f"audit_key={str(payload.get('audit_key') or '')[:12]} | "
        f"definition=movimiento_real_±5%_en_1-{payload.get('horizon_sessions')}_sesiones_point_in_time",
        send_discord=False,
    )
    log_info(
        "📊 [AUDIT FUNNEL] "
        f"MOVES={funnel.get('movements',0)} | "
        f"CORE={funnel.get('core_detected',0)} ({_fmt_pct(funnel.get('core_detected_pct'))}) | "
        f"RESEARCH={funnel.get('research_reached',0)} ({_fmt_pct(funnel.get('research_reached_pct'))}) | "
        f"GEX={funnel.get('gex_confirmed',0)} ({_fmt_pct(funnel.get('gex_confirmed_pct'))}) | "
        f"TIMING={funnel.get('timing_ready',0)} ({_fmt_pct(funnel.get('timing_ready_pct'))}) | "
        f"FUSION={funnel.get('fusion_ready',0)} ({_fmt_pct(funnel.get('fusion_ready_pct'))}) | "
        f"ENTER={funnel.get('enter_generated',0)} ({_fmt_pct(funnel.get('enter_generated_pct'))})",
        send_discord=False,
    )
    core_q = quality.get("core") or {}; research_q = quality.get("research") or {}; fusion_q = quality.get("fusion_ready") or {}; enter_q = quality.get("enter") or {}
    log_info(
        "📊 [AUDIT PRECISION] "
        f"CORE n={core_q.get('sample',0)} hit={_fmt_quality_hit(core_q)} MFE={_fmt_pct(core_q.get('avg_mfe'))} MAE={_fmt_pct(core_q.get('avg_mae'))} | "
        f"RESEARCH n={research_q.get('sample',0)} hit={_fmt_quality_hit(research_q)} | "
        f"FUSION n={fusion_q.get('sample',0)} hit={_fmt_quality_hit(fusion_q)} | "
        f"ENTER n={enter_q.get('sample',0)} hit={_fmt_quality_hit(enter_q)}",
        send_discord=False,
    )
    log_info(
        "📊 [AUDIT ANTICIPATION] "
        f"lead_median={_fmt_num(anticipation.get('median_lead_sessions'))}s "
        f"lead_avg={_fmt_num(anticipation.get('avg_lead_sessions'))}s | "
        f"move_consumed_median={_fmt_pct(anticipation.get('median_pre_move_pct'))} | "
        f"late={anticipation.get('late_events',0)} ({_fmt_pct(anticipation.get('late_pct'))})",
        send_discord=False,
    )
    recall_parts = []
    for k in config.MARKET_OUTCOME_AUDITOR_TOP_K:
        row = benchmarks.get(str(k)) or {}
        recall_parts.append(
            f"@{k}:CORE={_fmt_pct(row.get('core_recall'))},MOM={_fmt_pct(row.get('momentum_recall'))},"
            f"ATR={_fmt_pct(row.get('atr_recall'))},FLOW={_fmt_pct(row.get('flow_recall'))},RAND={_fmt_pct(row.get('random_expected'))}"
        )
    log_info("📊 [AUDIT RECALL] " + " | ".join(recall_parts), send_discord=False)
    log_info(
        "📊 [AUDIT GEX VALUE] "
        f"status={gex.get('status','ACCUMULATING')} min_n={gex.get('min_signal_sample',int(config.MARKET_OUTCOME_AUDITOR_MIN_SIGNAL_SAMPLE))} | "
        f"CORE n={gex.get('core_sample',0)} hit={_fmt_pct(gex.get('core_hit_rate')) if gex.get('core_evaluable') else 'ACCUMULATING'} | "
        f"GEX_CONFIRMED n={gex.get('confirmed_sample',0)} hit={_fmt_pct(gex.get('confirmed_hit_rate')) if gex.get('confirmed_evaluable') else 'ACCUMULATING'} "
        f"lift={_fmt_num(gex.get('lift_pp'))}pp | "
        f"GEX_BLOCKED={gex.get('blocked_sample',0)} correct={gex.get('blocked_correct',0)} "
        f"blocking_precision={_fmt_pct(gex.get('blocking_precision')) if gex.get('blocked_evaluable') else 'ACCUMULATING'} good_blocked={gex.get('good_opportunities_blocked',0)}",
        send_discord=False,
    )
    log_info(
        "📊 [AUDIT EXPANSION] "
        f"general_moves={expansion.get('events',0)} anticipated={expansion.get('anticipated',0)} "
        f"recall={_fmt_pct(expansion.get('recall'))}",
        send_discord=False,
    )
    reverse = metrics.get("daily_reverse_alpha") or {}
    if reverse:
        log_info(
            "📊 [AUDIT REVERSE ALPHA] "
            f"scope={reverse.get('universe_scope','n/a')} "
            f"top={reverse.get('evaluated_top_opportunities',0)} "
            f"alert_recall={_fmt_pct(reverse.get('alert_recall_pct'))} "
            f"eod_recall={_fmt_pct(reverse.get('eod_alpha_recall_pct'))} "
            f"misses={reverse.get('miss_reasons') or {}}",
            send_discord=False,
        )
    classes = diagnostics.get("classifications") or {}
    missed_total = max(0, int(funnel.get("movements", 0)) - int(funnel.get("enter_generated", 0)))
    log_info(
        "📊 [AUDIT MISSES] "
        f"missed={missed_total} | " + " · ".join(f"{key}={value}" for key, value in sorted(classes.items())),
        send_discord=False,
    )
    for event in diagnostics.get("top_misses") or []:
        flags = "/".join([
            f"C{1 if event.get('core_detected') else 0}",
            f"R{1 if event.get('research_reached') else 0}",
            f"G{1 if event.get('gex_confirmed') else 0}",
            f"T{1 if event.get('timing_ready') else 0}",
            f"F{1 if event.get('fusion_ready') else 0}",
            f"E{1 if event.get('enter_generated') else 0}",
        ])
        log_info(
            "   ↳ [AUDIT MISS] "
            f"{event.get('ticker')} {str(event.get('direction')).upper()} "
            f"signal={event.get('signal_date')} hit={event.get('hit_date')} h={event.get('first_hit_session')} | "
            f"{_fmt_num(event.get('entry_price'),2)}→{_fmt_num(event.get('threshold_price'),2)} "
            f"move={_fmt_pct(event.get('move_pct'))} | "
            f"score={_fmt_num(event.get('core_score'))} rank={event.get('core_rank') or 'n/a'} "
            f"lead={event.get('lead_sessions') if event.get('lead_sessions') is not None else 'n/a'} "
            f"flags={flags} class={event.get('classification')} blocker={event.get('blocker') or 'n/a'}",
            send_discord=False,
        )
    log_info(
        "📊 [AUDIT DRIFT] "
        f"status={drift.get('status')} recent_dates={drift.get('recent_dates',0)} recent_events={drift.get('recent_events',0)} | "
        f"coverage_recent={_fmt_pct(drift.get('recent_coverage'))} prior={_fmt_pct(drift.get('previous_coverage'))} | "
        f"precision_live={_fmt_pct(drift.get('prospective_hit_rate'))} OOS={_fmt_pct(drift.get('oos_hit_rate'))} | "
        f"warnings={'; '.join(drift.get('warnings') or []) or 'none'} "
        f"reason={drift.get('reason') or 'evidence_sufficient'}",
        send_discord=False,
    )
    files = payload.get("files") or {}
    log_info(
        f"📁 [AUDIT OUTPUT] json={files.get('json')} | events={files.get('events_csv')} | misses={files.get('misses_csv')} | history={files.get('history')}",
        send_discord=False,
    )

def _discord_report(payload: dict) -> str:
    metrics = payload.get("metrics") or {}; funnel = metrics.get("funnel") or {}
    quality = (metrics.get("signal_quality") or {}).get("core") or {}
    gex = metrics.get("gex_value") or {}; anticipation = metrics.get("anticipation") or {}; drift = metrics.get("drift") or {}
    scorecard = metrics.get("daily_alpha_scorecard") or {}
    classes = payload.get("diagnostics", {}).get("classifications") or {}
    return build_group(
        "📊 VALIDACIÓN PROSPECTIVA · MARKET OUTCOME AUDITOR",
        ["\n".join([
            f"**Histórico global:** `{(payload.get('history_maturity') or {}).get('status','n/a')}` · `{(payload.get('history_maturity') or {}).get('historical_sample',0)}` obs / `{(payload.get('history_maturity') or {}).get('historical_dates',0)}` fechas.",
            f"**Ventana prospectiva:** `{funnel.get('movements',0)}` movimientos ±{float(config.MARKET_OUTCOME_AUDITOR_TARGET_PCT):.0f}% · `{payload.get('source_feature_dates',0)}` fechas de capa.",
            f"**Cobertura:** CORE `{_fmt_pct(funnel.get('core_detected_pct'))}` · Research `{_fmt_pct(funnel.get('research_reached_pct'))}` · GEX `{_fmt_pct(funnel.get('gex_confirmed_pct'))}` · ENTER `{_fmt_pct(funnel.get('enter_generated_pct'))}`.",
            f"**Precisión CORE:** `{_fmt_quality_hit(quality)}` sobre `{quality.get('sample',0)}` señales · anticipación mediana `{_fmt_num(anticipation.get('median_lead_sessions'))}` sesiones.",
            f"**GEX:** `{gex.get('status','ACCUMULATING')}` · lift `{_fmt_num(gex.get('lift_pp'))} pp` · bloqueos observados `{gex.get('blocked_sample',0)}` · buenas oportunidades bloqueadas `{gex.get('good_opportunities_blocked',0)}`.",
            f"**Perdidos:** no CORE `{classes.get('NO_CORE_DIRECTION',0)}` · dirección errónea `{classes.get('WRONG_DIRECTION',0)}` · tarde `{classes.get('DETECTED_LATE',0)}` · GEX `{classes.get('GEX_NOT_CONFIRMED',0)}` · gates `{classes.get('FUSION_GATE',0)+classes.get('ENTER_GATE_OR_PORTFOLIO',0)}`.",
            f"**Reverse Alpha:** scope `{(metrics.get('daily_reverse_alpha') or {}).get('universe_scope','n/a')}` · recall alertas `{_fmt_pct(((metrics.get('daily_reverse_alpha') or {}).get('alert_recall_pct')))}` · recall Alpha EOD `{_fmt_pct(((metrics.get('daily_reverse_alpha') or {}).get('eod_alpha_recall_pct')))}`.",
            f"**Economic Scorecard:** EOD maduras `{scorecard.get('eod_mature_selections',0)}` · win `{_fmt_pct(scorecard.get('eod_overall_win_rate_pct'))}` · avg `{_fmt_pct(scorecard.get('eod_overall_avg_return_pct'))}` · PF `{_fmt_num(scorecard.get('eod_overall_profit_factor'),2)}` · mayor fuga `{scorecard.get('largest_alpha_leak') or 'ACCUMULATING'}`.",
            f"**Estado:** `{drift.get('status')}` · {'; '.join(drift.get('warnings') or []) or 'sin deterioro estadísticamente evaluable'}.",
        ])],
        footer="La calibración mide las señales emitidas; este auditor mide también todos los movimientos que el modelo dejó escapar.",
    )


def latest_market_outcome_audit_summary() -> dict:
    path = config.MARKET_OUTCOME_AUDITOR_OUTPUT_DIR / "latest_market_outcome_audit.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def run_market_outcome_audit(context: str = "manual", *, force: bool = False, send_discord: bool = True, console_if_unchanged: bool = False) -> dict:
    if not bool(config.MARKET_OUTCOME_AUDITOR_ENABLED):
        return {"status": "disabled", "executed": False}
    ensure_market_outcome_audit_schema()
    active = get_active_model() or {}
    active_id = _i(active.get("id"))
    fingerprint = _source_fingerprint(active_id)
    previous = str(get_runtime_state("market_outcome_audit_fingerprint", "") or "")
    if not force and previous == fingerprint:
        cached = latest_market_outcome_audit_summary()
        if console_if_unchanged and cached:
            funnel = ((cached.get("metrics") or {}).get("funnel") or {})
            log_info(
                "📊 [AUDIT CACHE] no_new_mature_outcomes | "
                f"context={context} events={funnel.get('movements',0)} "
                f"CORE={_fmt_pct(funnel.get('core_detected_pct'))} "
                f"ENTER={_fmt_pct(funnel.get('enter_generated_pct'))} status={cached.get('status')}",
                send_discord=False,
            )
        return {**cached, "executed": False, "status": cached.get("status", "unchanged") if cached else "unchanged"}

    feature_rows = _all_feature_rows()
    if int(config.MARKET_OUTCOME_AUDITOR_MAX_DATES) > 0:
        dates = sorted({str(r.get("snapshot_date") or r.get("signal_date") or "") for r in feature_rows if r.get("snapshot_date") or r.get("signal_date")})
        allowed = set(dates[-int(config.MARKET_OUTCOME_AUDITOR_MAX_DATES):])
        feature_rows = [r for r in feature_rows if str(r.get("snapshot_date") or r.get("signal_date") or "") in allowed]
    signal_rows = _all_signal_rows()
    canonical, feature_groups = _canonical_daily_rows(feature_rows)
    tickers = {ticker for _date, ticker in canonical}
    series = _price_series(tickers)
    ranks, universe_sizes = _rank_maps(canonical)
    candidates = _movement_candidates(canonical, series)
    signals = _signals_index(signal_rows)
    alerts = _alert_index()
    events = _cluster_and_classify(candidates, feature_groups, signals, ranks, universe_sizes, alerts)
    metrics, diagnostics = _aggregate_metrics(events, signal_rows, alerts, universe_sizes, active, canonical)
    eod_selection_rows = _eod_selection_journal_rows()

    # Reverse/hindsight ranking must prefer the reconciled FULL-DEEP universe.
    # The prospective layer audit above remains unchanged; this bridge is
    # read-only and only improves the denominator used by economic QA.
    reverse_max_dates = max(20, int(config.MARKET_OUTCOME_AUDITOR_MAX_DATES or 0))
    reverse_canonical, reverse_universe_meta, reverse_bridge = _full_deep_reverse_canonical(reverse_max_dates)
    if reverse_canonical:
        reverse_price_cache = _price_series({ticker for _date, ticker in reverse_canonical})
        reverse_outcome_series, reverse_outcome_bridge = _observation_outcome_price_series(reverse_canonical)
        reverse_series = _merge_price_series(reverse_price_cache, reverse_outcome_series)
        reverse_bridge["outcome_price_bridge"] = {
            **reverse_outcome_bridge,
            "price_cache_tickers": len(reverse_price_cache),
            "merged_tickers": len(reverse_series),
            "merged_session_rows": sum(len(rows) for rows in reverse_series.values()),
            "price_cache_precedence_on_overlap": True,
        }
        reverse_ranks, reverse_universe_sizes = _rank_maps(reverse_canonical)
        reverse_alpha = build_daily_reverse_alpha(
            reverse_canonical, signals, reverse_ranks, reverse_universe_sizes, alerts, reverse_series,
            eod_selection_rows=eod_selection_rows,
            top_n=5,
            detail_days=20,
            universe_metadata=reverse_universe_meta,
        )
        eod_eval = evaluate_eod_alpha(reverse_canonical, reverse_series, eod_selection_rows)
    else:
        reverse_bridge = {
            **(reverse_bridge or {}),
            "status": "FALLBACK_OBSERVED_FEATURE_SUBSET",
            "universe_scope": "OBSERVED_FEATURE_SUBSET",
        }
        fallback_meta = {
            date: {
                "universe_scope": "OBSERVED_FEATURE_SUBSET",
                "expected_universe": None,
                "available_universe": size,
            }
            for date, size in universe_sizes.items()
        }
        reverse_alpha = build_daily_reverse_alpha(
            canonical, signals, ranks, universe_sizes, alerts, series,
            eod_selection_rows=eod_selection_rows,
            top_n=5,
            detail_days=20,
            universe_metadata=fallback_meta,
        )
        eod_eval = evaluate_eod_alpha(canonical, series, eod_selection_rows)
    reverse_alpha["full_deep_bridge"] = reverse_bridge
    opportunity_ledger = build_opportunity_ledger(reverse_alpha, events, eod_eval)
    daily_scorecard = build_daily_alpha_scorecard(reverse_alpha, eod_eval, opportunity_ledger)
    metrics["daily_reverse_alpha"] = {
        key: reverse_alpha.get(key) for key in (
            "mode", "universe_scope", "strategic_conclusion_allowed", "universe_coverage", "horizons", "top_n_per_day", "evaluated_top_opportunities",
            "alerted_top_opportunities", "alert_recall_pct",
            "eod_alpha_selected_top_opportunities", "eod_alpha_recall_pct",
            "miss_reasons", "by_horizon", "missed_feature_profile", "policy",
        )
    }
    diagnostics["daily_reverse_top_opportunities"] = reverse_alpha.get("top_opportunities") or []
    diagnostics["daily_reverse_summary"] = reverse_alpha.get("daily_summary") or []
    diagnostics["daily_reverse_full_deep_bridge"] = reverse_alpha.get("full_deep_bridge") or {}
    diagnostics["opportunity_ledger"] = opportunity_ledger
    metrics["daily_alpha_scorecard"] = daily_scorecard
    source_dates = sorted({date for date, _ticker in canonical})
    min_events = int(config.MARKET_OUTCOME_AUDITOR_MIN_EVENTS)
    maturity = history_maturity_snapshot({
        "source_feature_dates": len(source_dates),
        "metrics": {"funnel": {"movements": len(events)}},
    })
    status = (
        "READY" if len(events) >= min_events
        else ("PROSPECTIVE_VALIDATION" if maturity.get("history_ready") else "ACCUMULATING")
    )
    payload = {
        "version": config.BOT_VERSION, "audit_key": fingerprint,
        "created_at_utc": ts_utc_str(), "created_at_madrid": ts_madrid_str(),
        "context": context, "status": status,
        "window_start_date": source_dates[0] if source_dates else None,
        "window_end_date": source_dates[-1] if source_dates else None,
        "horizon_sessions": int(config.MARKET_OUTCOME_AUDITOR_HORIZON_SESSIONS),
        "active_model_id": active_id, "active_model_key": active.get("model_key"),
        "source_feature_rows": len(feature_rows), "source_feature_dates": len(source_dates),
        "history_maturity": maturity,
        "metrics": metrics, "diagnostics": diagnostics,
    }
    files = _write_artifacts(payload, events)
    payload["files"] = files
    audit_id = _persist(fingerprint, payload, events, context)
    payload["audit_run_id"] = audit_id
    # Rewrite JSON with persistent id and artifact paths included.
    Path(files["json"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    set_runtime_state("market_outcome_audit_fingerprint", fingerprint)
    set_runtime_state("market_outcome_audit_status", status)
    set_runtime_state("market_outcome_audit_last_events", str(len(events)))
    reverse_compact = metrics.get("daily_reverse_alpha") or {}
    set_runtime_state("market_outcome_daily_reverse_summary", json.dumps(reverse_compact, ensure_ascii=False, default=str))
    full_scorecard = metrics.get("daily_alpha_scorecard") or {}
    scorecard_compact = {
        key: full_scorecard.get(key) for key in (
            "schema_version", "mode", "days", "ledger_rows",
            "eod_mature_selections", "eod_overall_win_rate_pct",
            "eod_overall_avg_return_pct", "eod_overall_profit_factor",
            "largest_alpha_leak", "miss_reasons", "policy",
        )
    }
    scorecard_compact["recent_day_horizon"] = list(full_scorecard.get("by_day_horizon") or [])[-12:]
    set_runtime_state("market_outcome_daily_alpha_scorecard", json.dumps(scorecard_compact, ensure_ascii=False, default=str))
    _console_report(payload)
    if send_discord and bool(config.MARKET_OUTCOME_AUDITOR_DISCORD_ENABLED):
        discord_fingerprint = hashlib.sha256(json.dumps({"status": status, "metrics": metrics, "diagnostics": diagnostics.get("classifications")}, sort_keys=True, default=str).encode()).hexdigest()
        if force or str(get_runtime_state("market_outcome_audit_discord_fingerprint", "") or "") != discord_fingerprint:
            send_daily(_discord_report(payload))
            set_runtime_state("market_outcome_audit_discord_fingerprint", discord_fingerprint)
    return {**payload, "executed": True}


def maybe_run_market_outcome_audit(context: str, *, send_discord: bool = True, console_if_unchanged: bool = False) -> dict:
    try:
        return run_market_outcome_audit(
            context=context, force=False, send_discord=send_discord,
            console_if_unchanged=console_if_unchanged,
        )
    except sqlite3.OperationalError as exc:
        log_info(f"⚠️ [AUDIT DEFERRED] reason=SQLITE_BUSY | auditor aplazado sin afectar Alerts: {exc}", send_discord=False)
        return {"status": "deferred_sqlite", "executed": False, "error": str(exc)}
    except Exception as exc:
        log_info(f"⚠️ [AUDIT DEFERRED] reason=NON_FATAL_ERROR | auditor aplazado: {type(exc).__name__}: {exc}", send_discord=False)
        return {"status": "deferred_error", "executed": False, "error": str(exc)}
