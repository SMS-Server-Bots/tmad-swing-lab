from __future__ import annotations

import math
from typing import Any

# These fields are available in CORE and/or live Scanner observations.  The
# explicit deltas let discovery study what changed before a mover, not only the
# static snapshot at the entry date.
TRANSITION_FIELDS = (
    "total_gex", "total_gex_change_pct", "momentum_score", "scanner_trend_score",
    "rsi", "rvol", "change_percent", "momentum_5d_pct", "price_vs_sma20_pct",
    "relative_strength_vs_market", "relative_strength_vs_sector",
    "market_context_score", "sector_etf_score", "market_breadth_score",
    "sector_breadth_score", "relationship_context_score",
    "relationship_lead_lag_score", "graph_context_score",
    "relational_deterioration_score", "flow_direction", "trend_direction",
    "atr_pct", "realized_volatility_20d",
    "gross_gex", "near_gex", "swing_gex", "long_gex",
    "near_gross_share_pct", "swing_gross_share_pct", "long_gross_share_pct",
    "gamma_flip_distance_pct", "near_gamma_flip_distance_pct",
    "swing_gamma_flip_distance_pct", "near_call_wall_strike_distance_pct",
    "near_put_wall_strike_distance_pct", "swing_call_wall_strike_distance_pct",
    "swing_put_wall_strike_distance_pct", "profile_changed_strikes",
    "profile_changed_expiries", "profile_material_change", "gex_term_near_minus_swing",
)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def add_transition_features(current: dict, previous: dict | None) -> dict:
    result = dict(current or {})
    previous = previous or {}
    changed = 0
    available = 0
    for field in TRANSITION_FIELDS:
        now = _number(result.get(field))
        before = _number(previous.get(field))
        key = f"delta_{field}"
        if now is None or before is None:
            result[key] = None
            continue
        delta = now - before
        result[key] = round(delta, 8)
        available += 1
        if abs(delta) > 1e-12:
            changed += 1
    result["transition_fields_available"] = available
    result["transition_fields_changed"] = changed
    result["has_previous_observation"] = 1 if previous else 0
    return result
