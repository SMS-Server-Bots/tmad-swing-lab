import json
import sqlite3
import hashlib
import config as cfg
import zlib
from datetime import timedelta
from pathlib import Path
from typing import Any

from config import SCANNER_DB, GEX_DB, RAW_DB, META_DB, LIVE_RETENTION_DAYS, RAW_COMPRESS, CLEAN_INVALID_RAW_PAYLOADS, CLEAN_INVALID_EXTRA_FIELDS, VACUUM_AFTER_INVALID_CLEANUP
from time_utils import ts_utc_str, ts_madrid_str, now_madrid, market_date_from_madrid_iso
from utils import json_dumps, payload_hash, value_type


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.row_factory = sqlite3.Row
    return conn


def scanner_conn(): return connect(SCANNER_DB)
def gex_conn(): return connect(GEX_DB)
def raw_conn(): return connect(RAW_DB)
def meta_conn(): return connect(META_DB)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str):
    cols = _table_columns(conn, table)
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: dict[str, str]):
    for col, ddl_type in cols.items():
        _ensure_column(conn, table, col, ddl_type)


def ensure_all_schemas():
    ensure_scanner_schema(); ensure_gex_schema(); ensure_raw_schema(); ensure_meta_schema()


def ensure_scanner_schema():
    conn = scanner_conn(); cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS scanner_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        run_source TEXT,
        status TEXT NOT NULL,
        total_tickers_expected INTEGER DEFAULT 0,
        total_tickers_ok INTEGER DEFAULT 0,
        total_tickers_failed INTEGER DEFAULT 0,
        scanner_date TEXT,
        last_sync_timestamp TEXT,
        last_sync_type TEXT,
        duration_seconds REAL,
        error_message TEXT,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        base_rows_saved INTEGER DEFAULT 0,
        base_unique_tickers INTEGER DEFAULT 0,
        enriched_ok INTEGER DEFAULT 0,
        enriched_failed INTEGER DEFAULT 0,
        completion_stage TEXT,
        previous_scanner_run_id INTEGER,
        scanner_changed_tickers INTEGER DEFAULT 0,
        scanner_new_tickers INTEGER DEFAULT 0,
        scanner_removed_tickers INTEGER DEFAULT 0,
        scanner_unchanged_tickers INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS scanner_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanner_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        name TEXT,
        category TEXT,
        side TEXT,
        current_price REAL,
        change_percent REAL,
        option_signal TEXT,
        unusual_flow_signal TEXT,
        total_gex REAL,
        gex_change REAL,
        gamma_flip REAL,
        max_pos_strike REAL,
        max_pos_gex REAL,
        max_neg_strike REAL,
        max_neg_gex REAL,
        max_delta_gex_strike REAL,
        max_delta_gex_value REAL,
        nearest_supply REAL,
        nearest_demand REAL,
        zone_scope TEXT,
        vwap REAL,
        rsi REAL,
        rvol REAL,
        detail_status TEXT,
        intraday_status TEXT,
        gex_status TEXT,
        error_message TEXT,
        raw_scanner_hash TEXT,
        raw_detail_hash TEXT,
        raw_intraday_hash TEXT,
        raw_gex_hash TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(scanner_run_id, ticker)
    );
    CREATE TABLE IF NOT EXISTS scanner_snapshot_deltas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanner_run_id INTEGER NOT NULL,
        previous_scanner_run_id INTEGER,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        change_kind TEXT NOT NULL,
        meaningful_change INTEGER NOT NULL DEFAULT 0,
        change_score REAL DEFAULT 0,
        changed_fields_json TEXT,
        field_deltas_json TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(scanner_run_id, ticker)
    );
    CREATE TABLE IF NOT EXISTS ticker_daily_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        session_type TEXT,
        side TEXT,
        name TEXT,
        category TEXT,
        current_price REAL,
        change_percent REAL,
        option_signal TEXT,
        unusual_flow_signal TEXT,
        total_gex REAL,
        gex_change REAL,
        gamma_flip REAL,
        max_pos_strike REAL,
        max_pos_gex REAL,
        max_neg_strike REAL,
        max_neg_gex REAL,
        max_delta_gex_strike REAL,
        max_delta_gex_value REAL,
        nearest_supply REAL,
        nearest_demand REAL,
        zone_scope TEXT,
        vwap REAL,
        rsi REAL,
        rvol REAL,
        last_run_id INTEGER,
        updated_at_utc TEXT NOT NULL,
        updated_at_madrid TEXT NOT NULL,
        UNIQUE(ticker, trade_date)
    );
    CREATE TABLE IF NOT EXISTS data_quality_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_at_utc TEXT NOT NULL,
        event_at_madrid TEXT NOT NULL,
        scope TEXT NOT NULL,
        severity TEXT NOT NULL,
        scanner_run_id INTEGER,
        ticker TEXT,
        message TEXT,
        metadata_json TEXT
    );

    CREATE TABLE IF NOT EXISTS ticker_detail_zones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanner_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        timeframe TEXT,
        zone_type TEXT,
        top REAL,
        bottom REAL,
        strength INTEGER,
        raw_detail_hash TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(scanner_run_id, ticker, timeframe, zone_type, top, bottom)
    );
    CREATE TABLE IF NOT EXISTS ticker_detail_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanner_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        history_date TEXT,
        signal TEXT,
        trend TEXT,
        momentum_score REAL,
        iv_z_spread REAL,
        raw_detail_hash TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(scanner_run_id, ticker, history_date, signal, trend)
    );
\n    CREATE TABLE IF NOT EXISTS ticker_price_history (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        scanner_run_id INTEGER NOT NULL,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        session_type TEXT,\n        ticker TEXT NOT NULL,\n        price_date TEXT NOT NULL,\n        open REAL,\n        high REAL,\n        low REAL,\n        close REAL,\n        price REAL,\n        volume REAL,\n        raw_detail_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(scanner_run_id, ticker, price_date)\n    );\n\n    CREATE TABLE IF NOT EXISTS score_runs (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        session_type TEXT,\n        status TEXT NOT NULL,\n        top_longs_count INTEGER DEFAULT 0,\n        basket_days_count INTEGER DEFAULT 0,\n        basket_positions_count INTEGER DEFAULT 0,\n        error_message TEXT,\n        created_at_utc TEXT NOT NULL,\n        updated_at_utc TEXT NOT NULL\n    );\n    CREATE TABLE IF NOT EXISTS score_top_longs (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        as_of TEXT,\n        market_is_open INTEGER,\n        locked INTEGER,\n        total_picks INTEGER,\n        universe_size INTEGER,\n        rank INTEGER,\n        ticker TEXT NOT NULL,\n        name TEXT,\n        current_price REAL,\n        change_percent REAL,\n        long_score REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id, ticker)\n    );\n    CREATE TABLE IF NOT EXISTS score_backtest_metrics (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        window_start TEXT,\n        window_end TEXT,\n        live_score_version TEXT,\n        total_return REAL,\n        cagr REAL,\n        sharpe REAL,\n        max_drawdown REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id)\n    );\n    CREATE TABLE IF NOT EXISTS score_backtest_equity_curve (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        curve_date TEXT NOT NULL,\n        portfolio REAL,\n        spy REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id, curve_date)\n    );\n    CREATE TABLE IF NOT EXISTS score_backtest_monte_carlo (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        curve_date TEXT NOT NULL,\n        actual REAL,\n        p5 REAL, p25 REAL, p50 REAL, p75 REAL, p95 REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id, curve_date)\n    );\n    CREATE TABLE IF NOT EXISTS score_backtest_bootstrap (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        metric TEXT NOT NULL,\n        actual REAL, median REAL, p5 REAL, p25 REAL, p75 REAL, p95 REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id, metric)\n    );\n    CREATE TABLE IF NOT EXISTS score_basket_history_summary (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        as_of TEXT,\n        window_days INTEGER,\n        avg_basket_return REAL,\n        avg_spy_return REAL,\n        avg_alpha REAL,\n        hit_rate REAL,\n        alpha_hit_rate REAL,\n        settled_days INTEGER,\n        pending_days INTEGER,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id)\n    );\n    CREATE TABLE IF NOT EXISTS score_basket_history_days (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        basket_date TEXT NOT NULL,\n        basket_return REAL,\n        spy_return REAL,\n        alpha REAL,\n        is_pending INTEGER,\n        settled_count INTEGER,\n        total_count INTEGER,\n        rolling_avg_30 REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id, basket_date)\n    );\n    CREATE TABLE IF NOT EXISTS score_basket_history_positions (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        score_run_id INTEGER NOT NULL,\n        basket_date TEXT NOT NULL,\n        ticker TEXT NOT NULL,\n        name TEXT,\n        rank INTEGER,\n        score REAL,\n        entry_price REAL,\n        exit_price REAL,\n        marked_return REAL,\n        forward_return REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(score_run_id, basket_date, ticker)\n    );\n\n    CREATE TABLE IF NOT EXISTS watchlist_runs (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        session_type TEXT,\n        status TEXT NOT NULL,\n        watchlists_count INTEGER DEFAULT 0,\n        dashboards_count INTEGER DEFAULT 0,\n        rows_count INTEGER DEFAULT 0,\n        error_message TEXT,\n        created_at_utc TEXT NOT NULL,\n        updated_at_utc TEXT NOT NULL\n    );\n    CREATE TABLE IF NOT EXISTS watchlists (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        watchlist_run_id INTEGER NOT NULL,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        watchlist_id TEXT NOT NULL,\n        name TEXT,\n        description TEXT,\n        icon TEXT,\n        color TEXT,\n        symbol_count INTEGER,\n        is_default INTEGER,\n        is_shared INTEGER,\n        sort_order INTEGER,\n        created_at TEXT,\n        updated_at TEXT,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(watchlist_run_id, watchlist_id)\n    );\n    CREATE TABLE IF NOT EXISTS watchlist_sections (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        watchlist_run_id INTEGER NOT NULL,\n        watchlist_id TEXT NOT NULL,\n        section_index INTEGER,\n        section_name TEXT,\n        section_json TEXT,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(watchlist_run_id, watchlist_id, section_index)\n    );\n    CREATE TABLE IF NOT EXISTS watchlist_dashboard_rows (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        watchlist_run_id INTEGER NOT NULL,\n        captured_at_utc TEXT NOT NULL,\n        captured_at_madrid TEXT NOT NULL,\n        snapshot_date TEXT NOT NULL,\n        watchlist_id TEXT NOT NULL,\n        watchlist_name TEXT,\n        dashboard_date TEXT,\n        ticker TEXT NOT NULL,\n        company_name TEXT,\n        category TEXT,\n        current_price REAL,\n        change_percent REAL,\n        option_signal TEXT,\n        unusual_flow_signal TEXT,\n        total_gex REAL,\n        previous_total_gex REAL,\n        iv_z_spread REAL,\n        previous_iv_z_spread REAL,\n        momentum_score REAL,\n        trend TEXT,\n        long_score REAL,\n        long_score_signals INTEGER,\n        otm_call_volume_z REAL,\n        otm_put_volume_z REAL,\n        rvol REAL,\n        daily_demand_range TEXT,\n        daily_supply_range TEXT,\n        weekly_demand_range TEXT,\n        weekly_supply_range TEXT,\n        sparkline_demand_level REAL,\n        sparkline_supply_level REAL,\n        is_starred INTEGER,\n        sparkline_json TEXT,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(watchlist_run_id, watchlist_id, ticker)\n    );\n    CREATE TABLE IF NOT EXISTS watchlist_sparklines (\n        id INTEGER PRIMARY KEY AUTOINCREMENT,\n        watchlist_run_id INTEGER NOT NULL,\n        watchlist_id TEXT NOT NULL,\n        ticker TEXT NOT NULL,\n        sparkline_json TEXT,\n        demand_level REAL,\n        supply_level REAL,\n        raw_hash TEXT,\n        created_at_utc TEXT NOT NULL,\n        UNIQUE(watchlist_run_id, watchlist_id, ticker)\n    );\n
    CREATE INDEX IF NOT EXISTS idx_scanner_runs_time ON scanner_runs(captured_at_madrid);
    CREATE INDEX IF NOT EXISTS idx_scanner_runs_date ON scanner_runs(snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_scanner_snapshots_ticker_date ON scanner_snapshots(ticker, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_scanner_snapshots_run ON scanner_snapshots(scanner_run_id);
    CREATE INDEX IF NOT EXISTS idx_scanner_snapshots_signal ON scanner_snapshots(option_signal, unusual_flow_signal);
    CREATE INDEX IF NOT EXISTS idx_scanner_snapshot_deltas_run ON scanner_snapshot_deltas(scanner_run_id);
    CREATE INDEX IF NOT EXISTS idx_scanner_snapshot_deltas_ticker ON scanner_snapshot_deltas(ticker, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_ticker_daily_history_ticker_date ON ticker_daily_history(ticker, trade_date);
    CREATE VIEW IF NOT EXISTS scanner_snapshots_success AS
        SELECT ss.* FROM scanner_snapshots ss
        JOIN scanner_runs sr ON sr.id = ss.scanner_run_id
        WHERE sr.status = 'success';
    CREATE VIEW IF NOT EXISTS ticker_daily_history_latest AS
        SELECT * FROM ticker_daily_history;
    ''')
    # Migraciones idempotentes: permite actualizar DBs existentes sin borrarlas.
    _ensure_columns(conn, "scanner_runs", {
        "base_rows_saved": "INTEGER DEFAULT 0",
        "base_unique_tickers": "INTEGER DEFAULT 0",
        "enriched_ok": "INTEGER DEFAULT 0",
        "enriched_failed": "INTEGER DEFAULT 0",
        "completion_stage": "TEXT",
        "previous_scanner_run_id": "INTEGER",
        "scanner_changed_tickers": "INTEGER DEFAULT 0",
        "scanner_new_tickers": "INTEGER DEFAULT 0",
        "scanner_removed_tickers": "INTEGER DEFAULT 0",
        "scanner_unchanged_tickers": "INTEGER DEFAULT 0"
    })
    _ensure_columns(conn, "scanner_snapshots", {
        "signals_available": "INTEGER",
        "trend": "TEXT",
        "momentum_score": "REAL",
        "rsi14": "REAL",
        "iv_z_spread": "REAL",
        "iv_change": "REAL",
        "otm_call_volume_z": "REAL",
        "otm_put_volume_z": "REAL",
        "pc_ratio": "REAL",
        "long_score": "REAL",
        "long_score_signals": "INTEGER",
        "price_change": "REAL",
        "is_live_price": "INTEGER",
        "put_call_ratio": "REAL",
        "detail_iv_z_spread": "REAL",
        "detail_momentum_score": "REAL",
        "detail_trend": "TEXT",
        "daily_supply_top": "REAL",
        "daily_supply_bottom": "REAL",
        "daily_supply_strength": "INTEGER",
        "daily_demand_top": "REAL",
        "daily_demand_bottom": "REAL",
        "daily_demand_strength": "INTEGER",
        "weekly_supply_top": "REAL",
        "weekly_supply_bottom": "REAL",
        "weekly_supply_strength": "INTEGER",
        "weekly_demand_top": "REAL",
        "weekly_demand_bottom": "REAL",
        "weekly_demand_strength": "INTEGER"
    })
    _ensure_columns(conn, "ticker_daily_history", {
        "signals_available": "INTEGER",
        "trend": "TEXT",
        "momentum_score": "REAL",
        "rsi14": "REAL",
        "iv_z_spread": "REAL",
        "iv_change": "REAL",
        "otm_call_volume_z": "REAL",
        "otm_put_volume_z": "REAL",
        "pc_ratio": "REAL",
        "long_score": "REAL",
        "long_score_signals": "INTEGER",
        "price_change": "REAL",
        "is_live_price": "INTEGER",
        "put_call_ratio": "REAL",
        "detail_iv_z_spread": "REAL",
        "detail_momentum_score": "REAL",
        "detail_trend": "TEXT",
        "daily_supply_top": "REAL",
        "daily_supply_bottom": "REAL",
        "daily_supply_strength": "INTEGER",
        "daily_demand_top": "REAL",
        "daily_demand_bottom": "REAL",
        "daily_demand_strength": "INTEGER",
        "weekly_supply_top": "REAL",
        "weekly_supply_bottom": "REAL",
        "weekly_supply_strength": "INTEGER",
        "weekly_demand_top": "REAL",
        "weekly_demand_bottom": "REAL",
        "weekly_demand_strength": "INTEGER"
    })
    wall_cols = {
        "put_wall": "REAL",
        "call_wall": "REAL",
        "put_wall_pct": "REAL",
        "call_wall_pct": "REAL",
        "put_wall_gex": "REAL",
        "call_wall_gex": "REAL",
        "put_wall_strength": "REAL",
        "call_wall_strength": "REAL",
        "wall_range_pct": "REAL",
        "wall_band_json": "TEXT"
    }
    _ensure_columns(conn, "scanner_snapshots", wall_cols)
    _ensure_columns(conn, "ticker_daily_history", wall_cols)

    _ensure_columns(conn, "ticker_detail_history", {
        "total_gex": "REAL",
        "change_percent": "REAL",
        "close_price": "REAL",
        "unusual_flow_signal": "TEXT",
        "is_live": "INTEGER"
    })
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS ticker_ohlc_daily (
        ticker TEXT NOT NULL,
        price_date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        price REAL,
        volume REAL,
        adjusted_close REAL,
        split_factor REAL,
        adjustment_json TEXT,
        source_scanner_run_id INTEGER,
        raw_detail_hash TEXT,
        updated_at_utc TEXT NOT NULL,
        updated_at_madrid TEXT NOT NULL,
        PRIMARY KEY (ticker, price_date)
    );
    CREATE TABLE IF NOT EXISTS ticker_signal_daily (
        ticker TEXT NOT NULL,
        history_date TEXT NOT NULL,
        signal TEXT,
        trend TEXT,
        momentum_score REAL,
        iv_z_spread REAL,
        total_gex REAL,
        change_percent REAL,
        close_price REAL,
        unusual_flow_signal TEXT,
        is_live INTEGER,
        source_scanner_run_id INTEGER,
        raw_detail_hash TEXT,
        updated_at_utc TEXT NOT NULL,
        updated_at_madrid TEXT NOT NULL,
        PRIMARY KEY (ticker, history_date)
    );
    CREATE TABLE IF NOT EXISTS core_bootstrap_ticker_state (
        ticker TEXT PRIMARY KEY,
        universe_fingerprint TEXT NOT NULL,
        window_fingerprint TEXT NOT NULL,
        universe_run_id INTEGER,
        universe_date TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        successful_payloads INTEGER NOT NULL DEFAULT 0,
        overlap_dates INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        exclusion_reason TEXT,
        last_attempt_at_utc TEXT,
        updated_at_utc TEXT NOT NULL,
        updated_at_madrid TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_core_bootstrap_state_window
      ON core_bootstrap_ticker_state(universe_fingerprint,window_fingerprint,status,attempts,last_attempt_at_utc);
    CREATE INDEX IF NOT EXISTS idx_ohlc_daily_date ON ticker_ohlc_daily(price_date,ticker);
    CREATE INDEX IF NOT EXISTS idx_signal_daily_date ON ticker_signal_daily(history_date,ticker);
    """)
    _ensure_columns(conn, "ticker_ohlc_daily", {
        "adjusted_close": "REAL",
        "split_factor": "REAL",
        "adjustment_json": "TEXT",
    })
    _ensure_columns(conn, "ticker_price_history", {
        "adjusted_close": "REAL",
        "split_factor": "REAL",
        "adjustment_json": "TEXT",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detail_zones_ticker_date ON ticker_detail_zones(ticker, snapshot_date, timeframe, zone_type);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detail_history_ticker_date ON ticker_detail_history(ticker, history_date);")
    conn.commit(); conn.close()


def ensure_gex_schema():
    conn = gex_conn(); cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS gex_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        run_source TEXT,
        status TEXT NOT NULL,
        total_tickers INTEGER DEFAULT 0,
        ok_tickers INTEGER DEFAULT 0,
        fail_tickers INTEGER DEFAULT 0,
        levels_count INTEGER DEFAULT 0,
        duration_seconds REAL,
        error_message TEXT,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gex_levels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gex_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        current_price REAL,
        change_percent REAL,
        strike REAL NOT NULL,
        gex REAL,
        call_gex REAL,
        put_gex REAL,
        call_oi REAL,
        put_oi REAL,
        distance_to_price_pct REAL,
        raw_gex_hash TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(gex_run_id, ticker, strike)
    );
    CREATE TABLE IF NOT EXISTS gex_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gex_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        current_price REAL,
        change_percent REAL,
        total_gex REAL,
        gamma_flip REAL,
        positive_gex_total REAL,
        negative_gex_total_abs REAL,
        dominant_strike REAL,
        dominant_gex REAL,
        max_pos_strike REAL,
        max_pos_gex REAL,
        max_neg_strike REAL,
        max_neg_gex REAL,
        nearest_pos_strike_above REAL,
        nearest_pos_gex_above REAL,
        nearest_neg_strike_below REAL,
        nearest_neg_gex_below REAL,
        previous_price REAL,
        price_change_from_prev_pct REAL,
        previous_total_gex REAL,
        total_gex_change_abs REAL,
        total_gex_change_pct REAL,
        previous_dominant_strike REAL,
        dominant_strike_changed INTEGER,
        call_accumulation_score REAL,
        put_accumulation_score REAL,
        profile_bias TEXT,
        profile_note TEXT,
        raw_gex_hash TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(gex_run_id, ticker)
    );
    CREATE TABLE IF NOT EXISTS gex_failures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gex_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        url TEXT,
        status_code INTEGER,
        error_type TEXT,
        error_message TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(gex_run_id, ticker)
    );
    CREATE INDEX IF NOT EXISTS idx_gex_runs_time ON gex_runs(captured_at_madrid);
    CREATE INDEX IF NOT EXISTS idx_gex_runs_date ON gex_runs(snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_gex_levels_ticker_date ON gex_levels(ticker, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_gex_levels_run ON gex_levels(gex_run_id);
    CREATE INDEX IF NOT EXISTS idx_gex_summary_ticker_date ON gex_summary(ticker, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_gex_summary_run ON gex_summary(gex_run_id);
    CREATE INDEX IF NOT EXISTS idx_gex_summary_bias_date ON gex_summary(profile_bias, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_gex_failures_run ON gex_failures(gex_run_id);
    CREATE INDEX IF NOT EXISTS idx_gex_failures_ticker_date ON gex_failures(ticker, snapshot_date);
    CREATE VIEW IF NOT EXISTS gex_levels_success AS
        SELECT gl.* FROM gex_levels gl
        JOIN gex_runs gr ON gr.id = gl.gex_run_id
        WHERE gr.status = 'success';
    CREATE VIEW IF NOT EXISTS gex_summary_success AS
        SELECT gs.* FROM gex_summary gs
        JOIN gex_runs gr ON gr.id = gs.gex_run_id
        WHERE gr.status = 'success';
    ''')
    _ensure_columns(conn, "gex_levels", {
        "previous_gex": "REAL",
        "gex_change_abs": "REAL",
        "gex_change_pct": "REAL",
        "sign_flip": "INTEGER"
    })
    _ensure_columns(conn, "gex_summary", {
        "strike_source_path": "TEXT",
        "expiry_source_path": "TEXT",
        "gross_gex": "REAL",
        "call_wall_strike": "REAL",
        "call_wall_gex": "REAL",
        "put_wall_strike": "REAL",
        "put_wall_gex": "REAL",
        "near_gex": "REAL",
        "near_call_gex": "REAL",
        "near_put_gex": "REAL",
        "near_gross_gex": "REAL",
        "near_gamma_flip": "REAL",
        "near_call_wall_strike": "REAL",
        "near_call_wall_gex": "REAL",
        "near_put_wall_strike": "REAL",
        "near_put_wall_gex": "REAL",
        "swing_gex": "REAL",
        "swing_call_gex": "REAL",
        "swing_put_gex": "REAL",
        "swing_gross_gex": "REAL",
        "swing_gamma_flip": "REAL",
        "swing_call_wall_strike": "REAL",
        "swing_call_wall_gex": "REAL",
        "swing_put_wall_strike": "REAL",
        "swing_put_wall_gex": "REAL",
        "long_gex": "REAL",
        "long_call_gex": "REAL",
        "long_put_gex": "REAL",
        "long_gross_gex": "REAL",
        "long_gamma_flip": "REAL",
        "long_call_wall_strike": "REAL",
        "long_call_wall_gex": "REAL",
        "long_put_wall_strike": "REAL",
        "long_put_wall_gex": "REAL",
        "profile_event_kind": "TEXT",
        "profile_hash": "TEXT",
        "profile_changed_fields": "INTEGER",
        "profile_changed_strikes": "INTEGER",
        "profile_changed_expiries": "INTEGER",
        "profile_material_change": "INTEGER"
    })
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS gex_expirations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gex_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        current_price REAL,
        change_percent REAL,
        expiration_date TEXT NOT NULL,
        dte INTEGER,
        expiry_bucket TEXT,
        net_gex REAL,
        call_gex REAL,
        put_gex REAL,
        call_oi REAL,
        put_oi REAL,
        abs_gex REAL,
        share_abs_gex REAL,
        previous_net_gex REAL,
        net_gex_change_abs REAL,
        net_gex_change_pct REAL,
        sign_flip INTEGER,
        raw_gex_hash TEXT,
        source_path TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(gex_run_id, ticker, expiration_date)
    );
    CREATE TABLE IF NOT EXISTS gex_expiry_buckets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gex_run_id INTEGER NOT NULL,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        expiry_bucket TEXT NOT NULL,
        min_dte INTEGER,
        max_dte INTEGER,
        expiration_count INTEGER,
        net_gex REAL,
        call_gex REAL,
        put_gex REAL,
        positive_gex_total REAL,
        negative_gex_total_abs REAL,
        abs_gex_total REAL,
        dominant_expiration TEXT,
        dominant_expiration_gex REAL,
        previous_net_gex REAL,
        net_gex_change_abs REAL,
        net_gex_change_pct REAL,
        sign_flip INTEGER,
        raw_gex_hash TEXT,
        created_at_utc TEXT NOT NULL,
        UNIQUE(gex_run_id, ticker, expiry_bucket)
    );
    CREATE INDEX IF NOT EXISTS idx_gex_exp_ticker_date ON gex_expirations(ticker, snapshot_date, expiration_date);
    CREATE INDEX IF NOT EXISTS idx_gex_exp_run ON gex_expirations(gex_run_id);
    CREATE INDEX IF NOT EXISTS idx_gex_bucket_ticker_date ON gex_expiry_buckets(ticker, snapshot_date, expiry_bucket);
    CREATE INDEX IF NOT EXISTS idx_gex_bucket_run ON gex_expiry_buckets(gex_run_id);
    CREATE VIEW IF NOT EXISTS gex_expirations_success AS
        SELECT ge.* FROM gex_expirations ge
        JOIN gex_runs gr ON gr.id = ge.gex_run_id
        WHERE gr.status = 'success';
    CREATE VIEW IF NOT EXISTS gex_expiry_buckets_success AS
        SELECT gb.* FROM gex_expiry_buckets gb
        JOIN gex_runs gr ON gr.id = gb.gex_run_id
        WHERE gr.status = 'success';

    CREATE TABLE IF NOT EXISTS gex_profile_state (
        ticker TEXT PRIMARY KEY,
        gex_run_id INTEGER,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        profile_hash TEXT NOT NULL,
        spot REAL,
        profile_blob BLOB NOT NULL,
        compressed INTEGER NOT NULL DEFAULT 1,
        updated_at_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gex_profile_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gex_run_id INTEGER NOT NULL,
        previous_gex_run_id INTEGER,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        session_type TEXT,
        ticker TEXT NOT NULL,
        event_kind TEXT NOT NULL,
        previous_profile_hash TEXT,
        profile_hash TEXT NOT NULL,
        spot REAL,
        previous_spot REAL,
        changed_field_count INTEGER DEFAULT 0,
        changed_strike_count INTEGER DEFAULT 0,
        changed_expiry_count INTEGER DEFAULT 0,
        material_change INTEGER DEFAULT 0,
        changed_fields_json TEXT,
        material_flags_json TEXT,
        profile_blob BLOB NOT NULL,
        compressed INTEGER NOT NULL DEFAULT 1,
        created_at_utc TEXT NOT NULL,
        UNIQUE(gex_run_id, ticker)
    );
    CREATE INDEX IF NOT EXISTS idx_gex_profile_events_date ON gex_profile_events(snapshot_date, ticker, captured_at_madrid);
    CREATE INDEX IF NOT EXISTS idx_gex_profile_events_run ON gex_profile_events(gex_run_id);
    CREATE INDEX IF NOT EXISTS idx_gex_profile_events_material ON gex_profile_events(material_change, snapshot_date);
    ''')
    conn.commit(); conn.close()


def ensure_raw_schema():
    conn = raw_conn(); cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS raw_api_payloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        run_type TEXT,
        run_id INTEGER,
        endpoint_type TEXT NOT NULL,
        ticker TEXT,
        url TEXT,
        status_code INTEGER,
        payload_hash TEXT NOT NULL,
        payload_json TEXT,
        payload_blob BLOB,
        compressed INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        created_at_utc TEXT NOT NULL,
        unique_key TEXT UNIQUE
    );
    CREATE INDEX IF NOT EXISTS idx_raw_payloads_time ON raw_api_payloads(captured_at_madrid);
    CREATE INDEX IF NOT EXISTS idx_raw_payloads_endpoint ON raw_api_payloads(endpoint_type, ticker, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_raw_payloads_hash ON raw_api_payloads(payload_hash);
    ''')
    conn.commit(); conn.close()


def ensure_meta_schema():
    conn = meta_conn(); cur = conn.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS schema_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint_type TEXT NOT NULL,
        field_path TEXT NOT NULL,
        first_seen_at_utc TEXT NOT NULL,
        first_seen_at_madrid TEXT NOT NULL,
        last_seen_at_utc TEXT NOT NULL,
        last_seen_at_madrid TEXT NOT NULL,
        value_type TEXT,
        sample_value TEXT,
        active INTEGER DEFAULT 1,
        UNIQUE(endpoint_type, field_path)
    );
    CREATE TABLE IF NOT EXISTS schema_change_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_at_utc TEXT NOT NULL,
        event_at_madrid TEXT NOT NULL,
        endpoint_type TEXT NOT NULL,
        field_path TEXT NOT NULL,
        change_type TEXT NOT NULL,
        old_value_type TEXT,
        new_value_type TEXT,
        sample_value TEXT
    );
    CREATE TABLE IF NOT EXISTS extra_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc TEXT NOT NULL,
        captured_at_madrid TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        endpoint_type TEXT NOT NULL,
        ticker TEXT,
        run_type TEXT,
        run_id INTEGER,
        field_path TEXT NOT NULL,
        field_value TEXT,
        value_type TEXT,
        payload_hash TEXT,
        unique_key TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS bot_heartbeats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        heartbeat_utc TEXT NOT NULL,
        heartbeat_madrid TEXT NOT NULL,
        status TEXT,
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_schema_registry_endpoint ON schema_registry(endpoint_type);
    CREATE INDEX IF NOT EXISTS idx_schema_changes_time ON schema_change_events(event_at_madrid);
    CREATE INDEX IF NOT EXISTS idx_extra_fields_endpoint ON extra_fields(endpoint_type, ticker, snapshot_date);
    ''')
    conn.commit(); conn.close()


def create_scanner_run(session_type: str, total_expected: int = 0) -> int:
    conn = scanner_conn(); cur = conn.cursor(); utc = ts_utc_str(); mad = ts_madrid_str()
    cur.execute('''INSERT INTO scanner_runs (captured_at_utc,captured_at_madrid,snapshot_date,session_type,run_source,status,total_tickers_expected,created_at_utc,updated_at_utc)
        VALUES (?,?,?,?,?,?,?,?,?)''', (utc, mad, market_date_from_madrid_iso(mad), session_type, 'tmad_core_data_bot', 'running', total_expected, utc, utc))
    run_id = cur.lastrowid; conn.commit(); conn.close(); return run_id


def update_scanner_run(run_id: int, status: str, ok: int = 0, failed: int = 0, total_expected: int | None = None, duration_seconds: float | None = None, error_message: str | None = None, scanner_meta: dict | None = None, base_rows_saved: int | None = None, base_unique_tickers: int | None = None, enriched_ok: int | None = None, enriched_failed: int | None = None, completion_stage: str | None = None, previous_scanner_run_id: int | None = None, scanner_changed_tickers: int | None = None, scanner_new_tickers: int | None = None, scanner_removed_tickers: int | None = None, scanner_unchanged_tickers: int | None = None):
    """Update scanner run status.

    Backwards compatible: ok/failed still populate total_tickers_ok/failed.
    New fields separate the base scanner snapshot from the enriched detail/intraday/GEX stage.
    This avoids misleading healthchecks such as ok=0 when the scanner base was already saved.
    """
    conn = scanner_conn(); cur = conn.cursor(); meta = scanner_meta or {}
    cur.execute("""UPDATE scanner_runs SET
            status=?,
            total_tickers_ok=?,
            total_tickers_failed=?,
            total_tickers_expected=COALESCE(?, total_tickers_expected),
            duration_seconds=?,
            error_message=?,
            scanner_date=?,
            last_sync_timestamp=?,
            last_sync_type=?,
            base_rows_saved=COALESCE(?, base_rows_saved),
            base_unique_tickers=COALESCE(?, base_unique_tickers),
            enriched_ok=COALESCE(?, enriched_ok),
            enriched_failed=COALESCE(?, enriched_failed),
            completion_stage=COALESCE(?, completion_stage),
            previous_scanner_run_id=COALESCE(?, previous_scanner_run_id),
            scanner_changed_tickers=COALESCE(?, scanner_changed_tickers),
            scanner_new_tickers=COALESCE(?, scanner_new_tickers),
            scanner_removed_tickers=COALESCE(?, scanner_removed_tickers),
            scanner_unchanged_tickers=COALESCE(?, scanner_unchanged_tickers),
            updated_at_utc=?
        WHERE id=?""",
        (status, ok, failed, total_expected, duration_seconds, error_message,
         meta.get('scannerDate') or meta.get('scanner_date'), meta.get('lastSyncTimestamp'), meta.get('lastSyncType'),
         base_rows_saved, base_unique_tickers, enriched_ok, enriched_failed, completion_stage,
         previous_scanner_run_id, scanner_changed_tickers, scanner_new_tickers, scanner_removed_tickers, scanner_unchanged_tickers,
         ts_utc_str(), run_id))
    conn.commit(); conn.close()


def insert_scanner_snapshot(row: dict):
    cols = list(row.keys()); sql = f"INSERT OR REPLACE INTO scanner_snapshots ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    conn = scanner_conn(); cur = conn.cursor(); cur.execute(sql, [row[c] for c in cols]); conn.commit(); conn.close()


def insert_scanner_snapshot_delta(row: dict):
    cols = list(row.keys())
    sql = f"INSERT OR REPLACE INTO scanner_snapshot_deltas ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    conn = scanner_conn(); cur = conn.cursor(); cur.execute(sql, [row[c] for c in cols]); conn.commit(); conn.close()


def insert_scanner_snapshot_deltas(rows: list[dict]):
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO scanner_snapshot_deltas ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    values = [[row.get(c) for c in cols] for row in rows]
    conn = scanner_conn(); cur = conn.cursor(); cur.executemany(sql, values); conn.commit(); count = len(values); conn.close(); return count


def upsert_daily_history_from_snapshot(row: dict):
    mapping = {k: row.get(k) for k in ['ticker','session_type','side','name','category','current_price','change_percent','option_signal','unusual_flow_signal','total_gex','gex_change','gamma_flip','max_pos_strike','max_pos_gex','max_neg_strike','max_neg_gex','max_delta_gex_strike','max_delta_gex_value','nearest_supply','nearest_demand','zone_scope','vwap','rsi','rvol','signals_available','trend','momentum_score','rsi14','iv_z_spread','iv_change','otm_call_volume_z','otm_put_volume_z','pc_ratio','long_score','long_score_signals','price_change','is_live_price','put_call_ratio','detail_iv_z_spread','detail_momentum_score','detail_trend','daily_supply_top','daily_supply_bottom','daily_supply_strength','daily_demand_top','daily_demand_bottom','daily_demand_strength','weekly_supply_top','weekly_supply_bottom','weekly_supply_strength','weekly_demand_top','weekly_demand_bottom','weekly_demand_strength']}
    mapping['trade_date'] = row.get('snapshot_date'); mapping['last_run_id'] = row.get('scanner_run_id'); mapping['updated_at_utc'] = ts_utc_str(); mapping['updated_at_madrid'] = ts_madrid_str()
    cols = list(mapping.keys()); update_cols = [c for c in cols if c not in ('ticker','trade_date')]
    sql = f"INSERT INTO ticker_daily_history ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))}) ON CONFLICT(ticker, trade_date) DO UPDATE SET {','.join([f'{c}=excluded.{c}' for c in update_cols])}"
    conn = scanner_conn(); cur = conn.cursor(); cur.execute(sql, [mapping[c] for c in cols]); conn.commit(); conn.close()


def rebuild_canonical_daily_history() -> dict:
    """Rebuild the compact daily table only from successful deep close runs.

    Selective hourly snapshots must never replace the canonical end-of-day state.
    For each market date the latest successful deep run is authoritative. The
    operation is idempotent and uses one SQL statement, so it is safe at startup.
    """
    conn = scanner_conn(); cur = conn.cursor()
    try:
        daily_cols = [str(row[1]) for row in cur.execute("PRAGMA table_info(ticker_daily_history)")]
        snap_cols = {str(row[1]) for row in cur.execute("PRAGMA table_info(scanner_snapshots)")}
        common = [
            col for col in daily_cols
            if col not in {"id", "trade_date", "last_run_id", "updated_at_utc", "updated_at_madrid"}
            and col in snap_cols
        ]
        insert_cols = common + ["trade_date", "last_run_id", "updated_at_utc", "updated_at_madrid"]
        select_cols = [f"s.{col}" for col in common] + [
            "s.snapshot_date", "s.scanner_run_id", "s.captured_at_utc", "s.captured_at_madrid"
        ]
        cur.execute("DELETE FROM ticker_daily_history")
        cur.execute(
            f"""INSERT OR REPLACE INTO ticker_daily_history ({','.join(insert_cols)})
                SELECT {','.join(select_cols)}
                FROM scanner_snapshots s
                JOIN scanner_runs r ON r.id=s.scanner_run_id
                JOIN (
                    SELECT snapshot_date,MAX(id) AS run_id
                    FROM scanner_runs
                    WHERE status='success' AND LOWER(COALESCE(session_type,'')) LIKE '%deep%'
                    GROUP BY snapshot_date
                ) canonical ON canonical.run_id=r.id
                ORDER BY s.snapshot_date,s.ticker"""
        )
        rows = int(cur.rowcount or 0)
        dates = int(cur.execute("SELECT COUNT(DISTINCT trade_date) FROM ticker_daily_history").fetchone()[0] or 0)
        conn.commit()
        return {"rows": rows, "dates": dates}
    finally:
        conn.close()



def _safe_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def _safe_int(x):
    try:
        if x is None or x == "":
            return None
        return int(float(x))
    except Exception:
        return None


def insert_detail_zones(scanner_run_id: int, session_type: str, ticker: str, detail_payload: dict | None, raw_detail_hash: str | None):
    """Guarda zonas daily/weekly del endpoint detail en tabla granular."""
    if not detail_payload:
        return 0
    data = detail_payload.get("data", {}) or {}
    utc = ts_utc_str(); mad = ts_madrid_str()
    inserted = 0
    conn = scanner_conn(); cur = conn.cursor()
    for timeframe, key in (("daily", "dailyZones"), ("weekly", "weeklyZones")):
        zones = data.get(key) or []
        if not isinstance(zones, list):
            continue
        for z in zones:
            if not isinstance(z, dict):
                continue
            cur.execute("""INSERT OR IGNORE INTO ticker_detail_zones
                (scanner_run_id,captured_at_utc,captured_at_madrid,snapshot_date,session_type,ticker,timeframe,zone_type,top,bottom,strength,raw_detail_hash,created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scanner_run_id, utc, mad, market_date_from_madrid_iso(mad), session_type, ticker, timeframe, z.get("type"),
                 _safe_float(z.get("top")), _safe_float(z.get("bottom")), _safe_int(z.get("strength")), raw_detail_hash, utc))
            inserted += cur.rowcount
    conn.commit(); conn.close()
    return inserted


def insert_detail_history(scanner_run_id: int, session_type: str, ticker: str, detail_payload: dict | None, raw_detail_hash: str | None):
    if not detail_payload:
        return 0
    data = detail_payload.get("data", {}) or {}
    hist = data.get("history") or []
    if not isinstance(hist, list):
        return 0
    utc = ts_utc_str(); mad = ts_madrid_str()
    cutoff = (now_madrid().date() - timedelta(days=cfg.COMPACT_HISTORY_DAYS)).isoformat()
    inserted = 0
    conn = scanner_conn(); cur = conn.cursor()
    for h in hist:
        if not isinstance(h, dict):
            continue
        history_date = str(h.get("date") or "")[:10]
        if not history_date or history_date < cutoff:
            continue
        cur.execute("""INSERT INTO ticker_signal_daily
            (ticker,history_date,signal,trend,momentum_score,iv_z_spread,total_gex,change_percent,close_price,
             unusual_flow_signal,is_live,source_scanner_run_id,raw_detail_hash,updated_at_utc,updated_at_madrid)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker,history_date) DO UPDATE SET
              signal=excluded.signal,trend=excluded.trend,momentum_score=excluded.momentum_score,
              iv_z_spread=excluded.iv_z_spread,total_gex=excluded.total_gex,change_percent=excluded.change_percent,
              close_price=excluded.close_price,unusual_flow_signal=excluded.unusual_flow_signal,is_live=excluded.is_live,
              source_scanner_run_id=excluded.source_scanner_run_id,raw_detail_hash=excluded.raw_detail_hash,
              updated_at_utc=excluded.updated_at_utc,updated_at_madrid=excluded.updated_at_madrid""",
            (ticker, history_date, h.get("signal"), h.get("trend"), _safe_float(h.get("momentumScore")),
             _safe_float(h.get("ivZSpread")), _safe_float(h.get("totalGex")), _safe_float(h.get("changePercent")),
             _safe_float(h.get("closePrice")), h.get("unusualFlowSignal"),
             1 if h.get("isLive") is True else (0 if h.get("isLive") is False else None), scanner_run_id,
             raw_detail_hash, utc, mad))
        inserted += 1
        if cfg.STORE_DETAIL_HISTORY_PER_RUN:
            cur.execute("""INSERT OR IGNORE INTO ticker_detail_history
                (scanner_run_id,captured_at_utc,captured_at_madrid,snapshot_date,session_type,ticker,history_date,signal,trend,momentum_score,iv_z_spread,total_gex,change_percent,close_price,unusual_flow_signal,is_live,raw_detail_hash,created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scanner_run_id, utc, mad, market_date_from_madrid_iso(mad), session_type, ticker, history_date, h.get("signal"), h.get("trend"),
                 _safe_float(h.get("momentumScore")), _safe_float(h.get("ivZSpread")), _safe_float(h.get("totalGex")),
                 _safe_float(h.get("changePercent")), _safe_float(h.get("closePrice")), h.get("unusualFlowSignal"),
                 1 if h.get("isLive") is True else (0 if h.get("isLive") is False else None), raw_detail_hash, utc))
    conn.commit(); conn.close()
    return inserted


def insert_price_history(scanner_run_id: int, session_type: str, ticker: str, detail_payload: dict | None, raw_detail_hash: str | None):
    """Upserts a compact OHLC daily table; optional per-run duplication is disabled by default."""
    if not detail_payload:
        return 0
    data = detail_payload.get("data", {}) or {}
    hist = data.get("priceHistory") or []
    if not isinstance(hist, list):
        return 0
    utc = ts_utc_str(); mad = ts_madrid_str()
    cutoff = (now_madrid().date() - timedelta(days=cfg.COMPACT_HISTORY_DAYS)).isoformat()
    inserted = 0
    conn = scanner_conn(); cur = conn.cursor()
    for h in hist:
        if not isinstance(h, dict):
            continue
        d = str(h.get("date") or h.get("time") or h.get("timestamp") or "")[:10]
        if not d or d < cutoff:
            continue
        adjusted_close = _safe_float(
            h.get("adjustedClose")
            if h.get("adjustedClose") is not None
            else h.get("adjusted_close")
            if h.get("adjusted_close") is not None
            else h.get("adjClose")
            if h.get("adjClose") is not None
            else h.get("adj_close")
        )
        split_factor = _safe_float(
            h.get("splitFactor")
            if h.get("splitFactor") is not None
            else h.get("split_factor")
            if h.get("split_factor") is not None
            else h.get("splitRatio")
            if h.get("splitRatio") is not None
            else h.get("split_ratio")
        )
        adjustment_meta = {
            str(k): v
            for k, v in h.items()
            if any(token in str(k).lower() for token in ("split", "adjust", "dividend"))
        }
        adjustment_json = json.dumps(adjustment_meta, ensure_ascii=False, default=str) if adjustment_meta else None
        values = (
            ticker, d, _safe_float(h.get("open")), _safe_float(h.get("high")), _safe_float(h.get("low")),
            _safe_float(h.get("close")), _safe_float(h.get("price")), _safe_float(h.get("volume")),
            adjusted_close, split_factor, adjustment_json,
            scanner_run_id, raw_detail_hash, utc, mad,
        )
        cur.execute("""INSERT INTO ticker_ohlc_daily
            (ticker,price_date,open,high,low,close,price,volume,adjusted_close,split_factor,adjustment_json,
             source_scanner_run_id,raw_detail_hash,updated_at_utc,updated_at_madrid)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker,price_date) DO UPDATE SET
              open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,price=excluded.price,
              volume=excluded.volume,adjusted_close=excluded.adjusted_close,split_factor=excluded.split_factor,
              adjustment_json=excluded.adjustment_json,source_scanner_run_id=excluded.source_scanner_run_id,
              raw_detail_hash=excluded.raw_detail_hash,updated_at_utc=excluded.updated_at_utc,
              updated_at_madrid=excluded.updated_at_madrid""", values)
        inserted += 1
        if cfg.STORE_PRICE_HISTORY_PER_RUN:
            cur.execute("""INSERT OR IGNORE INTO ticker_price_history
                (scanner_run_id,captured_at_utc,captured_at_madrid,snapshot_date,session_type,ticker,price_date,
                 open,high,low,close,price,volume,adjusted_close,split_factor,adjustment_json,raw_detail_hash,created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scanner_run_id, utc, mad, market_date_from_madrid_iso(mad), session_type, ticker, d,
                 _safe_float(h.get("open")), _safe_float(h.get("high")), _safe_float(h.get("low")),
                 _safe_float(h.get("close")), _safe_float(h.get("price")), _safe_float(h.get("volume")),
                 adjusted_close, split_factor, adjustment_json, raw_detail_hash, utc))
    conn.commit(); conn.close()
    return inserted



def rebuild_compact_history_from_raw_details(progress_callback=None) -> dict:
    """Incrementally rebuild compact TMAD history from latest detail payloads.

    Each (ticker,payload_hash) is processed once and recorded in the Scanner DB.
    Existing compact rows from older versions are adopted automatically, so a
    restart does not rewrite ~1,000 tickers when the raw source is unchanged.
    """
    stats = {
        "payloads": 0, "tickers": 0, "signal_rows": 0, "price_rows": 0,
        "errors": 0, "pending_payloads": 0, "skipped_unchanged": 0,
        "adopted_existing": 0, "cached": False,
    }
    if not cfg.CORE_HISTORY_REBUILD_FROM_RAW_ON_START or not RAW_DB.exists():
        return stats

    conn = raw_conn()
    try:
        rows = conn.execute(
            """WITH ranked AS (
                 SELECT *, ROW_NUMBER() OVER(
                   PARTITION BY ticker ORDER BY captured_at_madrid DESC,id DESC
                 ) AS rn
                 FROM raw_api_payloads
                 WHERE endpoint_type='detail' AND ticker IS NOT NULL
                   AND COALESCE(status_code,0) BETWEEN 200 AND 299
                   AND error_message IS NULL
               )
               SELECT * FROM ranked WHERE rn=1 ORDER BY ticker"""
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return stats

    scan = scanner_conn()
    try:
        scan.execute("""CREATE TABLE IF NOT EXISTS core_compact_payload_state (
            ticker TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            signal_rows INTEGER NOT NULL DEFAULT 0,
            price_rows INTEGER NOT NULL DEFAULT 0,
            processed_at_utc TEXT NOT NULL,
            PRIMARY KEY(ticker,payload_hash)
        )""")
        processed = {
            (str(r[0] or '').upper(), str(r[1] or ''))
            for r in scan.execute("SELECT ticker,payload_hash FROM core_compact_payload_state WHERE status IN ('processed','adopted')").fetchall()
        }
        signal_pairs = {
            (str(r[0] or '').upper(), str(r[1] or ''))
            for r in scan.execute("SELECT DISTINCT ticker,raw_detail_hash FROM ticker_signal_daily WHERE raw_detail_hash IS NOT NULL").fetchall()
        }
        price_pairs = {
            (str(r[0] or '').upper(), str(r[1] or ''))
            for r in scan.execute("SELECT DISTINCT ticker,raw_detail_hash FROM ticker_ohlc_daily WHERE raw_detail_hash IS NOT NULL").fetchall()
        }
        pending = []
        for row in rows:
            key=(str(row['ticker'] or '').upper(), str(row['payload_hash'] or ''))
            if key in processed:
                stats['skipped_unchanged'] += 1
                continue
            if key in signal_pairs or key in price_pairs:
                scan.execute(
                    "INSERT OR REPLACE INTO core_compact_payload_state (ticker,payload_hash,status,signal_rows,price_rows,processed_at_utc) VALUES (?,?,?,?,?,?)",
                    (key[0],key[1],'adopted',0,0,ts_utc_str()),
                )
                stats['adopted_existing'] += 1
                continue
            pending.append(row)
        scan.commit()
    finally:
        scan.close()

    stats['pending_payloads'] = len(pending)
    if not pending:
        stats['cached'] = True
        stats['payloads'] = len(rows)
        stats['tickers'] = len({str(r['ticker'] or '').upper() for r in rows})
        return stats

    tickers=set()
    total=len(pending)
    for idx,row in enumerate(pending,1):
        ticker=str(row['ticker'] or '').upper()
        payload_hash_value=str(row['payload_hash'] or '')
        signal_count=price_count=0
        status='processed'
        try:
            if int(row['compressed'] or 0):
                raw=zlib.decompress(bytes(row['payload_blob'] or b'')).decode('utf-8')
            else:
                raw=str(row['payload_json'] or '')
            payload=json.loads(raw)
            if not ticker or not isinstance(payload,dict):
                raise ValueError('payload detail inválido')
            run_id=int(row['run_id'] or 0)
            signal_count=int(insert_detail_history(run_id,'core_history_rebuild',ticker,payload,payload_hash_value) or 0)
            price_count=int(insert_price_history(run_id,'core_history_rebuild',ticker,payload,payload_hash_value) or 0)
            stats['signal_rows'] += signal_count
            stats['price_rows'] += price_count
            stats['payloads'] += 1
            tickers.add(ticker)
        except Exception:
            stats['errors'] += 1
            status='error'
        finally:
            state_conn=scanner_conn()
            try:
                state_conn.execute(
                    "INSERT OR REPLACE INTO core_compact_payload_state (ticker,payload_hash,status,signal_rows,price_rows,processed_at_utc) VALUES (?,?,?,?,?,?)",
                    (ticker,payload_hash_value,status,signal_count,price_count,ts_utc_str()),
                )
                state_conn.commit()
            finally:
                state_conn.close()
        if progress_callback and (idx == 1 or idx == total or idx % 100 == 0):
            try:
                progress_callback(idx,total,ticker,stats)
            except Exception:
                pass
    stats['tickers']=len(tickers)
    return stats


def create_score_run(session_type: str) -> int:
    conn = scanner_conn(); cur = conn.cursor(); utc = ts_utc_str(); mad = ts_madrid_str()
    cur.execute("""INSERT INTO score_runs (captured_at_utc,captured_at_madrid,snapshot_date,session_type,status,created_at_utc,updated_at_utc)
        VALUES (?,?,?,?,?,?,?)""", (utc, mad, market_date_from_madrid_iso(mad), session_type, 'running', utc, utc))
    rid = cur.lastrowid; conn.commit(); conn.close(); return rid


def update_score_run(score_run_id: int, status: str, top_longs_count: int = 0, basket_days_count: int = 0, basket_positions_count: int = 0, error_message: str | None = None):
    conn = scanner_conn(); cur = conn.cursor()
    cur.execute("""UPDATE score_runs SET status=?, top_longs_count=?, basket_days_count=?, basket_positions_count=?, error_message=?, updated_at_utc=? WHERE id=?""",
        (status, top_longs_count, basket_days_count, basket_positions_count, error_message, ts_utc_str(), score_run_id))
    conn.commit(); conn.close()


def insert_score_top_longs(score_run_id: int, payload: dict | None, raw_hash: str | None):
    if not payload: return 0
    data = payload.get('data') or payload
    rows = data.get('rows') or data.get('items') or [] if isinstance(data, dict) else []
    if not isinstance(rows, list): return 0
    utc=ts_utc_str(); mad=ts_madrid_str(); inserted=0
    conn=scanner_conn(); cur=conn.cursor()
    for r in rows:
        if not isinstance(r, dict): continue
        ticker = r.get('symbol') or r.get('ticker')
        if not ticker: continue
        cur.execute("""INSERT OR REPLACE INTO score_top_longs
            (score_run_id,captured_at_utc,captured_at_madrid,snapshot_date,as_of,market_is_open,locked,total_picks,universe_size,rank,ticker,name,current_price,change_percent,long_score,raw_hash,created_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (score_run_id, utc, mad, market_date_from_madrid_iso(mad), data.get('asOf'), 1 if data.get('marketIsOpen') else 0 if data.get('marketIsOpen') is False else None,
             1 if data.get('locked') else 0 if data.get('locked') is False else None, _safe_int(data.get('totalPicks')), _safe_int(data.get('universeSize')),
             _safe_int(r.get('rank')), str(ticker).upper(), r.get('name'), _safe_float(r.get('currentPrice')), _safe_float(r.get('changePercent')), _safe_float(r.get('longScore')), raw_hash, utc))
        inserted += 1
    conn.commit(); conn.close(); return inserted


def insert_score_backtest(score_run_id: int, payload: dict | None, raw_hash: str | None):
    if not payload: return {'metrics':0,'equity':0,'mc':0,'bootstrap':0}
    data = payload.get('data') or payload
    if not isinstance(data, dict): return {'metrics':0,'equity':0,'mc':0,'bootstrap':0}
    utc=ts_utc_str(); mad=ts_madrid_str(); counts={'metrics':0,'equity':0,'mc':0,'bootstrap':0}
    conn=scanner_conn(); cur=conn.cursor(); metrics=data.get('metrics') or {}
    cur.execute("""INSERT OR REPLACE INTO score_backtest_metrics
        (score_run_id,captured_at_utc,captured_at_madrid,snapshot_date,window_start,window_end,live_score_version,total_return,cagr,sharpe,max_drawdown,raw_hash,created_at_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (score_run_id, utc, mad, market_date_from_madrid_iso(mad), data.get('windowStart'), data.get('windowEnd'), str(data.get('liveScoreVersion') or ''),
         _safe_float(metrics.get('totalReturn')), _safe_float(metrics.get('cagr')), _safe_float(metrics.get('sharpe')), _safe_float(metrics.get('maxDrawdown')), raw_hash, utc))
    counts['metrics']=1
    for r in data.get('equityCurve') or []:
        if not isinstance(r, dict): continue
        cur.execute("""INSERT OR REPLACE INTO score_backtest_equity_curve (score_run_id,curve_date,portfolio,spy,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?)""",
            (score_run_id, r.get('date'), _safe_float(r.get('portfolio')), _safe_float(r.get('spy')), raw_hash, utc)); counts['equity']+=1
    for r in data.get('monteCarloBands') or []:
        if not isinstance(r, dict): continue
        cur.execute("""INSERT OR REPLACE INTO score_backtest_monte_carlo (score_run_id,curve_date,actual,p5,p25,p50,p75,p95,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (score_run_id, r.get('date'), _safe_float(r.get('actual')), _safe_float(r.get('p5')), _safe_float(r.get('p25')), _safe_float(r.get('p50')), _safe_float(r.get('p75')), _safe_float(r.get('p95')), raw_hash, utc)); counts['mc']+=1
    boot=data.get('bootstrap') or {}
    for metric, r in boot.items():
        if not isinstance(r, dict): continue
        cur.execute("""INSERT OR REPLACE INTO score_backtest_bootstrap (score_run_id,metric,actual,median,p5,p25,p75,p95,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (score_run_id, metric, _safe_float(r.get('actual')), _safe_float(r.get('median')), _safe_float(r.get('p5')), _safe_float(r.get('p25')), _safe_float(r.get('p75')), _safe_float(r.get('p95')), raw_hash, utc)); counts['bootstrap']+=1
    conn.commit(); conn.close(); return counts


def insert_score_basket_history(score_run_id: int, payload: dict | None, raw_hash: str | None):
    if not payload: return {'days':0,'positions':0}
    data = payload.get('data') or payload
    if not isinstance(data, dict): return {'days':0,'positions':0}
    utc=ts_utc_str(); mad=ts_madrid_str(); summary=data.get('summary') or {}; counts={'days':0,'positions':0}
    conn=scanner_conn(); cur=conn.cursor()
    cur.execute("""INSERT OR REPLACE INTO score_basket_history_summary
        (score_run_id,captured_at_utc,captured_at_madrid,snapshot_date,as_of,window_days,avg_basket_return,avg_spy_return,avg_alpha,hit_rate,alpha_hit_rate,settled_days,pending_days,raw_hash,created_at_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (score_run_id, utc, mad, market_date_from_madrid_iso(mad), data.get('asOf'), _safe_int(data.get('windowDays')), _safe_float(summary.get('avgBasketReturn')), _safe_float(summary.get('avgSpyReturn')), _safe_float(summary.get('avgAlpha')), _safe_float(summary.get('hitRate')), _safe_float(summary.get('alphaHitRate')), _safe_int(summary.get('settledDays')), _safe_int(summary.get('pendingDays')), raw_hash, utc))
    for day in data.get('rows') or []:
        if not isinstance(day, dict): continue
        d=day.get('date')
        cur.execute("""INSERT OR REPLACE INTO score_basket_history_days (score_run_id,basket_date,basket_return,spy_return,alpha,is_pending,settled_count,total_count,rolling_avg_30,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (score_run_id, d, _safe_float(day.get('basketReturn')), _safe_float(day.get('spyReturn')), _safe_float(day.get('alpha')), 1 if day.get('isPending') else 0 if day.get('isPending') is False else None, _safe_int(day.get('settledCount')), _safe_int(day.get('totalCount')), _safe_float(day.get('rollingAvg30')), raw_hash, utc)); counts['days']+=1
        for pos in day.get('positions') or []:
            if not isinstance(pos, dict): continue
            ticker=pos.get('symbol') or pos.get('ticker')
            if not ticker: continue
            cur.execute("""INSERT OR REPLACE INTO score_basket_history_positions (score_run_id,basket_date,ticker,name,rank,score,entry_price,exit_price,marked_return,forward_return,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (score_run_id, d, str(ticker).upper(), pos.get('name'), _safe_int(pos.get('rank')), _safe_float(pos.get('score')), _safe_float(pos.get('entryPrice')), _safe_float(pos.get('exitPrice')), _safe_float(pos.get('markedReturn')), _safe_float(pos.get('forwardReturn')), raw_hash, utc)); counts['positions']+=1
    conn.commit(); conn.close(); return counts


def create_watchlist_run(session_type: str) -> int:
    conn=scanner_conn(); cur=conn.cursor(); utc=ts_utc_str(); mad=ts_madrid_str()
    cur.execute("""INSERT INTO watchlist_runs (captured_at_utc,captured_at_madrid,snapshot_date,session_type,status,created_at_utc,updated_at_utc) VALUES (?,?,?,?,?,?,?)""", (utc,mad,market_date_from_madrid_iso(mad),session_type,'running',utc,utc))
    rid=cur.lastrowid; conn.commit(); conn.close(); return rid


def update_watchlist_run(run_id: int, status: str, watchlists_count: int=0, dashboards_count: int=0, rows_count: int=0, error_message: str | None=None):
    conn=scanner_conn(); cur=conn.cursor()
    cur.execute("""UPDATE watchlist_runs SET status=?, watchlists_count=?, dashboards_count=?, rows_count=?, error_message=?, updated_at_utc=? WHERE id=?""", (status, watchlists_count, dashboards_count, rows_count, error_message, ts_utc_str(), run_id))
    conn.commit(); conn.close()


def _get_watchlist_items(payload: dict | None) -> list[dict]:
    if not payload: return []
    data=payload.get('data') if isinstance(payload, dict) else None
    if isinstance(data, list): return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ('watchlists','rows','items','data'):
            v=data.get(k)
            if isinstance(v, list): return [x for x in v if isinstance(x, dict)]
    if isinstance(payload, list): return [x for x in payload if isinstance(x, dict)]
    return []


def insert_watchlists(run_id: int, payload: dict | None, raw_hash: str | None):
    items=_get_watchlist_items(payload); utc=ts_utc_str(); mad=ts_madrid_str(); conn=scanner_conn(); cur=conn.cursor(); n=0
    for w in items:
        wid=w.get('id') or w.get('watchlistId') or w.get('watchlist_id') or w.get('name')
        if wid is None: continue
        cur.execute("""INSERT OR REPLACE INTO watchlists (watchlist_run_id,captured_at_utc,captured_at_madrid,snapshot_date,watchlist_id,name,description,icon,color,symbol_count,is_default,is_shared,sort_order,created_at,updated_at,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, utc, mad, market_date_from_madrid_iso(mad), str(wid), w.get('name'), w.get('description'), w.get('icon'), w.get('color'), _safe_int(w.get('symbol_count') or w.get('symbolCount')), 1 if w.get('is_default') or w.get('isDefault') else 0 if (w.get('is_default') is False or w.get('isDefault') is False) else None, 1 if w.get('is_shared') or w.get('isShared') else 0 if (w.get('is_shared') is False or w.get('isShared') is False) else None, _safe_int(w.get('sort_order') or w.get('sortOrder')), w.get('created_at') or w.get('createdAt'), w.get('updated_at') or w.get('updatedAt'), raw_hash, utc)); n+=1
    conn.commit(); conn.close(); return n


def insert_watchlist_dashboard(run_id: int, watchlist_id: str, payload: dict | None, raw_hash: str | None):
    if not payload: return {'sections':0,'rows':0,'sparklines':0}
    data=payload.get('data') or payload
    if not isinstance(data, dict): return {'sections':0,'rows':0,'sparklines':0}
    utc=ts_utc_str(); mad=ts_madrid_str(); conn=scanner_conn(); cur=conn.cursor(); counts={'sections':0,'rows':0,'sparklines':0}
    wname=data.get('watchlistName') or data.get('name'); ddate=data.get('date') or data.get('snapshotDate')
    for idx, sec in enumerate(data.get('sections') or []):
        if not isinstance(sec, dict): continue
        cur.execute("""INSERT OR REPLACE INTO watchlist_sections (watchlist_run_id,watchlist_id,section_index,section_name,section_json,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?,?)""", (run_id, str(watchlist_id), idx, sec.get('name') or sec.get('title'), json_dumps(sec), raw_hash, utc)); counts['sections']+=1
    rows=data.get('rows') or data.get('items') or []
    if isinstance(rows, dict): rows=list(rows.values())
    for r in rows:
        if not isinstance(r, dict): continue
        ticker=r.get('ticker') or r.get('symbol')
        if not ticker: continue
        spark=r.get('sparklineData')
        cur.execute("""INSERT OR REPLACE INTO watchlist_dashboard_rows
            (watchlist_run_id,captured_at_utc,captured_at_madrid,snapshot_date,watchlist_id,watchlist_name,dashboard_date,ticker,company_name,category,current_price,change_percent,option_signal,unusual_flow_signal,total_gex,previous_total_gex,iv_z_spread,previous_iv_z_spread,momentum_score,trend,long_score,long_score_signals,otm_call_volume_z,otm_put_volume_z,rvol,daily_demand_range,daily_supply_range,weekly_demand_range,weekly_supply_range,sparkline_demand_level,sparkline_supply_level,is_starred,sparkline_json,raw_hash,created_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, utc, mad, market_date_from_madrid_iso(mad), str(watchlist_id), wname, ddate, str(ticker).upper(), r.get('companyName') or r.get('name'), r.get('category'), _safe_float(r.get('currentPrice')), _safe_float(r.get('changePercent')), r.get('optionSignal'), r.get('unusualFlowSignal'), _safe_float(r.get('totalGex')), _safe_float(r.get('previousTotalGex')), _safe_float(r.get('ivZSpread')), _safe_float(r.get('previousIvZSpread')), _safe_float(r.get('momentumScore')), r.get('trend'), _safe_float(r.get('longScore')), _safe_int(r.get('longScoreSignals')), _safe_float(r.get('otmCallVolumeZ')), _safe_float(r.get('otmPutVolumeZ')), _safe_float(r.get('rvol')), str(r.get('dailyDemandRange')) if r.get('dailyDemandRange') is not None else None, str(r.get('dailySupplyRange')) if r.get('dailySupplyRange') is not None else None, str(r.get('weeklyDemandRange')) if r.get('weeklyDemandRange') is not None else None, str(r.get('weeklySupplyRange')) if r.get('weeklySupplyRange') is not None else None, _safe_float(r.get('sparklineDemandLevel')), _safe_float(r.get('sparklineSupplyLevel')), 1 if r.get('isStarred') else 0 if r.get('isStarred') is False else None, json_dumps(spark) if spark is not None else None, raw_hash, utc)); counts['rows']+=1
        if spark is not None:
            cur.execute("""INSERT OR REPLACE INTO watchlist_sparklines (watchlist_run_id,watchlist_id,ticker,sparkline_json,demand_level,supply_level,raw_hash,created_at_utc) VALUES (?,?,?,?,?,?,?,?)""", (run_id, str(watchlist_id), str(ticker).upper(), json_dumps(spark), _safe_float(r.get('sparklineDemandLevel')), _safe_float(r.get('sparklineSupplyLevel')), raw_hash, utc)); counts['sparklines']+=1
    conn.commit(); conn.close(); return counts


def count_schema_fields(endpoint_type: str) -> int:
    conn = meta_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM schema_registry WHERE endpoint_type=?", (endpoint_type,))
    row = cur.fetchone(); conn.close()
    return int(row[0] if row else 0)


def create_gex_run(session_type: str, total_tickers: int = 0) -> int:
    conn = gex_conn(); cur = conn.cursor(); utc = ts_utc_str(); mad = ts_madrid_str()
    cur.execute('''INSERT INTO gex_runs (captured_at_utc,captured_at_madrid,snapshot_date,session_type,run_source,status,total_tickers,created_at_utc,updated_at_utc)
        VALUES (?,?,?,?,?,?,?,?,?)''', (utc, mad, market_date_from_madrid_iso(mad), session_type, 'tmad_core_data_bot', 'running', total_tickers, utc, utc))
    run_id = cur.lastrowid; conn.commit(); conn.close(); return run_id


def update_gex_run(run_id: int, status: str, ok: int = 0, failed: int = 0, levels_count: int = 0, duration_seconds: float | None = None, error_message: str | None = None):
    conn = gex_conn(); cur = conn.cursor()
    cur.execute('''UPDATE gex_runs SET status=?, ok_tickers=?, fail_tickers=?, levels_count=?, duration_seconds=?, error_message=?, updated_at_utc=? WHERE id=?''', (status, ok, failed, levels_count, duration_seconds, error_message, ts_utc_str(), run_id))
    conn.commit(); conn.close()


def insert_gex_level(row: dict):
    cols = list(row.keys()); sql = f"INSERT OR REPLACE INTO gex_levels ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    conn = gex_conn(); cur = conn.cursor(); cur.execute(sql, [row[c] for c in cols]); conn.commit(); conn.close()


def insert_gex_summary(row: dict):
    cols = list(row.keys()); sql = f"INSERT OR REPLACE INTO gex_summary ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    conn = gex_conn(); cur = conn.cursor(); cur.execute(sql, [row[c] for c in cols]); conn.commit(); conn.close()


def insert_gex_expiration(row: dict):
    cols = list(row.keys())
    sql = f"INSERT OR REPLACE INTO gex_expirations ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    conn = gex_conn(); conn.execute(sql, [row[c] for c in cols]); conn.commit(); conn.close()


def insert_gex_expiry_bucket(row: dict):
    cols = list(row.keys())
    sql = f"INSERT OR REPLACE INTO gex_expiry_buckets ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    conn = gex_conn(); conn.execute(sql, [row[c] for c in cols]); conn.commit(); conn.close()


def insert_gex_failure(row: dict):
    cols = list(row.keys())
    sql = f"INSERT OR REPLACE INTO gex_failures ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})"
    conn = gex_conn()
    cur = conn.cursor()
    cur.execute(sql, [row[c] for c in cols])
    conn.commit()
    conn.close()


def get_previous_gex_summary(ticker: str, before_run_id: int | None = None):
    conn = gex_conn(); cur = conn.cursor()
    if before_run_id:
        cur.execute('SELECT * FROM gex_summary WHERE ticker=? AND gex_run_id < ? ORDER BY gex_run_id DESC LIMIT 1', (ticker, before_run_id))
    else:
        cur.execute('SELECT * FROM gex_summary WHERE ticker=? ORDER BY gex_run_id DESC LIMIT 1', (ticker,))
    row = cur.fetchone(); conn.close(); return dict(row) if row else None


def get_previous_gex_levels(ticker: str, before_run_id: int | None = None) -> dict[float, dict]:
    conn = gex_conn(); cur = conn.cursor()
    if before_run_id:
        cur.execute("SELECT MAX(gex_run_id) FROM gex_levels WHERE ticker=? AND gex_run_id < ?", (ticker, before_run_id))
    else:
        cur.execute("SELECT MAX(gex_run_id) FROM gex_levels WHERE ticker=?", (ticker,))
    row = cur.fetchone(); rid = row[0] if row else None
    if rid is None:
        conn.close(); return {}
    cur.execute("SELECT * FROM gex_levels WHERE ticker=? AND gex_run_id=?", (ticker, rid))
    out = {float(r['strike']): dict(r) for r in cur.fetchall() if r['strike'] is not None}
    conn.close(); return out


def get_previous_gex_expirations(ticker: str, before_run_id: int | None = None) -> dict[str, dict]:
    conn = gex_conn(); cur = conn.cursor()
    if before_run_id:
        cur.execute("SELECT MAX(gex_run_id) FROM gex_expirations WHERE ticker=? AND gex_run_id < ?", (ticker, before_run_id))
    else:
        cur.execute("SELECT MAX(gex_run_id) FROM gex_expirations WHERE ticker=?", (ticker,))
    row = cur.fetchone(); rid = row[0] if row else None
    if rid is None:
        conn.close(); return {}
    cur.execute("SELECT * FROM gex_expirations WHERE ticker=? AND gex_run_id=?", (ticker, rid))
    out = {str(r['expiration_date']): dict(r) for r in cur.fetchall() if r['expiration_date']}
    conn.close(); return out


def get_previous_gex_expiry_buckets(ticker: str, before_run_id: int | None = None) -> dict[str, dict]:
    conn = gex_conn(); cur = conn.cursor()
    if before_run_id:
        cur.execute("SELECT MAX(gex_run_id) FROM gex_expiry_buckets WHERE ticker=? AND gex_run_id < ?", (ticker, before_run_id))
    else:
        cur.execute("SELECT MAX(gex_run_id) FROM gex_expiry_buckets WHERE ticker=?", (ticker,))
    row = cur.fetchone(); rid = row[0] if row else None
    if rid is None:
        conn.close(); return {}
    cur.execute("SELECT * FROM gex_expiry_buckets WHERE ticker=? AND gex_run_id=?", (ticker, rid))
    out = {str(r['expiry_bucket']): dict(r) for r in cur.fetchall() if r['expiry_bucket']}
    conn.close(); return out


def insert_raw_payload(run_type: str, run_id: int | None, endpoint_type: str, ticker: str | None, url: str | None, status_code: int | None, payload: Any, error_message: str | None = None) -> str | None:
    if payload is None and not error_message: return None
    utc = ts_utc_str(); mad = ts_madrid_str(); phash = payload_hash(payload if payload is not None else {'error': error_message})
    text = json_dumps(payload) if payload is not None else None; blob = None; compressed = 0; payload_json = text
    if RAW_COMPRESS and text is not None:
        blob = zlib.compress(text.encode('utf-8'), level=6); payload_json = None; compressed = 1
    unique_key = f"{mad}|{endpoint_type}|{ticker or ''}|{phash}"
    conn = raw_conn(); cur = conn.cursor()
    cur.execute('''INSERT OR IGNORE INTO raw_api_payloads (captured_at_utc,captured_at_madrid,snapshot_date,run_type,run_id,endpoint_type,ticker,url,status_code,payload_hash,payload_json,payload_blob,compressed,error_message,created_at_utc,unique_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (utc, mad, market_date_from_madrid_iso(mad), run_type, run_id, endpoint_type, ticker, url, status_code, phash, payload_json, blob, compressed, error_message, utc, unique_key))
    conn.commit(); conn.close(); return phash


def insert_schema_change(endpoint_type: str, field_path: str, change_type: str, old_value_type: str | None, new_value_type: str | None, sample_value: Any):
    conn = meta_conn(); cur = conn.cursor()
    cur.execute('INSERT INTO schema_change_events (event_at_utc,event_at_madrid,endpoint_type,field_path,change_type,old_value_type,new_value_type,sample_value) VALUES (?,?,?,?,?,?,?,?)', (ts_utc_str(), ts_madrid_str(), endpoint_type, field_path, change_type, old_value_type, new_value_type, str(sample_value)[:1000]))
    conn.commit(); conn.close()


def schema_value_type(val: Any) -> str:
    # Tipo estable para schema discovery.
    # TMAD mezcla con frecuencia int/float y null/no-null por ticker; eso no es un cambio real de contrato.
    if val is None:
        return 'null'
    if isinstance(val, bool):
        return 'bool'
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return 'number'
    if isinstance(val, list):
        return 'list'
    if isinstance(val, dict):
        return 'object'
    return 'string'


def _schema_types_compatible(old_type: str | None, new_type: str) -> bool:
    if old_type is None:
        return True
    if old_type == new_type:
        return True
    # null solo indica ausencia para ese ticker/momento; no debe disparar cambio.
    if old_type == 'null' or new_type == 'null':
        return True
    return False


def upsert_schema_field(endpoint_type: str, field_path: str, val: Any) -> tuple[bool, bool, str | None, str]:
    vt = schema_value_type(val); sample = str(val)[:1000]
    conn = meta_conn(); cur = conn.cursor(); cur.execute('SELECT value_type FROM schema_registry WHERE endpoint_type=? AND field_path=?', (endpoint_type, field_path)); existing = cur.fetchone()
    is_new = existing is None; type_changed = False; old_type = None
    if is_new:
        cur.execute('INSERT INTO schema_registry (endpoint_type,field_path,first_seen_at_utc,first_seen_at_madrid,last_seen_at_utc,last_seen_at_madrid,value_type,sample_value,active) VALUES (?,?,?,?,?,?,?,?,1)', (endpoint_type, field_path, ts_utc_str(), ts_madrid_str(), ts_utc_str(), ts_madrid_str(), vt, sample))
    else:
        old_type = existing['value_type']
        type_changed = not _schema_types_compatible(old_type, vt)
        # No degrades: si ya conocemos un tipo no-null, no lo sobreescribas por null.
        stored_type = old_type if (vt == 'null' and old_type and old_type != 'null') else vt
        # Si antes solo vimos null y ahora viene algo real, actualiza el baseline sin considerarlo cambio.
        if old_type == 'null' and vt != 'null':
            stored_type = vt
            type_changed = False
        cur.execute('UPDATE schema_registry SET last_seen_at_utc=?, last_seen_at_madrid=?, value_type=?, sample_value=?, active=1 WHERE endpoint_type=? AND field_path=?', (ts_utc_str(), ts_madrid_str(), stored_type, sample, endpoint_type, field_path))
    conn.commit(); conn.close()
    if is_new:
        insert_schema_change(endpoint_type, field_path, 'new_field', None, vt, val)
    elif type_changed:
        insert_schema_change(endpoint_type, field_path, 'type_change', old_type, vt, val)
    return is_new, type_changed, old_type, vt


def insert_extra_field(captured_at_utc: str, captured_at_madrid: str, endpoint_type: str, ticker: str | None, run_type: str | None, run_id: int | None, field_path: str, value: Any, phash: str | None):
    unique_key = f"{captured_at_madrid}|{endpoint_type}|{ticker or ''}|{field_path}|{phash or ''}"
    conn = meta_conn(); cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO extra_fields (captured_at_utc,captured_at_madrid,snapshot_date,endpoint_type,ticker,run_type,run_id,field_path,field_value,value_type,payload_hash,unique_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (captured_at_utc, captured_at_madrid, market_date_from_madrid_iso(captured_at_madrid), endpoint_type, ticker, run_type, run_id, field_path, str(value)[:4000], value_type(value), phash, unique_key))
    conn.commit(); conn.close()


def insert_heartbeat(status: str, note: str = ''):
    conn = meta_conn(); cur = conn.cursor(); cur.execute('INSERT INTO bot_heartbeats (heartbeat_utc, heartbeat_madrid, status, note) VALUES (?,?,?,?)', (ts_utc_str(), ts_madrid_str(), status, note)); conn.commit(); conn.close()


def log_quality(scope: str, severity: str, message: str, scanner_run_id: int | None = None, ticker: str | None = None, metadata: dict | None = None):
    conn = scanner_conn(); cur = conn.cursor(); cur.execute('INSERT INTO data_quality_events (event_at_utc,event_at_madrid,scope,severity,scanner_run_id,ticker,message,metadata_json) VALUES (?,?,?,?,?,?,?,?)', (ts_utc_str(), ts_madrid_str(), scope, severity, scanner_run_id, ticker, message, json.dumps(metadata or {}, ensure_ascii=False))); conn.commit(); conn.close()


def purge_live_retention():
    cutoff = (now_madrid() - timedelta(days=LIVE_RETENTION_DAYS)).isoformat()
    jobs = [(SCANNER_DB,[('scanner_snapshots','captured_at_madrid'),('scanner_snapshot_deltas','captured_at_madrid'),('scanner_runs','captured_at_madrid'),('ticker_daily_history','updated_at_madrid'),('ticker_detail_zones','captured_at_madrid'),('ticker_detail_history','captured_at_madrid'),('ticker_price_history','captured_at_madrid'),('score_runs','captured_at_madrid'),('score_top_longs','captured_at_madrid'),('score_backtest_metrics','captured_at_madrid'),('score_basket_history_summary','captured_at_madrid'),('watchlist_runs','captured_at_madrid'),('watchlists','captured_at_madrid'),('watchlist_dashboard_rows','captured_at_madrid'),('data_quality_events','event_at_madrid')]), (GEX_DB,[('gex_levels','captured_at_madrid'),('gex_summary','captured_at_madrid'),('gex_expirations','captured_at_madrid'),('gex_expiry_buckets','captured_at_madrid'),('gex_failures','captured_at_madrid'),('gex_runs','captured_at_madrid')]), (RAW_DB,[('raw_api_payloads','captured_at_madrid')]), (META_DB,[('extra_fields','captured_at_madrid'),('bot_heartbeats','heartbeat_madrid')])]
    for db_path, tables in jobs:
        conn = connect(db_path); cur = conn.cursor()
        for table, col in tables:
            try: cur.execute(f'DELETE FROM {table} WHERE {col} < ?', (cutoff,))
            except Exception: pass
        if db_path == SCANNER_DB:
            compact_cutoff = (now_madrid().date() - timedelta(days=cfg.COMPACT_HISTORY_DAYS)).isoformat()
            try: cur.execute('DELETE FROM ticker_ohlc_daily WHERE price_date < ?', (compact_cutoff,))
            except Exception: pass
            try: cur.execute('DELETE FROM ticker_signal_daily WHERE history_date < ?', (compact_cutoff,))
            except Exception: pass
        conn.commit(); conn.execute('PRAGMA wal_checkpoint(TRUNCATE);'); conn.close()


def _fetch_invalid_run_ids_with_success(conn: sqlite3.Connection, table: str) -> list[tuple[int, str, str]]:
    """Return non-success runs for dates where at least one success run exists.

    This avoids deleting the only available snapshot for a date before the repair
    capture has succeeded. It is intentionally conservative.
    """
    rows = conn.execute(f"""
        SELECT r.id, r.snapshot_date, COALESCE(r.status, '') AS status
        FROM {table} r
        WHERE COALESCE(r.status, '') <> 'success'
          AND EXISTS (
              SELECT 1 FROM {table} s
              WHERE s.snapshot_date = r.snapshot_date
                AND s.status = 'success'
          )
    """).fetchall()
    return [(int(r['id']), str(r['snapshot_date']), str(r['status'])) for r in rows]


def cleanup_invalid_runs_after_success() -> dict:
    """Delete partial/interrupted/failed runs after a valid success exists.

    Final/consumption DBs should not grow forever with partial bootstrap attempts.
    This function removes invalid scanner/GEX runs and their dependent rows only
    when a success run exists for the same snapshot_date.
    """
    stats = {
        'scanner_invalid_runs_deleted': 0,
        'scanner_child_rows_deleted': 0,
        'gex_invalid_runs_deleted': 0,
        'gex_child_rows_deleted': 0,
        'raw_rows_deleted': 0,
        'extra_fields_deleted': 0,
    }

    scanner_invalid: list[int] = []
    gex_invalid: list[int] = []

    # Scanner live cleanup
    conn = scanner_conn(); cur = conn.cursor()
    try:
        invalid = _fetch_invalid_run_ids_with_success(conn, 'scanner_runs')
        scanner_invalid = [rid for rid, _, _ in invalid]
        if scanner_invalid:
            ph = ','.join(['?'] * len(scanner_invalid))
            for table, col in [
                ('scanner_snapshots', 'scanner_run_id'),
                ('scanner_snapshot_deltas', 'scanner_run_id'),
                ('ticker_detail_zones', 'scanner_run_id'),
                ('ticker_detail_history', 'scanner_run_id'),
                ('ticker_price_history', 'scanner_run_id'),
                ('data_quality_events', 'scanner_run_id'),
            ]:
                try:
                    cur.execute(f'DELETE FROM {table} WHERE {col} IN ({ph})', scanner_invalid)
                    stats['scanner_child_rows_deleted'] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                except Exception:
                    pass
            try:
                cur.execute(f'DELETE FROM ticker_daily_history WHERE last_run_id IN ({ph})', scanner_invalid)
                stats['scanner_child_rows_deleted'] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except Exception:
                pass
            cur.execute(f'DELETE FROM scanner_runs WHERE id IN ({ph})', scanner_invalid)
            stats['scanner_invalid_runs_deleted'] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(scanner_invalid)
        conn.commit()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')
        if VACUUM_AFTER_INVALID_CLEANUP and scanner_invalid:
            conn.execute('VACUUM;')
    finally:
        conn.close()

    # GEX live cleanup
    conn = gex_conn(); cur = conn.cursor()
    try:
        invalid = _fetch_invalid_run_ids_with_success(conn, 'gex_runs')
        gex_invalid = [rid for rid, _, _ in invalid]
        if gex_invalid:
            ph = ','.join(['?'] * len(gex_invalid))
            for table, col in [
                ('gex_levels', 'gex_run_id'),
                ('gex_summary', 'gex_run_id'),
                ('gex_expirations', 'gex_run_id'),
                ('gex_expiry_buckets', 'gex_run_id'),
                ('gex_failures', 'gex_run_id'),
            ]:
                try:
                    cur.execute(f'DELETE FROM {table} WHERE {col} IN ({ph})', gex_invalid)
                    stats['gex_child_rows_deleted'] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                except Exception:
                    pass
            cur.execute(f'DELETE FROM gex_runs WHERE id IN ({ph})', gex_invalid)
            stats['gex_invalid_runs_deleted'] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(gex_invalid)
        conn.commit()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')
        if VACUUM_AFTER_INVALID_CLEANUP and gex_invalid:
            conn.execute('VACUUM;')
    finally:
        conn.close()

    # Raw payloads linked to invalid runs
    if CLEAN_INVALID_RAW_PAYLOADS:
        conn = raw_conn(); cur = conn.cursor()
        try:
            if scanner_invalid:
                ph = ','.join(['?'] * len(scanner_invalid))
                cur.execute(f"DELETE FROM raw_api_payloads WHERE run_type='scanner' AND run_id IN ({ph})", scanner_invalid)
                stats['raw_rows_deleted'] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            if gex_invalid:
                ph = ','.join(['?'] * len(gex_invalid))
                cur.execute(f"DELETE FROM raw_api_payloads WHERE run_type='gex' AND run_id IN ({ph})", gex_invalid)
                stats['raw_rows_deleted'] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit(); conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')
            if VACUUM_AFTER_INVALID_CLEANUP and (scanner_invalid or gex_invalid):
                conn.execute('VACUUM;')
        finally:
            conn.close()

    # Discovery extra fields linked to invalid runs
    if CLEAN_INVALID_EXTRA_FIELDS:
        conn = meta_conn(); cur = conn.cursor()
        try:
            if scanner_invalid:
                ph = ','.join(['?'] * len(scanner_invalid))
                cur.execute(f"DELETE FROM extra_fields WHERE run_type='scanner' AND run_id IN ({ph})", scanner_invalid)
                stats['extra_fields_deleted'] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            if gex_invalid:
                ph = ','.join(['?'] * len(gex_invalid))
                cur.execute(f"DELETE FROM extra_fields WHERE run_type='gex' AND run_id IN ({ph})", gex_invalid)
                stats['extra_fields_deleted'] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit(); conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')
            if VACUUM_AFTER_INVALID_CLEANUP and (scanner_invalid or gex_invalid):
                conn.execute('VACUUM;')
        finally:
            conn.close()

    return stats
