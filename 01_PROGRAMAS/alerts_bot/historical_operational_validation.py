from __future__ import annotations

"""Operational historical validation for TMAD Alpha Swing.

SHADOW/research only.  The goal is to answer the questions a trader would ask:
what would TMAD have selected, when would it have entered/exited, what would the
underlying have returned, what would the same indicative option contract have
done under the synthetic proxy, and which favourable market moves were captured
or missed.

Selection/ranking/lifecycle inputs are point-in-time historical TMAD snapshots.
Future prices are used only after a simulated OPEN, or as hindsight labels in
the reverse stress section.  Nothing in this module mutates LIVE strategy.
"""

from collections import Counter, defaultdict
import csv
import hashlib
from datetime import date, timedelta
import json
import math
from pathlib import Path
from research_streaming_io import atomic_stream_json
from statistics import mean, median
from typing import Any, Callable

import config
from daily_reverse_alpha import HORIZONS, _miss_reason, _stage_path, build_daily_reverse_alpha
from db import connect
from fusion_engine import build_fusion_signals
from eod_options_lab import build_eod_shadow_portfolio
from entry_quality_research import build_entry_quality_walkforward
from entry_quality_cross_sectional import build_cross_sectional_entry_quality_walkforward
from entry_quality_eligibility import build_entry_eligibility_walkforward
from entry_eligibility_falsification import (
    build_entry_eligibility_falsification,
    combine_eligibility_falsification,
)
from economic_waterfall_calibration import (
    build_economic_waterfall_calibration,
    combine_economic_waterfall_calibrations,
)
from economic_architecture_challengers import (
    build_economic_architecture_challengers,
    combine_economic_architecture_challengers,
)
from frozen_config_historical_simulation import (
    build_frozen_config_historical_simulation,
    combine_frozen_config_historical_simulations,
)
from scanner_recall_ranking_calibration import (
    build_scanner_recall_ranking_calibration,
    combine_scanner_recall_ranking_calibrations,
)
from scanner_core_interaction_simulation import (
    build_scanner_core_interaction_simulation,
    combine_scanner_core_interaction_simulations,
)
from scanner_direction_aware_ranking import (
    build_scanner_direction_aware_ranking,
    combine_scanner_direction_aware_rankings,
)
from core_context_precision_optimizer import (
    build_core_context_precision_optimizer,
    combine_core_context_precision_optimizers,
)
from autonomous_research_report import build_autonomous_research_summary
from gex_gate_calibration import (
    build_gex_gate_calibration,
    combine_gex_gate_calibrations,
)
from fusion_gate_calibration import (
    build_fusion_gate_calibration_from_trusted_series,
    combine_fusion_gate_calibrations,
)
from entry_thesis_quality import build_thesis_quality_walkforward
from momentum_direction_audit import build_momentum_direction_audit
from momentum_edge_attrition import (
    build_momentum_edge_attrition_audit,
    build_trusted_momentum_reconciliation,
)
from research_benchmark import operational_run_key_set, feature_observation_id_set
from eod_options_runtime import (
    _STATE_PRIORITY,
    _candidate_score,
    _gex_profile,
    _intraday_lifecycle,
    _swing_context,
    _volatility_proxy_input,
)
from market_outcome_auditor import (
    _all_feature_rows,
    _all_signal_rows,
    _cluster_and_classify,
    _full_deep_reverse_canonical,
    _merge_price_series,
    _movement_candidates,
    _observation_outcome_price_series,
    _price_series,
    _rank_maps,
    _rows_to_outcome_price_series,
    _signals_index,
)
from return_math import directional_return_pct, favorable_move_pct
from research_snapshot import research_db_path
from synthetic_option_proxy import (
    MIN_THEORETICAL_PREMIUM,
    _point_in_time_volatility,
    black_scholes_price,
)
from time_utils import ts_madrid_str


CLOSE_STATES = {"DIVERGENCE", "MARKET_BLOCKED"}
MAX_HOLD_SESSIONS = 5


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value or "{}")
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _feature_payload(row: dict) -> dict:
    payload = _json(row.get("payload_json") or row.get("features_json"))
    nested = payload.get("features")
    return nested if isinstance(nested, dict) else payload


def _day(row: dict) -> str:
    return str(row.get("snapshot_date") or row.get("signal_date") or row.get("observation_date") or "")[:10]


def _ticker(row: dict) -> str:
    return str(row.get("ticker") or row.get("symbol") or "").upper()


def _direction(row: dict) -> str:
    return str(row.get("direction") or "").upper()


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return round(gains / losses, 4)


def _trade_sequence_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(abs(worst), 4)


def _directional_return(direction: str, entry: float, exit_price: float) -> float:
    value = directional_return_pct(direction, entry, exit_price)
    return float(value or 0.0)


def _valid_invalidation(direction: str, entry: float, invalidation: float | None) -> float | None:
    if invalidation is None or invalidation <= 0 or entry <= 0:
        return None
    if direction == "LONG" and invalidation < entry:
        return float(invalidation)
    if direction == "SHORT" and invalidation > entry:
        return float(invalidation)
    return None


def _feature_meta_index(feature_rows: list[dict]) -> tuple[dict[tuple[str, int, str], dict], dict[tuple[str, str], dict]]:
    by_run: dict[tuple[str, int, str], dict] = {}
    by_day_ticker: dict[tuple[str, str], dict] = {}
    for row in feature_rows:
        day = _day(row)
        ticker = _ticker(row)
        run_id = int(row.get("source_scanner_run_id") or 0)
        if not day or not ticker:
            continue
        if run_id:
            by_run[(day, run_id, ticker)] = row
        key = (day, ticker)
        old = by_day_ticker.get(key)
        if old is None or run_id >= int(old.get("source_scanner_run_id") or 0):
            by_day_ticker[key] = row
    return by_run, by_day_ticker



def _active_model_snapshot_readonly() -> dict:
    """Read the currently active model without triggering repair/switch side effects."""
    conn = connect(research_db_path(config.ALERTS_DB), readonly=True)
    try:
        override_id = 0
        entry_mode = "shadow"
        try:
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key='entry_mode'"
            ).fetchone()
            entry_mode = str(row[0] if row else "shadow").lower()
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key='adaptive_shadow_model_id'"
            ).fetchone()
            override_id = int(row[0] or 0) if row else 0
        except Exception:
            override_id = 0

        model = None
        if entry_mode != "live" and override_id > 0:
            model = conn.execute("SELECT * FROM model_versions WHERE id=?", (override_id,)).fetchone()
        if model is None:
            model = conn.execute(
                """SELECT mv.* FROM model_versions mv
                   JOIN model_state ms ON ms.model_version_id=mv.id
                   WHERE ms.state_key='active'
                   ORDER BY mv.id DESC LIMIT 1"""
            ).fetchone()
        if model is None:
            model = conn.execute(
                "SELECT * FROM model_versions WHERE status='bootstrap' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if model is None:
            return {}
        item = dict(model)
        item["params"] = _json(item.get("params_json"))
        return item
    finally:
        conn.close()


def _counterfactual_current_policy_inputs(
    run_keys_override: set[tuple[str, int]] | None = None,
    observation_ids_override: set[int] | None = None,
    *,
    lane_label: str = "ROLLING_LATEST",
) -> tuple[list[dict], list[dict], dict]:
    """Re-run CURRENT Fusion policy on point-in-time Scanner snapshots.

    ROLLING_LATEST chooses the latest substantial Scanner run per mature date.
    FIXED_BENCHMARK may instead supply immutable (date, run_id) keys. In both
    cases ranking/features are point-in-time and current code is applied
    retrospectively; future prices remain outcome labels only.
    """
    override_keys = {
        (str(day)[:10], int(run_id))
        for day, run_id in (run_keys_override or set())
        if str(day)[:10] and int(run_id) > 0
    }
    override_observation_ids = {
        int(value) for value in (observation_ids_override or set())
        if int(value) > 0
    }

    conn = connect(research_db_path(config.ALERTS_DB), readonly=True)
    try:
        mature_rows = conn.execute(
            """SELECT DISTINCT observation_date
               FROM observation_outcomes
               WHERE complete=1 AND horizon_sessions=5
               ORDER BY observation_date"""
        ).fetchall()
        mature_dates = [str(row[0]) for row in mature_rows if row[0]]
        if not mature_dates and not override_keys:
            return [], [], {
                "status": "NO_MATURE_5D_DATES",
                "dates": 0,
                "runs": 0,
                "feature_rows": 0,
                "lane": lane_label,
            }

        if override_keys:
            chosen = [
                {
                    "snapshot_date": day,
                    "source_scanner_run_id": run_id,
                    "rows": None,
                    "observed_at_madrid": None,
                }
                for day, run_id in sorted(override_keys)
            ]
            selection_rule = "immutable benchmark (date, source_scanner_run_id) keys"
        else:
            stats: list[dict] = []
            for start_i in range(0, len(mature_dates), 300):
                dates = mature_dates[start_i:start_i + 300]
                placeholders = ",".join("?" for _ in dates)
                stats.extend(
                    dict(row)
                    for row in conn.execute(
                        f"""SELECT snapshot_date,source_scanner_run_id,COUNT(*) AS rows,
                                   MAX(observed_at_madrid) AS observed_at_madrid
                            FROM feature_observations
                            WHERE snapshot_date IN ({placeholders})
                            GROUP BY snapshot_date,source_scanner_run_id
                            ORDER BY snapshot_date,source_scanner_run_id""",
                        dates,
                    ).fetchall()
                )

            by_date: dict[str, list[dict]] = defaultdict(list)
            for row in stats:
                day = str(row.get("snapshot_date") or "")[:10]
                run_id = int(row.get("source_scanner_run_id") or 0)
                if day and run_id > 0:
                    by_date[day].append(row)

            chosen: list[dict] = []
            for day, runs in sorted(by_date.items()):
                max_rows = max(int(row.get("rows") or 0) for row in runs)
                threshold = max(4, int(math.ceil(max_rows * 0.50)))
                eligible = [row for row in runs if int(row.get("rows") or 0) >= threshold]
                if not eligible:
                    continue
                chosen.append(max(eligible, key=lambda row: int(row.get("source_scanner_run_id") or 0)))
            selection_rule = "latest run with >=50% of daily max cross-section and >=4 tickers"

        chosen_keys = {
            (
                str(row.get("snapshot_date") or "")[:10],
                int(row.get("source_scanner_run_id") or 0),
            )
            for row in chosen
            if str(row.get("snapshot_date") or "")[:10]
            and int(row.get("source_scanner_run_id") or 0) > 0
        }
        run_ids = sorted({run_id for _day_value, run_id in chosen_keys})
        raw_rows: list[dict] = []
        for start_i in range(0, len(run_ids), 300):
            batch = run_ids[start_i:start_i + 300]
            if not batch:
                continue
            ph = ",".join("?" for _ in batch)
            raw_rows.extend(
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM feature_observations
                        WHERE source_scanner_run_id IN ({ph})
                        ORDER BY snapshot_date,source_scanner_run_id,id""",
                    batch,
                ).fetchall()
            )
    finally:
        conn.close()

    raw_rows = [
        row for row in raw_rows
        if (_day(row), int(row.get("source_scanner_run_id") or 0)) in chosen_keys
    ]
    if override_observation_ids:
        raw_rows = [
            row for row in raw_rows
            if int(row.get("id") or row.get("observation_id") or 0) in override_observation_ids
        ]

    model = _active_model_snapshot_readonly()
    by_run: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in raw_rows:
        key = (_day(row), int(row.get("source_scanner_run_id") or 0))
        if key[0] and key[1] > 0:
            by_run[key].append(row)

    signal_rows: list[dict] = []
    feature_rows: list[dict] = []
    enter_count = 0
    fusion_enter_state_count = 0
    fusion_count = 0

    for (day, run_id), rows in sorted(by_run.items()):
        features_by_ticker: dict[str, dict] = {}
        normalized_features: list[dict] = []
        for row in rows:
            ticker = _ticker(row)
            if not ticker:
                continue
            features = dict(_feature_payload(row))
            features["ticker"] = ticker
            features["snapshot_date"] = day
            features["scanner_run_id"] = run_id
            if row.get("source_gex_run_id") not in (None, ""):
                features["gex_run_id"] = row.get("source_gex_run_id")
            price = _f(row.get("current_price"), _f(features.get("price")))
            if price is not None and price > 0:
                features["price"] = price
            features_by_ticker[ticker] = features

            normalized = dict(row)
            normalized["snapshot_date"] = day
            normalized["source_scanner_run_id"] = run_id
            normalized["price"] = price
            normalized["payload_json"] = json.dumps(features, ensure_ascii=False, default=str)
            normalized["features_json"] = normalized["payload_json"]
            normalized_features.append(normalized)

        if not features_by_ticker:
            continue

        try:
            signals, _actionable = build_fusion_signals(
                features_by_ticker,
                model,
                mode="intraday",
            )
        except Exception:
            continue

        by_ticker_layer: dict[tuple[str, str], dict] = {}
        for raw_signal in signals:
            key = (_ticker(raw_signal), str(raw_signal.get("layer") or "").upper())
            if key[0] and key[1]:
                old_signal = by_ticker_layer.get(key)
                if old_signal is None or float(_f(raw_signal.get("score"), 0.0) or 0.0) >= float(_f(old_signal.get("score"), 0.0) or 0.0):
                    by_ticker_layer[key] = raw_signal

        for normalized in normalized_features:
            ticker = _ticker(normalized)
            enriched_features = dict(features_by_ticker.get(ticker) or _feature_payload(normalized))
            normalized["payload_json"] = json.dumps(enriched_features, ensure_ascii=False, default=str)
            normalized["features_json"] = normalized["payload_json"]
            normalized["price"] = _f(normalized.get("price"), _f(enriched_features.get("price")))
            core = by_ticker_layer.get((ticker, "CORE")) or {}
            gex = by_ticker_layer.get((ticker, "GEX_DAILY")) or {}
            fusion = by_ticker_layer.get((ticker, "FUSION")) or {}
            normalized["outcome_entry_price"] = normalized.get("price")
            normalized["core_direction"] = str(core.get("direction") or "").lower()
            normalized["core_score"] = core.get("score")
            normalized["core_probability_pct"] = core.get("probability_pct")
            normalized["gex_direction"] = str(gex.get("direction") or "").lower()
            normalized["gex_score"] = gex.get("score")
            normalized["gex_intent"] = gex.get("gex_intent")
            normalized["fusion_state"] = fusion.get("opportunity_state") or fusion.get("state")
            normalized["fusion_score"] = fusion.get("score")
            normalized["actionable"] = 1 if bool(fusion.get("actionable")) else 0
        feature_rows.extend(normalized_features)

        for signal in signals:
            item = dict(signal)
            ticker = _ticker(item)
            if not ticker:
                continue
            item["snapshot_date"] = day
            item["signal_date"] = day
            item["source_scanner_run_id"] = run_id
            item["direction"] = str(item.get("direction") or "").lower()
            item["payload_json"] = json.dumps(signal, ensure_ascii=False, default=str)
            signal_rows.append(item)
            if str(item.get("layer") or "").upper() == "FUSION":
                fusion_count += 1
                fusion_state=str(item.get("opportunity_state") or item.get("state") or "").upper()
                if fusion_state == "ENTER":
                    fusion_enter_state_count += 1
                    if bool(item.get("actionable")):
                        enter_count += 1

    available_run_keys = set(by_run)
    return signal_rows, feature_rows, {
        "status": "EVALUABLE" if feature_rows else "EMPTY",
        "lane": str(lane_label or "ROLLING_LATEST").upper(),
        "mature_5d_dates": len(mature_dates),
        "selected_dates": len(chosen_keys),
        "selected_runs": len(chosen_keys),
        "selected_run_keys": [
            {"date": day, "source_scanner_run_id": run_id}
            for day, run_id in sorted(chosen_keys)
        ],
        "requested_run_keys": len(override_keys) if override_keys else None,
        "requested_feature_observation_ids": len(override_observation_ids) if override_observation_ids else None,
        "available_feature_observation_ids": len({
            int(row.get("id") or row.get("observation_id") or 0)
            for row in raw_rows
            if int(row.get("id") or row.get("observation_id") or 0) > 0
        }),
        "missing_requested_feature_observation_ids": (
            len(override_observation_ids - {
                int(row.get("id") or row.get("observation_id") or 0)
                for row in raw_rows
                if int(row.get("id") or row.get("observation_id") or 0) > 0
            })
            if override_observation_ids else 0
        ),
        "available_run_keys": len(available_run_keys),
        "missing_requested_run_keys": (
            len(override_keys - available_run_keys)
            if override_keys else 0
        ),
        "feature_rows": len(feature_rows),
        "all_component_signals": len(signal_rows),
        "fusion_signals": fusion_count,
        "fusion_enter_state": fusion_enter_state_count,
        "fusion_enter_actionable": enter_count,
        "active_model_id": model.get("id"),
        "active_model_key": model.get("model_key"),
        "run_selection": selection_rule,
        "interpretation": "RETROSPECTIVE_CURRENT_CODE_ON_POINT_IN_TIME_FEATURES",
        "lookahead_note": "current active model/calibration is applied retrospectively; use OOT challengers for unbiased economic proof",
    }


def _layer_outcome_price_series(feature_rows: list[dict]) -> dict[str, list[dict]]:
    """Layer feature outcomes retain actual OHLC session paths; use them as archive fallback."""
    return _rows_to_outcome_price_series(feature_rows)



def _movement_candidates_from_prices(
    canonical: dict[tuple[str, str], dict],
    series: dict[str, list[dict]],
    *,
    horizon_sessions: int = 5,
) -> list[dict]:
    """Derive +/- target moves directly from reconstructed OHLC paths.

    Future OHLC is label-only. No future value enters the historical decision.
    This avoids depending on legacy outcome schemas/flags.
    """
    target_pct = float(getattr(config, "MARKET_OUTCOME_AUDITOR_TARGET_PCT", 5.0))
    candidates: list[dict] = []
    for (signal_date, ticker), row in canonical.items():
        entry = _f(row.get("outcome_entry_price"), _f(row.get("price"), _f(row.get("current_price"))))
        if entry is None or entry <= 0:
            continue
        future = _same_segment_future_rows(
            series.get(ticker, []),
            signal_date,
            horizon_sessions,
        )
        if not future:
            continue

        long_hit_session = short_hit_session = None
        long_hit_row = short_hit_row = None
        max_up = None
        max_down = None
        for idx, item in enumerate(future, 1):
            high = _f(item.get("high"), _f(item.get("close")))
            low = _f(item.get("low"), _f(item.get("close")))
            if high is not None and high > 0:
                up = (float(high) / float(entry) - 1.0) * 100.0
                max_up = up if max_up is None else max(max_up, up)
                if long_hit_session is None and up >= target_pct:
                    long_hit_session, long_hit_row = idx, item
            if low is not None and low > 0:
                down = favorable_move_pct("SHORT", entry, low)
                if down is None:
                    continue
                max_down = down if max_down is None else max(max_down, down)
                if short_hit_session is None and down >= target_pct:
                    short_hit_session, short_hit_row = idx, item

        sector = row.get("sector") or _feature_payload(row).get("sector")
        industry = row.get("industry") or _feature_payload(row).get("industry")
        for direction, first, hit_row, move in (
            ("long", long_hit_session, long_hit_row, max_up),
            ("short", short_hit_session, short_hit_row, max_down),
        ):
            if first is None or hit_row is None or move is None:
                continue
            hit_date = str(hit_row.get("trade_date") or "")[:10]
            first_open = _f(hit_row.get("open"))
            gap_pct = None
            if first_open is not None and first_open > 0:
                gap_pct = (
                    (float(first_open) / float(entry) - 1.0) * 100.0
                    if direction == "long"
                    else float(directional_return_pct("SHORT", entry, first_open) or 0.0)
                )
            threshold = (
                float(entry) * (1.0 + target_pct / 100.0)
                if direction == "long"
                else float(entry) * (1.0 - target_pct / 100.0)
            )
            candidates.append({
                "ticker": ticker,
                "direction": direction,
                "signal_date": signal_date,
                "hit_date": hit_date,
                "entry_price": float(entry),
                "threshold_price": threshold,
                "move_pct": float(move),
                "first_hit_session": int(first),
                "gap_pct": gap_pct,
                "sector": sector,
                "industry": industry,
                "row": row,
            })
    return candidates


def _counterfactual_reverse_universe(
    counter_feature_rows: list[dict],
    full_deep_meta: dict[str, dict],
) -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
    canonical: dict[tuple[str, str], dict] = {}
    by_date_count: Counter = Counter()
    for row in counter_feature_rows:
        day, ticker = _day(row), _ticker(row)
        if not day or not ticker:
            continue
        key = (day, ticker)
        old = canonical.get(key)
        run_id = int(row.get("source_scanner_run_id") or 0)
        if old is None or run_id >= int(old.get("source_scanner_run_id") or 0):
            canonical[key] = row
    for day, _ticker_key in canonical:
        by_date_count[day] += 1

    metadata: dict[str, dict] = {}
    for day, available in sorted(by_date_count.items()):
        base = dict(full_deep_meta.get(day) or {})
        expected = int(base.get("expected_universe") or 0)
        scope = str(base.get("universe_scope") or "OBSERVED_FEATURE_SUBSET")
        if expected and available < expected:
            scope = "OBSERVED_FEATURE_SUBSET"
        metadata[day] = {
            **base,
            "universe_scope": scope,
            "available_universe": int(available),
            "expected_universe": expected or None,
            "coverage_pct": round(100.0 * available / expected, 3) if expected else None,
            "counterfactual_current_policy_snapshot": True,
        }
    return canonical, metadata

def _quality_enter_assessment(signal: dict, context: dict | None = None) -> dict:
    """Point-in-time structural quality gate for research ENTER selection.

    No hindsight returns or future prices are used. The gate only requires:
    1) CORE and GEX agree on direction,
    2) intraday timing is explicitly ENTER,
    3) the broad market regime is not strongly opposed to the direction.

    This intentionally avoids tuning numeric thresholds to historical P&L.
    """
    context = context if isinstance(context, dict) else {}
    direction = _direction(signal)
    state = str(
        signal.get("opportunity_state")
        or signal.get("state")
        or context.get("state")
        or ""
    ).upper()
    actionable = bool(
        signal.get("actionable")
        if signal.get("actionable") is not None
        else context.get("actionable")
    )
    origin = str(signal.get("signal_origin") or "").upper()
    intraday_state = str(signal.get("intraday_state") or "").upper()
    market_policy = str(signal.get("market_policy") or "NORMAL").upper()
    countertrend = bool(signal.get("countertrend_override"))

    direction_aligned = origin == "CORE_GEX_ALIGNED"
    timing_ready = intraday_state == "ENTER"
    opposed_policies = {
        "LONG": {"BLOCK_LONGS", "PARALLEL_CLOSE_ALL_WARNING"},
        "SHORT": {"BLOCK_SHORTS", "PARALLEL_CLOSE_ALL_WARNING"},
    }
    regime_not_strongly_opposed = (
        direction in {"LONG", "SHORT"}
        and market_policy not in opposed_policies.get(direction, set())
        and not countertrend
    )

    reasons: list[str] = []
    if not direction_aligned:
        reasons.append("CORE_GEX_NOT_ALIGNED")
    if not timing_ready:
        reasons.append("TIMING_NOT_ENTER")
    if not regime_not_strongly_opposed:
        reasons.append("REGIME_OPPOSED_OR_COUNTERTREND")

    qualified = bool(
        state == "ENTER"
        and actionable
        and direction_aligned
        and timing_ready
        and regime_not_strongly_opposed
    )
    return {
        "qualified": qualified,
        "direction_aligned": direction_aligned,
        "timing_ready": timing_ready,
        "regime_not_strongly_opposed": regime_not_strongly_opposed,
        "signal_origin": origin,
        "intraday_state": intraday_state,
        "market_policy": market_policy,
        "countertrend_override": countertrend,
        "reasons": reasons,
    }


def reconstruct_daily_portfolios(
    signal_rows: list[dict],
    feature_rows: list[dict],
    *,
    max_positions: int = 5,
    selection_policy: str = "CURRENT_SCORE_ONLY",
) -> list[dict]:
    """Apply Alpha Swing ranking to historical point-in-time rows.

    CURRENT_SCORE_ONLY reproduces LIVE behavior exactly.
    ACTIONABLE_ENTER_FIRST is a research-only challenger: actionable ENTERs
    occupy the available Top-N slots first, ranked by the existing alpha score;
    any remaining slots are filled by the unchanged current ranking.
    QUALITY_GATED_ENTER is a stricter SHADOW challenger: only actionable ENTERs
    with CORE/GEX directional agreement, ENTER timing, and a non-opposed market
    regime may open. Non-ENTER rows can still occupy unused display slots.
    ALL_ACTIONABLE_ENTER is a research lab lane that returns the complete
    canonical actionable ENTER cross-section without a Top-N compression.
    """
    fusion = [row for row in signal_rows if str(row.get("layer") or "").upper() == "FUSION" and _day(row)]
    if not fusion:
        return []

    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in fusion:
        by_date[_day(row)].append(row)
    feature_by_run, _feature_day = _feature_meta_index(feature_rows)
    feature_tickers_by_day_run: dict[tuple[str, int], set[str]] = defaultdict(set)
    for feature_day, feature_run, feature_ticker in feature_by_run:
        if feature_day and int(feature_run or 0) > 0 and feature_ticker:
            feature_tickers_by_day_run[(feature_day, int(feature_run))].add(str(feature_ticker).upper())

    portfolios: list[dict] = []
    for day, day_signals in sorted(by_date.items()):
        signal_tickers_by_run: dict[int, set[str]] = defaultdict(set)
        for row in day_signals:
            candidate_run = int(row.get("source_scanner_run_id") or 0)
            ticker = _ticker(row)
            if candidate_run > 0 and ticker:
                signal_tickers_by_run[candidate_run].add(ticker)

        run_diagnostics: list[dict] = []
        for candidate_run, signal_tickers in signal_tickers_by_run.items():
            feature_tickers = feature_tickers_by_day_run.get((day, candidate_run), set())
            overlap = signal_tickers & feature_tickers
            run_diagnostics.append({
                "run_id": int(candidate_run),
                "signal_tickers": len(signal_tickers),
                "feature_tickers": len(feature_tickers),
                "point_in_time_overlap": len(overlap),
            })
        eligible_runs = [row for row in run_diagnostics if int(row.get("point_in_time_overlap") or 0) > 0]
        if not eligible_runs:
            continue
        chosen_run = max(
            eligible_runs,
            key=lambda row: (
                int(row.get("point_in_time_overlap") or 0),
                min(int(row.get("signal_tickers") or 0), int(row.get("feature_tickers") or 0)),
                int(row.get("feature_tickers") or 0),
                int(row.get("signal_tickers") or 0),
                int(row.get("run_id") or 0),
            ),
        )
        run_id = int(chosen_run["run_id"])
        latest_rows = [row for row in day_signals if int(row.get("source_scanner_run_id") or 0) == run_id]
        cutoff = max((str(row.get("observed_at_madrid") or "") for row in latest_rows), default="")
        if cutoff:
            lifecycle_rows = [
                row for row in day_signals
                if (
                    str(row.get("observed_at_madrid") or "") <= cutoff
                    if str(row.get("observed_at_madrid") or "")
                    else int(row.get("source_scanner_run_id") or 0) <= run_id
                )
            ]
        else:
            lifecycle_rows = [
                row for row in day_signals
                if int(row.get("source_scanner_run_id") or 0) <= run_id
            ]
        lifecycle_by_ticker = _intraday_lifecycle(lifecycle_rows, day)

        strongest: dict[str, dict] = {}
        for signal in latest_rows:
            ticker = _ticker(signal)
            direction = _direction(signal)
            if not ticker or direction not in {"LONG", "SHORT"}:
                continue
            state = str(signal.get("opportunity_state") or signal.get("state") or "WATCH").upper()
            rank = _STATE_PRIORITY.get(state, 2) * 1000.0 + float(_f(signal.get("score"), 0.0) or 0.0)
            if ticker not in strongest or rank > float(strongest[ticker]["_rank"]):
                strongest[ticker] = {**signal, "_rank": rank, "_state": state}

        candidates: list[dict] = []
        gex_profiles: dict[str, dict] = {}
        source_rows: dict[str, dict] = {}
        for ticker, signal in strongest.items():
            meta = feature_by_run.get((day, run_id, ticker))
            if not meta:
                continue
            features = _feature_payload(meta)
            spot = _f(meta.get("price"), _f(meta.get("current_price"), _f(features.get("price"))))
            if spot is None or spot <= 0:
                continue
            life = lifecycle_by_ticker.get(ticker) or {}
            pipeline, early, sector, flow, lifecycle_score = _candidate_score(features, signal, life)
            blended_early = round(0.65 * early + 0.35 * lifecycle_score, 3)
            quality_gate = _quality_enter_assessment(signal)
            candidates.append({
                "ticker": ticker,
                "direction": _direction(signal),
                "spot": float(spot),
                "score": pipeline,
                "early_alpha_score": blended_early,
                "sector_strength": sector,
                "flow_score": flow,
                "opportunity_state": signal.get("_state"),
                "signal_origin": signal.get("signal_origin"),
                "intraday_state": signal.get("intraday_state"),
                "market_policy": signal.get("market_policy"),
                "market_adjustment": signal.get("market_adjustment"),
                "countertrend_override": bool(signal.get("countertrend_override")),
                "intraday_blocks": list(signal.get("intraday_blocks") or []),
                "intraday_adverse": list(signal.get("intraday_adverse") or []),
                "core_score": signal.get("core_score"),
                "gex_score": signal.get("gex_score"),
                "overextension_score": signal.get("overextension_score"),
                "quality_gate": quality_gate,
                "lifecycle": life,
                "swing_context": _swing_context(features, signal),
                "volatility_proxy_input": _volatility_proxy_input(features),
            })
            gex_profiles[ticker] = _gex_profile(features)
            source_rows[ticker] = meta

        policy = str(selection_policy or "CURRENT_SCORE_ONLY").upper()
        build_limit = max_positions if policy == "CURRENT_SCORE_ONLY" else max(max_positions, len(candidates))
        payload = build_eod_shadow_portfolio(
            candidates, gex_profiles, asof=day, max_positions=build_limit
        )
        by_ticker = {_ticker(row): row for row in candidates}
        for selected in payload.get("selected") or []:
            source = by_ticker.get(_ticker(selected)) or {}
            selected["opportunity_state"] = source.get("opportunity_state")
            selected["lifecycle"] = source.get("lifecycle") or {}
            selected["signal_origin"] = source.get("signal_origin")
            selected["intraday_state"] = source.get("intraday_state")
            selected["market_policy"] = source.get("market_policy")
            selected["market_adjustment"] = source.get("market_adjustment")
            selected["countertrend_override"] = bool(source.get("countertrend_override"))
            selected["intraday_blocks"] = list(source.get("intraday_blocks") or [])
            selected["intraday_adverse"] = list(source.get("intraday_adverse") or [])
            selected["core_score"] = source.get("core_score")
            selected["gex_score"] = source.get("gex_score")
            selected["overextension_score"] = source.get("overextension_score")
            selected["quality_gate"] = source.get("quality_gate") or {}
            selected["_source_feature_row"] = source_rows.get(_ticker(selected)) or {}
            selected["_source_scanner_run_id"] = run_id

        if policy in {"ACTIONABLE_ENTER_FIRST", "QUALITY_GATED_ENTER", "ALL_ACTIONABLE_ENTER"}:
            all_ranked = list(payload.get("selected") or [])

            def _is_actionable_enter(row: dict) -> bool:
                context = row.get("swing_context") if isinstance(row.get("swing_context"), dict) else {}
                state = str(row.get("opportunity_state") or context.get("state") or "").upper()
                return state == "ENTER" and bool(context.get("actionable"))

            if policy == "ALL_ACTIONABLE_ENTER":
                actionable = [row for row in all_ranked if _is_actionable_enter(row)]
                actionable.sort(
                    key=lambda row: (
                        float(_f(row.get("underlying_alpha_score"), 0.0) or 0.0),
                        float(_f(row.get("tradeability_score"), 0.0) or 0.0),
                        _ticker(row),
                    ),
                    reverse=True,
                )
                payload["selected"] = actionable
                payload["selected_count"] = len(actionable)
                payload["candidate_count"] = len(all_ranked)
                payload["selection_policy"] = "ALL_ACTIONABLE_ENTER_RESEARCH"
            elif policy == "QUALITY_GATED_ENTER":
                qualified = [
                    row for row in all_ranked
                    if _is_actionable_enter(row) and bool((row.get("quality_gate") or {}).get("qualified"))
                ]
                non_enter = [row for row in all_ranked if not _is_actionable_enter(row)]
                disqualified = [
                    row for row in all_ranked
                    if _is_actionable_enter(row) and not bool((row.get("quality_gate") or {}).get("qualified"))
                ]

                def _quality_key(row: dict) -> tuple[float, float, str]:
                    return (
                        float(_f(row.get("underlying_alpha_score"), 0.0) or 0.0),
                        float(_f(row.get("tradeability_score"), 0.0) or 0.0),
                        _ticker(row),
                    )

                qualified.sort(key=_quality_key, reverse=True)
                non_enter.sort(key=_quality_key, reverse=True)
                filtered_ranked = [*qualified, *non_enter]
                payload["selected"] = filtered_ranked[: max(0, int(max_positions))]
                payload["selected_count"] = len(payload["selected"])
                payload["candidate_count"] = len(all_ranked)
                payload["selection_policy"] = "QUALITY_GATED_ENTER_SHADOW"
                reason_counts: Counter = Counter()
                for row in disqualified:
                    for reason in (row.get("quality_gate") or {}).get("reasons") or ["UNSPECIFIED"]:
                        reason_counts[str(reason)] += 1
                payload["quality_gate_diagnostics"] = {
                    "actionable_enter_candidates": len(qualified) + len(disqualified),
                    "qualified_actionable_enters": len(qualified),
                    "disqualified_actionable_enters": len(disqualified),
                    "disqualification_reasons": dict(reason_counts),
                    "future_labels_used": False,
                }
            else:
                def _challenger_key(row: dict) -> tuple[int, float, float, str]:
                    return (
                        1 if _is_actionable_enter(row) else 0,
                        float(_f(row.get("underlying_alpha_score"), 0.0) or 0.0),
                        float(_f(row.get("tradeability_score"), 0.0) or 0.0),
                        _ticker(row),
                    )
                all_ranked.sort(key=_challenger_key, reverse=True)
                payload["selected"] = all_ranked[: max(0, int(max_positions))]
                payload["selected_count"] = len(payload["selected"])
                payload["candidate_count"] = len(all_ranked)
                payload["selection_policy"] = "ACTIONABLE_ENTER_FIRST_SHADOW"
        else:
            payload["selection_policy"] = "CURRENT_SCORE_ONLY"

        payload.update({
            "status": "ok",
            "asof": day,
            "source_scanner_run_id": run_id,
            "canonical_run_selection": {
                "policy": "MAX_POINT_IN_TIME_SIGNAL_FEATURE_OVERLAP",
                "chosen": chosen_run,
                "candidates": sorted(run_diagnostics, key=lambda row: int(row.get("run_id") or 0)),
                "lifecycle_cutoff_observed_at_madrid": cutoff or None,
                "future_same_day_runs_excluded_from_lifecycle": True,
                "future_outcome_labels_used": False,
            },
            "replay_mode": "CURRENT_PRODUCT_ON_HISTORICAL_POINT_IN_TIME_PIPELINE",
        })
        portfolios.append(payload)
    return portfolios



def _candidate_trade_from_selected(portfolio: dict, selected: dict) -> dict | None:
    """Build one independent actionable-ENTER opportunity for research labels.

    Unlike replay_stateful_alpha_book this does not impose portfolio concurrency
    or Top-N capacity. It is only the candidate-level input to the canonical
    boundary-outcome lab used by the cross-sectional ranking challenger.
    """
    day = str(portfolio.get("asof") or "")[:10]
    ticker = _ticker(selected)
    direction = _direction(selected)
    entry = _f(selected.get("spot"))
    context = selected.get("swing_context") if isinstance(selected.get("swing_context"), dict) else {}
    state = str(selected.get("opportunity_state") or context.get("state") or "").upper()
    if (
        not day or not ticker or direction not in {"LONG", "SHORT"}
        or entry is None or entry <= 0 or state != "ENTER" or not bool(context.get("actionable"))
    ):
        return None

    target = _f(context.get("target_price") or context.get("target_price_mid"))
    invalidation = _valid_invalidation(
        direction,
        float(entry),
        _f(context.get("invalidation_price") or context.get("opposite_level_price") or context.get("wall_support")),
    )
    source_feature = selected.get("_source_feature_row") if isinstance(selected.get("_source_feature_row"), dict) else {}
    source_payload = _feature_payload(source_feature)
    invalidation_distance_pct = None
    if invalidation is not None and invalidation > 0:
        if direction == "LONG":
            invalidation_distance_pct = 100.0 * (float(entry) - float(invalidation)) / float(entry)
        else:
            invalidation_distance_pct = 100.0 * (float(invalidation) - float(entry)) / float(entry)
        if invalidation_distance_pct < 0:
            invalidation_distance_pct = None
    life = selected.get("lifecycle") if isinstance(selected.get("lifecycle"), dict) else {}

    return {
        "status": "CANDIDATE",
        "signal_date": day,
        "ticker": ticker,
        "direction": direction,
        "entry_price": round(float(entry), 6),
        "target_price": round(float(target), 6) if target else None,
        "invalidation_price": round(float(invalidation), 6) if invalidation else None,
        "underlying_alpha_score": selected.get("underlying_alpha_score"),
        "tradeability_score": selected.get("tradeability_score"),
        "opportunity_state": state,
        "source_scanner_run_id": int(portfolio.get("source_scanner_run_id") or selected.get("_source_scanner_run_id") or 0),
        "entry_diagnostics": {
            "signal_origin": selected.get("signal_origin"),
            "intraday_state": selected.get("intraday_state"),
            "market_policy": selected.get("market_policy"),
            "market_adjustment": _f(selected.get("market_adjustment")),
            "countertrend_override": bool(selected.get("countertrend_override")),
            "intraday_blocks": list(selected.get("intraday_blocks") or []),
            "intraday_adverse": list(selected.get("intraday_adverse") or []),
            "core_score": _f(selected.get("core_score")),
            "gex_score": _f(selected.get("gex_score")),
            "overextension_score": _f(selected.get("overextension_score")),
            "invalidation_distance_pct": round(float(invalidation_distance_pct), 4) if invalidation_distance_pct is not None else None,
            "market_regime": source_payload.get("market_regime") or source_feature.get("market_regime"),
            "asset_type": source_payload.get("asset_type") or source_feature.get("asset_type"),
            "market_context_score": _f(source_payload.get("market_context_score"), _f(source_feature.get("market_context_score"))),
            "market_breadth_score": _f(source_payload.get("market_breadth_score"), _f(source_feature.get("market_breadth_score"))),
            "vix_stress_score": _f(source_payload.get("vix_stress_score"), _f(source_feature.get("vix_stress_score"))),
            "credit_risk_on_score": _f(source_payload.get("credit_risk_on_score"), _f(source_feature.get("credit_risk_on_score"))),
            "market_deterioration_score": _f(source_payload.get("market_deterioration_score")),
            "sector_context_score": _f(source_payload.get("sector_context_score"), _f(source_feature.get("sector_context_score"))),
            "sector_breadth_score": _f(source_payload.get("sector_breadth_score"), _f(source_feature.get("sector_breadth_score"))),
            "sector_deterioration_score": _f(source_payload.get("sector_deterioration_score")),
            "leveraged_confirmation_score": _f(source_payload.get("leveraged_confirmation_score"), _f(source_feature.get("leveraged_confirmation_score"))),
            "relationship_context_score": _f(source_payload.get("relationship_context_score"), _f(source_feature.get("relationship_context_score"))),
            "relationship_lead_lag_score": _f(source_payload.get("relationship_lead_lag_score"), _f(source_feature.get("relationship_lead_lag_score"))),
            "relationship_deterioration_score": _f(source_payload.get("relationship_deterioration_score")),
            "graph_context_score": _f(source_payload.get("graph_context_score"), _f(source_feature.get("graph_context_score"))),
            "relational_deterioration_score": _f(source_payload.get("relational_deterioration_score"), _f(source_feature.get("relational_deterioration_score"))),
            "relative_strength_vs_market": _f(source_payload.get("relative_strength_vs_market"), _f(source_feature.get("relative_strength_vs_market"))),
            "relative_strength_vs_sector": _f(source_payload.get("relative_strength_vs_sector"), _f(source_feature.get("relative_strength_vs_sector"))),
            "node_direction_score": _f(source_payload.get("node_direction_score")),
            "scanner_trend_score": _f(source_payload.get("scanner_trend_score")),
            "momentum_score": _f(source_payload.get("momentum_score")),
            "rsi": _f(source_payload.get("rsi"), _f(source_feature.get("rsi"))),
            "rvol": _f(source_payload.get("rvol"), _f(source_feature.get("rvol"))),
            "flow_direction": _f(source_payload.get("flow_direction")),
            "pc_ratio": _f(source_payload.get("pc_ratio")),
            "direction_consistency_pct": _f(life.get("direction_consistency_pct")),
            "same_direction_observations": int(life.get("same_direction_observations") or 0),
            "lifecycle_observations": int(life.get("observations") or 0),
            "quality_gate": selected.get("quality_gate") or {},
        },
    }


def _all_actionable_enter_candidate_trades(portfolios: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, int, str, str]] = set()
    for portfolio in portfolios:
        day = str(portfolio.get("asof") or "")[:10]
        run_id = int(portfolio.get("source_scanner_run_id") or 0)
        for selected in portfolio.get("selected") or []:
            trade = _candidate_trade_from_selected(portfolio, selected)
            if not trade:
                continue
            key = (day, run_id, str(trade["ticker"]), str(trade["direction"]))
            if key in seen:
                continue
            seen.add(key)
            out.append(trade)
    return sorted(out, key=lambda row: (str(row.get("signal_date") or ""), str(row.get("ticker") or ""), str(row.get("direction") or "")))


def _daily_fusion_index(signal_rows: list[dict]) -> dict[tuple[str, str], dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in signal_rows:
        if str(row.get("layer") or "").upper() != "FUSION":
            continue
        day = _day(row)
        ticker = _ticker(row)
        if day and ticker:
            grouped[(day, ticker)].append(row)
    out: dict[tuple[str, str], dict] = {}
    for key, rows in grouped.items():
        def score(row: dict) -> tuple[int, int, float]:
            state = str(row.get("opportunity_state") or row.get("state") or "WATCH").upper()
            return (
                int(row.get("source_scanner_run_id") or 0),
                _STATE_PRIORITY.get(state, 2),
                float(_f(row.get("score"), 0.0) or 0.0),
            )
        out[key] = max(rows, key=score)
    return out


def _fusion_by_run_index(signal_rows: list[dict]) -> dict[tuple[str, int, str], dict]:
    grouped: dict[tuple[str, int, str], list[dict]] = defaultdict(list)
    for row in signal_rows:
        if str(row.get("layer") or "").upper() != "FUSION":
            continue
        day = _day(row)
        ticker = _ticker(row)
        run_id = int(row.get("source_scanner_run_id") or 0)
        if day and ticker and run_id > 0:
            grouped[(day, run_id, ticker)].append(row)
    out: dict[tuple[str, int, str], dict] = {}
    for key, rows in grouped.items():
        def rank(row: dict) -> tuple[int, float]:
            state = str(row.get("opportunity_state") or row.get("state") or "WATCH").upper()
            return (
                _STATE_PRIORITY.get(state, 0),
                float(_f(row.get("score"), 0.0) or 0.0),
            )
        out[key] = max(rows, key=rank)
    return out


def _series_after(series: dict[str, list[dict]], ticker: str, day: str) -> list[dict]:
    return [row for row in series.get(ticker, []) if str(row.get("trade_date") or "") > day]


def _option_proxy_for_trade(selected: dict, trade: dict) -> dict:
    expression = selected.get("option_expression") if isinstance(selected.get("option_expression"), dict) else {}
    instrument = str(expression.get("instrument") or ("CALL" if trade["direction"] == "LONG" else "PUT")).upper()
    try:
        signal_day = date.fromisoformat(str(trade["signal_date"])[:10])
        exit_day = date.fromisoformat(str(trade["exit_date"])[:10])
    except Exception:
        return {"status": "MODEL_INPUT_UNAVAILABLE", "reason": "INVALID_TRADE_DATES"}

    source_feature = selected.get("_source_feature_row") if isinstance(selected.get("_source_feature_row"), dict) else {}
    vol = _point_in_time_volatility(selected, source_feature, [])
    annual_vol = float(vol["annual_vol_pct"])

    # Tier 1: preserve the actual indicative GEX expression when historical
    # strike + listed expiry exist. Tier 2: if chain history is incomplete,
    # estimate option convexity with a clearly-labelled normalized synthetic
    # contract instead of returning no result. This is never presented as an
    # executable/listed contract.
    expression_strike = _f(expression.get("strike"))
    expiry_text = str(expression.get("expiry") or "")[:10]
    expiry = None
    if expiry_text:
        try:
            expiry = date.fromisoformat(expiry_text)
        except Exception:
            expiry = None

    strike_source = str(expression.get("strike_source") or "")
    expiry_source = "HISTORICAL_LISTED_EXPIRY" if expiry is not None else ""
    normalized = False

    if expression_strike is not None and expression_strike > 0:
        strike = float(expression_strike)
    else:
        strike = float(trade["entry_price"])
        strike_source = "SYNTHETIC_ATM_ENTRY_SPOT"
        normalized = True

    if expiry is None or expiry <= signal_day:
        expiry = signal_day + timedelta(days=30)
        expiry_text = expiry.isoformat()
        expiry_source = "SYNTHETIC_30_CALENDAR_DAYS"
        normalized = True

    entry_dte = max(1, (expiry - signal_day).days)
    exit_dte = max(0, (expiry - exit_day).days)
    entry_opt = black_scholes_price(instrument, float(trade["entry_price"]), strike, entry_dte, annual_vol)
    exit_opt = black_scholes_price(instrument, float(trade["exit_price"]), strike, exit_dte, annual_vol)
    if entry_opt is None or exit_opt is None:
        return {"status": "MODEL_INPUT_UNAVAILABLE", "reason": "BLACK_SCHOLES_INPUT"}
    if entry_opt < MIN_THEORETICAL_PREMIUM:
        return {
            "status": "LOW_THEORETICAL_ENTRY_PREMIUM",
            "instrument": instrument,
            "strike": round(strike, 4),
            "expiry": expiry_text,
            "proxy_mode": "NORMALIZED_SYNTHETIC" if normalized else "GEX_ALIGNED_HISTORICAL_EXPRESSION",
        }

    ret = (exit_opt / entry_opt - 1.0) * 100.0
    return {
        "status": "EVALUATED_NORMALIZED_SYNTHETIC" if normalized else "EVALUATED",
        "proxy_mode": "NORMALIZED_SYNTHETIC" if normalized else "GEX_ALIGNED_HISTORICAL_EXPRESSION",
        "instrument": instrument,
        "strike": round(strike, 4),
        "strike_source": strike_source or "HISTORICAL_EXPRESSION",
        "expiry": expiry_text,
        "expiry_source": expiry_source,
        "entry_dte_calendar_days": entry_dte,
        "exit_dte_calendar_days": exit_dte,
        "annual_vol_pct": round(annual_vol, 4),
        "volatility_source": vol.get("source"),
        "volatility_confidence": vol.get("confidence"),
        "entry_theoretical_option_price": round(entry_opt, 6),
        "exit_theoretical_option_price": round(exit_opt, 6),
        "synthetic_option_return_pct": round(ret, 4),
        "one_contract_pnl_proxy": round((exit_opt - entry_opt) * 100.0, 2),
        "real_option_marks": False,
        "listed_contract_confirmed": not normalized,
        "interpretation": (
            "normalized ATM/GEX-strike 30DTE theoretical proxy; not a listed historical contract"
            if normalized else
            "same historical indicative GEX strike/expiry repriced theoretically"
        ),
    }

def simulate_trade(
    selected: dict,
    series: dict[str, list[dict]],
    daily_fusion: dict[tuple[str, str], dict],
    feature_day_ticker: dict[tuple[str, str], dict],
) -> dict:
    day = str(selected.get("asof") or selected.get("signal_date") or "")[:10]
    ticker = _ticker(selected)
    direction = _direction(selected)
    entry = _f(selected.get("spot"))
    if not day or not ticker or direction not in {"LONG", "SHORT"} or entry is None or entry <= 0:
        return {"status": "INVALID_OPEN", "signal_date": day, "ticker": ticker, "direction": direction}

    context = selected.get("swing_context") if isinstance(selected.get("swing_context"), dict) else {}
    target = _f(context.get("target_price") or context.get("target_price_mid"))
    invalidation = _valid_invalidation(
        direction, float(entry),
        _f(context.get("invalidation_price") or context.get("opposite_level_price") or context.get("wall_support")),
    )
    future = _series_after(series, ticker, day)
    next_open = _f((future[0] if future else {}).get("open"))
    best = 0.0
    worst = 0.0
    target_hit = False
    exit_date = None
    exit_price = None
    exit_reason = None
    sessions_seen = 0
    ambiguous = False

    for session in future[:MAX_HOLD_SESSIONS]:
        sessions_seen += 1
        session_day = str(session.get("trade_date") or "")[:10]
        close = _f(session.get("close"))
        high = _f(session.get("high"), close)
        low = _f(session.get("low"), close)
        if close is None or close <= 0:
            continue

        if direction == "LONG":
            if high is not None and high > 0:
                best = max(best, _directional_return(direction, float(entry), float(high)))
            if low is not None and low > 0:
                worst = min(worst, _directional_return(direction, float(entry), float(low)))
            if target and high is not None and high >= target:
                target_hit = True
            invalidation_hit = bool(invalidation and low is not None and low <= invalidation)
        else:
            if low is not None and low > 0:
                best = max(best, _directional_return(direction, float(entry), float(low)))
            if high is not None and high > 0:
                worst = min(worst, _directional_return(direction, float(entry), float(high)))
            if target and low is not None and low <= target:
                target_hit = True
            invalidation_hit = bool(invalidation and high is not None and high >= invalidation)

        close_candidates: list[tuple[str, float]] = []
        if invalidation_hit and invalidation:
            close_candidates.append(("PRICE_INVALIDATION", float(invalidation)))

        future_signal = daily_fusion.get((session_day, ticker))
        if future_signal:
            future_dir = _direction(future_signal)
            state = str(future_signal.get("opportunity_state") or future_signal.get("state") or "WATCH").upper()
            feature_row = feature_day_ticker.get((session_day, ticker)) or {}
            signal_price = _f(
                feature_row.get("price"),
                _f(feature_row.get("current_price"), _f(_feature_payload(feature_row).get("price"), close)),
            )
            if future_dir in {"LONG", "SHORT"} and future_dir != direction and signal_price:
                close_candidates.append(("DIRECTION_FLIP", float(signal_price)))
            elif state in CLOSE_STATES and signal_price:
                close_candidates.append((state, float(signal_price)))

        if close_candidates:
            ambiguous = len(close_candidates) > 1
            # Daily historical data cannot order two intraday triggers. Use the
            # worse directional fill so the replay is not optimistically biased.
            reason, chosen = min(
                close_candidates,
                key=lambda item: _directional_return(direction, float(entry), float(item[1])),
            )
            exit_date, exit_price, exit_reason = session_day, chosen, reason
            break

        if sessions_seen >= MAX_HOLD_SESSIONS:
            exit_date, exit_price, exit_reason = session_day, float(close), "TIME_STOP"
            break

    if not exit_date or exit_price is None:
        return {
            "status": "IMMATURE",
            "signal_date": day,
            "ticker": ticker,
            "direction": direction,
            "entry_price": round(float(entry), 6),
            "next_open_price": round(float(next_open), 6) if next_open else None,
            "sessions_seen": sessions_seen,
        }

    ret = _directional_return(direction, float(entry), float(exit_price))
    next_open_ret = (
        _directional_return(direction, float(next_open), float(exit_price))
        if next_open is not None and next_open > 0 else None
    )
    trade = {
        "status": "CLOSED",
        "signal_date": day,
        "ticker": ticker,
        "direction": direction,
        "entry_price": round(float(entry), 6),
        "entry_model": "POINT_IN_TIME_RECORDED_SPOT",
        "next_open_price": round(float(next_open), 6) if next_open else None,
        "next_open_gap_pct": round(
            ((float(next_open) / float(entry) - 1.0) * 100.0), 4
        ) if next_open else None,
        "exit_date": exit_date,
        "exit_price": round(float(exit_price), 6),
        "exit_reason": exit_reason,
        "sessions_held": sessions_seen,
        "underlying_return_pct": round(ret, 4),
        "next_open_entry_return_pct": round(next_open_ret, 4) if next_open_ret is not None else None,
        "best_return_pct": round(best, 4),
        "worst_return_pct": round(worst, 4),
        "target_price": round(float(target), 6) if target else None,
        "target_hit": target_hit,
        "invalidation_price": round(float(invalidation), 6) if invalidation else None,
        "same_day_exit_order_ambiguous": ambiguous,
        "underlying_alpha_score": selected.get("underlying_alpha_score"),
        "tradeability_score": selected.get("tradeability_score"),
        "opportunity_state": selected.get("opportunity_state"),
        "source_scanner_run_id": selected.get("_source_scanner_run_id"),
        "option_expression": {
            k: v for k, v in (selected.get("option_expression") or {}).items()
            if k in {"instrument", "strike", "strike_source", "expiry", "dte", "feasibility_status", "quote_status"}
        },
    }
    trade["option_proxy"] = _option_proxy_for_trade(selected, trade)
    return trade


def _sessions_seen_in_series(series: dict[str, list[dict]], ticker: str, opened_date: str, current_date: str) -> int:
    return sum(
        1
        for row in series.get(ticker, [])
        if opened_date < str(row.get("trade_date") or "")[:10] <= current_date
    )


def _market_sessions_seen(
    series: dict[str, list[dict]],
    opened_date: str,
    current_date: str,
) -> int:
    if not opened_date or not current_date or current_date <= opened_date:
        return 0
    days = {
        str(row.get("trade_date") or "")[:10]
        for rows in series.values()
        for row in rows
        if opened_date < str(row.get("trade_date") or "")[:10] <= current_date
    }
    return len({day for day in days if day})


def _session_count(
    series: dict[str, list[dict]],
    ticker: str,
    opened_date: str,
    current_date: str,
    policy: str,
) -> tuple[int, str]:
    ticker_sessions = _sessions_seen_in_series(series, ticker, opened_date, current_date)
    if ticker_sessions > 0 or str(policy).upper() == "TICKER_PRICE_CACHE_ONLY":
        return ticker_sessions, "TICKER_PRICE_SERIES"
    market_sessions = _market_sessions_seen(series, opened_date, current_date)
    return market_sessions, "GLOBAL_MARKET_CALENDAR_FALLBACK"


def _path_extremes(
    series: dict[str, list[dict]],
    ticker: str,
    opened_date: str,
    exit_date: str,
    direction: str,
    entry_price: float,
) -> tuple[float, float]:
    best = 0.0
    worst = 0.0
    for row in series.get(ticker, []):
        day = str(row.get("trade_date") or "")[:10]
        if not (opened_date < day <= exit_date):
            continue
        high = _f(row.get("high"), _f(row.get("close")))
        low = _f(row.get("low"), _f(row.get("close")))
        if direction == "LONG":
            if high and high > 0:
                best = max(best, _directional_return(direction, entry_price, float(high)))
            if low and low > 0:
                worst = min(worst, _directional_return(direction, entry_price, float(low)))
        else:
            if low and low > 0:
                best = max(best, _directional_return(direction, entry_price, float(low)))
            if high and high > 0:
                worst = min(worst, _directional_return(direction, entry_price, float(high)))
    return round(best, 4), round(worst, 4)


def replay_stateful_alpha_book(
    portfolios: list[dict],
    signal_rows: list[dict],
    feature_rows: list[dict],
    series: dict[str, list[dict]],
    *,
    session_count_policy: str = "TICKER_PRICE_CACHE_ONLY",
) -> list[dict]:
    """Replay Alpha Swing book chronologically using current engine semantics.

    Important: a daily Top-5 row is NOT automatically a new trade. Existing
    positions are maintained first; only actionable ENTER rows can OPEN, and a
    ticker already open cannot be reopened. This mirrors process_alpha_swing_book.

    Exit decisions use the point-in-time sampled feature price, just like the
    live engine. Daily OHLC is used only for post-hoc MFE/MAE diagnostics.
    """
    portfolio_by_day = {str(p.get("asof") or "")[:10]: p for p in portfolios if p.get("asof")}
    days = sorted(portfolio_by_day)
    if not days:
        return []

    feature_by_run, _feature_day = _feature_meta_index(feature_rows)
    fusion_by_run = _fusion_by_run_index(signal_rows)
    open_positions: dict[str, dict] = {}
    completed: list[dict] = []

    for day in days:
        portfolio = portfolio_by_day[day]
        source_run = int(portfolio.get("source_scanner_run_id") or 0)
        selected_by_ticker = {
            _ticker(row): row
            for row in (portfolio.get("selected") or [])
            if _ticker(row)
        }

        # 1) Maintain positions already open, exactly before considering new OPENs.
        for ticker in list(open_positions):
            position = open_positions[ticker]
            signal = fusion_by_run.get((day, source_run, ticker))
            feature = feature_by_run.get((day, source_run, ticker))
            if not signal or not feature:
                # Live engine: disappearing for one run is not a close reason.
                continue

            direction = str(position["direction"])
            features = _feature_payload(feature)
            price = _f(feature.get("price"), _f(feature.get("current_price"), _f(features.get("price"))))
            if price is None or price <= 0:
                continue
            current_direction = _direction(signal)
            state = str(signal.get("opportunity_state") or signal.get("state") or "UNKNOWN").upper()
            sessions_seen, session_source = _session_count(
                series,
                ticker,
                str(position["signal_date"]),
                day,
                session_count_policy,
            )
            position["session_count_source"] = session_source
            ret = _directional_return(direction, float(position["entry_price"]), float(price))
            position["last_price"] = float(price)
            position["last_return_pct"] = ret
            position["sessions_seen"] = sessions_seen
            position["sampled_best_return_pct"] = max(float(position.get("sampled_best_return_pct") or 0.0), ret)
            position["sampled_worst_return_pct"] = min(float(position.get("sampled_worst_return_pct") or 0.0), ret)

            target = _f(position.get("target_price"))
            if target:
                if (direction == "LONG" and price >= target) or (direction == "SHORT" and price <= target):
                    position["target_hit"] = True

            close_reason = None
            invalidation = _valid_invalidation(
                direction,
                float(position["entry_price"]),
                _f(position.get("invalidation_price")),
            )
            if current_direction in {"LONG", "SHORT"} and current_direction != direction:
                close_reason = "DIRECTION_FLIP"
            elif invalidation and (
                (direction == "LONG" and price <= invalidation)
                or (direction == "SHORT" and price >= invalidation)
            ):
                close_reason = "PRICE_INVALIDATION"
            elif state in CLOSE_STATES:
                close_reason = state
            elif sessions_seen >= int(position.get("max_hold_sessions") or MAX_HOLD_SESSIONS):
                close_reason = "TIME_STOP"

            if not close_reason:
                continue

            position["status"] = "CLOSED"
            position["exit_date"] = day
            position["exit_price"] = round(float(price), 6)
            position["exit_reason"] = close_reason
            position["sessions_held"] = sessions_seen
            position["underlying_return_pct"] = round(ret, 4)
            best, worst = _path_extremes(
                series, ticker, str(position["signal_date"]), day,
                direction, float(position["entry_price"]),
            )
            position["best_return_pct"] = best
            position["worst_return_pct"] = worst
            next_open = _f(position.get("next_open_price"))
            position["next_open_entry_return_pct"] = round(
                _directional_return(direction, float(next_open), float(price)), 4
            ) if next_open and next_open > 0 else None
            position["option_proxy"] = _option_proxy_for_trade(position["selected_snapshot"], position)
            completed.append(position)
            del open_positions[ticker]

        # 2) Only actionable current Top-5 ENTERs can create new positions.
        for ticker, selected in selected_by_ticker.items():
            if ticker in open_positions:
                continue
            context = selected.get("swing_context") if isinstance(selected.get("swing_context"), dict) else {}
            state = str(selected.get("opportunity_state") or context.get("state") or "UNKNOWN").upper()
            actionable = bool(context.get("actionable"))
            direction = _direction(selected)
            entry = _f(selected.get("spot"))
            if state != "ENTER" or not actionable or direction not in {"LONG", "SHORT"} or not entry or entry <= 0:
                continue

            future = _series_after(series, ticker, day)
            next_open = _f((future[0] if future else {}).get("open"))
            target = _f(context.get("target_price") or context.get("target_price_mid"))
            invalidation = _valid_invalidation(
                direction, float(entry),
                _f(context.get("invalidation_price") or context.get("opposite_level_price") or context.get("wall_support")),
            )
            source_feature = selected.get("_source_feature_row") if isinstance(selected.get("_source_feature_row"), dict) else {}
            source_payload = _feature_payload(source_feature)
            invalidation_distance_pct = None
            if invalidation is not None and invalidation > 0:
                if direction == "LONG":
                    invalidation_distance_pct = 100.0 * (float(entry) - float(invalidation)) / float(entry)
                else:
                    invalidation_distance_pct = 100.0 * (float(invalidation) - float(entry)) / float(entry)
                if invalidation_distance_pct < 0:
                    invalidation_distance_pct = None
            life = selected.get("lifecycle") if isinstance(selected.get("lifecycle"), dict) else {}
            open_positions[ticker] = {
                "status": "OPEN",
                "signal_date": day,
                "ticker": ticker,
                "direction": direction,
                "entry_price": round(float(entry), 6),
                "entry_model": "POINT_IN_TIME_RECORDED_SPOT",
                "next_open_price": round(float(next_open), 6) if next_open else None,
                "next_open_gap_pct": round((float(next_open) / float(entry) - 1.0) * 100.0, 4) if next_open else None,
                "target_price": round(float(target), 6) if target else None,
                "target_hit": False,
                "invalidation_price": round(float(invalidation), 6) if invalidation else None,
                "max_hold_sessions": int(_f(context.get("max_hold_sessions"), MAX_HOLD_SESSIONS) or MAX_HOLD_SESSIONS),
                "sampled_best_return_pct": 0.0,
                "sampled_worst_return_pct": 0.0,
                "sessions_seen": 0,
                "underlying_alpha_score": selected.get("underlying_alpha_score"),
                "tradeability_score": selected.get("tradeability_score"),
                "opportunity_state": state,
                "source_scanner_run_id": selected.get("_source_scanner_run_id"),
                "entry_diagnostics": {
                    "signal_origin": selected.get("signal_origin"),
                    "intraday_state": selected.get("intraday_state"),
                    "market_policy": selected.get("market_policy"),
                    "market_adjustment": _f(selected.get("market_adjustment")),
                    "countertrend_override": bool(selected.get("countertrend_override")),
                    "intraday_blocks": list(selected.get("intraday_blocks") or []),
                    "intraday_adverse": list(selected.get("intraday_adverse") or []),
                    "core_score": _f(selected.get("core_score")),
                    "gex_score": _f(selected.get("gex_score")),
                    "overextension_score": _f(selected.get("overextension_score")),
                    "invalidation_distance_pct": round(float(invalidation_distance_pct), 4) if invalidation_distance_pct is not None else None,
                    "market_regime": source_payload.get("market_regime") or source_feature.get("market_regime"),
                    "direction_consistency_pct": _f(life.get("direction_consistency_pct")),
                    "same_direction_observations": int(life.get("same_direction_observations") or 0),
                    "lifecycle_observations": int(life.get("observations") or 0),
                    "quality_gate": selected.get("quality_gate") or {},
                },
                "option_expression": {
                    k: v for k, v in (selected.get("option_expression") or {}).items()
                    if k in {"instrument", "strike", "strike_source", "expiry", "dte", "feasibility_status", "quote_status"}
                },
                "selected_snapshot": selected,
            }

    # Preserve still-open historical positions instead of inventing a terminal close.
    for ticker, position in sorted(open_positions.items()):
        position["status"] = "OPEN_AT_HISTORY_END"
        sessions_held, session_source = _session_count(
            series,
            ticker,
            str(position["signal_date"]),
            days[-1],
            session_count_policy,
        )
        position["sessions_held"] = sessions_held
        position["session_count_source"] = session_source
        last_rows = [
            row for row in series.get(ticker, [])
            if str(position["signal_date"]) < str(row.get("trade_date") or "")[:10] <= days[-1]
        ]
        if last_rows:
            last = last_rows[-1]
            close = _f(last.get("close"))
            if close and close > 0:
                position["last_price"] = round(float(close), 6)
                position["last_return_pct"] = round(
                    _directional_return(str(position["direction"]), float(position["entry_price"]), float(close)), 4
                )
                best, worst = _path_extremes(
                    series, ticker, str(position["signal_date"]), str(last.get("trade_date") or "")[:10],
                    str(position["direction"]), float(position["entry_price"]),
                )
                position["best_return_pct"] = best
                position["worst_return_pct"] = worst
        completed.append(position)

    return sorted(completed, key=lambda row: (str(row.get("signal_date") or ""), str(row.get("ticker") or "")))


def _metrics(trades: list[dict]) -> dict:
    closed = [row for row in trades if row.get("status") == "CLOSED"]
    returns = [float(row["underlying_return_pct"]) for row in closed]
    next_open_returns = [
        float(row["next_open_entry_return_pct"]) for row in closed
        if _f(row.get("next_open_entry_return_pct")) is not None
    ]
    option_rows = [
        row["option_proxy"] for row in closed
        if str((row.get("option_proxy") or {}).get("status") or "").startswith("EVALUATED")
    ]
    option_returns = [float(row["synthetic_option_return_pct"]) for row in option_rows]
    option_dollars = [float(row["one_contract_pnl_proxy"]) for row in option_rows]
    return {
        "opened": len(trades),
        "closed": len(closed),
        "immature": sum(row.get("status") in {"IMMATURE", "OPEN_AT_HISTORY_END"} for row in trades),
        "win_rate_pct": round(100.0 * sum(x > 0 for x in returns) / len(returns), 2) if returns else None,
        "avg_underlying_return_pct": round(mean(returns), 4) if returns else None,
        "median_underlying_return_pct": round(median(returns), 4) if returns else None,
        "underlying_profit_factor": _profit_factor(returns),
        "trade_sequence_max_drawdown_pct": _trade_sequence_drawdown(returns),
        "avg_sessions_held": round(mean(float(row["sessions_held"]) for row in closed), 3) if closed else None,
        "target_hit_rate_pct": round(100.0 * sum(bool(row.get("target_hit")) for row in closed) / len(closed), 2) if closed else None,
        "exit_reasons": dict(Counter(str(row.get("exit_reason") or "UNKNOWN") for row in closed)),
        "next_open_sensitivity": {
            "sample": len(next_open_returns),
            "avg_return_pct": round(mean(next_open_returns), 4) if next_open_returns else None,
            "profit_factor": _profit_factor(next_open_returns),
        },
        "synthetic_option_proxy": {
            "evaluable_trades": len(option_rows),
            "coverage_pct": round(100.0 * len(option_rows) / len(closed), 2) if closed else None,
            "win_rate_pct": round(100.0 * sum(x > 0 for x in option_returns) / len(option_returns), 2) if option_returns else None,
            "avg_return_pct": round(mean(option_returns), 4) if option_returns else None,
            "median_return_pct": round(median(option_returns), 4) if option_returns else None,
            "profit_factor": _profit_factor(option_returns),
            "one_contract_each_gross_pnl_proxy": round(sum(option_dollars), 2) if option_dollars else None,
            "proxy_mode_counts": dict(Counter(str(row.get("proxy_mode") or "UNKNOWN") for row in option_rows)),
            "normalized_synthetic_trades": sum(str(row.get("proxy_mode") or "") == "NORMALIZED_SYNTHETIC" for row in option_rows),
            "historical_expression_trades": sum(str(row.get("proxy_mode") or "") == "GEX_ALIGNED_HISTORICAL_EXPRESSION" for row in option_rows),
            "real_option_marks": False,
        },
    }


def _series_stats(series: dict[str, list[dict]]) -> dict:
    return {
        "tickers": len(series),
        "session_rows": sum(len(rows) for rows in series.values()),
        "first_date": min(
            (str(row.get("trade_date") or "")[:10] for rows in series.values() for row in rows if row.get("trade_date")),
            default=None,
        ),
        "last_date": max(
            (str(row.get("trade_date") or "")[:10] for rows in series.values() for row in rows if row.get("trade_date")),
            default=None,
        ),
    }


def _row_ohlc_invalid(row: dict) -> bool:
    close = _f(row.get("close"))
    open_px = _f(row.get("open"))
    high = _f(row.get("high"))
    low = _f(row.get("low"))
    if close is None or close <= 0:
        return False
    if high is None or low is None:
        return False
    if high < low:
        return True
    if open_px is not None and (open_px < low or open_px > high):
        return True
    return close < low or close > high


def _has_adjustment_metadata(row: dict) -> bool:
    adjusted = _f(row.get("adjusted_close"))
    split = _f(row.get("split_factor"))
    adjustment_json = row.get("adjustment_json")
    return bool(
        (adjusted is not None and adjusted > 0)
        or (split is not None and split > 0 and abs(split - 1.0) > 1e-9)
        or adjustment_json not in (None, "", "{}", "{}\n")
    )


def _trusted_reverse_path_series(
    series: dict[str, list[dict]],
    *,
    discontinuity_pct: float = 80.0,
) -> tuple[dict[str, list[dict]], dict]:
    """Create a conservative Reverse-only price lane.

    Raw history is never modified. Invalid OHLC rows are degraded to close-only
    evidence so impossible intraday highs/lows cannot manufacture a threshold
    hit. Adjusted close is preferred when available. Large close-to-close jumps
    without an adjusted price start a new continuity segment, so no historical
    outcome window is allowed to cross an unresolved corporate-action boundary.
    """
    trusted: dict[str, list[dict]] = {}
    downgraded = 0
    adjusted_scaled = 0
    continuity_barriers = 0
    known_corporate_barriers = 0
    ambiguous_barriers = 0
    dropped_nonpositive = 0
    by_source: Counter = Counter()
    invalid_by_source: Counter = Counter()
    barrier_examples: list[dict] = []
    invalid_examples: list[dict] = []

    for ticker, rows in series.items():
        ordered = sorted(rows, key=lambda row: str(row.get("trade_date") or ""))
        out: list[dict] = []
        previous: dict | None = None
        segment = 0
        for raw in ordered:
            item = dict(raw)
            source = str(item.get("path_source") or item.get("source") or "UNKNOWN")
            by_source[source] += 1
            close = _f(item.get("close"))
            if close is None or close <= 0:
                dropped_nonpositive += 1
                previous = None
                segment += 1
                continue

            adjusted = _f(item.get("adjusted_close"))
            if adjusted is not None and adjusted > 0 and close > 0:
                ratio = float(adjusted) / float(close)
                for key in ("open", "high", "low", "close"):
                    value = _f(item.get(key))
                    if value is not None and value > 0:
                        item[key] = float(value) * ratio
                item["raw_close"] = float(close)
                item["close"] = float(adjusted)
                item["reverse_price_adjustment"] = "ADJUSTED_CLOSE_SCALE"
                adjusted_scaled += 1

            if _row_ohlc_invalid(item):
                invalid_by_source[source] += 1
                downgraded += 1
                if len(invalid_examples) < 40:
                    invalid_examples.append({
                        "ticker": ticker,
                        "trade_date": str(item.get("trade_date") or "")[:10],
                        "source": source,
                        "open": _f(item.get("open")),
                        "high": _f(item.get("high")),
                        "low": _f(item.get("low")),
                        "close": _f(item.get("close")),
                    })
                close_value = float(_f(item.get("close")) or close)
                item["open"] = close_value
                item["high"] = close_value
                item["low"] = close_value
                item["close"] = close_value
                item["reverse_data_quality"] = "CLOSE_ONLY_INVALID_OHLC"

            trusted_close = _f(item.get("close"))
            if previous is not None and trusted_close is not None:
                prev_close = _f(previous.get("close"))
                if prev_close is not None and prev_close > 0:
                    change = (float(trusted_close) / float(prev_close) - 1.0) * 100.0
                    if abs(change) >= float(discontinuity_pct):
                        has_adjusted_price = _f(item.get("adjusted_close")) is not None
                        if not has_adjusted_price:
                            segment += 1
                            continuity_barriers += 1
                            known = _has_adjustment_metadata(item) or _has_adjustment_metadata(previous)
                            if known:
                                known_corporate_barriers += 1
                                reason = "CORPORATE_ACTION_METADATA_WITHOUT_ADJUSTED_PRICE"
                            else:
                                ambiguous_barriers += 1
                                reason = "AMBIGUOUS_LARGE_JUMP_NO_ADJUSTMENT"
                            item["reverse_continuity_barrier"] = True
                            item["reverse_continuity_reason"] = reason
                            if len(barrier_examples) < 40:
                                barrier_examples.append({
                                    "ticker": ticker,
                                    "from_date": str(previous.get("trade_date") or "")[:10],
                                    "to_date": str(item.get("trade_date") or "")[:10],
                                    "from_close": round(float(prev_close), 6),
                                    "to_close": round(float(trusted_close), 6),
                                    "simple_change_pct": round(change, 4),
                                    "reason": reason,
                                    "source": source,
                                })

            item["reverse_segment"] = segment
            out.append(item)
            previous = item
        trusted[ticker] = out

    return trusted, {
        "mode": "TRUSTED_REVERSE_DATA_LANE_V1",
        "discontinuity_pct": float(discontinuity_pct),
        "raw_rows": sum(by_source.values()),
        "trusted_rows": sum(len(rows) for rows in trusted.values()),
        "dropped_nonpositive_rows": dropped_nonpositive,
        "invalid_ohlc_downgraded_to_close": downgraded,
        "adjusted_close_scaled_rows": adjusted_scaled,
        "continuity_barriers": continuity_barriers,
        "known_corporate_action_barriers": known_corporate_barriers,
        "ambiguous_large_jump_barriers": ambiguous_barriers,
        "rows_by_source": dict(by_source),
        "invalid_ohlc_by_source": dict(invalid_by_source),
        "invalid_ohlc_examples": invalid_examples,
        "continuity_barrier_examples": barrier_examples,
        "policy": {
            "raw_history_preserved": True,
            "invalid_ohlc_uses_close_only": True,
            "unresolved_large_jump_breaks_reverse_window": True,
            "future_data_used_only_as_outcome_label": True,
            "live_execution_unchanged": True,
        },
    }


def _same_segment_future_rows(
    rows: list[dict],
    signal_date: str,
    horizon_sessions: int,
) -> list[dict]:
    """Future sessions restricted to the signal date's trusted continuity segment."""
    ordered = sorted(rows, key=lambda row: str(row.get("trade_date") or ""))
    has_segments = any("reverse_segment" in row for row in ordered)
    if not has_segments:
        return [
            row for row in ordered
            if str(row.get("trade_date") or "")[:10] > signal_date
        ][: max(1, int(horizon_sessions))]

    anchor = None
    for row in ordered:
        day = str(row.get("trade_date") or "")[:10]
        if day <= signal_date:
            anchor = row
        else:
            break
    if anchor is None:
        return []
    segment = anchor.get("reverse_segment")
    return [
        row for row in ordered
        if str(row.get("trade_date") or "")[:10] > signal_date
        and row.get("reverse_segment") == segment
    ][: max(1, int(horizon_sessions))]


def _price_path_integrity(series: dict[str, list[dict]]) -> dict:
    """Audit stored price paths without changing economic labels.

    Large observed jumps are review flags, not automatic exclusions: genuine
    small-cap moves can be extreme, while unadjusted splits/corporate actions can
    look identical when the source does not expose adjustment metadata.
    """
    rows_total = 0
    nonpositive = 0
    invalid_ohlc = 0
    adjusted_rows = 0
    split_rows = 0
    adjustment_meta_rows = 0
    jump_counts = {"ge_25pct": 0, "ge_50pct": 0, "ge_80pct": 0}
    top_jumps: list[dict] = []
    rows_by_source: Counter = Counter()
    invalid_by_source: Counter = Counter()
    invalid_examples: list[dict] = []

    for ticker, rows in series.items():
        ordered = sorted(rows, key=lambda row: str(row.get("trade_date") or ""))
        previous = None
        for row in ordered:
            rows_total += 1
            source = str(row.get("path_source") or row.get("source") or "UNKNOWN")
            rows_by_source[source] += 1
            close = _f(row.get("close"))
            open_px = _f(row.get("open"))
            high = _f(row.get("high"))
            low = _f(row.get("low"))
            adjusted = _f(row.get("adjusted_close"))
            split = _f(row.get("split_factor"))
            adjustment_json = row.get("adjustment_json")

            if adjusted is not None and adjusted > 0:
                adjusted_rows += 1
            if split is not None and split > 0 and abs(split - 1.0) > 1e-9:
                split_rows += 1
            if adjustment_json not in (None, "", "{}", "{}\n"):
                adjustment_meta_rows += 1
            if close is None or close <= 0:
                nonpositive += 1
                previous = None
                continue
            if _row_ohlc_invalid(row):
                invalid_ohlc += 1
                invalid_by_source[source] += 1
                if len(invalid_examples) < 40:
                    invalid_examples.append({
                        "ticker": str(ticker),
                        "trade_date": str(row.get("trade_date") or "")[:10],
                        "source": source,
                        "open": open_px,
                        "high": high,
                        "low": low,
                        "close": close,
                    })

            if previous is not None:
                prev_close = _f(previous.get("close"))
                if prev_close is not None and prev_close > 0:
                    change = (float(close) / float(prev_close) - 1.0) * 100.0
                    abs_change = abs(change)
                    if abs_change >= 25.0:
                        jump_counts["ge_25pct"] += 1
                    if abs_change >= 50.0:
                        jump_counts["ge_50pct"] += 1
                    if abs_change >= 80.0:
                        jump_counts["ge_80pct"] += 1
                    if abs_change >= 50.0:
                        item = {
                            "ticker": str(ticker),
                            "from_date": str(previous.get("trade_date") or "")[:10],
                            "to_date": str(row.get("trade_date") or "")[:10],
                            "from_close": round(float(prev_close), 6),
                            "to_close": round(float(close), 6),
                            "simple_change_pct": round(change, 4),
                            "from_source": previous.get("path_source") or previous.get("source"),
                            "to_source": row.get("path_source") or row.get("source"),
                            "adjusted_close_available": adjusted is not None,
                            "split_factor": split,
                            "adjustment_json": adjustment_json,
                        }
                        top_jumps.append(item)
            previous = row

    top_jumps.sort(key=lambda row: abs(float(row.get("simple_change_pct") or 0.0)), reverse=True)
    metadata_coverage = max(adjusted_rows, split_rows, adjustment_meta_rows)
    return {
        "rows": rows_total,
        "nonpositive_close_rows": nonpositive,
        "invalid_ohlc_rows": invalid_ohlc,
        "adjusted_close_rows": adjusted_rows,
        "split_factor_rows": split_rows,
        "adjustment_metadata_rows": adjustment_meta_rows,
        "adjustment_metadata_coverage_pct": round(
            100.0 * metadata_coverage / rows_total, 3
        ) if rows_total else None,
        "observed_close_jumps": jump_counts,
        "rows_by_source": dict(rows_by_source),
        "invalid_ohlc_by_source": dict(invalid_by_source),
        "invalid_ohlc_examples": invalid_examples,
        "top_large_jumps": top_jumps[:40],
        "policy": {
            "diagnostic_only": True,
            "large_jump_not_auto_excluded": True,
            "raw_ohlc_without_adjustment_metadata_is_not_proof_of_bad_data": True,
            "corporate_action_review_required_for_large_discontinuities": True,
        },
    }


def _feature_point_close_series(feature_rows: list[dict]) -> dict[str, list[dict]]:
    """Build a conservative daily close-path fallback from point-in-time features.

    This is used only for Reverse/forensic path coverage. It never replaces real
    OHLC in execution or exit simulation. When several scanner runs exist for a
    ticker/day, the latest run is used.
    """
    latest: dict[tuple[str, str], dict] = {}
    for row in feature_rows:
        day, ticker = _day(row), _ticker(row)
        if not day or not ticker:
            continue
        price = _f(row.get("price"), _f(row.get("current_price"), _f(_feature_payload(row).get("price"))))
        if price is None or price <= 0:
            continue
        key = (ticker, day)
        run_id = int(row.get("source_scanner_run_id") or 0)
        old = latest.get(key)
        if old is None or run_id >= int(old.get("source_scanner_run_id") or 0):
            latest[key] = {
                "trade_date": day,
                "close": float(price),
                "source_scanner_run_id": run_id,
                "path_source": "FEATURE_POINT_IN_TIME_CLOSE",
            }

    out: dict[str, list[dict]] = defaultdict(list)
    for (ticker, _day_key), row in latest.items():
        out[ticker].append(row)
    for ticker in out:
        out[ticker].sort(key=lambda row: str(row.get("trade_date") or ""))
    return dict(out)


def _merge_reverse_path_series(
    primary: dict[str, list[dict]],
    fallback: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Fill missing ticker/dates for diagnostics while preserving real OHLC."""
    merged: dict[str, list[dict]] = {}
    for ticker in sorted(set(primary) | set(fallback)):
        by_day: dict[str, dict] = {}
        for row in fallback.get(ticker, []):
            day = str(row.get("trade_date") or "")[:10]
            if day:
                by_day[day] = dict(row)
        for row in primary.get(ticker, []):
            day = str(row.get("trade_date") or "")[:10]
            if day:
                item = dict(row)
                item.setdefault("path_source", "PRIMARY_OHLC")
                by_day[day] = item
        merged[ticker] = [by_day[day] for day in sorted(by_day)]
    return merged


def _reverse_canonical_basis(
    canonical: dict[tuple[str, str], dict],
    series: dict[str, list[dict]],
    *,
    horizon_sessions: int = 5,
    top_n: int = 5,
) -> dict:
    """Stable basis for outcomes that do not depend on the current bot version."""
    target_pct = float(getattr(config, "MARKET_OUTCOME_AUDITOR_TARGET_PCT", 5.0))
    definition = {
        "schema_version": 3,
        "algorithm": "TRUSTED_REVERSE_CANONICAL_V3_SEGMENTED_DATA_INTEGRITY",
        "target_pct": target_pct,
        "threshold_horizon_sessions": int(horizon_sessions),
        "top_horizons": list(HORIZONS),
        "top_n": int(top_n),
    }
    digest = hashlib.sha256()
    digest.update(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    feature_keys = (
        "momentum_5d_pct", "atr_pct", "call_accumulation_score", "put_accumulation_score",
        "momentum_score", "rvol", "change_percent", "otm_call_volume_z", "otm_put_volume_z",
        "relative_strength_vs_market", "relative_strength_vs_sector", "market_breadth_score",
        "sector_breadth_score", "relationship_lead_lag_score", "total_gex_change_pct",
    )
    for (day, ticker), row in sorted(canonical.items()):
        features = _feature_payload(row)
        item = {
            "day": day,
            "ticker": ticker,
            "source_scanner_run_id": int(row.get("source_scanner_run_id") or 0),
            "entry": _f(row.get("outcome_entry_price"), _f(row.get("price"), _f(row.get("current_price")))),
            "core_direction": row.get("core_direction"),
            "core_score": _f(row.get("core_score")),
            "core_probability_pct": _f(row.get("core_probability_pct")),
            "gex_direction": row.get("gex_direction"),
            "gex_intent": row.get("gex_intent"),
            "sector": row.get("sector") or features.get("sector"),
            "industry": row.get("industry") or features.get("industry"),
            "features": {key: features.get(key) for key in feature_keys},
        }
        digest.update(json.dumps(item, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")

    series_rows = 0
    first_date = None
    last_date = None
    for ticker in sorted(series):
        for row in series[ticker]:
            day = str(row.get("trade_date") or "")[:10]
            if not day:
                continue
            series_rows += 1
            first_date = day if first_date is None or day < first_date else first_date
            last_date = day if last_date is None or day > last_date else last_date
            item = (
                ticker,
                day,
                _f(row.get("open")),
                _f(row.get("high")),
                _f(row.get("low")),
                _f(row.get("close")),
                str(row.get("path_source") or ""),
                row.get("reverse_segment"),
                bool(row.get("reverse_continuity_barrier")),
                str(row.get("reverse_data_quality") or ""),
            )
            digest.update(json.dumps(item, default=str, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")

    return {
        "definition": definition,
        "fingerprint": digest.hexdigest(),
        "canonical_rows": len(canonical),
        "series_rows": series_rows,
        "first_date": first_date,
        "last_date": last_date,
    }


def _load_or_build_reverse_canonical_cache(
    canonical: dict[tuple[str, str], dict],
    series: dict[str, list[dict]],
    ranks: dict,
    universe_sizes: dict[str, int],
    universe_meta: dict[str, dict],
    *,
    horizon_sessions: int = 5,
    top_n: int = 5,
) -> tuple[list[dict], dict, dict]:
    """Persist future-outcome labels independently from current TMAD logic."""
    basis = _reverse_canonical_basis(
        canonical,
        series,
        horizon_sessions=horizon_sessions,
        top_n=top_n,
    )
    out_dir = Path(config.OUTPUT_DIR) / "historical_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reverse_canonical_cache_v1.json"

    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (cached.get("basis") or {}) == basis:
                return (
                    list(cached.get("movement_candidates") or []),
                    dict(cached.get("reverse_alpha_base") or {}),
                    {
                        "mode": "REUSED",
                        "basis": basis,
                        "path": str(path),
                        "movement_candidates": len(cached.get("movement_candidates") or []),
                        "top_opportunities": len((cached.get("reverse_alpha_base") or {}).get("top_opportunities") or []),
                    },
                )
        except Exception:
            pass

    movement_candidates = [
        {key: value for key, value in item.items() if key != "row"}
        for item in _movement_candidates_from_prices(
            canonical,
            series,
            horizon_sessions=horizon_sessions,
        )
    ]
    reverse_alpha_base = build_daily_reverse_alpha(
        canonical,
        {},
        ranks,
        universe_sizes,
        set(),
        series,
        eod_selection_rows=[],
        top_n=top_n,
        detail_days=10000,
        universe_metadata=universe_meta,
    )
    payload = {
        "schema_version": 1,
        "basis": basis,
        "movement_candidates": movement_candidates,
        "reverse_alpha_base": reverse_alpha_base,
    }
    # Streaming encoder prevents an additional multi-hundred-MB json.dumps
    # allocation late in FULL replay. Atomic replacement keeps a prior cache
    # reusable if serialization fails or the process is interrupted.
    atomic_stream_json(path, payload)
    return (
        movement_candidates,
        reverse_alpha_base,
        {
            "mode": "REBUILT",
            "basis": basis,
            "path": str(path),
            "movement_candidates": len(movement_candidates),
            "top_opportunities": len(reverse_alpha_base.get("top_opportunities") or []),
        },
    )


def _reclassify_reverse_alpha_base(
    base: dict,
    signal_index: dict[tuple[str, str, str], list[dict]],
    alerts: set[tuple[str, str, str]],
    eod_selection_rows: list[dict],
) -> dict:
    """Apply current TMAD stages to frozen future-outcome labels."""
    eod = {
        (
            str(row.get("asof") or row.get("signal_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
            str(row.get("direction") or "").lower(),
        )
        for row in eod_selection_rows
        if str(row.get("ticker") or "")
    }
    details: list[dict] = []
    for source in base.get("top_opportunities") or []:
        item = dict(source)
        day = str(item.get("date") or "")[:10]
        ticker = str(item.get("ticker") or "").upper()
        direction = str(item.get("direction") or "").lower()
        sigs = signal_index.get((day, ticker, direction), [])
        path = _stage_path(sigs, direction)
        alerted = (day, ticker, direction) in alerts
        core_direction = str((item.get("core_snapshot") or {}).get("direction") or "").lower()
        opposite_core = core_direction in {"long", "short"} and core_direction != direction
        item["alerted"] = alerted
        item["selected_in_eod_alpha"] = (day, ticker, direction) in eod
        item["tmad_path"] = path
        item["miss_reason"] = _miss_reason(path, alerted, opposite_core)
        details.append(item)

    daily_summary: list[dict] = []
    base_summaries = base.get("daily_summary") or []
    for summary in base_summaries:
        day = str(summary.get("date") or "")[:10]
        horizon = int(summary.get("horizon_sessions") or 0)
        subset = [
            row for row in details
            if str(row.get("date") or "")[:10] == day
            and int(row.get("horizon_sessions") or 0) == horizon
        ]
        item = dict(summary)
        item["alert_recall_pct"] = round(
            100.0 * sum(bool(row.get("alerted")) for row in subset) / len(subset), 2
        ) if subset else None
        item["eod_alpha_recall_pct"] = round(
            100.0 * sum(bool(row.get("selected_in_eod_alpha")) for row in subset) / len(subset), 2
        ) if subset else None
        daily_summary.append(item)

    misses = Counter(str(row.get("miss_reason") or "UNKNOWN") for row in details if not row.get("alerted"))
    by_horizon = {}
    for horizon in HORIZONS:
        subset = [row for row in details if int(row.get("horizon_sessions") or 0) == int(horizon)]
        by_horizon[str(horizon)] = {
            "sample": len(subset),
            "alert_recall_pct": round(
                100.0 * sum(bool(row.get("alerted")) for row in subset) / len(subset), 2
            ) if subset else None,
            "eod_alpha_recall_pct": round(
                100.0 * sum(bool(row.get("selected_in_eod_alpha")) for row in subset) / len(subset), 2
            ) if subset else None,
            "avg_hindsight_return_pct": round(
                mean(float(row.get("realized_return_pct") or 0.0) for row in subset), 4
            ) if subset else None,
        }

    feature_groups: dict[str, list[float]] = defaultdict(list)
    for item in details:
        if item.get("alerted"):
            continue
        for key, value in (item.get("point_in_time_features") or {}).items():
            number = _f(value)
            if number is not None:
                feature_groups[key].append(number)

    out = dict(base)
    out["top_opportunities"] = details
    out["daily_summary"] = daily_summary
    out["evaluated_top_opportunities"] = len(details)
    out["alerted_top_opportunities"] = sum(bool(row.get("alerted")) for row in details)
    out["alert_recall_pct"] = round(
        100.0 * out["alerted_top_opportunities"] / len(details), 2
    ) if details else None
    out["eod_alpha_selected_top_opportunities"] = sum(
        bool(row.get("selected_in_eod_alpha")) for row in details
    )
    out["eod_alpha_recall_pct"] = round(
        100.0 * out["eod_alpha_selected_top_opportunities"] / len(details), 2
    ) if details else None
    out["miss_reasons"] = dict(misses)
    out["by_horizon"] = by_horizon
    out["missed_feature_profile"] = {
        key: {"sample": len(values), "avg": round(mean(values), 4)}
        for key, values in sorted(feature_groups.items())
        if values
    }
    out["current_bot_reclassified_from_canonical_outcomes"] = True
    return out


def _fixed_bucket(value: float | None, cuts: tuple[float, ...], labels: tuple[str, ...]) -> str:
    if value is None:
        return "UNKNOWN"
    for idx, cut in enumerate(cuts):
        if value <= cut:
            return labels[idx]
    return labels[-1]


def _forensic_group_metrics(rows: list[dict]) -> dict:
    closed = [
        row for row in rows
        if row.get("status") == "CLOSED" and _f(row.get("underlying_return_pct")) is not None
    ]
    values = [float(row["underlying_return_pct"]) for row in closed]
    return {
        "closed": len(closed),
        "win_rate_pct": round(100.0 * sum(v > 0 for v in values) / len(values), 2) if values else None,
        "avg_return_pct": round(mean(values), 4) if values else None,
        "median_return_pct": round(median(values), 4) if values else None,
        "profit_factor": _profit_factor(values),
        "return_sum_points": round(sum(values), 4) if values else None,
        "avg_sessions_held": round(mean(float(row.get("sessions_held") or 0) for row in closed), 3) if closed else None,
    }


def _trade_forensics(trades: list[dict]) -> dict:
    """Explain what the current replayed bot got right/wrong without fitting a rule."""
    closed = [row for row in trades if row.get("status") == "CLOSED"]
    dimensions: dict[str, dict[str, list[dict]]] = {
        "direction": defaultdict(list),
        "exit_reason": defaultdict(list),
        "market_regime": defaultdict(list),
        "market_policy": defaultdict(list),
        "invalidation_distance_bucket": defaultdict(list),
        "overextension_bucket": defaultdict(list),
        "direction_consistency_bucket": defaultdict(list),
    }
    invalidation_fast = 0
    invalidation_total = 0

    for row in closed:
        diag = row.get("entry_diagnostics") if isinstance(row.get("entry_diagnostics"), dict) else {}
        dimensions["direction"][str(row.get("direction") or "UNKNOWN")].append(row)
        dimensions["exit_reason"][str(row.get("exit_reason") or "UNKNOWN")].append(row)
        dimensions["market_regime"][str(diag.get("market_regime") or "UNKNOWN")].append(row)
        dimensions["market_policy"][str(diag.get("market_policy") or "UNKNOWN")].append(row)

        inv_dist = _f(diag.get("invalidation_distance_pct"))
        dimensions["invalidation_distance_bucket"][
            _fixed_bucket(inv_dist, (2.0, 4.0, 6.0), ("<=2%", "2-4%", "4-6%", ">6%"))
        ].append(row)

        over = _f(diag.get("overextension_score"))
        dimensions["overextension_bucket"][
            _fixed_bucket(over, (25.0, 50.0, 75.0), ("<=25", "25-50", "50-75", ">75"))
        ].append(row)

        persistence = _f(diag.get("direction_consistency_pct"))
        dimensions["direction_consistency_bucket"][
            _fixed_bucket(persistence, (50.0, 70.0, 85.0), ("<=50%", "50-70%", "70-85%", ">85%"))
        ].append(row)

        if str(row.get("exit_reason") or "") == "PRICE_INVALIDATION":
            invalidation_total += 1
            if int(row.get("sessions_held") or 0) <= 2:
                invalidation_fast += 1

    grouped = {
        dimension: {
            key: _forensic_group_metrics(items)
            for key, items in sorted(groups.items())
        }
        for dimension, groups in dimensions.items()
    }
    return {
        "overall": _forensic_group_metrics(closed),
        "dimensions": grouped,
        "price_invalidation": {
            "closed": invalidation_total,
            "within_2_sessions": invalidation_fast,
            "within_2_sessions_pct": round(
                100.0 * invalidation_fast / invalidation_total, 2
            ) if invalidation_total else None,
        },
        "methodology": {
            "future_data_used_for_grouping": False,
            "future_returns_used_only_as_outcomes": True,
            "fixed_bins_not_pnl_fitted": True,
        },
    }


def _scope_trades(trades: list[dict], universe_meta: dict[str, dict]) -> None:
    for trade in trades:
        scope = str(
            (universe_meta.get(str(trade.get("signal_date") or "")) or {}).get("universe_scope")
            or "OBSERVED_FEATURE_SUBSET"
        )
        trade["universe_scope"] = scope
        trade["full_deep"] = scope == "FULL_SCANNER_DEEP"


def _selection_rows_from_trades(trades: list[dict]) -> list[dict]:
    return [
        {
            "asof": trade["signal_date"],
            "ticker": trade["ticker"],
            "direction": trade["direction"],
            "underlying_alpha_score": trade.get("underlying_alpha_score"),
        }
        for trade in trades
        if trade.get("status") in {"CLOSED", "OPEN_AT_HISTORY_END"}
    ]


def _selection_compression_diagnostics(
    signal_rows: list[dict],
    portfolios: list[dict],
    trades: list[dict],
) -> dict:
    actionable_enters: set[tuple[str, int, str, str]] = set()
    for row in signal_rows:
        if str(row.get("layer") or "").upper() != "FUSION":
            continue
        state = str(row.get("opportunity_state") or row.get("state") or "").upper()
        if state != "ENTER" or not bool(row.get("actionable")):
            continue
        day = _day(row)
        run_id = int(row.get("source_scanner_run_id") or 0)
        ticker = _ticker(row)
        direction = _direction(row)
        if day and run_id > 0 and ticker and direction in {"LONG", "SHORT"}:
            actionable_enters.add((day, run_id, ticker, direction))

    selected_total = 0
    selected_state_counts: Counter = Counter()
    selected_enter_keys: set[tuple[str, int, str, str]] = set()
    selected_keys: set[tuple[str, int, str, str]] = set()
    for portfolio in portfolios:
        day = str(portfolio.get("asof") or "")[:10]
        run_id = int(portfolio.get("source_scanner_run_id") or 0)
        for selected in portfolio.get("selected") or []:
            ticker = _ticker(selected)
            direction = _direction(selected)
            state = str(
                selected.get("opportunity_state")
                or (selected.get("swing_context") or {}).get("state")
                or "UNKNOWN"
            ).upper()
            selected_total += 1
            selected_state_counts[state] += 1
            key = (day, run_id, ticker, direction)
            selected_keys.add(key)
            actionable = bool((selected.get("swing_context") or {}).get("actionable"))
            if state == "ENTER" and actionable:
                selected_enter_keys.add(key)

    trade_keys = {
        (
            str(trade.get("signal_date") or "")[:10],
            int(trade.get("source_scanner_run_id") or 0),
            _ticker(trade),
            _direction(trade),
        )
        for trade in trades
        if trade.get("status") in {"CLOSED", "OPEN_AT_HISTORY_END"}
    }
    omitted = actionable_enters - selected_enter_keys
    selected_but_not_opened = selected_enter_keys - trade_keys
    return {
        "fusion_enter_actionable_occurrences": len(actionable_enters),
        "portfolio_selected_slots": selected_total,
        "portfolio_selected_state_counts": dict(selected_state_counts),
        "selected_actionable_enter_occurrences": len(selected_enter_keys),
        "actionable_enter_selection_rate_pct": round(
            100.0 * len(selected_enter_keys) / len(actionable_enters), 2
        ) if actionable_enters else None,
        "actionable_enters_omitted_by_top5": len(omitted),
        "actual_open_trades": len(trade_keys),
        "selected_enters_not_opened": len(selected_but_not_opened),
        "days_with_actionable_enter": len({key[0] for key in actionable_enters}),
        "days_with_selected_actionable_enter": len({key[0] for key in selected_enter_keys}),
        "interpretation": (
            "diagnoses compression from FUSION ENTER candidates -> Top-5 portfolio -> stateful OPENs"
        ),
    }


def _reverse_capture_for_trade_book(
    trades: list[dict],
    threshold_rows: list[dict],
    top_rows: list[dict],
    series: dict[str, list[dict]],
) -> dict:
    """Re-label Reverse opportunities against an arbitrary reconstructed OPEN book."""
    trade_index = {
        (
            str(trade.get("signal_date") or "")[:10],
            _ticker(trade),
            _direction(trade),
        ): trade
        for trade in trades
        if trade.get("status") in {"CLOSED", "OPEN_AT_HISTORY_END"}
    }

    threshold_tagged: list[dict] = []
    for row in threshold_rows:
        ticker = str(row.get("ticker") or "").upper()
        direction = str(row.get("direction") or "").upper()
        source_dates = [
            str(day or "")[:10]
            for day in (row.get("source_dates") or [row.get("signal_date")])
            if str(day or "")[:10]
        ]
        matched_date = next(
            (
                day for day in source_dates
                if (day, ticker, direction) in trade_index
            ),
            None,
        )
        item = dict(row)
        item["captured"] = bool(matched_date)
        item["matched_trade_signal_date"] = matched_date
        if matched_date:
            trade = trade_index[(matched_date, ticker, direction)]
            item["trade_underlying_return_pct"] = trade.get("underlying_return_pct")
            item["trade_exit_reason"] = trade.get("exit_reason")
            item["option_proxy_return_pct"] = (
                (trade.get("option_proxy") or {}).get("synthetic_option_return_pct")
            )
        threshold_tagged.append(item)

    top_tagged: list[dict] = []
    for row in top_rows:
        key = (
            str(row.get("date") or "")[:10],
            str(row.get("ticker") or "").upper(),
            str(row.get("direction") or "").upper(),
        )
        matched = key if key in trade_index else None
        item = dict(row)
        item["captured"] = bool(matched)
        item["matched_trade_signal_date"] = matched[0] if matched else None
        top_tagged.append(item)

    overlap = _reverse_trade_overlap_diagnostics(
        trades, threshold_tagged, top_tagged, series
    )
    return {
        "threshold_events": len(threshold_tagged),
        "threshold_captured": sum(bool(row.get("captured")) for row in threshold_tagged),
        "top5_events": len(top_tagged),
        "top5_captured": sum(bool(row.get("captured")) for row in top_tagged),
        "overlap": overlap,
    }


def _lifecycle_session_diagnostics(trades: list[dict]) -> dict:
    closed = [row for row in trades if row.get("status") == "CLOSED"]
    cross_day_zero = 0
    session_sources: Counter = Counter()
    calendar_days_held: list[int] = []
    for row in closed:
        source = str(row.get("session_count_source") or "UNSPECIFIED")
        session_sources[source] += 1
        signal_date = str(row.get("signal_date") or "")[:10]
        exit_date = str(row.get("exit_date") or "")[:10]
        if signal_date and exit_date:
            try:
                gap = (date.fromisoformat(exit_date) - date.fromisoformat(signal_date)).days
                calendar_days_held.append(gap)
                if gap > 0 and int(row.get("sessions_held") or 0) <= 0:
                    cross_day_zero += 1
            except Exception:
                pass
    return {
        "closed_trades": len(closed),
        "cross_day_closed_with_zero_sessions": cross_day_zero,
        "session_count_sources": dict(session_sources),
        "avg_calendar_days_held": round(mean(calendar_days_held), 3) if calendar_days_held else None,
        "max_calendar_days_held": max(calendar_days_held) if calendar_days_held else None,
        "interpretation": (
            "cross-day exits with zero sessions indicate ticker-specific price_cache "
            "cannot enforce the intended 5-session lifecycle"
        ),
    }


def _trade_favorable_5d_diagnostic(
    trade: dict,
    series: dict[str, list[dict]],
) -> dict:
    entry = _f(trade.get("entry_price"))
    ticker = _ticker(trade)
    direction = _direction(trade)
    day = str(trade.get("signal_date") or "")[:10]
    if entry is None or entry <= 0 or not ticker or direction not in {"LONG", "SHORT"} or not day:
        return {"value_pct": None, "sessions": 0, "source": "UNAVAILABLE"}

    future = _series_after(series, ticker, day)[:5]
    best = None
    sources: Counter = Counter()
    usable = 0
    for row in future:
        if direction == "LONG":
            high = _f(row.get("high"))
            close = _f(row.get("close"))
            px = high if high is not None and high > 0 else close
        else:
            low = _f(row.get("low"))
            close = _f(row.get("close"))
            px = low if low is not None and low > 0 else close
        if px is None or px <= 0:
            continue

        usable += 1
        source = str(row.get("path_source") or "PRIMARY_OHLC")
        if (direction == "LONG" and _f(row.get("high")) is None) or (
            direction == "SHORT" and _f(row.get("low")) is None
        ):
            source = "FEATURE_POINT_IN_TIME_CLOSE" if source == "FEATURE_POINT_IN_TIME_CLOSE" else "CLOSE_ONLY"
        sources[source] += 1

        if direction == "LONG":
            move = (float(px) / float(entry) - 1.0) * 100.0
        else:
            move = favorable_move_pct("SHORT", entry, px)
            if move is None:
                continue
        best = move if best is None else max(best, move)

    if best is None:
        return {"value_pct": None, "sessions": 0, "source": "UNAVAILABLE"}
    if sources and set(sources) == {"PRIMARY_OHLC"}:
        source = "PRIMARY_OHLC"
    elif "PRIMARY_OHLC" in sources:
        source = "MIXED_OHLC_AND_FEATURE_CLOSE"
    elif sources:
        source = "FEATURE_CLOSE_LOWER_BOUND"
    else:
        source = "UNKNOWN"
    return {
        "value_pct": round(float(best), 4),
        "sessions": usable,
        "source": source,
        "source_counts": dict(sources),
        "is_lower_bound": source != "PRIMARY_OHLC",
    }


def _trade_favorable_5d_pct(trade: dict, series: dict[str, list[dict]]) -> float | None:
    return _trade_favorable_5d_diagnostic(trade, series).get("value_pct")


def _reverse_trade_overlap_diagnostics(
    trades: list[dict],
    threshold_rows: list[dict],
    top_rows: list[dict],
    series: dict[str, list[dict]],
) -> dict:
    threshold_keys = {
        (
            str(row.get("matched_trade_signal_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
            str(row.get("direction") or "").upper(),
        )
        for row in threshold_rows
        if row.get("captured") and row.get("matched_trade_signal_date")
    }
    top_keys = {
        (
            str(row.get("matched_trade_signal_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
            str(row.get("direction") or "").upper(),
        )
        for row in top_rows
        if row.get("captured") and row.get("matched_trade_signal_date")
    }
    digest: list[dict] = []
    for trade in trades:
        if trade.get("status") not in {"CLOSED", "OPEN_AT_HISTORY_END"}:
            continue
        key = (
            str(trade.get("signal_date") or "")[:10],
            _ticker(trade),
            _direction(trade),
        )
        favorable_diag = _trade_favorable_5d_diagnostic(trade, series)
        favorable = favorable_diag.get("value_pct")
        threshold_match = key in threshold_keys
        top_match = key in top_keys
        trade["max_favorable_5d_pct"] = favorable
        trade["reverse_threshold_capture"] = threshold_match
        trade["reverse_top5_capture"] = top_match
        digest.append({
            "signal_date": key[0],
            "ticker": key[1],
            "direction": key[2],
            "status": trade.get("status"),
            "exit_date": trade.get("exit_date"),
            "exit_reason": trade.get("exit_reason"),
            "underlying_return_pct": trade.get("underlying_return_pct"),
            "max_favorable_5d_pct": favorable,
            "max_favorable_5d_sessions": favorable_diag.get("sessions"),
            "max_favorable_5d_source": favorable_diag.get("source"),
            "max_favorable_5d_is_lower_bound": favorable_diag.get("is_lower_bound"),
            "reached_5pct_within_5_sessions": bool(favorable is not None and favorable >= 5.0),
            "threshold_capture": threshold_match,
            "top5_hindsight_capture": top_match,
            "option_proxy_status": (trade.get("option_proxy") or {}).get("status"),
            "option_proxy_mode": (trade.get("option_proxy") or {}).get("proxy_mode"),
            "option_proxy_return_pct": (trade.get("option_proxy") or {}).get("synthetic_option_return_pct"),
        })
    return {
        "opened_trades": len(digest),
        "trades_reaching_5pct_within_5_sessions": sum(
            bool(row.get("reached_5pct_within_5_sessions")) for row in digest
        ),
        "favorable_5d_path_coverage": {
            "trades": len(digest),
            "with_path": sum(row.get("max_favorable_5d_pct") is not None for row in digest),
            "primary_ohlc": sum(row.get("max_favorable_5d_source") == "PRIMARY_OHLC" for row in digest),
            "mixed": sum(row.get("max_favorable_5d_source") == "MIXED_OHLC_AND_FEATURE_CLOSE" for row in digest),
            "feature_close_lower_bound": sum(row.get("max_favorable_5d_source") == "FEATURE_CLOSE_LOWER_BOUND" for row in digest),
            "unavailable": sum(row.get("max_favorable_5d_pct") is None for row in digest),
        },
        "threshold_captured_trades": sum(bool(row.get("threshold_capture")) for row in digest),
        "top5_hindsight_captured_trades": sum(bool(row.get("top5_hindsight_capture")) for row in digest),
        "threshold_matching_consistency_errors": sum(
            not bool(row.get("capture_match_consistent"))
            for row in threshold_rows
            if row.get("capture_match_consistent") is not None
        ),
        "top5_matching_consistency_errors": sum(
            not bool(row.get("capture_match_consistent"))
            for row in top_rows
            if row.get("capture_match_consistent") is not None
        ),
        "trade_digest": digest,
        "interpretation": (
            "0 captured is genuine only when trades_reaching_5pct/top5 overlap are also zero; "
            "consistency errors indicate matching bugs"
        ),
    }


def _semantic_quality(
    *,
    counter_diag: dict,
    counter_trades: list[dict],
    reverse_threshold_rows: list[dict],
    reverse_top_rows: list[dict],
    price_stats: dict,
) -> dict:
    enters = int(counter_diag.get("fusion_enter_actionable") or 0)
    trade_count = len(counter_trades)
    closed_trades = [row for row in counter_trades if row.get("status") == "CLOSED"]
    option_evaluable = sum(
        str((row.get("option_proxy") or {}).get("status") or "").startswith("EVALUATED")
        for row in closed_trades
    )
    top_with_5pct = sum(
        1
        for row in reverse_top_rows
        if abs(float(_f(row.get("available_return_pct"), 0.0) or 0.0)) >= 5.0
    )
    errors: list[str] = []
    warnings: list[str] = []

    if str(counter_diag.get("status") or "").upper() != "EVALUABLE":
        errors.append("COUNTERFACTUAL_INPUTS_NOT_EVALUABLE")
    if int(counter_diag.get("selected_dates") or 0) <= 0 or int(counter_diag.get("feature_rows") or 0) <= 0:
        errors.append("NO_COUNTERFACTUAL_HISTORY")
    if int(price_stats.get("tickers") or 0) <= 0 or int(price_stats.get("session_rows") or 0) <= 0:
        errors.append("NO_PRICE_SERIES")
    if enters > 0 and trade_count <= 0:
        errors.append("CURRENT_POLICY_ENTER_WITH_ZERO_TRADES")
    if len(reverse_top_rows) <= 0:
        errors.append("REVERSE_TOP_OPPORTUNITIES_EMPTY")
    if top_with_5pct > 0 and len(reverse_threshold_rows) <= 0:
        errors.append("REVERSE_THRESHOLD_ZERO_DESPITE_TOP5_GT5")
    if enters <= 0:
        warnings.append("CURRENT_POLICY_GENERATED_ZERO_ACTIONABLE_ENTER")
    if trade_count <= 0:
        warnings.append("COUNTERFACTUAL_BOOK_HAS_ZERO_TRADES")
    if closed_trades and option_evaluable <= 0:
        errors.append("OPTION_PROXY_ZERO_FOR_CLOSED_TRADES")

    return {
        "status": "INVALID" if errors else "VALID",
        "errors": errors,
        "warnings": warnings,
        "counterfactual_fusion_enter_actionable": enters,
        "counterfactual_trades": trade_count,
        "counterfactual_closed_trades": len(closed_trades),
        "option_proxy_evaluable_closed_trades": option_evaluable,
        "reverse_threshold_events": len(reverse_threshold_rows),
        "reverse_top_events": len(reverse_top_rows),
        "reverse_top_events_abs_return_ge_5pct": top_with_5pct,
        "price_series_tickers": price_stats.get("tickers"),
        "price_series_session_rows": price_stats.get("session_rows"),
    }


def build_historical_operational_validation(
    *,
    max_positions: int = 5,
    momentum_reconciliation_reference: dict | None = None,
    research_benchmark: dict | None = None,
    research_feature_benchmark: dict | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
    defer_autonomous_context_research: bool = True,
) -> dict:
    def _progress(stage: str, detail: str) -> None:
        if not callable(progress_callback):
            return
        try:
            progress_callback(str(stage), str(detail))
        except Exception:
            pass

    _progress("inputs", "loading historical signal/feature rows")
    # ------------------------------------------------------------------
    # A) Historical emitted book: what old TMAD versions actually emitted.
    # ------------------------------------------------------------------
    emitted_signal_rows = _all_signal_rows()
    layer_feature_rows = _all_feature_rows()
    emitted_portfolios = reconstruct_daily_portfolios(
        emitted_signal_rows, layer_feature_rows, max_positions=max_positions
    )
    _progress(
        "inputs",
        f"loaded signals={len(emitted_signal_rows)} features={len(layer_feature_rows)}",
    )

    _progress("counterfactual", "building rolling/fixed current-policy inputs")
    # ------------------------------------------------------------------
    # B) Current-policy counterfactual: run today's Fusion policy on old,
    # point-in-time feature snapshots. This is the book answering the user's
    # "what would current TMAD have traded?" question. It is retrospective,
    # not unbiased OOT proof; OOT challengers remain a separate evidence lane.
    # ------------------------------------------------------------------
    counter_signal_rows, counter_feature_rows, counter_diag = _counterfactual_current_policy_inputs(
        lane_label="ROLLING_LATEST",
    )
    counter_portfolios = reconstruct_daily_portfolios(
        counter_signal_rows, counter_feature_rows, max_positions=max_positions
    )

    benchmark_keys = operational_run_key_set(research_benchmark or {})
    benchmark_feature_ids = feature_observation_id_set(research_feature_benchmark or {})
    rolling_keys = {
        (
            str(row.get("date") or "")[:10],
            int(row.get("source_scanner_run_id") or 0),
        )
        for row in (counter_diag.get("selected_run_keys") or [])
        if str(row.get("date") or "")[:10]
        and int(row.get("source_scanner_run_id") or 0) > 0
    }
    rolling_feature_ids = {
        int(row.get("id") or row.get("observation_id") or 0)
        for row in counter_feature_rows
        if int(row.get("id") or row.get("observation_id") or 0) > 0
    }
    benchmark_reuses_rolling = bool(
        benchmark_keys
        and benchmark_keys == rolling_keys
        and (
            not benchmark_feature_ids
            or benchmark_feature_ids == rolling_feature_ids
        )
    )
    if benchmark_reuses_rolling:
        benchmark_signal_rows = counter_signal_rows
        benchmark_feature_rows = counter_feature_rows
        benchmark_diag = {
            **counter_diag,
            "lane": "FIXED_BENCHMARK",
            "run_selection": "immutable benchmark keys; identical to rolling selection at anchor",
            "reused_rolling_inputs": True,
        }
    elif benchmark_keys:
        benchmark_signal_rows, benchmark_feature_rows, benchmark_diag = _counterfactual_current_policy_inputs(
            benchmark_keys,
            benchmark_feature_ids,
            lane_label="FIXED_BENCHMARK",
        )
        benchmark_diag["reused_rolling_inputs"] = False
        benchmark_diag["exact_feature_observation_ids_frozen"] = bool(benchmark_feature_ids)
    else:
        benchmark_signal_rows, benchmark_feature_rows, benchmark_diag = [], [], {
            "status": "BENCHMARK_NOT_AVAILABLE",
            "lane": "FIXED_BENCHMARK",
            "reused_rolling_inputs": False,
        }
    benchmark_portfolios = reconstruct_daily_portfolios(
        benchmark_signal_rows,
        benchmark_feature_rows,
        max_positions=max_positions,
    ) if benchmark_feature_rows else []

    _progress("reverse_universe", "building FULL/reverse canonical universes")
    full_deep_canonical, full_deep_meta, bridge = _full_deep_reverse_canonical(10000)
    reverse_canonical, universe_meta = _counterfactual_reverse_universe(
        counter_feature_rows, full_deep_meta
    )
    benchmark_reverse_canonical, benchmark_universe_meta = (
        _counterfactual_reverse_universe(benchmark_feature_rows, full_deep_meta)
        if benchmark_feature_rows else ({}, {})
    )

    _progress(
        "price_series",
        f"building trusted price paths rolling_rows={len(counter_feature_rows)} "
        f"fixed_rows={len(benchmark_feature_rows)}",
    )
    # ------------------------------------------------------------------
    # Price history. The Scanner daily store is canonical where present.
    # Layer feature outcomes are the archive fallback because their
    # sessions_json retains actual OHLC dictionaries. observation_outcomes
    # only retains session dates in current schema and is metadata-only here.
    # ------------------------------------------------------------------
    price_tickers = {
        _ticker(row)
        for row in [*counter_feature_rows, *benchmark_feature_rows, *layer_feature_rows]
        if _ticker(row)
    }
    price_tickers.update(ticker for _day_key, ticker in reverse_canonical)
    scanner_series = _price_series(price_tickers)
    layer_series = _layer_outcome_price_series(layer_feature_rows)
    series = _merge_price_series(scanner_series, layer_series)
    feature_close_series = _feature_point_close_series([
        *counter_feature_rows,
        *benchmark_feature_rows,
        *layer_feature_rows,
        *list(reverse_canonical.values()),
    ])
    reverse_path_series = _merge_reverse_path_series(series, feature_close_series)
    trusted_reverse_series, reverse_trust = _trusted_reverse_path_series(reverse_path_series)
    benchmark_created_date = str((research_benchmark or {}).get("created_at_utc") or "")[:10]
    try:
        benchmark_price_cutoff_date = (
            date.fromisoformat(benchmark_created_date) - timedelta(days=1)
        ).isoformat() if benchmark_created_date else ""
    except Exception:
        benchmark_price_cutoff_date = ""
    fixed_benchmark_trusted_series = (
        {
            ticker: [
                row for row in rows
                if str(row.get("trade_date") or "")[:10]
                and str(row.get("trade_date") or "")[:10] <= benchmark_price_cutoff_date
            ]
            for ticker, rows in trusted_reverse_series.items()
        }
        if benchmark_price_cutoff_date
        else trusted_reverse_series
    )
    fixed_benchmark_series = (
        {
            ticker: [
                row for row in rows
                if str(row.get("trade_date") or "")[:10]
                and str(row.get("trade_date") or "")[:10] <= benchmark_price_cutoff_date
            ]
            for ticker, rows in series.items()
        }
        if benchmark_price_cutoff_date
        else series
    )
    _outcome_date_series, outcome_bridge = _observation_outcome_price_series(full_deep_canonical)
    price_stats = _series_stats(series)
    price_integrity = _price_path_integrity(reverse_path_series)
    price_bridge = {
        "scanner_daily": _series_stats(scanner_series),
        "layer_outcome_ohlc": _series_stats(layer_series),
        "merged": price_stats,
        "feature_point_in_time_close_fallback": _series_stats(feature_close_series),
        "reverse_path_merged": _series_stats(reverse_path_series),
        "trusted_reverse_path": _series_stats(trusted_reverse_series),
        "observation_outcomes_metadata": outcome_bridge,
        "integrity": price_integrity,
        "trusted_reverse_data_lane": reverse_trust,
        "precedence": "scanner_daily_over_layer_outcome_ohlc; feature close fills missing dates only for Reverse/forensic path diagnostics",
    }

    _progress("baseline_books", "replaying emitted/current/fixed lifecycle books")
    # v6.1.132: the historical replay baseline now uses the global market
    # calendar whenever ticker-specific price coverage cannot count sessions.
    # This corrects an objective replay defect; it does not mutate LIVE logic.
    emitted_trades = replay_stateful_alpha_book(
        emitted_portfolios,
        emitted_signal_rows,
        layer_feature_rows,
        series,
        session_count_policy="GLOBAL_MARKET_CALENDAR_FALLBACK",
    )
    counter_trades = replay_stateful_alpha_book(
        counter_portfolios,
        counter_signal_rows,
        counter_feature_rows,
        series,
        session_count_policy="GLOBAL_MARKET_CALENDAR_FALLBACK",
    )
    benchmark_trades = (
        replay_stateful_alpha_book(
            benchmark_portfolios,
            benchmark_signal_rows,
            benchmark_feature_rows,
            fixed_benchmark_series,
            session_count_policy="GLOBAL_MARKET_CALENDAR_FALLBACK",
        )
        if benchmark_feature_rows else []
    )
    _scope_trades(emitted_trades, universe_meta)
    _scope_trades(counter_trades, universe_meta)
    _scope_trades(benchmark_trades, universe_meta)

    # Reverse-stress primary capture lane = current-policy counterfactual OPENs.
    # The emitted historical book is kept separately so zero legacy entries are
    # not misread as "current TMAD would never trade".
    selection_rows = _selection_rows_from_trades(counter_trades)
    simulated_alerts = {
        (str(row["asof"]), str(row["ticker"]).upper(), str(row["direction"]).lower())
        for row in selection_rows
    }
    benchmark_selection_rows = _selection_rows_from_trades(benchmark_trades)
    benchmark_simulated_alerts = {
        (str(row["asof"]), str(row["ticker"]).upper(), str(row["direction"]).lower())
        for row in benchmark_selection_rows
    }

    economic_waterfall_calibration = {
        "rolling_latest": {
            "mode": "ECONOMIC_WATERFALL_CALIBRATION_V1",
            "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "ECONOMIC_WATERFALL_CALIBRATION_V1",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "decision": {
            "mode": "ECONOMIC_WATERFALL_DUAL_LANE_REVIEW_V1",
            "status": "PARTIAL",
            "live_mutation": False,
            "promotion_allowed": False,
        },
    }
    economic_architecture_challengers = {
        "rolling_latest": {
            "mode": "ECONOMIC_ARCHITECTURE_CHALLENGERS_V1",
            "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "ECONOMIC_ARCHITECTURE_CHALLENGERS_V1",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "decision": {
            "mode": "ECONOMIC_ARCHITECTURE_DUAL_LANE_DECISION_V1",
            "status": "PARTIAL",
            "live_mutation": False,
            "promotion_allowed": False,
        },
    }
    frozen_config_historical_simulation = {
        "rolling_latest": {
            "mode": "FROZEN_CONFIG_HISTORICAL_SIMULATION_V1",
            "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "FROZEN_CONFIG_HISTORICAL_SIMULATION_V1",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "decision": {
            "mode": "FROZEN_CONFIG_HISTORICAL_DUAL_LANE_DECISION_V1",
            "status": "PARTIAL",
            "live_mutation": False,
            "promotion_allowed": False,
        },
    }
    scanner_recall_ranking_calibration = {
        "rolling_latest": {
            "mode": "SCANNER_RECALL_RANKING_CALIBRATION_V1",
            "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "SCANNER_RECALL_RANKING_CALIBRATION_V1",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "decision": {
            "mode": "SCANNER_RECALL_RANKING_DUAL_LANE_DECISION_V1",
            "status": "PARTIAL",
            "live_mutation": False,
            "promotion_allowed": False,
        },
    }
    scanner_core_interaction_simulation = {
        "rolling_latest": {
            "mode": "SCANNER_CORE_INTERACTION_SIMULATION_V1",
            "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "SCANNER_CORE_INTERACTION_SIMULATION_V1",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
            "promotion_allowed": False,
        },
        "decision": {
            "mode": "SCANNER_CORE_INTERACTION_DUAL_LANE_DECISION_V1",
            "status": "PARTIAL",
            "live_mutation": False,
            "promotion_allowed": False,
        },
    }

    scanner_direction_aware_ranking = {
        "rolling_latest": {
            "mode": "SCANNER_DIRECTION_AWARE_RANKING_V2", "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES", "live_mutation": False, "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "SCANNER_DIRECTION_AWARE_RANKING_V2", "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE", "live_mutation": False, "promotion_allowed": False,
        },
        "decision": {
            "mode": "SCANNER_DIRECTION_AWARE_DUAL_LANE_DECISION_V2",
            "status": "PARTIAL", "live_mutation": False, "promotion_allowed": False,
        },
    }

    core_context_precision_optimizer = {
        "rolling_latest": {
            "mode": "CORE_CONTEXT_PRECISION_OPTIMIZER_V1", "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES", "live_mutation": False, "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "CORE_CONTEXT_PRECISION_OPTIMIZER_V1", "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE", "live_mutation": False, "promotion_allowed": False,
        },
        "decision": {
            "mode": "CORE_CONTEXT_PRECISION_DUAL_LANE_DECISION_V1",
            "status": "PARTIAL", "live_mutation": False, "promotion_allowed": False,
        },
    }

    gex_gate_calibration = {
        "rolling_latest": {
            "mode": "GEX_GATE_CALIBRATION_V1", "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES", "live_mutation": False, "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "GEX_GATE_CALIBRATION_V1", "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE", "live_mutation": False, "promotion_allowed": False,
        },
        "decision": {
            "mode": "GEX_GATE_DUAL_LANE_DECISION_V1",
            "status": "PARTIAL", "live_mutation": False, "promotion_allowed": False,
        },
    }

    fusion_gate_calibration = {
        "rolling_latest": {
            "mode": "FUSION_GATE_CALIBRATION_V1", "lane": "ROLLING_LATEST",
            "status": "NO_EVALUABLE_CANDIDATES", "live_mutation": False, "promotion_allowed": False,
        },
        "fixed_benchmark": {
            "mode": "FUSION_GATE_CALIBRATION_V1", "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE", "live_mutation": False, "promotion_allowed": False,
        },
        "decision": {
            "mode": "FUSION_GATE_DUAL_LANE_DECISION_V1",
            "status": "PARTIAL", "live_mutation": False, "promotion_allowed": False,
        },
        "artifact_metadata": {
            "schema_version": 4,
            "producer_version": config.BOT_VERSION,
            "generated_at_madrid": ts_madrid_str(),
            "outcome_mode": "TRUSTED_REVERSE_SERIES_V1",
            "source": "FULL_HISTORICAL_REPLAY",
        },
    }

    reverse = {
        "status": "NO_FULL_DEEP_HISTORY",
        "threshold_movements": [],
        "top_opportunities": [],
    }
    if reverse_canonical:
        _progress("waterfall", "economic waterfall rolling/fixed")
        ranks, universe_sizes = _rank_maps(reverse_canonical)
        signals = _signals_index(counter_signal_rows)

        rolling_waterfall = build_economic_waterfall_calibration(
            reverse_canonical,
            signals,
            trusted_reverse_series,
            alerts=simulated_alerts,
            ranks=ranks,
            trades=counter_trades,
            mover_threshold_pct=5.0,
            horizon_sessions=5,
            lane_label="ROLLING_LATEST",
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            benchmark_ranks, _benchmark_universe_sizes = _rank_maps(benchmark_reverse_canonical)
            fixed_waterfall = build_economic_waterfall_calibration(
                benchmark_reverse_canonical,
                _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series,
                alerts=benchmark_simulated_alerts,
                ranks=benchmark_ranks,
                trades=benchmark_trades,
                mover_threshold_pct=5.0,
                horizon_sessions=5,
                lane_label="FIXED_BENCHMARK",
            )
        else:
            fixed_waterfall = economic_waterfall_calibration["fixed_benchmark"]
        economic_waterfall_calibration = {
            "rolling_latest": rolling_waterfall,
            "fixed_benchmark": fixed_waterfall,
            "decision": combine_economic_waterfall_calibrations(
                rolling_waterfall,
                fixed_waterfall,
            ),
        }

        _progress("architecture", "economic architecture challengers rolling/fixed")
        rolling_architecture = build_economic_architecture_challengers(
            reverse_canonical,
            signals,
            trusted_reverse_series,
            threshold_pct=5.0,
            horizon_sessions=5,
            lane_label="ROLLING_LATEST",
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            fixed_architecture = build_economic_architecture_challengers(
                benchmark_reverse_canonical,
                _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series,
                threshold_pct=5.0,
                horizon_sessions=5,
                lane_label="FIXED_BENCHMARK",
            )
        else:
            fixed_architecture = economic_architecture_challengers["fixed_benchmark"]
        economic_architecture_challengers = {
            "rolling_latest": rolling_architecture,
            "fixed_benchmark": fixed_architecture,
            "decision": combine_economic_architecture_challengers(
                rolling_architecture,
                fixed_architecture,
            ),
        }

        _progress("frozen_config", "frozen config historical simulation")
        rolling_frozen_config = build_frozen_config_historical_simulation(
            reverse_canonical,
            signals,
            trusted_reverse_series,
            alerts=simulated_alerts,
            lane_label="ROLLING_LATEST",
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            fixed_frozen_config = build_frozen_config_historical_simulation(
                benchmark_reverse_canonical,
                _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series,
                alerts=benchmark_simulated_alerts,
                lane_label="FIXED_BENCHMARK",
            )
        else:
            fixed_frozen_config = frozen_config_historical_simulation["fixed_benchmark"]
        frozen_config_historical_simulation = {
            "rolling_latest": rolling_frozen_config,
            "fixed_benchmark": fixed_frozen_config,
            "decision": combine_frozen_config_historical_simulations(
                rolling_frozen_config,
                fixed_frozen_config,
            ),
        }

        _progress("scanner_recall", "scanner recall/ranking calibration")
        rolling_scanner_calibration = build_scanner_recall_ranking_calibration(
            reverse_canonical,
            trusted_reverse_series,
            lane_label="ROLLING_LATEST",
        )
        if benchmark_reverse_canonical:
            fixed_scanner_calibration = build_scanner_recall_ranking_calibration(
                benchmark_reverse_canonical,
                fixed_benchmark_trusted_series,
                lane_label="FIXED_BENCHMARK",
            )
        else:
            fixed_scanner_calibration = scanner_recall_ranking_calibration["fixed_benchmark"]
        scanner_recall_ranking_calibration = {
            "rolling_latest": rolling_scanner_calibration,
            "fixed_benchmark": fixed_scanner_calibration,
            "decision": combine_scanner_recall_ranking_calibrations(
                rolling_scanner_calibration,
                fixed_scanner_calibration,
            ),
        }

        _progress("scanner_core", "scanner/core interaction simulation")
        rolling_scanner_core = build_scanner_core_interaction_simulation(
            reverse_canonical,
            signals,
            trusted_reverse_series,
            lane_label="ROLLING_LATEST",
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            fixed_scanner_core = build_scanner_core_interaction_simulation(
                benchmark_reverse_canonical,
                _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series,
                lane_label="FIXED_BENCHMARK",
            )
        else:
            fixed_scanner_core = scanner_core_interaction_simulation["fixed_benchmark"]
        scanner_core_interaction_simulation = {
            "rolling_latest": rolling_scanner_core,
            "fixed_benchmark": fixed_scanner_core,
            "decision": combine_scanner_core_interaction_simulations(
                rolling_scanner_core,
                fixed_scanner_core,
            ),
        }

        _progress("direction_aware", "direction-aware scanner ranking")
        rolling_direction_aware = build_scanner_direction_aware_ranking(
            reverse_canonical, signals, trusted_reverse_series, lane_label="ROLLING_LATEST"
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            fixed_direction_aware = build_scanner_direction_aware_ranking(
                benchmark_reverse_canonical, _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series, lane_label="FIXED_BENCHMARK",
            )
        else:
            fixed_direction_aware = scanner_direction_aware_ranking["fixed_benchmark"]
        scanner_direction_aware_ranking = {
            "rolling_latest": rolling_direction_aware,
            "fixed_benchmark": fixed_direction_aware,
            "decision": combine_scanner_direction_aware_rankings(
                rolling_direction_aware, fixed_direction_aware
            ),
        }

        _progress(
            "context_precision",
            "context precision + fast cache"
            + ("; autonomous tournament deferred to post-FULL adaptive" if defer_autonomous_context_research else "; embedded autonomous tournament enabled"),
        )
        rolling_context_precision = build_core_context_precision_optimizer(
            reverse_canonical,
            signals,
            trusted_reverse_series,
            lane_label="ROLLING_LATEST",
            run_autonomous_research=not defer_autonomous_context_research,
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            fixed_context_precision = build_core_context_precision_optimizer(
                benchmark_reverse_canonical,
                _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series,
                lane_label="FIXED_BENCHMARK",
                run_autonomous_research=not defer_autonomous_context_research,
            )
        else:
            fixed_context_precision = core_context_precision_optimizer["fixed_benchmark"]
        core_context_precision_optimizer = {
            "rolling_latest": rolling_context_precision,
            "fixed_benchmark": fixed_context_precision,
            "decision": combine_core_context_precision_optimizers(
                rolling_context_precision, fixed_context_precision
            ),
        }

        _progress("gex_gate", "GEX gate calibration rolling/fixed")
        rolling_gex_gate = build_gex_gate_calibration(
            reverse_canonical,
            signals,
            trusted_reverse_series,
            lane_label="ROLLING_LATEST",
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            fixed_gex_gate = build_gex_gate_calibration(
                benchmark_reverse_canonical,
                _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series,
                lane_label="FIXED_BENCHMARK",
            )
        else:
            fixed_gex_gate = gex_gate_calibration["fixed_benchmark"]
        gex_gate_calibration = {
            "rolling_latest": rolling_gex_gate,
            "fixed_benchmark": fixed_gex_gate,
            "decision": combine_gex_gate_calibrations(
                rolling_gex_gate, fixed_gex_gate
            ),
        }


        _progress("fusion_gate", "FUSION trusted calibration rolling/fixed")
        rolling_fusion_gate = build_fusion_gate_calibration_from_trusted_series(
            reverse_canonical,
            signals,
            trusted_reverse_series,
            lane_label="ROLLING_LATEST",
            expected_generation={
                "fusion_signals": int(counter_diag.get("fusion_signals") or 0),
                "fusion_enter_state": int(counter_diag.get("fusion_enter_state") or 0),
            },
        )
        if benchmark_reverse_canonical and benchmark_signal_rows:
            fixed_fusion_gate = build_fusion_gate_calibration_from_trusted_series(
                benchmark_reverse_canonical,
                _signals_index(benchmark_signal_rows),
                fixed_benchmark_trusted_series,
                lane_label="FIXED_BENCHMARK",
                expected_generation={
                    "fusion_signals": int(benchmark_diag.get("fusion_signals") or 0),
                    "fusion_enter_state": int(benchmark_diag.get("fusion_enter_state") or 0),
                },
            )
        else:
            fixed_fusion_gate = fusion_gate_calibration["fixed_benchmark"]
        fusion_decision = combine_fusion_gate_calibrations(
            rolling_fusion_gate, fixed_fusion_gate
        )
        fusion_gate_calibration = {
            "rolling_latest": rolling_fusion_gate,
            "fixed_benchmark": fixed_fusion_gate,
            "decision": fusion_decision,
            "artifact_metadata": {
                "schema_version": 4,
                "producer_version": config.BOT_VERSION,
                "generated_at_madrid": ts_madrid_str(),
                "outcome_mode": "TRUSTED_REVERSE_SERIES_V1",
                "source": "FULL_HISTORICAL_REPLAY",
                "price_lane": "trusted_reverse_series",
                "fixed_price_lane": "fixed_benchmark_trusted_series",
            },
            "research_validity": {
                "status": "VALID_FROZEN_INPUTS",
                "strategic_conclusion_allowed": str(fusion_decision.get("status") or "").upper()=="EVALUABLE",
                "live_mutation": False,
                "future_prices_are_labels_only": True,
            },
        }

        # Current-policy features first; canonical daily FULL-DEEP rows fill any
        # date/ticker not present in the selected counterfactual scanner runs.
        feature_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in counter_feature_rows:
            day, ticker = _day(row), _ticker(row)
            if day and ticker:
                feature_groups[(day, ticker)].append(row)
        for key, row in reverse_canonical.items():
            if key not in feature_groups:
                feature_groups[key] = [row]

        raw_movement_candidates = _movement_candidates_from_prices(
            reverse_canonical,
            reverse_path_series,
            horizon_sessions=5,
        )
        raw_threshold_events = _cluster_and_classify(
            raw_movement_candidates,
            feature_groups,
            signals,
            ranks,
            universe_sizes,
            simulated_alerts,
        )

        _progress("reverse_stress", "building/reusing reverse mover opportunity cache")
        movement_candidates, reverse_alpha_base, reverse_cache = _load_or_build_reverse_canonical_cache(
            reverse_canonical,
            trusted_reverse_series,
            ranks,
            universe_sizes,
            universe_meta,
            horizon_sessions=5,
            top_n=5,
        )
        threshold_events = _cluster_and_classify(
            movement_candidates,
            feature_groups,
            signals,
            ranks,
            universe_sizes,
            simulated_alerts,
        )
        reverse_alpha = _reclassify_reverse_alpha_base(
            reverse_alpha_base,
            signals,
            simulated_alerts,
            selection_rows,
        )
        reverse_short_over_100 = [
            row for row in (reverse_alpha.get("top_opportunities") or [])
            if str(row.get("direction") or "").lower() == "short"
            and float(row.get("realized_return_pct") or 0.0) > 100.000001
        ]

        trade_index: dict[tuple[str, str, str], dict] = {}
        for trade in counter_trades:
            if trade.get("status") not in {"CLOSED", "OPEN_AT_HISTORY_END"}:
                continue
            trade_index[
                (trade["signal_date"], trade["ticker"], trade["direction"].lower())
            ] = trade

        threshold_ledger: list[dict] = []
        for event in threshold_events:
            matching = [
                trade_index.get((source_day, event["ticker"], event["direction"]))
                for source_day in (event.get("source_dates") or [event.get("signal_date")])
            ]
            matching = [row for row in matching if row]
            trade = matching[0] if matching else None
            trade_return = _f(trade.get("underlying_return_pct")) if trade else None
            available_move = _f(event.get("move_pct"))
            capture_efficiency = (
                100.0 * float(trade_return) / float(available_move)
                if trade_return is not None
                and available_move is not None
                and available_move > 0
                else None
            )
            threshold_ledger.append({
                "ticker": event.get("ticker"),
                "direction": event.get("direction"),
                "signal_date": event.get("signal_date"),
                "hit_date": event.get("hit_date"),
                "available_move_pct": event.get("move_pct"),
                "first_hit_session": event.get("first_hit_session"),
                "captured": trade is not None,
                "auditor_enter_generated": bool(event.get("enter_generated")),
                "capture_match_consistent": bool(event.get("enter_generated")) == (trade is not None),
                "captured_profitably": trade is not None
                and trade_return is not None
                and trade_return > 0,
                "capture_efficiency_pct": round(capture_efficiency, 2)
                if capture_efficiency is not None
                else None,
                "classification": event.get("classification"),
                "blocker": event.get("blocker"),
                "core_detected": event.get("core_detected"),
                "research_reached": event.get("research_reached"),
                "gex_confirmed": event.get("gex_confirmed"),
                "timing_ready": event.get("timing_ready"),
                "fusion_ready": event.get("fusion_ready"),
                "core_rank": event.get("core_rank"),
                "matched_trade_signal_date": trade.get("signal_date") if trade else None,
                "trade_status": trade.get("status") if trade else None,
                "trade_underlying_return_pct": trade_return,
                "trade_exit_reason": trade.get("exit_reason") if trade else None,
                "option_proxy_return_pct": (
                    (trade.get("option_proxy") or {}).get("synthetic_option_return_pct")
                    if trade else None
                ),
                "source_dates": event.get("source_dates") or [],
            })

        top_ledger: list[dict] = []
        for item in reverse_alpha.get("top_opportunities") or []:
            trade = trade_index.get((
                str(item.get("date") or ""),
                str(item.get("ticker") or "").upper(),
                str(item.get("direction") or "").lower(),
            ))
            top_ledger.append({
                "date": item.get("date"),
                "horizon_sessions": item.get("horizon_sessions"),
                "hindsight_rank": item.get("hindsight_rank"),
                "ticker": item.get("ticker"),
                "direction": item.get("direction"),
                "available_return_pct": item.get("realized_return_pct"),
                "captured": trade is not None,
                "selected_in_eod_alpha": bool(item.get("selected_in_eod_alpha")),
                "capture_match_consistent": bool(item.get("selected_in_eod_alpha")) == (trade is not None),
                "miss_reason": item.get("miss_reason"),
                "tmad_path": item.get("tmad_path"),
                "core_rank": (item.get("ranks") or {}).get("core"),
                "trade_underlying_return_pct": (
                    trade.get("underlying_return_pct") if trade else None
                ),
                "trade_exit_reason": trade.get("exit_reason") if trade else None,
            })

        miss_counts = Counter(
            str(row.get("classification") or "UNKNOWN")
            for row in threshold_ledger if not row.get("captured")
        )
        captured_rows = [row for row in threshold_ledger if row.get("captured")]
        profitable_captured = [
            row for row in captured_rows if row.get("captured_profitably")
        ]
        losing_captured = [
            row for row in captured_rows
            if _f(row.get("trade_underlying_return_pct")) is not None
            and float(row.get("trade_underlying_return_pct")) <= 0
        ]
        efficiency_values = [
            float(row["capture_efficiency_pct"])
            for row in captured_rows
            if _f(row.get("capture_efficiency_pct")) is not None
        ]
        missed_move = defaultdict(float)
        for row in threshold_ledger:
            if not row.get("captured"):
                missed_move[str(row.get("classification") or "UNKNOWN")] += float(
                    row.get("available_move_pct") or 0.0
                )

        reverse = {
            "status": "EVALUABLE",
            "canonical_cache": reverse_cache,
            "data_quality": {
                "mode": "TRUSTED_REVERSE_WITH_RAW_FORENSIC_LANE",
                "raw_threshold_events": len(raw_threshold_events),
                "trusted_threshold_events": len(threshold_events),
                "threshold_events_quarantined": max(0, len(raw_threshold_events) - len(threshold_events)),
                "raw_movement_candidates": len(raw_movement_candidates),
                "trusted_movement_candidates": len(movement_candidates),
                "trusted_lane": reverse_trust,
                "raw_price_integrity": {
                    key: value for key, value in price_integrity.items()
                    if key != "top_large_jumps"
                },
                "raw_top_large_jumps": price_integrity.get("top_large_jumps") or [],
            },
            "return_math_sanity": {
                "formula_version": "ENTRY_NOTIONAL_V2",
                "short_top_return_over_100": len(reverse_short_over_100),
                "valid": len(reverse_short_over_100) == 0,
                "examples": reverse_short_over_100[:10],
            },
            "universe_scope": reverse_alpha.get("universe_scope"),
            "strategic_conclusion_allowed": reverse_alpha.get("strategic_conclusion_allowed"),
            "universe_coverage": reverse_alpha.get("universe_coverage"),
            "threshold_5pct_within_5_sessions": {
                "events": len(threshold_ledger),
                "captured": len(captured_rows),
                "capture_rate_pct": round(
                    100.0 * len(captured_rows) / len(threshold_ledger), 2
                ) if threshold_ledger else None,
                "captured_profitably": len(profitable_captured),
                "captured_but_lost": len(losing_captured),
                "profitable_capture_rate_pct": round(
                    100.0 * len(profitable_captured) / len(threshold_ledger), 2
                ) if threshold_ledger else None,
                "avg_capture_efficiency_pct": round(mean(efficiency_values), 2)
                if efficiency_values else None,
                "miss_reasons": dict(miss_counts),
                "missed_available_move_pct_sum_by_reason": {
                    key: round(value, 4)
                    for key, value in sorted(
                        missed_move.items(), key=lambda kv: -kv[1]
                    )
                },
            },
            "top5_daily_by_horizon": {
                "events": len(top_ledger),
                "captured": sum(bool(row.get("captured")) for row in top_ledger),
                "capture_rate_pct": round(
                    100.0 * sum(bool(row.get("captured")) for row in top_ledger)
                    / len(top_ledger),
                    2,
                ) if top_ledger else None,
                "by_horizon": reverse_alpha.get("by_horizon"),
                "miss_reasons": reverse_alpha.get("miss_reasons"),
            },
            "threshold_movement_ledger": threshold_ledger,
            "top_opportunity_ledger": top_ledger,
        }

    # ------------------------------------------------------------------
    # C) Research-only challengers.
    #
    # The session-count fallback is now the historical baseline because v6.1.131
    # proved ticker-only counting produced impossible cross-day 0-session holds.
    # ENTER-first remains as a deliberately broad reference challenger, but now
    # uses the corrected lifecycle so selection and lifecycle are not conflated.
    #
    # QUALITY_GATED_ENTER is the new focused challenger. It uses no future labels
    # and no fitted return threshold: actionable ENTERs must have CORE/GEX
    # directional agreement, explicit intraday ENTER timing, and a market regime
    # that is not strongly opposed.
    # ------------------------------------------------------------------
    legacy_ticker_session_trades = replay_stateful_alpha_book(
        counter_portfolios,
        counter_signal_rows,
        counter_feature_rows,
        series,
        session_count_policy="TICKER_PRICE_CACHE_ONLY",
    )
    _scope_trades(legacy_ticker_session_trades, universe_meta)

    enter_first_portfolios = reconstruct_daily_portfolios(
        counter_signal_rows,
        counter_feature_rows,
        max_positions=max_positions,
        selection_policy="ACTIONABLE_ENTER_FIRST",
    )
    enter_first_trades = replay_stateful_alpha_book(
        enter_first_portfolios,
        counter_signal_rows,
        counter_feature_rows,
        series,
        session_count_policy="GLOBAL_MARKET_CALENDAR_FALLBACK",
    )
    _scope_trades(enter_first_trades, universe_meta)
    enter_first_compression = _selection_compression_diagnostics(
        counter_signal_rows, enter_first_portfolios, enter_first_trades
    )
    enter_first_reverse = _reverse_capture_for_trade_book(
        enter_first_trades,
        reverse.get("threshold_movement_ledger") or [],
        reverse.get("top_opportunity_ledger") or [],
        trusted_reverse_series,
    )
    enter_first_closed_full = [
        row for row in enter_first_trades if row.get("full_deep")
    ]

    # v6.1.140 candidate lab: retain every canonical actionable ENTER before
    # Top-N compression, then label each opportunity independently with the
    # same trusted boundary lane used by Reverse.
    all_enter_portfolios = reconstruct_daily_portfolios(
        counter_signal_rows,
        counter_feature_rows,
        max_positions=max_positions,
        selection_policy="ALL_ACTIONABLE_ENTER",
    )
    all_enter_candidate_trades = _all_actionable_enter_candidate_trades(all_enter_portfolios)

    benchmark_all_enter_portfolios = (
        reconstruct_daily_portfolios(
            benchmark_signal_rows,
            benchmark_feature_rows,
            max_positions=max_positions,
            selection_policy="ALL_ACTIONABLE_ENTER",
        )
        if benchmark_feature_rows else []
    )
    benchmark_all_enter_candidate_trades = _all_actionable_enter_candidate_trades(
        benchmark_all_enter_portfolios
    ) if benchmark_all_enter_portfolios else []

    quality_portfolios = reconstruct_daily_portfolios(
        counter_signal_rows,
        counter_feature_rows,
        max_positions=max_positions,
        selection_policy="QUALITY_GATED_ENTER",
    )
    quality_trades = replay_stateful_alpha_book(
        quality_portfolios,
        counter_signal_rows,
        counter_feature_rows,
        series,
        session_count_policy="GLOBAL_MARKET_CALENDAR_FALLBACK",
    )
    _scope_trades(quality_trades, universe_meta)
    quality_compression = _selection_compression_diagnostics(
        counter_signal_rows, quality_portfolios, quality_trades
    )
    quality_reverse = _reverse_capture_for_trade_book(
        quality_trades,
        reverse.get("threshold_movement_ledger") or [],
        reverse.get("top_opportunity_ledger") or [],
        trusted_reverse_series,
    )
    quality_closed_full = [
        row for row in quality_trades if row.get("full_deep")
    ]
    quality_gate_totals: Counter = Counter()
    quality_gate_reasons: Counter = Counter()
    for portfolio in quality_portfolios:
        diag = portfolio.get("quality_gate_diagnostics") or {}
        quality_gate_totals["actionable_enter_candidates"] += int(diag.get("actionable_enter_candidates") or 0)
        quality_gate_totals["qualified_actionable_enters"] += int(diag.get("qualified_actionable_enters") or 0)
        quality_gate_totals["disqualified_actionable_enters"] += int(diag.get("disqualified_actionable_enters") or 0)
        for reason, count in (diag.get("disqualification_reasons") or {}).items():
            quality_gate_reasons[str(reason)] += int(count or 0)

    # Compatibility aliases retained for external readers introduced in v6.1.130.
    # market_session_fallback is now identical to the corrected baseline, while
    # the old combined challenger is identical to corrected ENTER-first.
    session_fallback_trades = counter_trades
    session_fallback_reverse = _reverse_capture_for_trade_book(
        session_fallback_trades,
        reverse.get("threshold_movement_ledger") or [],
        reverse.get("top_opportunity_ledger") or [],
        trusted_reverse_series,
    )
    session_fallback_closed_full = [
        row for row in session_fallback_trades if row.get("full_deep")
    ]

    combined_trades = enter_first_trades
    combined_reverse = enter_first_reverse
    combined_closed_full = enter_first_closed_full

    emitted_closed_full = [row for row in emitted_trades if row.get("full_deep")]
    counter_closed_full = [row for row in counter_trades if row.get("full_deep")]
    selection_compression = _selection_compression_diagnostics(
        counter_signal_rows, counter_portfolios, counter_trades
    )
    reverse_overlap = _reverse_trade_overlap_diagnostics(
        counter_trades,
        reverse.get("threshold_movement_ledger") or [],
        reverse.get("top_opportunity_ledger") or [],
        trusted_reverse_series,
    )
    quality = _semantic_quality(
        counter_diag=counter_diag,
        counter_trades=counter_trades,
        reverse_threshold_rows=reverse.get("threshold_movement_ledger") or [],
        reverse_top_rows=reverse.get("top_opportunity_ledger") or [],
        price_stats=price_stats,
    )
    consistency_errors = int(reverse_overlap.get("threshold_matching_consistency_errors") or 0) + int(
        reverse_overlap.get("top5_matching_consistency_errors") or 0
    )
    if consistency_errors > 0:
        quality.setdefault("errors", []).append("REVERSE_CAPTURE_MATCH_INCONSISTENT")
        quality["status"] = "INVALID"
    return_math_sanity = reverse.get("return_math_sanity") or {}
    quality["return_math_sanity"] = return_math_sanity
    if return_math_sanity and not bool(return_math_sanity.get("valid")):
        quality.setdefault("errors", []).append("IMPOSSIBLE_SHORT_RETURN_OVER_100")
        quality["status"] = "INVALID"

    quality["price_path_integrity"] = {
        key: value for key, value in price_integrity.items()
        if key not in {"top_large_jumps", "invalid_ohlc_examples"}
    }
    quality["reverse_data_quality"] = reverse.get("data_quality") or {}
    trust_quality = (reverse.get("data_quality") or {}).get("trusted_lane") or {}
    quarantined_rows = int(trust_quality.get("invalid_ohlc_downgraded_to_close") or 0)
    continuity_barriers = int(trust_quality.get("continuity_barriers") or 0)
    quality["data_quality_status"] = (
        "VALID_TRUSTED_REVERSE_WITH_QUARANTINE"
        if quarantined_rows > 0 or continuity_barriers > 0
        else "VALID_TRUSTED_REVERSE_CLEAN"
    )
    quality["strategic_reverse_uses_raw_unfiltered_prices"] = False
    quality["strategic_reverse_uses_trusted_quarantined_lane"] = True
    if int(price_integrity.get("invalid_ohlc_rows") or 0) > 0:
        quality.setdefault("warnings", []).append("INVALID_OHLC_ROWS_PRESENT")
    if (
        int((price_integrity.get("observed_close_jumps") or {}).get("ge_80pct") or 0) > 0
        and float(price_integrity.get("adjustment_metadata_coverage_pct") or 0.0) < 1.0
    ):
        quality.setdefault("warnings", []).append(
            "LARGE_PRICE_JUMPS_WITHOUT_CORPORATE_ACTION_METADATA"
        )

    quality["selection_compression"] = selection_compression
    baseline_lifecycle = _lifecycle_session_diagnostics(counter_trades)
    quality["lifecycle_session_integrity"] = baseline_lifecycle
    if int(baseline_lifecycle.get("cross_day_closed_with_zero_sessions") or 0) > 0:
        quality.setdefault("warnings", []).append("TICKER_SESSION_COUNT_COVERAGE_GAP")
    quality["reverse_trade_overlap"] = {
        key: value for key, value in reverse_overlap.items() if key != "trade_digest"
    }

    current_forensics = _trade_forensics(counter_trades)
    enter_first_forensics = _trade_forensics(enter_first_trades)
    quality_forensics = _trade_forensics(quality_trades)

    # v6.1.138: lifecycle must consume exactly the same trusted historical
    # price lane as Reverse. Rebuilding from compact live price_cache caused
    # v6.1.137 to report no_price_path for the actual current-policy trades.
    _progress("lifecycle", "stateful boundary lifecycle on trusted price lane")
    from lifecycle_research import analyze_boundary_lifecycle_from_series
    lifecycle_books = {
        "current_policy": analyze_boundary_lifecycle_from_series(
            counter_trades, trusted_reverse_series,
            label="current_policy", trust_metadata=reverse_trust,
        ),
        "enter_first": analyze_boundary_lifecycle_from_series(
            enter_first_trades, trusted_reverse_series,
            label="enter_first", trust_metadata=reverse_trust,
        ),
        "quality_gated": analyze_boundary_lifecycle_from_series(
            quality_trades, trusted_reverse_series,
            label="quality_gated", trust_metadata=reverse_trust,
        ),
        "all_actionable_enter_candidates": analyze_boundary_lifecycle_from_series(
            all_enter_candidate_trades, trusted_reverse_series,
            label="all_actionable_enter_candidates", trust_metadata=reverse_trust,
        ),
        "fixed_benchmark_all_actionable_enter_candidates": analyze_boundary_lifecycle_from_series(
            benchmark_all_enter_candidate_trades, fixed_benchmark_trusted_series,
            label="fixed_benchmark_all_actionable_enter_candidates", trust_metadata={
                **reverse_trust,
                "fixed_benchmark_price_cutoff_date": benchmark_price_cutoff_date,
            },
        ) if benchmark_all_enter_candidate_trades else {
            "label": "fixed_benchmark_all_actionable_enter_candidates",
            "evaluable": 0,
            "trades": [],
            "status": "BENCHMARK_NOT_AVAILABLE",
        },
        "historical_emitted": analyze_boundary_lifecycle_from_series(
            emitted_trades, trusted_reverse_series,
            label="historical_emitted", trust_metadata=reverse_trust,
        ),
    }
    lifecycle_current = lifecycle_books["current_policy"]
    entry_research_rows = list(
        lifecycle_books["all_actionable_enter_candidates"].get("trades") or []
    )
    entry_research_expected = int(
        lifecycle_books["all_actionable_enter_candidates"].get("evaluable") or 0
    )
    entry_research_input_integrity = {
        "source": "all_actionable_enter_candidates.stateful_boundary_lifecycle",
        "expected_evaluable_rows": entry_research_expected,
        "delivered_rows": len(entry_research_rows),
        "full_universe_delivered": len(entry_research_rows) == entry_research_expected,
        "tail_truncation_applied": False,
    }
    _progress("entry_research", "entry quality/eligibility/falsification walk-forwards")
    entry_quality_walkforward = build_entry_quality_walkforward(
        list(lifecycle_books["enter_first"].get("trades") or [])
    )
    entry_quality_cross_sectional = build_cross_sectional_entry_quality_walkforward(
        entry_research_rows,
        max_positions=max_positions,
    )
    entry_quality_eligibility = build_entry_eligibility_walkforward(
        entry_research_rows,
        max_positions=max_positions,
        trusted_price_series=trusted_reverse_series,
    )
    fixed_entry_research_rows = list(
        lifecycle_books["fixed_benchmark_all_actionable_enter_candidates"].get("trades") or []
    )
    entry_eligibility_falsification_rolling = build_entry_eligibility_falsification(
        entry_research_rows,
        trusted_reverse_series,
        max_positions=max_positions,
        lane_label="ROLLING_LATEST",
    )
    entry_eligibility_falsification_fixed = (
        build_entry_eligibility_falsification(
            fixed_entry_research_rows,
            fixed_benchmark_trusted_series,
            max_positions=max_positions,
            lane_label="FIXED_BENCHMARK",
        )
        if fixed_entry_research_rows
        else {
            "mode": "ENTRY_ELIGIBILITY_HISTORICAL_FALSIFICATION_V5",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
            "promotion_allowed": False,
            "historical_screen": {"survives_historical_falsification": False},
        }
    )
    entry_eligibility_dual_lane_decision = combine_eligibility_falsification(
        entry_eligibility_falsification_rolling,
        entry_eligibility_falsification_fixed,
    )
    economic_architecture_challengers["eligibility_reference"] = {
        "role": "ABSTENTION_RISK_CONTROL_REFERENCE",
        "historical_followup_candidate": bool(
            entry_eligibility_dual_lane_decision.get("historical_followup_candidate")
        ),
        "rolling_survives": bool(
            entry_eligibility_dual_lane_decision.get("rolling_survives")
        ),
        "fixed_benchmark_survives": bool(
            entry_eligibility_dual_lane_decision.get("fixed_benchmark_survives")
        ),
        "live_mutation": False,
        "interpretation": (
            "Architecture challengers cannot justify more capacity by themselves; "
            "Eligibility remains the separate abstention evidence layer."
        ),
    }
    _progress("diagnostics", "thesis quality + momentum attribution diagnostics")
    thesis_quality = build_thesis_quality_walkforward(
        entry_research_rows,
        max_positions=max_positions,
    )
    momentum_direction_audit = build_momentum_direction_audit(
        all_enter_candidate_trades,
        counter_feature_rows,
        trusted_reverse_series,
    )
    momentum_edge_attrition = build_momentum_edge_attrition_audit(
        counter_feature_rows,
        counter_signal_rows,
        counter_portfolios,
        trusted_reverse_series,
    )
    trusted_momentum_reconciliation = (
        build_trusted_momentum_reconciliation(
            counter_feature_rows,
            counter_signal_rows,
            counter_portfolios,
            trusted_reverse_series,
            momentum_reconciliation_reference or {},
            waterfall_reference=momentum_edge_attrition,
            lane_label="ROLLING_LATEST",
        )
        if momentum_reconciliation_reference
        else {
            "mode": "TRUSTED_MOMENTUM_RECONCILIATION_V12",
            "lane": "ROLLING_LATEST",
            "status": "NOT_REQUESTED",
            "live_mutation": False,
        }
    )
    fixed_benchmark_momentum_reconciliation = (
        build_trusted_momentum_reconciliation(
            benchmark_feature_rows,
            benchmark_signal_rows,
            benchmark_portfolios,
            fixed_benchmark_trusted_series,
            momentum_reconciliation_reference or {},
            waterfall_reference=None,
            lane_label="FIXED_BENCHMARK",
        )
        if momentum_reconciliation_reference and benchmark_feature_rows
        else {
            "mode": "TRUSTED_MOMENTUM_RECONCILIATION_V12",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
        }
    )
    if (
        momentum_reconciliation_reference
        and str(trusted_momentum_reconciliation.get("status") or "").upper() != "EVALUABLE"
    ):
        quality.setdefault("errors", []).append("TRUSTED_MOMENTUM_RECONCILIATION_NOT_EVALUABLE")
        quality["status"] = "INVALID"

    quality["stateful_boundary_lifecycle"] = {
        key: value for key, value in lifecycle_current.items() if key != "trades"
    }
    quality["entry_research_input_integrity"] = entry_research_input_integrity
    if not entry_research_input_integrity["full_universe_delivered"]:
        quality.setdefault("errors", []).append("ENTRY_RESEARCH_INPUT_TRUNCATED")
        quality["status"] = "INVALID"
    quality["entry_quality_walkforward"] = {
        key: value for key, value in entry_quality_walkforward.items()
        if key not in {"scored_trades", "latest_train_model"}
    }
    quality["entry_quality_cross_sectional"] = {
        key: value for key, value in entry_quality_cross_sectional.items()
        if key not in {"date_results", "latest_train_model"}
    }
    quality["entry_quality_eligibility"] = {
        key: value for key, value in entry_quality_eligibility.items()
        if key not in {"date_results", "latest_train_model", "latest_positive_stable_features"}
    }
    quality["entry_eligibility_falsification"] = {
        "rolling": {
            key: value for key, value in entry_eligibility_falsification_rolling.items()
            if key != "date_diagnostics"
        },
        "fixed_benchmark": {
            key: value for key, value in entry_eligibility_falsification_fixed.items()
            if key != "date_diagnostics"
        },
        "decision": entry_eligibility_dual_lane_decision,
    }
    quality["thesis_quality_completion"] = {
        key: value for key, value in thesis_quality.items()
        if key not in {"date_results", "latest_train_model"}
    }
    quality["momentum_direction_audit"] = momentum_direction_audit
    quality["momentum_edge_attrition"] = momentum_edge_attrition
    quality["economic_waterfall_calibration"] = economic_waterfall_calibration
    quality["economic_architecture_challengers"] = economic_architecture_challengers
    quality["frozen_config_historical_simulation"] = frozen_config_historical_simulation
    quality["scanner_recall_ranking_calibration"] = scanner_recall_ranking_calibration
    quality["trusted_momentum_reconciliation"] = trusted_momentum_reconciliation
    quality["fixed_benchmark_momentum_reconciliation"] = fixed_benchmark_momentum_reconciliation
    if int(lifecycle_current.get("input_trades") or 0) > 0 and int(lifecycle_current.get("evaluable") or 0) <= 0:
        quality.setdefault("errors", []).append("STATEFUL_LIFECYCLE_ZERO_EVALUABLE_TRADES")
        quality["status"] = "INVALID"

    current_metrics = _metrics(counter_trades)
    fixed_benchmark_metrics = _metrics(benchmark_trades)
    enter_first_metrics = _metrics(enter_first_trades)
    quality_metrics = _metrics(quality_trades)
    reverse_threshold_summary = reverse.get("threshold_5pct_within_5_sessions") or {}
    reverse_top_summary = reverse.get("top5_daily_by_horizon") or {}

    current_bot_history_reverse_comparison = {
        "mode": "CURRENT_BOT_APPLIED_TO_POINT_IN_TIME_HISTORY_PLUS_REVERSE",
        "live_mutation": False,
        "forward_current_policy": {
            "metrics": current_metrics,
            "forensics": current_forensics,
            "selection_compression": selection_compression,
            "reverse_overlap": {
                key: value for key, value in reverse_overlap.items() if key != "trade_digest"
            },
        },
        "fixed_benchmark_current_policy": {
            "metrics": fixed_benchmark_metrics,
            "diagnostics": benchmark_diag,
            "portfolios": len(benchmark_portfolios),
            "trades": len(benchmark_trades),
            "same_run_keys_as_rolling_at_anchor": benchmark_reuses_rolling,
            "benchmark_id": (research_benchmark or {}).get("benchmark_id"),
            "price_outcome_cutoff_date": benchmark_price_cutoff_date,
            "exact_feature_observation_ids_frozen": bool(benchmark_feature_ids),
        },
        "broad_actionable_enter_reference": {
            "metrics": enter_first_metrics,
            "forensics": enter_first_forensics,
            "selection_compression": enter_first_compression,
        },
        "quality_gated_reference": {
            "metrics": quality_metrics,
            "forensics": quality_forensics,
            "selection_compression": quality_compression,
            "gate_diagnostics": {
                **dict(quality_gate_totals),
                "disqualification_reasons": dict(quality_gate_reasons),
            },
            "reverse_overlap": {
                key: value
                for key, value in (quality_reverse.get("overlap") or {}).items()
                if key != "trade_digest"
            },
        },
        "reverse_false_negative_view": {
            "threshold_5pct_within_5_sessions": reverse_threshold_summary,
            "top5_daily_by_horizon": reverse_top_summary,
        },
        "comparison": {
            "quality_vs_broad_enter": {
                "closed_delta": int((quality_metrics.get("closed") or 0) - (enter_first_metrics.get("closed") or 0)),
                "avg_return_lift_pp": round(
                    float(quality_metrics.get("avg_underlying_return_pct") or 0.0)
                    - float(enter_first_metrics.get("avg_underlying_return_pct") or 0.0),
                    4,
                ),
                "profit_factor_lift": round(
                    float(quality_metrics.get("underlying_profit_factor") or 0.0)
                    - float(enter_first_metrics.get("underlying_profit_factor") or 0.0),
                    4,
                ),
                "drawdown_reduction_points": round(
                    float(enter_first_metrics.get("trade_sequence_max_drawdown_pct") or 0.0)
                    - float(quality_metrics.get("trade_sequence_max_drawdown_pct") or 0.0),
                    4,
                ),
            },
        },
        "methodology": {
            "current_code_replayed_on_historical_point_in_time_features": True,
            "reverse_uses_future_prices_only_as_outcome_labels": True,
            "feature_close_fallback_used_only_for_reverse_path_coverage": True,
            "no_new_live_rule_promoted_from_this_comparison": True,
        },
    }

    _progress("finalization", "assembling quality metrics and research artifacts")
    summary = {
        "schema_version": 2,
        "mode": "HISTORICAL_OPERATIONAL_VALIDATION_SHADOW",
        "version": config.BOT_VERSION,
        "semantic_quality": quality,
        "current_bot_history_reverse_comparison": current_bot_history_reverse_comparison,
        "product_replay": {
            "historical_emitted_book": {
                "mode": "HISTORICAL_EMITTED_PIPELINE",
                "portfolios": len(emitted_portfolios),
                "dates": len({
                    str(row.get("asof") or "") for row in emitted_portfolios
                }),
                "trades": len(emitted_trades),
                "metrics": _metrics(emitted_trades),
                "full_deep_metrics": _metrics(emitted_closed_full),
                "interpretation": "what historical TMAD versions actually emitted",
            },
            "current_policy_counterfactual": {
                "mode": "RETROSPECTIVE_CURRENT_CODE_ON_POINT_IN_TIME_FEATURES",
                "diagnostics": counter_diag,
                "selection_compression": selection_compression,
                "reverse_trade_overlap": reverse_overlap,
                "portfolios": len(counter_portfolios),
                "dates": len({
                    str(row.get("asof") or "") for row in counter_portfolios
                }),
                "trades": len(counter_trades),
                "metrics": current_metrics,
                "forensics": current_forensics,
                "full_deep_metrics": _metrics(counter_closed_full),
                "lifecycle_session_integrity": baseline_lifecycle,
                "interpretation": (
                    "what current code would select on historical snapshots; "
                    "historical lifecycle uses global market-session fallback when ticker coverage is sparse"
                ),
                "historical_session_count_policy": "GLOBAL_MARKET_CALENDAR_FALLBACK",
                "unbiased_oos_proof": False,
            },
            "fixed_benchmark_current_policy": {
                "mode": "FIXED_BENCHMARK_RETROSPECTIVE_CURRENT_CODE",
                "benchmark_id": (research_benchmark or {}).get("benchmark_id"),
                "diagnostics": benchmark_diag,
                "portfolios": len(benchmark_portfolios),
                "dates": len({
                    str(row.get("asof") or "") for row in benchmark_portfolios
                }),
                "trades": len(benchmark_trades),
                "metrics": fixed_benchmark_metrics,
                "same_run_keys_as_rolling_at_anchor": benchmark_reuses_rolling,
                "price_outcome_cutoff_date": benchmark_price_cutoff_date,
                "exact_feature_observation_ids_frozen": bool(benchmark_feature_ids),
                "unbiased_oos_proof": False,
                "interpretation": "same frozen Scanner run cohort across code versions; current code applied retrospectively",
            },
        },
        "research_challengers": {
            "economic_waterfall_calibration": {
                **economic_waterfall_calibration,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
            },
            "economic_architecture_challengers": {
                **economic_architecture_challengers,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
                "gex_timing_lifecycle_frozen": True,
            },
            "frozen_config_historical_simulation": {
                **frozen_config_historical_simulation,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
                "purpose": "detect config interaction/implementation errors before prospective maturity",
            },
            "scanner_recall_ranking_calibration": {
                **scanner_recall_ranking_calibration,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
                "scanner_cap_live_unchanged": True,
            },
            "scanner_core_interaction_simulation": {
                **scanner_core_interaction_simulation,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
                "scanner_live_unchanged": True,
                "purpose": "simulate combined config and detect interaction errors",
            },
            "scanner_direction_aware_ranking": {
                **scanner_direction_aware_ranking,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
                "scanner_live_unchanged": True,
            },
            "gex_gate_calibration": {
                **gex_gate_calibration,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
                "gex_live_unchanged": True,
                "coverage_gap_excluded_from_gate_decision": True,
            },
            "fusion_gate_calibration": {
                **fusion_gate_calibration,
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
                "fusion_live_unchanged": True,
                "price_lane": "same trusted_reverse_series used by Reverse Stress",
            },
            "trusted_momentum_reconciliation": trusted_momentum_reconciliation,
            "fixed_benchmark_momentum_reconciliation": fixed_benchmark_momentum_reconciliation,
            "momentum_edge_attrition": {
                **momentum_edge_attrition,
                "promotion_scope": "RESEARCH_ONLY",
            },
            "momentum_direction_audit": {
                **momentum_direction_audit,
                "candidate_portfolios": len(all_enter_portfolios),
                "candidate_trades_before_outcome": len(all_enter_candidate_trades),
                "promotion_scope": "RESEARCH_ONLY",
            },
            "thesis_quality_completion_walkforward": {
                **{
                    key: value for key, value in thesis_quality.items()
                    if key != "date_results"
                },
                "candidate_portfolios": len(all_enter_portfolios),
                "candidate_trades_before_outcome": len(all_enter_candidate_trades),
                "unbiased_oos_proof": False,
                "promotion_scope": "RESEARCH_ONLY",
            },
            "entry_quality_eligibility_walkforward": {
                **{
                    key: value for key, value in entry_quality_eligibility.items()
                    if key != "date_results"
                },
                "candidate_portfolios": len(all_enter_portfolios),
                "candidate_trades_before_outcome": len(all_enter_candidate_trades),
                "unbiased_oos_proof": False,
                "promotion_scope": "RESEARCH_ONLY",
            },
            "entry_eligibility_historical_falsification": {
                "rolling_latest": {
                    key: value for key, value in entry_eligibility_falsification_rolling.items()
                    if key != "date_diagnostics"
                },
                "fixed_benchmark": {
                    key: value for key, value in entry_eligibility_falsification_fixed.items()
                    if key != "date_diagnostics"
                },
                "decision": entry_eligibility_dual_lane_decision,
                "fixed_benchmark_candidate_portfolios": len(benchmark_all_enter_portfolios),
                "fixed_benchmark_candidate_trades_before_outcome": len(benchmark_all_enter_candidate_trades),
                "promotion_scope": "RESEARCH_ONLY",
                "prospective_required_before_any_live_change": True,
            },
            "entry_quality_cross_sectional_walkforward": {
                **{
                    key: value for key, value in entry_quality_cross_sectional.items()
                    if key != "date_results"
                },
                "candidate_portfolios": len(all_enter_portfolios),
                "candidate_trades_before_outcome": len(all_enter_candidate_trades),
                "unbiased_oos_proof": False,
                "promotion_scope": "RESEARCH_ONLY",
            },
            "entry_research_input_integrity": entry_research_input_integrity,
            "entry_quality_walkforward": {
                **{
                    key: value for key, value in entry_quality_walkforward.items()
                    if key != "scored_trades"
                },
                "unbiased_oos_proof": False,
                "promotion_scope": "RESEARCH_ONLY",
            },
            "stateful_boundary_lifecycle": {
                "mode": "CANONICAL_TRUSTED_PRICE_LANE",
                "live_mutation": False,
                "current_policy": {k: v for k, v in lifecycle_books["current_policy"].items() if k != "trades"},
                "enter_first": {k: v for k, v in lifecycle_books["enter_first"].items() if k != "trades"},
                "quality_gated": {k: v for k, v in lifecycle_books["quality_gated"].items() if k != "trades"},
                "all_actionable_enter_candidates": {k: v for k, v in lifecycle_books["all_actionable_enter_candidates"].items() if k != "trades"},
                "fixed_benchmark_all_actionable_enter_candidates": {k: v for k, v in lifecycle_books["fixed_benchmark_all_actionable_enter_candidates"].items() if k != "trades"},
                "historical_emitted": {k: v for k, v in lifecycle_books["historical_emitted"].items() if k != "trades"},
                "price_lane": "same trusted_reverse_series used by Reverse Stress",
                "future_labels_used_only_after_entry": True,
                "unbiased_oos_proof": False,
            },
            "actionable_enter_first_top5": {
                "mode": "SHADOW_SELECTION_POLICY_CHALLENGER",
                "live_mutation": False,
                "single_change": "prioritize_actionable_ENTER_before_current_score_ranking",
                "max_positions": int(max_positions),
                "selection_compression": enter_first_compression,
                "portfolios": len(enter_first_portfolios),
                "dates": len({
                    str(row.get("asof") or "") for row in enter_first_portfolios
                }),
                "trades": len(enter_first_trades),
                "metrics": enter_first_metrics,
                "forensics": enter_first_forensics,
                "full_deep_metrics": _metrics(enter_first_closed_full),
                "reverse_capture": enter_first_reverse,
                "benchmark_current_policy": {
                    "trades": len(counter_trades),
                    "metrics": _metrics(counter_trades),
                    "selection_compression": selection_compression,
                },
                "interpretation": (
                    "broad ENTER-first reference challenger retained after v6.1.131 rejection; "
                    "now uses the corrected historical lifecycle so selection is isolated cleanly"
                ),
                "lifecycle_session_integrity": _lifecycle_session_diagnostics(enter_first_trades),
                "unbiased_oos_proof": False,
            },
            "market_session_fallback": {
                "mode": "PROMOTED_HISTORICAL_BASELINE_COMPATIBILITY_VIEW",
                "live_mutation": False,
                "single_change": "fallback_to_global_market_sessions_when_ticker_price_series_has_zero_sessions",
                "promoted_to_historical_baseline": True,
                "trades": len(session_fallback_trades),
                "metrics": _metrics(session_fallback_trades),
                "full_deep_metrics": _metrics(session_fallback_closed_full),
                "lifecycle_session_integrity": _lifecycle_session_diagnostics(session_fallback_trades),
                "reverse_capture": session_fallback_reverse,
                "unbiased_oos_proof": False,
            },
            "legacy_ticker_session_count": {
                "mode": "LEGACY_REPLAY_DIAGNOSTIC",
                "live_mutation": False,
                "session_count_policy": "TICKER_PRICE_CACHE_ONLY",
                "trades": len(legacy_ticker_session_trades),
                "metrics": _metrics(legacy_ticker_session_trades),
                "lifecycle_session_integrity": _lifecycle_session_diagnostics(legacy_ticker_session_trades),
                "interpretation": (
                    "pre-v6.1.132 historical lifecycle retained only to prove the old zero-session defect"
                ),
                "unbiased_oos_proof": False,
            },
            "quality_gated_enter_top5": {
                "mode": "SHADOW_STRUCTURAL_QUALITY_CHALLENGER",
                "live_mutation": False,
                "max_positions": int(max_positions),
                "future_labels_used_for_gate": False,
                "gate_definition": {
                    "direction": "CORE_GEX_ALIGNED",
                    "timing": "intraday_state_ENTER",
                    "regime": "not_BLOCK_direction_and_not_PARALLEL_CLOSE_ALL_and_not_countertrend_override",
                    "numeric_pnl_thresholds": False,
                },
                "gate_diagnostics": {
                    **dict(quality_gate_totals),
                    "disqualification_reasons": dict(quality_gate_reasons),
                },
                "selection_compression": quality_compression,
                "portfolios": len(quality_portfolios),
                "dates": len({
                    str(row.get("asof") or "") for row in quality_portfolios
                }),
                "trades": len(quality_trades),
                "metrics": quality_metrics,
                "forensics": quality_forensics,
                "full_deep_metrics": _metrics(quality_closed_full),
                "lifecycle_session_integrity": _lifecycle_session_diagnostics(quality_trades),
                "reverse_capture": quality_reverse,
                "benchmark_current_policy": {
                    "trades": len(counter_trades),
                    "metrics": _metrics(counter_trades),
                    "selection_compression": selection_compression,
                },
                "interpretation": (
                    "tests whether structural direction/timing/regime agreement selects a smaller, "
                    "cleaner actionable ENTER subset without hindsight-fitted thresholds"
                ),
                "unbiased_oos_proof": False,
            },
            "actionable_enter_first_plus_market_session_fallback": {
                "mode": "SHADOW_COMBINED_CHALLENGER",
                "live_mutation": False,
                "changes": [
                    "prioritize_actionable_ENTER_before_current_score_ranking",
                    "global_market_session_fallback_now_baseline",
                ],
                "redundant_with": "actionable_enter_first_top5",
                "selection_compression": _selection_compression_diagnostics(
                    counter_signal_rows, enter_first_portfolios, combined_trades
                ),
                "trades": len(combined_trades),
                "metrics": _metrics(combined_trades),
                "full_deep_metrics": _metrics(combined_closed_full),
                "lifecycle_session_integrity": _lifecycle_session_diagnostics(combined_trades),
                "reverse_capture": combined_reverse,
                "unbiased_oos_proof": False,
            },
        },
        "reverse_stress": {
            key: value for key, value in reverse.items()
            if key not in {"threshold_movement_ledger", "top_opportunity_ledger"}
        },
        "history_bridge": {
            "full_deep": bridge,
            "counterfactual_reverse_universe": {
                "dates": len({day for day, _ticker_key in reverse_canonical}),
                "rows": len(reverse_canonical),
                "full_deep_dates": sum(
                    1 for row in universe_meta.values()
                    if row.get("universe_scope") == "FULL_SCANNER_DEEP"
                ),
                "subset_dates": sum(
                    1 for row in universe_meta.values()
                    if row.get("universe_scope") != "FULL_SCANNER_DEEP"
                ),
            },
            "observation_outcomes": outcome_bridge,
            "prices": price_bridge,
            "reverse_canonical_cache": reverse.get("canonical_cache") or {},
        },
        "policy": {
            "research_only": True,
            "live_mutation": False,
            "price_sources_read_only": True,
            "future_prices_used_only_after_open_or_as_reverse_labels": True,
            "historical_emitted_and_current_policy_books_are_separate": True,
            "current_policy_counterfactual_uses_current_model_retrospectively": True,
            "oot_challengers_remain_required_for_unbiased_promotion": True,
            "signal_entry_is_recorded_point_in_time_spot": True,
            "next_open_sensitivity_is_reported": True,
            "target_is_runner_milestone_not_automatic_close": True,
            "synthetic_option_proxy_is_not_real_option_pnl": True,
            "same_contract_strike_expiry_repriced": True,
            "historical_results_do_not_auto_promote_live": True,
            "historical_session_fallback_promoted_as_replay_correctness_fix_only": True,
            "quality_gated_enter_remains_shadow_only": True,
            "feature_close_fallback_is_reverse_diagnostic_only": True,
            "current_bot_history_reverse_comparison_is_primary_research_view": True,
        },
    }
    return {
        "summary": summary,
        # Primary ledger answers "what would current TMAD have traded?"
        "trade_ledger": counter_trades,
        "emitted_trade_ledger": emitted_trades,
        "enter_first_trade_ledger": enter_first_trades,
        "quality_gated_trade_ledger": quality_trades,
        "stateful_boundary_lifecycle": lifecycle_books,
        "entry_quality_walkforward": entry_quality_walkforward,
        "entry_quality_cross_sectional_walkforward": entry_quality_cross_sectional,
        "entry_quality_eligibility_walkforward": entry_quality_eligibility,
        "entry_eligibility_falsification_rolling": entry_eligibility_falsification_rolling,
        "entry_eligibility_falsification_fixed": entry_eligibility_falsification_fixed,
        "entry_eligibility_dual_lane_decision": entry_eligibility_dual_lane_decision,
        "fixed_benchmark_all_actionable_enter_candidate_trades": benchmark_all_enter_candidate_trades,
        "momentum_direction_audit": momentum_direction_audit,
        "momentum_edge_attrition": momentum_edge_attrition,
        "economic_waterfall_calibration": economic_waterfall_calibration,
        "economic_architecture_challengers": economic_architecture_challengers,
        "frozen_config_historical_simulation": frozen_config_historical_simulation,
        "scanner_recall_ranking_calibration": scanner_recall_ranking_calibration,
        "scanner_core_interaction_simulation": scanner_core_interaction_simulation,
        "scanner_direction_aware_ranking": scanner_direction_aware_ranking,
        "core_context_precision_optimizer": core_context_precision_optimizer,
        "gex_gate_calibration": gex_gate_calibration,
        "fusion_gate_calibration": fusion_gate_calibration,
        "thesis_quality_completion_walkforward": thesis_quality,
        "all_actionable_enter_candidate_trades": all_enter_candidate_trades,
        "current_bot_history_reverse_comparison": current_bot_history_reverse_comparison,
        "reverse_threshold_ledger": reverse.get("threshold_movement_ledger") or [],
        "reverse_top_opportunity_ledger": reverse.get("top_opportunity_ledger") or [],
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    flattened: list[dict] = []
    for row in rows:
        item: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                item[key] = json.dumps(value, ensure_ascii=False, default=str)
            else:
                item[key] = value
        flattened.append(item)
    fields = sorted({key for row in flattened for key in row})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flattened)


def write_historical_operational_artifacts(payload: dict) -> dict[str, str]:
    out = Path(config.OUTPUT_DIR) / "historical_validation"
    out.mkdir(parents=True, exist_ok=True)
    summary = out / "latest_historical_validation_summary.json"
    trades = out / "latest_historical_trade_ledger.json"
    reverse_threshold = out / "latest_reverse_threshold_ledger.json"
    reverse_top = out / "latest_reverse_top_opportunity_ledger.json"
    trades_csv = out / "latest_historical_trade_ledger.csv"
    reverse_threshold_csv = out / "latest_reverse_threshold_ledger.csv"
    reverse_top_csv = out / "latest_reverse_top_opportunity_ledger.csv"
    emitted_trades = out / "latest_historical_emitted_trade_ledger.json"
    emitted_trades_csv = out / "latest_historical_emitted_trade_ledger.csv"
    enter_first_trades = out / "latest_enter_first_trade_ledger.json"
    enter_first_trades_csv = out / "latest_enter_first_trade_ledger.csv"
    quality_trades = out / "latest_quality_gated_trade_ledger.json"
    quality_trades_csv = out / "latest_quality_gated_trade_ledger.csv"
    comparison = out / "latest_current_bot_history_reverse_comparison.json"
    lifecycle = out / "latest_stateful_boundary_lifecycle.json"
    entry_quality = out / "latest_entry_quality_walkforward.json"
    entry_quality_cross_sectional = out / "latest_entry_quality_cross_sectional_walkforward.json"
    entry_quality_eligibility = out / "latest_entry_quality_eligibility_walkforward.json"
    entry_eligibility_falsification_rolling = out / "latest_entry_eligibility_falsification_rolling.json"
    entry_eligibility_falsification_fixed = out / "latest_entry_eligibility_falsification_fixed_benchmark.json"
    entry_eligibility_dual_lane_decision = out / "latest_entry_eligibility_dual_lane_decision.json"
    fixed_benchmark_enter_candidates = out / "latest_fixed_benchmark_all_actionable_enter_candidates.json"
    momentum_direction = out / "latest_momentum_direction_audit.json"
    momentum_attrition = out / "latest_momentum_edge_attrition.json"
    economic_waterfall = out / "latest_economic_waterfall_calibration.json"
    economic_architecture = out / "latest_economic_architecture_challengers.json"
    frozen_config_sim = out / "latest_frozen_config_historical_simulation.json"
    scanner_calibration = out / "latest_scanner_recall_ranking_calibration.json"
    scanner_core_interaction = out / "latest_scanner_core_interaction_simulation.json"
    scanner_direction_aware = out / "latest_scanner_direction_aware_ranking.json"
    core_context_precision = out / "latest_core_context_precision_optimizer.json"
    autonomous_research_summary = out / "latest_autonomous_research_summary.json"
    gex_gate = out / "latest_gex_gate_calibration.json"
    fusion_gate = out / "latest_fusion_gate_calibration.json"
    thesis_quality = out / "latest_thesis_quality_completion_walkforward.json"
    all_enter_candidates = out / "latest_all_actionable_enter_candidates.json"
    trade_rows = payload.get("trade_ledger") or []
    emitted_rows = payload.get("emitted_trade_ledger") or []
    enter_first_rows = payload.get("enter_first_trade_ledger") or []
    quality_rows = payload.get("quality_gated_trade_ledger") or []
    threshold_rows = payload.get("reverse_threshold_ledger") or []
    top_rows = payload.get("reverse_top_opportunity_ledger") or []
    summary.write_text(json.dumps(payload.get("summary") or {}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    trades.write_text(json.dumps(trade_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    emitted_trades.write_text(json.dumps(emitted_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    enter_first_trades.write_text(json.dumps(enter_first_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    quality_trades.write_text(json.dumps(quality_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    comparison.write_text(
        json.dumps(payload.get("current_bot_history_reverse_comparison") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    lifecycle.write_text(
        json.dumps(payload.get("stateful_boundary_lifecycle") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    entry_quality.write_text(
        json.dumps(payload.get("entry_quality_walkforward") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    entry_quality_cross_sectional.write_text(
        json.dumps(payload.get("entry_quality_cross_sectional_walkforward") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    entry_quality_eligibility.write_text(
        json.dumps(payload.get("entry_quality_eligibility_walkforward") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    entry_eligibility_falsification_rolling.write_text(
        json.dumps(payload.get("entry_eligibility_falsification_rolling") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    entry_eligibility_falsification_fixed.write_text(
        json.dumps(payload.get("entry_eligibility_falsification_fixed") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    entry_eligibility_dual_lane_decision.write_text(
        json.dumps(payload.get("entry_eligibility_dual_lane_decision") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    fixed_benchmark_enter_candidates.write_text(
        json.dumps(payload.get("fixed_benchmark_all_actionable_enter_candidate_trades") or [], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    momentum_direction.write_text(
        json.dumps(payload.get("momentum_direction_audit") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    momentum_attrition.write_text(
        json.dumps(payload.get("momentum_edge_attrition") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    economic_waterfall.write_text(
        json.dumps(payload.get("economic_waterfall_calibration") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    economic_architecture.write_text(
        json.dumps(payload.get("economic_architecture_challengers") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    frozen_config_sim.write_text(
        json.dumps(payload.get("frozen_config_historical_simulation") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    scanner_calibration.write_text(
        json.dumps(payload.get("scanner_recall_ranking_calibration") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    scanner_core_interaction.write_text(
        json.dumps(payload.get("scanner_core_interaction_simulation") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    scanner_direction_aware.write_text(
        json.dumps(payload.get("scanner_direction_aware_ranking") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    core_context_payload = payload.get("core_context_precision_optimizer") or {}
    core_context_precision.write_text(
        json.dumps(core_context_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    autonomous_research_summary.write_text(
        json.dumps(
            build_autonomous_research_summary(
                core_context_payload,
                producer_version=config.BOT_VERSION,
            ),
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    gex_gate.write_text(
        json.dumps(payload.get("gex_gate_calibration") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    fusion_gate.write_text(
        json.dumps(payload.get("fusion_gate_calibration") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    thesis_quality.write_text(
        json.dumps(payload.get("thesis_quality_completion_walkforward") or {}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    all_enter_candidates.write_text(
        json.dumps(payload.get("all_actionable_enter_candidate_trades") or [], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    reverse_threshold.write_text(json.dumps(threshold_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    reverse_top.write_text(json.dumps(top_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_csv(trades_csv, trade_rows)
    _write_csv(emitted_trades_csv, emitted_rows)
    _write_csv(enter_first_trades_csv, enter_first_rows)
    _write_csv(quality_trades_csv, quality_rows)
    _write_csv(reverse_threshold_csv, threshold_rows)
    _write_csv(reverse_top_csv, top_rows)
    return {
        "summary": str(summary),
        "trade_ledger": str(trades),
        "trade_ledger_csv": str(trades_csv),
        "emitted_trade_ledger": str(emitted_trades),
        "emitted_trade_ledger_csv": str(emitted_trades_csv),
        "enter_first_trade_ledger": str(enter_first_trades),
        "enter_first_trade_ledger_csv": str(enter_first_trades_csv),
        "quality_gated_trade_ledger": str(quality_trades),
        "quality_gated_trade_ledger_csv": str(quality_trades_csv),
        "current_bot_history_reverse_comparison": str(comparison),
        "stateful_boundary_lifecycle": str(lifecycle),
        "entry_quality_walkforward": str(entry_quality),
        "entry_quality_cross_sectional_walkforward": str(entry_quality_cross_sectional),
        "entry_quality_eligibility_walkforward": str(entry_quality_eligibility),
        "entry_eligibility_falsification_rolling": str(entry_eligibility_falsification_rolling),
        "entry_eligibility_falsification_fixed_benchmark": str(entry_eligibility_falsification_fixed),
        "entry_eligibility_dual_lane_decision": str(entry_eligibility_dual_lane_decision),
        "fixed_benchmark_all_actionable_enter_candidates": str(fixed_benchmark_enter_candidates),
        "momentum_direction_audit": str(momentum_direction),
        "momentum_edge_attrition": str(momentum_attrition),
        "economic_waterfall_calibration": str(economic_waterfall),
        "economic_architecture_challengers": str(economic_architecture),
        "frozen_config_historical_simulation": str(frozen_config_sim),
        "scanner_recall_ranking_calibration": str(scanner_calibration),
        "scanner_core_interaction_simulation": str(scanner_core_interaction),
        "scanner_direction_aware_ranking": str(scanner_direction_aware),
        "core_context_precision_optimizer": str(core_context_precision),
        "autonomous_research_summary": str(autonomous_research_summary),
        "gex_gate_calibration": str(gex_gate),
        "fusion_gate_calibration": str(fusion_gate),
        "thesis_quality_completion_walkforward": str(thesis_quality),
        "all_actionable_enter_candidates": str(all_enter_candidates),
        "reverse_threshold_ledger": str(reverse_threshold),
        "reverse_threshold_ledger_csv": str(reverse_threshold_csv),
        "reverse_top_opportunity_ledger": str(reverse_top),
        "reverse_top_opportunity_ledger_csv": str(reverse_top_csv),
    }
