from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from eod_discord import publish_alpha_options, publish_alpha_swing_events
from alpha_swing_engine import process_alpha_swing_book
from eod_options_lab import build_eod_shadow_portfolio, write_eod_shadow_artifact

_MARKET_TZ = ZoneInfo("America/New_York")
_STATE_PRIORITY = {"ENTER": 9, "READY": 8, "RETEST": 7, "EARLY": 6, "DISCOVERED": 5, "WATCH": 4, "DIVERGENCE": 3, "CAPPED": 1, "MARKET_BLOCKED": 0}


def _clock_minutes(value: str) -> int:
    hour, minute = [int(x) for x in str(value).split(":", 1)]
    return hour * 60 + minute


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _f(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _preclose_window_open(now_market: datetime | None = None) -> bool:
    now_market = now_market or datetime.now(_MARKET_TZ)
    if now_market.tzinfo is None:
        now_market = now_market.replace(tzinfo=_MARKET_TZ)
    else:
        now_market = now_market.astimezone(_MARKET_TZ)
    if now_market.weekday() >= 5:
        return False
    start = _clock_minutes(getattr(config, "SETUP_REPORT_PRECLOSE_MIN_NY", "14:30"))
    current = now_market.hour * 60 + now_market.minute
    return start <= current <= 16 * 60 + 15


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ALERTS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _json(value: str | None) -> dict:
    try:
        item = json.loads(value or "{}")
        return item if isinstance(item, dict) else {}
    except Exception:
        return {}


def _latest_fusion_run(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(source_scanner_run_id) AS run_id FROM layer_signal_history WHERE UPPER(layer)='FUSION'").fetchone()
    return int((row["run_id"] if row else 0) or 0)


def _candidate_score(features: dict, signal: dict, lifecycle: dict | None = None) -> tuple[float, float, float, float, float]:
    lifecycle = lifecycle or {}
    direction = str(signal.get("direction") or "").upper()
    sign = 1.0 if direction == "LONG" else -1.0
    pipeline = _f(signal.get("score") or features.get("active_signal_score") or features.get("fusion_score"))
    early = max(_f(signal.get("core_rank_score")), _f(features.get("core_rank_score")), _f(features.get("active_core_score")), _f(signal.get("core_probability_pct")))
    sector_raw = _f(features.get("sector_etf_score") or features.get("sector_context_score")) * sign
    sector = _clamp(50.0 + sector_raw * 0.5)
    call_z = _f(features.get("otm_call_volume_z")); put_z = _f(features.get("otm_put_volume_z"))
    flow = _clamp(50.0 + 15.0 * ((call_z - put_z) * sign))
    persistence = _clamp(_f(lifecycle.get("persistence_score"), 50.0))
    improvement = _clamp(_f(lifecycle.get("improvement_score"), 50.0))
    lifecycle_score = round(0.60 * persistence + 0.40 * improvement, 3)
    return pipeline, early, sector, flow, lifecycle_score


def _volatility_proxy_input(features: dict) -> dict:
    realized = _f(features.get("realized_volatility_20d"))
    if realized > 0:
        return {"source": "REALIZED_VOLATILITY_20D", "daily_pct": round(realized, 6)}
    atr = _f(features.get("atr_pct"))
    if atr > 0:
        return {"source": "ATR_RANGE_TO_SIGMA_PROXY", "daily_pct": round(atr, 6)}
    return {"source": "UNAVAILABLE", "daily_pct": None}


def _gex_profile(features: dict) -> dict:
    expiries = []
    for row in features.get("top_expirations") or []:
        if not isinstance(row, dict):
            continue
        expiry = str(row.get("expiration_date") or row.get("expiration") or "")[:10]
        if expiry:
            expiries.append({"expiration": expiry})
    return {
        "gammaFlip": features.get("gamma_flip"),
        "summaryByHorizon": {"swing": {
            "callWallStrike": features.get("swing_call_wall_strike") or features.get("all_call_wall_strike") or features.get("call_wall"),
            "putWallStrike": features.get("swing_put_wall_strike") or features.get("all_put_wall_strike") or features.get("put_wall"),
            "gammaFlip": features.get("swing_gamma_flip") or features.get("gamma_flip"),
        }},
        "gexByExpiry": expiries,
    }


def _swing_context(features: dict, signal: dict) -> dict:
    payload = _json(signal.get("payload_json"))
    return {
        "state": str(signal.get("_state") or payload.get("opportunity_state") or payload.get("state") or signal.get("state") or "WATCH").upper(),
        "actionable": bool(payload.get("actionable") if payload.get("actionable") is not None else signal.get("actionable")),
        "rationale": payload.get("rationale") or signal.get("rationale"),
        "blockers": list(payload.get("blockers") or []),
        "core_score": payload.get("core_score") if payload.get("core_score") is not None else signal.get("core_rank_score"),
        "core_probability_pct": payload.get("probability_pct") if payload.get("probability_pct") is not None else signal.get("core_probability_pct"),
        "core_directional_edge": payload.get("core_directional_edge"),
        "gex_score": payload.get("gex_score"),
        "gex_intent": payload.get("gex_intent") or signal.get("gex_intent"),
        "gex_diagnostic_code": payload.get("gex_diagnostic_code"),
        "intraday_state": payload.get("intraday_state"),
        "intraday_blocks": list(payload.get("intraday_blocks") or []),
        "intraday_adverse": list(payload.get("intraday_adverse") or []),
        "target_price": payload.get("target_price") if payload.get("target_price") is not None else signal.get("target_price_mid"),
        "target_pct": payload.get("target_pct") if payload.get("target_pct") is not None else signal.get("target_pct"),
        "invalidation_price": payload.get("opposite_level_price") if payload.get("opposite_level_price") is not None else payload.get("wall_support"),
        "invalidation_level_name": payload.get("opposite_level_name"),
        "max_hold_sessions": int(_f(payload.get("max_hold_sessions") or payload.get("timing_max_sessions") or signal.get("timing_max_sessions"), 5)),
        "timing_min_sessions": int(_f(payload.get("timing_min_sessions") or signal.get("timing_min_sessions"), 1)),
        "market_regime": features.get("market_regime"),
        "market_policy": payload.get("market_policy") or features.get("market_policy"),
        "sector_context_score": features.get("sector_etf_score") or features.get("sector_context_score"),
        "relationship_context_score": features.get("relationship_context_score"),
        "relative_strength_vs_market": features.get("relative_strength_vs_market"),
        "relative_strength_vs_sector": features.get("relative_strength_vs_sector"),
        "option_signal": features.get("option_signal"),
        "unusual_flow_signal": features.get("unusual_flow_signal"),
        "source_gex_run_id": int(signal.get("source_gex_run_id") or features.get("gex_run_id") or 0),
        "model_version_id": int(signal.get("model_version_id") or payload.get("core_model_id") or 0),
        "contributor_engines": list(payload.get("contributor_engines") or []),
    }


def _intraday_lifecycle(signals: list[dict], snapshot_date: str) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in signals:
        if str(row.get("snapshot_date") or "")[:10] != snapshot_date[:10]:
            continue
        ticker = str(row.get("ticker") or "").upper()
        if ticker:
            grouped[ticker].append(row)
    out: dict[str, dict] = {}
    for ticker, rows in grouped.items():
        rows.sort(key=lambda r: (str(r.get("observed_at_madrid") or ""), int(r.get("id") or 0)))
        first = rows[0]; last = rows[-1]
        last_direction = str(last.get("direction") or "").upper()
        same_direction = [r for r in rows if str(r.get("direction") or "").upper() == last_direction]
        states = [str(r.get("opportunity_state") or r.get("state") or "WATCH").upper() for r in same_direction]
        scores = [_f(r.get("score")) for r in same_direction]
        state_ranks = [_STATE_PRIORITY.get(s, 2) for s in states]
        upgrades = sum(1 for i in range(1, len(state_ranks)) if state_ranks[i] > state_ranks[i - 1])
        downgrades = sum(1 for i in range(1, len(state_ranks)) if state_ranks[i] < state_ranks[i - 1])
        direction_consistency = 100.0 * len(same_direction) / max(1, len(rows))
        observations = len(same_direction)
        persistence = _clamp(25.0 + min(45.0, observations * 7.5) + 0.30 * direction_consistency)
        score_delta = (scores[-1] - scores[0]) if len(scores) >= 2 else 0.0
        state_delta = (state_ranks[-1] - state_ranks[0]) if len(state_ranks) >= 2 else 0
        improvement = _clamp(50.0 + score_delta * 1.5 + state_delta * 7.5 + upgrades * 4.0 - downgrades * 8.0)
        out[ticker] = {
            "first_seen_madrid": first.get("observed_at_madrid"), "last_seen_madrid": last.get("observed_at_madrid"),
            "observations": len(rows), "same_direction_observations": observations,
            "direction_consistency_pct": round(direction_consistency, 2),
            "first_state": states[0] if states else None, "last_state": states[-1] if states else None,
            "max_state": max(states, key=lambda s: _STATE_PRIORITY.get(s, 2)) if states else None,
            "first_score": scores[0] if scores else None, "last_score": scores[-1] if scores else None,
            "max_score": max(scores) if scores else None, "upgrades": upgrades, "downgrades": downgrades,
            "persistence_score": round(persistence, 3), "improvement_score": round(improvement, 3),
        }
    return out


def _next_session_plan(selected: list[dict]) -> list[dict]:
    plans = []
    for row in selected:
        life = row.get("lifecycle") or {}; state = str(row.get("opportunity_state") or life.get("last_state") or "WATCH").upper(); conviction = _f(row.get("conviction"))
        if state in {"ENTER", "READY"} and conviction >= 70:
            open_action = "OPEN_CONFIRM"
        elif state in {"EARLY", "RETEST"} and conviction >= 60:
            open_action = "HOLD_OR_ADD_WATCH"
        else:
            open_action = "HOLD_WITH_INVALIDATION_WATCH"
        plans.append({
            "ticker": row.get("ticker"), "direction": row.get("direction"), "open_action": open_action,
            "confirm_if": ["direction_unchanged", "conviction_not_deteriorating", "regime_not_strongly_opposed"],
            "close_if": ["direction_flip", "thesis_invalidated", "strong_regime_change", "time_stop"],
            "source": "EOD_OPTIONS_SHADOW", "execution_allowed": False,
        })
    return plans


def build_latest_runtime_portfolio(*, max_positions: int = 5) -> dict:
    conn = _connect()
    try:
        run_id = _latest_fusion_run(conn)
        if not run_id:
            return {"status": "waiting_for_fusion", "source_scanner_run_id": 0}
        latest_rows = [dict(row) for row in conn.execute("SELECT * FROM layer_signal_history WHERE source_scanner_run_id=? AND UPPER(layer)='FUSION' ORDER BY score DESC,id DESC", (run_id,)).fetchall()]
        if not latest_rows:
            return {"status": "waiting_for_fusion", "source_scanner_run_id": run_id}
        snapshot_date = str(latest_rows[0].get("snapshot_date") or "")[:10]
        day_signals = [dict(row) for row in conn.execute("SELECT * FROM layer_signal_history WHERE snapshot_date=? AND UPPER(layer)='FUSION' ORDER BY observed_at_madrid,id", (snapshot_date,)).fetchall()]
        feature_rows = [dict(row) for row in conn.execute("SELECT * FROM layer_feature_history WHERE source_scanner_run_id=?", (run_id,)).fetchall()]
    finally:
        conn.close()

    lifecycle_by_ticker = _intraday_lifecycle(day_signals, snapshot_date)
    features_by_ticker: dict[str, dict] = {}; feature_meta: dict[str, dict] = {}
    for row in feature_rows:
        ticker = str(row.get("ticker") or "").upper(); payload = _json(row.get("payload_json"))
        features_by_ticker[ticker] = payload.get("features") if isinstance(payload.get("features"), dict) else {}; feature_meta[ticker] = row

    strongest: dict[str, dict] = {}
    for signal in latest_rows:
        ticker = str(signal.get("ticker") or "").upper(); direction = str(signal.get("direction") or "").upper()
        if not ticker or direction not in {"LONG", "SHORT"}:
            continue
        state = str(signal.get("opportunity_state") or signal.get("state") or "WATCH").upper(); rank = _STATE_PRIORITY.get(state, 2) * 1000.0 + _f(signal.get("score"))
        if ticker not in strongest or rank > strongest[ticker]["_rank"]:
            strongest[ticker] = {**signal, "_rank": rank, "_state": state}

    candidates: list[dict] = []; gex_profiles: dict[str, dict] = {}; observed_at_madrid = ""
    for ticker, signal in strongest.items():
        features = features_by_ticker.get(ticker) or {}; meta = feature_meta.get(ticker) or {}; life = lifecycle_by_ticker.get(ticker) or {}; spot = _f(meta.get("price") or features.get("price"))
        if spot <= 0:
            continue
        pipeline, early, sector, flow, lifecycle_score = _candidate_score(features, signal, life); blended_early = round(0.65 * early + 0.35 * lifecycle_score, 3)
        swing_context = _swing_context(features, signal)
        candidates.append({
            "ticker": ticker,
            "direction": str(signal.get("direction") or "").upper(),
            "spot": spot,
            "score": pipeline,
            "early_alpha_score": blended_early,
            "sector_strength": sector,
            "flow_score": flow,
            "opportunity_state": signal.get("_state"),
            "signal_origin": signal.get("signal_origin"),
            "lifecycle": life,
            "swing_context": swing_context,
            "volatility_proxy_input": _volatility_proxy_input(features),
        })
        gex_profiles[ticker] = _gex_profile(features); observed_at_madrid = observed_at_madrid or str(meta.get("observed_at_madrid") or signal.get("observed_at_madrid") or "")

    payload = build_eod_shadow_portfolio(candidates, gex_profiles, asof=snapshot_date or datetime.now(_MARKET_TZ).date().isoformat(), max_positions=max_positions)
    by_ticker = {str(c.get("ticker") or "").upper(): c for c in candidates}
    for row in payload.get("selected") or []:
        source = by_ticker.get(str(row.get("ticker") or "").upper()) or {}; row["opportunity_state"] = source.get("opportunity_state"); row["lifecycle"] = source.get("lifecycle") or {}
    payload.update({"status": "ok", "source_scanner_run_id": run_id, "source_observed_at_madrid": observed_at_madrid, "lane": "CONNECTED_INTRADAY_TO_EOD", "lifecycle_version": 2, "next_session_plan": _next_session_plan(payload.get("selected") or []), "connected_to_intraday_alert_history": True})
    payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return payload


def _payload_fingerprint(payload: dict) -> str:
    clone = {key: value for key, value in payload.items() if key != "fingerprint"}
    return hashlib.sha256(json.dumps(clone, sort_keys=True, default=str).encode()).hexdigest()


def run_alpha_swing_output(*, now_market: datetime | None = None, max_positions: int = 5) -> dict:
    """Run the user-facing Alpha Swing List and its persistent lifecycle."""
    now_market = now_market or datetime.now(_MARKET_TZ)
    if now_market.tzinfo is None:
        now_market = now_market.replace(tzinfo=_MARKET_TZ)
    else:
        now_market = now_market.astimezone(_MARKET_TZ)
    if now_market.weekday() >= 5:
        return {"status": "outside_market_day"}

    payload = build_latest_runtime_portfolio(max_positions=max_positions)
    if payload.get("status") != "ok":
        return payload

    book = process_alpha_swing_book(payload)
    payload = book.get("payload") or payload
    payload["product"] = "ALPHA_SWING_LIST"
    payload["book_summary"] = book.get("summary") or {}
    payload["fingerprint"] = _payload_fingerprint(payload)

    event_delivery = publish_alpha_swing_events(book.get("events") or [])
    list_delivery = publish_alpha_options(payload, now_market=now_market)

    output_dir = config.OUTPUT_DIR / "alpha_lab"
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "latest_alpha_swing_list.json"
    previous = _json(latest.read_text(encoding="utf-8")) if latest.exists() else {}
    artifact_status = "unchanged"
    if previous.get("fingerprint") != payload.get("fingerprint"):
        latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        history = output_dir / "alpha_swing_list_history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        artifact_status = "written"

    return {
        **payload,
        "status": "ok",
        "artifact_status": artifact_status,
        "artifact": str(latest),
        "alpha_output_status": list_delivery.get("status"),
        "alpha_event_delivery": event_delivery,
        "alpha_events": book.get("events") or [],
    }


def run_eod_options_shadow(*, now_market: datetime | None = None, max_positions: int = 5) -> dict:
    if not _preclose_window_open(now_market):
        return {"status": "outside_preclose_window"}
    payload = build_latest_runtime_portfolio(max_positions=max_positions)
    if payload.get("status") != "ok":
        return payload

    # The EOD lane is research/artifact-only. Alpha Swing Output owns Discord
    # publication. Keeping a single publisher prevents fingerprint ping-pong
    # between the live Swing payload and the frozen EOD research payload.
    output_dir = config.OUTPUT_DIR / "alpha_lab"; latest = output_dir / "latest_eod_options_shadow.json"
    if latest.exists():
        previous = _json(latest.read_text(encoding="utf-8"))
        if previous.get("fingerprint") == payload.get("fingerprint") and previous.get("source_scanner_run_id") == payload.get("source_scanner_run_id"):
            return {**payload, "artifact_status": "unchanged", "artifact": str(latest), "alpha_options_discord_status": "owned_by_alpha_swing_output"}
    artifact = write_eod_shadow_artifact(payload, output_dir)
    return {**payload, "artifact_status": "written", "artifact": artifact, "alpha_options_discord_status": "owned_by_alpha_swing_output"}
