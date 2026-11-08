from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import config
from model_metrics import normalized_oos_metrics
from asset_universe import classify_asset
from time_utils import ts_madrid_str, ts_utc_str
from market_regime import ticker_gex_confirmation_state, broad_signal_local_exit_gate
from discord_presentation import build_group, signal_source_label, signal_model_lineage, ticker_bullet_header, gex_intent_display

FULL_RULE_DIRECTIONS = {
    "PUT_WALL_DEFENSE_LONG": "long",
    "CALL_WALL_REJECTION_SHORT": "short",
    "CALL_WALL_BREAK_LONG": "long",
    "PUT_WALL_BREAK_SHORT": "short",
    "EXPIRY_GEX_SHIFT_LONG": "long",
    "EXPIRY_GEX_SHIFT_SHORT": "short",
}
CORE_RULE_DIRECTIONS = {
    "CORE_FLOW_MOMENTUM_LONG": "long",
    "CORE_FLOW_MOMENTUM_SHORT": "short",
    "CORE_RELATIVE_STRENGTH_LONG": "long",
    "CORE_RELATIVE_STRENGTH_SHORT": "short",
    **{f"DISCOVERED_MOVER_LONG_{i}": "long" for i in range(1, int(config.CORE_MOVER_DISCOVERY_MAX_PATTERNS_PER_DIRECTION) + 1)},
    **{f"DISCOVERED_MOVER_SHORT_{i}": "short" for i in range(1, int(config.CORE_MOVER_DISCOVERY_MAX_PATTERNS_PER_DIRECTION) + 1)},
}
RULE_DIRECTIONS = {**FULL_RULE_DIRECTIONS, **CORE_RULE_DIRECTIONS}


def _effective_target_pct(params: dict | None = None) -> float:
    """Compatibility label: CORE learns movement >=5%; GEX owns live targets."""
    return float(config.PATTERN_TARGET_PCT)


def _effective_stop_pct(params: dict | None = None) -> float:
    """Compatibility field only; fixed price-stop exits are disabled."""
    return 0.0


def f(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def text(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_any(values: list[Any], words: tuple[str, ...]) -> bool:
    joined = " ".join(text(value) for value in values)
    return any(word in joined for word in words)


def _trend_score(snapshot: dict) -> float:
    score = f(snapshot.get("momentum_score"), 0.0) or 0.0
    score += (f(snapshot.get("long_score"), 0.0) or 0.0) * 0.25
    trend = text(snapshot.get("trend") or snapshot.get("detail_trend"))
    if "bull" in trend or "up" in trend:
        score += 10
    if "bear" in trend or "down" in trend:
        score -= 10
    rsi = f(snapshot.get("rsi") or snapshot.get("rsi14"))
    if rsi is not None:
        if rsi >= 55:
            score += min(8.0, (rsi - 55) / 3)
        elif rsi <= 45:
            score -= min(8.0, (45 - rsi) / 3)
    return score


def _flow_direction(snapshot: dict) -> int:
    values = [snapshot.get("option_signal"), snapshot.get("unusual_flow_signal")]
    bullish = _contains_any(values, ("bull", "call", "long", "buy"))
    bearish = _contains_any(values, ("bear", "put", "short", "sell"))
    call_z = f(snapshot.get("otm_call_volume_z"), 0.0) or 0.0
    put_z = f(snapshot.get("otm_put_volume_z"), 0.0) or 0.0
    if call_z - put_z >= 0.75:
        bullish = True
    if put_z - call_z >= 0.75:
        bearish = True
    if bullish and not bearish:
        return 1
    if bearish and not bullish:
        return -1
    return 0


def _bucket(expiry_buckets: dict[str, dict] | None, name: str) -> dict:
    return (expiry_buckets or {}).get(name) or {}


def _minutes_between(later: Any, earlier: Any) -> float | None:
    if not later or not earlier:
        return None
    try:
        return (datetime.fromisoformat(str(later)) - datetime.fromisoformat(str(earlier))).total_seconds() / 60.0
    except Exception:
        return None


def build_features(
    snapshot: dict,
    latest_gex: dict | None,
    expiry_buckets: dict[str, dict] | None,
    expirations: list[dict] | None = None,
) -> dict:
    gex = latest_gex or {}
    front = _bucket(expiry_buckets, "front_0_2d")
    week = _bucket(expiry_buckets, "week_3_7d")
    short = _bucket(expiry_buckets, "short_0_7d")
    next_bucket = _bucket(expiry_buckets, "next_8_14d")
    month_tail = _bucket(expiry_buckets, "month_15_31d")
    month_all = _bucket(expiry_buckets, "month_0_31d") or _bucket(expiry_buckets, "all_captured")
    near_horizon = _bucket(expiry_buckets, "near_0_7d") or short
    swing_horizon = _bucket(expiry_buckets, "swing_8_56d")
    long_horizon = _bucket(expiry_buckets, "long_57d_plus")
    price = f(snapshot.get("current_price"))
    vwap = f(snapshot.get("vwap"))
    scanner_captured = snapshot.get("captured_at_madrid")
    gex_captured = gex.get("captured_at_madrid")
    session_type = str(snapshot.get("session_type") or "")
    is_daily_close = "deep" in session_type.lower() or session_type.lower() in {"daily_close", "canonical_close", "postmarket"}
    features = {
        "ticker": snapshot.get("ticker"),
        "session_type": session_type,
        "observation_layer": "daily_close" if is_daily_close else "intraday",
        "feature_tier": "FULL" if is_daily_close else "LIVE",
        "source_kind": "FULL_DEEP" if is_daily_close else "INTRADAY_LIVE",
        "is_canonical_daily_close": 1 if is_daily_close else 0,
        "has_gex": 1 if latest_gex else 0,
        "name": snapshot.get("name"),
        "category": snapshot.get("category"),
        "sector": snapshot.get("sector") or snapshot.get("category"),
        "industry": snapshot.get("industry") or snapshot.get("sub_industry"),
        "asset_type": classify_asset(
            str(snapshot.get("ticker") or ""), snapshot.get("name"), snapshot.get("category")
        ),
        "snapshot_date": snapshot.get("snapshot_date"),
        "scanner_run_id": snapshot.get("scanner_run_id"),
        "gex_run_id": gex.get("gex_run_id"),
        "scanner_captured_at_madrid": scanner_captured,
        "gex_captured_at_madrid": gex_captured,
        "gex_lag_minutes": _minutes_between(scanner_captured, gex_captured),
        "price": price,
        "change_percent": f(snapshot.get("change_percent")),
        "vwap": vwap,
        "price_vs_vwap_pct": ((price / vwap - 1.0) * 100.0) if price not in (None, 0) and vwap not in (None, 0) else None,
        "rsi": f(snapshot.get("rsi") or snapshot.get("rsi14")),
        "rvol": f(snapshot.get("rvol")),
        "momentum_score": f(snapshot.get("momentum_score") or snapshot.get("detail_momentum_score")),
        "long_score": f(snapshot.get("long_score")),
        "trend": snapshot.get("trend") or snapshot.get("detail_trend"),
        "option_signal": snapshot.get("option_signal"),
        "unusual_flow_signal": snapshot.get("unusual_flow_signal"),
        "otm_call_volume_z": f(snapshot.get("otm_call_volume_z")),
        "otm_put_volume_z": f(snapshot.get("otm_put_volume_z")),
        "iv_z_spread": f(snapshot.get("iv_z_spread") or snapshot.get("detail_iv_z_spread")),
        "pc_ratio": f(snapshot.get("pc_ratio") or snapshot.get("put_call_ratio")),
        "total_gex": f(gex.get("total_gex") if gex else snapshot.get("total_gex")),
        "total_gex_change_pct": f(gex.get("total_gex_change_pct") if gex else snapshot.get("gex_change")),
        "gamma_flip": f(gex.get("gamma_flip") if gex else snapshot.get("gamma_flip")),
        "profile_bias": gex.get("profile_bias"),
        "call_accumulation_score": f(gex.get("call_accumulation_score")),
        "put_accumulation_score": f(gex.get("put_accumulation_score")),
        "dominant_strike_changed": gex.get("dominant_strike_changed"),
        "gross_gex": f(gex.get("gross_gex")),
        "profile_event_kind": gex.get("profile_event_kind"),
        "profile_changed_fields": f(gex.get("profile_changed_fields")),
        "profile_changed_strikes": f(gex.get("profile_changed_strikes")),
        "profile_changed_expiries": f(gex.get("profile_changed_expiries")),
        "profile_material_change": f(gex.get("profile_material_change")),
        "all_call_wall_strike": f(gex.get("call_wall_strike")),
        "all_put_wall_strike": f(gex.get("put_wall_strike")),
        "near_gex": f(gex.get("near_gex") if gex.get("near_gex") is not None else near_horizon.get("net_gex")),
        "near_gex_change_pct": f(near_horizon.get("net_gex_change_pct")),
        "near_call_gex": f(gex.get("near_call_gex")),
        "near_put_gex": f(gex.get("near_put_gex")),
        "near_gross_gex": f(gex.get("near_gross_gex")),
        "near_gamma_flip": f(gex.get("near_gamma_flip")),
        "near_call_wall_strike": f(gex.get("near_call_wall_strike")),
        "near_put_wall_strike": f(gex.get("near_put_wall_strike")),
        "swing_gex": f(gex.get("swing_gex") if gex.get("swing_gex") is not None else swing_horizon.get("net_gex")),
        "swing_gex_change_pct": f(swing_horizon.get("net_gex_change_pct")),
        "swing_call_gex": f(gex.get("swing_call_gex")),
        "swing_put_gex": f(gex.get("swing_put_gex")),
        "swing_gross_gex": f(gex.get("swing_gross_gex")),
        "swing_gamma_flip": f(gex.get("swing_gamma_flip")),
        "swing_call_wall_strike": f(gex.get("swing_call_wall_strike")),
        "swing_put_wall_strike": f(gex.get("swing_put_wall_strike")),
        "long_gex": f(gex.get("long_gex") if gex.get("long_gex") is not None else long_horizon.get("net_gex")),
        "long_gex_change_pct": f(long_horizon.get("net_gex_change_pct")),
        "long_call_gex": f(gex.get("long_call_gex")),
        "long_put_gex": f(gex.get("long_put_gex")),
        "long_gross_gex": f(gex.get("long_gross_gex")),
        "long_gamma_flip": f(gex.get("long_gamma_flip")),
        "long_call_wall_strike": f(gex.get("long_call_wall_strike")),
        "long_put_wall_strike": f(gex.get("long_put_wall_strike")),
        "put_wall": f(snapshot.get("put_wall")),
        "call_wall": f(snapshot.get("call_wall")),
        "put_wall_pct": f(snapshot.get("put_wall_pct")),
        "call_wall_pct": f(snapshot.get("call_wall_pct")),
        "put_wall_strength": f(snapshot.get("put_wall_strength")),
        "call_wall_strength": f(snapshot.get("call_wall_strength")),
        "wall_range_pct": f(snapshot.get("wall_range_pct")),
        "front_gex": f(front.get("net_gex")),
        "front_gex_change_pct": f(front.get("net_gex_change_pct")),
        "front_sign_flip": front.get("sign_flip"),
        "week_gex": f(week.get("net_gex")),
        "week_gex_change_pct": f(week.get("net_gex_change_pct")),
        "week_sign_flip": week.get("sign_flip"),
        "short_gex": f(short.get("net_gex")),
        "short_gex_change_pct": f(short.get("net_gex_change_pct")),
        "short_sign_flip": short.get("sign_flip"),
        "next_gex": f(next_bucket.get("net_gex")),
        "next_gex_change_pct": f(next_bucket.get("net_gex_change_pct")),
        "month_tail_gex": f(month_tail.get("net_gex")),
        "month_tail_gex_change_pct": f(month_tail.get("net_gex_change_pct")),
        "month_gex": f(month_all.get("net_gex")),
        "month_gex_change_pct": f(month_all.get("net_gex_change_pct")),
        "dominant_expiration": month_all.get("dominant_expiration"),
        "top_expirations": [
            {
                "expiration_date": item.get("expiration_date"),
                "dte": item.get("dte"),
                "net_gex": item.get("net_gex"),
                "change_pct": item.get("net_gex_change_pct"),
                "share_abs_gex": item.get("share_abs_gex"),
            }
            for item in (expirations or [])[:8]
        ],
    }
    gross = f(features.get("gross_gex"))
    for horizon in ("near", "swing", "long"):
        h_gross = f(features.get(f"{horizon}_gross_gex"))
        features[f"{horizon}_gross_share_pct"] = (h_gross / gross * 100.0) if gross not in (None, 0) and h_gross is not None else None
        for level_name in ("gamma_flip", "call_wall_strike", "put_wall_strike"):
            level = f(features.get(f"{horizon}_{level_name}"))
            features[f"{horizon}_{level_name}_distance_pct"] = ((level / price - 1.0) * 100.0) if price not in (None, 0) and level is not None else None
    all_gamma = f(features.get("gamma_flip"))
    features["gamma_flip_distance_pct"] = ((all_gamma / price - 1.0) * 100.0) if price not in (None, 0) and all_gamma is not None else None
    features["gex_term_near_minus_swing"] = (f(features.get("near_gex")) - f(features.get("swing_gex"))) if f(features.get("near_gex")) is not None and f(features.get("swing_gex")) is not None else None
    features["flow_direction"] = _flow_direction(snapshot)
    features["scanner_trend_score"] = round(_trend_score(snapshot), 3)
    return features


def feature_observation(snapshot: dict, gex: dict | None, features: dict) -> dict:
    now_utc = snapshot.get("captured_at_utc") or ts_utc_str()
    now_mad = snapshot.get("captured_at_madrid") or ts_madrid_str()
    run_id = int(snapshot.get("scanner_run_id"))
    ticker = str(snapshot.get("ticker"))
    return {
        "observed_at_utc": now_utc,
        "observed_at_madrid": now_mad,
        "snapshot_date": snapshot.get("snapshot_date") or now_mad[:10],
        "source_scanner_run_id": run_id,
        "source_gex_run_id": (gex or {}).get("gex_run_id"),
        "ticker": ticker,
        "session_type": features.get("session_type") or snapshot.get("session_type"),
        "observation_layer": features.get("observation_layer") or "intraday",
        "is_canonical_daily_close": int(features.get("is_canonical_daily_close") or 0),
        "has_gex": int(features.get("has_gex") or (1 if gex else 0)),
        "source_archive": features.get("source_archive"),
        "feature_tier": features.get("feature_tier") or "FULL",
        "universe_quality": features.get("universe_quality"),
        "source_kind": features.get("source_kind"),
        "scanner_captured_at_madrid": features.get("scanner_captured_at_madrid"),
        "gex_captured_at_madrid": features.get("gex_captured_at_madrid"),
        "gex_lag_minutes": features.get("gex_lag_minutes"),
        "current_price": features.get("price"),
        "total_gex": features.get("total_gex"),
        "total_gex_change_pct": features.get("total_gex_change_pct"),
        "front_gex": features.get("front_gex"),
        "front_gex_change_pct": features.get("front_gex_change_pct"),
        "week_gex": features.get("week_gex"),
        "week_gex_change_pct": features.get("week_gex_change_pct"),
        "short_gex": features.get("short_gex"),
        "short_gex_change_pct": features.get("short_gex_change_pct"),
        "next_gex": features.get("next_gex"),
        "next_gex_change_pct": features.get("next_gex_change_pct"),
        "month_gex": features.get("month_gex"),
        "month_gex_change_pct": features.get("month_gex_change_pct"),
        "put_wall_pct": features.get("put_wall_pct"),
        "call_wall_pct": features.get("call_wall_pct"),
        "wall_range_pct": features.get("wall_range_pct"),
        "rvol": features.get("rvol"),
        "rsi": features.get("rsi"),
        "momentum_score": features.get("momentum_score"),
        "trend": features.get("trend"),
        "option_signal": features.get("option_signal"),
        "unusual_flow_signal": features.get("unusual_flow_signal"),
        "feature_schema_version": int(features.get("relational_feature_version") or config.RELATIONAL_FEATURE_SCHEMA_VERSION),
        "sector_etf": features.get("sector_etf"),
        "market_regime": features.get("market_regime"),
        "market_context_score": features.get("market_context_score"),
        "market_breadth_score": features.get("market_breadth_score"),
        "vix_stress_score": features.get("vix_stress_score"),
        "credit_risk_on_score": features.get("credit_risk_on_score"),
        "sector_context_score": features.get("sector_etf_score"),
        "sector_breadth_score": features.get("sector_breadth_score"),
        "leveraged_confirmation_score": features.get("leveraged_confirmation_score"),
        "relationship_context_score": features.get("relationship_context_score"),
        "relationship_lead_lag_score": features.get("relationship_lead_lag_score"),
        "graph_context_score": features.get("graph_context_score"),
        "relational_deterioration_score": features.get("relational_deterioration_score"),
        "relative_strength_vs_market": features.get("relative_strength_vs_market"),
        "relative_strength_vs_sector": features.get("relative_strength_vs_sector"),
        "relational_json": features.get("relational_json"),
        "features_json": json.dumps(features, ensure_ascii=False),
        "unique_key": f"{run_id}|{ticker}",
    }


def params_for_rule(model_params: dict, rule_name: str) -> dict:
    rules = model_params.get("rules") or {}
    return dict(rules.get(rule_name) or {})


def _confirmations(features: dict) -> tuple[bool, bool, float, int, float | None]:
    flow = int(features.get("flow_direction") or 0)
    trend_score = float(features.get("scanner_trend_score") or 0.0)
    price_vs_vwap = f(features.get("price_vs_vwap_pct"))
    bullish = flow > 0 or trend_score >= 8 or (price_vs_vwap is not None and price_vs_vwap > 0 and trend_score > 0)
    bearish = flow < 0 or trend_score <= -8 or (price_vs_vwap is not None and price_vs_vwap < 0 and trend_score < 0)
    return bullish, bearish, trend_score, flow, price_vs_vwap


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _directional_context(direction: str, features: dict, params: dict) -> tuple[bool, float, list[str]]:
    """Calibrated graph gate and score adjustment.

    All scores are risk-on. Shorts invert them. Missing context is neutral, so a
    relationship is only used when it actually exists in the stored feature set.
    """
    if not config.RELATIONAL_FEATURES_ENABLED:
        return True, 0.0, []
    sign = 1.0 if direction == "long" else -1.0
    stored_graph = f(features.get("graph_context_score"))
    market = f(features.get("market_context_score"))
    sector = f(features.get("sector_etf_score"))
    market_breadth = f(features.get("market_breadth_score"))
    sector_breadth = f(features.get("sector_breadth_score"))
    leveraged = f(features.get("leveraged_confirmation_score"))
    dynamic = f(features.get("relationship_context_score"))
    lead_lag = f(features.get("relationship_lead_lag_score"))
    component_values = [
        (market, float(params.get("market_component_weight", config.DEFAULT_MARKET_COMPONENT_WEIGHT))),
        (sector, float(params.get("sector_component_weight", config.DEFAULT_SECTOR_COMPONENT_WEIGHT))),
        (f(features.get("market_breadth_score")), float(params.get("breadth_component_weight", config.DEFAULT_BREADTH_COMPONENT_WEIGHT)) * 0.5),
        (sector_breadth, float(params.get("breadth_component_weight", config.DEFAULT_BREADTH_COMPONENT_WEIGHT)) * 0.5),
        (leveraged, float(params.get("leveraged_component_weight", config.DEFAULT_LEVERAGED_COMPONENT_WEIGHT))),
        (dynamic, float(params.get("dynamic_component_weight", config.DEFAULT_DYNAMIC_COMPONENT_WEIGHT))),
        (lead_lag, float(params.get("lead_lag_component_weight", config.DEFAULT_LEAD_LAG_COMPONENT_WEIGHT))),
    ]
    usable = [(value, component_weight) for value, component_weight in component_values if value is not None and component_weight > 0]
    graph = (
        sum(float(value) * component_weight for value, component_weight in usable) / sum(component_weight for _, component_weight in usable)
        if usable else (0.0 if any(component_weight == 0 for _, component_weight in component_values) else stored_graph)
    )
    deterioration = f(features.get("relational_deterioration_score"), 0.0) or 0.0
    weight = float(params.get("context_weight", config.DEFAULT_CONTEXT_WEIGHT))
    min_context = float(params.get("min_context_score", config.DEFAULT_MIN_CONTEXT_SCORE))
    min_market = float(params.get("min_market_score", config.DEFAULT_MIN_MARKET_SCORE))
    min_sector = float(params.get("min_sector_score", config.DEFAULT_MIN_SECTOR_SCORE))
    max_adverse = float(params.get("max_adverse_context", config.DEFAULT_MAX_ADVERSE_CONTEXT))
    directional_graph = sign * graph if graph is not None else 0.0
    directional_market = sign * market if market is not None else 0.0
    directional_sector = sign * sector if sector is not None else 0.0
    if config.RELATIONAL_CONTEXT_HARD_BLOCK:
        if graph is not None and directional_graph < min_context:
            return False, 0.0, [f"contexto grafo adverso {directional_graph:.1f}"]
        if market is not None and directional_market < min_market:
            return False, 0.0, [f"mercado adverso {directional_market:.1f}"]
        if sector is not None and directional_sector < min_sector:
            return False, 0.0, [f"ETF sectorial adverso {directional_sector:.1f}"]
        if deterioration > max_adverse:
            return False, 0.0, [f"deterioro relacional {deterioration:.1f}"]
    adjustment = _clamp(directional_graph * weight * 0.30, -25.0, 25.0)
    reasons: list[str] = []
    if abs(adjustment) >= 2.0:
        reasons.append(f"contexto mercado/sector {directional_graph:+.1f}")
    sector_etf = features.get("sector_etf")
    if sector_etf and abs(directional_sector) >= 15:
        reasons.append(f"{sector_etf} {'confirma' if directional_sector > 0 else 'deteriora'}")
    if leveraged is not None and abs(leveraged) >= 20:
        directional_lev = sign * leveraged
        reasons.append(f"ETF apalancados {'confirman' if directional_lev > 0 else 'divergen'}")
    if lead_lag is not None and abs(sign * lead_lag) >= 15:
        reasons.append(f"nodos líderes {'confirman' if sign * lead_lag > 0 else 'deterioran'}")
    return True, adjustment, reasons


def _daily_projection(
    rule_name: str,
    features: dict,
    params: dict,
    expiry_signal: float | None,
    total_change: float | None,
    rvol: float | None,
    score: float,
) -> float:
    """Estimate how much of the calibrated daily-close signature is already present.

    100 means the live state already matches or exceeds the daily thresholds. Values
    below 100 are partial intraday completions and may still be tradable when the
    calibrated trigger factor allows anticipation before the close.
    """
    components: list[float] = []
    min_score = max(1.0, float(params.get("min_score", config.DEFAULT_MIN_SCORE)))
    components.append(_clamp(score / min_score, 0.0, 1.35))

    min_rvol = float(params.get("min_rvol", config.DEFAULT_MIN_RVOL))
    if min_rvol > 0 and rvol is not None:
        components.append(_clamp(rvol / min_rvol, 0.0, 1.35))

    expiry_threshold = max(0.01, float(params.get("expiry_change_pct", config.DEFAULT_EXPIRY_CHANGE_PCT)))
    strike_threshold = max(0.01, float(params.get("strike_change_pct", config.DEFAULT_STRIKE_CHANGE_PCT)))
    direction = RULE_DIRECTIONS.get(rule_name)
    if expiry_signal is not None:
        directional = expiry_signal if direction == "long" else -expiry_signal
        components.append(_clamp(directional / expiry_threshold, 0.0, 1.50))
    if total_change is not None and "WALL" not in rule_name:
        directional = total_change if direction == "long" else -total_change
        components.append(_clamp(directional / strike_threshold, 0.0, 1.50))

    if not components:
        return 0.0
    return round(sum(components) / len(components) * 100.0, 2)




def _evaluate_discovered_rule(rule_name: str, features: dict, params: dict, mode: str) -> dict | None:
    direction = CORE_RULE_DIRECTIONS.get(rule_name)
    conditions = list(params.get("conditions") or [])
    if not direction or not conditions or params.get("enabled") is False:
        return None
    price = f(features.get("price"))
    if price is None or price < config.MIN_PRICE:
        return None
    reasons=[]; margins=[]
    for condition in conditions:
        feature=str(condition.get("feature") or ""); op=str(condition.get("op") or ""); threshold=f(condition.get("threshold"))
        value=f(features.get(feature))
        if value is None or threshold is None:
            return None
        matched = value >= threshold if op == ">=" else value <= threshold
        if not matched:
            return None
        scale=max(abs(threshold),1.0)
        margin=(value-threshold)/scale if op==">=" else (threshold-value)/scale
        margins.append(max(0.0,min(1.0,margin)))
        reasons.append(f"{feature} {op} {threshold:.2f} (actual {value:.2f})")
    stats=params.get("discovery_stats") or {}
    precision=float(stats.get("precision") or 0.0); lift=float(stats.get("lift") or 0.0)
    score=min(100.0,55.0+precision*25.0+min(lift,3.0)*5.0+(sum(margins)/len(margins) if margins else 0.0)*5.0)
    min_score=float(params.get("min_score",70.0))
    if score < min_score:
        return None
    projection=min(130.0,max(100.0,score/min_score*100.0))
    if mode=="intraday" and projection < float(params.get("min_daily_pattern_projection",100.0)):
        return None
    reasons.append(f"patrón mover/control precision {precision*100:.1f}% lift {lift:.2f}x")
    return {"rule_name":rule_name,"direction":direction,"score":round(score,2),"daily_pattern_projection_pct":round(projection,2),"reason":"; ".join(reasons),"target_pct":_effective_target_pct(params),"stop_pct": _effective_stop_pct(params),"max_hold_sessions":min(int(params.get("hold_sessions",config.DEFAULT_HOLD_SESSIONS)),int(config.MAX_PATTERN_HORIZON_SESSIONS))}


def _evaluate_core_rule(rule_name: str, features: dict, params: dict, mode: str) -> dict | None:
    direction = CORE_RULE_DIRECTIONS.get(rule_name)
    if not direction or params.get("enabled") is False:
        return None
    price = f(features.get("price"))
    if price is None or price < config.MIN_PRICE:
        return None
    sign = 1.0 if direction == "long" else -1.0
    momentum = f(features.get("momentum_score"), 0.0) or 0.0
    trend_score = f(features.get("scanner_trend_score"), momentum) or momentum
    rvol = f(features.get("rvol"))
    rsi = f(features.get("rsi"))
    flow = int(f(features.get("flow_direction"), 0.0) or 0)
    change = f(features.get("change_percent"), 0.0) or 0.0
    rs_market = f(features.get("relative_strength_vs_market"), 0.0) or 0.0
    rs_sector = f(features.get("relative_strength_vs_sector"), 0.0) or 0.0
    context = f(features.get("graph_context_score"), 0.0) or 0.0

    min_momentum = float(params.get("core_min_momentum", 55.0))
    min_rvol = float(params.get("min_rvol", 0.0))
    min_rs = float(params.get("core_min_relative_strength", 0.0))
    min_context = float(params.get("min_context_score", config.DEFAULT_MIN_CONTEXT_SCORE))
    require_flow = int(params.get("core_require_flow", 0))

    directional_momentum = sign * trend_score
    directional_change = sign * change
    directional_rs = sign * ((rs_market + rs_sector) / 2.0)
    directional_context = sign * context
    if min_rvol > 0 and rvol is not None and rvol < min_rvol:
        return None
    if directional_context < min_context:
        return None
    if require_flow and sign * flow <= 0:
        return None

    reasons: list[str] = []
    score = 35.0
    if rule_name in {"CORE_FLOW_MOMENTUM_LONG", "CORE_FLOW_MOMENTUM_SHORT"}:
        if sign * flow <= 0:
            return None
        if directional_momentum < max(35.0, min_momentum - 10.0):
            return None
        if directional_change <= 0:
            return None
        score += 18.0 + min(25.0, directional_momentum * 0.30) + min(12.0, directional_change * 4.0)
        reasons = [f"flow {'alcista' if direction == 'long' else 'bajista'}", f"momentum {directional_momentum:.1f}"]
    elif rule_name in {"CORE_RELATIVE_STRENGTH_LONG", "CORE_RELATIVE_STRENGTH_SHORT"}:
        if directional_rs < min_rs:
            return None
        if directional_momentum < max(30.0, min_momentum - 15.0):
            return None
        if directional_change <= 0:
            return None
        score += min(25.0, directional_rs * 0.8) + min(22.0, directional_momentum * 0.25)
        reasons = [f"fortaleza relativa {directional_rs:+.1f}", f"momentum {directional_momentum:.1f}"]
        if sign * flow > 0:
            score += 8.0
            reasons.append("flow confirma")
    else:
        return None

    score += max(-15.0, min(15.0, directional_context * float(params.get("context_weight", 0.5)) * 0.20))
    if rvol is not None and rvol >= max(1.0, min_rvol):
        score += min(8.0, (rvol - 1.0) * 8.0)
        reasons.append(f"RVOL {rvol:.2f}")
    min_score = float(params.get("min_score", config.DEFAULT_MIN_SCORE))
    if score < min_score:
        return None

    # CORE patterns are calibrated at daily close. During the session the same
    # policy acts conservatively until enough intraday ZIPs validate timing.
    projection = min(130.0, max(0.0, score / max(min_score, 1.0) * 100.0))
    min_projection = float(params.get("min_daily_pattern_projection", config.UNVALIDATED_MIN_DAILY_PATTERN_PROJECTION))
    if mode == "intraday":
        if direction == "long" and change <= 0:
            return None
        if direction == "short" and change >= 0:
            return None
        if projection < min_projection:
            return None
        reasons.append(f"patrón CORE intradía {projection:.0f}%")

    return {
        "rule_name": rule_name,
        "direction": direction,
        "score": round(min(score, 100.0), 2),
        "daily_pattern_projection_pct": round(projection, 2),
        "reason": "; ".join(reasons),
        "target_pct": _effective_target_pct(params),
        "stop_pct": _effective_stop_pct(params),
        "max_hold_sessions": min(
            int(params.get("hold_sessions", config.DEFAULT_HOLD_SESSIONS)),
            int(config.MAX_PATTERN_HORIZON_SESSIONS),
        ),
    }



def evaluate_rule(rule_name: str, features: dict, params: dict, mode: str = "daily_close") -> dict | None:
    if rule_name.startswith("DISCOVERED_MOVER_"):
        return _evaluate_discovered_rule(rule_name, features, params, mode)
    if rule_name in CORE_RULE_DIRECTIONS:
        return _evaluate_core_rule(rule_name, features, params, mode)
    if params.get("enabled") is False:
        return None
    direction = RULE_DIRECTIONS.get(rule_name)
    if not direction:
        return None
    price = f(features.get("price"))
    if price is None or price < config.MIN_PRICE:
        return None

    min_score = float(params.get("min_score", config.DEFAULT_MIN_SCORE))
    min_rvol = float(params.get("min_rvol", config.DEFAULT_MIN_RVOL))
    expiry_threshold = float(params.get("expiry_change_pct", config.DEFAULT_EXPIRY_CHANGE_PCT))
    strike_threshold = float(params.get("strike_change_pct", config.DEFAULT_STRIKE_CHANGE_PCT))
    near_wall = float(params.get("near_wall_pct", config.DEFAULT_NEAR_WALL_PCT))
    break_wall = float(params.get("break_wall_pct", config.DEFAULT_BREAK_WALL_PCT))
    intraday_factor = float(params.get("intraday_threshold_factor", config.DEFAULT_INTRADAY_THRESHOLD_FACTOR))
    if mode == "intraday" and config.INTRADAY_ANTICIPATION_ENABLED:
        expiry_gate = expiry_threshold * intraday_factor
        strike_gate = strike_threshold * intraday_factor
    else:
        expiry_gate = expiry_threshold
        strike_gate = strike_threshold

    rvol = f(features.get("rvol"))
    if min_rvol > 0 and rvol is not None and rvol < min_rvol:
        return None

    bullish, bearish, trend_score, _flow, price_vs_vwap = _confirmations(features)
    change_percent = f(features.get("change_percent"))
    if mode == "intraday" and config.INTRADAY_REQUIRE_DIRECTION_CONFIRMATION:
        if direction == "long":
            intraday_direction_ok = bullish and (
                (price_vs_vwap is not None and price_vs_vwap > 0)
                or (change_percent is not None and change_percent > 0)
            )
        else:
            intraday_direction_ok = bearish and (
                (price_vs_vwap is not None and price_vs_vwap < 0)
                or (change_percent is not None and change_percent < 0)
            )
        if not intraday_direction_ok:
            return None

    rvol_bonus = 8.0 if rvol is not None and rvol >= max(min_rvol, 1.0) else 0.0
    short_gex = f(features.get("short_gex"))
    short_change = f(features.get("short_gex_change_pct"))
    front_change = f(features.get("front_gex_change_pct"))
    total_change = f(features.get("total_gex_change_pct"))
    put_wall_pct = f(features.get("put_wall_pct"))
    call_wall_pct = f(features.get("call_wall_pct"))
    reasons: list[str] = []
    score: float | None = None

    expiry_signal = front_change if front_change is not None else short_change

    if rule_name == "PUT_WALL_DEFENSE_LONG":
        if put_wall_pct is None or not (-near_wall <= put_wall_pct <= break_wall) or not bullish:
            return None
        score = 48 + rvol_bonus + max(0.0, trend_score)
        reasons = [f"put wall {put_wall_pct:.2f}%", "scanner alcista"]
        if short_gex is not None and short_gex > 0:
            score += 10; reasons.append("GEX 0-7d positivo")
        if expiry_signal is not None and expiry_signal >= expiry_gate:
            score += 10; reasons.append(f"GEX corto +{expiry_signal:.1f}%")

    elif rule_name == "CALL_WALL_REJECTION_SHORT":
        if call_wall_pct is None or not (-break_wall <= call_wall_pct <= near_wall) or not bearish:
            return None
        score = 48 + rvol_bonus + max(0.0, -trend_score)
        reasons = [f"call wall {call_wall_pct:.2f}%", "scanner bajista"]
        if short_gex is not None and short_gex < 0:
            score += 10; reasons.append("GEX 0-7d negativo")
        if expiry_signal is not None and expiry_signal <= -expiry_gate:
            score += 10; reasons.append(f"GEX corto {expiry_signal:.1f}%")

    elif rule_name == "CALL_WALL_BREAK_LONG":
        if call_wall_pct is None or call_wall_pct >= -break_wall or not bullish:
            return None
        score = 52 + rvol_bonus + max(0.0, trend_score)
        reasons = [f"{abs(call_wall_pct):.2f}% sobre call wall", "ruptura confirmada"]
        if expiry_signal is not None and expiry_signal >= expiry_gate:
            score += 10; reasons.append("GEX corto acompaña")

    elif rule_name == "PUT_WALL_BREAK_SHORT":
        if put_wall_pct is None or put_wall_pct <= break_wall or not bearish:
            return None
        score = 52 + rvol_bonus + max(0.0, -trend_score)
        reasons = [f"{put_wall_pct:.2f}% bajo put wall", "pérdida confirmada"]
        if expiry_signal is not None and expiry_signal <= -expiry_gate:
            score += 10; reasons.append("GEX corto acompaña")

    elif rule_name == "EXPIRY_GEX_SHIFT_LONG":
        if expiry_signal is None or expiry_signal < expiry_gate or not bullish:
            return None
        score = 45 + min(20.0, expiry_signal / 5.0) + rvol_bonus + max(0.0, trend_score)
        reasons = [f"GEX 0-7d +{expiry_signal:.1f}%", "scanner al alza"]
        if total_change is not None and total_change >= strike_gate:
            score += 10; reasons.append(f"GEX strike +{total_change:.1f}%")

    elif rule_name == "EXPIRY_GEX_SHIFT_SHORT":
        if expiry_signal is None or expiry_signal > -expiry_gate or not bearish:
            return None
        score = 45 + min(20.0, abs(expiry_signal) / 5.0) + rvol_bonus + max(0.0, -trend_score)
        reasons = [f"GEX 0-7d {expiry_signal:.1f}%", "scanner a la baja"]
        if total_change is not None and total_change <= -strike_gate:
            score += 10; reasons.append(f"GEX strike {total_change:.1f}%")

    if score is None:
        return None
    context_ok, context_adjustment, context_reasons = _directional_context(direction, features, params)
    if not context_ok:
        return None
    score += context_adjustment
    reasons.extend(context_reasons)
    if score < min_score:
        return None

    projection = _daily_projection(rule_name, features, params, expiry_signal, total_change, rvol, score)
    min_projection = float(params.get("min_daily_pattern_projection", config.DEFAULT_MIN_DAILY_PATTERN_PROJECTION))
    if mode == "intraday" and projection < min_projection:
        return None
    if mode == "intraday":
        reasons.append(f"anticipación cierre diario {projection:.0f}%")

    return {
        "rule_name": rule_name,
        "direction": direction,
        "score": round(min(score, 100.0), 2),
        "daily_pattern_projection_pct": projection,
        "reason": "; ".join(reasons),
        "target_pct": _effective_target_pct(params),
        "stop_pct": _effective_stop_pct(params),
        "max_hold_sessions": min(
            int(params.get("hold_sessions", config.DEFAULT_HOLD_SESSIONS)),
            int(config.MAX_PATTERN_HORIZON_SESSIONS),
        ),
    }


def exit_pattern_signal(
    direction: str,
    features: dict,
    params: dict,
    current_return_pct: float,
    best_return_pct: float,
) -> dict | None:
    """Detect a calibrated invalidation/reversal pattern for an open position."""
    reversal_threshold = float(params.get("exit_gex_reversal_pct", config.DEFAULT_EXIT_GEX_REVERSAL_PCT))
    score_threshold = float(params.get("exit_pattern_score", config.DEFAULT_EXIT_PATTERN_SCORE))
    trailing_activation = float(params.get("trailing_activation_pct", config.DEFAULT_TRAILING_ACTIVATION_PCT))
    trailing_giveback = float(params.get("trailing_giveback_pct", config.DEFAULT_TRAILING_GIVEBACK_PCT))

    if (
        getattr(config, "PATTERN_TRAILING_EXIT_ENABLED", False)
        and best_return_pct >= trailing_activation
        and current_return_pct <= best_return_pct - trailing_giveback
    ):
        return {
            "reason": "TRAILING_PATTERN",
            "score": 100.0,
            "detail": f"cede {best_return_pct-current_return_pct:.2f}% desde MFE {best_return_pct:.2f}%",
        }

    bullish, bearish, trend_score, flow, _pvwap = _confirmations(features)
    front_change = f(features.get("front_gex_change_pct"))
    short_change = f(features.get("short_gex_change_pct"))
    total_change = f(features.get("total_gex_change_pct"))
    expiry_signal = front_change if front_change is not None else short_change
    put_wall_pct = f(features.get("put_wall_pct"))
    call_wall_pct = f(features.get("call_wall_pct"))
    break_wall = float(params.get("break_wall_pct", config.DEFAULT_BREAK_WALL_PCT))

    score = 0.0
    reasons: list[str] = []
    if direction == "long":
        if expiry_signal is not None and expiry_signal <= -reversal_threshold:
            score += 30 + min(20.0, abs(expiry_signal) / 4.0)
            reasons.append(f"GEX expiry revierte {expiry_signal:.1f}%")
        if total_change is not None and total_change <= -reversal_threshold:
            score += 20 + min(15.0, abs(total_change) / 5.0)
            reasons.append(f"GEX strike revierte {total_change:.1f}%")
        if bearish:
            score += 20 + max(0.0, min(15.0, -trend_score))
            reasons.append("scanner bajista")
        if flow < 0:
            score += 10
            reasons.append("flow bajista")
        if config.EXIT_WALL_INVALIDATION_ENABLED and put_wall_pct is not None and put_wall_pct > break_wall:
            score += 45
            reasons.append(f"pierde put wall {put_wall_pct:.2f}%")
    else:
        if expiry_signal is not None and expiry_signal >= reversal_threshold:
            score += 30 + min(20.0, abs(expiry_signal) / 4.0)
            reasons.append(f"GEX expiry revierte +{expiry_signal:.1f}%")
        if total_change is not None and total_change >= reversal_threshold:
            score += 20 + min(15.0, abs(total_change) / 5.0)
            reasons.append(f"GEX strike revierte +{total_change:.1f}%")
        if bullish:
            score += 20 + max(0.0, min(15.0, trend_score))
            reasons.append("scanner alcista")
        if flow > 0:
            score += 10
            reasons.append("flow alcista")
        if config.EXIT_WALL_INVALIDATION_ENABLED and call_wall_pct is not None and call_wall_pct < -break_wall:
            score += 45
            reasons.append(f"supera call wall {call_wall_pct:.2f}%")

    relational_exit_threshold = float(params.get("relational_exit_score", config.DEFAULT_RELATIONAL_EXIT_SCORE))
    graph = f(features.get("graph_context_score"))
    deterioration = f(features.get("relational_deterioration_score"), 0.0) or 0.0
    sector_score = f(features.get("sector_etf_score"))
    sign = 1.0 if direction == "long" else -1.0
    adverse_graph = -(sign * graph) if graph is not None else 0.0
    adverse_sector = -(sign * sector_score) if sector_score is not None else 0.0
    relational_signal = max(0.0, adverse_graph) * 0.45 + max(0.0, adverse_sector) * 0.25 + deterioration * 0.30
    broad_warning_active = bool(
        features.get("market_close_all_longs_active") if direction == "long"
        else features.get("market_close_all_shorts_active")
    )
    local_gex_state = ticker_gex_confirmation_state(direction, features)
    # The broad warning is anticipatory. During an active FULL CLOSE signal,
    # market/sector propagation cannot become an individual exit contribution
    # until the ticker's own GEX has actually reversed. Scanner/flow/CORE and
    # hard wall invalidations remain independent ticker-specific evidence.
    if relational_signal >= relational_exit_threshold:
        if broad_warning_active and not bool(local_gex_state.get("confirmed")):
            reasons.append(
                f"presión mercado/sector {relational_signal:.1f}, pero GEX ticker {local_gex_state.get('state')} aún no confirma"
            )
        else:
            score += min(50.0, relational_signal * 0.65)
            reasons.append(f"deterioro mercado/sector {relational_signal:.1f}")

    if score < score_threshold:
        return None
    local_exit_gate = broad_signal_local_exit_gate(direction, features)
    if not bool(local_exit_gate.get("exit_allowed")):
        # During an active CLOSE ALL warning, scanner/flow/CORE/sector can warn
        # but cannot close the ticker before its own GEX confirms reversal.
        return None
    wall_invalidation = any("wall" in reason for reason in reasons)
    return {
        "reason": "PATTERN_REVERSAL",
        "score": round(min(score, 100.0), 2),
        "detail": "; ".join(reasons),
        "hard": bool(wall_invalidation or score >= score_threshold + 30.0),
    }


def structural_runner_exit_signal(
    direction: str,
    entry_features: dict,
    current_features: dict,
    *,
    target_touched: bool,
) -> dict | None:
    """Detect CORE/GEX exhaustion for a runner without using price-only exits.

    The signal is intentionally evidence-based: target touch only activates the
    evaluation. A close requires multiple adverse structural blocks and is soft
    unless the active CORE direction has clearly reversed with high score.
    """
    direction = str(direction or "long").lower()
    opposite = "short" if direction == "long" else "long"
    active_direction = str(current_features.get("active_signal_direction") or "").lower()
    active_core_score = f(current_features.get("active_core_score"), 0.0) or 0.0
    active_state = str(current_features.get("active_signal_state") or "").upper()
    intent = str(current_features.get("gex_intent") or current_features.get("active_gex_intent") or "").upper()
    flow_direction = int(current_features.get("flow_direction") or 0)
    scanner = f(current_features.get("scanner_trend_score"), 0.0) or 0.0
    expiry = f(current_features.get("front_gex_change_pct"))
    if expiry is None:
        expiry = f(current_features.get("short_gex_change_pct"))
    total = f(current_features.get("total_gex_change_pct"))
    migration = target_migration_status(entry_features, current_features, direction)

    score = 0.0
    reasons: list[str] = []
    if active_direction == opposite and active_core_score >= 65.0:
        score += 45.0
        reasons.append(f"CORE activo revierte a {opposite.upper()} con score {active_core_score:.1f}")
    elif active_direction == opposite:
        score += 25.0
        reasons.append(f"CORE activo apunta a {opposite.upper()}")
    if active_state in {"DIVERGENCE", "MARKET_BLOCKED"}:
        score += 15.0
        reasons.append(f"estado activo {active_state}")
    if intent in {"HEDGING", "UNCLEAR"}:
        score += 12.0
        reasons.append(f"GEX {intent}")
    if direction == "long":
        if flow_direction < 0:
            score += 12.0; reasons.append("flow contrario")
        if scanner <= -12:
            score += 12.0; reasons.append(f"scanner {scanner:.1f}")
        if expiry is not None and expiry <= -config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            score += 18.0; reasons.append(f"GEX expiry {expiry:+.1f}%")
        if total is not None and total <= -config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            score += 15.0; reasons.append(f"GEX total {total:+.1f}%")
    else:
        if flow_direction > 0:
            score += 12.0; reasons.append("flow contrario")
        if scanner >= 12:
            score += 12.0; reasons.append(f"scanner {scanner:.1f}")
        if expiry is not None and expiry >= config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            score += 18.0; reasons.append(f"GEX expiry {expiry:+.1f}%")
        if total is not None and total >= config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            score += 15.0; reasons.append(f"GEX total {total:+.1f}%")

    if "migra a favor" in migration:
        score -= 30.0
        reasons.append("wall/objetivo migra a favor")
    elif target_touched:
        reasons.append("zona objetivo original alcanzada")

    if not target_touched and active_direction != opposite:
        return None
    if score < 50.0:
        return None
    local_exit_gate = broad_signal_local_exit_gate(direction, current_features)
    if not bool(local_exit_gate.get("exit_allowed")):
        return None
    reason = "CORE_REVERSAL" if active_direction == opposite and active_core_score >= 65.0 else "GEX_EXHAUSTION"
    return {
        "reason": reason,
        "score": round(min(100.0, score), 2),
        "detail": "; ".join(dict.fromkeys(reasons)),
        "hard": bool(score >= 80.0),
    }

def evaluate_features(features: dict, model_params: dict, mode: str = "daily_close") -> list[dict]:
    candidates: list[dict] = []
    configured = model_params.get("rules") or {}
    rule_names = [name for name in configured if name in RULE_DIRECTIONS]
    if not rule_names:
        rule_names = list(FULL_RULE_DIRECTIONS)
    for rule_name in rule_names:
        result = evaluate_rule(rule_name, features, params_for_rule(model_params, rule_name), mode=mode)
        if result:
            candidates.append(result)
    return candidates


def build_event(snapshot: dict, gex: dict | None, features: dict, candidate: dict, model: dict, live: bool) -> dict:
    features = dict(features)
    # Persist the complete decision path so lifecycle/reports can distinguish a
    # statistical CORE label from an actionable GEX target.
    metadata_fields = (
        "daily_pattern_projection_pct", "precision_confirmation_count",
        "precision_confirming_blocks", "precision_rank_score",
        "precision_rule_oos_sample", "precision_rule_oos_win_rate",
        "precision_rule_oos_avg_return", "precision_rule_oos_profit_factor",
        "layer", "signal_origin", "state", "actionable", "target_source",
        "target_zone_low", "target_zone_high", "target_price", "target_pct",
        "timing_min_sessions", "timing_max_sessions", "expiry_date",
        "probability_pct", "core_rule", "core_score", "gex_score",
        "market_adjustment", "rationale", "primary_engine", "contributor_engines",
        "model_lineage", "core_model_id", "core_model_key", "core_model_status",
        "gex_daily_model_status", "gex_intraday_model_status",
        "graph_model_status", "fusion_model_status", "graph_source",
    )
    for field in metadata_fields:
        if candidate.get(field) is not None:
            features[field] = candidate.get(field)
    features.setdefault("precision_confirming_blocks", candidate.get("precision_confirming_blocks") or [])
    now_utc = ts_utc_str()
    now_mad = ts_madrid_str()
    event_date = snapshot.get("snapshot_date") or now_mad[:10]
    target_pct = float(candidate.get("target_pct") or 0.0)
    max_hold = int(candidate.get("timing_max_sessions") or candidate.get("max_hold_sessions") or config.MAX_PATTERN_HORIZON_SESSIONS)
    return {
        "event_at_utc": now_utc,
        "event_at_madrid": now_mad,
        "snapshot_date": event_date,
        "source_scanner_run_id": snapshot.get("scanner_run_id"),
        "source_gex_run_id": candidate.get("source_gex_run_id") or (gex or {}).get("gex_run_id"),
        "model_version_id": model.get("id"),
        "ticker": snapshot.get("ticker"),
        "rule_name": candidate["rule_name"],
        "direction": candidate["direction"],
        "score": candidate["score"],
        "current_price": features.get("price"),
        "target_pct": target_pct,
        "stop_pct": 0.0,
        "max_hold_sessions": min(max_hold, int(config.MAX_PATTERN_HORIZON_SESSIONS)),
        "reason": candidate.get("reason") or candidate.get("rationale") or "layered setup",
        "features_json": json.dumps(features, ensure_ascii=False),
        "status": "open" if live else "shadow",
        "sent_to_discord": 0,
        "discord_message": None,
        "unique_key": f"{event_date}|{snapshot.get('ticker')}|{candidate['rule_name']}|{snapshot.get('scanner_run_id')}|{model.get('id')}",
    }


def _fmt_num(value: Any, decimals: int = 2) -> str:
    number = f(value)
    if number is None:
        return "n/a"
    if decimals <= 0:
        return str(int(round(number)))
    text_value = f"{number:.{decimals}f}"
    return text_value.rstrip("0").rstrip(".")


def _fmt_pct(value: Any, decimals: int = 1, signed: bool = True) -> str:
    number = f(value)
    if number is None:
        return "n/a"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}{number:.{decimals}f}%"


def _date_label(value: Any, include_time: bool = False) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d/%m %H:%M" if include_time else "%d/%m")
    except Exception:
        raw = str(value or "")
        if len(raw) >= 10:
            try:
                return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%d/%m")
            except Exception:
                pass
        return raw or "n/a"


def _side_label(direction: str) -> tuple[str, str]:
    return ("Call", "alcista") if str(direction).lower() == "long" else ("Put", "bajista")


def _flow_label(features: dict, direction: str | None = None) -> str:
    """Render the exact TMAD flow evidence without inventing Spidey labels."""
    direction = str(direction or "").lower()
    bias = str(features.get("profile_bias") or "").strip()
    option = str(features.get("option_signal") or "").strip()
    unusual = str(features.get("unusual_flow_signal") or "").strip()
    flow_direction = int(features.get("flow_direction") or 0)
    parts: list[str] = []
    if bias:
        parts.append(f"bias `{bias}`")
    if option and option.lower() not in {"neutral", "none", "n/a"}:
        parts.append(f"option `{option}`")
    if unusual and unusual.lower() not in {"neutral", "none", "n/a"}:
        parts.append(f"unusual `{unusual}`")
    if flow_direction:
        parts.append(f"flow_direction `{flow_direction:+d}`")
    if not parts:
        parts.append("flujo sin lectura concluyente")
    return " · ".join(parts)


def _preferred_wall(features: dict, side: str) -> tuple[str | None, float | None]:
    """Return the exact actionable wall name and price available for one side."""
    keys = (
        (("near_call_wall", "near_call_wall_strike"), ("swing_call_wall", "swing_call_wall_strike"), ("all_call_wall", "all_call_wall_strike"), ("scanner_call_wall", "call_wall"))
        if side == "call" else
        (("near_put_wall", "near_put_wall_strike"), ("swing_put_wall", "swing_put_wall_strike"), ("all_put_wall", "all_put_wall_strike"), ("scanner_put_wall", "put_wall"))
    )
    for label, key in keys:
        value = f(features.get(key))
        if value is not None:
            return label, value
    return None, None


def _list_value(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [f"{key}={val}" for key, val in value.items()]
    raw = str(value).strip()
    return [raw] if raw else []


def _line(label: str, value: Any, *, code: bool = True) -> str | None:
    if value in (None, "", [], {}):
        return None
    rendered = str(value)
    return f"• **{label}:** `{rendered}`" if code else f"• **{label}:** {rendered}"


def _num_line(label: str, value: Any, decimals: int = 1, *, suffix: str = "") -> str | None:
    number = f(value)
    if number is None:
        return None
    return f"• **{label}:** `{_fmt_num(number, decimals)}{suffix}`"


def _pct_line(label: str, value: Any, decimals: int = 1, *, signed: bool = True) -> str | None:
    number = f(value)
    if number is None:
        return None
    return f"• **{label}:** `{_fmt_pct(number, decimals, signed=signed)}`"


def _section(title: str, lines: list[str | None]) -> str:
    clean = [str(item) for item in lines if item]
    return "" if not clean else "\n".join([f"### {title}", *clean])


def _target_zone_values(features: dict) -> tuple[float | None, float | None]:
    low = f(features.get("target_zone_low"))
    high = f(features.get("target_zone_high"))
    if low is None and high is None:
        target = f(features.get("target_price") or features.get("wall_target"))
        low = high = target
    return low, high


def target_migration_status(entry_features: dict, current_features: dict, direction: str) -> str:
    """Describe target migration; touching a target never means automatic exit."""
    entry_low, entry_high = _target_zone_values(entry_features)
    current_low, current_high = _target_zone_values(current_features)
    if entry_low is None or entry_high is None:
        return "objetivo original no disponible"
    if current_low is None or current_high is None:
        return "objetivo actual no disponible; mantener hasta confirmación estructural"
    entry_mid = (entry_low + entry_high) / 2.0
    current_mid = (current_low + current_high) / 2.0
    if str(direction).lower() == "long":
        delta = (current_mid / entry_mid - 1.0) * 100.0 if entry_mid else 0.0
        state = "migra a favor" if delta > 0.25 else ("retrocede" if delta < -0.25 else "estable")
    else:
        delta = (entry_mid / current_mid - 1.0) * 100.0 if current_mid else 0.0
        state = "migra a favor" if delta > 0.25 else ("retrocede" if delta < -0.25 else "estable")
    return f"{state} `{delta:+.1f}%` · original `{_format_target_zone(entry_low, entry_high).strip('`')}` · actual `{_format_target_zone(current_low, current_high).strip('`')}`"


def _native_core_lines(features: dict) -> list[str | None]:
    model_id = features.get("core_model_id")
    model_status = str(features.get("core_model_status") or "").upper()
    model_key = features.get("core_model_key")
    model_text = None
    if model_id not in (None, "", 0, "0") or model_key or model_status:
        bits = [f"#{model_id}" if model_id not in (None, "", 0, "0") else None, model_status or None, str(model_key) if model_key else None]
        model_text = " · ".join(bit for bit in bits if bit)
    return [
        _line("Modelo", model_text),
        _line("Regla CORE", features.get("core_rule")),
        _num_line("Score CORE", features.get("core_score"), 1),
        _pct_line("P(±5%≤5 sesiones)", features.get("probability_pct"), 1, signed=False),
        _pct_line("Proyección patrón diario", features.get("daily_pattern_projection_pct"), 1, signed=False),
        _num_line("Rank CORE", features.get("core_rank_score"), 1),
        _num_line("Ventaja direccional", features.get("core_directional_edge"), 1),
        _num_line("Sobreextensión", features.get("overextension_score"), 1),
        _num_line("OOS regla n", features.get("precision_rule_oos_sample"), 0),
        _pct_line("OOS regla win", features.get("precision_rule_oos_win_rate"), 1, signed=False),
        _num_line("OOS regla PF", features.get("precision_rule_oos_profit_factor"), 2),
    ]


def _native_gex_lines(features: dict, direction: str) -> list[str | None]:
    side = "call" if str(direction).lower() == "long" else "put"
    wall_name, wall_price = _preferred_wall(features, side)
    low, high = _target_zone_values(features)
    target = _format_target_zone(low, high).strip("`") if low is not None and high is not None else None
    target_pct = f(features.get("target_pct"))
    target_text = None
    if target:
        target_text = target + (f" · runway `{abs(target_pct):.1f}%`" if target_pct is not None else "")
    expiry = features.get("expiry_date") or features.get("dominant_expiration")
    intent = str(features.get("gex_intent") or "UNCLEAR").upper()
    confirmation_reasons: list[str] = []
    if intent not in {"DIRECTIONAL", "SQUEEZE"}:
        confirmation_reasons.append(f"intent {intent}")
    if features.get("operational_target") is False or features.get("capped"):
        confirmation_reasons.append("target/wall no operativo")
    if str(features.get("target_source") or "").upper() != "GEX_CALIBRATED":
        confirmation_reasons.append(f"target {features.get('target_source') or 'sin fuente'}")
    confirmation_text = (
        "CONFIRMADO PARA LIVE"
        if not confirmation_reasons else
        "NO CONFIRMADO · " + "; ".join(confirmation_reasons)
    )
    return [
        _num_line("Score de perfil GEX", features.get("gex_score"), 1),
        _line("Lectura / intent GEX", gex_intent_display(features.get("gex_intent")) if features.get("gex_intent") else None),
        _line("Confirmación operativa GEX", confirmation_text, code=False),
        _line("Target source", features.get("target_source")),
        _line("Zona objetivo", target, code=False),
        _line("Nivel seleccionado", f"{features.get('level_name')} {_fmt_num(features.get('level_price'))}" if features.get("level_name") else None),
        _line("Wall operativo", f"{wall_name} {_fmt_num(wall_price)}" if wall_name and wall_price is not None else None),
        _num_line("Asimetría wall", features.get("wall_asymmetry"), 2, suffix="x"),
        _pct_line("Reward wall", features.get("wall_reward_pct"), 1, signed=False),
        _pct_line("Risk wall", features.get("wall_risk_pct"), 1, signed=False),
        _line("Bias perfil", features.get("profile_bias")),
        _num_line("Call accumulation", features.get("call_accumulation_score"), 1),
        _num_line("Put accumulation", features.get("put_accumulation_score"), 1),
        _num_line("OTM call volume z", features.get("otm_call_volume_z"), 2),
        _num_line("OTM put volume z", features.get("otm_put_volume_z"), 2),
        _pct_line("ΔGEX total", features.get("total_gex_change_pct"), 1),
        _pct_line("ΔGEX 0-7d", features.get("short_gex_change_pct"), 1),
        _pct_line("ΔGEX 8-56d", features.get("swing_gex_change_pct"), 1),
        _pct_line("ΔGEX front", features.get("front_gex_change_pct"), 1),
        _pct_line("ΔGEX near", features.get("near_gex_change_pct"), 1),
        _num_line("Call wall strength", features.get("call_wall_strength"), 2),
        _num_line("Put wall strength", features.get("put_wall_strength"), 2),
        _num_line("Gamma flip", features.get("gamma_flip"), 2),
        _line("Expiración relevante", expiry),
        _line("Régimen expiración", features.get("expiry_regime")),
        _line("Bucket expiración", features.get("expiry_bucket")),
        _line("Modelo GEX diario", features.get("gex_daily_model_status")),
        _num_line("Lag GEX", features.get("gex_lag_minutes"), 1, suffix=" min"),
    ]


def _native_timing_lines(features: dict) -> list[str | None]:
    blocks = _list_value(features.get("intraday_blocks") or features.get("precision_confirming_blocks"))
    adverse = _list_value(features.get("intraday_adverse"))
    blockers = _list_value(features.get("blockers"))
    return [
        _line("Estado timing", features.get("intraday_state")),
        _num_line("Confirmaciones", features.get("precision_confirmation_count"), 0),
        _line("Bloques confirmados", ", ".join(blocks) if blocks else None, code=False),
        _line("Adversos", ", ".join(adverse) if adverse else None, code=False),
        _line("Gate / bloqueos", ", ".join(blockers) if blockers else None, code=False),
        _line("Modelo timing", features.get("gex_intraday_model_status")),
        _line("Horizonte", (
            f"{int(features.get('timing_min_sessions') or 1)}–{int(features.get('timing_max_sessions') or config.MAX_PATTERN_HORIZON_SESSIONS)} sesiones"
            if features.get("timing_min_sessions") is not None or features.get("timing_max_sessions") is not None else None
        )),
    ]


def _native_scanner_lines(features: dict) -> list[str | None]:
    return [
        _line("Trend", features.get("trend")),
        _num_line("Scanner trend score", features.get("scanner_trend_score"), 1),
        _num_line("Momentum score", features.get("momentum_score"), 1),
        _pct_line("Momentum 5d", features.get("momentum_5d_pct"), 1),
        _num_line("RSI", features.get("rsi"), 1),
        _num_line("RVOL", features.get("rvol"), 2),
        _pct_line("Precio vs VWAP", features.get("price_vs_vwap_pct"), 1),
        _pct_line("Precio vs SMA20", features.get("price_vs_sma20_pct"), 1),
        _line("Option signal", features.get("option_signal")),
        _line("Unusual flow", features.get("unusual_flow_signal")),
        _line("Cambio material de perfil", features.get("profile_material_change")),
        _line("Flujo TMAD", _flow_label(features), code=False),
    ]


def _native_context_lines(features: dict) -> list[str | None]:
    return [
        _line("Régimen mercado", features.get("market_regime")),
        _line("MARKET CORE", (
            f"#{features.get('market_core_model_id') or 'n/a'} {features.get('market_core_model_status') or 'n/a'} · cobertura {int(features.get('market_core_coverage') or 0)}/4"
            if features.get("market_core_model_status") else None
        )),
        _pct_line("P risk-off MARKET CORE", features.get("market_core_risk_off_probability"), 1, signed=False),
        _pct_line("P risk-on MARKET CORE", features.get("market_core_risk_on_probability"), 1, signed=False),
        _num_line("GEX main ETFs risk-on", features.get("main_etf_gex_risk_on_score"), 1),
        _num_line("GEX complejo VIX stress", features.get("vol_gex_stress_score"), 1),
        _line("FULL CLOSE LONGS paralelo", (
            f"ACTIVO · score {float(features.get('market_close_all_longs_score') or 0):.1f} · no cierra este ticker"
            if features.get("market_close_all_longs_active") else None
        ), code=False),
        _line("FULL CLOSE SHORTS paralelo", (
            f"ACTIVO · score {float(features.get('market_close_all_shorts_score') or 0):.1f} · no cierra este ticker"
            if features.get("market_close_all_shorts_active") else None
        ), code=False),
        _line("GEX local para salida", (
            lambda state: f"{state.get('state')} · {'CLOSE habilitado' if state.get('confirmed') else 'CLOSE bloqueado durante señal general'}"
        )(ticker_gex_confirmation_state(
            str(features.get("active_signal_direction") or features.get("direction") or "long"), features
        )) if (features.get("market_close_all_longs_active") or features.get("market_close_all_shorts_active")) else None, code=False),
        _num_line("Mercado", features.get("market_context_score"), 1),
        _num_line("Breadth mercado", features.get("market_breadth_score"), 1),
        _num_line("VIX stress", features.get("vix_stress_score"), 1),
        _num_line("Credit risk-on", features.get("credit_risk_on_score"), 1),
        _line("ETF sector", features.get("sector_etf")),
        _num_line("Sector", features.get("sector_etf_score"), 1),
        _num_line("Breadth sector", features.get("sector_breadth_score"), 1),
        _num_line("Leveraged confirmation", features.get("leveraged_confirmation_score"), 1),
        _num_line("Grafo", features.get("graph_context_score"), 1),
        _num_line("Relaciones", features.get("relationship_context_score"), 1),
        _num_line("Lead/lag", features.get("relationship_lead_lag_score"), 1),
        _num_line("Deterioro relacional", features.get("relational_deterioration_score"), 1),
        _pct_line("Fuerza vs mercado", features.get("relative_strength_vs_market"), 1),
        _pct_line("Fuerza vs sector", features.get("relative_strength_vs_sector"), 1),
        _line("Modelo grafo", features.get("graph_model_status")),
        _line("Modelo fusión", features.get("fusion_model_status")),
    ]


def native_feature_sections(features: dict, direction: str, *, include_core: bool = True) -> list[str]:
    sections = []
    if include_core:
        sections.append(_section("🧠 CORE", _native_core_lines(features)))
    sections.extend([
        _section("🧲 GEX", _native_gex_lines(features, direction)),
        _section("⏱️ TIMING", _native_timing_lines(features)),
        _section("📡 SCANNER / FLOW", _native_scanner_lines(features)),
        _section("🌐 CONTEXTO", _native_context_lines(features)),
    ])
    return [item for item in sections if item]


def feature_comparison_lines(entry_features: dict, current_features: dict, direction: str) -> list[str]:
    comparisons: list[str] = []
    comparisons.append(f"• **Objetivo GEX:** {target_migration_status(entry_features, current_features, direction)}")
    fields = [
        ("CORE score", "core_score", 1),
        ("Probabilidad", "probability_pct", 1),
        ("GEX score", "gex_score", 1),
        ("GEX intent", "gex_intent", None),
        ("Bias", "profile_bias", None),
        ("Call acc", "call_accumulation_score", 1),
        ("Put acc", "put_accumulation_score", 1),
        ("Call z", "otm_call_volume_z", 2),
        ("Put z", "otm_put_volume_z", 2),
        ("ΔGEX total", "total_gex_change_pct", 1),
        ("ΔGEX 0-7d", "short_gex_change_pct", 1),
        ("ΔGEX 8-56d", "swing_gex_change_pct", 1),
        ("Timing", "intraday_state", None),
        ("Scanner trend", "scanner_trend_score", 1),
        ("Mercado", "market_context_score", 1),
        ("Sector", "sector_etf_score", 1),
    ]
    for label, key, decimals in fields:
        before = entry_features.get(key)
        after = current_features.get(key)
        if before in (None, "", [], {}) and after in (None, "", [], {}):
            continue
        if decimals is None:
            if str(before or "") != str(after or ""):
                comparisons.append(f"• **{label}:** entrada `{before or 'n/a'}` → actual `{after or 'n/a'}`")
        else:
            b = f(before); a = f(after)
            if b is None and a is None:
                continue
            comparisons.append(f"• **{label}:** entrada `{_fmt_num(b, decimals) if b is not None else 'n/a'}` → actual `{_fmt_num(a, decimals) if a is not None else 'n/a'}`")
    return comparisons

def _clean_reason(value: Any, limit: int = 190) -> str:
    rendered = " ".join(str(value or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(0, limit - 1)].rstrip() + "…"


def _format_target_zone(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "`pendiente de GEX`"
    if abs(float(low) - float(high)) < 1e-9:
        return f"`{_fmt_num(low)}`"
    return f"`{_fmt_num(low)}–{_fmt_num(high)}`"


def _origin_confirmation_text(origin: str) -> str:
    key = str(origin or "").upper()
    return {
        "CORE_GEX_ALIGNED": "CORE + GEX alineados",
        "GEX_STANDALONE": "GEX independiente",
        "CORE_DIRECTIONAL": "CORE; target GEX pendiente",
        "CORE_GEX_DIVERGENCE": "CORE y GEX divergen",
        "GRAPH_PROPAGATION": "propagación ETF / grafo",
        "FUSION": "fusión de capas",
    }.get(key, key.replace("_", " ").title() or "modelo")


def format_alert_card(event: dict, model: dict | None) -> str:
    model = model or {}
    features = json.loads(event.get("features_json") or "{}")
    direction = str(event.get("direction") or "").lower()
    side = "CALL" if direction == "long" else "PUT"
    source = signal_source_label(features, rule_name=event.get("rule_name"))
    status = str(event.get("status") or "shadow").upper()
    price = f(event.get("current_price"), 0.0) or 0.0
    low, high = _target_zone_values(features)
    target = _format_target_zone(low, high).strip("`") if low is not None and high is not None else "pendiente de GEX"
    target_pct = f(features.get("target_pct"))
    horizon_min = int(features.get("timing_min_sessions") or 1)
    horizon_max = int(features.get("timing_max_sessions") or event.get("max_hold_sessions") or config.MAX_PATTERN_HORIZON_SESSIONS)
    score = f(event.get("score"), 0.0) or 0.0
    confirmations = features.get("confirming_blocks") or features.get("intraday_confirmations") or []
    if not isinstance(confirmations, list): confirmations = [str(confirmations)] if confirmations else []
    rationale = _clean_reason(features.get("rationale") or event.get("reason") or "setup confirmado por capas", 260)
    target_line = f"`{target}`" + (f" · recorrido `{abs(target_pct):.1f}%`" if target_pct is not None else "")
    return "\n".join([
        f"**${str(event.get('ticker') or 'N/A').upper()} · {side} · [{source}] · {status}**",
        f"**Entrada:** `{_fmt_num(price)}` → **target GEX:** {target_line} · **horizonte:** `{horizon_min}–{horizon_max} ses.`",
        f"**Score:** `{score:.0f}` · **confirmaciones:** `{len(confirmations)}` · **modelos:** CORE `#{int(event.get('model_version_id') or model.get('id') or 0)}`",
        f"**Motivo:** {rationale}",
        "±5%/target = milestone; cierre solo por reversión propia CORE/GEX/estructura confirmada.",
    ])

def format_alert_message(event: dict, model: dict) -> str:
    status = str(event.get("status") or "").lower()
    live = status == "open"
    title = "ENTER 🚨 · LIVE" if live else "ENTER 🚨 · SHADOW"
    context = "Entrada operativa validada" if live else "Entrada de aprendizaje · no operativa"
    return build_group(title, [format_alert_card(event, model)], context=context)


def position_update_assessment(direction: str, features: dict, current_return_pct: float) -> dict | None:
    bullish, bearish, _trend_score_value, _flow, price_vs_vwap = _confirmations(features)
    front = f(features.get("front_gex_change_pct"))
    short = f(features.get("short_gex_change_pct"))
    total = f(features.get("total_gex_change_pct"))
    expiry = front if front is not None else short
    reasons: list[str] = []
    deterioration = False
    favorable = False

    if direction == "long":
        if current_return_pct <= -config.POSITION_DETERIORATION_ADVERSE_PCT:
            deterioration = True; reasons.append(f"movimiento adverso {_fmt_pct(current_return_pct)} vs entry")
        if bearish:
            deterioration = True; reasons.append("scanner/flow gira bajista")
        if expiry is not None and expiry <= -config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            deterioration = True; reasons.append(f"GEX expiry se deteriora {_fmt_pct(expiry)}")
        if total is not None and total <= -config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            deterioration = True; reasons.append(f"GEX strike se deteriora {_fmt_pct(total)}")
        if price_vs_vwap is not None and price_vs_vwap < 0:
            reasons.append("Actual por debajo de VWAP")
        if current_return_pct >= config.POSITION_UPDATE_MIN_FAVORABLE_PCT and bullish:
            favorable = True; reasons.append("setup avanza con confirmación alcista")
        if expiry is not None and expiry >= config.DEFAULT_EXPIRY_CHANGE_PCT and current_return_pct >= 0:
            favorable = True; reasons.append(f"GEX expiry acompaña {_fmt_pct(expiry)}")
    else:
        if current_return_pct <= -config.POSITION_DETERIORATION_ADVERSE_PCT:
            deterioration = True; reasons.append(f"movimiento adverso {_fmt_pct(current_return_pct)} vs entry")
        if bullish:
            deterioration = True; reasons.append("scanner/flow gira alcista")
        if expiry is not None and expiry >= config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            deterioration = True; reasons.append(f"GEX expiry se deteriora {_fmt_pct(expiry)}")
        if total is not None and total >= config.DEFAULT_EXIT_GEX_REVERSAL_PCT / 2:
            deterioration = True; reasons.append(f"GEX strike se deteriora {_fmt_pct(total)}")
        if price_vs_vwap is not None and price_vs_vwap > 0:
            reasons.append("Actual por encima de VWAP")
        if current_return_pct >= config.POSITION_UPDATE_MIN_FAVORABLE_PCT and bearish:
            favorable = True; reasons.append("setup avanza con confirmación bajista")
        if expiry is not None and expiry <= -config.DEFAULT_EXPIRY_CHANGE_PCT and current_return_pct >= 0:
            favorable = True; reasons.append(f"GEX expiry acompaña {_fmt_pct(expiry)}")

    relational_deterioration = f(features.get("relational_deterioration_score"), 0.0) or 0.0
    graph = f(features.get("graph_context_score"), 0.0) or 0.0
    directional_graph = graph if direction == "long" else -graph
    if relational_deterioration >= config.DEFAULT_RELATIONAL_EXIT_SCORE or directional_graph <= -25:
        deterioration = True
        reasons.append(
            f"contexto mercado/sector se deteriora (grafo {directional_graph:+.1f}; alerta {relational_deterioration:.1f})"
        )

    if deterioration:
        local_exit_gate = broad_signal_local_exit_gate(direction, features)
        if local_exit_gate.get("broad_active") and not local_exit_gate.get("exit_allowed"):
            local = local_exit_gate.get("local_gex_state") or {}
            reasons.append(
                f"CLOSE ALL activo: vigilancia anticipatoria; GEX ticker {local.get('state') or 'NO_GEX'} aún no habilita cierre"
            )
        return {
            "kind": "deterioration",
            "detail": "; ".join(dict.fromkeys(reasons)),
            "local_gex_state": (local_exit_gate.get("local_gex_state") or {}).get("state"),
            "close_blocked_by_local_gex": bool(local_exit_gate.get("broad_active") and not local_exit_gate.get("exit_allowed")),
        }
    if favorable:
        return {"kind": "favorable", "detail": "; ".join(dict.fromkeys(reasons))}
    return None


def _trade_model_trace(trade: dict, entry_features: dict, current_features: dict | None = None) -> str:
    entry_lineage = signal_model_lineage(entry_features, model_id=trade.get("model_version_id"), model_key=trade.get("model_key"))
    current_features = current_features or {}
    has_current_lineage = any(
        current_features.get(key) not in (None, "", [], {})
        for key in ("signal_origin", "core_model_id", "core_model_key", "core_rule", "target_source", "contributor_engines", "model_lineage", "gex_daily_model_status", "gex_intraday_model_status", "graph_source")
    )
    current_lineage = signal_model_lineage(current_features, model_id=current_features.get("core_model_id"), model_key=current_features.get("core_model_key")) if has_current_lineage else ""
    if current_lineage and current_lineage != entry_lineage:
        return f"entrada `{entry_lineage}` · activo `{current_lineage}`"
    return f"entrada `{entry_lineage}`"


def _trade_stats_lines(trade: dict, current_return: float) -> list[str | None]:
    best = f(trade.get("best_return_pct"), current_return) or current_return
    giveback = max(0.0, best - current_return)
    hit = bool(int(trade.get("setup_hit") or 0))
    return [
        _line("Estado lifecycle", trade.get("lifecycle_state") or ("OPEN_RUNNER" if hit else "OPEN")),
        _line("Setup ganado", "SÍ" if hit else "NO"),
        _pct_line("Resultado actual", current_return, 1),
        _pct_line("MFE", best, 1),
        _num_line("Giveback desde MFE", giveback, 1, suffix=" pp"),
        _line("Hit registrado", trade.get("setup_hit_at_madrid") if hit else None),
        _pct_line("Retorno al hit", trade.get("setup_hit_return_pct"), 1),
        _num_line("Sesiones hasta hit", trade.get("setup_hit_sessions"), 0),
        _line("Seguimiento", f"sesión {int(trade.get('sessions_seen') or 0)}/{int(trade.get('max_hold_sessions') or config.MAX_PATTERN_HORIZON_SESSIONS)}"),
    ]


def format_position_update_card(trade: dict, features: dict, kind: str, detail: str) -> str:
    direction = str(trade.get("direction") or "long").lower()
    side = "CALL" if direction == "long" else "PUT"
    current = f(features.get("price"), f(trade.get("last_price"), 0.0)) or 0.0
    entry = f(trade.get("entry_price"), 0.0) or 0.0
    directional = _directional_move(current, entry, direction)
    try:
        entry_features = json.loads(trade.get("entry_features_json") or "{}")
    except Exception:
        entry_features = {}
    entry_date = _date_label(trade.get("entry_date") or trade.get("entry_at_madrid"))
    source = signal_source_label(entry_features, rule_name=trade.get("rule_name"))
    streak = int(trade.get("deterioration_streak") or 0)
    confirmation_runs = max(1, int(config.SOFT_EXIT_CONFIRMATION_RUNS))
    titles = {
        "setup_hit": "SETUP GANADO · RUNNER ACTIVO",
        "target_touch": "OBJETIVO GEX ALCANZADO · RUNNER ACTIVO",
        "runner": "UPDATE RUNNER",
        "favorable": "UPDATE POSITION",
        "deterioration": "UPDATE DETERIORO",
    }
    status_title = titles.get(kind, "UPDATE POSITION")
    if kind == "setup_hit":
        diagnosis = "El setup alcanza el umbral estadístico ±5% y cuenta como ganado. La posición permanece abierta; no existe cierre automático."
    elif kind == "target_touch":
        diagnosis = "El precio alcanza la zona objetivo original. Se mantiene el runner mientras CORE/GEX no confirmen agotamiento o reversión."
    elif kind == "deterioration":
        diagnosis = f"Deterioro de la tesis `{streak}/{confirmation_runs}` runs. {_clean_reason(detail, 520)}"
    elif kind == "runner":
        diagnosis = f"El runner continúa abierto después del hit. {_clean_reason(detail, 520)}"
    else:
        diagnosis = f"La posición avanza a favor de la tesis. {_clean_reason(detail, 520)}"
    header = "\n".join([
        f"**${str(trade.get('ticker') or 'N/A').upper()}** · **{side}** · `[{source}]` · `{status_title}`",
        f"📍 **Actual:** `{_fmt_num(current)}` · **Entrada:** `{_fmt_num(entry)}` · **Movimiento favorable:** `{directional:+.1f}%`",
        f"⏳ **Entrada:** `{entry_date}` · **Run actual:** `{features.get('source_scanner_run_id') or features.get('scanner_run_id') or 'n/a'}`",
        f"🧬 **Modelo:** {_trade_model_trace(trade, entry_features, features)}",
        "",
        "### 📝 DIAGNÓSTICO",
        diagnosis,
    ])
    stats = _section("📊 RESULTADO / RUNNER", _trade_stats_lines(trade, directional))
    comparison = _section("🔄 ENTRADA → ACTUAL", feature_comparison_lines(entry_features, features, direction))
    action_map = {
        "setup_hit": "MANTENER RUNNER · el hit valida el setup, no la salida.",
        "target_touch": "MANTENER o extender objetivo si el wall migra; cerrar solo con agotamiento/reversión confirmada.",
        "runner": "MANTENER RUNNER mientras la ventaja estructural continúe.",
        "favorable": "MANTENER mientras CORE/GEX sigan alineados.",
        "deterioration": (
            f"OBSERVAR · durante CLOSE ALL, el cierre queda bloqueado hasta reversión GEX local; fuera de esa señal, exige {confirmation_runs}/{confirmation_runs} runs o invalidación hard."
        ),
    }
    action = _section("🚦 ACCIÓN", [action_map.get(kind, "Mantener seguimiento estructural.")])
    return "\n\n".join([header, stats, comparison, *native_feature_sections(features, direction), action])


def format_position_update_message(trade: dict, features: dict, kind: str, detail: str, *, as_card: bool = False) -> str:
    card = format_position_update_card(trade, features, kind, detail)
    if as_card:
        return card
    titles = {
        "setup_hit": "✅ SETUP GANADO · RUNNER ACTIVO",
        "target_touch": "🎯 OBJETIVO GEX ALCANZADO · RUNNER ACTIVO",
        "runner": "🔁 UPDATE RUNNER",
        "deterioration": "⚠️ UPDATE DETERIORO",
        "favorable": "🔁 UPDATE POSITION",
    }
    return build_group(titles.get(kind, "🔁 UPDATE POSITION"), [card])


def _directional_move(current: float, entry: float, direction: str) -> float:
    if not entry:
        return 0.0
    raw = (current / entry - 1.0) * 100.0
    return raw if direction == "long" else -raw


def _exit_title_reason(reason: str, pnl: float) -> tuple[str, str]:
    upper = str(reason or "").upper()
    if upper.startswith("GEX_EXHAUSTION"):
        return "CLOSE · AGOTAMIENTO GEX", "GEX confirma agotamiento de la extensión y deja de sostener el runner."
    if upper.startswith("CORE_REVERSAL"):
        return "CLOSE · CAMBIO CORE", "CORE confirma dirección contraria a la tesis de entrada."
    if upper.startswith("MARKET_REGIME"):
        return "CLOSE · LEGACY MARKET", "Código heredado: v6.1.25 nunca genera cierres individuales desde la señal general."
    if upper.startswith("LEGACY_TARGET_TOUCH_CLOSE"):
        return "CIERRE LEGACY · TARGET GEX", "Cierre histórico reclasificado: tocar el target GEX es milestone, no una salida estructural en v6.1.25."
    if upper.startswith("SETUP_HORIZON_EXPIRED") or upper.startswith("MAX"):
        return "CLOSE · HORIZONTE AGOTADO", "El setup no alcanzó el movimiento esperado dentro del horizonte de descubrimiento."
    if upper.startswith("PATTERN_REVERSAL") or upper.startswith("TRAILING_PATTERN"):
        return "CLOSE · ESTRUCTURA INVALIDADA", "CORE/GEX/scanner-flow confirman que la estructura original ha dejado de ser válida."
    return ("CLOSE · TESIS FINALIZADA", "La posición se cierra por la señal estructural registrada por TMAD.")


def format_exit_card(trade: dict) -> str:
    pnl = float(trade.get("exit_return_pct") or 0.0)
    direction = str(trade.get("direction") or "long").lower()
    side = "CALL" if direction == "long" else "PUT"
    try:
        entry_features = json.loads(trade.get("entry_features_json") or "{}")
    except Exception:
        entry_features = {}
    try:
        final_features = json.loads(trade.get("last_features_json") or "{}")
    except Exception:
        final_features = {}
    reason = str(trade.get("exit_reason") or "")
    title, explanation = _exit_title_reason(reason, pnl)
    entry_date = _date_label(trade.get("entry_date") or trade.get("entry_at_madrid"))
    exit_date = _date_label(trade.get("exit_date") or trade.get("exit_at_madrid"), include_time=True)
    source = signal_source_label(entry_features, rule_name=trade.get("rule_name"))
    best = float(trade.get("best_return_pct") or 0.0)
    giveback = float(trade.get("exit_giveback_pct") or max(0.0, best - pnl))
    capture = trade.get("exit_capture_ratio_pct")
    hit = bool(int(trade.get("setup_hit") or 0))
    header = "\n".join([
        f"**${str(trade.get('ticker') or 'N/A').upper()}** · **{side}** · `[{source}]` · `{title}`",
        f"📍 **Salida:** `{_fmt_num(trade.get('exit_price'))}` · **Entrada:** `{_fmt_num(trade.get('entry_price'))}` · **Resultado final:** `{pnl:+.1f}%`",
        f"⏳ **Duración:** `{int(trade.get('sessions_seen') or 0)} sesiones` · **Cierre:** `{exit_date}`",
        f"🧬 **Modelo:** {_trade_model_trace(trade, entry_features, final_features)}",
        "",
        "### 📝 MOTIVO",
        explanation,
        f"**Código:** `{reason or 'n/a'}`",
    ])
    result = _section("📊 RESULTADO DEL SETUP Y GESTIÓN", [
        _line("Setup hit ±5%", "SÍ" if hit else "NO"),
        _line("Setup contabilizado como ganado", "SÍ" if hit else "NO"),
        _pct_line("Retorno al hit", trade.get("setup_hit_return_pct"), 1),
        _num_line("Sesiones hasta hit", trade.get("setup_hit_sessions"), 0),
        _pct_line("MFE", best, 1),
        _pct_line("Retorno de salida", pnl, 1),
        _num_line("Giveback", giveback, 1, suffix=" pp"),
        _pct_line("Capture ratio", capture, 1, signed=False),
        _line("Lifecycle final", trade.get("lifecycle_state") or "CLOSED"),
    ])
    comparison = _section("🔄 ENTRADA → CIERRE", feature_comparison_lines(entry_features, final_features, direction) if final_features else [])
    return "\n\n".join([header, result, comparison, *native_feature_sections(final_features, direction)])


def format_exit_message(trade: dict, *, as_card: bool = False) -> str:
    card = format_exit_card(trade)
    if as_card:
        return card
    title, _explanation = _exit_title_reason(str(trade.get("exit_reason") or ""), float(trade.get("exit_return_pct") or 0.0))
    return build_group(title, [card])
