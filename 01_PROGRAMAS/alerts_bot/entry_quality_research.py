from __future__ import annotations

import math
from statistics import mean, median
from typing import Any

MIN_TRAIN_TRADES = 30
MIN_TRAIN_DATES = 10
MIN_FEATURE_OBSERVATIONS = 20
MIN_SPLIT_SIDE = 5

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
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _profit_factor(values: list[float]) -> float | None:
    positive = sum(x for x in values if x > 0)
    negative = abs(sum(x for x in values if x < 0))
    if negative <= 0:
        return None if positive <= 0 else float("inf")
    return round(positive / negative, 4)


def _entry_features(row: dict) -> dict[str, float | None]:
    diag = row.get("entry_diagnostics") if isinstance(row.get("entry_diagnostics"), dict) else {}
    entry = _f(row.get("entry_price"))
    target = _f(row.get("target_price"))
    direction = str(row.get("direction") or "").upper()
    target_distance = None
    if entry and entry > 0 and target and target > 0:
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
        and str(row.get("boundary_exit_date") or "")[:10]
        and str(row.get("entry_date") or "")[:10]
    ]


def _balanced_split_cut(values: list[float]) -> float | None:
    ordered = sorted(float(value) for value in values)
    if len(ordered) < 2:
        return None
    best_cut = None
    best_imbalance = None
    best_min_side = None
    for index in range(len(ordered) - 1):
        left, right = ordered[index], ordered[index + 1]
        if left == right:
            continue
        low_n = index + 1
        high_n = len(ordered) - low_n
        imbalance = abs(low_n - high_n)
        min_side = min(low_n, high_n)
        cut = (left + right) / 2.0
        if best_imbalance is None or imbalance < best_imbalance or (imbalance == best_imbalance and min_side > int(best_min_side or 0)):
            best_cut = cut
            best_imbalance = imbalance
            best_min_side = min_side
    return float(best_cut) if best_cut is not None else None

def _fit(train_rows: list[dict]) -> dict:
    base_returns = [float(row["boundary_return_pct"]) for row in train_rows]
    model = {
        "base_return_pct": mean(base_returns) if base_returns else 0.0,
        "features": {},
        "train_trades": len(train_rows),
        "train_dates": len({str(row.get("entry_date") or "")[:10] for row in train_rows}),
    }
    for name in FEATURES:
        pairs: list[tuple[float, float]] = []
        for row in train_rows:
            value = _entry_features(row).get(name)
            ret = _f(row.get("boundary_return_pct"))
            if value is not None and ret is not None:
                pairs.append((float(value), float(ret)))
        if len(pairs) < MIN_FEATURE_OBSERVATIONS:
            continue
        cut = _balanced_split_cut([value for value, _ret in pairs])
        if cut is None:
            continue
        low = [ret for value, ret in pairs if value <= cut]
        high = [ret for value, ret in pairs if value > cut]
        if len(low) < MIN_SPLIT_SIDE or len(high) < MIN_SPLIT_SIDE:
            continue
        low_mean = mean(low)
        high_mean = mean(high)
        edge = (high_mean - low_mean) / 2.0
        model["features"][name] = {
            "median": float(cut),
            "low_n": len(low),
            "high_n": len(high),
            "low_avg_return_pct": round(low_mean, 4),
            "high_avg_return_pct": round(high_mean, 4),
            "half_spread_pp": round(edge, 4),
        }
    return model


def _predict(row: dict, model: dict) -> tuple[float | None, dict]:
    contributions: list[float] = []
    detail: dict[str, float] = {}
    values = _entry_features(row)
    for name, meta in (model.get("features") or {}).items():
        value = values.get(name)
        if value is None:
            continue
        edge = float(meta["half_spread_pp"])
        contribution = edge if float(value) > float(meta["median"]) else -edge
        contributions.append(contribution)
        detail[name] = round(contribution, 4)
    if len(contributions) < 3:
        return None, detail
    prediction = float(model.get("base_return_pct") or 0.0) + mean(contributions)
    return round(prediction, 4), detail


def _metrics(rows: list[dict]) -> dict:
    returns = [float(row["boundary_return_pct"]) for row in rows if _f(row.get("boundary_return_pct")) is not None]
    completed = [row for row in rows if row.get("boundary_exit_reason") == "MOVEMENT_COMPLETED"]
    invalidated = [
        row for row in rows
        if str(row.get("boundary_exit_reason") or "").startswith("THESIS_INVALIDATED")
        or row.get("boundary_exit_reason") == "INVALIDATION_AMBIGUOUS_BAR"
    ]
    return {
        "sample": len(returns),
        "win_rate_pct": round(100.0 * sum(x > 0 for x in returns) / len(returns), 2) if returns else None,
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "profit_factor": _profit_factor(returns),
        "movement_completed": len(completed),
        "movement_completed_pct": round(100.0 * len(completed) / len(rows), 2) if rows else None,
        "thesis_invalidated": len(invalidated),
        "thesis_invalidated_pct": round(100.0 * len(invalidated) / len(rows), 2) if rows else None,
    }


def _correlation(rows: list[dict]) -> float | None:
    pairs = [
        (float(row["entry_quality_predicted_return_pct"]), float(row["boundary_return_pct"]))
        for row in rows
        if _f(row.get("entry_quality_predicted_return_pct")) is not None
        and _f(row.get("boundary_return_pct")) is not None
    ]
    if len(pairs) < 3:
        return None
    xs = [x for x, _y in pairs]
    ys = [y for _x, y in pairs]
    mx, my = mean(xs), mean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return None
    return round(numerator / (dx * dy), 4)


def _quartiles(rows: list[dict]) -> list[dict]:
    ordered = sorted(
        [row for row in rows if _f(row.get("entry_quality_predicted_return_pct")) is not None],
        key=lambda row: float(row["entry_quality_predicted_return_pct"]),
    )
    if not ordered:
        return []
    result = []
    n = len(ordered)
    for index in range(4):
        start = (n * index) // 4
        end = (n * (index + 1)) // 4
        bucket = ordered[start:end]
        if not bucket:
            continue
        result.append({
            "quartile": index + 1,
            "score_min": round(float(bucket[0]["entry_quality_predicted_return_pct"]), 4),
            "score_max": round(float(bucket[-1]["entry_quality_predicted_return_pct"]), 4),
            "metrics": _metrics(bucket),
        })
    return result


def build_entry_quality_walkforward(lifecycle_rows: list[dict]) -> dict:
    """Out-of-time entry-quality diagnostic using only previously resolved trades.

    The model is deliberately simple and interpretable:
    - fixed point-in-time feature list;
    - train-only median splits;
    - feature edge estimated only from outcomes resolved before each test entry;
    - no hindsight threshold tuning;
    - predicted return > 0 is the only acceptance rule.

    This is research-only and does not alter LIVE selection or lifecycle.
    """
    resolved = _resolved(lifecycle_rows)
    resolved.sort(key=lambda row: (str(row.get("entry_date") or ""), str(row.get("ticker") or "")))

    scored: list[dict] = []
    skipped = 0
    latest_model: dict = {}
    for row in resolved:
        entry_date = str(row.get("entry_date") or "")[:10]
        train = [
            prior for prior in resolved
            if str(prior.get("boundary_exit_date") or "")[:10] < entry_date
        ]
        train_dates = {str(prior.get("entry_date") or "")[:10] for prior in train}
        if len(train) < MIN_TRAIN_TRADES or len(train_dates) < MIN_TRAIN_DATES:
            skipped += 1
            continue
        model = _fit(train)
        if len(model.get("features") or {}) < 3:
            skipped += 1
            continue
        prediction, contributions = _predict(row, model)
        if prediction is None:
            skipped += 1
            continue
        latest_model = model
        item = dict(row)
        item["entry_quality_predicted_return_pct"] = prediction
        item["entry_quality_accept"] = prediction > 0.0
        item["entry_quality_train_trades"] = len(train)
        item["entry_quality_train_dates"] = len(train_dates)
        item["entry_quality_features_used"] = len(contributions)
        item["entry_quality_contributions"] = contributions
        scored.append(item)

    accepted = [row for row in scored if row.get("entry_quality_accept")]
    rejected = [row for row in scored if not row.get("entry_quality_accept")]
    quartiles = _quartiles(scored)
    quartile_avgs = [
        _f((row.get("metrics") or {}).get("avg_return_pct"))
        for row in quartiles
    ]
    quartile_avgs = [float(x) for x in quartile_avgs if x is not None]
    monotonic = (
        len(quartile_avgs) == len(quartiles) == 4
        and all(a <= b for a, b in zip(quartile_avgs, quartile_avgs[1:]))
    )

    all_metrics = _metrics(scored)
    accepted_metrics = _metrics(accepted)
    rejected_metrics = _metrics(rejected)
    avg_lift = None
    if accepted_metrics.get("avg_return_pct") is not None and all_metrics.get("avg_return_pct") is not None:
        avg_lift = round(
            float(accepted_metrics["avg_return_pct"]) - float(all_metrics["avg_return_pct"]),
            4,
        )

    return {
        "mode": "ENTRY_QUALITY_WALK_FORWARD_SHADOW_V1",
        "status": "EVALUABLE" if scored else "INSUFFICIENT_OOT_SAMPLE",
        "live_mutation": False,
        "source": "ENTER_FIRST_STATEFUL_BOUNDARY_LIFECYCLE",
        "resolved_input_trades": len(resolved),
        "oot_scored_trades": len(scored),
        "warmup_skipped_trades": skipped,
        "accepted_trades": len(accepted),
        "rejected_trades": len(rejected),
        "coverage_pct": round(100.0 * len(scored) / len(resolved), 2) if resolved else None,
        "all_scored": all_metrics,
        "accepted": accepted_metrics,
        "rejected": rejected_metrics,
        "accepted_avg_return_lift_vs_all_pp": avg_lift,
        "prediction_actual_correlation": _correlation(scored),
        "score_quartiles": quartiles,
        "quartile_avg_return_monotonic": monotonic,
        "latest_train_model": latest_model,
        "methodology": {
            "training_labels_available_only_after_boundary_exit": True,
            "train_rows_must_resolve_before_test_entry": True,
            "future_test_return_used_only_for_evaluation": True,
            "fixed_features_not_test_selected": list(FEATURES),
            "train_only_median_splits": True,
            "acceptance_rule": "predicted_return_pct > 0",
            "threshold_optimized_on_test": False,
            "minimum_train_trades": MIN_TRAIN_TRADES,
            "minimum_train_dates": MIN_TRAIN_DATES,
            "purpose": "test whether point-in-time entry diagnostics separate higher-expectancy ENTER candidates before building a capital-slot challenger",
        },
        "scored_trades": scored[-250:],
    }
