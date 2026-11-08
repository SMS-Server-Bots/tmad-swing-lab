from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _expiry_days(expiry: str, asof: str) -> int | None:
    try:
        return (date.fromisoformat(str(expiry)[:10]) - date.fromisoformat(str(asof)[:10])).days
    except Exception:
        return None


def choose_gex_aligned_contract(direction: str, spot: float, profile: dict, asof: str, target_dte: int = 30) -> dict:
    """Choose an indicative SHADOW option expression without forcing a bad strike."""
    direction = str(direction or "").upper()
    horizons = profile.get("summaryByHorizon") or {}
    swing = horizons.get("swing") or horizons.get("near") or horizons.get("all") or {}
    wall_key = "callWallStrike" if direction == "LONG" else "putWallStrike"
    wall = _f(swing.get(wall_key))
    gamma_flip = _f(swing.get("gammaFlip") or profile.get("gammaFlip"))

    strike = None
    strike_source = None
    if wall is not None and ((direction == "LONG" and wall >= spot) or (direction == "SHORT" and wall <= spot)):
        strike, strike_source = wall, wall_key
    elif gamma_flip is not None and ((direction == "LONG" and gamma_flip >= spot) or (direction == "SHORT" and gamma_flip <= spot)):
        strike, strike_source = gamma_flip, "gammaFlip"

    expiry_candidates = []
    for row in profile.get("gexByExpiry") or []:
        expiry = str(row.get("expiration") or row.get("expirationDate") or row.get("expiry") or "")[:10]
        dte = _expiry_days(expiry, asof)
        if dte is not None and dte >= 14:
            expiry_candidates.append((abs(dte - target_dte), dte, expiry))
    best_expiry = sorted(expiry_candidates)[0] if expiry_candidates else None
    expiry = best_expiry[2] if best_expiry else None
    dte = best_expiry[1] if best_expiry else None

    distance_pct = abs(float(strike) / float(spot) - 1.0) * 100.0 if strike is not None and spot else None
    # For a 1-5 session thesis, a GEX reference >20% from spot is a map, not a sensible
    # forced contract. We deliberately refuse to manufacture an ATM strike without chain data.
    feasible = strike is not None and distance_pct is not None and distance_pct <= 20.0 and expiry is not None
    if feasible:
        feasibility_status = "INDICATIVE_GEX_EXPRESSION"
        feasibility_score = max(0.0, 100.0 - 3.0 * distance_pct - (abs((dte or target_dte) - target_dte) * 1.5))
    elif strike is None:
        feasibility_status = "NO_DIRECTIONAL_GEX_STRIKE"
        feasibility_score = 0.0
    elif distance_pct is not None and distance_pct > 20.0:
        feasibility_status = "NO_SUITABLE_OPTION_EXPRESSION_YET"
        feasibility_score = 0.0
    else:
        feasibility_status = "NO_SUITABLE_EXPIRY_YET"
        feasibility_score = 0.0

    return {
        "instrument": "CALL" if direction == "LONG" else "PUT",
        "strike": round(float(strike), 2) if strike is not None else None,
        "strike_source": strike_source,
        "strike_distance_pct": round(distance_pct, 3) if distance_pct is not None else None,
        "expiry": expiry,
        "dte": dte,
        "target_dte": int(target_dte),
        "quote_status": "INDICATIVE_NO_OPTION_QUOTE",
        "feasibility_status": feasibility_status,
        "feasibility_score": round(feasibility_score, 3),
        "execution_allowed": False,
    }


def build_eod_shadow_portfolio(candidates: list[dict], gex_profiles: dict[str, dict], *, asof: str, max_positions: int = 5) -> dict:
    """Rank underlying alpha first, then separately score whether an option expression is usable."""
    ranked = []
    for row in candidates:
        ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").upper()
        spot = _f(row.get("spot") or row.get("price"))
        if not ticker or direction not in {"LONG", "SHORT"} or not spot:
            continue
        score = _f(row.get("score") or row.get("fusion_score") or row.get("research_score"), 0.0) or 0.0
        early = _f(row.get("early_alpha_score"), 0.0) or 0.0
        sector = _f(row.get("sector_strength"), 0.0) or 0.0
        flow = _f(row.get("flow_score"), 0.0) or 0.0
        conviction = round(0.55 * score + 0.15 * early + 0.15 * sector + 0.15 * flow, 3)
        expression = choose_gex_aligned_contract(direction, float(spot), gex_profiles.get(ticker) or {}, asof)
        tradeability = round(0.85 * conviction + 0.15 * float(expression.get("feasibility_score") or 0.0), 3)
        ranked.append({
            "ticker": ticker, "direction": direction, "spot": float(spot),
            "conviction": conviction, "underlying_alpha_score": conviction,
            "option_feasibility_score": expression.get("feasibility_score"), "tradeability_score": tradeability,
            "evidence": {"pipeline_score": score, "early_alpha_score": early, "sector_strength": sector, "flow_score": flow, "gex_used_as_map_not_mandatory_gate": True},
            "swing_context": row.get("swing_context") if isinstance(row.get("swing_context"), dict) else {},
            "option_expression": expression,
            "volatility_proxy_input": row.get("volatility_proxy_input") or {"source": "UNAVAILABLE", "daily_pct": None},
            "holding_thesis_sessions": [1, 2, 3, 5],
            "exit_policy": ["thesis_invalidated", "invalidation_level_breached", "direction_flip", "market_block", "time_stop_5_sessions", "gex_target_is_runner_milestone"],
        })
    ranked.sort(key=lambda x: (x["underlying_alpha_score"], x["ticker"]), reverse=True)
    selected = ranked[: max(0, int(max_positions))]
    payload = {
        "schema_version": 2, "mode": "EOD_OPTIONS_SHADOW", "asof": asof,
        "objective": "rank underlying alpha for 1-5 sessions and separately assess a GEX-aligned ~30DTE option expression",
        "selected": selected, "candidate_count": len(ranked), "selected_count": len(selected), "benchmark": "FLOW_ONLY_SHADOW",
        "policy": {"live_orders": False, "auto_promote_live": False, "gex_is_map_and_evidence_not_universal_veto": True, "never_force_option_expression_without_chain_data": True, "evaluate_mfe_mae_and_time_to_profit": True, "synthetic_option_proxy_is_research_only": True},
    }
    payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return payload


def write_eod_shadow_artifact(payload: dict, output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "latest_eod_options_shadow.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history = output_dir / "eod_options_shadow_history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return str(path)
