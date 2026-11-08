from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import config
from db_core import (
    get_previous_gex_summary,
    get_previous_gex_levels,
    get_previous_gex_expirations,
    get_previous_gex_expiry_buckets,
)
from time_utils import ts_utc_str, ts_madrid_str, market_date_from_madrid_iso
from utils import to_float, side_from_signal


def _pick(mapping: dict | None, *keys: str):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _row_get(mapping: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _walk_lists(obj: Any, path: str = "data"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_path = f"{path}.{key}" if path else key
            if isinstance(value, list):
                yield next_path, value
            elif isinstance(value, (dict, list)):
                yield from _walk_lists(value, next_path)
    elif isinstance(obj, list):
        for i, value in enumerate(obj[:5]):
            if isinstance(value, (dict, list)):
                yield from _walk_lists(value, f"{path}[{i}]")




def _mapping_rows(value: Any, key_name: str) -> list[dict]:
    """Convert API maps such as {"2026-07-24": {...}} into regular rows."""
    if not isinstance(value, dict):
        return []
    rows: list[dict] = []
    for key, item in value.items():
        if isinstance(item, dict):
            row = dict(item)
            row.setdefault(key_name, key)
        else:
            row = {key_name: key, "gex": item}
        rows.append(row)
    return rows

def _find_strike_rows(data: dict) -> tuple[list[dict], str | None]:
    preferred = ["gexByStrike", "gex_by_strike", "byStrike", "strikeGex", "strikeExposure"]
    for key in preferred:
        rows = data.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)], f"data.{key}"
        mapped = _mapping_rows(rows, "strike")
        if mapped:
            return mapped, f"data.{key}"
    for path, rows in _walk_lists(data):
        dict_rows = [x for x in rows if isinstance(x, dict)]
        if not dict_rows:
            continue
        sample = dict_rows[0]
        keys = {str(k).lower() for k in sample}
        if "strike" in keys and any("gex" in k or "gamma" in k for k in keys):
            return dict_rows, path
    return [], None


def _find_expiry_rows(data: dict) -> tuple[list[dict], str | None]:
    preferred = [
        "gexByExpiry", "gexByExpiration", "gexByExpirationDate", "gex_by_expiry",
        "gex_by_expiration", "byExpiry", "byExpiration", "expiryGex", "expirationGex",
    ]
    for key in preferred:
        rows = data.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)], f"data.{key}"
        mapped = _mapping_rows(rows, "expirationDate")
        if mapped:
            return mapped, f"data.{key}"
    expiry_tokens = ("expiry", "expiration", "expirationdate", "expirydate")
    for path, rows in _walk_lists(data):
        dict_rows = [x for x in rows if isinstance(x, dict)]
        if not dict_rows:
            continue
        sample = dict_rows[0]
        keys = {str(k).lower().replace("_", "") for k in sample}
        has_expiry = any(any(token in key for token in expiry_tokens) for key in keys)
        has_gex = any("gex" in key or "gamma" in key for key in keys)
        if has_expiry and has_gex and "strike" not in keys:
            return dict_rows, path
    return [], None


def _parse_expiration(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    text = text[:10] if len(text) >= 10 and text[4:5] == "-" else text
    for fmt in (None, "%m/%d/%Y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            if fmt is None:
                return date.fromisoformat(text[:10]).isoformat()
            return datetime.strptime(text, fmt).date().isoformat()
        except Exception:
            pass
    return text


def _dte(expiration_date: str | None, explicit: Any = None) -> int | None:
    explicit_f = to_float(explicit)
    if explicit_f is not None:
        return int(round(explicit_f))
    if not expiration_date:
        return None
    try:
        today = date.fromisoformat(ts_madrid_str()[:10])
        return (date.fromisoformat(expiration_date[:10]) - today).days
    except Exception:
        return None


def expiry_bucket(dte: int | None) -> str:
    """Preserve the full TMAD expiry profile while retaining granular short buckets."""
    if dte is None:
        return "unknown"
    if dte < 0:
        return "expired"
    if dte <= config.GEX_EXPIRY_FRONT_MAX_DTE:
        return "front_0_2d"
    if dte <= config.GEX_EXPIRY_SHORT_MAX_DTE:
        return "week_3_7d"
    if dte <= config.GEX_EXPIRY_NEXT_MAX_DTE:
        return "next_8_14d"
    if dte <= 31:
        return "month_15_31d"
    if dte <= 56:
        return "swing_32_56d"
    return "long_57d_plus"


def _pct_change(current: float | None, previous: float | None) -> tuple[float | None, float | None]:
    if current is None or previous is None:
        return None, None
    absolute = current - previous
    denom = max(abs(previous), float(config.GEX_MIN_CHANGE_DENOMINATOR or 1.0))
    return absolute, absolute / denom * 100.0


def _sign_flip(current: float | None, previous: float | None) -> int:
    if current is None or previous is None or current == 0 or previous == 0:
        return 0
    return 1 if (current > 0) != (previous > 0) else 0


def summarize_gex_payload(
    gex_payload: dict | None,
    current_price=None,
    ticker: str | None = None,
    gex_run_id: int | None = None,
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    data = gex_payload.get("data", {}) if isinstance(gex_payload, dict) else {}
    if not isinstance(data, dict):
        data = {}
    strike_rows, strike_source_path = _find_strike_rows(data)
    if config.GEX_EXPIRY_ENABLED:
        expiry_rows_raw, expiry_source_path = _find_expiry_rows(data)
    else:
        expiry_rows_raw, expiry_source_path = [], None

    previous_levels = get_previous_gex_levels(ticker, before_run_id=gex_run_id) if ticker else {}
    previous_expiries = get_previous_gex_expirations(ticker, before_run_id=gex_run_id) if ticker else {}
    previous_buckets = get_previous_gex_expiry_buckets(ticker, before_run_id=gex_run_id) if ticker else {}

    out_levels: list[dict] = []
    max_pos = max_neg = dominant = None
    pos_total = 0.0
    neg_total_abs = 0.0
    nearest_pos_above = nearest_neg_below = None
    price = to_float(current_price)

    for row in strike_rows:
        strike = to_float(_row_get(row, ("strike", "strikePrice", "priceLevel")))
        gex = to_float(_row_get(row, ("gex", "netGex", "totalGex", "gammaExposure", "netGamma")))
        if strike is None or gex is None:
            continue
        previous = previous_levels.get(float(strike), {})
        previous_gex = to_float(previous.get("gex"))
        delta_abs, delta_pct = _pct_change(gex, previous_gex)
        level = {
            "strike": strike,
            "gex": gex,
            "call_gex": to_float(_row_get(row, ("callGex", "callsGex", "callGammaExposure", "callGamma"))),
            "put_gex": to_float(_row_get(row, ("putGex", "putsGex", "putGammaExposure", "putGamma"))),
            "call_oi": to_float(_row_get(row, ("callOI", "callOi", "callsOpenInterest", "callOpenInterest"))),
            "put_oi": to_float(_row_get(row, ("putOI", "putOi", "putsOpenInterest", "putOpenInterest"))),
            "previous_gex": previous_gex,
            "gex_change_abs": delta_abs,
            "gex_change_pct": delta_pct,
            "sign_flip": _sign_flip(gex, previous_gex),
        }
        out_levels.append(level)
        if gex >= 0:
            pos_total += gex
        else:
            neg_total_abs += abs(gex)
        if max_pos is None or gex > max_pos["gex"]:
            max_pos = {"strike": strike, "gex": gex}
        if max_neg is None or gex < max_neg["gex"]:
            max_neg = {"strike": strike, "gex": gex}
        if dominant is None or abs(gex) > abs(dominant["gex"]):
            dominant = {"strike": strike, "gex": gex}
        if price is not None:
            if strike >= price and gex > 0 and (nearest_pos_above is None or strike < nearest_pos_above["strike"]):
                nearest_pos_above = {"strike": strike, "gex": gex}
            if strike <= price and gex < 0 and (nearest_neg_below is None or strike > nearest_neg_below["strike"]):
                nearest_neg_below = {"strike": strike, "gex": gex}

    total_gex = to_float(_pick(data, "totalGex", "netGex", "totalGammaExposure", "netGamma"))
    if total_gex is None and out_levels:
        total_gex = sum(x["gex"] for x in out_levels if x.get("gex") is not None)
    gamma_flip = to_float(_pick(data, "gammaFlip", "zeroGamma", "gammaFlipPrice", "zeroGammaLevel"))
    previous_summary = get_previous_gex_summary(ticker, before_run_id=gex_run_id) if ticker else None
    previous_price = previous_summary.get("current_price") if previous_summary else None
    previous_total = previous_summary.get("total_gex") if previous_summary else None
    previous_dominant = previous_summary.get("dominant_strike") if previous_summary else None
    total_change_abs, total_change_pct = _pct_change(total_gex, to_float(previous_total))
    price_change_pct = None
    if previous_price not in (None, 0) and price is not None:
        price_change_pct = ((price - float(previous_price)) / float(previous_price)) * 100.0

    call_acc = put_acc = None
    if pos_total or neg_total_abs:
        denom = max(pos_total + neg_total_abs, 1.0)
        call_acc = pos_total / denom * 100.0
        put_acc = neg_total_abs / denom * 100.0
    profile_bias = "neutral"
    note = ""
    if call_acc is not None and put_acc is not None:
        if call_acc >= 60 and call_acc > put_acc:
            profile_bias = "call_accumulation"
            note = "Positive GEX dominates profile."
        elif put_acc >= 60 and put_acc > call_acc:
            profile_bias = "put_accumulation"
            note = "Negative GEX dominates profile."
        elif dominant and dominant["gex"] > 0:
            profile_bias = "positive_dominant_strike"
        elif dominant and dominant["gex"] < 0:
            profile_bias = "negative_dominant_strike"

    horizon_data = data.get("summaryByHorizon") if isinstance(data.get("summaryByHorizon"), dict) else {}
    def _hz(name: str, field: str):
        row = horizon_data.get(name) if isinstance(horizon_data.get(name), dict) else {}
        return to_float(row.get(field))

    summary = {
        "total_gex": total_gex,
        "gross_gex": _hz("all", "grossGex"),
        "call_wall_strike": _hz("all", "callWallStrike"),
        "call_wall_gex": _hz("all", "callWallGex"),
        "put_wall_strike": _hz("all", "putWallStrike"),
        "put_wall_gex": _hz("all", "putWallGex"),
        "near_gex": _hz("near", "gex"),
        "near_call_gex": _hz("near", "callGex"),
        "near_put_gex": _hz("near", "putGex"),
        "near_gross_gex": _hz("near", "grossGex"),
        "near_gamma_flip": _hz("near", "gammaFlip"),
        "near_call_wall_strike": _hz("near", "callWallStrike"),
        "near_call_wall_gex": _hz("near", "callWallGex"),
        "near_put_wall_strike": _hz("near", "putWallStrike"),
        "near_put_wall_gex": _hz("near", "putWallGex"),
        "swing_gex": _hz("swing", "gex"),
        "swing_call_gex": _hz("swing", "callGex"),
        "swing_put_gex": _hz("swing", "putGex"),
        "swing_gross_gex": _hz("swing", "grossGex"),
        "swing_gamma_flip": _hz("swing", "gammaFlip"),
        "swing_call_wall_strike": _hz("swing", "callWallStrike"),
        "swing_call_wall_gex": _hz("swing", "callWallGex"),
        "swing_put_wall_strike": _hz("swing", "putWallStrike"),
        "swing_put_wall_gex": _hz("swing", "putWallGex"),
        "long_gex": _hz("long", "gex"),
        "long_call_gex": _hz("long", "callGex"),
        "long_put_gex": _hz("long", "putGex"),
        "long_gross_gex": _hz("long", "grossGex"),
        "long_gamma_flip": _hz("long", "gammaFlip"),
        "long_call_wall_strike": _hz("long", "callWallStrike"),
        "long_call_wall_gex": _hz("long", "callWallGex"),
        "long_put_wall_strike": _hz("long", "putWallStrike"),
        "long_put_wall_gex": _hz("long", "putWallGex"),
        "gamma_flip": gamma_flip,
        "positive_gex_total": pos_total,
        "negative_gex_total_abs": neg_total_abs,
        "dominant_strike": dominant["strike"] if dominant else None,
        "dominant_gex": dominant["gex"] if dominant else None,
        "max_pos_strike": max_pos["strike"] if max_pos else None,
        "max_pos_gex": max_pos["gex"] if max_pos else None,
        "max_neg_strike": max_neg["strike"] if max_neg else None,
        "max_neg_gex": max_neg["gex"] if max_neg else None,
        "nearest_pos_strike_above": nearest_pos_above["strike"] if nearest_pos_above else None,
        "nearest_pos_gex_above": nearest_pos_above["gex"] if nearest_pos_above else None,
        "nearest_neg_strike_below": nearest_neg_below["strike"] if nearest_neg_below else None,
        "nearest_neg_gex_below": nearest_neg_below["gex"] if nearest_neg_below else None,
        "previous_price": previous_price,
        "price_change_from_prev_pct": price_change_pct,
        "previous_total_gex": previous_total,
        "total_gex_change_abs": total_change_abs,
        "total_gex_change_pct": total_change_pct,
        "previous_dominant_strike": previous_dominant,
        "dominant_strike_changed": 1 if previous_dominant is not None and dominant and previous_dominant != dominant["strike"] else 0,
        "call_accumulation_score": call_acc,
        "put_accumulation_score": put_acc,
        "profile_bias": profile_bias,
        "profile_note": note,
        "strike_source_path": strike_source_path,
        "expiry_source_path": expiry_source_path,
    }

    expirations: list[dict] = []
    total_abs = 0.0
    for row in expiry_rows_raw:
        expiration_date = _parse_expiration(_row_get(row, (
            "expirationDate", "expiryDate", "expiration", "expiry", "date", "expiration_date", "expiry_date"
        )))
        if not expiration_date:
            continue
        dte = _dte(expiration_date, _row_get(row, ("dte", "daysToExpiration", "daysToExpiry", "days")))
        bucket = expiry_bucket(dte)
        if bucket == "expired":
            continue
        if bucket == "unknown" and not config.GEX_EXPIRY_INCLUDE_UNKNOWN_DTE:
            continue
        net_gex = to_float(_row_get(row, ("gex", "netGex", "totalGex", "gammaExposure", "netGamma")))
        call_gex = to_float(_row_get(row, ("callGex", "callsGex", "callGammaExposure", "callGamma")))
        put_gex = to_float(_row_get(row, ("putGex", "putsGex", "putGammaExposure", "putGamma")))
        if net_gex is None and call_gex is not None and put_gex is not None:
            net_gex = call_gex + put_gex
        if net_gex is None:
            continue
        previous = previous_expiries.get(expiration_date, {})
        previous_net = to_float(previous.get("net_gex"))
        delta_abs, delta_pct = _pct_change(net_gex, previous_net)
        item = {
            "expiration_date": expiration_date,
            "dte": dte,
            "expiry_bucket": bucket,
            "net_gex": net_gex,
            "call_gex": call_gex,
            "put_gex": put_gex,
            "call_oi": to_float(_row_get(row, ("callOI", "callOi", "callOpenInterest"))),
            "put_oi": to_float(_row_get(row, ("putOI", "putOi", "putOpenInterest"))),
            "abs_gex": abs(net_gex),
            "share_abs_gex": None,
            "previous_net_gex": previous_net,
            "net_gex_change_abs": delta_abs,
            "net_gex_change_pct": delta_pct,
            "sign_flip": _sign_flip(net_gex, previous_net),
            "source_path": expiry_source_path,
        }
        expirations.append(item)
        total_abs += abs(net_gex)
    if total_abs:
        for item in expirations:
            item["share_abs_gex"] = item["abs_gex"] / total_abs * 100.0

    buckets: list[dict] = []
    bucket_names = [
        "all_captured", "near_0_7d", "swing_8_56d", "long_57d_plus",
        "month_0_31d", "short_0_7d", "front_0_2d", "week_3_7d",
        "next_8_14d", "month_15_31d", "swing_32_56d", "unknown",
    ]
    for bucket_name in bucket_names:
        if bucket_name == "all_captured":
            members = list(expirations)
        elif bucket_name == "month_0_31d":
            members = [x for x in expirations if x.get("dte") is not None and 0 <= int(x["dte"]) <= 31]
        elif bucket_name in {"near_0_7d", "short_0_7d"}:
            members = [x for x in expirations if x.get("dte") is not None and 0 <= int(x["dte"]) <= 7]
        elif bucket_name == "swing_8_56d":
            members = [x for x in expirations if x.get("dte") is not None and 8 <= int(x["dte"]) <= 56]
        elif bucket_name == "long_57d_plus":
            members = [x for x in expirations if x.get("dte") is not None and int(x["dte"]) >= 57]
        else:
            members = [x for x in expirations if x["expiry_bucket"] == bucket_name]
        if not members:
            continue
        net = sum(x["net_gex"] for x in members if x.get("net_gex") is not None)
        call = sum(x["call_gex"] for x in members if x.get("call_gex") is not None) or None
        put = sum(x["put_gex"] for x in members if x.get("put_gex") is not None) or None
        pos = sum(max(x["net_gex"], 0) for x in members)
        neg_abs = sum(abs(min(x["net_gex"], 0)) for x in members)
        dominant_exp = max(members, key=lambda x: abs(x["net_gex"]))
        previous = previous_buckets.get(bucket_name, {})
        previous_net = to_float(previous.get("net_gex"))
        delta_abs, delta_pct = _pct_change(net, previous_net)
        dtes = [x["dte"] for x in members if x.get("dte") is not None]
        buckets.append({
            "expiry_bucket": bucket_name,
            "min_dte": min(dtes) if dtes else None,
            "max_dte": max(dtes) if dtes else None,
            "expiration_count": len(members),
            "net_gex": net,
            "call_gex": call,
            "put_gex": put,
            "positive_gex_total": pos,
            "negative_gex_total_abs": neg_abs,
            "abs_gex_total": pos + neg_abs,
            "dominant_expiration": dominant_exp["expiration_date"],
            "dominant_expiration_gex": dominant_exp["net_gex"],
            "previous_net_gex": previous_net,
            "net_gex_change_abs": delta_abs,
            "net_gex_change_pct": delta_pct,
            "sign_flip": _sign_flip(net, previous_net),
        })

    return summary, out_levels, expirations, buckets


def _first_zone(zones, ztype: str):
    if not isinstance(zones, list):
        return {}
    ztype_l = ztype.lower()
    for z in zones:
        if isinstance(z, dict) and str(z.get("type", "")).lower() == ztype_l:
            return z
    return {}


def _wall_value(scanner_row: dict, detail: dict, aliases: tuple[str, ...]):
    for source in (scanner_row, detail):
        value = _row_get(source, aliases)
        if value is not None:
            return value
    return None


def build_scanner_snapshot(scanner_run_id: int, session_type: str, ticker: str, scanner_row: dict, detail_payload: dict | None, intraday_payload: dict | None, gex_summary: dict | None, hashes: dict, statuses: dict) -> dict:
    detail = detail_payload.get("data", {}) if detail_payload else {}
    intraday = intraday_payload.get("data", {}) if intraday_payload else {}
    if not isinstance(detail, dict):
        detail = {}
    if not isinstance(intraday, dict):
        intraday = {}
    session_stats = intraday.get("sessionStats", {}) or {}
    key_levels = intraday.get("keyLevels", {}) or {}
    option_signal = detail.get("optionSignal") or scanner_row.get("optionSignal")
    current_price = to_float(detail.get("currentPrice") if detail.get("currentPrice") is not None else scanner_row.get("currentPrice"))
    change_pct = to_float(detail.get("priceChangePercent") if detail.get("priceChangePercent") is not None else scanner_row.get("changePercent"))
    gex_summary = gex_summary or {}
    utc = ts_utc_str()
    mad = ts_madrid_str()

    daily_supply = _first_zone(detail.get("dailyZones"), "supply")
    daily_demand = _first_zone(detail.get("dailyZones"), "demand")
    weekly_supply = _first_zone(detail.get("weeklyZones"), "supply")
    weekly_demand = _first_zone(detail.get("weeklyZones"), "demand")

    put_wall = to_float(_wall_value(scanner_row, detail, ("putWall", "putWallStrike", "put_wall", "putWallPrice")))
    call_wall = to_float(_wall_value(scanner_row, detail, ("callWall", "callWallStrike", "call_wall", "callWallPrice")))
    if put_wall is None:
        put_wall = to_float(gex_summary.get("max_neg_strike"))
    if call_wall is None:
        call_wall = to_float(gex_summary.get("max_pos_strike"))
    put_wall_pct = to_float(_wall_value(scanner_row, detail, ("putWallPct", "putWallPercent", "putWallDistancePct", "put_wall_pct")))
    call_wall_pct = to_float(_wall_value(scanner_row, detail, ("callWallPct", "callWallPercent", "callWallDistancePct", "call_wall_pct")))
    if put_wall_pct is None:
        put_wall_pct = distance_to_price_pct(put_wall, current_price)
    if call_wall_pct is None:
        call_wall_pct = distance_to_price_pct(call_wall, current_price)
    wall_band = _wall_value(scanner_row, detail, ("wallBand", "wallBandData", "wall_band"))

    return {
        "scanner_run_id": scanner_run_id,
        "captured_at_utc": utc,
        "captured_at_madrid": mad,
        "snapshot_date": market_date_from_madrid_iso(mad),
        "session_type": session_type,
        "ticker": ticker,
        "name": scanner_row.get("name") or detail.get("name") or detail.get("companyName"),
        "category": scanner_row.get("category") or detail.get("category"),
        "side": side_from_signal(option_signal),
        "current_price": current_price,
        "change_percent": change_pct,
        "option_signal": option_signal,
        "unusual_flow_signal": scanner_row.get("unusualFlowSignal") or detail.get("unusualFlowSignal"),
        "total_gex": gex_summary.get("total_gex") if gex_summary.get("total_gex") is not None else to_float(scanner_row.get("totalGex")),
        "gex_change": to_float(scanner_row.get("gexChange")),
        "gamma_flip": gex_summary.get("gamma_flip"),
        "max_pos_strike": gex_summary.get("max_pos_strike"),
        "max_pos_gex": gex_summary.get("max_pos_gex"),
        "max_neg_strike": gex_summary.get("max_neg_strike"),
        "max_neg_gex": gex_summary.get("max_neg_gex"),
        "put_wall": put_wall,
        "call_wall": call_wall,
        "put_wall_pct": put_wall_pct,
        "call_wall_pct": call_wall_pct,
        "put_wall_gex": to_float(_wall_value(scanner_row, detail, ("putWallGex", "putWallGamma", "put_wall_gex"))) or gex_summary.get("max_neg_gex"),
        "call_wall_gex": to_float(_wall_value(scanner_row, detail, ("callWallGex", "callWallGamma", "call_wall_gex"))) or gex_summary.get("max_pos_gex"),
        "put_wall_strength": to_float(_wall_value(scanner_row, detail, ("putWallStrength", "put_wall_strength"))),
        "call_wall_strength": to_float(_wall_value(scanner_row, detail, ("callWallStrength", "call_wall_strength"))),
        "wall_range_pct": (call_wall_pct - put_wall_pct) if call_wall_pct is not None and put_wall_pct is not None else None,
        "wall_band_json": json.dumps(wall_band, ensure_ascii=False) if wall_band is not None else None,
        "max_delta_gex_strike": None,
        "max_delta_gex_value": None,
        "nearest_supply": to_float(key_levels.get("nearestSupply")),
        "nearest_demand": to_float(key_levels.get("nearestDemand")),
        "zone_scope": "D",
        "vwap": to_float(session_stats.get("vwap")),
        "rsi": to_float(session_stats.get("rsi")),
        "rvol": to_float(scanner_row.get("rvol") if scanner_row.get("rvol") is not None else session_stats.get("rvol")),
        "signals_available": to_float(scanner_row.get("signalsAvailable")),
        "trend": scanner_row.get("trend"),
        "momentum_score": to_float(scanner_row.get("momentumScore")),
        "rsi14": to_float(scanner_row.get("rsi14")),
        "iv_z_spread": to_float(scanner_row.get("ivZSpread")),
        "iv_change": to_float(scanner_row.get("ivChange")),
        "otm_call_volume_z": to_float(scanner_row.get("otmCallVolumeZ")),
        "otm_put_volume_z": to_float(scanner_row.get("otmPutVolumeZ")),
        "pc_ratio": to_float(scanner_row.get("pcRatio")),
        "long_score": to_float(scanner_row.get("longScore")),
        "long_score_signals": to_float(scanner_row.get("longScoreSignals")),
        "price_change": to_float(detail.get("priceChange")),
        "is_live_price": 1 if detail.get("isLivePrice") is True else (0 if detail.get("isLivePrice") is False else None),
        "put_call_ratio": to_float(detail.get("putCallRatio")),
        "detail_iv_z_spread": to_float(detail.get("ivZSpread")),
        "detail_momentum_score": to_float(detail.get("momentumScore")),
        "detail_trend": detail.get("trend"),
        "daily_supply_top": to_float(daily_supply.get("top")),
        "daily_supply_bottom": to_float(daily_supply.get("bottom")),
        "daily_supply_strength": to_float(daily_supply.get("strength")),
        "daily_demand_top": to_float(daily_demand.get("top")),
        "daily_demand_bottom": to_float(daily_demand.get("bottom")),
        "daily_demand_strength": to_float(daily_demand.get("strength")),
        "weekly_supply_top": to_float(weekly_supply.get("top")),
        "weekly_supply_bottom": to_float(weekly_supply.get("bottom")),
        "weekly_supply_strength": to_float(weekly_supply.get("strength")),
        "weekly_demand_top": to_float(weekly_demand.get("top")),
        "weekly_demand_bottom": to_float(weekly_demand.get("bottom")),
        "weekly_demand_strength": to_float(weekly_demand.get("strength")),
        "detail_status": statuses.get("detail_status"),
        "intraday_status": statuses.get("intraday_status"),
        "gex_status": statuses.get("gex_status"),
        "error_message": statuses.get("error_message"),
        "raw_scanner_hash": hashes.get("scanner"),
        "raw_detail_hash": hashes.get("detail"),
        "raw_intraday_hash": hashes.get("intraday"),
        "raw_gex_hash": hashes.get("gex"),
        "created_at_utc": utc,
    }


def distance_to_price_pct(strike, price):
    strike = to_float(strike)
    price = to_float(price)
    if strike is None or price in (None, 0):
        return None
    return ((strike - price) / price) * 100.0
