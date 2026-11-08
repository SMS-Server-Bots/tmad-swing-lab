from __future__ import annotations

import json
import sqlite3
import time

"""Cross-sectional scanner interpretation inspired by the TMAD workflow.

The scanner is a ranking engine, not a rigid checklist. This module turns the
current Scanner universe into point-in-time percentiles and flexible directional
scores. The historical CORE model remains the scientific source of calibrated
probability; the cross-sectional rank is a discovery fallback and an explanatory
layer when no strict discovered pattern fires.
"""

import math
from typing import Any

import config
from rules import evaluate_features


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _percentiles(values: dict[str, float | None], *, invert: bool = False) -> dict[str, float]:
    clean = sorted((float(v), key) for key, v in values.items() if v is not None and math.isfinite(float(v)))
    out = {key: 50.0 for key in values}
    n = len(clean)
    if n <= 1:
        return out
    # Average tied ranks, deterministic by ticker only after the numeric rank.
    index = 0
    while index < n:
        end = index + 1
        value = clean[index][0]
        while end < n and clean[end][0] == value:
            end += 1
        avg_rank = (index + end - 1) / 2.0
        pct = avg_rank / (n - 1) * 100.0
        if invert:
            pct = 100.0 - pct
        for _, key in clean[index:end]:
            out[key] = round(pct, 4)
        index = end
    return out


def enrich_cross_section(features_by_ticker: dict[str, dict]) -> dict[str, dict]:
    """Add point-in-time ranks without using future information."""
    if not features_by_ticker:
        return features_by_ticker
    metrics = {
        "momentum": {t: _f(f.get("momentum_score")) for t, f in features_by_ticker.items()},
        "trend": {t: _f(f.get("scanner_trend_score")) for t, f in features_by_ticker.items()},
        "rvol": {t: _f(f.get("rvol")) for t, f in features_by_ticker.items()},
        "change": {t: _f(f.get("change_percent")) for t, f in features_by_ticker.items()},
        "flow": {
            t: (_f(f.get("otm_call_volume_z"), 0.0) or 0.0) - (_f(f.get("otm_put_volume_z"), 0.0) or 0.0)
            for t, f in features_by_ticker.items()
        },
        "rs_market": {t: _f(f.get("relative_strength_vs_market")) for t, f in features_by_ticker.items()},
        "rs_sector": {t: _f(f.get("relative_strength_vs_sector")) for t, f in features_by_ticker.items()},
        "graph": {t: _f(f.get("graph_context_score")) for t, f in features_by_ticker.items()},
    }
    ranks = {name: _percentiles(values) for name, values in metrics.items()}
    for ticker, features in features_by_ticker.items():
        features["momentum_percentile"] = ranks["momentum"].get(ticker, 50.0)
        features["trend_percentile"] = ranks["trend"].get(ticker, 50.0)
        features["rvol_percentile"] = ranks["rvol"].get(ticker, 50.0)
        features["change_percentile"] = ranks["change"].get(ticker, 50.0)
        features["flow_percentile"] = ranks["flow"].get(ticker, 50.0)
        features["relative_strength_market_percentile"] = ranks["rs_market"].get(ticker, 50.0)
        features["relative_strength_sector_percentile"] = ranks["rs_sector"].get(ticker, 50.0)
        features["graph_percentile"] = ranks["graph"].get(ticker, 50.0)
    return features_by_ticker


_RANK_CALIBRATION_CACHE: dict = {"loaded_at": 0.0, "bins": {}}


def _rank_calibration_bins() -> dict[str, dict]:
    """Read persisted cross-sectional calibration without touching archived ZIPs per ticker.

    layer_calibration refreshes this aggregate whenever outcomes mature. The cache keeps
    the live scanner path cheap while allowing probabilities to improve automatically.
    """
    now = time.monotonic()
    if now - float(_RANK_CALIBRATION_CACHE.get("loaded_at") or 0.0) < 300:
        return dict(_RANK_CALIBRATION_CACHE.get("bins") or {})
    bins: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(config.ALERTS_DB, timeout=5)
        row = conn.execute("SELECT metrics_json FROM layer_model_calibration WHERE layer='CORE'").fetchone()
        conn.close()
        if row and row[0]:
            payload = json.loads(row[0])
            bins = dict(payload.get("cross_section_quality") or {})
    except Exception:
        bins = {}
    _RANK_CALIBRATION_CACHE.update({"loaded_at": now, "bins": bins})
    return bins


def _calibrated_rank_probability(score: float) -> tuple[float | None, int]:
    low = int(max(0.0, min(99.999, float(score))) // 10) * 10
    bucket = _rank_calibration_bins().get(f"{low:02d}-{low+9:02d}") or {}
    sample = int(bucket.get("sample") or 0)
    hit = bucket.get("hit_rate")
    if sample < 20 or hit is None:
        return None, sample
    return round(float(hit), 2), sample


def _model_votes(features: dict, model: dict | None, mode: str) -> tuple[list[dict], list[dict]]:
    if not model:
        return [], []
    candidates = evaluate_features(features, model.get("params") or {}, mode=mode)
    longs = sorted((dict(c) for c in candidates if str(c.get("direction")) == "long"), key=lambda x: float(x.get("score") or 0), reverse=True)
    shorts = sorted((dict(c) for c in candidates if str(c.get("direction")) == "short"), key=lambda x: float(x.get("score") or 0), reverse=True)
    return longs, shorts


def _directional_rank(features: dict, direction: str) -> tuple[float, list[str]]:
    sign = 1.0 if direction == "long" else -1.0
    def pct(name: str) -> float:
        value = float(features.get(name) or 50.0)
        return value if direction == "long" else 100.0 - value

    momentum = pct("momentum_percentile")
    trend = pct("trend_percentile")
    flow = pct("flow_percentile")
    change = pct("change_percentile")
    rs = (pct("relative_strength_market_percentile") + pct("relative_strength_sector_percentile")) / 2.0
    graph = pct("graph_percentile")
    rvol = float(features.get("rvol_percentile") or 50.0)
    market = _clamp(50.0 + sign * float(features.get("market_context_score") or 0.0) * 0.5)
    sector = _clamp(50.0 + sign * float(features.get("sector_etf_score") or 0.0) * 0.5)
    score = (
        momentum * 0.24 + trend * 0.20 + flow * 0.13 + rvol * 0.09
        + change * 0.08 + rs * 0.10 + graph * 0.06 + market * 0.05 + sector * 0.05
    )
    reasons: list[str] = []
    for label, value in (
        ("momentum", momentum), ("trend", trend), ("flow", flow), ("RVOL", rvol),
        ("RS", rs), ("grafo", graph), ("mercado", market), ("sector", sector),
    ):
        if value >= 70:
            reasons.append(f"{label} p{value:.0f}")
        elif value <= 25:
            reasons.append(f"{label} débil p{value:.0f}")
    return round(score, 4), reasons


def _overextension(features: dict, direction: str) -> tuple[float, list[str]]:
    sign = 1.0 if direction == "long" else -1.0
    risk = 0.0
    bits: list[str] = []
    change = sign * float(features.get("change_percent") or 0.0)
    rvol = float(features.get("rvol") or 0.0)
    rsi = float(features.get("rsi") or 50.0)
    pvw = sign * float(features.get("price_vs_vwap_pct") or 0.0)
    directional_rsi = rsi if direction == "long" else 100.0 - rsi
    if change >= float(config.CORE_EXTENDED_DAY_CHANGE_PCT):
        risk += min(35.0, change * 2.0); bits.append(f"día {change:+.1f}%")
    if rvol >= float(config.CORE_FOMO_RVOL):
        risk += min(30.0, (rvol - 1.0) * 6.0); bits.append(f"RVOL {rvol:.1f}")
    if directional_rsi >= float(config.CORE_EXTENDED_RSI):
        risk += min(25.0, (directional_rsi - 55.0) * 1.2); bits.append(f"RSI {rsi:.0f}")
    if pvw >= float(config.CORE_EXTENDED_VWAP_PCT):
        risk += min(20.0, pvw * 2.0); bits.append(f"VWAP {pvw:+.1f}%")
    return round(min(100.0, risk), 4), bits


def build_core_opportunity(features: dict, model: dict | None, mode: str) -> dict | None:
    """Create a flexible CORE opportunity.

    A discovered rule is evidence, not a mandatory gate. The cross-sectional
    ranking can surface a ticker even when no conjunction exactly matches.
    """
    longs, shorts = _model_votes(features, model, mode)
    long_rank, long_bits = _directional_rank(features, "long")
    short_rank, short_bits = _directional_rank(features, "short")

    long_model = longs[0] if longs else None
    short_model = shorts[0] if shorts else None
    long_model_score = float((long_model or {}).get("score") or 0.0)
    short_model_score = float((short_model or {}).get("score") or 0.0)
    long_score = long_rank * 0.62 + long_model_score * 0.38 if long_model else long_rank
    short_score = short_rank * 0.62 + short_model_score * 0.38 if short_model else short_rank
    direction = "long" if long_score >= short_score else "short"
    score = long_score if direction == "long" else short_score
    opposite = short_score if direction == "long" else long_score
    model_vote = long_model if direction == "long" else short_model
    reasons = long_bits if direction == "long" else short_bits

    minimum = float(config.CORE_DISCOVERY_MIN_RANK_SCORE)
    if score < minimum and not model_vote:
        return None
    # Ambiguous cross-section: preserve for data, but do not present as a strong setup.
    directional_edge = score - opposite
    probability = None
    stats = {}
    if model_vote:
        params = ((model or {}).get("params") or {}).get("rules") or {}
        stats = (params.get(model_vote.get("rule_name")) or {}).get("discovery_stats") or {}
        if stats:
            probability = round(float(stats.get("precision") or 0.0) * 100.0, 2)
    probability_source = "MODEL_OOS" if stats else "RANK_HEURISTIC"
    probability_sample = int(stats.get("sample") or stats.get("n") or 0) if stats else 0
    if probability is None:
        probability, probability_sample = _calibrated_rank_probability(score)
        if probability is not None:
            probability_source = "RANK_CALIBRATED"
        else:
            # Explicitly heuristic until enough full-universe outcomes calibrate the rank bins.
            probability = round(_clamp(float(config.CORE_BASE_MOVE_RATE_PCT) + max(0.0, score - 50.0) * 0.55 + max(0.0, directional_edge) * 0.25, 5.0, 95.0), 2)

    extension, extension_bits = _overextension(features, direction)
    state = "DISCOVERED"
    if directional_edge < float(config.CORE_MIN_DIRECTIONAL_EDGE):
        state = "WATCH"
    elif score >= float(config.CORE_READY_RANK_SCORE):
        state = "EARLY"
    if extension >= float(config.CORE_EXTENDED_SCORE):
        state = "EXTENDED"
    reason_bits = reasons[:4]
    if model_vote:
        reason_bits.insert(0, str(model_vote.get("rule_name") or "pattern vote"))
    if extension_bits:
        reason_bits.append("extensión " + ",".join(extension_bits[:2]))
    return {
        "layer": "CORE",
        "signal_origin": "CORE_DIRECTIONAL",
        "direction": direction,
        "score": round(_clamp(score), 2),
        "core_rank_score": round(_clamp(score), 2),
        "core_opposite_score": round(_clamp(opposite), 2),
        "directional_edge": round(directional_edge, 2),
        "probability_pct": probability,
        "probability_source": probability_source,
        "probability_sample": probability_sample,
        "rule_name": (model_vote or {}).get("rule_name") or f"CORE_RANK_{direction.upper()}",
        "model_vote_score": round(float((model_vote or {}).get("score") or 0.0), 2),
        "state": state,
        "actionable": False,
        "target_source": None,
        "target_pct": None,
        "label_threshold_pct": float(config.PATTERN_TARGET_PCT),
        "timing_min_sessions": 1,
        "timing_max_sessions": int(config.MAX_PATTERN_HORIZON_SESSIONS),
        "overextension_score": extension,
        "rationale": " · ".join(reason_bits or ["ranking Scanner"]),
        "reason": "CORE rankea probabilidad de movimiento; no fija target operativo",
        "daily_pattern_projection_pct": float((model_vote or {}).get("daily_pattern_projection_pct") or score),
    }
