from __future__ import annotations

"""Quality-controlled broad-market warnings for TMAD.

CLOSE ALL LONGS / CLOSE ALL SHORTS is a *parallel anticipatory signal*.
It can warn before the GEX of an individual ticker reverses, but it never closes
that position. During an active broad warning, an individual exit requires the
position ticker's own GEX to confirm the reversal. The ±5% statistical hit and a
GEX target touch are milestones, never automatic exits.
"""

from collections import defaultdict
from typing import Any
import json
import sqlite3

import config
from db import get_runtime_state, set_runtime_state
from time_utils import ts_madrid_str, ts_utc_str

MAIN_ETF_WEIGHTS = {"SPY": 0.35, "QQQ": 0.30, "IWM": 0.20, "DIA": 0.15}
LONG_VOL_WEIGHTS = {
    "VIX": 1.00,
    "VVIX": 0.85,
    "UVIX": 0.80,
    "UVXY": 0.75,
    "VXX": 0.65,
    "VIXY": 0.55,
    "VIXM": 0.45,
    "VXZ": 0.40,
}
SHORT_VOL_WEIGHTS = {"SVIX": 0.70, "SVXY": 0.65, "ZIVB": 0.45}
_USABLE_CORE_STATUSES = {"CHAMPION", "PROVISIONAL_CHAMPION", "SCHEDULED_CHAMPION"}


def _strategy_namespace() -> str:
    """Namespace runtime state by strategy version.

    Persistent 03_DATOS survives deployments. Without a versioned namespace, an
    ACTIVE/PENDING signal from an older algorithm could leak into the new one.
    """
    raw = str(getattr(config, "MARKET_CLOSE_ALL_STRATEGY_VERSION", "DEFAULT") or "DEFAULT")
    normalized = "".join(char.lower() if char.isalnum() else "_" for char in raw)
    return "_".join(part for part in normalized.split("_") if part)[:96] or "default"


def _signal_state_prefix(direction: str) -> str:
    side = "long" if str(direction or "long").lower() == "long" else "short"
    return f"market_close_all_{_strategy_namespace()}_signal_{side}"


def _delivery_state_key(direction: str, suffix: str) -> str:
    side = "long" if str(direction or "long").lower() == "long" else "short"
    return f"market_close_all_{_strategy_namespace()}_{side}_{suffix}"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value not in (None, "") else int(default)
    except Exception:
        return int(default)


def _clamp(value: float, low: float = -100.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _weighted(values: list[tuple[float | None, float]]) -> float:
    clean = [(float(value), float(weight)) for value, weight in values if value is not None and weight > 0]
    if not clean:
        return 0.0
    denominator = sum(weight for _value, weight in clean)
    return sum(value * weight for value, weight in clean) / denominator if denominator else 0.0


def _weight_coverage(values: list[tuple[float | None, float]]) -> float:
    return round(sum(weight for value, weight in values if value is not None), 4)


def _agreement(values: list[tuple[float | None, float]], sign: int) -> float:
    clean = [(float(value), float(weight)) for value, weight in values if value is not None and weight > 0]
    if not clean:
        return 0.0
    total = sum(weight for _value, weight in clean)
    aligned = sum(weight for value, weight in clean if value * sign > 0)
    return round(aligned / total if total else 0.0, 4)


def _signal_direction(signal: dict | None) -> int:
    direction = str((signal or {}).get("direction") or "").lower()
    return 1 if direction == "long" else (-1 if direction == "short" else 0)


def _best_layer_signals(signals: list[dict], layer: str) -> dict[str, dict]:
    best: dict[str, dict] = {}
    layer = str(layer).upper()
    for row in signals or []:
        if str(row.get("layer") or "").upper() != layer:
            continue
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        state = str(row.get("opportunity_state") or row.get("state") or "WATCH").upper()
        state_rank = {
            "ENTER": 9,
            "READY": 8,
            "RETEST": 7,
            "EARLY": 6,
            "DISCOVERED": 5,
            "WATCH": 4,
            "DIVERGENCE": 1,
            "CAPPED": 0,
        }.get(state, 3)
        rank = state_rank * 1000.0 + _f(row.get("score"))
        if ticker not in best or rank > _f(best[ticker].get("_market_rank"), -1.0):
            best[ticker] = {**row, "_market_rank": rank}
    return best


def _core_signal_state(signal: dict | None) -> dict:
    """Return one ETF's *calibrated* directional edge.

    Probability is converted to an edge around 50%. Thus four 68% SHORT signals
    aggregate to 68% risk-off, not an inflated 84%. Heuristic, sample-zero
    probabilities remain visible but cannot trigger the broad alert.
    """
    row = signal or {}
    direction = _signal_direction(row)
    source = str(row.get("probability_source") or "").upper()
    sample = _i(row.get("probability_sample"), 0)
    probability = row.get("probability_pct")
    p = _clamp(_f(probability, 50.0), 0.0, 100.0)
    state = str(row.get("opportunity_state") or row.get("state") or "WATCH").upper()
    state_usable = state not in {"DIVERGENCE", "CAPPED", "MARKET_BLOCKED", "EXTENDED", "NONE", "INVALID"}
    usable = bool(
        direction
        and probability not in (None, "")
        and source in set(config.MARKET_CORE_ALLOWED_PROBABILITY_SOURCES)
        and sample >= int(config.MARKET_CORE_MIN_SIGNAL_SAMPLE)
        and p > 50.0
        and state_usable
    )
    signed_edge = float(direction) * (2.0 * p - 100.0) if usable else None
    return {
        "direction": row.get("direction"),
        "score": row.get("score"),
        "probability_pct": probability,
        "probability_source": source or None,
        "probability_sample": sample,
        "state": state,
        "state_usable": state_usable,
        "rule_name": row.get("rule_name") or row.get("core_rule"),
        "usable": usable,
        "signed_edge": round(signed_edge, 4) if signed_edge is not None else None,
    }


def _bias_sign(features: dict) -> int:
    text = " ".join(
        str(features.get(key) or "")
        for key in ("profile_bias", "gex_intent", "active_gex_intent", "option_signal", "unusual_flow_signal")
    ).lower()
    call = any(token in text for token in ("call", "bull", "positive", "long"))
    put = any(token in text for token in ("put", "bear", "negative", "short"))
    if call and not put:
        return 1
    if put and not call:
        return -1
    return 0


def _has_gex(features: dict | None) -> bool:
    data = features or {}
    if _i(data.get("has_gex"), 0):
        return True
    return any(
        data.get(key) not in (None, "")
        for key in (
            "total_gex_change_pct",
            "front_gex_change_pct",
            "short_gex_change_pct",
            "call_accumulation_score",
            "put_accumulation_score",
            "profile_bias",
            "gex_intent",
        )
    )


def _gex_directional_score(features: dict | None) -> float | None:
    """Return risk-on directional GEX score for one underlying in [-100,100]."""
    data = features or {}
    if not _has_gex(data):
        return None
    bias = _bias_sign(data)
    call_acc = _f(data.get("call_accumulation_score"))
    put_acc = _f(data.get("put_accumulation_score"))
    call_flow = _f(data.get("otm_call_volume_z"))
    put_flow = _f(data.get("otm_put_volume_z"))
    score = (call_acc - put_acc) * 0.65
    score += _clamp((call_flow - put_flow) * 7.0, -24.0, 24.0)
    if bias:
        score += bias * 20.0
    # GEX changes are signed directional evidence. A CALL-labelled profile with
    # sharply negative 0–7d/near changes is a divergence and must reduce, not
    # strengthen, the risk-on score.
    changes = [
        data.get("front_gex_change_pct"),
        data.get("short_gex_change_pct"),
        data.get("near_gex_change_pct"),
        data.get("swing_gex_change_pct"),
    ]
    score += sum(
        _clamp(_f(value) * 0.06, -6.0, 6.0)
        for value in changes
        if value not in (None, "")
    )
    gamma_distance = data.get("gamma_flip_distance_pct")
    if gamma_distance not in (None, ""):
        # A flip above current price is a headwind; below price is supportive.
        score -= _clamp(_f(gamma_distance) * 4.0, -12.0, 12.0)
    return round(_clamp(score), 4)


def _compact_gex_state(features: dict | None, *, invert: bool = False) -> dict:
    data = features or {}
    score = _gex_directional_score(data)
    converted = None if score is None else (-score if invert else score)
    return {
        "available": score is not None,
        "score": round(converted, 2) if converted is not None else None,
        "risk_on_score": round(score, 2) if score is not None else None,
        "bias": data.get("profile_bias") or data.get("active_gex_intent") or data.get("gex_intent"),
        "call_acc": data.get("call_accumulation_score"),
        "put_acc": data.get("put_accumulation_score"),
        "call_flow_z": data.get("otm_call_volume_z"),
        "put_flow_z": data.get("otm_put_volume_z"),
        "front_change": data.get("front_gex_change_pct"),
        "short_change": data.get("short_gex_change_pct"),
        "total_change": data.get("total_gex_change_pct"),
        "gamma_flip_distance_pct": data.get("gamma_flip_distance_pct"),
        "expiry": data.get("expiry_date") or data.get("dominant_expiration"),
    }


# ---------------------------------------------------------------------------
# Prospective calibration of the broad alert itself
# ---------------------------------------------------------------------------


def _connect_market_db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ALERTS_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def ensure_market_signal_schema() -> None:
    conn = _connect_market_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_close_all_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_key TEXT NOT NULL UNIQUE,
                direction TEXT NOT NULL,
                action TEXT NOT NULL,
                activated_at_utc TEXT NOT NULL,
                activated_at_madrid TEXT NOT NULL,
                activated_date TEXT NOT NULL,
                source_run_id TEXT,
                model_key TEXT,
                strategy_version TEXT,
                entry_prices_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                horizon_sessions INTEGER NOT NULL,
                target_move_pct REAL NOT NULL,
                sessions_observed INTEGER NOT NULL DEFAULT 0,
                max_favorable_pct REAL,
                max_adverse_pct REAL,
                horizon_return_pct REAL,
                first_hit_session INTEGER,
                hit INTEGER,
                outcome_json TEXT,
                labeled_at_utc TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_market_close_all_events_direction_status
                ON market_close_all_events(direction,status,activated_date);
            """
        )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(market_close_all_events)").fetchall()}
        if "model_key" not in columns:
            conn.execute("ALTER TABLE market_close_all_events ADD COLUMN model_key TEXT")
        if "strategy_version" not in columns:
            conn.execute("ALTER TABLE market_close_all_events ADD COLUMN strategy_version TEXT")
        if "first_hit_session" not in columns:
            conn.execute("ALTER TABLE market_close_all_events ADD COLUMN first_hit_session INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_close_all_events_model "
            "ON market_close_all_events(direction,strategy_version,model_key,status)"
        )
        conn.commit()
    finally:
        conn.close()


def _record_market_signal_event(decision: dict) -> int | None:
    ensure_market_signal_schema()
    direction = str(decision.get("direction") or "long").lower()
    snapshot = decision.get("market_snapshot") or {}
    run_id = str(decision.get("run_id") or "")
    activation_key = str(decision.get("activation_key") or f"{direction}:{run_id}:{ts_madrid_str()}")
    prices = {
        ticker: _f((snapshot.get("main_etf_prices") or {}).get(ticker), 0.0)
        for ticker in MAIN_ETF_WEIGHTS
    }
    covered = sum(weight for ticker, weight in MAIN_ETF_WEIGHTS.items() if prices.get(ticker, 0.0) > 0)
    if covered < float(config.MARKET_CORE_MIN_WEIGHT_COVERAGE):
        return None
    activated_madrid = ts_madrid_str()
    activated_date = str(decision.get("snapshot_date") or activated_madrid[:10])
    model_key = str(snapshot.get("market_core_model_key") or snapshot.get("market_core_model_id") or "UNKNOWN")
    strategy_version = str(getattr(config, "MARKET_CLOSE_ALL_STRATEGY_VERSION", "MARKET_CORE_GEX_VIX_PARALLEL_V1"))
    conn = _connect_market_db()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO market_close_all_events(
                   signal_key,direction,action,activated_at_utc,activated_at_madrid,activated_date,
                   source_run_id,model_key,strategy_version,entry_prices_json,snapshot_json,status,horizon_sessions,target_move_pct
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)""",
            (
                activation_key,
                direction,
                str(decision.get("action") or ""),
                ts_utc_str(),
                activated_madrid,
                activated_date,
                run_id,
                model_key,
                strategy_version,
                json.dumps(prices, ensure_ascii=False),
                json.dumps(decision, ensure_ascii=False, default=str),
                int(config.MARKET_CLOSE_ALL_OUTCOME_HORIZON_SESSIONS),
                float(config.MARKET_CLOSE_ALL_OUTCOME_MOVE_PCT),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM market_close_all_events WHERE signal_key=?", (activation_key,)).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def _event_sessions(conn: sqlite3.Connection, event: sqlite3.Row) -> list[dict]:
    try:
        entries = {str(k).upper(): float(v) for k, v in json.loads(event["entry_prices_json"] or "{}").items() if float(v) > 0}
    except Exception:
        entries = {}
    if not entries:
        return []
    tickers = [ticker for ticker in MAIN_ETF_WEIGHTS if ticker in entries]
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"""SELECT ticker,trade_date,high,low,close FROM price_cache
            WHERE ticker IN ({placeholders}) AND trade_date>?
            ORDER BY trade_date,ticker""",
        (*tickers, str(event["activated_date"])),
    ).fetchall()
    by_date: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_date[str(row["trade_date"])].append(row)
    output: list[dict] = []
    direction = str(event["direction"] or "long").lower()
    for trade_date in sorted(by_date):
        favorable_values: list[tuple[float | None, float]] = []
        adverse_values: list[tuple[float | None, float]] = []
        close_values: list[tuple[float | None, float]] = []
        for row in by_date[trade_date]:
            ticker = str(row["ticker"]).upper()
            entry = entries.get(ticker, 0.0)
            weight = MAIN_ETF_WEIGHTS.get(ticker, 0.0)
            if entry <= 0 or weight <= 0:
                continue
            high = _f(row["high"], 0.0)
            low = _f(row["low"], 0.0)
            close = _f(row["close"], 0.0)
            if not (high > 0 and low > 0 and close > 0):
                continue
            if direction == "long":  # close longs predicts downside
                favorable = max(0.0, (entry - low) / entry * 100.0)
                adverse = max(0.0, (high - entry) / entry * 100.0)
                close_ret = (entry - close) / entry * 100.0
            else:  # close shorts predicts upside
                favorable = max(0.0, (high - entry) / entry * 100.0)
                adverse = max(0.0, (entry - low) / entry * 100.0)
                close_ret = (close - entry) / entry * 100.0
            favorable_values.append((favorable, weight))
            adverse_values.append((adverse, weight))
            close_values.append((close_ret, weight))
        coverage = _weight_coverage(favorable_values)
        if coverage < float(config.MARKET_CORE_MIN_WEIGHT_COVERAGE):
            continue
        output.append({
            "trade_date": trade_date,
            "coverage": coverage,
            "favorable_pct": round(_weighted(favorable_values), 6),
            "adverse_pct": round(_weighted(adverse_values), 6),
            "close_return_pct": round(_weighted(close_values), 6),
        })
    return output


def materialize_market_signal_outcomes() -> int:
    """Label completed signals prospectively, using sessions after activation."""
    ensure_market_signal_schema()
    conn = _connect_market_db()
    completed = 0
    try:
        events = conn.execute("SELECT * FROM market_close_all_events WHERE status='OPEN' ORDER BY id").fetchall()
        for event in events:
            sessions = _event_sessions(conn, event)
            horizon = max(1, _i(event["horizon_sessions"], int(config.MARKET_CLOSE_ALL_OUTCOME_HORIZON_SESSIONS)))
            observed = min(len(sessions), horizon)
            selected = sessions[:horizon]
            max_favorable = max((row["favorable_pct"] for row in selected), default=None)
            max_adverse = max((row["adverse_pct"] for row in selected), default=None)
            horizon_return = selected[-1]["close_return_pct"] if selected else None
            target_move = _f(event["target_move_pct"], float(config.MARKET_CLOSE_ALL_OUTCOME_MOVE_PCT))
            first_hit_session = next(
                (index for index, row in enumerate(selected, start=1) if _f(row.get("favorable_pct")) >= target_move),
                None,
            )
            is_complete = len(selected) >= horizon
            hit = int(first_hit_session is not None) if is_complete else None
            conn.execute(
                """UPDATE market_close_all_events SET
                       sessions_observed=?,max_favorable_pct=?,max_adverse_pct=?,horizon_return_pct=?,
                       first_hit_session=?,status=?,hit=?,outcome_json=?,labeled_at_utc=?
                   WHERE id=?""",
                (
                    observed,
                    max_favorable,
                    max_adverse,
                    horizon_return,
                    first_hit_session,
                    "MATURED" if is_complete else "OPEN",
                    hit,
                    json.dumps(selected, ensure_ascii=False),
                    ts_utc_str() if is_complete else None,
                    int(event["id"]),
                ),
            )
            completed += int(is_complete)
        conn.commit()
    finally:
        conn.close()
    return completed


def _wilson_lower_bound_pct(hits: int, sample: int, z: float = 1.6448536269514722) -> float:
    """One-sided 90% Wilson lower confidence bound for a binomial hit rate."""
    n = max(0, int(sample))
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(hits) / n))
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    margin = z * ((p * (1.0 - p) / n + z2 / (4.0 * n * n)) ** 0.5)
    return round(max(0.0, (centre - margin) / denominator) * 100.0, 2)


def market_signal_calibration_metrics(*, model_key: str | None = None, strategy_version: str | None = None) -> dict[str, dict]:
    ensure_market_signal_schema()
    conn = _connect_market_db()
    try:
        result: dict[str, dict] = {}
        strategy = str(strategy_version or getattr(config, "MARKET_CLOSE_ALL_STRATEGY_VERSION", "MARKET_CORE_GEX_VIX_PARALLEL_V1"))
        normalized_model = str(model_key or "").strip()
        for direction in ("long", "short"):
            filters = ["direction=?", "status='MATURED'", "COALESCE(strategy_version,'')=?"]
            params: list[Any] = [direction, strategy]
            open_filters = ["direction=?", "status='OPEN'", "COALESCE(strategy_version,'')=?"]
            open_params: list[Any] = [direction, strategy]
            if normalized_model:
                filters.append("COALESCE(model_key,'')=?")
                params.append(normalized_model)
                open_filters.append("COALESCE(model_key,'')=?")
                open_params.append(normalized_model)
            rows = conn.execute(
                "SELECT * FROM market_close_all_events WHERE " + " AND ".join(filters) + " ORDER BY id",
                tuple(params),
            ).fetchall()
            open_count = _i(conn.execute(
                "SELECT COUNT(*) FROM market_close_all_events WHERE " + " AND ".join(open_filters),
                tuple(open_params),
            ).fetchone()[0], 0)
            sample = len(rows)
            hits = sum(_i(row["hit"], 0) for row in rows)
            hit_rate = round(hits / sample * 100.0, 2) if sample else 0.0
            avg_mfe = round(sum(_f(row["max_favorable_pct"]) for row in rows) / sample, 3) if sample else None
            avg_mae = round(sum(_f(row["max_adverse_pct"]) for row in rows) / sample, 3) if sample else None
            avg_horizon = round(sum(_f(row["horizon_return_pct"]) for row in rows) / sample, 3) if sample else None
            hit_sessions = [_i(row["first_hit_session"]) for row in rows if row["first_hit_session"] not in (None, "")]
            avg_first_hit = round(sum(hit_sessions) / len(hit_sessions), 3) if hit_sessions else None
            min_events = int(config.MARKET_CLOSE_ALL_VALIDATION_MIN_EVENTS)
            min_hit = float(config.MARKET_CLOSE_ALL_VALIDATION_MIN_HIT_RATE)
            min_wilson = float(config.MARKET_CLOSE_ALL_VALIDATION_MIN_WILSON_LOWER)
            min_ratio = float(config.MARKET_CLOSE_ALL_VALIDATION_MIN_MFE_MAE_RATIO)
            wilson_low = _wilson_lower_bound_pct(hits, sample)
            mfe_mae_ratio = round(float(avg_mfe or 0.0) / max(float(avg_mae or 0.0), 0.25), 3) if sample else None
            if sample < min_events:
                status = "ACCUMULATING"
            elif hit_rate >= min_hit and wilson_low >= min_wilson and float(mfe_mae_ratio or 0.0) >= min_ratio:
                status = "VALIDATED"
            else:
                status = "DEGRADED"
            result[direction] = {
                "status": status,
                "sample": sample,
                "open_events": open_count,
                "hits": hits,
                "hit_rate_pct": hit_rate,
                "wilson_lower_90_pct": wilson_low,
                "mfe_mae_ratio": mfe_mae_ratio,
                "avg_max_favorable_pct": avg_mfe,
                "avg_max_adverse_pct": avg_mae,
                "avg_horizon_return_pct": avg_horizon,
                "avg_first_hit_session": avg_first_hit,
                "horizon_sessions": int(config.MARKET_CLOSE_ALL_OUTCOME_HORIZON_SESSIONS),
                "target_move_pct": float(config.MARKET_CLOSE_ALL_OUTCOME_MOVE_PCT),
                "minimum_events": min_events,
                "minimum_hit_rate_pct": min_hit,
                "minimum_wilson_lower_90_pct": min_wilson,
                "minimum_mfe_mae_ratio": min_ratio,
                "method": "PROSPECTIVE_MAIN_ETF_OOS",
                "model_key": normalized_model or "ALL_MODELS",
                "strategy_version": strategy,
            }
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Feature aggregation and quality gates
# ---------------------------------------------------------------------------


def attach_market_confirmation_layers(
    features_by_ticker: dict[str, dict],
    all_layer_signals: list[dict],
    core_model: dict | None,
    relational_summary: dict | None = None,
) -> dict:
    """Attach quality-controlled MARKET CORE/GEX/VIX data to every ticker."""
    core_signals = _best_layer_signals(all_layer_signals, "CORE")
    main_core: dict[str, dict] = {}
    weighted_core: list[tuple[float | None, float]] = []
    for ticker, weight in MAIN_ETF_WEIGHTS.items():
        state = _core_signal_state(core_signals.get(ticker))
        main_core[ticker] = state
        weighted_core.append((state.get("signed_edge"), weight))
    core_coverage = sum(1 for value, _weight in weighted_core if value is not None)
    core_weight_coverage = _weight_coverage(weighted_core)
    core_risk_on_edge = round(_clamp(_weighted(weighted_core)), 4)
    core_risk_on_probability = round(_clamp(50.0 + core_risk_on_edge * 0.50, 0.0, 100.0), 2)
    core_risk_off_probability = round(100.0 - core_risk_on_probability, 2)
    core_risk_on_agreement = _agreement(weighted_core, 1)
    core_risk_off_agreement = _agreement(weighted_core, -1)

    main_gex: dict[str, dict] = {}
    weighted_main_gex: list[tuple[float | None, float]] = []
    for ticker, weight in MAIN_ETF_WEIGHTS.items():
        state = _compact_gex_state(features_by_ticker.get(ticker))
        main_gex[ticker] = state
        weighted_main_gex.append((state.get("risk_on_score"), weight))
    main_gex_coverage = sum(1 for value, _weight in weighted_main_gex if value is not None)
    main_gex_weight_coverage = _weight_coverage(weighted_main_gex)
    main_gex_risk_on_score = round(_clamp(_weighted(weighted_main_gex)), 4)
    main_gex_risk_on_agreement = _agreement(weighted_main_gex, 1)
    main_gex_risk_off_agreement = _agreement(weighted_main_gex, -1)

    vol_complex: dict[str, dict] = {}
    weighted_vol_gex: list[tuple[float | None, float]] = []
    weighted_long_vol_gex: list[tuple[float | None, float]] = []
    weighted_short_vol_gex: list[tuple[float | None, float]] = []
    long_vol_coverage = short_vol_coverage = 0
    for ticker, weight in LONG_VOL_WEIGHTS.items():
        state = _compact_gex_state(features_by_ticker.get(ticker))
        # Long-vol risk-on score is *stress*: bullish GEX in VIX products is risk-off.
        stress = state.get("risk_on_score")
        vol_complex[ticker] = {**state, "family": "LONG_VOL", "stress_score": stress}
        weighted_vol_gex.append((stress, weight))
        weighted_long_vol_gex.append((stress, weight))
        long_vol_coverage += int(stress is not None)
    for ticker, weight in SHORT_VOL_WEIGHTS.items():
        state = _compact_gex_state(features_by_ticker.get(ticker), invert=True)
        # Short-vol is inverted: bearish GEX in SVIX/SVXY/ZIVB is stress.
        stress = state.get("score")
        vol_complex[ticker] = {**state, "family": "SHORT_VOL", "stress_score": stress}
        weighted_vol_gex.append((stress, weight))
        weighted_short_vol_gex.append((stress, weight))
        short_vol_coverage += int(stress is not None)
    vol_gex_coverage = sum(1 for value, _weight in weighted_vol_gex if value is not None)
    vol_gex_stress_score = round(_clamp(_weighted(weighted_vol_gex)), 4)
    vol_gex_stress_agreement = _agreement(weighted_vol_gex, 1)
    vol_gex_relax_agreement = _agreement(weighted_vol_gex, -1)
    vol_gex_long_stress_agreement = _agreement(weighted_long_vol_gex, 1)
    vol_gex_short_stress_agreement = _agreement(weighted_short_vol_gex, 1)
    vol_gex_long_relax_agreement = _agreement(weighted_long_vol_gex, -1)
    vol_gex_short_relax_agreement = _agreement(weighted_short_vol_gex, -1)

    summary = dict(relational_summary or {})
    scanner_scores = summary.get("volatility_scores") or {}
    weighted_vol_scanner: list[tuple[float | None, float]] = []
    weighted_long_vol_scanner: list[tuple[float | None, float]] = []
    weighted_short_vol_scanner: list[tuple[float | None, float]] = []
    scanner_long_coverage = scanner_short_coverage = 0
    for ticker, weight in LONG_VOL_WEIGHTS.items():
        value = scanner_scores.get(ticker)
        score = None if value is None else _f(value)
        weighted_vol_scanner.append((score, weight))
        weighted_long_vol_scanner.append((score, weight))
        scanner_long_coverage += int(value is not None)
    for ticker, weight in SHORT_VOL_WEIGHTS.items():
        value = scanner_scores.get(ticker)
        stress = None if value is None else -_f(value)
        weighted_vol_scanner.append((stress, weight))
        weighted_short_vol_scanner.append((stress, weight))
        scanner_short_coverage += int(value is not None)
    vol_scanner_coverage = scanner_long_coverage + scanner_short_coverage
    vol_scanner_stress_score = round(_clamp(_weighted(weighted_vol_scanner)), 4)
    vol_scanner_stress_agreement = _agreement(weighted_vol_scanner, 1)
    vol_scanner_relax_agreement = _agreement(weighted_vol_scanner, -1)
    vol_scanner_long_stress_agreement = _agreement(weighted_long_vol_scanner, 1)
    vol_scanner_short_stress_agreement = _agreement(weighted_short_vol_scanner, 1)
    vol_scanner_long_relax_agreement = _agreement(weighted_long_vol_scanner, -1)
    vol_scanner_short_relax_agreement = _agreement(weighted_short_vol_scanner, -1)

    model = core_model or {}
    calibration_model_key = str(model.get("model_key") or model.get("id") or "UNKNOWN")
    try:
        materialize_market_signal_outcomes()
        calibration = market_signal_calibration_metrics(model_key=calibration_model_key)
    except Exception:
        calibration = {
            "long": {"status": "ACCUMULATING", "sample": 0, "hit_rate_pct": 0.0, "method": "PROSPECTIVE_MAIN_ETF_OOS", "model_key": calibration_model_key},
            "short": {"status": "ACCUMULATING", "sample": 0, "hit_rate_pct": 0.0, "method": "PROSPECTIVE_MAIN_ETF_OOS", "model_key": calibration_model_key},
        }

    model_status = str(model.get("status") or "NONE").upper()
    summary.update({
        "market_core_model_id": model.get("id"),
        "market_core_model_key": model.get("model_key"),
        "market_core_model_status": model_status,
        "market_core_coverage": core_coverage,
        "market_core_weight_coverage": core_weight_coverage,
        "market_core_main_etfs": main_core,
        "market_core_risk_on_score": core_risk_on_edge,
        "market_core_risk_on_probability": core_risk_on_probability,
        "market_core_risk_off_probability": core_risk_off_probability,
        "market_core_risk_on_agreement": core_risk_on_agreement,
        "market_core_risk_off_agreement": core_risk_off_agreement,
        "main_etf_gex_coverage": main_gex_coverage,
        "main_etf_gex_weight_coverage": main_gex_weight_coverage,
        "main_etf_gex_risk_on_score": main_gex_risk_on_score,
        "main_etf_gex_risk_on_agreement": main_gex_risk_on_agreement,
        "main_etf_gex_risk_off_agreement": main_gex_risk_off_agreement,
        "main_etf_gex": main_gex,
        "vol_gex_coverage": vol_gex_coverage,
        "vol_gex_long_coverage": long_vol_coverage,
        "vol_gex_short_coverage": short_vol_coverage,
        "vol_gex_stress_score": vol_gex_stress_score,
        "vol_gex_stress_agreement": vol_gex_stress_agreement,
        "vol_gex_relax_agreement": vol_gex_relax_agreement,
        "vol_gex_long_stress_agreement": vol_gex_long_stress_agreement,
        "vol_gex_short_stress_agreement": vol_gex_short_stress_agreement,
        "vol_gex_long_relax_agreement": vol_gex_long_relax_agreement,
        "vol_gex_short_relax_agreement": vol_gex_short_relax_agreement,
        "vol_complex_gex": vol_complex,
        "vol_scanner_coverage": vol_scanner_coverage,
        "vol_scanner_long_coverage": scanner_long_coverage,
        "vol_scanner_short_coverage": scanner_short_coverage,
        "vol_scanner_stress_score": vol_scanner_stress_score,
        "vol_scanner_stress_agreement": vol_scanner_stress_agreement,
        "vol_scanner_relax_agreement": vol_scanner_relax_agreement,
        "vol_scanner_long_stress_agreement": vol_scanner_long_stress_agreement,
        "vol_scanner_short_stress_agreement": vol_scanner_short_stress_agreement,
        "vol_scanner_long_relax_agreement": vol_scanner_long_relax_agreement,
        "vol_scanner_short_relax_agreement": vol_scanner_short_relax_agreement,
        "main_etf_prices": {
            ticker: _f((features_by_ticker.get(ticker) or {}).get("price"), 0.0)
            for ticker in MAIN_ETF_WEIGHTS
        },
        "market_close_all_calibration": calibration,
        "market_close_all_longs_calibration_status": (calibration.get("long") or {}).get("status"),
        "market_close_all_shorts_calibration_status": (calibration.get("short") or {}).get("status"),
        "market_close_all_longs_calibration_sample": (calibration.get("long") or {}).get("sample"),
        "market_close_all_shorts_calibration_sample": (calibration.get("short") or {}).get("sample"),
        "market_close_all_longs_hit_rate_pct": (calibration.get("long") or {}).get("hit_rate_pct"),
        "market_close_all_shorts_hit_rate_pct": (calibration.get("short") or {}).get("hit_rate_pct"),
    })
    common = {
        key: value
        for key, value in summary.items()
        if key.startswith("market_core_")
        or key.startswith("main_etf_gex_")
        or key.startswith("vol_gex_")
        or key.startswith("vol_scanner_")
        or key.startswith("market_close_all_")
        or key in {"main_etf_gex", "vol_complex_gex", "main_etf_prices"}
    }
    for features in features_by_ticker.values():
        features.update(common)
    return summary


def _market_core_data_quality(features: dict, direction: str) -> dict:
    coverage = _i(features.get("market_core_coverage"), 0)
    weight_coverage = _f(features.get("market_core_weight_coverage"))
    agreement = _f(features.get(
        "market_core_risk_off_agreement" if direction == "long" else "market_core_risk_on_agreement"
    ))
    status = str(features.get("market_core_model_status") or "").upper()
    usable_model = status in _USABLE_CORE_STATUSES
    ready = bool(
        usable_model
        and coverage >= int(config.MARKET_CORE_MIN_MAIN_ETFS)
        and weight_coverage >= float(config.MARKET_CORE_MIN_WEIGHT_COVERAGE)
        and agreement >= float(config.MARKET_CORE_MIN_DIRECTIONAL_AGREEMENT)
    )
    return {
        "ready": ready,
        "usable_model": usable_model,
        "coverage": coverage,
        "weight_coverage": weight_coverage,
        "agreement": agreement,
        "model_status": status,
    }


def _market_core_ready(features: dict, direction: str) -> bool:
    return bool(_market_core_data_quality(features, direction).get("ready"))


def _main_gex_data_ready(features: dict) -> bool:
    return bool(
        _i(features.get("main_etf_gex_coverage"), 0) >= int(config.MARKET_MAIN_GEX_MIN_ETFS)
        and _f(features.get("main_etf_gex_weight_coverage")) >= float(config.MARKET_MAIN_GEX_MIN_WEIGHT_COVERAGE)
    )


def _vol_data_ready(features: dict) -> bool:
    return bool(_vol_gex_data_ready(features) or _vol_scanner_data_ready(features))


def _vol_gex_data_ready(features: dict) -> bool:
    """Require actual GEX coverage in both volatility subfamilies."""
    return bool(
        _i(features.get("vol_gex_coverage"), 0) >= int(config.MARKET_VOL_GEX_MIN_PRODUCTS)
        and _i(features.get("vol_gex_long_coverage"), 0) >= int(config.MARKET_VOL_GEX_MIN_LONG_PRODUCTS)
        and _i(features.get("vol_gex_short_coverage"), 0) >= int(config.MARKET_VOL_GEX_MIN_SHORT_PRODUCTS)
    )


def _vol_scanner_data_ready(features: dict) -> bool:
    return bool(
        _i(features.get("vol_scanner_coverage"), 0) >= int(config.MARKET_VOL_SCANNER_MIN_PRODUCTS)
        and _i(features.get("vol_scanner_long_coverage"), 0) >= int(config.MARKET_VOL_SCANNER_MIN_LONG_PRODUCTS)
        and _i(features.get("vol_scanner_short_coverage"), 0) >= int(config.MARKET_VOL_SCANNER_MIN_SHORT_PRODUCTS)
    )


def _vol_gex_family_qualified(direction: str, features: dict) -> bool:
    """True only when the volatility confirmation is backed by actual GEX.

    Scanner consensus remains a useful independent VOL_COMPLEX confirmation, but
    it cannot satisfy MARKET_CLOSE_ALL_REQUIRE_GEX_FAMILY by itself.
    """
    stress_side = str(direction or "long").lower() == "long"
    score = _f(features.get("vol_gex_stress_score"))
    agreement = _f(features.get("vol_gex_stress_agreement" if stress_side else "vol_gex_relax_agreement"))
    long_agreement = _f(features.get("vol_gex_long_stress_agreement" if stress_side else "vol_gex_long_relax_agreement"))
    short_agreement = _f(features.get("vol_gex_short_stress_agreement" if stress_side else "vol_gex_short_relax_agreement"))
    signed = score >= float(config.MARKET_VOL_GEX_CONFIRM_SCORE) if stress_side else score <= -float(config.MARKET_VOL_GEX_CONFIRM_SCORE)
    return bool(
        signed
        and _vol_gex_data_ready(features)
        and agreement >= float(config.MARKET_VOL_GEX_MIN_AGREEMENT)
        and long_agreement >= float(config.MARKET_VOL_GEX_MIN_SUBFAMILY_AGREEMENT)
        and short_agreement >= float(config.MARKET_VOL_GEX_MIN_SUBFAMILY_AGREEMENT)
    )


def _main_gex_family(direction: str, features: dict) -> tuple[bool, str]:
    score = _f(features.get("main_etf_gex_risk_on_score"))
    count = _i(features.get("main_etf_gex_coverage"), 0)
    weight = _f(features.get("main_etf_gex_weight_coverage"))
    agreement = _f(features.get(
        "main_etf_gex_risk_off_agreement" if direction == "long" else "main_etf_gex_risk_on_agreement"
    ))
    signed_ok = score <= -float(config.MARKET_MAIN_GEX_CONFIRM_SCORE) if direction == "long" else score >= float(config.MARKET_MAIN_GEX_CONFIRM_SCORE)
    quality = bool(
        _main_gex_data_ready(features)
        and agreement >= float(config.MARKET_MAIN_GEX_MIN_AGREEMENT)
    )
    detail = f"GEX main ETFs {score:+.0f} · cobertura {count}/4 ({weight:.0%}) · acuerdo {agreement:.0%}"
    return bool(signed_ok and quality), detail


def _vol_family(direction: str, features: dict) -> tuple[bool, str]:
    stress_side = direction == "long"
    gex_score = _f(features.get("vol_gex_stress_score"))
    gex_count = _i(features.get("vol_gex_coverage"), 0)
    gex_long = _i(features.get("vol_gex_long_coverage"), 0)
    gex_short = _i(features.get("vol_gex_short_coverage"), 0)
    gex_agreement = _f(features.get("vol_gex_stress_agreement" if stress_side else "vol_gex_relax_agreement"))
    gex_long_agreement = _f(features.get("vol_gex_long_stress_agreement" if stress_side else "vol_gex_long_relax_agreement"))
    gex_short_agreement = _f(features.get("vol_gex_short_stress_agreement" if stress_side else "vol_gex_short_relax_agreement"))
    gex_signed = gex_score >= float(config.MARKET_VOL_GEX_CONFIRM_SCORE) if stress_side else gex_score <= -float(config.MARKET_VOL_GEX_CONFIRM_SCORE)
    gex_quality = bool(
        gex_count >= int(config.MARKET_VOL_GEX_MIN_PRODUCTS)
        and gex_long >= int(config.MARKET_VOL_GEX_MIN_LONG_PRODUCTS)
        and gex_short >= int(config.MARKET_VOL_GEX_MIN_SHORT_PRODUCTS)
        and gex_agreement >= float(config.MARKET_VOL_GEX_MIN_AGREEMENT)
        and gex_long_agreement >= float(config.MARKET_VOL_GEX_MIN_SUBFAMILY_AGREEMENT)
        and gex_short_agreement >= float(config.MARKET_VOL_GEX_MIN_SUBFAMILY_AGREEMENT)
    )

    scanner_score = _f(features.get("vol_scanner_stress_score"), _f(features.get("vix_stress_score")))
    scanner_count = _i(features.get("vol_scanner_coverage"), 0)
    scanner_long = _i(features.get("vol_scanner_long_coverage"), 0)
    scanner_short = _i(features.get("vol_scanner_short_coverage"), 0)
    scanner_agreement = _f(features.get("vol_scanner_stress_agreement" if stress_side else "vol_scanner_relax_agreement"))
    scanner_long_agreement = _f(features.get("vol_scanner_long_stress_agreement" if stress_side else "vol_scanner_long_relax_agreement"))
    scanner_short_agreement = _f(features.get("vol_scanner_short_stress_agreement" if stress_side else "vol_scanner_short_relax_agreement"))
    scanner_signed = scanner_score >= float(config.MARKET_LONG_EXIT_VIX_STRESS) if stress_side else scanner_score <= float(config.MARKET_SHORT_EXIT_VIX_STRESS)
    scanner_quality = bool(
        scanner_count >= int(config.MARKET_VOL_SCANNER_MIN_PRODUCTS)
        and scanner_long >= int(config.MARKET_VOL_SCANNER_MIN_LONG_PRODUCTS)
        and scanner_short >= int(config.MARKET_VOL_SCANNER_MIN_SHORT_PRODUCTS)
        and scanner_agreement >= float(config.MARKET_VOL_GEX_MIN_AGREEMENT)
        and scanner_long_agreement >= float(config.MARKET_VOL_GEX_MIN_SUBFAMILY_AGREEMENT)
        and scanner_short_agreement >= float(config.MARKET_VOL_GEX_MIN_SUBFAMILY_AGREEMENT)
    )

    parts: list[str] = []
    if gex_signed and gex_quality:
        parts.append(
            f"GEX {gex_score:+.0f} · {gex_count} productos ({gex_long} long-vol/{gex_short} short-vol) · "
            f"acuerdo total {gex_agreement:.0%} · subfamilias {gex_long_agreement:.0%}/{gex_short_agreement:.0%}"
        )
    if scanner_signed and scanner_quality:
        parts.append(
            f"scanner {scanner_score:+.0f} · {scanner_count} productos ({scanner_long} long-vol/{scanner_short} short-vol) · "
            f"acuerdo total {scanner_agreement:.0%} · subfamilias {scanner_long_agreement:.0%}/{scanner_short_agreement:.0%}"
        )
    if parts:
        return True, " / ".join(parts)
    return False, (
        f"sin consenso suficiente: GEX {gex_count} ({gex_long}L/{gex_short}S; total {gex_agreement:.0%}; sub {gex_long_agreement:.0%}/{gex_short_agreement:.0%}), "
        f"scanner {scanner_count} ({scanner_long}L/{scanner_short}S; total {scanner_agreement:.0%}; sub {scanner_long_agreement:.0%}/{scanner_short_agreement:.0%})"
    )


def _global_market_candidate(direction: str, features: dict) -> dict | None:
    """Build a broad alert only from sufficiently independent, qualified layers."""
    if not bool(getattr(config, "MARKET_CLOSE_ALL_SIGNAL_ENABLED", True)):
        return None
    direction = str(direction or "long").lower()
    quality = _market_core_data_quality(features, direction)
    if not quality["ready"]:
        return None
    core_probability = _f(features.get(
        "market_core_risk_off_probability" if direction == "long" else "market_core_risk_on_probability"
    ))
    if core_probability < float(config.MARKET_CLOSE_ALL_PROBABILITY):
        return None

    market = _f(features.get("market_context_score"))
    breadth = _f(features.get("market_breadth_score"))
    credit = _f(features.get("credit_risk_on_score"))
    deterioration = _f(features.get("market_deterioration_score"))
    regime = str(features.get("market_regime") or "")
    main_gex_score = _f(features.get("main_etf_gex_risk_on_score"))
    vol_gex_score = _f(features.get("vol_gex_stress_score"))

    families: dict[str, str] = {}
    main_ok, main_detail = _main_gex_family(direction, features)
    if main_ok:
        families["MAIN_ETF_GEX"] = main_detail
    vol_ok, vol_detail = _vol_family(direction, features)
    if vol_ok:
        families["VOL_COMPLEX"] = vol_detail

    if direction == "long":
        market_breadth_ok = bool(
            (market <= float(config.MARKET_LONG_EXIT_MARKET_SCORE) and breadth <= float(config.MARKET_LONG_EXIT_BREADTH_SCORE))
            or (market <= float(config.MARKET_EXTREME_RISK_OFF_SCORE) and breadth < 0)
            or (breadth <= -55.0 and market < 0)
        )
        if market_breadth_ok:
            families["MARKET_BREADTH"] = f"main ETFs {market:+.0f}; breadth {breadth:+.0f}"
        if credit <= float(config.MARKET_LONG_EXIT_CREDIT_SCORE):
            families["CREDIT"] = f"crédito {credit:+.0f}"
        extreme = core_probability >= 88.0 and main_ok and vol_ok
        thesis = "riesgo anticipado de movimiento bajista general"
    else:
        market_breadth_ok = bool(
            (market >= float(config.MARKET_SHORT_EXIT_MARKET_SCORE) and breadth >= float(config.MARKET_SHORT_EXIT_BREADTH_SCORE))
            or (market >= float(config.MARKET_EXTREME_RISK_ON_SCORE) and breadth > 0)
            or (breadth >= 55.0 and market > 0)
        )
        if market_breadth_ok:
            families["MARKET_BREADTH"] = f"main ETFs {market:+.0f}; breadth {breadth:+.0f}"
        if credit >= float(config.MARKET_SHORT_EXIT_CREDIT_SCORE):
            families["CREDIT"] = f"crédito {credit:+.0f}"
        extreme = core_probability >= 88.0 and main_ok and vol_ok
        thesis = "riesgo anticipado de squeeze alcista general"

    required = max(1, int(config.MARKET_CLOSE_ALL_MIN_CONFIRMATION_FAMILIES))
    vol_gex_ok = _vol_gex_family_qualified(direction, features)
    # Scanner-only volatility consensus may confirm context, but the broad signal
    # must still contain at least one genuine GEX family.
    has_gex_family = bool(main_ok or vol_gex_ok)
    if bool(config.MARKET_CLOSE_ALL_REQUIRE_GEX_FAMILY) and not has_gex_family:
        return None
    if len(families) < required and not extreme:
        return None

    core_agreement = _f(features.get(
        "market_core_risk_off_agreement" if direction == "long" else "market_core_risk_on_agreement"
    ))
    main_directional_agreement = _f(features.get(
        "main_etf_gex_risk_off_agreement" if direction == "long" else "main_etf_gex_risk_on_agreement"
    )) if main_ok else 0.0
    vol_directional_agreement = _f(features.get(
        "vol_gex_stress_agreement" if direction == "long" else "vol_gex_relax_agreement"
    )) if vol_ok else 0.0
    gex_agreement = max(main_directional_agreement, vol_directional_agreement)
    # Conviction can only be increased by GEX aligned with this warning. A
    # strong *opposite* GEX family remains visible as divergence but cannot
    # inflate the score through abs(...).
    aligned_main_gex = max(0.0, -main_gex_score) if direction == "long" else max(0.0, main_gex_score)
    aligned_vol_gex = max(0.0, vol_gex_score) if direction == "long" else max(0.0, -vol_gex_score)
    score = _clamp(
        core_probability * 0.62
        + min(100.0, aligned_main_gex) * 0.12
        + min(100.0, aligned_vol_gex) * 0.08
        + core_agreement * 100.0 * 0.10
        + gex_agreement * 100.0 * 0.05
        + min(100.0, len(families) * 20.0) * 0.03,
        0.0,
        100.0,
    )
    return {
        "direction": direction,
        "action": "FULL_CLOSE_LONGS" if direction == "long" else "FULL_CLOSE_SHORTS",
        "signal_kind": "ANTICIPATORY_MARKET_SIGNAL",
        "core_model_id": features.get("market_core_model_id"),
        "core_model_key": features.get("market_core_model_key"),
        "core_model_status": features.get("market_core_model_status"),
        "core_probability": round(core_probability, 2),
        "score": round(score, 2),
        "families": families,
        "blocks": list(families.values()),
        "thesis": thesis,
        "extreme": bool(extreme),
        "regime": regime,
        "regime_context": f"{regime or 'n/a'} · deterioro {deterioration:.0f}",
        "quality": {
            "core_coverage": quality["coverage"],
            "core_weight_coverage": round(quality["weight_coverage"], 4),
            "core_agreement": round(core_agreement, 4),
            "main_gex_qualified": main_ok,
            "vol_qualified": vol_ok,
            "vol_gex_qualified": vol_gex_ok,
            "actual_gex_family": has_gex_family,
            "gex_agreement": round(gex_agreement, 4),
        },
    }


def _release_assessment(direction: str, features: dict) -> str:
    quality = _market_core_data_quality(features, direction)
    # Missing/incomplete data must not be interpreted as a real signal resolution.
    if (
        not quality["usable_model"]
        or quality["coverage"] < int(config.MARKET_CORE_MIN_MAIN_ETFS)
        or quality["weight_coverage"] < float(config.MARKET_CORE_MIN_WEIGHT_COVERAGE)
    ):
        return "DATA_GAP"
    # If both GEX/volatility confirmation families lost coverage, that is a data
    # gap, not proof that the warning has resolved.
    if not (_main_gex_data_ready(features) or _vol_gex_data_ready(features)):
        return "DATA_GAP"
    probability = _f(features.get(
        "market_core_risk_off_probability" if direction == "long" else "market_core_risk_on_probability"
    ))
    agreement = _f(features.get(
        "market_core_risk_off_agreement" if direction == "long" else "market_core_risk_on_agreement"
    ))
    main_ok, _detail = _main_gex_family(direction, features)
    vol_gex_ok = _vol_gex_family_qualified(direction, features)
    if (
        probability >= float(config.MARKET_CLOSE_ALL_RELEASE_PROBABILITY)
        and agreement >= float(config.MARKET_CLOSE_ALL_RELEASE_MIN_DIRECTIONAL_AGREEMENT)
        and (main_ok or vol_gex_ok)
    ):
        return "SUPPORTED"
    return "RELEASE"


def _signal_state(direction: str, run_id: str, candidate: dict | None, release_assessment: str) -> dict:
    side = "long" if direction == "long" else "short"
    prefix = _signal_state_prefix(side)
    effective_run_id = str(run_id or "unknown")
    previous_run = str(get_runtime_state(prefix + "_last_run", "") or "")
    # A persistence gate must advance only on distinct data runs. If the caller
    # cannot provide a run id, repeated evaluations remain the same unknown run
    # rather than manufacturing confirmation.
    if previous_run == effective_run_id:
        try:
            return json.loads(str(get_runtime_state(prefix + "_last_state_json", "{}") or "{}"))
        except Exception:
            pass

    was_active = bool(_i(get_runtime_state(prefix + "_active", 0), 0))
    candidate_streak = _i(get_runtime_state(prefix + "_candidate_streak", 0), 0)
    release_streak = _i(get_runtime_state(prefix + "_release_streak", 0), 0)
    previous_score = _f(get_runtime_state(prefix + "_last_score", 0.0), 0.0)
    previous_families = str(get_runtime_state(prefix + "_last_families", "") or "")
    previous_model_key = str(get_runtime_state(prefix + "_last_model_key", "") or "")
    current_model_key = str((candidate or {}).get("core_model_key") or "")
    model_changed = bool(current_model_key and previous_model_key and current_model_key != previous_model_key)
    activation_key = str(get_runtime_state(prefix + "_activation_key", "") or "")
    activation_streak = _i(get_runtime_state(prefix + "_activation_streak", 0), 0)
    required = 1 if candidate and candidate.get("extreme") else max(1, int(config.MARKET_CLOSE_ALL_CONFIRMATION_RUNS))
    release_required = max(1, int(config.MARKET_CLOSE_ALL_RELEASE_RUNS))

    transition = "OFF"
    if candidate:
        # A pending signal from one CORE lineage cannot donate its first run to a
        # newly selected model. Active warnings remain active and publish a
        # material lineage update instead of disappearing during recalibration.
        if model_changed and not was_active:
            candidate_streak = 0
        candidate_streak += 1
        release_streak = 0
        active = bool(was_active or candidate_streak >= required)
        if active and not was_active:
            transition = "ACTIVATED"
            activation_key = f"{side}:{effective_run_id}:{ts_madrid_str()}"
            activation_streak = candidate_streak
        else:
            transition = "ACTIVE" if active else "PENDING"
    elif was_active and release_assessment == "DATA_GAP":
        active = True
        candidate_streak = 0
        transition = "DATA_GAP"
    elif was_active and release_assessment == "SUPPORTED":
        active = True
        candidate_streak = 0
        release_streak = 0
        transition = "HYSTERESIS"
    elif was_active:
        active = True
        candidate_streak = 0
        release_streak += 1
        if release_streak >= release_required:
            active = False
            transition = "CLEARED"
        else:
            transition = "RELEASING"
    else:
        active = False
        candidate_streak = 0
        release_streak = 0

    score = _f((candidate or {}).get("score"), previous_score)
    families_signature = "|".join(sorted((candidate or {}).get("families") or {}))
    material_update = bool(
        active
        and was_active
        and candidate
        and (
            abs(score - previous_score) >= float(config.MARKET_CLOSE_ALL_UPDATE_SCORE_STEP)
            or families_signature != previous_families
            or model_changed
        )
    )
    state = {
        "direction": direction,
        "active": active,
        "was_active": was_active,
        "candidate": bool(candidate),
        "candidate_streak": candidate_streak,
        "streak": activation_streak if active and activation_streak else candidate_streak,
        "required_streak": required,
        "release_streak": release_streak,
        "release_required": release_required,
        "score": score if (candidate or was_active) else None,
        "action": "FULL_CLOSE_LONGS" if direction == "long" else "FULL_CLOSE_SHORTS",
        "transition": transition,
        "material_update": material_update,
        "model_changed": model_changed,
        "previous_model_key": previous_model_key or None,
        "current_model_key": current_model_key or previous_model_key or None,
        "activation_key": activation_key,
        "release_assessment": release_assessment,
        "run_id": effective_run_id,
    }
    set_runtime_state(prefix + "_last_run", effective_run_id)
    set_runtime_state(prefix + "_active", 1 if active else 0)
    set_runtime_state(prefix + "_candidate_streak", candidate_streak)
    set_runtime_state(prefix + "_release_streak", release_streak)
    set_runtime_state(prefix + "_last_score", score)
    set_runtime_state(prefix + "_activation_key", activation_key)
    set_runtime_state(prefix + "_activation_streak", activation_streak)
    if candidate:
        set_runtime_state(prefix + "_last_families", families_signature)
        if current_model_key:
            set_runtime_state(prefix + "_last_model_key", current_model_key)
        set_runtime_state(prefix + "_last_candidate_json", json.dumps(candidate, ensure_ascii=False))
    set_runtime_state(prefix + "_last_state_json", json.dumps(state, ensure_ascii=False))
    return state


def evaluate_market_close_all_signals(features_by_ticker: dict[str, dict], run: dict | None = None) -> dict:
    """Evaluate broad signals independently from the portfolio, with hysteresis."""
    common = next((row for row in features_by_ticker.values() if row), {})
    run_id = str((run or {}).get("id") or common.get("source_scanner_run_id") or common.get("scanner_run_id") or "")
    decisions: dict[str, dict] = {}
    states: dict[str, dict] = {}
    for direction in ("long", "short"):
        candidate = _global_market_candidate(direction, common)
        release_assessment = _release_assessment(direction, common)
        state = _signal_state(direction, run_id, candidate, release_assessment)
        states[direction] = state
        decision: dict | None = None
        if state.get("active"):
            if candidate:
                decision = dict(candidate)
            else:
                try:
                    decision = json.loads(str(get_runtime_state(_signal_state_prefix(direction) + "_last_candidate_json", "{}") or "{}"))
                except Exception:
                    decision = {}
        elif state.get("transition") == "CLEARED":
            try:
                decision = json.loads(str(get_runtime_state(_signal_state_prefix(direction) + "_last_candidate_json", "{}") or "{}"))
            except Exception:
                decision = {}
        if decision is not None and (state.get("active") or state.get("transition") == "CLEARED"):
            decisions[direction] = {
                **decision,
                "direction": direction,
                "action": state.get("action"),
                "streak": state.get("streak"),
                "required_streak": state.get("required_streak"),
                "release_streak": state.get("release_streak"),
                "release_required": state.get("release_required"),
                "transition": state.get("transition"),
                "material_update": state.get("material_update"),
                "model_changed": state.get("model_changed"),
                "previous_model_key": state.get("previous_model_key"),
                "current_model_key": state.get("current_model_key"),
                "activation_key": state.get("activation_key"),
                "run_id": run_id,
                "snapshot_date": (run or {}).get("snapshot_date"),
                "market_snapshot": common,
            }

    common_flags = {
        "market_close_all_longs_active": bool(states["long"].get("active")),
        "market_close_all_shorts_active": bool(states["short"].get("active")),
        "market_close_all_longs_score": states["long"].get("score"),
        "market_close_all_shorts_score": states["short"].get("score"),
        "market_close_all_longs_streak": states["long"].get("streak"),
        "market_close_all_shorts_streak": states["short"].get("streak"),
        "market_close_all_longs_transition": states["long"].get("transition"),
        "market_close_all_shorts_transition": states["short"].get("transition"),
    }
    for row in features_by_ticker.values():
        row.update(common_flags)
    return {"decisions": decisions, "states": states}


# ---------------------------------------------------------------------------
# Local ticker GEX gate
# ---------------------------------------------------------------------------


def ticker_gex_confirmation_state(direction: str, features: dict | None, entry_features: dict | None = None) -> dict:
    """Return whether the position ticker's own GEX has confirmed reversal.

    Evidence is grouped into independent families. Correlated fields such as
    profile bias and accumulation, or front and total GEX changes, cannot count
    twice by themselves. A decisive entry-to-current composite sign crossing or
    a broken structural wall remains hard reversal evidence.
    """
    data = features or {}
    entry = entry_features or (data.get("_entry_features") if isinstance(data.get("_entry_features"), dict) else {}) or {}
    if not _has_gex(data):
        return {
            "state": "NO_GEX",
            "confirmed": False,
            "score": 0.0,
            "blocks": ["GEX ticker no disponible"],
            "favorable_blocks": [],
            "block_families": [],
            "favorable_families": [],
            "hard": False,
            "required_blocks": max(1, int(config.MARKET_TICKER_GEX_CONFIRM_BLOCKS)),
            "entry_composite_score": None,
            "current_composite_score": None,
        }

    direction = str(direction or "long").lower()
    expiry_raw = data.get("front_gex_change_pct")
    if expiry_raw in (None, ""):
        expiry_raw = data.get("short_gex_change_pct")
    expiry = None if expiry_raw in (None, "") else _f(expiry_raw)
    total = None if data.get("total_gex_change_pct") in (None, "") else _f(data.get("total_gex_change_pct"))
    call_acc = _f(data.get("call_accumulation_score"))
    put_acc = _f(data.get("put_accumulation_score"))
    call_z = _f(data.get("otm_call_volume_z"))
    put_z = _f(data.get("otm_put_volume_z"))
    bias = " ".join(str(data.get(key) or "") for key in ("profile_bias", "gex_intent", "active_gex_intent")).lower()
    reversal_gate = max(5.0, float(config.DEFAULT_EXIT_GEX_REVERSAL_PCT) / 2.0)
    bias_gap = float(config.MARKET_TICKER_GEX_BIAS_GAP)
    composite_cross = float(config.MARKET_TICKER_GEX_COMPOSITE_CROSS_SCORE)

    adverse_families: dict[str, list[str]] = {}
    favorable_families: dict[str, list[str]] = {}
    hard_families: set[str] = set()

    def add(target: dict[str, list[str]], family: str, detail: str, *, hard: bool = False) -> None:
        target.setdefault(family, []).append(detail)
        if hard:
            hard_families.add(family)

    current_composite = _gex_directional_score(data)
    entry_composite = _gex_directional_score(entry) if entry else None
    if current_composite is not None and entry_composite is not None:
        if direction == "long":
            if entry_composite >= composite_cross and current_composite <= -composite_cross:
                add(
                    adverse_families,
                    "COMPOSITE_CROSS",
                    f"GEX compuesto revierte {entry_composite:+.0f}→{current_composite:+.0f}",
                    hard=True,
                )
            elif entry_composite > 0 and current_composite >= max(composite_cross, entry_composite - 10.0):
                add(favorable_families, "COMPOSITE", f"GEX compuesto mantiene {entry_composite:+.0f}→{current_composite:+.0f}")
        else:
            if entry_composite <= -composite_cross and current_composite >= composite_cross:
                add(
                    adverse_families,
                    "COMPOSITE_CROSS",
                    f"GEX compuesto revierte {entry_composite:+.0f}→{current_composite:+.0f}",
                    hard=True,
                )
            elif entry_composite < 0 and current_composite <= min(-composite_cross, entry_composite + 10.0):
                add(favorable_families, "COMPOSITE", f"GEX compuesto mantiene {entry_composite:+.0f}→{current_composite:+.0f}")

    if direction == "long":
        if expiry is not None and expiry <= -reversal_gate:
            add(adverse_families, "GEX_CHANGE", f"GEX expiry {expiry:+.1f}%")
        elif expiry is not None and expiry >= reversal_gate:
            add(favorable_families, "GEX_CHANGE", f"GEX expiry {expiry:+.1f}%")
        if total is not None and total <= -reversal_gate:
            add(adverse_families, "GEX_CHANGE", f"GEX total {total:+.1f}%")
        elif total is not None and total >= reversal_gate:
            add(favorable_families, "GEX_CHANGE", f"GEX total {total:+.1f}%")
        if put_acc - call_acc >= bias_gap:
            add(adverse_families, "POSITIONING", f"put accumulation {put_acc:.0f}>{call_acc:.0f}")
        elif call_acc - put_acc >= bias_gap:
            add(favorable_families, "POSITIONING", f"call accumulation {call_acc:.0f}>{put_acc:.0f}")
        if put_z - call_z >= 1.0:
            add(adverse_families, "FLOW", f"put flow z {put_z:+.1f}")
        elif call_z - put_z >= 1.0:
            add(favorable_families, "FLOW", f"call flow z {call_z:+.1f}")
        if any(token in bias for token in ("put", "bear", "negative", "short")):
            add(adverse_families, "POSITIONING", f"bias {bias[:80]}")
        elif any(token in bias for token in ("call", "bull", "positive", "long")):
            add(favorable_families, "POSITIONING", f"bias {bias[:80]}")
        if data.get("put_wall_pct") not in (None, "") and _f(data.get("put_wall_pct")) > float(config.DEFAULT_BREAK_WALL_PCT):
            add(adverse_families, "WALL", f"pierde put wall {_f(data.get('put_wall_pct')):+.1f}%", hard=True)
    else:
        if expiry is not None and expiry >= reversal_gate:
            add(adverse_families, "GEX_CHANGE", f"GEX expiry {expiry:+.1f}%")
        elif expiry is not None and expiry <= -reversal_gate:
            add(favorable_families, "GEX_CHANGE", f"GEX expiry {expiry:+.1f}%")
        if total is not None and total >= reversal_gate:
            add(adverse_families, "GEX_CHANGE", f"GEX total {total:+.1f}%")
        elif total is not None and total <= -reversal_gate:
            add(favorable_families, "GEX_CHANGE", f"GEX total {total:+.1f}%")
        if call_acc - put_acc >= bias_gap:
            add(adverse_families, "POSITIONING", f"call accumulation {call_acc:.0f}>{put_acc:.0f}")
        elif put_acc - call_acc >= bias_gap:
            add(favorable_families, "POSITIONING", f"put accumulation {put_acc:.0f}>{call_acc:.0f}")
        if call_z - put_z >= 1.0:
            add(adverse_families, "FLOW", f"call flow z {call_z:+.1f}")
        elif put_z - call_z >= 1.0:
            add(favorable_families, "FLOW", f"put flow z {put_z:+.1f}")
        if any(token in bias for token in ("call", "bull", "positive", "long")):
            add(adverse_families, "POSITIONING", f"bias {bias[:80]}")
        elif any(token in bias for token in ("put", "bear", "negative", "short")):
            add(favorable_families, "POSITIONING", f"bias {bias[:80]}")
        if data.get("call_wall_pct") not in (None, "") and _f(data.get("call_wall_pct")) < -float(config.DEFAULT_BREAK_WALL_PCT):
            add(adverse_families, "WALL", f"supera call wall {_f(data.get('call_wall_pct')):+.1f}%", hard=True)

    def flatten(families: dict[str, list[str]]) -> list[str]:
        return list(dict.fromkeys(detail for items in families.values() for detail in items))

    adverse = flatten(adverse_families)
    favorable = flatten(favorable_families)
    required = max(1, int(config.MARKET_TICKER_GEX_CONFIRM_BLOCKS))
    hard = bool(hard_families)
    confirmed = bool(hard or len(adverse_families) >= required)
    if confirmed:
        state = "REVERSED"
    elif len(favorable_families) >= required:
        state = "FAVORABLE"
    elif adverse:
        state = "DETERIORATING"
    else:
        state = "UNCHANGED"
    score = min(100.0, len(adverse_families) * 28.0 + (44.0 if hard else 0.0))
    return {
        "state": state,
        "confirmed": confirmed,
        "score": round(score, 2),
        "blocks": adverse,
        "favorable_blocks": favorable,
        "block_families": sorted(adverse_families),
        "favorable_families": sorted(favorable_families),
        "hard": hard,
        "hard_families": sorted(hard_families),
        "required_blocks": required,
        "entry_composite_score": round(entry_composite, 2) if entry_composite is not None else None,
        "current_composite_score": round(current_composite, 2) if current_composite is not None else None,
    }

def broad_signal_active_for_direction(direction: str, features: dict | None) -> bool:
    data = features or {}
    return bool(
        data.get("market_close_all_longs_active")
        if str(direction or "long").lower() == "long"
        else data.get("market_close_all_shorts_active")
    )


def broad_signal_local_exit_gate(direction: str, features: dict | None) -> dict:
    """Gate an individual exit only while the matching broad signal is active."""
    active = broad_signal_active_for_direction(direction, features)
    local = ticker_gex_confirmation_state(direction, features)
    required = bool(getattr(config, "MARKET_CLOSE_ALL_REQUIRE_LOCAL_GEX_FOR_EXIT", True))
    allowed = bool(not (active and required) or local.get("confirmed"))
    return {
        "broad_active": active,
        "local_gex_required": required,
        "local_gex_state": local,
        "exit_allowed": allowed,
    }


# ---------------------------------------------------------------------------
# Discord rendering and delivery
# ---------------------------------------------------------------------------


def _trade_direction(trade: dict) -> str:
    raw = str(trade.get("direction") or trade.get("side") or "long").lower()
    if raw in {"short", "put", "puts", "bear", "bearish"}:
        return "short"
    return "long"


def _trade_entry_features(trade: dict) -> dict:
    direct = trade.get("entry_features")
    if isinstance(direct, dict):
        return direct
    raw = trade.get("entry_features_json")
    if isinstance(raw, dict):
        return raw
    if raw not in (None, ""):
        try:
            parsed = json.loads(str(raw))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _position_state_signature(direction: str, open_trades: list[dict], features_by_ticker: dict[str, dict]) -> str:
    """Stable signature used to publish a broad-signal update only when local GEX changes."""
    parts: list[str] = []
    for trade in open_trades:
        if _trade_direction(trade) != direction:
            continue
        ticker = str(trade.get("ticker") or "").upper()
        if not ticker:
            continue
        state = ticker_gex_confirmation_state(
            direction, features_by_ticker.get(ticker) or {}, _trade_entry_features(trade)
        )
        # Numeric GEX values move every run. Update the broad Discord message only
        # when the actionable local classification changes, not on harmless jitter.
        parts.append(
            f"{ticker}:{state.get('state') or 'NO_GEX'}:"
            f"{1 if state.get('confirmed') else 0}:{1 if state.get('hard') else 0}"
        )
    return "|".join(sorted(parts)) or "NO_OPEN_POSITIONS"


def _position_context_rows(direction: str, open_trades: list[dict], features_by_ticker: dict[str, dict]) -> tuple[list[str], int, int, int]:
    rows: list[str] = []
    reversed_count = favorable_count = pending_count = 0
    for trade in open_trades:
        if _trade_direction(trade) != direction:
            continue
        ticker = str(trade.get("ticker") or "").upper()
        state = ticker_gex_confirmation_state(
            direction, features_by_ticker.get(ticker) or {}, _trade_entry_features(trade)
        )
        label = str(state.get("state") or "NO_GEX")
        if label == "REVERSED":
            reversed_count += 1
            icon = "🔴"
        elif label == "FAVORABLE":
            favorable_count += 1
            icon = "🟢"
        else:
            pending_count += 1
            icon = "🟡" if label == "DETERIORATING" else "⚪"
        blocks = "; ".join(state.get("blocks") or state.get("favorable_blocks") or []) or "sin confirmación GEX suficiente"
        rows.append(f"• {icon} **${ticker}** · GEX ticker `{label}` · {blocks}")
    return rows, reversed_count, favorable_count, pending_count


def _fmt(value: Any, decimals: int = 0, *, signed: bool = True) -> str:
    try:
        sign = "+" if signed else ""
        return f"{float(value):{sign}.{decimals}f}"
    except Exception:
        return "n/a"


def _pct(value: Any, decimals: int = 1, *, signed: bool = False) -> str:
    formatted = _fmt(value, decimals, signed=signed)
    return formatted if formatted == "n/a" else formatted + "%"


def _calibration_for_direction(snapshot: dict, direction: str) -> dict:
    return dict(((snapshot.get("market_close_all_calibration") or {}).get(direction) or {}))


def _message_title(direction: str, transition: str, shadow: bool) -> str:
    side = "LONGS" if direction == "long" else "SHORTS"
    icon = "🔴" if direction == "long" else "🟢"
    label = {
        "ACTIVATED": "SEÑAL ANTICIPATORIA ACTIVADA",
        "ACTIVE": "ACTUALIZACIÓN DE SEÑAL",
        "CLEARED": "SEÑAL ANTICIPATORIA DESACTIVADA",
        "DATA_GAP": "SEÑAL ACTIVA · DATOS PARCIALES",
    }.get(transition, "SEÑAL ANTICIPATORIA")
    return f"{icon} CLOSE ALL {side} · {label} · {'SHADOW' if shadow else 'LIVE'}"


def build_market_close_all_message(
    decision: dict,
    *,
    open_trades: list[dict] | None = None,
    features_by_ticker: dict[str, dict] | None = None,
    shadow: bool,
) -> str:
    from discord_presentation import build_group

    snapshot = decision.get("market_snapshot") or {}
    direction = str(decision.get("direction") or "long").lower()
    transition = str(decision.get("transition") or "ACTIVATED").upper()
    calibration = _calibration_for_direction(snapshot, direction)
    model_id = snapshot.get("market_core_model_id")
    model_status = snapshot.get("market_core_model_status") or "N/A"
    main_core = snapshot.get("market_core_main_etfs") or {}
    main_gex = snapshot.get("main_etf_gex") or {}
    vol = snapshot.get("vol_complex_gex") or {}

    core_lines: list[str] = []
    for ticker in MAIN_ETF_WEIGHTS:
        row = main_core.get(ticker) or {}
        usable = "✅" if row.get("usable") else "⚪"
        core_lines.append(
            f"• {usable} **{ticker}:** `{str(row.get('direction') or 'n/a').upper()}` · "
            f"P `{_fmt(row.get('probability_pct'),1,signed=False)}%` · fuente `{row.get('probability_source') or 'n/a'}` · n `{_i(row.get('probability_sample'))}`"
        )
    gex_lines: list[str] = []
    for ticker in MAIN_ETF_WEIGHTS:
        row = main_gex.get(ticker) or {}
        gex_lines.append(
            f"• **{ticker}:** risk-on `{_fmt(row.get('risk_on_score'),0)}` · bias `{row.get('bias') or 'n/a'}` · "
            f"call/put acc `{_fmt(row.get('call_acc'),0,signed=False)}/{_fmt(row.get('put_acc'),0,signed=False)}` · 0–7d `{_fmt(row.get('short_change'),0)}`"
        )
    vol_lines: list[str] = []
    for ticker in list(LONG_VOL_WEIGHTS) + list(SHORT_VOL_WEIGHTS):
        row = vol.get(ticker) or {}
        if not row.get("available"):
            continue
        vol_lines.append(
            f"• **{ticker}:** `{row.get('family')}` · stress GEX `{_fmt(row.get('stress_score'),0)}` · "
            f"bias `{row.get('bias') or 'n/a'}` · 0–7d `{_fmt(row.get('short_change'),0)}`"
        )
    position_rows, reversed_count, favorable_count, pending_count = _position_context_rows(
        direction, list(open_trades or []), features_by_ticker or {}
    )
    model_change_line = ""
    if decision.get("model_changed"):
        model_change_line = (
            f"**Cambio de modelo:** `{decision.get('previous_model_key') or 'n/a'}` → "
            f"`{decision.get('current_model_key') or snapshot.get('market_core_model_key') or 'n/a'}`; "
            "la señal continúa, pero la validación LIVE se reevalúa con el modelo nuevo."
        )

    if transition == "CLEARED":
        cards = [
            "\n".join([
                f"**Señal:** `{decision.get('action') or 'n/a'}` · **Estado:** `DESACTIVADA`",
                f"**Liberación:** `{decision.get('release_streak')}/{decision.get('release_required')}` runs",
                "La presión general ha dejado de cumplir los umbrales de persistencia. Esto no constituye una entrada contraria.",
                "Las posiciones que siguieran abiertas mantienen su lifecycle individual CORE/GEX.",
            ]),
            "\n".join([
                "### 📊 CALIBRACIÓN PROSPECTIVA",
                f"• Estado: `{calibration.get('status') or 'ACCUMULATING'}` · muestra `{_i(calibration.get('sample'))}` · abiertas `{_i(calibration.get('open_events'))}` · hit `{_pct(calibration.get('hit_rate_pct'))}`",
                f"• Confianza Wilson 90% inferior `{_pct(calibration.get('wilson_lower_90_pct'))}` · ratio MFE/MAE `{_fmt(calibration.get('mfe_mae_ratio'),2,signed=False)}`",
                f"• Alcance: estrategia `{calibration.get('strategy_version') or getattr(config, 'MARKET_CLOSE_ALL_STRATEGY_VERSION', 'n/a')}` · modelo `{calibration.get('model_key') or snapshot.get('market_core_model_key') or 'n/a'}`",
                f"• Objetivo de validación: movimiento agregado `{_pct(calibration.get('target_move_pct'))}` en `{_i(calibration.get('horizon_sessions'),5)}` sesiones",
            ]),
        ]
        return build_group(
            _message_title(direction, transition, shadow),
            cards,
            footer="Desactivar CLOSE ALL no abre posiciones automáticamente; solo elimina el aviso general paralelo.",
        )

    cards = [
        "\n".join([
            f"**Modo:** `{'SHADOW' if shadow else 'LIVE'}` · **Señal:** `{decision.get('action') or 'n/a'}` · **Transición:** `{transition}`",
            f"**Persistencia:** `{decision.get('streak')}/{decision.get('required_streak')}` · **Score:** `{_fmt(decision.get('score'),1,signed=False)}`",
            f"**Tesis:** {decision.get('thesis') or 'cambio general anticipado'}",
            "**Naturaleza:** alerta paralela que intenta anticipar una futura reversión del GEX de los tickers.",
            "**Ejecución:** NO cierra ninguna posición. Cada ticker espera confirmación de su propio GEX.",
            "**±5% y tocar el objetivo GEX no son salidas:** son hit y milestone; nunca disparan CLOSE.",
            *([model_change_line] if model_change_line else []),
        ]),
        "\n".join([
            "### 🧠 MARKET CORE · CALIDAD",
            f"• Modelo: `CORE #{model_id or 'n/a'} {model_status}`",
            f"• P(risk-off): `{_fmt(snapshot.get('market_core_risk_off_probability'),1,signed=False)}%` · P(risk-on): `{_fmt(snapshot.get('market_core_risk_on_probability'),1,signed=False)}%`",
            f"• Cobertura: `{_i(snapshot.get('market_core_coverage'))}/4` · peso `{_f(snapshot.get('market_core_weight_coverage')):.0%}`",
            f"• Acuerdo direccional: risk-off `{_f(snapshot.get('market_core_risk_off_agreement')):.0%}` · risk-on `{_f(snapshot.get('market_core_risk_on_agreement')):.0%}`",
            *core_lines,
        ]),
        "\n".join([
            "### 🧲 GEX · MAIN ETFs",
            f"• Agregado risk-on: `{_fmt(snapshot.get('main_etf_gex_risk_on_score'),1)}`",
            f"• Cobertura: `{_i(snapshot.get('main_etf_gex_coverage'))}/4` · peso `{_f(snapshot.get('main_etf_gex_weight_coverage')):.0%}`",
            f"• Acuerdo: risk-off `{_f(snapshot.get('main_etf_gex_risk_off_agreement')):.0%}` · risk-on `{_f(snapshot.get('main_etf_gex_risk_on_agreement')):.0%}`",
            *gex_lines,
        ]),
        "\n".join([
            "### 🌡️ COMPLEJO VIX / VOLATILIDAD",
            f"• GEX stress agregado: `{_fmt(snapshot.get('vol_gex_stress_score'),1)}` · acuerdo stress `{_f(snapshot.get('vol_gex_stress_agreement')):.0%}` · relax `{_f(snapshot.get('vol_gex_relax_agreement')):.0%}`",
            f"• Cobertura GEX: `{_i(snapshot.get('vol_gex_coverage'))}` productos · long-vol `{_i(snapshot.get('vol_gex_long_coverage'))}` · short-vol `{_i(snapshot.get('vol_gex_short_coverage'))}`",
            f"• Acuerdo subfamilias GEX: stress `{_f(snapshot.get('vol_gex_long_stress_agreement')):.0%}/{_f(snapshot.get('vol_gex_short_stress_agreement')):.0%}` · relax `{_f(snapshot.get('vol_gex_long_relax_agreement')):.0%}/{_f(snapshot.get('vol_gex_short_relax_agreement')):.0%}`",
            f"• Scanner agregado: `{_fmt(snapshot.get('vol_scanner_stress_score'),1)}` · cobertura `{_i(snapshot.get('vol_scanner_coverage'))}` (`{_i(snapshot.get('vol_scanner_long_coverage'))}L/{_i(snapshot.get('vol_scanner_short_coverage'))}S`) · subfamilias stress `{_f(snapshot.get('vol_scanner_long_stress_agreement')):.0%}/{_f(snapshot.get('vol_scanner_short_stress_agreement')):.0%}`",
            "• VIX aislado nunca cuenta como familia suficiente.",
            *(vol_lines or ["• Sin GEX granular suficiente: el complejo no puede confirmar por sí solo este run."]),
        ]),
        "\n".join([
            "### 🌐 CONFIRMACIONES INDEPENDIENTES",
            f"• Régimen (solo contexto, no voto adicional): `{snapshot.get('market_regime') or 'n/a'}`",
            f"• Main ETFs scanner: `{_fmt(snapshot.get('market_context_score'),1)}` · breadth `{_fmt(snapshot.get('market_breadth_score'),1)}`",
            f"• Crédito HYG/LQD: `{_fmt(snapshot.get('credit_risk_on_score'),1)}` · deterioro `{_fmt(snapshot.get('market_deterioration_score'),1)}`",
            f"• Familias cualificadas: `{'; '.join(decision.get('blocks') or []) or 'n/a'}`",
        ]),
        "\n".join([
            "### 📌 POSICIONES ABIERTAS · GEX LOCAL",
            f"• Revertidas: `{reversed_count}` · favorables: `{favorable_count}` · pendientes/sin GEX: `{pending_count}`",
            *(position_rows or ["• No hay posiciones abiertas de este lado; la señal se registra y calibra igualmente."]),
            "• GEX favorable/estable/sin confirmar = posición abierta. Solo `REVERSED` habilita el CLOSE individual durante esta señal.",
        ]),
        "\n".join([
            "### 📊 CALIBRACIÓN PROSPECTIVA DE LA ALERTA",
            f"• Estado: `{calibration.get('status') or 'ACCUMULATING'}` · método `{calibration.get('method') or 'PROSPECTIVE_MAIN_ETF_OOS'}`",
            f"• Alcance: estrategia `{calibration.get('strategy_version') or getattr(config, 'MARKET_CLOSE_ALL_STRATEGY_VERSION', 'n/a')}` · modelo `{calibration.get('model_key') or snapshot.get('market_core_model_key') or 'n/a'}`",
            f"• Muestra madura: `{_i(calibration.get('sample'))}` · abiertas pendientes: `{_i(calibration.get('open_events'))}` · hit `{_pct(calibration.get('hit_rate_pct'))}`",
            f"• MFE agregado medio: `{_pct(calibration.get('avg_max_favorable_pct'),2,signed=True)}` · MAE medio: `{_pct(calibration.get('avg_max_adverse_pct'),2)}` · ratio MFE/MAE `{_fmt(calibration.get('mfe_mae_ratio'),2,signed=False)}`",
            f"• Anticipación: primer movimiento objetivo en `{_fmt(calibration.get('avg_first_hit_session'),2,signed=False)}` sesiones de media entre los hits.",
            f"• Confianza: Wilson 90% inferior `{_pct(calibration.get('wilson_lower_90_pct'))}` · mínimo `{_pct(calibration.get('minimum_wilson_lower_90_pct'))}`",
            f"• Criterio: movimiento agregado `{_pct(calibration.get('target_move_pct'))}` en `{_i(calibration.get('horizon_sessions'),5)}` sesiones; LIVE exige `{_i(calibration.get('minimum_events'),int(config.MARKET_CLOSE_ALL_VALIDATION_MIN_EVENTS))}` eventos, hit ≥ `{_pct(calibration.get('minimum_hit_rate_pct'))}` y ratio ≥ `{_fmt(calibration.get('minimum_mfe_mae_ratio'),2,signed=False)}`.",
        ]),
    ]
    return build_group(
        _message_title(direction, transition, shadow),
        cards,
        footer="CLOSE ALL es paralelo: no liquida ni bloquea por sí solo una alerta de ticker. Los CLOSE de posiciones solo llegan tras reversión GEX local confirmada.",
    )


def maybe_send_market_close_all_signals(
    result: dict,
    *,
    open_trades: list[dict] | None = None,
    features_by_ticker: dict[str, dict] | None = None,
) -> int:
    """Send activation, update and clear cards with durable retry semantics.

    State transitions are persisted before the Discord call. A failed UPDATE or
    CLEARED delivery therefore survives the next market run, just as activation
    retries already did. Newer transitions replace stale pending updates.
    """
    from discord_utils import send_alert

    sent = 0
    decisions = dict(result.get("decisions") or {})
    states = dict(result.get("states") or {})
    entry_mode = str(get_runtime_state("entry_mode", "off") or "off").lower()
    for direction in ("long", "short"):
        current_decision = decisions.get(direction)
        current_state = states.get(direction) or {}
        pending_key = _delivery_state_key(direction, "pending_delivery_json")
        try:
            pending = json.loads(str(get_runtime_state(pending_key, "{}") or "{}"))
            if not isinstance(pending, dict):
                pending = {}
        except Exception:
            pending = {}

        decision = dict(current_decision or {})
        state = dict(current_state or {})
        kind = ""
        if decision:
            transition = str(decision.get("transition") or state.get("transition") or "").upper()
            activation_key = str(decision.get("activation_key") or state.get("activation_key") or "")
            position_signature = _position_state_signature(direction, open_trades or [], features_by_ticker or {})
            position_signature_key = _delivery_state_key(direction, "last_position_signature")
            previous_position_signature = str(get_runtime_state(position_signature_key, "") or "")
            position_material_update = bool(
                state.get("active")
                and previous_position_signature
                and position_signature != previous_position_signature
            )
            if position_material_update:
                decision["position_material_update"] = True

            if transition == "ACTIVATED":
                kind = "ACTIVATED"
                _record_market_signal_event(decision)
            elif bool(state.get("active")) and activation_key and str(get_runtime_state(_delivery_state_key(direction, "delivered_activation"), "") or "") != activation_key:
                kind = "ACTIVATED"
                decision["transition"] = "ACTIVATED"
                _record_market_signal_event(decision)
            elif bool(state.get("active")) and (bool(decision.get("material_update")) or position_material_update):
                kind = "UPDATE"
                decision["transition"] = "ACTIVE"
            elif transition == "CLEARED":
                kind = "CLEARED"

            if kind:
                pending = {
                    "kind": kind,
                    "decision": decision,
                    "state": state,
                    "position_signature": position_signature,
                }
                set_runtime_state(pending_key, json.dumps(pending, ensure_ascii=False))

        if not kind and pending:
            kind = str(pending.get("kind") or "").upper()
            decision = dict(pending.get("decision") or {})
            state = dict(pending.get("state") or {})
            position_signature = str(pending.get("position_signature") or "")

        if not kind or not decision:
            continue

        activation_key = str(decision.get("activation_key") or state.get("activation_key") or "")
        snapshot = decision.get("market_snapshot") or {}
        core_status = str(snapshot.get("market_core_model_status") or "").upper()
        calibration = _calibration_for_direction(snapshot, direction)
        signal_validated = str(calibration.get("status") or "ACCUMULATING").upper() == "VALIDATED"
        live = bool(
            entry_mode == "live"
            and (not bool(config.MARKET_CLOSE_ALL_LIVE_REQUIRE_CHAMPION) or core_status in {"CHAMPION", "SCHEDULED_CHAMPION"})
            and (not bool(config.MARKET_CLOSE_ALL_LIVE_REQUIRE_SIGNAL_VALIDATED) or signal_validated)
        )

        # Refresh every attempt so a retried card contains current calibration.
        model_key = str(snapshot.get("market_core_model_key") or snapshot.get("market_core_model_id") or "UNKNOWN")
        refreshed = market_signal_calibration_metrics(model_key=model_key).get(direction) or {}
        calibration_map = dict(snapshot.get("market_close_all_calibration") or {})
        calibration_map[direction] = refreshed
        snapshot["market_close_all_calibration"] = calibration_map
        decision["market_snapshot"] = snapshot

        signature = "|".join([
            activation_key,
            kind,
            f"{_f(decision.get('score')):.1f}",
            ",".join(sorted((decision.get("families") or {}).keys())),
            str(decision.get("current_model_key") or decision.get("core_model_key") or ""),
            str(decision.get("release_streak") or ""),
            position_signature,
        ])
        signature_key = _delivery_state_key(direction, "last_delivery_signature")
        if str(get_runtime_state(signature_key, "") or "") == signature:
            set_runtime_state(pending_key, "")
            continue

        message_enabled = bool(config.MARKET_CLOSE_ALL_MESSAGE_ENABLED)
        delivered = bool(send_alert(build_market_close_all_message(
            decision,
            open_trades=open_trades or [],
            features_by_ticker=features_by_ticker or {},
            shadow=not live,
        ))) if message_enabled else True
        if not delivered:
            continue

        set_runtime_state(signature_key, signature)
        set_runtime_state(_delivery_state_key(direction, "last_position_signature"), position_signature)
        set_runtime_state(pending_key, "")
        if kind == "ACTIVATED":
            set_runtime_state(_delivery_state_key(direction, "delivered_activation"), activation_key)
        elif kind == "CLEARED":
            set_runtime_state(_delivery_state_key(direction, "delivered_activation"), "")
        if message_enabled:
            sent += 1
    return sent


# ---------------------------------------------------------------------------
# Compatibility APIs and entry policy
# ---------------------------------------------------------------------------


def market_regime_exit_signal(direction: str, features: dict) -> dict | None:
    """A broad market warning never directly closes one trade."""
    return None


def build_portfolio_market_decisions(open_trades: list[dict], features_by_ticker: dict[str, dict]) -> dict:
    result = evaluate_market_close_all_signals(features_by_ticker)
    return {**result, "trade_exit_map": {}}


def build_portfolio_close_message(decision: dict, *, shadow: bool) -> str:
    return build_market_close_all_message(decision, shadow=shadow)


def market_entry_state(direction: str, features: dict) -> dict:
    """Return market context. CLOSE ALL itself is parallel and never blocks a ticker."""
    direction = str(direction or "long").lower()
    sign = 1.0 if direction == "long" else -1.0
    market = _f(features.get("market_context_score"))
    breadth = _f(features.get("market_breadth_score"))
    vix = _f(features.get("vol_scanner_stress_score"), _f(features.get("vix_stress_score")))
    credit = _f(features.get("credit_risk_on_score"))
    core = _f(features.get("market_core_risk_on_score"))
    main_gex = _f(features.get("main_etf_gex_risk_on_score"))
    vol_gex = _f(features.get("vol_gex_stress_score"))
    directional = sign * (
        core * 0.35
        + main_gex * 0.20
        + market * 0.20
        + breadth * 0.10
        + credit * 0.10
        - (vix + vol_gex) * 0.05
    )
    regime = str(features.get("market_regime") or "")
    close_all_active = broad_signal_active_for_direction(direction, features)
    extreme_block = (
        direction == "long" and regime == "RISK_OFF_VOLATILITY"
    ) or (
        direction == "short" and regime == "RISK_ON_TREND" and market >= 50 and breadth >= 35
    )
    if close_all_active:
        policy = "PARALLEL_CLOSE_ALL_WARNING"
    elif regime in {"RISK_OFF_VOLATILITY", "RISK_OFF_TREND"}:
        policy = "BLOCK_LONGS" if direction == "long" else "FAVOR_SHORTS"
    elif regime == "RISK_ON_TREND":
        policy = "FAVOR_LONGS" if direction == "long" else "BLOCK_SHORTS"
    elif regime == "MIXED_ROTATION":
        policy = "ROTATION_SELECTIVE"
    else:
        policy = "NORMAL"
    return {
        "directional_score": round(directional, 2),
        "adjustment": round(max(-15.0, min(15.0, directional * 0.15)), 2),
        "extreme_block": bool(extreme_block),
        "close_all_active": bool(close_all_active),
        "countertrend_override": False,
        "regime": regime,
        "policy": policy,
    }
