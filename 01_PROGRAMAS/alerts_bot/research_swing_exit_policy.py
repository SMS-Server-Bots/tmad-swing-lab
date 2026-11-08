from __future__ import annotations

"""Detached swing exit-policy backtest on frozen historical entries (SHADOW ONLY).

Research design:
- same historical entry, side, next-session open and forward OHLC across arms;
- no future maximum, MFE, target-hit label or next close can influence an exit;
- daily-close trailing decisions use only prior close peak;
- stop gaps execute at next open when worse than the invalidation;
- no synthesized high/low: incomplete OHLC, splits and discontinuities fail closed;
- all policies share a cohort with 10 complete sessions; report 5D separately;
- no LIVE entry/sizing/config update, and no automatic promotion.
"""

from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path
import argparse
import json
import math
import sqlite3
from zoneinfo import ZoneInfo
from statistics import mean
from typing import Any, Callable

import config
from research_streaming_io import atomic_stream_json

MODE = "SWING_EXIT_POLICY_REPLAY_V1"
POLICIES = ("TIME_5", "TIME_10", "TRAIL_CLOSE_1R", "TRAIL_CLOSE_2R", "GEX_MILESTONE_TRAIL")
DEFAULT_ROUNDTRIP_COST_BPS = 20.0
MIN_COMMON_TRADES = 30
MIN_COMMON_DATES = 15


def _f(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _day(value: Any) -> str:
    raw = str(value or "")[:10]
    try:
        return date.fromisoformat(raw).isoformat()
    except (TypeError, ValueError):
        return ""


def _ret(direction: str, entry: float, price: float) -> float:
    return 100.0 * ((price / entry) - 1.0) if direction == "LONG" else 100.0 * ((entry / price) - 1.0)


def _profit_factor(returns: list[float]) -> float | None:
    wins = sum(x for x in returns if x > 0)
    losses = abs(sum(x for x in returns if x < 0))
    return round(wins / losses, 4) if losses else (None if not wins else None)


def _normalized_entry(raw: dict) -> dict | None:
    ticker = str(raw.get("ticker") or "").upper().strip()
    direction = str(raw.get("direction") or "").upper()
    signal_date = _day(raw.get("signal_date") or raw.get("entry_date"))
    original = _f(raw.get("entry_price"))
    target = _f(raw.get("target_price"))
    invalidation = _f(raw.get("invalidation_price"))
    if not ticker or direction not in ("LONG", "SHORT") or not signal_date or original is None or original <= 0:
        return None
    return {
        "ticker": ticker, "direction": direction, "signal_date": signal_date,
        "signal_spot": original, "target_price": target, "invalidation_price": invalidation,
        "source_scanner_run_id": raw.get("source_scanner_run_id"),
        "recorded_exit_date": _day(raw.get("exit_date")),
        "recorded_exit_reason": str(raw.get("exit_reason") or ""),
    }


def _valid_ohlc(raw: dict, *, signal_date: str) -> dict | None:
    day = _day(raw.get("trade_date") or raw.get("date"))
    if not day or day <= signal_date:
        return None
    # Some historical pathways explicitly persist CLOSE-only synthetic bars.
    # Such bars are valid return labels, never proof for intraday stop order.
    provenance = str(raw.get("source") or raw.get("path_source") or "").upper()
    if "CLOSE_ONLY" in provenance or "FEATURE_POINT_IN_TIME_CLOSE" in provenance:
        return None
    values = [_f(raw.get(name)) for name in ("open", "high", "low", "close")]
    if any(v is None or v <= 0 for v in values):
        return None
    op, hi, lo, close = [float(v) for v in values]
    if lo > min(op, close) or hi < max(op, close) or lo > hi:
        return None
    split = _f(raw.get("split_factor"))
    if split is not None and abs(split - 1.0) > 1e-5:
        return None
    return {"date": day, "open": op, "high": hi, "low": lo, "close": close}


def _future_path(entry: dict, rows: list[dict], *, sessions: int = 10) -> tuple[list[dict], str | None]:
    # Identical daily path for every proposed exit policy. Reject all gaps/splits
    # rather than quietly joining two different adjusted-price segments.
    dates = set()
    path = []
    previous_close = None
    for raw in sorted(rows, key=lambda x: str(x.get("trade_date") or x.get("date") or "")):
        day = _day(raw.get("trade_date") or raw.get("date"))
        if not day or day <= entry["signal_date"]:
            continue
        if day in dates:
            return [], "DUPLICATE_SESSION"
        dates.add(day)
        candle = _valid_ohlc(raw, signal_date=entry["signal_date"])
        if candle is None:
            return [], "INVALID_OR_CLOSE_ONLY_OHLC"
        # A path must start in the first trading week after the actual
        # signal. Otherwise ten bars months later would be falsely counted
        # as 10 forward sessions (a serious historical leakage/coverage bug).
        if not path and (date.fromisoformat(day) -
                         date.fromisoformat(entry["signal_date"])).days > 6:
            return [], "STALE_ENTRY_TO_FIRST_BAR"
        if previous_close is not None:
            if abs(candle["open"] / previous_close - 1.0) > 0.50:
                return [], "LARGE_UNVERIFIED_PRICE_DISCONTINUITY"
        previous_close = candle["close"]
        if path and (date.fromisoformat(day) - date.fromisoformat(path[-1]["date"])).days > 6:
            return [], "STALE_SERIES_GAP"
        path.append(candle)
        if len(path) >= sessions:
            break
    if len(path) < sessions:
        return path, "INSUFFICIENT_FORWARD_SESSIONS"
    return path, None


def _initial_risk(entry: float, stop: float | None, direction: str) -> float | None:
    if stop is None or stop <= 0:
        return None
    if direction == "LONG" and stop < entry:
        return 100.0 * (1.0 - stop / entry)
    if direction == "SHORT" and stop > entry:
        return 100.0 * (1.0 - entry / stop)
    return None


def _stop_fill(candle: dict, direction: str, stop: float) -> tuple[float | None, str | None]:
    op, hi, lo = candle["open"], candle["high"], candle["low"]
    if direction == "LONG":
        if op <= stop:
            return op, "GAP_THROUGH_STOP"
        if lo <= stop:
            return stop, "INVALIDATION_STOP"
    else:
        if op >= stop:
            return op, "GAP_THROUGH_STOP"
        if hi >= stop:
            return stop, "INVALIDATION_STOP"
    return None, None


def simulate_exit(
    entry: dict,
    path: list[dict],
    policy: str,
    *,
    roundtrip_cost_bps: float = DEFAULT_ROUNDTRIP_COST_BPS,
    observation_cap: int | None = None,
) -> dict | None:
    if policy not in POLICIES or not path:
        return None
    direction = entry["direction"]
    opening = float(path[0]["open"])
    stop = _f(entry.get("invalidation_price"))
    risk_pct = _initial_risk(opening, stop, direction)
    if risk_pct is None or not 0.20 <= risk_pct <= 30.0:
        return None  # No invented stop/risk; entry invalidation must be sound.
    target = _f(entry.get("target_price"))
    target_valid = (target is not None and
                    ((direction == "LONG" and target > opening) or
                     (direction == "SHORT" and target < opening)))
    policy_horizon = 5 if policy == "TIME_5" else 10
    # The 5-session diagnostic uses the same five mature bars for *both*
    # arms. Runner policies do not require 10D data to be assessed at 5D.
    expiry = min(policy_horizon, observation_cap) if observation_cap is not None else policy_horizon
    if len(path) < expiry:
        return None
    best_close_ret = 0.0  # previous confirmed CLOSE, not future intra-bar high
    peak_mfe = 0.0
    trough_mae = 0.0
    target_seen = False
    for i, candle in enumerate(path[:expiry], 1):
        stopped_price, stopped_reason = _stop_fill(candle, direction, float(stop))
        if stopped_price is not None:
            exit_price, reason = stopped_price, stopped_reason
        else:
            close_ret = _ret(direction, opening, candle["close"])
            threshold = None
            if policy == "TRAIL_CLOSE_1R" and best_close_ret >= risk_pct:
                threshold = best_close_ret - risk_pct
            elif policy == "TRAIL_CLOSE_2R" and best_close_ret >= 2 * risk_pct:
                threshold = best_close_ret - 1.5 * risk_pct
            elif policy == "GEX_MILESTONE_TRAIL" and target_seen:
                threshold = best_close_ret - 0.75 * risk_pct
            if threshold is not None and close_ret <= threshold:
                exit_price, reason = candle["close"], "PRIOR_CLOSE_PEAK_GIVEBACK"
            elif i == expiry:
                exit_price, reason = candle["close"], "RESEARCH_OBSERVATION_END"
            else:
                exit_price = reason = None

        if exit_price is not None:
            # When a stop touches inside a candle, subsequent high/low order
            # cannot be reconstructed from daily OHLC. Do not credit unproven
            # same-candle favorable excursion after that stop.
            gross = _ret(direction, opening, float(exit_price))
            net = gross - roundtrip_cost_bps / 100.0
            return {
                "ticker": entry["ticker"], "signal_date": entry["signal_date"],
                "direction": direction, "policy": policy, "entry_date": path[0]["date"],
                "entry_price": round(opening, 6), "exit_date": candle["date"],
                "exit_price": round(float(exit_price), 6), "exit_reason": reason,
                "held_sessions": i, "initial_risk_pct": round(risk_pct, 4),
                "gross_return_pct": round(gross, 4), "net_return_pct": round(net, 4),
                "capital_session_pct": round(net / i, 4),
                "roundtrip_cost_bps": roundtrip_cost_bps,
                "valid_target_at_entry": bool(target_valid),
                "max_confirmed_close_ret_before_exit_pct": round(best_close_ret, 4),
                "max_observed_favorable_excursion_pct": round(max(peak_mfe, gross), 4),
                "max_observed_adverse_excursion_pct": round(min(trough_mae, gross), 4),
                "capturable_return_not_hindsight_maximum": True,
            }
        best_close_ret = max(best_close_ret, _ret(direction, opening, candle["close"]))
        favourable_price = candle["high"] if direction == "LONG" else candle["low"]
        adverse_price = candle["low"] if direction == "LONG" else candle["high"]
        peak_mfe = max(peak_mfe, _ret(direction, opening, favourable_price))
        trough_mae = min(trough_mae, _ret(direction, opening, adverse_price))
        # A target first touched during today's bar affects tomorrow's decision.
        if target_valid:
            target_seen = target_seen or (
                candle["high"] >= target if direction == "LONG" else candle["low"] <= target
            )
    raise AssertionError("expiry policy did not close")


def _stats(items: list[dict]) -> dict:
    values = [float(x["net_return_pct"]) for x in items]
    n = len(values)
    return {
        "trades": n, "dates": len({x["signal_date"] for x in items}),
        "win_rate_pct": round(100 * sum(x > 0 for x in values) / n, 3) if n else None,
        "avg_net_return_pct": round(mean(values), 4) if n else None,
        "profit_factor_net": _profit_factor(values),
        "avg_held_sessions": round(mean(x["held_sessions"] for x in items), 3) if n else None,
        "avg_return_per_capital_session_pct": round(mean(x["capital_session_pct"] for x in items), 4) if n else None,
        "avg_giveback_from_prior_closes_pct": round(mean(max(0.0, x["max_confirmed_close_ret_before_exit_pct"] - x["gross_return_pct"]) for x in items), 4) if n else None,
        "avg_mfe_pct": round(mean(x["max_observed_favorable_excursion_pct"] for x in items), 4) if n else None,
        "avg_mae_pct": round(mean(x["max_observed_adverse_excursion_pct"] for x in items), 4) if n else None,
        "avg_realized_fraction_of_mfe_pct": round(mean(
            100 * max(0.0, x["gross_return_pct"]) / x["max_observed_favorable_excursion_pct"]
            for x in items if x["max_observed_favorable_excursion_pct"] > 0.001
        ), 4) if any(x["max_observed_favorable_excursion_pct"] > 0.001 for x in items) else None,
        "exit_reasons": dict(Counter(x["exit_reason"] for x in items)),
        "trades_with_valid_target_at_entry": sum(bool(x["valid_target_at_entry"]) for x in items),
    }


def compare_entries(
    entries: list[dict],
    paths: dict[tuple[str, str], list[dict]],
    *,
    cost_bps: float = DEFAULT_ROUNDTRIP_COST_BPS,
) -> dict:
    # Two separate matched groups. The 10-session group is the main research
    # comparison; the 5-session group is a short-horizon health check.
    rejected = Counter()
    # A ticker/date/side can appear repeatedly at different scanner runs.
    # Preserve the LAST observation available that day for the EOD study,
    # never count duplicate ticker/date/side paths as independent evidence.
    unique = {}
    for raw in entries:
        identity = (str(raw.get("ticker") or "").upper(),
                    _day(raw.get("signal_date") or raw.get("entry_date")),
                    str(raw.get("direction") or "").upper())
        rank = int(_f(raw.get("source_scanner_run_id")) or 0)
        if identity not in unique or rank > unique[identity][0]:
            unique[identity] = (rank, raw)
    deduped = [pair[1] for pair in unique.values()]
    rejected["DEDUPED_SAME_DAY_TICKER_DIRECTION"] = max(0, len(entries) - len(deduped))
    groups: dict[str, dict[str, list[dict]]] = {
        "COMPLETE_5D": {"TIME_5": [], "TRAIL_CLOSE_1R": []},
        "COMPLETE_10D": {p: [] for p in POLICIES},
    }
    for raw in deduped:
        entry = _normalized_entry(raw)
        if entry is None:
            rejected["INVALID_ENTRY"] += 1
            continue
        path, error = _future_path(entry, paths.get((entry["ticker"], entry["signal_date"]), []), sessions=10)
        if error and error != "INSUFFICIENT_FORWARD_SESSIONS":
            rejected[error] += 1
            continue
        if len(path) < 5:
            rejected["LESS_THAN_5_VALID_SESSIONS"] += 1
            continue
        for cohort, names, subpath in [
            ("COMPLETE_5D", ("TIME_5", "TRAIL_CLOSE_1R"), path[:5]),
            ("COMPLETE_10D", POLICIES, path),
        ]:
            if cohort == "COMPLETE_10D" and len(path) < 10:
                rejected["LESS_THAN_10_VALID_SESSIONS"] += 1
                continue
            outputs = [
                simulate_exit(
                    entry, subpath, name, roundtrip_cost_bps=cost_bps,
                    observation_cap=5 if cohort == "COMPLETE_5D" else 10,
                )
                for name in names
            ]
            if any(item is None for item in outputs):
                rejected["INVALID_ENTRY_STOP_OR_RISK"] += 1
                continue
            for name, item in zip(names, outputs):
                groups[cohort][name].append(item)

    views = {}
    for cohort, arms in groups.items():
        summaries = {name: _stats(items) for name, items in arms.items()}
        sizes = {(x["trades"], x["dates"]) for x in summaries.values()}
        n = next(iter(sizes))[0] if len(sizes) == 1 else 0
        base = summaries["TIME_5"]
        differentials = {}
        for name, result in summaries.items():
            if name == "TIME_5":
                continue
            differentials[name] = {
                "net_return_lift_pp": round(result["avg_net_return_pct"] - base["avg_net_return_pct"], 4) if n else None,
                "profit_factor_lift": round(result["profit_factor_net"] - base["profit_factor_net"], 4)
                if n and result["profit_factor_net"] is not None and base["profit_factor_net"] is not None else None,
                "avg_capital_sessions_delta": round(result["avg_held_sessions"] - base["avg_held_sessions"], 3) if n else None,
            }
        views[cohort] = {
            "status": "EVALUABLE" if n >= MIN_COMMON_TRADES and base["dates"] >= MIN_COMMON_DATES
                else "INSUFFICIENT_EVIDENCE",
            "same_entries_and_dates": len(sizes) == 1,
            "sample": n, "arms": summaries, "vs_time5": differentials,
            "last_entry_date": max((x["signal_date"] for x in next(iter(arms.values()))), default=None),
        }
    return {"cohorts": views, "rejected": dict(rejected), "input_entries": len(entries), "unique_candidate_entries": len(deduped)}


def _load_entries(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _load_db_price_paths(entries: list[dict], *, progress: Callable[[str], None] | None = None) -> tuple[dict[tuple[str, str], list[dict]], dict]:
    db = Path(config.ALERTS_DB)
    if not db.is_file():
        return {}, {"status": "DB_NOT_FOUND", "database": str(db)}
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=20.0)
    conn.row_factory = sqlite3.Row
    keys = sorted({(str(x.get("ticker") or "").upper(), _day(x.get("signal_date") or x.get("entry_date"))) for x in entries})
    paths = {}
    counts = Counter()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        price_columns = (
            {r[1] for r in conn.execute("PRAGMA table_info(price_cache)")}
            if "price_cache" in tables else set()
        )
        split_col = "split_factor" if "split_factor" in price_columns else "NULL AS split_factor"
        for index, (ticker, day) in enumerate(keys, 1):
            if progress is not None and (index == 1 or index % 250 == 0):
                progress(f"price_paths {index}/{len(keys)} | observation database read-only")
            if not ticker or not day:
                continue
            options = []
            if "price_cache" in tables:
                options = [dict(r) for r in conn.execute(
                    f"SELECT trade_date,open,high,low,close,source,{split_col} FROM price_cache WHERE ticker=? AND trade_date>? ORDER BY trade_date LIMIT 20",
                    (ticker, day),
                )]
                counts["price_cache_queries"] += 1
            # Only augment MISSING dates from mature observation outcomes.
            # Never replace credible raw OHLC with a close-only synthetic series.
            if "observation_outcomes" in tables:
                observations = conn.execute(
                    "SELECT sessions_json FROM observation_outcomes WHERE ticker=? AND observation_date=? AND complete=1 AND sessions_json IS NOT NULL ORDER BY horizon_sessions DESC,id DESC LIMIT 1",
                    (ticker, day),
                ).fetchone()
                if observations is not None:
                    try:
                        sessions = json.loads(observations[0])
                    except (ValueError, TypeError):
                        sessions = []
                    if isinstance(sessions, list):
                        existing = {str(r.get("trade_date") or "")[:10]: r for r in options}
                        for row in sessions:
                            if not isinstance(row, dict):
                                continue
                            key = str(row.get("trade_date") or row.get("date") or "")[:10]
                            if key and key not in existing:
                                existing[key] = {**row, "trade_date": key, "source": "OBSERVATION_OUTCOME_OHLC"}
                        options = list(existing.values())
                        counts["outcome_matches"] += 1
            paths[(ticker, day)] = sorted(options, key=lambda r: str(r.get("trade_date") or ""))
        # Research-only verified OHLC supplement. Canonical price_cache and
        # observation_outcomes remain read-only. Use a complete provider path
        # as one homogeneous price basis; never splice questionable partial
        # bars into a historical trade's forward trajectory.
        overlay_path = Path(config.DATA_ROOT) / "alerts_bot" / "db" / "research_ohlc_backfill.sqlite"
        if overlay_path.is_file():
            overlay = None
            try:
                overlay = sqlite3.connect(
                    f"file:{overlay_path.resolve().as_posix()}?mode=ro", uri=True, timeout=15.0
                )
                overlay.row_factory = sqlite3.Row
                for (ticker, day), original in paths.items():
                    raw = [dict(y) for y in overlay.execute(
                        "SELECT trade_date,open,high,low,close,source,split_factor "
                        "FROM research_ohlc WHERE ticker=? AND trade_date>? "
                        "ORDER BY trade_date LIMIT 20", (ticker, day)
                    )]
                    if len(raw) < 10:
                        counts["backfill_incomplete"] += 1
                        continue
                    research_entry = {"ticker": ticker, "signal_date": day}
                    complete, err = _future_path(research_entry, raw, sessions=10)
                    if err:
                        counts["backfill_rejected_" + err] += 1
                        continue
                    old_anchor = {
                        str(y.get("trade_date") or ""): y
                        for y in original
                        if _valid_ohlc(y, signal_date=day) is not None
                    }
                    overlap = [
                        abs(float(y["close"]) / float(old_anchor[y["trade_date"]]["close"]) - 1.0)
                        for y in raw if y["trade_date"] in old_anchor
                        and _f(old_anchor[y["trade_date"]].get("close"))
                    ]
                    if overlap and max(overlap) > 0.08:
                        counts["backfill_price_basis_conflict"] += 1
                        continue
                    current_path, current_error = _future_path(research_entry, original, sessions=10)
                    if current_error is None:
                        counts["backfill_existing_valid_kept"] += 1
                        continue
                    paths[(ticker, day)] = raw
                    counts["backfill_complete_paths_applied"] += 1
            except (sqlite3.Error, ValueError, ZeroDivisionError) as exc:
                counts["backfill_overlay_error"] += 1
                counts["backfill_overlay_error_type"] = type(exc).__name__
            finally:
                if overlay is not None:
                    overlay.close()
        return paths, {**dict(counts), "status": "OK", "unique_entry_keys": len(keys), "paths_loaded": len(paths)}
    finally:
        conn.close()


SOURCE_FILES = {
    "current_policy": "latest_historical_trade_ledger.json",
    "all_actionable": "latest_all_actionable_enter_candidates.json",
    "enter_first": "latest_enter_first_trade_ledger.json",
    "quality_gated": "latest_quality_gated_trade_ledger.json",
}


def run(*, cost_bps: float = DEFAULT_ROUNDTRIP_COST_BPS, source_lane: str = "all_actionable") -> dict:
    if source_lane not in SOURCE_FILES:
        raise ValueError("unknown source lane: " + str(source_lane))
    output = Path(config.OUTPUT_DIR) / "historical_validation"
    source = output / SOURCE_FILES[source_lane]
    print(f"[EXIT_RESEARCH] source={source_lane} | ledger={source} | reading entries", flush=True)
    entries = _load_entries(source)
    print(f"[EXIT_RESEARCH] entries={len(entries)} | read-only OHLC lookup started", flush=True)
    paths, coverage = _load_db_price_paths(
        entries, progress=lambda message: print(f"[EXIT_RESEARCH] {message}", flush=True)
    )
    print(f"[EXIT_RESEARCH] price_keys={len(paths)} | paired policy replay started", flush=True)
    scored = compare_entries(entries, paths, cost_bps=cost_bps)
    print(f"[EXIT_RESEARCH] 5D n={scored['cohorts']['COMPLETE_5D']['sample']} | 10D n={scored['cohorts']['COMPLETE_10D']['sample']} | publishing", flush=True)
    main = scored["cohorts"]["COMPLETE_10D"]
    # These are predeclared exploratory gates, NOT independent OOS success:
    # same frozen historical cohort was available during previous research.
    base = (main.get("arms") or {}).get("TIME_5") or {}
    survivors = []
    if main["status"] == "EVALUABLE":
        for name, metrics in main["arms"].items():
            if name == "TIME_5":
                continue
            superior = (
                metrics.get("avg_net_return_pct") is not None
                and base.get("avg_net_return_pct") is not None
                and metrics["avg_net_return_pct"] > base["avg_net_return_pct"]
                and metrics.get("profit_factor_net") is not None
                and base.get("profit_factor_net") is not None
                and metrics["profit_factor_net"] > base["profit_factor_net"]
                and metrics.get("avg_mae_pct") is not None
                and base.get("avg_mae_pct") is not None
                and metrics["avg_mae_pct"] >= base["avg_mae_pct"] - 0.5
            )
            if superior:
                survivors.append(name)
    decision = {
        "status": "INSUFFICIENT_EVIDENCE" if main["status"] != "EVALUABLE"
                  else ("EXPLORATORY_SURVIVORS_NOT_VALIDATED" if survivors
                        else "NO_EXIT_POLICY_EDGE_DEMONSTRATED"),
        "exploratory_survivors": survivors, "winning_policy": None,
        "promotion_allowed": False, "final_test_locked": True,
        "next_step": ("CHECK_PRICE_PATH_AND_TRADE_ENTRY_COVERAGE" if main["status"] != "EVALUABLE"
                      else "REQUIRE_CHRONOLOGICAL_OOS_AND_PROSPECTIVE_TRADES_BEFORE_POLICY_DECISION"),
    }
    payload = {
        "mode": MODE, "generated_at_madrid": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "producer_version": config.BOT_VERSION,
        "entry_source": str(source), "entry_source_exists": source.is_file(),
        "source_lane": source_lane,
        "cost_assumption_bps": cost_bps,
        "price_coverage": coverage, **scored, "decision": decision,
        "invariants": {
            "live_mutation": False, "final_test_locked": True,
            "no_new_full_replay": True, "no_lookahead_exit": True,
            "entry_source_is_historical_simulation_not_executed_fill": True,
            "next_session_open_is_research_entry_price": True,
            "intrabar_same_bar_stop_uses_conservative_fill": True,
            "stop_gaps_use_open": True,
            "trailing_uses_prior_close_peak_only": True,
            "gex_target_is_milestone_not_forced_exit": True,
            "no_forced_strategy_promotion": True,
            "no_simulated_real_time_structure": True,
            "missing_or_invalid_ohlc_fails_closed": True,
            "costs_are_assumptions_not_live_quotes": True,
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    atomic_stream_json(output / "latest_swing_exit_policy_comparison.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_ROUNDTRIP_COST_BPS)
    parser.add_argument("--source", choices=tuple(SOURCE_FILES), default="all_actionable")
    args = parser.parse_args()
    if not 0 <= args.cost_bps <= 200:
        parser.error("cost-bps must be between 0 and 200")
    result = run(cost_bps=args.cost_bps, source_lane=args.source)
    print(json.dumps({
        "mode": MODE, "decision": result["decision"],
        "10d": result["cohorts"]["COMPLETE_10D"]["status"],
        "10d_sample": result["cohorts"]["COMPLETE_10D"]["sample"],
        "5d": result["cohorts"]["COMPLETE_5D"]["status"],
        "5d_sample": result["cohorts"]["COMPLETE_5D"]["sample"],
        "rejected": result["rejected"],
        "artifact": str(Path(config.OUTPUT_DIR) / "historical_validation" / "latest_swing_exit_policy_comparison.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
