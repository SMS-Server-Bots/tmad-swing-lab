from __future__ import annotations

"""Daily perfect-hindsight reverse audit for TMAD.

Research-only module. It starts from what actually happened in the following
1/2/3/5 sessions, identifies the strongest directional opportunities per day,
and then inspects only point-in-time TMAD information that existed on the
signal date. It must never mutate LIVE strategy or execution.
"""

from collections import Counter, defaultdict
from statistics import mean
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


def _feature_payload(row: dict) -> dict:
    value = row.get("payload_json")
    if isinstance(value, dict):
        payload = value
    else:
        import json
        try:
            payload = json.loads(value or "{}")
        except Exception:
            payload = {}
    features = payload.get("features") if isinstance(payload, dict) else None
    return features if isinstance(features, dict) else (payload if isinstance(payload, dict) else {})


def _future_close(series: dict[str, list[dict]], ticker: str, signal_date: str, horizon: int) -> float | None:
    rows = sorted(series.get(ticker, []), key=lambda row: str(row.get("trade_date") or ""))
    has_segments = any("reverse_segment" in row for row in rows)
    if has_segments:
        anchor = None
        for row in rows:
            day = str(row.get("trade_date") or "")[:10]
            if day <= signal_date:
                anchor = row
            else:
                break
        if anchor is None:
            return None
        segment = anchor.get("reverse_segment")
        future = [
            row for row in rows
            if str(row.get("trade_date") or "")[:10] > signal_date
            and row.get("reverse_segment") == segment
        ]
    else:
        future = [
            row for row in rows
            if str(row.get("trade_date") or "")[:10] > signal_date
        ]
    if len(future) < horizon:
        return None
    return _f(future[horizon - 1].get("close"))


def _eod_keys(rows: list[dict] | None) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for row in rows or []:
        date = str(row.get("asof") or row.get("signal_date") or "")
        ticker = str(row.get("ticker") or "").upper()
        direction = str(row.get("direction") or "").lower()
        if date and ticker and direction in {"long", "short"}:
            out.add((date, ticker, direction))
    return out


def _stage_path(signals: list[dict], direction: str) -> dict:
    rows = [r for r in signals if str(r.get("direction") or "").lower() == direction]
    layers = defaultdict(list)
    for row in rows:
        layers[str(row.get("layer") or "").upper()].append(row)

    def any_state(layer: str, states: set[str]) -> bool:
        return any(str(r.get("state") or r.get("opportunity_state") or "").upper() in states for r in layers.get(layer, []))

    core = bool(layers.get("CORE"))
    research = any_state("CORE", {"DISCOVERED", "EARLY", "CAPPED", "READY", "RETEST", "ENTER"})
    gex = bool(layers.get("GEX_DAILY"))
    timing = any_state("GEX_INTRADAY", {"READY", "RETEST", "ENTER"})
    fusion = any(
        bool(r.get("actionable")) or str(r.get("state") or r.get("opportunity_state") or "").upper() in {"READY", "RETEST", "ENTER"}
        for r in layers.get("FUSION", [])
    )
    return {
        "core": core,
        "research": research,
        "gex": gex,
        "timing": timing,
        "fusion": fusion,
    }


def _miss_reason(path: dict, alerted: bool, opposite_core: bool) -> str:
    if alerted:
        return "ALERTED"
    if not path["core"] and opposite_core:
        return "WRONG_DIRECTION"
    if not path["core"]:
        return "NO_CORE"
    if not path["research"]:
        return "RESEARCH_BLOCK"
    if not path["gex"]:
        return "GEX_BLOCK"
    if not path["timing"]:
        return "TIMING_NOT_READY"
    if not path["fusion"]:
        return "FUSION_BLOCK"
    return "ENTER_NOT_EMITTED"


def build_daily_reverse_alpha(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    ranks: dict,
    universe_sizes: dict[str, int],
    alerts: set[tuple[str, str, str]],
    series: dict[str, list[dict]],
    *,
    eod_selection_rows: list[dict] | None = None,
    top_n: int = 5,
    detail_days: int = 20,
    universe_metadata: dict[str, dict] | None = None,
) -> dict:
    """Build a no-lookahead reverse audit from mature future returns.

    Future prices are used only to define the hindsight label. All explanatory
    features, ranks and pipeline states come from the signal date.
    """
    eod = _eod_keys(eod_selection_rows)
    universe_metadata = universe_metadata or {}
    by_date_horizon: dict[tuple[str, int], list[dict]] = defaultdict(list)

    for (date, ticker), row in canonical.items():
        entry = _f(row.get("outcome_entry_price"), _f(row.get("price")))
        if not entry or entry <= 0:
            continue
        features = _feature_payload(row)
        opposite_dir = str(row.get("core_direction") or "").lower()

        for horizon in HORIZONS:
            close = _future_close(series, ticker, date, horizon)
            if not close or close <= 0:
                continue
            long_ret = float(directional_return_pct("LONG", entry, close) or 0.0)
            short_ret = float(directional_return_pct("SHORT", entry, close) or 0.0)
            direction, realized = ("long", long_ret) if long_ret >= short_ret else ("short", short_ret)
            sigs = signal_index.get((date, ticker, direction), [])
            path = _stage_path(sigs, direction)
            alerted = (date, ticker, direction) in alerts
            opposite_core = opposite_dir in {"long", "short"} and opposite_dir != direction

            by_date_horizon[(date, horizon)].append({
                "date": date,
                "horizon_sessions": horizon,
                "ticker": ticker,
                "direction": direction,
                "realized_return_pct": round(realized, 4),
                "entry_price": round(entry, 6),
                "future_close": round(close, 6),
                "source_scanner_run_id": row.get("source_scanner_run_id"),
                "observation_layer": row.get("observation_layer"),
                "universe_size": universe_sizes.get(date),
                "alerted": alerted,
                "selected_in_eod_alpha": (date, ticker, direction) in eod,
                "tmad_path": path,
                "miss_reason": _miss_reason(path, alerted, opposite_core),
                "ranks": {
                    "core": ranks.get((date, direction, ticker, "core")),
                    "momentum": ranks.get((date, direction, ticker, "momentum")),
                    "atr": ranks.get((date, direction, ticker, "atr")),
                    "flow": ranks.get((date, direction, ticker, "flow")),
                },
                "point_in_time_features": {
                    key: features.get(key) for key in (
                        "momentum_score", "rvol", "change_percent",
                        "otm_call_volume_z", "otm_put_volume_z",
                        "relative_strength_vs_market", "relative_strength_vs_sector",
                        "market_breadth_score", "sector_breadth_score",
                        "relationship_lead_lag_score", "total_gex_change_pct",
                    )
                },
                "core_snapshot": {
                    "direction": row.get("core_direction"),
                    "score": row.get("core_score"),
                    "probability_pct": row.get("core_probability_pct"),
                },
                "gex_snapshot": {
                    "direction": row.get("gex_direction"),
                    "intent": row.get("gex_intent"),
                },
            })

    details: list[dict] = []
    daily_summaries: list[dict] = []
    for (date, horizon), rows in sorted(by_date_horizon.items()):
        ranked = sorted(rows, key=lambda x: float(x["realized_return_pct"]), reverse=True)
        winners = ranked[: max(1, int(top_n))]
        meta = dict(universe_metadata.get(date) or {})
        expected = int(meta.get("expected_universe") or 0)
        available = int(meta.get("available_universe") or len(ranked))
        source_scope = str(meta.get("universe_scope") or "OBSERVED_FEATURE_SUBSET")
        coverage_pct = round(100.0 * available / expected, 3) if expected else None
        ranked_coverage_pct = round(100.0 * len(ranked) / expected, 3) if expected else None
        scope = source_scope
        if scope == "FULL_SCANNER_DEEP" and expected and available < expected:
            scope = "OBSERVED_FEATURE_SUBSET"
        if scope == "FULL_SCANNER_DEEP" and expected and len(ranked) < expected:
            scope = "FULL_SCANNER_DEEP_PARTIAL_OUTCOME_COVERAGE"
        for rank, item in enumerate(winners, 1):
            item["hindsight_rank"] = rank
            item["universe_scope"] = scope
            details.append(item)
        daily_summaries.append({
            "date": date,
            "horizon_sessions": horizon,
            "universe_scope": scope,
            "scanner_expected_universe": expected or None,
            "scanner_available_universe": available,
            "coverage_pct": coverage_pct,
            "mature_ranked_universe": len(ranked),
            "mature_ranked_coverage_pct": ranked_coverage_pct,
            "top_n_requested": int(top_n),
            "top_n_effective": len(winners),
            "evaluated_universe": len(ranked),
            "top_n": len(winners),
            "avg_top_return_pct": round(mean(float(x["realized_return_pct"]) for x in winners), 4) if winners else None,
            "alert_recall_pct": round(100.0 * sum(bool(x["alerted"]) for x in winners) / len(winners), 2) if winners else None,
            "eod_alpha_recall_pct": round(100.0 * sum(bool(x["selected_in_eod_alpha"]) for x in winners) / len(winners), 2) if winners else None,
        })

    dates = sorted({x["date"] for x in details})
    keep_dates = set(dates[-max(1, int(detail_days)):])
    details = [x for x in details if x["date"] in keep_dates]

    misses = Counter(x["miss_reason"] for x in details if not x["alerted"])
    total = len(details)
    alerted = sum(bool(x["alerted"]) for x in details)
    eod_hit = sum(bool(x["selected_in_eod_alpha"]) for x in details)

    by_horizon = {}
    for horizon in HORIZONS:
        subset = [x for x in details if x["horizon_sessions"] == horizon]
        by_horizon[str(horizon)] = {
            "sample": len(subset),
            "alert_recall_pct": round(100.0 * sum(bool(x["alerted"]) for x in subset) / len(subset), 2) if subset else None,
            "eod_alpha_recall_pct": round(100.0 * sum(bool(x["selected_in_eod_alpha"]) for x in subset) / len(subset), 2) if subset else None,
            "avg_hindsight_return_pct": round(mean(float(x["realized_return_pct"]) for x in subset), 4) if subset else None,
        }

    feature_groups: dict[str, list[float]] = defaultdict(list)
    for item in details:
        if item["alerted"]:
            continue
        for key, value in item["point_in_time_features"].items():
            number = _f(value)
            if number is not None:
                feature_groups[key].append(number)

    missed_feature_profile = {
        key: {"sample": len(vals), "avg": round(mean(vals), 4)}
        for key, vals in sorted(feature_groups.items())
        if vals
    }

    scopes = {str(row.get("universe_scope") or "OBSERVED_FEATURE_SUBSET") for row in daily_summaries}
    if scopes == {"FULL_SCANNER_DEEP"}:
        overall_scope = "FULL_SCANNER_DEEP"
    elif scopes == {"OBSERVED_FEATURE_SUBSET"}:
        overall_scope = "OBSERVED_FEATURE_SUBSET"
    elif not scopes:
        overall_scope = "NO_MATURE_UNIVERSE"
    elif scopes <= {"FULL_SCANNER_DEEP", "FULL_SCANNER_DEEP_PARTIAL_OUTCOME_COVERAGE"}:
        overall_scope = "FULL_SCANNER_DEEP_PARTIAL_OUTCOME_COVERAGE"
    else:
        overall_scope = "MIXED_FULL_DEEP_AND_OBSERVED_SUBSET"

    coverage_values = [
        float(row["coverage_pct"]) for row in daily_summaries
        if row.get("coverage_pct") is not None
    ]
    coverage_summary = {
        "summaries": len(daily_summaries),
        "full_deep_summaries": sum(row.get("universe_scope") == "FULL_SCANNER_DEEP" for row in daily_summaries),
        "partial_outcome_summaries": sum(row.get("universe_scope") == "FULL_SCANNER_DEEP_PARTIAL_OUTCOME_COVERAGE" for row in daily_summaries),
        "subset_summaries": sum(row.get("universe_scope") == "OBSERVED_FEATURE_SUBSET" for row in daily_summaries),
        "min_source_coverage_pct": round(min(coverage_values), 3) if coverage_values else None,
        "max_source_coverage_pct": round(max(coverage_values), 3) if coverage_values else None,
        "min_top_n_effective": min((int(row.get("top_n_effective") or 0) for row in daily_summaries), default=0),
        "max_top_n_effective": max((int(row.get("top_n_effective") or 0) for row in daily_summaries), default=0),
    }

    return {
        "schema_version": 2,
        "mode": "DAILY_REVERSE_ALPHA_SHADOW",
        "universe_scope": overall_scope,
        "strategic_conclusion_allowed": overall_scope == "FULL_SCANNER_DEEP",
        "universe_coverage": coverage_summary,
        "horizons": list(HORIZONS),
        "top_n_per_day": int(top_n),
        "detail_days": int(detail_days),
        "evaluated_top_opportunities": total,
        "alerted_top_opportunities": alerted,
        "alert_recall_pct": round(100.0 * alerted / total, 2) if total else None,
        "eod_alpha_selected_top_opportunities": eod_hit,
        "eod_alpha_recall_pct": round(100.0 * eod_hit / total, 2) if total else None,
        "miss_reasons": dict(misses),
        "by_horizon": by_horizon,
        "missed_feature_profile": missed_feature_profile,
        "daily_summary": daily_summaries[-max(4, detail_days * len(HORIZONS)):],
        "top_opportunities": details,
        "policy": {
            "research_only": True,
            "future_data_used_only_for_labels": True,
            "live_mutation": False,
            "requires_full_scanner_deep_for_strategic_conclusions": True,
            "subset_results_are_non_conclusive_for_full_universe": True,
            "purpose": "discover what the best daily opportunities looked like before they moved",
        },
    }
