from __future__ import annotations


def normalized_oos_metrics(metrics: dict | None) -> dict:
    """Return one stable OOS metrics shape for classic optimizer and mover discovery.

    Classic optimizer stores metrics under walk_forward.walk_forward_oos.
    Mover discovery stores the OOS aggregate directly under walk_forward.
    """
    payload = metrics or {}
    walk = payload.get("walk_forward") or {}
    nested = walk.get("walk_forward_oos") if isinstance(walk, dict) else None
    if isinstance(nested, dict) and nested:
        return nested
    if isinstance(walk, dict) and any(
        key in walk
        for key in (
            "sample", "market_dates", "target_hit_rate", "win_rate",
            "avg_return", "profit_factor", "max_drawdown",
        )
    ):
        return walk
    direct = payload.get("walk_forward_oos") or {}
    return direct if isinstance(direct, dict) else {}
