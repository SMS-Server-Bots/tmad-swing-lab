from __future__ import annotations

"""Detached, validation-only selection x direction factorial, no FULL replay.

Reuses the immutable Rolling/Fixed fast research caches. Fits feature models on
TRAIN and evaluates only VALIDATION; FINAL TEST is never evaluated. All four
arms share dates, point-in-time eligible universe, capacity and labels. This
does not claim randomized causal identification or after-cost profitability.
"""

from collections import Counter, defaultdict
from datetime import datetime
import gc
import json
from pathlib import Path
from statistics import mean
from typing import Any

import config
from autonomous_research_cache import cache_filename, load_fast_research_cache, split_cached_rows
from core_context_precision_optimizer import (
    _f, _family_models, _fast_cache_contract_hash, _fit_train_models,
    _profit_factor, _score_row,
)

MODE = "SELECTION_DIRECTION_FACTORIAL_VALIDATION_V1"
LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")
FAMILY = "CONTEXT_LEAN"  # Frozen diagnostic hypothesis, NOT retuned on test
CAPACITY = 5
ARMS = ("CONTROL_SELECTION_CORE_DIRECTION", "CONTEXT_SELECTION_CORE_DIRECTION",
        "CONTROL_SELECTION_CONTEXT_DIRECTION", "CONTEXT_SELECTION_CONTEXT_DIRECTION")


def _path(row: dict, direction: str) -> dict:
    return row.get("long_path" if direction == "LONG" else "short_path") or {}


def _path_evaluable(row: dict, direction: str) -> bool:
    p = _path(row, direction)
    return all(_f(p.get(k)) is not None for k in
               ("mfe_pct", "mae_pct", "horizon_close_return_pct"))


def _ticker_identity(row: dict) -> tuple[str, str]:
    return str(row.get("date") or ""), str(row.get("ticker") or "")


def _metrics(trades: list[dict]) -> dict:
    rets = [float(t["ret"]) for t in trades]
    return {
        "sample": len(trades),
        "dates": len({t["date"] for t in trades}),
        "movers_5d": sum(t["mfe"] >= 5.0 for t in trades),
        "precision_pct": round(100 * sum(t["mfe"] >= 5.0 for t in trades) / len(trades), 3) if trades else None,
        "avg_mfe_pct": round(mean(t["mfe"] for t in trades), 4) if trades else None,
        "avg_mae_pct": round(mean(t["mae"] for t in trades), 4) if trades else None,
        "avg_horizon_close_return_pct": round(mean(rets), 4) if rets else None,
        "horizon_close_profit_factor": _profit_factor(rets),
        "losses": sum(x < 0 for x in rets),
        "profit_after_costs_known": False,
    }


def _trade(item: dict, direction: str) -> dict:
    row = item["row"]
    p = _path(row, direction)
    diag = row.get("context_diagnostics") or {}
    features = (item.get("chosen") or row).get("context_features") or {}
    if diag.get("explicit_sector_etf"):
        coverage = "EXPLICIT_SAME_RUN"
    elif any(k.startswith("sector.") for k in features):
        coverage = "PROXY_ONLY"
    else:
        coverage = "UNKNOWN"
    return {
        "date": str(row["date"]), "ticker": str(row["ticker"]), "direction": direction,
        "mfe": float(p["mfe_pct"]), "mae": float(p["mae_pct"]),
        "ret": float(p["horizon_close_return_pct"]),
        "market_regime": str(diag.get("market_regime") or "UNKNOWN"),
        "sector_context": coverage,
    }


def evaluate_factorial(validation: list[dict], models: dict, *, capacity: int = CAPACITY) -> dict:
    """Compare exactly equal capacity/date/label coverage across 4 strategy arms."""
    by_day: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for row in validation:
        day, ticker = _ticker_identity(row)
        direction = str(row.get("context_direction") or "").upper()
        if day and ticker and direction in {"LONG", "SHORT"}:
            by_day[day][ticker][direction] = row

    trades: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    rejected = Counter()
    overlap = changed_direction = 0
    evaluated_dates = []
    capacities = {}
    available_count = 0
    context_coverage = Counter()

    for day, tickers in sorted(by_day.items()):
        eligible = []
        for ticker, pair in tickers.items():
            if not {"LONG", "SHORT"}.issubset(pair):
                rejected["MISSING_DIRECTION_HYPOTHESIS"] += 1
                continue
            source = pair["LONG"]
            current = str(source.get("current_direction") or "").upper()
            if current not in {"LONG", "SHORT"}:
                rejected["CURRENT_DIRECTION_UNKNOWN"] += 1
                continue
            if not (_path_evaluable(source, "LONG") and _path_evaluable(source, "SHORT")):
                rejected["INCOMPLETE_BIDIRECTIONAL_LABELS"] += 1
                continue
            if not (source.get("context_usable") and pair["SHORT"].get("context_usable")):
                rejected["POINT_IN_TIME_CONTEXT_MISSING"] += 1
                continue
            scored = []
            for direction in ("LONG", "SHORT"):
                score, _ = _score_row(pair[direction], models)
                if score is not None:
                    scored.append((float(score), direction, pair[direction]))
            if len(scored) != 2:
                rejected["CONTEXT_SCORE_UNAVAILABLE"] += 1
                continue
            best = max(scored, key=lambda x: (x[0], x[1]))
            eligible.append({
                "row": source, "new_direction": best[1], "chosen": best[2],
                "score": best[0], "current_direction": current,
                "ticker": ticker,
                "core": bool((source.get("stage") or {}).get("core")),
                "rank": tuple(source.get("research_rank") or ()),
            })

        control_pool = [x for x in eligible if x["core"]]
        available_count += len(eligible)
        if len(control_pool) < capacity or len(eligible) < capacity:
            rejected["INSUFFICIENT_SAME_DAY_CAPACITY"] += 1
            continue
        baseline = sorted(control_pool, key=lambda x: (x["rank"], x["ticker"]), reverse=True)[:capacity]
        challenger = sorted(eligible, key=lambda x: (x["score"], x["ticker"]), reverse=True)[:capacity]
        controls = {x["ticker"] for x in baseline}
        overlap += sum(x["ticker"] in controls for x in challenger)
        changed_direction += sum(x["new_direction"] != x["current_direction"] for x in challenger)
        capacities[day] = capacity
        evaluated_dates.append(day)
        for arm, selected, direction_key in (
            (ARMS[0], baseline, "current_direction"),
            (ARMS[1], challenger, "current_direction"),
            (ARMS[2], baseline, "new_direction"),
            (ARMS[3], challenger, "new_direction"),
        ):
            for item in selected:
                t = _trade(item, item[direction_key])
                trades[arm].append(t)
                context_coverage[t["sector_context"]] += 1

    summary = {arm: _metrics(trades[arm]) for arm in ARMS}
    same_denominators = bool(evaluated_dates) and len({
        (x["sample"], x["dates"]) for x in summary.values()
    }) == 1 and all(x["sample"] == len(evaluated_dates) * capacity for x in summary.values())
    deltas = {}
    for arm, control in ((ARMS[1], ARMS[0]), (ARMS[2], ARMS[0]), (ARMS[3], ARMS[1])):
        a, b = summary[arm], summary[control]
        deltas[arm + "_vs_" + control] = {
            "precision_pp": round(a["precision_pct"] - b["precision_pct"], 3) if same_denominators else None,
            "return_pp": round(a["avg_horizon_close_return_pct"] - b["avg_horizon_close_return_pct"], 4) if same_denominators else None,
        }
    false_positives = Counter()
    for t in trades[ARMS[3]]:
        if t["mfe"] < 5.0:
            false_positives[t["market_regime"] + "|" + t["sector_context"] + "|" + t["direction"]] += 1
    return {
        "status": "EVALUABLE" if same_denominators else "INSUFFICIENT_EVIDENCE",
        "candidate": FAMILY + "@TOP" + str(capacity),
        "selection": {"same_dates": evaluated_dates, "capacity_by_date": capacities,
                      "same_denominators": same_denominators, "eligible_tickers": available_count,
                      "shared_tickers_selected": overlap, "context_selection_direction_changes": changed_direction,
                      "rejection_reasons": dict(rejected)},
        "arms": summary, "paired_deltas": deltas,
        "false_positive_context": dict(false_positives),
        "sector_coverage_trade_arm_totals": dict(context_coverage),
        "methodology": {
            "same_dates_capacity_eligible_universe": True,
            "point_in_time_features_only": True,
            "new_direction_scored_without_future_labels": True,
            "train_only_feature_fit": True,
            "validation_only_no_final_test_evaluation": True,
            "portfolio_arms_have_different_tickers": True,
            "not_randomized_causal_identification": True,
            "gross_underlying_close_return_proxy": True,
            "option_fills_spread_slippage_and_fees_not_modelled": True,
        },
        "promotion_allowed": False, "live_mutation": False,
    }


def _load_lane(lane: str) -> dict:
    p = config.RUNTIME_ROOT / "autonomous_research" / "cache" / cache_filename(lane)
    payload = load_fast_research_cache(
        p, expected_contract_hash=_fast_cache_contract_hash(), verify_fingerprint=True
    )
    train, validation, _test = split_cached_rows(payload)
    models = _family_models(_fit_train_models(train), FAMILY)
    result = evaluate_factorial(validation, models)
    result["lane"] = lane
    result["cache"] = {
        "producer_version": payload.get("producer_version"),
        "dataset_fingerprint": payload.get("dataset_fingerprint"),
        "train_dates": len(payload.get("split_dates", {}).get("train") or []),
        "validation_dates": len(payload.get("split_dates", {}).get("validation") or []),
        "final_test_locked": True,
    }
    return result


def run() -> dict:
    results = {}
    for lane in LANES:
        try:
            results[lane] = _load_lane(lane)
        except Exception as exc:
            results[lane] = {"status": "CACHE_ERROR", "error": type(exc).__name__ + ": " + str(exc)}
        gc.collect()
    rolling, fixed = (results.get(lane) or {} for lane in LANES)
    evaluable = all(row.get("status") == "EVALUABLE" and
                    row.get("selection", {}).get("same_denominators") for row in (rolling, fixed))
    best = all(
        row["arms"][ARMS[1]]["avg_horizon_close_return_pct"] > 0
        and (row["arms"][ARMS[1]]["horizon_close_profit_factor"] or 0) > 1
        and row["arms"][ARMS[1]]["avg_horizon_close_return_pct"]
            >= row["arms"][ARMS[0]]["avg_horizon_close_return_pct"]
        for row in (rolling, fixed)
    ) if evaluable else False
    return {
        "mode": MODE, "generated_at": datetime.now().astimezone().isoformat(),
        "rolling_latest": rolling, "fixed_benchmark": fixed,
        "decision": {
            "status": "INSUFFICIENT_EVIDENCE" if not evaluable else (
                "REJECT" if not best else "VALIDATION_ONLY_REQUIRES_PROSPECTIVE_AND_COSTS"),
            "final_test_status": "LOCKED",
            "promotion_allowed": False, "live_mutation": False,
            "do_not_run_full_replay": True,
            "next_step": "Review same-denominator factorial and sector/regime failure map; require prospective SHADOW before any LIVE change",
        },
    }


if __name__ == "__main__":
    result = run()
    target = config.OUTPUT_DIR / "historical_validation" / "latest_selection_direction_factorial.json"
    from research_streaming_io import atomic_stream_json
    atomic_stream_json(target, result)
    print(json.dumps({"path": str(target), "decision": result["decision"], "lanes": {
        k: result[k].get("status") for k in ("rolling_latest", "fixed_benchmark")
    }}, ensure_ascii=False))
