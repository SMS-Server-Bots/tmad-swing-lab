from __future__ import annotations

"""Persistent Alpha Swing book.

This is the user-facing lifecycle above TMAD's existing evidence stack:
Scanner -> CORE/GEX/Timing/Relational/FUSION -> Alpha Swing List ->
OPEN/UPDATE/CLOSE.

It deliberately does not invent a second entry model. OPEN requires the existing
FUSION decision to be ENTER + actionable. The book only persists, explains and
evaluates that decision as a swing.
"""

import json
import sqlite3
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

import config
from db import set_runtime_state

_MADRID = ZoneInfo("Europe/Madrid")
_STATE_PRIORITY = {
    "ENTER": 9, "READY": 8, "RETEST": 7, "EARLY": 6, "DISCOVERED": 5,
    "WATCH": 4, "CAPPED": 3, "DIVERGENCE": 1, "MARKET_BLOCKED": 0,
    "EXTENDED": 0, "UNKNOWN": 0,
}
_CLOSE_STATES = {"DIVERGENCE", "MARKET_BLOCKED"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ALERTS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _json(value, default=None):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, dict) else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def _f(value, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _now() -> str:
    return datetime.now(_MADRID).isoformat()


def _directional_return(price: float, entry: float, direction: str) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    return ((price / entry) - 1.0) * 100.0 if direction == "LONG" else ((entry / price) - 1.0) * 100.0


def ensure_alpha_swing_schema() -> None:
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_swing_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            status TEXT NOT NULL,
            opened_at_madrid TEXT NOT NULL,
            opened_date TEXT NOT NULL,
            open_source_scanner_run_id INTEGER NOT NULL,
            source_gex_run_id INTEGER,
            model_version_id INTEGER,
            entry_price REAL NOT NULL,
            target_price REAL,
            invalidation_price REAL,
            max_hold_sessions INTEGER NOT NULL DEFAULT 5,
            option_json TEXT,
            thesis_json TEXT,
            last_seen_at_madrid TEXT,
            last_snapshot_date TEXT,
            last_source_scanner_run_id INTEGER,
            last_price REAL,
            last_return_pct REAL,
            last_state TEXT,
            last_score REAL,
            last_update_return_pct REAL,
            target_hit INTEGER NOT NULL DEFAULT 0,
            target_hit_at_madrid TEXT,
            best_return_pct REAL,
            worst_return_pct REAL,
            sessions_seen INTEGER NOT NULL DEFAULT 0,
            closed_at_madrid TEXT,
            exit_price REAL,
            exit_return_pct REAL,
            exit_reason TEXT,
            payload_json TEXT,
            updated_at_madrid TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_swing_open
            ON alpha_swing_positions(status,ticker,direction);
        CREATE INDEX IF NOT EXISTS idx_alpha_swing_open_date
            ON alpha_swing_positions(opened_date,status);

        CREATE TABLE IF NOT EXISTS alpha_swing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            event_type TEXT NOT NULL,
            event_at_madrid TEXT NOT NULL,
            snapshot_date TEXT,
            source_scanner_run_id INTEGER,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            price REAL,
            return_pct REAL,
            alpha_score REAL,
            fusion_state TEXT,
            reason TEXT,
            payload_json TEXT,
            unique_key TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_swing_events_date
            ON alpha_swing_events(snapshot_date,event_type,ticker);
        """
    )
    conn.commit()
    conn.close()


def _signal_payload(row: dict) -> dict:
    payload = _json(row.get("payload_json"), {})
    return payload if payload else dict(row)


def _latest_fusion_states(conn: sqlite3.Connection, run_id: int) -> dict[str, dict]:
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM layer_signal_history
               WHERE source_scanner_run_id=? AND UPPER(layer)='FUSION'
               ORDER BY ticker,id""",
            (int(run_id),),
        ).fetchall()
    ]
    out: dict[str, dict] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        payload = _signal_payload(row)
        state = str(payload.get("opportunity_state") or payload.get("state") or row.get("state") or "UNKNOWN").upper()
        score = float(_f(payload.get("score") or row.get("score"), 0.0) or 0.0)
        rank = _STATE_PRIORITY.get(state, 0) * 1000.0 + score
        previous = out.get(ticker)
        if previous is None or rank > float(previous.get("_rank") or -1):
            out[ticker] = {**row, "payload": payload, "_rank": rank, "_state": state}
    return out


def _latest_features(conn: sqlite3.Connection, run_id: int) -> dict[str, dict]:
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT ticker,price,payload_json
               FROM layer_feature_history
               WHERE source_scanner_run_id=?
               ORDER BY id DESC""",
            (int(run_id),),
        ).fetchall()
    ]
    out: dict[str, dict] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker or ticker in out:
            continue
        payload = _json(row.get("payload_json"), {})
        features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
        out[ticker] = {"price": _f(row.get("price") or features.get("price"), 0.0), "features": features}
    return out


def _sessions_seen(conn: sqlite3.Connection, ticker: str, opened_date: str, current_date: str) -> int:
    if not opened_date or not current_date or current_date <= opened_date:
        return 0
    row = conn.execute(
        """SELECT COUNT(DISTINCT trade_date)
           FROM price_cache
           WHERE ticker=? AND trade_date>? AND trade_date<=?""",
        (ticker, opened_date, current_date),
    ).fetchone()
    return int((row[0] if row else 0) or 0)


def _record_event(
    conn: sqlite3.Connection,
    *,
    position_id: int | None,
    event_type: str,
    snapshot_date: str,
    source_run: int,
    ticker: str,
    direction: str,
    price: float | None,
    return_pct: float | None,
    alpha_score: float | None,
    fusion_state: str,
    reason: str,
    payload: dict,
) -> dict | None:
    key = f"{position_id or 0}|{source_run}|{event_type}|{ticker}|{direction}|{reason}"
    now = _now()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO alpha_swing_events(
               position_id,event_type,event_at_madrid,snapshot_date,source_scanner_run_id,
               ticker,direction,price,return_pct,alpha_score,fusion_state,reason,payload_json,unique_key
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            position_id, event_type, now, snapshot_date, source_run, ticker, direction,
            price, return_pct, alpha_score, fusion_state, reason,
            json.dumps(payload, ensure_ascii=False, default=str), key,
        ),
    )
    if cursor.rowcount != 1:
        return None
    return {
        "position_id": position_id,
        "event_type": event_type,
        "event_at_madrid": now,
        "snapshot_date": snapshot_date,
        "source_scanner_run_id": source_run,
        "ticker": ticker,
        "direction": direction,
        "price": price,
        "return_pct": return_pct,
        "alpha_score": alpha_score,
        "fusion_state": fusion_state,
        "reason": reason,
        "payload": payload,
    }


def _selected_by_ticker(payload: dict) -> dict[str, dict]:
    return {
        str(row.get("ticker") or "").upper(): row
        for row in (payload.get("selected") or [])
        if str(row.get("ticker") or "").strip()
    }


def _current_context(signal: dict | None, feature: dict | None) -> dict:
    signal = signal or {}
    payload = signal.get("payload") if isinstance(signal.get("payload"), dict) else _signal_payload(signal)
    features = (feature or {}).get("features") if isinstance((feature or {}).get("features"), dict) else {}
    return {
        "state": str(payload.get("opportunity_state") or payload.get("state") or signal.get("_state") or "UNKNOWN").upper(),
        "direction": str(payload.get("direction") or signal.get("direction") or "").upper(),
        "fusion_score": _f(payload.get("score") or signal.get("score"), 0.0),
        "actionable": bool(payload.get("actionable") if payload.get("actionable") is not None else signal.get("actionable")),
        "target_price": _f(payload.get("target_price") or payload.get("target_price_mid") or signal.get("target_price_mid"), None),
        "target_pct": _f(payload.get("target_pct") or signal.get("target_pct"), None),
        "invalidation_price": _f(payload.get("opposite_level_price") or payload.get("wall_support"), None),
        "max_hold_sessions": int(_f(payload.get("max_hold_sessions") or payload.get("timing_max_sessions"), 5) or 5),
        "core_score": _f(payload.get("core_score"), None),
        "core_directional_edge": _f(payload.get("core_directional_edge"), None),
        "gex_score": _f(payload.get("gex_score"), None),
        "gex_intent": payload.get("gex_intent"),
        "intraday_state": payload.get("intraday_state"),
        "market_policy": payload.get("market_policy"),
        "blockers": list(payload.get("blockers") or []),
        "rationale": payload.get("rationale") or signal.get("rationale"),
        "price": _f((feature or {}).get("price") or payload.get("price"), 0.0),
        "market_regime": features.get("market_regime"),
        "option_signal": features.get("option_signal"),
        "unusual_flow_signal": features.get("unusual_flow_signal"),
        "sector_context_score": _f(features.get("sector_etf_score") or features.get("sector_context_score"), None),
        "relationship_context_score": _f(features.get("relationship_context_score"), None),
    }


def _target_hit(direction: str, price: float, target: float | None) -> bool:
    if not target or price <= 0:
        return False
    return price >= target if direction == "LONG" else price <= target


def _valid_invalidation(direction: str, entry_price: float, invalidation: float | None) -> float | None:
    """Keep only a thesis invalidation that sits on the loss side of entry."""
    if invalidation is None or entry_price <= 0 or invalidation <= 0:
        return None
    if direction == "LONG" and invalidation < entry_price:
        return float(invalidation)
    if direction == "SHORT" and invalidation > entry_price:
        return float(invalidation)
    return None


def _invalidation_breached(direction: str, price: float, invalidation: float | None) -> bool:
    if invalidation is None or price <= 0:
        return False
    if direction == "LONG":
        return price <= invalidation
    if direction == "SHORT":
        return price >= invalidation
    return False


def _closed_economics(conn: sqlite3.Connection) -> dict:
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT exit_return_pct,best_return_pct,worst_return_pct,sessions_seen,
                      target_hit,exit_reason
               FROM alpha_swing_positions
               WHERE status='CLOSED' AND exit_return_pct IS NOT NULL
               ORDER BY id"""
        ).fetchall()
    ]
    returns = [float(row["exit_return_pct"]) for row in rows if row.get("exit_return_pct") is not None]
    positive = [value for value in returns if value > 0]
    negative = [value for value in returns if value < 0]
    flats = [value for value in returns if value == 0]
    gross_profit = sum(positive)
    gross_loss = abs(sum(negative))
    reasons: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("exit_reason") or "UNKNOWN").upper()
        reasons[reason] = reasons.get(reason, 0) + 1
    sample = len(returns)
    return {
        "sample": sample,
        "wins": len(positive),
        "losses": len(negative),
        "flat": len(flats),
        "win_rate_pct": round(100.0 * len(positive) / sample, 2) if sample else None,
        "avg_return_pct": round(sum(returns) / sample, 4) if sample else None,
        "median_return_pct": round(float(median(returns)), 4) if sample else None,
        "cumulative_return_pct": round(sum(returns), 4) if sample else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "avg_best_return_pct": round(
            sum(float(row.get("best_return_pct") or 0.0) for row in rows) / sample, 4
        ) if sample else None,
        "avg_worst_return_pct": round(
            sum(float(row.get("worst_return_pct") or 0.0) for row in rows) / sample, 4
        ) if sample else None,
        "avg_sessions": round(
            sum(float(row.get("sessions_seen") or 0.0) for row in rows) / sample, 3
        ) if sample else None,
        "target_hit_rate_pct": round(
            100.0 * sum(1 for row in rows if int(row.get("target_hit") or 0) == 1) / sample, 2
        ) if sample else None,
        "exit_reasons": reasons,
        "scope": "closed_alpha_swings_underlying_return",
        "auto_promotion": False,
    }


def process_alpha_swing_book(payload: dict) -> dict:
    """Persist Alpha swings and derive OPEN/UPDATE/CLOSE events.

    Ranking only controls the displayed watchlist. Existing open swings are
    monitored against the full latest FUSION state, so dropping out of Top 5 is
    never itself a CLOSE reason.
    """
    ensure_alpha_swing_schema()
    source_run = int(payload.get("source_scanner_run_id") or 0)
    snapshot_date = str(payload.get("asof") or "")[:10]
    selected = _selected_by_ticker(payload)
    events: list[dict] = []
    now = _now()

    conn = _connect()
    try:
        state_map = _latest_fusion_states(conn, source_run)
        feature_map = _latest_features(conn, source_run)
        open_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM alpha_swing_positions WHERE status IN ('OPEN','OPEN_RUNNER') ORDER BY id"
            ).fetchall()
        ]

        # First maintain already-open Alpha swings using the full latest FUSION map.
        for position in open_rows:
            ticker = str(position.get("ticker") or "").upper()
            direction = str(position.get("direction") or "").upper()
            signal = state_map.get(ticker)
            feature = feature_map.get(ticker)
            if not signal or not feature:
                # Missing from one Scanner run is not a trading invalidation.
                continue
            current = _current_context(signal, feature)
            price = float(current.get("price") or 0.0)
            if price <= 0:
                continue
            ret = _directional_return(price, float(position.get("entry_price") or 0.0), direction)
            sessions = _sessions_seen(conn, ticker, str(position.get("opened_date") or ""), snapshot_date)
            previous_best = _f(position.get("best_return_pct"), ret)
            previous_worst = _f(position.get("worst_return_pct"), ret)
            best = max(float(previous_best if previous_best is not None else ret), ret)
            worst = min(float(previous_worst if previous_worst is not None else ret), ret)
            current_direction = str(current.get("direction") or "").upper()
            state = str(current.get("state") or "UNKNOWN").upper()
            close_reason = None
            entry_price = float(position.get("entry_price") or 0.0)
            invalidation = _valid_invalidation(
                direction, entry_price, _f(position.get("invalidation_price"), None)
            )
            if current_direction and current_direction != direction:
                close_reason = "DIRECTION_FLIP"
            elif _invalidation_breached(direction, price, invalidation):
                close_reason = "PRICE_INVALIDATION"
            elif state in _CLOSE_STATES:
                close_reason = state
            elif sessions >= int(position.get("max_hold_sessions") or 5):
                close_reason = "TIME_STOP"

            selected_row = selected.get(ticker) or {}
            alpha_score = _f(selected_row.get("underlying_alpha_score") or selected_row.get("conviction"), current.get("fusion_score"))
            event_payload = {
                "position": position,
                "current": current,
                "selected": selected_row,
                "sessions_seen": sessions,
                "best_return_pct": round(best, 4),
                "worst_return_pct": round(worst, 4),
            }

            if close_reason:
                conn.execute(
                    """UPDATE alpha_swing_positions SET
                       status='CLOSED',last_seen_at_madrid=?,last_snapshot_date=?,
                       last_source_scanner_run_id=?,last_price=?,last_return_pct=?,last_state=?,
                       last_score=?,best_return_pct=?,worst_return_pct=?,sessions_seen=?,
                       closed_at_madrid=?,exit_price=?,exit_return_pct=?,exit_reason=?,
                       payload_json=?,updated_at_madrid=?
                       WHERE id=?""",
                    (
                        now, snapshot_date, source_run, price, ret, state, current.get("fusion_score"),
                        best, worst, sessions, now, price, ret, close_reason,
                        json.dumps(event_payload, ensure_ascii=False, default=str), now, int(position["id"]),
                    ),
                )
                event = _record_event(
                    conn, position_id=int(position["id"]), event_type="CLOSE",
                    snapshot_date=snapshot_date, source_run=source_run, ticker=ticker,
                    direction=direction, price=price, return_pct=ret, alpha_score=alpha_score,
                    fusion_state=state, reason=close_reason, payload=event_payload,
                )
                if event:
                    events.append(event)
                continue

            target = _f(position.get("target_price"), None)
            target_hit = bool(int(position.get("target_hit") or 0))
            just_hit_target = (not target_hit) and _target_hit(direction, price, target)
            last_state = str(position.get("last_state") or "")
            last_update_return = _f(position.get("last_update_return_pct"), 0.0) or 0.0
            material_return = abs(ret - last_update_return) >= float(config.POSITION_UPDATE_MIN_STEP_PCT)
            state_changed = bool(last_state and state != last_state)
            update_reason = None
            new_status = str(position.get("status") or "OPEN")
            if just_hit_target:
                update_reason = "TARGET_REACHED_RUNNER"
                new_status = "OPEN_RUNNER"
            elif state_changed:
                update_reason = "STATE_CHANGE"
            elif material_return:
                update_reason = "MATERIAL_MOVE"

            conn.execute(
                """UPDATE alpha_swing_positions SET
                   status=?,last_seen_at_madrid=?,last_snapshot_date=?,last_source_scanner_run_id=?,
                   last_price=?,last_return_pct=?,last_state=?,last_score=?,
                   target_hit=?,target_hit_at_madrid=CASE WHEN ?=1 AND target_hit_at_madrid IS NULL THEN ? ELSE target_hit_at_madrid END,
                   best_return_pct=?,worst_return_pct=?,sessions_seen=?,payload_json=?,updated_at_madrid=?
                   WHERE id=?""",
                (
                    new_status, now, snapshot_date, source_run, price, ret, state, current.get("fusion_score"),
                    1 if (target_hit or just_hit_target) else 0, 1 if just_hit_target else 0, now,
                    best, worst, sessions, json.dumps(event_payload, ensure_ascii=False, default=str), now,
                    int(position["id"]),
                ),
            )
            if update_reason:
                event = _record_event(
                    conn, position_id=int(position["id"]), event_type="UPDATE",
                    snapshot_date=snapshot_date, source_run=source_run, ticker=ticker,
                    direction=direction, price=price, return_pct=ret, alpha_score=alpha_score,
                    fusion_state=state, reason=update_reason, payload=event_payload,
                )
                if event:
                    events.append(event)
                    conn.execute(
                        "UPDATE alpha_swing_positions SET last_update_return_pct=? WHERE id=?",
                        (ret, int(position["id"])),
                    )

        # Then convert selected actionable ENTERs into Alpha OPENs.
        open_now = {
            str(row[0]).upper()
            for row in conn.execute(
                "SELECT ticker FROM alpha_swing_positions WHERE status IN ('OPEN','OPEN_RUNNER')"
            ).fetchall()
        }
        for ticker, row in selected.items():
            context = row.get("swing_context") if isinstance(row.get("swing_context"), dict) else {}
            state = str(row.get("opportunity_state") or context.get("state") or "UNKNOWN").upper()
            actionable = bool(context.get("actionable"))
            direction = str(row.get("direction") or "").upper()
            price = float(_f(row.get("spot"), 0.0) or 0.0)
            if ticker in open_now or state != "ENTER" or not actionable or direction not in {"LONG", "SHORT"} or price <= 0:
                continue
            target = _f(context.get("target_price"), None)
            invalidation = _valid_invalidation(
                direction, price, _f(context.get("invalidation_price"), None)
            )
            max_hold = int(_f(context.get("max_hold_sessions"), 5) or 5)
            thesis = {
                "rationale": context.get("rationale"),
                "core_score": context.get("core_score"),
                "core_directional_edge": context.get("core_directional_edge"),
                "gex_score": context.get("gex_score"),
                "gex_intent": context.get("gex_intent"),
                "intraday_state": context.get("intraday_state"),
                "market_regime": context.get("market_regime"),
                "market_policy": context.get("market_policy"),
                "option_signal": context.get("option_signal"),
                "unusual_flow_signal": context.get("unusual_flow_signal"),
                "blockers": context.get("blockers") or [],
            }
            cursor = conn.execute(
                """INSERT INTO alpha_swing_positions(
                   ticker,direction,status,opened_at_madrid,opened_date,open_source_scanner_run_id,
                   source_gex_run_id,model_version_id,entry_price,target_price,invalidation_price,
                   max_hold_sessions,option_json,thesis_json,last_seen_at_madrid,last_snapshot_date,
                   last_source_scanner_run_id,last_price,last_return_pct,last_state,last_score,
                   last_update_return_pct,target_hit,best_return_pct,worst_return_pct,sessions_seen,
                   payload_json,updated_at_madrid
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticker, direction, "OPEN", now, snapshot_date, source_run,
                    int(context.get("source_gex_run_id") or 0) or None,
                    int(context.get("model_version_id") or 0) or None,
                    price, target, invalidation, max_hold,
                    json.dumps(row.get("option_expression") or {}, ensure_ascii=False),
                    json.dumps(thesis, ensure_ascii=False, default=str),
                    now, snapshot_date, source_run, price, 0.0, state,
                    _f(row.get("underlying_alpha_score") or row.get("conviction"), None),
                    0.0, 0, 0.0, 0.0, 0,
                    json.dumps({"selected": row, "context": context}, ensure_ascii=False, default=str), now,
                ),
            )
            position_id = int(cursor.lastrowid)
            open_now.add(ticker)
            event = _record_event(
                conn, position_id=position_id, event_type="OPEN", snapshot_date=snapshot_date,
                source_run=source_run, ticker=ticker, direction=direction, price=price,
                return_pct=0.0, alpha_score=_f(row.get("underlying_alpha_score") or row.get("conviction"), None),
                fusion_state=state, reason="FUSION_ENTER_ACTIONABLE",
                payload={"selected": row, "context": context, "thesis": thesis},
            )
            if event:
                events.append(event)

        conn.commit()
        positions = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM alpha_swing_positions WHERE status IN ('OPEN','OPEN_RUNNER') ORDER BY opened_at_madrid,id"
            ).fetchall()
        ]
        economics = _closed_economics(conn)
    finally:
        conn.close()

    open_map = {str(row.get("ticker") or "").upper(): row for row in positions}
    for row in payload.get("selected") or []:
        ticker = str(row.get("ticker") or "").upper()
        position = open_map.get(ticker)
        if position and str(position.get("direction") or "").upper() == str(row.get("direction") or "").upper():
            row["alpha_book_status"] = str(position.get("status") or "OPEN")
            row["alpha_position_id"] = int(position.get("id") or 0)
            row["alpha_entry_price"] = position.get("entry_price")
            row["alpha_return_pct"] = position.get("last_return_pct")
        else:
            row["alpha_book_status"] = "WATCH"

    summary = {
        "schema_version": 2,
        "mode": "ALPHA_SWING_BOOK_SHADOW",
        "snapshot_date": snapshot_date,
        "source_scanner_run_id": source_run,
        "selected_count": len(payload.get("selected") or []),
        "open_count": len(positions),
        "open_positions": [
            {
                "id": row.get("id"), "ticker": row.get("ticker"), "direction": row.get("direction"),
                "status": row.get("status"), "entry_price": row.get("entry_price"),
                "last_price": row.get("last_price"), "last_return_pct": row.get("last_return_pct"),
                "target_price": row.get("target_price"), "invalidation_price": row.get("invalidation_price"),
                "sessions_seen": row.get("sessions_seen"), "max_hold_sessions": row.get("max_hold_sessions"),
            }
            for row in positions
        ],
        "events_emitted": len(events),
        "closed_economics": economics,
    }
    set_runtime_state("alpha_swing_book_snapshot", json.dumps(summary, ensure_ascii=False, default=str))
    set_runtime_state("alpha_swing_book_source_run", str(source_run))
    set_runtime_state("alpha_swing_economics", json.dumps(economics, ensure_ascii=False, default=str))
    return {"events": events, "summary": summary, "positions": positions, "payload": payload}
