from __future__ import annotations

"""TMAD 6.2 Swing V2 research and prospective shadow engine.

Research-only by design. It learns horizon-specific 2/3/5 session models from
the full canonical Scanner feature universe, lets feature signs become
continuation or mean-reversion, validates with purged rolling walk-forward,
compares against simple baselines using identical daily slots, and journals
future shadow picks. Nothing here mutates LIVE execution.
"""

from bisect import bisect_right
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any

import config
from db import get_runtime_state, set_runtime_state
from time_utils import ts_madrid_str
from research_snapshot import research_input_path

SCHEMA_VERSION = 1
MODEL_VERSION = "SWING_V2_HORIZON_FAMILY_V1"
HORIZONS = (2, 3, 5)
MAX_POSITIONS = 5
MIN_TRAIN_DATES = 28
MIN_FEATURE_SAMPLE = 30
MIN_FEATURE_TAIL = 8
VALIDATION_THRESHOLD_QUANTILE = 0.70
MIN_VALIDATION_TRADES = 10
MIN_WALKFORWARD_TRADES = 30
MIN_WALKFORWARD_DATES = 15
MIN_PROSPECTIVE_TRADES = 30
MIN_PROSPECTIVE_DATES = 15
MIN_PROFIT_FACTOR = 1.20
MIN_BASELINE_LIFT_PP = 0.15

FEATURE_FAMILIES: dict[str, tuple[str, ...]] = {
    "PRICE_MOMENTUM": (
        "momentum_score", "scanner_trend_score", "momentum_5d_pct",
        "change_percent", "price_vs_sma20_pct", "price_vs_vwap_pct", "rsi",
    ),
    "FLOW_PARTICIPATION": (
        "flow_direction", "rvol", "otm_call_volume_z", "otm_put_volume_z",
        "pc_ratio",
    ),
    "MARKET": (
        "market_context_score", "market_breadth_score", "vix_stress_score",
        "credit_risk_on_score", "market_deterioration_score",
    ),
    "SECTOR_RELATIVE_STRENGTH": (
        "sector_etf_score", "sector_context_score", "sector_breadth_score",
        "relative_strength_vs_market", "relative_strength_vs_sector",
    ),
    "GRAPH_RELATIONAL": (
        "graph_context_score", "relationship_context_score",
        "relationship_lead_lag_score", "leveraged_confirmation_score",
        "relational_deterioration_score", "relationship_deterioration_score",
    ),
    "VOLATILITY_EXTENSION": (
        "atr_pct", "realized_volatility_20d", "overextension_score",
    ),
}
BASELINE_FEATURES = ("momentum_score", "scanner_trend_score", "flow_direction", "rvol")


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value or "{}")
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _day(row: dict) -> str:
    return str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]


def _ticker(row: dict) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").upper()


def _features(row: dict) -> dict:
    cached = row.get("_swing_v2_features_cache")
    if isinstance(cached, dict):
        return cached
    parsed = _json(row.get("features_json") or row.get("payload_json"))
    row["_swing_v2_features_cache"] = parsed
    return parsed


def _quantile_sorted(clean: list[float], q: float) -> float | None:
    if not clean:
        return None
    q = max(0.0, min(1.0, float(q)))
    pos = (len(clean) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return clean[lo]
    return clean[lo] * (hi - pos) + clean[hi] * (pos - lo)


def _quantile(values: list[float], q: float) -> float | None:
    clean = sorted(float(v) for v in values if math.isfinite(float(v)))
    return _quantile_sorted(clean, q)


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return round(gains / losses, 4)


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    equity = peak = 0.0
    worst = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(abs(worst), 4)


def _metrics(scored: list[dict]) -> dict:
    ordered = sorted(scored, key=lambda r: (str(r.get("date") or ""), str(r.get("ticker") or "")))
    returns = [float(r["return_pct"]) for r in ordered if _f(r.get("return_pct")) is not None]
    positive = [v for v in returns if v > 0]
    mfe = [float(v) for v in (_f(r.get("mfe_pct")) for r in ordered) if v is not None]
    mae = [float(v) for v in (_f(r.get("mae_pct")) for r in ordered) if v is not None]
    dates = {str(r.get("date") or "") for r in ordered if r.get("date")}
    return {
        "sample": len(returns),
        "dates": len(dates),
        "win_rate_pct": round(100.0 * len(positive) / len(returns), 2) if returns else None,
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "median_return_pct": round(median(returns), 4) if returns else None,
        "profit_factor": _profit_factor(returns),
        "max_drawdown_return_points": _max_drawdown(returns),
        "avg_mfe_pct": round(mean(mfe), 4) if mfe else None,
        "avg_mae_pct": round(mean(mae), 4) if mae else None,
        "return_sum_points": round(sum(returns), 4) if returns else None,
    }


def _canonical_rows(rows: list[dict]) -> list[dict]:
    """One EOD-like observation per date/ticker/horizon."""
    chosen: dict[tuple[str, str, int], dict] = {}
    for raw in rows:
        row = dict(raw)
        day = _day(row)
        ticker = _ticker(row)
        horizon = int(row.get("horizon_sessions") or 0)
        ret = _f(row.get("return_long_pct"))
        if not day or not ticker or horizon not in HORIZONS or ret is None:
            continue
        if "complete" in row and row.get("complete") is not None:
            try:
                if int(row.get("complete") or 0) != 1:
                    continue
            except Exception:
                continue
        key = (day, ticker, horizon)
        session = str(row.get("session_type") or "").lower()
        rank = (
            1 if int(row.get("is_canonical_daily_close") or 0) == 1 else 0,
            1 if ("deep" in session or "full" in session or "close" in session) else 0,
            int(row.get("source_scanner_run_id") or 0),
            int(row.get("observation_id") or row.get("feature_observation_id") or 0),
        )
        old = chosen.get(key)
        if old is None or rank > old["_swing_v2_rank"]:
            row["_swing_v2_rank"] = rank
            chosen[key] = row
    out = []
    for row in chosen.values():
        row.pop("_swing_v2_rank", None)
        out.append(row)
    return sorted(out, key=lambda r: (_day(r), _ticker(r), int(r.get("horizon_sessions") or 0)))


def _row_return(row: dict, direction: str) -> float | None:
    ret = _f(row.get("return_long_pct"))
    if ret is None:
        return None
    return float(ret) if direction == "LONG" else -float(ret)


def _row_excursions(row: dict, direction: str) -> tuple[float | None, float | None]:
    mfe_long = _f(row.get("mfe_long_pct"))
    mae_long = _f(row.get("mae_long_pct"))
    if direction == "LONG":
        return mfe_long, mae_long
    favorable = -float(mae_long) if mae_long is not None else None
    adverse = -float(mfe_long) if mfe_long is not None else None
    return favorable, adverse


def _feature_effect(rows: list[dict], feature: str) -> dict | None:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        x = _f(_features(row).get(feature))
        y = _f(row.get("return_long_pct"))
        if x is not None and y is not None:
            pairs.append((float(x), float(y)))
    if len(pairs) < MIN_FEATURE_SAMPLE:
        return None
    pairs.sort(key=lambda item: item[0])
    xs = [x for x, _ in pairs]
    q25 = _quantile_sorted(xs, 0.25)
    q75 = _quantile_sorted(xs, 0.75)
    if q25 is None or q75 is None or q25 == q75:
        return None
    low = [y for x, y in pairs if x <= q25]
    high = [y for x, y in pairs if x >= q75]
    if len(low) < MIN_FEATURE_TAIL or len(high) < MIN_FEATURE_TAIL:
        return None
    edge = mean(high) - mean(low)
    if abs(edge) < 1e-9:
        return None
    reliability = min(1.0, abs(edge) / 1.0) * min(1.0, math.sqrt(len(pairs) / 100.0))
    quantiles = [
        _quantile_sorted(xs, q) for q in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90)
    ]
    return {
        "sample": len(pairs),
        "edge_high_minus_low_pct": round(edge, 6),
        "sign": 1 if edge > 0 else -1,
        "reliability": round(reliability, 6),
        "quantiles": [round(float(v), 8) for v in quantiles if v is not None],
        "low_avg_return_pct": round(mean(low), 6),
        "high_avg_return_pct": round(mean(high), 6),
    }


def _fit_model(rows: list[dict], horizon: int, families: tuple[str, ...] | None = None) -> dict:
    horizon_rows = [r for r in rows if int(r.get("horizon_sessions") or 0) == int(horizon)]
    allowed = set(families or tuple(FEATURE_FAMILIES))
    family_models: dict[str, dict] = {}
    for family, names in FEATURE_FAMILIES.items():
        if family not in allowed:
            continue
        feature_models = {}
        for name in names:
            effect = _feature_effect(horizon_rows, name)
            if effect:
                feature_models[name] = effect
        if feature_models:
            family_models[family] = {"features": feature_models}
    return {
        "model_version": MODEL_VERSION,
        "horizon_sessions": int(horizon),
        "train_rows": len(horizon_rows),
        "train_dates": len({_day(r) for r in horizon_rows}),
        "families": family_models,
        "family_count": len(family_models),
        "feature_count": sum(len(v["features"]) for v in family_models.values()),
    }


def _approx_percentile(value: float, quantiles: list[float]) -> float:
    if not quantiles:
        return 0.5
    idx = bisect_right(quantiles, float(value))
    return (idx + 0.5) / (len(quantiles) + 1.0)


def score_features(model: dict, features: dict) -> dict:
    family_scores: dict[str, float] = {}
    feature_contributions: dict[str, dict] = {}
    for family, family_model in (model.get("families") or {}).items():
        numerator = denominator = 0.0
        for name, params in (family_model.get("features") or {}).items():
            value = _f(features.get(name))
            if value is None:
                continue
            reliability = float(params.get("reliability") or 0.0)
            if reliability <= 0:
                continue
            pct = _approx_percentile(float(value), list(params.get("quantiles") or []))
            centered = (pct - 0.5) * 2.0
            contribution = float(params.get("sign") or 0) * centered
            numerator += contribution * reliability
            denominator += reliability
            feature_contributions[name] = {
                "family": family,
                "percentile": round(pct, 4),
                "learned_sign": int(params.get("sign") or 0),
                "contribution": round(contribution, 4),
                "reliability": round(reliability, 4),
            }
        if denominator > 0:
            family_scores[family] = max(-1.0, min(1.0, numerator / denominator))
    score = mean(family_scores.values()) if family_scores else 0.0
    return {
        "score": round(float(score), 6),
        "strength": round(abs(float(score)), 6),
        "direction": "LONG" if score >= 0 else "SHORT",
        "family_scores": {k: round(v, 6) for k, v in family_scores.items()},
        "feature_contributions": feature_contributions,
        "available_families": len(family_scores),
    }


def _score_rows(model: dict, rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    horizon = int(model.get("horizon_sessions") or 0)
    for row in rows:
        if int(row.get("horizon_sessions") or 0) != horizon:
            continue
        scored = score_features(model, _features(row))
        direction = str(scored["direction"])
        ret = _row_return(row, direction)
        if ret is None:
            continue
        mfe, mae = _row_excursions(row, direction)
        out.append({
            "date": _day(row),
            "ticker": _ticker(row),
            "horizon_sessions": horizon,
            "direction": direction,
            "score": scored["score"],
            "strength": scored["strength"],
            "return_pct": round(float(ret), 6),
            "mfe_pct": round(float(mfe), 6) if mfe is not None else None,
            "mae_pct": round(float(mae), 6) if mae is not None else None,
            "family_scores": scored["family_scores"],
        })
    return out


def _validation_gate(model: dict, validation_rows: list[dict]) -> dict:
    scored = _score_rows(model, validation_rows)
    strengths = [float(r["strength"]) for r in scored]
    threshold = _quantile(strengths, VALIDATION_THRESHOLD_QUANTILE)
    if threshold is None:
        return {
            "active": False, "status": "INSUFFICIENT_VALIDATION",
            "threshold_abs_score": None, "metrics": _metrics([]),
        }
    selected = [r for r in scored if float(r["strength"]) >= float(threshold)]
    metrics = _metrics(selected)
    pf = metrics.get("profit_factor")
    active = bool(
        int(metrics.get("sample") or 0) >= MIN_VALIDATION_TRADES
        and float(metrics.get("avg_return_pct") or 0.0) > 0.0
        and pf is not None
        and float(pf) > 1.0
    )
    return {
        "active": active,
        "status": "ACTIVE" if active else "ABSTAIN_NO_VALIDATION_EDGE",
        "threshold_abs_score": round(float(threshold), 6),
        "selection_quantile": VALIDATION_THRESHOLD_QUANTILE,
        "metrics": metrics,
    }


def _selected_test(model: dict, test_rows: list[dict], gate: dict) -> list[dict]:
    if not gate.get("active") or gate.get("threshold_abs_score") is None:
        return []
    threshold = float(gate["threshold_abs_score"])
    return [row for row in _score_rows(model, test_rows) if float(row["strength"]) >= threshold]


def _cross_section_percentiles(rows: list[dict], feature: str) -> dict[str, float]:
    pairs = []
    for row in rows:
        value = _f(_features(row).get(feature))
        if value is not None:
            pairs.append((_ticker(row), float(value)))
    pairs.sort(key=lambda item: (item[1], item[0]))
    n = len(pairs)
    if n <= 1:
        return {ticker: 0.5 for ticker, _ in pairs}
    return {ticker: idx / (n - 1) for idx, (ticker, _value) in enumerate(pairs)}


def _baseline_for_test(test_rows: list[dict], selected: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    horizon = int(selected[0].get("horizon_sessions") or 0) if selected else (
        int(test_rows[0].get("horizon_sessions") or 0) if test_rows else 0
    )
    for row in selected:
        counts[str(row.get("date") or "")] += 1
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in test_rows:
        if int(row.get("horizon_sessions") or 0) == horizon:
            by_day[_day(row)].append(row)
    continuation: list[dict] = []
    reversal: list[dict] = []
    for day, day_rows in sorted(by_day.items()):
        k = int(counts.get(day) or 0)
        if k <= 0:
            continue
        percentile_maps = {name: _cross_section_percentiles(day_rows, name) for name in BASELINE_FEATURES}
        scored = []
        for row in day_rows:
            ticker = _ticker(row)
            vals = [m[ticker] for m in percentile_maps.values() if ticker in m]
            if not vals:
                continue
            cont = mean(vals) - 0.5
            scored.append((abs(cont), cont, row))
        scored.sort(key=lambda item: (item[0], _ticker(item[2])), reverse=True)
        for _strength, cont, row in scored[:k]:
            direction = "LONG" if cont >= 0 else "SHORT"
            ret = _row_return(row, direction)
            mfe, mae = _row_excursions(row, direction)
            continuation.append({
                "date": day, "ticker": _ticker(row), "horizon_sessions": horizon,
                "direction": direction, "return_pct": ret, "mfe_pct": mfe, "mae_pct": mae,
            })
            reverse_direction = "SHORT" if direction == "LONG" else "LONG"
            reverse_ret = _row_return(row, reverse_direction)
            reverse_mfe, reverse_mae = _row_excursions(row, reverse_direction)
            reversal.append({
                "date": day, "ticker": _ticker(row), "horizon_sessions": horizon,
                "direction": reverse_direction, "return_pct": reverse_ret,
                "mfe_pct": reverse_mfe, "mae_pct": reverse_mae,
            })
    return {
        "momentum_continuation": _metrics(continuation),
        "mean_reversion": _metrics(reversal),
    }


def _purged(rows: list[dict], allowed_dates: set[str], label_cutoff: str | None = None) -> list[dict]:
    out = []
    for row in rows:
        if _day(row) not in allowed_dates:
            continue
        if label_cutoff:
            exit_date = str(row.get("exit_date") or "")[:10]
            if not exit_date or exit_date >= label_cutoff:
                continue
        out.append(row)
    return out


def _rolling_walkforward(rows: list[dict], horizon: int, families: tuple[str, ...] | None = None) -> dict:
    hrows = [r for r in rows if int(r.get("horizon_sessions") or 0) == horizon]
    dates = sorted({_day(r) for r in hrows})
    if len(dates) < MIN_TRAIN_DATES + 10:
        return {
            "status": "INSUFFICIENT_HISTORY", "horizon_sessions": horizon,
            "dates": len(dates), "folds": 0, "selected": _metrics([]),
        }
    initial_train = max(MIN_TRAIN_DATES, int(len(dates) * 0.45))
    block = max(5, int(len(dates) * 0.12))
    start = initial_train
    folds = []
    selected_all: list[dict] = []
    continuation_rows: list[dict] = []
    reversal_rows: list[dict] = []
    slot_audit_all_days_fixed5: list[dict] = []
    slot_audit_active_fixed_k: dict[int, list[dict]] = {k: [] for k in range(1, MAX_POSITIONS + 1)}
    slot_audit_matched_capped5: list[dict] = []
    slot_count_values: list[int] = []
    thresholds: list[float] = []

    while start + block + 5 <= len(dates):
        val_end = min(len(dates) - 5, start + block)
        test_end = min(len(dates), val_end + block)
        if val_end <= start or test_end <= val_end:
            break
        train_dates = set(dates[:start])
        val_dates = set(dates[start:val_end])
        test_dates = set(dates[val_end:test_end])
        val_start = dates[start]
        test_start = dates[val_end]
        train = _purged(hrows, train_dates, label_cutoff=val_start)
        validation = _purged(hrows, val_dates, label_cutoff=test_start)
        test = _purged(hrows, test_dates)
        model = _fit_model(train, horizon, families=families)
        gate = _validation_gate(model, validation)
        selected = _selected_test(model, test, gate)
        baseline = _baseline_for_test(test, selected)
        if gate.get("threshold_abs_score") is not None:
            thresholds.append(float(gate["threshold_abs_score"]))
        selected_all.extend(selected)

        counts: dict[str, int] = defaultdict(int)
        for row in selected:
            counts[str(row.get("date") or "")] += 1
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in test:
            by_day[_day(row)].append(row)
        for day, day_rows in by_day.items():
            percentile_maps = {name: _cross_section_percentiles(day_rows, name) for name in BASELINE_FEATURES}
            ranked = []
            for row in day_rows:
                ticker = _ticker(row)
                vals = [m[ticker] for m in percentile_maps.values() if ticker in m]
                if vals:
                    cont = mean(vals) - 0.5
                    ranked.append((abs(cont), cont, row))
            ranked.sort(key=lambda item: (item[0], _ticker(item[2])), reverse=True)

            def _append_momentum(target: list[dict], limit: int) -> None:
                for _strength, cont, row in ranked[: max(0, int(limit))]:
                    direction = "LONG" if cont >= 0 else "SHORT"
                    ret = _row_return(row, direction)
                    mfe, mae = _row_excursions(row, direction)
                    target.append({
                        "date": day,
                        "ticker": _ticker(row),
                        "horizon_sessions": horizon,
                        "direction": direction,
                        "return_pct": ret,
                        "mfe_pct": mfe,
                        "mae_pct": mae,
                    })

            # Product-shaped diagnostic: Top-5 momentum on every OOT test day,
            # irrespective of whether the Swing V2 validation gate is active.
            _append_momentum(slot_audit_all_days_fixed5, MAX_POSITIONS)

            # The validation gate is fitted strictly before the test block. This
            # isolates whether its day/block abstention is what makes momentum
            # look strong, without borrowing the model's uncapped slot count.
            if gate.get("active"):
                for fixed_k in range(1, MAX_POSITIONS + 1):
                    _append_momentum(slot_audit_active_fixed_k[fixed_k], fixed_k)

            k = int(counts.get(day) or 0)
            if not k:
                continue
            slot_count_values.append(k)
            _append_momentum(slot_audit_matched_capped5, min(k, MAX_POSITIONS))

            for _strength, cont, row in ranked[:k]:
                direction = "LONG" if cont >= 0 else "SHORT"
                ret = _row_return(row, direction)
                mfe, mae = _row_excursions(row, direction)
                continuation_rows.append({"date": day, "ticker": _ticker(row), "return_pct": ret, "mfe_pct": mfe, "mae_pct": mae})
                reverse_direction = "SHORT" if direction == "LONG" else "LONG"
                ret2 = _row_return(row, reverse_direction)
                mfe2, mae2 = _row_excursions(row, reverse_direction)
                reversal_rows.append({"date": day, "ticker": _ticker(row), "return_pct": ret2, "mfe_pct": mfe2, "mae_pct": mae2})

        folds.append({
            "fold": len(folds) + 1,
            "train_start": dates[0],
            "train_end": dates[start - 1],
            "validation_start": val_start,
            "validation_end": dates[val_end - 1],
            "test_start": test_start,
            "test_end": dates[test_end - 1],
            "train_rows": len(train),
            "validation_rows": len(validation),
            "test_rows": len(test),
            "model_family_count": model.get("family_count"),
            "model_feature_count": model.get("feature_count"),
            "validation_gate": gate,
            "test_selected": _metrics(selected),
            "baselines": baseline,
        })
        start += block

    return {
        "status": "EVALUABLE" if folds else "INSUFFICIENT_HISTORY",
        "horizon_sessions": horizon,
        "dates": len(dates),
        "folds": len(folds),
        "selected": _metrics(selected_all),
        "baselines": {
            "momentum_continuation": _metrics(continuation_rows),
            "mean_reversion": _metrics(reversal_rows),
        },
        "momentum_baseline_slot_audit_v8": {
            "all_test_days_fixed_top5": _metrics(slot_audit_all_days_fixed5),
            "validation_active_days_fixed_k": {
                str(k): _metrics(slot_audit_active_fixed_k[k])
                for k in range(1, MAX_POSITIONS + 1)
            },
            "matched_dynamic_slots_uncapped": _metrics(continuation_rows),
            "matched_daily_slots_capped_at_5": _metrics(slot_audit_matched_capped5),
            "dynamic_slot_count": {
                "active_days": len(slot_count_values),
                "total_slots": sum(slot_count_values),
                "avg_slots_per_active_day": round(mean(slot_count_values), 3) if slot_count_values else None,
                "median_slots_per_active_day": round(float(median(slot_count_values)), 3) if slot_count_values else None,
                "max_slots_per_active_day": max(slot_count_values) if slot_count_values else 0,
                "days_above_5_slots": sum(value > MAX_POSITIONS for value in slot_count_values),
                "product_max_positions": MAX_POSITIONS,
            },
            "policy": {
                "research_only": True,
                "future_returns_evaluation_only": True,
                "validation_gate_precedes_test": True,
                "fixed_k_never_exceeds_product_max_positions": True,
                "matched_dynamic_slots_is_legacy_baseline_reference": True,
                "no_live_mutation": True,
            },
        },
        "median_validation_threshold_abs_score": round(float(median(thresholds)), 6) if thresholds else None,
        "fold_details": folds,
        "policy": {
            "expanding_train": True,
            "validation_precedes_test": True,
            "training_labels_must_exit_before_validation": True,
            "validation_labels_must_exit_before_test": True,
            "test_never_tunes_its_own_model_or_threshold": True,
            "same_daily_slot_count_for_baselines": True,
        },
    }


def _aggregate_horizons(by_horizon: dict[str, dict]) -> dict:
    sample = 0
    weighted_return = 0.0
    weighted_win = 0.0
    positive_horizons = 0
    pf_values = []
    blocks = set()
    max_selected_dates = 0
    for panel in by_horizon.values():
        metrics = panel.get("selected") or {}
        n = int(metrics.get("sample") or 0)
        sample += n
        max_selected_dates = max(max_selected_dates, int(metrics.get("dates") or 0))
        if n and metrics.get("avg_return_pct") is not None:
            weighted_return += n * float(metrics["avg_return_pct"])
        if n and metrics.get("win_rate_pct") is not None:
            weighted_win += n * float(metrics["win_rate_pct"])
        if metrics.get("avg_return_pct") is not None and float(metrics["avg_return_pct"]) > 0:
            positive_horizons += 1
        if metrics.get("profit_factor") not in (None, float("inf")):
            pf_values.append((n, float(metrics["profit_factor"])))
        for fold in panel.get("fold_details") or []:
            if fold.get("test_start") and fold.get("test_end"):
                blocks.add((fold["test_start"], fold["test_end"]))
    weighted_pf = (
        sum(n * pf for n, pf in pf_values) / sum(n for n, _ in pf_values)
        if pf_values and sum(n for n, _ in pf_values) else None
    )
    return {
        "sample": sample,
        "test_blocks": len(blocks),
        "max_selected_dates_per_horizon": max_selected_dates,
        "avg_return_pct": round(weighted_return / sample, 4) if sample else None,
        "win_rate_pct": round(weighted_win / sample, 2) if sample else None,
        "sample_weighted_profit_factor": round(weighted_pf, 4) if weighted_pf is not None else None,
        "positive_horizons": positive_horizons,
        "evaluable_horizons": sum(1 for panel in by_horizon.values() if panel.get("status") == "EVALUABLE"),
    }


def _best_baseline(by_horizon: dict[str, dict]) -> dict:
    totals = {
        "momentum_continuation": {"sample": 0.0, "return_sum": 0.0, "pf_weighted": 0.0, "pf_n": 0.0},
        "mean_reversion": {"sample": 0.0, "return_sum": 0.0, "pf_weighted": 0.0, "pf_n": 0.0},
    }
    for panel in by_horizon.values():
        for name, metrics in (panel.get("baselines") or {}).items():
            n = int(metrics.get("sample") or 0)
            if name not in totals or n <= 0:
                continue
            totals[name]["sample"] += n
            totals[name]["return_sum"] += n * float(metrics.get("avg_return_pct") or 0.0)
            pf = metrics.get("profit_factor")
            if pf not in (None, float("inf")):
                totals[name]["pf_weighted"] += n * float(pf)
                totals[name]["pf_n"] += n
    out = {}
    for name, item in totals.items():
        n = int(item["sample"])
        out[name] = {
            "sample": n,
            "avg_return_pct": round(item["return_sum"] / n, 4) if n else None,
            "sample_weighted_profit_factor": round(item["pf_weighted"] / item["pf_n"], 4) if item["pf_n"] else None,
        }
    candidates = [(float(v.get("avg_return_pct") or -1e9), name) for name, v in out.items() if v.get("sample")]
    winner = max(candidates)[1] if candidates else None
    return {"by_baseline": out, "best_by_avg_return": winner, "best": out.get(winner) if winner else {}}


def _historical_gate(by_horizon: dict[str, dict]) -> dict:
    aggregate = _aggregate_horizons(by_horizon)
    best = _best_baseline(by_horizon)
    best_metrics = best.get("best") or {}
    avg_return = _f(aggregate.get("avg_return_pct"), 0.0) or 0.0
    weighted_pf = _f(aggregate.get("sample_weighted_profit_factor"), 0.0) or 0.0
    baseline_avg = _f(best_metrics.get("avg_return_pct"), 0.0) or 0.0
    conditions = {
        "min_trades": int(aggregate.get("sample") or 0) >= MIN_WALKFORWARD_TRADES,
        "min_distinct_test_dates": int(aggregate.get("max_selected_dates_per_horizon") or 0) >= MIN_WALKFORWARD_DATES,
        "enough_horizons": int(aggregate.get("positive_horizons") or 0) >= 2,
        "positive_expectancy": avg_return > 0.0,
        "profit_factor": weighted_pf >= MIN_PROFIT_FACTOR,
        "beats_best_simple_baseline": avg_return >= baseline_avg + MIN_BASELINE_LIFT_PP,
    }
    return {
        "status": "PASS_HISTORICAL_ONLY" if all(conditions.values()) else "FAIL_HISTORICAL",
        "conditions": conditions,
        "aggregate": aggregate,
        "baseline_comparison": best,
        "required_baseline_lift_pp": MIN_BASELINE_LIFT_PP,
        "important": "Historical pass is necessary but never sufficient for LIVE promotion.",
    }


def _frozen_ablation_panel(rows: list[dict], horizon: int, families: tuple[str, ...]) -> dict:
    hrows = [r for r in rows if int(r.get("horizon_sessions") or 0) == horizon]
    dates = sorted({_day(r) for r in hrows})
    if len(dates) < MIN_TRAIN_DATES + 10:
        return {"status": "INSUFFICIENT_HISTORY", "selected": _metrics([])}
    train_end = max(MIN_TRAIN_DATES, int(len(dates) * 0.55))
    validation_end = max(train_end + 5, int(len(dates) * 0.75))
    validation_end = min(validation_end, len(dates) - 5)
    if validation_end <= train_end or validation_end >= len(dates):
        return {"status": "INSUFFICIENT_HISTORY", "selected": _metrics([])}
    val_start = dates[train_end]
    test_start = dates[validation_end]
    train = _purged(hrows, set(dates[:train_end]), label_cutoff=val_start)
    validation = _purged(hrows, set(dates[train_end:validation_end]), label_cutoff=test_start)
    test = [r for r in hrows if _day(r) in set(dates[validation_end:])]
    model = _fit_model(train, horizon, families=families)
    gate = _validation_gate(model, validation)
    selected = _selected_test(model, test, gate)
    return {
        "status": "EVALUABLE",
        "train_dates": train_end,
        "validation_dates": validation_end - train_end,
        "test_dates": len(dates) - validation_end,
        "validation_gate": gate,
        "selected": _metrics(selected),
    }


def _ablation_last_window(rows: list[dict]) -> dict:
    """Frozen diagnostics only; ablation never auto-removes a module."""
    result = {}
    configs: dict[str, tuple[str, ...]] = {"FULL": tuple(FEATURE_FAMILIES)}
    for family in FEATURE_FAMILIES:
        configs[f"WITHOUT_{family}"] = tuple(name for name in FEATURE_FAMILIES if name != family)
    configs["PRICE_FLOW_ONLY"] = ("PRICE_MOMENTUM", "FLOW_PARTICIPATION")
    for label, families in configs.items():
        panels = {str(h): _frozen_ablation_panel(rows, h, families) for h in HORIZONS}
        result[label] = {"aggregate": _aggregate_horizons(panels), "by_horizon": panels}
    return result


def _journal_path() -> Path:
    return config.OUTPUT_DIR / "swing_v2" / "swing_v2_prospective_journal.jsonl"


def _read_journal(path: Path | None = None) -> list[dict]:
    path = research_input_path(path or _journal_path())
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    out.append(row)
            except Exception:
                continue
    except Exception:
        return []
    return out


def _prospective_audit(rows: list[dict], journal_rows: list[dict]) -> dict:
    canonical = _canonical_rows(rows)
    index = {(_day(row), _ticker(row), int(row.get("horizon_sessions") or 0)): row for row in canonical}
    resolved = []
    pending = 0
    for item in journal_rows:
        key = (
            str(item.get("snapshot_date") or "")[:10],
            str(item.get("ticker") or "").upper(),
            int(item.get("horizon_sessions") or 0),
        )
        row = index.get(key)
        if row is None:
            pending += 1
            continue
        direction = str(item.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            continue
        ret = _row_return(row, direction)
        mfe, mae = _row_excursions(row, direction)
        if ret is None:
            pending += 1
            continue
        resolved.append({
            "date": key[0], "ticker": key[1], "horizon_sessions": key[2],
            "direction": direction, "return_pct": ret, "mfe_pct": mfe, "mae_pct": mae,
            "decision_snapshot_id": item.get("decision_snapshot_id"),
        })
    metrics = _metrics(resolved)
    by_horizon = {str(h): _metrics([r for r in resolved if int(r.get("horizon_sessions") or 0) == h]) for h in HORIZONS}
    positive_horizons = sum(
        1 for panel in by_horizon.values()
        if panel.get("avg_return_pct") is not None and float(panel["avg_return_pct"]) > 0
    )
    pf = _f(metrics.get("profit_factor"), 0.0) or 0.0
    conditions = {
        "min_resolved_trades": int(metrics.get("sample") or 0) >= MIN_PROSPECTIVE_TRADES,
        "min_distinct_dates": int(metrics.get("dates") or 0) >= MIN_PROSPECTIVE_DATES,
        "positive_expectancy": float(metrics.get("avg_return_pct") or 0.0) > 0.0,
        "profit_factor": pf >= MIN_PROFIT_FACTOR,
        "positive_in_at_least_two_horizons": positive_horizons >= 2,
    }
    return {
        "status": "PASS_PROSPECTIVE" if all(conditions.values()) else "ACCUMULATING",
        "metrics": metrics,
        "by_horizon": by_horizon,
        "positive_horizons": positive_horizons,
        "pending_unmatured": pending,
        "journal_entries": len(journal_rows),
        "conditions": conditions,
        "policy": {
            "only_predictions_written_before_the_outcome_are_scored": True,
            "no_backfilled_predictions": True,
            "prospective_pass_required_before_any_live_promotion": True,
        },
    }


def _final_model(rows: list[dict], by_horizon: dict[str, dict]) -> dict:
    horizon_models = {}
    for horizon in HORIZONS:
        panel = by_horizon.get(str(horizon)) or {}
        metrics = panel.get("selected") or {}
        threshold = panel.get("median_validation_threshold_abs_score")
        active = bool(
            threshold is not None
            and int(metrics.get("sample") or 0) >= 8
            and float(metrics.get("avg_return_pct") or 0.0) > 0.0
            and (_f(metrics.get("profit_factor"), 0.0) or 0.0) > 1.0
        )
        model = _fit_model(rows, horizon)
        model.update({
            "threshold_abs_score": threshold,
            "active_for_prospective_shadow": active,
            "walkforward_expectancy_pct": metrics.get("avg_return_pct"),
            "walkforward_profit_factor": metrics.get("profit_factor"),
            "walkforward_sample": metrics.get("sample"),
        })
        horizon_models[str(horizon)] = model
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at_madrid": ts_madrid_str(),
        "mode": "PROSPECTIVE_SHADOW_MODEL",
        "horizons": horizon_models,
        "policy": {
            "live_mutation": False,
            "gex_is_advisory_not_gate": True,
            "unused_slots_allowed": True,
            "max_positions": MAX_POSITIONS,
            "fit_uses_mature_history_only": True,
        },
    }
    clone = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    snapshot["fingerprint"] = hashlib.sha256(clone.encode("utf-8")).hexdigest()
    return snapshot


def build_swing_v2_research(
    feature_rows: list[dict],
    *,
    persist: bool = True,
    journal_rows: list[dict] | None = None,
) -> dict:
    rows = _canonical_rows(feature_rows)
    by_horizon = {str(h): _rolling_walkforward(rows, h) for h in HORIZONS}
    historical_gate = _historical_gate(by_horizon)
    ablation = _ablation_last_window(rows)
    prospective = _prospective_audit(rows, list(journal_rows) if journal_rows is not None else _read_journal())
    model_snapshot = _final_model(rows, by_horizon)
    promotion_ready = bool(
        historical_gate.get("status") == "PASS_HISTORICAL_ONLY"
        and prospective.get("status") == "PASS_PROSPECTIVE"
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at_madrid": ts_madrid_str(),
        "mode": "SWING_V2_RESEARCH_ONLY",
        "objective": "select 2/3/5-session trades from the full canonical Scanner universe using one economic objective",
        "canonical_rows": len(rows),
        "canonical_dates": len({_day(r) for r in rows}),
        "by_horizon": by_horizon,
        "historical_gate": historical_gate,
        "ablation": ablation,
        "prospective_audit": prospective,
        "promotion_ready": promotion_ready,
        "model_snapshot": model_snapshot,
        "verification_contract": {
            "historical_is_not_enough": True,
            "prospective_shadow_required": True,
            "walkforward_is_purged_by_label_exit_date": True,
            "simple_baselines_use_same_daily_slot_count": True,
            "ablation_is_diagnostic_and_cannot_auto_remove_modules": True,
            "no_live_auto_promotion": True,
        },
    }
    if persist:
        out = config.OUTPUT_DIR / "swing_v2"
        out.mkdir(parents=True, exist_ok=True)
        (out / "latest_swing_v2_research.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        set_runtime_state("swing_v2_research_summary", json.dumps({
            "schema_version": SCHEMA_VERSION,
            "model_version": MODEL_VERSION,
            "generated_at_madrid": payload["generated_at_madrid"],
            "canonical_rows": payload["canonical_rows"],
            "canonical_dates": payload["canonical_dates"],
            "historical_gate": historical_gate,
            "prospective_audit": prospective,
            "promotion_ready": promotion_ready,
        }, ensure_ascii=False, default=str))
        set_runtime_state("swing_v2_model_snapshot", json.dumps(model_snapshot, ensure_ascii=False, default=str))
    return payload


def _gex_advisory(features: dict, direction: str) -> dict:
    target = _f(features.get("target_price") or features.get("active_target_price"))
    target_pct = _f(features.get("target_pct") or features.get("wall_reward_pct"))
    invalidation = _f(features.get("opposite_level_price") or features.get("wall_support"))
    return {
        "available": bool(int(features.get("has_gex") or 0) or target or invalidation),
        "target_price": target,
        "target_pct": target_pct,
        "invalidation_price": invalidation,
        "gex_intent": features.get("gex_intent") or features.get("active_gex_intent"),
        "used_as_gate": False,
        "direction": direction,
    }


def score_current_snapshot(
    features_by_ticker: dict[str, dict],
    run: dict,
    *,
    max_positions: int = MAX_POSITIONS,
    model_snapshot: dict | None = None,
    persist: bool = True,
) -> dict:
    if model_snapshot is None:
        try:
            model_snapshot = _json(get_runtime_state("swing_v2_model_snapshot", "") or "")
        except Exception:
            model_snapshot = {}
    if not model_snapshot or not (model_snapshot.get("horizons") or {}):
        return {"status": "waiting_for_swing_v2_model", "mode": "SWING_V2_PROSPECTIVE_SHADOW"}

    candidates = []
    for ticker, features in features_by_ticker.items():
        ticker = str(ticker or "").upper()
        if not ticker:
            continue
        best = None
        for horizon in HORIZONS:
            model = (model_snapshot.get("horizons") or {}).get(str(horizon)) or {}
            if not bool(model.get("active_for_prospective_shadow")):
                continue
            threshold = _f(model.get("threshold_abs_score"))
            if threshold is None:
                continue
            scored = score_features(model, features)
            if int(scored.get("available_families") or 0) < 2:
                continue
            if float(scored.get("strength") or 0.0) < float(threshold):
                continue
            expectancy = _f(model.get("walkforward_expectancy_pct"), 0.0) or 0.0
            ratio = min(2.0, max(0.5, float(scored["strength"]) / max(float(threshold), 1e-6)))
            expected_proxy = max(0.0, expectancy) * ratio
            item = {
                "ticker": ticker,
                "direction": scored["direction"],
                "horizon_sessions": horizon,
                "score": scored["score"],
                "strength": scored["strength"],
                "expected_return_proxy_pct": round(expected_proxy, 4),
                "walkforward_expectancy_pct": model.get("walkforward_expectancy_pct"),
                "walkforward_profit_factor": model.get("walkforward_profit_factor"),
                "threshold_abs_score": threshold,
                "family_scores": scored["family_scores"],
                "gex_advisory": _gex_advisory(features, scored["direction"]),
                "spot": _f(features.get("price") or features.get("current_price")),
            }
            rank = (float(item["expected_return_proxy_pct"]), float(item["strength"]), ticker)
            if best is None or rank > best[0]:
                best = (rank, item)
        if best is not None:
            candidates.append(best[1])

    candidates.sort(
        key=lambda r: (float(r.get("expected_return_proxy_pct") or 0.0), float(r.get("strength") or 0.0), str(r.get("ticker") or "")),
        reverse=True,
    )
    selected = candidates[: max(0, int(max_positions))]
    snapshot_date = str(run.get("snapshot_date") or "")[:10]
    run_id = int(run.get("id") or run.get("source_scanner_run_id") or 0)
    decision_source = {
        "snapshot_date": snapshot_date,
        "source_scanner_run_id": run_id,
        "model_fingerprint": model_snapshot.get("fingerprint"),
        "selected": [(r["ticker"], r["direction"], r["horizon_sessions"], r["score"]) for r in selected],
    }
    decision_id = hashlib.sha256(
        json.dumps(decision_source, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:20]
    payload = {
        "status": "ok",
        "mode": "SWING_V2_PROSPECTIVE_SHADOW",
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "snapshot_date": snapshot_date,
        "source_scanner_run_id": run_id,
        "decision_snapshot_id": decision_id,
        "model_fingerprint": model_snapshot.get("fingerprint"),
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected": selected,
        "policy": {
            "live_mutation": False,
            "discord_publication": False,
            "gex_is_advisory_not_gate": True,
            "unused_slots_allowed": True,
            "max_positions": int(max_positions),
            "prospective_journal_is_immutable_append_only": True,
        },
    }
    if persist:
        out = config.OUTPUT_DIR / "swing_v2"
        out.mkdir(parents=True, exist_ok=True)
        (out / "latest_swing_v2_shadow.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        set_runtime_state("swing_v2_shadow_snapshot", json.dumps(payload, ensure_ascii=False, default=str))
        journal = _journal_path()
        existing = {
            (
                str(row.get("snapshot_date") or "")[:10],
                str(row.get("ticker") or "").upper(),
                int(row.get("horizon_sessions") or 0),
                str(row.get("model_fingerprint") or ""),
            )
            for row in _read_journal(journal)
        }
        with journal.open("a", encoding="utf-8") as handle:
            for row in selected:
                key = (
                    snapshot_date, str(row["ticker"]).upper(),
                    int(row["horizon_sessions"]), str(model_snapshot.get("fingerprint") or ""),
                )
                if key in existing:
                    continue
                item = {
                    "snapshot_date": snapshot_date,
                    "source_scanner_run_id": run_id,
                    "decision_snapshot_id": decision_id,
                    "model_fingerprint": model_snapshot.get("fingerprint"),
                    "ticker": row["ticker"],
                    "direction": row["direction"],
                    "horizon_sessions": row["horizon_sessions"],
                    "score": row["score"],
                    "strength": row["strength"],
                    "expected_return_proxy_pct": row["expected_return_proxy_pct"],
                    "spot": row.get("spot"),
                    "journaled_at_madrid": ts_madrid_str(),
                }
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                existing.add(key)
    return payload
