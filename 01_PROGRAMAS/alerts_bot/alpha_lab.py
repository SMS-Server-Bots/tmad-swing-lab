from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def _f(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _tier(sample: int) -> str:
    if sample <= 0:
        return "UNSEEN"
    if sample <= 2:
        return "EXPERIMENTAL"
    if sample <= 7:
        return "EMERGING"
    if sample <= 19:
        return "SUPPORTED"
    return "VALIDATED"


def _confidence(sample: int, *, severity: float = 1.0) -> float:
    # Deliberately continuous: Alpha Lab can act in SHADOW from the first
    # observation without pretending the evidence is already production-grade.
    base = min(1.0, max(0.0, sample) / 12.0)
    return round(100.0 * min(1.0, (0.15 if sample else 0.0) + 0.85 * base) * max(0.25, min(1.0, severity)), 2)


def _hypothesis(
    key: str,
    sample: int,
    *,
    description: str,
    experiment: str,
    live_action: str = "HOLD",
    severity: float = 1.0,
    evidence: dict | None = None,
) -> dict:
    return {
        "key": key,
        "status": _tier(sample),
        "evidence_count": int(sample),
        "confidence_pct": _confidence(sample, severity=severity),
        "description": description,
        "shadow_experiment": experiment,
        "live_action": live_action,
        "evidence": evidence or {},
    }


def build_alpha_lab_snapshot(
    audit: dict,
    *,
    regime: dict | None = None,
    current_research_gate: float = 82.0,
    research_gate_min: float = 80.0,
    research_gate_max: float = 86.0,
) -> dict:
    metrics = audit.get("metrics") or {}
    funnel = metrics.get("funnel") or {}
    gex = metrics.get("gex_value") or {}
    anticipation = metrics.get("anticipation") or {}
    diagnostics = audit.get("diagnostics") or {}
    classes = {str(k): _i(v) for k, v in (diagnostics.get("classifications") or {}).items()}

    movements = _i(funnel.get("movements"))
    hypotheses: list[dict] = []

    wrong_direction = _i(classes.get("WRONG_DIRECTION"))
    if wrong_direction:
        hypotheses.append(_hypothesis(
            "directional_precision",
            wrong_direction,
            description="CORE/feature stack is finding names but direction can be wrong.",
            experiment="score both observed and opposite direction in SHADOW; attribute which feature block discriminates direction by regime",
            severity=min(1.0, wrong_direction / max(1.0, float(movements or wrong_direction))),
            evidence={"wrong_direction": wrong_direction, "movements": movements},
        ))

    detected_late = _i(classes.get("DETECTED_LATE"))
    if detected_late:
        hypotheses.append(_hypothesis(
            "earlier_detection",
            detected_late,
            description="The setup is visible, but at least some moves are being detected too late.",
            experiment="run a permissive SHADOW early-entry lane using pre-gate candidates and measure lead-time/MFE/MAE versus normal timing",
            severity=min(1.0, detected_late / max(1.0, float(movements or detected_late))),
            evidence={
                "detected_late": detected_late,
                "median_lead_sessions": _f(anticipation.get("median_lead_sessions"), None),
                "late_pct": _f(anticipation.get("late_pct"), None),
            },
        ))

    no_core = _i(classes.get("NO_CORE_DIRECTION")) + _i(classes.get("NO_CORE")) + _i(classes.get("NOT_SELECTED"))
    if no_core:
        hypotheses.append(_hypothesis(
            "discovery_recall",
            no_core,
            description="Relevant moves are being missed before or at CORE discovery.",
            experiment="retain missed movers as next-session SHADOW positives and compare their pre-hit Scanner/SELECTIVE ranks against selected names",
            evidence={"pre_core_misses": no_core},
        ))

    below_research = _i(classes.get("BELOW_RESEARCH_GATE")) + _i(classes.get("RESEARCH_BLOCK"))
    if below_research:
        hypotheses.append(_hypothesis(
            "research_gate_coverage",
            below_research,
            description="Research gate may be suppressing otherwise viable setups.",
            experiment="parallel SHADOW lane at a slightly lower research gate; compare expectancy and false-positive cost",
            evidence={"research_blocks": below_research},
        ))

    gex_blocked = _i(gex.get("blocked_sample"))
    good_blocked = _i(gex.get("good_opportunities_blocked"))
    if gex_blocked:
        hypotheses.append(_hypothesis(
            "gex_filter_value",
            gex_blocked,
            description="GEX is actively filtering candidates; measure both protection and opportunity cost.",
            experiment="counterfactual SHADOW bypass of GEX-only blocks while preserving all other gates",
            severity=min(1.0, max(1.0, good_blocked) / max(1.0, float(gex_blocked))),
            evidence={
                "blocked_sample": gex_blocked,
                "blocked_correct": _i(gex.get("blocked_correct")),
                "good_opportunities_blocked": good_blocked,
                "blocking_raw_precision": _f(gex.get("blocking_raw_precision"), None),
                "lift_pp": _f(gex.get("lift_pp"), None),
            },
        ))

    timing_ready = _i(funnel.get("timing_ready"))
    fusion_ready = _i(funnel.get("fusion_ready"))
    enter_generated = _i(funnel.get("enter_generated"))
    fusion_losses = max(0, timing_ready - fusion_ready)
    if fusion_losses:
        hypotheses.append(_hypothesis(
            "fusion_opportunity_cost",
            fusion_losses,
            description="Timing-ready ideas are being removed before FUSION readiness.",
            experiment="counterfactual SHADOW lane that records outcomes for timing-ready/FUSION-blocked setups without sending LIVE entries",
            evidence={"timing_ready": timing_ready, "fusion_ready": fusion_ready, "enter_generated": enter_generated},
        ))

    # A bounded, reversible SHADOW-only gate nudge can start with fresh evidence.
    # It is intentionally much smaller than a production parameter change.
    gate_delta = 0.0
    if below_research:
        gate_delta -= min(1.5, 0.5 * below_research)
    if detected_late:
        gate_delta -= min(1.0, 0.25 * detected_late)
    experimental_gate = round(max(research_gate_min, min(research_gate_max, current_research_gate + gate_delta)), 2)

    active = sorted(
        hypotheses,
        key=lambda row: (row["confidence_pct"], row["evidence_count"], row["key"]),
        reverse=True,
    )
    snapshot = {
        "schema_version": 1,
        "mode": "LIVE_DISCOVERY_SHADOW_EXECUTION",
        "audit_key": str(audit.get("audit_key") or ""),
        "audit_status": str(audit.get("status") or "ACCUMULATING").upper(),
        "mature_movements": movements,
        "regime": (regime or {}).get("regime") or "UNKNOWN",
        "hypotheses": active,
        "active_experiments": [row["key"] for row in active if row["status"] != "UNSEEN"],
        "shadow_parameters": {
            "research_gate_baseline": round(float(current_research_gate), 2),
            "research_gate_experimental": experimental_gate,
            "research_gate_delta": round(experimental_gate - float(current_research_gate), 2),
        },
        "policy": {
            "shadow_can_experiment_from_first_observation": True,
            "shadow_hypotheses_are_not_live_validation": True,
            "auto_mutate_live": False,
            "auto_mutate_sizing_stops_tp": False,
            "live_requires_strong_validation": True,
        },
    }
    snapshot["fingerprint"] = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return snapshot


def write_alpha_lab_artifacts(snapshot: dict, output_dir: Path, *, created_at_madrid: str) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "latest_alpha_lab.json"
    history = output_dir / "alpha_lab_history.jsonl"
    payload = {**snapshot, "created_at_madrid": created_at_madrid}
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return {"json": str(latest), "history": str(history)}
