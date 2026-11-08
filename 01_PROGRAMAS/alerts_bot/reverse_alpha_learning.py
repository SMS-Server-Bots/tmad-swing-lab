from __future__ import annotations

"""Bridge Daily Reverse Alpha into Adaptive Learning as diagnostic evidence.

This module is intentionally advisory. Hindsight outcomes may explain where the
pipeline missed the day's best opportunities, but they must never directly
change LIVE execution, thresholds, or open/close decisions.
"""

from collections import Counter
from typing import Any


_REVERSE_PLAN_MAP = {
    "NO_CORE": "HIGH_COVERAGE",
    "WRONG_DIRECTION": "DIRECTIONAL_PRECISION",
    "RESEARCH_BLOCK": "ADAPTIVE_GATE",
    "GEX_BLOCK": "GEX_VALUE",
    "TIMING_NOT_READY": "EARLY_DETECTION",
    "FUSION_BLOCK": "ADAPTIVE_GATE",
    "ENTER_NOT_EMITTED": "ADAPTIVE_GATE",
}


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def extract_reverse_learning_evidence(audit: dict) -> dict:
    metrics = (audit or {}).get("metrics") or {}
    diagnostics = (audit or {}).get("diagnostics") or {}
    reverse = metrics.get("daily_reverse_alpha") or {}
    scorecard = metrics.get("daily_alpha_scorecard") or {}
    miss_reasons = {
        str(key): _i(value)
        for key, value in (reverse.get("miss_reasons") or {}).items()
        if str(key).upper() != "ALERTED"
    }
    dominant = "NONE"
    if miss_reasons:
        dominant = max(miss_reasons.items(), key=lambda item: (_i(item[1]), str(item[0])))[0]

    opportunities = list(diagnostics.get("daily_reverse_top_opportunities") or [])
    missed = [
        row for row in opportunities
        if not bool(row.get("alerted")) and str(row.get("miss_reason") or "").upper() != "ALERTED"
    ]
    missed.sort(
        key=lambda row: (
            _f(row.get("realized_return_pct"), 0.0) or 0.0,
            -_i(row.get("hindsight_rank"), 999),
        ),
        reverse=True,
    )
    top_missed = []
    for row in missed[:5]:
        top_missed.append({
            "date": row.get("date"),
            "horizon_sessions": row.get("horizon_sessions"),
            "ticker": row.get("ticker"),
            "direction": row.get("direction"),
            "realized_return_pct": row.get("realized_return_pct"),
            "hindsight_rank": row.get("hindsight_rank"),
            "miss_reason": row.get("miss_reason"),
            "tmad_path": row.get("tmad_path") or {},
            "ranks": row.get("ranks") or {},
            "point_in_time_features": row.get("point_in_time_features") or {},
            "core_snapshot": row.get("core_snapshot") or {},
            "gex_snapshot": row.get("gex_snapshot") or {},
        })

    scope = str(reverse.get("universe_scope") or "NO_MATURE_UNIVERSE")
    strategic = bool(reverse.get("strategic_conclusion_allowed"))
    evaluated = _i(reverse.get("evaluated_top_opportunities"))
    return {
        "mode": str(reverse.get("mode") or "DAILY_REVERSE_ALPHA_SHADOW"),
        "scope": scope,
        "strategic_conclusion_allowed": strategic,
        "advisory_only": True,
        "evaluated_top_opportunities": evaluated,
        "alert_recall_pct": _f(reverse.get("alert_recall_pct"), None),
        "eod_alpha_recall_pct": _f(reverse.get("eod_alpha_recall_pct"), None),
        "miss_reasons": miss_reasons,
        "dominant_miss": dominant,
        "by_horizon": dict(reverse.get("by_horizon") or {}),
        "missed_feature_profile": dict(reverse.get("missed_feature_profile") or {}),
        "top_missed_opportunities": top_missed,
        "largest_alpha_leak": scorecard.get("largest_alpha_leak"),
        "evaluable_for_research_direction": bool(strategic and evaluated > 0),
        "policy": {
            "may_steer_challenger_research": True,
            "may_change_live": False,
            "may_change_thresholds_directly": False,
            "may_trigger_trade": False,
            "future_data_is_label_only": True,
        },
    }


def reverse_specialist_plans(reverse_evidence: dict) -> list[str]:
    """Translate trusted reverse misses into research objectives only."""
    evidence = reverse_evidence or {}
    if not bool(evidence.get("evaluable_for_research_direction")):
        return []
    misses = evidence.get("miss_reasons") or {}
    ordered = sorted(
        ((str(reason), _i(count)) for reason, count in misses.items() if _i(count) > 0),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )
    plans: list[str] = []
    for reason, _count in ordered:
        plan = _REVERSE_PLAN_MAP.get(reason)
        if plan and plan not in plans:
            plans.append(plan)
    return plans[:3]
