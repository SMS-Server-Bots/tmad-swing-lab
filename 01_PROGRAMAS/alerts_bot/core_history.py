from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from statistics import mean, pstdev
from typing import Any, Callable

import config
from db import (
    connect, upsert_feature_observations_bulk, upsert_asset_relationship_history,
    get_runtime_state, set_runtime_state, history_coverage, core_feature_date_inventory,
)
from relational_features import attach_relational_features, build_relational_context
from relationship_learning import _relationship_map, _relationship_rows_as_of, _returns_map
from asset_universe import classify_asset
from time_utils import ts_utc_str
from transition_features import add_transition_features


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _flow_direction(signal: Any, unusual: Any) -> int:
    text = f"{signal or ''} {unusual or ''}".lower()
    bull = any(word in text for word in ("bull", "call", "long", "buy"))
    bear = any(word in text for word in ("bear", "put", "short", "sell"))
    return 1 if bull and not bear else (-1 if bear and not bull else 0)


def _trend_direction(trend: Any) -> int:
    text = str(trend or "").lower()
    if any(word in text for word in ("bull", "up", "long", "positive")):
        return 1
    if any(word in text for word in ("bear", "down", "short", "negative")):
        return -1
    return 0


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = diffs[-period:]
    gains = sum(max(0.0, x) for x in recent) / period
    losses = sum(max(0.0, -x) for x in recent) / period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def _atr_pct(rows: list[dict], period: int = 14) -> float | None:
    if len(rows) <= period:
        return None
    trs: list[float] = []
    for i in range(1, len(rows)):
        prev_close = _f(rows[i - 1].get("close"))
        high = _f(rows[i].get("high"))
        low = _f(rows[i].get("low"))
        if prev_close is None or high is None or low is None:
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    close = _f(rows[-1].get("close"))
    if len(trs) < period or not close:
        return None
    return mean(trs[-period:]) / close * 100.0


def _safe_pct(current: float | None, previous: float | None, floor: float = 1.0) -> float | None:
    if current is None or previous is None:
        return None
    denominator = max(abs(previous), floor)
    return (current - previous) / denominator * 100.0


def _metadata_by_ticker(conn: sqlite3.Connection) -> dict[str, dict]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "scanner_snapshots" not in tables:
        return {}
    rows = conn.execute(
        """WITH ranked AS (
             SELECT ticker,name,category,
                    ROW_NUMBER() OVER(PARTITION BY ticker ORDER BY captured_at_madrid DESC,id DESC) rn
             FROM scanner_snapshots
           )
           SELECT ticker,name,category FROM ranked WHERE rn=1"""
    ).fetchall()
    return {
        str(row["ticker"]): {"name": row["name"], "category": row["category"]}
        for row in rows
    }



def _current_scanner_universe(conn: sqlite3.Connection) -> dict:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"scanner_runs", "scanner_snapshots"}.issubset(tables):
        return {"run_id": 0, "snapshot_date": None, "tickers": []}
    row = conn.execute(
        """SELECT id,snapshot_date FROM scanner_runs
           WHERE status='success' ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return {"run_id": 0, "snapshot_date": None, "tickers": []}
    run_id = int(row[0] or 0)
    tickers = [
        str(item[0]).upper()
        for item in conn.execute(
            """SELECT ticker FROM scanner_snapshots
               WHERE scanner_run_id=? AND ticker IS NOT NULL
               GROUP BY ticker ORDER BY MIN(rowid)""",
            (run_id,),
        ).fetchall()
        if str(item[0] or "").strip()
    ]
    return {"run_id": run_id, "snapshot_date": str(row[1] or ""), "tickers": tickers}


CORE_COMPLETION_POLICY = "per_ticker_latest_sessions_v1"


def _core_ticker_session_counts(
    conn: sqlite3.Connection, tickers: list[str]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not tickers:
        return counts
    for start in range(0, len(tickers), 350):
        chunk = tickers[start:start + 350]
        ticker_ph = ",".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""SELECT s.ticker,COUNT(DISTINCT s.history_date) n
                FROM ticker_signal_daily s
                LEFT JOIN ticker_ohlc_daily p
                  ON p.ticker=s.ticker AND p.price_date=s.history_date
                WHERE s.ticker IN ({ticker_ph})
                  AND COALESCE(s.close_price,p.close,p.price) IS NOT NULL
                GROUP BY s.ticker""",
            chunk,
        ).fetchall()
        for row in rows:
            counts[str(row[0]).upper()] = int(row[1] or 0)
    return counts


def _core_ticker_completion(
    conn: sqlite3.Connection, tickers: list[str], target: int
) -> tuple[int, list[str], dict[str, int]]:
    counts = _core_ticker_session_counts(conn, tickers)
    missing = [ticker for ticker in tickers if int(counts.get(ticker, 0)) < int(target)]
    return len(tickers) - len(missing), missing, counts


def _core_latest_session_union(
    conn: sqlite3.Connection, tickers: list[str], target: int
) -> tuple[list[str], dict[str, int]]:
    """Union of every eligible ticker's latest target sessions.

    This must use the same 30-session eligibility window as Data Bot. Passing the
    longer retention cap (504) makes every young ticker fail the union gate and
    silently falls back to a sparse global calendar.
    """
    by_ticker: dict[str, list[str]] = {ticker: [] for ticker in tickers}
    if not tickers:
        return [], {}
    minimum = max(1, int(target or 1))
    for start in range(0, len(tickers), 350):
        chunk = tickers[start:start + 350]
        ph = ",".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""SELECT s.ticker,s.history_date
                FROM ticker_signal_daily s
                LEFT JOIN ticker_ohlc_daily p
                  ON p.ticker=s.ticker AND p.price_date=s.history_date
                WHERE s.ticker IN ({ph})
                  AND COALESCE(s.close_price,p.close,p.price) IS NOT NULL
                GROUP BY s.ticker,s.history_date
                ORDER BY s.ticker,s.history_date""",
            chunk,
        ).fetchall()
        for ticker, day in rows:
            by_ticker.setdefault(str(ticker).upper(), []).append(str(day))
    date_counts: dict[str, int] = {}
    for ticker in tickers:
        days = sorted(set(by_ticker.get(ticker) or []))[-minimum:]
        if len(days) < minimum:
            continue
        for day in days:
            date_counts[day] = date_counts.get(day, 0) + 1
    return sorted(date_counts), date_counts


def _prune_core_history_observations(allowed_keys: set[str]) -> dict[str, int]:
    """Remove obsolete CORE rows/outcomes from prior strict-calendar versions."""
    conn = connect(config.ALERTS_DB)
    try:
        stale = [
            (int(row[0]), str(row[1] or ""))
            for row in conn.execute(
                """SELECT id,unique_key FROM feature_observations
                   WHERE observation_layer='core_history' AND feature_tier='CORE'"""
            ).fetchall()
            if str(row[1] or "") not in allowed_keys
        ]
        outcomes = observations = 0
        for start in range(0, len(stale), 500):
            ids = [row[0] for row in stale[start:start + 500]]
            if not ids:
                continue
            ph = ",".join(["?"] * len(ids))
            cur = conn.execute(f"DELETE FROM observation_outcomes WHERE observation_id IN ({ph})", ids)
            outcomes += max(0, int(cur.rowcount or 0))
            cur = conn.execute(f"DELETE FROM feature_observations WHERE id IN ({ph})", ids)
            observations += max(0, int(cur.rowcount or 0))
        conn.commit()
        return {"observations": observations, "outcomes": outcomes}
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _restrict_history_to_source_calendar(
    history_by_ticker: dict[str, list[dict]],
    source_calendar: list[str] | tuple[str, ...],
    target: int,
) -> tuple[dict[str, list[dict]], list[str]]:
    """Return only rows that belong to Data Bot's authoritative CORE calendar."""
    dates = sorted({str(day) for day in source_calendar if str(day)})
    allowed = set(dates)
    minimum = max(1, int(target or 1))
    restricted: dict[str, list[dict]] = {}
    for ticker, rows in history_by_ticker.items():
        ordered = sorted(
            (row for row in rows if str(row.get("history_date") or "") in allowed),
            key=lambda row: str(row.get("history_date") or ""),
        )
        valid_dates = {
            str(row.get("history_date") or "")
            for row in ordered
            if row.get("effective_close") is not None
        }
        restricted[ticker] = ordered if len(valid_dates) >= minimum else []
    return restricted, dates


def _core_window_fingerprint(
    universe_fingerprint: str, universe_date: str | None, dates: list[str]
) -> str:
    if not universe_fingerprint:
        return ""
    latest = str(dates[-1] if dates else universe_date or "")
    token = f"{CORE_COMPLETION_POLICY}|{universe_fingerprint}|{universe_date or ''}|{latest}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _core_resolution_state(
    conn: sqlite3.Connection, universe_fingerprint: str, universe_date: str | None, dates: list[str], missing_tickers: list[str]
) -> dict:
    window_fingerprint = _core_window_fingerprint(universe_fingerprint, universe_date, dates)
    excluded: list[str] = []
    if window_fingerprint and _table_exists(conn, "core_bootstrap_ticker_state"):
        rows = conn.execute(
            """SELECT ticker FROM core_bootstrap_ticker_state
               WHERE universe_fingerprint=? AND window_fingerprint=? AND status='excluded'""",
            (universe_fingerprint, window_fingerprint),
        ).fetchall()
        missing = set(missing_tickers)
        excluded = sorted({str(row[0] or "").upper() for row in rows if str(row[0] or "").upper() in missing})
    excluded_set = set(excluded)
    pending = [ticker for ticker in missing_tickers if ticker not in excluded_set]
    return {
        "window_fingerprint": window_fingerprint,
        "excluded_tickers_list": excluded,
        "pending_tickers_list": pending,
        "excluded_fingerprint": hashlib.sha256("\n".join(excluded).encode("utf-8")).hexdigest() if excluded else "",
    }


def core_source_coverage() -> dict:
    target = max(1, int(config.CORE_HISTORY_TARGET_SESSIONS))
    max_sessions = max(target, int(config.CORE_HISTORY_MAX_SESSIONS))
    empty = {
        "ready": False, "dates": [], "date_count": 0, "target": target, "max_sessions": max_sessions,
        "rows": 0, "tickers": 0, "start_date": None, "end_date": None,
        "quality_threshold": int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE),
        "date_row_counts": {},
        "universe_run_id": 0, "universe_date": None, "universe_fingerprint": "",
        "universe_tickers": 0, "complete_tickers": 0, "remaining_tickers": 0,
        "universe_complete": False, "universe_completion_ratio": 0.0,
        "resolved_tickers": 0, "excluded_tickers": 0, "pending_tickers": 0,
        "excluded_fingerprint": "", "window_fingerprint": "",
        "excluded_ticker_sample": [], "pending_ticker_sample": [],
        "missing_ticker_sample": [],
    }
    if not config.SCANNER_DB.exists():
        return empty
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"ticker_signal_daily", "ticker_ohlc_daily"}.issubset(tables):
            return empty
        universe = _current_scanner_universe(conn)
        universe_tickers = list(universe.get("tickers") or [])
        universe_fingerprint = hashlib.sha256(
            "\n".join(sorted(universe_tickers)).encode("utf-8")
        ).hexdigest() if universe_tickers else ""
        universe_base = {
            "universe_run_id": int(universe.get("run_id") or 0),
            "universe_date": universe.get("snapshot_date"),
            "universe_fingerprint": universe_fingerprint,
            "universe_tickers": len(universe_tickers),
        }
        empty_universe_meta = {
            **universe_base,
            "complete_tickers": 0,
            "remaining_tickers": len(universe_tickers),
            "universe_complete": False,
            "universe_completion_ratio": 0.0,
            "resolved_tickers": 0,
            "excluded_tickers": 0,
            "pending_tickers": len(universe_tickers),
            "excluded_fingerprint": "",
            "window_fingerprint": "",
            "excluded_ticker_sample": [],
            "pending_ticker_sample": universe_tickers[:50],
            "missing_ticker_sample": universe_tickers[:50],
        }
        rows = conn.execute(
            """SELECT s.history_date,COUNT(DISTINCT s.ticker) n
               FROM ticker_signal_daily s
               LEFT JOIN ticker_ohlc_daily p
                 ON p.ticker=s.ticker AND p.price_date=s.history_date
               WHERE COALESCE(s.close_price,p.close,p.price) IS NOT NULL
               GROUP BY s.history_date ORDER BY s.history_date"""
        ).fetchall()
        if not rows:
            return {**empty, **empty_universe_meta}
        maximum = max(int(row["n"] or 0) for row in rows)
        threshold = max(
            int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE),
            int(math.ceil(maximum * float(config.CORE_HISTORY_MIN_COVERAGE_RATIO))),
        )
        calendar_threshold = max(1, min(int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE), maximum))
        qualifying = [(str(row["history_date"]), int(row["n"] or 0)) for row in rows if int(row["n"] or 0) >= calendar_threshold]
        selected = qualifying[-max_sessions:]
        dates = [day for day, _ in selected]
        if not dates:
            return {**empty, **empty_universe_meta, "quality_threshold": threshold}
        complete_tickers, missing_tickers, ticker_session_counts = _core_ticker_completion(
            conn, universe_tickers, target
        )
        eligible_tickers = [ticker for ticker in universe_tickers if ticker not in set(missing_tickers)]
        materialized_dates, materialized_date_counts = _core_latest_session_union(
            conn, eligible_tickers, target
        )
        if materialized_dates:
            dates = materialized_dates
        resolution = _core_resolution_state(
            conn, universe_fingerprint, universe.get("snapshot_date"), dates, missing_tickers
        )
        excluded_tickers_list = list(resolution.get("excluded_tickers_list") or [])
        pending_tickers_list = list(resolution.get("pending_tickers_list") or [])
        resolved_tickers = int(complete_tickers) + len(excluded_tickers_list)
        eligible_ratio = complete_tickers / max(len(universe_tickers), 1)
        minimum_eligible_ratio = max(0.80, min(1.0, float(config.CORE_HISTORY_MIN_ELIGIBLE_UNIVERSE_RATIO)))
        universe_complete = bool(
            len(dates) >= target
            and universe_tickers
            and not pending_tickers_list
            and complete_tickers >= int(config.CORE_HISTORY_MIN_TICKERS_PER_DATE)
            and eligible_ratio >= minimum_eligible_ratio
        )
        universe_meta = {
            **universe_base,
            "complete_tickers": int(complete_tickers),
            "resolved_tickers": int(resolved_tickers),
            "excluded_tickers": len(excluded_tickers_list),
            "pending_tickers": len(pending_tickers_list),
            "remaining_tickers": len(pending_tickers_list),
            "universe_complete": universe_complete,
            "universe_completion_ratio": round(eligible_ratio, 6),
            "universe_resolved_ratio": round(resolved_tickers / max(len(universe_tickers), 1), 6),
            "minimum_eligible_ratio": minimum_eligible_ratio,
            "window_fingerprint": str(resolution.get("window_fingerprint") or ""),
            "excluded_fingerprint": str(resolution.get("excluded_fingerprint") or ""),
            "excluded_ticker_sample": excluded_tickers_list[:50],
            "pending_ticker_sample": pending_tickers_list[:50],
            "missing_ticker_sample": missing_tickers[:50],
        }
        capped_rows = sum(min(int(ticker_session_counts.get(ticker, 0)), max_sessions) for ticker in universe_tickers)
        return {
            "ready": bool(len(dates) >= target and universe_complete),
            "dates": dates,
            "date_count": len(dates),
            "target": target,
            "max_sessions": max_sessions,
            "rows": int(capped_rows),
            "tickers": int(complete_tickers),
            "start_date": dates[0],
            "end_date": dates[-1],
            "quality_threshold": threshold,
            "calendar_threshold": calendar_threshold,
            "date_row_counts": materialized_date_counts or {day: count for day, count in selected},
            **universe_meta,
            "source": "TMAD_DETAIL_GROWING",
            "completion_policy": CORE_COMPLETION_POLICY,
            "universe_quality": (
                "COMPLETE_CURRENT_SCANNER" if universe_complete and not excluded_tickers_list
                else ("RESOLVED_WITH_AUDITED_EXCLUSIONS" if universe_complete else "PARTIAL_CURRENT_SCANNER")
            ),
        }
    finally:
        conn.close()


def materialize_core_history(progress_callback: Callable | None = None) -> dict:
    """Build point-in-time CORE observations from TMAD detail history + OHLCV."""
    coverage = core_source_coverage()
    dates = list(coverage.get("dates") or [])
    result = {
        **coverage,
        "inserted_or_updated": 0,
        "with_total_gex": 0,
        "scanner_only": 0,
        "relational_dates": 0,
        "pruned_observations": 0,
        "pruned_outcomes": 0,
        "errors": 0,
        "error_samples": [],
        "source_complete_tickers": 0,
        "materializable_tickers": 0,
        "nonmaterializable_tickers": 0,
        "nonmaterializable_ticker_sample": [],
    }
    if not dates:
        return result

    def _progress(stage: str, current: int = 0, total: int = 0, detail: str = "") -> None:
        if not progress_callback:
            return
        try:
            progress_callback(stage, current, total, detail)
        except Exception:
            pass

    _progress("loading", 0, 0, "Leyendo histórico compacto del Data Bot")
    data_conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    data_conn.row_factory = sqlite3.Row
    try:
        universe_tickers = list(_current_scanner_universe(data_conn).get("tickers") or [])
        if not universe_tickers:
            return result
        ticker_ph = ",".join(["?"] * len(universe_tickers))
        signal_rows = data_conn.execute(
            f"""SELECT s.*,COALESCE(s.close_price,p.close,p.price) AS effective_close,
                       p.open,p.high,p.low,p.close AS ohlc_close,p.volume
                FROM ticker_signal_daily s
                LEFT JOIN ticker_ohlc_daily p
                  ON p.ticker=s.ticker AND p.price_date=s.history_date
                WHERE s.ticker IN ({ticker_ph})
                  AND COALESCE(s.close_price,p.close,p.price) IS NOT NULL
                ORDER BY s.ticker,s.history_date""",
            universe_tickers,
        ).fetchall()
        price_rows = data_conn.execute(
            """SELECT ticker,price_date,open,high,low,COALESCE(close,price) close,volume
               FROM ticker_ohlc_daily ORDER BY ticker,price_date"""
        ).fetchall()
        metadata = _metadata_by_ticker(data_conn)
    finally:
        data_conn.close()

    _progress("loaded", len(signal_rows), len(signal_rows), f"{len(universe_tickers)} tickers cargados")
    prices_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in price_rows:
        prices_by_ticker[str(row["ticker"])].append(dict(row))
    history_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in signal_rows:
        history_by_ticker[str(row["ticker"])].append(dict(row))
    target = int(coverage.get("target") or config.CORE_HISTORY_TARGET_SESSIONS)
    # Data Bot's source inventory is authoritative.  Earlier builds selected the
    # last N rows independently for every ticker, which reintroduced old dates
    # from tickers with a longer local history.  That produced a permanent
    # ``drift_fechas`` even after a forced rebuild.  Materialise and retain only
    # the exact source calendar published by Data.
    source_complete_tickers = sorted(
        ticker
        for ticker, rows in history_by_ticker.items()
        if len({str(row.get("history_date") or "") for row in rows if row.get("effective_close") is not None}) >= target
    )
    history_by_ticker, dates = _restrict_history_to_source_calendar(
        history_by_ticker, coverage.get("dates") or [], target
    )
    materializable_tickers = sorted(ticker for ticker in source_complete_tickers if history_by_ticker.get(ticker))
    nonmaterializable_tickers = sorted(set(source_complete_tickers) - set(materializable_tickers))
    result["source_complete_tickers"] = len(source_complete_tickers)
    result["materializable_tickers"] = len(materializable_tickers)
    result["nonmaterializable_tickers"] = len(nonmaterializable_tickers)
    result["nonmaterializable_ticker_sample"] = nonmaterializable_tickers[:50]
    result["materialized_calendar_dates"] = len(dates)
    result["calendar_restricted_to_source"] = True
    _progress(
        "calendar",
        len(materializable_tickers),
        len(source_complete_tickers),
        f"calendario CORE {len(dates)} sesiones · materializables {len(materializable_tickers)}/{len(source_complete_tickers)}",
    )
    if nonmaterializable_tickers:
        # Never prune a healthy cache when the source calendar cannot explain
        # every ticker that Data marked eligible. This is a hard reconciliation
        # error, not a non-fatal per-row feature exception.
        result["errors"] += len(nonmaterializable_tickers)
        result["materialization_blocked"] = True
        result["error_samples"].extend(
            {
                "ticker": ticker,
                "date": None,
                "error": "eligible ticker has fewer than target sessions inside authoritative CORE calendar",
            }
            for ticker in nonmaterializable_tickers[:20]
        )
        return result

    base_by_date: dict[str, dict[str, dict]] = defaultdict(dict)
    for ticker, rows in history_by_ticker.items():
        all_prices = prices_by_ticker.get(ticker, [])
        price_index = {str(row.get("price_date")): idx for idx, row in enumerate(all_prices)}
        price_dates = [str(row.get("price_date") or "") for row in all_prices]
        previous_gex: float | None = None
        for row in rows:
            day = str(row["history_date"])
            idx = price_index.get(day)
            if idx is None:
                # TMAD history and priceHistory can roll one session apart while
                # the bootstrap spans a market close. Use only OHLC information
                # already known on or before the signal date; entry close remains
                # the signal history closePrice.
                prior_idx = bisect_right(price_dates, day) - 1
                idx = prior_idx if prior_idx >= 0 else None
            history_slice = all_prices[: idx + 1] if idx is not None else []
            closes = [float(item["close"]) for item in history_slice if _f(item.get("close")) is not None]
            volumes = [float(item["volume"]) for item in history_slice[:-1] if _f(item.get("volume")) is not None]
            close = _f(row["effective_close"])
            current_volume = _f(row["volume"])
            avg_volume = mean(volumes[-20:]) if volumes[-20:] else None
            rvol = current_volume / avg_volume if current_volume is not None and avg_volume not in (None, 0) else None
            rsi = _rsi(closes)
            sma20 = mean(closes[-20:]) if len(closes) >= 20 else (mean(closes) if closes else None)
            momentum_5d = (close / closes[-6] - 1.0) * 100.0 if close and len(closes) >= 6 and closes[-6] else None
            returns = [(closes[i] / closes[i - 1] - 1.0) * 100.0 for i in range(1, len(closes)) if closes[i - 1]]
            vol20 = pstdev(returns[-20:]) if len(returns) >= 2 else None
            total_gex = _f(row["total_gex"])
            gex_change = _safe_pct(total_gex, previous_gex)
            previous_gex = total_gex if total_gex is not None else previous_gex
            flow = _flow_direction(row["signal"], row["unusual_flow_signal"])
            trend_dir = _trend_direction(row["trend"])
            momentum = _f(row["momentum_score"], 0.0) or 0.0
            scanner_trend_score = momentum + trend_dir * 10.0
            if rsi is not None:
                scanner_trend_score += max(-8.0, min(8.0, (rsi - 50.0) / 3.0))
            meta = metadata.get(ticker, {})
            features = {
                "ticker": ticker,
                "name": meta.get("name"),
                "category": meta.get("category"),
                "sector": meta.get("category"),
                "industry": None,
                "asset_type": classify_asset(ticker, meta.get("name"), meta.get("category")),
                "snapshot_date": day,
                "session_type": "core_history_tmad",
                "observation_layer": "core_history",
                "feature_tier": "CORE",
                "source_kind": "TMAD_DETAIL_30D",
                "universe_quality": coverage.get("universe_quality"),
                "is_canonical_daily_close": 0,
                "has_gex": 1 if total_gex is not None else 0,
                "price": close,
                "open": _f(row["open"]),
                "high": _f(row["high"]),
                "low": _f(row["low"]),
                "volume": current_volume,
                "change_percent": _f(row["change_percent"]),
                "rsi": rsi,
                "rvol": rvol,
                "atr_pct": _atr_pct(history_slice),
                "momentum_5d_pct": momentum_5d,
                "realized_volatility_20d": vol20,
                "sma20": sma20,
                "price_vs_sma20_pct": ((close / sma20 - 1.0) * 100.0) if close and sma20 else None,
                "momentum_score": momentum,
                "trend": row["trend"],
                "trend_direction": trend_dir,
                "option_signal": row["signal"],
                "unusual_flow_signal": row["unusual_flow_signal"],
                "iv_z_spread": _f(row["iv_z_spread"]),
                "total_gex": total_gex,
                "total_gex_change_pct": gex_change,
                "flow_direction": flow,
                "scanner_trend_score": scanner_trend_score,
                "is_live": int(row["is_live"] or 0),
                "price_vs_vwap_pct": None,
            }
            base_by_date[day][ticker] = features

    # Build the relationship graph independently for every historical date.
    # Correlation, beta and lead-lag only use prices available on or before that
    # date; the 30-day CORE backfill therefore cannot see future relationships.
    relationship_prices = {
        ticker: [
            {
                "trade_date": str(row.get("price_date") or ""),
                "close": row.get("close"),
            }
            for row in rows
            if row.get("close") is not None
        ]
        for ticker, rows in prices_by_ticker.items()
    }
    returns_by_ticker = _returns_map(relationship_prices)
    relationship_metadata = {
        ticker: {
            "name": item.get("name"),
            "category": item.get("category"),
            "sector": item.get("category"),
            "industry": None,
            "sector_etf": None,
            "asset_type": classify_asset(ticker, item.get("name"), item.get("category")),
        }
        for ticker, item in metadata.items()
    }

    previous_map: dict[str, dict] = {}
    previous_enriched_map: dict[str, dict] = {}
    pending_rows: list[dict] = []
    total_dates = len(dates)
    for day_index, day in enumerate(dates, 1):
        if day_index == 1 or day_index == total_dates or day_index % 5 == 0:
            _progress("relationships", day_index, total_dates, f"grafo PIT {day}")
        base = base_by_date.get(day) or {}
        if not base:
            continue
        graph_rows = _relationship_rows_as_of(
            day,
            returns_by_ticker,
            relationship_metadata,
        )
        upsert_asset_relationship_history(graph_rows)
        relationship_map = _relationship_map(graph_rows)
        relational, _summary = build_relational_context(
            base,
            previous_by_ticker=previous_map,
            relationship_map=relationship_map,
        )
        result["relational_dates"] += 1
        observed_mad = f"{day}T22:05:00+02:00"
        observed_utc = f"{day}T20:05:00+00:00"
        synthetic_run_id = -int(day.replace("-", ""))
        current_enriched_map: dict[str, dict] = {}
        for ticker, raw_features in base.items():
            try:
                features = attach_relational_features(raw_features, relational.get(ticker))
                features = add_transition_features(features, previous_enriched_map.get(ticker))
                current_enriched_map[ticker] = features
                features["source_archive"] = "tmad_detail_30d"
                row = {
                    "observed_at_utc": observed_utc,
                    "observed_at_madrid": observed_mad,
                    "snapshot_date": day,
                    "source_scanner_run_id": synthetic_run_id,
                    "source_gex_run_id": None,
                    "ticker": ticker,
                    "session_type": "core_history_tmad",
                    "observation_layer": "core_history",
                    "is_canonical_daily_close": 0,
                    "has_gex": int(features.get("has_gex") or 0),
                    "source_archive": "tmad_detail_30d",
                    "feature_schema_version": int(config.RELATIONAL_FEATURE_SCHEMA_VERSION),
                    "feature_tier": "CORE",
                    "universe_quality": coverage.get("universe_quality"),
                    "source_kind": "TMAD_DETAIL_30D",
                    "scanner_captured_at_madrid": observed_mad,
                    "gex_captured_at_madrid": None,
                    "gex_lag_minutes": None,
                    "current_price": features.get("price"),
                    "total_gex": features.get("total_gex"),
                    "total_gex_change_pct": features.get("total_gex_change_pct"),
                    "front_gex": None,
                    "front_gex_change_pct": None,
                    "week_gex": None,
                    "week_gex_change_pct": None,
                    "short_gex": None,
                    "short_gex_change_pct": None,
                    "next_gex": None,
                    "next_gex_change_pct": None,
                    "month_gex": None,
                    "month_gex_change_pct": None,
                    "put_wall_pct": None,
                    "call_wall_pct": None,
                    "wall_range_pct": None,
                    "rvol": features.get("rvol"),
                    "rsi": features.get("rsi"),
                    "momentum_score": features.get("momentum_score"),
                    "trend": features.get("trend"),
                    "option_signal": features.get("option_signal"),
                    "unusual_flow_signal": features.get("unusual_flow_signal"),
                    "sector_etf": features.get("sector_etf"),
                    "market_regime": features.get("market_regime"),
                    "market_context_score": features.get("market_context_score"),
                    "market_breadth_score": features.get("market_breadth_score"),
                    "vix_stress_score": features.get("vix_stress_score"),
                    "credit_risk_on_score": features.get("credit_risk_on_score"),
                    "sector_context_score": features.get("sector_etf_score"),
                    "sector_breadth_score": features.get("sector_breadth_score"),
                    "leveraged_confirmation_score": features.get("leveraged_confirmation_score"),
                    "relationship_context_score": features.get("relationship_context_score"),
                    "relationship_lead_lag_score": features.get("relationship_lead_lag_score"),
                    "graph_context_score": features.get("graph_context_score"),
                    "relational_deterioration_score": features.get("relational_deterioration_score"),
                    "relative_strength_vs_market": features.get("relative_strength_vs_market"),
                    "relative_strength_vs_sector": features.get("relative_strength_vs_sector"),
                    "relational_json": features.get("relational_json"),
                    "features_json": json.dumps(features, ensure_ascii=False),
                    "unique_key": f"core|{day}|{ticker}",
                }
                pending_rows.append(row)
                if row["has_gex"]:
                    result["with_total_gex"] += 1
                else:
                    result["scanner_only"] += 1
            except Exception as exc:
                result["errors"] += 1
                if len(result["error_samples"]) < 20:
                    result["error_samples"].append({"ticker": ticker, "date": day, "error": str(exc)[:300]})
        previous_map = base
        previous_enriched_map = current_enriched_map

    allowed_keys = {str(row.get("unique_key") or "") for row in pending_rows if row.get("unique_key")}
    pruned = _prune_core_history_observations(allowed_keys)
    result["pruned_observations"] = int(pruned.get("observations") or 0)
    result["pruned_outcomes"] = int(pruned.get("outcomes") or 0)
    _progress("saving", len(pending_rows), len(pending_rows), "Guardando observaciones CORE")
    try:
        result["inserted_or_updated"] = upsert_feature_observations_bulk(pending_rows)
    except Exception:
        result["errors"] += len(pending_rows)
        raise
    return result



def _core_materialization_signature(source: dict) -> str:
    token = "|".join([
        str(getattr(config, "CORE_MATERIALIZATION_ENGINE_SIGNATURE", "core_features_v1")),
        str(source.get("universe_fingerprint") or ""),
        str(source.get("excluded_fingerprint") or ""),
        str(source.get("window_fingerprint") or ""),
        str(source.get("end_date") or ""),
        str(int(source.get("complete_tickers") or 0)),
        str(int(source.get("target") or config.CORE_HISTORY_TARGET_SESSIONS)),
    ])
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def materialize_core_history_if_needed(*, force: bool = False, progress_callback: Callable | None = None) -> dict:
    """Reuse a valid Alerts CORE cache and rebuild only when Data changed."""
    source = core_source_coverage()
    signature = _core_materialization_signature(source)
    existing = history_coverage()
    target = int(source.get("target") or config.CORE_HISTORY_TARGET_SESSIONS)
    complete_tickers = int(source.get("complete_tickers") or 0)
    expected_rows = complete_tickers * target
    source_dates = sorted({str(day) for day in (source.get("dates") or []) if str(day)})
    feature_dates = [str(row.get("date") or "") for row in core_feature_date_inventory() if str(row.get("date") or "")]
    calendar_matches = bool(source_dates and feature_dates and source_dates == feature_dates)
    cache_matches = bool(
        source.get("ready")
        and calendar_matches
        and int(existing.get("core_feature_dates") or 0) >= target
        and str(existing.get("core_feature_end_date") or "") == str(source.get("end_date") or "")
        and int(existing.get("core_feature_tickers") or 0) == complete_tickers
        and int(existing.get("core_feature_rows") or 0) >= expected_rows
    )
    stored = str(get_runtime_state("core_materialization_source_signature") or "")
    if not force and cache_matches and (stored == signature or not stored):
        set_runtime_state("core_materialization_source_signature", signature)
        return {
            **source,
            "cached": True,
            "cache_adopted": not bool(stored),
            "inserted_or_updated": 0,
            "with_total_gex": 0,
            "scanner_only": 0,
            "relational_dates": int(existing.get("core_feature_dates") or 0),
            "pruned_observations": 0,
            "pruned_outcomes": 0,
            "calendar_matches": calendar_matches,
            "errors": 0,
        }
    result = materialize_core_history(progress_callback=progress_callback)
    errors = int(result.get("errors") or 0)
    nonfatal_limit = max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25)))
    current = history_coverage()
    usable = bool(
        int(current.get("core_feature_dates") or 0) >= target
        and int(current.get("core_feature_tickers") or 0) >= complete_tickers
        and errors <= nonfatal_limit
    )
    if usable:
        set_runtime_state("core_materialization_source_signature", signature)
    result["cache_signature_stored"] = usable
    result["cached"] = False
    return result
