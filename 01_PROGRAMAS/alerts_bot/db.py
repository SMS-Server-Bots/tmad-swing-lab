from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import config
from time_utils import now_madrid, ts_madrid_str, ts_utc_str, market_date_from_madrid_iso


def connect(path: Path, readonly: bool = False, *, live: bool = False) -> sqlite3.Connection:
    # Historical replay activates a process-local T0 snapshot. Redirect every
    # ordinary DB open (read-only or not) to that snapshot so helper modules
    # cannot accidentally escape to the live DB. Explicit control/observability
    # writes use live=True.
    if not live:
        try:
            from research_snapshot import research_db_path
            path = research_db_path(path)
        except Exception:
            pass

    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000;")
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000;")

    # WAL is persistent at database level. Reissuing PRAGMA journal_mode=WAL on
    # every connection can require an exclusive lock and makes concurrent
    # observers (Alpha Lab / replay) able to kill the production daemon at
    # startup. Only switch modes when the database is not already WAL.
    current_mode = ""
    try:
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        current_mode = str(row[0] if row else "").strip().lower()
    except sqlite3.OperationalError:
        current_mode = ""
    if current_mode != "wal":
        last_exc: Exception | None = None
        for attempt in range(8):
            try:
                row = conn.execute("PRAGMA journal_mode=WAL;").fetchone()
                mode = str(row[0] if row else "").strip().lower()
                if mode == "wal":
                    last_exc = None
                    break
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    conn.close()
                    raise
            time.sleep(min(2.0, 0.20 * (attempt + 1)))
        if last_exc is not None:
            conn.close()
            raise last_exc

    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}




def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

def _ensure_feature_catalog_v51(conn: sqlite3.Connection) -> None:
    now = ts_utc_str()
    features = {
        "market_context_score": ("market", "Composite SPY/QQQ/IWM/DIA risk-on score adjusted by volatility stress."),
        "market_breadth_score": ("market", "Cross-sectional breadth of currently observed nodes."),
        "vix_stress_score": ("market", "Volatility stress from VIX/VIXY/UVXY/SVXY when available."),
        "credit_risk_on_score": ("market", "Risk-on confirmation from HYG/LQD credit ETFs when available."),
        "sector_etf_score": ("sector", "Directional state of the primary related sector/industry ETF."),
        "sector_breadth_score": ("sector", "Breadth of tickers linked to the same sector ETF."),
        "leveraged_confirmation_score": ("relationship", "Confirmation from long/inverse leveraged ETF pair without double-counting it as an independent vote."),
        "relationship_context_score": ("relationship", "Weighted state of static and dynamically learned related ETFs."),
        "relationship_lead_lag_score": ("relationship", "State of historically leading related nodes, with point-in-time lead-lag weights."),
        "graph_context_score": ("relationship", "Combined market, sector, breadth, leveraged and dynamic relationship score."),
        "relational_deterioration_score": ("exit", "Early-warning deterioration propagated from market, sector and related nodes."),
        "relative_strength_vs_market": ("relative_strength", "Ticker node score minus broad market context."),
        "relative_strength_vs_sector": ("relative_strength", "Ticker node score minus its primary sector ETF score."),
        "market_core_risk_on_probability": ("market_core", "Risk-on probability from the active calibrated CORE model applied to SPY/QQQ/IWM/DIA."),
        "market_core_risk_off_probability": ("market_core", "Risk-off probability from the active calibrated CORE model applied to SPY/QQQ/IWM/DIA."),
        "main_etf_gex_risk_on_score": ("market_gex", "Aggregate risk-on GEX score across SPY/QQQ/IWM/DIA."),
        "vol_gex_stress_score": ("volatility_gex", "Aggregate stress score from VIX/VVIX and long/short volatility ETFs with sign inversion for short-vol products."),
    }
    conn.executemany(
        """INSERT INTO feature_catalog(feature_name,category,schema_version,description,enabled,created_at_utc,updated_at_utc)
           VALUES(?,?,?,?,1,?,?)
           ON CONFLICT(feature_name) DO UPDATE SET category=excluded.category,schema_version=excluded.schema_version,
             description=excluded.description,enabled=1,updated_at_utc=excluded.updated_at_utc""",
        [(name, category, config.RELATIONAL_FEATURE_SCHEMA_VERSION, description, now, now)
         for name, (category, description) in features.items()],
    )



def _reset_archive_layers_v52(conn: sqlite3.Connection) -> dict[str, int]:
    """One-time cleanup before reimporting weekly archives as canonical closes.

    Older versions could import selective runs from weekly ZIPs. The weekly
    archive is now exclusively the daily deep-close source; hourly rows live in
    dedicated intraday ZIPs. Only derived research rows are reset. Alerts,
    positions, models and raw Data Bot history are untouched.
    """
    marker = config.RUNTIME_ROOT / "migrations" / "v5_2_archive_layers.done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        return {"observations": 0, "outcomes": 0, "imports": 0}
    ids = [int(row[0]) for row in conn.execute(
        """SELECT id FROM feature_observations
           WHERE source_archive IS NOT NULL
             AND source_archive NOT LIKE 'intraday_%'"""
    ).fetchall()]
    outcomes = observations = 0
    if ids:
        ph = ",".join(["?"] * len(ids))
        cur = conn.execute(f"DELETE FROM observation_outcomes WHERE observation_id IN ({ph})", ids)
        outcomes = max(0, int(cur.rowcount or 0))
        cur = conn.execute(f"DELETE FROM feature_observations WHERE id IN ({ph})", ids)
        observations = max(0, int(cur.rowcount or 0))
    cur = conn.execute("DELETE FROM archive_imports WHERE archive_name NOT LIKE 'intraday/%'")
    imports = max(0, int(cur.rowcount or 0))
    conn.commit()
    marker.write_text(
        f"observations={observations}\noutcomes={outcomes}\narchive_imports={imports}\n",
        encoding="utf-8",
    )
    return {"observations": observations, "outcomes": outcomes, "imports": imports}

def ensure_alerts_schema() -> None:
    conn = connect(config.ALERTS_DB)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS feature_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at_utc TEXT NOT NULL,
            observed_at_madrid TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            source_scanner_run_id INTEGER NOT NULL,
            source_gex_run_id INTEGER,
            ticker TEXT NOT NULL,
            scanner_captured_at_madrid TEXT,
            gex_captured_at_madrid TEXT,
            gex_lag_minutes REAL,
            current_price REAL,
            total_gex REAL,
            total_gex_change_pct REAL,
            front_gex REAL,
            front_gex_change_pct REAL,
            week_gex REAL,
            week_gex_change_pct REAL,
            short_gex REAL,
            short_gex_change_pct REAL,
            next_gex REAL,
            next_gex_change_pct REAL,
            month_gex REAL,
            month_gex_change_pct REAL,
            put_wall_pct REAL,
            call_wall_pct REAL,
            wall_range_pct REAL,
            rvol REAL,
            rsi REAL,
            momentum_score REAL,
            trend TEXT,
            option_signal TEXT,
            unusual_flow_signal TEXT,
            features_json TEXT NOT NULL,
            unique_key TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS price_cache (
            ticker TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            source TEXT,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (ticker, trade_date)
        );

        CREATE TABLE IF NOT EXISTS observation_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            horizon_sessions INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            first_session_date TEXT,
            exit_date TEXT,
            exit_price REAL,
            return_long_pct REAL,
            mfe_long_pct REAL,
            mae_long_pct REAL,
            sessions_json TEXT,
            complete INTEGER NOT NULL DEFAULT 0,
            labeled_at_utc TEXT NOT NULL,
            unique_key TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS model_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_key TEXT NOT NULL UNIQUE,
            created_at_utc TEXT NOT NULL,
            created_at_madrid TEXT NOT NULL,
            effective_from_date TEXT NOT NULL,
            training_start_date TEXT,
            training_end_date TEXT,
            window_days INTEGER NOT NULL,
            status TEXT NOT NULL,
            parent_model_id INTEGER,
            objective_score REAL,
            train_sample INTEGER,
            validation_sample INTEGER,
            validation_win_rate REAL,
            validation_avg_return_pct REAL,
            validation_profit_factor REAL,
            params_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS model_state (
            state_key TEXT PRIMARY KEY,
            model_version_id INTEGER,
            updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT,
            updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL,
            previous_state TEXT,
            stage TEXT,
            generation INTEGER DEFAULT 0,
            entry_mode TEXT,
            error_code TEXT,
            model_id TEXT,
            reason TEXT,
            details_json TEXT,
            created_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_state_history_created
            ON pipeline_state_history(created_at_utc);

        CREATE TABLE IF NOT EXISTS archive_imports (
            archive_name TEXT PRIMARY KEY,
            imported_at_utc TEXT NOT NULL,
            feature_rows INTEGER DEFAULT 0,
            price_rows INTEGER DEFAULT 0,
            note TEXT
        );


        CREATE TABLE IF NOT EXISTS mover_discovery_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at_utc TEXT NOT NULL, finished_at_utc TEXT,
            training_start_date TEXT, training_end_date TEXT, status TEXT NOT NULL,
            observations INTEGER DEFAULT 0, long_movers INTEGER DEFAULT 0,
            short_movers INTEGER DEFAULT 0, controls INTEGER DEFAULT 0,
            oos_trades INTEGER DEFAULT 0, oos_dates INTEGER DEFAULT 0,
            model_version_id INTEGER, report_dir TEXT, summary_json TEXT
        );

        CREATE TABLE IF NOT EXISTS discovered_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, discovery_run_id INTEGER NOT NULL,
            model_version_id INTEGER, direction TEXT NOT NULL, pattern_rank INTEGER NOT NULL,
            rule_name TEXT NOT NULL, conditions_json TEXT NOT NULL,
            train_selected INTEGER, train_positive INTEGER, train_control INTEGER,
            train_precision REAL, train_recall REAL, train_lift REAL, score REAL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(discovery_run_id,direction,pattern_rank)
        );

        CREATE TABLE IF NOT EXISTS tuning_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            training_start_date TEXT,
            training_end_date TEXT,
            status TEXT NOT NULL,
            observations INTEGER DEFAULT 0,
            labeled_rows INTEGER DEFAULT 0,
            candidates_evaluated INTEGER DEFAULT 0,
            candidate_model_id INTEGER,
            promoted_model_id INTEGER,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_at_utc TEXT NOT NULL,
            event_at_madrid TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            source_scanner_run_id INTEGER,
            source_gex_run_id INTEGER,
            model_version_id INTEGER,
            ticker TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            direction TEXT NOT NULL,
            score REAL NOT NULL,
            current_price REAL NOT NULL,
            target_pct REAL NOT NULL,
            stop_pct REAL NOT NULL,
            max_hold_sessions INTEGER NOT NULL,
            reason TEXT,
            features_json TEXT,
            status TEXT NOT NULL DEFAULT 'shadow',
            sent_to_discord INTEGER NOT NULL DEFAULT 0,
            discord_message TEXT,
            unique_key TEXT UNIQUE
        );

        CREATE TABLE IF NOT EXISTS open_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_event_id INTEGER NOT NULL UNIQUE,
            model_version_id INTEGER,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_at_madrid TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            target_pct REAL NOT NULL,
            stop_pct REAL NOT NULL,
            max_hold_sessions INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            last_price REAL,
            best_return_pct REAL DEFAULT 0,
            worst_return_pct REAL DEFAULT 0,
            sessions_seen INTEGER DEFAULT 0,
            last_seen_date TEXT,
            lifecycle_state TEXT NOT NULL DEFAULT 'OPEN',
            setup_hit INTEGER NOT NULL DEFAULT 0,
            setup_hit_at_madrid TEXT,
            setup_hit_date TEXT,
            setup_hit_price REAL,
            setup_hit_return_pct REAL,
            setup_hit_sessions INTEGER,
            setup_hit_sent INTEGER NOT NULL DEFAULT 0,
            target_touch_at_madrid TEXT,
            target_touch_price REAL,
            target_touch_count INTEGER NOT NULL DEFAULT 0,
            target_touch_sent INTEGER NOT NULL DEFAULT 0,
            exit_giveback_pct REAL,
            exit_capture_ratio_pct REAL,
            exit_at_madrid TEXT,
            exit_date TEXT,
            exit_price REAL,
            exit_return_pct REAL,
            exit_reason TEXT,
            sent_exit_to_discord INTEGER NOT NULL DEFAULT 0,
            updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alert_state (
            ticker TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            last_event_at_madrid TEXT,
            last_price REAL,
            last_score REAL,
            status TEXT,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (ticker, rule_name)
        );

        CREATE TABLE IF NOT EXISTS bot_heartbeats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            heartbeat_utc TEXT NOT NULL,
            heartbeat_madrid TEXT NOT NULL,
            status TEXT,
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS asset_universe_nodes (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            category TEXT,
            sector TEXT,
            industry TEXT,
            asset_type TEXT,
            first_seen_date TEXT,
            last_seen_date TEXT NOT NULL,
            source_scanner_run_id INTEGER,
            metadata_json TEXT,
            updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS asset_relationships (
            ticker TEXT NOT NULL,
            related_ticker TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            base_weight REAL,
            dynamic_weight REAL,
            correlation REAL,
            beta REAL,
            lead_lag_corr REAL,
            sample_size INTEGER DEFAULT 0,
            effective_date TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            metadata_json TEXT,
            PRIMARY KEY (ticker, related_ticker, relation_type)
        );

        CREATE TABLE IF NOT EXISTS asset_relationship_history (
            ticker TEXT NOT NULL,
            related_ticker TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            base_weight REAL,
            dynamic_weight REAL,
            correlation REAL,
            beta REAL,
            lead_lag_corr REAL,
            sample_size INTEGER DEFAULT 0,
            effective_date TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            metadata_json TEXT,
            PRIMARY KEY (ticker, related_ticker, relation_type, effective_date)
        );

        CREATE TABLE IF NOT EXISTS relational_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_scanner_run_id INTEGER NOT NULL,
            snapshot_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            sector_etf TEXT,
            market_regime TEXT,
            market_context_score REAL,
            market_breadth_score REAL,
            vix_stress_score REAL,
            credit_risk_on_score REAL,
            sector_context_score REAL,
            sector_breadth_score REAL,
            relationship_context_score REAL,
            relationship_lead_lag_score REAL,
            leveraged_confirmation_score REAL,
            relative_strength_vs_market REAL,
            relative_strength_vs_sector REAL,
            graph_context_score REAL,
            relational_deterioration_score REAL,
            context_json TEXT NOT NULL,
            unique_key TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS feature_catalog (
            feature_name TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            description TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS model_feature_importance (
            model_version_id INTEGER NOT NULL,
            feature_name TEXT NOT NULL,
            importance REAL,
            method TEXT NOT NULL,
            sample_size INTEGER,
            details_json TEXT,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY (model_version_id, feature_name, method)
        );

        CREATE INDEX IF NOT EXISTS idx_feature_obs_ticker_date ON feature_observations(ticker, snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_feature_obs_run ON feature_observations(source_scanner_run_id, ticker);
        CREATE INDEX IF NOT EXISTS idx_price_cache_date ON price_cache(trade_date,ticker);
        CREATE INDEX IF NOT EXISTS idx_outcomes_obs_horizon ON observation_outcomes(observation_id, horizon_sessions);
        CREATE INDEX IF NOT EXISTS idx_outcomes_date ON observation_outcomes(observation_date, complete);
        CREATE INDEX IF NOT EXISTS idx_outcomes_ticker_date_horizon ON observation_outcomes(ticker, observation_date, complete, horizon_sessions);
        CREATE INDEX IF NOT EXISTS idx_models_status_effective ON model_versions(status, effective_from_date);
        CREATE INDEX IF NOT EXISTS idx_alert_events_date_score ON alert_events(snapshot_date, score DESC);
        CREATE INDEX IF NOT EXISTS idx_open_trades_status ON open_trades(status, ticker);
        CREATE INDEX IF NOT EXISTS idx_universe_nodes_type ON asset_universe_nodes(asset_type,last_seen_date);
        CREATE INDEX IF NOT EXISTS idx_relationships_ticker ON asset_relationships(ticker, dynamic_weight DESC);
        CREATE INDEX IF NOT EXISTS idx_relationship_history_date ON asset_relationship_history(effective_date,ticker);
        CREATE INDEX IF NOT EXISTS idx_rel_context_run ON relational_context_snapshots(source_scanner_run_id,ticker);
        CREATE INDEX IF NOT EXISTS idx_rel_context_date ON relational_context_snapshots(snapshot_date,market_regime);
        """
    )
    # Non-destructive migrations for existing persistent databases.
    _ensure_column(conn, "price_cache", "adjusted_close", "REAL")
    _ensure_column(conn, "price_cache", "split_factor", "REAL")
    _ensure_column(conn, "price_cache", "adjustment_json", "TEXT")
    _ensure_column(conn, "open_trades", "last_update_at_madrid", "TEXT")
    _ensure_column(conn, "open_trades", "last_update_return_pct", "REAL")
    _ensure_column(conn, "open_trades", "last_update_kind", "TEXT")
    _ensure_column(conn, "open_trades", "last_update_date", "TEXT")
    _ensure_column(conn, "open_trades", "updates_sent_today", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "open_trades", "deterioration_streak", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "open_trades", "favorable_streak", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "open_trades", "last_features_json", "TEXT")
    _ensure_column(conn, "open_trades", "lifecycle_state", "TEXT NOT NULL DEFAULT 'OPEN'")
    _ensure_column(conn, "open_trades", "setup_hit", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "open_trades", "setup_hit_at_madrid", "TEXT")
    _ensure_column(conn, "open_trades", "setup_hit_date", "TEXT")
    _ensure_column(conn, "open_trades", "setup_hit_price", "REAL")
    _ensure_column(conn, "open_trades", "setup_hit_return_pct", "REAL")
    _ensure_column(conn, "open_trades", "setup_hit_sessions", "INTEGER")
    _ensure_column(conn, "open_trades", "setup_hit_sent", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "open_trades", "target_touch_at_madrid", "TEXT")
    _ensure_column(conn, "open_trades", "target_touch_price", "REAL")
    _ensure_column(conn, "open_trades", "target_touch_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "open_trades", "target_touch_sent", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "open_trades", "exit_giveback_pct", "REAL")
    _ensure_column(conn, "open_trades", "exit_capture_ratio_pct", "REAL")
    _ensure_column(conn, "open_trades", "legacy_exit_reason", "TEXT")
    _ensure_column(conn, "open_trades", "calibration_excluded_reason", "TEXT")
    _ensure_column(conn, "feature_observations", "session_type", "TEXT")
    _ensure_column(conn, "feature_observations", "observation_layer", "TEXT NOT NULL DEFAULT 'intraday'")
    _ensure_column(conn, "feature_observations", "is_canonical_daily_close", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "feature_observations", "has_gex", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "feature_observations", "source_archive", "TEXT")
    _ensure_column(conn, "feature_observations", "feature_schema_version", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "feature_observations", "feature_tier", "TEXT NOT NULL DEFAULT 'FULL'")
    _ensure_column(conn, "feature_observations", "universe_quality", "TEXT")
    _ensure_column(conn, "feature_observations", "source_kind", "TEXT")
    _ensure_column(conn, "feature_observations", "sector_etf", "TEXT")
    _ensure_column(conn, "feature_observations", "market_regime", "TEXT")
    _ensure_column(conn, "feature_observations", "market_context_score", "REAL")
    _ensure_column(conn, "feature_observations", "market_breadth_score", "REAL")
    _ensure_column(conn, "feature_observations", "vix_stress_score", "REAL")
    _ensure_column(conn, "feature_observations", "credit_risk_on_score", "REAL")
    _ensure_column(conn, "feature_observations", "sector_context_score", "REAL")
    _ensure_column(conn, "feature_observations", "sector_breadth_score", "REAL")
    _ensure_column(conn, "feature_observations", "leveraged_confirmation_score", "REAL")
    _ensure_column(conn, "feature_observations", "relationship_context_score", "REAL")
    _ensure_column(conn, "feature_observations", "relationship_lead_lag_score", "REAL")
    _ensure_column(conn, "feature_observations", "graph_context_score", "REAL")
    _ensure_column(conn, "feature_observations", "relational_deterioration_score", "REAL")
    _ensure_column(conn, "feature_observations", "relative_strength_vs_market", "REAL")
    _ensure_column(conn, "feature_observations", "relative_strength_vs_sector", "REAL")
    _ensure_column(conn, "feature_observations", "relational_json", "TEXT")
    _ensure_column(conn, "relational_context_snapshots", "market_breadth_score", "REAL")
    _ensure_column(conn, "relational_context_snapshots", "vix_stress_score", "REAL")
    _ensure_column(conn, "relational_context_snapshots", "credit_risk_on_score", "REAL")
    _ensure_column(conn, "relational_context_snapshots", "sector_breadth_score", "REAL")
    _ensure_column(conn, "relational_context_snapshots", "relationship_lead_lag_score", "REAL")
    _ensure_column(conn, "relational_context_snapshots", "relative_strength_vs_market", "REAL")
    _ensure_column(conn, "relational_context_snapshots", "relative_strength_vs_sector", "REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_obs_layer_date ON feature_observations(observation_layer,is_canonical_daily_close,snapshot_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_obs_gex_date ON feature_observations(has_gex,snapshot_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_obs_tier_date ON feature_observations(feature_tier,observation_layer,snapshot_date)")
    _ensure_feature_catalog_v51(conn)
    _reset_archive_layers_v52(conn)

    # v5.0: conserva las filas históricas, pero archiva candidatos shadow de
    # versiones anteriores que nunca se enviaron. Esos registros podían quedar
    # abiertos indefinidamente, bloquear nuevas señales del mismo ticker y ocupar
    # prioridad GEX aunque el usuario nunca hubiese recibido la alerta.
    legacy_shadow = conn.execute(
        """SELECT COUNT(*)
           FROM open_trades ot
           JOIN alert_events ae ON ae.id=ot.alert_event_id
           WHERE ot.status='open' AND ae.sent_to_discord=0
             AND ae.status LIKE 'shadow%'"""
    ).fetchone()[0]
    if legacy_shadow:
        conn.execute(
            """UPDATE open_trades
               SET status='archived',
                   exit_reason='LEGACY_UNSENT_SHADOW_RESET_V4_8',
                   exit_at_madrid=COALESCE(exit_at_madrid, ?),
                   exit_date=COALESCE(exit_date, ?),
                   updated_at_utc=?
               WHERE status='open' AND alert_event_id IN (
                   SELECT id FROM alert_events
                   WHERE sent_to_discord=0 AND status LIKE 'shadow%'
               )""",
            (ts_madrid_str(), now_madrid().date().isoformat(), ts_utc_str()),
        )
    # v5.6.9: any genuinely open legacy position adopts the new invariant.
    conn.execute(
        "UPDATE open_trades SET target_pct=COALESCE(target_pct,?), stop_pct=0.0, max_hold_sessions=COALESCE(max_hold_sessions,?) WHERE status='open'",
        (float(config.PATTERN_TARGET_PCT), int(config.MAX_PATTERN_HORIZON_SESSIONS)),
    )
    conn.execute(
        """UPDATE alert_events
           SET target_pct=COALESCE(target_pct,?), stop_pct=0.0, max_hold_sessions=COALESCE(max_hold_sessions,?)
           WHERE id IN (SELECT alert_event_id FROM open_trades WHERE status='open')""",
        (float(config.PATTERN_TARGET_PCT), int(config.MAX_PATTERN_HORIZON_SESSIONS)),
    )
    conn.commit()
    _normalize_market_dates_v49(conn)
    _ensure_bootstrap_model(conn)
    conn.close()




def _normalize_market_dates_v49(conn: sqlite3.Connection) -> None:
    """One-time correction for rows created after midnight Madrid.

    Trading coverage and 1-5 session outcomes must be grouped by the New York
    market date, not by the server's Madrid calendar date.
    """
    marker = config.RUNTIME_ROOT / "migrations" / "v4_9_alert_market_dates.done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        return
    mappings = [
        ("feature_observations", "snapshot_date", "observed_at_madrid"),
        ("alert_events", "snapshot_date", "event_at_madrid"),
        ("open_trades", "entry_date", "entry_at_madrid"),
        ("open_trades", "exit_date", "exit_at_madrid"),
    ]
    updated = 0
    for table, date_col, ts_col in mappings:
        cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if date_col not in cols or ts_col not in cols:
            continue
        rows = conn.execute(
            f"SELECT rowid,{date_col},{ts_col} FROM {table} WHERE {ts_col} IS NOT NULL"
        ).fetchall()
        changes = []
        for row in rows:
            new_date = market_date_from_madrid_iso(row[2])
            if new_date and new_date != row[1]:
                changes.append((new_date, row[0]))
        if changes:
            conn.executemany(f"UPDATE {table} SET {date_col}=? WHERE rowid=?", changes)
            updated += len(changes)
    conn.execute(
        """UPDATE observation_outcomes
           SET observation_date=(SELECT f.snapshot_date FROM feature_observations f WHERE f.id=observation_outcomes.observation_id)
           WHERE EXISTS (SELECT 1 FROM feature_observations f WHERE f.id=observation_outcomes.observation_id
                         AND f.snapshot_date<>observation_outcomes.observation_date)"""
    )
    conn.commit()
    marker.write_text(f"updated_rows={updated}\n", encoding="utf-8")

def default_model_params() -> dict[str, Any]:
    base = {
        "enabled": True,
        "min_score": config.DEFAULT_MIN_SCORE,
        "min_rvol": config.DEFAULT_MIN_RVOL,
        "expiry_change_pct": config.DEFAULT_EXPIRY_CHANGE_PCT,
        "strike_change_pct": config.DEFAULT_STRIKE_CHANGE_PCT,
        "near_wall_pct": config.DEFAULT_NEAR_WALL_PCT,
        "break_wall_pct": config.DEFAULT_BREAK_WALL_PCT,
        "target_pct": config.PATTERN_TARGET_PCT,
        "stop_pct": 0.0,
        "hold_sessions": config.DEFAULT_HOLD_SESSIONS,
        "intraday_threshold_factor": config.UNVALIDATED_INTRADAY_THRESHOLD_FACTOR,
        "min_daily_pattern_projection": config.UNVALIDATED_MIN_DAILY_PATTERN_PROJECTION,
        "intraday_calibrated": False,
        "exit_gex_reversal_pct": config.DEFAULT_EXIT_GEX_REVERSAL_PCT,
        "exit_pattern_score": config.DEFAULT_EXIT_PATTERN_SCORE,
        "trailing_activation_pct": config.DEFAULT_TRAILING_ACTIVATION_PCT,
        "trailing_giveback_pct": config.DEFAULT_TRAILING_GIVEBACK_PCT,
        "context_weight": config.DEFAULT_CONTEXT_WEIGHT,
        "market_component_weight": config.DEFAULT_MARKET_COMPONENT_WEIGHT,
        "sector_component_weight": config.DEFAULT_SECTOR_COMPONENT_WEIGHT,
        "breadth_component_weight": config.DEFAULT_BREADTH_COMPONENT_WEIGHT,
        "leveraged_component_weight": config.DEFAULT_LEVERAGED_COMPONENT_WEIGHT,
        "dynamic_component_weight": config.DEFAULT_DYNAMIC_COMPONENT_WEIGHT,
        "lead_lag_component_weight": config.DEFAULT_LEAD_LAG_COMPONENT_WEIGHT,
        "min_context_score": config.DEFAULT_MIN_CONTEXT_SCORE,
        "min_market_score": config.DEFAULT_MIN_MARKET_SCORE,
        "min_sector_score": config.DEFAULT_MIN_SECTOR_SCORE,
        "max_adverse_context": config.DEFAULT_MAX_ADVERSE_CONTEXT,
        "relational_exit_score": config.DEFAULT_RELATIONAL_EXIT_SCORE,
    }
    rule_names = [
        "PUT_WALL_DEFENSE_LONG",
        "CALL_WALL_REJECTION_SHORT",
        "CALL_WALL_BREAK_LONG",
        "PUT_WALL_BREAK_SHORT",
        "EXPIRY_GEX_SHIFT_LONG",
        "EXPIRY_GEX_SHIFT_SHORT",
    ]
    rules = {name: dict(base) for name in rule_names}
    rules["_portfolio"] = {
        "max_signals_per_sector": (
            config.PRECISION_MAX_SIGNALS_PER_SECTOR
            if config.ALERT_SELECTION_PROFILE == "precision_first"
            else config.PORTFOLIO_MAX_SIGNALS_PER_SECTOR
        ),
        "max_signals_per_direction": (
            config.PRECISION_MAX_SIGNALS_PER_DIRECTION
            if config.ALERT_SELECTION_PROFILE == "precision_first"
            else config.PORTFOLIO_MAX_SIGNALS_PER_DIRECTION
        ),
    }
    return {
        "version": 9,
        "feature_tier": "BOOTSTRAP",
        "pattern_target_pct": config.PATTERN_TARGET_PCT,
        "max_horizon_sessions": config.MAX_PATTERN_HORIZON_SESSIONS,
        "rules": rules,
    }


def _ensure_bootstrap_model(conn: sqlite3.Connection) -> None:
    """Ensure the v5.6 bootstrap exists without replacing a validated champion."""
    bootstrap_key = "bootstrap-v5-6-precision-graph"
    row = conn.execute("SELECT id FROM model_versions WHERE model_key=?", (bootstrap_key,)).fetchone()
    now_utc = ts_utc_str()
    now_mad = ts_madrid_str()
    if not row:
        cur = conn.execute(
            """INSERT INTO model_versions(
                model_key,created_at_utc,created_at_madrid,effective_from_date,window_days,status,
                objective_score,train_sample,validation_sample,params_json,metrics_json,reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                bootstrap_key,
                now_utc,
                now_mad,
                now_mad[:10],
                config.TRAINING_WINDOW_DAYS,
                "bootstrap",
                None,
                0,
                0,
                json.dumps(default_model_params(), ensure_ascii=False),
                json.dumps({"target": "±5% directional", "max_sessions": 5, "exit": "pattern_invalidation_or_horizon", "calibration": "purged_walk_forward"}, ensure_ascii=False),
                "Bootstrap v5.6: CORE TMAD 30 sesiones para champion inicial + FULL DEEP e intradía como challengers.",
            ),
        )
        bootstrap_id = int(cur.lastrowid)
    else:
        bootstrap_id = int(row["id"])
        conn.execute(
            "UPDATE model_versions SET params_json=?, metrics_json=?, reason=? WHERE id=?",
            (
                json.dumps(default_model_params(), ensure_ascii=False),
                json.dumps({"target": "±5% directional", "max_sessions": 5, "exit": "pattern_invalidation_or_horizon", "calibration": "purged_walk_forward"}, ensure_ascii=False),
                "Bootstrap v5.6: CORE TMAD 30 sesiones para champion inicial + FULL DEEP e intradía como challengers.",
                bootstrap_id,
            ),
        )

    active = conn.execute(
        """SELECT mv.id,mv.status,mv.model_key FROM model_state ms
           JOIN model_versions mv ON mv.id=ms.model_version_id
           WHERE ms.state_key='active'"""
    ).fetchone()
    # Models trained with the old 1-2 session objective are incompatible with the
    # new +5% / five-session target. Preserve only a version-2 adaptive model.
    compatible = False
    if active and str(active["status"] or "").lower() != "bootstrap":
        payload = conn.execute("SELECT params_json FROM model_versions WHERE id=?", (active["id"],)).fetchone()
        try:
            compatible = int(json.loads(payload["params_json"] or "{}").get("version") or 0) >= 9
        except Exception:
            compatible = False
    if not compatible:
        if active and str(active["status"] or "").lower() != "bootstrap":
            conn.execute("UPDATE model_versions SET status='retired' WHERE id=?", (active["id"],))
        conn.execute(
            "INSERT OR REPLACE INTO model_state(state_key,model_version_id,updated_at_utc) VALUES('active',?,?)",
            (bootstrap_id, now_utc),
        )
    conn.commit()

def previous_feature_map(before_scanner_run_id: int) -> dict[str, dict]:
    """Load the immediately preceding Scanner cross-section.

    Scanner runs already contain the broad universe. Transition features compare
    adjacent runs, so scanning millions of historical observations with a
    per-ticker ROW_NUMBER is both slower and semantically less precise.
    idx_feature_obs_run(source_scanner_run_id,ticker) makes this lookup bounded.
    """
    conn = connect(config.ALERTS_DB)
    previous = conn.execute(
        "SELECT MAX(source_scanner_run_id) FROM feature_observations WHERE source_scanner_run_id < ?",
        (int(before_scanner_run_id),),
    ).fetchone()
    previous_run_id = int((previous[0] if previous else 0) or 0)
    if previous_run_id <= 0:
        conn.close()
        return {}
    rows = conn.execute(
        """SELECT ticker,features_json
           FROM feature_observations
           WHERE source_scanner_run_id=?
           ORDER BY id DESC""",
        (previous_run_id,),
    ).fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        ticker = str(row["ticker"] or "")
        if not ticker or ticker in out:
            continue
        try:
            out[ticker] = json.loads(row["features_json"] or "{}")
        except Exception:
            pass
    return out


def insert_relational_context(row: dict) -> int | None:
    conn = connect(config.ALERTS_DB)
    cols = list(row.keys())
    sql = f"INSERT OR REPLACE INTO relational_context_snapshots ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
    cur = conn.execute(sql, [row[col] for col in cols])
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return int(row_id) if row_id else None


def upsert_asset_relationships(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect(config.ALERTS_DB)
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO asset_relationships ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
    conn.executemany(sql, [[row.get(col) for col in cols] for row in rows])
    conn.commit()
    conn.close()
    return len(rows)


def upsert_asset_relationship_history(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect(config.ALERTS_DB)
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO asset_relationship_history ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
    conn.executemany(sql, [[row.get(col) for col in cols] for row in rows])
    conn.commit()
    conn.close()
    return len(rows)


def get_asset_relationships_as_of(effective_date: str) -> dict[str, list[dict]]:
    conn = connect(config.ALERTS_DB)
    rows = [dict(row) for row in conn.execute(
        """WITH latest AS (
               SELECT ticker,related_ticker,relation_type,MAX(effective_date) AS effective_date
               FROM asset_relationship_history
               WHERE effective_date<=?
               GROUP BY ticker,related_ticker,relation_type
           )
           SELECT h.* FROM asset_relationship_history h
           JOIN latest l ON l.ticker=h.ticker AND l.related_ticker=h.related_ticker
             AND l.relation_type=h.relation_type AND l.effective_date=h.effective_date
           ORDER BY h.ticker,COALESCE(h.dynamic_weight,h.base_weight,0) DESC,h.related_ticker""",
        (str(effective_date),),
    ).fetchall()]
    conn.close()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row.get("ticker") or ""), []).append(row)
    return out


def get_asset_relationships() -> dict[str, list[dict]]:
    conn = connect(config.ALERTS_DB)
    rows = [dict(row) for row in conn.execute(
        """SELECT * FROM asset_relationships
           ORDER BY ticker,COALESCE(dynamic_weight,base_weight,0) DESC,related_ticker"""
    ).fetchall()]
    conn.close()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row.get("ticker") or ""), []).append(row)
    return out


def get_latest_ticker_metadata() -> dict[str, dict]:
    conn = connect(config.ALERTS_DB)
    rows = conn.execute(
        """WITH ranked AS (
               SELECT ticker,features_json,
                      ROW_NUMBER() OVER(PARTITION BY ticker ORDER BY observed_at_madrid DESC,id DESC) rn
               FROM feature_observations
           ) SELECT ticker,features_json FROM ranked WHERE rn=1"""
    ).fetchall()
    conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            features = json.loads(row["features_json"] or "{}")
        except Exception:
            features = {}
        out[str(row["ticker"])] = {
            "name": features.get("name"),
            "category": features.get("category"),
            "sector": features.get("sector") or features.get("category"),
            "industry": features.get("industry"),
            "sector_etf": features.get("sector_etf"),
            "asset_type": features.get("asset_type"),
        }
    return out


def upsert_asset_universe_nodes(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect(config.ALERTS_DB)
    now = ts_utc_str()
    payload = []
    for row in rows:
        ticker = str(row.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        last_seen = str(row.get("last_seen_date") or row.get("snapshot_date") or now[:10])[:10]
        payload.append((
            ticker, row.get("name"), row.get("category"), row.get("sector") or row.get("category"),
            row.get("industry"), row.get("asset_type"), last_seen, last_seen,
            row.get("source_scanner_run_id"),
            json.dumps(row.get("metadata") or {}, ensure_ascii=False), now,
        ))
    conn.executemany(
        """INSERT INTO asset_universe_nodes(
               ticker,name,category,sector,industry,asset_type,first_seen_date,last_seen_date,
               source_scanner_run_id,metadata_json,updated_at_utc
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
               name=COALESCE(excluded.name,asset_universe_nodes.name),
               category=COALESCE(excluded.category,asset_universe_nodes.category),
               sector=COALESCE(excluded.sector,asset_universe_nodes.sector),
               industry=COALESCE(excluded.industry,asset_universe_nodes.industry),
               asset_type=COALESCE(excluded.asset_type,asset_universe_nodes.asset_type),
               first_seen_date=MIN(asset_universe_nodes.first_seen_date,excluded.first_seen_date),
               last_seen_date=MAX(asset_universe_nodes.last_seen_date,excluded.last_seen_date),
               source_scanner_run_id=COALESCE(excluded.source_scanner_run_id,asset_universe_nodes.source_scanner_run_id),
               metadata_json=excluded.metadata_json,updated_at_utc=excluded.updated_at_utc""",
        payload,
    )
    conn.commit(); conn.close()
    return len(payload)


def get_asset_universe_nodes() -> dict[str, dict]:
    conn = connect(config.ALERTS_DB)
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM asset_universe_nodes ORDER BY ticker"
    ).fetchall()]
    conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            metadata = {}
        row["metadata"] = metadata
        out[str(row.get("ticker") or "")] = row
    return out


def feature_runs_for_relational_backfill(limit: int = 1000) -> list[int]:
    conn = connect(config.ALERTS_DB)
    rows = conn.execute(
        """SELECT source_scanner_run_id
           FROM feature_observations
           GROUP BY source_scanner_run_id
           HAVING MAX(COALESCE(feature_schema_version,0)) < ?
               OR SUM(CASE WHEN graph_context_score IS NULL THEN 1 ELSE 0 END) > 0
           ORDER BY MIN(observed_at_madrid)
           LIMIT ?""",
        (int(config.RELATIONAL_FEATURE_SCHEMA_VERSION), int(limit)),
    ).fetchall()
    conn.close()
    return [int(row[0]) for row in rows]


def feature_rows_for_run(run_id: int) -> list[dict]:
    conn = connect(config.ALERTS_DB)
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM feature_observations WHERE source_scanner_run_id=? ORDER BY ticker,id",
        (int(run_id),),
    ).fetchall()]
    conn.close()
    return rows


def update_feature_relational(observation_id: int, features: dict, relational: dict) -> None:
    conn = connect(config.ALERTS_DB)
    conn.execute(
        """UPDATE feature_observations SET
               feature_schema_version=?,sector_etf=?,market_regime=?,market_context_score=?,
               market_breadth_score=?,vix_stress_score=?,credit_risk_on_score=?,
               sector_context_score=?,sector_breadth_score=?,leveraged_confirmation_score=?,
               relationship_context_score=?,relationship_lead_lag_score=?,graph_context_score=?,
               relational_deterioration_score=?,relative_strength_vs_market=?,relative_strength_vs_sector=?,
               relational_json=?,features_json=?
           WHERE id=?""",
        (
            int(config.RELATIONAL_FEATURE_SCHEMA_VERSION), relational.get("sector_etf"),
            relational.get("market_regime"), relational.get("market_context_score"),
            relational.get("market_breadth_score"), relational.get("vix_stress_score"), relational.get("credit_risk_on_score"),
            relational.get("sector_etf_score"), relational.get("sector_breadth_score"), relational.get("leveraged_confirmation_score"),
            relational.get("relationship_context_score"), relational.get("relationship_lead_lag_score"),
            relational.get("graph_context_score"), relational.get("relational_deterioration_score"),
            relational.get("relative_strength_vs_market"), relational.get("relative_strength_vs_sector"),
            json.dumps(relational, ensure_ascii=False), json.dumps(features, ensure_ascii=False), int(observation_id),
        ),
    )
    conn.commit(); conn.close()


def insert_model_feature_importance(model_version_id: int, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect(config.ALERTS_DB)
    payload = []
    for row in rows:
        payload.append((
            int(model_version_id), str(row.get("feature_name")), row.get("importance"),
            str(row.get("method") or "ablation"), row.get("sample_size"),
            json.dumps(row.get("details") or {}, ensure_ascii=False), ts_utc_str(),
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO model_feature_importance(
               model_version_id,feature_name,importance,method,sample_size,details_json,created_at_utc
           ) VALUES(?,?,?,?,?,?,?)""",
        payload,
    )
    conn.commit(); conn.close()
    return len(payload)


def insert_heartbeat(status: str, note: str = "", retries: int = 3) -> bool:
    """Persist the local heartbeat without ever taking down the daemon.

    Coordination Bus remains the primary liveness signal. The local SQLite
    heartbeat is operational telemetry and must degrade safely when a writer
    temporarily owns the database.
    """
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        conn = None
        try:
            config.ALERTS_DB.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(config.ALERTS_DB, timeout=2)
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute(
                "INSERT INTO bot_heartbeats(heartbeat_utc,heartbeat_madrid,status,note) VALUES(?,?,?,?)",
                (ts_utc_str(), ts_madrid_str(), status, note),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if attempt < attempts:
                time.sleep(0.5 * attempt)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return False


def data_dbs_available() -> tuple[bool, str]:
    missing = [str(path) for path in (config.SCANNER_DB, config.GEX_DB) if not path.exists()]
    if missing:
        return False, "Faltan DBs del Data Bot: " + ", ".join(missing)
    return True, "ok"


def latest_successful_scanner_run() -> dict | None:
    conn = connect(config.SCANNER_DB, readonly=True)
    row = conn.execute(
        """SELECT r.*, (SELECT COUNT(*) FROM scanner_snapshots s WHERE s.scanner_run_id=r.id) AS snapshot_count
           FROM scanner_runs r
           WHERE r.status='success'
           ORDER BY r.captured_at_madrid DESC LIMIT 1"""
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def successful_scanner_runs_after(run_id: int, limit: int = 12) -> list[dict]:
    """Return valid Scanner runs not yet materialised by Alerts, oldest first."""
    conn = connect(config.SCANNER_DB, readonly=True)
    try:
        rows = conn.execute(
            """SELECT r.*, (SELECT COUNT(*) FROM scanner_snapshots s WHERE s.scanner_run_id=r.id) AS snapshot_count
               FROM scanner_runs r
               WHERE r.status='success' AND r.id>?
               ORDER BY r.id ASC LIMIT ?""",
            (int(run_id or 0), max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def latest_scanner_run_any() -> dict | None:
    conn = connect(config.SCANNER_DB, readonly=True)
    row = conn.execute(
        """SELECT r.*, (SELECT COUNT(*) FROM scanner_snapshots s WHERE s.scanner_run_id=r.id) AS snapshot_count
           FROM scanner_runs r
           ORDER BY r.id DESC LIMIT 1"""
    ).fetchone()
    conn.close()
    return dict(row) if row else None



def classify_capture_layer(session_type: str | None) -> tuple[str, int]:
    session = str(session_type or "").lower()
    daily = "deep" in session or session in {"daily_close", "canonical_close", "postmarket"}
    return ("daily_close", 1) if daily else ("intraday", 0)


def backfill_capture_layers_from_live_runs() -> dict[str, int]:
    """Classify persisted observations using the authoritative Data Bot run label."""
    if not config.SCANNER_DB.exists():
        return {"daily": 0, "intraday": 0}
    src = connect(config.SCANNER_DB, readonly=True)
    try:
        rows = src.execute("SELECT id,session_type FROM scanner_runs").fetchall()
    finally:
        src.close()
    conn = connect(config.ALERTS_DB)
    daily = intraday = 0
    try:
        for row in rows:
            layer, canonical = classify_capture_layer(row[1])
            cur = conn.execute(
                """UPDATE feature_observations SET session_type=?,observation_layer=?,is_canonical_daily_close=?
                   WHERE source_scanner_run_id=?""",
                (row[1], layer, canonical, int(row[0])),
            )
            if layer == "daily_close": daily += max(0, int(cur.rowcount or 0))
            else: intraday += max(0, int(cur.rowcount or 0))
        conn.commit()
    finally:
        conn.close()
    return {"daily": daily, "intraday": intraday}


def purge_intraday_observations_through(trade_date: str) -> dict[str, int]:
    """Remove the ephemeral live feature layer after Data Bot has archived it."""
    conn = connect(config.ALERTS_DB)
    try:
        ids = [int(row[0]) for row in conn.execute(
            "SELECT id FROM feature_observations WHERE observation_layer='intraday' AND source_archive IS NULL AND snapshot_date<=?",
            (str(trade_date),),
        ).fetchall()]
        outcomes = observations = contexts = 0
        if ids:
            ph = ",".join(["?"] * len(ids))
            cur = conn.execute(f"DELETE FROM observation_outcomes WHERE observation_id IN ({ph})", ids)
            outcomes = max(0, int(cur.rowcount or 0))
            run_ids = [int(row[0]) for row in conn.execute(
                f"SELECT DISTINCT source_scanner_run_id FROM feature_observations WHERE id IN ({ph})", ids
            ).fetchall()]
            cur = conn.execute(f"DELETE FROM feature_observations WHERE id IN ({ph})", ids)
            observations = max(0, int(cur.rowcount or 0))
            if run_ids:
                rph = ",".join(["?"] * len(run_ids))
                cur = conn.execute(f"DELETE FROM relational_context_snapshots WHERE source_scanner_run_id IN ({rph})", run_ids)
                contexts = max(0, int(cur.rowcount or 0))
        conn.commit()
        return {"observations": observations, "outcomes": outcomes, "contexts": contexts}
    finally:
        conn.close()


def _ensure_runtime_state_table(conn: sqlite3.Connection) -> None:
    # Some focused validators and recovery tools open a minimal Alerts DB.
    # Keep runtime deduplication fail-safe without requiring a full schema bootstrap.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runtime_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT,
            updated_at_utc TEXT
        )"""
    )


def set_runtime_state(key: str, value: str) -> None:
    # Runtime progress/results must remain visible to production while research
    # reads stay frozen at T0.
    conn = connect(config.ALERTS_DB, live=True)
    try:
        _ensure_runtime_state_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO runtime_state(state_key,state_value,updated_at_utc) VALUES(?,?,?)",
            (str(key), str(value), ts_utc_str()),
        )
        conn.commit()
    finally:
        conn.close()


def get_runtime_state(key: str, default: str | None = None) -> str | None:
    try:
        from research_snapshot import research_db_path
        source = research_db_path(config.ALERTS_DB)
    except Exception:
        source = config.ALERTS_DB
    conn = connect(source)
    try:
        _ensure_runtime_state_table(conn)
        row = conn.execute("SELECT state_value FROM runtime_state WHERE state_key=?", (str(key),)).fetchone()
        return str(row[0]) if row else default
    finally:
        conn.close()



def record_pipeline_state(payload: dict) -> None:
    conn = connect(config.ALERTS_DB)
    conn.execute(
        """INSERT INTO pipeline_state_history(
            state,previous_state,stage,generation,entry_mode,error_code,model_id,reason,details_json,created_at_utc
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            str(payload.get("state") or "ERROR"),
            str(payload.get("previous_state") or ""),
            str(payload.get("stage") or ""),
            int(payload.get("generation") or 0),
            str(payload.get("entry_mode") or "off"),
            str(payload.get("error_code") or ""),
            str(payload.get("model_id") or ""),
            str(payload.get("reason") or ""),
            json.dumps(payload.get("details") or {}, ensure_ascii=False, default=str),
            ts_utc_str(),
        ),
    )
    conn.commit(); conn.close()

def latest_processed_scanner_run_id() -> int:
    conn = connect(config.ALERTS_DB)
    row = conn.execute("SELECT MAX(source_scanner_run_id) FROM feature_observations").fetchone()
    conn.close()
    return int(row[0] or 0)


def latest_scanner_snapshots(run_id: int) -> list[dict]:
    conn = connect(config.SCANNER_DB, readonly=True)
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM scanner_snapshots WHERE scanner_run_id=?", (run_id,)
    ).fetchall()]
    conn.close()
    return rows


def latest_gex_by_ticker(as_of_madrid: str | None = None) -> dict[str, dict]:
    conn = connect(config.GEX_DB, readonly=True)
    where = "WHERE gs.captured_at_madrid <= ?" if as_of_madrid else ""
    params: tuple[Any, ...] = (as_of_madrid,) if as_of_madrid else ()
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT gs.*, ROW_NUMBER() OVER(
                PARTITION BY gs.ticker ORDER BY gs.captured_at_madrid DESC, gs.gex_run_id DESC
            ) AS rn
            FROM gex_summary gs
            JOIN gex_runs gr ON gr.id=gs.gex_run_id AND gr.status='success'
            {where}
        )
        SELECT * FROM ranked WHERE rn=1
        """,
        params,
    ).fetchall()
    out = {str(row["ticker"]): dict(row) for row in rows}
    conn.close()
    return out


def latest_expiry_buckets_by_ticker(as_of_madrid: str | None = None) -> dict[str, dict[str, dict]]:
    conn = connect(config.GEX_DB, readonly=True)
    if "gex_expiry_buckets" not in _table_names(conn):
        conn.close()
        return {}
    where = "WHERE b.captured_at_madrid <= ?" if as_of_madrid else ""
    params: tuple[Any, ...] = (as_of_madrid,) if as_of_madrid else ()
    rows = conn.execute(
        f"""
        WITH ranked_runs AS (
            SELECT b.ticker,b.gex_run_id,b.captured_at_madrid,
                   ROW_NUMBER() OVER(PARTITION BY b.ticker ORDER BY b.captured_at_madrid DESC,b.gex_run_id DESC) rn
            FROM gex_expiry_buckets b
            JOIN gex_runs gr ON gr.id=b.gex_run_id AND gr.status='success'
            {where}
            GROUP BY b.ticker,b.gex_run_id,b.captured_at_madrid
        )
        SELECT b.* FROM gex_expiry_buckets b
        JOIN ranked_runs r ON r.ticker=b.ticker AND r.gex_run_id=b.gex_run_id AND r.rn=1
        """,
        params,
    ).fetchall()
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), {})[str(row["expiry_bucket"])] = dict(row)
    conn.close()
    return out


def latest_expirations_by_ticker(limit_each: int = 8, as_of_madrid: str | None = None) -> dict[str, list[dict]]:
    conn = connect(config.GEX_DB, readonly=True)
    if "gex_expirations" not in _table_names(conn):
        conn.close()
        return {}
    where = "WHERE e.captured_at_madrid <= ?" if as_of_madrid else ""
    params: tuple[Any, ...] = (as_of_madrid, limit_each) if as_of_madrid else (limit_each,)
    rows = conn.execute(
        f"""
        WITH latest AS (
          SELECT e.ticker,e.gex_run_id,e.captured_at_madrid,
                 ROW_NUMBER() OVER(PARTITION BY e.ticker ORDER BY e.captured_at_madrid DESC,e.gex_run_id DESC) rn
          FROM gex_expirations e
          JOIN gex_runs gr ON gr.id=e.gex_run_id AND gr.status='success'
          {where}
          GROUP BY e.ticker,e.gex_run_id,e.captured_at_madrid
        ), ranked AS (
          SELECT e.*,ROW_NUMBER() OVER(PARTITION BY e.ticker ORDER BY e.dte ASC,e.abs_gex DESC) item_rn
          FROM gex_expirations e JOIN latest l ON l.ticker=e.ticker AND l.gex_run_id=e.gex_run_id AND l.rn=1
        )
        SELECT * FROM ranked WHERE item_rn<=?
        """,
        params,
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), []).append(dict(row))
    conn.close()
    return out


def insert_feature_observation(row: dict) -> int | None:
    conn = connect(config.ALERTS_DB)
    cols = list(row.keys())
    update_cols = [col for col in cols if col != "unique_key"]
    sql = (
        f"INSERT INTO feature_observations ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))}) "
        f"ON CONFLICT(unique_key) DO UPDATE SET {','.join([f'{col}=excluded.{col}' for col in update_cols])}"
    )
    conn.execute(sql, [row[col] for col in cols])
    stored = conn.execute("SELECT id FROM feature_observations WHERE unique_key=?", (row.get("unique_key"),)).fetchone()
    conn.commit(); conn.close()
    return int(stored[0]) if stored else None


def upsert_feature_observations_bulk(rows: list[dict], batch_size: int = 2000) -> int:
    """Upsert homogeneous feature rows in one WAL transaction.

    CORE backfills can contain tens of thousands of ticker/date observations.
    Opening one SQLite connection per row made startup take hours on Windows.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    if any(list(row.keys()) != cols for row in rows):
        raise ValueError("Bulk feature rows must have the same ordered columns.")
    update_cols = [col for col in cols if col != "unique_key"]
    sql = (
        f"INSERT INTO feature_observations ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))}) "
        f"ON CONFLICT(unique_key) DO UPDATE SET "
        f"{','.join([f'{col}=excluded.{col}' for col in update_cols])}"
    )
    conn = connect(config.ALERTS_DB)
    try:
        total = 0
        size = max(1, int(batch_size))
        for start in range(0, len(rows), size):
            batch = rows[start:start + size]
            conn.executemany(sql, [[row[col] for col in cols] for row in batch])
            total += len(batch)
        conn.commit()
        return total
    finally:
        conn.close()


def feature_observation_id(unique_key: str) -> int | None:
    conn = connect(config.ALERTS_DB)
    row = conn.execute("SELECT id FROM feature_observations WHERE unique_key=?", (unique_key,)).fetchone()
    conn.close()
    return int(row["id"]) if row else None


def observations_needing_labels_batch(
    *,
    after_id: int = 0,
    batch_size: int = 1000,
) -> list[dict]:
    """Return a bounded keyset batch of observations missing any required horizon.

    v6.2.56 keeps outcome materialisation memory-bounded. The previous startup
    path fetched every pending feature observation into Python at once; with
    hundreds of thousands of observations that can exhaust the Alerts process
    while a historical replay is also active.

    Only fields required by outcomes.py are projected. Existing complete
    horizons are returned per observation so callers do not need a global
    in-memory set of observation_outcomes.
    """
    cutoff = (now_madrid().date() - timedelta(days=config.FEATURE_RETENTION_DAYS)).isoformat()
    horizons = sorted({int(value) for value in config.OUTCOME_HORIZONS_SESSIONS})
    if not horizons:
        return []
    size = max(1, min(10000, int(batch_size)))
    missing_predicate = " OR ".join(
        "NOT EXISTS ("
        "SELECT 1 FROM observation_outcomes oh "
        "WHERE oh.observation_id=f.id AND oh.complete=1 AND oh.horizon_sessions=?"
        ")"
        for _ in horizons
    )
    sql = f"""
        SELECT
            f.id,
            f.ticker,
            f.snapshot_date,
            f.current_price,
            f.observation_layer,
            (
                SELECT GROUP_CONCAT(DISTINCT oe.horizon_sessions)
                FROM observation_outcomes oe
                WHERE oe.observation_id=f.id AND oe.complete=1
            ) AS completed_horizons
        FROM feature_observations f
        WHERE f.snapshot_date>=?
          AND f.id>?
          AND ({missing_predicate})
        ORDER BY f.id
        LIMIT ?
    """
    params = [cutoff, max(0, int(after_id)), *horizons, size]
    conn = connect(config.ALERTS_DB)
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def observations_needing_labels() -> list[dict]:
    """Compatibility helper; prefer observations_needing_labels_batch in runtime paths."""
    rows: list[dict] = []
    after_id = 0
    while True:
        batch = observations_needing_labels_batch(after_id=after_id, batch_size=2000)
        if not batch:
            break
        rows.extend(batch)
        after_id = int(batch[-1]["id"])
    return rows


def upsert_observation_outcome(row: dict) -> None:
    conn = connect(config.ALERTS_DB)
    cols = list(row.keys())
    sql = f"INSERT OR REPLACE INTO observation_outcomes ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
    conn.execute(sql, [row[col] for col in cols])
    conn.commit()
    conn.close()


def upsert_observation_outcomes_bulk(rows: list[dict], batch_size: int = 5000) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    if any(list(row.keys()) != cols for row in rows):
        raise ValueError("Bulk outcome rows must have the same ordered columns.")
    sql = (
        f"INSERT INTO observation_outcomes ({','.join(cols)}) "
        f"VALUES ({','.join(['?'] * len(cols))}) "
        f"ON CONFLICT(unique_key) DO UPDATE SET "
        + ",".join(f"{col}=excluded.{col}" for col in cols if col not in {"id", "unique_key"})
    )
    conn = connect(config.ALERTS_DB)
    try:
        total = 0
        size = max(1, int(batch_size))
        for start in range(0, len(rows), size):
            batch = rows[start:start + size]
            conn.executemany(sql, [[row[col] for col in cols] for row in batch])
            total += len(batch)
        conn.commit()
        return total
    finally:
        conn.close()


def _live_price_rows() -> list[dict]:
    conn = connect(config.SCANNER_DB, readonly=True)
    tables = _table_names(conn)

    def _optional_projection(table: str) -> str:
        cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        return ",".join([
            "adjusted_close" if "adjusted_close" in cols else "NULL AS adjusted_close",
            "split_factor" if "split_factor" in cols else "NULL AS split_factor",
            "adjustment_json" if "adjustment_json" in cols else "NULL AS adjustment_json",
        ])

    if "ticker_ohlc_daily" in tables:
        extra = _optional_projection("ticker_ohlc_daily")
        rows = conn.execute(
            f"""SELECT ticker,price_date AS trade_date,open,high,low,COALESCE(close,price) AS close,
                       {extra}
                FROM ticker_ohlc_daily
                WHERE COALESCE(close,price) IS NOT NULL
                ORDER BY ticker,price_date"""
        ).fetchall()
    elif "ticker_price_history" in tables:
        extra = _optional_projection("ticker_price_history")
        rows = conn.execute(
            f"""
            WITH ranked AS (
              SELECT ticker,price_date AS trade_date,open,high,low,COALESCE(close,price) AS close,
                     {extra},
                     ROW_NUMBER() OVER(PARTITION BY ticker,price_date ORDER BY captured_at_madrid DESC,id DESC) rn
              FROM ticker_price_history WHERE COALESCE(close,price) IS NOT NULL
            )
            SELECT ticker,trade_date,open,high,low,close,adjusted_close,split_factor,adjustment_json
            FROM ranked WHERE rn=1 ORDER BY ticker,trade_date
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT ticker,trade_date,current_price AS open,current_price AS high,
                      current_price AS low,current_price AS close,
                      NULL AS adjusted_close,NULL AS split_factor,NULL AS adjustment_json
               FROM ticker_daily_history WHERE current_price IS NOT NULL ORDER BY ticker,trade_date"""
        ).fetchall()
    out = [dict(row) for row in rows]
    conn.close()
    return out


def upsert_price_cache_rows(rows: list[dict], source: str) -> int:
    if not rows:
        return 0
    conn = connect(config.ALERTS_DB)
    count = 0
    for row in rows:
        ticker = row.get("ticker")
        trade_date = row.get("trade_date") or row.get("price_date")
        close = row.get("close") if row.get("close") not in (None, "") else row.get("price")
        if not ticker or not trade_date or close in (None, ""):
            continue
        conn.execute(
            """INSERT INTO price_cache(
                   ticker,trade_date,open,high,low,close,adjusted_close,split_factor,adjustment_json,source,updated_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker,trade_date) DO UPDATE SET
                 open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                 adjusted_close=excluded.adjusted_close,split_factor=excluded.split_factor,
                 adjustment_json=excluded.adjustment_json,source=excluded.source,
                 updated_at_utc=excluded.updated_at_utc""",
            (
                ticker, str(trade_date)[:10], row.get("open"), row.get("high"), row.get("low"), close,
                row.get("adjusted_close"), row.get("split_factor"), row.get("adjustment_json"),
                source, ts_utc_str(),
            ),
        )
        count += 1
    conn.commit(); conn.close()
    return count


def sync_live_price_cache() -> int:
    if not config.SCANNER_DB.exists():
        return 0
    return upsert_price_cache_rows(_live_price_rows(), "data_live")


def daily_price_series() -> dict[str, list[dict]]:
    sync_live_price_cache()
    conn = connect(config.ALERTS_DB)
    rows = conn.execute(
        "SELECT ticker,trade_date,open,high,low,close FROM price_cache ORDER BY ticker,trade_date"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(str(row["ticker"]), []).append(dict(row))
    conn.close()
    return out


def latest_canonical_daily_dates(limit: int | None = None) -> list[str]:
    limit = int(limit or config.DAILY_CLOSE_WINDOW_SESSIONS)
    conn = connect(config.ALERTS_DB)
    rows = conn.execute(
        """SELECT DISTINCT snapshot_date FROM feature_observations
           WHERE observation_layer='daily_close' AND is_canonical_daily_close=1
           ORDER BY snapshot_date DESC LIMIT ?""",
        (max(1, limit),),
    ).fetchall()
    conn.close()
    return sorted(str(row[0]) for row in rows if row[0])



def latest_core_daily_dates(limit: int | None = None) -> list[str]:
    limit = int(limit or config.CORE_HISTORY_TARGET_SESSIONS)
    conn = connect(config.ALERTS_DB)
    rows = conn.execute(
        """SELECT DISTINCT snapshot_date FROM feature_observations
           WHERE observation_layer='core_history' AND feature_tier='CORE'
           ORDER BY snapshot_date DESC LIMIT ?""",
        (max(1, limit),),
    ).fetchall()
    conn.close()
    return sorted(str(row[0]) for row in rows if row[0])


def get_training_rows(layer: str | None = None) -> list[dict]:
    cutoff = (now_madrid().date() - timedelta(days=config.MODEL_HISTORY_MAX_DAYS)).isoformat()
    conn = connect(config.ALERTS_DB)
    where = "f.snapshot_date>=?"
    params: list[Any] = [cutoff]
    if layer == "core":
        where += " AND f.observation_layer='core_history' AND f.feature_tier='CORE' AND o.horizon_sessions=?"
        params.append(int(config.MAX_PATTERN_HORIZON_SESSIONS))
    elif layer == "full":
        where += " AND f.observation_layer='daily_close' AND f.is_canonical_daily_close=1 AND o.horizon_sessions=?"
        params.append(int(config.MAX_PATTERN_HORIZON_SESSIONS))
    rows = [dict(row) for row in conn.execute(
        f"""
        SELECT f.*,o.horizon_sessions,o.exit_date,o.exit_price,o.return_long_pct,o.mfe_long_pct,o.mae_long_pct
        FROM feature_observations f
        JOIN observation_outcomes o ON o.observation_id=f.id AND o.complete=1
        WHERE {where}
        ORDER BY f.snapshot_date,f.observed_at_madrid,f.id,o.horizon_sessions
        """,
        params,
    ).fetchall()]
    conn.close()
    return rows



def get_core_feature_history() -> dict[str, dict[str, dict]]:
    """Point-in-time CORE features by ticker/date for historical exit replay."""
    conn = connect(config.ALERTS_DB)
    rows = conn.execute(
        """SELECT ticker,snapshot_date,observed_at_madrid,features_json
           FROM feature_observations
           WHERE observation_layer='core_history' AND feature_tier='CORE'
           ORDER BY ticker,snapshot_date,observed_at_madrid,id"""
    ).fetchall()
    conn.close()
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        try:
            features = json.loads(row["features_json"] or "{}")
        except Exception:
            continue
        out.setdefault(str(row["ticker"]), {})[str(row["snapshot_date"])] = features
    return out


def repair_active_provisional_model() -> int | None:
    """Keep the best safe CORE provisional model active.

    v6.1.11 activated every newly calibrated SHADOW model, so a weaker model
    could replace a materially better one. This migration is idempotent and may
    restore the best historical provisional model using only already persisted
    purged walk-forward metrics. A validated champion is never changed.
    """
    from model_metrics import normalized_oos_metrics

    conn = connect(config.ALERTS_DB)
    current = conn.execute(
        """SELECT mv.* FROM model_state ms
           JOIN model_versions mv ON mv.id=ms.model_version_id
           WHERE ms.state_key='active'"""
    ).fetchone()
    if current and str(current["status"] or "").lower() == "champion":
        model_id = int(current["id"])
        conn.close()
        return model_id

    rows = conn.execute(
        """SELECT * FROM model_versions
           WHERE status IN ('provisional_champion','discovery_shadow','challenger','retired')
           ORDER BY id DESC"""
    ).fetchall()
    candidates: list[tuple[float, int, sqlite3.Row]] = []
    for row in rows:
        try:
            params = json.loads(row["params_json"] or "{}")
            metrics_payload = json.loads(row["metrics_json"] or "{}")
        except Exception:
            continue
        if str(params.get("feature_tier") or "").upper() != "CORE":
            continue
        metrics = normalized_oos_metrics(metrics_payload)
        sample = int(metrics.get("sample") or 0)
        dates = int(metrics.get("market_dates") or 0)
        win = float(metrics.get("win_rate") or 0.0)
        avg = float(metrics.get("avg_return") or 0.0)
        pf = float(metrics.get("profit_factor") or 0.0)
        max_dd = float(metrics.get("max_drawdown") or 999.0)
        safe_dd = float(getattr(config, "PRECISION_MAX_PROMOTION_DRAWDOWN", 6.0))
        if sample < 8 or dates < 4 or win < 50.0 or avg <= 0.0 or pf < 1.0 or max_dd > safe_dd:
            continue
        objective = float(row["objective_score"] or metrics.get("objective") or -999.0)
        candidates.append((objective, int(row["id"]), row))
    if not candidates:
        model_id = int(current["id"]) if current else None
        conn.close()
        return model_id

    _objective, best_id, _best = max(candidates, key=lambda item: (item[0], item[1]))
    current_id = int(current["id"]) if current else None
    if current_id != best_id or str((_best["status"] or "")).lower() != "provisional_champion":
        conn.execute(
            "UPDATE model_versions SET status='retired' WHERE status='provisional_champion' AND id<>?",
            (best_id,),
        )
        conn.execute("UPDATE model_versions SET status='provisional_champion' WHERE id=?", (best_id,))
        conn.execute(
            "INSERT OR REPLACE INTO model_state(state_key,model_version_id,updated_at_utc) VALUES('active',?,?)",
            (best_id, ts_utc_str()),
        )
        conn.commit()
    conn.close()
    return best_id


def get_active_model() -> dict:
    # v6.1.24: resolve the operating scope before any provisional-model repair.
    # A SHADOW override must never mutate model_state, and LIVE must keep the
    # explicitly validated model_state stable. The legacy repair remains active
    # only in ordinary SHADOW operation when no adaptive override is present.
    conn = connect(config.ALERTS_DB)
    today = now_madrid().date().isoformat()
    _ensure_runtime_state_table(conn)
    entry_row = conn.execute("SELECT state_value FROM runtime_state WHERE state_key='entry_mode'").fetchone()
    entry_mode = str(entry_row[0] if entry_row else "shadow").lower()
    override_row = conn.execute("SELECT state_value FROM runtime_state WHERE state_key='adaptive_shadow_model_id'").fetchone()
    try:
        override_id = int(override_row[0] or 0) if override_row else 0
    except Exception:
        override_id = 0
    active_exists = bool(conn.execute("SELECT 1 FROM model_state WHERE state_key='active' AND model_version_id IS NOT NULL").fetchone())
    conn.close()

    # The Adaptive Learning Controller owns every automatic SHADOW switch in
    # v6.1.24. Keep the legacy repair only as a bootstrap fallback (or when the
    # controller is explicitly disabled), otherwise a rollback could be undone
    # immediately by the old objective-only selector.
    adaptive_enabled = bool(getattr(config, "ADAPTIVE_LEARNING_ENABLED", False))
    if entry_mode != "live" and override_id <= 0 and (not adaptive_enabled or not active_exists):
        repair_active_provisional_model()

    conn = connect(config.ALERTS_DB)
    row = None
    adaptive_override = False
    if entry_mode != "live" and override_id > 0:
        row = conn.execute(
            """SELECT * FROM model_versions
               WHERE id=? AND effective_from_date<=?
                 AND status IN ('challenger','discovery_shadow','provisional_champion','retired','champion')""",
            (override_id, today),
        ).fetchone()
        adaptive_override = bool(row)
    if not row:
        row = conn.execute(
            """
            SELECT mv.* FROM model_versions mv
            JOIN model_state ms ON ms.model_version_id=mv.id AND ms.state_key='active'
            WHERE mv.effective_from_date<=?
            """,
            (today,),
        ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM model_versions WHERE status='bootstrap' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {}
    item = dict(row)
    item["params"] = json.loads(item.get("params_json") or "{}")
    item["metrics"] = json.loads(item.get("metrics_json") or "{}")
    item["adaptive_shadow_override"] = adaptive_override
    return item


def get_best_model_for_tier(tier: str, usable_only: bool = True) -> dict | None:
    """Latest model for one independent layer (CORE/FULL/GEX)."""
    tier = str(tier or "").upper()
    conn = connect(config.ALERTS_DB)
    rows = conn.execute("SELECT * FROM model_versions ORDER BY id DESC").fetchall()
    conn.close()
    allowed = {"champion", "scheduled_champion", "provisional_champion", "challenger"}
    for row in rows:
        item = dict(row)
        try:
            params = json.loads(item.get("params_json") or "{}")
        except Exception:
            params = {}
        try:
            metrics = json.loads(item.get("metrics_json") or "{}")
        except Exception:
            metrics = {}
        item_tier = str(params.get("feature_tier") or metrics.get("feature_tier") or "BOOTSTRAP").upper()
        if item_tier != tier:
            continue
        if usable_only and str(item.get("status") or "").lower() not in allowed:
            continue
        item["params"] = params
        item["metrics"] = metrics
        return item
    return None

def current_champion() -> dict | None:
    conn = connect(config.ALERTS_DB)
    row = conn.execute(
        "SELECT * FROM model_versions WHERE status='champion' ORDER BY effective_from_date DESC,id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    item["params"] = json.loads(item.get("params_json") or "{}")
    item["metrics"] = json.loads(item.get("metrics_json") or "{}")
    return item


def insert_model_version(row: dict) -> int:
    conn = connect(config.ALERTS_DB)
    cols = list(row.keys())
    sql = f"INSERT INTO model_versions ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    cur = conn.execute(sql, [row[col] for col in cols])
    conn.commit()
    model_id = int(cur.lastrowid)
    conn.close()
    return model_id


def schedule_model(model_id: int, effective_date: str | None = None) -> None:
    conn = connect(config.ALERTS_DB)
    if effective_date:
        conn.execute(
            "UPDATE model_versions SET status='scheduled_champion', effective_from_date=? WHERE id=?",
            (effective_date, model_id),
        )
    else:
        conn.execute("UPDATE model_versions SET status='scheduled_champion' WHERE id=?", (model_id,))
    conn.commit()
    conn.close()


def activate_due_model() -> int | None:
    # v6.1.24: scheduled LIVE promotion is disabled while controlled adaptive
    # learning is active. Promotion remains SHADOW-only and reversible.
    if bool(getattr(config, "ADAPTIVE_LEARNING_ENABLED", False)):
        return None
    conn = connect(config.ALERTS_DB)
    today = now_madrid().date().isoformat()
    row = conn.execute(
        "SELECT id FROM model_versions WHERE status='scheduled_champion' AND effective_from_date<=? ORDER BY id DESC LIMIT 1",
        (today,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    model_id = int(row["id"])
    conn.execute("UPDATE model_versions SET status='retired' WHERE status='champion' AND id<>?", (model_id,))
    conn.execute("UPDATE model_versions SET status='champion' WHERE id=?", (model_id,))
    conn.execute(
        "INSERT OR REPLACE INTO model_state(state_key,model_version_id,updated_at_utc) VALUES('active',?,?)",
        (model_id, ts_utc_str()),
    )
    conn.commit()
    conn.close()
    return model_id


def activate_provisional_model(model_id: int) -> int:
    """Activate the best backtested CORE policy in shadow mode.

    This never retires a validated champion. It replaces only bootstrap or an
    older provisional model, so the bot uses calibrated parameters from day 0
    without presenting them as validated live performance.
    """
    conn = connect(config.ALERTS_DB)
    current = conn.execute(
        """SELECT mv.id,mv.status FROM model_state ms
           JOIN model_versions mv ON mv.id=ms.model_version_id
           WHERE ms.state_key='active'"""
    ).fetchone()
    if current and str(current["status"] or "").lower() == "champion":
        conn.close()
        return int(current["id"])
    conn.execute(
        "UPDATE model_versions SET status='retired' "
        "WHERE status='provisional_champion' AND id<>?",
        (int(model_id),),
    )
    conn.execute(
        "UPDATE model_versions SET status='provisional_champion' WHERE id=?",
        (int(model_id),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO model_state(state_key,model_version_id,updated_at_utc) "
        "VALUES('active',?,?)",
        (int(model_id), ts_utc_str()),
    )
    conn.commit()
    conn.close()
    return int(model_id)



def create_mover_discovery_run(start_date: str, end_date: str) -> int:
    conn = connect(config.ALERTS_DB)
    cur = conn.execute(
        "INSERT INTO mover_discovery_runs(started_at_utc,training_start_date,training_end_date,status) VALUES(?,?,?,'running')",
        (ts_utc_str(), start_date, end_date),
    )
    conn.commit(); run_id = int(cur.lastrowid); conn.close(); return run_id


def finish_mover_discovery_run(run_id: int, **fields: Any) -> None:
    conn = connect(config.ALERTS_DB)
    fields = {**fields, "finished_at_utc": ts_utc_str()}
    assignments = ",".join(f"{key}=?" for key in fields)
    conn.execute(f"UPDATE mover_discovery_runs SET {assignments} WHERE id=?", [*fields.values(), int(run_id)])
    conn.commit(); conn.close()


def insert_discovered_patterns(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = connect(config.ALERTS_DB)
    payload=[]
    for row in rows:
        payload.append((int(row['discovery_run_id']),row.get('model_version_id'),row['direction'],int(row['pattern_rank']),row['rule_name'],row['conditions_json'],row.get('train_selected'),row.get('train_positive'),row.get('train_control'),row.get('train_precision'),row.get('train_recall'),row.get('train_lift'),row.get('score'),ts_utc_str()))
    conn.executemany(
        """INSERT OR REPLACE INTO discovered_patterns(
             discovery_run_id,model_version_id,direction,pattern_rank,rule_name,conditions_json,
             train_selected,train_positive,train_control,train_precision,train_recall,train_lift,score,created_at_utc
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", payload)
    conn.commit(); conn.close(); return len(payload)


def create_tuning_run(start_date: str, end_date: str) -> int:
    conn = connect(config.ALERTS_DB)
    cur = conn.execute(
        "INSERT INTO tuning_runs(started_at_utc,training_start_date,training_end_date,status) VALUES(?,?,?,'running')",
        (ts_utc_str(), start_date, end_date),
    )
    conn.commit()
    rid = int(cur.lastrowid)
    conn.close()
    return rid


def finish_tuning_run(run_id: int, **fields: Any) -> None:
    conn = connect(config.ALERTS_DB)
    fields = {**fields, "finished_at_utc": ts_utc_str()}
    assignments = ",".join(f"{key}=?" for key in fields)
    conn.execute(f"UPDATE tuning_runs SET {assignments} WHERE id=?", [*fields.values(), run_id])
    conn.commit()
    conn.close()


def cooldown_ok(ticker: str, rule_name: str) -> bool:
    conn = connect(config.ALERTS_DB)
    row = conn.execute(
        "SELECT last_event_at_madrid FROM alert_state WHERE ticker=? AND rule_name=?", (ticker, rule_name)
    ).fetchone()
    conn.close()
    if not row or not row["last_event_at_madrid"]:
        return True
    try:
        return now_madrid() - datetime.fromisoformat(row["last_event_at_madrid"]) >= timedelta(hours=config.ALERT_COOLDOWN_HOURS)
    except Exception:
        return True


def daily_alert_count(snapshot_date: str, mode: str = "all") -> int:
    """Count operative alerts already created for one market session."""
    conn = connect(config.ALERTS_DB)
    where = "snapshot_date=? AND status NOT IN ('archived','ignored')"
    params: list[Any] = [snapshot_date]
    if mode == "live":
        where += " AND status NOT LIKE 'shadow%'"
    elif mode == "shadow":
        where += " AND status LIKE 'shadow%'"
    count = conn.execute(f"SELECT COUNT(*) FROM alert_events WHERE {where}", params).fetchone()[0]
    conn.close()
    return int(count or 0)


def has_open_trade(ticker: str, live_only: bool = False) -> bool:
    conn = connect(config.ALERTS_DB)
    if live_only:
        count = conn.execute(
            """SELECT COUNT(*) FROM open_trades ot
               JOIN alert_events ae ON ae.id=ot.alert_event_id
               WHERE ot.ticker=? AND ot.status='open' AND ae.status IN ('open','sent')""",
            (ticker,),
        ).fetchone()[0]
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM open_trades WHERE ticker=? AND status='open'",
            (ticker,),
        ).fetchone()[0]
    conn.close()
    return int(count) >= config.MAX_OPEN_TRADES_PER_TICKER


def insert_alert_event(event: dict) -> int | None:
    conn = connect(config.ALERTS_DB)
    cols = list(event.keys())
    sql = f"INSERT OR IGNORE INTO alert_events ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    cur = conn.execute(sql, [event[col] for col in cols])
    event_id = int(cur.lastrowid) if cur.rowcount > 0 else None
    if event_id:
        conn.execute(
            """INSERT INTO alert_state(ticker,rule_name,last_event_at_madrid,last_price,last_score,status,updated_at_utc)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(ticker,rule_name) DO UPDATE SET
                 last_event_at_madrid=excluded.last_event_at_madrid,last_price=excluded.last_price,
                 last_score=excluded.last_score,status=excluded.status,updated_at_utc=excluded.updated_at_utc""",
            (
                event["ticker"], event["rule_name"], event["event_at_madrid"], event["current_price"],
                event["score"], event["status"], ts_utc_str(),
            ),
        )
        conn.execute(
            """INSERT INTO open_trades(
                alert_event_id,model_version_id,ticker,direction,entry_at_madrid,entry_date,entry_price,
                target_pct,stop_pct,max_hold_sessions,status,last_price,last_seen_date,updated_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'open',?,?,?)""",
            (
                event_id, event.get("model_version_id"), event["ticker"], event["direction"],
                event["event_at_madrid"], event["snapshot_date"], event["current_price"],
                event["target_pct"], event["stop_pct"], event["max_hold_sessions"],
                event["current_price"], event["snapshot_date"], ts_utc_str(),
            ),
        )
    conn.commit()
    conn.close()
    return event_id


def mark_alert_sent(event_id: int, message: str) -> None:
    conn = connect(config.ALERTS_DB)
    conn.execute(
        "UPDATE alert_events SET sent_to_discord=1,discord_message=?,status='sent' WHERE id=?", (message, event_id)
    )
    conn.commit()
    conn.close()


def mark_research_alert_sent(event_id: int, message: str) -> None:
    conn = connect(config.ALERTS_DB)
    conn.execute(
        "UPDATE alert_events SET sent_to_discord=1,discord_message=?,status='shadow_sent' WHERE id=?",
        (message, event_id),
    )
    conn.commit()
    conn.close()


def get_pending_exit_deliveries(limit: int = 20, max_age_days: int = 7) -> list[dict]:
    """Closed lifecycle events whose Discord CLOSE has not been confirmed yet."""
    conn = connect(config.ALERTS_DB)
    try:
        rows = [dict(row) for row in conn.execute(
            """SELECT ot.*,ae.sent_to_discord,ae.rule_name,ae.score,ae.status AS alert_status,
                      ae.features_json AS entry_features_json,mv.model_key
               FROM open_trades ot
               JOIN alert_events ae ON ae.id=ot.alert_event_id
               LEFT JOIN model_versions mv ON mv.id=ot.model_version_id
               WHERE ot.status='closed'
                 AND COALESCE(ot.sent_exit_to_discord,0)=0
                 AND ot.exit_at_madrid IS NOT NULL
                 AND date(ot.exit_date)>=date('now', ?)
               ORDER BY ot.exit_at_madrid ASC
               LIMIT ?""",
            (f"-{max(1, int(max_age_days))} day", max(1, int(limit))),
        ).fetchall()]
        return rows
    finally:
        conn.close()


def get_open_trades() -> list[dict]:
    conn = connect(config.ALERTS_DB)
    rows = [dict(row) for row in conn.execute(
        """SELECT ot.*,ae.sent_to_discord,ae.rule_name,ae.score,ae.status AS alert_status,
                  ae.features_json AS entry_features_json,mv.model_key
           FROM open_trades ot
           JOIN alert_events ae ON ae.id=ot.alert_event_id
           LEFT JOIN model_versions mv ON mv.id=ot.model_version_id
           WHERE ot.status='open' ORDER BY ot.entry_at_madrid"""
    ).fetchall()]
    conn.close()
    return rows


def update_open_trade(trade_id: int, **fields: Any) -> None:
    conn = connect(config.ALERTS_DB)
    fields = {**fields, "updated_at_utc": ts_utc_str()}
    assignments = ",".join(f"{key}=?" for key in fields)
    conn.execute(f"UPDATE open_trades SET {assignments} WHERE id=?", [*fields.values(), trade_id])
    conn.commit()
    conn.close()


def mark_exit_sent(trade_id: int) -> None:
    update_open_trade(trade_id, sent_exit_to_discord=1)


def archive_imported(archive_name: str) -> bool:
    conn = connect(config.ALERTS_DB)
    row = conn.execute("SELECT 1 FROM archive_imports WHERE archive_name=?", (archive_name,)).fetchone()
    conn.close()
    return row is not None


def mark_archive_imported(archive_name: str, feature_rows: int, price_rows: int, note: str = "") -> None:
    conn = connect(config.ALERTS_DB)
    conn.execute(
        "INSERT OR REPLACE INTO archive_imports(archive_name,imported_at_utc,feature_rows,price_rows,note) VALUES(?,?,?,?,?)",
        (archive_name, ts_utc_str(), feature_rows, price_rows, note),
    )
    conn.commit(); conn.close()


def purge_research_retention() -> dict[str, int]:
    daily_cutoff = (now_madrid().date() - timedelta(days=config.FEATURE_RETENTION_DAYS)).isoformat()
    intraday_cutoff = (now_madrid().date() - timedelta(days=config.INTRADAY_LIVE_RETENTION_DAYS)).isoformat()
    conn = connect(config.ALERTS_DB)
    counts: dict[str, int] = {}
    stale_ids = [int(row[0]) for row in conn.execute(
        """SELECT id FROM feature_observations
           WHERE (observation_layer='daily_close' AND snapshot_date<?)
              OR (observation_layer='intraday' AND source_archive IS NULL AND snapshot_date<?)""",
        (daily_cutoff, intraday_cutoff),
    ).fetchall()]
    if stale_ids:
        ph = ",".join(["?"] * len(stale_ids))
        cur = conn.execute(f"DELETE FROM observation_outcomes WHERE observation_id IN ({ph})", stale_ids)
        counts["outcomes"] = max(0, int(cur.rowcount or 0))
        cur = conn.execute(f"DELETE FROM feature_observations WHERE id IN ({ph})", stale_ids)
        counts["observations"] = max(0, int(cur.rowcount or 0))
    else:
        counts["outcomes"] = counts["observations"] = 0
    cur = conn.execute("DELETE FROM price_cache WHERE trade_date<?", (daily_cutoff,))
    counts["prices"] = max(0, int(cur.rowcount or 0))
    conn.commit(); conn.close()
    return counts


def scanner_run_by_id(run_id: int) -> dict | None:
    if not config.SCANNER_DB.exists():
        return None
    conn = connect(config.SCANNER_DB, readonly=True)
    row = conn.execute(
        """SELECT r.*, (SELECT COUNT(*) FROM scanner_snapshots s WHERE s.scanner_run_id=r.id) AS snapshot_count
           FROM scanner_runs r WHERE r.id=?""",
        (run_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def successful_scanner_runs_since(cutoff_date: str, limit: int = 500) -> list[dict]:
    if not config.SCANNER_DB.exists():
        return []
    conn = connect(config.SCANNER_DB, readonly=True)
    rows = [dict(row) for row in conn.execute(
        """SELECT r.*, (SELECT COUNT(*) FROM scanner_snapshots s WHERE s.scanner_run_id=r.id) AS snapshot_count
           FROM scanner_runs r
           WHERE r.status='success' AND r.snapshot_date>=?
           ORDER BY r.captured_at_madrid ASC, r.id ASC
           LIMIT ?""",
        (cutoff_date, int(limit)),
    ).fetchall()]
    conn.close()
    return rows


def canonical_deep_run_inventory_since(cutoff_date: str, limit: int = 500) -> list[dict]:
    """Latest successful deep run per market date with the complete Scanner row count.

    Data Bot's canonical daily science uses one deep close per session. When more
    than one deep run exists for the same date, only the latest successful run is
    canonical. This inventory is used to repair old partial Alerts materializations.
    """
    runs = successful_scanner_runs_since(cutoff_date, limit)
    latest_by_date: dict[str, dict] = {}
    for run in runs:
        session = str(run.get("session_type") or "").lower()
        if "deep" not in session:
            continue
        trade_date = str(run.get("snapshot_date") or "")
        if not trade_date:
            continue
        previous = latest_by_date.get(trade_date)
        current_key = (str(run.get("captured_at_madrid") or ""), int(run.get("id") or 0))
        previous_key = (str((previous or {}).get("captured_at_madrid") or ""), int((previous or {}).get("id") or 0))
        if previous is None or current_key >= previous_key:
            latest_by_date[trade_date] = dict(run)
    out: list[dict] = []
    for trade_date in sorted(latest_by_date):
        run = latest_by_date[trade_date]
        out.append({
            "run_id": int(run.get("id") or 0),
            "snapshot_date": trade_date,
            "session_type": run.get("session_type"),
            "captured_at_madrid": run.get("captured_at_madrid"),
            "expected_rows": int(run.get("snapshot_count") or run.get("base_rows_saved") or 0),
        })
    return out


def canonical_observation_counts_by_run() -> dict[int, dict]:
    try:
        from research_snapshot import research_db_path
        source = research_db_path(config.ALERTS_DB)
    except Exception:
        source = config.ALERTS_DB
    conn = connect(source)
    rows = conn.execute(
        """SELECT source_scanner_run_id,snapshot_date,COUNT(*) AS rows,
                  SUM(CASE WHEN has_gex=1 THEN 1 ELSE 0 END) AS gex_rows
           FROM feature_observations
           WHERE observation_layer='daily_close' AND is_canonical_daily_close=1
           GROUP BY source_scanner_run_id,snapshot_date"""
    ).fetchall()
    conn.close()
    return {
        int(row["source_scanner_run_id"]): {
            "snapshot_date": str(row["snapshot_date"] or ""),
            "actual_rows": int(row["rows"] or 0),
            "gex_rows": int(row["gex_rows"] or 0),
        }
        for row in rows if row["source_scanner_run_id"] is not None
    }


def canonical_materialization_reconciliation(cutoff_date: str, limit: int = 500) -> dict:
    expected_runs = canonical_deep_run_inventory_since(cutoff_date, limit)
    actual_by_run = canonical_observation_counts_by_run()
    details: list[dict] = []
    for item in expected_runs:
        actual = actual_by_run.get(int(item["run_id"]), {})
        actual_rows = int(actual.get("actual_rows") or 0)
        expected_rows = int(item.get("expected_rows") or 0)
        details.append({
            **item,
            "actual_rows": actual_rows,
            "gex_rows": int(actual.get("gex_rows") or 0),
            "missing_rows": max(0, expected_rows - actual_rows),
            "complete": bool(expected_rows > 0 and actual_rows >= expected_rows),
        })
    return {
        "expected_dates": len(expected_runs),
        "expected_rows": sum(int(x.get("expected_rows") or 0) for x in details),
        "actual_rows": sum(min(int(x.get("actual_rows") or 0), int(x.get("expected_rows") or 0)) for x in details),
        "gex_rows": sum(int(x.get("gex_rows") or 0) for x in details),
        "missing_rows": sum(int(x.get("missing_rows") or 0) for x in details),
        "incomplete_runs": [x for x in details if not x.get("complete")],
        "details": details,
    }


def processed_scanner_run_ids() -> set[int]:
    conn = connect(config.ALERTS_DB)
    rows = conn.execute("SELECT DISTINCT source_scanner_run_id FROM feature_observations").fetchall()
    conn.close()
    return {int(row[0]) for row in rows if row[0] is not None}


def core_feature_date_inventory() -> list[dict]:
    """Return the materialised CORE calendar with per-date coverage.

    Reconciliation must compare the actual source session set with the actual
    SQLite session set. Aggregate counters/end-date metadata can drift when an
    older cache contains additional administrative dates, even though every
    required market session is present.
    """
    conn = connect(config.ALERTS_DB)
    try:
        if "feature_observations" not in _table_names(conn):
            return []
        rows = conn.execute(
            """SELECT snapshot_date, COUNT(*) AS rows, COUNT(DISTINCT ticker) AS tickers
               FROM feature_observations
               WHERE observation_layer='core_history' AND feature_tier='CORE'
               GROUP BY snapshot_date ORDER BY snapshot_date"""
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "date": str(row["snapshot_date"] or ""),
            "rows": int(row["rows"] or 0),
            "tickers": int(row["tickers"] or 0),
        }
        for row in rows
        if str(row["snapshot_date"] or "")
    ]


def history_coverage() -> dict:
    conn = connect(config.ALERTS_DB)
    feature = conn.execute(
        """SELECT COUNT(*) AS rows, COUNT(DISTINCT snapshot_date) AS dates,
                  MIN(snapshot_date) AS start_date, MAX(snapshot_date) AS end_date,
                  COUNT(DISTINCT source_scanner_run_id) AS runs
           FROM feature_observations
           WHERE observation_layer='daily_close' AND is_canonical_daily_close=1"""
    ).fetchone()
    core = conn.execute(
        """SELECT COUNT(*) AS rows, COUNT(DISTINCT snapshot_date) AS dates,
                  MIN(snapshot_date) AS start_date, MAX(snapshot_date) AS end_date,
                  COUNT(DISTINCT ticker) AS tickers
           FROM feature_observations
           WHERE observation_layer='core_history' AND feature_tier='CORE'"""
    ).fetchone()
    core_outcomes = conn.execute(
        """SELECT COUNT(*) AS rows, COUNT(DISTINCT f.snapshot_date) AS dates,
                  MIN(f.snapshot_date) AS start_date, MAX(f.snapshot_date) AS end_date
           FROM observation_outcomes o
           JOIN feature_observations f ON f.id=o.observation_id
           WHERE o.complete=1 AND o.horizon_sessions=?
             AND f.observation_layer='core_history' AND f.feature_tier='CORE'""",
        (int(config.MAX_PATTERN_HORIZON_SESSIONS),),
    ).fetchone()
    intraday = conn.execute(
        """SELECT COUNT(*) AS rows,COUNT(DISTINCT snapshot_date) AS dates,
                  COUNT(DISTINCT source_scanner_run_id) AS runs
           FROM feature_observations WHERE observation_layer='intraday'"""
    ).fetchone()
    outcomes = conn.execute(
        """SELECT COUNT(*) AS rows, COUNT(DISTINCT f.snapshot_date) AS dates,
                  MIN(f.snapshot_date) AS start_date, MAX(f.snapshot_date) AS end_date
           FROM observation_outcomes o
           JOIN feature_observations f ON f.id=o.observation_id
           WHERE o.complete=1 AND o.horizon_sessions=?
             AND f.observation_layer='daily_close' AND f.is_canonical_daily_close=1""",
        (int(config.MAX_PATTERN_HORIZON_SESSIONS),),
    ).fetchone()
    archives = conn.execute(
        """SELECT COUNT(*) AS zips, COALESCE(SUM(feature_rows),0) AS features,
                  COALESCE(SUM(price_rows),0) AS prices,
                  SUM(CASE WHEN archive_name LIKE 'intraday/%' THEN 1 ELSE 0 END) AS intraday_zips
           FROM archive_imports"""
    ).fetchone()
    conn.close()
    return {
        "core_feature_rows": int(core["rows"] or 0),
        "core_feature_dates": int(core["dates"] or 0),
        "core_feature_tickers": int(core["tickers"] or 0),
        "core_feature_start_date": core["start_date"],
        "core_feature_end_date": core["end_date"],
        "core_history_target": int(config.CORE_HISTORY_TARGET_SESSIONS),
        "core_history_ready": int(core["dates"] or 0) >= int(config.CORE_HISTORY_TARGET_SESSIONS),
        "core_outcome_rows": int(core_outcomes["rows"] or 0),
        "core_outcome_dates": int(core_outcomes["dates"] or 0),
        "core_outcome_start_date": core_outcomes["start_date"],
        "core_outcome_end_date": core_outcomes["end_date"],
        "feature_rows": int(feature["rows"] or 0),
        "feature_dates": int(feature["dates"] or 0),
        "feature_runs": int(feature["runs"] or 0),
        "feature_start_date": feature["start_date"],
        "feature_end_date": feature["end_date"],
        "daily_close_target": int(config.DAILY_CLOSE_WINDOW_SESSIONS),
        "daily_close_ready": int(feature["dates"] or 0) >= int(config.DAILY_CLOSE_WINDOW_SESSIONS),
        "intraday_rows": int(intraday["rows"] or 0),
        "intraday_dates": int(intraday["dates"] or 0),
        "intraday_runs": int(intraday["runs"] or 0),
        "outcome_rows": int(outcomes["rows"] or 0),
        "outcome_dates": int(outcomes["dates"] or 0),
        "outcome_start_date": outcomes["start_date"],
        "outcome_end_date": outcomes["end_date"],
        "archive_zips": int(archives["zips"] or 0),
        "intraday_archive_zips": int(archives["intraday_zips"] or 0),
        "archive_features": int(archives["features"] or 0),
        "archive_prices": int(archives["prices"] or 0),
    }
