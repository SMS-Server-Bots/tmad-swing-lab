from __future__ import annotations

"""Canonical side of TMAD momentum reconciliation.

v11 deliberately stops trying to reconstruct point-in-time outcomes from
observation_outcomes. Canonical EOD evidence is produced here; the point-in-time
Scanner side is evaluated later by historical operational validation using the
same trusted price lane as the existing momentum waterfall.

This module also evaluates the immutable economic benchmark when available.
"""

from collections import defaultdict
import math
from statistics import mean
from typing import Any

from swing_v2 import (
    BASELINE_FEATURES,
    HORIZONS,
    MAX_POSITIONS,
    _canonical_rows,
    _cross_section_percentiles,
    _day,
    _metrics,
    _row_return,
    _ticker,
)

MODE = "MOMENTUM_RECONCILIATION_V11"


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _obs_id(row: dict) -> int:
    try:
        return int(row.get("observation_id") or row.get("feature_observation_id") or row.get("id") or 0)
    except Exception:
        return 0


def _rows_by_day(rows: list[dict], horizon: int) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if int(row.get("horizon_sessions") or 0) != int(horizon):
            continue
        if _f(row.get("return_long_pct")) is None:
            continue
        day = _day(row)
        ticker = _ticker(row)
        if day and ticker:
            grouped[day].append(row)
    return dict(grouped)


def _oot_dates(panel: dict, available_dates: set[str]) -> list[str]:
    selected: set[str] = set()
    for fold in panel.get("fold_details") or []:
        start = str(fold.get("test_start") or "")[:10]
        end = str(fold.get("test_end") or "")[:10]
        if start and end:
            selected.update(day for day in available_dates if start <= day <= end)
    return sorted(selected)


def _rank_day(rows: list[dict], limit: int = MAX_POSITIONS) -> list[dict]:
    percentile_maps = {
        feature: _cross_section_percentiles(rows, feature)
        for feature in BASELINE_FEATURES
    }
    ranked: list[tuple[float, float, dict]] = []
    for row in rows:
        ticker = _ticker(row)
        values = [mapping[ticker] for mapping in percentile_maps.values() if ticker in mapping]
        if not values:
            continue
        continuation = mean(values) - 0.5
        ranked.append((abs(continuation), continuation, row))
    ranked.sort(key=lambda item: (item[0], _ticker(item[2])), reverse=True)
    return [
        {
            "row": row,
            "date": _day(row),
            "ticker": _ticker(row),
            "direction": "LONG" if continuation >= 0.0 else "SHORT",
            "strength": float(strength),
            "observation_id": _obs_id(row),
        }
        for strength, continuation, row in ranked[: max(0, int(limit))]
    ]


def _aggregate(
    rows_by_day: dict[str, list[dict]],
    dates: list[str] | set[str],
) -> tuple[dict, list[dict]]:
    scored: list[dict] = []
    identities: list[dict] = []
    requested_dates = sorted(set(dates))
    for day in requested_dates:
        selected = _rank_day(rows_by_day.get(day) or [])
        for item in selected:
            ret = _row_return(item["row"], item["direction"])
            if ret is None:
                continue
            scored.append({
                "date": day,
                "ticker": item["ticker"],
                "direction": item["direction"],
                "return_pct": float(ret),
            })
            identities.append({
                "date": day,
                "ticker": item["ticker"],
                "direction": item["direction"],
                "observation_id": int(item["observation_id"]),
                "strength": round(float(item["strength"]), 8),
            })
    metrics = _metrics(scored)
    metrics["requested_dates"] = len(requested_dates)
    metrics["dates_with_selection"] = len({row["date"] for row in identities})
    metrics["selected_total"] = len(identities)
    return metrics, identities


def _reference_check(observed: dict, reference: dict) -> dict:
    avg_obs, avg_ref = _f(observed.get("avg_return_pct")), _f(reference.get("avg_return_pct"))
    pf_obs, pf_ref = _f(observed.get("profit_factor")), _f(reference.get("profit_factor"))
    sample_match = int(observed.get("sample") or 0) == int(reference.get("sample") or 0)
    avg_diff = abs(float(avg_obs) - float(avg_ref)) if avg_obs is not None and avg_ref is not None else None
    pf_diff = abs(float(pf_obs) - float(pf_ref)) if pf_obs is not None and pf_ref is not None else None
    passed = bool(
        sample_match
        and avg_diff is not None
        and avg_diff <= 0.0002
        and (pf_diff is None or pf_diff <= 0.0002)
    )
    return {
        "sample_match": sample_match,
        "avg_abs_diff_pp": round(avg_diff, 6) if avg_diff is not None else None,
        "pf_abs_diff": round(pf_diff, 6) if pf_diff is not None else None,
        "pass": passed,
    }


def _benchmark_panel(feature_rows: list[dict], benchmark: dict, horizon: int) -> dict:
    ids = {
        int(value)
        for value in (
            (benchmark.get("canonical_observation_ids_by_horizon") or {}).get(str(horizon))
            or []
        )
    }
    if not ids:
        return {
            "status": "BENCHMARK_NOT_AVAILABLE",
            "expected_rows": 0,
            "matched_rows": 0,
            "row_coverage_pct": None,
            "metrics": _metrics([]),
        }
    rows = [
        row for row in feature_rows
        if int(row.get("horizon_sessions") or 0) == int(horizon)
        and _obs_id(row) in ids
    ]
    rows_by_day = _rows_by_day(rows, horizon)
    dates = sorted(rows_by_day)
    metrics, identities = _aggregate(rows_by_day, dates)
    coverage = round(100.0 * len(rows) / len(ids), 3) if ids else None
    return {
        "status": "EVALUABLE" if rows else "MISSING_BENCHMARK_ROWS",
        "expected_rows": len(ids),
        "matched_rows": len(rows),
        "row_coverage_pct": coverage,
        "dates": len(dates),
        "metrics": metrics,
        "selection_identity_sha_input": [
            [row["date"], row["ticker"], row["direction"], row["observation_id"]]
            for row in identities
        ],
    }


def build_momentum_reconciliation(
    feature_rows: list[dict],
    swing_v2_payload: dict,
    research_benchmark: dict | None = None,
) -> dict:
    canonical = _canonical_rows(feature_rows)
    by_horizon: dict[str, dict] = {}
    reference_passes: list[bool] = []
    benchmark_coverage_passes: list[bool] = []

    for horizon in HORIZONS:
        panel = (swing_v2_payload.get("by_horizon") or {}).get(str(horizon)) or {}
        canonical_rows = [
            row for row in canonical
            if int(row.get("horizon_sessions") or 0) == int(horizon)
        ]
        rows_by_day = _rows_by_day(canonical_rows, horizon)
        test_dates = _oot_dates(panel, set(rows_by_day))
        canonical_oot, identities = _aggregate(rows_by_day, test_dates)
        slot_ref = (
            ((panel.get("momentum_baseline_slot_audit_v8") or {}).get("all_test_days_fixed_top5"))
            or {}
        )
        ref_check = _reference_check(canonical_oot, slot_ref)
        reference_passes.append(bool(ref_check.get("pass")))

        benchmark_panel = (
            _benchmark_panel(feature_rows, research_benchmark or {}, horizon)
            if research_benchmark
            else {
                "status": "BENCHMARK_NOT_AVAILABLE",
                "expected_rows": 0,
                "matched_rows": 0,
                "row_coverage_pct": None,
                "metrics": _metrics([]),
            }
        )
        if research_benchmark:
            benchmark_coverage_passes.append(
                benchmark_panel.get("status") == "EVALUABLE"
                and float(benchmark_panel.get("row_coverage_pct") or 0.0) >= 99.999
            )

        by_horizon[str(horizon)] = {
            "canonical_oot": canonical_oot,
            "canonical_oot_dates": test_dates,
            "canonical_oot_selections": identities,
            "reference_check_vs_slots_v8": ref_check,
            "fixed_benchmark": benchmark_panel,
        }

    reference_ok = bool(reference_passes and all(reference_passes))
    benchmark_ok = (
        bool(benchmark_coverage_passes and all(benchmark_coverage_passes))
        if research_benchmark
        else False
    )
    status = "EVALUABLE" if reference_ok else "REFERENCE_MISMATCH"

    return {
        "mode": MODE,
        "status": status,
        "live_mutation": False,
        "promotion_allowed": False,
        "unbiased_oos_proof": False,
        "canonical_rows": len(canonical),
        "canonical_dates": len({_day(row) for row in canonical}),
        "by_horizon": by_horizon,
        "reference_reproduced_all_horizons": reference_ok,
        "fixed_benchmark_available": bool(research_benchmark),
        "fixed_benchmark_full_row_coverage_all_horizons": benchmark_ok,
        "fixed_benchmark_id": (research_benchmark or {}).get("benchmark_id"),
        "point_in_time_lane": "DEFERRED_TO_OPERATIONAL_TRUSTED_PRICE_RECONCILIATION",
        "decision_contract": {
            "canonical_and_point_in_time_lanes_are_separate": True,
            "point_in_time_outcomes_must_use_same_trusted_price_lane_as_waterfall": True,
            "fixed_benchmark_membership_never_rolls_forward": True,
            "rolling_latest_remains_separate": True,
            "future_returns_are_labels_only": True,
            "no_threshold_tuning": True,
            "no_live_rule_change": True,
        },
    }
