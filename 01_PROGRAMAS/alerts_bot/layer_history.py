from __future__ import annotations

from return_math import adverse_move_pct, directional_return_pct, favorable_move_pct

import json
import sqlite3
from typing import Any

import config
from time_utils import now_madrid, ts_madrid_str, ts_utc_str


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ALERTS_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for name, sql_type in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def ensure_layer_history_schema() -> None:
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS layer_signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at_utc TEXT NOT NULL,
            observed_at_madrid TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            source_scanner_run_id INTEGER NOT NULL,
            source_gex_run_id INTEGER,
            ticker TEXT NOT NULL,
            layer TEXT NOT NULL,
            signal_origin TEXT NOT NULL,
            direction TEXT,
            score REAL,
            state TEXT,
            actionable INTEGER NOT NULL DEFAULT 0,
            target_source TEXT,
            target_price_low REAL,
            target_price_high REAL,
            target_price_mid REAL,
            target_pct REAL,
            timing_min_sessions INTEGER,
            timing_max_sessions INTEGER,
            timing_expiration TEXT,
            model_version_id INTEGER,
            rationale TEXT,
            payload_json TEXT NOT NULL,
            unique_key TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_signal_date ON layer_signal_history(snapshot_date,layer,ticker);
        CREATE INDEX IF NOT EXISTS idx_layer_signal_run ON layer_signal_history(source_scanner_run_id,layer);
        CREATE INDEX IF NOT EXISTS idx_layer_signal_state ON layer_signal_history(layer,state,signal_origin);

        CREATE TABLE IF NOT EXISTS layer_signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer_signal_id INTEGER NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            max_horizon_sessions INTEGER NOT NULL,
            movement_5pct_hit INTEGER,
            target_zone_hit INTEGER,
            first_5pct_session INTEGER,
            first_target_session INTEGER,
            max_favorable_pct REAL,
            max_adverse_pct REAL,
            target_error_pct REAL,
            sessions_json TEXT,
            complete INTEGER NOT NULL DEFAULT 0,
            labeled_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_layer_outcome_date ON layer_signal_outcomes(signal_date,complete);

        CREATE TABLE IF NOT EXISTS layer_feature_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at_utc TEXT NOT NULL,
            observed_at_madrid TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            source_scanner_run_id INTEGER NOT NULL,
            source_gex_run_id INTEGER,
            ticker TEXT NOT NULL,
            price REAL,
            asset_type TEXT,
            sector TEXT,
            industry TEXT,
            observation_layer TEXT,
            core_direction TEXT,
            core_score REAL,
            core_probability_pct REAL,
            core_probability_source TEXT,
            core_state TEXT,
            core_directional_edge REAL,
            overextension_score REAL,
            graph_direction TEXT,
            graph_score REAL,
            graph_source TEXT,
            gex_direction TEXT,
            gex_score REAL,
            gex_intent TEXT,
            expiry_regime TEXT,
            wall_asymmetry REAL,
            gex_target_pct REAL,
            gex_target_price REAL,
            timing_state TEXT,
            fusion_state TEXT,
            fusion_score REAL,
            actionable INTEGER NOT NULL DEFAULT 0,
            market_regime TEXT,
            market_policy TEXT,
            payload_json TEXT NOT NULL,
            unique_key TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_layer_feature_date ON layer_feature_history(snapshot_date,ticker);
        CREATE INDEX IF NOT EXISTS idx_layer_feature_run ON layer_feature_history(source_scanner_run_id,ticker);
        CREATE INDEX IF NOT EXISTS idx_layer_feature_states ON layer_feature_history(core_state,gex_intent,fusion_state);

        CREATE TABLE IF NOT EXISTS layer_feature_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer_feature_id INTEGER NOT NULL UNIQUE,
            ticker TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            horizon_sessions INTEGER NOT NULL,
            long_5pct_hit INTEGER,
            short_5pct_hit INTEGER,
            first_long_5pct_session INTEGER,
            first_short_5pct_session INTEGER,
            max_up_pct REAL,
            max_down_pct REAL,
            close_return_pct REAL,
            sessions_json TEXT,
            complete INTEGER NOT NULL DEFAULT 0,
            labeled_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_feature_outcome_date ON layer_feature_outcomes(signal_date,complete);

        CREATE TABLE IF NOT EXISTS market_regime_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at_utc TEXT NOT NULL,
            observed_at_madrid TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            source_scanner_run_id INTEGER NOT NULL UNIQUE,
            market_regime TEXT,
            market_context_score REAL,
            market_breadth_score REAL,
            vix_stress_score REAL,
            credit_risk_on_score REAL,
            long_exit_pressure REAL,
            short_exit_pressure REAL,
            main_etf_scores_json TEXT,
            volatility_scores_json TEXT,
            credit_scores_json TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_market_regime_date ON market_regime_history(snapshot_date,market_regime);

        CREATE TABLE IF NOT EXISTS layer_signal_pending (
            layer_signal_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            max_horizon_sessions INTEGER NOT NULL,
            target_price_low REAL,
            target_price_high REAL,
            target_price_mid REAL,
            target_pct REAL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signal_pending_date ON layer_signal_pending(signal_date);

        CREATE TABLE IF NOT EXISTS layer_feature_pending (
            layer_feature_id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            horizon_sessions INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_feature_pending_date ON layer_feature_pending(signal_date);

        CREATE TABLE IF NOT EXISTS layer_archive_catalog (
            trade_date TEXT PRIMARY KEY,
            zip_path TEXT NOT NULL,
            archive_revision INTEGER NOT NULL DEFAULT 1,
            signal_rows INTEGER NOT NULL DEFAULT 0,
            signal_outcome_rows INTEGER NOT NULL DEFAULT 0,
            feature_rows INTEGER NOT NULL DEFAULT 0,
            feature_outcome_rows INTEGER NOT NULL DEFAULT 0,
            regime_rows INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            archived_at_utc TEXT NOT NULL
        );
        """
    )
    _add_columns(conn, "layer_signal_history", {
        "opportunity_state": "TEXT", "gex_intent": "TEXT", "expiry_regime": "TEXT",
        "wall_asymmetry": "REAL", "core_rank_score": "REAL", "core_probability_pct": "REAL",
        "graph_source": "TEXT", "market_policy": "TEXT", "archive_status": "TEXT DEFAULT 'live'",
    })
    _add_columns(conn, "layer_signal_outcomes", {
        "close_return_pct": "REAL", "resolved_reason": "TEXT", "future_path_json": "TEXT",
    })
    _add_columns(conn, "market_regime_history", {
        "main_etf_scores_json": "TEXT", "volatility_scores_json": "TEXT", "credit_scores_json": "TEXT",
        "correlation_regime": "TEXT", "entry_policy": "TEXT",
        "close_all_longs_signal": "INTEGER DEFAULT 0", "close_all_shorts_signal": "INTEGER DEFAULT 0",
        "close_all_longs_score": "REAL", "close_all_shorts_score": "REAL",
        "close_all_longs_streak": "INTEGER DEFAULT 0", "close_all_shorts_streak": "INTEGER DEFAULT 0",
    })
    conn.commit(); conn.close()


def persist_market_regime(run: dict, summary: dict) -> None:
    if not run or not summary:
        return
    ensure_layer_history_schema()
    market = float(summary.get("market_context_score") or 0.0)
    breadth = float(summary.get("market_breadth_score") or 0.0)
    vix = float(summary.get("vix_stress_score") or 0.0)
    credit = float(summary.get("credit_risk_on_score") or 0.0)
    core_risk_off = float(summary.get("market_core_risk_off_probability") or 0.0)
    core_risk_on = float(summary.get("market_core_risk_on_probability") or 0.0)
    main_gex = float(summary.get("main_etf_gex_risk_on_score") or 0.0)
    vol_gex = float(summary.get("vol_gex_stress_score") or 0.0)
    # MARKET CORE is primary. GEX of main ETFs and the volatility complex
    # amplify/confirm it; scanner breadth/credit remain independent context.
    long_pressure = (
        core_risk_off * 0.45 + max(0.0, -main_gex) * 0.20 + max(0.0, vol_gex) * 0.15
        + max(0.0, -market) * 0.08 + max(0.0, -breadth) * 0.06
        + max(0.0, vix) * 0.04 + max(0.0, -credit) * 0.02
    )
    short_pressure = (
        core_risk_on * 0.45 + max(0.0, main_gex) * 0.20 + max(0.0, -vol_gex) * 0.15
        + max(0.0, market) * 0.08 + max(0.0, breadth) * 0.06
        + max(0.0, -vix) * 0.04 + max(0.0, credit) * 0.02
    )
    entry_policy = "NORMAL"
    if long_pressure >= 65 or short_pressure >= 65:
        entry_policy = "BLOCK_ONE_SIDE"
    if max(long_pressure, short_pressure) >= 85:
        entry_policy = "RISK_OFF"
    signal_states = summary.get("market_close_all_signal_states") or {}
    long_signal = signal_states.get("long") or {}
    short_signal = signal_states.get("short") or {}
    conn = _connect()
    conn.execute(
        """INSERT INTO market_regime_history(
               observed_at_utc,observed_at_madrid,snapshot_date,source_scanner_run_id,
               market_regime,market_context_score,market_breadth_score,vix_stress_score,
               credit_risk_on_score,long_exit_pressure,short_exit_pressure,main_etf_scores_json,
               volatility_scores_json,credit_scores_json,correlation_regime,entry_policy,
               close_all_longs_signal,close_all_shorts_signal,close_all_longs_score,close_all_shorts_score,
               close_all_longs_streak,close_all_shorts_streak,payload_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_scanner_run_id) DO UPDATE SET
               observed_at_utc=excluded.observed_at_utc,observed_at_madrid=excluded.observed_at_madrid,
               snapshot_date=excluded.snapshot_date,market_regime=excluded.market_regime,
               market_context_score=excluded.market_context_score,market_breadth_score=excluded.market_breadth_score,
               vix_stress_score=excluded.vix_stress_score,credit_risk_on_score=excluded.credit_risk_on_score,
               long_exit_pressure=excluded.long_exit_pressure,short_exit_pressure=excluded.short_exit_pressure,
               main_etf_scores_json=excluded.main_etf_scores_json,
               volatility_scores_json=excluded.volatility_scores_json,
               credit_scores_json=excluded.credit_scores_json,correlation_regime=excluded.correlation_regime,
               entry_policy=excluded.entry_policy,close_all_longs_signal=excluded.close_all_longs_signal,
               close_all_shorts_signal=excluded.close_all_shorts_signal,close_all_longs_score=excluded.close_all_longs_score,
               close_all_shorts_score=excluded.close_all_shorts_score,close_all_longs_streak=excluded.close_all_longs_streak,
               close_all_shorts_streak=excluded.close_all_shorts_streak,payload_json=excluded.payload_json""",
        (
            ts_utc_str(), ts_madrid_str(), str(run.get("snapshot_date") or now_madrid().date().isoformat()),
            int(run.get("id") or 0), summary.get("market_regime"), market, breadth, vix, credit,
            round(long_pressure, 4), round(short_pressure, 4),
            json.dumps(summary.get("main_etf_scores") or {}, ensure_ascii=False),
            json.dumps(summary.get("volatility_scores") or {}, ensure_ascii=False),
            json.dumps(summary.get("credit_scores") or {}, ensure_ascii=False),
            summary.get("correlation_regime"), entry_policy,
            1 if long_signal.get("active") else 0, 1 if short_signal.get("active") else 0,
            long_signal.get("score"), short_signal.get("score"),
            int(long_signal.get("streak") or 0), int(short_signal.get("streak") or 0),
            json.dumps({**summary, "entry_policy": entry_policy}, ensure_ascii=False),
        ),
    )
    conn.commit(); conn.close()


def _signal_map(signals: list[dict]) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for signal in signals:
        ticker = str(signal.get("ticker") or "").upper()
        layer = str(signal.get("layer") or "").upper()
        if not ticker or not layer:
            continue
        key = (ticker, layer)
        if key not in out or float(signal.get("score") or 0) > float(out[key].get("score") or 0):
            out[key] = signal
    return out


def persist_layer_features(run: dict, features_by_ticker: dict[str, dict], signals: list[dict]) -> int:
    if not run or not features_by_ticker or not getattr(config, "LAYER_FEATURE_HISTORY_ENABLED", True):
        return 0
    ensure_layer_history_schema()
    best = _signal_map(signals)
    now_utc = ts_utc_str(); now_mad = ts_madrid_str()
    snapshot_date = str(run.get("snapshot_date") or now_mad[:10]); run_id = int(run.get("id") or 0)
    rows = []
    for ticker, features in features_by_ticker.items():
        ticker = str(ticker).upper()
        core = best.get((ticker, "CORE"), {})
        graph = best.get((ticker, "GRAPH"), {})
        gex = best.get((ticker, "GEX_DAILY"), {})
        timing = best.get((ticker, "GEX_INTRADAY"), {})
        fusion = best.get((ticker, "FUSION"), {})
        payload = {
            "features": features, "core": core, "graph": graph, "gex_daily": gex,
            "gex_intraday": timing, "fusion": fusion,
        }
        rows.append((
            now_utc, now_mad, snapshot_date, run_id, features.get("gex_run_id"), ticker,
            features.get("price"), features.get("asset_type"), features.get("sector"), features.get("industry"),
            features.get("observation_layer"), core.get("direction"), core.get("score"), core.get("probability_pct"),
            core.get("probability_source"), core.get("state"), core.get("directional_edge"), core.get("overextension_score"),
            graph.get("direction"), graph.get("score"), graph.get("graph_source"),
            gex.get("direction"), gex.get("score"), gex.get("gex_intent"), gex.get("expiry_regime"),
            gex.get("wall_asymmetry"), gex.get("target_pct"), gex.get("target_price"),
            timing.get("state"), fusion.get("state"), fusion.get("score"), 1 if fusion.get("actionable") else 0,
            features.get("market_regime"), fusion.get("market_policy"), json.dumps(payload, ensure_ascii=False),
            f"{run_id}|{ticker}",
        ))
    conn = _connect()
    conn.executemany(
        """INSERT INTO layer_feature_history(
               observed_at_utc,observed_at_madrid,snapshot_date,source_scanner_run_id,source_gex_run_id,
               ticker,price,asset_type,sector,industry,observation_layer,core_direction,core_score,
               core_probability_pct,core_probability_source,core_state,core_directional_edge,overextension_score,
               graph_direction,graph_score,graph_source,gex_direction,gex_score,gex_intent,expiry_regime,
               wall_asymmetry,gex_target_pct,gex_target_price,timing_state,fusion_state,fusion_score,actionable,
               market_regime,market_policy,payload_json,unique_key
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(unique_key) DO UPDATE SET
               source_gex_run_id=excluded.source_gex_run_id,price=excluded.price,core_direction=excluded.core_direction,
               core_score=excluded.core_score,core_probability_pct=excluded.core_probability_pct,
               core_probability_source=excluded.core_probability_source,core_state=excluded.core_state,
               core_directional_edge=excluded.core_directional_edge,overextension_score=excluded.overextension_score,
               graph_direction=excluded.graph_direction,graph_score=excluded.graph_score,graph_source=excluded.graph_source,
               gex_direction=excluded.gex_direction,gex_score=excluded.gex_score,gex_intent=excluded.gex_intent,
               expiry_regime=excluded.expiry_regime,wall_asymmetry=excluded.wall_asymmetry,
               gex_target_pct=excluded.gex_target_pct,gex_target_price=excluded.gex_target_price,
               timing_state=excluded.timing_state,fusion_state=excluded.fusion_state,fusion_score=excluded.fusion_score,
               actionable=excluded.actionable,market_regime=excluded.market_regime,market_policy=excluded.market_policy,
               payload_json=excluded.payload_json""",
        rows,
    )
    # Compact pending queue survives after the full feature payload moves to ZIP.
    pending = conn.execute(
        "SELECT id,ticker,snapshot_date,price FROM layer_feature_history WHERE source_scanner_run_id=?",
        (run_id,),
    ).fetchall()
    conn.executemany(
        """INSERT INTO layer_feature_pending(layer_feature_id,ticker,signal_date,entry_price,horizon_sessions,created_at_utc,updated_at_utc)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(layer_feature_id) DO UPDATE SET entry_price=excluded.entry_price,updated_at_utc=excluded.updated_at_utc""",
        [(int(r[0]),str(r[1]),str(r[2]),float(r[3] or 0.0),int(config.MAX_PATTERN_HORIZON_SESSIONS),now_utc,now_utc)
         for r in pending if float(r[3] or 0.0)>0],
    )
    conn.commit(); conn.close()
    return len(rows)


def persist_layer_signals(run: dict, signals: list[dict], model_version_id: int | None = None) -> int:
    if not run or not signals:
        return 0
    ensure_layer_history_schema()
    now_utc = ts_utc_str(); now_mad = ts_madrid_str()
    snapshot_date = str(run.get("snapshot_date") or now_mad[:10]); run_id = int(run.get("id") or 0)
    rows = []
    for signal in signals:
        ticker = str(signal.get("ticker") or "").upper(); layer = str(signal.get("layer") or "FUSION").upper()
        origin = str(signal.get("signal_origin") or layer).upper()
        if not ticker:
            continue
        unique_key = f"{run_id}|{ticker}|{layer}|{origin}|{signal.get('direction') or 'none'}"
        rows.append((
            now_utc, now_mad, snapshot_date, run_id, signal.get("source_gex_run_id"), ticker,
            layer, origin, signal.get("direction"), signal.get("score"), signal.get("state"),
            1 if signal.get("actionable") else 0, signal.get("target_source"),
            signal.get("target_zone_low"), signal.get("target_zone_high"), signal.get("target_price"),
            signal.get("target_pct"), signal.get("timing_min_sessions"), signal.get("timing_max_sessions"),
            signal.get("expiry_date"), model_version_id, signal.get("rationale"),
            json.dumps(signal, ensure_ascii=False), unique_key, signal.get("opportunity_state") or signal.get("state"),
            signal.get("gex_intent"), signal.get("expiry_regime"), signal.get("wall_asymmetry"),
            signal.get("core_rank_score") or signal.get("core_score"), signal.get("probability_pct"),
            signal.get("graph_source"), signal.get("market_policy"), "live",
        ))
    conn = _connect()
    conn.executemany(
        """INSERT INTO layer_signal_history(
               observed_at_utc,observed_at_madrid,snapshot_date,source_scanner_run_id,source_gex_run_id,
               ticker,layer,signal_origin,direction,score,state,actionable,target_source,
               target_price_low,target_price_high,target_price_mid,target_pct,timing_min_sessions,
               timing_max_sessions,timing_expiration,model_version_id,rationale,payload_json,unique_key,
               opportunity_state,gex_intent,expiry_regime,wall_asymmetry,core_rank_score,core_probability_pct,
               graph_source,market_policy,archive_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(unique_key) DO UPDATE SET
               score=excluded.score,state=excluded.state,actionable=excluded.actionable,
               target_source=excluded.target_source,target_price_low=excluded.target_price_low,
               target_price_high=excluded.target_price_high,target_price_mid=excluded.target_price_mid,
               target_pct=excluded.target_pct,timing_min_sessions=excluded.timing_min_sessions,
               timing_max_sessions=excluded.timing_max_sessions,timing_expiration=excluded.timing_expiration,
               rationale=excluded.rationale,payload_json=excluded.payload_json,
               opportunity_state=excluded.opportunity_state,gex_intent=excluded.gex_intent,
               expiry_regime=excluded.expiry_regime,wall_asymmetry=excluded.wall_asymmetry,
               core_rank_score=excluded.core_rank_score,core_probability_pct=excluded.core_probability_pct,
               graph_source=excluded.graph_source,market_policy=excluded.market_policy""",
        rows,
    )
    pending = conn.execute(
        """SELECT id,ticker,snapshot_date,direction,COALESCE(json_extract(payload_json,'$.price'),0),
                  COALESCE(timing_max_sessions,?),target_price_low,target_price_high,target_price_mid,target_pct
           FROM layer_signal_history WHERE source_scanner_run_id=? AND direction IN ('long','short')""",
        (int(config.MAX_PATTERN_HORIZON_SESSIONS),run_id),
    ).fetchall()
    conn.executemany(
        """INSERT INTO layer_signal_pending(layer_signal_id,ticker,signal_date,direction,entry_price,max_horizon_sessions,
               target_price_low,target_price_high,target_price_mid,target_pct,created_at_utc,updated_at_utc)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(layer_signal_id) DO UPDATE SET entry_price=excluded.entry_price,max_horizon_sessions=excluded.max_horizon_sessions,
               target_price_low=excluded.target_price_low,target_price_high=excluded.target_price_high,
               target_price_mid=excluded.target_price_mid,target_pct=excluded.target_pct,updated_at_utc=excluded.updated_at_utc""",
        [(int(r[0]),str(r[1]),str(r[2]),str(r[3]),float(r[4] or 0.0),int(r[5] or config.MAX_PATTERN_HORIZON_SESSIONS),
          r[6],r[7],r[8],r[9],now_utc,now_utc) for r in pending if float(r[4] or 0.0)>0],
    )
    conn.commit(); conn.close()
    return len(rows)


def latest_layer_signals(limit: int = 100) -> list[dict]:
    ensure_layer_history_schema(); conn = _connect()
    row = conn.execute("SELECT MAX(source_scanner_run_id) FROM layer_signal_history").fetchone(); run_id = int(row[0] or 0)
    rows = [dict(item) for item in conn.execute(
        "SELECT * FROM layer_signal_history WHERE source_scanner_run_id=? ORDER BY score DESC,id LIMIT ?", (run_id, int(limit))
    ).fetchall()] if run_id else []
    conn.close()
    for row in rows:
        try: row["payload"] = json.loads(row.get("payload_json") or "{}")
        except Exception: row["payload"] = {}
    return rows


def _future_prices(conn: sqlite3.Connection, ticker: str, signal_date: str, horizon: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        """SELECT trade_date,open,high,low,close FROM price_cache
           WHERE ticker=? AND trade_date>? ORDER BY trade_date LIMIT ?""", (ticker, signal_date, horizon)
    ).fetchall()]


def _entry_price(conn: sqlite3.Connection, ticker: str, signal_date: str, payload: dict, fallback: float | None = None) -> float:
    value = payload.get("price") if isinstance(payload, dict) else None
    if value in (None, 0, ""):
        row = conn.execute("SELECT close FROM price_cache WHERE ticker=? AND trade_date=?", (ticker, signal_date)).fetchone()
        value = row[0] if row else fallback
    try: return float(value or 0.0)
    except Exception: return 0.0


def materialize_layer_outcomes() -> dict:
    """Mature signal and full-universe outcomes as future sessions arrive."""
    ensure_layer_history_schema(); conn = _connect()
    signals = [dict(row) for row in conn.execute(
        """SELECT p.*,p.layer_signal_id AS id,p.signal_date AS snapshot_date,
                  p.target_price_low,p.target_price_high,p.target_price_mid,p.target_pct,
                  o.sessions_json AS existing_sessions_json,o.complete AS existing_complete
           FROM layer_signal_pending p LEFT JOIN layer_signal_outcomes o ON o.layer_signal_id=p.layer_signal_id
           WHERE o.id IS NULL OR o.complete=0 ORDER BY p.signal_date,p.layer_signal_id"""
    ).fetchall()]
    signal_labeled = signal_complete = 0
    for signal in signals:
        horizon = int(signal.get("timing_max_sessions") or config.MAX_PATTERN_HORIZON_SESSIONS)
        prices = _future_prices(conn, signal["ticker"], signal["snapshot_date"], horizon)
        if not prices: continue
        try:
            existing_prices = json.loads(signal.get("existing_sessions_json") or "[]")
        except Exception:
            existing_prices = []
        if len(existing_prices) == len(prices):
            # No new market session: do not rewrite the same pending outcome or
            # generate a fake archive revision merely because the daemon polled.
            continue
        entry = float(signal.get("entry_price") or 0.0)
        if entry <= 0: continue
        direction = str(signal["direction"]); favorable=[]; adverse=[]; first5=first_target=None
        target_low=signal.get("target_price_low"); target_high=signal.get("target_price_high")
        for idx,row in enumerate(prices,1):
            high=float(row.get("high") or row.get("close") or 0); low=float(row.get("low") or row.get("close") or 0)
            if direction=="long":
                fav=(high/entry-1)*100; adv=(low/entry-1)*100; hit=target_low is not None and high>=float(target_low)
            else:
                fav=float(favorable_move_pct("SHORT", entry, low) or 0.0)
                adv=float(adverse_move_pct("SHORT", entry, high) or 0.0)
                hit=target_high is not None and low<=float(target_high)
            favorable.append(fav); adverse.append(adv)
            if first5 is None and fav>=float(config.PATTERN_TARGET_PCT): first5=idx
            if first_target is None and hit: first_target=idx
        max_fav=max(favorable) if favorable else 0; max_adv=min(adverse) if adverse else 0
        complete=len(prices)>=horizon; predicted=float(signal.get("target_pct") or 0); has_target=signal.get("target_price_mid") is not None
        target_error=max_fav-predicted if has_target else None
        close=float(prices[-1].get("close") or entry)
        close_ret=float(directional_return_pct(direction, entry, close) or 0.0)
        resolved = "TARGET_GEX" if first_target is not None else ("MOVE_5PCT" if first5 is not None else ("HORIZON" if complete else "PENDING"))
        conn.execute(
            """INSERT INTO layer_signal_outcomes(
                   layer_signal_id,ticker,signal_date,direction,entry_price,max_horizon_sessions,
                   movement_5pct_hit,target_zone_hit,first_5pct_session,first_target_session,max_favorable_pct,
                   max_adverse_pct,target_error_pct,sessions_json,complete,labeled_at_utc,close_return_pct,
                   resolved_reason,future_path_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(layer_signal_id) DO UPDATE SET
                   movement_5pct_hit=excluded.movement_5pct_hit,target_zone_hit=excluded.target_zone_hit,
                   first_5pct_session=excluded.first_5pct_session,first_target_session=excluded.first_target_session,
                   max_favorable_pct=excluded.max_favorable_pct,max_adverse_pct=excluded.max_adverse_pct,
                   target_error_pct=excluded.target_error_pct,sessions_json=excluded.sessions_json,
                   complete=excluded.complete,labeled_at_utc=excluded.labeled_at_utc,
                   close_return_pct=excluded.close_return_pct,resolved_reason=excluded.resolved_reason,
                   future_path_json=excluded.future_path_json""",
            (int(signal["id"]),signal["ticker"],signal["snapshot_date"],direction,entry,horizon,
             1 if first5 else 0,1 if first_target else 0,first5,first_target,round(max_fav,6),round(max_adv,6),
             round(target_error,6) if target_error is not None else None,json.dumps(prices,ensure_ascii=False),
             1 if complete else 0,ts_utc_str(),round(close_ret,6),resolved,json.dumps(prices,ensure_ascii=False)),
        )
        signal_labeled += 1; signal_complete += 1 if complete else 0

    feature_rows = [dict(row) for row in conn.execute(
        """SELECT p.*,p.layer_feature_id AS id,p.signal_date AS snapshot_date,
                  o.sessions_json AS existing_sessions_json,o.complete AS existing_complete
           FROM layer_feature_pending p LEFT JOIN layer_feature_outcomes o ON o.layer_feature_id=p.layer_feature_id
           WHERE o.id IS NULL OR o.complete=0 ORDER BY p.signal_date,p.layer_feature_id"""
    ).fetchall()]
    feature_labeled = feature_complete = 0
    horizon = int(config.MAX_PATTERN_HORIZON_SESSIONS)
    for feature in feature_rows:
        prices=_future_prices(conn,feature["ticker"],feature["snapshot_date"],horizon)
        if not prices: continue
        try:
            existing_prices = json.loads(feature.get("existing_sessions_json") or "[]")
        except Exception:
            existing_prices = []
        if len(existing_prices) == len(prices):
            continue
        entry=float(feature.get("entry_price") or 0.0)
        if entry<=0: continue
        up=[]; down=[]; first_up=first_down=None
        for idx,row in enumerate(prices,1):
            high=float(row.get("high") or row.get("close") or 0); low=float(row.get("low") or row.get("close") or 0)
            u=(high/entry-1)*100; d=(low/entry-1)*100; up.append(u); down.append(d)
            if first_up is None and u>=float(config.PATTERN_TARGET_PCT): first_up=idx
            if first_down is None and d<=-float(config.PATTERN_TARGET_PCT): first_down=idx
        complete=len(prices)>=horizon; close=float(prices[-1].get("close") or entry); close_ret=(close/entry-1)*100
        conn.execute(
            """INSERT INTO layer_feature_outcomes(
                   layer_feature_id,ticker,signal_date,entry_price,horizon_sessions,long_5pct_hit,short_5pct_hit,
                   first_long_5pct_session,first_short_5pct_session,max_up_pct,max_down_pct,close_return_pct,
                   sessions_json,complete,labeled_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(layer_feature_id) DO UPDATE SET
                   long_5pct_hit=excluded.long_5pct_hit,short_5pct_hit=excluded.short_5pct_hit,
                   first_long_5pct_session=excluded.first_long_5pct_session,
                   first_short_5pct_session=excluded.first_short_5pct_session,max_up_pct=excluded.max_up_pct,
                   max_down_pct=excluded.max_down_pct,close_return_pct=excluded.close_return_pct,
                   sessions_json=excluded.sessions_json,complete=excluded.complete,labeled_at_utc=excluded.labeled_at_utc""",
            (int(feature["id"]),feature["ticker"],feature["snapshot_date"],entry,horizon,
             1 if first_up else 0,1 if first_down else 0,first_up,first_down,round(max(up),6),round(min(down),6),
             round(close_ret,6),json.dumps(prices,ensure_ascii=False),1 if complete else 0,ts_utc_str()),
        )
        feature_labeled += 1; feature_complete += 1 if complete else 0
    conn.commit(); conn.close()
    return {
        "signal_labeled": signal_labeled, "signal_complete": signal_complete,
        "feature_labeled": feature_labeled, "feature_complete": feature_complete,
        "pending": max(0,len(signals)-signal_complete)+max(0,len(feature_rows)-feature_complete),
    }
