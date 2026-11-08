from __future__ import annotations

"""Unified economic evidence layer for TMAD.

Research/SHADOW only. It combines reverse-hindsight opportunities with the
forward TMAD path and evaluates EOD selections at 1/2/3/5 sessions. Future data
is used only for mature outcome labels, never to reconstruct point-in-time
features or to mutate LIVE behavior.
"""

from collections import Counter, defaultdict
from statistics import mean, median
from typing import Any

from return_math import directional_return_pct


HORIZONS = (1, 2, 3, 5)


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        x = float(value)
        return x if x == x else default
    except Exception:
        return default


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return round(gains / losses, 4)


def _future_close(series: dict[str, list[dict]], ticker: str, signal_date: str, horizon: int) -> float | None:
    future = [r for r in series.get(ticker, []) if str(r.get("trade_date") or "") > signal_date]
    if len(future) < horizon:
        return None
    return _f(future[horizon - 1].get("close"))


def normalize_eod_selection_rows(rows: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for raw in rows or []:
        selected = raw.get("selected")
        if isinstance(selected, list):
            asof = str(raw.get("asof") or raw.get("market_date") or raw.get("date") or "")
            for item in selected:
                if not isinstance(item, dict):
                    continue
                ticker = str(item.get("ticker") or "").upper()
                direction = str(item.get("direction") or "").lower()
                if asof and ticker and direction in {"long", "short"}:
                    out.append({**item, "asof": asof, "ticker": ticker, "direction": direction})
            continue
        asof = str(raw.get("asof") or raw.get("market_date") or raw.get("date") or "")
        ticker = str(raw.get("ticker") or "").upper()
        direction = str(raw.get("direction") or "").lower()
        if asof and ticker and direction in {"long", "short"}:
            out.append({**raw, "asof": asof, "ticker": ticker, "direction": direction})

    dedup: dict[tuple[str, str, str], dict] = {}
    for row in out:
        key = (row["asof"], row["ticker"], row["direction"])
        old = dedup.get(key)
        score = _f(row.get("underlying_alpha_score"), _f(row.get("conviction"), _f(row.get("score"), 0.0))) or 0.0
        old_score = _f((old or {}).get("underlying_alpha_score"), _f((old or {}).get("conviction"), _f((old or {}).get("score"), -1.0))) if old else -1.0
        if old is None or score >= float(old_score or -1.0):
            dedup[key] = row
    return list(dedup.values())


def evaluate_eod_alpha(
    canonical: dict[tuple[str, str], dict],
    series: dict[str, list[dict]],
    eod_selection_rows: list[dict] | None,
) -> dict:
    selections = normalize_eod_selection_rows(eod_selection_rows)
    by_horizon: dict[str, dict] = {}
    detail: list[dict] = []

    for horizon in HORIZONS:
        returns: list[float] = []
        rows: list[dict] = []
        for sel in selections:
            date, ticker, direction = sel["asof"], sel["ticker"], sel["direction"]
            point = canonical.get((date, ticker))
            if not point:
                continue
            entry = _f(point.get("outcome_entry_price"), _f(point.get("price")))
            close = _future_close(series, ticker, date, horizon)
            if not entry or not close or entry <= 0:
                continue
            ret = float(directional_return_pct(direction, entry, close) or 0.0)
            returns.append(ret)
            row = {
                "date": date,
                "ticker": ticker,
                "direction": direction,
                "horizon_sessions": horizon,
                "entry_price": round(entry, 6),
                "future_close": round(close, 6),
                "realized_return_pct": round(ret, 4),
                "winner": ret > 0,
                "alpha_score": _f(sel.get("underlying_alpha_score"), _f(sel.get("conviction"), _f(sel.get("score")))),
                "tradeability_score": _f(sel.get("tradeability_score")),
                "opportunity_state": sel.get("opportunity_state"),
            }
            rows.append(row)
            detail.append(row)

        by_horizon[str(horizon)] = {
            "sample": len(returns),
            "win_rate_pct": round(100.0 * sum(x > 0 for x in returns) / len(returns), 2) if returns else None,
            "avg_return_pct": round(mean(returns), 4) if returns else None,
            "median_return_pct": round(median(returns), 4) if returns else None,
            "profit_factor": _profit_factor(returns) if returns else None,
        }

    return {
        "selection_count": len(selections),
        "by_horizon": by_horizon,
        "details": detail,
    }


def build_opportunity_ledger(reverse_alpha: dict, audit_events: list[dict], eod_eval: dict) -> list[dict]:
    audit_index: dict[tuple[str, str, str], dict] = {}
    for event in audit_events or []:
        key = (
            str(event.get("signal_date") or ""),
            str(event.get("ticker") or "").upper(),
            str(event.get("direction") or "").lower(),
        )
        old = audit_index.get(key)
        if old is None or float(event.get("move_pct") or 0.0) > float(old.get("move_pct") or 0.0):
            audit_index[key] = event

    eod_index: dict[tuple[str, str, str, int], dict] = {}
    for row in (eod_eval or {}).get("details") or []:
        eod_index[(row["date"], row["ticker"], row["direction"], int(row["horizon_sessions"]))] = row

    ledger: list[dict] = []
    seen: set[tuple[str, int, str, str]] = set()
    for item in reverse_alpha.get("top_opportunities") or []:
        date = str(item.get("date") or "")
        horizon = int(item.get("horizon_sessions") or 0)
        ticker = str(item.get("ticker") or "").upper()
        direction = str(item.get("direction") or "").lower()
        key = (date, horizon, ticker, direction)
        if not date or not ticker or horizon not in HORIZONS or key in seen:
            continue
        seen.add(key)
        audit = audit_index.get((date, ticker, direction)) or {}
        eod = eod_index.get(key) or {}
        ledger.append({
            "date": date,
            "ticker": ticker,
            "direction": direction,
            "horizon_sessions": horizon,
            "hindsight_rank": item.get("hindsight_rank"),
            "hindsight_return_pct": item.get("realized_return_pct"),
            "alerted": bool(item.get("alerted")),
            "selected_in_eod_alpha": bool(item.get("selected_in_eod_alpha")),
            "eod_realized_return_pct": eod.get("realized_return_pct"),
            "tmad_path": item.get("tmad_path") or {
                "core": bool(audit.get("core_detected")),
                "research": bool(audit.get("research_reached")),
                "gex": bool(audit.get("gex_confirmed")),
                "timing": bool(audit.get("timing_ready")),
                "fusion": bool(audit.get("fusion_ready")),
            },
            "miss_reason": item.get("miss_reason") or audit.get("classification"),
            "core_rank": (item.get("ranks") or {}).get("core", audit.get("core_rank")),
            "momentum_rank": (item.get("ranks") or {}).get("momentum", audit.get("momentum_rank")),
            "atr_rank": (item.get("ranks") or {}).get("atr", audit.get("atr_rank")),
            "flow_rank": (item.get("ranks") or {}).get("flow", audit.get("flow_rank")),
            "core_score": ((item.get("core_snapshot") or {}).get("score", audit.get("core_score"))),
            "core_probability_pct": ((item.get("core_snapshot") or {}).get("probability_pct", audit.get("core_probability_pct"))),
            "point_in_time_features": item.get("point_in_time_features") or {},
            "source_scanner_run_id": item.get("source_scanner_run_id"),
            "research_only": True,
        })
    return sorted(ledger, key=lambda x: (x["date"], x["horizon_sessions"], x.get("hindsight_rank") or 9999))


def build_daily_alpha_scorecard(reverse_alpha: dict, eod_eval: dict, ledger: list[dict]) -> dict:
    reverse_daily = reverse_alpha.get("daily_summary") or []
    daily: dict[tuple[str, int], dict] = {}
    for row in reverse_daily:
        key = (str(row.get("date") or ""), int(row.get("horizon_sessions") or 0))
        if key[0] and key[1] in HORIZONS:
            daily[key] = {
                "date": key[0],
                "horizon_sessions": key[1],
                "hindsight_top_n": row.get("top_n"),
                "hindsight_avg_top_return_pct": row.get("avg_top_return_pct"),
                "alert_recall_pct": row.get("alert_recall_pct"),
                "eod_alpha_recall_pct": row.get("eod_alpha_recall_pct"),
            }

    eod_details = (eod_eval or {}).get("details") or []
    grouped_eod: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in eod_details:
        grouped_eod[(row["date"], int(row["horizon_sessions"]))].append(row)

    ledger_group: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in ledger:
        ledger_group[(row["date"], int(row["horizon_sessions"]))].append(row)

    for key in set(daily) | set(grouped_eod) | set(ledger_group):
        rec = daily.setdefault(key, {"date": key[0], "horizon_sessions": key[1]})
        selections = grouped_eod.get(key, [])
        returns = [float(x["realized_return_pct"]) for x in selections]
        misses = Counter(str(x.get("miss_reason") or "UNKNOWN") for x in ledger_group.get(key, []) if not x.get("alerted"))
        rec.update({
            "eod_selected_mature": len(selections),
            "eod_win_rate_pct": round(100.0 * sum(x > 0 for x in returns) / len(returns), 2) if returns else None,
            "eod_avg_return_pct": round(mean(returns), 4) if returns else None,
            "eod_median_return_pct": round(median(returns), 4) if returns else None,
            "eod_profit_factor": _profit_factor(returns) if returns else None,
            "largest_alpha_leak": misses.most_common(1)[0][0] if misses else None,
            "miss_reasons": dict(misses),
        })

    ordered = [daily[k] for k in sorted(daily)]
    mature_returns = [float(x["realized_return_pct"]) for x in eod_details]
    all_misses = Counter(str(x.get("miss_reason") or "UNKNOWN") for x in ledger if not x.get("alerted"))

    return {
        "schema_version": 1,
        "mode": "ECONOMIC_EVIDENCE_SCORECARD_SHADOW",
        "days": len({x["date"] for x in ordered}),
        "ledger_rows": len(ledger),
        "eod_mature_selections": len(eod_details),
        "eod_overall_win_rate_pct": round(100.0 * sum(x > 0 for x in mature_returns) / len(mature_returns), 2) if mature_returns else None,
        "eod_overall_avg_return_pct": round(mean(mature_returns), 4) if mature_returns else None,
        "eod_overall_profit_factor": _profit_factor(mature_returns) if mature_returns else None,
        "largest_alpha_leak": all_misses.most_common(1)[0][0] if all_misses else None,
        "miss_reasons": dict(all_misses),
        "by_day_horizon": ordered[-80:],
        "policy": {
            "research_only": True,
            "live_mutation": False,
            "no_lookahead_features": True,
            "promotion_requires_prospective_validation": True,
        },
    }
