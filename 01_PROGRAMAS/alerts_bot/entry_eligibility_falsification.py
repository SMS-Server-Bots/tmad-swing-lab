from __future__ import annotations

"""Historical falsification suite for TMAD Entry Eligibility.

v6.2.17 does not change LIVE selection. It asks one narrow question:
does the existing v3 abstention rule separate better and worse actionable
FUSION ENTER candidates robustly enough to justify a later prospective test?

The rule, feature fitting, thresholds and ranking are imported from the existing
Eligibility implementation. Test outcomes never tune the rule.
"""

from collections import defaultdict
from statistics import mean
from typing import Any

from entry_quality_cross_sectional import (
    FEATURES,
    _f,
    _fit_stable_feature_directions,
    _resolved,
    _score_day,
    _selection_key_baseline,
    _selection_key_challenger,
)
from entry_quality_eligibility import (
    FIXED_HORIZONS,
    GEOMETRY_SENSITIVE_FEATURES,
    MIN_ELIGIBILITY_FEATURES,
    _eligibility_evidence,
    _fixed_horizon_lift,
    _fixed_horizon_metrics,
    _metrics_plus,
    _positive_stable_features,
)

MODE = "ENTRY_ELIGIBILITY_HISTORICAL_FALSIFICATION_V5"


def _day(row: dict) -> str:
    return str(row.get("entry_date") or row.get("signal_date") or "")[:10]


def _direction(row: dict) -> str:
    return str(row.get("direction") or "").upper()


def _pf_gt_one(metrics: dict) -> bool:
    try:
        value = metrics.get("profit_factor")
        return value is not None and float(value) > 1.0
    except Exception:
        return False


def _avg_positive(metrics: dict) -> bool:
    try:
        return float(metrics.get("avg_return_pct") or 0.0) > 0.0
    except Exception:
        return False


def _metric_delta(left: dict, right: dict) -> dict:
    """Return right-left so positive means the second population is better."""
    la = _f(left.get("avg_return_pct"))
    ra = _f(right.get("avg_return_pct"))
    lp = _f(left.get("profit_factor"))
    rp = _f(right.get("profit_factor"))
    return {
        "avg_return_pp": round(float(ra) - float(la), 4) if la is not None and ra is not None else None,
        "profit_factor": round(float(rp) - float(lp), 4) if lp is not None and rp is not None else None,
    }


def _classify_day(
    rows: list[dict],
    model: dict,
    positive_features: dict[str, dict],
    *,
    excluded_features: set[str] | None = None,
) -> list[dict]:
    excluded = set(excluded_features or set())
    active_features = {
        key: value for key, value in positive_features.items()
        if key not in excluded
    }
    scored = _score_day(rows, model)
    out: list[dict] = []
    for row in scored:
        item = dict(row)
        evidence = _eligibility_evidence(item, active_features)
        item["entry_eligibility"] = evidence
        item["entry_eligibility_accept"] = bool(evidence["eligible"])
        out.append(item)
    return out


def _select(
    augmented: list[dict],
    *,
    max_positions: int,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    selection_n = min(max(0, int(max_positions)), len(augmented))
    baseline = sorted(
        augmented,
        key=_selection_key_baseline,
        reverse=True,
    )[:selection_n]
    eligible_pool = [row for row in augmented if row.get("entry_eligibility_accept")]
    ineligible_pool = [row for row in augmented if not row.get("entry_eligibility_accept")]
    eligible = sorted(
        eligible_pool,
        key=_selection_key_challenger,
        reverse=True,
    )[: max(0, int(max_positions))]
    return baseline, eligible, eligible_pool, ineligible_pool


def _rows_on_dates(rows: list[dict], dates: set[str]) -> list[dict]:
    return [row for row in rows if _day(row) in dates]


def _direction_metrics(
    rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
    horizon: int,
) -> dict:
    out: dict[str, dict] = {}
    for direction in ("LONG", "SHORT"):
        subset = [row for row in rows if _direction(row) == direction]
        out[direction] = _fixed_horizon_metrics(subset, trusted_price_series, horizon)
    return out


def _horizon_screen(
    eligible_selected: dict,
    baseline: dict,
    eligible_all: dict,
    ineligible_all: dict,
    early: dict,
    late: dict,
) -> dict:
    sample = int(eligible_selected.get("sample") or 0)
    dates = int(eligible_selected.get("dates") or 0)
    lift_vs_baseline = _metric_delta(baseline, eligible_selected)
    separation = _metric_delta(ineligible_all, eligible_all)
    screen = {
        "min_sample_20": sample >= 20,
        "min_dates_8": dates >= 8,
        "positive_expectancy": _avg_positive(eligible_selected),
        "profit_factor_gt_1": _pf_gt_one(eligible_selected),
        "positive_lift_vs_baseline": float(lift_vs_baseline.get("avg_return_pp") or 0.0) > 0.0,
        "eligible_beats_ineligible": float(separation.get("avg_return_pp") or 0.0) > 0.0,
        "early_positive": _avg_positive(early),
        "late_positive": _avg_positive(late),
    }
    screen["economic_pass"] = all(
        screen[key]
        for key in (
            "min_sample_20",
            "min_dates_8",
            "positive_expectancy",
            "profit_factor_gt_1",
            "positive_lift_vs_baseline",
            "eligible_beats_ineligible",
        )
    )
    screen["temporal_pass"] = bool(
        screen["economic_pass"]
        and screen["early_positive"]
        and screen["late_positive"]
    )
    return {
        **screen,
        "lift_vs_baseline": lift_vs_baseline,
        "eligible_vs_ineligible": separation,
    }


def _variant_summary(
    rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
) -> dict:
    by_horizon: dict[str, dict] = {}
    passes: list[int] = []
    for horizon in FIXED_HORIZONS:
        metrics = _fixed_horizon_metrics(rows, trusted_price_series, horizon)
        passed = (
            int(metrics.get("sample") or 0) >= 20
            and int(metrics.get("dates") or 0) >= 8
            and _avg_positive(metrics)
            and _pf_gt_one(metrics)
        )
        if passed:
            passes.append(int(horizon))
        by_horizon[str(horizon)] = {
            "metrics": metrics,
            "positive_screen": passed,
        }
    return {
        "selected_total": len(rows),
        "passing_horizons": passes,
        "passing_horizon_count": len(passes),
        "followup_candidate": len(passes) >= 2,
        "by_horizon": by_horizon,
    }


def build_entry_eligibility_falsification(
    candidate_lifecycle_rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
    *,
    max_positions: int = 5,
    lane_label: str = "ROLLING_LATEST",
) -> dict:
    resolved = _resolved(candidate_lifecycle_rows)
    candidates = [
        dict(row) for row in candidate_lifecycle_rows
        if _day(row) and _f(row.get("entry_price")) is not None
    ]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        by_date[_day(row)].append(row)

    baseline_selected: list[dict] = []
    eligible_selected: list[dict] = []
    eligible_candidates: list[dict] = []
    ineligible_candidates: list[dict] = []
    selected_by_date: dict[str, list[dict]] = {}
    baseline_by_date: dict[str, list[dict]] = {}
    eligible_candidates_by_date: dict[str, list[dict]] = {}
    ineligible_candidates_by_date: dict[str, list[dict]] = {}
    feature_seen_dates: dict[str, int] = defaultdict(int)
    loo_selected: dict[str, list[dict]] = defaultdict(list)
    geometry_free_selected: list[dict] = []
    test_dates: list[str] = []
    skipped_dates = 0
    date_diagnostics: list[dict] = []

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

        augmented = _classify_day(by_date[test_date], model, positive_features)
        baseline, eligible, eligible_pool, ineligible_pool = _select(
            augmented,
            max_positions=max_positions,
        )
        baseline_selected.extend(baseline)
        eligible_selected.extend(eligible)
        eligible_candidates.extend(eligible_pool)
        ineligible_candidates.extend(ineligible_pool)
        baseline_by_date[test_date] = baseline
        selected_by_date[test_date] = eligible
        eligible_candidates_by_date[test_date] = eligible_pool
        ineligible_candidates_by_date[test_date] = ineligible_pool
        test_dates.append(test_date)

        for feature in FEATURES:
            if feature in positive_features:
                feature_seen_dates[feature] += 1
            reduced = {
                key: value for key, value in positive_features.items()
                if key != feature
            }
            if len(reduced) < MIN_ELIGIBILITY_FEATURES:
                continue
            variant_augmented = _classify_day(
                by_date[test_date],
                model,
                reduced,
            )
            _base, variant_eligible, _ep, _ip = _select(
                variant_augmented,
                max_positions=max_positions,
            )
            loo_selected[feature].extend(variant_eligible)

        geometry_reduced = {
            key: value for key, value in positive_features.items()
            if key not in GEOMETRY_SENSITIVE_FEATURES
        }
        if len(geometry_reduced) >= MIN_ELIGIBILITY_FEATURES:
            geometry_augmented = _classify_day(
                by_date[test_date],
                model,
                geometry_reduced,
            )
            _base, geometry_eligible, _ep, _ip = _select(
                geometry_augmented,
                max_positions=max_positions,
            )
            geometry_free_selected.extend(geometry_eligible)

        date_diagnostics.append({
            "date": test_date,
            "train_trades": len(train),
            "train_dates": len({_day(row) for row in train}),
            "positive_stable_features": sorted(positive_features),
            "candidate_count": len(augmented),
            "eligible_candidates": len(eligible_pool),
            "ineligible_candidates": len(ineligible_pool),
            "selected_baseline": len(baseline),
            "selected_eligible": len(eligible),
            "unused_slots": max(0, int(max_positions) - len(eligible)),
        })

    split = len(test_dates) // 2
    early_dates = set(test_dates[:split])
    late_dates = set(test_dates[split:])

    by_horizon: dict[str, dict] = {}
    economic_passes: list[int] = []
    temporal_passes: list[int] = []
    direction_failures: list[dict] = []

    for horizon in FIXED_HORIZONS:
        baseline_metrics = _fixed_horizon_metrics(
            baseline_selected, trusted_price_series, horizon
        )
        eligible_metrics = _fixed_horizon_metrics(
            eligible_selected, trusted_price_series, horizon
        )
        eligible_all_metrics = _fixed_horizon_metrics(
            eligible_candidates, trusted_price_series, horizon
        )
        ineligible_all_metrics = _fixed_horizon_metrics(
            ineligible_candidates, trusted_price_series, horizon
        )
        early_metrics = _fixed_horizon_metrics(
            _rows_on_dates(eligible_selected, early_dates),
            trusted_price_series,
            horizon,
        )
        late_metrics = _fixed_horizon_metrics(
            _rows_on_dates(eligible_selected, late_dates),
            trusted_price_series,
            horizon,
        )
        direction_metrics = _direction_metrics(
            eligible_selected,
            trusted_price_series,
            horizon,
        )
        screen = _horizon_screen(
            eligible_metrics,
            baseline_metrics,
            eligible_all_metrics,
            ineligible_all_metrics,
            early_metrics,
            late_metrics,
        )
        if screen["economic_pass"]:
            economic_passes.append(int(horizon))
        if screen["temporal_pass"]:
            temporal_passes.append(int(horizon))

        direction_screen: dict[str, dict] = {}
        for direction, metrics in direction_metrics.items():
            sample = int(metrics.get("sample") or 0)
            evaluable = sample >= 10
            passed = (not evaluable) or (_avg_positive(metrics) and _pf_gt_one(metrics))
            direction_screen[direction] = {
                "evaluable": evaluable,
                "pass": passed,
                "metrics": metrics,
            }
            if evaluable and not passed:
                direction_failures.append({
                    "horizon": int(horizon),
                    "direction": direction,
                    "metrics": metrics,
                })

        by_horizon[str(horizon)] = {
            "baseline_fixed_n": baseline_metrics,
            "eligible_selected": eligible_metrics,
            "eligible_all_candidates": eligible_all_metrics,
            "ineligible_all_candidates": ineligible_all_metrics,
            "early_half_eligible": early_metrics,
            "late_half_eligible": late_metrics,
            "by_direction": direction_screen,
            "screen": screen,
        }

    loo: dict[str, dict] = {}
    loo_followup = 0
    for feature in sorted(feature_seen_dates):
        summary = _variant_summary(
            loo_selected.get(feature) or [],
            trusted_price_series,
        )
        summary["feature_seen_test_dates"] = int(feature_seen_dates.get(feature) or 0)
        loo[feature] = summary
        if summary["followup_candidate"]:
            loo_followup += 1

    geometry_free = _variant_summary(
        geometry_free_selected,
        trusted_price_series,
    )
    loo_total = len(loo)
    loo_followup_pct = round(100.0 * loo_followup / loo_total, 2) if loo_total else None
    single_feature_dependency = bool(
        loo_total
        and any(int(row.get("passing_horizon_count") or 0) == 0 for row in loo.values())
    )

    boundary = {
        "baseline_fixed_n": _metrics_plus([
            row for row in baseline_selected
            if _f(row.get("boundary_return_pct")) is not None
        ]),
        "eligible_selected": _metrics_plus([
            row for row in eligible_selected
            if _f(row.get("boundary_return_pct")) is not None
        ]),
        "eligible_all_candidates": _metrics_plus([
            row for row in eligible_candidates
            if _f(row.get("boundary_return_pct")) is not None
        ]),
        "ineligible_all_candidates": _metrics_plus([
            row for row in ineligible_candidates
            if _f(row.get("boundary_return_pct")) is not None
        ]),
    }
    boundary["eligible_vs_ineligible"] = _metric_delta(
        boundary["ineligible_all_candidates"],
        boundary["eligible_all_candidates"],
    )

    historical_screen = {
        "at_least_two_economic_horizons": len(economic_passes) >= 2,
        "at_least_two_temporally_stable_horizons": len(temporal_passes) >= 2,
        "no_sufficiently_sampled_direction_failure": not direction_failures,
        "loo_at_least_half_variants_keep_two_horizons": (
            loo_followup_pct is not None and float(loo_followup_pct) >= 50.0
        ),
        "no_single_feature_removal_kills_all_horizons": not single_feature_dependency,
        "geometry_free_has_positive_horizon": int(geometry_free.get("passing_horizon_count") or 0) >= 1,
    }
    historical_screen["survives_historical_falsification"] = all(historical_screen.values())

    return {
        "mode": MODE,
        "lane": str(lane_label or "ROLLING_LATEST").upper(),
        "status": "EVALUABLE" if test_dates else "INSUFFICIENT_OOT_SAMPLE",
        "live_mutation": False,
        "promotion_allowed": False,
        "candidate_rows": len(candidates),
        "resolved_training_rows": len(resolved),
        "test_dates": len(test_dates),
        "skipped_warmup_or_no_positive_feature_dates": skipped_dates,
        "selected_eligible": len(eligible_selected),
        "selected_baseline": len(baseline_selected),
        "eligible_candidates": len(eligible_candidates),
        "ineligible_candidates": len(ineligible_candidates),
        "unused_slots_total": sum(int(row.get("unused_slots") or 0) for row in date_diagnostics),
        "boundary_outcome": boundary,
        "by_horizon": by_horizon,
        "economic_passing_horizons": economic_passes,
        "temporal_passing_horizons": temporal_passes,
        "direction_failures": direction_failures,
        "leave_one_feature_out": {
            "variants": loo,
            "variant_count": loo_total,
            "followup_candidate_count": loo_followup,
            "followup_candidate_pct": loo_followup_pct,
            "single_feature_dependency_detected": single_feature_dependency,
        },
        "geometry_free": geometry_free,
        "historical_screen": historical_screen,
        "methodology": {
            "eligibility_rule_identical_to_v3": True,
            "training_rows_must_resolve_before_test_entry": True,
            "stable_feature_direction_requires_chronological_half_consistency": True,
            "test_outcomes_do_not_tune_thresholds": True,
            "fixed_horizon_outcomes_use_trusted_price_lane": True,
            "eligible_vs_ineligible_uses_full_actionable_enter_candidate_pool": True,
            "early_late_split_is_chronological_test_date_split": True,
            "direction_subgroups_only_fail_when_sample_at_least_10": True,
            "leave_one_feature_out_removes_feature_before_test_day_classification": True,
            "geometry_free_removes_target_and_invalidation_distance_features": True,
            "max_positions": int(max_positions),
            "purpose": (
                "falsify whether abstention separates economically useful ENTER candidates "
                "before any prospective or LIVE promotion"
            ),
        },
        "date_diagnostics": date_diagnostics[-160:],
    }


def combine_eligibility_falsification(
    rolling: dict,
    fixed: dict,
) -> dict:
    rolling_survives = bool(
        (rolling.get("historical_screen") or {}).get("survives_historical_falsification")
    )
    fixed_survives = bool(
        (fixed.get("historical_screen") or {}).get("survives_historical_falsification")
    )
    return {
        "mode": "ENTRY_ELIGIBILITY_DUAL_LANE_DECISION_V5",
        "status": "EVALUABLE" if rolling.get("status") == "EVALUABLE" and fixed.get("status") == "EVALUABLE" else "INCOMPLETE",
        "live_mutation": False,
        "promotion_allowed": False,
        "rolling_survives": rolling_survives,
        "fixed_benchmark_survives": fixed_survives,
        "historical_followup_candidate": bool(rolling_survives and fixed_survives),
        "next_step_if_pass": "PROSPECTIVE_JOURNAL_ONLY",
        "next_step_if_fail": "CLOSE_ELIGIBILITY_AS_PRIMARY_PATH_AND_REVISIT_GENERATOR_OR_TARGET",
        "decision_rule": (
            "Eligibility proceeds only if both rolling and fixed-benchmark lanes survive the "
            "predeclared historical falsification screen."
        ),
    }
