from __future__ import annotations

"""Economic waterfall calibration for TMAD.

Research-only. The economic label is deliberately simple and aligned with the
trading objective: did a Scanner candidate offer >=5% favourable excursion in
the best direction within the next <=5 trusted sessions?

All explanatory features/states are point-in-time. Future OHLC is used only to
build outcome labels and diagnostics. The module never mutates LIVE thresholds
and never auto-promotes a calibration.
"""

from collections import Counter, defaultdict
import json
import math
from statistics import mean, median
from typing import Any

from daily_reverse_alpha import _stage_path


MODE = "ECONOMIC_WATERFALL_CALIBRATION_V1"
STAGES = ("scanner", "core", "research", "gex", "timing", "fusion", "emitted")
DEFAULT_THRESHOLD_PCT = 5.0
DEFAULT_HORIZON_SESSIONS = 5

_FEATURE_TOKENS = (
    "score", "prob", "projection", "confirm", "confidence", "rank",
    "rvol", "momentum", "breadth", "gex", "flow", "tradeability",
    "quality", "consistency", "distance", "overextension", "strength",
    "change_percent", "relative_strength",
)
_EXCLUDED_KEY_TOKENS = (
    "_id", "price", "strike", "expiry", "timestamp", "observed_at",
    "created_at", "updated_at",
)


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _json_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _feature_payload(row: dict) -> dict:
    payload = _json_dict(row.get("payload_json") or row.get("features_json"))
    nested = payload.get("features")
    return nested if isinstance(nested, dict) else payload


def _day(row: dict) -> str:
    return str(
        row.get("snapshot_date")
        or row.get("signal_date")
        or row.get("observation_date")
        or row.get("date")
        or ""
    )[:10]


def _ticker(row: dict) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").upper()


def _entry_price(row: dict) -> float | None:
    for key in ("outcome_entry_price", "price", "close", "entry_price"):
        value = _f(row.get(key))
        if value is not None and value > 0:
            return float(value)
    payload = _feature_payload(row)
    for key in ("price", "close", "last", "mark"):
        value = _f(payload.get(key))
        if value is not None and value > 0:
            return float(value)
    return None


def _future_same_segment(
    rows: list[dict],
    signal_date: str,
    horizon_sessions: int,
) -> list[dict]:
    ordered = sorted(rows, key=lambda row: str(row.get("trade_date") or "")[:10])
    if not ordered:
        return []
    has_segments = any("reverse_segment" in row for row in ordered)
    segment = None
    if has_segments:
        anchor = None
        for row in ordered:
            day = str(row.get("trade_date") or "")[:10]
            if day <= signal_date:
                anchor = row
            else:
                break
        if anchor is None:
            return []
        segment = anchor.get("reverse_segment")
    future = [
        row for row in ordered
        if str(row.get("trade_date") or "")[:10] > signal_date
        and (not has_segments or row.get("reverse_segment") == segment)
    ]
    return future[: max(1, int(horizon_sessions))]


def _direction_path(
    entry: float,
    direction: str,
    future: list[dict],
    threshold_pct: float,
) -> dict:
    direction = str(direction).upper()
    mfe = 0.0
    mae = 0.0
    first_hit = None
    close_return = None
    used = 0
    for session, row in enumerate(future, start=1):
        close = _f(row.get("close"))
        high = _f(row.get("high"), close)
        low = _f(row.get("low"), close)
        if close is None or high is None or low is None:
            continue
        if min(close, high, low) <= 0:
            continue
        used += 1
        if direction == "LONG":
            favorable = 100.0 * (high / entry - 1.0)
            adverse = 100.0 * (1.0 - low / entry)
            close_return = 100.0 * (close / entry - 1.0)
        else:
            favorable = 100.0 * (1.0 - low / entry)
            adverse = 100.0 * (high / entry - 1.0)
            close_return = 100.0 * (1.0 - close / entry)
        mfe = max(mfe, favorable)
        mae = max(mae, adverse)
        if first_hit is None and favorable >= threshold_pct:
            first_hit = session
    return {
        "sessions": used,
        "mfe_pct": round(max(0.0, mfe), 6),
        "mae_pct": round(max(0.0, mae), 6),
        "first_hit_session": first_hit,
        "horizon_close_return_pct": round(float(close_return), 6) if close_return is not None else None,
    }


def _best_direction(
    entry: float,
    future: list[dict],
    threshold_pct: float,
) -> tuple[str, dict]:
    long_path = _direction_path(entry, "LONG", future, threshold_pct)
    short_path = _direction_path(entry, "SHORT", future, threshold_pct)
    long_key = (
        float(long_path.get("mfe_pct") or 0.0),
        float(long_path.get("horizon_close_return_pct") or -1e12),
    )
    short_key = (
        float(short_path.get("mfe_pct") or 0.0),
        float(short_path.get("horizon_close_return_pct") or -1e12),
    )
    if short_key > long_key:
        return "SHORT", short_path
    return "LONG", long_path


def _numeric_signal_features(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        layer = str(row.get("layer") or "signal").lower()
        sources: list[tuple[str, dict]] = [("", row)]
        for key in ("payload_json", "features_json", "details_json", "metadata_json", "diagnostics_json"):
            payload = _json_dict(row.get(key))
            if payload:
                sources.append((key.replace("_json", ""), payload))
        for namespace, source in sources:
            for key, value in source.items():
                name = str(key)
                low = name.lower()
                if any(token in low for token in _EXCLUDED_KEY_TOKENS):
                    continue
                if not any(token in low for token in _FEATURE_TOKENS):
                    continue
                number = _f(value)
                if number is None:
                    continue
                prefix = f"{layer}.{namespace}." if namespace else f"{layer}."
                out[prefix + name] = float(number)
    return out


def _candidate_features(
    canonical_row: dict,
    signal_rows: list[dict],
    *,
    date_value: str,
    ticker: str,
    direction: str,
    ranks: dict | None,
) -> dict[str, float]:
    out: dict[str, float] = {}
    payload = _feature_payload(canonical_row)
    for key, value in payload.items():
        low = str(key).lower()
        if any(token in low for token in _EXCLUDED_KEY_TOKENS):
            continue
        if not any(token in low for token in _FEATURE_TOKENS):
            continue
        number = _f(value)
        if number is not None:
            out["scanner." + str(key)] = float(number)

    for key in ("core_score", "core_probability_pct"):
        number = _f(canonical_row.get(key))
        if number is not None:
            out["core_snapshot." + key] = float(number)

    if ranks:
        direction_lower = direction.lower()
        for name in ("core", "momentum", "atr", "flow"):
            number = _f(ranks.get((date_value, direction_lower, ticker, name)))
            if number is not None:
                out["scanner_rank." + name] = float(number)

    out.update(_numeric_signal_features(signal_rows))
    return out


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return round(gains / losses, 4)


def _population_metrics(rows: list[dict], *, total_movers: int | None = None) -> dict:
    movers = [row for row in rows if row.get("mover_5d")]
    mfes = [float(row["mfe_pct"]) for row in rows if _f(row.get("mfe_pct")) is not None]
    maes = [float(row["mae_pct"]) for row in rows if _f(row.get("mae_pct")) is not None]
    closes = [
        float(row["horizon_close_return_pct"])
        for row in rows if _f(row.get("horizon_close_return_pct")) is not None
    ]
    hit_sessions = [
        int(row["first_hit_session"])
        for row in movers if row.get("first_hit_session") is not None
    ]
    denominator = int(total_movers if total_movers is not None else len(movers))
    return {
        "sample": len(rows),
        "dates": len({str(row.get("date") or "") for row in rows}),
        "movers": len(movers),
        "mover_precision_pct": round(100.0 * len(movers) / len(rows), 3) if rows else None,
        "mover_recall_pct": round(100.0 * len(movers) / denominator, 3) if denominator else None,
        "avg_mfe_pct": round(mean(mfes), 4) if mfes else None,
        "median_mfe_pct": round(median(mfes), 4) if mfes else None,
        "avg_mae_pct": round(mean(maes), 4) if maes else None,
        "avg_horizon_close_return_pct": round(mean(closes), 4) if closes else None,
        "horizon_close_profit_factor": _profit_factor(closes),
        "avg_first_hit_session": round(mean(hit_sessions), 3) if hit_sessions else None,
    }


def _first_failure(row: dict) -> str:
    if row.get("core_direction_known") and not row.get("core_direction_correct"):
        return "WRONG_DIRECTION"
    raw = row.get("raw_stage_pass") or {}
    for stage in STAGES[1:]:
        if not bool(raw.get(stage)):
            return stage.upper() + "_BLOCK"
    return "SURVIVED_ALL"


def _waterfall(rows: list[dict]) -> tuple[dict, dict]:
    total_movers = sum(bool(row.get("mover_5d")) for row in rows)
    by_stage: dict[str, dict] = {}
    previous_metrics = None
    previous_passed: list[dict] = []
    for stage in STAGES:
        passed = [row for row in rows if bool((row.get("cumulative_stage_pass") or {}).get(stage))]
        metrics = _population_metrics(passed, total_movers=total_movers)
        if previous_metrics is None:
            metrics["precision_lift_vs_previous_pp"] = None
            metrics["movers_lost_at_stage"] = 0
            metrics["candidates_removed_at_stage"] = 0
        else:
            prev_precision = _f(previous_metrics.get("mover_precision_pct"))
            this_precision = _f(metrics.get("mover_precision_pct"))
            metrics["precision_lift_vs_previous_pp"] = (
                round(float(this_precision) - float(prev_precision), 3)
                if prev_precision is not None and this_precision is not None else None
            )
            metrics["movers_lost_at_stage"] = (
                int(previous_metrics.get("movers") or 0) - int(metrics.get("movers") or 0)
            )
            metrics["candidates_removed_at_stage"] = len(previous_passed) - len(passed)
        by_stage[stage] = metrics
        previous_metrics = metrics
        previous_passed = passed

    mover_failures = Counter(
        _first_failure(row) for row in rows
        if row.get("mover_5d") and _first_failure(row) != "SURVIVED_ALL"
    )
    bottleneck = None
    candidates = [
        (stage, int(metrics.get("movers_lost_at_stage") or 0))
        for stage, metrics in by_stage.items()
        if stage != "scanner"
    ]
    if candidates:
        stage, lost = max(candidates, key=lambda item: (item[1], -STAGES.index(item[0])))
        if lost > 0:
            bottleneck = stage
    return by_stage, {
        "mover_first_failure_reasons": dict(mover_failures),
        "largest_mover_loss_stage": bottleneck,
        "largest_mover_loss_count": (
            int((by_stage.get(bottleneck) or {}).get("movers_lost_at_stage") or 0)
            if bottleneck else 0
        ),
    }


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    weight = pos - lo
    return float(ordered[lo]) * (1.0 - weight) + float(ordered[hi]) * weight


def _threshold_metrics(
    rows: list[dict],
    feature: str,
    operator: str,
    threshold: float,
) -> dict:
    available = [
        row for row in rows
        if _f((row.get("features") or {}).get(feature)) is not None
    ]
    if operator == ">=":
        selected = [
            row for row in available
            if float((row.get("features") or {})[feature]) >= threshold
        ]
    else:
        selected = [
            row for row in available
            if float((row.get("features") or {})[feature]) <= threshold
        ]
    baseline_movers = sum(bool(row.get("mover_5d")) for row in available)
    selected_metrics = _population_metrics(selected, total_movers=baseline_movers)
    baseline_precision = (
        100.0 * baseline_movers / len(available) if available else None
    )
    precision = _f(selected_metrics.get("mover_precision_pct"))
    selected_metrics.update({
        "available_sample": len(available),
        "coverage_pct": round(100.0 * len(selected) / len(available), 3) if available else None,
        "baseline_mover_precision_pct": round(baseline_precision, 3) if baseline_precision is not None else None,
        "precision_lift_pp": (
            round(float(precision) - float(baseline_precision), 3)
            if precision is not None and baseline_precision is not None else None
        ),
    })
    return selected_metrics


def _feature_calibration(rows: list[dict], *, max_features: int = 40) -> dict:
    dates = sorted({str(row.get("date") or "") for row in rows if row.get("date")})
    split = max(1, len(dates) // 2)
    early_dates = set(dates[:split])
    late_dates = set(dates[split:])
    early = [row for row in rows if row.get("date") in early_dates]
    late = [row for row in rows if row.get("date") in late_dates]

    feature_counts: Counter[str] = Counter()
    for row in rows:
        feature_counts.update((row.get("features") or {}).keys())
    min_feature_sample = max(20, int(math.ceil(len(rows) * 0.20))) if rows else 20
    features = [
        name for name, count in feature_counts.items()
        if count >= min_feature_sample
    ]
    features.sort(key=lambda name: (-int(feature_counts[name]), name))
    features = features[: max(1, int(max_features))]

    profiles: dict[str, dict] = {}
    stable_feature_names: list[str] = []
    for feature in features:
        train_values = [
            float((row.get("features") or {})[feature])
            for row in early
            if _f((row.get("features") or {}).get(feature)) is not None
        ]
        if len(train_values) < 10:
            continue
        cuts = []
        for q in (0.20, 0.40, 0.60, 0.80):
            value = round(_quantile(train_values, q), 6)
            if value not in cuts:
                cuts.append(value)

        candidates: list[dict] = []
        stable: list[dict] = []
        for threshold in cuts:
            for operator in (">=", "<="):
                early_metrics = _threshold_metrics(early, feature, operator, threshold)
                late_metrics = _threshold_metrics(late, feature, operator, threshold)
                candidate = {
                    "operator": operator,
                    "threshold": threshold,
                    "early": early_metrics,
                    "late": late_metrics,
                }
                candidates.append(candidate)
                early_lift = _f(early_metrics.get("precision_lift_pp"))
                late_lift = _f(late_metrics.get("precision_lift_pp"))
                late_recall = _f(late_metrics.get("mover_recall_pct"))
                early_n = int(early_metrics.get("sample") or 0)
                late_n = int(late_metrics.get("sample") or 0)
                if (
                    early_n >= 10
                    and late_n >= 10
                    and early_lift is not None and early_lift > 0
                    and late_lift is not None and late_lift > 0
                    and late_recall is not None and late_recall >= 25.0
                ):
                    stable.append(candidate)

        stable.sort(
            key=lambda item: (
                -float((item["late"] or {}).get("coverage_pct") or 0.0),
                -min(
                    float((item["early"] or {}).get("precision_lift_pp") or 0.0),
                    float((item["late"] or {}).get("precision_lift_pp") or 0.0),
                ),
                str(item["operator"]),
                float(item["threshold"]),
            )
        )
        if stable:
            stable_feature_names.append(feature)
        profiles[feature] = {
            "sample": int(feature_counts[feature]),
            "train_cutpoints_from_early_half": cuts,
            "threshold_candidates": candidates,
            "stable_early_late_candidates": stable[:6],
            "has_stable_candidate": bool(stable),
        }

    by_module: dict[str, list[str]] = defaultdict(list)
    for feature in stable_feature_names:
        prefix = feature.split(".", 1)[0]
        by_module[prefix].append(feature)

    return {
        "dates": len(dates),
        "early_dates": len(early_dates),
        "late_dates": len(late_dates),
        "min_feature_sample": min_feature_sample,
        "features_evaluated": len(profiles),
        "stable_feature_count": len(stable_feature_names),
        "stable_feature_names": stable_feature_names,
        "stable_features_by_module": {
            key: sorted(value) for key, value in sorted(by_module.items())
        },
        "profiles": profiles,
        "methodology": {
            "cutpoints_fit_on_early_half_only": True,
            "late_half_used_as_temporal_falsification": True,
            "minimum_late_mover_recall_pct": 25.0,
            "no_threshold_auto_selected": True,
        },
    }


def _direction_quality(rows: list[dict]) -> dict:
    known = [row for row in rows if row.get("core_direction_known")]
    movers = [row for row in known if row.get("mover_5d")]
    matched = [row for row in known if row.get("core_direction_correct")]
    mover_matched = [row for row in movers if row.get("core_direction_correct")]
    return {
        "known_core_direction": len(known),
        "all_direction_match_pct": (
            round(100.0 * len(matched) / len(known), 3) if known else None
        ),
        "mover_direction_match_pct": (
            round(100.0 * len(mover_matched) / len(movers), 3) if movers else None
        ),
        "movers_with_wrong_core_direction": len(movers) - len(mover_matched),
        "movers_with_correct_core_direction": len(mover_matched),
        "avg_mfe_correct_direction_pct": (
            round(mean(float(row["mfe_pct"]) for row in mover_matched), 4)
            if mover_matched else None
        ),
        "avg_mfe_wrong_direction_pct": (
            round(mean(float(row["mfe_pct"]) for row in movers if not row.get("core_direction_correct")), 4)
            if any(not row.get("core_direction_correct") for row in movers) else None
        ),
    }


def _exit_capture_calibration(
    trades: list[dict],
    trusted_price_series: dict[str, list[dict]],
    *,
    threshold_pct: float,
    horizon_sessions: int,
) -> dict:
    evaluated: list[dict] = []
    for trade in trades:
        ticker = _ticker(trade)
        direction = str(trade.get("direction") or "").upper()
        signal_date = str(trade.get("signal_date") or trade.get("entry_date") or "")[:10]
        entry = _entry_price(trade)
        realized = _f(
            trade.get("underlying_return_pct"),
            _f(trade.get("exit_return_pct"), _f(trade.get("actual_return_pct"))),
        )
        if (
            not ticker
            or direction not in {"LONG", "SHORT"}
            or not signal_date
            or entry is None
            or entry <= 0
            or realized is None
        ):
            continue
        future = _future_same_segment(
            trusted_price_series.get(ticker, []),
            signal_date,
            horizon_sessions,
        )
        if not future:
            continue
        path = _direction_path(float(entry), direction, future, threshold_pct)
        mfe = float(path.get("mfe_pct") or 0.0)
        capture = (100.0 * float(realized) / mfe) if mfe > 0 else None
        evaluated.append({
            "ticker": ticker,
            "direction": direction,
            "signal_date": signal_date,
            "exit_reason": str(trade.get("exit_reason") or "UNKNOWN"),
            "realized_return_pct": float(realized),
            "mfe_pct": mfe,
            "mae_pct": float(path.get("mae_pct") or 0.0),
            "first_hit_session": path.get("first_hit_session"),
            "mover_5d": bool(mfe >= float(threshold_pct)),
            "capture_ratio_pct": round(capture, 4) if capture is not None else None,
        })

    def metrics(rows: list[dict]) -> dict:
        realized = [float(row["realized_return_pct"]) for row in rows]
        mfes = [float(row["mfe_pct"]) for row in rows]
        captures = [
            float(row["capture_ratio_pct"])
            for row in rows if _f(row.get("capture_ratio_pct")) is not None
        ]
        movers = [row for row in rows if row.get("mover_5d")]
        under_monetized = [
            row for row in movers
            if float(row.get("realized_return_pct") or 0.0)
            < 0.50 * float(row.get("mfe_pct") or 0.0)
        ]
        return {
            "sample": len(rows),
            "movers_5d": len(movers),
            "avg_realized_return_pct": round(mean(realized), 4) if realized else None,
            "realized_profit_factor": _profit_factor(realized),
            "avg_mfe_pct": round(mean(mfes), 4) if mfes else None,
            "avg_capture_ratio_pct": round(mean(captures), 3) if captures else None,
            "median_capture_ratio_pct": round(median(captures), 3) if captures else None,
            "under_monetized_movers": len(under_monetized),
            "under_monetized_movers_pct": (
                round(100.0 * len(under_monetized) / len(movers), 3)
                if movers else None
            ),
            "avg_uncaptured_mfe_pp_on_movers": (
                round(mean(
                    float(row.get("mfe_pct") or 0.0)
                    - float(row.get("realized_return_pct") or 0.0)
                    for row in movers
                ), 4)
                if movers else None
            ),
        }

    by_reason: dict[str, dict] = {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in evaluated:
        groups[str(row.get("exit_reason") or "UNKNOWN")].append(row)
    for reason, reason_rows in sorted(groups.items()):
        by_reason[reason] = metrics(reason_rows)

    return {
        "status": "EVALUABLE" if evaluated else "NO_EVALUABLE_EXITS",
        "definition": "realized underlying return divided by max favourable excursion available within <=5 trusted sessions",
        "overall": metrics(evaluated),
        "by_exit_reason": by_reason,
        "policy": {
            "diagnostic_only": True,
            "no_exit_rule_auto_selected": True,
            "hard_and_soft_exit_reasons_reported_separately": True,
        },
    }


def build_economic_waterfall_calibration(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    alerts: set[tuple[str, str, str]] | None = None,
    ranks: dict | None = None,
    trades: list[dict] | None = None,
    mover_threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    lane_label: str = "ROLLING_LATEST",
    max_features: int = 40,
) -> dict:
    alerts = alerts or set()
    rows: list[dict] = []
    no_entry = no_path = 0

    for (date_value, ticker_value), source in sorted(canonical.items()):
        date_key = str(date_value)[:10]
        ticker = str(ticker_value).upper()
        entry = _entry_price(source)
        if entry is None or entry <= 0:
            no_entry += 1
            continue
        future = _future_same_segment(
            trusted_price_series.get(ticker, []),
            date_key,
            horizon_sessions,
        )
        if not future:
            no_path += 1
            continue

        direction, path_stats = _best_direction(
            float(entry), future, float(mover_threshold_pct)
        )
        signal_rows = list(
            signal_index.get((date_key, ticker, direction.lower()), []) or []
        )
        stage_path = _stage_path(signal_rows, direction.lower())
        raw_pass = {
            "scanner": True,
            "core": bool(stage_path.get("core")),
            "research": bool(stage_path.get("research")),
            "gex": bool(stage_path.get("gex")),
            "timing": bool(stage_path.get("timing")),
            "fusion": bool(stage_path.get("fusion")),
            "emitted": (date_key, ticker, direction.lower()) in alerts,
        }
        cumulative: dict[str, bool] = {}
        running = True
        for stage in STAGES:
            running = bool(running and raw_pass[stage])
            cumulative[stage] = running

        core_direction = str(source.get("core_direction") or "").upper()
        core_known = core_direction in {"LONG", "SHORT"}
        features = _candidate_features(
            source,
            signal_rows,
            date_value=date_key,
            ticker=ticker,
            direction=direction,
            ranks=ranks,
        )
        rows.append({
            "date": date_key,
            "ticker": ticker,
            "direction": direction,
            "entry_price": round(float(entry), 6),
            "mfe_pct": path_stats.get("mfe_pct"),
            "mae_pct": path_stats.get("mae_pct"),
            "first_hit_session": path_stats.get("first_hit_session"),
            "horizon_close_return_pct": path_stats.get("horizon_close_return_pct"),
            "mover_5d": bool(
                float(path_stats.get("mfe_pct") or 0.0) >= float(mover_threshold_pct)
            ),
            "core_direction": core_direction if core_known else None,
            "core_direction_known": core_known,
            "core_direction_correct": bool(core_known and core_direction == direction),
            "raw_stage_pass": raw_pass,
            "cumulative_stage_pass": cumulative,
            "features": features,
        })

    total_movers = sum(bool(row.get("mover_5d")) for row in rows)
    waterfall, attrition = _waterfall(rows)
    hit_distribution = Counter(
        int(row["first_hit_session"])
        for row in rows
        if row.get("mover_5d") and row.get("first_hit_session") is not None
    )
    feature_calibration = _feature_calibration(rows, max_features=max_features)

    return {
        "mode": MODE,
        "lane": str(lane_label or "ROLLING_LATEST").upper(),
        "status": "EVALUABLE" if rows else "NO_EVALUABLE_CANDIDATES",
        "live_mutation": False,
        "promotion_allowed": False,
        "economic_label": {
            "definition": f"max favourable excursion >= {float(mover_threshold_pct):g}% within <= {int(horizon_sessions)} trusted sessions",
            "threshold_pct": float(mover_threshold_pct),
            "horizon_sessions": int(horizon_sessions),
            "uses_close_at_day_5_as_primary_label": False,
            "future_prices_are_labels_only": True,
        },
        "candidate_population": {
            "canonical_candidates": len(canonical),
            "evaluable_candidates": len(rows),
            "missing_entry_price": no_entry,
            "missing_future_path": no_path,
            "movers": total_movers,
            "mover_rate_pct": round(100.0 * total_movers / len(rows), 3) if rows else None,
        },
        "time_to_threshold_sessions": {
            str(session): int(hit_distribution.get(session, 0))
            for session in range(1, int(horizon_sessions) + 1)
        },
        "direction_quality": _direction_quality(rows),
        "waterfall": waterfall,
        "attrition": attrition,
        "feature_calibration": feature_calibration,
        "exit_capture_calibration": _exit_capture_calibration(
            list(trades or []),
            trusted_price_series,
            threshold_pct=float(mover_threshold_pct),
            horizon_sessions=int(horizon_sessions),
        ),
        "policy": {
            "research_only": True,
            "no_live_threshold_change": True,
            "no_hindsight_rule_auto_promotion": True,
            "threshold_frontiers_are_diagnostics_not_recommendations": True,
            "prospective_or_fixed_benchmark_confirmation_required": True,
        },
    }


def combine_economic_waterfall_calibrations(
    rolling: dict,
    fixed: dict,
) -> dict:
    rolling_ok = str(rolling.get("status") or "").upper() == "EVALUABLE"
    fixed_ok = str(fixed.get("status") or "").upper() == "EVALUABLE"
    rolling_bottleneck = (rolling.get("attrition") or {}).get("largest_mover_loss_stage")
    fixed_bottleneck = (fixed.get("attrition") or {}).get("largest_mover_loss_stage")
    rolling_features = set(
        ((rolling.get("feature_calibration") or {}).get("stable_feature_names") or [])
    )
    fixed_features = set(
        ((fixed.get("feature_calibration") or {}).get("stable_feature_names") or [])
    )
    shared = sorted(rolling_features & fixed_features)
    return {
        "mode": "ECONOMIC_WATERFALL_DUAL_LANE_REVIEW_V1",
        "status": "EVALUABLE" if rolling_ok and fixed_ok else "PARTIAL",
        "live_mutation": False,
        "promotion_allowed": False,
        "rolling_bottleneck": rolling_bottleneck,
        "fixed_benchmark_bottleneck": fixed_bottleneck,
        "same_largest_mover_loss_stage": bool(
            rolling_bottleneck
            and fixed_bottleneck
            and rolling_bottleneck == fixed_bottleneck
        ),
        "shared_stable_feature_names": shared,
        "shared_stable_feature_count": len(shared),
        "interpretation": (
            "Use this only to prioritize which module/feature deserves the next "
            "prospective test. It is not permission to change LIVE thresholds."
        ),
    }
