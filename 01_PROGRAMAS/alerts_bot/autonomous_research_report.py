from __future__ import annotations

"""Compact observability for the TMAD autonomous research tournament.

This module is deliberately report-only. It never mutates strategy, configuration,
runtime thresholds or experiment selection.
"""

from datetime import datetime
from typing import Any


def _metrics(view: dict) -> dict:
    challenger = (view or {}).get("challenger") or {}
    control = (view or {}).get("control") or {}
    lift = (view or {}).get("lift") or {}
    gate = (view or {}).get("gate") or {}
    return {
        "sample": challenger.get("sample"),
        "precision_pct": challenger.get("precision_pct"),
        "precision_lift_pp": lift.get("precision_pp"),
        "avg_return_pct": challenger.get("avg_horizon_close_return_pct"),
        "return_lift_pp": lift.get("return_pp"),
        "profit_factor": challenger.get("horizon_close_profit_factor"),
        "profit_factor_lift": lift.get("pf"),
        "avg_mae_pct": challenger.get("avg_mae_pct"),
        "control_precision_pct": control.get("precision_pct"),
        "control_avg_return_pct": control.get("avg_horizon_close_return_pct"),
        "control_profit_factor": control.get("horizon_close_profit_factor"),
        "gate_pass": bool(gate.get("pass")),
    }


def _leaderboard(rows: list[dict], limit: int = 8) -> list[dict]:
    out = []
    for row in list(rows or [])[: max(1, int(limit))]:
        out.append({
            "rank": row.get("rank"),
            "candidate_name": row.get("candidate_name"),
            "generation": row.get("generation"),
            "parent": row.get("parent"),
            "stable_train_validation": bool(row.get("stable_train_validation")),
            "spec": row.get("spec") or {},
            "train": _metrics(row.get("train") or {}),
            "validation": _metrics(row.get("validation") or {}),
        })
    return out


def _lane(payload: dict) -> dict:
    payload = payload or {}
    return {
        "status": payload.get("status"),
        "dataset_fingerprint": payload.get("dataset_fingerprint"),
        "candidate_count": int(payload.get("candidate_count") or 0),
        "generation_counts": payload.get("generation_counts") or {},
        "stable_train_validation_count": int(payload.get("stable_train_validation_count") or 0),
        "stability_survivor_count": int(payload.get("stability_survivor_count") or 0),
        "selected_candidate": payload.get("selected_candidate"),
        "selected_spec": payload.get("selected_spec") or {},
        "selected_features": payload.get("selected_features") or {},
        "selected_stability": payload.get("selected_stability") or {},
        "critic": payload.get("critic") or {},
        "screen_survivor": bool(payload.get("screen_survivor")),
        "final_test_status": payload.get("final_test_status"),
        "final_test": _metrics(payload.get("final_test") or {}),
        "historical_followup_candidate": bool(payload.get("historical_followup_candidate")),
        "performance": payload.get("performance") or {},
        "registry": payload.get("registry") or {},
        "leaderboard": _leaderboard(payload.get("leaderboard") or []),
    }


def build_autonomous_research_summary(core_context_payload: dict, *, producer_version: str) -> dict:
    core_context_payload = core_context_payload or {}
    rolling_context = core_context_payload.get("rolling_latest") or {}
    fixed_context = core_context_payload.get("fixed_benchmark") or {}
    rolling = rolling_context.get("autonomous_research") or {}
    fixed = fixed_context.get("autonomous_research") or {}
    decision = ((core_context_payload.get("decision") or {}).get("autonomous_research") or {})
    return {
        "schema_version": 1,
        "mode": "AUTONOMOUS_RESEARCH_SUMMARY_V1",
        "producer_version": str(producer_version),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rolling_latest": {
            **_lane(rolling),
            "fast_research_cache": rolling_context.get("fast_research_cache") or {},
        },
        "fixed_benchmark": {
            **_lane(fixed),
            "fast_research_cache": fixed_context.get("fast_research_cache") or {},
        },
        "decision": {
            "status": decision.get("status"),
            "rolling_selected_candidate": decision.get("rolling_selected_candidate"),
            "fixed_selected_candidate": decision.get("fixed_selected_candidate"),
            "same_candidate_across_lanes": bool(
                decision.get("same_candidate_across_lanes")
                or decision.get("same_individual_winner_across_lanes")
            ),
            "dual_lane_consensus_candidate": decision.get("dual_lane_consensus_candidate"),
            "dual_lane_consensus": decision.get("dual_lane_consensus") or {},
            "dual_lane_screen_survivor": bool(decision.get("dual_lane_screen_survivor")),
            "rolling_final_pass": bool(decision.get("rolling_final_pass")),
            "fixed_final_pass": bool(decision.get("fixed_final_pass")),
            "dual_lane_survivor": bool(decision.get("dual_lane_survivor")),
            "next_step": decision.get("next_step"),
            "promotion_allowed": False,
            "live_mutation": False,
        },
        "policy": {
            "summary_only": True,
            "source_code_self_mutation": False,
            "live_mutation": False,
            "human_gate_required": True,
        },
    }
