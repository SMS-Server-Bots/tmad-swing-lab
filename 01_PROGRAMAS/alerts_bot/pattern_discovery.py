from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

import config
from db import (
    activate_provisional_model, create_mover_discovery_run, daily_price_series,
    finish_mover_discovery_run, get_active_model, get_core_feature_history, get_training_rows,
    insert_discovered_patterns, insert_model_feature_importance,
    insert_model_version, schedule_model,
)
from discord_utils import log_error, log_info, send_daily
from discord_presentation import build_group
from rules import exit_pattern_signal
from model_metrics import normalized_oos_metrics
from calibration_v621 import enrich_core_trades_with_gex, core_gex_selection_bonus, publish_calibration, active_layer_model
from time_utils import now_madrid, ts_madrid_str, ts_utc_str

LONG_RULES = [f"DISCOVERED_MOVER_LONG_{i}" for i in range(1, int(config.CORE_MOVER_DISCOVERY_MAX_PATTERNS_PER_DIRECTION) + 1)]
SHORT_RULES = [f"DISCOVERED_MOVER_SHORT_{i}" for i in range(1, int(config.CORE_MOVER_DISCOVERY_MAX_PATTERNS_PER_DIRECTION) + 1)]

# Current values plus genuine transitions.  Categorical states remain useful as
# numeric encodings (flow/trend direction), while free text is never mined.
DISCOVERY_FEATURES = (
    # Direct ticker state. CORE learns what tends to move ±5% within five
    # sessions; it does not use strike/expiry GEX to manufacture a target.
    "momentum_score", "scanner_trend_score", "rsi", "rvol", "change_percent",
    "momentum_5d_pct", "price_vs_sma20_pct", "price_vs_vwap_pct", "atr_pct",
    "realized_volatility_20d", "flow_direction", "trend_direction",
    # Broad regime and simple sector context are valid CORE inputs. Dynamic
    # graph propagation and all GEX fields are calibrated in separate layers.
    "market_context_score", "market_breadth_score", "vix_stress_score",
    "credit_risk_on_score", "market_deterioration_score",
    "sector_etf_score", "sector_breadth_score",
    "relative_strength_vs_market", "relative_strength_vs_sector",
    # Genuine point-in-time transitions.
    "delta_momentum_score", "delta_scanner_trend_score", "delta_rsi",
    "delta_rvol", "delta_change_percent", "delta_momentum_5d_pct",
    "delta_price_vs_sma20_pct", "delta_price_vs_vwap_pct",
    "delta_atr_pct", "delta_realized_volatility_20d",
    "delta_flow_direction", "delta_trend_direction",
    "delta_market_context_score", "delta_market_breadth_score",
    "delta_vix_stress_score", "delta_credit_risk_on_score",
    "delta_market_deterioration_score", "delta_sector_etf_score",
    "delta_sector_breadth_score", "delta_relative_strength_vs_market",
    "delta_relative_strength_vs_sector",
)


def _f(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except Exception:
        return None


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def _prepare_examples() -> list[dict]:
    rows = get_training_rows("core")
    selected: dict[int, dict] = {}
    for row in rows:
        try:
            if int(row.get("horizon_sessions") or 0) != int(config.MAX_PATTERN_HORIZON_SESSIONS):
                continue
            obs_id = int(row.get("id") or 0)
            features = json.loads(row.get("features_json") or "{}")
        except Exception:
            continue
        mfe = _f(row.get("mfe_long_pct")); mae = _f(row.get("mae_long_pct"))
        if not obs_id or mfe is None or mae is None:
            continue
        long_mover = mfe >= float(config.PATTERN_TARGET_PCT)
        short_mover = mae <= -float(config.PATTERN_TARGET_PCT)
        # A daily bar path cannot establish which side happened first when both
        # targets were reached.  Those observations remain in the audit counts,
        # but are excluded from directional pattern discovery.
        ambiguous = bool(long_mover and short_mover)
        selected[obs_id] = {
            **row, "features": features, "long_mover": long_mover,
            "short_mover": short_mover, "ambiguous": ambiguous,
        }
    return sorted(selected.values(), key=lambda r: (str(r.get("snapshot_date") or ""), str(r.get("ticker") or ""), int(r.get("id") or 0)))


def _condition_match(features: dict, condition: dict) -> bool:
    value = _f(features.get(str(condition.get("feature") or "")))
    if value is None:
        return False
    threshold = float(condition.get("threshold") or 0.0)
    return value >= threshold if condition.get("op") == ">=" else value <= threshold


def _pattern_match(example: dict, pattern: dict) -> bool:
    return all(_condition_match(example.get("features") or {}, item) for item in pattern.get("conditions") or [])


def _stats(examples: list[dict], indexes: list[int], direction: str) -> dict:
    label = "long_mover" if direction == "long" else "short_mover"
    usable = [row for row in examples if not row.get("ambiguous")]
    positive_total = sum(1 for row in usable if row.get(label))
    selected = [usable[i] for i in indexes]
    positives = sum(1 for row in selected if row.get(label))
    controls = len(selected) - positives
    base = positive_total / len(usable) if usable else 0.0
    precision = positives / len(selected) if selected else 0.0
    recall = positives / positive_total if positive_total else 0.0
    lift = precision / base if base > 0 else 0.0
    return {
        "selected": len(selected), "positive": positives, "control": controls,
        "precision": precision, "recall": recall, "lift": lift,
        "base_rate": base, "positive_total": positive_total, "usable_total": len(usable),
    }


def _candidate_score(stats: dict, conditions: int) -> float:
    if not stats.get("selected"):
        return -999.0
    support = math.log1p(stats["positive"])
    complexity = max(0, conditions - 1) * 0.12
    return (
        stats["precision"] * 3.0 + min(stats["lift"], 5.0) * 0.75
        + stats["recall"] * 1.25 + support * 0.18 - complexity
    )


def _selected_indexes(examples: list[dict], conditions: list[dict]) -> list[int]:
    usable = [row for row in examples if not row.get("ambiguous")]
    return [i for i, row in enumerate(usable) if all(_condition_match(row.get("features") or {}, c) for c in conditions)]


def _valid_stats(stats: dict, total_rows: int) -> bool:
    min_selected = max(10, min(40, int(total_rows * 0.003)))
    min_positive = max(5, min(15, int(stats.get("positive_total", 0) * 0.015)))
    return bool(
        stats["selected"] >= min_selected
        and stats["positive"] >= min_positive
        and stats["lift"] >= 1.12
        and stats["precision"] >= stats["base_rate"] + 0.035
    )



def _feature_family(feature: str) -> str:
    name = str(feature or "").lower()
    if "atr" in name or "volatility" in name or "rvol" in name:
        return "VOLATILITY_PARTICIPATION"
    if "market_breadth" in name or "market_context" in name or "vix" in name or "credit" in name or "market_deterioration" in name:
        return "MARKET_REGIME"
    if "sector" in name or "relative_strength" in name:
        return "SECTOR_RELATIVE_STRENGTH"
    if "flow" in name:
        return "OPTIONS_FLOW"
    if "momentum" in name or "trend" in name or "sma" in name or "vwap" in name:
        return "MOMENTUM_TREND"
    if "rsi" in name or "change_percent" in name:
        return "PRICE_EXTENSION_REVERSAL"
    return "OTHER"


def _pattern_family(pattern: dict) -> str:
    families = sorted({_feature_family(c.get("feature")) for c in pattern.get("conditions") or []})
    return "+".join(families) or "OTHER"

def _discover_direction(examples: list[dict], direction: str, max_patterns: int | None = None) -> list[dict]:
    if max_patterns is None:
        max_patterns = int(config.CORE_MOVER_DISCOVERY_MAX_PATTERNS_PER_DIRECTION)
    usable = [row for row in examples if not row.get("ambiguous")]
    if len(usable) < 100:
        return []
    singles: list[dict] = []
    for feature in DISCOVERY_FEATURES:
        values = [_f((row.get("features") or {}).get(feature)) for row in usable]
        numeric = [v for v in values if v is not None]
        if len(numeric) < max(30, int(len(usable) * 0.35)):
            continue
        thresholds = sorted({round(_quantile(numeric, q), 8) for q in (0.10,0.20,0.30,0.40,0.50,0.60,0.70,0.80,0.90)})
        for threshold in thresholds:
            for op in (">=", "<="):
                condition = {"feature": feature, "op": op, "threshold": threshold}
                idx = _selected_indexes(usable, [condition])
                stats = _stats(usable, idx, direction)
                if _valid_stats(stats, len(usable)):
                    singles.append({"conditions": [condition], "stats": stats, "score": _candidate_score(stats, 1), "indexes": idx})
    singles.sort(key=lambda item: item["score"], reverse=True)
    top = singles[:24]
    candidates = list(top)

    # Interactions are essential: a setup is usually a transition plus context,
    # not a single extreme variable.  The bounded search prevents overfitting.
    for i in range(min(16, len(top))):
        for j in range(i + 1, min(16, len(top))):
            a, b = top[i]["conditions"][0], top[j]["conditions"][0]
            if a["feature"] == b["feature"]:
                continue
            conditions = [a, b]
            idx = _selected_indexes(usable, conditions)
            stats = _stats(usable, idx, direction)
            if _valid_stats(stats, len(usable)):
                candidates.append({"conditions": conditions, "stats": stats, "score": _candidate_score(stats, 2), "indexes": idx})

    pairs = sorted([c for c in candidates if len(c["conditions"]) == 2], key=lambda x: x["score"], reverse=True)[:12]
    for pair in pairs:
        used = {c["feature"] for c in pair["conditions"]}
        for single in top[:10]:
            condition = single["conditions"][0]
            if condition["feature"] in used:
                continue
            conditions = [*pair["conditions"], condition]
            idx = _selected_indexes(usable, conditions)
            stats = _stats(usable, idx, direction)
            if _valid_stats(stats, len(usable)):
                candidates.append({"conditions": conditions, "stats": stats, "score": _candidate_score(stats, 3), "indexes": idx})

    candidates.sort(key=lambda item: item["score"], reverse=True)
    chosen: list[dict] = []
    used_families: set[str] = set()
    family_cap = min(int(max_patterns), int(config.CORE_DISCOVERY_MAX_UNIQUE_FAMILIES_PER_DIRECTION))
    for candidate in candidates:
        family = _pattern_family(candidate)
        if family in used_families:
            continue
        selected = set(candidate["indexes"])
        redundant = False
        for existing in chosen:
            other = set(existing["indexes"])
            union = len(selected | other)
            jaccard = len(selected & other) / union if union else 1.0
            if jaccard >= 0.72:
                redundant = True
                break
        if redundant:
            continue
        candidate = {**candidate, "family": family}
        chosen.append(candidate)
        used_families.add(family)
        if len(chosen) >= family_cap:
            break
    return [{k: v for k, v in item.items() if k != "indexes"} for item in chosen]


def _future_prices(prices: dict[str, list[dict]], ticker: str, entry_date: str, hold: int) -> list[dict]:
    return [row for row in prices.get(ticker, []) if str(row.get("trade_date") or "") > entry_date][:hold]


def _simulate(
    example: dict,
    direction: str,
    score: float,
    rule_name: str,
    prices: dict[str, list[dict]],
    feature_history: dict[str, dict[str, dict]],
) -> dict | None:
    ticker = str(example.get("ticker") or ""); entry_date = str(example.get("snapshot_date") or "")
    entry = _f(example.get("current_price"))
    if not ticker or not entry_date or not entry or entry <= 0:
        return None
    hold = int(config.MAX_PATTERN_HORIZON_SESSIONS)
    future = _future_prices(prices, ticker, entry_date, hold)
    if len(future) < hold:
        return None
    target = float(config.PATTERN_TARGET_PCT)
    best = 0.0; worst = 0.0
    exit_ret = None; exit_date = None; exit_reason = None; exit_index = None; exit_price = None
    soft_exit_streak = 0
    for index, day in enumerate(future, start=1):
        close = _f(day.get("close")); high = _f(day.get("high")) or close; low = _f(day.get("low")) or close
        if close is None or high is None or low is None:
            return None
        if direction == "long":
            favorable = (high / entry - 1.0) * 100.0; adverse = (low / entry - 1.0) * 100.0
            close_ret = (close / entry - 1.0) * 100.0
        else:
            favorable = -(low / entry - 1.0) * 100.0; adverse = -(high / entry - 1.0) * 100.0
            close_ret = -(close / entry - 1.0) * 100.0
        best = max(best, favorable); worst = min(worst, adverse)
        if favorable >= target:
            exit_ret = target; exit_date = str(day.get("trade_date")); exit_reason = "TARGET_5PCT"; exit_index = index - 1; exit_price = entry * (1.0 + target/100.0) if direction == "long" else entry * (1.0 - target/100.0); break
        day_features = (feature_history.get(ticker) or {}).get(str(day.get("trade_date") or ""))
        signal = exit_pattern_signal(direction, day_features, {}, close_ret, best) if day_features else None
        if signal:
            if bool(signal.get("hard")):
                soft_exit_streak = int(config.SOFT_EXIT_CONFIRMATION_RUNS)
            else:
                soft_exit_streak += 1
            if soft_exit_streak >= int(config.SOFT_EXIT_CONFIRMATION_RUNS):
                exit_ret = close_ret; exit_date = str(day.get("trade_date"));
                exit_reason = str(signal.get("reason") or "PATTERN_REVERSAL"); exit_index = index - 1; exit_price = close; break
        else:
            soft_exit_streak = 0
        if index == hold:
            exit_ret = close_ret; exit_date = str(day.get("trade_date")); exit_reason = "MAX_5_SESSIONS"; exit_index = index - 1; exit_price = close
    if exit_ret is None or exit_index is None:
        return None
    target_after_invalidation = False
    if str(exit_reason).startswith("PATTERN_REVERSAL"):
        for day in future[exit_index + 1:]:
            high = _f(day.get("high")) or _f(day.get("close")); low = _f(day.get("low")) or _f(day.get("close"))
            if high is None or low is None:
                continue
            favorable = (high / entry - 1.0) * 100.0 if direction == "long" else -(low / entry - 1.0) * 100.0
            if favorable >= target:
                target_after_invalidation = True
                break
    return {
        "ticker": ticker, "entry_date": entry_date, "exit_date": exit_date,
        "entry_price": round(entry, 6), "exit_price": round(float(exit_price), 6) if exit_price is not None else None,
        "direction": direction, "rule_name": rule_name, "score": round(score, 4),
        "return_pct": round(exit_ret, 6), "mfe_pct": round(best, 6),
        "mae_pct": round(worst, 6), "target_5pct_hit": best >= target,
        "exit_reason": exit_reason,
        "target_after_invalidation": target_after_invalidation,
        "exit_session": int(exit_index + 1),
        "target_session": int(exit_index + 1) if best >= target else None,
        "market_regime": (example.get("features") or {}).get("market_regime"),
        "sector_etf": (example.get("features") or {}).get("sector_etf"),
    }


def _pattern_score(pattern: dict, example: dict) -> float:
    stats = pattern.get("stats") or {}
    margins: list[float] = []
    features = example.get("features") or {}
    for condition in pattern.get("conditions") or []:
        value = _f(features.get(condition["feature"])); threshold = float(condition["threshold"])
        if value is None:
            continue
        scale = max(abs(threshold), 1.0)
        margin = (value - threshold) / scale if condition["op"] == ">=" else (threshold - value) / scale
        margins.append(max(0.0, min(1.0, margin)))
    confidence = float(stats.get("precision") or 0.0)
    lift = float(stats.get("lift") or 0.0)
    return min(100.0, 55.0 + confidence * 25.0 + min(lift, 3.0) * 5.0 + (mean(margins) if margins else 0.0) * 5.0)


def _apply_patterns(examples: list[dict], patterns_by_direction: dict[str, list[dict]], prices: dict[str, list[dict]], feature_history: dict[str, dict[str, dict]]) -> list[dict]:
    by_day: dict[str, list[tuple[float, dict, str, str]]] = defaultdict(list)
    for example in examples:
        # Do not filter OOS signals using future outcome labels. Ambiguous labels
        # may be excluded from pattern fitting, but live/test selection cannot know them.
        best = None
        for direction, patterns in patterns_by_direction.items():
            for rank, pattern in enumerate(patterns, start=1):
                if not _pattern_match(example, pattern):
                    continue
                rule_name = (LONG_RULES if direction == "long" else SHORT_RULES)[rank - 1]
                score = _pattern_score(pattern, example)
                item = (score, example, direction, rule_name)
                if best is None or score > best[0]:
                    best = item
        if best:
            by_day[str(example.get("snapshot_date") or "")].append(best)
    trades: list[dict] = []
    blocked_until: dict[str, str] = {}
    for day in sorted(by_day):
        sector_count: Counter[str] = Counter(); direction_count: Counter[str] = Counter(); accepted = 0
        for score, example, direction, rule_name in sorted(by_day[day], key=lambda x: x[0], reverse=True):
            if accepted >= int(config.PRECISION_MAX_ALERTS_PER_DAY):
                break
            ticker = str(example.get("ticker") or "")
            if blocked_until.get(ticker, "") >= day:
                continue
            sector = str((example.get("features") or {}).get("sector_etf") or "UNMAPPED")
            if sector_count[sector] >= int(config.PRECISION_MAX_SIGNALS_PER_SECTOR):
                continue
            if direction_count[direction] >= int(config.PRECISION_MAX_SIGNALS_PER_DIRECTION):
                continue
            trade = _simulate(example, direction, score, rule_name, prices, feature_history)
            if not trade:
                continue
            trades.append(trade); blocked_until[ticker] = str(trade["exit_date"])
            sector_count[sector] += 1; direction_count[direction] += 1; accepted += 1
    return trades


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"sample":0,"market_dates":0,"win_rate":0.0,"target_hit_rate":0.0,"avg_return":0.0,"profit_factor":0.0,"max_drawdown":0.0,"avg_trades_per_day":0.0,"objective":-999.0,"pattern_invalidation_exits":0,"post_invalidation_target_count":0,"post_invalidation_target_rate":None}
    returns = [float(t["return_pct"]) for t in trades]
    wins = [r for r in returns if r > 0]; losses = [r for r in returns if r < 0]
    by_day: dict[str,list[float]] = defaultdict(list)
    for trade in trades: by_day[str(trade["entry_date"])].append(float(trade["return_pct"]))
    daily = [mean(by_day[d]) for d in sorted(by_day)]
    equity=peak=maxdd=0.0
    for value in daily:
        equity += value; peak=max(peak,equity); maxdd=max(maxdd,peak-equity)
    pf = sum(wins)/abs(sum(losses)) if losses else (5.0 if wins else 0.0)
    avg=mean(returns); win=len(wins)/len(returns)*100.0; hit=sum(1 for t in trades if t.get("target_5pct_hit"))/len(trades)*100.0
    objective = avg + (pf-1.0)*0.25 + (win-50.0)*0.015 - maxdd*0.08
    invalidation = [t for t in trades if str(t.get("exit_reason") or "").startswith("PATTERN_REVERSAL")]
    post_count = sum(1 for t in invalidation if t.get("target_after_invalidation"))
    post_rate = (post_count / len(invalidation) * 100.0) if invalidation else None
    target_sessions=[int(t.get("target_session")) for t in trades if t.get("target_session") is not None]
    return {"sample":len(trades),"market_dates":len(daily),"win_rate":win,"target_hit_rate":hit,"avg_return":avg,"profit_factor":pf,"max_drawdown":maxdd,"avg_trades_per_day":len(trades)/max(1,len(daily)),"objective":objective,"median_return":median(returns),"avg_mfe_pct":mean([float(t.get("mfe_pct") or 0.0) for t in trades]),"avg_mae_pct":mean([float(t.get("mae_pct") or 0.0) for t in trades]),"avg_exit_session":mean([int(t.get("exit_session") or 0) for t in trades]),"avg_target_session":(mean(target_sessions) if target_sessions else None),"pattern_invalidation_exits":len(invalidation),"post_invalidation_target_count":post_count,"post_invalidation_target_rate":post_rate}


def _folds(dates: list[str]) -> list[dict]:
    purge = max(int(config.MAX_PATTERN_HORIZON_SESSIONS), int(config.WALKFORWARD_PURGE_SESSIONS))
    min_train = max(8, int(config.WALKFORWARD_MIN_TRAIN_DATES))
    out=[]
    for test_index in range(min_train + purge, len(dates)):
        train_end = test_index - purge
        train_dates = dates[max(0, train_end-int(config.WALKFORWARD_MAX_TRAIN_DATES)):train_end]
        if len(train_dates) < min_train: continue
        out.append({"train_dates":train_dates,"test_date":dates[test_index]})
    max_folds = int(getattr(config, "CORE_EXPANDING_MAX_FOLDS", max(504, int(config.WALKFORWARD_MAX_FOLDS))))
    return out[-max_folds:]


def _pattern_rows(patterns_by_direction: dict[str,list[dict]], discovery_run_id: int, model_id: int | None = None) -> list[dict]:
    rows=[]
    for direction, patterns in patterns_by_direction.items():
        names = LONG_RULES if direction == "long" else SHORT_RULES
        for rank, pattern in enumerate(patterns, start=1):
            stats=pattern.get("stats") or {}
            rows.append({"discovery_run_id":discovery_run_id,"model_version_id":model_id,"direction":direction,"pattern_rank":rank,"rule_name":names[rank-1],"conditions_json":json.dumps(pattern.get("conditions") or [],ensure_ascii=False),"train_selected":stats.get("selected"),"train_positive":stats.get("positive"),"train_control":stats.get("control"),"train_precision":stats.get("precision"),"train_recall":stats.get("recall"),"train_lift":stats.get("lift"),"score":pattern.get("score")})
    return rows


def _write_report(run_key: str, examples: list[dict], patterns: dict, folds_payload: list[dict], oos_trades: list[dict], metrics: dict) -> str:
    output = config.OUTPUT_DIR / run_key; output.mkdir(parents=True, exist_ok=True)
    counts={"observations":len(examples),"long_movers":sum(1 for e in examples if e.get("long_mover") and not e.get("ambiguous")),"short_movers":sum(1 for e in examples if e.get("short_mover") and not e.get("ambiguous")),"ambiguous":sum(1 for e in examples if e.get("ambiguous")),"controls_long":sum(1 for e in examples if not e.get("long_mover") and not e.get("ambiguous")),"controls_short":sum(1 for e in examples if not e.get("short_mover") and not e.get("ambiguous"))}
    payload={"method":"mover_vs_control_transition_discovery","target":"directional ±5% within five sessions","counts":counts,"patterns":patterns,"folds":folds_payload,"oos_metrics":metrics,"generated_at_madrid":ts_madrid_str()}
    (output/'discovery_summary.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    with (output/'oos_trades.csv').open('w',newline='',encoding='utf-8-sig') as handle:
        fields=["entry_date","exit_date","ticker","direction","rule_name","score","entry_price","exit_price","return_pct","mfe_pct","mae_pct","target_5pct_hit","exit_session","target_session","gex_validation_status","market_regime","sector_etf"]
        writer=csv.DictWriter(handle,fieldnames=fields,extrasaction='ignore'); writer.writeheader(); writer.writerows(oos_trades)
    with (output/'patterns.csv').open('w',newline='',encoding='utf-8-sig') as handle:
        fields=["direction","rank","rule_name","conditions","selected","positive","control","precision","recall","lift","score"]
        writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader()
        for direction, items in patterns.items():
            names=LONG_RULES if direction=='long' else SHORT_RULES
            for rank,p in enumerate(items,1):
                s=p.get('stats') or {}; writer.writerow({"direction":direction,"rank":rank,"rule_name":names[rank-1],"conditions":json.dumps(p.get('conditions') or [],ensure_ascii=False),"selected":s.get('selected'),"positive":s.get('positive'),"control":s.get('control'),"precision":s.get('precision'),"recall":s.get('recall'),"lift":s.get('lift'),"score":p.get('score')})
    config.CORE_HISTORY_REPORT_FILE.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    return str(output)


def _candidate_beats_active_provisional(candidate_metrics: dict, active_model: dict | None) -> tuple[bool, list[str]]:
    """Select the valid CORE model with the best point-in-time OOS result.

    Minimum sample/quality gates are evaluated before this function. Once two
    models are comparable, the effective model is the one with the higher
    selection score (CORE objective plus a bounded GEX-confirmation bonus).
    """
    if not active_model:
        return True, ["sin modelo activo comparable"]
    active_metrics = normalized_oos_metrics(active_model.get("metrics") or {})
    if not active_metrics:
        return True, ["activo sin métricas OOS comparables"]
    candidate_score = float(candidate_metrics.get("selection_score") or candidate_metrics.get("objective") or -999.0)
    active_score = float((active_model.get("metrics") or {}).get("selection_score") or active_model.get("objective_score") or active_metrics.get("objective") or -999.0)
    if candidate_score > active_score + 1e-9:
        return True, [f"score selección {active_score:.3f} → {candidate_score:.3f}"]
    return False, [f"score selección {candidate_score:.3f} no mejora activo {active_score:.3f}"]


def run_mover_discovery(startup: bool = False) -> int | None:
    examples = _prepare_examples()
    dates = sorted({str(e.get("snapshot_date") or "") for e in examples if e.get("snapshot_date")})
    log_info(
        "🔄 Descubrimiento CORE · preparando muestra | "
        f"observaciones={len(examples)} · fechas={len(dates)}",
        send_discord=False,
    )
    if len(dates) < int(config.CORE_HISTORY_MIN_FULLY_LABELED_DATES):
        log_info(f"ℹ️ Descubrimiento movers: fechas etiquetadas insuficientes {len(dates)}/{config.CORE_HISTORY_MIN_FULLY_LABELED_DATES}.", send_discord=False)
        return None
    run_id = create_mover_discovery_run(dates[0], dates[-1])
    try:
        prices = daily_price_series(); feature_history = get_core_feature_history(); folds=_folds(dates); oos=[]; folds_payload=[]
        total_folds = len(folds)
        log_info(
            "🔄 Descubrimiento CORE · walk-forward iniciado | "
            f"folds={total_folds} · fechas={dates[0]}→{dates[-1]}",
            send_discord=False,
        )
        for fold_index, fold in enumerate(folds, 1):
            train_set=set(fold['train_dates']); test_date=str(fold['test_date'])
            train=[e for e in examples if str(e.get('snapshot_date')) in train_set]
            test=[e for e in examples if str(e.get('snapshot_date'))==test_date]
            patterns={"long":_discover_direction(train,"long"),"short":_discover_direction(train,"short")}
            trades=_apply_patterns(test,patterns,prices,feature_history); oos.extend(trades)
            folds_payload.append({"test_date":test_date,"train_start":fold['train_dates'][0],"train_end":fold['train_dates'][-1],"train_rows":len(train),"long_patterns":len(patterns['long']),"short_patterns":len(patterns['short']),"trades":len(trades)})
            if fold_index == 1 or fold_index == total_folds or fold_index % 5 == 0:
                log_info(
                    "   ↳ Descubrimiento CORE "
                    f"{fold_index}/{total_folds} folds · test={test_date} · OOS acumuladas={len(oos)}",
                    send_discord=False,
                )
        oos, gex_validation = enrich_core_trades_with_gex(oos)
        metrics=_metrics(oos)
        metrics["gex_validation"] = gex_validation
        metrics["gex_confirmation_bonus"] = core_gex_selection_bonus(gex_validation)
        metrics["selection_score"] = float(metrics.get("objective") or 0.0) + float(metrics.get("gex_confirmation_bonus") or 0.0)
        log_info(
            "🔄 Descubrimiento CORE · seleccionando patrones finales y generando reporte…",
            send_discord=False,
        )
        usable_total=max(1,sum(1 for e in examples if not e.get('ambiguous')))
        base_long=sum(1 for e in examples if e.get('long_mover') and not e.get('ambiguous'))/usable_total*100.0
        base_short=sum(1 for e in examples if e.get('short_mover') and not e.get('ambiguous'))/usable_total*100.0
        direction_counts=Counter(str(t.get('direction') or '') for t in oos)
        weighted_base=((direction_counts.get('long',0)*base_long)+(direction_counts.get('short',0)*base_short))/max(1,len(oos))
        metrics['base_mover_rate_pct']=weighted_base
        metrics['discovery_lift']=((metrics.get('target_hit_rate') or 0.0)/weighted_base if weighted_base>0 else None)
        final_patterns={"long":_discover_direction(examples,"long"),"short":_discover_direction(examples,"short")}
        run_key=f"mover_discovery_{now_madrid().strftime('%Y%m%d_%H%M%S')}"
        report_dir=_write_report(run_key,examples,final_patterns,folds_payload,oos,metrics)
        count_long=sum(1 for e in examples if e.get('long_mover') and not e.get('ambiguous'))
        count_short=sum(1 for e in examples if e.get('short_mover') and not e.get('ambiguous'))
        active=get_active_model()
        rules={}
        for direction, patterns in final_patterns.items():
            names=LONG_RULES if direction=='long' else SHORT_RULES
            for rank, pattern in enumerate(patterns,1):
                rules[names[rank-1]]={"enabled":True,"family":pattern.get('family') or _pattern_family(pattern),"conditions":pattern.get('conditions') or [],"discovery_stats":pattern.get('stats') or {},"min_score":70.0,"target_pct":float(config.PATTERN_TARGET_PCT),"stop_pct":0.0,"hold_sessions":int(config.MAX_PATTERN_HORIZON_SESSIONS),"intraday_threshold_factor":1.0,"min_daily_pattern_projection":100.0,"intraday_calibrated":False}
        rules['_portfolio']={"max_signals_per_sector":int(config.PRECISION_MAX_SIGNALS_PER_SECTOR),"max_signals_per_direction":int(config.PRECISION_MAX_SIGNALS_PER_DIRECTION)}
        invalidation_proven = bool(
            int(metrics.get('pattern_invalidation_exits') or 0) >= 3
            and int(metrics.get('post_invalidation_target_count') or 0) == 0
        )
        passes_shadow=bool(metrics['sample']>=8 and metrics['market_dates']>=4 and metrics['win_rate']>=50.0 and metrics['avg_return']>0 and metrics['profit_factor']>=1.0 and (final_patterns['long'] or final_patterns['short']))
        passes_champion=bool(metrics['sample']>=int(config.PRECISION_MIN_VALIDATION_TRADES) and metrics['market_dates']>=int(config.PRECISION_MIN_OOS_DATES) and metrics['win_rate']>=float(config.PRECISION_MIN_PROMOTION_WIN_RATE) and metrics['avg_return']>=float(config.PRECISION_MIN_PROMOTION_AVG_RETURN) and metrics['profit_factor']>=float(config.PRECISION_MIN_PROMOTION_PROFIT_FACTOR) and metrics['max_drawdown']<=float(config.PRECISION_MAX_PROMOTION_DRAWDOWN) and invalidation_proven)
        log_info(
            "🔄 Descubrimiento CORE · persistiendo modelo y métricas OOS…",
            send_discord=False,
        )
        model_key=f"core-mover-discovery-{now_madrid().strftime('%Y%m%d-%H%M%S')}"
        model_params={"version":11,"feature_tier":"CORE","source_kind":"TMAD_DETAIL_GROWING","universe_quality":"CURRENT_UNIVERSE_BACKFILL","method":"mover_vs_control_transition_discovery_expanding_purged_walkforward","trained_at_madrid":ts_madrid_str(),"pattern_target_pct":float(config.PATTERN_TARGET_PCT),"max_horizon_sessions":int(config.MAX_PATTERN_HORIZON_SESSIONS),"rules":rules}
        per_rule=defaultdict(list)
        for trade in oos: per_rule[str(trade.get('rule_name'))].append(trade)
        per_rule_metrics={name:_metrics(items) for name,items in per_rule.items()}
        metrics_payload={"discovery":{"long_movers":count_long,"short_movers":count_short,"ambiguous":sum(1 for e in examples if e.get('ambiguous')),"controls_compared":True,"transition_features":True,"patterns":final_patterns},"walk_forward":metrics,"folds":folds_payload,"precision_rule_oos_metrics":per_rule_metrics,"report_dir":report_dir,"target_definition":"Mover long reaches +5%; mover short reaches -5% within five sessions. Ambiguous two-sided bars excluded from directional discovery.","control_definition":"Same historical Scanner universe/date observations that did not reach the directional ±5% target.","validation_design":"Expanding point-in-time history; patterns discovered only on prior dates; five-session purge; every later eligible date tested OOS; maximum two trades per day; GEX validates CORE conditionally without leaking future data.","invalidation_validation":{"minimum_confirmed_exits":3,"post_invalidation_targets_allowed":0,"passed":invalidation_proven},"gex_validation":gex_validation,"selection_score":metrics.get("selection_score")}
        status='challenger' if passes_champion else ('discovery_shadow' if passes_shadow else 'rejected')
        model_id=insert_model_version({"model_key":model_key,"created_at_utc":ts_utc_str(),"created_at_madrid":ts_madrid_str(),"effective_from_date":now_madrid().date().isoformat(),"training_start_date":dates[0],"training_end_date":dates[-1],"window_days":len(dates),"status":status,"parent_model_id":active.get('id'),"objective_score":metrics['selection_score'],"train_sample":len(examples),"validation_sample":metrics['sample'],"validation_win_rate":metrics['win_rate'],"validation_avg_return_pct":metrics['avg_return'],"validation_profit_factor":metrics['profit_factor'],"params_json":json.dumps(model_params,ensure_ascii=False),"metrics_json":json.dumps(metrics_payload,ensure_ascii=False),"reason":f"movers long={count_long}; short={count_short}; controls=yes; OOS n={metrics['sample']} days={metrics['market_dates']} win={metrics['win_rate']:.2f} avg={metrics['avg_return']:.4f} PF={metrics['profit_factor']:.3f}; shadow={passes_shadow}; champion={passes_champion}; report={report_dir}"})
        rows=_pattern_rows(final_patterns,run_id,model_id); insert_discovered_patterns(rows)
        importance=[]
        for direction,items in final_patterns.items():
            for pattern in items:
                for condition in pattern.get('conditions') or []:
                    importance.append({"feature_name":condition['feature'],"importance":float((pattern.get('stats') or {}).get('lift') or 0.0),"method":"mover_control_lift","sample_size":int((pattern.get('stats') or {}).get('selected') or 0),"details":{"direction":direction,"condition":condition,"stats":pattern.get('stats') or {}}})
        insert_model_feature_importance(model_id,importance)
        activated=None
        activation_blocks: list[str] = []
        active_before = active
        should_activate, activation_blocks = _candidate_beats_active_provisional(metrics, active_before)
        if str((active_before or {}).get("status") or "").lower() == "champion" and not passes_champion:
            should_activate = False
            activation_blocks = ["el activo es CHAMPION; el challenger no supera los gates CHAMPION"]
        if passes_champion and config.AUTO_PROMOTE_ENABLED and should_activate:
            schedule_model(model_id, effective_date=(now_madrid().date()+timedelta(days=1)).isoformat())
            tuning_status='scheduled_best_champion'
        elif passes_shadow and should_activate and not bool(getattr(config, "ADAPTIVE_LEARNING_ENABLED", False)):
            activated=activate_provisional_model(model_id)
            tuning_status='discovery_best_active' if activated==model_id else 'activation_deferred'
        elif passes_shadow and should_activate:
            tuning_status='adaptive_challenger_ready'
        elif passes_shadow:
            tuning_status='challenger_preserved_active'
        else:
            tuning_status='discovery_rejected_no_activation'
        finish_mover_discovery_run(run_id,status=tuning_status,observations=len(examples),long_movers=count_long,short_movers=count_short,controls=max(0,len(examples)-count_long-count_short),oos_trades=metrics['sample'],oos_dates=metrics['market_dates'],model_version_id=model_id,report_dir=report_dir,summary_json=json.dumps(metrics_payload,ensure_ascii=False))
        log_info(
            f"✅ Descubrimiento movers ±5% | model={model_key} | status={tuning_status} | "
            f"long={count_long} short={count_short} | patterns={len(final_patterns['long'])+len(final_patterns['short'])} | "
            f"OOS n={metrics['sample']} days={metrics['market_dates']} "
            f"hit5={metrics['target_hit_rate']:.2f}% win={metrics['win_rate']:.2f}% "
            f"avg={metrics['avg_return']:+.3f}% PF={metrics['profit_factor']:.2f} MaxDD={metrics['max_drawdown']:.2f}",
            send_discord=False,
        )
        rules_short=[]
        for direction in ("long","short"):
            for rank, pattern in enumerate(final_patterns.get(direction) or [],1):
                conditions=pattern.get("conditions") or []
                if conditions:
                    c=conditions[0]
                    rules_short.append(f"{direction.upper()}{rank}: {c.get('feature')} {c.get('op')} {float(c.get('threshold') or 0):.1f}")
        promotion_blocks=[]
        if metrics['sample'] < int(config.PRECISION_MIN_VALIDATION_TRADES): promotion_blocks.append(f"muestra {metrics['sample']}/{int(config.PRECISION_MIN_VALIDATION_TRADES)}")
        if metrics['market_dates'] < int(config.PRECISION_MIN_OOS_DATES): promotion_blocks.append(f"fechas OOS {metrics['market_dates']}/{int(config.PRECISION_MIN_OOS_DATES)}")
        if metrics['win_rate'] < float(config.PRECISION_MIN_PROMOTION_WIN_RATE): promotion_blocks.append(f"win {metrics['win_rate']:.1f}%<{float(config.PRECISION_MIN_PROMOTION_WIN_RATE):.1f}%")
        if metrics['avg_return'] < float(config.PRECISION_MIN_PROMOTION_AVG_RETURN): promotion_blocks.append(f"retorno {metrics['avg_return']:+.2f}%<{float(config.PRECISION_MIN_PROMOTION_AVG_RETURN):+.2f}%")
        if metrics['profit_factor'] < float(config.PRECISION_MIN_PROMOTION_PROFIT_FACTOR): promotion_blocks.append(f"PF {metrics['profit_factor']:.2f}<{float(config.PRECISION_MIN_PROMOTION_PROFIT_FACTOR):.2f}")
        if metrics['max_drawdown'] > float(config.PRECISION_MAX_PROMOTION_DRAWDOWN): promotion_blocks.append(f"MaxDD {metrics['max_drawdown']:.1f}%>{float(config.PRECISION_MAX_PROMOTION_DRAWDOWN):.1f}%")
        if not invalidation_proven: promotion_blocks.append("cierres por invalidación todavía no validados")
        if passes_champion:
            decision = "apto para promoción"
        elif passes_shadow and activated == model_id:
            decision = "activo en SHADOW"
        elif passes_shadow:
            decision = "challenger no activo"
        else:
            decision = "rechazado"
        active_after = get_active_model()
        comparison_line = (
            f"**Modelo efectivo:** `#{int((active_after or {}).get('id') or 0)} · {(active_after or {}).get('model_key') or 'sin-modelo'}`"
            + (f" · **Motivo:** {', '.join(activation_blocks)}" if activation_blocks else "")
        )
        candidate_model = {"id": model_id, "model_key": model_key, "status": status, "params": model_params, "metrics": metrics_payload, "objective_score": metrics.get("selection_score")}
        try:
            from layer_calibration import refresh_layer_calibrations
            refresh_layer_calibrations()
        except Exception as layer_exc:
            log_info(f"⚠️ Calibración por capas pendiente: {layer_exc}", send_discord=False)
        publish_calibration(active_before, candidate_model, active_after, {
            "GEX_DAILY": active_layer_model("GEX_DAILY"),
            "GEX_INTRADAY": active_layer_model("GEX_INTRADAY"),
            "GRAPH": active_layer_model("GRAPH"),
        })
        return model_id
    except Exception as exc:
        finish_mover_discovery_run(run_id,status='error',summary_json=json.dumps({'error':str(exc)},ensure_ascii=False))
        log_error(f"🟥 Error descubrimiento movers: {exc}")
        raise
