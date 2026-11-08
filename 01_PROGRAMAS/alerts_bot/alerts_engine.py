from __future__ import annotations

from collections import Counter
from datetime import datetime
import time
from zoneinfo import ZoneInfo

import config
from db import (
    activate_due_model,
    cooldown_ok,
    data_dbs_available,
    get_active_model,
    get_best_model_for_tier,
    has_open_trade,
    insert_alert_event,
    insert_feature_observation,
    upsert_feature_observations_bulk,
    latest_expirations_by_ticker,
    latest_expiry_buckets_by_ticker,
    latest_gex_by_ticker,
    latest_scanner_snapshots,
    latest_successful_scanner_run,
    latest_scanner_run_any,
    scanner_run_by_id,
    mark_alert_sent,
    mark_research_alert_sent,
    get_asset_relationships,
    previous_feature_map,
    insert_relational_context,
    get_runtime_state,
    get_open_trades,
    upsert_asset_universe_nodes,
    daily_alert_count,
)
from discord_utils import log_error, log_info, log_warning, send_alert, send_shadow, send_research, send_preliminary_alert
from alerts_bulk_persistence import upsert_relational_contexts_bulk
from lifecycle import update_trade_lifecycle
from entry_gate import decide_entry_gate
from rules import build_event, build_features, feature_observation, format_alert_card
from relational_features import attach_relational_features, build_relational_context
from asset_universe import infer_structural_relationships, classify_asset
from time_utils import now_madrid
from transition_features import add_transition_features
from fusion_engine import build_fusion_signals
from layer_history import (
    ensure_layer_history_schema, materialize_layer_outcomes, persist_layer_signals,
    persist_layer_features, persist_market_regime,
)
from layer_calibration import get_layer_calibrations, refresh_layer_calibrations
from layer_archive import archive_completed_layer_dates
from layer_reporting import maybe_send_layer_calibration
from discord_presentation import build_group
from calibration_v621 import send_alert_traceability
from positions_overview import maybe_send_open_positions_overview
from reporting_v621 import emit_console_snapshot
from market_outcome_auditor import maybe_run_market_outcome_audit
from adaptive_learning import maybe_run_adaptive_learning, effective_research_gate
from market_regime import (
    attach_market_confirmation_layers, evaluate_market_close_all_signals,
    maybe_send_market_close_all_signals,
)


LEVERAGED_EXPOSURE_BUCKETS = {
    "SOXL": "SEMICONDUCTORS", "SOXS": "SEMICONDUCTORS", "SOXX": "SEMICONDUCTORS", "SMH": "SEMICONDUCTORS",
    "TQQQ": "NASDAQ100", "SQQQ": "NASDAQ100", "QQQ": "NASDAQ100",
    "SPXL": "SP500", "SPXS": "SP500", "UPRO": "SP500", "SPXU": "SP500", "SPY": "SP500",
    "TNA": "RUSSELL2000", "TZA": "RUSSELL2000", "IWM": "RUSSELL2000",
    "LABU": "BIOTECH", "LABD": "BIOTECH", "XBI": "BIOTECH",
    "NUGT": "GOLD_MINERS", "DUST": "GOLD_MINERS", "GDX": "GOLD_MINERS",
    "BOIL": "NATGAS", "KOLD": "NATGAS", "UNG": "NATGAS",
}


def _portfolio_sector_bucket(ticker: str, features: dict | None = None) -> str:
    symbol = str(ticker or "").upper()
    if symbol in LEVERAGED_EXPOSURE_BUCKETS:
        return LEVERAGED_EXPOSURE_BUCKETS[symbol]
    data = features or {}
    sector = str(data.get("sector_etf") or data.get("sector") or data.get("industry") or "UNMAPPED").upper()
    return sector if sector and sector != "UNMAPPED" else f"TICKER:{symbol or 'UNKNOWN'}"


def _open_portfolio_counts(*, live_mode: bool, shadow_mode: bool) -> tuple[Counter[str], Counter[str]]:
    sectors: Counter[str] = Counter()
    directions: Counter[str] = Counter()
    for trade in get_open_trades():
        status = str(trade.get("alert_status") or "").lower()
        is_live = status in {"open", "sent"}
        is_shadow = status.startswith("shadow")
        if (live_mode and not is_live) or (shadow_mode and not is_shadow):
            continue
        try:
            entry_features = __import__("json").loads(trade.get("entry_features_json") or "{}")
        except Exception:
            entry_features = {}
        sectors[_portfolio_sector_bucket(str(trade.get("ticker") or ""), entry_features)] += 1
        directions[str(trade.get("direction") or "")] += 1
    return sectors, directions


def _gex_run_counts(snapshots: list[dict], valid_count: int, stale_count: int) -> dict[str, int]:
    not_selected = 0
    requested = 0
    failed = 0
    for snapshot in snapshots:
        status = str(snapshot.get("gex_status") or "").lower()
        if status in {"", "not_selected", "not_requested", "scanner_only", "skipped"}:
            not_selected += 1
        else:
            requested += 1
            if status != "ok":
                failed += 1
    return {
        "requested": requested,
        "valid": int(valid_count),
        "failed": max(failed, int(stale_count), requested - int(valid_count)),
        "not_enriched": not_selected,
    }


def _age_minutes(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        return (now_madrid() - datetime.fromisoformat(timestamp)).total_seconds() / 60.0
    except Exception:
        return None


def _clock_minutes(value: str) -> int:
    hour, minute = [int(x) for x in value.split(":", 1)]
    return hour * 60 + minute




def _discord_session_badge(session_type: object) -> str:
    session = str(session_type or "").lower()
    if any(token in session for token in ("premarket", "startup", "first_run")):
        return "[Pre-market ☀️]"
    if any(token in session for token in ("deep", "postmarket", "close")):
        return "[Post-market 🌙]"
    return "[Market 🌐]"

def _entry_window_open() -> bool:
    if config.ALLOW_ALERTS_OUTSIDE_MARKET:
        return True
    try:
        now_market = datetime.now(ZoneInfo(config.ENTRY_MARKET_TIMEZONE))
        if now_market.weekday() >= 5:
            return False
        current = now_market.hour * 60 + now_market.minute
        return _clock_minutes(config.ENTRY_WINDOW_START) <= current <= _clock_minutes(config.ENTRY_WINDOW_END)
    except Exception:
        return False


def _precision_first_gate(candidate: dict, features: dict, model: dict | None = None) -> tuple[bool, int, list[str]]:
    """Conservative live gate: fewer alerts, stronger independent confirmation.

    The calibrated rule still determines the setup. This gate requires the live
    state to match the high-confidence operating profile and counts independent
    market/sector/leveraged/relationship blocks rather than duplicate votes.
    """
    if config.ALERT_SELECTION_PROFILE != "precision_first":
        return True, 0, []
    direction = str(candidate.get("direction") or "").lower()
    sign = 1.0 if direction == "long" else -1.0
    score = float(candidate.get("score") or 0.0)
    projection = float(candidate.get("daily_pattern_projection_pct") or 0.0)
    if score < config.PRECISION_FIRST_MIN_LIVE_SCORE:
        return False, 0, [f"score {score:.1f}<{config.PRECISION_FIRST_MIN_LIVE_SCORE:.1f}"]
    if projection < config.PRECISION_FIRST_MIN_LIVE_PROJECTION:
        return False, 0, [f"projection {projection:.1f}<{config.PRECISION_FIRST_MIN_LIVE_PROJECTION:.1f}"]

    blocks = {
        "market": features.get("market_context_score"),
        "sector": features.get("sector_etf_score"),
        "breadth": features.get("sector_breadth_score"),
        "leveraged": features.get("leveraged_confirmation_score"),
        "relationships": features.get("relationship_context_score"),
        "leaders": features.get("relationship_lead_lag_score"),
    }
    confirming: list[str] = []
    for name, raw in blocks.items():
        try:
            value = sign * float(raw)
        except Exception:
            continue
        if value >= 10.0:
            confirming.append(name)
        elif value <= -20.0:
            return False, len(confirming), [f"{name} adverso {value:.1f}"]
    if config.PRECISION_FIRST_REQUIRE_RELATIONAL_CONFIRMATION and len(confirming) < config.PRECISION_FIRST_MIN_CONFIRMING_BLOCKS:
        return False, len(confirming), [f"confirmaciones {len(confirming)}/{config.PRECISION_FIRST_MIN_CONFIRMING_BLOCKS}"]

    # A validated portfolio is not allowed to hide a weak component rule. The
    # champion stores purged walk-forward metrics for every rule and the live
    # gate checks them again as a defence-in-depth control. Provisional SHADOW
    # models remain measurable but are never presented as validated alerts.
    rule_name = str(candidate.get("rule_name") or "")
    model = model or {}
    rule_metrics = ((model.get("metrics") or {}).get("precision_rule_oos_metrics") or {}).get(rule_name) or {}
    if str(model.get("status") or "").lower() == "champion":
        sample = int(rule_metrics.get("sample") or 0)
        win_rate = float(rule_metrics.get("win_rate") or 0.0)
        avg_return = float(rule_metrics.get("avg_return") or 0.0)
        profit_factor = float(rule_metrics.get("profit_factor") or 0.0)
        if sample < int(config.PRECISION_MIN_RULE_OOS_TRADES):
            return False, len(confirming), [f"regla OOS n={sample}<{config.PRECISION_MIN_RULE_OOS_TRADES}"]
        if win_rate < float(config.PRECISION_MIN_RULE_WIN_RATE):
            return False, len(confirming), [f"regla OOS win={win_rate:.1f}%<{config.PRECISION_MIN_RULE_WIN_RATE:.1f}%"]
        if avg_return < float(config.PRECISION_MIN_RULE_AVG_RETURN):
            return False, len(confirming), [f"regla OOS avg={avg_return:.2f}%<{config.PRECISION_MIN_RULE_AVG_RETURN:.2f}%"]
        if profit_factor < float(config.PRECISION_MIN_RULE_PROFIT_FACTOR):
            return False, len(confirming), [f"regla OOS PF={profit_factor:.2f}<{config.PRECISION_MIN_RULE_PROFIT_FACTOR:.2f}"]
        candidate["precision_rule_oos_sample"] = sample
        candidate["precision_rule_oos_win_rate"] = win_rate
        candidate["precision_rule_oos_avg_return"] = avg_return
        candidate["precision_rule_oos_profit_factor"] = profit_factor

    candidate["precision_rank_score"] = (
        score
        + 3.0 * len(confirming)
        + max(0.0, float(candidate.get("precision_rule_oos_win_rate") or 50.0) - 50.0) * 0.15
        + min(5.0, float(candidate.get("precision_rule_oos_profit_factor") or 1.0)) * 1.5
    )
    return True, len(confirming), confirming


def run_alert_scan(
    scanner_run_id: int | None = None,
    historical: bool = False,
    capture_prospective_architecture: bool = False,
) -> int:
    """Materialize one Data Bot run and optionally evaluate live entries.

    historical=True bypasses recency and entry creation. It is used during startup
    to rebuild the 30-day training window from weekly ZIPs/live DB runs before the
    first fresh run is evaluated.

    capture_prospective_architecture is an explicit, narrow exception for the
    authoritative daily-close materialization path. It may persist SHADOW journal
    evidence while historical=True keeps all LIVE side effects disabled. Historical
    replay callers leave it False and therefore cannot write prospective evidence.
    """
    scan_started = time.time()
    activate_due_model()
    ok, message = data_dbs_available()
    if not ok:
        log_error(f"🟥 No se puede escanear alertas: {message}")
        return 0

    run = scanner_run_by_id(int(scanner_run_id)) if scanner_run_id else latest_successful_scanner_run()
    if not run:
        log_info("⏳ ALERTS · todavía no existe un Scanner SUCCESS consumible.", send_discord=False)
        return 0

    run_status = str(run.get("status") or "").lower()
    if run_status != "success":
        log_info(
            f"⏳ ALERTS · run `{run.get('id')}` ignorado hasta SUCCESS | status={run_status or 'n/a'}",
            send_discord=False,
        )
        return 0

    if not historical and scanner_run_id is None:
        newest_any = latest_scanner_run_any()
        if newest_any and int(newest_any.get("id") or 0) > int(run.get("id") or 0) and str(newest_any.get("status") or "").lower() == "running":
            log_info(
                f"⏳ Data Bot todavía enriqueciendo scanner_run={newest_any.get('id')} "
                f"| base_rows={newest_any.get('base_rows_saved') or newest_any.get('snapshot_count') or 0} "
                f"| enriched={newest_any.get('enriched_ok') or 0}. Se esperará a su cierre.",
                send_discord=False,
            )
            return 0

    if int(run.get("snapshot_count") or 0) < config.MIN_SCANNER_SNAPSHOTS:
        log_warning(build_group(
            "🟧 ALERTS · SCANNER NO CONSUMIBLE",
            [f"**Run:** `{run['id']}` · **Snapshots:** `{run.get('snapshot_count')}` · mínimo `{config.MIN_SCANNER_SNAPSHOTS}`\nEl run se ignora; se conserva el último Scanner SUCCESS."],
        ))
        return 0

    snapshots = latest_scanner_snapshots(int(run["id"]))
    freshest_snapshot = max((str(row.get("captured_at_madrid") or "") for row in snapshots), default="")
    age = _age_minutes(freshest_snapshot or run.get("captured_at_madrid"))
    scanner_base_age = _age_minutes(run.get("captured_at_madrid"))
    try:
        run_duration_minutes = float(run.get("duration_seconds") or 0.0) / 60.0
    except Exception:
        run_duration_minutes = 0.0
    if not historical and (age is None or age > config.MAX_SCANNER_AGE_MINUTES):
        log_info(
            f"⏳ ALERTS · esperando Scanner fresco | último_success={run['id']} | age_minutes={age}",
            send_discord=False,
        )
        return 0

    as_of_madrid = max((str(row.get("captured_at_madrid") or "") for row in snapshots), default=None)
    gex_map = latest_gex_by_ticker(as_of_madrid=as_of_madrid)
    expiry_map = latest_expiry_buckets_by_ticker(as_of_madrid=as_of_madrid)
    expirations_map = latest_expirations_by_ticker(as_of_madrid=as_of_madrid)
    model = get_active_model()
    core_model_obj = get_best_model_for_tier("CORE", usable_only=True)
    if not core_model_obj:
        params = model.get("params") or {}
        if str(params.get("feature_tier") or "").upper() == "CORE":
            core_model_obj = model
    full_model_obj = get_best_model_for_tier("FULL", usable_only=True)
    decision_model = core_model_obj or full_model_obj or model
    ensure_layer_history_schema()
    log_info(
        "▶️ [ALERTS SCAN START] "
        f"run={int(run.get('id') or 0)} date={run.get('snapshot_date') or 'n/a'} "
        f"session={run.get('session_type') or 'n/a'} historical={bool(historical)} "
        f"scanner_rows={len(snapshots)} handoff_age_min={f'{age:.1f}' if age is not None else 'n/a'} "
        f"scanner_base_age_min={f'{scanner_base_age:.1f}' if scanner_base_age is not None else 'n/a'} "
        f"data_run_duration_min={run_duration_minutes:.1f} | "
        f"model=#{int((decision_model or {}).get('id') or 0)} status={str((decision_model or {}).get('status') or 'none').upper()} "
        f"key={(decision_model or {}).get('model_key') or 'sin_modelo'}",
        send_discord=False,
    )

    # Build the full Scanner universe first. The whole cross-section feeds breadth,
    # sector and graph context even when a ticker was not selected for granular GEX.
    # Only GEX-complete tickers may emit a GEX-based live alert.
    feature_build_started = time.time()
    base_features_by_ticker: dict[str, dict] = {}
    gex_valid_by_ticker: dict[str, dict] = {}
    stale_gex = 0
    scanner_only = 0
    for snapshot in snapshots:
        ticker = str(snapshot.get("ticker") or "")
        if not ticker:
            continue
        candidate_gex = gex_map.get(ticker) if str(snapshot.get("gex_status") or "").lower() == "ok" else None
        features = build_features(snapshot, candidate_gex, expiry_map.get(ticker), expirations_map.get(ticker))
        lag = features.get("gex_lag_minutes")
        gex_is_fresh = bool(candidate_gex and lag is not None and 0 <= float(lag) <= config.MAX_GEX_LAG_MINUTES)
        if gex_is_fresh:
            gex_valid_by_ticker[ticker] = candidate_gex
        else:
            features["has_gex"] = 0
            if str(snapshot.get("gex_status") or "").lower() in {"ok", "pending"}:
                stale_gex += 1
            else:
                scanner_only += 1
        base_features_by_ticker[ticker] = features
    feature_build_elapsed = time.time() - feature_build_started

    previous_lookup_started = time.time()
    prior_features_by_ticker = previous_feature_map(int(run["id"]))
    previous_lookup_elapsed = time.time() - previous_lookup_started
    relational_started = time.time()
    relational_by_ticker: dict[str, dict] = {}
    relational_summary: dict = {}
    if config.RELATIONAL_FEATURES_ENABLED and base_features_by_ticker:
        # Every Scanner row is a graph node. Structural relationships inferred
        # from the current names/categories are available in this same run, not
        # only after the next daily refresh.
        universe = {
            ticker: {
                "name": features.get("name"),
                "category": features.get("category"),
                "sector": features.get("sector"),
                "industry": features.get("industry"),
            }
            for ticker, features in base_features_by_ticker.items()
        }
        upsert_asset_universe_nodes([
            {
                "ticker": ticker, "name": meta.get("name"), "category": meta.get("category"),
                "sector": meta.get("sector"), "industry": meta.get("industry"),
                "asset_type": classify_asset(ticker, meta.get("name"), meta.get("category")),
                "last_seen_date": str(run.get("snapshot_date") or now_madrid().date().isoformat()),
                "source_scanner_run_id": int(run["id"]),
                "metadata": {"source": "full_scanner_live"},
            }
            for ticker, meta in universe.items()
        ])
        relationship_map = get_asset_relationships()
        for edge in infer_structural_relationships(universe):
            polarity = -1.0 if float(edge.get("polarity") or 1.0) < 0 else 1.0
            relationship_map.setdefault(str(edge["ticker"]), []).append({
                "ticker": edge["ticker"],
                "related_ticker": edge["related_ticker"],
                "relation_type": edge["relation_type"],
                "base_weight": edge["base_weight"],
                "dynamic_weight": edge["base_weight"],
                "correlation": polarity * float(edge["base_weight"]),
                "lead_lag_corr": None,
                "beta": None,
                "sample_size": 0,
            })
        relational_by_ticker, relational_summary = build_relational_context(
            base_features_by_ticker,
            previous_by_ticker=prior_features_by_ticker,
            relationship_map=relationship_map,
        )
        relational_summary["full_scanner_nodes"] = len(universe)
        relational_summary["structural_edges_live"] = sum(
            1 for rows in relationship_map.values() for row in rows
            if str(row.get("relation_type") or "") in {
                "leveraged_parent", "leveraged_confirmation",
                "single_stock_parent", "single_stock_confirmation",
            }
        )
    features_by_ticker = {
        ticker: add_transition_features(
            attach_relational_features(features, relational_by_ticker.get(ticker)),
            prior_features_by_ticker.get(ticker),
        )
        for ticker, features in base_features_by_ticker.items()
    }
    relational_elapsed = time.time() - relational_started

    # Objective calibration gates are derived only from already completed future
    # outcomes. They never look at the current run, preventing availability leak.
    calibration_lookup_started = time.time()
    layer_calibrations_before = get_layer_calibrations()
    if not layer_calibrations_before:
        # Bootstrap fallback only. Normal scans reuse the last completed daily
        # calibration; the expensive archive-backed refresh is not signal-path work.
        layer_calibrations_before = refresh_layer_calibrations()
    calibration_lookup_elapsed = time.time() - calibration_lookup_started
    gex_daily_validated = str((layer_calibrations_before.get("GEX_DAILY") or {}).get("status") or "") == "VALIDATED"
    gex_intraday_validated = str((layer_calibrations_before.get("GEX_INTRADAY") or {}).get("status") or "") == "VALIDATED"
    core_status = str((core_model_obj or {}).get("status") or "NONE").upper()
    gex_daily_status = str((layer_calibrations_before.get("GEX_DAILY") or {}).get("status") or "ACCUMULATING").upper()
    gex_intraday_status = str((layer_calibrations_before.get("GEX_INTRADAY") or {}).get("status") or "ACCUMULATING").upper()
    graph_status = str((layer_calibrations_before.get("GRAPH") or {}).get("status") or "ACCUMULATING").upper()
    fusion_status = str((layer_calibrations_before.get("FUSION") or {}).get("status") or "ACCUMULATING").upper()
    for features in features_by_ticker.values():
        features["gex_daily_model_validated"] = 1 if gex_daily_validated else 0
        features["gex_intraday_model_validated"] = 1 if gex_intraday_validated else 0
        features["core_model_id"] = (core_model_obj or {}).get("id")
        features["core_model_key"] = (core_model_obj or {}).get("model_key")
        features["core_model_status"] = core_status
        features["gex_daily_model_status"] = gex_daily_status
        features["gex_intraday_model_status"] = gex_intraday_status
        features["graph_model_status"] = graph_status
        features["fusion_model_status"] = fusion_status

    # Persist every layer, including non-actionable CORE/graph/GEX watch signals.
    # These rows are the growing calibration memory for future challengers.
    layer_mode = "daily_close" if historical or "deep" in str(run.get("session_type") or "").lower() else "intraday"
    fusion_started = time.time()
    all_layer_signals, actionable_by_ticker = build_fusion_signals(
        features_by_ticker, core_model_obj, full_model_obj, mode=layer_mode
    )
    fusion_elapsed = time.time() - fusion_started

    # v6.1.15: lifecycle needs the current CORE/GEX/fusion interpretation, not
    # only raw Scanner/GEX features. Merge the strongest FUSION signal into the
    # per-ticker current snapshot so runners can exit when CORE/GEX says so.
    state_priority = {"ENTER": 9, "READY": 8, "RETEST": 7, "EARLY": 6, "DISCOVERED": 5, "WATCH": 4, "DIVERGENCE": 2, "CAPPED": 1, "MARKET_BLOCKED": 0}
    active_by_ticker: dict[str, dict] = {}
    for signal in all_layer_signals:
        if str(signal.get("layer") or "").upper() != "FUSION":
            continue
        ticker = str(signal.get("ticker") or "").upper()
        if not ticker:
            continue
        state = str(signal.get("opportunity_state") or signal.get("state") or "WATCH").upper()
        rank = state_priority.get(state, 3) * 1000.0 + float(signal.get("score") or 0.0)
        if ticker not in active_by_ticker or rank > float(active_by_ticker[ticker].get("_rank") or 0.0):
            active_by_ticker[ticker] = {**signal, "_rank": rank}
    current_signal_fields = (
        "signal_origin", "target_source", "target_zone_low", "target_zone_high", "target_price", "target_pct",
        "timing_min_sessions", "timing_max_sessions", "expiry_date", "expiry_bucket", "expiry_regime",
        "probability_pct", "core_rule", "core_score", "core_rank_score", "core_directional_edge", "overextension_score",
        "gex_score", "gex_intent", "operational_target", "capped", "wall_asymmetry", "wall_reward_pct", "wall_risk_pct", "level_name", "level_price",
        "intraday_state", "intraday_blocks", "intraday_adverse", "blockers", "rationale", "contributor_engines", "model_lineage",
    )
    for ticker, signal in active_by_ticker.items():
        current = features_by_ticker.get(ticker)
        if not current:
            continue
        for key in current_signal_fields:
            if signal.get(key) not in (None, "", [], {}):
                current[key] = signal.get(key)
        current["active_signal_direction"] = signal.get("direction")
        current["active_signal_state"] = signal.get("opportunity_state") or signal.get("state")
        current["active_signal_origin"] = signal.get("signal_origin")
        current["active_signal_score"] = signal.get("score")
        current["active_core_score"] = signal.get("core_score")
        current["active_gex_score"] = signal.get("gex_score")
        current["active_gex_intent"] = signal.get("gex_intent")

    # v6.1.23: calibrated MARKET CORE + quality-controlled main-ETF GEX + the VIX/volatility
    # complex produce a parallel anticipatory CLOSE ALL signal. It is evaluated
    # independently from the open portfolio and never writes an individual exit.
    market_layers_started = time.time()
    relational_summary = attach_market_confirmation_layers(
        features_by_ticker, all_layer_signals, core_model_obj, relational_summary
    )
    market_signal_result = evaluate_market_close_all_signals(features_by_ticker, run)
    relational_summary["market_close_all_signal_states"] = market_signal_result.get("states") or {}
    persist_market_regime(run, relational_summary)
    if not historical:
        maybe_send_market_close_all_signals(
            market_signal_result, open_trades=get_open_trades(), features_by_ticker=features_by_ticker
        )
    market_layers_elapsed = time.time() - market_layers_started

    layer_persist_started = time.time()
    layer_rows = persist_layer_signals(
        run, all_layer_signals, model_version_id=(decision_model or {}).get("id")
    )
    layer_feature_rows = persist_layer_features(run, features_by_ticker, all_layer_signals)
    layer_persist_elapsed = time.time() - layer_persist_started

    # TMAD 6.2.31: prospective architecture journal. The authoritative daily
    # close is materialized with historical=True to suppress LIVE side effects, so
    # capture uses a separate explicit permission granted only by _daily_recalibration.
    # Ordinary historical replay remains unable to write prospective evidence.
    prospective_architecture = {"status": "not_applicable", "live_mutation": False}
    architecture_capture_allowed = bool(
        (not historical) or capture_prospective_architecture
    )
    if architecture_capture_allowed and "deep" in str(run.get("session_type") or "").lower():
        try:
            from prospective_architecture_journal import capture_prospective_architecture_journal
            prospective_architecture = capture_prospective_architecture_journal(
                run,
                features_by_ticker,
                all_layer_signals,
            )
            log_info(
                "🧪 [ARCHITECTURE JOURNAL] "
                f"run={int(run.get('id') or 0)} "
                f"core={len(prospective_architecture.get('core_direction_rows') or [])} "
                f"research_top1={len(prospective_architecture.get('research_override_rows') or [])} "
                f"inserted={int(prospective_architecture.get('inserted') or 0)} "
                "scope=PROSPECTIVE_SHADOW LIVE=UNCHANGED",
                send_discord=False,
            )
        except Exception as architecture_exc:
            prospective_architecture = {
                "status": "error",
                "error": str(architecture_exc),
                "live_mutation": False,
            }
            log_info(
                f"⚠️ [ARCHITECTURE JOURNAL] prospective capture skipped: {architecture_exc}",
                send_discord=False,
            )

    # TMAD 6.2: EOD Swing V2 runs only on a fresh deep close, in prospective
    # SHADOW. It uses the full Scanner feature universe and the model frozen by
    # prior mature history. No Discord publication, position mutation or LIVE gate.
    swing_v2_shadow = {"status": "not_applicable"}
    if not historical and "deep" in str(run.get("session_type") or "").lower():
        try:
            from swing_v2 import score_current_snapshot
            swing_v2_shadow = score_current_snapshot(
                features_by_ticker,
                run,
                max_positions=5,
                persist=True,
            )
            log_info(
                "🧪 [SWING V2 EOD] "
                f"run={int(run.get('id') or 0)} status={swing_v2_shadow.get('status')} "
                f"candidates={int(swing_v2_shadow.get('candidate_count') or 0)} "
                f"selected={int(swing_v2_shadow.get('selected_count') or 0)} "
                "scope=PROSPECTIVE_SHADOW",
                send_discord=False,
            )
        except Exception as swing_v2_exc:
            log_info(
                f"⚠️ [SWING V2 EOD] research snapshot skipped: {swing_v2_exc}",
                send_discord=False,
            )
            swing_v2_shadow = {"status": "error", "error": str(swing_v2_exc)}

    # Critical-path rule (v6.1.107): a fresh Scanner run must deliver lifecycle
    # and entries before global historical maintenance. Outcome maturation,
    # ZIP archival, archive-backed calibration, Auditor and Learning run at
    # startup/daily-close maintenance, where they cannot make a fresh signal stale.
    layer_outcomes = {"status": "DEFERRED_TO_MAINTENANCE", "signal_labeled": 0, "feature_labeled": 0}
    layer_archives: list[dict] = []
    layer_calibrations = layer_calibrations_before
    maybe_send_layer_calibration(layer_calibrations, all_layer_signals, run, market_summary=relational_summary)
    market_audit = {
        "status": str(get_runtime_state("market_outcome_audit_status", "DEFERRED") or "DEFERRED"),
        "executed": False,
        "metrics": {},
        "deferred_reason": "fresh_signal_path",
    }
    adaptive_learning = {
        "status": str(get_runtime_state("adaptive_learning_state", "DEFERRED") or "DEFERRED"),
        "executed": False,
        "shadow_model_id": int(get_runtime_state("adaptive_learning_shadow_model_id", "0") or 0),
        "deferred_reason": "fresh_signal_path",
    }

    lifecycle_started = time.time()
    closed = 0 if historical else update_trade_lifecycle(
        snapshots, gex_map=gex_valid_by_ticker, expiry_map=expiry_map, expirations_map=expirations_map,
        model=decision_model, features_by_ticker=features_by_ticker,
    )
    lifecycle_elapsed = time.time() - lifecycle_started
    model_params = decision_model.get("params") or {}
    model_tier = str(model_params.get("feature_tier") or "CORE").upper()
    model_validated = decision_model.get("status") == "champion"
    pipeline_entry_mode = str(get_runtime_state("entry_mode", "off") or "off").lower()
    validated_delivery_enabled = bool(
        config.CALIBRATED_CHAMPION_ALERTS_ENABLED
        or (config.AUTO_SEND_VALIDATED_ALERTS and not config.RESEARCH_MODE)
    )
    session_label = str(run.get("session_type") or "").lower()
    run_research_only = historical or (config.DEEP_RUN_RESEARCH_ONLY and "deep" in session_label) or (
        config.FIRST_RUN_RESEARCH_ONLY and "first_run" in session_label
    )
    market_window_open = _entry_window_open()
    entry_gate = decide_entry_gate(
        pipeline_entry_mode=pipeline_entry_mode,
        model_validated=model_validated,
        validated_delivery_enabled=validated_delivery_enabled,
        precalibration_shadow_enabled=config.PRECALIBRATION_SHADOW_ENABLED,
        run_research_only=run_research_only,
        market_window_open=market_window_open,
    )
    live_mode = bool(entry_gate["live_mode"])
    shadow_mode = bool(entry_gate["shadow_mode"])
    entries_allowed = bool(entry_gate["entries_allowed"])
    entry_gate_reason = str(entry_gate["reason"])
    if live_mode:
        entry_gate_scope = "LIVE_VALIDATED"
    elif shadow_mode:
        entry_gate_scope = "SHADOW_LEARNING"
    else:
        entry_gate_scope = "CLOSED"

    observations = 0
    observation_rows: list[dict] = []
    relational_rows: list[dict] = []
    candidates: list[tuple[dict, dict, dict, dict]] = []
    precision_rejected = 0
    precision_reasons: Counter[str] = Counter()
    for snapshot in snapshots:
        ticker = str(snapshot.get("ticker") or "")
        features = features_by_ticker.get(ticker)
        gex = gex_valid_by_ticker.get(ticker)
        if not ticker or not features:
            continue
        if config.STORE_ALL_FEATURE_OBSERVATIONS:
            observation_rows.append(feature_observation(snapshot, gex, features))
            relational = relational_by_ticker.get(ticker)
            if relational:
                relational_rows.append({
                    "source_scanner_run_id": int(run["id"]),
                    "snapshot_date": snapshot.get("snapshot_date"),
                    "ticker": ticker,
                    "sector_etf": relational.get("sector_etf"),
                    "market_regime": relational.get("market_regime"),
                    "market_context_score": relational.get("market_context_score"),
                    "market_breadth_score": relational.get("market_breadth_score"),
                    "vix_stress_score": relational.get("vix_stress_score"),
                    "credit_risk_on_score": relational.get("credit_risk_on_score"),
                    "sector_context_score": relational.get("sector_etf_score"),
                    "sector_breadth_score": relational.get("sector_breadth_score"),
                    "relationship_context_score": relational.get("relationship_context_score"),
                    "relationship_lead_lag_score": relational.get("relationship_lead_lag_score"),
                    "leveraged_confirmation_score": relational.get("leveraged_confirmation_score"),
                    "relative_strength_vs_market": relational.get("relative_strength_vs_market"),
                    "relative_strength_vs_sector": relational.get("relative_strength_vs_sector"),
                    "graph_context_score": relational.get("graph_context_score"),
                    "relational_deterioration_score": relational.get("relational_deterioration_score"),
                    "context_json": __import__("json").dumps(relational, ensure_ascii=False),
                    "unique_key": f"{int(run['id'])}|{ticker}",
                })
        if not entries_allowed:
            continue
        # Only fused decisions with an actual GEX target/timing become entries.
        # CORE-only and graph-propagation signals remain persisted watchlist rows.
        for candidate in actionable_by_ticker.get(ticker, []):
            candidate_live = bool(
                live_mode
                and str(candidate.get("target_source") or "") == "GEX_CALIBRATED"
                and str((layer_calibrations.get("GEX_INTRADAY") or {}).get("status") or "") == "VALIDATED"
            )
            if candidate_live:
                precision_ok, confirmation_count, precision_details = _precision_first_gate(
                    candidate, features, decision_model
                )
                candidate["precision_confirmation_count"] = int(confirmation_count)
                candidate["precision_confirming_blocks"] = list(precision_details if precision_ok else [])
                candidate["entry_gate_scope"] = "LIVE_VALIDATED"
                if not precision_ok:
                    precision_rejected += 1
                    for reason in precision_details or ["precision_gate_unknown"]:
                        precision_reasons[str(reason)] += 1
                    continue
            else:
                # SHADOW is deliberately a learning book. It records theoretical
                # FUSION entries without pretending they passed the LIVE 85+3 gate.
                confirming = list(candidate.get("intraday_blocks") or [])
                candidate["precision_confirmation_count"] = len(confirming)
                candidate["precision_confirming_blocks"] = confirming
                candidate["precision_rank_score"] = float(candidate.get("score") or 0.0) + len(confirming) * 2.0
                candidate["entry_gate_scope"] = (
                    "SHADOW_LEARNING" if shadow_mode else "SHADOW_LEARNING_FALLBACK"
                )
            features["precision_confirmation_count"] = candidate.get("precision_confirmation_count")
            features["precision_confirming_blocks"] = candidate.get("precision_confirming_blocks") or []
            features["precision_rank_score"] = candidate.get("precision_rank_score") or float(candidate.get("score") or 0.0)
            features["entry_gate_scope"] = candidate.get("entry_gate_scope")
            event_model = decision_model
            event = build_event(snapshot, gex, features, candidate, event_model, live=candidate_live)
            candidates.append((event, snapshot, gex, features))

    persist_started = time.time()
    relational_persisted = 0
    if config.STORE_ALL_FEATURE_OBSERVATIONS:
        observations = upsert_feature_observations_bulk(observation_rows)
        relational_persisted = upsert_relational_contexts_bulk(relational_rows)
    persist_elapsed = time.time() - persist_started
    if observation_rows or relational_rows:
        log_info(
            "🗄️ [ALERTS PERSIST] "
            f"run={int(run.get('id') or 0)} features={observations}/{len(observation_rows)} "
            f"relational={relational_persisted}/{len(relational_rows)} sqlite_seconds={persist_elapsed:.2f} mode=bulk",
            send_discord=False,
        )

    candidates.sort(
        key=lambda item: (
            float((item[3] or {}).get("precision_rank_score") or 0.0),
            float(item[0].get("score") or 0.0),
        ),
        reverse=True,
    )
    portfolio_policy = (model_params.get("rules") or {}).get("_portfolio") or {}
    default_sector = (
        config.PRECISION_MAX_SIGNALS_PER_SECTOR
        if config.ALERT_SELECTION_PROFILE == "precision_first"
        else config.PORTFOLIO_MAX_SIGNALS_PER_SECTOR
    )
    default_direction = (
        config.PRECISION_MAX_SIGNALS_PER_DIRECTION
        if config.ALERT_SELECTION_PROFILE == "precision_first"
        else config.PORTFOLIO_MAX_SIGNALS_PER_DIRECTION
    )
    max_sector = max(1, int(portfolio_policy.get("max_signals_per_sector", default_sector)))
    max_direction = max(1, int(portfolio_policy.get("max_signals_per_direction", default_direction)))
    if config.ALERT_SELECTION_PROFILE == "precision_first":
        max_sector = min(max_sector, int(config.PRECISION_MAX_SIGNALS_PER_SECTOR))
        max_direction = min(max_direction, int(config.PRECISION_MAX_SIGNALS_PER_DIRECTION))
    max_alerts_this_run = (
        int(config.PRECISION_MAX_ALERTS_PER_RUN)
        if config.ALERT_SELECTION_PROFILE == "precision_first"
        else int(config.MAX_ALERTS_PER_RUN)
    )
    session_date = str(run.get("snapshot_date") or (snapshots[0].get("snapshot_date") if snapshots else ""))
    existing_daily_alerts = 0
    if config.ALERT_SELECTION_PROFILE == "precision_first" and session_date:
        existing_daily_alerts = daily_alert_count(
            session_date, "live" if live_mode else ("shadow" if shadow_mode else "all")
        )
        max_alerts_this_run = min(
            max_alerts_this_run,
            max(0, int(config.PRECISION_MAX_ALERTS_PER_DAY) - existing_daily_alerts),
        )
    sector_counts, direction_counts = _open_portfolio_counts(live_mode=live_mode, shadow_mode=shadow_mode)
    inserted = sent = 0
    entry_rejections: Counter[str] = Counter()
    live_cards: list[tuple[int, str, dict]] = []
    shadow_cards: list[tuple[int, str, dict]] = []
    for candidate_index, (event, _snapshot, _gex, _features) in enumerate(candidates):
        if inserted >= max_alerts_this_run:
            entry_rejections["run_or_daily_cap"] += max(1, len(candidates) - candidate_index)
            break
        sector = _portfolio_sector_bucket(str(event.get("ticker") or ""), _features)
        direction = str(event.get("direction") or "")
        if sector_counts[sector] >= max_sector:
            entry_rejections["sector_cap"] += 1
            continue
        if direction_counts[direction] >= max_direction:
            entry_rejections["direction_cap"] += 1
            continue
        if not cooldown_ok(str(event["ticker"]), str(event["rule_name"])):
            entry_rejections["cooldown"] += 1
            continue
        event_live = str(event.get("status") or "").lower() == "open"
        if has_open_trade(str(event["ticker"]), live_only=event_live):
            entry_rejections["already_open"] += 1
            continue
        event_id = insert_alert_event(event)
        if not event_id:
            entry_rejections["duplicate_or_db_rejected"] += 1
            continue
        inserted += 1
        sector_counts[sector] += 1
        direction_counts[direction] += 1
        card = format_alert_card(event, decision_model)
        if event_live:
            live_cards.append((event_id, card, event))
        else:
            shadow_cards.append((event_id, card, event))

    # Discord order is deterministic for every run: lifecycle CLOSE/DETERIORATION/UPDATE
    # is delivered first by update_trade_lifecycle; new entries follow here, ranked by
    # precision score. Cards are grouped semantically instead of raw 1,900-char cuts.
    if live_cards:
        live_message = build_group(
            "ENTER 🚨 · LIVE",
            [card for _event_id, card, _event in live_cards],
            context=(
                f"{_discord_session_badge(run.get('session_type'))} · Run `{run['id']}` · "
                f"`{run.get('session_type') or 'intraday'}` · `{len(live_cards)}` entradas validadas · ordenadas por convicción · LIVE"
            ),
            footer="Alerts Live contiene únicamente lifecycle LIVE validado y señales generales LIVE; SHADOW se publica en Alerts Shadow.",
        )
        if send_alert(live_message):
            for event_id, _card, event in live_cards:
                mark_alert_sent(event_id, live_message)
                try:
                    send_alert_traceability(event, decision_model)
                except Exception as trace_exc:
                    log_info(f"⚠️ Trazabilidad no entregada para {event.get('ticker')}: {trace_exc}", send_discord=False)
            sent += len(live_cards)

    if shadow_cards:
        shadow_message = build_group(
            "ENTER 🚨 · SHADOW",
            [card for _event_id, card, _event in shadow_cards],
            context=(
                f"{_discord_session_badge(run.get('session_type'))} · Run `{run['id']}` · "
                f"`{run.get('session_type') or 'intraday'}` · `{len(shadow_cards)}` entradas de aprendizaje · no operativas"
            ),
            footer="⚠️ SHADOW: aprendizaje simulado; se publica exclusivamente en Alerts Shadow.",
        )
        delivered = bool(send_shadow(shadow_message))
        if delivered:
            for event_id, _card, event in shadow_cards:
                mark_research_alert_sent(event_id, shadow_message)
                try:
                    send_alert_traceability(event, decision_model)
                except Exception as trace_exc:
                    log_info(f"⚠️ Trazabilidad no entregada para {event.get('ticker')}: {trace_exc}", send_discord=False)
            sent += len(shadow_cards)

    if not historical:
        # Final state of the run: lifecycle has already updated/closed existing
        # positions and new entries have already been persisted. The overview
        # therefore shows the real complete open LIVE book.
        maybe_send_open_positions_overview(features_by_ticker=features_by_ticker, run=run)

    if config.SEND_RUN_SUMMARY:
        run_mode_label = (
            "HISTÓRICO" if historical else
            ("RUN LIVE" if live_mode else ("RUN SHADOW" if shadow_mode else "RUN OBSERVACIÓN"))
        )
        gex_counts = _gex_run_counts(snapshots, len(gex_valid_by_ticker), stale_gex)
        log_info(
            build_group(
                f"🔎 ALERTS · {run_mode_label}",
                ["\n".join([
                    f"**Run:** `{run['id']}` · **Sesión:** `{run.get('session_type') or 'sin etiqueta'}`",
                    f"**Datos:** Scanner `{len(snapshots)}` · enriquecidos GEX `{gex_counts['requested']}` · GEX válido `{gex_counts['valid']}` · fallos/stale `{gex_counts['failed']}` · no enriquecidos `{gex_counts['not_enriched']}`",
                    f"**Resultado:** FUSION accionables `{sum(len(v) for v in actionable_by_ticker.values())}` · candidatos tras gate LIVE `{len(candidates)}` · nuevas `{inserted}` · cierres lifecycle `{closed}`",
                    f"**Rechazos:** gate precisión LIVE `{precision_rejected}` · cartera/cooldown/duplicados `{sum(entry_rejections.values())}` · detalle `{dict(entry_rejections) or 'ninguno'}`",
                    f"**Discord:** LIVE `{len(live_cards)}` · SHADOW `{len(shadow_cards)}` · Research `shortlist` · overview `Alerts Status`",
                    f"**Gate entradas:** `{'ABIERTO' if entries_allowed else 'CERRADO'}` · scope `{entry_gate_scope}` · {entry_gate_reason}",
                    (
                        f"**Market:** CORE risk-off `{float(relational_summary.get('market_core_risk_off_probability') or 0):.1f}%` · risk-on `{float(relational_summary.get('market_core_risk_on_probability') or 0):.1f}%` · cobertura `{int(relational_summary.get('market_core_coverage') or 0)}/4` / `{float(relational_summary.get('market_core_weight_coverage') or 0):.0%}`"
                        if int(relational_summary.get('market_core_coverage') or 0) > 0 else
                        "**Market:** CORE `NO DISPONIBLE` · cobertura `0/4`; el 50/50 interno es placeholder neutral y no una predicción."
                    ),
                    f"**Confirmación:** GEX índices `{float(relational_summary.get('main_etf_gex_risk_on_score') or 0):+.1f}` (`{int(relational_summary.get('main_etf_gex_coverage') or 0)}/4`) · VIX GEX `{float(relational_summary.get('vol_gex_stress_score') or 0):+.1f}` (`{int(relational_summary.get('vol_gex_long_coverage') or 0)}L/{int(relational_summary.get('vol_gex_short_coverage') or 0)}S`)",
                    f"**CLOSE ALL paralelo:** LONGS `{'ACTIVO' if (market_signal_result.get('states') or {}).get('long', {}).get('active') else 'OFF'}` / `{relational_summary.get('market_close_all_longs_calibration_status') or 'ACCUMULATING'}` n=`{int(relational_summary.get('market_close_all_longs_calibration_sample') or 0)}` · SHORTS `{'ACTIVO' if (market_signal_result.get('states') or {}).get('short', {}).get('active') else 'OFF'}` / `{relational_summary.get('market_close_all_shorts_calibration_status') or 'ACCUMULATING'}` n=`{int(relational_summary.get('market_close_all_shorts_calibration_sample') or 0)}`",
                    f"**Auditor:** `{market_audit.get('status','pendiente')}` · movimientos maduros `{((market_audit.get('metrics') or {}).get('funnel') or {}).get('movements',0)}` · ejecutado `{'sí' if market_audit.get('executed') else 'no/caché'}`",
                    f"**Aprendizaje:** `{adaptive_learning.get('status','pendiente')}` · shadow model `#{adaptive_learning.get('shadow_model_id') or 0}` · Research gate `{effective_research_gate():.1f}` · LIVE sin auto-promoción.",
                    "**Lifecycle:** `ACTIVO` · CLOSE ALL nunca liquida; mientras esté activo, el CLOSE individual exige reversión del GEX propio del ticker. ±5% y target son milestones.",
                ])],
            )
        )
    gex_lags = sorted(
        float(features.get("gex_lag_minutes"))
        for features in base_features_by_ticker.values()
        if features.get("gex_lag_minutes") is not None
    )
    gex_lag_median = gex_lags[len(gex_lags)//2] if gex_lags else None
    gex_lag_max = gex_lags[-1] if gex_lags else None
    state_counts = Counter(
        str(signal.get("opportunity_state") or signal.get("state") or "UNKNOWN").upper()
        for signal in all_layer_signals
    )
    gex_counts = _gex_run_counts(snapshots, len(gex_valid_by_ticker), stale_gex)
    audit_funnel = ((market_audit.get("metrics") or {}).get("funnel") or {})
    log_info(
        "✅ [ALERTS SCAN END] "
        f"run={int(run.get('id') or 0)} session={run.get('session_type') or 'n/a'} "
        f"elapsed={time.time()-scan_started:.1f}s historical={bool(historical)} | "
        f"universe={len(snapshots)} gex_requested={gex_counts['requested']} valid={gex_counts['valid']} "
        f"failed_or_stale={gex_counts['failed']} scanner_only={gex_counts['not_enriched']} "
        f"freshness=handoff:{f'{age:.1f}m' if age is not None else 'n/a'},base:{f'{scanner_base_age:.1f}m' if scanner_base_age is not None else 'n/a'},"
        f"gex_med:{f'{gex_lag_median:.1f}m' if gex_lag_median is not None else 'n/a'},gex_max:{f'{gex_lag_max:.1f}m' if gex_lag_max is not None else 'n/a'} | "
        f"layer_signals={len(all_layer_signals)} actionable_tickers={len(actionable_by_ticker)} "
        f"states={dict(state_counts.most_common(8))} "
        f"timing=features:{feature_build_elapsed:.2f}s,previous:{previous_lookup_elapsed:.2f}s,relational:{relational_elapsed:.2f}s,"
        f"fusion:{fusion_elapsed:.2f}s,market:{market_layers_elapsed:.2f}s,calibration:{calibration_lookup_elapsed:.2f}s,"
        f"layer_persist:{layer_persist_elapsed:.2f}s,lifecycle:{lifecycle_elapsed:.2f}s,feature_persist:{persist_elapsed:.2f}s | "
        f"entry_gate={'OPEN' if entries_allowed else 'CLOSED'} scope={entry_gate_scope} reason={entry_gate_reason} "
        f"live_requirements=score>={config.PRECISION_FIRST_MIN_LIVE_SCORE:.0f}+projection>={config.PRECISION_FIRST_MIN_LIVE_PROJECTION:.0f}%+confirms>={config.PRECISION_FIRST_MIN_CONFIRMING_BLOCKS} "
        f"fusion_actionable={sum(len(v) for v in actionable_by_ticker.values())} candidates_after_gate={len(candidates)} "
        f"precision_rejected={precision_rejected} precision_reasons={dict(precision_reasons.most_common(6))} "
        f"portfolio_rejected={sum(entry_rejections.values())} portfolio_reasons={dict(entry_rejections.most_common(6))} "
        f"inserted={inserted} delivered_live={len(live_cards)} delivered_shadow={len(shadow_cards)} lifecycle_closed={closed} | "
        f"audit={market_audit.get('status','n/a')} mature_events={audit_funnel.get('movements',0)} "
        f"audit_executed={bool(market_audit.get('executed'))} | "
        f"learning={adaptive_learning.get('status','n/a')} learning_executed={bool(adaptive_learning.get('executed'))} "
        f"shadow_model={adaptive_learning.get('shadow_model_id') or 0} research_gate={effective_research_gate():.1f}",
        send_discord=False,
    )
    emit_console_snapshot(context=f"scan end run={int(run.get('id') or 0)}")
    return observations if historical else inserted

