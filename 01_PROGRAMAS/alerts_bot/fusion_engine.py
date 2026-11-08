from __future__ import annotations

from typing import Any

import config
from gex_intelligence import project_gex
from market_regime import market_entry_state
from scanner_intelligence import build_core_opportunity, enrich_cross_section
from adaptive_learning import effective_research_gate, calibrated_probability


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except Exception:
        return default


def _graph_candidate(features: dict) -> dict | None:
    if not getattr(config, "GRAPH_PROPAGATION_ENABLED", True):
        return None
    lead = _f(features.get("relationship_lead_lag_score"))
    graph = _f(features.get("graph_context_score"))
    sector = _f(features.get("sector_etf_score"))
    breadth = _f(features.get("sector_breadth_score"))
    leveraged = _f(features.get("leveraged_confirmation_score"))
    dynamic = _f(features.get("relationship_context_score"))
    strongest = max(abs(v) for v in (lead, graph, sector, breadth, leveraged, dynamic))
    if strongest < float(config.GRAPH_PROPAGATION_MIN_CONTEXT_SCORE):
        return None
    composite = lead * 0.30 + graph * 0.20 + sector * 0.15 + breadth * 0.10 + leveraged * 0.10 + dynamic * 0.15
    if abs(composite) < float(config.GRAPH_PROPAGATION_MIN_COMPOSITE_SCORE):
        return None
    direction = "long" if composite > 0 else "short"
    related = list(features.get("related_nodes") or [])[:5]
    leaders = [str(item.get("ticker")) for item in related if item.get("ticker")]
    source_type = "ETF_ROTATION" if features.get("sector_etf") and abs(sector) >= 20 else "LEAD_LAG"
    if abs(leveraged) >= 25:
        source_type = "LEVERAGED_PROPAGATION"
    rationale = f"grafo {graph:+.0f} · lead {lead:+.0f} · sector {sector:+.0f} · breadth {breadth:+.0f}"
    if leaders:
        rationale += " · líderes " + ",".join(leaders[:3])
    return {
        "layer": "GRAPH",
        "signal_origin": "GRAPH_PROPAGATION",
        "graph_source": source_type,
        "direction": direction,
        "score": round(min(100.0, 50.0 + abs(composite) * 0.55), 2),
        "state": "WATCH",
        "actionable": False,
        "target_source": None,
        "target_pct": None,
        "timing_min_sessions": 1,
        "timing_max_sessions": int(config.MAX_PATTERN_HORIZON_SESSIONS),
        "lead_lag_score": lead,
        "sector_score": sector,
        "breadth_score": breadth,
        "leveraged_score": leveraged,
        "leaders": leaders,
        "rationale": rationale,
    }


def _intraday_state(features: dict, direction: str, gex: dict | None = None) -> dict:
    """Timing layer: direction stays upstream; this only says ENTER/WAIT/RETEST."""
    sign = 1.0 if direction == "long" else -1.0
    blocks: list[str] = []
    adverse: list[str] = []
    flow = _f(features.get("flow_direction"))
    if sign * flow > 0:
        blocks.append("flow")
    elif sign * flow < 0:
        adverse.append("flow contrario")
    near = _f(features.get("near_gex_change_pct"))
    if sign * near >= 8.0:
        blocks.append("GEX 0-7d")
    elif sign * near <= -8.0:
        adverse.append("GEX 0-7d revierte")
    transition = _f(features.get("delta_near_gex"))
    if sign * transition > 0:
        blocks.append("ΔGEX")
    material = _f(features.get("profile_material_change"))
    if material > 0:
        blocks.append("perfil")
    pvw = sign * _f(features.get("price_vs_vwap_pct"))
    if -float(config.CORE_RETEST_MAX_VWAP_DISTANCE_PCT) <= pvw <= float(config.CORE_RETEST_MAX_VWAP_DISTANCE_PCT):
        blocks.append("retest VWAP")
    elif pvw > 0:
        blocks.append("VWAP")
    else:
        adverse.append("bajo VWAP" if direction == "long" else "sobre VWAP")
    market_adverse = sign * _f(features.get("market_context_score")) <= -35.0
    local_gex_strength = _f((gex or {}).get("score"))
    countertrend_timing = bool(
        market_adverse
        and bool(getattr(config, "MARKET_CLOSE_ALL_COUNTERTREND_ENTRIES_ENABLED", True))
        and gex
        and str(gex.get("direction") or "") == direction
        and local_gex_strength >= float(config.MARKET_CLOSE_ALL_COUNTERTREND_MIN_GEX_SCORE)
        and len(blocks) >= int(config.MARKET_CLOSE_ALL_COUNTERTREND_MIN_TIMING_BLOCKS)
    )
    if market_adverse:
        adverse.append("mercado adverso" + (" · override local en evaluación" if countertrend_timing else ""))
    if gex and str(gex.get("gex_intent")) in {"HEDGING", "STABILIZING", "UNCLEAR"}:
        adverse.append(str(gex.get("gex_intent")))
    if gex and bool(gex.get("capped")):
        adverse.append("runway capped")
    non_market_adverse = [item for item in adverse if not str(item).startswith("mercado adverso")]
    if len(blocks) >= int(config.FUSION_INTRADAY_MIN_BLOCKS) and (not market_adverse or countertrend_timing) and len(non_market_adverse) <= 1:
        state = "ENTER"
    elif "retest VWAP" in blocks and (not market_adverse or countertrend_timing):
        state = "RETEST"
    else:
        state = "WAIT"
    return {
        "state": state,
        "blocks": blocks,
        "adverse": adverse,
        "score": min(100.0, max(0.0, len(blocks) * 22.0 - len(adverse) * 12.0)),
        "adverse_market": market_adverse,
        "countertrend_timing": countertrend_timing,
    }


def _countertrend_entry_override(core: dict | None, gex: dict | None, timing: dict | None, market: dict) -> bool:
    if not bool(getattr(config, "MARKET_CLOSE_ALL_COUNTERTREND_ENTRIES_ENABLED", True)):
        return False
    if not market.get("extreme_block"):
        return False
    if not (core and gex and timing):
        return False
    if str(core.get("direction") or "") != str(gex.get("direction") or ""):
        return False
    return bool(
        _f(core.get("probability_pct")) >= float(config.MARKET_CLOSE_ALL_COUNTERTREND_MIN_CORE_PROBABILITY)
        and _f(gex.get("score")) >= float(config.MARKET_CLOSE_ALL_COUNTERTREND_MIN_GEX_SCORE)
        and str(gex.get("gex_intent") or "") in {"DIRECTIONAL", "SQUEEZE"}
        and bool(gex.get("operational_target"))
        and str(timing.get("state") or "") == "ENTER"
        and len(timing.get("blocks") or []) >= int(config.MARKET_CLOSE_ALL_COUNTERTREND_MIN_TIMING_BLOCKS)
        and bool(timing.get("countertrend_timing"))
    )


def _execution_state(core: dict | None, graph: dict | None, gex: dict | None, timing: dict | None, market: dict, mode: str) -> tuple[str, bool, list[str]]:
    blockers: list[str] = []
    if market.get("extreme_block"):
        if _countertrend_entry_override(core, gex, timing, market):
            market["countertrend_override"] = True
        else:
            return "MARKET_BLOCKED", False, ["régimen/mercado extremo sin confirmación local suficiente"]
    if core and str(core.get("state")) == "EXTENDED":
        blockers.append("CORE extendido")
    if gex and str(gex.get("gex_intent")) == "FOMO":
        blockers.append("GEX FOMO")
    if gex and bool(gex.get("capped")):
        blockers.append("recorrido GEX <5% o estructura wall no válida")
    if core and gex and core.get("direction") != gex.get("direction"):
        return "DIVERGENCE", False, ["CORE/GEX divergen"]
    if blockers:
        return "EXTENDED" if any("extendido" in b or "FOMO" in b for b in blockers) else "CAPPED", False, blockers
    if not gex:
        if core and graph:
            return "WATCH", False, ["target GEX pendiente"]
        if core:
            return str(core.get("state") or "DISCOVERED"), False, ["target GEX pendiente"]
        if graph:
            return "WATCH", False, ["confirmación ticker pendiente"]
        return "NONE", False, []
    intent = str(gex.get("gex_intent") or "UNCLEAR")
    if intent in {"HEDGING", "STABILIZING", "UNCLEAR", "DISTRIBUTION"}:
        return "EARLY", False, [f"GEX {intent.lower()}"]
    model_status = str(gex.get("model_status") or "ACCUMULATING").upper()
    if model_status != "VALIDATED" and not core:
        strong_native_shadow = bool(
            getattr(config, "GEX_STANDALONE_SHADOW_ENABLED", True)
            and mode == "intraday"
            and float(gex.get("score") or 0.0) >= float(config.GEX_STANDALONE_MIN_SCORE)
            and bool(gex.get("operational_target"))
            and intent in {"DIRECTIONAL", "SQUEEZE"}
            and str((timing or {}).get("state") or "WAIT") == "ENTER"
        )
        if strong_native_shadow:
            # It may enter the learning book independently of CORE. LIVE remains
            # impossible until both daily GEX and intraday timing are VALIDATED.
            return "ENTER", True, []
        return "EARLY", False, ["GEX standalone acumulando histórico; timing/convicción insuficientes"]
    if mode != "intraday":
        return "READY", False, ["esperando timing intradía"]
    timing_state = str((timing or {}).get("state") or "WAIT")
    if timing_state == "ENTER":
        return "ENTER", True, []
    if timing_state == "RETEST":
        return "RETEST", False, ["esperando confirmación retest"]
    return "READY", False, ["timing WAIT"]


def _gex_diagnostic_code(features: dict, gex: dict | None, blockers: list[str]) -> str:
    # Machine-readable reason for GEX readiness; diagnostics only, never a gate.
    if not gex:
        return "GEX_NO_OPERATIONAL_PROJECTION" if int(_f(features.get("has_gex"))) else "GEX_DATA_MISSING"
    if bool(gex.get("expiry_stale")):
        return "GEX_STALE_EXPIRY"
    if bool(gex.get("capped")):
        return "GEX_CAPPED"
    intent = str(gex.get("gex_intent") or "UNCLEAR").upper()
    if intent in {"FOMO", "HEDGING", "STABILIZING", "UNCLEAR", "DISTRIBUTION"}:
        return f"GEX_{intent}"
    if not bool(gex.get("operational_target")):
        return "GEX_TARGET_UNAVAILABLE"
    blocker_text = " | ".join(str(x).lower() for x in blockers)
    if "timing" in blocker_text or "retest" in blocker_text:
        return "TIMING_PENDING"
    return "GEX_READY"


def _fusion_score(core: dict | None, graph: dict | None, gex: dict | None, market: dict) -> float:
    parts: list[tuple[float, float]] = []
    if core:
        parts.append((float(core.get("score") or 0.0), 0.42))
    if gex:
        parts.append((float(gex.get("score") or 0.0), 0.38))
    if graph:
        parts.append((float(graph.get("score") or 0.0), 0.12))
    parts.append((50.0 + float(market.get("adjustment") or 0.0), 0.08))
    total_weight = sum(weight for _, weight in parts)
    return min(100.0, max(0.0, sum(value * weight for value, weight in parts) / max(total_weight, 0.01)))


def build_fusion_signals(
    features_by_ticker: dict[str, dict],
    core_model: dict | None,
    full_model: dict | None = None,
    *,
    mode: str = "intraday",
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Build independent CORE, GRAPH and GEX signals plus a state machine.

    CORE and GEX are independent discovery engines. GEX is not merely a filter on
    CORE: a GEX_STANDALONE signal is stored even when CORE is neutral.
    """
    enrich_cross_section(features_by_ticker)
    all_signals: list[dict] = []
    actionable: dict[str, list[dict]] = {}
    for ticker, raw_features in features_by_ticker.items():
        features = dict(raw_features)
        core = build_core_opportunity(features, core_model, mode)
        graph = _graph_candidate(features)
        core_direction = str(core.get("direction")) if core else None
        gex = project_gex(features, core_direction=core_direction)
        if gex:
            snapshot_date = str(features.get("snapshot_date") or features.get("trade_date") or "")
            expiry_date = str(gex.get("expiry_date") or "")
            if snapshot_date and expiry_date and expiry_date < snapshot_date:
                gex["expiry_stale"] = True
                gex["operational_target"] = False
                gex["capped"] = True
                gex["gex_intent"] = "UNCLEAR"
                gex["rationale"] = str(gex.get("rationale") or "") + " · expiración vencida"
        direction = str((gex or core or graph or {}).get("direction") or "")
        timing = _intraday_state(features, direction, gex) if direction else None

        components: list[dict] = []
        if core:
            components.append({
                **core, "ticker": ticker, "price": features.get("price"),
                "source_gex_run_id": features.get("gex_run_id"),
            })
        if graph:
            components.append({**graph, "ticker": ticker, "price": features.get("price")})
        if gex:
            components.append({
                **gex, "ticker": ticker, "price": features.get("price"),
                "source_gex_run_id": features.get("gex_run_id"),
            })
            components.append({
                "ticker": ticker,
                "layer": "GEX_INTRADAY",
                "signal_origin": "GEX_TIMING",
                "direction": gex.get("direction"),
                "score": (timing or {}).get("score"),
                "state": (timing or {}).get("state", "WAIT"),
                "actionable": False,
                "target_source": gex.get("target_source"),
                "target_zone_low": gex.get("target_zone_low"),
                "target_zone_high": gex.get("target_zone_high"),
                "target_price": gex.get("target_price"),
                "target_pct": gex.get("target_pct"),
                "timing_min_sessions": gex.get("timing_min_sessions"),
                "timing_max_sessions": gex.get("timing_max_sessions"),
                "expiry_date": gex.get("expiry_date"),
                "gex_intent": gex.get("gex_intent"),
                "expiry_regime": gex.get("expiry_regime"),
                "rationale": "timing " + str((timing or {}).get("state", "WAIT")) + " · " + ",".join((timing or {}).get("blocks") or ["sin confirmaciones"]),
                "price": features.get("price"),
                "source_gex_run_id": features.get("gex_run_id"),
            })

        fusion: dict | None = None
        if core or graph or gex:
            fusion_direction = str((gex or core or graph or {}).get("direction") or "")
            market = market_entry_state(fusion_direction, features) if fusion_direction else {"adjustment": 0.0, "extreme_block": False}
            state, is_actionable, blockers = _execution_state(core, graph, gex, timing, market, mode)
            aligned_core_gex = bool(core and gex and core.get("direction") == gex.get("direction"))
            if gex and not core:
                origin = "GEX_STANDALONE"
            elif core and gex and not aligned_core_gex:
                origin = "CORE_GEX_DIVERGENCE"
            elif core and gex:
                origin = "CORE_GEX_ALIGNED"
            elif core:
                origin = "CORE_DIRECTIONAL"
            else:
                origin = "GRAPH_PROPAGATION"
            score = _fusion_score(core, graph, gex, market)
            # A real entry requires GEX target + non-hedging intent + intraday ENTER.
            actionable_now = bool(
                is_actionable
                and gex
                and bool(gex.get("operational_target"))
                and score >= float(effective_research_gate())
                and str(gex.get("gex_intent") or "") in {"DIRECTIONAL", "SQUEEZE"}
            )
            adaptive_probability = calibrated_probability((core or {}).get("probability_pct"))
            rationale_parts = []
            if core:
                raw_probability = (core or {}).get("probability_pct")
                probability_text = ""
                if raw_probability is not None:
                    probability_label = "P_cal" if adaptive_probability is not None else "P_est"
                    probability_text = f"/{probability_label} {float(adaptive_probability if adaptive_probability is not None else raw_probability):.0f}%"
                    if adaptive_probability is not None and abs(float(adaptive_probability) - float(raw_probability)) >= 0.5:
                        probability_text += f" (raw {float(raw_probability):.0f}%)"
                rationale_parts.append(f"CORE {float(core.get('score') or 0):.0f}" + probability_text)
            if graph:
                rationale_parts.append(f"grafo {float(graph.get('score') or 0):.0f}")
            if gex:
                rationale_parts.append(str(gex.get("rationale") or "GEX"))
            if market.get("countertrend_override"):
                rationale_parts.append("countertrend validado: CORE+GEX+timing local superan la alerta general")
            if blockers:
                rationale_parts.append("bloqueo " + ",".join(blockers))
            contributor_engines: list[str] = []
            if core:
                contributor_engines.append("CORE")
            if gex:
                contributor_engines.append("GEX")
            if graph:
                contributor_engines.append("EXPANSION")
            if timing:
                contributor_engines.append("TIMING")
            model_lineage = {
                "core_model_id": (core_model or {}).get("id"),
                "core_model_key": (core_model or {}).get("model_key"),
                "core_model_status": (core_model or {}).get("status"),
                "gex_daily_status": features.get("gex_daily_model_status") or (gex or {}).get("model_status"),
                "timing_status": features.get("gex_intraday_model_status"),
                "expansion_status": features.get("graph_model_status"),
                "fusion_status": features.get("fusion_model_status"),
                "expansion_type": (graph or {}).get("graph_source"),
            }
            fusion = {
                **(gex or {}),
                "ticker": ticker,
                "primary_engine": origin,
                "contributor_engines": contributor_engines,
                "model_lineage": model_lineage,
                "core_model_id": (core_model or {}).get("id"),
                "core_model_key": (core_model or {}).get("model_key"),
                "core_model_status": (core_model or {}).get("status"),
                "gex_daily_model_status": features.get("gex_daily_model_status") or (gex or {}).get("model_status"),
                "gex_intraday_model_status": features.get("gex_intraday_model_status"),
                "graph_model_status": features.get("graph_model_status"),
                "fusion_model_status": features.get("fusion_model_status"),
                "layer": "FUSION",
                "signal_origin": origin,
                "rule_name": f"FUSION_{origin}_{fusion_direction.upper()}",
                "direction": fusion_direction,
                "score": round(score, 2),
                "state": "ENTER" if actionable_now else state,
                "opportunity_state": "ENTER" if actionable_now else state,
                "actionable": actionable_now,
                "probability_pct": adaptive_probability if adaptive_probability is not None else (core or {}).get("probability_pct"),
                "probability_raw_pct": (core or {}).get("probability_pct"),
                "probability_source": "ADAPTIVE_SHADOW_CALIBRATION" if adaptive_probability is not None and adaptive_probability != (core or {}).get("probability_pct") else (core or {}).get("probability_source"),
                "adaptive_research_gate": effective_research_gate(),
                "core_rule": (core or {}).get("rule_name"),
                "core_score": (core or {}).get("score"),
                "core_rank_score": (core or {}).get("core_rank_score"),
                "core_directional_edge": (core or {}).get("directional_edge"),
                "overextension_score": (core or {}).get("overextension_score"),
                "graph_score": (graph or {}).get("score"),
                "graph_source": (graph or {}).get("graph_source"),
                "gex_score": (gex or {}).get("score"),
                "gex_intent": (gex or {}).get("gex_intent"),
                "gex_diagnostic_code": _gex_diagnostic_code(features, gex, blockers),
                "gex_data_available": bool(int(_f(features.get("has_gex")))),
                "expiry_regime": (gex or {}).get("expiry_regime"),
                "wall_asymmetry": (gex or {}).get("wall_asymmetry"),
                "market_adjustment": market.get("adjustment"),
                "market_policy": "COUNTERTREND_VALIDATED" if market.get("countertrend_override") else (market.get("policy") or ("BLOCK" if market.get("extreme_block") else "NORMAL")),
                "countertrend_override": bool(market.get("countertrend_override")),
                "intraday_state": (timing or {}).get("state"),
                "intraday_blocks": (timing or {}).get("blocks"),
                "intraday_adverse": (timing or {}).get("adverse"),
                "blockers": blockers,
                "rationale": " · ".join(rationale_parts),
                "price": features.get("price"),
                "source_gex_run_id": features.get("gex_run_id"),
                "stop_pct": 0.0,
                "max_hold_sessions": int((gex or {}).get("timing_max_sessions") or config.MAX_PATTERN_HORIZON_SESSIONS),
                "daily_pattern_projection_pct": (core or {}).get("daily_pattern_projection_pct"),
                "reason": "Origen y contribuciones persistidos: CORE, GEX, expansión y timing se evalúan como motores separados",
            }
            components.append(fusion)
            if actionable_now:
                actionable.setdefault(ticker, []).append(fusion)
        all_signals.extend(components)
    return all_signals, actionable
