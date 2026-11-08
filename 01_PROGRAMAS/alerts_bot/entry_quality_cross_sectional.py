from __future__ import annotations

"""Cross-sectional, out-of-time Entry Quality research.

This lane answers the capital-slot question directly:
given every actionable FUSION ENTER available on a historical date, which
candidates should occupy the fixed Top-N slots?

Training uses only trades whose boundary outcome was already resolved before the
test date. Feature directions must agree in two chronological training halves.
The actual ranking is relative within the same day's candidate set, so regime or
score-level drift cannot be mistaken for cross-sectional quality.

Research/SHADOW only. Nothing in this module mutates LIVE selection.
"""

import math
from collections import defaultdict
from statistics import mean, median
from typing import Any

MIN_TRAIN_TRADES = 40
MIN_TRAIN_DATES = 12
MIN_HALF_FEATURE_OBSERVATIONS = 12
MIN_SPLIT_SIDE = 4
MIN_STABLE_FEATURES = 3

FEATURES = (
    "underlying_alpha_score",
    "tradeability_score",
    "core_score",
    "gex_score",
    "overextension_score",
    "market_adjustment",
    "invalidation_distance_pct",
    "target_distance_pct",
    "direction_consistency_pct",
    "same_direction_observations",
    "lifecycle_observations",
    "countertrend_override",
    "direction_long",
)


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return round(gains / losses, 4)


def _entry_features(row: dict) -> dict[str, float | None]:
    diag = row.get("entry_diagnostics") if isinstance(row.get("entry_diagnostics"), dict) else {}
    entry = _f(row.get("entry_price"))
    target = _f(row.get("target_price"))
    direction = str(row.get("direction") or "").upper()
    target_distance = None
    if entry is not None and entry > 0 and target is not None and target > 0:
        if direction == "LONG" and target > entry:
            target_distance = 100.0 * (target - entry) / entry
        elif direction == "SHORT" and target < entry:
            target_distance = 100.0 * (entry - target) / entry
    return {
        "underlying_alpha_score": _f(row.get("underlying_alpha_score")),
        "tradeability_score": _f(row.get("tradeability_score")),
        "core_score": _f(diag.get("core_score")),
        "gex_score": _f(diag.get("gex_score")),
        "overextension_score": _f(diag.get("overextension_score")),
        "market_adjustment": _f(diag.get("market_adjustment")),
        "invalidation_distance_pct": _f(diag.get("invalidation_distance_pct")),
        "target_distance_pct": target_distance,
        "direction_consistency_pct": _f(diag.get("direction_consistency_pct")),
        "same_direction_observations": _f(diag.get("same_direction_observations")),
        "lifecycle_observations": _f(diag.get("lifecycle_observations")),
        "countertrend_override": 1.0 if bool(diag.get("countertrend_override")) else 0.0,
        "direction_long": 1.0 if direction == "LONG" else 0.0,
    }


def _resolved(rows: list[dict]) -> list[dict]:
    return [
        dict(row)
        for row in rows
        if str(row.get("boundary_status") or "").upper() == "RESOLVED"
        and _f(row.get("boundary_return_pct")) is not None
        and str(row.get("entry_date") or "")[:10]
        and str(row.get("boundary_exit_date") or "")[:10]
    ]


def _balanced_cut(values: list[float]) -> float | None:
    ordered = sorted(values)
    if len(ordered) < 2:
        return None
    mid = len(ordered) // 2
    left = ordered[mid - 1]
    right = ordered[mid]
    if left != right:
        return (left + right) / 2.0
    unique = sorted(set(ordered))
    if len(unique) < 2:
        return None
    centre = len(unique) // 2
    if centre <= 0:
        return None
    return (unique[centre - 1] + unique[centre]) / 2.0


def _feature_effect(rows: list[dict], feature: str) -> dict | None:
    pairs: list[tuple[float, float]] = []
    for row in rows:
        value = _entry_features(row).get(feature)
        ret = _f(row.get("boundary_return_pct"))
        if value is not None and ret is not None:
            pairs.append((float(value), float(ret)))
    if len(pairs) < MIN_HALF_FEATURE_OBSERVATIONS:
        return None
    cut = _balanced_cut([value for value, _ret in pairs])
    if cut is None:
        return None
    low = [ret for value, ret in pairs if value <= cut]
    high = [ret for value, ret in pairs if value > cut]
    if len(low) < MIN_SPLIT_SIDE or len(high) < MIN_SPLIT_SIDE:
        return None
    low_avg = mean(low)
    high_avg = mean(high)
    return {
        "sample": len(pairs),
        "cut": round(cut, 6),
        "low_n": len(low),
        "high_n": len(high),
        "low_avg_return_pct": round(low_avg, 4),
        "high_avg_return_pct": round(high_avg, 4),
        "high_minus_low_pp": round(high_avg - low_avg, 4),
    }


def _fit_stable_feature_directions(train_rows: list[dict]) -> dict:
    dates = sorted({str(row.get("entry_date") or "")[:10] for row in train_rows})
    if len(train_rows) < MIN_TRAIN_TRADES or len(dates) < MIN_TRAIN_DATES:
        return {
            "status": "INSUFFICIENT_TRAIN",
            "train_trades": len(train_rows),
            "train_dates": len(dates),
            "features": {},
        }
    split = max(1, len(dates) // 2)
    first_dates = set(dates[:split])
    second_dates = set(dates[split:])
    first = [row for row in train_rows if str(row.get("entry_date") or "")[:10] in first_dates]
    second = [row for row in train_rows if str(row.get("entry_date") or "")[:10] in second_dates]

    stable: dict[str, dict] = {}
    for feature in FEATURES:
        first_effect = _feature_effect(first, feature)
        second_effect = _feature_effect(second, feature)
        if not first_effect or not second_effect:
            continue
        e1 = float(first_effect["high_minus_low_pp"])
        e2 = float(second_effect["high_minus_low_pp"])
        if e1 == 0.0 or e2 == 0.0 or (e1 > 0) != (e2 > 0):
            continue
        stable[feature] = {
            "direction": 1 if e1 > 0 else -1,
            "first_half": first_effect,
            "second_half": second_effect,
            "min_abs_edge_pp": round(min(abs(e1), abs(e2)), 4),
            "ranking_weight": 1.0,
        }
    return {
        "status": "EVALUABLE" if len(stable) >= MIN_STABLE_FEATURES else "INSUFFICIENT_STABLE_FEATURES",
        "train_trades": len(train_rows),
        "train_dates": len(dates),
        "first_half_dates": len(first_dates),
        "second_half_dates": len(second_dates),
        "features": stable,
        "ranking_uses_edge_magnitude": False,
    }


def _percentile_map(values: list[tuple[int, float]]) -> dict[int, float]:
    if not values:
        return {}
    ordered = sorted(values, key=lambda item: item[1])
    by_value: dict[float, list[int]] = defaultdict(list)
    for index, value in ordered:
        by_value[float(value)].append(index)
    unique = sorted(by_value)
    if len(unique) <= 1:
        return {index: 0.0 for index, _value in values}
    result: dict[int, float] = {}
    for rank, value in enumerate(unique):
        pct = rank / (len(unique) - 1)
        centred = 2.0 * pct - 1.0
        for index in by_value[value]:
            result[index] = centred
    return result


def _score_day(rows: list[dict], model: dict) -> list[dict]:
    features = model.get("features") or {}
    accum: list[list[float]] = [[] for _ in rows]
    contributions: list[dict[str, float]] = [dict() for _ in rows]
    for feature, meta in features.items():
        observed: list[tuple[int, float]] = []
        for index, row in enumerate(rows):
            value = _entry_features(row).get(feature)
            if value is not None:
                observed.append((index, float(value)))
        percentiles = _percentile_map(observed)
        direction = float(meta.get("direction") or 0.0)
        for index, relative in percentiles.items():
            contribution = direction * float(relative)
            accum[index].append(contribution)
            contributions[index][feature] = round(contribution, 4)

    scored: list[dict] = []
    for index, row in enumerate(rows):
        item = dict(row)
        used = len(accum[index])
        item["cross_sectional_features_used"] = used
        item["cross_sectional_contributions"] = contributions[index]
        item["cross_sectional_entry_quality_score"] = (
            round(mean(accum[index]), 6)
            if used >= MIN_STABLE_FEATURES else None
        )
        scored.append(item)
    return scored


def _selection_key_challenger(row: dict) -> tuple[float, float, float, str]:
    score = _f(row.get("cross_sectional_entry_quality_score"))
    alpha = _f(row.get("underlying_alpha_score")) or -1e12
    tradeability = _f(row.get("tradeability_score")) or -1e12
    return (
        float(score) if score is not None else -1e12,
        float(alpha),
        float(tradeability),
        str(row.get("ticker") or ""),
    )


def _selection_key_baseline(row: dict) -> tuple[float, float, str]:
    return (
        float(_f(row.get("underlying_alpha_score")) or -1e12),
        float(_f(row.get("tradeability_score")) or -1e12),
        str(row.get("ticker") or ""),
    )


def _metrics(rows: list[dict]) -> dict:
    returns = [
        float(row["boundary_return_pct"])
        for row in rows if _f(row.get("boundary_return_pct")) is not None
    ]
    completed = [row for row in rows if row.get("boundary_exit_reason") == "MOVEMENT_COMPLETED"]
    invalidated = [
        row for row in rows
        if str(row.get("boundary_exit_reason") or "").startswith("THESIS_INVALIDATED")
        or row.get("boundary_exit_reason") == "INVALIDATION_AMBIGUOUS_BAR"
    ]
    return {
        "sample": len(returns),
        "win_rate_pct": round(100.0 * sum(value > 0 for value in returns) / len(returns), 2) if returns else None,
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "median_return_pct": round(median(returns), 4) if returns else None,
        "profit_factor": _profit_factor(returns),
        "movement_completed": len(completed),
        "thesis_invalidated": len(invalidated),
    }


def _correlation(rows: list[dict]) -> float | None:
    pairs = [
        (float(row["cross_sectional_entry_quality_score"]), float(row["boundary_return_pct"]))
        for row in rows
        if _f(row.get("cross_sectional_entry_quality_score")) is not None
        and _f(row.get("boundary_return_pct")) is not None
    ]
    if len(pairs) < 3:
        return None
    xs = [x for x, _y in pairs]
    ys = [y for _x, y in pairs]
    mx = mean(xs)
    my = mean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return None
    return round(numerator / (dx * dy), 4)


def _lift(challenger: dict, baseline: dict) -> dict:
    avg_c = _f(challenger.get("avg_return_pct"))
    avg_b = _f(baseline.get("avg_return_pct"))
    pf_c = _f(challenger.get("profit_factor"))
    pf_b = _f(baseline.get("profit_factor"))
    return {
        "avg_return_lift_pp": round(avg_c - avg_b, 4) if avg_c is not None and avg_b is not None else None,
        "profit_factor_lift": round(pf_c - pf_b, 4) if pf_c is not None and pf_b is not None else None,
    }


def build_cross_sectional_entry_quality_walkforward(
    candidate_lifecycle_rows: list[dict],
    *,
    max_positions: int = 5,
) -> dict:
    """Walk-forward Top-N ranking over the full actionable ENTER candidate pool."""
    resolved = _resolved(candidate_lifecycle_rows)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in resolved:
        by_date[str(row.get("entry_date") or "")[:10]].append(row)

    baseline_selected: list[dict] = []
    challenger_selected: list[dict] = []
    crowded_baseline: list[dict] = []
    crowded_challenger: list[dict] = []
    scored_candidates: list[dict] = []
    date_results: list[dict] = []
    latest_model: dict = {}
    skipped_dates = 0

    for test_date in sorted(by_date):
        day_rows = by_date[test_date]
        train = [
            row for row in resolved
            if str(row.get("boundary_exit_date") or "")[:10] < test_date
        ]
        model = _fit_stable_feature_directions(train)
        if model.get("status") != "EVALUABLE":
            skipped_dates += 1
            continue
        latest_model = model
        scored = _score_day(day_rows, model)
        scored_candidates.extend(scored)
        selection_n = min(max(0, int(max_positions)), len(scored))
        if selection_n <= 0:
            continue

        baseline = sorted(scored, key=_selection_key_baseline, reverse=True)[:selection_n]
        challenger = sorted(scored, key=_selection_key_challenger, reverse=True)[:selection_n]
        baseline_selected.extend(baseline)
        challenger_selected.extend(challenger)

        baseline_keys = {(str(row.get("ticker")), str(row.get("direction"))) for row in baseline}
        challenger_keys = {(str(row.get("ticker")), str(row.get("direction"))) for row in challenger}
        crowded = len(scored) > selection_n
        if crowded:
            crowded_baseline.extend(baseline)
            crowded_challenger.extend(challenger)

        baseline_avg = mean(float(row["boundary_return_pct"]) for row in baseline)
        challenger_avg = mean(float(row["boundary_return_pct"]) for row in challenger)
        date_results.append({
            "date": test_date,
            "train_trades": len(train),
            "train_dates": len({str(row.get("entry_date") or "")[:10] for row in train}),
            "stable_features": len(model.get("features") or {}),
            "candidate_count": len(scored),
            "selection_n": selection_n,
            "crowded": crowded,
            "selection_changed": baseline_keys != challenger_keys,
            "selection_overlap": len(baseline_keys & challenger_keys),
            "baseline_avg_return_pct": round(baseline_avg, 4),
            "challenger_avg_return_pct": round(challenger_avg, 4),
            "challenger_lift_pp": round(challenger_avg - baseline_avg, 4),
        })

    baseline_metrics = _metrics(baseline_selected)
    challenger_metrics = _metrics(challenger_selected)
    crowded_baseline_metrics = _metrics(crowded_baseline)
    crowded_challenger_metrics = _metrics(crowded_challenger)
    changed_dates = [row for row in date_results if row.get("selection_changed")]
    crowded_dates = [row for row in date_results if row.get("crowded")]
    positive_lift_dates = [
        row for row in changed_dates
        if _f(row.get("challenger_lift_pp")) is not None and float(row["challenger_lift_pp"]) > 0
    ]

    return {
        "mode": "ENTRY_QUALITY_CROSS_SECTIONAL_WALK_FORWARD_SHADOW_V2",
        "status": "EVALUABLE" if date_results else "INSUFFICIENT_OOT_SAMPLE",
        "live_mutation": False,
        "source": "ALL_ACTIONABLE_FUSION_ENTER_CANONICAL_BOUNDARY_LIFECYCLE",
        "candidate_pool_resolved": len(resolved),
        "test_dates": len(date_results),
        "skipped_warmup_dates": skipped_dates,
        "crowded_test_dates": len(crowded_dates),
        "selection_changed_dates": len(changed_dates),
        "selection_changed_pct": round(100.0 * len(changed_dates) / len(date_results), 2) if date_results else None,
        "positive_lift_changed_dates": len(positive_lift_dates),
        "positive_lift_changed_dates_pct": round(
            100.0 * len(positive_lift_dates) / len(changed_dates), 2
        ) if changed_dates else None,
        "all_test_dates": {
            "baseline_current_alpha_rank": baseline_metrics,
            "challenger_cross_sectional_rank": challenger_metrics,
            "lift": _lift(challenger_metrics, baseline_metrics),
        },
        "crowded_dates_only": {
            "baseline_current_alpha_rank": crowded_baseline_metrics,
            "challenger_cross_sectional_rank": crowded_challenger_metrics,
            "lift": _lift(crowded_challenger_metrics, crowded_baseline_metrics),
        },
        "score_actual_correlation": _correlation(scored_candidates),
        "latest_train_model": latest_model,
        "methodology": {
            "training_labels_available_only_after_boundary_exit": True,
            "train_rows_must_resolve_before_test_entry": True,
            "candidate_pool_is_full_actionable_enter": True,
            "test_selection_count_matches_baseline": True,
            "ranking_is_within_same_day_cross_section": True,
            "feature_direction_requires_chronological_half_consistency": True,
            "feature_effect_magnitude_not_used_as_weight": True,
            "threshold_optimized_on_test": False,
            "max_positions": int(max_positions),
            "minimum_train_trades": MIN_TRAIN_TRADES,
            "minimum_train_dates": MIN_TRAIN_DATES,
            "minimum_stable_features": MIN_STABLE_FEATURES,
            "purpose": "isolate whether point-in-time features improve Top-N capital-slot ranking among actionable ENTER candidates",
        },
        "date_results": date_results[-120:],
    }
