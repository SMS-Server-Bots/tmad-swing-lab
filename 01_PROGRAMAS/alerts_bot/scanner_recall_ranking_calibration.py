from __future__ import annotations

"""TMAD 6.2.25 Scanner recall & ranking calibration.

Research / SHADOW only.

The full Scanner snapshot is persisted before the bounded enrichment selection.
This module therefore separates two questions:

1) COVERAGE: how many >=5% within <=5-session opportunities exist inside the
   persisted full Scanner cross-section?
2) PRIORITY: does the bounded enrichment/ranking logic concentrate those
   opportunities inside Top-5/10/20/50/100/180?

Future prices are labels only. No Scanner selection, enrichment cap, CORE,
Research, GEX, Timing, FUSION, Lifecycle or LIVE rule is modified.
"""

from collections import defaultdict
import math
from statistics import mean, median
from typing import Any, Callable

from economic_waterfall_calibration import (
    _best_direction,
    _direction_path,
    _entry_price,
    _f,
    _feature_payload,
    _future_same_segment,
)


MODE = "SCANNER_RECALL_RANKING_CALIBRATION_V1"
TARGET_PCT = 5.0
HORIZON_SESSIONS = 5
CUTS = (5, 10, 20, 50, 100, 180)
CAPACITY = 180
MIN_FEATURE_COVERAGE_PCT = 80.0
MIN_STABLE_LIFT_PP = 0.5

_EMPTY_SIGNAL_TEXT = {"", "none", "null", "n/a", "na", "no signal", "neutral", "unknown"}


def _num(value: Any) -> float | None:
    return _f(value)


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 0:
        return None
    return round(gains / losses, 4)


def _mean_value(rows: list[dict], key: str) -> float | None:
    values = [
        float(value)
        for value in (_num(row.get(key)) for row in rows)
        if value is not None
    ]
    return round(mean(values), 4) if values else None


def _directional_subset_metrics(rows: list[dict]) -> dict:
    """Economic sanity using only the point-in-time current CORE direction.

    This deliberately does not use the hindsight best direction for P&L. The
    best-direction label remains useful for opportunity recall, but a Scanner
    challenger must not pass merely by selecting volatile names that current
    CORE would trade in the wrong direction.
    """
    known = [
        row for row in rows
        if str(row.get("current_core_direction") or "").upper() in {"LONG", "SHORT"}
    ]
    best_movers = [row for row in known if bool(row.get("best_mover_5d"))]
    directional_movers = [
        row for row in known if bool(row.get("current_direction_mover_5d"))
    ]
    returns = [
        float(value)
        for value in (_num(row.get("current_direction_close_return_pct")) for row in known)
        if value is not None
    ]
    correct = sum(
        str(row.get("current_core_direction") or "").upper()
        == str(row.get("best_direction") or "").upper()
        for row in best_movers
    )
    first_hits = [
        float(value)
        for value in (_num(row.get("current_direction_first_hit_session")) for row in directional_movers)
        if value is not None
    ]
    return {
        "selected_sample": len(rows),
        "core_known": len(known),
        "core_coverage_pct": (
            round(100.0 * len(known) / len(rows), 3) if rows else None
        ),
        "best_movers_with_core": len(best_movers),
        "direction_accuracy_on_best_movers_pct": (
            round(100.0 * correct / len(best_movers), 3) if best_movers else None
        ),
        "directional_movers_5d": len(directional_movers),
        "directional_mover_precision_pct": (
            round(100.0 * len(directional_movers) / len(known), 3) if known else None
        ),
        "avg_mfe_pct": _mean_value(known, "current_direction_mfe_pct"),
        "avg_mae_pct": _mean_value(known, "current_direction_mae_pct"),
        "avg_horizon_close_return_pct": _mean_value(
            known, "current_direction_close_return_pct"
        ),
        "horizon_close_profit_factor": _profit_factor(returns),
        "avg_first_hit_session": (
            round(mean(first_hits), 3) if first_hits else None
        ),
    }


def _aggregate_directional(by_date_ranked: dict[str, list[dict]]) -> dict:
    out: dict[str, dict] = {}
    for cutoff in CUTS:
        selected = [
            row for day in sorted(by_date_ranked)
            for row in by_date_ranked[day]
            if int(row["rank"]) <= int(cutoff)
        ]
        out[f"top_{cutoff}"] = _directional_subset_metrics(selected)
    return out


def _selection_map(
    by_date_ranked: dict[str, list[dict]],
    cutoff: int,
) -> dict[tuple[str, str], dict]:
    return {
        (str(row.get("date") or ""), str(row.get("ticker") or "")): row
        for day in sorted(by_date_ranked)
        for row in by_date_ranked[day]
        if int(row.get("rank") or 0) <= int(cutoff)
    }


def _replacement_analysis(
    current_ranked: dict[str, list[dict]],
    challenger_ranked: dict[str, list[dict]],
    cutoff: int,
) -> dict:
    current = _selection_map(current_ranked, cutoff)
    challenger = _selection_map(challenger_ranked, cutoff)
    current_keys = set(current)
    challenger_keys = set(challenger)
    overlap = current_keys & challenger_keys
    added_keys = challenger_keys - current_keys
    dropped_keys = current_keys - challenger_keys
    added = [challenger[key] for key in sorted(added_keys)]
    dropped = [current[key] for key in sorted(dropped_keys)]
    return {
        "cutoff": int(cutoff),
        "current_identities": len(current_keys),
        "challenger_identities": len(challenger_keys),
        "overlap": len(overlap),
        "added": len(added_keys),
        "dropped": len(dropped_keys),
        "replacement_pct_of_challenger": (
            round(100.0 * len(added_keys) / len(challenger_keys), 3)
            if challenger_keys else None
        ),
        "added_directional_economics": _directional_subset_metrics(added),
        "dropped_directional_economics": _directional_subset_metrics(dropped),
    }


def _not_worse(
    challenger: float | None,
    current: float | None,
    *,
    tolerance: float = 0.0,
    higher_is_better: bool = True,
) -> bool:
    if current is None:
        return True
    if challenger is None:
        return False
    if higher_is_better:
        return float(challenger) + float(tolerance) >= float(current)
    return float(challenger) <= float(current) + float(tolerance)


def _signal(value: Any) -> bool:
    return str(value or "").strip().lower() not in _EMPTY_SIGNAL_TEXT


def _payload_value(payload: dict, *keys: str) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def _wall_distance(payload: dict) -> float | None:
    values = []
    for key in ("put_wall_pct", "call_wall_pct", "putWallPct", "callWallPct"):
        value = _num(payload.get(key))
        if value is not None:
            values.append(abs(float(value)))
    return min(values) if values else None


def _scanner_metrics(payload: dict) -> dict:
    gex_change = _num(_payload_value(
        payload,
        "gex_change", "gexChange", "gexChangePct", "total_gex_change_pct",
    ))
    long_score = _num(_payload_value(payload, "long_score", "longScore"))
    momentum = _num(_payload_value(payload, "momentum_score", "momentumScore"))
    rvol = _num(_payload_value(payload, "rvol", "relativeVolume", "relative_volume"))
    call_z = _num(_payload_value(payload, "otm_call_volume_z", "otmCallVolumeZ"))
    put_z = _num(_payload_value(payload, "otm_put_volume_z", "otmPutVolumeZ"))
    option_signal = _payload_value(payload, "option_signal", "optionSignal", "signal")
    flow_signal = _payload_value(payload, "unusual_flow_signal", "unusualFlowSignal", "flowSignal")
    volume_z_values = [abs(v) for v in (call_z, put_z) if v is not None]
    return {
        "has_signal": _signal(option_signal) or _signal(flow_signal),
        "abs_gex_change": abs(gex_change) if gex_change is not None else None,
        "wall_distance": _wall_distance(payload),
        "rvol": rvol,
        "long_score": long_score,
        "momentum": momentum,
        "volume_z": max(volume_z_values) if volume_z_values else None,
    }


def _current_static_priority(payload: dict) -> float:
    """Pure proxy for the stable selection_base._priority_score.

    Dynamic overlays (same-day change-map, mandatory/open positions, continuity,
    relationship seeds and negative-cache state) are intentionally excluded
    because they are not reproducible for every frozen historical date.
    """
    m = _scanner_metrics(payload)
    score = 0.0
    if m["has_signal"]:
        score += 160.0

    gex = m["abs_gex_change"]
    if gex is not None:
        score += min(float(gex), 100.0) * 1.5

    wall = m["wall_distance"]
    if wall is not None:
        if wall <= 0.50:
            score += 180.0
        elif wall <= 1.00:
            score += 130.0
        elif wall <= 1.50:
            score += 90.0
        elif wall <= 2.50:
            score += 45.0

    rvol = m["rvol"]
    if rvol is not None:
        if rvol >= 2.0:
            score += 120.0
        elif rvol >= 1.5:
            score += 80.0
        elif rvol >= 1.2:
            score += 45.0

    long_score = m["long_score"]
    if long_score is not None and (long_score >= 70.0 or long_score <= 30.0):
        score += min(abs(long_score - 50.0), 50.0) * 1.5

    momentum = m["momentum"]
    if momentum is not None and abs(momentum) >= 60.0:
        score += min(abs(momentum), 100.0) * 0.6

    volume_z = m["volume_z"]
    if volume_z is not None:
        if volume_z >= 3.0:
            score += 100.0
        elif volume_z >= 2.0:
            score += 60.0
    return float(score)


def _feature_abs_long_score(payload: dict) -> float | None:
    value = _num(_payload_value(payload, "long_score", "longScore"))
    return abs(float(value) - 50.0) if value is not None else None


def _feature_abs_momentum(payload: dict) -> float | None:
    value = _num(_payload_value(payload, "momentum_score", "momentumScore"))
    return abs(float(value)) if value is not None else None


def _feature_rvol(payload: dict) -> float | None:
    return _num(_payload_value(payload, "rvol", "relativeVolume", "relative_volume"))


def _feature_option_volume_z(payload: dict) -> float | None:
    m = _scanner_metrics(payload)
    return m["volume_z"]


def _feature_abs_gex_change(payload: dict) -> float | None:
    return _scanner_metrics(payload)["abs_gex_change"]


def _feature_wall_proximity(payload: dict) -> float | None:
    distance = _wall_distance(payload)
    return -float(distance) if distance is not None else None


def _feature_abs_change(payload: dict) -> float | None:
    value = _num(_payload_value(payload, "change_percent", "changePercent"))
    return abs(float(value)) if value is not None else None


def _feature_abs_trend(payload: dict) -> float | None:
    value = _num(_payload_value(payload, "scanner_trend_score", "scannerTrendScore"))
    return abs(float(value)) if value is not None else None


def _feature_rsi_extreme(payload: dict) -> float | None:
    value = _num(_payload_value(payload, "rsi", "rsi14", "rsi14Value"))
    return abs(float(value) - 50.0) if value is not None else None


FEATURES: dict[str, Callable[[dict], float | None]] = {
    "abs_long_score_extreme": _feature_abs_long_score,
    "abs_momentum": _feature_abs_momentum,
    "rvol": _feature_rvol,
    "option_volume_z": _feature_option_volume_z,
    "abs_gex_change": _feature_abs_gex_change,
    "wall_proximity": _feature_wall_proximity,
    "abs_change_percent": _feature_abs_change,
    "abs_scanner_trend": _feature_abs_trend,
    "rsi_extreme": _feature_rsi_extreme,
}


def _build_rows(
    canonical: dict[tuple[str, str], dict],
    trusted_price_series: dict[str, list[dict]],
) -> list[dict]:
    rows: list[dict] = []
    for (date_value, ticker_value), source in sorted(canonical.items()):
        day = str(date_value)[:10]
        ticker = str(ticker_value).upper()
        entry = _entry_price(source)
        if entry is None or entry <= 0:
            continue
        future = _future_same_segment(
            trusted_price_series.get(ticker, []),
            day,
            HORIZON_SESSIONS,
        )
        if not future:
            continue
        best_direction, best_path = _best_direction(float(entry), future, TARGET_PCT)
        current_core_direction = str(source.get("core_direction") or "").upper()
        current_core_known = current_core_direction in {"LONG", "SHORT"}
        current_path = (
            _direction_path(
                float(entry),
                current_core_direction,
                future,
                TARGET_PCT,
            )
            if current_core_known
            else {}
        )
        payload = _feature_payload(source)
        feature_values = {
            name: fn(payload)
            for name, fn in FEATURES.items()
        }
        rows.append({
            "date": day,
            "ticker": ticker,
            "entry_price": float(entry),
            "best_direction": best_direction,
            "best_mover_5d": bool(float(best_path.get("mfe_pct") or 0.0) >= TARGET_PCT),
            "best_mfe_pct": float(best_path.get("mfe_pct") or 0.0),
            "best_mae_pct": float(best_path.get("mae_pct") or 0.0),
            "first_hit_session": best_path.get("first_hit_session"),
            "current_core_known": current_core_known,
            "current_core_direction": (
                current_core_direction if current_core_known else None
            ),
            "current_direction_mover_5d": bool(
                current_core_known
                and float(current_path.get("mfe_pct") or 0.0) >= TARGET_PCT
            ),
            "current_direction_mfe_pct": (
                float(current_path.get("mfe_pct") or 0.0)
                if current_core_known else None
            ),
            "current_direction_mae_pct": (
                float(current_path.get("mae_pct") or 0.0)
                if current_core_known else None
            ),
            "current_direction_first_hit_session": (
                current_path.get("first_hit_session")
                if current_core_known else None
            ),
            "current_direction_close_return_pct": (
                _num(current_path.get("horizon_close_return_pct"))
                if current_core_known else None
            ),
            "current_static_priority": _current_static_priority(payload),
            "feature_values": feature_values,
        })
    return rows


def _split_dates(rows: list[dict]) -> tuple[set[str], set[str], set[str]]:
    dates = sorted({str(row.get("date") or "") for row in rows if row.get("date")})
    if len(dates) < 8:
        return set(dates), set(), set()
    n = len(dates)
    a = max(1, int(math.floor(n * 0.50)))
    b = min(n - 1, max(a + 1, int(math.floor(n * 0.75))))
    return set(dates[:a]), set(dates[a:b]), set(dates[b:])


def _rank_rows_by_score(rows: list[dict], score_key: str) -> list[dict]:
    ranked = sorted(
        rows,
        key=lambda row: (
            float(row.get(score_key) or 0.0),
            str(row.get("ticker") or ""),
        ),
        reverse=True,
    )
    return [{**row, "rank": idx} for idx, row in enumerate(ranked, start=1)]


def _percentile_scores(rows: list[dict], features: list[str]) -> dict[str, float]:
    if not rows or not features:
        return {}
    by_feature: dict[str, dict[str, float]] = {}
    for feature in features:
        clean = [
            (float((row.get("feature_values") or {}).get(feature)), str(row["ticker"]))
            for row in rows
            if _num((row.get("feature_values") or {}).get(feature)) is not None
        ]
        clean.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
        n = len(clean)
        pcts: dict[str, float] = {}
        for idx, (_value, ticker) in enumerate(clean):
            pcts[ticker] = 100.0 if n <= 1 else 100.0 * (1.0 - idx / (n - 1))
        by_feature[feature] = pcts

    scores: dict[str, float] = {}
    for row in rows:
        ticker = str(row["ticker"])
        values = [
            by_feature[feature][ticker]
            for feature in features
            if ticker in by_feature.get(feature, {})
        ]
        if values:
            scores[ticker] = mean(values)
    return scores


def _rank_rows_challenger(rows: list[dict], features: list[str]) -> list[dict]:
    scores = _percentile_scores(rows, features)
    prepared = [
        {**row, "challenger_score": scores.get(str(row["ticker"]))}
        for row in rows
        if str(row["ticker"]) in scores
    ]
    return _rank_rows_by_score(prepared, "challenger_score")


def _rank_map(rows: list[dict]) -> dict[tuple[str, str], int]:
    return {
        (str(row["date"]), str(row["ticker"])): int(row["rank"])
        for row in rows
    }


def _selection_metrics(ranked: list[dict], cutoff: int) -> dict:
    selected = [row for row in ranked if int(row["rank"]) <= int(cutoff)]
    all_movers = sum(bool(row.get("best_mover_5d")) for row in ranked)
    selected_movers = sum(bool(row.get("best_mover_5d")) for row in selected)
    precision = 100.0 * selected_movers / len(selected) if selected else None
    recall = 100.0 * selected_movers / all_movers if all_movers else None
    base_rate = 100.0 * all_movers / len(ranked) if ranked else None
    expected_random_movers = (
        all_movers * min(int(cutoff), len(ranked)) / len(ranked)
        if ranked else 0.0
    )
    random_recall = 100.0 * expected_random_movers / all_movers if all_movers else None
    mover_rows = [row for row in selected if row.get("best_mover_5d")]
    return {
        "sample": len(selected),
        "selected_movers": selected_movers,
        "all_movers": all_movers,
        "precision_pct": round(precision, 3) if precision is not None else None,
        "recall_pct": round(recall, 3) if recall is not None else None,
        "universe_mover_rate_pct": round(base_rate, 3) if base_rate is not None else None,
        "precision_lift_vs_universe_pp": (
            round(precision - base_rate, 3)
            if precision is not None and base_rate is not None else None
        ),
        "random_expected_recall_pct": (
            round(random_recall, 3) if random_recall is not None else None
        ),
        "recall_lift_vs_random_pp": (
            round(recall - random_recall, 3)
            if recall is not None and random_recall is not None else None
        ),
        "avg_best_mfe_pct": (
            round(mean(float(row["best_mfe_pct"]) for row in mover_rows), 4)
            if mover_rows else None
        ),
        "avg_first_hit_session": (
            round(mean(
                float(row["first_hit_session"])
                for row in mover_rows
                if row.get("first_hit_session") is not None
            ), 3)
            if any(row.get("first_hit_session") is not None for row in mover_rows)
            else None
        ),
    }


def _aggregate_ranked(by_date_ranked: dict[str, list[dict]]) -> dict:
    out = {}
    flattened = [row for day in sorted(by_date_ranked) for row in by_date_ranked[day]]
    total_movers = sum(bool(row.get("best_mover_5d")) for row in flattened)
    for cutoff in CUTS:
        selected = [
            row for day in sorted(by_date_ranked)
            for row in by_date_ranked[day]
            if int(row["rank"]) <= cutoff
        ]
        selected_movers = sum(bool(row.get("best_mover_5d")) for row in selected)
        base_rate = 100.0 * total_movers / len(flattened) if flattened else None
        precision = 100.0 * selected_movers / len(selected) if selected else None
        recall = 100.0 * selected_movers / total_movers if total_movers else None

        expected = 0.0
        for day_rows in by_date_ranked.values():
            movers = sum(bool(row.get("best_mover_5d")) for row in day_rows)
            if day_rows:
                expected += movers * min(cutoff, len(day_rows)) / len(day_rows)
        random_recall = 100.0 * expected / total_movers if total_movers else None

        mover_rows = [row for row in selected if row.get("best_mover_5d")]
        out[f"top_{cutoff}"] = {
            "sample": len(selected),
            "selected_movers": selected_movers,
            "all_movers": total_movers,
            "precision_pct": round(precision, 3) if precision is not None else None,
            "recall_pct": round(recall, 3) if recall is not None else None,
            "universe_mover_rate_pct": round(base_rate, 3) if base_rate is not None else None,
            "precision_lift_vs_universe_pp": (
                round(precision - base_rate, 3)
                if precision is not None and base_rate is not None else None
            ),
            "random_expected_recall_pct": (
                round(random_recall, 3) if random_recall is not None else None
            ),
            "recall_lift_vs_random_pp": (
                round(recall - random_recall, 3)
                if recall is not None and random_recall is not None else None
            ),
            "avg_best_mfe_pct": (
                round(mean(float(row["best_mfe_pct"]) for row in mover_rows), 4)
                if mover_rows else None
            ),
            "avg_first_hit_session": (
                round(mean(
                    float(row["first_hit_session"])
                    for row in mover_rows
                    if row.get("first_hit_session") is not None
                ), 3)
                if any(row.get("first_hit_session") is not None for row in mover_rows)
                else None
            ),
        }

    mover_ranks = [
        int(row["rank"])
        for row in flattened
        if row.get("best_mover_5d")
    ]
    mover_ranks.sort()
    def pct(values: list[int], q: float) -> float | None:
        if not values:
            return None
        pos = (len(values) - 1) * q
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return float(values[lo])
        return float(values[lo]) * (hi - pos) + float(values[hi]) * (pos - lo)

    out["mover_rank_distribution"] = {
        "sample": len(mover_ranks),
        "median_rank": round(float(median(mover_ranks)), 2) if mover_ranks else None,
        "p75_rank": round(float(pct(mover_ranks, 0.75)), 2) if mover_ranks else None,
        "p90_rank": round(float(pct(mover_ranks, 0.90)), 2) if mover_ranks else None,
        "movers_below_180": sum(rank > CAPACITY for rank in mover_ranks),
        "movers_inside_180": sum(rank <= CAPACITY for rank in mover_ranks),
    }
    return out


def _group_by_date(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["date"])].append(row)
    return grouped


def _current_rank(rows: list[dict]) -> dict[str, list[dict]]:
    return {
        day: _rank_rows_by_score(day_rows, "current_static_priority")
        for day, day_rows in _group_by_date(rows).items()
    }


def _challenger_rank(rows: list[dict], features: list[str]) -> dict[str, list[dict]]:
    return {
        day: _rank_rows_challenger(day_rows, features)
        for day, day_rows in _group_by_date(rows).items()
    }


def _universe_summary(rows: list[dict]) -> dict:
    grouped = _group_by_date(rows)
    sizes = [len(v) for v in grouped.values()]
    movers = [row for row in rows if row.get("best_mover_5d")]
    core_known = [row for row in movers if row.get("current_core_known")]
    return {
        "rows": len(rows),
        "dates": len(grouped),
        "avg_names_per_date": round(mean(sizes), 2) if sizes else None,
        "median_names_per_date": round(float(median(sizes)), 2) if sizes else None,
        "min_names_per_date": min(sizes) if sizes else None,
        "max_names_per_date": max(sizes) if sizes else None,
        "movers_5d": len(movers),
        "mover_rate_pct": round(100.0 * len(movers) / len(rows), 3) if rows else None,
        "movers_with_current_core": len(core_known),
        "movers_without_current_core": len(movers) - len(core_known),
        "full_scanner_persisted_before_cap": True,
        "external_market_movers_outside_scanner_universe_identifiable": False,
        "interpretation": (
            "Coverage metrics are conditional on the frozen full Scanner universe. "
            "The dataset cannot prove recall for symbols never present in TMAD Scanner."
        ),
    }


def _feature_test(rows: list[dict], feature: str) -> dict:
    available = [
        row for row in rows
        if _num((row.get("feature_values") or {}).get(feature)) is not None
    ]
    coverage = 100.0 * len(available) / len(rows) if rows else 0.0
    ranked = _challenger_rank(available, [feature])
    metrics = _aggregate_ranked(ranked)
    return {
        "coverage_pct": round(coverage, 3),
        "top_50": metrics.get("top_50") or {},
        "top_180": metrics.get("top_180") or {},
    }


def _select_stable_features(train: list[dict], valid: list[dict]) -> list[dict]:
    selected = []
    for feature in FEATURES:
        tr = _feature_test(train, feature)
        va = _feature_test(valid, feature)
        tr180 = tr.get("top_180") or {}
        va180 = va.get("top_180") or {}
        va50 = va.get("top_50") or {}
        tr_lift = _num(tr180.get("precision_lift_vs_universe_pp"))
        va_lift = _num(va180.get("precision_lift_vs_universe_pp"))
        va50_lift = _num(va50.get("precision_lift_vs_universe_pp"))
        stable = bool(
            tr["coverage_pct"] >= MIN_FEATURE_COVERAGE_PCT
            and va["coverage_pct"] >= MIN_FEATURE_COVERAGE_PCT
            and tr_lift is not None and tr_lift >= MIN_STABLE_LIFT_PP
            and va_lift is not None and va_lift >= MIN_STABLE_LIFT_PP
            and va50_lift is not None and va50_lift >= 0.0
        )
        selected.append({
            "feature": feature,
            "stable": stable,
            "train": tr,
            "validation": va,
        })
    stable = [row for row in selected if row["stable"]]
    stable.sort(
        key=lambda row: (
            -float(((row["validation"]["top_180"] or {}).get("precision_lift_vs_universe_pp") or 0.0)),
            -float(((row["validation"]["top_50"] or {}).get("precision_lift_vs_universe_pp") or 0.0)),
            row["feature"],
        )
    )
    return stable[:6]


def _lane(
    canonical: dict[tuple[str, str], dict],
    trusted_price_series: dict[str, list[dict]],
    *,
    lane_label: str,
) -> dict:
    rows = _build_rows(canonical, trusted_price_series)
    train_dates, valid_dates, test_dates = _split_dates(rows)
    train = [row for row in rows if row["date"] in train_dates]
    valid = [row for row in rows if row["date"] in valid_dates]
    test = [row for row in rows if row["date"] in test_dates]

    stable = _select_stable_features(train, valid) if valid and test else []
    selected_names = [row["feature"] for row in stable]

    current_rank_all = _current_rank(rows) if rows else {}
    current_rank_test = _current_rank(test) if test else {}
    challenger_rank_test = (
        _challenger_rank(test, selected_names)
        if test and selected_names else {}
    )
    current_all = _aggregate_ranked(current_rank_all) if current_rank_all else {}
    current_test = _aggregate_ranked(current_rank_test) if current_rank_test else {}
    challenger_test = (
        _aggregate_ranked(challenger_rank_test)
        if challenger_rank_test else {}
    )
    current_directional_test = (
        _aggregate_directional(current_rank_test) if current_rank_test else {}
    )
    challenger_directional_test = (
        _aggregate_directional(challenger_rank_test)
        if challenger_rank_test else {}
    )
    replacement_top180 = (
        _replacement_analysis(current_rank_test, challenger_rank_test, 180)
        if current_rank_test and challenger_rank_test else {}
    )

    current_180 = current_test.get("top_180") or {}
    challenger_180 = challenger_test.get("top_180") or {}
    current_50 = current_test.get("top_50") or {}
    challenger_50 = challenger_test.get("top_50") or {}

    cur_recall = _num(current_180.get("recall_pct"))
    new_recall = _num(challenger_180.get("recall_pct"))
    cur_precision = _num(current_180.get("precision_pct"))
    new_precision = _num(challenger_180.get("precision_pct"))
    cur50_precision = _num(current_50.get("precision_pct"))
    new50_precision = _num(challenger_50.get("precision_pct"))

    recall_followup = bool(
        selected_names
        and cur_recall is not None and new_recall is not None
        and cur_precision is not None and new_precision is not None
        and cur50_precision is not None and new50_precision is not None
        and new_recall > cur_recall
        and new_precision > cur_precision
        and new50_precision >= cur50_precision
    )

    current_dir180 = current_directional_test.get("top_180") or {}
    challenger_dir180 = challenger_directional_test.get("top_180") or {}
    cur_core_coverage = _num(current_dir180.get("core_coverage_pct"))
    new_core_coverage = _num(challenger_dir180.get("core_coverage_pct"))
    cur_dir_accuracy = _num(
        current_dir180.get("direction_accuracy_on_best_movers_pct")
    )
    new_dir_accuracy = _num(
        challenger_dir180.get("direction_accuracy_on_best_movers_pct")
    )
    cur_dir_return = _num(
        current_dir180.get("avg_horizon_close_return_pct")
    )
    new_dir_return = _num(
        challenger_dir180.get("avg_horizon_close_return_pct")
    )
    cur_dir_pf = _num(current_dir180.get("horizon_close_profit_factor"))
    new_dir_pf = _num(challenger_dir180.get("horizon_close_profit_factor"))
    cur_dir_mae = _num(current_dir180.get("avg_mae_pct"))
    new_dir_mae = _num(challenger_dir180.get("avg_mae_pct"))

    directional_guard = {
        "core_coverage_not_worse_by_more_than_5pp": bool(
            cur_core_coverage is None
            or (
                new_core_coverage is not None
                and new_core_coverage >= cur_core_coverage - 5.0
            )
        ),
        "direction_accuracy_not_worse": _not_worse(
            new_dir_accuracy, cur_dir_accuracy
        ),
        "close_return_not_worse": _not_worse(
            new_dir_return, cur_dir_return
        ),
        "profit_factor_not_worse": _not_worse(
            new_dir_pf, cur_dir_pf
        ),
        "mae_not_worse_by_more_than_1pp": _not_worse(
            new_dir_mae, cur_dir_mae,
            tolerance=1.0,
            higher_is_better=False,
        ),
    }
    directional_guard["pass"] = bool(
        directional_guard["core_coverage_not_worse_by_more_than_5pp"]
        and directional_guard["direction_accuracy_not_worse"]
        and directional_guard["close_return_not_worse"]
        and directional_guard["profit_factor_not_worse"]
    )
    followup = bool(recall_followup and directional_guard["pass"])

    error_flags: list[dict] = []
    if recall_followup and not directional_guard["pass"]:
        error_flags.append({
            "code": "VOLATILITY_ONLY_DIRECTIONAL_REGRESSION",
            "severity": "WARN",
            "detail": (
                "Scanner mover recall/precision improves, but the same Top-180 "
                "fails the point-in-time CORE-direction economic guard. Do not "
                "promote a volatility-only enrichment ranking."
            ),
        })
    if not directional_guard["mae_not_worse_by_more_than_1pp"]:
        error_flags.append({
            "code": "SCANNER_CHALLENGER_MAE_INCREASE",
            "severity": "INFO",
            "detail": (
                "Challenger Top-180 raises average adverse excursion by >1pp "
                "under current CORE direction; inspect risk before shadow promotion."
            ),
        })

    universe = _universe_summary(rows)
    current180_all = current_all.get("top_180") or {}
    random_recall = _num(current180_all.get("random_expected_recall_pct"))
    observed_recall = _num(current180_all.get("recall_pct"))
    if observed_recall is None:
        diagnosis = "INSUFFICIENT"
    elif random_recall is not None and observed_recall <= random_recall + 2.0:
        diagnosis = "RANKING_WEAK_NEAR_RANDOM"
    elif observed_recall < 50.0:
        diagnosis = "RANKING_RECALL_LIMITED"
    else:
        diagnosis = "RANKING_HAS_SIGNAL"

    return {
        "mode": MODE,
        "lane": str(lane_label).upper(),
        "status": "EVALUABLE" if rows and test else "INSUFFICIENT",
        "live_mutation": False,
        "promotion_allowed": False,
        "economic_truth": {
            "threshold_pct": TARGET_PCT,
            "horizon_sessions": HORIZON_SESSIONS,
        },
        "universe": universe,
        "split": {
            "train_dates": len(train_dates),
            "validation_dates": len(valid_dates),
            "test_dates": len(test_dates),
            "train_rows": len(train),
            "validation_rows": len(valid),
            "test_rows": len(test),
        },
        "current_static_priority_proxy": {
            "scope": "STATIC_COMPONENT_OF_SELECTION_BASE",
            "excludes_dynamic_overlays": [
                "same_day_change_map",
                "mandatory_open_recent",
                "continuity",
                "relationship_seed_boost",
                "negative_cache",
            ],
            "all_dates": current_all,
            "final_test": current_test,
            "directional_final_test": current_directional_test,
        },
        "challenger": {
            "method": "equal_weight_cross_sectional_percentiles_of_train_valid_stable_features",
            "feature_selection_rule": {
                "min_coverage_pct": MIN_FEATURE_COVERAGE_PCT,
                "train_top180_precision_lift_min_pp": MIN_STABLE_LIFT_PP,
                "validation_top180_precision_lift_min_pp": MIN_STABLE_LIFT_PP,
                "validation_top50_precision_lift_min_pp": 0.0,
                "weights_optimized": False,
            },
            "stable_features": stable,
            "selected_feature_names": selected_names,
            "final_test": challenger_test,
            "directional_final_test": challenger_directional_test,
            "replacement_analysis_top180": replacement_top180,
            "historical_recall_followup_candidate": recall_followup,
            "directional_guard": directional_guard,
            "historical_followup_candidate": followup,
        },
        "error_flags": error_flags,
        "diagnosis": {
            "within_scanner_universe": diagnosis,
            "external_universe_recall": "NOT_IDENTIFIABLE",
            "full_scanner_is_persisted_before_180_cap": True,
            "meaning": (
                "If Top-180 recall is weak while the full persisted Scanner universe "
                "contains the movers, the bottleneck is enrichment priority/ranking, "
                "not full-Scanner ingestion."
            ),
        },
        "policy": {
            "scanner_live_selection_unchanged": True,
            "scanner_cap_unchanged": True,
            "core_unchanged": True,
            "research_unchanged": True,
            "gex_unchanged": True,
            "timing_unchanged": True,
            "fusion_unchanged": True,
            "lifecycle_unchanged": True,
            "future_prices_labels_only": True,
        },
    }


def build_scanner_recall_ranking_calibration(
    canonical: dict[tuple[str, str], dict],
    trusted_price_series: dict[str, list[dict]],
    *,
    lane_label: str = "ROLLING_LATEST",
) -> dict:
    return _lane(canonical, trusted_price_series, lane_label=lane_label)


def combine_scanner_recall_ranking_calibrations(
    rolling: dict,
    fixed: dict,
) -> dict:
    def follow(lane: dict) -> bool:
        return bool((lane.get("challenger") or {}).get("historical_followup_candidate"))

    rolling_follow = follow(rolling)
    fixed_follow = follow(fixed)
    rolling_recall_follow = bool(
        (rolling.get("challenger") or {}).get("historical_recall_followup_candidate")
    )
    fixed_recall_follow = bool(
        (fixed.get("challenger") or {}).get("historical_recall_followup_candidate")
    )
    rolling_directional = bool(
        ((rolling.get("challenger") or {}).get("directional_guard") or {}).get("pass")
    )
    fixed_directional = bool(
        ((fixed.get("challenger") or {}).get("directional_guard") or {}).get("pass")
    )
    r_features = set((rolling.get("challenger") or {}).get("selected_feature_names") or [])
    f_features = set((fixed.get("challenger") or {}).get("selected_feature_names") or [])
    overlap = sorted(r_features & f_features)

    return {
        "mode": "SCANNER_RECALL_RANKING_DUAL_LANE_DECISION_V1",
        "status": (
            "EVALUABLE"
            if rolling.get("status") == "EVALUABLE" and fixed.get("status") == "EVALUABLE"
            else "PARTIAL"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "rolling_recall_followup": rolling_recall_follow,
        "fixed_recall_followup": fixed_recall_follow,
        "rolling_directional_sanity": rolling_directional,
        "fixed_directional_sanity": fixed_directional,
        "rolling_followup": rolling_follow,
        "fixed_followup": fixed_follow,
        "dual_lane_followup": bool(rolling_follow and fixed_follow),
        "stable_feature_overlap": overlap,
        "next_step": (
            "PROSPECTIVE_SHADOW_SELECTION_JOURNAL"
            if rolling_follow and fixed_follow and overlap
            else (
                "REJECT_VOLATILITY_ONLY_CHALLENGER_OR_REDESIGN"
                if rolling_recall_follow and fixed_recall_follow
                and not (rolling_directional and fixed_directional)
                else "KEEP_CURRENT_SELECTION_AND_REVISIT_SCANNER_FEATURES_OR_DYNAMIC_PRIORITY"
            )
        ),
        "interpretation": (
            "This calibrates the bounded enrichment priority inside the persisted "
            "Scanner universe and now requires a point-in-time CORE-direction economic "
            "sanity check before prospective follow-up. It does not establish recall "
            "for symbols outside TMAD Scanner."
        ),
    }
