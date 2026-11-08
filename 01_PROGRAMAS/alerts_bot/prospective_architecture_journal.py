from __future__ import annotations

"""Prospective SHADOW journal for TMAD 6.2.22.

This module freezes two hypotheses selected by the 6.2.21 dual-lane historical
review and observes them prospectively without changing LIVE decisions:

* CORE Direction V2 uses only the two direction features that survived both
  rolling and fixed-benchmark historical lanes.
* Research Override Top-1 records the strongest CORE candidate rejected by the
  current Research state on each authoritative deep-close run.

The journal never creates alerts, opens/closes positions, changes thresholds,
or mutates CORE/Research/GEX/Timing/FUSION/Lifecycle decisions.
"""

import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

import config
from db import connect, set_runtime_state
from time_utils import ts_utc_str


MODE = "PROSPECTIVE_ARCHITECTURE_JOURNAL_V1"
CORE_JOURNAL = "CORE_DIRECTION_V2"
RESEARCH_JOURNAL = "RESEARCH_OVERRIDE_TOP1"

# Frozen from the overlap of rolling + fixed benchmark in 6.2.21.
CORE_DIRECTION_RULES = (
    {
        "feature": "relationship_lead_lag_score",
        "threshold": 11.17,
        "high_direction": "SHORT",
        "historical_source": "rolling+fixed",
    },
    {
        "feature": "node_direction_score",
        "threshold": 15.9137,
        "high_direction": "SHORT",
        "historical_source": "rolling+fixed",
    },
)

RESEARCH_PASS_STATES = {"DISCOVERED", "EARLY", "CAPPED", "READY", "RETEST", "ENTER"}
STATE_PRIORITY = {
    "BLOCKED": 0,
    "MARKET_BLOCKED": 0,
    "DIVERGENCE": 0,
    "WATCH": 1,
    "DISCOVERED": 2,
    "EARLY": 3,
    "CAPPED": 4,
    "READY": 5,
    "RETEST": 6,
    "ENTER": 7,
}

CORE_MIN_MATURE_DATES = 10
CORE_MIN_MATURE_ROWS = 1000
RESEARCH_MIN_MATURE_DATES = 20
TARGET_PCT = 5.0
HORIZON_SESSIONS = 5


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _direction(value: Any) -> str | None:
    text = str(value or "").upper()
    return text if text in {"LONG", "SHORT"} else None


def _state(signal: dict) -> str:
    return str(signal.get("opportunity_state") or signal.get("state") or "WATCH").upper()


def _signal_score(signal: dict) -> float:
    for key in ("research_score", "score", "probability_pct", "core_score"):
        value = _f(signal.get(key))
        if value is not None:
            return float(value)
    return 0.0


def _signal_probability(signal: dict) -> float:
    for key in ("probability_pct", "core_probability_pct"):
        value = _f(signal.get(key))
        if value is not None:
            return float(value)
    return 0.0


def _best_core_signals(signals: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for signal in signals:
        if str(signal.get("layer") or "").upper() != "CORE":
            continue
        ticker = str(signal.get("ticker") or "").upper()
        if not ticker:
            continue
        state = _state(signal)
        rank = (
            int(STATE_PRIORITY.get(state, 1)),
            _signal_score(signal),
            _signal_probability(signal),
        )
        current = best.get(ticker)
        if current is None or rank > current["_journal_rank"]:
            best[ticker] = {**signal, "_journal_rank": rank}
    return best


def core_direction_v2(features: dict, current_direction: str | None) -> dict:
    current = _direction(current_direction)
    votes: list[dict] = []
    for rule in CORE_DIRECTION_RULES:
        value = _f(features.get(rule["feature"]))
        if value is None:
            continue
        high_direction = str(rule["high_direction"])
        predicted = (
            high_direction
            if value >= float(rule["threshold"])
            else ("LONG" if high_direction == "SHORT" else "SHORT")
        )
        votes.append({
            "feature": rule["feature"],
            "value": round(float(value), 8),
            "threshold": float(rule["threshold"]),
            "vote": predicted,
        })

    challenger = current
    if votes:
        longs = sum(row["vote"] == "LONG" for row in votes)
        shorts = len(votes) - longs
        if longs != shorts:
            challenger = "LONG" if longs > shorts else "SHORT"

    return {
        "current_direction": current,
        "challenger_direction": challenger,
        "votes": votes,
        "changed": bool(current and challenger and current != challenger),
        "enough_information": bool(votes and current),
        "tie_falls_back_to_current": True,
    }


def core_direction_v2_strict(features: dict, current_direction: str | None) -> dict:
    """Conservative prospective variant: both frozen features must exist and agree."""
    current = _direction(current_direction)
    votes: list[dict] = []
    for rule in CORE_DIRECTION_RULES:
        value = _f(features.get(rule["feature"]))
        if value is None:
            continue
        high_direction = str(rule["high_direction"])
        predicted = (
            high_direction
            if value >= float(rule["threshold"])
            else ("LONG" if high_direction == "SHORT" else "SHORT")
        )
        votes.append({
            "feature": rule["feature"],
            "value": round(float(value), 8),
            "threshold": float(rule["threshold"]),
            "vote": predicted,
        })

    enough = bool(current and len(votes) == len(CORE_DIRECTION_RULES))
    agree = bool(enough and len({row["vote"] for row in votes}) == 1)
    challenger = votes[0]["vote"] if agree else current
    return {
        "current_direction": current,
        "challenger_direction": challenger,
        "votes": votes,
        "changed": bool(current and challenger and current != challenger),
        "enough_information": enough,
        "votes_agree": agree,
        "strict_consensus": True,
        "missing_or_disagreement_falls_back_to_current": True,
    }


def build_prospective_snapshot(
    run: dict,
    features_by_ticker: dict[str, dict],
    signals: list[dict],
) -> dict:
    """Pure point-in-time snapshot builder used by runtime and validation."""
    snapshot_date = str(run.get("snapshot_date") or "")[:10]
    run_id = int(run.get("id") or 0)
    core_signals = _best_core_signals(signals)

    core_rows: list[dict] = []
    research_rejects: list[dict] = []
    for ticker, signal in sorted(core_signals.items()):
        features = dict(features_by_ticker.get(ticker) or {})
        current = _direction(signal.get("direction") or features.get("active_signal_direction"))
        if not current:
            continue

        direction_view = core_direction_v2_strict(features, current)
        core_rows.append({
            "journal_type": CORE_JOURNAL,
            "snapshot_date": snapshot_date,
            "source_scanner_run_id": run_id,
            "ticker": ticker,
            "current_direction": current,
            "challenger_direction": direction_view["challenger_direction"],
            "research_state": _state(signal),
            "research_score": _signal_score(signal),
            "research_probability_pct": _signal_probability(signal),
            "entry_price": _f(features.get("price") or features.get("current_price")),
            "metadata": {
                "rules": list(CORE_DIRECTION_RULES),
                "votes": direction_view["votes"],
                "changed": direction_view["changed"],
                "strict_consensus": True,
                "missing_or_disagreement_falls_back_to_current": True,
                "historical_loose_equals_strict_when_both_features_present": True,
            },
        })

        research_state = _state(signal)
        if research_state not in RESEARCH_PASS_STATES:
            research_rejects.append({
                "journal_type": RESEARCH_JOURNAL,
                "snapshot_date": snapshot_date,
                "source_scanner_run_id": run_id,
                "ticker": ticker,
                "current_direction": current,
                "challenger_direction": current,
                "research_state": research_state,
                "research_score": _signal_score(signal),
                "research_probability_pct": _signal_probability(signal),
                "entry_price": _f(features.get("price") or features.get("current_price")),
                "_rank": (
                    _signal_score(signal),
                    _signal_probability(signal),
                    int(STATE_PRIORITY.get(research_state, 1)),
                    ticker,
                ),
            })

    research_rows: list[dict] = []
    if research_rejects:
        top = max(research_rejects, key=lambda row: row["_rank"])
        top = dict(top)
        top.pop("_rank", None)
        top["metadata"] = {
            "selection": "TOP1_RESEARCH_HARD_REJECT",
            "pool_size": len(research_rejects),
            "existing_scores_only": True,
            "no_threshold_change": True,
            "direction_policy": "CURRENT_CORE_ONLY",
            "do_not_combine_with_core_v2": True,
            "reason": "historical 6.2.23 interaction test: CORE V2 worsened Research Top-1 close return/PF",
        }
        research_rows = [top]

    return {
        "mode": MODE,
        "scope": "PROSPECTIVE_SHADOW_ONLY",
        "live_mutation": False,
        "promotion_allowed": False,
        "snapshot_date": snapshot_date,
        "source_scanner_run_id": run_id,
        "core_direction_rows": core_rows,
        "research_override_rows": research_rows,
        "research_reject_pool": len(research_rejects),
    }


def _ensure_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prospective_architecture_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_key TEXT NOT NULL UNIQUE,
            journal_type TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            source_scanner_run_id INTEGER NOT NULL,
            layer_feature_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            current_direction TEXT NOT NULL,
            challenger_direction TEXT NOT NULL,
            research_state TEXT,
            research_score REAL,
            research_probability_pct REAL,
            entry_price REAL,
            metadata_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            matured INTEGER NOT NULL DEFAULT 0,
            best_direction TEXT,
            current_direction_correct INTEGER,
            challenger_direction_correct INTEGER,
            current_mover_5d INTEGER,
            challenger_mover_5d INTEGER,
            current_mfe_pct REAL,
            challenger_mfe_pct REAL,
            current_mae_pct REAL,
            challenger_mae_pct REAL,
            current_close_return_pct REAL,
            challenger_close_return_pct REAL,
            first_hit_current INTEGER,
            first_hit_challenger INTEGER,
            matured_at_utc TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_arch_journal_pending
            ON prospective_architecture_journal(matured,journal_type,snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_arch_journal_type
            ON prospective_architecture_journal(journal_type,snapshot_date,ticker);
        """
    )


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 0:
        return None
    return round(gains / losses, 4)


def _avg(rows: list[dict], key: str) -> float | None:
    values = [_f(row.get(key)) for row in rows]
    clean = [float(value) for value in values if value is not None]
    return round(mean(clean), 4) if clean else None


def _pct(rows: list[dict], key: str) -> float | None:
    if not rows:
        return None
    return round(100.0 * sum(bool(row.get(key)) for row in rows) / len(rows), 3)


def _core_summary(rows: list[dict]) -> dict:
    dates = len({str(row.get("snapshot_date") or "") for row in rows})
    changed = [row for row in rows if row.get("current_direction") != row.get("challenger_direction")]
    current_acc = _pct(rows, "current_direction_correct")
    challenger_acc = _pct(rows, "challenger_direction_correct")
    current_hit = _pct(rows, "current_mover_5d")
    challenger_hit = _pct(rows, "challenger_mover_5d")
    status = (
        "EVALUABLE"
        if dates >= CORE_MIN_MATURE_DATES and len(rows) >= CORE_MIN_MATURE_ROWS
        else "ACCUMULATING"
    )
    screen_pass = bool(
        status == "EVALUABLE"
        and current_acc is not None
        and challenger_acc is not None
        and current_hit is not None
        and challenger_hit is not None
        and challenger_acc > current_acc
        and challenger_hit > current_hit
        and (_avg(rows, "challenger_mfe_pct") or 0.0) >= (_avg(rows, "current_mfe_pct") or 0.0)
        and (_avg(rows, "challenger_mae_pct") or 1e9) <= (_avg(rows, "current_mae_pct") or 1e9)
    )
    return {
        "status": status,
        "sample": len(rows),
        "mature_dates": dates,
        "changed_direction": len(changed),
        "current": {
            "direction_accuracy_pct": current_acc,
            "mover_5d_pct": current_hit,
            "avg_mfe_pct": _avg(rows, "current_mfe_pct"),
            "avg_mae_pct": _avg(rows, "current_mae_pct"),
            "avg_close_return_pct": _avg(rows, "current_close_return_pct"),
        },
        "challenger": {
            "direction_accuracy_pct": challenger_acc,
            "mover_5d_pct": challenger_hit,
            "avg_mfe_pct": _avg(rows, "challenger_mfe_pct"),
            "avg_mae_pct": _avg(rows, "challenger_mae_pct"),
            "avg_close_return_pct": _avg(rows, "challenger_close_return_pct"),
        },
        "lift": {
            "direction_accuracy_pp": (
                round(challenger_acc - current_acc, 3)
                if challenger_acc is not None and current_acc is not None else None
            ),
            "mover_capture_pp": (
                round(challenger_hit - current_hit, 3)
                if challenger_hit is not None and current_hit is not None else None
            ),
        },
        "disagreement_subset": {
            "sample": len(changed),
            "current_mover_5d_pct": _pct(changed, "current_mover_5d"),
            "challenger_mover_5d_pct": _pct(changed, "challenger_mover_5d"),
            "current_avg_close_return_pct": _avg(changed, "current_close_return_pct"),
            "challenger_avg_close_return_pct": _avg(changed, "challenger_close_return_pct"),
        },
        "prospective_screen_pass": screen_pass,
        "minimum_evidence": {
            "mature_dates": CORE_MIN_MATURE_DATES,
            "mature_rows": CORE_MIN_MATURE_ROWS,
        },
    }


def _research_summary(rows: list[dict]) -> dict:
    dates = len({str(row.get("snapshot_date") or "") for row in rows})
    returns = [
        float(value) for value in (_f(row.get("current_close_return_pct")) for row in rows)
        if value is not None
    ]
    hit_rate = _pct(rows, "current_mover_5d")
    avg_mfe = _avg(rows, "current_mfe_pct")
    avg_close = _avg(rows, "current_close_return_pct")
    pf = _profit_factor(returns)
    status = "EVALUABLE" if dates >= RESEARCH_MIN_MATURE_DATES else "ACCUMULATING"
    screen_pass = bool(
        status == "EVALUABLE"
        and hit_rate is not None and hit_rate >= 40.0
        and avg_mfe is not None and avg_mfe >= TARGET_PCT
        and avg_close is not None and avg_close > 0.0
        and pf is not None and pf > 1.0
    )
    return {
        "status": status,
        "sample": len(rows),
        "mature_dates": dates,
        "mover_5d_pct": hit_rate,
        "avg_mfe_pct": avg_mfe,
        "avg_mae_pct": _avg(rows, "current_mae_pct"),
        "avg_close_return_pct": avg_close,
        "close_return_profit_factor": pf,
        "avg_first_hit_session": _avg(rows, "first_hit_current"),
        "prospective_screen_pass": screen_pass,
        "minimum_evidence": {
            "mature_dates": RESEARCH_MIN_MATURE_DATES,
            "mover_5d_pct": 40.0,
            "avg_mfe_pct": TARGET_PCT,
            "avg_close_return_pct_gt": 0.0,
            "profit_factor_gt": 1.0,
        },
    }


def _publish_summary(summary: dict) -> None:
    payload = json.dumps(summary, ensure_ascii=False, default=str)
    set_runtime_state("prospective_architecture_journal_snapshot", payload)
    try:
        out = Path(config.OUTPUT_DIR) / "prospective_architecture"
        out.mkdir(parents=True, exist_ok=True)
        (out / "latest_prospective_architecture_journal.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


def prospective_architecture_summary() -> dict:
    conn = connect(config.ALERTS_DB, live=True)
    try:
        _ensure_schema(conn)
        rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM prospective_architecture_journal WHERE matured=1 ORDER BY snapshot_date,id"
            ).fetchall()
        ]
        coverage_rows = conn.execute(
            """SELECT journal_type,matured,COUNT(*) AS row_count,
                      COUNT(DISTINCT snapshot_date) AS date_count,
                      MIN(snapshot_date) AS first_date,
                      MAX(snapshot_date) AS last_date
               FROM prospective_architecture_journal
               GROUP BY journal_type,matured
               ORDER BY journal_type,matured"""
        ).fetchall()
    finally:
        conn.close()

    journal_coverage: dict[str, dict] = {}
    for row in coverage_rows:
        journal_type = str(row["journal_type"])
        item = journal_coverage.setdefault(journal_type, {
            "captured_rows": 0,
            "captured_dates": 0,
            "pending_rows": 0,
            "pending_dates": 0,
            "matured_rows": 0,
            "matured_dates": 0,
            "first_date": None,
            "last_date": None,
        })
        count = int(row["row_count"] or 0)
        dates = int(row["date_count"] or 0)
        matured_flag = int(row["matured"] or 0)
        item["captured_rows"] += count
        item["captured_dates"] += dates
        if matured_flag:
            item["matured_rows"] += count
            item["matured_dates"] += dates
        else:
            item["pending_rows"] += count
            item["pending_dates"] += dates
        first_date = str(row["first_date"] or "") or None
        last_date = str(row["last_date"] or "") or None
        if first_date and (item["first_date"] is None or first_date < item["first_date"]):
            item["first_date"] = first_date
        if last_date and (item["last_date"] is None or last_date > item["last_date"]):
            item["last_date"] = last_date

    pending = sum(int(item["pending_rows"]) for item in journal_coverage.values())

    core = [row for row in rows if row.get("journal_type") == CORE_JOURNAL]
    research = [row for row in rows if row.get("journal_type") == RESEARCH_JOURNAL]
    return {
        "mode": MODE,
        "scope": "PROSPECTIVE_SHADOW_ONLY",
        "live_mutation": False,
        "promotion_allowed": False,
        "economic_truth": {"threshold_pct": TARGET_PCT, "horizon_sessions": HORIZON_SESSIONS},
        "core_direction_v2": _core_summary(core),
        "research_override_top1": _research_summary(research),
        "pending_rows": pending,
        "journal_coverage": journal_coverage,
        "frozen_core_rules": list(CORE_DIRECTION_RULES),
        "policy": {
            "gex_unchanged": True,
            "timing_unchanged": True,
            "fusion_unchanged": True,
            "lifecycle_unchanged": True,
            "research_hard_gate_live_unchanged": True,
            "core_direction_shadow_variant": "V2_STRICT_CONSENSUS",
            "research_override_direction_policy": "CURRENT_CORE_ONLY",
            "research_override_do_not_combine_with_core_v2": True,
            "research_override_needs_downstream_shadow_continuation_before_live": True,
            "prospective_pass_requires_manual_combined_review": True,
        },
    }


def capture_prospective_architecture_journal(
    run: dict,
    features_by_ticker: dict[str, dict],
    signals: list[dict],
) -> dict:
    """Persist one authoritative deep-close snapshot. Idempotent by run/ticker/type."""
    snapshot = build_prospective_snapshot(run, features_by_ticker, signals)
    run_id = int(snapshot.get("source_scanner_run_id") or 0)
    if run_id <= 0:
        return {**snapshot, "status": "INVALID_RUN", "inserted": 0}

    conn = connect(config.ALERTS_DB, live=True)
    inserted = 0
    try:
        _ensure_schema(conn)
        feature_rows = conn.execute(
            "SELECT id,ticker,price FROM layer_feature_history WHERE source_scanner_run_id=?",
            (run_id,),
        ).fetchall()
        feature_map = {
            str(row["ticker"]).upper(): {
                "layer_feature_id": int(row["id"]),
                "price": _f(row["price"]),
            }
            for row in feature_rows
        }
        now = ts_utc_str()
        all_rows = list(snapshot["core_direction_rows"]) + list(snapshot["research_override_rows"])
        for row in all_rows:
            ticker = str(row.get("ticker") or "").upper()
            feature = feature_map.get(ticker)
            if not feature:
                continue
            entry_price = _f(row.get("entry_price"))
            if entry_price is None:
                entry_price = feature.get("price")
            if entry_price is None or entry_price <= 0:
                continue
            key = f"{row['journal_type']}|{run_id}|{ticker}"
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO prospective_architecture_journal(
                       journal_key,journal_type,snapshot_date,source_scanner_run_id,layer_feature_id,
                       ticker,current_direction,challenger_direction,research_state,research_score,
                       research_probability_pct,entry_price,metadata_json,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key, row["journal_type"], row["snapshot_date"], run_id,
                    int(feature["layer_feature_id"]), ticker, row["current_direction"],
                    row["challenger_direction"], row.get("research_state"),
                    row.get("research_score"), row.get("research_probability_pct"),
                    float(entry_price), json.dumps(row.get("metadata") or {}, ensure_ascii=False),
                    now,
                ),
            )
            inserted += max(0, conn.total_changes - before)
        conn.commit()
    finally:
        conn.close()

    summary = prospective_architecture_summary()
    _publish_summary(summary)
    return {
        **snapshot,
        "status": "CAPTURED",
        "inserted": inserted,
        "summary": summary,
    }


def _direction_metrics(
    direction: str,
    *,
    long_mfe: float,
    short_mfe: float,
    long_mae: float,
    short_mae: float,
    long_close: float,
    first_long: int | None,
    first_short: int | None,
) -> dict:
    if direction == "SHORT":
        return {
            "mfe": short_mfe,
            "mae": short_mae,
            "close": -long_close,
            "first_hit": first_short,
        }
    return {
        "mfe": long_mfe,
        "mae": long_mae,
        "close": long_close,
        "first_hit": first_long,
    }


def mature_prospective_architecture_journal() -> dict:
    """Attach only already-complete 5-session outcomes to frozen prospective rows."""
    conn = connect(config.ALERTS_DB, live=True)
    matured = 0
    try:
        _ensure_schema(conn)
        pending = [
            dict(row) for row in conn.execute(
                """SELECT j.*,o.max_up_pct,o.max_down_pct,o.close_return_pct,
                          o.first_long_5pct_session,o.first_short_5pct_session
                   FROM prospective_architecture_journal j
                   JOIN layer_feature_outcomes o ON o.layer_feature_id=j.layer_feature_id
                   WHERE j.matured=0 AND o.complete=1
                   ORDER BY j.snapshot_date,j.id"""
            ).fetchall()
        ]
        now = ts_utc_str()
        for row in pending:
            long_mfe = max(0.0, float(_f(row.get("max_up_pct")) or 0.0))
            short_mfe = max(0.0, -float(_f(row.get("max_down_pct")) or 0.0))
            long_mae = max(0.0, -float(_f(row.get("max_down_pct")) or 0.0))
            short_mae = max(0.0, float(_f(row.get("max_up_pct")) or 0.0))
            long_close = float(_f(row.get("close_return_pct")) or 0.0)
            current = _direction(str(row.get("current_direction") or ""))
            challenger = _direction(str(row.get("challenger_direction") or ""))
            if not current or not challenger:
                continue

            current_metrics = _direction_metrics(
                current,
                long_mfe=long_mfe, short_mfe=short_mfe,
                long_mae=long_mae, short_mae=short_mae,
                long_close=long_close,
                first_long=row.get("first_long_5pct_session"),
                first_short=row.get("first_short_5pct_session"),
            )
            challenger_metrics = _direction_metrics(
                challenger,
                long_mfe=long_mfe, short_mfe=short_mfe,
                long_mae=long_mae, short_mae=short_mae,
                long_close=long_close,
                first_long=row.get("first_long_5pct_session"),
                first_short=row.get("first_short_5pct_session"),
            )
            best_direction = "LONG" if long_mfe >= short_mfe else "SHORT"

            conn.execute(
                """UPDATE prospective_architecture_journal SET
                       matured=1,best_direction=?,
                       current_direction_correct=?,challenger_direction_correct=?,
                       current_mover_5d=?,challenger_mover_5d=?,
                       current_mfe_pct=?,challenger_mfe_pct=?,
                       current_mae_pct=?,challenger_mae_pct=?,
                       current_close_return_pct=?,challenger_close_return_pct=?,
                       first_hit_current=?,first_hit_challenger=?,matured_at_utc=?
                   WHERE id=?""",
                (
                    best_direction,
                    1 if current == best_direction else 0,
                    1 if challenger == best_direction else 0,
                    1 if current_metrics["mfe"] >= TARGET_PCT else 0,
                    1 if challenger_metrics["mfe"] >= TARGET_PCT else 0,
                    round(current_metrics["mfe"], 6),
                    round(challenger_metrics["mfe"], 6),
                    round(current_metrics["mae"], 6),
                    round(challenger_metrics["mae"], 6),
                    round(current_metrics["close"], 6),
                    round(challenger_metrics["close"], 6),
                    current_metrics["first_hit"],
                    challenger_metrics["first_hit"],
                    now,
                    int(row["id"]),
                ),
            )
            matured += 1
        conn.commit()
    finally:
        conn.close()

    summary = prospective_architecture_summary()
    _publish_summary(summary)
    return {
        "status": "OK",
        "matured_now": matured,
        "summary": summary,
        "live_mutation": False,
    }
