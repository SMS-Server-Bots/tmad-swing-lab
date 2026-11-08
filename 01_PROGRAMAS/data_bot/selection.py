from __future__ import annotations

"""Adaptive Scanner priority overlay for TMAD v6.1.55.

The stable Scanner/SELECTIVE implementation lives in ``selection_base``.  This
module adds cross-sectional materiality and rank-jump evidence without changing
trading thresholds, the 180-name SELECTIVE cap, risk, sizing, stops or entries.
"""

from datetime import datetime, timezone
from typing import Any, Iterable

import selection_base as _base
from selection_base import *  # noqa: F401,F403 - preserve the public contract


def __getattr__(name: str):
    """Preserve compatibility for callers importing private helpers."""
    return getattr(_base, name)


def _metric_vector(row: dict) -> dict[str, float | None]:
    metrics = _base._row_metrics(row)
    long_score = metrics.get("long_score")
    momentum = metrics.get("momentum")
    rvol = metrics.get("rvol")
    gex = metrics.get("abs_gex_change")
    return {
        "long_score_extreme": abs(float(long_score) - 50.0) if long_score is not None else None,
        "momentum_abs": abs(float(momentum)) if momentum is not None else None,
        "rvol": float(rvol) if rvol is not None else None,
        "gex_change_abs": float(gex) if gex is not None and float(gex) >= 0 else None,
    }


def _rank_percentiles(rows_by_ticker: dict[str, dict]) -> dict[str, dict[str, float]]:
    """Top=100, bottom=0 for each observable metric; no fixed market threshold."""
    vectors = {ticker: _metric_vector(row) for ticker, row in rows_by_ticker.items()}
    out: dict[str, dict[str, float]] = {ticker: {} for ticker in rows_by_ticker}
    for metric in ("long_score_extreme", "momentum_abs", "rvol", "gex_change_abs"):
        ranked = sorted(
            ((ticker, vector.get(metric)) for ticker, vector in vectors.items() if vector.get(metric) is not None),
            key=lambda item: (float(item[1]), item[0]),
            reverse=True,
        )
        n = len(ranked)
        for index, (ticker, _value) in enumerate(ranked):
            pct = 100.0 if n <= 1 else 100.0 * (1.0 - index / (n - 1))
            out[ticker][metric] = round(pct, 6)
    return out


def _direction_flips(current_row: dict, previous_row: dict) -> list[str]:
    current = _base._row_metrics(current_row)
    previous = _base._row_metrics(previous_row)
    flips: list[str] = []
    pairs = (
        ("momentum", current.get("momentum"), previous.get("momentum"), 0.0),
        ("long_score", current.get("long_score"), previous.get("long_score"), 50.0),
        ("gex_change", current.get("gex_change"), previous.get("gex_change"), 0.0),
    )
    for name, after, before, pivot in pairs:
        if after is None or before is None:
            continue
        before_side = float(before) - pivot
        after_side = float(after) - pivot
        if before_side * after_side < 0:
            flips.append(name)
    return flips


def compute_scanner_changes(
    rows: Iterable[dict],
    previous_by_ticker: dict[str, dict] | None,
    *,
    previous_run_id: int | None = None,
) -> tuple[dict[str, dict], list[dict], dict]:
    """Base diff + adaptive cross-sectional materiality/rank-jump evidence."""
    rows_list = list(rows)
    changes, report, summary = _base.compute_scanner_changes(
        rows_list,
        previous_by_ticker,
        previous_run_id=previous_run_id,
    )
    current_rows: dict[str, dict] = {}
    for row in rows_list:
        ticker = _base.ticker_from_row(row)
        if ticker:
            current_rows.setdefault(str(ticker).upper(), row)
    previous_rows = {
        str(ticker).upper(): dict(row)
        for ticker, row in (previous_by_ticker or {}).items()
        if str(ticker or "").strip()
    }
    current_pct = _rank_percentiles(current_rows)
    previous_pct = _rank_percentiles(previous_rows)

    rank_jump_candidates = 0
    max_rank_jump = 0.0
    for item in report:
        ticker = str(item.get("ticker") or "").upper()
        jumps: dict[str, float] = {}
        if ticker in previous_rows:
            for metric, current_value in (current_pct.get(ticker) or {}).items():
                previous_value = (previous_pct.get(ticker) or {}).get(metric)
                if previous_value is None:
                    continue
                jump = round(float(current_value) - float(previous_value), 6)
                jumps[metric] = jump
        positive = {name: value for name, value in jumps.items() if value > 0}
        jump_max = max(positive.values(), default=0.0)
        jump_sum = round(sum(positive.values()), 6)
        item["rank_jump_pct_by_metric"] = jumps
        item["rank_jump_fields"] = sorted(positive, key=lambda name: positive[name], reverse=True)
        item["rank_jump_max_pct"] = round(jump_max, 6)
        item["rank_jump_sum_pct"] = jump_sum
        item["direction_flips"] = _direction_flips(current_rows[ticker], previous_rows[ticker]) if ticker in current_rows and ticker in previous_rows else []
        if jump_max > 0:
            rank_jump_candidates += 1
            max_rank_jump = max(max_rank_jump, jump_max)

    meaningful = [item for item in report if item.get("meaningful_change")]
    meaningful.sort(key=lambda item: (float(item.get("change_score") or 0.0), str(item.get("ticker") or "")), reverse=True)
    n = len(meaningful)
    for index, item in enumerate(meaningful, start=1):
        percentile = 100.0 if n <= 1 else 100.0 * (1.0 - (index - 1) / (n - 1))
        item["material_rank"] = index
        item["material_percentile"] = round(percentile, 6)
    for item in report:
        item.setdefault("material_rank", None)
        item.setdefault("material_percentile", None)
        changes[str(item.get("ticker") or "").upper()] = item

    report.sort(
        key=lambda item: (
            bool(item.get("meaningful_change")),
            float(item.get("change_score") or 0.0),
            float(item.get("rank_jump_max_pct") or 0.0),
            str(item.get("ticker") or ""),
        ),
        reverse=True,
    )
    summary = dict(summary)
    summary.update({
        "material_change_candidates": n,
        "rank_jump_candidates": rank_jump_candidates,
        "max_rank_jump_pct": round(max_rank_jump, 6),
        "top_material_sample": [
            {
                "ticker": item.get("ticker"),
                "change_score": item.get("change_score"),
                "material_percentile": item.get("material_percentile"),
                "rank_jump_max_pct": item.get("rank_jump_max_pct"),
                "rank_jump_fields": item.get("rank_jump_fields"),
                "direction_flips": item.get("direction_flips"),
            }
            for item in meaningful[:20]
        ],
        "adaptive_materiality": "cross_sectional_rank_v1",
    })
    return changes, report, summary


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
    """Prioritize relative rank jumps while preserving all existing safety caps."""
    original_map = {str(k).upper(): dict(v) for k, v in (change_map or {}).items()}
    priority_map: dict[str, dict] = {}
    for ticker, item in original_map.items():
        adjusted = dict(item)
        raw_score = float(item.get("change_score") or 0.0)
        # Rank-jump is expressed in percentile points (0..100). Adding those
        # points to the already-existing change score is universe-relative and
        # bounded; no new market/trading threshold is introduced.
        rank_bonus = max(0.0, float(item.get("rank_jump_max_pct") or 0.0))
        adjusted["change_score"] = raw_score + rank_bonus
        adjusted["raw_change_score"] = raw_score
        adjusted["rank_jump_priority_bonus"] = rank_bonus
        priority_map[ticker] = adjusted

    pairs, report, summary = _base.select_hourly_enrichment(
        rows,
        max_tickers=max_tickers,
        always_tickers=always_tickers,
        open_trade_tickers=open_trade_tickers,
        recent_alert_tickers=recent_alert_tickers,
        previous_enriched_tickers=previous_enriched_tickers,
        change_map=priority_map,
        top_changed=top_changed,
        top_gex_change=top_gex_change,
        top_near_wall=top_near_wall,
        top_rvol=top_rvol,
        top_signals=top_signals,
        continuity_max=continuity_max,
    )
    selected_at = datetime.now(timezone.utc).isoformat()
    selected_rank_jump = 0
    for row in report:
        ticker = str(row.get("ticker") or "").upper()
        original = original_map.get(ticker) or {}
        raw_score = float(original.get("change_score") or 0.0)
        rank_bonus = max(0.0, float(original.get("rank_jump_max_pct") or 0.0))
        row["scanner_priority_change_score"] = round(raw_score + rank_bonus, 6)
        row["scanner_change_score"] = round(raw_score, 6)
        row["scanner_material_rank"] = original.get("material_rank")
        row["scanner_material_percentile"] = original.get("material_percentile")
        row["scanner_rank_jump_max_pct"] = round(rank_bonus, 6)
        row["scanner_rank_jump_sum_pct"] = round(float(original.get("rank_jump_sum_pct") or 0.0), 6)
        row["scanner_rank_jump_fields"] = ",".join(original.get("rank_jump_fields") or [])
        row["scanner_direction_flips"] = ",".join(original.get("direction_flips") or [])
        row["selection_computed_at_utc"] = selected_at
        if rank_bonus > 0:
            selected_rank_jump += 1

    summary = dict(summary)
    summary.update({
        "selection_computed_at_utc": selected_at,
        "rank_jump_priority_enabled": True,
        "rank_jump_priority_basis": "positive cross-sectional percentile improvement vs previous successful Scanner run",
        "selected_rank_jump_candidates": selected_rank_jump,
        "configured_cap_unchanged": int(max_tickers or 0),
    })
    return pairs, report, summary
