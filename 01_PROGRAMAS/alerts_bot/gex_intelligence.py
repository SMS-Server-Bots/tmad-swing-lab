from __future__ import annotations

import math
from typing import Any

import config


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _runway(price: float, level: float, direction: str) -> float:
    return (level / price - 1.0) * 100.0 if direction == "long" else (price / level - 1.0) * 100.0


def _directional_levels(features: dict, direction: str) -> list[tuple[str, float, float, float]]:
    price = _f(features.get("price"))
    if not price or price <= 0:
        return []
    if direction == "long":
        names = (
            ("near_call_wall", features.get("near_call_wall_strike"), 1.35, features.get("call_wall_strength")),
            ("swing_call_wall", features.get("swing_call_wall_strike"), 1.20, features.get("call_wall_strength")),
            ("all_call_wall", features.get("all_call_wall_strike"), 1.00, features.get("call_wall_strength")),
            ("scanner_call_wall", features.get("call_wall"), 0.85, features.get("call_wall_strength")),
        )
    else:
        names = (
            ("near_put_wall", features.get("near_put_wall_strike"), 1.35, features.get("put_wall_strength")),
            ("swing_put_wall", features.get("swing_put_wall_strike"), 1.20, features.get("put_wall_strength")),
            ("all_put_wall", features.get("all_put_wall_strike"), 1.00, features.get("put_wall_strength")),
            ("scanner_put_wall", features.get("put_wall"), 0.85, features.get("put_wall_strength")),
        )
    out: list[tuple[str, float, float, float]] = []
    seen: set[float] = set()
    for name, raw, weight, strength_raw in names:
        level = _f(raw)
        if level is None or level <= 0:
            continue
        runway = _runway(price, level, direction)
        if runway <= 0 or runway > float(config.GEX_TARGET_MAX_RUNWAY_PCT):
            continue
        rounded = round(level, 6)
        if rounded in seen:
            continue
        seen.add(rounded)
        strength = abs(_f(strength_raw, 0.0) or 0.0)
        out.append((name, level, weight, strength))
    return out


def _wall_asymmetry(features: dict, direction: str) -> dict:
    price = _f(features.get("price"))
    if not price or price <= 0:
        return {"reward_pct": None, "risk_pct": None, "ratio": None, "support": None, "target": None}
    call = _f(features.get("near_call_wall_strike") or features.get("swing_call_wall_strike") or features.get("call_wall") or features.get("all_call_wall_strike"))
    put = _f(features.get("near_put_wall_strike") or features.get("swing_put_wall_strike") or features.get("put_wall") or features.get("all_put_wall_strike"))
    if direction == "long":
        reward = _runway(price, call, "long") if call and call > price else None
        risk = (price / put - 1.0) * 100.0 if put and 0 < put < price else None
        target, support = call, put
    else:
        reward = _runway(price, put, "short") if put and 0 < put < price else None
        risk = (call / price - 1.0) * 100.0 if call and call > price else None
        target, support = put, call
    ratio = reward / risk if reward is not None and risk not in (None, 0) and risk > 0 else None
    return {
        "reward_pct": round(reward, 4) if reward is not None else None,
        "risk_pct": round(risk, 4) if risk is not None else None,
        "ratio": round(ratio, 4) if ratio is not None else None,
        "support": support,
        "target": target,
    }


def _expiry_map(features: dict, direction: str) -> dict:
    rows = list(features.get("top_expirations") or [])
    sign = 1.0 if direction == "long" else -1.0
    buckets = {"short": [], "mid": [], "long": []}
    snapshot_date = str(features.get("snapshot_date") or features.get("trade_date") or "")
    for row in rows:
        dte = int(_f(row.get("dte"), 999) or 999)
        expiration_date = str(row.get("expiration_date") or "")
        if dte < 0 or (snapshot_date and expiration_date and expiration_date < snapshot_date):
            continue
        net = _f(row.get("net_gex"), 0.0) or 0.0
        change = _f(row.get("change_pct"), 0.0) or 0.0
        share = abs(_f(row.get("share_abs_gex"), 0.0) or 0.0)
        directional = sign * (net if abs(net) > 0 else change)
        item = {**row, "directional": directional, "share": share}
        if dte <= 7:
            buckets["short"].append(item)
        elif int(config.GEX_MIDTERM_MIN_DTE) <= dte <= int(config.GEX_MIDTERM_MAX_DTE):
            buckets["mid"].append(item)
        else:
            buckets["long"].append(item)

    def aggregate(items: list[dict]) -> tuple[float, float, int | None, str | None]:
        if not items:
            return 0.0, 0.0, None, None
        weighted = sum(float(i["directional"]) * max(0.01, float(i["share"])) for i in items)
        share = sum(float(i["share"]) for i in items)
        best = max(items, key=lambda i: abs(float(i["directional"])) * max(0.01, float(i["share"])))
        return weighted, share, int(_f(best.get("dte"), 0) or 0), str(best.get("expiration_date") or "") or None

    short_score, short_share, short_dte, short_expiry = aggregate(buckets["short"])
    mid_score, mid_share, mid_dte, mid_expiry = aggregate(buckets["mid"])
    long_score, long_share, long_dte, long_expiry = aggregate(buckets["long"])
    positive_blocks = sum(score > 0 for score in (short_score, mid_score, long_score))
    negative_blocks = sum(score < 0 for score in (short_score, mid_score, long_score))
    if positive_blocks >= 2 and negative_blocks == 0:
        regime = "EXPIRY_ALIGNMENT"
    elif positive_blocks and negative_blocks:
        regime = "EXPIRY_CONFLICT"
    elif mid_score > 0 and mid_share >= float(config.GEX_EXPIRY_ALIGNMENT_MIN_SHARE):
        regime = "MID_DIRECTIONAL_CONVICTION"
    elif long_score > 0 and long_share >= float(config.GEX_EXPIRY_ALIGNMENT_MIN_SHARE):
        regime = "LONG_INSTITUTIONAL_POSITIONING"
    elif short_score > 0:
        near = _f(features.get("near_gex"), 0.0) or 0.0
        regime = "SHORT_STABILIZING_GAMMA" if near > 0 else "SHORT_ACCELERATION_RISK"
    else:
        regime = "SHORT_HEDGING_NOISE" if short_share > max(mid_share, long_share) else "EXPIRY_UNCLEAR"
    preferred = max(
        [(mid_score * max(mid_share, 0.01), mid_dte, mid_expiry, "mid"),
         (short_score * max(short_share, 0.01), short_dte, short_expiry, "short"),
         (long_score * max(long_share, 0.01), long_dte, long_expiry, "long")],
        key=lambda x: x[0],
    )
    dte = max(1, int(preferred[1] or config.MAX_PATTERN_HORIZON_SESSIONS))
    timing_max = min(int(config.MAX_PATTERN_HORIZON_SESSIONS), dte)
    timing_min = 1 if dte <= 2 else min(2, timing_max)
    return {
        "regime": regime,
        "timing_min": timing_min,
        "timing_max": timing_max,
        "expiry": preferred[2],
        "dominant_bucket": preferred[3],
        "short_score": short_score,
        "mid_score": mid_score,
        "long_score": long_score,
        "short_share": short_share,
        "mid_share": mid_share,
        "long_share": long_share,
    }


def _bias_scores(features: dict) -> tuple[float, float, list[str], list[str]]:
    long_score = 0.0; short_score = 0.0
    long_bits: list[str] = []; short_bits: list[str] = []
    bias = str(features.get("profile_bias") or "").lower()
    call_acc = _f(features.get("call_accumulation_score"), 0.0) or 0.0
    put_acc = _f(features.get("put_accumulation_score"), 0.0) or 0.0
    total_change = _f(features.get("total_gex_change_pct"), 0.0) or 0.0
    near_change = _f(features.get("near_gex_change_pct"), 0.0) or 0.0
    swing_change = _f(features.get("swing_gex_change_pct"), 0.0) or 0.0
    call_z = _f(features.get("otm_call_volume_z"), 0.0) or 0.0
    put_z = _f(features.get("otm_put_volume_z"), 0.0) or 0.0
    if any(word in bias for word in ("call", "bull", "positive", "up")):
        long_score += 22.0; long_bits.append(f"bias {bias}")
    if any(word in bias for word in ("put", "bear", "negative", "down")):
        short_score += 22.0; short_bits.append(f"bias {bias}")
    if call_acc > 0:
        points = min(24.0, abs(call_acc) * 0.24); long_score += points; long_bits.append(f"call acc {call_acc:.0f}")
    if put_acc > 0:
        points = min(24.0, abs(put_acc) * 0.24); short_score += points; short_bits.append(f"put acc {put_acc:.0f}")
    if call_z - put_z >= float(config.GEX_DIRECTIONAL_FLOW_Z):
        long_score += min(16.0, (call_z - put_z) * 5.0); long_bits.append(f"call flow z {call_z-put_z:+.1f}")
    if put_z - call_z >= float(config.GEX_DIRECTIONAL_FLOW_Z):
        short_score += min(16.0, (put_z - call_z) * 5.0); short_bits.append(f"put flow z {put_z-call_z:+.1f}")
    for value, label, weight in ((total_change, "Δstrike", 0.14), (near_change, "Δ0-7d", 0.18), (swing_change, "Δ8-56d", 0.14)):
        if value > 0:
            points = min(16.0, value * weight); long_score += points; long_bits.append(f"{label} {value:+.0f}%")
        elif value < 0:
            points = min(16.0, abs(value) * weight); short_score += points; short_bits.append(f"{label} {value:+.0f}%")
    return long_score, short_score, long_bits, short_bits


def _classify_intent(features: dict, direction: str, score: float, asymmetry: dict, expiry: dict) -> tuple[str, list[str]]:
    sign = 1.0 if direction == "long" else -1.0
    change = sign * (_f(features.get("change_percent"), 0.0) or 0.0)
    rvol = _f(features.get("rvol"), 0.0) or 0.0
    flow = sign * (_f(features.get("flow_direction"), 0.0) or 0.0)
    pvw = sign * (_f(features.get("price_vs_vwap_pct"), 0.0) or 0.0)
    near = sign * (_f(features.get("near_gex_change_pct"), 0.0) or 0.0)
    swing = sign * (_f(features.get("swing_gex_change_pct"), 0.0) or 0.0)
    ratio = _f(asymmetry.get("ratio"), 0.0) or 0.0
    bits: list[str] = []
    if change >= float(config.GEX_FOMO_DAY_CHANGE_PCT) and rvol >= float(config.GEX_FOMO_RVOL):
        return "FOMO", [f"día {change:+.1f}%", f"RVOL {rvol:.1f}"]
    if expiry.get("regime") == "SHORT_STABILIZING_GAMMA" and abs(swing) < 5:
        return "STABILIZING", ["gamma corto positivo", "swing sin convicción"]
    if flow > 0 and swing > 0 and ratio >= float(config.GEX_MIN_WALL_ASYMMETRY):
        bits = ["flow confirma", f"swing {swing:+.0f}%", f"asimetría {ratio:.1f}x"]
        return "DIRECTIONAL", bits
    if flow <= 0 and near > 0 and str(expiry.get("regime")) in {"SHORT_HEDGING_NOISE", "EXPIRY_CONFLICT"}:
        return "HEDGING", ["GEX sube sin flow", str(expiry.get("regime"))]
    if score >= 80 and ratio >= float(config.GEX_STRONG_WALL_ASYMMETRY) and pvw <= 2.0:
        return "SQUEEZE", [f"asimetría {ratio:.1f}x", "precio aún no extendido"]
    if flow < 0 and change > 0:
        return "DISTRIBUTION", ["precio avanza", "flow contrario"]
    return "UNCLEAR", bits


def project_gex(features: dict, core_direction: str | None = None) -> dict | None:
    """Independent GEX discovery, target map and time map."""
    if not getattr(config, "GEX_PROJECTION_ENABLED", True) or not int(features.get("has_gex") or 0):
        return None
    price = _f(features.get("price"))
    if not price or price <= 0:
        return None
    long_levels = _directional_levels(features, "long")
    short_levels = _directional_levels(features, "short")
    long_score, short_score, long_bits, short_bits = _bias_scores(features)
    if long_levels:
        long_score += 28.0 + max(weight for _, _, weight, _ in long_levels) * 8.0
    if short_levels:
        short_score += 28.0 + max(weight for _, _, weight, _ in short_levels) * 8.0
    # Alignment is recorded but intentionally small: GEX remains an independent detector.
    if core_direction == "long":
        long_score += min(5.0, float(config.GEX_CORE_ALIGNMENT_BONUS) * 0.5)
    elif core_direction == "short":
        short_score += min(5.0, float(config.GEX_CORE_ALIGNMENT_BONUS) * 0.5)
    if max(long_score, short_score) < float(config.GEX_MIN_PROJECTION_SCORE):
        return None
    direction = "long" if long_score >= short_score else "short"
    levels = long_levels if direction == "long" else short_levels
    bits = long_bits if direction == "long" else short_bits
    if not levels:
        return None
    asymmetry = _wall_asymmetry(features, direction)
    ranked = []
    for name, level, weight, strength in levels:
        runway = _runway(price, level, direction)
        quality = weight * 10.0 + min(10.0, math.log1p(strength) if strength > 0 else 0.0)
        quality -= abs(runway - float(config.PATTERN_TARGET_PCT)) * 0.25
        if runway >= float(config.GEX_TARGET_MIN_RUNWAY_PCT):
            quality += 8.0
        ranked.append((quality, name, level, runway, strength))
    ranked.sort(reverse=True)
    anchor = ranked[0]
    nearby = [item for item in ranked if abs(item[2] / anchor[2] - 1.0) * 100.0 <= 2.5]
    values = [item[2] for item in nearby] or [anchor[2]]
    target_low = min(values); target_high = max(values); target_mid = sum(values) / len(values)
    target_pct = _runway(price, target_mid, direction)
    selected_risk = _f(asymmetry.get("risk_pct"))
    selected_ratio = (target_pct / selected_risk) if selected_risk not in (None, 0) and selected_risk > 0 else asymmetry.get("ratio")
    expiry = _expiry_map(features, direction)
    confidence = min(100.0, max(long_score, short_score) + min(10.0, abs(float(expiry.get("mid_score") or 0.0)) * 0.02))
    intent, intent_bits = _classify_intent(features, direction, confidence, asymmetry, expiry)
    minimum_operational_runway = max(
        float(config.GEX_TARGET_MIN_RUNWAY_PCT),
        float(config.PATTERN_TARGET_PCT),
    )
    wall_ratio = selected_ratio
    wall_structure_valid = wall_ratio is None or float(wall_ratio) > 0.0
    operational_target = bool(target_pct >= minimum_operational_runway and wall_structure_valid)
    capped = not operational_target
    aligned = core_direction in {None, direction}
    source = "GEX_CALIBRATED" if int(features.get("gex_daily_model_validated") or 0) else "GEX_HEURISTIC_SHADOW"
    rationale_bits = bits[:3] + [f"{anchor[1]} {anchor[2]:.2f}", f"runway {target_pct:+.1f}%"]
    if asymmetry.get("ratio") is not None:
        rationale_bits.append(f"wall {float(asymmetry['ratio']):.1f}x")
    rationale_bits += intent_bits[:2]
    if expiry.get("expiry"):
        rationale_bits.append(f"exp {expiry['expiry']}")
    return {
        "layer": "GEX_DAILY",
        "signal_origin": "GEX_STANDALONE" if core_direction is None else ("CORE_GEX_ALIGNED" if aligned else "CORE_GEX_DIVERGENCE"),
        "direction": direction,
        "score": round(confidence, 2),
        "target_source": source,
        "target_zone_low": round(target_low, 6),
        "target_zone_high": round(target_high, 6),
        "target_price": round(target_mid, 6),
        "target_pct": round(target_pct, 4),
        "timing_min_sessions": int(expiry.get("timing_min") or 1),
        "timing_max_sessions": int(expiry.get("timing_max") or config.MAX_PATTERN_HORIZON_SESSIONS),
        "expiry_date": expiry.get("expiry"),
        "expiry_bucket": expiry.get("dominant_bucket"),
        "expiry_regime": expiry.get("regime"),
        "gex_intent": intent,
        "wall_reward_pct": round(target_pct, 4),
        "wall_risk_pct": asymmetry.get("risk_pct"),
        "wall_asymmetry": round(float(selected_ratio), 4) if selected_ratio is not None else None,
        "wall_support": asymmetry.get("support"),
        "wall_target": asymmetry.get("target"),
        "opposite_level_name": "put_wall" if direction == "long" else "call_wall",
        "opposite_level_price": asymmetry.get("support"),
        "total_gex_change_pct": _f(features.get("total_gex_change_pct")),
        "near_gex_change_pct": _f(features.get("near_gex_change_pct")),
        "swing_gex_change_pct": _f(features.get("swing_gex_change_pct")),
        "call_accumulation_score": _f(features.get("call_accumulation_score")),
        "put_accumulation_score": _f(features.get("put_accumulation_score")),
        "expiry_short_score": expiry.get("short_score"),
        "expiry_mid_score": expiry.get("mid_score"),
        "expiry_long_score": expiry.get("long_score"),
        "expiry_short_share": expiry.get("short_share"),
        "expiry_mid_share": expiry.get("mid_share"),
        "expiry_long_share": expiry.get("long_share"),
        "capped": bool(capped),
        "operational_target": bool(operational_target),
        "minimum_operational_runway_pct": round(minimum_operational_runway, 4),
        "wall_structure_valid": bool(wall_structure_valid),
        "aligned_with_core": bool(aligned),
        "rationale": " · ".join(rationale_bits),
        "level_name": anchor[1],
        "level_price": anchor[2],
        "model_status": "VALIDATED" if source == "GEX_CALIBRATED" else "ACCUMULATING",
        "expiry_map": expiry,
    }
