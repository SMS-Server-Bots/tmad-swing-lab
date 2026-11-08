from __future__ import annotations

import json
from typing import Any

import config
from db import connect
from layer_archive import archived_feature_rows_with_outcomes, archived_joined_signal_outcomes
from model_metrics import normalized_oos_metrics
from return_math import normalize_signal_outcome_row


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except Exception:
        return default


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def canonical_signal_outcomes(archived_rows: list[dict] | None = None) -> list[dict]:
    """Economic signal outcomes deduplicated across archived + live snapshots.

    Multiple technical snapshots can represent the same economic opportunity.
    Calibration must count one opportunity per date/ticker/layer/direction, while
    retaining the latest source run for traceability.
    """
    rows = list(archived_rows if archived_rows is not None else archived_joined_signal_outcomes())
    conn = connect(config.ALERTS_DB)
    try:
        if _table_exists(conn, "layer_signal_history") and _table_exists(conn, "layer_signal_outcomes"):
            history_cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(layer_signal_history)").fetchall()
            }
            target_projection = "s.target_pct AS target_pct" if "target_pct" in history_cols else "NULL AS target_pct"
            rows.extend(dict(row) for row in conn.execute(
                f"""SELECT s.layer,s.snapshot_date,s.source_scanner_run_id,s.source_gex_run_id,
                           s.score,s.score AS layer_score,s.signal_origin,s.state,s.actionable,
                           s.opportunity_state,s.payload_json,{target_projection},o.*
                    FROM layer_signal_history s
                    JOIN layer_signal_outcomes o ON o.layer_signal_id=s.id
                    WHERE o.complete=1"""
            ).fetchall())
    finally:
        conn.close()

    rows = [normalize_signal_outcome_row(row) for row in rows]

    dedup: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        key = (
            str(row.get("signal_date") or row.get("snapshot_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
            str(row.get("layer") or "UNKNOWN").upper(),
            str(row.get("direction") or "").lower(),
        )
        if not key[0] or not key[1] or not key[3]:
            continue
        old = dedup.get(key)
        new_rank = (
            _i(row.get("source_scanner_run_id")),
            float(row.get("score") or 0.0),
            _i(row.get("id")),
        )
        old_rank = (
            _i((old or {}).get("source_scanner_run_id")),
            float((old or {}).get("score") or 0.0),
            _i((old or {}).get("id")),
        )
        if old is None or new_rank >= old_rank:
            dedup[key] = row
    return list(dedup.values())


def _best_model_evidence(conn) -> dict:
    if not _table_exists(conn, "model_versions"):
        return {"sample": 0, "market_dates": 0, "model_id": 0, "model_key": None}
    best = {"sample": 0, "market_dates": 0, "model_id": 0, "model_key": None}
    for row in conn.execute(
        """SELECT id,model_key,status,validation_sample,metrics_json
           FROM model_versions
           ORDER BY id DESC LIMIT 250"""
    ).fetchall():
        try:
            metrics = normalized_oos_metrics(json.loads(row["metrics_json"] or "{}"))
        except Exception:
            metrics = {}
        sample = max(_i(row["validation_sample"]), _i(metrics.get("sample")))
        dates = _i(metrics.get("market_dates"))
        rank = (dates, sample)
        if rank > (best["market_dates"], best["sample"]):
            best = {
                "sample": sample,
                "market_dates": dates,
                "model_id": _i(row["id"]),
                "model_key": row["model_key"],
                "status": row["status"],
            }
    return best


def history_maturity_snapshot(audit: dict | None = None) -> dict:
    """Single read-only view of how much usable history TMAD actually has.

    Historical maturity and prospective pipeline audit are deliberately separate:
    a short recent auditor window must not be reported as "no history" when
    purged OOS model evidence and mature CORE outcomes already exist.
    """
    audit = audit or {}
    archived_features = archived_feature_rows_with_outcomes()
    archived_feature_keys = {
        (
            _i(row.get("source_scanner_run_id")),
            str(row.get("signal_date") or row.get("snapshot_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
        )
        for row in archived_features
        if row.get("signal_date") or row.get("snapshot_date")
    }
    archived_feature_keys = {key for key in archived_feature_keys if key[1] and key[2]}

    conn = connect(config.ALERTS_DB)
    try:
        core_rows = core_dates = full_rows = full_dates = 0
        if _table_exists(conn, "feature_observations") and _table_exists(conn, "observation_outcomes"):
            horizon = int(config.MAX_PATTERN_HORIZON_SESSIONS)
            row = conn.execute(
                """SELECT COUNT(*) AS n, COUNT(DISTINCT f.snapshot_date) AS d
                   FROM feature_observations f
                   JOIN observation_outcomes o ON o.observation_id=f.id
                   WHERE o.complete=1 AND o.horizon_sessions=?
                     AND f.observation_layer='core_history' AND f.feature_tier='CORE'""",
                (horizon,),
            ).fetchone()
            core_rows, core_dates = _i(row["n"]), _i(row["d"])
            row = conn.execute(
                """SELECT COUNT(*) AS n, COUNT(DISTINCT f.snapshot_date) AS d
                   FROM feature_observations f
                   JOIN observation_outcomes o ON o.observation_id=f.id
                   WHERE o.complete=1 AND o.horizon_sessions=?
                     AND f.observation_layer='daily_close' AND f.is_canonical_daily_close=1""",
                (horizon,),
            ).fetchone()
            full_rows, full_dates = _i(row["n"]), _i(row["d"])

        live_feature_keys: set[tuple[int, str, str]] = set()
        if _table_exists(conn, "layer_feature_outcomes") and _table_exists(conn, "layer_feature_history"):
            for row in conn.execute(
                """SELECT h.source_scanner_run_id,o.signal_date,h.ticker
                   FROM layer_feature_outcomes o
                   JOIN layer_feature_history h ON h.id=o.layer_feature_id
                   WHERE o.complete=1"""
            ).fetchall():
                key = (
                    _i(row["source_scanner_run_id"]),
                    str(row["signal_date"] or "")[:10],
                    str(row["ticker"] or "").upper(),
                )
                if key[1] and key[2]:
                    live_feature_keys.add(key)
        layer_feature_keys = archived_feature_keys | live_feature_keys
        layer_feature_rows = len(layer_feature_keys)
        layer_feature_dates = len({key[1] for key in layer_feature_keys})

        closed_trades = reversal_trades = 0
        if _table_exists(conn, "open_trades"):
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          SUM(CASE WHEN exit_reason LIKE 'PATTERN_REVERSAL%' THEN 1 ELSE 0 END) AS r
                   FROM open_trades WHERE status='closed'"""
            ).fetchone()
            closed_trades, reversal_trades = _i(row["n"]), _i(row["r"])

        best_model = _best_model_evidence(conn)
    finally:
        conn.close()

    archived_signals = archived_joined_signal_outcomes()
    canonical_signals = canonical_signal_outcomes(archived_signals)
    raw_signal_count = 0
    try:
        raw_signal_count = len(archived_signals)
        conn = connect(config.ALERTS_DB)
        try:
            if _table_exists(conn, "layer_signal_outcomes"):
                raw_signal_count += _i(conn.execute(
                    "SELECT COUNT(*) FROM layer_signal_outcomes WHERE complete=1"
                ).fetchone()[0])
        finally:
            conn.close()
    except Exception:
        raw_signal_count = len(canonical_signals)

    canonical_signal_dates = len({
        str(row.get("signal_date") or row.get("snapshot_date") or "")[:10]
        for row in canonical_signals
        if row.get("signal_date") or row.get("snapshot_date")
    })
    canonical_signal_count = len(canonical_signals)
    duplicate_ratio = (
        round(raw_signal_count / canonical_signal_count, 3)
        if canonical_signal_count else (1.0 if raw_signal_count == 0 else None)
    )

    min_dates = max(
        int(getattr(config, "CORE_HISTORY_MIN_FULLY_LABELED_DATES", 15)),
        int(getattr(config, "ADAPTIVE_MIN_OOS_DATES", 4)),
    )
    min_sample = max(
        int(getattr(config, "CORE_HISTORY_MIN_VALIDATION_TRADES", 30)),
        int(getattr(config, "ADAPTIVE_MIN_OOS_SAMPLE", 15)),
    )
    historical_dates = max(core_dates, _i(best_model.get("market_dates")))
    historical_sample = max(core_rows, _i(best_model.get("sample")))
    history_ready = historical_dates >= min_dates and historical_sample >= min_sample

    prospective_dates = _i(audit.get("source_feature_dates"))
    prospective_movements = _i(((audit.get("metrics") or {}).get("funnel") or {}).get("movements"))
    prospective_ready = bool(
        prospective_dates >= int(getattr(config, "ADAPTIVE_MIN_OOS_DATES", 4))
        and prospective_movements >= int(getattr(config, "ADAPTIVE_MIN_MATURE_MOVEMENTS", 30))
    )

    flags: list[str] = []
    if history_ready and full_dates < min_dates:
        flags.append("FULL_HISTORY_GAP")
    if history_ready and prospective_dates < min_dates:
        flags.append("PROSPECTIVE_LAYER_GAP")
    if duplicate_ratio is not None and duplicate_ratio >= 1.20:
        flags.append("DUPLICATED_SIGNAL_OUTCOMES")
    if history_ready and canonical_signal_count == 0:
        flags.append("SIGNAL_OUTCOME_GAP")

    if history_ready and not prospective_ready:
        status = "HISTORY_READY_PROSPECTIVE_VALIDATION"
    elif history_ready:
        status = "HISTORY_READY"
    else:
        status = "BUILDING_HISTORY"

    return {
        "status": status,
        "history_ready": history_ready,
        "prospective_ready": prospective_ready,
        "thresholds": {"min_dates": min_dates, "min_sample": min_sample},
        "core_complete_outcomes": core_rows,
        "core_complete_dates": core_dates,
        "full_complete_outcomes": full_rows,
        "full_complete_dates": full_dates,
        "best_model_oos": best_model,
        "historical_sample": historical_sample,
        "historical_dates": historical_dates,
        "layer_feature_outcomes": layer_feature_rows,
        "layer_feature_dates": layer_feature_dates,
        "raw_signal_outcomes": raw_signal_count,
        "canonical_signal_outcomes": canonical_signal_count,
        "canonical_signal_dates": canonical_signal_dates,
        "signal_duplication_ratio": duplicate_ratio,
        "closed_trades": closed_trades,
        "pattern_reversal_trades": reversal_trades,
        "prospective_source_dates": prospective_dates,
        "prospective_movements": prospective_movements,
        "fragmentation_flags": flags,
    }
