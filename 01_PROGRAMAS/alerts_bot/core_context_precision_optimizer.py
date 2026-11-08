from __future__ import annotations

"""Context-aware CORE precision optimizer (research only).

The objective is NOT maximum raw mover recall. A candidate is evaluated together
with the market state that existed at T0: broad ETFs, volatility complex,
sector ETF and relational/leveraged context. The optimizer prefers fewer,
higher-confidence directional movers when that improves precision, return and
profit factor.

Discovery is historical and immediate:
- future <=5D prices are labels only;
- all explanatory inputs are point-in-time;
- train -> validation -> untouched final test;
- daily capacity is never above the current CORE pass capacity;
- current CORE at the SAME capacity is the control;
- rolling and fixed-benchmark lanes must agree before prospective follow-up;
- LIVE is never mutated here.
"""

from collections import defaultdict
from hashlib import sha256
import inspect
import math
from statistics import mean
from typing import Any

from asset_universe import primary_sector_etf
from economic_architecture_challengers import _build_rows
from economic_waterfall_calibration import _feature_payload
from frozen_config_historical_simulation import _consensus_direction
from relational_features import _direction_score


MODE = "CORE_CONTEXT_PRECISION_OPTIMIZER_V1"
TARGET_PCT = 5.0
HORIZON_SESSIONS = 5
QUANTILE_BINS = 5
MIN_FEATURE_SAMPLE = 300
MIN_FEATURE_COVERAGE_PCT = 35.0
DAILY_CAPACITIES = (1, 3, 5, 10)

# Fixed model families. No hidden weight search: every available block receives
# one equal vote, and feature scores inside a block are averaged.
MODEL_FAMILIES = {
    "CONTEXT_LEAN": {"node": 2, "market": 2, "sector": 1, "relation": 1, "interaction": 2},
    "CONTEXT_BALANCED": {"node": 3, "market": 4, "sector": 2, "relation": 2, "interaction": 4},
    "CONTEXT_RICH": {"node": 5, "market": 6, "sector": 4, "relation": 4, "interaction": 8},
}

MAIN_ETFS = ("SPY", "QQQ", "IWM", "DIA")
LONG_VOL = ("VIX", "VVIX", "UVIX", "UVXY", "VXX", "VIXY", "VIXM", "VXZ")
SHORT_VOL = ("SVIX", "SVXY", "ZIVB")


def _f(value: Any) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return round(gains / losses, 4)


def _sign(direction: str | None) -> float:
    return 1.0 if str(direction or "").upper() == "LONG" else -1.0


def _split_dates(rows: list[dict]) -> tuple[set[str], set[str], set[str]]:
    dates = sorted({str(row.get("date") or "") for row in rows if row.get("date")})
    if len(dates) < 8:
        return set(dates), set(), set()
    n = len(dates)
    train_end = max(1, int(math.floor(n * 0.50)))
    valid_end = min(n - 1, max(train_end + 1, int(math.floor(n * 0.75))))
    return (
        set(dates[:train_end]),
        set(dates[train_end:valid_end]),
        set(dates[valid_end:]),
    )


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    pos = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    weight = pos - lo
    return float(ordered[lo]) * (1.0 - weight) + float(ordered[hi]) * weight


def _cuts(values: list[float]) -> list[float]:
    out: list[float] = []
    for idx in range(1, QUANTILE_BINS):
        value = round(_quantile(values, idx / QUANTILE_BINS), 10)
        if not out or value > out[-1]:
            out.append(value)
    return out


def _bin(value: float, cuts: list[float]) -> int:
    for idx, threshold in enumerate(cuts):
        if float(value) <= float(threshold):
            return idx
    return len(cuts)


def _strict_direction(row: dict) -> str | None:
    current = str(row.get("current_direction") or "").upper()
    if current not in {"LONG", "SHORT"}:
        return None
    result = _consensus_direction(row.get("direction_features") or {}, current)
    direction = str(result.get("direction") or current).upper()
    return direction if direction in {"LONG", "SHORT"} else current


def _path(row: dict, direction: str | None) -> dict:
    if direction == "LONG":
        return row.get("long_path") or {}
    if direction == "SHORT":
        return row.get("short_path") or {}
    return {}


def _canonical_source(
    canonical: dict[tuple[str, str], dict],
    day: str,
    ticker: str,
    *,
    expected_run_id: int | None = None,
) -> dict:
    source = canonical.get((day, ticker)) or {}
    if expected_run_id is not None and int(expected_run_id or 0) > 0:
        if int(source.get("source_scanner_run_id") or 0) != int(expected_run_id):
            return {}
    return source


def _canonical_payload(
    canonical: dict[tuple[str, str], dict],
    day: str,
    ticker: str,
    *,
    expected_run_id: int | None = None,
) -> dict:
    return _feature_payload(
        _canonical_source(
            canonical,
            day,
            ticker,
            expected_run_id=expected_run_id,
        )
    )


def _node_score(
    canonical: dict[tuple[str, str], dict],
    day: str,
    ticker: str,
    *,
    expected_run_id: int | None = None,
) -> float | None:
    payload = _canonical_payload(
        canonical,
        day,
        ticker,
        expected_run_id=expected_run_id,
    )
    return float(_direction_score(payload)) if payload else None


def _mean_existing(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return mean(clean) if clean else None


def _related_context(payload: dict, direction: str) -> dict[str, float | None]:
    sign = _sign(direction)
    related = payload.get("related_nodes")
    if not isinstance(related, list):
        related = []
    adjusted = []
    raw = []
    lead = []
    inverse_count = 0
    for item in related[:8]:
        if not isinstance(item, dict):
            continue
        value = _f(item.get("adjusted_score"))
        raw_value = _f(item.get("score"))
        if value is not None:
            adjusted.append(sign * value)
        if raw_value is not None:
            raw.append(abs(raw_value))
        lag = _f(item.get("lead_lag_corr"))
        if lag is not None:
            lead.append(abs(lag))
        polarity = _f(item.get("polarity"))
        if polarity is not None and polarity < 0:
            inverse_count += 1
    return {
        "related_aligned_mean": _mean_existing(adjusted),
        "related_abs_strength_mean": _mean_existing(raw),
        "related_lead_lag_strength": _mean_existing(lead),
        "related_inverse_count": float(inverse_count) if related else None,
        "related_node_count": float(len(related)) if related else None,
    }


def _context_features(
    row: dict,
    canonical: dict[tuple[str, str], dict],
    direction: str,
) -> tuple[dict[str, float], dict]:
    day = str(row.get("date") or "")
    ticker = str(row.get("ticker") or "").upper()
    direction = str(direction or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return {}, {"usable": False, "reason": "NO_DIRECTION"}
    sign = _sign(direction)
    candidate_source = _canonical_source(canonical, day, ticker)
    run_id = int(candidate_source.get("source_scanner_run_id") or 0)
    payload = _feature_payload(candidate_source)

    features: dict[str, float] = {}
    blocks: dict[str, int] = defaultdict(int)

    def add(block: str, name: str, value: Any) -> None:
        number = _f(value)
        if number is None:
            return
        features[f"{block}.{name}"] = float(number)
        blocks[block] += 1

    # ------------------------ candidate/node ------------------------
    # Existing modules are feature producers, not architectural constraints.
    # We expose raw point-in-time candidate state plus derived interactions so
    # the historical optimizer can discover a more reliable decision surface.
    node_aligned: dict[str, float] = {}
    for key in ("momentum_score", "scanner_trend_score", "change_percent"):
        value = _f(payload.get(key))
        if value is not None:
            aligned = sign * value
            add("node", key + "_aligned", aligned)
            node_aligned[key] = aligned
    flow = _f(payload.get("flow_direction"))
    if flow is not None:
        add("node", "flow_direction_aligned", sign * flow)
    long_score = _f(payload.get("long_score"))
    if long_score is not None:
        add("node", "long_score_bias_aligned", sign * (long_score - 50.0))
    for key in ("rvol", "rvol_percentile", "change_percentile"):
        add("node", key, payload.get(key))
    add("node", "directional_edge_abs", abs(_f(payload.get("directional_edge")) or 0.0))

    # ------------------------ broad market / VIX -------------------
    regime = str(payload.get("market_regime") or "").upper()
    for name in (
        "RISK_ON_TREND",
        "RISK_OFF_TREND",
        "RISK_OFF_VOLATILITY",
        "RANGE_PINNING",
        "MIXED_ROTATION",
    ):
        add("market", "regime_" + name.lower(), 1.0 if regime == name else 0.0)

    for key in (
        "market_context_score",
        "market_breadth_score",
        "credit_risk_on_score",
    ):
        value = _f(payload.get(key))
        if value is not None:
            add("market", key + "_aligned", sign * value)
    # Deterioration and volatility stress are risk-off quantities.
    for key in ("market_deterioration_score", "vix_stress_score"):
        value = _f(payload.get(key))
        if value is not None:
            add("market", key + "_aligned", -sign * value)

    for symbol in MAIN_ETFS:
        value = _node_score(canonical, day, symbol, expected_run_id=run_id)
        if value is not None:
            add("market", f"{symbol.lower()}_aligned", sign * value)
    for symbol in LONG_VOL:
        value = _node_score(canonical, day, symbol, expected_run_id=run_id)
        if value is not None:
            add("market", f"{symbol.lower()}_aligned", -sign * value)
    explicit_market_scores: dict[str, float] = {}
    for symbol in SHORT_VOL:
        value = _node_score(canonical, day, symbol, expected_run_id=run_id)
        if value is not None:
            aligned = sign * value
            add("market", f"{symbol.lower()}_aligned", aligned)
            explicit_market_scores[symbol] = aligned

    # Context interactions: these are deliberately simple, signed and
    # interpretable. They allow the optimizer to learn rules such as
    # "strong ticker setup + risk-off UVIX spike = reject" without hard-coding
    # today's module boundaries.
    market_context = _f(payload.get("market_context_score"))
    vix_stress = _f(payload.get("vix_stress_score"))
    node_momentum = node_aligned.get("momentum_score")
    node_trend = node_aligned.get("scanner_trend_score")
    if node_momentum is not None and market_context is not None:
        add("interaction", "momentum_x_market_context", node_momentum * sign * market_context / 100.0)
        add("interaction", "momentum_minus_market_context", node_momentum - sign * market_context)
    if node_trend is not None and market_context is not None:
        add("interaction", "trend_x_market_context", node_trend * sign * market_context / 100.0)
    if node_momentum is not None and vix_stress is not None:
        add("interaction", "momentum_x_inverse_vix", node_momentum * (-sign * vix_stress) / 100.0)
    uvix = _node_score(canonical, day, "UVIX", expected_run_id=run_id)
    if node_momentum is not None and uvix is not None:
        add("interaction", "momentum_x_uvix", node_momentum * (-sign * uvix) / 100.0)
        add("interaction", "momentum_minus_uvix_risk", node_momentum - (-sign * uvix))

    # ------------------------ sector / ETF --------------------------
    sector_etf = str(payload.get("sector_etf") or "").upper()
    if not sector_etf:
        sector_etf = str(
            primary_sector_etf(
                ticker,
                payload.get("sector"),
                payload.get("industry"),
            )
            or ""
        ).upper()
    sector_score = (
        _node_score(canonical, day, sector_etf, expected_run_id=run_id)
        if sector_etf else None
    )
    if sector_score is not None:
        sector_aligned = sign * sector_score
        add("sector", "sector_etf_explicit_aligned", sector_aligned)
        if node_momentum is not None:
            add("interaction", "momentum_x_sector_etf", node_momentum * sector_aligned / 100.0)
            add("interaction", "momentum_minus_sector_etf", node_momentum - sector_aligned)
        if node_trend is not None:
            add("interaction", "trend_minus_sector_etf", node_trend - sector_aligned)
    for key in ("sector_etf_score", "sector_breadth_score"):
        value = _f(payload.get(key))
        if value is not None:
            add("sector", key + "_aligned", sign * value)
    value = _f(payload.get("sector_deterioration_score"))
    if value is not None:
        add("sector", "sector_deterioration_aligned", -sign * value)
    for key in ("relative_strength_vs_sector",):
        value = _f(payload.get(key))
        if value is not None:
            add("sector", key + "_aligned", sign * value)

    # ------------------------ dynamic relationships ----------------
    for key in (
        "relationship_context_score",
        "relationship_lead_lag_score",
        "leveraged_confirmation_score",
        "graph_context_score",
        "relative_strength_vs_market",
    ):
        value = _f(payload.get(key))
        if value is not None:
            add("relation", key + "_aligned", sign * value)
    value = _f(payload.get("relational_deterioration_score"))
    if value is not None:
        add("relation", "relational_deterioration_aligned", -sign * value)
    related_features = _related_context(payload, direction)
    for name, value in related_features.items():
        add("relation", name, value)
    related_aligned = _f(related_features.get("related_aligned_mean"))
    if node_momentum is not None and related_aligned is not None:
        add("interaction", "momentum_x_related", node_momentum * related_aligned / 100.0)
        add("interaction", "momentum_minus_related", node_momentum - related_aligned)

    explicit_main_count = sum(
        1 for symbol in MAIN_ETFS
        if _node_score(canonical, day, symbol, expected_run_id=run_id) is not None
    )
    explicit_vol_count = sum(
        1 for symbol in (*LONG_VOL, *SHORT_VOL)
        if _node_score(canonical, day, symbol, expected_run_id=run_id) is not None
    )
    market_available = blocks.get("market", 0) >= 3
    node_available = blocks.get("node", 0) >= 2
    local_available = (
        blocks.get("sector", 0) >= 1
        or blocks.get("relation", 0) >= 2
        or blocks.get("interaction", 0) >= 2
    )
    broad_context_available = explicit_main_count >= 2
    vol_context_available = explicit_vol_count >= 1
    usable = bool(
        market_available
        and node_available
        and local_available
        and broad_context_available
        and vol_context_available
    )
    diagnostics = {
        "usable": usable,
        "direction": direction,
        "market_regime": regime or None,
        "source_scanner_run_id": run_id,
        "same_run_context_required": True,
        "sector_etf": sector_etf or None,
        "blocks": dict(blocks),
        "market_available": market_available,
        "node_available": node_available,
        "local_context_available": local_available,
        "broad_context_available": broad_context_available,
        "vol_context_available": vol_context_available,
        "explicit_main_etf_count": explicit_main_count,
        "explicit_vol_count": explicit_vol_count,
        "explicit_sector_etf": bool(sector_score is not None),
    }
    return (features if usable else {}), diagnostics


def _same_run_gex_context(
    signal_index: dict[tuple[str, str, str], list[dict]],
    day: str,
    ticker: str,
    scanner_run_id: int,
    direction: str,
) -> dict:
    """Conservative daily historical GEX join, with explicit provenance.

    A date-only join is NOT proof of exact timestamp availability; preserve
    that limitation rather than silently treating missing/late GEX as zero.
    """
    if scanner_run_id <= 0:
        return {"status": "NO_SCANNER_RUN_ID", "same_run": False}
    def safe_int(value) -> int:
        try:
            return int(float(value or 0))
        except (ValueError, TypeError, OverflowError):
            return 0
    def pick(side: str) -> dict | None:
        source = signal_index.get((day, ticker, side.lower()), []) or []
        matches = [
            s for s in source
            if str(s.get("layer") or "").upper() == "GEX_DAILY"
            and safe_int(s.get("source_scanner_run_id")) == scanner_run_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda s: _f(s.get("score")) or 0.0)
    same = pick(direction)
    opposite = pick("SHORT" if direction == "LONG" else "LONG")
    if not same and not opposite:
        return {"status": "NO_SAME_RUN_GEX", "same_run": False}
    return {
        "status": "SAME_RUN_DATE_ONLY",
        "same_run": True,
        "exact_timestamp_verified": False,
        "same_direction": bool(same),
        "opposite_direction": bool(opposite),
        "same_score": _f((same or {}).get("score")),
        "opposite_score": _f((opposite or {}).get("score")),
        "same_intent": str((same or {}).get("gex_intent") or "").upper(),
        "opposite_intent": str((opposite or {}).get("gex_intent") or "").upper(),
    }


def _build_context_rows(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
) -> list[dict]:
    architecture = _build_rows(
        canonical,
        signal_index,
        trusted_price_series,
        threshold_pct=TARGET_PCT,
        horizon_sessions=HORIZON_SESSIONS,
    )
    out: list[dict] = []
    for source in architecture:
        for direction in ("LONG", "SHORT"):
            path = _path(source, direction)
            features, context = _context_features(source, canonical, direction)
            raw_run_id = (canonical.get((
                str(source.get("date") or ""),
                str(source.get("ticker") or "").upper()
            )) or {}).get("source_scanner_run_id")
            try:
                run_id = int(float(raw_run_id or 0))
            except (TypeError, ValueError, OverflowError):
                run_id = 0
            gex_context = _same_run_gex_context(
                signal_index, str(source.get("date") or ""),
                str(source.get("ticker") or "").upper(), run_id, direction
            )
            out.append({
                **source,
                "context_direction": direction,
                "direction_hypothesis_independent_of_current_core": True,
                "context_features": features,
                "context_diagnostics": context,
                "gex_context": gex_context,
                "context_usable": bool(features),
                "directional_mover_5d": bool(
                    float(path.get("mfe_pct") or 0.0) >= TARGET_PCT
                ),
                "directional_mfe_pct": float(path.get("mfe_pct") or 0.0),
                "directional_mae_pct": float(path.get("mae_pct") or 0.0),
                "directional_return_pct": _f(path.get("horizon_close_return_pct")),
            })
    return out



def _fast_cache_contract_hash() -> str:
    """Hash point-in-time row-building semantics; search logic may evolve."""
    digest = sha256()
    for fn in (
        _strict_direction,
        _path,
        _canonical_source,
        _canonical_payload,
        _node_score,
        _mean_existing,
        _related_context,
        _context_features,
        _same_run_gex_context,
        _build_context_rows,
    ):
        digest.update(inspect.getsource(fn).encode("utf-8"))

    modules = []
    for fn in (
        primary_sector_etf,
        _build_rows,
        _feature_payload,
        _consensus_direction,
        _direction_score,
    ):
        module = inspect.getmodule(fn)
        if module is not None and module not in modules:
            modules.append(module)
    for module in modules:
        digest.update(str(getattr(module, "__name__", "")).encode("utf-8"))
        digest.update(inspect.getsource(module).encode("utf-8"))

    digest.update(
        repr(
            {
                "target_pct": TARGET_PCT,
                "horizon_sessions": HORIZON_SESSIONS,
            }
        ).encode("utf-8")
    )
    return digest.hexdigest()

def _feature_candidates(rows: list[dict]) -> dict[str, list[str]]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for key, value in (row.get("context_features") or {}).items():
            if _f(value) is not None:
                counts[key] += 1
    minimum = max(
        MIN_FEATURE_SAMPLE,
        int(math.ceil(len(rows) * MIN_FEATURE_COVERAGE_PCT / 100.0)),
    )
    by_block: dict[str, list[str]] = defaultdict(list)
    for name, count in counts.items():
        if count >= minimum and "." in name:
            block = name.split(".", 1)[0]
            by_block[block].append(name)
    return {key: sorted(value) for key, value in by_block.items()}


def _fit_feature(rows: list[dict], feature: str) -> dict | None:
    sample = [
        row for row in rows
        if _f((row.get("context_features") or {}).get(feature)) is not None
    ]
    if len(sample) < MIN_FEATURE_SAMPLE:
        return None
    values = [float(row["context_features"][feature]) for row in sample]
    cuts = _cuts(values)
    movers = sum(bool(row.get("directional_mover_5d")) for row in sample)
    non = len(sample) - movers
    base_log_odds = math.log((movers + 1.0) / (non + 1.0))
    bins = []
    weighted_abs = 0.0
    for idx in range(len(cuts) + 1):
        bucket = [
            row for row in sample
            if _bin(float(row["context_features"][feature]), cuts) == idx
        ]
        positive = sum(bool(row.get("directional_mover_5d")) for row in bucket)
        negative = len(bucket) - positive
        score = math.log((positive + 1.0) / (negative + 1.0)) - base_log_odds
        weighted_abs += abs(score) * len(bucket)
        bins.append({
            "bin": idx,
            "sample": len(bucket),
            "movers": positive,
            "precision_pct": (
                round(100.0 * positive / len(bucket), 3) if bucket else None
            ),
            "score": round(score, 8),
        })
    discrimination = weighted_abs / max(1, len(sample))
    if discrimination <= 1e-12:
        return None
    return {
        "feature": feature,
        "cuts": cuts,
        "bins": bins,
        "sample": len(sample),
        "coverage_pct": round(100.0 * len(sample) / len(rows), 3),
        "discrimination": round(discrimination, 8),
    }


def _fit_train_models(train: list[dict]) -> dict:
    available = _feature_candidates(train)
    fitted: dict[str, list[dict]] = {}
    for block, names in available.items():
        models = [model for name in names if (model := _fit_feature(train, name))]
        models.sort(
            key=lambda item: (
                -float(item.get("discrimination") or 0.0),
                str(item.get("feature") or ""),
            )
        )
        fitted[block] = models
    return fitted


def _family_models(fitted: dict, family: str) -> dict[str, list[dict]]:
    limits = MODEL_FAMILIES[family]
    return {
        block: list(fitted.get(block) or [])[: int(limit)]
        for block, limit in limits.items()
    }


def _score_feature(value: float, model: dict) -> float | None:
    idx = _bin(float(value), list(model.get("cuts") or []))
    bins = list(model.get("bins") or [])
    if idx >= len(bins):
        return None
    return _f(bins[idx].get("score"))


def _score_row(row: dict, models: dict[str, list[dict]]) -> tuple[float | None, dict]:
    features = row.get("context_features") or {}
    block_scores: dict[str, float] = {}
    detail: dict[str, list[dict]] = {}
    for block in ("node", "market", "sector", "relation", "interaction"):
        values = []
        rows = []
        for model in models.get(block) or []:
            raw = _f(features.get(model["feature"]))
            if raw is None:
                continue
            score = _score_feature(float(raw), model)
            if score is None:
                continue
            values.append(float(score))
            rows.append({
                "feature": model["feature"],
                "value": round(float(raw), 6),
                "score": round(float(score), 6),
            })
        if values:
            block_scores[block] = float(mean(values))
            detail[block] = rows

    # Reliability invariant requested by design: ticker + broad market +
    # sector/relationship context must all be observable.
    if "node" not in block_scores or "market" not in block_scores:
        return None, {"block_scores": block_scores, "feature_detail": detail}
    if (
        "sector" not in block_scores
        and "relation" not in block_scores
        and "interaction" not in block_scores
    ):
        return None, {"block_scores": block_scores, "feature_detail": detail}

    # Equal family voting avoids silently letting a large raw feature family
    # dominate. Existing module outputs and new interactions are peers here.
    score = mean(block_scores.values())
    return float(score), {
        "block_scores": {k: round(v, 6) for k, v in block_scores.items()},
        "feature_detail": detail,
    }


def _current_core_rows(rows: list[dict]) -> list[dict]:
    by_identity: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not bool((row.get("stage") or {}).get("core")):
            continue
        if row.get("current_direction") not in {"LONG", "SHORT"}:
            continue
        key = (str(row.get("date") or ""), str(row.get("ticker") or ""))
        by_identity.setdefault(key, row)
    return list(by_identity.values())


def _current_rank_key(row: dict) -> tuple:
    rank = tuple(row.get("research_rank") or ())
    return (rank, str(row.get("ticker") or ""))


def _same_capacity_control(
    day_rows: list[dict],
    capacity: int,
) -> list[dict]:
    current = _current_core_rows(day_rows)
    return sorted(current, key=_current_rank_key, reverse=True)[:capacity]


def _select_period(
    rows: list[dict],
    models: dict[str, list[dict]],
    daily_capacity: int,
) -> tuple[list[dict], list[dict], dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("date") or "")].append(row)

    control: list[dict] = []
    challenger: list[dict] = []
    scoreable = 0
    context_rejects = 0
    capacities: dict[str, int] = {}

    for day, day_rows in sorted(grouped.items()):
        capacity = max(1, int(daily_capacity))
        capacities[day] = capacity
        control.extend(_same_capacity_control(day_rows, capacity))

        scored = []
        for row in day_rows:
            if not row.get("context_usable"):
                context_rejects += 1
                continue
            score, explanation = _score_row(row, models)
            if score is None:
                context_rejects += 1
                continue
            scoreable += 1
            scored.append({
                **row,
                "context_precision_score": score,
                "context_precision_explanation": explanation,
            })

        # The new architecture is allowed to disagree with CORE direction.
        # First choose the strongest LONG/SHORT hypothesis for each ticker,
        # then rank tickers against one another.
        best_by_ticker: dict[str, dict] = {}
        for item in scored:
            ticker = str(item.get("ticker") or "")
            old = best_by_ticker.get(ticker)
            if old is None or (
                float(item.get("context_precision_score") or -1e18),
                str(item.get("context_direction") or ""),
            ) > (
                float(old.get("context_precision_score") or -1e18),
                str(old.get("context_direction") or ""),
            ):
                best_by_ticker[ticker] = item

        ranked = sorted(
            best_by_ticker.values(),
            key=lambda row: (
                float(row.get("context_precision_score") or -1e18),
                str(row.get("ticker") or ""),
            ),
            reverse=True,
        )
        challenger.extend(ranked[:capacity])

    return control, challenger, {
        "daily_capacity": int(daily_capacity),
        "dates": len(grouped),
        "scoreable_rows": scoreable,
        "context_rejects": context_rejects,
        "capacity_total": sum(capacities.values()),
        "capacity_by_date": capacities,
    }


def _metrics(rows: list[dict], *, use_context_direction: bool) -> dict:
    evaluated = []
    best_movers = correct = 0
    for row in rows:
        direction = (
            str(row.get("context_direction") or "").upper()
            if use_context_direction
            else str(row.get("current_direction") or "").upper()
        )
        path = _path(row, direction)
        if not path:
            continue
        mover = float(path.get("mfe_pct") or 0.0) >= TARGET_PCT
        if row.get("best_mover_5d"):
            best_movers += 1
            correct += int(direction == row.get("best_direction"))
        evaluated.append({
            "mover": mover,
            "mfe": float(path.get("mfe_pct") or 0.0),
            "mae": float(path.get("mae_pct") or 0.0),
            "ret": _f(path.get("horizon_close_return_pct")),
        })
    movers = [row for row in evaluated if row["mover"]]
    returns = [float(row["ret"]) for row in evaluated if row["ret"] is not None]
    return {
        "sample": len(evaluated),
        "movers": len(movers),
        "precision_pct": (
            round(100.0 * len(movers) / len(evaluated), 3) if evaluated else None
        ),
        "avg_mfe_pct": round(mean(row["mfe"] for row in evaluated), 4) if evaluated else None,
        "avg_mae_pct": round(mean(row["mae"] for row in evaluated), 4) if evaluated else None,
        "avg_horizon_close_return_pct": round(mean(returns), 4) if returns else None,
        "horizon_close_profit_factor": _profit_factor(returns),
        "direction_accuracy_on_best_movers_pct": (
            round(100.0 * correct / best_movers, 3) if best_movers else None
        ),
    }


def _context_profile(rows: list[dict]) -> dict:
    if not rows:
        return {"sample": 0}
    block_values: dict[str, list[float]] = defaultdict(list)
    feature_values: dict[str, list[float]] = defaultdict(list)
    regimes: dict[str, int] = defaultdict(int)
    explicit_sector = main_count = vol_count = 0
    profile_keys = (
        "node.momentum_score_aligned",
        "node.scanner_trend_score_aligned",
        "market.spy_aligned",
        "market.qqq_aligned",
        "market.uvix_aligned",
        "market.vix_aligned",
        "market.vvix_aligned",
        "market.vix_stress_score_aligned",
        "sector.sector_etf_explicit_aligned",
        "sector.relative_strength_vs_sector_aligned",
        "relation.relationship_context_score_aligned",
        "relation.relationship_lead_lag_score_aligned",
        "interaction.momentum_x_market_context",
        "interaction.momentum_x_uvix",
        "interaction.momentum_x_sector_etf",
        "interaction.momentum_x_related",
    )
    for row in rows:
        diag = row.get("context_diagnostics") or {}
        explicit_sector += int(bool(diag.get("explicit_sector_etf")))
        main_count += int(diag.get("explicit_main_etf_count") or 0)
        vol_count += int(diag.get("explicit_vol_count") or 0)
        payload_blocks = ((row.get("context_precision_explanation") or {}).get("block_scores") or {})
        for block, value in payload_blocks.items():
            number = _f(value)
            if number is not None:
                block_values[block].append(number)
        payload = row.get("context_features") or {}
        for key in profile_keys:
            number = _f(payload.get(key))
            if number is not None:
                feature_values[key].append(number)
        regime = str(diag.get("market_regime") or "")
        if regime:
            regimes[regime] += 1
    return {
        "sample": len(rows),
        "avg_block_scores": {
            key: round(mean(values), 5)
            for key, values in sorted(block_values.items()) if values
        },
        "avg_context_features": {
            key: round(mean(values), 5)
            for key, values in sorted(feature_values.items()) if values
        },
        "explicit_sector_etf_coverage_pct": round(100.0 * explicit_sector / len(rows), 3),
        "avg_explicit_main_etfs": round(main_count / len(rows), 3),
        "avg_explicit_vol_nodes": round(vol_count / len(rows), 3),
        "market_regimes": dict(regimes),
    }


def _comparison(rows: list[dict], models: dict[str, list[dict]], daily_capacity: int) -> dict:
    control, challenger, selection = _select_period(rows, models, daily_capacity)
    control_metrics = _metrics(control, use_context_direction=False)
    challenger_current_direction = _metrics(challenger, use_context_direction=False)
    challenger_metrics = _metrics(challenger, use_context_direction=True)

    control_keys = {(str(r["date"]), str(r["ticker"])) for r in control}
    gained = [
        row for row in challenger
        if (str(row["date"]), str(row["ticker"])) not in control_keys
    ]
    true_pos = [
        row for row in challenger
        if float(
            _path(
                row,
                str(row.get("context_direction") or "").upper(),
            ).get("mfe_pct") or 0.0
        ) >= TARGET_PCT
    ]
    false_pos = [row for row in challenger if row not in true_pos]

    cp = _f(control_metrics.get("precision_pct"))
    np = _f(challenger_metrics.get("precision_pct"))
    cr = _f(control_metrics.get("avg_horizon_close_return_pct"))
    nr = _f(challenger_metrics.get("avg_horizon_close_return_pct"))
    cpf = _f(control_metrics.get("horizon_close_profit_factor"))
    npf = _f(challenger_metrics.get("horizon_close_profit_factor"))
    cmae = _f(control_metrics.get("avg_mae_pct"))
    nmae = _f(challenger_metrics.get("avg_mae_pct"))

    checks = {
        "sample": int(challenger_metrics.get("sample") or 0) >= 30,
        "precision_positive_lift": (
            cp is not None and np is not None and np >= cp + 2.0
        ),
        "return_not_worse": (
            cr is not None and nr is not None and nr >= cr
        ),
        "return_positive": nr is not None and nr > 0.0,
        "pf_not_worse": (
            cpf is not None and npf is not None and npf >= cpf
        ),
        "pf_above_one": npf is not None and npf > 1.0,
        "mae_controlled": (
            cmae is not None and nmae is not None and nmae <= cmae + 0.5
        ),
    }
    checks["pass"] = all(checks.values())
    return {
        "selection": selection,
        "control": control_metrics,
        "challenger_selection_current_direction": challenger_current_direction,
        "challenger": challenger_metrics,
        "attribution": {
            "selection_only_precision_pp": (
                round(
                    float(challenger_current_direction.get("precision_pct") or 0.0)
                    - float(control_metrics.get("precision_pct") or 0.0),
                    3,
                )
                if challenger_current_direction.get("precision_pct") is not None
                and control_metrics.get("precision_pct") is not None
                else None
            ),
            "direction_increment_precision_pp": (
                round(
                    float(challenger_metrics.get("precision_pct") or 0.0)
                    - float(challenger_current_direction.get("precision_pct") or 0.0),
                    3,
                )
                if challenger_metrics.get("precision_pct") is not None
                and challenger_current_direction.get("precision_pct") is not None
                else None
            ),
        },
        "lift": {
            "precision_pp": (
                round(np - cp, 3) if np is not None and cp is not None else None
            ),
            "return_pp": (
                round(nr - cr, 4) if nr is not None and cr is not None else None
            ),
            "pf": (
                round(npf - cpf, 4) if npf is not None and cpf is not None else None
            ),
            "mae_pp": (
                round(nmae - cmae, 4) if nmae is not None and cmae is not None else None
            ),
        },
        "gate": checks,
        "gained_vs_same_capacity_control": _metrics(
            gained, use_context_direction=True
        ),
        "true_positive_context": _context_profile(true_pos),
        "false_positive_context": _context_profile(false_pos),
    }


def _family_signature(models: dict[str, list[dict]]) -> dict:
    return {
        block: [model["feature"] for model in values]
        for block, values in sorted(models.items())
    }


def _lane(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    lane_label: str,
    run_autonomous_research: bool = True,
) -> dict:
    rows = _build_context_rows(canonical, signal_index, trusted_price_series)
    train_dates, valid_dates, test_dates = _split_dates(rows)
    train = [row for row in rows if row.get("date") in train_dates]
    valid = [row for row in rows if row.get("date") in valid_dates]
    test = [row for row in rows if row.get("date") in test_dates]
    if not train or not valid or not test:
        return {
            "mode": MODE,
            "lane": str(lane_label).upper(),
            "status": "INSUFFICIENT",
            "live_mutation": False,
            "promotion_allowed": False,
        }

    fast_research_cache = {
        "status": "DISABLED",
        "lane": str(lane_label).upper(),
        "live_mutation": False,
    }
    try:
        import config as _config
        if bool(getattr(_config, "FAST_RESEARCH_CACHE_ENABLED", True)):
            from autonomous_research_cache import (
                update_cache_status_file,
                write_fast_research_cache,
            )
            fast_research_cache = write_fast_research_cache(
                rows=rows,
                train_dates=train_dates,
                validation_dates=valid_dates,
                test_dates=test_dates,
                lane_label=lane_label,
                producer_version=_config.BOT_VERSION,
                contract_hash=_fast_cache_contract_hash(),
                cache_root=_config.RUNTIME_ROOT / "autonomous_research" / "cache",
                compression_level=int(
                    getattr(_config, "FAST_RESEARCH_CACHE_COMPRESSION_LEVEL", 6)
                ),
            )
            update_cache_status_file(
                _config.OUTPUT_DIR
                / "historical_validation"
                / "latest_fast_research_cache_status.json",
                lane_label=lane_label,
                lane_status=fast_research_cache,
                producer_version=_config.BOT_VERSION,
            )
    except Exception as exc:
        fast_research_cache = {
            "status": "ERROR",
            "lane": str(lane_label).upper(),
            "error": f"{type(exc).__name__}: {exc}",
            "live_mutation": False,
        }

    fitted = _fit_train_models(train)
    candidates = {}
    stable = []
    for family in MODEL_FAMILIES:
        models = _family_models(fitted, family)
        # Context must actually participate.
        if not models.get("node") or not models.get("market"):
            continue
        if (
            not models.get("sector")
            and not models.get("relation")
            and not models.get("interaction")
        ):
            continue
        for daily_capacity in DAILY_CAPACITIES:
            name = f"{family}@TOP{daily_capacity}"
            train_view = _comparison(train, models, daily_capacity)
            valid_view = _comparison(valid, models, daily_capacity)
            ok = bool(
                (train_view.get("gate") or {}).get("pass")
                and (valid_view.get("gate") or {}).get("pass")
            )
            candidates[name] = {
                "family": family,
                "daily_capacity": daily_capacity,
                "selected_features": _family_signature(models),
                "train": train_view,
                "validation": valid_view,
                "stable_train_validation": ok,
            }
            if ok:
                stable.append(name)

    def _score(name: str) -> tuple:
        view = candidates[name]["validation"]
        metrics = view.get("challenger") or {}
        lift = view.get("lift") or {}
        # Reliability first, then economics, then coverage.
        return (
            float(_f(metrics.get("precision_pct")) or -1e9),
            float(_f(metrics.get("horizon_close_profit_factor")) or -1e9),
            float(_f(metrics.get("avg_horizon_close_return_pct")) or -1e9),
            float(_f(lift.get("precision_pp")) or -1e9),
            -float(candidates[name]["daily_capacity"]),
        )

    selected_name = max(stable, key=_score) if stable else None
    final_test = {}
    follow = False
    selected_features = {}
    if selected_name:
        config = candidates[selected_name]
        models = _family_models(fitted, config["family"])
        selected_features = _family_signature(models)
        final_test = _comparison(test, models, int(config["daily_capacity"]))
        follow = bool((final_test.get("gate") or {}).get("pass"))

    usable = [row for row in rows if row.get("context_usable")]
    context_diag = {
        "rows": len(rows),
        "usable_rows": len(usable),
        "usable_pct": round(100.0 * len(usable) / len(rows), 3) if rows else None,
        "avg_explicit_main_etfs": (
            round(mean(
                float((row.get("context_diagnostics") or {}).get("explicit_main_etf_count") or 0)
                for row in usable
            ), 3) if usable else None
        ),
        "avg_explicit_vol_nodes": (
            round(mean(
                float((row.get("context_diagnostics") or {}).get("explicit_vol_count") or 0)
                for row in usable
            ), 3) if usable else None
        ),
        "explicit_sector_etf_coverage_pct": (
            round(
                100.0 * sum(
                    bool((row.get("context_diagnostics") or {}).get("explicit_sector_etf"))
                    for row in usable
                ) / len(usable),
                3,
            ) if usable else None
        ),
    }

    autonomous_research = {
        "mode": "AUTONOMOUS_RESEARCH_LAB_V1",
        "lane": str(lane_label).upper(),
        "status": (
            "DISABLED"
            if run_autonomous_research
            else "DEFERRED_TO_POST_FULL_ADAPTIVE"
        ),
        "reason": (
            None
            if run_autonomous_research
            else "Fast cache is authoritative for the detached adaptive search; avoid duplicating the heavy tournament inside FULL replay."
        ),
        "live_mutation": False,
        "promotion_allowed": False,
    }
    try:
        import config as _config
        if run_autonomous_research and bool(getattr(_config, "AUTONOMOUS_RESEARCH_ENABLED", True)):
            from autonomous_research_lab import run_autonomous_research_lane
            autonomous_research = run_autonomous_research_lane(
                train=train,
                validation=valid,
                test=test,
                fitted=fitted,
                comparison_fn=_comparison,
                lane_label=lane_label,
                registry_path=(
                    _config.RUNTIME_ROOT / "autonomous_research" / "experiments.sqlite"
                    if bool(getattr(_config, "AUTONOMOUS_RESEARCH_REGISTRY_ENABLED", True))
                    else None
                ),
                max_candidates=int(getattr(_config, "AUTONOMOUS_RESEARCH_MAX_CANDIDATES", 48)),
                top_report=int(getattr(_config, "AUTONOMOUS_RESEARCH_TOP_REPORT", 20)),
                touch_final_test=False,
            )
    except Exception as exc:
        autonomous_research = {
            "mode": "AUTONOMOUS_RESEARCH_LAB_V1",
            "lane": str(lane_label).upper(),
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "live_mutation": False,
            "promotion_allowed": False,
        }

    return {
        "mode": MODE,
        "lane": str(lane_label).upper(),
        "status": "EVALUABLE",
        "live_mutation": False,
        "promotion_allowed": False,
        "objective": "PRECISION_FIRST_CONTEXT_AWARE_DIRECTIONAL_MOVERS",
        "split": {
            "train_dates": len(train_dates),
            "validation_dates": len(valid_dates),
            "test_dates": len(test_dates),
            "train_rows": len(train),
            "validation_rows": len(valid),
            "test_rows": len(test),
        },
        "context_coverage": context_diag,
        "fast_research_cache": fast_research_cache,
        "train_fit": {
            "features_by_block": {
                block: [
                    {
                        "feature": item["feature"],
                        "discrimination": item["discrimination"],
                        "coverage_pct": item["coverage_pct"],
                    }
                    for item in models
                ]
                for block, models in sorted(fitted.items())
            },
        },
        "candidate_configs": candidates,
        "stable_train_validation": stable,
        "selected_candidate": selected_name,
        "selected_features": selected_features,
        "final_test": final_test,
        "historical_followup_candidate": follow,
        "autonomous_research": autonomous_research,
        "policy": {
            "precision_prioritized_over_recall": True,
            "small_fixed_daily_capacity_search": list(DAILY_CAPACITIES),
            "current_core_capacity_is_not_a_constraint": True,
            "main_etf_context_required": True,
            "volatility_context_included": True,
            "sector_or_relationship_context_required": True,
            "existing_modules_are_features_not_constraints": True,
            "current_core_direction_is_control_only": True,
            "long_short_hypotheses_scored_independently": True,
            "new_context_interactions_allowed": True,
            "future_prices_labels_only": True,
            "train_only_feature_fit": True,
            "validation_selects_config": True,
            "final_test_touched_once_after_selection": True,
            "embedded_autonomous_research_run": bool(run_autonomous_research),
            "post_full_adaptive_can_reuse_fast_cache": True,
            "live_unchanged": True,
        },
    }


def build_core_context_precision_optimizer(
    canonical,
    signal_index,
    trusted_price_series,
    *,
    lane_label="ROLLING_LATEST",
    run_autonomous_research: bool = True,
):
    return _lane(
        canonical,
        signal_index,
        trusted_price_series,
        lane_label=lane_label,
        run_autonomous_research=run_autonomous_research,
    )


def combine_core_context_precision_optimizers(rolling: dict, fixed: dict) -> dict:
    try:
        from autonomous_research_lab import combine_autonomous_research_lanes
        autonomous_decision = combine_autonomous_research_lanes(
            rolling.get("autonomous_research") or {},
            fixed.get("autonomous_research") or {},
        )
    except Exception as exc:
        autonomous_decision = {
            "mode": "AUTONOMOUS_RESEARCH_DUAL_LANE_DECISION_V1",
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "dual_lane_survivor": False,
            "promotion_allowed": False,
            "live_mutation": False,
        }
    rn = rolling.get("selected_candidate")
    fn = fixed.get("selected_candidate")
    ro = bool(rolling.get("historical_followup_candidate"))
    fo = bool(fixed.get("historical_followup_candidate"))
    same = bool(rn and fn and rn == fn)
    dual = bool(ro and fo and same)
    return {
        "mode": "CORE_CONTEXT_PRECISION_DUAL_LANE_DECISION_V1",
        "status": (
            "EVALUABLE"
            if rolling.get("status") == fixed.get("status") == "EVALUABLE"
            else "PARTIAL"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "rolling_selected_candidate": rn,
        "fixed_selected_candidate": fn,
        "same_candidate_across_lanes": same,
        "rolling_followup": ro,
        "fixed_followup": fo,
        "dual_lane_followup": dual,
        "autonomous_research": autonomous_decision,
        "next_step": (
            "FREEZE_CONTEXT_RULE_AND_PROSPECTIVE_SHADOW"
            if dual
            else "NO_CONTEXT_PRECISION_SURVIVOR_REVISIT_RULES"
        ),
        "interpretation": (
            "Historical labels are used now for discovery/falsification. "
            "Prospective evidence is required only after a rule survives the "
            "chronological rolling + fixed final tests."
        ),
    }
