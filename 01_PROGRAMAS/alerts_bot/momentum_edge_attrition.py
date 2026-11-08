from __future__ import annotations

"""Research-only waterfall for momentum edge attrition through TMAD stages.

The starting population is a fixed point-in-time Scanner momentum ranking using
the already-existing Swing V2 momentum-continuation formula. For each selected
Scanner run, Top-K candidates by absolute continuation strength are frozen
before outcomes. The audit then asks which of those same candidates survive,
in the same momentum direction, through CORE -> Research -> GEX -> Timing ->
FUSION -> actionable ENTER -> current Top-5 ENTER.

Future prices are used only for fixed 2/3/5-session evaluation. No gate is
changed and no stage is optimized on the historical outcomes.
"""

from collections import defaultdict
import math
from typing import Any

from momentum_direction_audit import (
    HORIZONS,
    _build_run_momentum,
    _future_return,
    _metrics,
)

PRIMARY_TOP_K = 5
SENSITIVITY_TOP_K = 10
TOP_KS = (PRIMARY_TOP_K, SENSITIVITY_TOP_K)
RESEARCH_STATES = {"DISCOVERED", "EARLY", "CAPPED", "READY", "RETEST", "ENTER"}
TIMING_STATES = {"READY", "RETEST", "ENTER"}
FUSION_STATES = {"READY", "RETEST", "ENTER"}

STAGES = (
    "SCANNER_MOMENTUM",
    "CORE",
    "RESEARCH",
    "GEX",
    "TIMING",
    "FUSION",
    "ENTER",
    "TOP5_ENTER",
)


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _day(row: dict) -> str:
    return str(
        row.get("snapshot_date")
        or row.get("signal_date")
        or row.get("observation_date")
        or ""
    )[:10]


def _ticker(row: dict) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").upper()


def _run_id(row: dict) -> int:
    try:
        return int(row.get("source_scanner_run_id") or row.get("scanner_run_id") or 0)
    except Exception:
        return 0


def _state(row: dict) -> str:
    return str(row.get("opportunity_state") or row.get("state") or "").upper()


def _direction(row: dict) -> str:
    return str(row.get("direction") or "").upper()


def _feature_entry_price(row: dict) -> float | None:
    price = _f(row.get("outcome_entry_price"))
    if price is not None and price > 0:
        return price
    price = _f(row.get("price"))
    if price is not None and price > 0:
        return price
    price = _f(row.get("current_price"))
    if price is not None and price > 0:
        return price
    payload = row.get("payload_json") or row.get("features_json")
    if isinstance(payload, dict):
        price = _f(payload.get("price"))
        if price is not None and price > 0:
            return price
    return None


def _feature_index(feature_rows: list[dict]) -> dict[tuple[str, int, str], dict]:
    out: dict[tuple[str, int, str], dict] = {}
    for row in feature_rows:
        key = (_day(row), _run_id(row), _ticker(row))
        if not key[0] or key[1] <= 0 or not key[2]:
            continue
        old = out.get(key)
        if old is None:
            out[key] = row
            continue
        old_obs = int(old.get("observation_id") or old.get("feature_observation_id") or old.get("id") or 0)
        new_obs = int(row.get("observation_id") or row.get("feature_observation_id") or row.get("id") or 0)
        if new_obs >= old_obs:
            out[key] = row
    return out


def _signal_index(signal_rows: list[dict]) -> dict[tuple[str, int, str], list[dict]]:
    out: dict[tuple[str, int, str], list[dict]] = defaultdict(list)
    for row in signal_rows:
        key = (_day(row), _run_id(row), _ticker(row))
        if key[0] and key[1] > 0 and key[2]:
            out[key].append(row)
    return out


def _top5_enter_keys(portfolios: list[dict]) -> set[tuple[str, int, str, str]]:
    keys: set[tuple[str, int, str, str]] = set()
    for portfolio in portfolios:
        day = str(portfolio.get("asof") or "")[:10]
        run_id = int(portfolio.get("source_scanner_run_id") or 0)
        for selected in portfolio.get("selected") or []:
            ticker = _ticker(selected)
            direction = _direction(selected)
            context = selected.get("swing_context") if isinstance(selected.get("swing_context"), dict) else {}
            state = str(selected.get("opportunity_state") or context.get("state") or "").upper()
            actionable = bool(context.get("actionable"))
            if day and run_id > 0 and ticker and direction in {"LONG", "SHORT"} and state == "ENTER" and actionable:
                keys.add((day, run_id, ticker, direction))
    return keys


def _same_direction_rows(rows: list[dict], direction: str, layer: str) -> list[dict]:
    return [
        row for row in rows
        if _direction(row) == direction and str(row.get("layer") or "").upper() == layer
    ]


def _stage_flags(
    candidate: dict,
    signals: list[dict],
    top5_enter_keys: set[tuple[str, int, str, str]],
) -> dict[str, bool]:
    direction = str(candidate["momentum_direction"]).upper()
    core_rows = _same_direction_rows(signals, direction, "CORE")
    gex_rows = _same_direction_rows(signals, direction, "GEX_DAILY")
    timing_rows = _same_direction_rows(signals, direction, "GEX_INTRADAY")
    fusion_rows = _same_direction_rows(signals, direction, "FUSION")

    core = bool(core_rows)
    research = core and any(_state(row) in RESEARCH_STATES for row in core_rows)
    gex = research and bool(gex_rows)
    timing = gex and any(_state(row) in TIMING_STATES for row in timing_rows)
    fusion = timing and any(
        bool(row.get("actionable")) or _state(row) in FUSION_STATES
        for row in fusion_rows
    )
    enter = fusion and any(
        bool(row.get("actionable")) and _state(row) == "ENTER"
        for row in fusion_rows
    )
    key = (
        candidate["signal_date"],
        int(candidate["source_scanner_run_id"]),
        candidate["ticker"],
        direction,
    )
    top5_enter = enter and key in top5_enter_keys

    return {
        "SCANNER_MOMENTUM": True,
        "CORE": core,
        "RESEARCH": research,
        "GEX": gex,
        "TIMING": timing,
        "FUSION": fusion,
        "ENTER": enter,
        "TOP5_ENTER": top5_enter,
    }


def _build_candidates(
    feature_rows: list[dict],
    signal_rows: list[dict],
    portfolios: list[dict],
    top_k: int,
) -> tuple[list[dict], dict]:
    momentum_index, scanner_context = _build_run_momentum(feature_rows)
    features = _feature_index(feature_rows)
    signals = _signal_index(signal_rows)
    top5_keys = _top5_enter_keys(portfolios)

    by_run: dict[tuple[str, int], list[tuple[str, dict]]] = defaultdict(list)
    for (day, run_id, ticker), momentum in momentum_index.items():
        by_run[(day, run_id)].append((ticker, momentum))

    candidates: list[dict] = []
    runs_with_candidates = 0
    missing_entry_price = 0
    for (day, run_id), rows in sorted(by_run.items()):
        ranked = sorted(
            rows,
            key=lambda item: (float(item[1].get("strength") or 0.0), item[0]),
            reverse=True,
        )[: int(top_k)]
        if not ranked:
            continue
        runs_with_candidates += 1
        for rank, (ticker, momentum) in enumerate(ranked, 1):
            feature = features.get((day, run_id, ticker)) or {}
            entry_price = _feature_entry_price(feature)
            if entry_price is None or entry_price <= 0:
                missing_entry_price += 1
                continue
            candidate = {
                "signal_date": day,
                "source_scanner_run_id": run_id,
                "ticker": ticker,
                "entry_price": float(entry_price),
                "momentum_direction": str(momentum.get("direction") or "").upper(),
                "momentum_strength": float(momentum.get("strength") or 0.0),
                "momentum_rank": rank,
                "momentum_features_available": int(momentum.get("features_available") or 0),
            }
            candidate["stages"] = _stage_flags(
                candidate,
                signals.get((day, run_id, ticker), []),
                top5_keys,
            )
            candidates.append(candidate)

    return candidates, {
        **scanner_context,
        "top_k": int(top_k),
        "runs_with_ranked_candidates": runs_with_candidates,
        "candidates_with_entry_price": len(candidates),
        "missing_entry_price": missing_entry_price,
    }


def _evaluate(
    rows: list[dict],
    trusted_price_series: dict[str, list[dict]],
    horizon: int,
) -> dict:
    values: list[tuple[str, float]] = []
    for row in rows:
        ret = _future_return(
            row,
            str(row.get("momentum_direction") or ""),
            trusted_price_series,
            horizon,
        )
        if ret is not None:
            values.append((str(row.get("signal_date") or "")[:10], float(ret)))
    result = _metrics(values)
    result["selected_total"] = len(rows)
    result["unmatured_or_unavailable"] = max(0, len(rows) - int(result.get("sample") or 0))
    return result


def _delta(current: dict, previous: dict) -> dict:
    curr_avg = _f(current.get("avg_return_pct"))
    prev_avg = _f(previous.get("avg_return_pct"))
    curr_pf = _f(current.get("profit_factor"))
    prev_pf = _f(previous.get("profit_factor"))
    return {
        "avg_return_change_pp": (
            round(float(curr_avg) - float(prev_avg), 4)
            if curr_avg is not None and prev_avg is not None else None
        ),
        "profit_factor_change": (
            round(float(curr_pf) - float(prev_pf), 4)
            if curr_pf is not None and prev_pf is not None else None
        ),
    }


def _panel(
    candidates: list[dict],
    trusted_price_series: dict[str, list[dict]],
    horizon: int,
) -> dict:
    stage_rows: dict[str, list[dict]] = {}
    previous_rows = list(candidates)
    stage_rows["SCANNER_MOMENTUM"] = previous_rows
    for stage in STAGES[1:]:
        current_rows = [row for row in previous_rows if bool((row.get("stages") or {}).get(stage))]
        stage_rows[stage] = current_rows
        previous_rows = current_rows

    stages: dict[str, dict] = {}
    previous_stage = None
    for stage in STAGES:
        rows = stage_rows[stage]
        metrics = _evaluate(rows, trusted_price_series, horizon)
        block = {
            "metrics": metrics,
            "retained_from_prior": len(rows),
            "retention_vs_scanner_pct": (
                round(100.0 * len(rows) / len(candidates), 2) if candidates else None
            ),
        }
        if previous_stage is not None:
            prior_rows = stage_rows[previous_stage]
            prior_keys = {
                (r["signal_date"], r["source_scanner_run_id"], r["ticker"])
                for r in prior_rows
            }
            kept_keys = {
                (r["signal_date"], r["source_scanner_run_id"], r["ticker"])
                for r in rows
            }
            rejected = [
                r for r in prior_rows
                if (r["signal_date"], r["source_scanner_run_id"], r["ticker"]) in (prior_keys - kept_keys)
            ]
            prior_metrics = stages[previous_stage]["metrics"]
            rejected_metrics = _evaluate(rejected, trusted_price_series, horizon)
            block["retention_vs_prior_pct"] = (
                round(100.0 * len(rows) / len(prior_rows), 2) if prior_rows else None
            )
            block["rejected_at_stage"] = rejected_metrics
            block["delta_vs_prior"] = _delta(metrics, prior_metrics)
            survivor_avg = _f(metrics.get("avg_return_pct"))
            rejected_avg = _f(rejected_metrics.get("avg_return_pct"))
            block["gate_diagnostic"] = {
                "survivor_better_than_prior": (
                    _f(block["delta_vs_prior"].get("avg_return_change_pp")) is not None
                    and float(block["delta_vs_prior"]["avg_return_change_pp"]) > 0.0
                ),
                "rejected_better_than_survivors": (
                    survivor_avg is not None
                    and rejected_avg is not None
                    and float(rejected_avg) > float(survivor_avg)
                ),
            }
        stages[stage] = block
        previous_stage = stage

    return {"horizon_sessions": horizon, "stages": stages}


def _harm_summary(by_horizon: dict[str, dict]) -> dict:
    result: dict[str, dict] = {}
    for stage in STAGES[1:]:
        harmful = []
        beneficial = []
        for horizon in HORIZONS:
            block = (((by_horizon.get(str(horizon)) or {}).get("stages") or {}).get(stage) or {})
            diagnostic = block.get("gate_diagnostic") or {}
            delta = _f((block.get("delta_vs_prior") or {}).get("avg_return_change_pp"))
            rejected_better = bool(diagnostic.get("rejected_better_than_survivors"))
            if delta is not None and delta < 0.0 and rejected_better:
                harmful.append(horizon)
            if delta is not None and delta > 0.0 and not rejected_better:
                beneficial.append(horizon)
        result[stage] = {
            "harmful_horizons": harmful,
            "harmful_horizon_count": len(harmful),
            "beneficial_horizons": beneficial,
            "beneficial_horizon_count": len(beneficial),
        }
    return result


def _chronological_stability(
    candidates: list[dict],
    trusted_price_series: dict[str, list[dict]],
) -> dict:
    dates = sorted({str(row.get("signal_date") or "")[:10] for row in candidates})
    if not dates:
        return {}
    split = max(1, len(dates) // 2)
    early = set(dates[:split])
    late = set(dates[split:])
    out = {}
    for label, allowed in (("early_half", early), ("late_half", late)):
        subset = [row for row in candidates if str(row.get("signal_date") or "")[:10] in allowed]
        out[label] = {
            "dates": len(allowed),
            "scanner": {
                str(h): _evaluate(subset, trusted_price_series, h)
                for h in HORIZONS
            },
            "enter": {
                str(h): _evaluate(
                    [row for row in subset if bool((row.get("stages") or {}).get("ENTER"))],
                    trusted_price_series,
                    h,
                )
                for h in HORIZONS
            },
        }
    return out


def build_momentum_edge_attrition_audit(
    feature_rows: list[dict],
    signal_rows: list[dict],
    current_portfolios: list[dict],
    trusted_price_series: dict[str, list[dict]],
) -> dict:
    topk_results = {}
    for top_k in TOP_KS:
        candidates, context = _build_candidates(
            feature_rows,
            signal_rows,
            current_portfolios,
            top_k,
        )
        by_horizon = {
            str(horizon): _panel(candidates, trusted_price_series, horizon)
            for horizon in HORIZONS
        }
        topk_results[str(top_k)] = {
            "context": context,
            "candidate_count": len(candidates),
            "by_horizon": by_horizon,
            "gate_harm_summary": _harm_summary(by_horizon),
            "chronological_stability": _chronological_stability(candidates, trusted_price_series),
        }

    primary = topk_results[str(PRIMARY_TOP_K)]
    harmful_multi = [
        stage
        for stage, block in (primary.get("gate_harm_summary") or {}).items()
        if int(block.get("harmful_horizon_count") or 0) >= 2
    ]
    return {
        "mode": "MOMENTUM_EDGE_ATTRITION_WATERFALL_V7",
        "status": "EVALUABLE" if int(primary.get("candidate_count") or 0) > 0 else "INSUFFICIENT_POINT_IN_TIME_COVERAGE",
        "live_mutation": False,
        "promotion_allowed": False,
        "unbiased_oos_proof": False,
        "primary_top_k": PRIMARY_TOP_K,
        "sensitivity_top_k": SENSITIVITY_TOP_K,
        "top_k_results": topk_results,
        "primary_harmful_stages_across_2plus_horizons": harmful_multi,
        "methodology": {
            "starting_rule_preexisted_in_swing_v2": True,
            "starting_population": "same-run Scanner Top-K by absolute momentum-continuation strength",
            "momentum_formula": "mean same-run percentiles of momentum_score, scanner_trend_score, flow_direction, rvol",
            "direction_frozen_before_outcome": True,
            "stage_sequence": list(STAGES),
            "cumulative_survival": True,
            "rejected_bucket_reported_at_each_stage": True,
            "future_prices_used_only_for_evaluation": True,
            "fixed_horizon_outcomes": list(HORIZONS),
            "no_label_fitting": True,
            "no_threshold_tuning": True,
            "top5_primary_matches_product_goal": True,
            "top10_is_sensitivity_only": True,
            "live_mutation": False,
            "interpretation": (
                "descriptive attribution of where the pre-existing Scanner momentum edge is retained or lost; "
                "not sufficient for LIVE changes because current-policy stages are retrospectively replayed"
            ),
        },
    }


def _selection_overlap_with_canonical(
    candidates: list[dict],
    canonical_selections: list[dict],
    allowed_dates: set[str],
) -> dict:
    point_keys = {
        (
            str(row.get("signal_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
            str(row.get("momentum_direction") or "").upper(),
        )
        for row in candidates
        if str(row.get("signal_date") or "")[:10] in allowed_dates
    }
    canonical_keys = {
        (
            str(row.get("date") or "")[:10],
            str(row.get("ticker") or "").upper(),
            str(row.get("direction") or "").upper(),
        )
        for row in canonical_selections
        if str(row.get("date") or "")[:10] in allowed_dates
    }
    overlap = point_keys & canonical_keys
    identity_overlap = {
        (day, ticker)
        for day, ticker, _direction in point_keys
    } & {
        (day, ticker)
        for day, ticker, _direction in canonical_keys
    }
    denominator = min(len(point_keys), len(canonical_keys))
    return {
        "point_in_time_selected": len(point_keys),
        "canonical_selected": len(canonical_keys),
        "same_ticker_and_direction": len(overlap),
        "same_ticker_regardless_of_direction": len(identity_overlap),
        "same_ticker_and_direction_pct_vs_smaller": (
            round(100.0 * len(overlap) / denominator, 2)
            if denominator else None
        ),
        "same_ticker_pct_vs_smaller": (
            round(100.0 * len(identity_overlap) / denominator, 2)
            if denominator else None
        ),
    }


def _metric_delta(left: dict, right: dict) -> dict:
    left_avg = _f(left.get("avg_return_pct"))
    right_avg = _f(right.get("avg_return_pct"))
    left_pf = _f(left.get("profit_factor"))
    right_pf = _f(right.get("profit_factor"))
    return {
        "avg_return_pp": (
            round(float(right_avg) - float(left_avg), 4)
            if left_avg is not None and right_avg is not None else None
        ),
        "profit_factor": (
            round(float(right_pf) - float(left_pf), 4)
            if left_pf is not None and right_pf is not None else None
        ),
    }


def build_trusted_momentum_reconciliation(
    feature_rows: list[dict],
    signal_rows: list[dict],
    current_portfolios: list[dict],
    trusted_price_series: dict[str, list[dict]],
    canonical_reference: dict,
    *,
    waterfall_reference: dict | None = None,
    lane_label: str = "ROLLING_LATEST",
) -> dict:
    """Evaluate the exact Scanner Top-5 with the same price lane as the waterfall.

    This is the only point-in-time side of momentum reconciliation from v6.2.16
    onward. It intentionally reuses _build_candidates() and _evaluate(), so the
    all-date reference must reproduce SCANNER_MOMENTUM from the waterfall.
    """
    candidates, context = _build_candidates(
        feature_rows,
        signal_rows,
        current_portfolios,
        PRIMARY_TOP_K,
    )
    by_horizon: dict[str, dict] = {}
    waterfall_passes: list[bool] = []
    same_day_samples: list[int] = []

    for horizon in HORIZONS:
        canonical_panel = (
            (canonical_reference.get("by_horizon") or {}).get(str(horizon))
            or {}
        )
        oot_dates = {
            str(value)[:10]
            for value in (canonical_panel.get("canonical_oot_dates") or [])
            if str(value)[:10]
        }
        same_days = [
            row for row in candidates
            if str(row.get("signal_date") or "")[:10] in oot_dates
        ]
        all_metrics = _evaluate(candidates, trusted_price_series, horizon)
        same_day_metrics = _evaluate(same_days, trusted_price_series, horizon)
        same_day_samples.append(int(same_day_metrics.get("sample") or 0))
        canonical_metrics = canonical_panel.get("canonical_oot") or {}
        fixed_canonical_metrics = (
            (canonical_panel.get("fixed_benchmark") or {}).get("metrics")
            or {}
        )
        overlap = _selection_overlap_with_canonical(
            same_days,
            canonical_panel.get("canonical_oot_selections") or [],
            oot_dates,
        )

        waterfall_metrics = (
            (((((waterfall_reference or {}).get("top_k_results") or {}).get(str(PRIMARY_TOP_K)) or {})
              .get("by_horizon") or {}).get(str(horizon)) or {})
             .get("stages") or {}
        ).get("SCANNER_MOMENTUM", {}).get("metrics", {})
        sample_delta = int(all_metrics.get("sample") or 0) - int(waterfall_metrics.get("sample") or 0)
        avg_diff = None
        pf_diff = None
        if waterfall_metrics:
            a = _f(all_metrics.get("avg_return_pct"))
            b = _f(waterfall_metrics.get("avg_return_pct"))
            if a is not None and b is not None:
                avg_diff = round(float(a) - float(b), 6)
            a = _f(all_metrics.get("profit_factor"))
            b = _f(waterfall_metrics.get("profit_factor"))
            if a is not None and b is not None:
                pf_diff = round(float(a) - float(b), 6)
        waterfall_pass = bool(
            waterfall_metrics
            and sample_delta == 0
            and avg_diff is not None
            and abs(float(avg_diff)) <= 0.0002
            and (pf_diff is None or abs(float(pf_diff)) <= 0.0002)
        )
        if waterfall_reference is not None:
            waterfall_passes.append(waterfall_pass)

        by_horizon[str(horizon)] = {
            "canonical_oot": canonical_metrics,
            "trusted_point_in_time_same_oot_days": same_day_metrics,
            "trusted_point_in_time_all_selected_runs": all_metrics,
            "fixed_canonical_benchmark": fixed_canonical_metrics,
            "canonical_minus_point_in_time_same_days": _metric_delta(
                same_day_metrics,
                canonical_metrics,
            ),
            "fixed_canonical_minus_point_in_time_all_runs": (
                _metric_delta(all_metrics, fixed_canonical_metrics)
                if str(lane_label or "").upper() == "FIXED_BENCHMARK"
                and fixed_canonical_metrics
                else {}
            ),
            "selection_overlap_same_oot_days": overlap,
            "oot_dates_requested": len(oot_dates),
            "waterfall_reference_check": {
                "pass": waterfall_pass,
                "sample_delta": sample_delta,
                "avg_return_diff_pp": avg_diff,
                "profit_factor_diff": pf_diff,
                "waterfall_scanner": waterfall_metrics,
            },
        }

    waterfall_ok = (
        bool(waterfall_passes and all(waterfall_passes))
        if waterfall_reference is not None
        else None
    )
    return {
        "mode": "TRUSTED_MOMENTUM_RECONCILIATION_V12",
        "lane": str(lane_label or "ROLLING_LATEST").upper(),
        "status": (
            "EVALUABLE"
            if candidates and all(value > 0 for value in same_day_samples)
            and (waterfall_ok is not False)
            else "WATERFALL_REFERENCE_MISMATCH"
            if waterfall_ok is False
            else "INSUFFICIENT_SAME_DAY_TRUSTED_OUTCOMES"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "candidate_context": context,
        "candidate_count": len(candidates),
        "waterfall_reference_reproduced_all_horizons": waterfall_ok,
        "by_horizon": by_horizon,
        "methodology": {
            "candidate_builder_identical_to_momentum_waterfall": True,
            "outcome_evaluator_identical_to_momentum_waterfall": True,
            "trusted_price_lane_supplied_by_historical_operational_validation": True,
            "same_oot_dates_from_canonical_reference": True,
            "future_prices_used_only_for_evaluation": True,
            "no_live_rule_change": True,
        },
    }
