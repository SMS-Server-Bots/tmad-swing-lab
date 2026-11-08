from __future__ import annotations

"""Point-in-time diagnostic for FUSION direction versus the existing momentum baseline.

This module intentionally reuses the already-defined Swing V2 momentum-continuation
baseline formula: within the same Scanner run, percentile-rank momentum_score,
scanner_trend_score, flow_direction and rvol; average available percentiles; LONG
when the average is >= 0.5 and SHORT otherwise.

It changes only direction on the same actionable FUSION ENTER identities. It does
not choose new tickers, fit labels, tune thresholds, or mutate LIVE/SHADOW logic.
Future closes are used only to evaluate the frozen point-in-time direction.
"""

from collections import defaultdict
import json
import math
from statistics import mean, median
from typing import Any

from return_math import directional_return_pct

HORIZONS = (2, 3, 5)
BASELINE_FEATURES = (
    "momentum_score",
    "scanner_trend_score",
    "flow_direction",
    "rvol",
)
MIN_AVAILABLE_FEATURES = 2


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _day(row: dict) -> str:
    return str(
        row.get("snapshot_date")
        or row.get("observation_date")
        or row.get("signal_date")
        or row.get("entry_date")
        or ""
    )[:10]


def _ticker(row: dict) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").upper()


def _run_id(row: dict) -> int:
    try:
        return int(row.get("source_scanner_run_id") or row.get("scanner_run_id") or 0)
    except Exception:
        return 0


def _feature_payload(row: dict) -> dict:
    payload = _json(row.get("payload_json") or row.get("features_json"))
    nested = payload.get("features")
    if isinstance(nested, dict):
        payload = {**payload, **nested}
    return payload


def _feature_value(row: dict, feature: str) -> float | None:
    direct = _f(row.get(feature))
    if direct is not None:
        return direct
    return _f(_feature_payload(row).get(feature))


def _percentile_map(rows: list[dict], feature: str) -> dict[str, float]:
    pairs: list[tuple[str, float]] = []
    for row in rows:
        ticker = _ticker(row)
        value = _feature_value(row, feature)
        if ticker and value is not None:
            pairs.append((ticker, float(value)))
    if not pairs:
        return {}

    # Preserve the Swing V2 baseline semantics: ties remain deterministically
    # ordered by ticker, so this diagnostic measures the same pre-existing rule.
    pairs.sort(key=lambda item: (item[1], item[0]))
    n = len(pairs)
    if n <= 1:
        return {ticker: 0.5 for ticker, _ in pairs}
    return {
        ticker: index / (n - 1)
        for index, (ticker, _value) in enumerate(pairs)
    }


def _build_run_momentum(feature_rows: list[dict]) -> tuple[dict[tuple[str, int, str], dict], dict]:
    by_run: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in feature_rows:
        day = _day(row)
        run_id = _run_id(row)
        ticker = _ticker(row)
        if day and run_id > 0 and ticker:
            by_run[(day, run_id)].append(row)

    out: dict[tuple[str, int, str], dict] = {}
    run_sizes: list[int] = []
    for (day, run_id), rows in sorted(by_run.items()):
        # Keep the latest/most complete row per ticker when a source contains
        # duplicates for the same run.
        chosen: dict[str, dict] = {}
        for row in rows:
            ticker = _ticker(row)
            old = chosen.get(ticker)
            if old is None:
                chosen[ticker] = row
                continue
            old_obs = int(old.get("observation_id") or old.get("feature_observation_id") or 0)
            new_obs = int(row.get("observation_id") or row.get("feature_observation_id") or 0)
            if new_obs >= old_obs:
                chosen[ticker] = row
        universe = list(chosen.values())
        run_sizes.append(len(universe))
        percentile_maps = {
            feature: _percentile_map(universe, feature)
            for feature in BASELINE_FEATURES
        }
        for ticker in sorted(chosen):
            values = [
                mapping[ticker]
                for mapping in percentile_maps.values()
                if ticker in mapping
            ]
            if len(values) < MIN_AVAILABLE_FEATURES:
                continue
            avg_pct = mean(values)
            continuation = float(avg_pct) - 0.5
            out[(day, run_id, ticker)] = {
                "direction": "LONG" if continuation >= 0.0 else "SHORT",
                "continuation_score": round(continuation, 6),
                "strength": round(abs(continuation), 6),
                "features_available": len(values),
                "feature_percentiles": {
                    feature: round(mapping[ticker], 6)
                    for feature, mapping in percentile_maps.items()
                    if ticker in mapping
                },
                "scanner_universe": len(universe),
            }

    return out, {
        "scanner_runs": len(by_run),
        "runs_with_universe": len(run_sizes),
        "min_run_universe": min(run_sizes) if run_sizes else 0,
        "max_run_universe": max(run_sizes) if run_sizes else 0,
        "avg_run_universe": round(mean(run_sizes), 2) if run_sizes else None,
    }


def _future_return(
    row: dict,
    direction: str,
    trusted_price_series: dict[str, list[dict]],
    horizon_sessions: int,
) -> float | None:
    ticker = _ticker(row)
    signal_date = str(row.get("signal_date") or row.get("entry_date") or "")[:10]
    entry_price = _f(row.get("entry_price"))
    if not ticker or not signal_date or entry_price is None or entry_price <= 0:
        return None

    ordered = sorted(
        trusted_price_series.get(ticker, []),
        key=lambda item: str(item.get("trade_date") or "")[:10],
    )
    if not ordered:
        return None

    has_segments = any("reverse_segment" in item for item in ordered)
    anchor_found = False
    anchor_segment = None
    for item in ordered:
        day = str(item.get("trade_date") or "")[:10]
        if day and day <= signal_date:
            anchor_found = True
            if has_segments:
                anchor_segment = item.get("reverse_segment")
        elif day and day > signal_date:
            break
    if has_segments and not anchor_found:
        return None

    future = [
        item
        for item in ordered
        if str(item.get("trade_date") or "")[:10] > signal_date
        and (not has_segments or item.get("reverse_segment") == anchor_segment)
    ]
    horizon = max(1, int(horizon_sessions))
    if len(future) < horizon:
        return None
    close = _f(future[horizon - 1].get("close"))
    if close is None or close <= 0:
        return None
    value = directional_return_pct(direction, float(entry_price), float(close))
    return float(value) if value is not None else None


def _metrics(values: list[tuple[str, float]]) -> dict:
    returns = [float(value) for _day_value, value in values]
    dates = {day for day, _value in values if day}
    gains = sum(value for value in returns if value > 0.0)
    losses = -sum(value for value in returns if value < 0.0)
    if losses > 0.0:
        pf = round(gains / losses, 4)
    elif gains > 0.0:
        pf = float("inf")
    else:
        pf = None
    return {
        "sample": len(returns),
        "dates": len(dates),
        "win_rate_pct": round(100.0 * sum(value > 0.0 for value in returns) / len(returns), 2) if returns else None,
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "median_return_pct": round(median(returns), 4) if returns else None,
        "profit_factor": pf,
        "return_sum_points": round(sum(returns), 4) if returns else None,
    }


def _evaluate_rows(
    rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
    horizon: int,
    direction_key: str,
) -> dict:
    values: list[tuple[str, float]] = []
    for row in rows:
        direction = str(row.get(direction_key) or "").upper()
        if direction not in {"LONG", "SHORT"}:
            continue
        ret = _future_return(row, direction, trusted_price_series, horizon)
        if ret is None:
            continue
        values.append((str(row.get("signal_date") or "")[:10], float(ret)))
    result = _metrics(values)
    result["selected_total"] = len(rows)
    result["unmatured_or_unavailable"] = max(0, len(rows) - int(result["sample"]))
    return result


def _lift(challenger: dict, baseline: dict) -> dict:
    ca = _f(challenger.get("avg_return_pct"))
    ba = _f(baseline.get("avg_return_pct"))
    cp = challenger.get("profit_factor")
    bp = baseline.get("profit_factor")
    pf_lift = None
    try:
        if cp is not None and bp is not None:
            pf_lift = round(float(cp) - float(bp), 4)
    except Exception:
        pf_lift = None
    return {
        "avg_return_lift_pp": round(float(ca) - float(ba), 4) if ca is not None and ba is not None else None,
        "profit_factor_lift": pf_lift,
    }


def _horizon_panel(
    rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
    horizon: int,
) -> dict:
    agreement = [
        row for row in rows
        if row.get("momentum_direction") == row.get("current_direction")
    ]
    disagreement = [
        row for row in rows
        if row.get("momentum_direction") != row.get("current_direction")
    ]

    current_all = _evaluate_rows(rows, trusted_price_series, horizon, "current_direction")
    momentum_all = _evaluate_rows(rows, trusted_price_series, horizon, "momentum_direction")
    agreement_current = _evaluate_rows(agreement, trusted_price_series, horizon, "current_direction")
    disagreement_current = _evaluate_rows(disagreement, trusted_price_series, horizon, "current_direction")
    disagreement_momentum = _evaluate_rows(disagreement, trusted_price_series, horizon, "momentum_direction")

    momentum_lift = _lift(momentum_all, current_all)
    disagreement_flip_lift = _lift(disagreement_momentum, disagreement_current)
    momentum_pf = _f(momentum_all.get("profit_factor"))
    return {
        "current_direction_same_identities": current_all,
        "momentum_direction_same_identities": momentum_all,
        "momentum_direction_lift": momentum_lift,
        "agreement_only_current_direction": agreement_current,
        "disagreement_current_direction": disagreement_current,
        "disagreement_momentum_flip": disagreement_momentum,
        "disagreement_flip_lift": disagreement_flip_lift,
        "research_screen": {
            "min_sample_40": int(momentum_all.get("sample") or 0) >= 40,
            "min_dates_12": int(momentum_all.get("dates") or 0) >= 12,
            "positive_expectancy": float(momentum_all.get("avg_return_pct") or 0.0) > 0.0,
            "profit_factor_gt_1": momentum_pf is not None and float(momentum_pf) > 1.0,
            "positive_lift_vs_current_direction": float(momentum_lift.get("avg_return_lift_pp") or 0.0) > 0.0,
            "disagreement_flip_positive_lift": float(disagreement_flip_lift.get("avg_return_lift_pp") or 0.0) > 0.0,
        },
    }


def build_momentum_direction_audit(
    candidate_trades: list[dict],
    feature_rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
) -> dict:
    momentum_index, universe = _build_run_momentum(feature_rows)
    evaluated: list[dict] = []
    missing_momentum = 0
    for raw in candidate_trades:
        row = dict(raw)
        key = (_day(row), _run_id(row), _ticker(row))
        momentum = momentum_index.get(key)
        current = str(row.get("direction") or "").upper()
        if not momentum or current not in {"LONG", "SHORT"}:
            missing_momentum += 1
            continue
        row["current_direction"] = current
        row["momentum_direction"] = str(momentum["direction"])
        row["momentum_continuation_score"] = momentum["continuation_score"]
        row["momentum_strength"] = momentum["strength"]
        row["momentum_features_available"] = momentum["features_available"]
        row["momentum_feature_percentiles"] = momentum["feature_percentiles"]
        row["momentum_scanner_universe"] = momentum["scanner_universe"]
        evaluated.append(row)

    by_horizon = {
        str(horizon): _horizon_panel(evaluated, trusted_price_series, horizon)
        for horizon in HORIZONS
    }

    dates = sorted({str(row.get("signal_date") or "")[:10] for row in evaluated})
    split = max(1, len(dates) // 2) if dates else 0
    early_dates = set(dates[:split])
    late_dates = set(dates[split:])
    chronological = {}
    for label, allowed in (("early_half", early_dates), ("late_half", late_dates)):
        subset = [row for row in evaluated if str(row.get("signal_date") or "")[:10] in allowed]
        chronological[label] = {
            "dates": len(allowed),
            "by_horizon": {
                str(horizon): {
                    "current_direction_same_identities": _evaluate_rows(
                        subset, trusted_price_series, horizon, "current_direction"
                    ),
                    "momentum_direction_same_identities": _evaluate_rows(
                        subset, trusted_price_series, horizon, "momentum_direction"
                    ),
                }
                for horizon in HORIZONS
            },
        }

    agreement = sum(
        row.get("current_direction") == row.get("momentum_direction")
        for row in evaluated
    )
    passing = []
    for horizon in HORIZONS:
        panel = by_horizon[str(horizon)]
        screen = panel["research_screen"]
        screen["pass"] = all(screen.values())
        if screen["pass"]:
            passing.append(horizon)

    return {
        "mode": "ACTIONABLE_ENTER_MOMENTUM_DIRECTION_AUDIT_V6",
        "status": "EVALUABLE" if evaluated else "INSUFFICIENT_POINT_IN_TIME_COVERAGE",
        "live_mutation": False,
        "promotion_allowed": False,
        "unbiased_oos_proof": False,
        "candidate_trades": len(candidate_trades),
        "evaluated_candidates": len(evaluated),
        "missing_momentum_context": missing_momentum,
        "coverage_pct": round(100.0 * len(evaluated) / len(candidate_trades), 2) if candidate_trades else None,
        "direction_agreement": agreement,
        "direction_disagreement": len(evaluated) - agreement,
        "direction_agreement_pct": round(100.0 * agreement / len(evaluated), 2) if evaluated else None,
        "scanner_context": universe,
        "by_horizon": by_horizon,
        "chronological_stability": chronological,
        "research_screen": {
            "passing_horizons": passing,
            "passing_horizon_count": len(passing),
            "requires_at_least_two_horizons_for_prospective_followup": True,
            "prospective_followup_candidate": len(passing) >= 2,
        },
        "methodology": {
            "same_candidate_identities_for_current_and_momentum": True,
            "only_direction_changes": True,
            "momentum_formula_preexisted_in_swing_v2_baseline": True,
            "formula": "mean same-run Scanner percentiles of momentum_score, scanner_trend_score, flow_direction, rvol; LONG if >=0.5 else SHORT",
            "minimum_available_features": MIN_AVAILABLE_FEATURES,
            "full_scanner_same_run_percentiles": True,
            "future_prices_used_only_for_evaluation": True,
            "fixed_horizon_outcomes": [2, 3, 5],
            "trusted_reverse_segment_respected": True,
            "no_label_fitting": True,
            "no_threshold_tuning": True,
            "selection_not_changed": True,
            "live_mutation": False,
            "interpretation": (
                "diagnostic only: because the momentum baseline was selected for inspection after historical results, "
                "positive findings require a prospective journal before any execution change"
            ),
        },
    }
