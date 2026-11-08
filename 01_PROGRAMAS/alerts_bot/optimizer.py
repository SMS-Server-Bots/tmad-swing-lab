from __future__ import annotations

import csv
import html
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

import config
from db import (
    create_tuning_run,
    current_champion,
    daily_price_series,
    finish_tuning_run,
    get_active_model,
    get_training_rows,
    latest_canonical_daily_dates,
    latest_core_daily_dates,
    insert_model_version,
    schedule_model,
    activate_provisional_model,
    insert_model_feature_importance,
)
from discord_utils import log_error, log_info, send_daily
from discord_presentation import build_group
from rules import RULE_DIRECTIONS, FULL_RULE_DIRECTIONS, CORE_RULE_DIRECTIONS, evaluate_rule, exit_pattern_signal, params_for_rule
from time_utils import now_madrid, ts_madrid_str, ts_utc_str, market_session_date


RELATIONAL_COMPONENT_FIELDS = {
    "market_context": "market_component_weight",
    "sector_context": "sector_component_weight",
    "breadth_context": "breadth_component_weight",
    "leveraged_etf_context": "leveraged_component_weight",
    "dynamic_relationship_context": "dynamic_component_weight",
    "lead_lag_context": "lead_lag_component_weight",
}


def _max_alerts_per_group() -> int:
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        return int(config.PRECISION_MAX_ALERTS_PER_RUN)
    return int(config.MAX_ALERTS_PER_RUN)


def _max_alerts_per_day() -> int:
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        return int(config.PRECISION_MAX_ALERTS_PER_DAY)
    return 10**9


def _precision_portfolio_caps() -> tuple[int, int]:
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        return (
            int(config.PRECISION_MAX_SIGNALS_PER_SECTOR),
            int(config.PRECISION_MAX_SIGNALS_PER_DIRECTION),
        )
    return (
        int(config.PORTFOLIO_MAX_SIGNALS_PER_SECTOR),
        int(config.PORTFOLIO_MAX_SIGNALS_PER_DIRECTION),
    )


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _directional_move(raw_long_pct: float, direction: str) -> float:
    return raw_long_pct if direction == "long" else -raw_long_pct


def _params_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def _candidate_params(rule_name: str, base: dict, seed: str) -> list[dict]:
    """Bounded deterministic search space.

    The target remains fixed at directional ±5%. Entry filters and pattern-based
    exit policy are searched together because a good entry with a poor thesis
    invalidation policy is not a profitable strategy. v5.6.9 has no fixed
    adverse price stop.
    """
    if rule_name in CORE_RULE_DIRECTIONS:
        grids: dict[str, list[Any]] = {
            "min_score": config.GRID_MIN_SCORE,
            "min_rvol": config.CORE_GRID_RVOL,
            "core_min_momentum": config.CORE_GRID_MOMENTUM,
            "core_min_gex_change_pct": config.CORE_GRID_GEX_CHANGE_PCT,
            "core_rsi_long_min": config.CORE_GRID_RSI_LONG_MIN,
            "core_rsi_short_max": config.CORE_GRID_RSI_SHORT_MAX,
            "core_min_relative_strength": config.CORE_GRID_RELATIVE_STRENGTH,
            "core_require_flow": config.CORE_GRID_FLOW_REQUIRED,
            "target_pct": config.GRID_TARGET_PCT,
            "exit_gex_reversal_pct": config.GRID_EXIT_GEX_REVERSAL_PCT,
            "exit_pattern_score": config.GRID_EXIT_PATTERN_SCORE,
            "context_weight": config.GRID_CONTEXT_WEIGHT,
            "market_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
            "sector_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
            "breadth_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
            "leveraged_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
            "dynamic_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
            "lead_lag_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
            "min_context_score": config.CORE_GRID_CONTEXT_SCORE,
            "min_market_score": config.GRID_MIN_MARKET_SCORE,
            "min_sector_score": config.GRID_MIN_SECTOR_SCORE,
            "max_adverse_context": config.GRID_MAX_ADVERSE_CONTEXT,
            "relational_exit_score": config.GRID_RELATIONAL_EXIT_SCORE,
        }
        relevant = list(grids)
        candidates: list[dict] = [dict(base)]
        for key in relevant:
            for value in grids[key]:
                item = dict(base)
                item[key] = value
                item["enabled"] = True
                candidates.append(item)
        rng = random.Random(seed)
        hard_cap = min(config.CORE_HISTORY_MAX_CANDIDATES_PER_RULE, config.WALKFORWARD_MAX_CANDIDATES_PER_RULE)
        attempts = 0
        while len(candidates) < hard_cap and attempts < hard_cap * 20:
            attempts += 1
            item = dict(base)
            item["enabled"] = True
            for key in relevant:
                item[key] = rng.choice(grids[key])
            candidates.append(item)
        unique: dict[str, dict] = {}
        for item in candidates:
            item["target_pct"] = float(config.PATTERN_TARGET_PCT)
            item["stop_pct"] = 0.0
            item["hold_sessions"] = int(config.MAX_PATTERN_HORIZON_SESSIONS)
            item["trailing_activation_pct"] = 0.0
            item["trailing_giveback_pct"] = 0.0
            unique[_params_key(item)] = item
            if len(unique) >= hard_cap:
                break
        return list(unique.values())

    grids: dict[str, list[Any]] = {
        "min_score": config.GRID_MIN_SCORE,
        "min_rvol": config.GRID_MIN_RVOL,
        "expiry_change_pct": config.GRID_EXPIRY_CHANGE_PCT,
        "strike_change_pct": config.GRID_STRIKE_CHANGE_PCT,
        "near_wall_pct": config.GRID_NEAR_WALL_PCT,
        "break_wall_pct": config.GRID_BREAK_WALL_PCT,
        "target_pct": config.GRID_TARGET_PCT,
        "exit_gex_reversal_pct": config.GRID_EXIT_GEX_REVERSAL_PCT,
        "exit_pattern_score": config.GRID_EXIT_PATTERN_SCORE,
        "context_weight": config.GRID_CONTEXT_WEIGHT,
        "market_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
        "sector_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
        "breadth_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
        "leveraged_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
        "dynamic_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
        "lead_lag_component_weight": config.GRID_CONTEXT_COMPONENT_WEIGHT,
        "min_context_score": config.GRID_MIN_CONTEXT_SCORE,
        "min_market_score": config.GRID_MIN_MARKET_SCORE,
        "min_sector_score": config.GRID_MIN_SECTOR_SCORE,
        "max_adverse_context": config.GRID_MAX_ADVERSE_CONTEXT,
        "relational_exit_score": config.GRID_RELATIONAL_EXIT_SCORE,
    }
    relevant = [
        "min_score",
        "min_rvol",
        "expiry_change_pct",
        "target_pct",
        "exit_gex_reversal_pct",
        "exit_pattern_score",
        "context_weight",
        "market_component_weight",
        "sector_component_weight",
        "breadth_component_weight",
        "leveraged_component_weight",
        "dynamic_component_weight",
        "lead_lag_component_weight",
        "min_context_score",
        "min_market_score",
        "min_sector_score",
        "max_adverse_context",
        "relational_exit_score",
    ]
    if "WALL" in rule_name:
        relevant += ["near_wall_pct", "break_wall_pct"]
    else:
        relevant += ["strike_change_pct"]

    candidates: list[dict] = [dict(base)]
    for key in relevant:
        for value in grids[key]:
            item = dict(base)
            item[key] = value
            item["enabled"] = True
            candidates.append(item)

    rng = random.Random(seed)
    hard_cap = min(config.MAX_CANDIDATES_PER_RULE, config.WALKFORWARD_MAX_CANDIDATES_PER_RULE)
    attempts = 0
    while len(candidates) < hard_cap and attempts < hard_cap * 20:
        attempts += 1
        item = dict(base)
        item["enabled"] = True
        for key in relevant:
            item[key] = rng.choice(grids[key])
        candidates.append(item)

    unique: dict[str, dict] = {}
    for item in candidates:
        item["target_pct"] = float(config.PATTERN_TARGET_PCT)
        item["stop_pct"] = 0.0
        item["hold_sessions"] = int(config.MAX_PATTERN_HORIZON_SESSIONS)
        item["trailing_activation_pct"] = 0.0
        item["trailing_giveback_pct"] = 0.0
        unique[_params_key(item)] = item
        if len(unique) >= hard_cap:
            break
    return list(unique.values())


def _market_minutes(hhmm: str) -> int:
    hour, minute = [int(x) for x in hhmm.split(":", 1)]
    return hour * 60 + minute


def _is_close_snapshot(row: dict) -> bool:
    if row.get("is_canonical_daily_close") is not None:
        try:
            return bool(int(row.get("is_canonical_daily_close") or 0))
        except Exception:
            pass
    layer = str(row.get("observation_layer") or "").lower()
    if layer:
        return layer in {"daily_close", "core_history"}
    timestamp = row.get("scanner_captured_at_madrid") or row.get("observed_at_madrid")
    if not timestamp:
        return False
    try:
        dt = datetime.fromisoformat(str(timestamp))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(config.TIMEZONE_NAME))
        market = dt.astimezone(ZoneInfo(config.ENTRY_MARKET_TIMEZONE))
        return market.hour * 60 + market.minute >= _market_minutes(config.CLOSE_SNAPSHOT_MARKET_TIME)
    except Exception:
        return True


def _prepare_rows(rows: list[dict]) -> list[dict]:
    prepared: dict[int, dict] = {}
    for row in rows:
        try:
            obs_id = int(row.get("id"))
            features = json.loads(row.get("features_json") or "{}")
        except Exception:
            continue
        item = {**row, "features": features}
        item["is_close_snapshot"] = _is_close_snapshot(item)
        try:
            horizon = int(row.get("horizon_sessions") or 0)
        except Exception:
            horizon = 0
        existing = prepared.get(obs_id)
        if existing is None:
            item["max_complete_horizon"] = horizon
            prepared[obs_id] = item
        else:
            existing["max_complete_horizon"] = max(int(existing.get("max_complete_horizon") or 0), horizon)
        # get_training_rows returns one row per outcome horizon. The path replay
        # below only needs one copy of each observation, but requires proof that
        # the complete five-session future was available for fair comparison.
    return sorted(
        prepared.values(),
        key=lambda row: (
            str(row.get("snapshot_date") or ""),
            str(row.get("observed_at_madrid") or ""),
            int(row.get("id") or 0),
        ),
    )


def _build_close_feature_index(rows: list[dict]) -> dict[str, dict[str, dict]]:
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not row.get("is_close_snapshot"):
            continue
        ticker = str(row.get("ticker") or "")
        day = str(row.get("snapshot_date") or "")
        if not ticker or not day:
            continue
        key = (ticker, day)
        previous = latest.get(key)
        current_ts = str(row.get("observed_at_madrid") or "")
        if previous is None or current_ts >= str(previous.get("observed_at_madrid") or ""):
            latest[key] = row
    out: dict[str, dict[str, dict]] = {}
    for (ticker, day), row in latest.items():
        out.setdefault(ticker, {})[day] = row["features"]
    return out


def _simulate_trade(
    row: dict,
    rule_name: str,
    direction: str,
    params: dict,
    close_features: dict[str, dict[str, dict]],
    prices: dict[str, list[dict]],
    score: float,
) -> dict | None:
    """Replay entry and exit for no more than five sessions.

    The entry-quality label is whether MFE reaches +5%. Realised performance is
    determined by target, calibrated pattern reversal, or time exit. v5.6.9
    deliberately removes the fixed adverse price stop; adverse MAE remains
    measured for diagnostics, but it does not force an exit.
    """
    ticker = str(row.get("ticker") or "")
    entry_date = str(row.get("snapshot_date") or "")
    try:
        entry = float(row.get("current_price"))
    except Exception:
        return None
    if not ticker or not entry_date or entry <= 0:
        return None

    hold = int(config.MAX_PATTERN_HORIZON_SESSIONS)
    target = float(config.PATTERN_TARGET_PCT)
    stop = 0.0
    future = [item for item in prices.get(ticker, []) if str(item.get("trade_date")) > entry_date][:hold]
    if len(future) < hold:
        return None

    best = 0.0
    worst = 0.0
    target_hit = False
    exit_return: float | None = None
    exit_reason: str | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    soft_exit_streak = 0

    for index, day in enumerate(future, start=1):
        try:
            close = float(day.get("close"))
            high = float(day.get("high") if day.get("high") is not None else close)
            low = float(day.get("low") if day.get("low") is not None else close)
        except Exception:
            return None
        raw_close = (close / entry - 1.0) * 100.0
        close_ret = _directional_move(raw_close, direction)
        if direction == "long":
            favorable = (high / entry - 1.0) * 100.0
            adverse = (low / entry - 1.0) * 100.0
        else:
            favorable = -(low / entry - 1.0) * 100.0
            adverse = -(high / entry - 1.0) * 100.0
        best = max(best, favorable)
        worst = min(worst, adverse)
        target_hit = target_hit or favorable >= target

        if favorable >= target:
            exit_return = target
            exit_reason = "TARGET_5PCT"
            exit_date = str(day.get("trade_date"))
            exit_price = entry * (1.0 + target / 100.0 if direction == "long" else 1.0 - target / 100.0)
            break

        day_features = (close_features.get(ticker) or {}).get(str(day.get("trade_date")))
        if day_features:
            signal = exit_pattern_signal(direction, day_features, params, close_ret, best)
            if signal:
                if bool(signal.get("hard")):
                    soft_exit_streak = int(config.SOFT_EXIT_CONFIRMATION_RUNS)
                else:
                    soft_exit_streak += 1
                if soft_exit_streak >= int(config.SOFT_EXIT_CONFIRMATION_RUNS):
                    exit_return = close_ret
                    exit_reason = str(signal.get("reason") or "PATTERN_REVERSAL")
                    exit_date = str(day.get("trade_date"))
                    exit_price = close
                    break
            else:
                soft_exit_streak = 0
        if index >= hold:
            exit_return = close_ret
            exit_reason = "MAX_5_SESSIONS"
            exit_date = str(day.get("trade_date"))
            exit_price = close
            break

    if exit_return is None or exit_date is None:
        return None
    return {
        "observation_id": int(row.get("id") or 0),
        "source_scanner_run_id": int(row.get("source_scanner_run_id") or 0),
        "entry_at": str(row.get("observed_at_madrid") or ""),
        "entry_date": entry_date,
        "exit_date": exit_date,
        "ticker": ticker,
        "rule_name": rule_name,
        "direction": direction,
        "score": round(float(score), 4),
        "entry_price": entry,
        "exit_price": exit_price,
        "return_pct": round(float(exit_return), 6),
        "mfe_pct": round(float(best), 6),
        "mae_pct": round(float(worst), 6),
        "target_5pct_hit": bool(target_hit),
        "exit_reason": exit_reason,
        "hold_sessions": hold,
        "stop_pct": stop,
        "target_pct": target,
        "sector_etf": (row.get("features") or {}).get("sector_etf"),
        "market_regime": (row.get("features") or {}).get("market_regime"),
        "graph_context_score": (row.get("features") or {}).get("graph_context_score"),
    }


def _group_key(row: dict, mode: str) -> tuple[str, int]:
    if mode == "intraday":
        return (str(row.get("snapshot_date") or ""), int(row.get("source_scanner_run_id") or 0))
    return (str(row.get("snapshot_date") or ""), 0)


def _rule_trades(
    rule_name: str,
    params: dict,
    rows: list[dict],
    allowed_dates: set[str],
    close_features: dict[str, dict[str, dict]],
    prices: dict[str, list[dict]],
    mode: str = "daily_close",
) -> list[dict]:
    if params.get("enabled") is False:
        return []
    direction = RULE_DIRECTIONS[rule_name]
    groups: dict[tuple[str, int], list[tuple[float, dict]]] = defaultdict(list)
    for row in rows:
        day = str(row.get("snapshot_date") or "")
        if day not in allowed_dates:
            continue
        if mode == "daily_close" and not row.get("is_close_snapshot"):
            continue
        if mode == "intraday" and row.get("is_close_snapshot"):
            continue
        candidate = evaluate_rule(rule_name, row["features"], params, mode=mode)
        if not candidate:
            continue
        groups[_group_key(row, mode)].append((float(candidate.get("score") or 0.0), row))

    blocked_until: dict[str, str] = {}
    trades: list[dict] = []
    daily_counts: Counter[str] = Counter()
    for key in sorted(groups):
        accepted = 0
        group_day = str(key[0])
        for score, row in sorted(groups[key], key=lambda item: item[0], reverse=True):
            if accepted >= _max_alerts_per_group() or daily_counts[group_day] >= _max_alerts_per_day():
                break
            ticker = str(row.get("ticker") or "")
            day = str(row.get("snapshot_date") or "")
            if not ticker or blocked_until.get(ticker, "") >= day:
                continue
            trade = _simulate_trade(row, rule_name, direction, params, close_features, prices, score)
            if not trade:
                continue
            trades.append(trade)
            blocked_until[ticker] = str(trade["exit_date"])
            accepted += 1
            daily_counts[group_day] += 1
    return trades


def _portfolio_limits(selected_params: dict[str, dict]) -> tuple[int, int]:
    policy = selected_params.get("_portfolio") or {}
    default_sector, default_direction = _precision_portfolio_caps()
    max_sector = int(policy.get("max_signals_per_sector", default_sector))
    max_direction = int(policy.get("max_signals_per_direction", default_direction))
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        max_sector = min(max_sector, int(config.PRECISION_MAX_SIGNALS_PER_SECTOR))
        max_direction = min(max_direction, int(config.PRECISION_MAX_SIGNALS_PER_DIRECTION))
    return max(1, max_sector), max(1, max_direction)


def _portfolio_trades(
    selected_params: dict[str, dict],
    rows: list[dict],
    allowed_dates: set[str],
    close_features: dict[str, dict[str, dict]],
    prices: dict[str, list[dict]],
    mode: str = "daily_close",
) -> list[dict]:
    active_rule_names = [name for name in selected_params if name in RULE_DIRECTIONS]
    rule_order = {name: index for index, name in enumerate(active_rule_names)}
    groups: dict[tuple[str, int], list[tuple[float, int, str, str, dict, dict]]] = defaultdict(list)
    for row in rows:
        day = str(row.get("snapshot_date") or "")
        if day not in allowed_dates:
            continue
        if mode == "daily_close" and not row.get("is_close_snapshot"):
            continue
        if mode == "intraday" and row.get("is_close_snapshot"):
            continue
        best: tuple[float, int, str, str, dict, dict] | None = None
        for rule_name in active_rule_names:
            direction = RULE_DIRECTIONS[rule_name]
            params = selected_params.get(rule_name) or {}
            if params.get("enabled") is False:
                continue
            candidate = evaluate_rule(rule_name, row["features"], params, mode=mode)
            if not candidate:
                continue
            item = (
                float(candidate.get("score") or 0.0),
                -rule_order[rule_name],
                rule_name,
                direction,
                params,
                row,
            )
            if best is None or (item[0], item[1]) > (best[0], best[1]):
                best = item
        if best:
            groups[_group_key(row, mode)].append(best)

    blocked_until: dict[str, str] = {}
    trades: list[dict] = []
    daily_counts: Counter[str] = Counter()
    max_sector, max_direction = _portfolio_limits(selected_params)
    for key in sorted(groups):
        accepted = 0
        group_day = str(key[0])
        sector_counts: Counter[str] = Counter()
        direction_counts: Counter[str] = Counter()
        for score, _order, rule_name, direction, params, row in sorted(
            groups[key], key=lambda item: (item[0], item[1]), reverse=True
        ):
            if accepted >= _max_alerts_per_group() or daily_counts[group_day] >= _max_alerts_per_day():
                break
            ticker = str(row.get("ticker") or "")
            day = str(row.get("snapshot_date") or "")
            if not ticker or blocked_until.get(ticker, "") >= day:
                continue
            sector = str((row.get("features") or {}).get("sector_etf") or "UNMAPPED")
            if sector_counts[sector] >= max_sector or direction_counts[direction] >= max_direction:
                continue
            trade = _simulate_trade(row, rule_name, direction, params, close_features, prices, score)
            if not trade:
                continue
            trades.append(trade)
            blocked_until[ticker] = str(trade["exit_date"])
            sector_counts[sector] += 1
            direction_counts[direction] += 1
            accepted += 1
            daily_counts[group_day] += 1
    return trades


# ---------------------------------------------------------------------------
# Robust metrics and purged walk-forward
# ---------------------------------------------------------------------------

def _bootstrap_ci(daily_returns: list[float], seed: str) -> tuple[float | None, float | None]:
    if len(daily_returns) < 3:
        return None, None
    rng = random.Random(seed)
    samples: list[float] = []
    iterations = max(100, int(config.WALKFORWARD_BOOTSTRAP_ITERATIONS))
    for _ in range(iterations):
        draw = [rng.choice(daily_returns) for _ in daily_returns]
        samples.append(mean(draw))
    samples.sort()
    lo = samples[max(0, int(len(samples) * 0.05) - 1)]
    hi = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    return lo, hi


def _metrics(trades: list[dict], with_ci: bool = False, seed: str = "tmad") -> dict[str, Any]:
    if not trades:
        return {
            "sample": 0,
            "market_dates": 0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "weighted_avg_daily_return": 0.0,
            "win_rate": 0.0,
            "target_hit_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "positive_day_rate": 0.0,
            "worst_day": 0.0,
            "avg_trades_per_day": 0.0,
            "precision_first_penalty": 0.0,
            "objective": -999.0,
            "mean_daily_return_ci90_low": None,
            "mean_daily_return_ci90_high": None,
        }
    returns = [float(item["return_pct"]) for item in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else (5.0 if gross_win > 0 else 0.0)
    by_day: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        by_day[str(trade["entry_date"])].append(float(trade["return_pct"]))
    sorted_days = sorted(by_day)
    daily_returns = [mean(by_day[day]) for day in sorted_days]
    half_life = max(1.0, float(config.RECENCY_HALF_LIFE_SESSIONS))
    recency_weights = [0.5 ** ((len(daily_returns) - 1 - idx) / half_life) for idx in range(len(daily_returns))]
    weighted_avg_daily = (
        sum(value * weight for value, weight in zip(daily_returns, recency_weights)) / sum(recency_weights)
        if recency_weights else 0.0
    )
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in daily_returns:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    avg_ret = mean(returns)
    med_ret = median(returns)
    avg_daily = mean(daily_returns)
    win_rate = len(wins) / len(returns) * 100.0
    hit_rate = sum(1 for item in trades if item.get("target_5pct_hit")) / len(trades) * 100.0
    positive_day_rate = sum(1 for value in daily_returns if value > 0) / len(daily_returns) * 100.0
    worst_day = min(daily_returns)
    concentration = max(len(values) for values in by_day.values()) / len(trades)
    avg_trades_per_day = len(trades) / max(1, len(daily_returns))
    frequency_penalty = 0.0
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        frequency_penalty = 0.25 * max(0.0, avg_trades_per_day - config.PRECISION_FIRST_TARGET_ALERTS_PER_DAY)
    sample_factor = min(1.0, math.sqrt(len(trades) / max(1.0, float(config.WALKFORWARD_TARGET_SAMPLE))))
    date_factor = min(1.0, math.sqrt(len(daily_returns) / max(1.0, float(config.WALKFORWARD_TARGET_OOS_DATES))))
    # Equal-weighting by date limits the false confidence caused by hundreds of
    # correlated tickers on the same market day. Drawdown, concentration and
    # worst-day penalties prevent a configuration from winning on one lucky day.
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        # Precision-first intentionally accepts days with no alert. Win rate,
        # target quality, confidence and concentration matter more than raw
        # signal count; excessive daily frequency is explicitly penalized.
        raw_objective = (
            weighted_avg_daily
            + 0.035 * (win_rate - 55.0)
            + 0.025 * (hit_rate - 25.0)
            + 0.40 * math.log(max(0.20, min(5.0, pf)))
            - 0.12 * max_dd
            - 0.07 * abs(min(0.0, worst_day))
            - 0.55 * concentration
            - frequency_penalty
        )
    else:
        raw_objective = (
            weighted_avg_daily
            + 0.018 * (win_rate - 50.0)
            + 0.020 * (hit_rate - 20.0)
            + 0.35 * math.log(max(0.20, min(5.0, pf)))
            - 0.10 * max_dd
            - 0.05 * abs(min(0.0, worst_day))
            - 0.45 * concentration
        )
    # Under precision-first, cross-sectional trade count is not a reward: five
    # correlated alerts on one day do not provide five independent pieces of
    # evidence. Reliability grows with independent market dates; the minimum
    # trade count remains a separate promotion condition.
    objective = (raw_objective * date_factor) if config.ALERT_SELECTION_PROFILE == "precision_first" else (raw_objective * sample_factor * date_factor)
    ci_low = ci_high = None
    if with_ci:
        ci_low, ci_high = _bootstrap_ci(daily_returns, seed)
    return {
        "sample": len(trades),
        "market_dates": len(daily_returns),
        "avg_return": avg_ret,
        "median_return": med_ret,
        "avg_daily_return": avg_daily,
        "weighted_avg_daily_return": weighted_avg_daily,
        "recency_half_life_sessions": half_life,
        "win_rate": win_rate,
        "target_hit_rate": hit_rate,
        "profit_factor": pf,
        "max_drawdown": max_dd,
        "positive_day_rate": positive_day_rate,
        "worst_day": worst_day,
        "concentration": concentration,
        "avg_trades_per_day": avg_trades_per_day,
        "precision_first_penalty": frequency_penalty,
        "objective": objective,
        "mean_daily_return_ci90_low": ci_low,
        "mean_daily_return_ci90_high": ci_high,
    }


def _filter_trades(trades: list[dict], dates: set[str]) -> list[dict]:
    return [trade for trade in trades if str(trade.get("entry_date")) in dates]


def _best_candidate(
    rule_name: str,
    candidates: list[dict],
    cache: dict[str, list[dict]],
    train_dates: set[str],
) -> tuple[dict, dict]:
    best_params: dict | None = None
    best_metrics: dict | None = None
    for params in candidates:
        trades = _filter_trades(cache[_params_key(params)], train_dates)
        metrics = _metrics(trades)
        if metrics["sample"] < config.WALKFORWARD_MIN_RULE_TRADES:
            continue
        if best_metrics is None or metrics["objective"] > best_metrics["objective"]:
            best_metrics = metrics
            best_params = params
    if best_params is None:
        disabled = dict(candidates[0] if candidates else {})
        disabled["enabled"] = False
        return disabled, _metrics([])
    # A negative robust objective means the safest calibrated rule is no trade.
    if float(best_metrics["objective"]) <= config.WALKFORWARD_MIN_RULE_OBJECTIVE:
        disabled = dict(best_params)
        disabled["enabled"] = False
        return disabled, best_metrics
    enabled = dict(best_params)
    enabled["enabled"] = True
    return enabled, best_metrics


def _ranked_candidate_options(
    candidates: list[dict],
    cache: dict[str, list[dict]],
    train_dates: set[str],
    limit: int,
) -> list[dict]:
    ranked: list[tuple[float, int, dict]] = []
    for params in candidates:
        metrics = _metrics(_filter_trades(cache[_params_key(params)], train_dates))
        if metrics["sample"] < config.WALKFORWARD_MIN_RULE_TRADES:
            continue
        ranked.append((float(metrics["objective"]), int(metrics["sample"]), params))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    options = [dict(item[2]) for item in ranked[:max(1, int(limit))]]
    if candidates:
        disabled = dict(candidates[0])
        disabled["enabled"] = False
        options.append(disabled)
    unique: dict[str, dict] = {}
    for item in options:
        unique[_params_key(item)] = item
    return list(unique.values())


def _portfolio_from_cached_rules(
    selected: dict[str, dict],
    candidate_cache: dict[str, dict[str, list[dict]]],
    allowed_dates: set[str],
) -> list[dict]:
    """Fast portfolio approximation for joint parameter search.

    Candidate trade paths were already simulated with target/pattern-reversal. This
    layer resolves collisions across rules, directions and sector clusters. The
    final OOS replay still uses _portfolio_trades from raw observations.
    """
    pool: list[dict] = []
    max_sector, max_direction = _portfolio_limits(selected)
    for rule_name, params in selected.items():
        if rule_name not in RULE_DIRECTIONS:
            continue
        if params.get("enabled") is False:
            continue
        trades = candidate_cache.get(rule_name, {}).get(_params_key(params), [])
        pool.extend(_filter_trades(trades, allowed_dates))
    by_day: dict[str, list[dict]] = defaultdict(list)
    for trade in pool:
        by_day[str(trade.get("entry_date") or "")].append(trade)
    blocked_until: dict[str, str] = {}
    accepted_trades: list[dict] = []
    for day in sorted(by_day):
        sector_counts: Counter[str] = Counter()
        direction_counts: Counter[str] = Counter()
        seen_tickers: set[str] = set()
        accepted = 0
        ordered = sorted(
            by_day[day],
            key=lambda item: (
                float(item.get("score") or 0.0),
                float(item.get("graph_context_score") or 0.0)
                * (1.0 if item.get("direction") == "long" else -1.0),
            ),
            reverse=True,
        )
        for trade in ordered:
            if accepted >= min(_max_alerts_per_group(), _max_alerts_per_day()):
                break
            ticker = str(trade.get("ticker") or "")
            direction = str(trade.get("direction") or "")
            sector = str(trade.get("sector_etf") or "UNMAPPED")
            if not ticker or ticker in seen_tickers or blocked_until.get(ticker, "") >= day:
                continue
            if sector_counts[sector] >= max_sector:
                continue
            if direction_counts[direction] >= max_direction:
                continue
            accepted_trades.append(trade)
            seen_tickers.add(ticker)
            sector_counts[sector] += 1
            direction_counts[direction] += 1
            blocked_until[ticker] = str(trade.get("exit_date") or day)
            accepted += 1
    return accepted_trades


def _joint_policy_select(
    initial: dict[str, dict],
    candidate_pools: dict[str, list[dict]],
    candidate_cache: dict[str, dict[str, list[dict]]],
    train_dates: set[str],
) -> tuple[dict[str, dict], dict]:
    """Coordinate-descent search of the complete entry/exit portfolio policy.

    Per-rule tuning supplies strong candidates; this second stage chooses their
    combination jointly so correlated sector signals and rule collisions are not
    optimized as if they were independent trades.
    """
    selected = {name: dict(params) for name, params in initial.items()}
    best_metrics = _metrics(_portfolio_from_cached_rules(selected, candidate_cache, train_dates))
    passes = max(1, int(config.JOINT_POLICY_COORDINATE_PASSES))
    for _ in range(passes):
        changed = False
        for rule_name in candidate_pools:
            options = _ranked_candidate_options(
                candidate_pools[rule_name],
                candidate_cache[rule_name],
                train_dates,
                config.JOINT_POLICY_TOP_CANDIDATES,
            )
            local_best = selected.get(rule_name, {})
            local_metrics = best_metrics
            for option in options:
                trial = {name: dict(params) for name, params in selected.items()}
                trial[rule_name] = dict(option)
                metrics = _metrics(_portfolio_from_cached_rules(trial, candidate_cache, train_dates))
                if metrics["objective"] > local_metrics["objective"] + 1e-12:
                    local_best = dict(option)
                    local_metrics = metrics
            if _params_key(local_best) != _params_key(selected.get(rule_name, {})):
                selected[rule_name] = local_best
                best_metrics = local_metrics
                changed = True
        if not changed:
            break

    # Portfolio concentration is calibrated jointly rather than imposed by hand.
    precision_sector, precision_direction = _precision_portfolio_caps()
    current_policy = selected.get("_portfolio") or {
        "max_signals_per_sector": precision_sector,
        "max_signals_per_direction": precision_direction,
    }
    best_policy = dict(current_policy)
    sector_grid = config.GRID_PORTFOLIO_MAX_SIGNALS_PER_SECTOR
    direction_grid = config.GRID_PORTFOLIO_MAX_SIGNALS_PER_DIRECTION
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        sector_grid = [value for value in sector_grid if value <= config.PRECISION_MAX_SIGNALS_PER_SECTOR] or [config.PRECISION_MAX_SIGNALS_PER_SECTOR]
        direction_grid = [value for value in direction_grid if value <= config.PRECISION_MAX_SIGNALS_PER_DIRECTION] or [config.PRECISION_MAX_SIGNALS_PER_DIRECTION]
    for max_sector in sector_grid:
        for max_direction in direction_grid:
            trial = {name: dict(params) for name, params in selected.items() if name != "_portfolio"}
            trial["_portfolio"] = {
                "max_signals_per_sector": int(max_sector),
                "max_signals_per_direction": int(max_direction),
            }
            metrics = _metrics(_portfolio_from_cached_rules(trial, candidate_cache, train_dates))
            if metrics["objective"] > best_metrics["objective"] + 1e-12:
                best_policy = dict(trial["_portfolio"])
                best_metrics = metrics
    selected["_portfolio"] = best_policy
    return selected, best_metrics


def _component_disabled_params(selected: dict[str, dict], component_field: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rule_name, params in selected.items():
        item = dict(params)
        if rule_name != "_portfolio":
            item[component_field] = 0.0
        out[rule_name] = item
    return out


def _context_disabled_params(selected: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rule_name, params in selected.items():
        if rule_name == "_portfolio":
            out[rule_name] = dict(params)
            continue
        item = dict(params)
        item.update({
            "context_weight": 0.0,
            "min_context_score": -999.0,
            "min_market_score": -999.0,
            "min_sector_score": -999.0,
            "max_adverse_context": 999.0,
            "relational_exit_score": 999.0,
        })
        out[rule_name] = item
    return out


def _build_folds(dates: list[str]) -> list[dict]:
    purge = max(config.MAX_PATTERN_HORIZON_SESSIONS, config.WALKFORWARD_PURGE_SESSIONS)
    folds: list[dict] = []
    for test_index in range(config.WALKFORWARD_MIN_TRAIN_DATES + purge, len(dates)):
        train_end = test_index - purge
        train_start = max(0, train_end - config.WALKFORWARD_MAX_TRAIN_DATES)
        train_dates = dates[train_start:train_end]
        if len(train_dates) < config.WALKFORWARD_MIN_TRAIN_DATES:
            continue
        folds.append({
            "fold": len(folds) + 1,
            "train_dates": train_dates,
            "purged_dates": dates[train_end:test_index],
            "test_date": dates[test_index],
        })
    if len(folds) > config.WALKFORWARD_MAX_FOLDS:
        folds = folds[-config.WALKFORWARD_MAX_FOLDS:]
        for index, fold in enumerate(folds, start=1):
            fold["fold"] = index
    return folds


def _param_stability(fold_params: list[dict[str, dict]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    keys = [
        "enabled", "min_score", "min_rvol", "expiry_change_pct", "strike_change_pct",
        "near_wall_pct", "break_wall_pct", "stop_pct", "hold_sessions",
        "exit_gex_reversal_pct", "exit_pattern_score", "trailing_activation_pct",
        "trailing_giveback_pct", "context_weight", "market_component_weight", "sector_component_weight",
        "breadth_component_weight", "leveraged_component_weight", "dynamic_component_weight", "lead_lag_component_weight", "min_context_score",
        "min_market_score", "min_sector_score", "max_adverse_context",
        "relational_exit_score", "core_min_momentum", "core_min_gex_change_pct",
        "core_rsi_long_min", "core_rsi_short_max", "core_min_relative_strength",
        "core_require_flow",
    ]
    rule_names = sorted({name for fold in fold_params for name in fold if name in RULE_DIRECTIONS})
    for rule_name in rule_names:
        rule_result: dict[str, Any] = {}
        for key in keys:
            values = [json.dumps(params.get(rule_name, {}).get(key), sort_keys=True) for params in fold_params]
            if not values:
                continue
            counter = Counter(values)
            value, count = counter.most_common(1)[0]
            try:
                decoded = json.loads(value)
            except Exception:
                decoded = value
            rule_result[key] = {
                "mode": decoded,
                "frequency_pct": count / len(values) * 100.0,
                "distinct_values": len(counter),
            }
        result[rule_name] = rule_result
    return result


def _tune_intraday_final(
    final_params: dict[str, dict],
    rows: list[dict],
    dates: list[str],
    folds: list[dict],
    close_features: dict[str, dict[str, dict]],
    prices: dict[str, list[dict]],
) -> tuple[dict[str, dict], dict[str, Any]]:
    """Calibrate how early the daily signature may be acted on.

    This second stage uses only actual hourly observations. If their walk-forward
    sample is insufficient, the daily configuration remains calibrated but the
    intraday trigger stays conservative and explicitly marked as fallback.
    """
    tuned = {name: dict(params) for name, params in final_params.items()}
    summary: dict[str, Any] = {"status": "fallback", "rules": {}, "oos": _metrics([])}
    any_validated = False
    all_oos: list[dict] = []

    for rule_name, base in list(tuned.items()):
        if rule_name not in RULE_DIRECTIONS:
            continue
        if base.get("enabled") is False:
            summary["rules"][rule_name] = {"status": "disabled"}
            continue
        combos: list[dict] = []
        for factor in config.GRID_INTRADAY_THRESHOLD_FACTOR:
            for projection in config.GRID_MIN_DAILY_PATTERN_PROJECTION:
                item = dict(base)
                item["intraday_threshold_factor"] = factor
                item["min_daily_pattern_projection"] = projection
                combos.append(item)
        cache: dict[str, list[dict]] = {}
        all_dates = set(dates)
        for item in combos:
            cache[_params_key(item)] = _rule_trades(
                rule_name, item, rows, all_dates, close_features, prices, mode="intraday"
            )
        rule_oos: list[dict] = []
        fold_choices: list[dict] = []
        for fold in folds:
            selected, _train_metrics = _best_candidate(
                rule_name, combos, cache, set(fold["train_dates"])
            )
            fold_choices.append(selected)
            if selected.get("enabled") is False:
                continue
            rule_oos.extend(_filter_trades(cache[_params_key(selected)], {fold["test_date"]}))
        oos_metrics = _metrics(rule_oos, with_ci=True, seed=f"intraday-{rule_name}")
        final_choice, final_metrics = _best_candidate(rule_name, combos, cache, set(dates))
        validated = (
            oos_metrics["sample"] >= config.MIN_INTRADAY_TRIGGER_SAMPLES
            and oos_metrics["market_dates"] >= config.MIN_INTRADAY_TRIGGER_DATES
            and oos_metrics["profit_factor"] >= config.MIN_INTRADAY_TRIGGER_PROFIT_FACTOR
            and oos_metrics["avg_return"] > 0
        )
        if validated and final_choice.get("enabled") is not False:
            tuned[rule_name].update({
                "intraday_threshold_factor": final_choice["intraday_threshold_factor"],
                "min_daily_pattern_projection": final_choice["min_daily_pattern_projection"],
                "intraday_calibrated": True,
            })
            any_validated = True
            all_oos.extend(rule_oos)
            status = "validated"
        else:
            tuned[rule_name].update({
                "intraday_threshold_factor": config.UNVALIDATED_INTRADAY_THRESHOLD_FACTOR,
                "min_daily_pattern_projection": config.UNVALIDATED_MIN_DAILY_PATTERN_PROJECTION,
                "intraday_calibrated": False,
            })
            status = "fallback"
        summary["rules"][rule_name] = {
            "status": status,
            "selected": {
                "intraday_threshold_factor": tuned[rule_name]["intraday_threshold_factor"],
                "min_daily_pattern_projection": tuned[rule_name]["min_daily_pattern_projection"],
            },
            "full_history": final_metrics,
            "walk_forward_oos": oos_metrics,
        }
    summary["status"] = "validated" if any_validated else "fallback"
    summary["oos"] = _metrics(all_oos, with_ci=True, seed="intraday-global")
    return tuned, summary


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _by_rule(trades: list[dict]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for rule_name in sorted({str(item.get("rule_name")) for item in trades if item.get("rule_name")}):
        subset = [item for item in trades if item.get("rule_name") == rule_name]
        result[rule_name] = _metrics(subset, with_ci=False)
    return result


def _write_walkforward_report(
    model_key: str,
    dates: list[str],
    folds_payload: list[dict],
    oos_trades: list[dict],
    baseline_trades: list[dict],
    final_params: dict[str, dict],
    summary: dict,
    stability: dict,
    intraday_summary: dict,
    calibration_layer: str = "full",
) -> str:
    stamp = now_madrid().strftime("%Y%m%d_%H%M%S")
    output = config.OUTPUT_DIR / f"{calibration_layer}_walkforward_{stamp}"
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_key": model_key,
        "generated_at_madrid": now_madrid().isoformat(),
        "method": f"purged_expanding_walk_forward_relational_precision_first_{calibration_layer}",
        "target_pct": config.PATTERN_TARGET_PCT,
        "max_horizon_sessions": config.MAX_PATTERN_HORIZON_SESSIONS,
        "market_dates": dates,
        "summary": summary,
        "by_rule": _by_rule(oos_trades),
        "baseline": _metrics(baseline_trades, with_ci=True, seed="baseline"),
        "parameter_stability": stability,
        "intraday": intraday_summary,
        "effective_rules": final_params,
        "folds": folds_payload,
        "assumptions": {
            "no_lookahead": "Each test day uses only entry dates whose full five-session outcome was knowable before that test day.",
            "purge_sessions": max(config.MAX_PATTERN_HORIZON_SESSIONS, config.WALKFORWARD_PURGE_SESSIONS),
            "daily_weighting": "Candidate ranking equal-weights market dates to reduce cross-sectional correlation bias.",
            "same_candle_order": "No fixed stop is used; if target is touched in daily OHLC, the mover target is credited.",
            "overlap": "At most one open trade per ticker; per-run alert cap matches production.",
        },
    }
    (output / "walkforward_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (output / "effective_config.json").write_text(
        json.dumps({"model_key": model_key, "rules": final_params}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fields = [
        "fold", "train_start", "train_end", "train_dates", "purged_dates", "test_date",
        "trades", "target_hit_rate", "win_rate", "avg_return", "profit_factor", "objective",
        "baseline_trades", "baseline_objective",
    ]
    with (output / "walkforward_folds.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for fold in folds_payload:
            writer.writerow({key: fold.get(key) for key in fields})
    trade_fields = list(oos_trades[0].keys()) if oos_trades else ["entry_date", "ticker", "rule_name", "return_pct"]
    with (output / "walkforward_trades.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=trade_fields)
        writer.writeheader()
        if oos_trades:
            writer.writerows(oos_trades)
    with (output / "walkforward_by_rule.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "rule_name", "sample", "market_dates", "target_hit_rate", "win_rate", "avg_return",
            "profit_factor", "max_drawdown", "objective",
        ])
        writer.writeheader()
        for rule_name, metrics in payload["by_rule"].items():
            writer.writerow({"rule_name": rule_name, **{key: metrics.get(key) for key in writer.fieldnames if key != "rule_name"}})

    oos = summary["walk_forward_oos"]
    baseline = payload["baseline"]
    rows_html = "".join(
        f"<tr><td>{html.escape(rule)}</td><td>{m['sample']}</td><td>{m['target_hit_rate']:.2f}%</td>"
        f"<td>{m['win_rate']:.2f}%</td><td>{m['avg_return']:+.3f}%</td><td>{m['profit_factor']:.2f}</td></tr>"
        for rule, m in payload["by_rule"].items()
    )
    report = f"""<!doctype html><html><head><meta charset='utf-8'><title>TMAD Walk-forward rolling-30 relacional v5.6</title>
<style>body{{font-family:Segoe UI,Arial;margin:32px;color:#152238}}h1{{color:#17365d}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{border:1px solid #ccd6e0;padding:16px;border-radius:8px}}.value{{font-size:26px;font-weight:700}}table{{border-collapse:collapse;width:100%;margin-top:24px}}th,td{{border:1px solid #ccd6e0;padding:8px;text-align:left}}th{{background:#1f4e78;color:white}}.note{{background:#eef5fb;padding:14px;border-radius:8px}}</style></head><body>
<h1>TMAD — backtesting purgado walk-forward · PRECISION FIRST</h1><div class='note'>Cada fecha de test se simula con reglas elegidas exclusivamente con fechas anteriores y un embargo de cinco sesiones. El objetivo es seleccionar pocos setups de alta confianza: un día sin operaciones es válido. La configuración efectiva solo se activa si supera retorno, win rate, profit factor, drawdown, intervalo de confianza y frecuencia máxima fuera de muestra.</div>
<div class='grid'><div class='card'>OOS trades<div class='value'>{oos['sample']}</div></div><div class='card'>Hit +5%<div class='value'>{oos['target_hit_rate']:.2f}%</div></div><div class='card'>Win rate<div class='value'>{oos['win_rate']:.2f}%</div></div><div class='card'>Retorno medio<div class='value'>{oos['avg_return']:+.3f}%</div></div></div>
<h2>Robustez</h2><p>PF {oos['profit_factor']:.2f} · Max drawdown {oos['max_drawdown']:.2f} · Días OOS {oos['market_dates']} · Alertas/día {oos.get('avg_trades_per_day',0):.2f} · CI90 retorno diario medio [{oos.get('mean_daily_return_ci90_low')}, {oos.get('mean_daily_return_ci90_high')}]</p>
<h2>Comparación bootstrap</h2><p>Objetivo OOS {oos['objective']:.4f} vs baseline {baseline['objective']:.4f}.</p>
<h2>Por regla</h2><table><tr><th>Regla</th><th>Trades</th><th>Hit +5%</th><th>Win rate</th><th>Retorno</th><th>PF</th></tr>{rows_html}</table>
<p>Archivos: walkforward_summary.json, walkforward_folds.csv, walkforward_trades.csv, walkforward_by_rule.csv y effective_config.json.</p></body></html>"""
    (output / "walkforward_report.html").write_text(report, encoding="utf-8")
    (config.CORE_HISTORY_REPORT_FILE if calibration_layer == "core" else config.LATEST_WALKFORWARD_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return str(output)


def _next_market_session_date() -> str:
    value = market_session_date() + timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value.isoformat()


# ---------------------------------------------------------------------------
# Public optimizer
# ---------------------------------------------------------------------------

def run_optimizer(startup: bool = False, calibration_layer: str = "full") -> int | None:
    if str(calibration_layer or "full").lower() == "core" and getattr(config, "CORE_MOVER_DISCOVERY_ENABLED", True):
        from pattern_discovery import run_mover_discovery
        return run_mover_discovery(startup=startup)
    if not config.AUTO_TUNE_ENABLED:
        log_info("ℹ️ Auto-tune desactivado.", send_discord=False)
        return None
    calibration_layer = str(calibration_layer or "full").lower()
    is_core = calibration_layer == "core"
    target_sessions = int(config.CORE_HISTORY_TARGET_SESSIONS if is_core else config.DAILY_CLOSE_WINDOW_SESSIONS)
    training_source = "CORE_HISTORY_NATIVE" if is_core else "FULL_DAILY_CLOSE_NATIVE"
    shadow_history_bridge = False

    native_window_dates = (
        latest_core_daily_dates(target_sessions)
        if is_core else latest_canonical_daily_dates(target_sessions)
    )
    raw_rows = get_training_rows("core" if is_core else "full")
    rows = _prepare_rows(raw_rows)
    if is_core:
        for row in rows:
            row["is_close_snapshot"] = True

    eligible_native_dates = sorted({
        str(row.get("snapshot_date") or "")
        for row in rows
        if int(row.get("max_complete_horizon") or 0) >= config.MAX_PATTERN_HORIZON_SESSIONS
        and row.get("is_close_snapshot")
        and row.get("snapshot_date")
    })
    window_dates = [day for day in native_window_dates if day in set(eligible_native_dates)]

    # FULL daily-close history started later than CORE history, although the
    # historical CORE snapshots already contain the same point-in-time Scanner,
    # GEX and relational feature stack used by FULL rules. When native FULL is
    # too short, replay FULL rules over that richer historical memory as a
    # SHADOW-only bridge. The bridge can generate evidence, but can never be
    # scheduled/promoted because its universe includes historical backfill.
    if not is_core and len(window_dates) < target_sessions:
        bridge_rows = _prepare_rows(get_training_rows("core"))
        for row in bridge_rows:
            row["is_close_snapshot"] = True
            row["_training_source"] = "CORE_HISTORY_SHADOW_BRIDGE"
        bridge_dates = sorted({
            str(row.get("snapshot_date") or "")
            for row in bridge_rows
            if int(row.get("max_complete_horizon") or 0) >= config.MAX_PATTERN_HORIZON_SESSIONS
            and row.get("snapshot_date")
        })
        if len(bridge_dates) >= target_sessions:
            training_source = "CORE_HISTORY_SHADOW_BRIDGE"
            shadow_history_bridge = True
            window_dates = bridge_dates[-target_sessions:]
            rows = bridge_rows

    window_set = set(window_dates)
    rows = [
        row for row in rows
        if int(row.get("max_complete_horizon") or 0) >= config.MAX_PATTERN_HORIZON_SESSIONS
        and row.get("is_close_snapshot")
        and str(row.get("snapshot_date") or "") in window_set
    ]
    if len(window_dates) < target_sessions:
        log_info(
            f"ℹ️ Auto-tune {calibration_layer}: fuente canónica de esta capa insuficiente | "
            f"{len(window_dates)}/{target_sessions}. "
            + ("FULL bridge tampoco alcanza aún el mínimo; CORE/Discovery siguen usando su memoria propia." if not is_core else ""),
            send_discord=False,
        )
        return None
    if not rows:
        log_info("ℹ️ Auto-tune: todavía no hay outcomes completos a cinco sesiones.", send_discord=False)
        return None
    if shadow_history_bridge:
        log_info(
            f"🧪 [FULL HISTORY BRIDGE] {len(window_dates)} fechas maduras desde CORE_HISTORY; "
            "replay FULL habilitado solo para SHADOW/OOS, promoción bloqueada.",
            send_discord=False,
        )

    close_rows = [row for row in rows if row.get("is_close_snapshot")]
    dates = sorted({str(row.get("snapshot_date")) for row in close_rows if row.get("snapshot_date")})
    if not dates:
        log_info("ℹ️ Auto-tune: no hay snapshots diarios de cierre.", send_discord=False)
        return None
    start_date, end_date = dates[0], dates[-1]
    tuning_id = create_tuning_run(start_date, end_date)

    try:
        folds = _build_folds(dates)
        min_fully_labeled_dates = (
            int(config.CORE_HISTORY_MIN_FULLY_LABELED_DATES)
            if is_core else int(config.WALKFORWARD_MIN_FULLY_LABELED_DATES)
        )
        min_required_oos_dates = (
            int(config.CORE_HISTORY_MIN_OOS_DATES)
            if is_core else int(config.WALKFORWARD_MIN_OOS_DATES)
        )
        if len(dates) < min_fully_labeled_dates or len(folds) < min_required_oos_dates:
            message = (
                f"Histórico insuficiente para walk-forward {calibration_layer}: "
                f"fully_labeled_dates={len(dates)}/{min_fully_labeled_dates}; "
                f"oos_folds={len(folds)}/{min_required_oos_dates}; "
                f"purge={max(config.MAX_PATTERN_HORIZON_SESSIONS, config.WALKFORWARD_PURGE_SESSIONS)} sesiones."
            )
            finish_tuning_run(
                tuning_id, status="insufficient", observations=len(rows), labeled_rows=len(rows), message=message
            )
            send_daily(build_group("🟨 CALIBRACIÓN · PENDIENTE", [f"**Capa:** `{calibration_layer.upper()}` · muestra insuficiente para una validación OOS fiable.\n**Acción:** seguir acumulando cierres y outcomes."], footer="El detalle numérico queda guardado localmente."))
            return None

        active = get_active_model()
        active_params = active.get("params") or {}
        active_rules = active_params.get("rules") or {}
        rule_directions = CORE_RULE_DIRECTIONS if is_core else FULL_RULE_DIRECTIONS
        active_rules = {
            name: dict(active_rules.get(name) or {})
            for name in rule_directions
            if active_rules.get(name)
        }
        active_rules["_portfolio"] = dict((active_params.get("rules") or {}).get("_portfolio") or {
            "max_signals_per_sector": _precision_portfolio_caps()[0],
            "max_signals_per_direction": _precision_portfolio_caps()[1],
        })
        prices = daily_price_series()
        close_features = _build_close_feature_index(rows)
        all_dates = set(dates)

        candidate_pools: dict[str, list[dict]] = {}
        candidate_cache: dict[str, dict[str, list[dict]]] = {}
        candidates_evaluated = 0
        for rule_name in rule_directions:
            base = params_for_rule(active_params, rule_name)
            if not base:
                base = {
                    "enabled": True,
                    "min_score": config.DEFAULT_MIN_SCORE,
                    "min_rvol": 0.0 if is_core else config.DEFAULT_MIN_RVOL,
                    "expiry_change_pct": config.DEFAULT_EXPIRY_CHANGE_PCT,
                    "strike_change_pct": config.DEFAULT_STRIKE_CHANGE_PCT,
                    "near_wall_pct": config.DEFAULT_NEAR_WALL_PCT,
                    "break_wall_pct": config.DEFAULT_BREAK_WALL_PCT,
                    "core_min_momentum": 55.0,
                    "core_min_gex_change_pct": 10.0,
                    "core_rsi_long_min": 50.0,
                    "core_rsi_short_max": 50.0,
                    "core_min_relative_strength": 0.0,
                    "core_require_flow": 0,
                    "target_pct": config.PATTERN_TARGET_PCT,
                    "stop_pct": 0.0,
                    "hold_sessions": config.DEFAULT_HOLD_SESSIONS,
                    "exit_gex_reversal_pct": config.DEFAULT_EXIT_GEX_REVERSAL_PCT,
                    "exit_pattern_score": config.DEFAULT_EXIT_PATTERN_SCORE,
                    "trailing_activation_pct": config.DEFAULT_TRAILING_ACTIVATION_PCT,
                    "trailing_giveback_pct": config.DEFAULT_TRAILING_GIVEBACK_PCT,
                    "intraday_threshold_factor": config.UNVALIDATED_INTRADAY_THRESHOLD_FACTOR,
                    "min_daily_pattern_projection": config.UNVALIDATED_MIN_DAILY_PATTERN_PROJECTION,
                    "intraday_calibrated": False,
                    "context_weight": config.DEFAULT_CONTEXT_WEIGHT,
                    "market_component_weight": config.DEFAULT_MARKET_COMPONENT_WEIGHT,
                    "sector_component_weight": config.DEFAULT_SECTOR_COMPONENT_WEIGHT,
                    "breadth_component_weight": config.DEFAULT_BREADTH_COMPONENT_WEIGHT,
                    "leveraged_component_weight": config.DEFAULT_LEVERAGED_COMPONENT_WEIGHT,
                    "dynamic_component_weight": config.DEFAULT_DYNAMIC_COMPONENT_WEIGHT,
                    "lead_lag_component_weight": config.DEFAULT_LEAD_LAG_COMPONENT_WEIGHT,
                    "min_context_score": config.DEFAULT_MIN_CONTEXT_SCORE,
                    "min_market_score": config.DEFAULT_MIN_MARKET_SCORE,
                    "min_sector_score": config.DEFAULT_MIN_SECTOR_SCORE,
                    "max_adverse_context": config.DEFAULT_MAX_ADVERSE_CONTEXT,
                    "relational_exit_score": config.DEFAULT_RELATIONAL_EXIT_SCORE,
                }
            pool = _candidate_params(rule_name, base, f"v5.6-{calibration_layer}-rolling30-relational|{rule_name}|{end_date}")
            candidate_pools[rule_name] = pool
            cache: dict[str, list[dict]] = {}
            for candidate in pool:
                cache[_params_key(candidate)] = _rule_trades(
                    rule_name, candidate, rows, all_dates, close_features, prices, mode="daily_close"
                )
                candidates_evaluated += 1
            candidate_cache[rule_name] = cache
        if not any(name in active_rules for name in rule_directions):
            for name in rule_directions:
                active_rules[name] = dict(candidate_pools[name][0])
        active_rules.setdefault("_portfolio", {
            "max_signals_per_sector": _precision_portfolio_caps()[0],
            "max_signals_per_direction": _precision_portfolio_caps()[1],
        })

        oos_trades: list[dict] = []
        baseline_trades: list[dict] = []
        context_ablation_trades: list[dict] = []
        component_ablation_trades: dict[str, list[dict]] = {name: [] for name in RELATIONAL_COMPONENT_FIELDS}
        folds_payload: list[dict] = []
        fold_params_history: list[dict[str, dict]] = []

        for fold in folds:
            train_dates = set(fold["train_dates"])
            test_dates = {str(fold["test_date"])}
            selected_seed: dict[str, dict] = {}
            for rule_name in rule_directions:
                selected_seed[rule_name], _ = _best_candidate(
                    rule_name,
                    candidate_pools[rule_name],
                    candidate_cache[rule_name],
                    train_dates,
                )
            selected, joint_train_metrics = _joint_policy_select(
                selected_seed, candidate_pools, candidate_cache, train_dates
            )
            fold_params_history.append(selected)
            test_trades = _portfolio_trades(
                selected, rows, test_dates, close_features, prices, mode="daily_close"
            )
            baseline = _portfolio_trades(
                active_rules, rows, test_dates, close_features, prices, mode="daily_close"
            )
            context_ablation = _portfolio_trades(
                _context_disabled_params(selected), rows, test_dates, close_features, prices, mode="daily_close"
            )
            for component_name, component_field in RELATIONAL_COMPONENT_FIELDS.items():
                component_ablation_trades[component_name].extend(
                    _portfolio_trades(
                        _component_disabled_params(selected, component_field),
                        rows,
                        test_dates,
                        close_features,
                        prices,
                        mode="daily_close",
                    )
                )
            oos_trades.extend(test_trades)
            baseline_trades.extend(baseline)
            context_ablation_trades.extend(context_ablation)
            m = _metrics(test_trades)
            bm = _metrics(baseline)
            folds_payload.append({
                "fold": fold["fold"],
                "train_start": fold["train_dates"][0],
                "train_end": fold["train_dates"][-1],
                "train_dates": len(fold["train_dates"]),
                "purged_dates": ",".join(fold["purged_dates"]),
                "test_date": fold["test_date"],
                "trades": m["sample"],
                "target_hit_rate": m["target_hit_rate"],
                "win_rate": m["win_rate"],
                "avg_return": m["avg_return"],
                "profit_factor": m["profit_factor"],
                "objective": m["objective"],
                "baseline_trades": bm["sample"],
                "baseline_objective": bm["objective"],
                "joint_train_objective": joint_train_metrics.get("objective"),
            })

        oos_metrics = _metrics(oos_trades, with_ci=True, seed=f"oos-{end_date}")
        # A model with zero genuine OOS trades is not a model. Older versions
        # persisted these as rejected challengers (sample=0, dates=0, DD=999),
        # polluting the registry and making Learning look chronically immature.
        if int(oos_metrics.get("sample") or 0) <= 0 or int(oos_metrics.get("market_dates") or 0) <= 0:
            message = (
                f"No genuine OOS trades for {calibration_layer}: "
                f"folds={len(folds)} rows={len(rows)} dates={len(dates)}. "
                "Registry write suppressed; repair the historical source/feature compatibility instead."
            )
            finish_tuning_run(
                tuning_id,
                status="blocked_no_oos",
                observations=len(rows),
                labeled_rows=len(rows),
                candidates_evaluated=candidates_evaluated,
                message=message,
            )
            log_info(f"⚠️ [WALK-FORWARD BLOCKED] {message}", send_discord=False)
            return None
        # Rule-level OOS evidence is retained separately. A portfolio can look
        # strong while one component rule is weak; precision-first disables such
        # rules instead of allowing the aggregate to hide them.
        oos_rule_metrics = {
            rule_name: _metrics(
                [trade for trade in oos_trades if str(trade.get("rule_name") or "") == rule_name],
                with_ci=True,
                seed=f"oos-rule-{rule_name}-{end_date}",
            )
            for rule_name in rule_directions
        }
        baseline_metrics = _metrics(baseline_trades, with_ci=True, seed=f"baseline-{end_date}")
        context_ablation_metrics = _metrics(context_ablation_trades, with_ci=True, seed=f"context-ablation-{end_date}")
        relational_objective_lift = oos_metrics["objective"] - context_ablation_metrics["objective"]
        component_ablation_metrics = {
            name: _metrics(trades, with_ci=True, seed=f"component-{name}-{end_date}")
            for name, trades in component_ablation_trades.items()
        }
        component_objective_lifts = {
            name: oos_metrics["objective"] - metrics["objective"]
            for name, metrics in component_ablation_metrics.items()
        }

        # Effective config: after the unbiased walk-forward estimate is known, fit
        # each rule on all fully labelled dates. This is the configuration used now.
        final_seed: dict[str, dict] = {}
        full_history_rule_metrics: dict[str, dict] = {}
        for rule_name in rule_directions:
            final_seed[rule_name], full_history_rule_metrics[rule_name] = _best_candidate(
                rule_name,
                candidate_pools[rule_name],
                candidate_cache[rule_name],
                all_dates,
            )
        final_params, joint_full_history_metrics = _joint_policy_select(
            final_seed, candidate_pools, candidate_cache, all_dates
        )

        precision_rule_decisions: dict[str, dict] = {}
        if config.ALERT_SELECTION_PROFILE == "precision_first":
            for rule_name in rule_directions:
                metrics = oos_rule_metrics.get(rule_name) or {}
                enough = int(metrics.get("sample") or 0) >= int(config.PRECISION_MIN_RULE_OOS_TRADES)
                reliable = bool(
                    enough
                    and float(metrics.get("win_rate") or 0.0) >= float(config.PRECISION_MIN_RULE_WIN_RATE)
                    and float(metrics.get("avg_return") or 0.0) >= float(config.PRECISION_MIN_RULE_AVG_RETURN)
                    and float(metrics.get("profit_factor") or 0.0) >= float(config.PRECISION_MIN_RULE_PROFIT_FACTOR)
                )
                precision_rule_decisions[rule_name] = {
                    "enabled": reliable,
                    "reason": "oos_rule_quality_pass" if reliable else "oos_rule_quality_rejected",
                    "metrics": metrics,
                }
                final_params.setdefault(rule_name, {})["enabled"] = reliable
                final_params[rule_name]["precision_oos_sample"] = int(metrics.get("sample") or 0)
                final_params[rule_name]["precision_oos_win_rate"] = float(metrics.get("win_rate") or 0.0)
                final_params[rule_name]["precision_oos_avg_return"] = float(metrics.get("avg_return") or 0.0)
                final_params[rule_name]["precision_oos_profit_factor"] = float(metrics.get("profit_factor") or 0.0)

        if is_core:
            intraday_summary = {
                "status": "fallback",
                "reason": "CORE daily policy calibrated; timing remains conservative until intraday ZIP sample is sufficient.",
                "oos": _metrics([]),
                "rules": {},
            }
            for name in rule_directions:
                final_params[name]["intraday_threshold_factor"] = config.UNVALIDATED_INTRADAY_THRESHOLD_FACTOR
                final_params[name]["min_daily_pattern_projection"] = config.UNVALIDATED_MIN_DAILY_PATTERN_PROJECTION
                final_params[name]["intraday_calibrated"] = False
        else:
            final_params, intraday_summary = _tune_intraday_final(
                final_params, rows, dates, folds, close_features, prices
            )
        stability = _param_stability(fold_params_history)
        improvement = oos_metrics["objective"] - baseline_metrics["objective"]
        ci_low = oos_metrics.get("mean_daily_return_ci90_low")
        min_sample = config.CORE_HISTORY_MIN_VALIDATION_TRADES if is_core else config.MIN_VALIDATION_SAMPLES
        min_oos_dates = config.CORE_HISTORY_MIN_OOS_DATES if is_core else config.WALKFORWARD_MIN_OOS_DATES
        min_win = config.CORE_HISTORY_MIN_WIN_RATE if is_core else config.MIN_VALIDATION_WIN_RATE
        min_avg = config.CORE_HISTORY_MIN_AVG_RETURN if is_core else config.MIN_VALIDATION_AVG_RETURN
        min_pf = config.CORE_HISTORY_MIN_PROFIT_FACTOR if is_core else config.MIN_VALIDATION_PROFIT_FACTOR
        max_dd = config.CORE_HISTORY_MAX_DRAWDOWN if is_core else config.WALKFORWARD_MAX_DRAWDOWN
        if config.ALERT_SELECTION_PROFILE == "precision_first":
            # The frequency cap makes the older 30-trade requirement unreachable
            # in some 30-session folds. Require evidence across independent dates
            # and a feasible precision-sized sample instead.
            min_sample = int(config.PRECISION_MIN_VALIDATION_TRADES)
            min_oos_dates = max(int(min_oos_dates), int(config.PRECISION_MIN_OOS_DATES))
            min_win = max(min_win, config.PRECISION_MIN_PROMOTION_WIN_RATE)
            min_avg = max(min_avg, config.PRECISION_MIN_PROMOTION_AVG_RETURN)
            min_pf = max(min_pf, config.PRECISION_MIN_PROMOTION_PROFIT_FACTOR)
            max_dd = min(max_dd, config.PRECISION_MAX_PROMOTION_DRAWDOWN)
        min_improvement = config.CORE_HISTORY_MIN_PROMOTION_IMPROVEMENT if is_core else config.MIN_PROMOTION_IMPROVEMENT
        passes = (
            oos_metrics["sample"] >= min_sample
            and oos_metrics["market_dates"] >= min_oos_dates
            and oos_metrics["win_rate"] >= min_win
            and oos_metrics["avg_return"] >= min_avg
            and oos_metrics["profit_factor"] >= min_pf
            and oos_metrics["max_drawdown"] <= max_dd
            and improvement >= min_improvement
            and (ci_low is None or ci_low >= (config.PRECISION_MIN_CI90_DAILY_RETURN if config.ALERT_SELECTION_PROFILE == "precision_first" else config.WALKFORWARD_MIN_CI90_DAILY_RETURN))
            and (
                config.ALERT_SELECTION_PROFILE != "precision_first"
                or oos_metrics.get("avg_trades_per_day", 0.0) <= config.PRECISION_MAX_AVG_TRADES_PER_DAY
            )
            and any((final_params.get(name) or {}).get("enabled") is not False for name in rule_directions)
        )

        champion = current_champion()
        champion_tier = str(((champion or {}).get("params") or {}).get("feature_tier") or "").upper()
        champion_objective = float(champion.get("objective_score") or -999.0) if champion else -999.0
        champion_improvement = oos_metrics["objective"] - champion_objective
        if shadow_history_bridge:
            # Historical backfill is valid for research/OOS comparison, but it
            # is not eligible for routing or promotion to SHADOW/LIVE.
            promote = False
        elif is_core and champion_tier == "FULL":
            promote = False
        else:
            promote = passes and (
                champion is None
                or champion_tier in {"", "BOOTSTRAP"}
                or champion_improvement >= min_improvement
            )
        effective_date = (
            now_madrid().date().isoformat()
            if (startup and config.STARTUP_ACTIVATE_VALID_MODEL_TODAY)
            else _next_market_session_date()
        )
        model_prefix = "full-shadow-bridge" if shadow_history_bridge else ("core-adaptive" if is_core else "full-adaptive")
        model_key = f"{model_prefix}-wf-{now_madrid().strftime('%Y%m%d-%H%M%S')}"
        summary = {
            "status": ("shadow_bridge_passes" if passes else "shadow_bridge_rejected") if shadow_history_bridge else ("passes" if passes else "rejected"),
            "training_source": training_source,
            "promotion_blocked": bool(shadow_history_bridge),
            "walk_forward_oos": oos_metrics,
            "baseline_oos": baseline_metrics,
            "objective_improvement_vs_baseline": improvement,
            "objective_improvement_vs_champion": champion_improvement,
            "relational_context_ablation": context_ablation_metrics,
            "relational_objective_lift": relational_objective_lift,
            "relational_component_ablation": component_ablation_metrics,
            "relational_component_lifts": component_objective_lifts,
            "fold_count": len(folds),
            "market_dates": len(dates),
            "candidates_evaluated": candidates_evaluated,
            "joint_policy_full_history": joint_full_history_metrics,
            "joint_policy_method": "bounded coordinate descent over per-rule candidates with sector/direction collision control",
            "enabled_rules": [name for name in rule_directions if (final_params.get(name) or {}).get("enabled") is not False],
            "disabled_rules": [name for name in rule_directions if (final_params.get(name) or {}).get("enabled") is False],
            "portfolio_policy": final_params.get("_portfolio") or {},
            "precision_rule_oos_metrics": oos_rule_metrics,
            "precision_rule_decisions": precision_rule_decisions,
            "precision_thresholds": {
                "min_validation_trades": int(config.PRECISION_MIN_VALIDATION_TRADES),
                "min_oos_dates": int(config.PRECISION_MIN_OOS_DATES),
                "min_win_rate": float(config.PRECISION_MIN_PROMOTION_WIN_RATE),
                "min_avg_return": float(config.PRECISION_MIN_PROMOTION_AVG_RETURN),
                "min_profit_factor": float(config.PRECISION_MIN_PROMOTION_PROFIT_FACTOR),
                "max_drawdown": float(config.PRECISION_MAX_PROMOTION_DRAWDOWN),
            },
        }
        output_dir = _write_walkforward_report(
            model_key,
            dates,
            folds_payload,
            oos_trades,
            baseline_trades,
            final_params,
            summary,
            stability,
            intraday_summary,
            calibration_layer=calibration_layer,
        )
        model_params = {
            "version": 10,
            "feature_tier": "CORE" if is_core else "FULL",
            "source_kind": (
                "CORE_HISTORY_FULL_RULE_REPLAY"
                if shadow_history_bridge
                else ("TMAD_DETAIL_30D" if is_core else "FULL_DEEP_POINT_IN_TIME")
            ),
            "universe_quality": (
                "HISTORICAL_BACKFILL_SHADOW_ONLY"
                if shadow_history_bridge
                else ("CURRENT_UNIVERSE_BACKFILL" if is_core else "POINT_IN_TIME_FULL_DEEP")
            ),
            "training_source": training_source,
            "promotion_scope": "SHADOW_RESEARCH_ONLY" if shadow_history_bridge else "NORMAL_GATED",
            "method": f"purged_expanding_walk_forward_relational_precision_first_{calibration_layer}",
            "trained_at_madrid": ts_madrid_str(),
            "training_window_days": config.TRAINING_WINDOW_DAYS,
            "pattern_target_pct": config.PATTERN_TARGET_PCT,
            "max_horizon_sessions": config.MAX_PATTERN_HORIZON_SESSIONS,
            "rules": final_params,
        }
        metrics_payload = {
            "walk_forward": summary,
            "folds": folds_payload,
            "parameter_stability": stability,
            "full_history_rule_metrics": full_history_rule_metrics,
            "precision_rule_oos_metrics": oos_rule_metrics,
            "precision_rule_decisions": precision_rule_decisions,
            "intraday_trigger": intraday_summary,
            "report_dir": output_dir,
            "target_definition": "Entry is successful when directional MFE reaches +5% within five sessions.",
            "performance_definition": "Realised simulated return after target, calibrated pattern reversal, or max-horizon exit; no fixed price stop.",
            "validation_design": "Purged expanding walk-forward; no random split; five-session embargo; date-equal-weighted objective with recency decay; joint portfolio policy refinement.",
            "feature_tier": "CORE" if is_core else "FULL",
            "training_source": training_source,
            "promotion_blocked": bool(shadow_history_bridge),
            "source_quality": (
                "HISTORICAL_BACKFILL_SHADOW_ONLY"
                if shadow_history_bridge
                else ("CURRENT_UNIVERSE_BACKFILL" if is_core else "POINT_IN_TIME_FULL_DEEP")
            ),
            "relational_ablation": {
                "without_context": context_ablation_metrics,
                "objective_lift": relational_objective_lift,
                "component_without": component_ablation_metrics,
                "component_lifts": component_objective_lifts,
                "interpretation": "Positive lift means the graph or component improved OOS policy return after the same entry/exit replay.",
            },
        }
        reason = (
            f"source={training_source}; shadow_bridge={shadow_history_bridge}; "
            f"passes={passes}; promote={promote}; folds={len(folds)}; oos_n={oos_metrics['sample']}; "
            f"oos_days={oos_metrics['market_dates']}; hit5={oos_metrics['target_hit_rate']:.2f}; "
            f"win={oos_metrics['win_rate']:.2f}; avg={oos_metrics['avg_return']:.4f}; "
            f"pf={oos_metrics['profit_factor']:.3f}; maxdd={oos_metrics['max_drawdown']:.3f}; "
            f"improvement={improvement:.4f}; report={output_dir}"
        )
        status = "shadow_bridge" if shadow_history_bridge else ("challenger" if passes else "rejected")
        model_id = insert_model_version({
            "model_key": model_key,
            "created_at_utc": ts_utc_str(),
            "created_at_madrid": ts_madrid_str(),
            "effective_from_date": effective_date,
            "training_start_date": start_date,
            "training_end_date": end_date,
            "window_days": config.TRAINING_WINDOW_DAYS,
            "status": status,
            "parent_model_id": active.get("id"),
            "objective_score": oos_metrics["objective"],
            "train_sample": sum(metrics.get("sample", 0) for metrics in full_history_rule_metrics.values()),
            "validation_sample": oos_metrics["sample"],
            "validation_win_rate": oos_metrics["win_rate"],
            "validation_avg_return_pct": oos_metrics["avg_return"],
            "validation_profit_factor": oos_metrics["profit_factor"],
            "params_json": json.dumps(model_params, ensure_ascii=False),
            "metrics_json": json.dumps(metrics_payload, ensure_ascii=False),
            "reason": reason,
        })
        importance_rows = [{
            "feature_name": "relational_context_bundle",
            "importance": relational_objective_lift,
            "method": "purged_walkforward_ablation",
            "sample_size": oos_metrics.get("sample", 0),
            "details": {"full": oos_metrics, "without_context": context_ablation_metrics},
        }]
        for component_name, lift in component_objective_lifts.items():
            importance_rows.append({
                "feature_name": component_name,
                "importance": lift,
                "method": "purged_walkforward_component_ablation",
                "sample_size": oos_metrics.get("sample", 0),
                "details": {"full": oos_metrics, "without_component": component_ablation_metrics[component_name]},
            })
        insert_model_feature_importance(model_id, importance_rows)
        promoted_id = None
        provisional_id = None
        if (not shadow_history_bridge) and promote and config.AUTO_PROMOTE_ENABLED:
            schedule_model(model_id, effective_date=effective_date)
            promoted_id = model_id
        elif is_core and not bool(getattr(config, "ADAPTIVE_LEARNING_ENABLED", False)):
            # Legacy mode only. In v6.1.24 the Adaptive Learning Controller owns
            # all automatic SHADOW model switches and validates rollback itself.
            active_status = str(active.get("status") or "").lower()
            if active_status != "champion":
                provisional_id = activate_provisional_model(model_id)
        tuning_status = (
            "scheduled" if promoted_id
            else ("provisional_active" if provisional_id == model_id else status)
        )
        finish_tuning_run(
            tuning_id,
            status=tuning_status,
            observations=len(rows),
            labeled_rows=len(rows),
            candidates_evaluated=candidates_evaluated,
            candidate_model_id=model_id,
            promoted_model_id=promoted_id,
            message=reason,
        )
        log_info(
            f"✅ Auto-tune v5.6 {calibration_layer.upper()} ROLLING-30 RELATIONAL WALK-FORWARD | model={model_key} | status={tuning_status} "
            f"| source={training_source} "
            f"| folds={len(folds)} | OOS n={oos_metrics['sample']} days={oos_metrics['market_dates']} "
            f"| +5% hit={oos_metrics['target_hit_rate']:.2f}% | win={oos_metrics['win_rate']:.2f}% "
            f"| avg={oos_metrics['avg_return']:.3f}% | PF={oos_metrics['profit_factor']:.2f} "
            f"| alertas/día={oos_metrics.get('avg_trades_per_day', 0.0):.2f} "
            f"| MaxDD={oos_metrics['max_drawdown']:.2f} | Δobjective={improvement:.3f} "
            f"| graph_lift={relational_objective_lift:+.3f} | candidates={candidates_evaluated}",
            send_discord=False,
        )
        send_daily(build_group(
            f"🧪 BACKTEST · {calibration_layer.upper()} ROLLING-30",
            [
                "\n".join([
                    "### Modelo",
                    f"**Clave:** `{model_key}`",
                    f"**Estado:** {(('ACTIVO HOY' if startup else 'PROGRAMADO') if promoted_id else ('PRELIMINAR / SHADOW' if provisional_id == model_id else status.upper()))}",
                    f"**Replay:** `{len(folds)}` fechas OOS · **Embargo:** `{max(config.MAX_PATTERN_HORIZON_SESSIONS, config.WALKFORWARD_PURGE_SESSIONS)}` sesiones",
                    f"**Fuente:** `{training_source}`" + (" · **SHADOW ONLY / NO PROMOTION**" if shadow_history_bridge else ""),
                ]),
                "\n".join([
                    "### Rendimiento OOS",
                    f"**Trades:** `{oos_metrics['sample']}` · **Hit ±5%:** `{oos_metrics['target_hit_rate']:.2f}%` · **Win:** `{oos_metrics['win_rate']:.2f}%`",
                    f"**Media:** `{oos_metrics['avg_return']:+.3f}%` · **PF:** `{oos_metrics['profit_factor']:.2f}` · **MaxDD:** `{oos_metrics['max_drawdown']:.2f}`",
                    f"**Alertas/día:** `{oos_metrics.get('avg_trades_per_day', 0.0):.2f}`",
                ]),
                "\n".join([
                    "### Gate precision-first",
                    f"**Score mínimo live:** `{config.PRECISION_FIRST_MIN_LIVE_SCORE:.0f}` · **Confirmaciones:** `{config.PRECISION_FIRST_MIN_CONFIRMING_BLOCKS}`",
                    f"**Mejora vs bootstrap:** `{improvement:+.4f}` · **Reglas activas:** `{len(summary['enabled_rules'])}`",
                    f"**Trigger intradía:** {intraday_summary['status'].upper()} · **Lift grafo OOS:** `{relational_objective_lift:+.3f}`",
                ]),
            ],
            footer="La configuración y los reportes técnicos quedan guardados localmente.",
        ))
        return model_id
    except Exception as exc:
        finish_tuning_run(tuning_id, status="error", message=str(exc))
        log_error(f"🟥 Error auto-tune walk-forward: {exc}")
        raise
