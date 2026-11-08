from __future__ import annotations

import json
import math
from collections import defaultdict
from statistics import mean, median
from typing import Any

import config
from asset_universe import CORE_MARKET_SYMBOLS, LEVERAGED_PAIRS, primary_sector_etf, relationship_priors

FEATURE_VERSION = int(config.RELATIONAL_FEATURE_SCHEMA_VERSION)


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _weighted(values: list[tuple[float | None, float]], default: float = 0.0) -> float:
    clean = [(float(v), float(w)) for v, w in values if v is not None and w > 0]
    if not clean:
        return default
    denom = sum(w for _, w in clean)
    return sum(v * w for v, w in clean) / denom if denom else default


def _direction_score(features: dict) -> float:
    """Non-GEX directional state of one node, normalized to [-100, 100].

    The graph and broad regime must remain independently testable from the GEX
    challenger. Strike/expiry/wall fields are therefore deliberately excluded.
    """
    trend = _f(features.get("scanner_trend_score"), 0.0) or 0.0
    flow = int(_f(features.get("flow_direction"), 0.0) or 0)
    pvw = _f(features.get("price_vs_vwap_pct"), 0.0) or 0.0
    change = _f(features.get("change_percent"), 0.0) or 0.0
    momentum_5d = _f(features.get("momentum_5d_pct"), 0.0) or 0.0
    relative = _f(features.get("price_vs_sma20_pct"), 0.0) or 0.0
    score = _clamp(trend * 1.25, -38, 38)
    score += 15.0 * flow
    score += _clamp(pvw * 9.0, -15, 15)
    score += _clamp(change * 3.0, -12, 12)
    score += _clamp(momentum_5d * 1.5, -12, 12)
    score += _clamp(relative * 1.2, -8, 8)
    return round(_clamp(score), 4)

def _regime(score: float, vix_stress: float, breadth: float) -> str:
    if vix_stress >= 55 and score <= -15:
        return "RISK_OFF_VOLATILITY"
    if score >= 25 and breadth >= 10:
        return "RISK_ON_TREND"
    if score <= -25 and breadth <= -10:
        return "RISK_OFF_TREND"
    if abs(score) <= 15 and abs(breadth) <= 12:
        return "RANGE_PINNING"
    return "MIXED_ROTATION"


def _vix_stress(features_by_ticker: dict[str, dict]) -> float:
    candidates = []
    for symbol, weight in (("VIX", 1.0), ("VVIX", 0.85), ("UVIX", 0.75), ("UVXY", 0.70), ("VXX", 0.60), ("VIXY", 0.55), ("VIXM", 0.45), ("VXZ", 0.40), ("SVIX", -0.60), ("SVXY", -0.55), ("ZIVB", -0.40)):
        feat = features_by_ticker.get(symbol)
        if not feat:
            continue
        change = _f(feat.get("change_percent"), 0.0) or 0.0
        pvw = _f(feat.get("price_vs_vwap_pct"), 0.0) or 0.0
        trend = _f(feat.get("scanner_trend_score"), 0.0) or 0.0
        raw = _clamp(change * 10.0 + pvw * 8.0 + trend, -100, 100)
        candidates.append((raw * (1 if weight > 0 else -1), abs(weight)))
    return round(_weighted(candidates), 4)


def _breadth(scores: list[float]) -> float:
    if not scores:
        return 0.0
    positive = sum(1 for x in scores if x >= 10)
    negative = sum(1 for x in scores if x <= -10)
    return round((positive - negative) / len(scores) * 100.0, 4)


def _leveraged_confirmation(
    ticker: str,
    base_etf: str | None,
    scores: dict[str, float],
    relationship_rows: list[dict] | None = None,
) -> float:
    """Signed confirmation from related leveraged/inverse products.

    Products are derived both from broad/sector catalogues and from Scanner
    structural edges (for example TSLA↔TSLL or NVDA↔NVDL). They are a single
    contextual block, not independent votes, preventing double-counting.
    """
    values: list[tuple[float | None, float]] = []
    seen: set[str] = set()
    if base_etf and base_etf in LEVERAGED_PAIRS:
        for symbol in LEVERAGED_PAIRS[base_etf]:
            if symbol in seen:
                continue
            value = scores.get(symbol)
            if value is None:
                continue
            # Negative correlation in the dynamic graph will supersede this
            # static fallback. Known inverse names are signed here.
            inverse = symbol in {
                "SPXS", "SPXU", "SH", "SQQQ", "QID", "PSQ", "TZA", "TWM", "RWM",
                "SDOW", "DXD", "DOG", "SOXS", "FAZ", "WDRW", "LABD", "ERY", "EMTY",
                "SARK", "DUST", "JDST", "TMV", "TBT", "SVIX", "SVXY",
            }
            values.append((-value if inverse else value, 0.50))
            seen.add(symbol)
    for row in relationship_rows or []:
        relation_type = str(row.get("relation_type") or "")
        if relation_type not in {"leveraged_confirmation", "single_stock_confirmation"}:
            continue
        symbol = str(row.get("related_ticker") or "").upper()
        if not symbol or symbol in seen or symbol not in scores:
            continue
        corr = _f(row.get("correlation"))
        polarity = -1.0 if corr is not None and corr < 0 else 1.0
        weight = _f(row.get("dynamic_weight"), _f(row.get("base_weight"), 0.5)) or 0.5
        values.append((scores[symbol] * polarity, max(0.10, weight)))
        seen.add(symbol)
    return round(_weighted(values), 4)


def build_relational_context(
    features_by_ticker: dict[str, dict],
    previous_by_ticker: dict[str, dict] | None = None,
    relationship_map: dict[str, list[dict]] | None = None,
) -> tuple[dict[str, dict], dict]:
    """Create one relational feature vector per ticker for the current run.

    It combines node state, broad market, sector ETF, sector breadth, leveraged
    confirmation, and dynamic price relationships. Missing nodes are neutral and
    never fabricated.
    """
    previous_by_ticker = previous_by_ticker or {}
    relationship_map = relationship_map or {}
    scores = {ticker: _direction_score(features) for ticker, features in features_by_ticker.items()}
    previous_scores = {ticker: _direction_score(features) for ticker, features in previous_by_ticker.items()}

    market_score = _weighted([
        (scores.get("SPY"), 0.35),
        (scores.get("QQQ"), 0.30),
        (scores.get("IWM"), 0.20),
        (scores.get("DIA"), 0.15),
    ])
    previous_market = _weighted([
        (previous_scores.get("SPY"), 0.35),
        (previous_scores.get("QQQ"), 0.30),
        (previous_scores.get("IWM"), 0.20),
        (previous_scores.get("DIA"), 0.15),
    ])
    universe_scores = [value for ticker, value in scores.items() if ticker not in CORE_MARKET_SYMBOLS]
    market_breadth = _breadth(universe_scores)
    vix_stress = _vix_stress(features_by_ticker)
    credit_risk_on = _weighted([(scores.get("HYG"), 0.65), (scores.get("LQD"), 0.35)])
    risk_on_market_score = round(_clamp(_weighted([(market_score, 0.85), (credit_risk_on, 0.15)]) - vix_stress * 0.35), 4)
    market_deterioration = round(max(0.0, previous_market - market_score) + max(0.0, vix_stress) * 0.25, 4)
    market_regime = _regime(risk_on_market_score, vix_stress, market_breadth)

    groups: dict[str, list[str]] = defaultdict(list)
    ticker_sector: dict[str, str | None] = {}
    for ticker, features in features_by_ticker.items():
        sector_etf = primary_sector_etf(ticker, features.get("sector"), features.get("industry"))
        ticker_sector[ticker] = sector_etf
        if sector_etf:
            groups[sector_etf].append(ticker)

    out: dict[str, dict] = {}
    for ticker, features in features_by_ticker.items():
        sector_etf = ticker_sector.get(ticker)
        sector_etf_score = scores.get(sector_etf) if sector_etf else None
        previous_sector_score = previous_scores.get(sector_etf) if sector_etf else None
        peer_scores = [scores[t] for t in groups.get(sector_etf or "", []) if t in scores and t not in {ticker, sector_etf}]
        sector_breadth_score = _breadth(peer_scores)
        leveraged_score = _leveraged_confirmation(ticker, sector_etf, scores, relationship_map.get(ticker, []))

        priors = relationship_priors(ticker, features.get("sector"), features.get("industry"))
        dynamic_rows = relationship_map.get(ticker, [])
        related: list[tuple[str, float, str, float]] = []
        seen: set[str] = set()
        for row in dynamic_rows:
            symbol = str(row.get("related_ticker") or "").upper()
            if not symbol or symbol == ticker or symbol in seen:
                continue
            raw_corr = _f(row.get("correlation"))
            lead_lag = _f(row.get("lead_lag_corr"))
            use_lead = lead_lag is not None and abs(lead_lag) > abs(raw_corr or 0.0)
            predictive_corr = lead_lag if use_lead else raw_corr
            learned_strength = max(abs(raw_corr or 0.0), abs(lead_lag or 0.0) * 0.90)
            weight = _f(row.get("dynamic_weight"), learned_strength) or learned_strength
            polarity = -1.0 if predictive_corr is not None and predictive_corr < 0 else 1.0
            source = "dynamic_lead" if use_lead else "dynamic"
            related.append((symbol, max(0.05, weight), source, polarity, raw_corr, lead_lag, _f(row.get("beta"))))
            seen.add(symbol)
        for rank, symbol in enumerate(priors):
            if symbol == ticker or symbol in seen:
                continue
            related.append((symbol, max(0.15, 0.65 - rank * 0.10), "prior", 1.0, None, None, None))
            seen.add(symbol)

        related_values: list[tuple[float | None, float]] = []
        related_payload: list[dict] = []
        lead_values: list[tuple[float | None, float]] = []
        for symbol, weight, source, polarity, corr, lead_lag, beta in related[:8]:
            value = scores.get(symbol)
            if value is None:
                continue
            adjusted_value = value * polarity
            related_values.append((adjusted_value, weight))
            if lead_lag is not None:
                lead_values.append((adjusted_value, abs(lead_lag)))
            related_payload.append({
                "ticker": symbol, "score": value, "adjusted_score": adjusted_value,
                "weight": round(weight, 4), "source": source, "polarity": polarity,
                "correlation": corr, "lead_lag_corr": lead_lag, "beta": beta,
            })
        relationship_score = _weighted(related_values)
        relationship_lead_lag_score = _weighted(lead_values)
        node_score = scores.get(ticker, 0.0)
        graph_score = _weighted([
            (risk_on_market_score, config.RELATIONAL_MARKET_WEIGHT),
            (sector_etf_score, config.RELATIONAL_SECTOR_ETF_WEIGHT),
            (sector_breadth_score, config.RELATIONAL_SECTOR_BREADTH_WEIGHT),
            (leveraged_score, config.RELATIONAL_LEVERAGED_WEIGHT),
            (relationship_score, config.RELATIONAL_DYNAMIC_WEIGHT),
        ])
        sector_deterioration = max(0.0, (previous_sector_score or 0.0) - (sector_etf_score or 0.0))
        related_previous = _weighted([
            ((previous_scores.get(item["ticker"]) or 0.0) * float(item.get("polarity") or 1.0), item["weight"])
            for item in related_payload
        ])
        relationship_deterioration = max(0.0, related_previous - relationship_score)
        total_deterioration = _weighted([
            (market_deterioration, 0.40),
            (sector_deterioration, 0.35),
            (relationship_deterioration, 0.25),
        ])
        rel_strength_market = node_score - risk_on_market_score
        rel_strength_sector = node_score - (sector_etf_score if sector_etf_score is not None else risk_on_market_score)
        out[ticker] = {
            "relational_feature_version": FEATURE_VERSION,
            "sector": features.get("sector"),
            "industry": features.get("industry"),
            "sector_etf": sector_etf,
            "related_etfs": [item["ticker"] for item in related_payload],
            "market_regime": market_regime,
            "market_context_score": round(risk_on_market_score, 4),
            "market_breadth_score": round(market_breadth, 4),
            "vix_stress_score": round(vix_stress, 4),
            "credit_risk_on_score": round(credit_risk_on, 4),
            "market_deterioration_score": round(market_deterioration, 4),
            "sector_etf_score": round(sector_etf_score or 0.0, 4),
            "sector_breadth_score": round(sector_breadth_score, 4),
            "sector_deterioration_score": round(sector_deterioration, 4),
            "leveraged_confirmation_score": round(leveraged_score, 4),
            "relationship_context_score": round(relationship_score, 4),
            "relationship_lead_lag_score": round(relationship_lead_lag_score, 4),
            "relationship_deterioration_score": round(relationship_deterioration, 4),
            "graph_context_score": round(graph_score, 4),
            "relational_deterioration_score": round(total_deterioration, 4),
            "relative_strength_vs_market": round(rel_strength_market, 4),
            "relative_strength_vs_sector": round(rel_strength_sector, 4),
            "node_direction_score": round(node_score, 4),
            "related_nodes": related_payload,
        }
    summary = {
        "feature_version": FEATURE_VERSION,
        "market_regime": market_regime,
        "market_context_score": round(risk_on_market_score, 4),
        "market_breadth_score": round(market_breadth, 4),
        "vix_stress_score": round(vix_stress, 4),
        "credit_risk_on_score": round(credit_risk_on, 4),
        "market_deterioration_score": round(market_deterioration, 4),
        "main_etf_scores": {symbol: scores.get(symbol) for symbol in ("SPY", "QQQ", "IWM", "DIA")},
        "volatility_scores": {symbol: scores.get(symbol) for symbol in ("VIX", "VVIX", "UVIX", "UVXY", "VXX", "VIXY", "VIXM", "VXZ", "SVIX", "SVXY", "ZIVB") if symbol in scores},
        "credit_scores": {symbol: scores.get(symbol) for symbol in ("HYG", "LQD")},
        "nodes": len(features_by_ticker),
        "sector_groups": {key: len(value) for key, value in groups.items()},
    }
    return out, summary


def attach_relational_features(base_features: dict, relational: dict | None) -> dict:
    result = dict(base_features)
    if relational:
        result.update(relational)
        result["relational_json"] = json.dumps(relational, ensure_ascii=False, separators=(",", ":"))
    else:
        result.setdefault("relational_feature_version", FEATURE_VERSION)
    return result
