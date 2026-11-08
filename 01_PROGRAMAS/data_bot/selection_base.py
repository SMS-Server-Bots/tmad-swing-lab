from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable
import re
import json
import os
from datetime import date
from pathlib import Path

from utils import ticker_from_row, to_float

_EMPTY_SIGNALS = {"", "none", "neutral", "n/a", "na", "null", "false", "0", "no signal"}

_PARENT_PRODUCTS: dict[str, tuple[str, ...]] = {
    "SPY": ("UPRO", "SPXL", "SPXS", "SPXU", "SH"),
    "QQQ": ("TQQQ", "QLD", "SQQQ", "QID", "PSQ"),
    "IWM": ("TNA", "UWM", "TZA", "TWM", "RWM"),
    "DIA": ("UDOW", "DDM", "SDOW", "DXD", "DOG"),
    "SMH": ("SOXL", "SOXS"), "SOXX": ("SOXL", "SOXS"),
    "XLF": ("FAS", "FAZ"), "KRE": ("DPST", "WDRW"),
    "XBI": ("LABU", "LABD"), "XLE": ("ERX", "ERY"),
    "ARKK": ("TARK", "SARK"), "GDX": ("NUGT", "DUST"),
    "GDXJ": ("JNUG", "JDST"), "TLT": ("TMF", "TMV", "TBT"),
    "VIX": ("VVIX", "UVIX", "UVXY", "VXX", "VIXY", "VIXM", "VXZ", "SVIX", "SVXY", "ZIVB"),
}
_SINGLE_STOCK_PARENT: dict[str, str] = {
    "TSLL": "TSLA", "TSLQ": "TSLA", "TSLS": "TSLA",
    "NVDL": "NVDA", "NVDS": "NVDA", "NVDQ": "NVDA",
    "AAPU": "AAPL", "AAPD": "AAPL", "GGLL": "GOOGL", "GGLS": "GOOGL",
    "AMZU": "AMZN", "AMZD": "AMZN", "MSFU": "MSFT", "MSFD": "MSFT",
    "METU": "META", "METD": "META", "NFLU": "NFLX", "NFLD": "NFLX",
    "AMDL": "AMD", "AMDS": "AMD", "CONL": "COIN", "CONY": "COIN",
    "MSTU": "MSTR", "MSTX": "MSTR", "MSTZ": "MSTR",
}


def _active_gex_negative_cache() -> set[str]:
    """Return tickers still inside a persisted 400/404 backoff window."""
    root = Path(os.getenv("TMAD_DATA_DIR", Path(__file__).resolve().parents[2] / "03_DATOS"))
    path = root / "data_bot" / "output" / "gex_negative_cache.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    today = date.today().isoformat()
    return {str(t).upper() for t, item in (payload.get("tickers") or {}).items() if str((item or {}).get("retry_after_date") or "") >= today}


def _asset_type(row: dict) -> str:
    name = str(_first(row, "name", "companyName") or "").lower()
    category = str(_first(row, "category", "assetType") or "").lower()
    leveraged = any(token in name for token in ("2x", "3x", "ultra", "bull", "bear", "short", "inverse"))
    if leveraged and ("etf" in category or "etf" in name or "shares" in name or "fund" in name):
        return "leveraged"
    if "etf" in category or " etf" in name or " etn" in name or "fund" in name:
        return "etf"
    if "stock" in category or "equity" in category:
        return "stock"
    return "other"


def _explicit_single_stock_parent(name: object) -> str | None:
    text = str(name or "").upper()
    for pattern in (
        r"\bDAILY\s+([A-Z]{1,6})\s+(?:BULL|BEAR)\b",
        r"\b(?:LONG|SHORT)\s+([A-Z]{1,6})\s+DAILY\b",
        r"\b(?:LONG|SHORT)\s+([A-Z]{1,6})\s+(?:TARGET|ETF)\b",
        r"\bSINGLE[- ]STOCK\s+([A-Z]{1,6})\b",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _scanner_relationship_map(unique: dict[str, dict]) -> dict[str, set[str]]:
    """Infer parent/leveraged relationships from the complete Scanner snapshot."""
    mapping: dict[str, set[str]] = defaultdict(set)
    symbols = set(unique)
    for parent, products in _PARENT_PRODUCTS.items():
        if parent not in symbols:
            continue
        for product in products:
            if product in symbols:
                mapping[parent].add(product); mapping[product].add(parent)
    for product, parent in _SINGLE_STOCK_PARENT.items():
        if product in symbols and parent in symbols:
            mapping[parent].add(product); mapping[product].add(parent)

    stocks = {ticker for ticker, row in unique.items() if _asset_type(row) == "stock"}
    for product, row in unique.items():
        if _asset_type(row) != "leveraged":
            continue
        name = _first(row, "name", "companyName")
        if mapping.get(product):
            continue
        underlying = _explicit_single_stock_parent(name)
        if underlying and underlying in stocks and underlying != product:
            mapping[product].add(underlying); mapping[underlying].add(product)
    return mapping



def _first(row: dict, *names: str) -> Any:
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return row.get(name)
    return None


def _number(row: dict, *names: str) -> float | None:
    return to_float(_first(row, *names))


def _signal(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in _EMPTY_SIGNALS


def _wall_distance(row: dict) -> float | None:
    values = [
        _number(row, "putWallPct", "putWallPercent", "putWallDistancePct", "put_wall_pct"),
        _number(row, "callWallPct", "callWallPercent", "callWallDistancePct", "call_wall_pct"),
    ]
    values = [abs(x) for x in values if x is not None]
    return min(values) if values else None


def _row_metrics(row: dict) -> dict[str, Any]:
    option_signal = _first(row, "optionSignal", "option_signal", "signal")
    flow_signal = _first(row, "unusualFlowSignal", "unusual_flow_signal", "flowSignal")
    gex_change = _number(row, "gexChange", "gexChangePct", "gex_change", "totalGexChangePct")
    rvol = _number(row, "rvol", "relativeVolume", "relative_volume")
    long_score = _number(row, "longScore", "long_score")
    momentum = _number(row, "momentumScore", "momentum_score")
    call_z = _number(row, "otmCallVolumeZ", "otm_call_volume_z")
    put_z = _number(row, "otmPutVolumeZ", "otm_put_volume_z")
    wall_distance = _wall_distance(row)
    return {
        "option_signal": option_signal,
        "flow_signal": flow_signal,
        "has_signal": _signal(option_signal) or _signal(flow_signal),
        "gex_change": gex_change,
        "abs_gex_change": abs(gex_change) if gex_change is not None else -1.0,
        "rvol": rvol,
        "long_score": long_score,
        "momentum": momentum,
        "volume_z": max([abs(x) for x in (call_z, put_z) if x is not None], default=None),
        "wall_distance": wall_distance,
    }



# Fields used to decide whether an expensive detail/GEX refresh adds information.
# The Scanner itself is still read in full on every run. Numeric tolerances avoid
# treating harmless float jitter as a meaningful market change.
_DIFF_FIELDS: dict[str, dict[str, Any]] = {
    "current_price": {"aliases": ("current_price", "currentPrice"), "kind": "relative", "threshold": 0.0015, "weight": 1.0},
    "change_percent": {"aliases": ("change_percent", "changePercent"), "kind": "absolute", "threshold": 0.25, "weight": 1.2},
    "option_signal": {"aliases": ("option_signal", "optionSignal", "signal"), "kind": "text", "weight": 5.0},
    "unusual_flow_signal": {"aliases": ("unusual_flow_signal", "unusualFlowSignal", "flowSignal"), "kind": "text", "weight": 5.0},
    "trend": {"aliases": ("trend",), "kind": "text", "weight": 3.0},
    "total_gex": {"aliases": ("total_gex", "totalGex"), "kind": "relative", "threshold": 0.05, "weight": 4.0},
    "gex_change": {"aliases": ("gex_change", "gexChange", "gexChangePct"), "kind": "absolute", "threshold": 3.0, "weight": 4.0},
    "momentum_score": {"aliases": ("momentum_score", "momentumScore"), "kind": "absolute", "threshold": 5.0, "weight": 2.0},
    "rsi14": {"aliases": ("rsi14", "rsi", "rsi14Value"), "kind": "absolute", "threshold": 3.0, "weight": 1.2},
    "rvol": {"aliases": ("rvol", "relativeVolume", "relative_volume"), "kind": "absolute", "threshold": 0.15, "weight": 2.0},
    "long_score": {"aliases": ("long_score", "longScore"), "kind": "absolute", "threshold": 5.0, "weight": 2.0},
    "iv_z_spread": {"aliases": ("iv_z_spread", "ivZSpread"), "kind": "absolute", "threshold": 0.25, "weight": 1.5},
    "put_wall": {"aliases": ("put_wall", "putWall", "putWallStrike"), "kind": "relative", "threshold": 0.001, "weight": 2.0},
    "call_wall": {"aliases": ("call_wall", "callWall", "callWallStrike"), "kind": "relative", "threshold": 0.001, "weight": 2.0},
    "put_wall_pct": {"aliases": ("put_wall_pct", "putWallPct", "putWallPercent", "putWallDistancePct"), "kind": "absolute", "threshold": 0.25, "weight": 1.5},
    "call_wall_pct": {"aliases": ("call_wall_pct", "callWallPct", "callWallPercent", "callWallDistancePct"), "kind": "absolute", "threshold": 0.25, "weight": 1.5},
}


def _canonical_scanner_values(row: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, spec in _DIFF_FIELDS.items():
        value = _first(row, *spec["aliases"])
        if spec["kind"] == "text":
            out[field] = str(value or "").strip().lower()
        else:
            out[field] = to_float(value)
    return out


def compute_scanner_changes(
    rows: Iterable[dict],
    previous_by_ticker: dict[str, dict] | None,
    *,
    previous_run_id: int | None = None,
) -> tuple[dict[str, dict], list[dict], dict]:
    """Compare the complete current Scanner with the previous successful run.

    This is deliberately independent from the expensive enrichment cap. Every
    current ticker is compared first; the resulting change score then drives the
    detail/GEX priority universe. On the first observed run there is no false
    "all changed" event: it is classified as an initial checkpoint.
    """
    previous_by_ticker = {
        str(ticker).upper(): dict(row)
        for ticker, row in (previous_by_ticker or {}).items()
        if str(ticker or "").strip()
    }
    current: dict[str, dict] = {}
    raw_current: dict[str, dict] = {}
    for row in rows:
        ticker = ticker_from_row(row)
        if not ticker:
            continue
        ticker = str(ticker).upper()
        raw_current.setdefault(ticker, row)
        current.setdefault(ticker, _canonical_scanner_values(row))

    baseline = not bool(previous_by_ticker)
    changes: dict[str, dict] = {}
    report: list[dict] = []
    changed_count = new_count = unchanged_count = 0

    for ticker in sorted(current):
        values = current[ticker]
        previous_raw = previous_by_ticker.get(ticker)
        if baseline:
            item = {
                "ticker": ticker,
                "change_kind": "initial_checkpoint",
                "meaningful_change": False,
                "change_score": 0.0,
                "changed_fields": [],
                "previous_run_id": int(previous_run_id or 0),
            }
            unchanged_count += 1
        elif previous_raw is None:
            item = {
                "ticker": ticker,
                "change_kind": "new_ticker",
                "meaningful_change": True,
                "change_score": 100.0,
                "changed_fields": ["new_ticker"],
                "previous_run_id": int(previous_run_id or 0),
            }
            new_count += 1
        else:
            previous = _canonical_scanner_values(previous_raw)
            changed_fields: list[str] = []
            score = 0.0
            deltas: dict[str, Any] = {}
            for field, spec in _DIFF_FIELDS.items():
                before = previous.get(field)
                after = values.get(field)
                if spec["kind"] == "text":
                    changed = before != after
                    magnitude = 1.0 if changed else 0.0
                elif before is None and after is None:
                    changed = False; magnitude = 0.0
                elif before is None or after is None:
                    changed = True; magnitude = 1.0
                elif spec["kind"] == "relative":
                    denom = max(abs(float(before)), 1e-9)
                    relative = abs(float(after) - float(before)) / denom
                    sign_flip = field == "total_gex" and float(before) * float(after) < 0
                    changed = bool(sign_flip or relative >= float(spec["threshold"]))
                    magnitude = relative / max(float(spec["threshold"]), 1e-9)
                    deltas[field] = round(relative * 100.0, 6)
                else:
                    absolute = abs(float(after) - float(before))
                    changed = absolute >= float(spec["threshold"])
                    magnitude = absolute / max(float(spec["threshold"]), 1e-9)
                    deltas[field] = round(float(after) - float(before), 6)
                if changed:
                    changed_fields.append(field)
                    score += float(spec.get("weight") or 1.0) * min(max(magnitude, 1.0), 8.0)
            meaningful = bool(changed_fields)
            item = {
                "ticker": ticker,
                "change_kind": "changed" if meaningful else "unchanged",
                "meaningful_change": meaningful,
                "change_score": round(score, 6),
                "changed_fields": changed_fields,
                "field_deltas": deltas,
                "previous_run_id": int(previous_run_id or 0),
            }
            if meaningful:
                changed_count += 1
            else:
                unchanged_count += 1
        changes[ticker] = item
        report.append(item)

    removed = sorted(set(previous_by_ticker) - set(current)) if not baseline else []
    report.sort(
        key=lambda item: (
            bool(item.get("meaningful_change")),
            float(item.get("change_score") or 0.0),
            item.get("ticker") or "",
        ),
        reverse=True,
    )
    summary = {
        "previous_run_id": int(previous_run_id or 0),
        "baseline_checkpoint": baseline,
        "current_tickers": len(current),
        "previous_tickers": len(previous_by_ticker),
        "changed_tickers": changed_count,
        "new_tickers": new_count,
        "unchanged_tickers": unchanged_count,
        "removed_tickers": len(removed),
        "removed_ticker_sample": removed[:50],
    }
    return changes, report, summary


def _priority_score(metrics: dict[str, Any], *, mandatory: bool, previous: bool) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if mandatory:
        score += 10000.0
    if metrics["has_signal"]:
        score += 160.0
        reasons.append("scanner_signal")
    gex = metrics["abs_gex_change"]
    if gex >= 0:
        score += min(gex, 100.0) * 1.5
        if gex >= 10:
            reasons.append("gex_change_rank")
    wall = metrics["wall_distance"]
    if wall is not None:
        if wall <= 0.50:
            score += 180.0
            reasons.append("wall_0_5pct")
        elif wall <= 1.00:
            score += 130.0
            reasons.append("wall_1pct")
        elif wall <= 1.50:
            score += 90.0
            reasons.append("wall_1_5pct")
        elif wall <= 2.50:
            score += 45.0
            reasons.append("wall_2_5pct")
    rvol = metrics["rvol"]
    if rvol is not None:
        if rvol >= 2.0:
            score += 120.0
            reasons.append("rvol_2x")
        elif rvol >= 1.5:
            score += 80.0
            reasons.append("rvol_1_5x")
        elif rvol >= 1.2:
            score += 45.0
            reasons.append("rvol_1_2x")
    long_score = metrics["long_score"]
    if long_score is not None and (long_score >= 70 or long_score <= 30):
        score += min(abs(long_score - 50.0), 50.0) * 1.5
        reasons.append("extreme_long_score")
    momentum = metrics["momentum"]
    if momentum is not None and abs(momentum) >= 60:
        score += min(abs(momentum), 100.0) * 0.6
        reasons.append("momentum")
    volume_z = metrics["volume_z"]
    if volume_z is not None:
        if volume_z >= 3:
            score += 100.0
            reasons.append("option_volume_z3")
        elif volume_z >= 2:
            score += 60.0
            reasons.append("option_volume_z2")
    if previous:
        score += 20.0
        reasons.append("continuity")
    return score, reasons


def select_hourly_enrichment(
    rows: Iterable[dict],
    *,
    max_tickers: int,
    always_tickers: set[str] | None = None,
    open_trade_tickers: set[str] | None = None,
    recent_alert_tickers: set[str] | None = None,
    previous_enriched_tickers: set[str] | None = None,
    change_map: dict[str, dict] | None = None,
    top_changed: int | None = None,
    top_gex_change: int = 80,
    top_near_wall: int = 50,
    top_rvol: int = 40,
    top_signals: int = 60,
    continuity_max: int = 40,
) -> tuple[list[tuple[str, dict]], list[dict], dict]:
    """Select a bounded, explainable enrichment universe from the full scanner.

    The full scanner is still persisted. Only detail/intraday/GEX requests are bounded.
    Mandatory tickers (manual list and open/recent alerts) are never dropped by the cap.
    """
    always_tickers = {x.upper() for x in (always_tickers or set())}
    open_trade_tickers = {x.upper() for x in (open_trade_tickers or set())}
    recent_alert_tickers = {x.upper() for x in (recent_alert_tickers or set())}
    previous_enriched_tickers = {x.upper() for x in (previous_enriched_tickers or set())}
    change_map = {str(k).upper(): dict(v) for k, v in (change_map or {}).items()}
    mandatory_set = always_tickers | open_trade_tickers | recent_alert_tickers
    gex_backoff_tickers = _active_gex_negative_cache()

    unique: dict[str, dict] = {}
    for row in rows:
        ticker = ticker_from_row(row)
        if ticker:
            unique.setdefault(str(ticker).upper(), row)

    relationship_map = _scanner_relationship_map(unique)
    # Related products of open/recent positions are mandatory context nodes.
    for parent in list(open_trade_tickers | recent_alert_tickers):
        mandatory_set.update(relationship_map.get(parent, set()))

    records: list[dict] = []
    for ticker, row in unique.items():
        metrics = _row_metrics(row)
        mandatory = ticker in mandatory_set
        previous = ticker in previous_enriched_tickers
        score, reasons = _priority_score(metrics, mandatory=mandatory, previous=previous)
        change = change_map.get(ticker) or {}
        change_kind = str(change.get("change_kind") or "unknown")
        change_score = float(change.get("change_score") or 0.0)
        if change_kind == "new_ticker":
            score += 8000.0 + change_score * 10.0
            reasons.append("new_ticker")
        elif bool(change.get("meaningful_change")):
            score += 4000.0 + change_score * 10.0
            reasons.append("scanner_changed")
        backoff = ticker in gex_backoff_tickers and not mandatory and not metrics["has_signal"] and not bool(change.get("meaningful_change"))
        if backoff:
            score -= 100000.0
            reasons.append("gex_negative_cache_backoff")
        mandatory_reasons: list[str] = []
        if ticker in always_tickers:
            mandatory_reasons.append("always")
        if ticker in open_trade_tickers:
            mandatory_reasons.append("open_trade")
        if ticker in recent_alert_tickers:
            mandatory_reasons.append("recent_alert")
        records.append({
            "ticker": ticker,
            "row": row,
            "score": score,
            "reasons": mandatory_reasons + reasons,
            "mandatory": mandatory,
            "gex_backoff": backoff,
            "change_kind": change_kind,
            "change_score": change_score,
            "changed_fields": list(change.get("changed_fields") or []),
            **metrics,
        })

    selected: set[str] = {r["ticker"] for r in records if r["mandatory"]}
    selected_reason: dict[str, set[str]] = {r["ticker"]: set(r["reasons"]) for r in records if r["mandatory"]}

    def add_pool(pool: list[dict], limit: int, reason: str) -> None:
        if limit <= 0:
            return
        for rec in pool[:limit]:
            selected.add(rec["ticker"])
            selected_reason.setdefault(rec["ticker"], set(rec["reasons"])).add(reason)

    changed_limit = max(0, int(top_changed if top_changed is not None else max_tickers))
    add_pool(
        sorted(
            [r for r in records if not r["gex_backoff"] and r["change_kind"] in {"changed", "new_ticker"}],
            key=lambda r: (r["change_kind"] == "new_ticker", r["change_score"], r["score"]),
            reverse=True,
        ),
        changed_limit,
        "scanner_change_pool",
    )

    add_pool(
        sorted([r for r in records if not r["gex_backoff"] and r["abs_gex_change"] >= 0], key=lambda r: (r["abs_gex_change"], r["score"]), reverse=True),
        top_gex_change,
        "top_gex_change",
    )
    add_pool(
        sorted([r for r in records if not r["gex_backoff"] and r["wall_distance"] is not None], key=lambda r: (r["wall_distance"], -r["score"])),
        top_near_wall,
        "top_near_wall",
    )
    add_pool(
        sorted([r for r in records if not r["gex_backoff"] and r["rvol"] is not None], key=lambda r: (r["rvol"], r["score"]), reverse=True),
        top_rvol,
        "top_rvol",
    )
    add_pool(
        sorted([r for r in records if not r["gex_backoff"] and r["has_signal"]], key=lambda r: r["score"], reverse=True),
        top_signals,
        "scanner_signal_pool",
    )
    add_pool(
        sorted([r for r in records if not r["gex_backoff"] and r["ticker"] in previous_enriched_tickers], key=lambda r: r["score"], reverse=True),
        continuity_max,
        "continuity_pool",
    )

    # Reserve granular context for products structurally related to the strongest
    # current setups. Using every selected pool member as a seed made the graph
    # expansion too broad; only the strongest current candidates plus open/recent
    # positions seed this enrichment. A large but bounded boost reserves room for
    # TSLA↔TSLL/TSLQ, NVDA↔NVDL, QQQ↔TQQQ/SQQQ, etc.
    provisional_ranked = sorted(
        [rec for rec in records if rec["ticker"] in selected],
        key=lambda rec: (rec["mandatory"], rec["score"], rec["abs_gex_change"]),
        reverse=True,
    )
    seed_limit = max(10, min(40, max(1, int(max_tickers or 0)) // 4))
    seed_tickers = {rec["ticker"] for rec in provisional_ranked[:seed_limit]} | open_trade_tickers | recent_alert_tickers
    related_nodes: set[str] = set()
    for ticker in seed_tickers:
        related_nodes.update(relationship_map.get(ticker, set()))
    for rec in records:
        if rec["ticker"] in related_nodes and not rec["gex_backoff"]:
            rec["score"] += 2500.0
            rec["reasons"].append("related_graph_node")
            selected.add(rec["ticker"])
            selected_reason.setdefault(rec["ticker"], set(rec["reasons"])).add("related_graph_node")

    # Keep mandatory tickers even if they exceed the configured cap.
    cap = max(int(max_tickers or 0), len(mandatory_set & set(unique)))
    ranked = sorted(records, key=lambda r: (r["mandatory"], r["score"], r["abs_gex_change"]), reverse=True)
    final: list[dict] = []
    for rec in ranked:
        if rec["ticker"] in selected and len(final) < cap:
            final.append(rec)
    if len(final) < cap:
        present = {r["ticker"] for r in final}
        for rec in ranked:
            if rec["ticker"] in present or rec["gex_backoff"]:
                continue
            selected_reason.setdefault(rec["ticker"], set(rec["reasons"])).add("priority_fill")
            final.append(rec)
            present.add(rec["ticker"])
            if len(final) >= cap:
                break

    # Mandatory tickers cannot be lost due to pool ordering.
    final_tickers = {r["ticker"] for r in final}
    for rec in ranked:
        if rec["mandatory"] and rec["ticker"] not in final_tickers:
            final.append(rec)
            final_tickers.add(rec["ticker"])

    report: list[dict] = []
    pairs: list[tuple[str, dict]] = []
    reason_counter: Counter[str] = Counter()
    for rank, rec in enumerate(final, start=1):
        reasons = sorted(selected_reason.get(rec["ticker"], set(rec["reasons"]))) or ["priority_fill"]
        reason_counter.update(reasons)
        pairs.append((rec["ticker"], rec["row"]))
        report.append({
            "rank": rank,
            "ticker": rec["ticker"],
            "priority_score": round(float(rec["score"]), 4),
            "selection_reasons": ",".join(reasons),
            "scanner_change_kind": rec["change_kind"],
            "scanner_change_score": round(float(rec["change_score"]), 6),
            "scanner_changed_fields": ",".join(rec["changed_fields"]),
            "scanner_gex_change": rec["gex_change"],
            "nearest_wall_pct": rec["wall_distance"],
            "rvol": rec["rvol"],
            "option_signal": rec["option_signal"],
            "unusual_flow_signal": rec["flow_signal"],
            "long_score": rec["long_score"],
            "momentum_score": rec["momentum"],
            "option_volume_z": rec["volume_z"],
        })
    summary = {
        "scanner_unique_tickers": len(unique),
        "selected_tickers": len(pairs),
        "configured_cap": int(max_tickers or 0),
        "mandatory_tickers": len(mandatory_set & set(unique)),
        "open_trade_tickers": len(open_trade_tickers & set(unique)),
        "recent_alert_tickers": len(recent_alert_tickers & set(unique)),
        "related_graph_nodes": len(related_nodes & set(unique)),
        "always_tickers": len(always_tickers & set(unique)),
        "previous_enriched_candidates": len(previous_enriched_tickers & set(unique)),
        "changed_candidates": sum(1 for r in records if r["change_kind"] == "changed"),
        "new_ticker_candidates": sum(1 for r in records if r["change_kind"] == "new_ticker"),
        "selected_changed": sum(1 for r in final if r["change_kind"] in {"changed", "new_ticker"}),
        "gex_negative_cache_active": len(gex_backoff_tickers & set(unique)),
        "gex_negative_cache_skipped": sum(1 for r in records if r["gex_backoff"]),
        "reason_counts": dict(reason_counter.most_common()),
    }
    return pairs, report, summary
