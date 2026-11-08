from __future__ import annotations

"""Shared directional return math for TMAD historical/research paths.

The denominator for both LONG and SHORT underlying P&L is the entry notional.
This keeps underlying returns bounded at +100% for a fully collapsing SHORT and
avoids the inverse-return distortion (entry / exit - 1) that exaggerates large
down moves.
"""

import json
from typing import Any


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if out == out else None


def directional_return_pct(direction: str, entry: Any, exit_price: Any) -> float | None:
    e = _f(entry)
    x = _f(exit_price)
    if e is None or x is None or e <= 0 or x <= 0:
        return None
    side = str(direction or "").upper()
    if side == "LONG":
        return (x - e) / e * 100.0
    if side == "SHORT":
        return (e - x) / e * 100.0
    return None


def favorable_move_pct(direction: str, entry: Any, favorable_price: Any) -> float | None:
    return directional_return_pct(direction, entry, favorable_price)


def adverse_move_pct(direction: str, entry: Any, adverse_price: Any) -> float | None:
    return directional_return_pct(direction, entry, adverse_price)


def normalize_signal_outcome_row(row: dict, *, target_pct: float = 5.0) -> dict:
    """Recompute persisted SHORT outcome metrics from the stored future path.

    This is deliberately read-time safe and idempotent, so archived rows created
    by older TMAD versions are corrected even before their archive files are
    physically rewritten.
    """
    item = dict(row)
    if str(item.get("direction") or "").lower() != "short":
        return item
    entry = _f(item.get("entry_price"))
    if entry is None or entry <= 0:
        return item

    raw = item.get("sessions_json") or item.get("future_path_json") or "[]"
    try:
        sessions = raw if isinstance(raw, list) else json.loads(raw)
    except Exception:
        sessions = []
    if not isinstance(sessions, list) or not sessions:
        item["return_math_status"] = "SHORT_PATH_UNAVAILABLE"
        return item

    favorable: list[float] = []
    adverse: list[float] = []
    first5 = None
    last_close = None
    threshold = float(target_pct)
    for idx, session in enumerate(sessions, 1):
        if not isinstance(session, dict):
            continue
        close = _f(session.get("close"))
        high = _f(session.get("high"))
        low = _f(session.get("low"))
        if low is None:
            low = close
        if high is None:
            high = close
        if close is not None and close > 0:
            last_close = close
        if low is None or high is None or low <= 0 or high <= 0:
            continue
        fav = favorable_move_pct("SHORT", entry, low)
        adv = adverse_move_pct("SHORT", entry, high)
        if fav is None or adv is None:
            continue
        favorable.append(float(fav))
        adverse.append(float(adv))
        if first5 is None and float(fav) >= threshold:
            first5 = idx

    if not favorable or last_close is None:
        item["return_math_status"] = "SHORT_PATH_UNAVAILABLE"
        return item

    max_fav = max(favorable)
    max_adv = min(adverse)
    close_ret = directional_return_pct("SHORT", entry, last_close)
    item["movement_5pct_hit"] = 1 if first5 is not None else 0
    item["first_5pct_session"] = first5
    item["max_favorable_pct"] = round(max_fav, 6)
    item["max_adverse_pct"] = round(max_adv, 6)
    item["close_return_pct"] = round(float(close_ret or 0.0), 6)

    predicted = _f(item.get("target_pct"))
    if predicted is not None:
        item["target_error_pct"] = round(max_fav - predicted, 6)

    first_target = item.get("first_target_session")
    complete = bool(int(item.get("complete") or 0))
    item["resolved_reason"] = (
        "TARGET_GEX" if first_target not in (None, "", 0)
        else ("MOVE_5PCT" if first5 is not None else ("HORIZON" if complete else "PENDING"))
    )
    item["return_math_status"] = "SHORT_V2_ENTRY_NOTIONAL"
    return item
