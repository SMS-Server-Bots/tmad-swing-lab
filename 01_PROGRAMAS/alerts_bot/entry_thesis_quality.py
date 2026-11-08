from __future__ import annotations

"""Out-of-time thesis-quality research for actionable FUSION ENTER candidates.

v6.1.141 showed that return-trained eligibility separates eligible from
ineligible candidates but still does not create positive expectancy. This lane
changes the learning target from boundary return magnitude to the actual thesis
question: does MOVEMENT_COMPLETED happen before THESIS_INVALIDATED?

The model intentionally excludes target/invalidation distance from the learning
features so it does not "learn" payoff geometry as a proxy for signal quality.
It uses richer point-in-time market/relational context already captured by TMAD.
Training uses only outcomes resolved before each test entry date. Test-day
selection is made over the full candidate set, including candidates whose final
boundary outcome is not yet evaluable at the end of history.

Research/SHADOW only. Nothing here mutates LIVE selection.
"""

import math
from collections import defaultdict
from statistics import mean, median
from typing import Any

MIN_TRAIN_TRADES = 50
MIN_TRAIN_DATES = 14
MIN_HALF_OBSERVATIONS = 14
MIN_SPLIT_SIDE = 5
MIN_STABLE_FEATURES = 3
MIN_CATEGORY_HALF = 6

NUMERIC_FEATURES = (
    "underlying_alpha_score",
    "tradeability_score",
    "core_score",
    "gex_score",
    "overextension_score",
    "market_adjustment",
    "direction_consistency_pct",
    "same_direction_observations",
    "lifecycle_observations",
    "countertrend_override",
    "direction_long",
    "market_context_score",
    "market_breadth_score",
    "vix_stress_score",
    "credit_risk_on_score",
    "market_deterioration_score",
    "sector_context_score",
    "sector_breadth_score",
    "sector_deterioration_score",
    "leveraged_confirmation_score",
    "relationship_context_score",
    "relationship_lead_lag_score",
    "relationship_deterioration_score",
    "graph_context_score",
    "relational_deterioration_score",
    "relative_strength_vs_market",
    "relative_strength_vs_sector",
    "node_direction_score",
    "scanner_trend_score",
    "momentum_score",
    "rsi",
    "rvol",
    "flow_direction",
    "pc_ratio",
)

CATEGORICAL_FEATURES = (
    "market_regime",
    "market_policy",
    "signal_origin",
    "intraday_state",
    "asset_type",
)


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _completion_label(row: dict) -> int | None:
    reason = str(row.get("boundary_exit_reason") or "")
    if reason == "MOVEMENT_COMPLETED":
        return 1
    if reason.startswith("THESIS_INVALIDATED") or reason == "INVALIDATION_AMBIGUOUS_BAR":
        return 0
    return None


def _resolved(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if str(row.get("boundary_status") or "").upper() != "RESOLVED":
            continue
        if _completion_label(row) is None:
            continue
        if not str(row.get("entry_date") or "")[:10] or not str(row.get("boundary_exit_date") or "")[:10]:
            continue
        out.append(dict(row))
    return out


def _candidate_rows(rows: list[dict]) -> list[dict]:
    return [
        dict(row)
        for row in rows
        if str(row.get("entry_date") or "")[:10]
        and str(row.get("ticker") or "")
        and str(row.get("direction") or "").upper() in {"LONG", "SHORT"}
    ]


def _numeric_values(row: dict) -> dict[str, float | None]:
    diag = row.get("entry_diagnostics") if isinstance(row.get("entry_diagnostics"), dict) else {}
    direction = str(row.get("direction") or "").upper()
    return {
        "underlying_alpha_score": _f(row.get("underlying_alpha_score")),
        "tradeability_score": _f(row.get("tradeability_score")),
        "core_score": _f(diag.get("core_score")),
        "gex_score": _f(diag.get("gex_score")),
        "overextension_score": _f(diag.get("overextension_score")),
        "market_adjustment": _f(diag.get("market_adjustment")),
        "direction_consistency_pct": _f(diag.get("direction_consistency_pct")),
        "same_direction_observations": _f(diag.get("same_direction_observations")),
        "lifecycle_observations": _f(diag.get("lifecycle_observations")),
        "countertrend_override": 1.0 if bool(diag.get("countertrend_override")) else 0.0,
        "direction_long": 1.0 if direction == "LONG" else 0.0,
        "market_context_score": _f(diag.get("market_context_score")),
        "market_breadth_score": _f(diag.get("market_breadth_score")),
        "vix_stress_score": _f(diag.get("vix_stress_score")),
        "credit_risk_on_score": _f(diag.get("credit_risk_on_score")),
        "market_deterioration_score": _f(diag.get("market_deterioration_score")),
        "sector_context_score": _f(diag.get("sector_context_score")),
        "sector_breadth_score": _f(diag.get("sector_breadth_score")),
        "sector_deterioration_score": _f(diag.get("sector_deterioration_score")),
        "leveraged_confirmation_score": _f(diag.get("leveraged_confirmation_score")),
        "relationship_context_score": _f(diag.get("relationship_context_score")),
        "relationship_lead_lag_score": _f(diag.get("relationship_lead_lag_score")),
        "relationship_deterioration_score": _f(diag.get("relationship_deterioration_score")),
        "graph_context_score": _f(diag.get("graph_context_score")),
        "relational_deterioration_score": _f(diag.get("relational_deterioration_score")),
        "relative_strength_vs_market": _f(diag.get("relative_strength_vs_market")),
        "relative_strength_vs_sector": _f(diag.get("relative_strength_vs_sector")),
        "node_direction_score": _f(diag.get("node_direction_score")),
        "scanner_trend_score": _f(diag.get("scanner_trend_score")),
        "momentum_score": _f(diag.get("momentum_score")),
        "rsi": _f(diag.get("rsi")),
        "rvol": _f(diag.get("rvol")),
        "flow_direction": _f(diag.get("flow_direction")),
        "pc_ratio": _f(diag.get("pc_ratio")),
    }


def _categorical_values(row: dict) -> dict[str, str]:
    diag = row.get("entry_diagnostics") if isinstance(row.get("entry_diagnostics"), dict) else {}
    return {
        name: str(diag.get(name) or "").strip().upper()
        for name in CATEGORICAL_FEATURES
    }


def _balanced_cut(values: list[float]) -> float | None:
    ordered = sorted(float(value) for value in values)
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
    mid = len(unique) // 2
    if mid <= 0:
        return None
    return (unique[mid - 1] + unique[mid]) / 2.0


def _completion_rate(rows: list[dict]) -> float | None:
    labels = [_completion_label(row) for row in rows]
    labels = [int(label) for label in labels if label is not None]
    return mean(labels) if labels else None


def _numeric_effect(rows: list[dict], feature: str) -> dict | None:
    pairs: list[tuple[float, int]] = []
    for row in rows:
        value = _numeric_values(row).get(feature)
        label = _completion_label(row)
        if value is not None and label is not None:
            pairs.append((float(value), int(label)))
    if len(pairs) < MIN_HALF_OBSERVATIONS:
        return None
    cut = _balanced_cut([value for value, _label in pairs])
    if cut is None:
        return None
    low = [label for value, label in pairs if value <= cut]
    high = [label for value, label in pairs if value > cut]
    if len(low) < MIN_SPLIT_SIDE or len(high) < MIN_SPLIT_SIDE:
        return None
    return {
        "sample": len(pairs),
        "cut": round(float(cut), 6),
        "low_n": len(low),
        "high_n": len(high),
        "low_completion_rate": round(mean(low), 6),
        "high_completion_rate": round(mean(high), 6),
        "high_minus_low": round(mean(high) - mean(low), 6),
    }


def _fit_model(train_rows: list[dict]) -> dict:
    dates = sorted({str(row.get("entry_date") or "")[:10] for row in train_rows})
    if len(train_rows) < MIN_TRAIN_TRADES or len(dates) < MIN_TRAIN_DATES:
        return {
            "status": "INSUFFICIENT_TRAIN",
            "train_trades": len(train_rows),
            "train_dates": len(dates),
            "numeric": {},
            "categorical": {},
        }

    split = max(1, len(dates) // 2)
    first_dates = set(dates[:split])
    second_dates = set(dates[split:])
    first = [row for row in train_rows if str(row.get("entry_date") or "")[:10] in first_dates]
    second = [row for row in train_rows if str(row.get("entry_date") or "")[:10] in second_dates]
    first_base = _completion_rate(first)
    second_base = _completion_rate(second)
    if first_base is None or second_base is None:
        return {
            "status": "INSUFFICIENT_LABELS",
            "train_trades": len(train_rows),
            "train_dates": len(dates),
            "numeric": {},
            "categorical": {},
        }

    numeric: dict[str, dict] = {}
    for feature in NUMERIC_FEATURES:
        e1 = _numeric_effect(first, feature)
        e2 = _numeric_effect(second, feature)
        if not e1 or not e2:
            continue
        d1 = float(e1["high_minus_low"])
        d2 = float(e2["high_minus_low"])
        if d1 == 0.0 or d2 == 0.0 or (d1 > 0) != (d2 > 0):
            continue
        direction = 1 if d1 > 0 else -1
        if direction > 0:
            favorable1 = float(e1["high_completion_rate"])
            favorable2 = float(e2["high_completion_rate"])
            conservative_cut = max(float(e1["cut"]), float(e2["cut"]))
            side = "HIGH"
        else:
            favorable1 = float(e1["low_completion_rate"])
            favorable2 = float(e2["low_completion_rate"])
            conservative_cut = min(float(e1["cut"]), float(e2["cut"]))
            side = "LOW"
        if favorable1 <= float(first_base) or favorable2 <= float(second_base):
            continue
        numeric[feature] = {
            "direction": direction,
            "favorable_side": side,
            "conservative_cut": round(conservative_cut, 6),
            "first_half": e1,
            "second_half": e2,
            "first_half_base_completion_rate": round(float(first_base), 6),
            "second_half_base_completion_rate": round(float(second_base), 6),
            "min_favorable_lift": round(
                min(favorable1 - float(first_base), favorable2 - float(second_base)), 6
            ),
        }

    categorical: dict[str, dict] = {}
    for feature in CATEGORICAL_FEATURES:
        first_groups: dict[str, list[dict]] = defaultdict(list)
        second_groups: dict[str, list[dict]] = defaultdict(list)
        for row in first:
            value = _categorical_values(row).get(feature) or ""
            if value:
                first_groups[value].append(row)
        for row in second:
            value = _categorical_values(row).get(feature) or ""
            if value:
                second_groups[value].append(row)
        stable_values: dict[str, str] = {}
        details: dict[str, dict] = {}
        for value in sorted(set(first_groups) & set(second_groups)):
            a = first_groups[value]
            b = second_groups[value]
            if len(a) < MIN_CATEGORY_HALF or len(b) < MIN_CATEGORY_HALF:
                continue
            ra = _completion_rate(a)
            rb = _completion_rate(b)
            if ra is None or rb is None:
                continue
            if ra > first_base and rb > second_base:
                state = "SUPPORT"
            elif ra < first_base and rb < second_base:
                state = "OPPOSE"
            else:
                continue
            stable_values[value] = state
            details[value] = {
                "state": state,
                "first_n": len(a),
                "second_n": len(b),
                "first_completion_rate": round(float(ra), 6),
                "second_completion_rate": round(float(rb), 6),
            }
        if stable_values:
            categorical[feature] = {
                "values": stable_values,
                "details": details,
            }

    evidence_dimensions = len(numeric) + len(categorical)
    return {
        "status": "EVALUABLE" if evidence_dimensions >= MIN_STABLE_FEATURES else "INSUFFICIENT_STABLE_FEATURES",
        "train_trades": len(train_rows),
        "train_dates": len(dates),
        "first_half_dates": len(first_dates),
        "second_half_dates": len(second_dates),
        "first_half_base_completion_rate": round(float(first_base), 6),
        "second_half_base_completion_rate": round(float(second_base), 6),
        "numeric": numeric,
        "categorical": categorical,
        "target": "MOVEMENT_COMPLETED_BEFORE_THESIS_INVALIDATED",
        "payoff_geometry_features_excluded": [
            "target_distance_pct",
            "invalidation_distance_pct",
        ],
    }


def _evidence(row: dict, model: dict) -> dict:
    numeric_values = _numeric_values(row)
    categorical_values = _categorical_values(row)
    support = 0
    oppose = 0
    detail: dict[str, str] = {}

    for feature, meta in (model.get("numeric") or {}).items():
        value = numeric_values.get(feature)
        if value is None:
            continue
        cut = float(meta["conservative_cut"])
        favorable = float(value) > cut if int(meta["direction"]) > 0 else float(value) <= cut
        state = "SUPPORT" if favorable else "OPPOSE"
        support += int(favorable)
        oppose += int(not favorable)
        detail[feature] = state

    for feature, meta in (model.get("categorical") or {}).items():
        value = categorical_values.get(feature) or ""
        state = str((meta.get("values") or {}).get(value) or "")
        if state == "SUPPORT":
            support += 1
            detail[f"{feature}={value}"] = "SUPPORT"
        elif state == "OPPOSE":
            oppose += 1
            detail[f"{feature}={value}"] = "OPPOSE"

    observed = support + oppose
    score = (support - oppose) / observed if observed else None
    eligible = (
        observed >= MIN_STABLE_FEATURES
        and support > oppose
    )
    return {
        "eligible": eligible,
        "support": support,
        "oppose": oppose,
        "observed": observed,
        "net_support": support - oppose,
        "score": round(float(score), 6) if score is not None else None,
        "detail": detail,
    }


def _economic_metrics(rows: list[dict]) -> dict:
    evaluable = [
        row for row in rows
        if str(row.get("boundary_status") or "").upper() == "RESOLVED"
        and _completion_label(row) is not None
        and _f(row.get("boundary_return_pct")) is not None
    ]
    returns = [float(row["boundary_return_pct"]) for row in evaluable]
    wins = sum(_completion_label(row) == 1 for row in evaluable)
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    pf = None if losses <= 0 and gains <= 0 else (float("inf") if losses <= 0 else gains / losses)
    return {
        "selected_total": len(rows),
        "evaluable": len(evaluable),
        "unresolved_selected": len(rows) - len(evaluable),
        "completion_rate_pct": round(100.0 * wins / len(evaluable), 2) if evaluable else None,
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "median_return_pct": round(median(returns), 4) if returns else None,
        "profit_factor": round(float(pf), 4) if pf is not None and math.isfinite(float(pf)) else pf,
        "return_sum_points": round(sum(returns), 4) if returns else None,
        "movement_completed": wins,
        "thesis_invalidated": len(evaluable) - wins,
    }


def _selection_key_score(row: dict) -> tuple[float, float, float, str]:
    evidence = row.get("thesis_quality") if isinstance(row.get("thesis_quality"), dict) else {}
    score = _f(evidence.get("score"))
    alpha = _f(row.get("underlying_alpha_score"))
    tradeability = _f(row.get("tradeability_score"))
    return (
        float(score) if score is not None else -1e12,
        float(alpha) if alpha is not None else -1e12,
        float(tradeability) if tradeability is not None else -1e12,
        str(row.get("ticker") or ""),
    )


def _selection_key_alpha(row: dict) -> tuple[float, float, str]:
    alpha = _f(row.get("underlying_alpha_score"))
    tradeability = _f(row.get("tradeability_score"))
    return (
        float(alpha) if alpha is not None else -1e12,
        float(tradeability) if tradeability is not None else -1e12,
        str(row.get("ticker") or ""),
    )


def _lift(challenger: dict, baseline: dict) -> dict:
    out = {}
    for key, out_key in (
        ("avg_return_pct", "avg_return_lift_pp"),
        ("profit_factor", "profit_factor_lift"),
        ("completion_rate_pct", "completion_rate_lift_pp"),
    ):
        a = _f(challenger.get(key))
        b = _f(baseline.get(key))
        out[out_key] = round(a - b, 4) if a is not None and b is not None else None
    return out


def _by_direction(rows: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("direction") or "UNKNOWN").upper()].append(row)
    return {key: _economic_metrics(items) for key, items in sorted(grouped.items())}


def build_thesis_quality_walkforward(
    candidate_lifecycle_rows: list[dict],
    *,
    max_positions: int = 5,
) -> dict:
    all_candidates = _candidate_rows(candidate_lifecycle_rows)
    resolved = _resolved(candidate_lifecycle_rows)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in all_candidates:
        by_date[str(row.get("entry_date") or "")[:10]].append(row)

    baseline_selected: list[dict] = []
    fixed_score_selected: list[dict] = []
    eligible_selected: list[dict] = []
    eligible_alpha_selected: list[dict] = []
    eligible_all_candidates: list[dict] = []
    eligible_candidates_evaluable: list[dict] = []
    ineligible_candidates_evaluable: list[dict] = []
    selected_by_date: dict[str, list[dict]] = {}
    eligible_alpha_by_date: dict[str, list[dict]] = {}
    fixed_by_date: dict[str, list[dict]] = {}
    date_results: list[dict] = []
    latest_model: dict = {}
    skipped_dates = 0
    capacity_pressure_dates = 0
    ranking_changed_dates = 0

    for test_date in sorted(by_date):
        train = [
            row for row in resolved
            if str(row.get("boundary_exit_date") or "")[:10] < test_date
        ]
        model = _fit_model(train)
        if model.get("status") != "EVALUABLE":
            skipped_dates += 1
            continue
        latest_model = model

        day_rows: list[dict] = []
        for raw in by_date[test_date]:
            item = dict(raw)
            evidence = _evidence(item, model)
            item["thesis_quality"] = evidence
            item["thesis_quality_eligible"] = bool(evidence["eligible"])
            day_rows.append(item)

        selection_n = min(max(0, int(max_positions)), len(day_rows))
        if selection_n <= 0:
            continue

        baseline = sorted(day_rows, key=_selection_key_alpha, reverse=True)[:selection_n]
        fixed_score = sorted(day_rows, key=_selection_key_score, reverse=True)[:selection_n]
        eligible_pool = [row for row in day_rows if row.get("thesis_quality_eligible")]
        eligible = sorted(eligible_pool, key=_selection_key_score, reverse=True)[: max(0, int(max_positions))]
        eligible_alpha = sorted(eligible_pool, key=_selection_key_alpha, reverse=True)[: max(0, int(max_positions))]

        baseline_selected.extend(baseline)
        fixed_score_selected.extend(fixed_score)
        eligible_selected.extend(eligible)
        eligible_alpha_selected.extend(eligible_alpha)
        eligible_all_candidates.extend(eligible_pool)
        selected_by_date[test_date] = eligible
        eligible_alpha_by_date[test_date] = eligible_alpha
        fixed_by_date[test_date] = fixed_score

        if len(eligible_pool) > max(0, int(max_positions)):
            capacity_pressure_dates += 1
        thesis_keys = {
            (str(row.get("ticker") or ""), str(row.get("direction") or ""))
            for row in eligible
        }
        alpha_keys = {
            (str(row.get("ticker") or ""), str(row.get("direction") or ""))
            for row in eligible_alpha
        }
        if thesis_keys != alpha_keys:
            ranking_changed_dates += 1

        for row in day_rows:
            if str(row.get("boundary_status") or "").upper() != "RESOLVED" or _completion_label(row) is None:
                continue
            if row.get("thesis_quality_eligible"):
                eligible_candidates_evaluable.append(row)
            else:
                ineligible_candidates_evaluable.append(row)

        date_results.append({
            "date": test_date,
            "train_trades": len(train),
            "train_dates": len({str(row.get("entry_date") or "")[:10] for row in train}),
            "stable_numeric_features": len(model.get("numeric") or {}),
            "stable_categorical_dimensions": len(model.get("categorical") or {}),
            "candidate_count": len(day_rows),
            "resolved_candidate_count": sum(
                str(row.get("boundary_status") or "").upper() == "RESOLVED"
                for row in day_rows
            ),
            "eligible_candidate_count": len(eligible_pool),
            "selected_fixed_n": len(fixed_score),
            "selected_eligible": len(eligible),
            "selected_eligible_alpha_rank": len(eligible_alpha),
            "capacity_limited": len(eligible_pool) > max(0, int(max_positions)),
            "ranking_selection_changed": thesis_keys != alpha_keys,
            "unused_slots": max(0, int(max_positions) - len(eligible)),
            "abstained_completely": len(eligible) == 0,
        })

    baseline_metrics = _economic_metrics(baseline_selected)
    fixed_metrics = _economic_metrics(fixed_score_selected)
    eligible_metrics = _economic_metrics(eligible_selected)
    eligible_alpha_metrics = _economic_metrics(eligible_alpha_selected)
    eligible_uncapped_metrics = _economic_metrics(eligible_all_candidates)
    eligible_candidate_metrics = _economic_metrics(eligible_candidates_evaluable)
    ineligible_candidate_metrics = _economic_metrics(ineligible_candidates_evaluable)

    selected_thesis_keys = {
        (
            str(row.get("entry_date") or "")[:10],
            str(row.get("ticker") or ""),
            str(row.get("direction") or ""),
        )
        for row in eligible_selected
    }
    capacity_excluded_eligible = [
        row for row in eligible_all_candidates
        if (
            str(row.get("entry_date") or "")[:10],
            str(row.get("ticker") or ""),
            str(row.get("direction") or ""),
        ) not in selected_thesis_keys
    ]
    capacity_excluded_metrics = _economic_metrics(capacity_excluded_eligible)

    dates = sorted(selected_by_date)
    split = len(dates) // 2
    early = set(dates[:split])
    late = set(dates[split:])
    early_selected = [row for day in dates if day in early for row in selected_by_date.get(day, [])]
    late_selected = [row for day in dates if day in late for row in selected_by_date.get(day, [])]
    early_alpha_selected = [row for day in dates if day in early for row in eligible_alpha_by_date.get(day, [])]
    late_alpha_selected = [row for day in dates if day in late for row in eligible_alpha_by_date.get(day, [])]
    early_fixed = [row for day in dates if day in early for row in fixed_by_date.get(day, [])]
    late_fixed = [row for day in dates if day in late for row in fixed_by_date.get(day, [])]

    return {
        "mode": "THESIS_QUALITY_COMPLETION_WALK_FORWARD_SHADOW_V5_FULL_UNIVERSE_RANK_ABLATION",
        "status": "EVALUABLE" if date_results else "INSUFFICIENT_OOT_SAMPLE",
        "live_mutation": False,
        "source": "ALL_ACTIONABLE_FUSION_ENTER_CANONICAL_BOUNDARY_LIFECYCLE",
        "candidate_pool_total": len(all_candidates),
        "candidate_pool_resolved": len(resolved),
        "test_dates": len(date_results),
        "skipped_warmup_dates": skipped_dates,
        "dates_with_any_eligible": sum(1 for row in date_results if int(row.get("selected_eligible") or 0) > 0),
        "dates_abstained_completely": sum(bool(row.get("abstained_completely")) for row in date_results),
        "selected_trades": len(eligible_selected),
        "unused_slots_total": sum(int(row.get("unused_slots") or 0) for row in date_results),
        "avg_selected_per_test_date": round(len(eligible_selected) / len(date_results), 3) if date_results else None,
        "baseline_current_alpha_fixed_n": baseline_metrics,
        "thesis_score_fixed_n": fixed_metrics,
        "thesis_eligibility_variable_n": eligible_metrics,
        "eligibility_lift_vs_fixed_thesis_rank": _lift(eligible_metrics, fixed_metrics),
        "eligibility_lift_vs_current_alpha": _lift(eligible_metrics, baseline_metrics),
        "ranking_ablation": {
            "uncapped_eligible": eligible_uncapped_metrics,
            "top_n_by_thesis_score": eligible_metrics,
            "top_n_by_current_alpha": eligible_alpha_metrics,
            "thesis_minus_alpha": _lift(eligible_metrics, eligible_alpha_metrics),
            "capacity_excluded_by_thesis": capacity_excluded_metrics,
            "capacity_pressure_dates": capacity_pressure_dates,
            "ranking_selection_changed_dates": ranking_changed_dates,
            "ranking_selection_changed_pct": (
                round(100.0 * ranking_changed_dates / len(date_results), 2)
                if date_results else None
            ),
            "interpretation": "separates eligibility quality from the ranking used when eligible candidates exceed available capital slots",
        },
        "candidate_diagnostics": {
            "eligible": eligible_candidate_metrics,
            "ineligible": ineligible_candidate_metrics,
            "completion_rate_separation_pp": (
                round(
                    float(eligible_candidate_metrics["completion_rate_pct"])
                    - float(ineligible_candidate_metrics["completion_rate_pct"]),
                    4,
                )
                if eligible_candidate_metrics.get("completion_rate_pct") is not None
                and ineligible_candidate_metrics.get("completion_rate_pct") is not None
                else None
            ),
            "avg_return_separation_pp": (
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
                "dates": len(early),
                "fixed_n": _economic_metrics(early_fixed),
                "eligibility": _economic_metrics(early_selected),
                "eligibility_alpha_rank": _economic_metrics(early_alpha_selected),
                "thesis_vs_alpha_rank": _lift(_economic_metrics(early_selected), _economic_metrics(early_alpha_selected)),
                "lift": _lift(_economic_metrics(early_selected), _economic_metrics(early_fixed)),
            },
            "late_half": {
                "dates": len(late),
                "fixed_n": _economic_metrics(late_fixed),
                "eligibility": _economic_metrics(late_selected),
                "eligibility_alpha_rank": _economic_metrics(late_alpha_selected),
                "thesis_vs_alpha_rank": _lift(_economic_metrics(late_selected), _economic_metrics(late_alpha_selected)),
                "lift": _lift(_economic_metrics(late_selected), _economic_metrics(late_fixed)),
            },
        },
        "latest_train_model": latest_model,
        "methodology": {
            "training_target": "MOVEMENT_COMPLETED before THESIS_INVALIDATED",
            "training_labels_available_only_after_boundary_exit": True,
            "train_rows_must_resolve_before_test_entry": True,
            "test_selection_uses_full_candidate_pool_including_unresolved": True,
            "research_input_must_not_be_tail_truncated": True,
            "eligible_rank_ablation_is_point_in_time_only": True,
            "uncapped_eligible_is_diagnostic_not_a_portfolio": True,
            "future_outcome_used_only_for_evaluation": True,
            "payoff_geometry_features_excluded_from_learning": True,
            "richer_point_in_time_market_relational_features_used": True,
            "numeric_feature_direction_requires_chronological_half_consistency": True,
            "favorable_numeric_side_must_beat_half_train_base_rate_in_both_halves": True,
            "categorical_value_requires_same_support_or_oppose_state_in_both_halves": True,
            "eligibility_rule": "strict majority support with >=3 observed stable evidence dimensions",
            "unused_slots_allowed": True,
            "threshold_optimized_on_test": False,
            "live_mutation": False,
            "max_positions": int(max_positions),
            "purpose": "learn thesis quality rather than payoff geometry and test whether TMAD can abstain on low-completion candidates",
        },
        "date_results": date_results[-120:],
    }
