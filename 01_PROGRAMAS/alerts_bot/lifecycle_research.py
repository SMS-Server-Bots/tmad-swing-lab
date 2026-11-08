from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

import config
from db import connect, set_runtime_state
from time_utils import ts_madrid_str


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except Exception:
        return default


def _directional_close_return(price: float, entry: float, direction: str) -> float:
    return (price / entry - 1.0) * 100.0 if direction == "long" else (entry / price - 1.0) * 100.0


def _directional_favorable(high: float, low: float, entry: float, direction: str) -> float:
    return (high / entry - 1.0) * 100.0 if direction == "long" else (entry / low - 1.0) * 100.0


def price_rows_from_layer_outcomes(rows: list[dict]) -> list[dict]:
    """Reconstruct canonical future OHLC rows from archived/live layer outcomes.

    Layer outcome ZIPs preserve the actual 1-5 session OHLC path even after the
    lighter price_cache is compacted.  This helper is deliberately pure so QA
    can replay lifecycle research without mutating production.
    """
    dedup: dict[tuple[str, str], dict] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        raw = row.get("sessions_json")
        try:
            sessions = raw if isinstance(raw, list) else json.loads(raw or "[]")
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
            open_ = _f(session.get("open"), close)
            high = _f(session.get("high"), close)
            low = _f(session.get("low"), close)
            candidate = {
                "ticker": ticker,
                "trade_date": trade_date,
                "open": open_ if open_ is not None else close,
                "high": high if high is not None else close,
                "low": low if low is not None else close,
                "close": close,
                "source": "layer_outcome_history",
            }
            key = (ticker, trade_date)
            old = dedup.get(key)
            quality = sum(candidate.get(name) is not None for name in ("open", "high", "low", "close"))
            old_quality = sum((old or {}).get(name) is not None for name in ("open", "high", "low", "close"))
            if old is None or quality >= old_quality:
                dedup[key] = candidate
    return sorted(dedup.values(), key=lambda row: (str(row["ticker"]), str(row["trade_date"])))


def merge_price_rows(primary: list[dict], fallback: list[dict]) -> list[dict]:
    """Merge OHLC sources with price_cache (primary) taking precedence."""
    merged: dict[tuple[str, str], dict] = {}
    for source_rows in (fallback, primary):
        for row in source_rows:
            ticker = str(row.get("ticker") or "").upper()
            trade_date = str(row.get("trade_date") or "")[:10]
            close = _f(row.get("close"))
            if not ticker or not trade_date or close is None or close <= 0:
                continue
            normalized = dict(row)
            normalized["ticker"] = ticker
            normalized["trade_date"] = trade_date
            normalized["close"] = close
            for name in ("open", "high", "low"):
                if _f(normalized.get(name)) is None:
                    normalized[name] = close
            merged[(ticker, trade_date)] = normalized
    return sorted(merged.values(), key=lambda row: (str(row["ticker"]), str(row["trade_date"])))


def observation_outcome_paths_for_trades(conn, trades: list[dict]) -> tuple[list[dict], dict]:
    """Load one mature longest-horizon future path for each closed trade key.

    Queries are exact ticker/date lookups backed by
    idx_outcomes_ticker_date_horizon. This reuses already-labelled future OHLC
    paths without scanning the observation outcome table or inventing prices.
    """
    if not trades:
        return [], {"requested_trade_keys": 0, "matched_trade_keys": 0, "rows": 0}

    tables = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "observation_outcomes" not in tables:
        return [], {"requested_trade_keys": 0, "matched_trade_keys": 0, "rows": 0}

    keys = sorted({
        (str(trade.get("ticker") or "").upper(), str(trade.get("entry_date") or "")[:10])
        for trade in trades
        if trade.get("ticker") and trade.get("entry_date")
    })
    matched: list[dict] = []
    for ticker, entry_date in keys:
        row = conn.execute(
            """SELECT ticker,observation_date,horizon_sessions,sessions_json
               FROM observation_outcomes
               WHERE ticker=? AND observation_date=? AND complete=1
                 AND sessions_json IS NOT NULL AND sessions_json<>''
               ORDER BY horizon_sessions DESC,id DESC
               LIMIT 1""",
            (ticker, entry_date),
        ).fetchone()
        if row is not None:
            matched.append(dict(row))
    return matched, {
        "requested_trade_keys": len(keys),
        "matched_trade_keys": len(matched),
        "rows": len(matched),
    }


def analyze_closed_trade_rows(trades: list[dict], price_rows: list[dict]) -> dict:
    """Compare actual lifecycle exits with non-lookahead fixed-hold counterfactuals.

    This is research-only. It never changes an open trade or a LIVE gate.
    """
    prices: dict[str, list[dict]] = defaultdict(list)
    for row in price_rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker and row.get("trade_date"):
            prices[ticker].append(dict(row))
    for rows in prices.values():
        rows.sort(key=lambda row: str(row.get("trade_date") or ""))

    evaluated: list[dict] = []
    for trade in trades:
        ticker = str(trade.get("ticker") or "").upper()
        direction = str(trade.get("direction") or "long").lower()
        entry_date = str(trade.get("entry_date") or "")[:10]
        exit_date = str(trade.get("exit_date") or "")[:10]
        entry = _f(trade.get("entry_price"))
        actual = _f(trade.get("exit_return_pct"))
        hold = int(trade.get("max_hold_sessions") or 0)
        if not ticker or not entry_date or not entry or entry <= 0 or hold <= 0 or actual is None:
            continue

        future = [row for row in prices.get(ticker, []) if str(row.get("trade_date") or "") > entry_date][:hold]
        if not future:
            continue
        closes: list[tuple[str, float]] = []
        best = 0.0
        target = abs(_f(trade.get("target_pct"), 5.0) or 5.0)
        target_after_exit = False
        first_target_session = None
        for index, row in enumerate(future, start=1):
            close = _f(row.get("close"))
            high = _f(row.get("high"), close)
            low = _f(row.get("low"), close)
            if close is None or high is None or low is None or close <= 0 or high <= 0 or low <= 0:
                continue
            trade_date = str(row.get("trade_date") or "")[:10]
            close_ret = _directional_close_return(close, entry, direction)
            favorable = _directional_favorable(high, low, entry, direction)
            best = max(best, favorable)
            closes.append((trade_date, close_ret))
            if favorable >= target and first_target_session is None:
                first_target_session = index
            if exit_date and trade_date > exit_date and favorable >= target:
                target_after_exit = True

        if not closes:
            continue
        hold_returns = {str(i): closes[i - 1][1] for i in (1, 2, 3, 5) if len(closes) >= i}
        horizon_return = closes[min(len(closes), hold) - 1][1]
        capture = (actual / best * 100.0) if best > 0 else None
        evaluated.append({
            "trade_id": trade.get("id"),
            "ticker": ticker,
            "direction": direction,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "exit_reason": trade.get("exit_reason"),
            "same_day_exit": bool(exit_date and exit_date == entry_date),
            "actual_exit_return_pct": round(actual, 6),
            "horizon_return_pct": round(horizon_return, 6),
            "best_favorable_pct": round(best, 6),
            "actual_capture_pct": round(capture, 3) if capture is not None else None,
            "target_after_exit": bool(target_after_exit),
            "target_reached_in_horizon": bool(first_target_session is not None),
            "first_target_session": first_target_session,
            "fixed_hold_close_returns": {k: round(v, 6) for k, v in hold_returns.items()},
        })

    reversals = [row for row in evaluated if str(row.get("exit_reason") or "").startswith("PATTERN_REVERSAL")]
    same_day = [row for row in reversals if row.get("same_day_exit")]
    post_target = [row for row in reversals if row.get("target_after_exit")]
    captures = [float(row["actual_capture_pct"]) for row in reversals if row.get("actual_capture_pct") is not None]
    actual_returns = [float(row["actual_exit_return_pct"]) for row in reversals]
    horizon_returns = [float(row["horizon_return_pct"]) for row in reversals]

    fixed: dict[str, dict] = {}
    for session in ("1", "2", "3", "5"):
        pairs = [
            (float(row["actual_exit_return_pct"]), float(row["fixed_hold_close_returns"][session]))
            for row in reversals if session in (row.get("fixed_hold_close_returns") or {})
        ]
        fixed[session] = {
            "sample": len(pairs),
            "avg_actual_return_pct": round(mean(x[0] for x in pairs), 4) if pairs else None,
            "avg_fixed_hold_return_pct": round(mean(x[1] for x in pairs), 4) if pairs else None,
            "fixed_hold_edge_pp": round(mean(x[1] - x[0] for x in pairs), 4) if pairs else None,
        }

    return {
        "mode": "SHADOW_RESEARCH_ONLY",
        "sample": len(evaluated),
        "pattern_reversal_sample": len(reversals),
        "pattern_reversal_same_day": len(same_day),
        "pattern_reversal_same_day_pct": round(len(same_day) * 100.0 / len(reversals), 3) if reversals else None,
        "target_after_pattern_reversal": len(post_target),
        "target_after_pattern_reversal_pct": round(len(post_target) * 100.0 / len(reversals), 3) if reversals else None,
        "avg_actual_reversal_return_pct": round(mean(actual_returns), 4) if actual_returns else None,
        "avg_horizon_return_pct": round(mean(horizon_returns), 4) if horizon_returns else None,
        "avg_actual_capture_pct": round(mean(captures), 3) if captures else None,
        "fixed_hold_comparison": fixed,
        "policy": {
            "live_mutation": False,
            "purpose": "measure whether soft PATTERN_REVERSAL exits destroy 1-5 session edge",
            "hard_exits_unchanged": True,
        },
        "trades": evaluated[-100:],
    }



def _load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _profit_factor(values: list[float]) -> float | None:
    positive = sum(value for value in values if value > 0)
    negative = abs(sum(value for value in values if value < 0))
    if negative <= 0:
        return None if positive <= 0 else float("inf")
    return round(positive / negative, 4)


def _normalize_historical_trade(trade: dict) -> dict | None:
    ticker = str(trade.get("ticker") or "").upper()
    direction = str(trade.get("direction") or "").upper()
    entry_date = str(trade.get("signal_date") or trade.get("entry_date") or "")[:10]
    entry = _f(trade.get("entry_price"))
    if not ticker or direction not in {"LONG", "SHORT"} or not entry_date or entry is None or entry <= 0:
        return None
    target = _f(trade.get("target_price"))
    invalidation = _f(trade.get("invalidation_price"))
    if target is not None:
        if direction == "LONG" and target <= entry:
            target = None
        elif direction == "SHORT" and target >= entry:
            target = None
    if invalidation is not None:
        if direction == "LONG" and invalidation >= entry:
            invalidation = None
        elif direction == "SHORT" and invalidation <= entry:
            invalidation = None
    actual = _f(trade.get("underlying_return_pct"), _f(trade.get("exit_return_pct")))
    return {
        "trade_id": trade.get("id"),
        "ticker": ticker,
        "direction": direction,
        "entry_date": entry_date,
        "entry_price": float(entry),
        "target_price": float(target) if target is not None else None,
        "invalidation_price": float(invalidation) if invalidation is not None else None,
        "status": str(trade.get("status") or "").upper(),
        "exit_date": str(trade.get("exit_date") or "")[:10],
        "exit_reason": str(trade.get("exit_reason") or ""),
        "actual_return_pct": actual,
        "source_scanner_run_id": trade.get("source_scanner_run_id"),
        "underlying_alpha_score": _f(trade.get("underlying_alpha_score")),
        "tradeability_score": _f(trade.get("tradeability_score")),
        "opportunity_state": trade.get("opportunity_state"),
        "entry_diagnostics": dict(trade.get("entry_diagnostics") or {}),
        "selected_snapshot": dict(trade.get("selected_snapshot") or {}),
    }


def _trusted_price_series(price_rows: list[dict]) -> tuple[dict[str, list[dict]], dict]:
    from historical_operational_validation import _trusted_reverse_path_series

    raw: dict[str, list[dict]] = defaultdict(list)
    for row in price_rows:
        ticker = str(row.get("ticker") or "").upper()
        day = str(row.get("trade_date") or "")[:10]
        close = _f(row.get("close"))
        if ticker and day and close is not None and close > 0:
            raw[ticker].append(dict(row))
    return _trusted_reverse_path_series(dict(raw))


def _future_same_segment(rows: list[dict], entry_date: str) -> list[dict]:
    ordered = sorted(rows, key=lambda row: str(row.get("trade_date") or "")[:10])
    anchor = None
    for row in ordered:
        day = str(row.get("trade_date") or "")[:10]
        if day <= entry_date:
            anchor = row
        else:
            break
    if anchor is None:
        return []
    segment = anchor.get("reverse_segment")
    return [
        row for row in ordered
        if str(row.get("trade_date") or "")[:10] > entry_date
        and row.get("reverse_segment") == segment
    ]


def _boundary_exit(trade: dict, series: dict[str, list[dict]]) -> dict:
    future = _future_same_segment(series.get(str(trade["ticker"]), []), str(trade["entry_date"]))
    target = _f(trade.get("target_price"))
    invalidation = _f(trade.get("invalidation_price"))
    direction = str(trade.get("direction") or "").upper()
    entry = float(trade["entry_price"])
    if not future:
        return {"status": "NO_PRICE_PATH"}
    if target is None and invalidation is None:
        return {"status": "NO_BOUNDARIES"}

    for sessions, row in enumerate(future, start=1):
        day = str(row.get("trade_date") or "")[:10]
        close = _f(row.get("close"))
        high = _f(row.get("high"), close)
        low = _f(row.get("low"), close)
        if close is None or high is None or low is None or close <= 0 or high <= 0 or low <= 0:
            continue

        if direction == "LONG":
            target_hit = bool(target is not None and high >= target)
            invalidation_hit = bool(invalidation is not None and low <= invalidation)
        else:
            target_hit = bool(target is not None and low <= target)
            invalidation_hit = bool(invalidation is not None and high >= invalidation)

        if target_hit and invalidation_hit:
            chosen = float(invalidation)
            return {
                "status": "RESOLVED",
                "exit_reason": "INVALIDATION_AMBIGUOUS_BAR",
                "exit_date": day,
                "exit_price": chosen,
                "sessions": sessions,
                "return_pct": round(_directional_close_return(chosen, entry, direction.lower()), 4),
                "same_bar_target_and_invalidation": True,
            }
        if invalidation_hit:
            chosen = float(invalidation)
            return {
                "status": "RESOLVED",
                "exit_reason": "THESIS_INVALIDATED",
                "exit_date": day,
                "exit_price": chosen,
                "sessions": sessions,
                "return_pct": round(_directional_close_return(chosen, entry, direction.lower()), 4),
                "same_bar_target_and_invalidation": False,
            }
        if target_hit:
            chosen = float(target)
            return {
                "status": "RESOLVED",
                "exit_reason": "MOVEMENT_COMPLETED",
                "exit_date": day,
                "exit_price": chosen,
                "sessions": sessions,
                "return_pct": round(_directional_close_return(chosen, entry, direction.lower()), 4),
                "same_bar_target_and_invalidation": False,
            }

    last = future[-1]
    last_close = _f(last.get("close"))
    return {
        "status": "OPEN_AT_HISTORY_END",
        "exit_reason": None,
        "exit_date": None,
        "exit_price": None,
        "sessions": len(future),
        "return_pct": None,
        "last_date": str(last.get("trade_date") or "")[:10],
        "last_close": last_close,
        "last_mark_return_pct": (
            round(_directional_close_return(float(last_close), entry, direction.lower()), 4)
            if last_close is not None and last_close > 0 else None
        ),
    }


def analyze_boundary_lifecycle_from_series(
    trades: list[dict],
    trusted_series: dict[str, list[dict]],
    *,
    label: str,
    trust_metadata: dict | None = None,
) -> dict:
    """Analyze lifecycle against an already trusted historical series.

    This is the canonical path for historical replay. It deliberately consumes
    the same trusted price lane used by Reverse so lifecycle cannot silently
    downgrade to the compact live price_cache.
    """
    trust = dict(trust_metadata or {})
    details: list[dict] = []
    no_path = no_boundaries = 0
    for raw in trades:
        trade = _normalize_historical_trade(raw)
        if trade is None:
            continue
        boundary = _boundary_exit(trade, trusted_series)
        if boundary.get("status") == "NO_PRICE_PATH":
            no_path += 1
            continue
        if boundary.get("status") == "NO_BOUNDARIES":
            no_boundaries += 1
            continue

        actual = _f(trade.get("actual_return_pct"))
        boundary_return = _f(boundary.get("return_pct"))
        exit_date = str(trade.get("exit_date") or "")
        boundary_date = str(boundary.get("exit_date") or "")
        details.append({
            **trade,
            "boundary_status": boundary.get("status"),
            "boundary_exit_reason": boundary.get("exit_reason"),
            "boundary_exit_date": boundary.get("exit_date"),
            "boundary_exit_price": boundary.get("exit_price"),
            "boundary_sessions": boundary.get("sessions"),
            "boundary_return_pct": boundary_return,
            "boundary_last_mark_return_pct": boundary.get("last_mark_return_pct"),
            "same_bar_target_and_invalidation": bool(boundary.get("same_bar_target_and_invalidation")),
            "boundary_after_current_exit": bool(boundary_date and exit_date and boundary_date > exit_date),
            "movement_completed_after_current_exit": bool(
                boundary.get("exit_reason") == "MOVEMENT_COMPLETED"
                and boundary_date and exit_date and boundary_date > exit_date
            ),
            "return_lift_vs_current_pp": (
                round(float(boundary_return) - float(actual), 4)
                if boundary_return is not None and actual is not None else None
            ),
        })

    resolved = [row for row in details if row.get("boundary_status") == "RESOLVED"]
    completions = [row for row in resolved if row.get("boundary_exit_reason") == "MOVEMENT_COMPLETED"]
    invalidations = [
        row for row in resolved
        if str(row.get("boundary_exit_reason") or "").startswith("THESIS_INVALIDATED")
        or row.get("boundary_exit_reason") == "INVALIDATION_AMBIGUOUS_BAR"
    ]
    unresolved = [row for row in details if row.get("boundary_status") == "OPEN_AT_HISTORY_END"]
    boundary_returns = [float(row["boundary_return_pct"]) for row in resolved if row.get("boundary_return_pct") is not None]
    current_rows = [row for row in details if row.get("actual_return_pct") is not None]
    current_returns = [float(row["actual_return_pct"]) for row in current_rows]
    matched = [
        row for row in resolved
        if row.get("boundary_return_pct") is not None and row.get("actual_return_pct") is not None
    ]
    lifts = [float(row["return_lift_vs_current_pp"]) for row in matched if row.get("return_lift_vs_current_pp") is not None]
    time_stops = [row for row in details if str(row.get("exit_reason") or "").upper() == "TIME_STOP"]
    time_stop_resolved = [row for row in time_stops if row.get("boundary_status") == "RESOLVED"]
    time_stop_lifts = [
        float(row["return_lift_vs_current_pp"])
        for row in time_stop_resolved if row.get("return_lift_vs_current_pp") is not None
    ]

    return {
        "mode": "STATEFUL_BOUNDARY_LIFECYCLE_SHADOW",
        "label": label,
        "input_trades": len(trades),
        "evaluable": len(details),
        "resolved": len(resolved),
        "movement_completed": len(completions),
        "thesis_invalidated": len(invalidations),
        "open_at_history_end": len(unresolved),
        "no_price_path": no_path,
        "no_valid_boundaries": no_boundaries,
        "avg_boundary_return_pct": round(mean(boundary_returns), 4) if boundary_returns else None,
        "boundary_profit_factor": _profit_factor(boundary_returns),
        "avg_boundary_sessions": round(mean(float(row.get("boundary_sessions") or 0) for row in resolved), 3) if resolved else None,
        "current_closed_comparable": len(current_rows),
        "avg_current_return_pct": round(mean(current_returns), 4) if current_returns else None,
        "current_profit_factor": _profit_factor(current_returns),
        "matched_resolved_with_current": len(matched),
        "avg_boundary_lift_vs_current_pp": round(mean(lifts), 4) if lifts else None,
        "movement_completed_after_current_exit": sum(1 for row in details if row.get("movement_completed_after_current_exit")),
        "time_stop_counterfactual": {
            "current_time_stop_trades": len(time_stops),
            "boundary_resolved": len(time_stop_resolved),
            # v6.1.140: "later" means strictly after the historical TIME_STOP.
            # v6.1.138/139 counted the terminal boundary class regardless of
            # whether that boundary had already been crossed before the stop.
            "later_completed": sum(
                1 for row in time_stops
                if row.get("boundary_exit_reason") == "MOVEMENT_COMPLETED"
                and bool(row.get("boundary_after_current_exit"))
            ),
            "completed_on_or_before_time_stop": sum(
                1 for row in time_stops
                if row.get("boundary_exit_reason") == "MOVEMENT_COMPLETED"
                and not bool(row.get("boundary_after_current_exit"))
            ),
            "later_invalidated": sum(
                1 for row in time_stops
                if (
                    str(row.get("boundary_exit_reason") or "").startswith("THESIS_INVALIDATED")
                    or row.get("boundary_exit_reason") == "INVALIDATION_AMBIGUOUS_BAR"
                )
                and bool(row.get("boundary_after_current_exit"))
            ),
            "invalidated_on_or_before_time_stop": sum(
                1 for row in time_stops
                if (
                    str(row.get("boundary_exit_reason") or "").startswith("THESIS_INVALIDATED")
                    or row.get("boundary_exit_reason") == "INVALIDATION_AMBIGUOUS_BAR"
                )
                and not bool(row.get("boundary_after_current_exit"))
            ),
            "still_open_at_history_end": sum(1 for row in time_stops if row.get("boundary_status") == "OPEN_AT_HISTORY_END"),
            "avg_lift_vs_time_stop_pp": round(mean(time_stop_lifts), 4) if time_stop_lifts else None,
        },
        "trusted_price_lane": {
            key: value for key, value in trust.items()
            if key not in {"invalid_ohlc_examples", "continuity_barrier_examples"}
        },
        "research_input_integrity": {
            "full_trade_details_returned": True,
            "trade_details_count": len(details),
            "research_truncation_limit": None,
        },
        "policy": {
            "live_mutation": False,
            "fixed_time_stop_removed_in_challenger": True,
            "entry_target_frozen": True,
            "entry_invalidation_frozen": True,
            "same_bar_conflict": "INVALIDATION_FIRST_CONSERVATIVE",
            "unresolved_trades_are_not_force_closed": True,
            "canonical_price_lane_reused": True,
        },
        "trades": details,
    }


def analyze_boundary_lifecycle(
    trades: list[dict],
    price_rows: list[dict],
    *,
    label: str,
) -> dict:
    """Compatibility wrapper for standalone research outside historical replay."""
    trusted_series, trust = _trusted_price_series(price_rows)
    return analyze_boundary_lifecycle_from_series(
        trades,
        trusted_series,
        label=label,
        trust_metadata=trust,
    )


def run_lifecycle_research() -> dict:
    conn = connect(config.ALERTS_DB)
    live_layer_outcomes: list[dict] = []
    observation_outcome_paths: list[dict] = []
    observation_bridge = {"requested_trade_keys": 0, "matched_trade_keys": 0, "rows": 0}
    try:
        legacy_trades = [dict(row) for row in conn.execute(
            """SELECT * FROM open_trades
               WHERE status='closed' AND exit_at_madrid IS NOT NULL
               ORDER BY id DESC LIMIT 500"""
        ).fetchall()]
        prices_cache = [dict(row) for row in conn.execute(
            """SELECT * FROM price_cache ORDER BY ticker,trade_date"""
        ).fetchall()]
        observation_outcome_paths, observation_bridge = observation_outcome_paths_for_trades(conn, legacy_trades)
        tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if {"layer_feature_history", "layer_feature_outcomes"}.issubset(tables):
            live_layer_outcomes = [dict(row) for row in conn.execute(
                """SELECT h.ticker,o.sessions_json
                   FROM layer_feature_history h
                   JOIN layer_feature_outcomes o ON o.layer_feature_id=h.id
                   WHERE o.sessions_json IS NOT NULL AND o.sessions_json<>''"""
            ).fetchall()]
    finally:
        conn.close()

    archived_rows: list[dict] = []
    try:
        from layer_archive import archived_feature_rows_with_outcomes
        archived_rows = list(archived_feature_rows_with_outcomes())
    except Exception:
        archived_rows = []

    live_prices = price_rows_from_layer_outcomes(live_layer_outcomes)
    archive_prices = price_rows_from_layer_outcomes(archived_rows)
    observation_prices = price_rows_from_layer_outcomes(observation_outcome_paths)
    for row in observation_prices:
        row["source"] = "observation_outcome_history"
    prices = merge_price_rows(prices_cache, observation_prices + archive_prices + live_prices)

    legacy_payload = analyze_closed_trade_rows(legacy_trades, prices)
    ledger_rows = _historical_alpha_ledgers()
    historical = {
        name: analyze_boundary_lifecycle(rows, prices, label=name)
        for name, rows in ledger_rows.items()
        if rows
    }
    primary = historical.get("current_policy") or {}

    payload = {
        "mode": "SHADOW_RESEARCH_ONLY_V2",
        "primary_lane": "historical_alpha_ledgers" if historical else "legacy_open_trades",
        "historical_alpha_ledgers": historical,
        "legacy_open_trades": legacy_payload,
        # Compatibility keys retained for existing dashboards/readers.
        "sample": legacy_payload.get("sample"),
        "pattern_reversal_sample": legacy_payload.get("pattern_reversal_sample"),
        "pattern_reversal_same_day": legacy_payload.get("pattern_reversal_same_day"),
        "pattern_reversal_same_day_pct": legacy_payload.get("pattern_reversal_same_day_pct"),
        "target_after_pattern_reversal": legacy_payload.get("target_after_pattern_reversal"),
        "target_after_pattern_reversal_pct": legacy_payload.get("target_after_pattern_reversal_pct"),
        "avg_actual_reversal_return_pct": legacy_payload.get("avg_actual_reversal_return_pct"),
        "avg_horizon_return_pct": legacy_payload.get("avg_horizon_return_pct"),
        "avg_actual_capture_pct": legacy_payload.get("avg_actual_capture_pct"),
        "fixed_hold_comparison": legacy_payload.get("fixed_hold_comparison") or {},
        "boundary_current_policy_sample": primary.get("evaluable", 0),
        "boundary_current_policy_resolved": primary.get("resolved", 0),
        "policy": {
            "live_mutation": False,
            "legacy_purpose": "measure whether soft PATTERN_REVERSAL exits destroy 1-5 session edge",
            "primary_purpose": "compare current exits with hold-until-invalidation-or-movement-completion on historical Alpha Swing ledgers",
            "hard_exits_unchanged": True,
            "no_live_rule_auto_promoted": True,
        },
    }
    payload["price_sources"] = {
        "price_cache_rows": len(prices_cache),
        "live_layer_price_rows": len(live_prices),
        "archive_layer_price_rows": len(archive_prices),
        "observation_outcome_paths": len(observation_outcome_paths),
        "observation_outcome_price_rows": len(observation_prices),
        "observation_bridge": observation_bridge,
        "merged_price_rows": len(prices),
    }
    payload["generated_at_madrid"] = ts_madrid_str()
    fingerprint_source = {
        "legacy_sample": payload.get("sample"),
        "historical": {
            key: {
                "input_trades": value.get("input_trades"),
                "evaluable": value.get("evaluable"),
                "resolved": value.get("resolved"),
                "movement_completed": value.get("movement_completed"),
                "thesis_invalidated": value.get("thesis_invalidated"),
                "avg_boundary_return_pct": value.get("avg_boundary_return_pct"),
                "boundary_profit_factor": value.get("boundary_profit_factor"),
                "avg_boundary_lift_vs_current_pp": value.get("avg_boundary_lift_vs_current_pp"),
            }
            for key, value in historical.items()
        },
        "price_sources": payload.get("price_sources"),
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    output_dir = Path(config.OUTPUT_DIR) / "lifecycle_research"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest_lifecycle_counterfactual.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    runtime_summary = dict(payload)
    runtime_summary["historical_alpha_ledgers"] = {
        key: {k: v for k, v in value.items() if k != "trades"}
        for key, value in historical.items()
    }
    if isinstance(runtime_summary.get("legacy_open_trades"), dict):
        runtime_summary["legacy_open_trades"] = {
            key: value for key, value in runtime_summary["legacy_open_trades"].items()
            if key != "trades"
        }
    set_runtime_state("lifecycle_research_snapshot", json.dumps(runtime_summary, ensure_ascii=False, default=str))
    set_runtime_state("lifecycle_research_fingerprint", str(payload["fingerprint"]))
    set_runtime_state("lifecycle_research_sample", str(primary.get("evaluable") or legacy_payload.get("pattern_reversal_sample") or 0))
    set_runtime_state("lifecycle_research_boundary_sample", str(primary.get("evaluable") or 0))
    return payload

