from __future__ import annotations

"""Out-of-time capital eligibility gate for actionable FUSION ENTER candidates.

v2 proved that relative cross-sectional ranking can improve ordering on crowded
days, but ranking alone still fills slots even when the whole opportunity set is
weak. This v3 challenger tests abstention: a candidate is capital-eligible only
when a strict majority of previously stable, independently profitable feature
sides support it. All thresholds come from training data that resolved before
the test entry date. No LIVE mutation and no test-set threshold optimisation.
"""

from collections import defaultdict
from statistics import mean, median
from typing import Any

from return_math import directional_return_pct

from entry_quality_cross_sectional import (
    MIN_STABLE_FEATURES,
    _entry_features,
    _f,
    _fit_stable_feature_directions,
    _lift,
    _metrics,
    _resolved,
    _score_day,
    _selection_key_baseline,
    _selection_key_challenger,
)

MIN_ELIGIBILITY_FEATURES = 3
FIXED_HORIZONS = (2, 3, 5)
GEOMETRY_SENSITIVE_FEATURES = frozenset({"invalidation_distance_pct", "target_distance_pct"})


def _positive_stable_features(model: dict) -> dict[str, dict]:
    """Keep only feature sides that were profitable in both train halves."""
    out: dict[str, dict] = {}
    for feature, meta in (model.get("features") or {}).items():
        direction = int(meta.get("direction") or 0)
        first = meta.get("first_half") or {}
        second = meta.get("second_half") or {}
        if direction > 0:
            first_good = _f(first.get("high_avg_return_pct"))
            second_good = _f(second.get("high_avg_return_pct"))
            cut = max(float(first.get("cut") or 0.0), float(second.get("cut") or 0.0))
            side = "HIGH"
        elif direction < 0:
            first_good = _f(first.get("low_avg_return_pct"))
            second_good = _f(second.get("low_avg_return_pct"))
            cut = min(float(first.get("cut") or 0.0), float(second.get("cut") or 0.0))
            side = "LOW"
        else:
            continue
        if first_good is None or second_good is None:
            continue
        if float(first_good) <= 0.0 or float(second_good) <= 0.0:
            continue
        out[feature] = {
            "direction": direction,
            "favorable_side": side,
            "conservative_cut": round(cut, 6),
            "first_half_favorable_avg_return_pct": round(float(first_good), 4),
            "second_half_favorable_avg_return_pct": round(float(second_good), 4),
            "min_favorable_avg_return_pct": round(min(float(first_good), float(second_good)), 4),
        }
    return out


def _eligibility_evidence(row: dict, positive_features: dict[str, dict]) -> dict:
    values = _entry_features(row)
    support = 0
    oppose = 0
    evidence: dict[str, str] = {}
    for feature, meta in positive_features.items():
        value = values.get(feature)
        if value is None:
            continue
        cut = float(meta["conservative_cut"])
        favorable = float(value) > cut if int(meta["direction"]) > 0 else float(value) <= cut
        if favorable:
            support += 1
            evidence[feature] = "SUPPORT"
        else:
            oppose += 1
            evidence[feature] = "OPPOSE"
    observed = support + oppose
    eligible = (
        observed >= MIN_ELIGIBILITY_FEATURES
        and support > oppose
    )
    return {
        "eligible": eligible,
        "support": support,
        "oppose": oppose,
        "observed": observed,
        "net_support": support - oppose,
        "support_ratio": round(support / observed, 4) if observed else None,
        "evidence": evidence,
    }


def _metrics_plus(rows: list[dict]) -> dict:
    result = dict(_metrics(rows))
    returns = [
        float(row["boundary_return_pct"])
        for row in rows if _f(row.get("boundary_return_pct")) is not None
    ]
    result["return_sum_points"] = round(sum(returns), 4) if returns else None
    return result


def _by_direction(rows: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("direction") or "UNKNOWN").upper()].append(row)
    return {key: _metrics_plus(items) for key, items in sorted(grouped.items())}



def _fixed_horizon_return_pct(
    row: dict,
    trusted_price_series: dict[str, list[dict]],
    horizon_sessions: int,
) -> float | None:
    """Directional close return after N future sessions, independent of trade barriers."""
    ticker = str(row.get("ticker") or "").upper()
    entry_date = str(row.get("entry_date") or row.get("signal_date") or "")[:10]
    entry_price = _f(row.get("entry_price"))
    direction = str(row.get("direction") or "").upper()
    if (
        not ticker
        or not entry_date
        or entry_price is None
        or float(entry_price) <= 0.0
        or direction not in {"LONG", "SHORT"}
    ):
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
        trade_date = str(item.get("trade_date") or "")[:10]
        if trade_date and trade_date <= entry_date:
            anchor_found = True
            if has_segments:
                anchor_segment = item.get("reverse_segment")
        elif trade_date and trade_date > entry_date:
            break
    if has_segments and not anchor_found:
        return None

    future = [
        item for item in ordered
        if str(item.get("trade_date") or "")[:10] > entry_date
        and (not has_segments or item.get("reverse_segment") == anchor_segment)
    ]
    horizon = max(1, int(horizon_sessions))
    if len(future) < horizon:
        return None
    close = _f(future[horizon - 1].get("close"))
    if close is None or float(close) <= 0.0:
        return None
    value = directional_return_pct(direction, float(entry_price), float(close))
    return float(value) if value is not None else None


def _fixed_horizon_metrics(
    rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
    horizon_sessions: int,
) -> dict:
    values: list[float] = []
    dates: set[str] = set()
    for row in sorted(
        rows,
        key=lambda item: (
            str(item.get("entry_date") or item.get("signal_date") or ""),
            str(item.get("ticker") or ""),
        ),
    ):
        ret = _fixed_horizon_return_pct(row, trusted_price_series, horizon_sessions)
        if ret is None:
            continue
        values.append(float(ret))
        day = str(row.get("entry_date") or row.get("signal_date") or "")[:10]
        if day:
            dates.add(day)

    gains = sum(value for value in values if value > 0.0)
    losses = -sum(value for value in values if value < 0.0)
    if losses > 0.0:
        profit_factor = round(gains / losses, 4)
    elif gains > 0.0:
        profit_factor = float("inf")
    else:
        profit_factor = None

    return {
        "selected_total": len(rows),
        "sample": len(values),
        "unmatured_or_unavailable": max(0, len(rows) - len(values)),
        "dates": len(dates),
        "win_rate_pct": round(100.0 * sum(value > 0.0 for value in values) / len(values), 2) if values else None,
        "avg_return_pct": round(mean(values), 4) if values else None,
        "median_return_pct": round(median(values), 4) if values else None,
        "profit_factor": profit_factor,
        "return_sum_points": round(sum(values), 4) if values else None,
    }


def _fixed_horizon_lift(challenger: dict, baseline: dict) -> dict:
    challenger_avg = _f(challenger.get("avg_return_pct"))
    baseline_avg = _f(baseline.get("avg_return_pct"))
    challenger_pf = challenger.get("profit_factor")
    baseline_pf = baseline.get("profit_factor")
    pf_lift = None
    try:
        if challenger_pf is not None and baseline_pf is not None:
            pf_lift = round(float(challenger_pf) - float(baseline_pf), 4)
    except Exception:
        pf_lift = None
    return {
        "avg_return_lift_pp": (
            round(float(challenger_avg) - float(baseline_avg), 4)
            if challenger_avg is not None and baseline_avg is not None
            else None
        ),
        "profit_factor_lift": pf_lift,
    }


def _build_fixed_horizon_crosscheck(
    candidate_lifecycle_rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
    *,
    max_positions: int,
) -> dict:
    """Cross-check v3 selection on fixed 2/3/5-session closes.

    Training stays strictly chronological and uses only resolved boundary labels
    available before each test date. Selection is frozen before future prices
    are inspected. The evaluation outcome itself is independent of target and
    invalidation barriers, which exposes barrier-geometry artefacts in v3.
    """
    resolved = _resolved(candidate_lifecycle_rows)
    candidates = [
        dict(row)
        for row in candidate_lifecycle_rows
        if str(row.get("entry_date") or row.get("signal_date") or "")[:10]
        and _f(row.get("entry_price")) is not None
    ]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        day = str(row.get("entry_date") or row.get("signal_date") or "")[:10]
        by_date[day].append(row)

    baseline_selected: list[dict] = []
    eligible_selected: list[dict] = []
    test_dates: list[str] = []
    skipped_dates = 0
    latest_positive_features: dict[str, dict] = {}

    for test_date in sorted(by_date):
        train = [
            row for row in resolved
            if str(row.get("boundary_exit_date") or "")[:10] < test_date
        ]
        model = _fit_stable_feature_directions(train)
        if model.get("status") != "EVALUABLE":
            skipped_dates += 1
            continue
        positive_features = _positive_stable_features(model)
        if len(positive_features) < MIN_ELIGIBILITY_FEATURES:
            skipped_dates += 1
            continue

        latest_positive_features = positive_features
        scored = _score_day(by_date[test_date], model)
        augmented: list[dict] = []
        for row in scored:
            item = dict(row)
            evidence = _eligibility_evidence(item, positive_features)
            item["entry_eligibility"] = evidence
            item["entry_eligibility_accept"] = bool(evidence["eligible"])
            augmented.append(item)
        selection_n = min(max(0, int(max_positions)), len(augmented))
        if selection_n <= 0:
            continue

        baseline = sorted(augmented, key=_selection_key_baseline, reverse=True)[:selection_n]
        eligible_pool = [row for row in augmented if row.get("entry_eligibility_accept")]
        eligible = sorted(
            eligible_pool,
            key=_selection_key_challenger,
            reverse=True,
        )[: max(0, int(max_positions))]
        baseline_selected.extend(baseline)
        eligible_selected.extend(eligible)
        test_dates.append(test_date)

    by_horizon: dict[str, dict] = {}
    passing_horizons: list[int] = []
    for horizon in FIXED_HORIZONS:
        baseline_metrics = _fixed_horizon_metrics(
            baseline_selected, trusted_price_series, horizon
        )
        eligible_metrics = _fixed_horizon_metrics(
            eligible_selected, trusted_price_series, horizon
        )
        lift = _fixed_horizon_lift(eligible_metrics, baseline_metrics)
        pf = eligible_metrics.get("profit_factor")
        try:
            pf_positive = pf is not None and float(pf) > 1.0
        except Exception:
            pf_positive = False
        screen = {
            "min_sample_20": int(eligible_metrics.get("sample") or 0) >= 20,
            "min_dates_8": int(eligible_metrics.get("dates") or 0) >= 8,
            "positive_expectancy": float(eligible_metrics.get("avg_return_pct") or 0.0) > 0.0,
            "profit_factor_gt_1": pf_positive,
            "positive_lift_vs_fixed_n": float(lift.get("avg_return_lift_pp") or 0.0) > 0.0,
        }
        screen["pass"] = all(screen.values())
        if screen["pass"]:
            passing_horizons.append(int(horizon))
        by_horizon[str(horizon)] = {
            "baseline_current_alpha_fixed_n": baseline_metrics,
            "eligibility_v3_variable_n": eligible_metrics,
            "lift": lift,
            "research_screen": screen,
        }

    geometry_features = sorted(
        feature for feature in latest_positive_features
        if feature in GEOMETRY_SENSITIVE_FEATURES
    )
    return {
        "mode": "ENTRY_ELIGIBILITY_FIXED_HORIZON_CROSSCHECK_V4",
        "status": "EVALUABLE" if test_dates else "INSUFFICIENT_OOT_SAMPLE",
        "live_mutation": False,
        "promotion_allowed": False,
        "test_dates": len(test_dates),
        "skipped_warmup_or_no_positive_feature_dates": skipped_dates,
        "baseline_selected_total": len(baseline_selected),
        "eligibility_selected_total": len(eligible_selected),
        "by_horizon": by_horizon,
        "research_screen": {
            "passing_horizons": passing_horizons,
            "passing_horizon_count": len(passing_horizons),
            "requires_at_least_two_horizons_for_followup": True,
            "followup_candidate": len(passing_horizons) >= 2,
        },
        "geometry_feature_dependence": {
            "latest_positive_stable_features": sorted(latest_positive_features),
            "geometry_sensitive_features": geometry_features,
            "geometry_risk_detected": bool(geometry_features),
            "interpretation": (
                "v3 may partly benefit from barrier geometry when target/invalidation "
                "distance features are selected; fixed-horizon outcomes are independent "
                "of those exit barriers."
            ),
        },
        "methodology": {
            "selection_decided_before_fixed_horizon_outcome": True,
            "training_rows_must_resolve_before_test_entry": True,
            "training_label_remains_boundary_return_pct": True,
            "evaluation_outcome": "DIRECTIONAL_CLOSE_RETURN_AFTER_2_3_5_FUTURE_SESSIONS",
            "evaluation_excludes_target_and_invalidation_barriers": True,
            "trusted_reverse_segment_respected": True,
            "future_prices_used_only_for_evaluation": True,
            "threshold_optimized_on_fixed_horizon_test": False,
            "live_mutation": False,
            "purpose": (
                "falsify barrier-geometry artefacts before any prospective eligibility promotion"
            ),
        },
    }


def build_entry_eligibility_walkforward(
    candidate_lifecycle_rows: list[dict],
    *,
    max_positions: int = 5,
    trusted_price_series: dict[str, list[dict]] | None = None,
) -> dict:
    """Test whether abstaining on weak days improves capital-slot economics."""
    resolved = _resolved(candidate_lifecycle_rows)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in resolved:
        by_date[str(row.get("entry_date") or "")[:10]].append(row)

    baseline_selected: list[dict] = []
    fixed_rank_selected: list[dict] = []
    eligible_selected: list[dict] = []
    all_eligible_candidates: list[dict] = []
    all_ineligible_candidates: list[dict] = []
    selected_by_date: dict[str, list[dict]] = {}
    fixed_by_date: dict[str, list[dict]] = {}
    date_results: list[dict] = []
    latest_model: dict = {}
    latest_positive_features: dict[str, dict] = {}
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
        positive_features = _positive_stable_features(model)
        if len(positive_features) < MIN_ELIGIBILITY_FEATURES:
            skipped_dates += 1
            continue

        latest_model = model
        latest_positive_features = positive_features
        scored = _score_day(day_rows, model)
        augmented: list[dict] = []
        for row in scored:
            item = dict(row)
            evidence = _eligibility_evidence(item, positive_features)
            item["entry_eligibility"] = evidence
            item["entry_eligibility_accept"] = bool(evidence["eligible"])
            augmented.append(item)

        selection_n = min(max(0, int(max_positions)), len(augmented))
        if selection_n <= 0:
            continue

        baseline = sorted(augmented, key=_selection_key_baseline, reverse=True)[:selection_n]
        fixed_rank = sorted(augmented, key=_selection_key_challenger, reverse=True)[:selection_n]
        eligible_pool = [row for row in augmented if row.get("entry_eligibility_accept")]
        ineligible_pool = [row for row in augmented if not row.get("entry_eligibility_accept")]
        eligible = sorted(eligible_pool, key=_selection_key_challenger, reverse=True)[: max(0, int(max_positions))]

        baseline_selected.extend(baseline)
        fixed_rank_selected.extend(fixed_rank)
        eligible_selected.extend(eligible)
        all_eligible_candidates.extend(eligible_pool)
        all_ineligible_candidates.extend(ineligible_pool)
        selected_by_date[test_date] = eligible
        fixed_by_date[test_date] = fixed_rank

        fixed_avg = mean(float(row["boundary_return_pct"]) for row in fixed_rank)
        eligible_avg = (
            mean(float(row["boundary_return_pct"]) for row in eligible)
            if eligible else None
        )
        date_results.append({
            "date": test_date,
            "train_trades": len(train),
            "train_dates": len({str(row.get("entry_date") or "")[:10] for row in train}),
            "stable_features": len(model.get("features") or {}),
            "positive_stable_features": len(positive_features),
            "candidate_count": len(augmented),
            "eligible_candidate_count": len(eligible_pool),
            "selected_fixed_n": len(fixed_rank),
            "selected_eligible": len(eligible),
            "unused_slots": max(0, int(max_positions) - len(eligible)),
            "abstained_completely": len(eligible) == 0,
            "fixed_rank_avg_return_pct": round(fixed_avg, 4),
            "eligible_avg_return_pct": round(eligible_avg, 4) if eligible_avg is not None else None,
            "eligible_vs_fixed_avg_lift_pp": (
                round(eligible_avg - fixed_avg, 4)
                if eligible_avg is not None else None
            ),
        })

    dates = sorted(selected_by_date)
    split = len(dates) // 2
    early_dates = set(dates[:split])
    late_dates = set(dates[split:])
    early_selected = [row for day in dates if day in early_dates for row in selected_by_date.get(day, [])]
    late_selected = [row for day in dates if day in late_dates for row in selected_by_date.get(day, [])]
    early_fixed = [row for day in dates if day in early_dates for row in fixed_by_date.get(day, [])]
    late_fixed = [row for day in dates if day in late_dates for row in fixed_by_date.get(day, [])]

    baseline_metrics = _metrics_plus(baseline_selected)
    fixed_metrics = _metrics_plus(fixed_rank_selected)
    eligible_metrics = _metrics_plus(eligible_selected)
    eligible_candidate_metrics = _metrics_plus(all_eligible_candidates)
    ineligible_candidate_metrics = _metrics_plus(all_ineligible_candidates)

    return {
        "mode": "ENTRY_QUALITY_ELIGIBILITY_WALK_FORWARD_SHADOW_V3",
        "status": "EVALUABLE" if date_results else "INSUFFICIENT_OOT_SAMPLE",
        "live_mutation": False,
        "source": "ALL_ACTIONABLE_FUSION_ENTER_CANONICAL_BOUNDARY_LIFECYCLE",
        "candidate_pool_resolved": len(resolved),
        "test_dates": len(date_results),
        "skipped_warmup_or_no_positive_feature_dates": skipped_dates,
        "dates_with_any_eligible": sum(1 for row in date_results if int(row.get("selected_eligible") or 0) > 0),
        "dates_abstained_completely": sum(bool(row.get("abstained_completely")) for row in date_results),
        "selected_trades": len(eligible_selected),
        "unused_slots_total": sum(int(row.get("unused_slots") or 0) for row in date_results),
        "avg_selected_per_test_date": round(len(eligible_selected) / len(date_results), 3) if date_results else None,
        "baseline_current_alpha_fixed_n": baseline_metrics,
        "cross_sectional_v2_fixed_n": fixed_metrics,
        "eligibility_v3_variable_n": eligible_metrics,
        "eligibility_lift_vs_v2": _lift(eligible_metrics, fixed_metrics),
        "eligibility_lift_vs_current_alpha": _lift(eligible_metrics, baseline_metrics),
        "candidate_diagnostics": {
            "eligible": eligible_candidate_metrics,
            "ineligible": ineligible_candidate_metrics,
            "eligible_minus_ineligible_avg_return_pp": (
                round(
                    float(eligible_candidate_metrics["avg_return_pct"])
                    - float(ineligible_candidate_metrics["avg_return_pct"]),
                    4,
                )
                if eligible_candidate_metrics.get("avg_return_pct") is not None
                and ineligible_candidate_metrics.get("avg_return_pct") is not None
                else None
            ),
        },
        "by_direction": _by_direction(eligible_selected),
        "chronological_stability": {
            "early_half": {
                "dates": len(early_dates),
                "fixed_n": _metrics_plus(early_fixed),
                "eligibility": _metrics_plus(early_selected),
                "lift": _lift(_metrics_plus(early_selected), _metrics_plus(early_fixed)),
            },
            "late_half": {
                "dates": len(late_dates),
                "fixed_n": _metrics_plus(late_fixed),
                "eligibility": _metrics_plus(late_selected),
                "lift": _lift(_metrics_plus(late_selected), _metrics_plus(late_fixed)),
            },
        },
        "latest_train_model": latest_model,
        "latest_positive_stable_features": latest_positive_features,
        "fixed_horizon_crosscheck_v4": (
            _build_fixed_horizon_crosscheck(
                candidate_lifecycle_rows,
                trusted_price_series,
                max_positions=max_positions,
            )
            if trusted_price_series
            else {
                "mode": "ENTRY_ELIGIBILITY_FIXED_HORIZON_CROSSCHECK_V4",
                "status": "NOT_RUN_NO_TRUSTED_PRICE_SERIES",
                "live_mutation": False,
                "promotion_allowed": False,
            }
        ),
        "methodology": {
            "training_labels_available_only_after_boundary_exit": True,
            "train_rows_must_resolve_before_test_entry": True,
            "candidate_pool_is_full_actionable_enter": True,
            "stable_feature_direction_requires_chronological_half_consistency": True,
            "favorable_feature_side_must_be_profitable_in_both_train_halves": True,
            "eligibility_cut_is_conservative_across_train_halves": True,
            "eligibility_rule": "strict majority support with >=3 observed positive-stable features",
            "ranking_after_eligibility": "cross_sectional_v2_score",
            "unused_slots_allowed": True,
            "threshold_optimized_on_test": False,
            "live_mutation": False,
            "max_positions": int(max_positions),
            "purpose": "test whether TMAD should abstain instead of filling capital slots when point-in-time evidence is weak",
        },
        "date_results": date_results[-120:],
    }
