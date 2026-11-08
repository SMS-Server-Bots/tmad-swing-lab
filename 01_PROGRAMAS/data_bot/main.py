import argparse
import csv
import sys


def _fmt_count_es(value) -> str:
    try:
        return f"{int(value or 0):,}".replace(",", ".")
    except Exception:
        return "0"


def _configure_console_utf8() -> None:
    """Keep Windows consoles/subprocess pipes from crashing on emoji output."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


_configure_console_utf8()
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid
import hashlib
from datetime import timedelta
from pathlib import Path
import sqlite3
import json
import httpx

import config
from archive_manager import archive_recent_weeks
from history_coverage import ensure_history_archives, mark_history_bootstrap_started, core_history_coverage
from intraday_archive import archive_intraday_date, archive_pending_intraday, current_market_date
from gex_profile_archive import archive_gex_profile_date, archive_pending_gex_profiles
from gex_profile_history import record_gex_profile_event
from gex_negative_cache import should_skip as gex_cache_should_skip, record_failure as gex_cache_record_failure, clear_success as gex_cache_clear_success
from coordination_bus import read_ack, read_heartbeat, heartbeat_age_seconds, write_heartbeat
from db_core import (
    ensure_all_schemas, create_scanner_run, update_scanner_run, insert_scanner_snapshot, insert_scanner_snapshot_delta, insert_scanner_snapshot_deltas, upsert_daily_history_from_snapshot, rebuild_canonical_daily_history,
    create_gex_run, update_gex_run, insert_gex_level, insert_gex_summary, insert_gex_expiration, insert_gex_expiry_bucket, insert_gex_failure, insert_raw_payload, purge_live_retention, insert_heartbeat, log_quality, insert_detail_zones, insert_detail_history, insert_price_history,
    create_score_run, update_score_run, insert_score_top_longs, insert_score_backtest, insert_score_basket_history,
    create_watchlist_run, update_watchlist_run, insert_watchlists, insert_watchlist_dashboard,
    cleanup_invalid_runs_after_success, rebuild_compact_history_from_raw_details,
)
from discovery import inspect_payload, flush_schema_alerts, reset_schema_alert_buffer
from discord_utils import log_info, log_error, send_quality
from pipeline_diagnostics import record_stage, stage_line, friendly_stage_line, webhook_status
from discord_presentation import build_group
from reporting_v621 import (
    send_storage_summary, send_heartbeat as send_data_heartbeat, auditor_feed_console_line,
    console_health_line, run_completion_line, gex_quality_summary,
)
from scheduler import due_jobs
from selection import compute_scanner_changes, select_hourly_enrichment
from time_utils import now_madrid, ts_utc_str, ts_madrid_str, market_session_date_str, market_date_from_madrid_iso, latest_completed_market_date, MARKET_TZ, is_operational_window, next_operational_start
from tmad_client import make_client, fetch_scanner, fetch_stock_detail, fetch_stock_intraday, fetch_stock_gex, fetch_score_top_longs, fetch_score_backtest, fetch_score_basket_history, fetch_watchlists, fetch_watchlist_dashboard
from transforms import summarize_gex_payload, build_scanner_snapshot, distance_to_price_pct
from gex_persistence import persist_gex_bundle
from partial_risk_refresh import build_partial_risk_refresh
from utils import rows_from_scanner_payload, ticker_from_row, to_float

LOCK_FILE = config.LOCK_DIR / "tmad_core_data_bot.lock"




def _gex_transient_error(exc: Exception) -> tuple[bool, int | None]:
    """Classify only retryable technical GEX failures; never retry 404 absence."""
    status_code = None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status_code = exc.response.status_code
    retryable = bool(
        (status_code is not None and (int(status_code) == 429 or int(status_code) >= 500))
        or isinstance(exc, (httpx.TimeoutException, TimeoutError))
    )
    return retryable, status_code


def _fetch_gex_with_profile_retry(client, ticker: str, profile: str):
    """Retry retryable transport/upstream failures; never retry semantic 404 absence."""
    if str(profile).lower() == "deep":
        attempts = int(config.GEX_DEEP_TECHNICAL_RETRY_ATTEMPTS)
        backoff = float(config.GEX_DEEP_TECHNICAL_RETRY_BACKOFF_SECONDS)
    else:
        attempts = int(getattr(config, "GEX_SELECTIVE_TECHNICAL_RETRY_ATTEMPTS", 2))
        backoff = float(getattr(config, "GEX_SELECTIVE_TECHNICAL_RETRY_BACKOFF_SECONDS", 1.0))
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        try:
            return fetch_stock_gex(client, ticker)
        except Exception as exc:
            retryable, status_code = _gex_transient_error(exc)
            if not retryable or attempt >= attempts:
                raise
            cprint(
                f"🔁 GEX retry técnico {str(profile).lower()} | ticker={ticker} attempt={attempt + 1}/{attempts} "
                f"status={status_code or type(exc).__name__}"
            )
            pause = backoff * attempt
            if pause > 0:
                time.sleep(pause)




def _fetch_gex_chunk(tickers: list[str], profile: str) -> dict[str, tuple[object | None, int | None, Exception | None]]:
    """Fetch one worker chunk with its own HTTP client; never write shared state/SQLite."""
    results: dict[str, tuple[object | None, int | None, Exception | None]] = {}
    worker_client = make_client()
    try:
        for ticker in tickers:
            try:
                payload, status = _fetch_gex_with_profile_retry(worker_client, ticker, profile)
                results[ticker] = (payload, status, None)
            except Exception as exc:
                results[ticker] = (None, None, exc)
    finally:
        try:
            worker_client.close()
        except Exception:
            pass
    return results


def _prefetch_selective_gex(tickers: list[str], profile: str) -> dict[str, tuple[object | None, int | None, Exception | None]]:
    """Bounded network-only prefetch for SELECTIVE GEX; deep remains sequential."""
    if (
        str(profile).lower() != "selective"
        or not bool(getattr(config, "GEX_SELECTIVE_PARALLEL_FETCH_ENABLED", True))
        or len(tickers) < 2
    ):
        return {}
    workers = max(1, min(4, int(getattr(config, "GEX_SELECTIVE_FETCH_WORKERS", 3))))
    if workers <= 1:
        return {}
    chunks = [tickers[index::workers] for index in range(workers)]
    chunks = [chunk for chunk in chunks if chunk]
    started = time.time()
    results: dict[str, tuple[object | None, int | None, Exception | None]] = {}
    with ThreadPoolExecutor(max_workers=len(chunks), thread_name_prefix="tmad-gex") as pool:
        futures = [pool.submit(_fetch_gex_chunk, chunk, profile) for chunk in chunks]
        for future in as_completed(futures):
            results.update(future.result())
    cprint(
        f"⚡ GEX SELECTIVE · prefetch paralelo completado | tickers={len(tickers)} "
        f"workers={len(chunks)} elapsed={time.time()-started:.1f}s | persistencia=secuencial"
    )
    return results


def normalize_existing_snapshot_dates_v49():
    """Normalize local-Madrid dates to the US market session date once.

    A restart after midnight Madrid used to create a false extra history date even
    though New York was still on the prior trading session. This migration updates
    every local table that contains snapshot_date + captured_at_madrid.
    """
    migration_dir = config.RUNTIME_ROOT / "migrations"
    migration_dir.mkdir(parents=True, exist_ok=True)
    done = migration_dir / "v4_9_market_session_dates.done"
    if done.exists():
        return 0
    total = 0
    for db_path in (config.SCANNER_DB, config.GEX_DB, config.RAW_DB, config.META_DB):
        if not db_path.exists():
            continue
        conn = sqlite3.connect(db_path, timeout=60)
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if not {"snapshot_date", "captured_at_madrid"}.issubset(cols):
                    continue
                changes = []
                for rowid, captured, old_date in conn.execute(
                    f"SELECT rowid,captured_at_madrid,snapshot_date FROM {table} WHERE captured_at_madrid IS NOT NULL"
                ):
                    new_date = market_date_from_madrid_iso(captured)
                    if new_date and new_date != old_date:
                        changes.append((new_date, rowid))
                if changes:
                    conn.executemany(f"UPDATE {table} SET snapshot_date=? WHERE rowid=?", changes)
                    total += len(changes)
            conn.commit()
        finally:
            conn.close()
    done.write_text(f"normalized_rows={total}\n", encoding="utf-8")
    cprint(f"🗓️ Migración v5.0: fechas normalizadas a sesión NY | rows={total}")
    return total


def cprint(message: str):
    line = f"[{ts_madrid_str()}] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        _configure_console_utf8()
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
            print(safe, flush=True)
    try:
        if getattr(config, "FILE_LOGGING_ENABLED", True):
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = config.LOG_DIR / f"tmad_core_data_{now_madrid().strftime('%Y-%m-%d')}.log"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def write_output_json(name: str, payload: dict):
    if not getattr(config, "WRITE_OUTPUT_SUMMARIES", True):
        return
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.OUTPUT_DIR / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        try:
            print(f"[output-json-error] {e}", flush=True)
        except Exception:
            pass




def run_cleanup_invalid_runs_after_success(reason: str = ''):
    if not config.CLEAN_INVALID_RUNS_AFTER_SUCCESS:
        return {}
    stats = cleanup_invalid_runs_after_success()
    total = sum(int(v or 0) for v in stats.values())
    if total:
        cprint(
            f"🧹 Cleanup invalid runs {reason} | "
            f"scanner_runs={stats.get('scanner_invalid_runs_deleted')} "
            f"scanner_rows={stats.get('scanner_child_rows_deleted')} | "
            f"gex_runs={stats.get('gex_invalid_runs_deleted')} "
            f"gex_rows={stats.get('gex_child_rows_deleted')} | "
            f"raw={stats.get('raw_rows_deleted')} | extra={stats.get('extra_fields_deleted')}"
        )
    return stats

def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == __import__('os').getpid():
        return True
    import os
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and int(code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            try:
                import subprocess
                completed = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True,
                    text=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                output = (completed.stdout or '').lower()
                return str(pid) in output and 'no tasks' not in output and 'ninguna tarea' not in output
            except Exception:
                return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_lock():
    import os
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding='utf-8').strip())
        except Exception:
            old_pid = 0
        if _pid_is_running(old_pid):
            raise RuntimeError(f'El Data Bot ya parece activo con PID {old_pid}.')
        if config.LOCK_AUTO_REMOVE_STALE:
            LOCK_FILE.unlink(missing_ok=True)
            cprint(f'🧹 Lock antiguo eliminado automáticamente | pid={old_pid or "inválido"}')
        else:
            raise RuntimeError(f'Existe lock antiguo y LOCK_AUTO_REMOVE_STALE=false: {LOCK_FILE}')
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()), encoding='utf-8')
    cprint(f'LOCK adquirido: {LOCK_FILE}')

def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def raw_and_discover(run_type, run_id, endpoint_type, ticker, url, status, payload, error=None):
    if not config.SAVE_RAW_PAYLOADS and error is None:
        phash = None
    else:
        phash = insert_raw_payload(run_type, run_id, endpoint_type, ticker, url, status, payload, error)
    if payload is not None:
        inspect_payload(endpoint_type, payload, ticker=ticker, run_type=run_type, run_id=run_id, payload_hash=phash)
    return phash


def get_scanner_rows(client):
    payload, status = fetch_scanner(client)
    rows = rows_from_scanner_payload(payload)
    return payload, status, rows


def scanner_download_integrity(scanner_meta: dict, rows: list[dict]) -> dict:
    """Compare the unique Scanner payload with TMAD's declared universe."""
    unique = {str(ticker_from_row(row) or "").upper() for row in rows}
    unique.discard("")
    try:
        expected = int(
            scanner_meta.get("_pagination_api_total")
            or scanner_meta.get("totalCount")
            or len(unique)
        )
    except Exception:
        expected = len(unique)
    explicit_complete = scanner_meta.get("_pagination_complete")
    shortfall = max(0, expected - len(unique))
    ratio = (len(unique) / max(1, expected)) if expected else 1.0
    complete = bool(len(unique) == expected and explicit_complete is not False)
    near_complete = bool(
        not complete
        and getattr(config, "SCANNER_NEAR_COMPLETE_ENABLED", True)
        and shortfall > 0
        and shortfall <= int(getattr(config, "SCANNER_NEAR_COMPLETE_MAX_SHORTFALL", 3))
        and ratio >= float(getattr(config, "SCANNER_NEAR_COMPLETE_MIN_RATIO", 0.995))
    )
    return {
        "expected": expected,
        "received_rows": len(rows),
        "unique_tickers": len(unique),
        "complete": complete,
        "near_complete": near_complete,
        "accepted": bool(complete or near_complete),
        "coverage_ratio": ratio,
        "shortfall": shortfall,
        "aggregate_rows": int(scanner_meta.get("_pagination_aggregate_rows") or len(unique)),
    }


def _resolve_enrichment_profile(session_type: str, requested: str | None = None) -> str:
    requested = (requested or "auto").strip().lower()
    if requested in {"deep", "selective"}:
        return requested
    if not config.HOURLY_SELECTIVE_ENRICHMENT:
        return "deep"
    label = (session_type or "").lower()
    if "deep" in label:
        return "deep"
    if label in {"first_run", "startup_bootstrap"}:
        return "deep" if config.DEEP_CAPTURE_ON_FIRST_RUN else "selective"
    return "selective"


def _alerts_priority_tickers() -> tuple[set[str], set[str]]:
    open_tickers: set[str] = set()
    recent_tickers: set[str] = set()
    path = config.ALERTS_DB_PATH
    if not path.exists():
        return open_tickers, recent_tickers
    try:
        conn = sqlite3.connect(path, timeout=10)
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "open_trades" in tables:
            # Every open lifecycle must be refreshed, including shadow positions.
            # Excluding non-Discord trades would prevent the learning layer from
            # observing exits and deterioration on the very positions it simulates.
            open_rows = conn.execute(
                "SELECT DISTINCT ticker FROM open_trades WHERE status='open' AND ticker IS NOT NULL"
            ).fetchall()
            open_tickers = {str(row[0]).upper() for row in open_rows}
        if "alert_events" in tables:
            cutoff = (now_madrid().date() - timedelta(days=config.RECENT_ALERT_PRIORITY_DAYS)).isoformat()
            recent_tickers = {
                str(row[0]).upper() for row in conn.execute(
                    """SELECT DISTINCT ticker FROM alert_events
                       WHERE snapshot_date>=? AND sent_to_discord=1
                         AND ticker IS NOT NULL""",
                    (cutoff,),
                ).fetchall()
            }
        conn.close()
    except Exception as exc:
        cprint(f"⚠️ No se pudieron leer tickers prioritarios del Alerts Bot: {exc}")
    return open_tickers, recent_tickers


def _previous_enriched_tickers(before_run_id: int) -> set[str]:
    if not config.SCANNER_DB.exists():
        return set()
    try:
        conn = sqlite3.connect(config.SCANNER_DB, timeout=10)
        row = conn.execute(
            "SELECT MAX(scanner_run_id) FROM scanner_snapshots WHERE scanner_run_id<? AND gex_status='ok'",
            (before_run_id,),
        ).fetchone()
        previous_run = int(row[0] or 0)
        if not previous_run:
            conn.close()
            return set()
        tickers = {
            str(row[0]).upper() for row in conn.execute(
                "SELECT DISTINCT ticker FROM scanner_snapshots WHERE scanner_run_id=? AND gex_status='ok'",
                (previous_run,),
            ).fetchall()
        }
        conn.close()
        return tickers
    except Exception as exc:
        cprint(f"⚠️ No se pudo cargar continuidad del enriquecimiento: {exc}")
        return set()



def _previous_scanner_snapshot_rows(before_run_id: int) -> tuple[int, dict[str, dict]]:
    """Load the complete previous successful Scanner checkpoint."""
    if not config.SCANNER_DB.exists():
        return 0, {}
    conn = sqlite3.connect(config.SCANNER_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT id FROM scanner_runs
               WHERE id<? AND status='success'
                 AND COALESCE(base_unique_tickers,0)>0
               ORDER BY id DESC LIMIT 1""",
            (int(before_run_id),),
        ).fetchone()
        previous_run_id = int(row["id"] or 0) if row else 0
        if not previous_run_id:
            return 0, {}
        rows = conn.execute(
            "SELECT * FROM scanner_snapshots WHERE scanner_run_id=?",
            (previous_run_id,),
        ).fetchall()
        return previous_run_id, {
            str(item["ticker"]).upper(): dict(item)
            for item in rows if str(item["ticker"] or "").strip()
        }
    finally:
        conn.close()


def _write_scanner_changes(run_id: int, session_type: str, report: list[dict], summary: dict) -> None:
    payload = {
        "version": config.BOT_VERSION,
        "scanner_run_id": int(run_id),
        "session_type": session_type,
        "generated_at_madrid": ts_madrid_str(),
        **summary,
        "tickers": report,
    }
    write_output_json("latest_scanner_changes.json", payload)
    if getattr(config, "WRITE_SCANNER_DIFF_CSV", True):
        try:
            config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            path = config.OUTPUT_DIR / "latest_scanner_changes.csv"
            fieldnames = [
                "ticker", "change_kind", "meaningful_change", "change_score",
                "changed_fields", "field_deltas", "previous_run_id",
            ]
            with path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for item in report:
                    row = dict(item)
                    row["changed_fields"] = ",".join(item.get("changed_fields") or [])
                    row["field_deltas"] = json.dumps(item.get("field_deltas") or {}, ensure_ascii=False, sort_keys=True)
                    writer.writerow(row)
        except Exception as exc:
            cprint(f"⚠️ No se pudo escribir el CSV diferencial del Scanner: {exc}")


def _write_enrichment_selection(run_id: int, session_type: str, profile: str, report: list[dict], summary: dict) -> None:
    payload = {
        "scanner_run_id": run_id,
        "session_type": session_type,
        "profile": profile,
        "generated_at_madrid": ts_madrid_str(),
        **summary,
        "tickers": report,
    }
    write_output_json("latest_enrichment_selection.json", payload)
    if not config.WRITE_ENRICHMENT_SELECTION_CSV:
        return
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.OUTPUT_DIR / "latest_enrichment_selection.csv"
        fieldnames = list(report[0].keys()) if report else ["rank", "ticker", "selection_reasons"]
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(report)
    except Exception as exc:
        cprint(f"⚠️ No se pudo escribir CSV de selección: {exc}")


def _write_full_scanner_universe(run_id: int, session_type: str, rows: list[dict]) -> None:
    """Export the complete current Scanner universe for graph audit/debugging."""
    if not getattr(config, "WRITE_OUTPUT_SUMMARIES", True):
        return
    try:
        unique: dict[str, dict] = {}
        for row in rows:
            ticker = ticker_from_row(row)
            if ticker:
                unique[str(ticker).upper()] = dict(row)
        payload = {
            "version": config.BOT_VERSION,
            "scanner_run_id": int(run_id),
            "session_type": session_type,
            "generated_at_madrid": ts_madrid_str(),
            "node_count": len(unique),
            "nodes": [unique[ticker] for ticker in sorted(unique)],
        }
        write_output_json("scanner_universe_latest.json", payload)
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = config.OUTPUT_DIR / "scanner_universe_latest.csv"
        fields = [
            "ticker", "name", "category", "currentPrice", "changePercent", "trend",
            "momentumScore", "rsi14", "optionSignal", "totalGex", "gexChange",
            "ivZSpread", "unusualFlowSignal", "rvol", "longScore",
            "callWallStrike", "putWallStrike", "callWallDistance", "putWallDistance",
        ]
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for ticker in sorted(unique):
                writer.writerow(unique[ticker])
        cprint(f"🧭 Universo Scanner exportado | nodos={len(unique)} | {csv_path}")
    except Exception as exc:
        cprint(f"⚠️ No se pudo exportar universo Scanner: {exc}")



def _latest_successful_scanner_universe() -> dict:
    """Return the complete ticker universe from the latest successful Scanner run."""
    result = {"run_id": 0, "snapshot_date": None, "session_type": None, "tickers": []}
    if not config.SCANNER_DB.exists():
        return result
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT id,snapshot_date,session_type
               FROM scanner_runs WHERE status='success'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if not row:
            return result
        tickers = [
            str(r[0]).upper() for r in conn.execute(
                """SELECT ticker FROM scanner_snapshots
                   WHERE scanner_run_id=? AND ticker IS NOT NULL
                   GROUP BY ticker ORDER BY MIN(rowid)""",
                (int(row["id"]),),
            ).fetchall()
            if str(r[0] or "").strip()
        ]
        return {
            "run_id": int(row["id"]),
            "snapshot_date": str(row["snapshot_date"] or ""),
            "session_type": str(row["session_type"] or ""),
            "tickers": tickers,
        }
    finally:
        conn.close()


def _core_overlap_counts(tickers: list[str]) -> dict[str, int]:
    """Count dates where both TMAD signal history and OHLC history exist per ticker."""
    counts: dict[str, int] = {}
    if not tickers or not config.SCANNER_DB.exists():
        return counts
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    try:
        for start in range(0, len(tickers), 400):
            chunk = tickers[start:start + 400]
            placeholders = ",".join(["?"] * len(chunk))
            rows = conn.execute(
                f"""SELECT s.ticker,COUNT(DISTINCT s.history_date)
                    FROM ticker_signal_daily s
                    LEFT JOIN ticker_ohlc_daily p
                      ON p.ticker=s.ticker AND p.price_date=s.history_date
                    WHERE s.ticker IN ({placeholders})
                      AND COALESCE(s.close_price,p.close,p.price) IS NOT NULL
                    GROUP BY s.ticker""",
                chunk,
            ).fetchall()
            for ticker, n in rows:
                counts[str(ticker).upper()] = int(n or 0)
    finally:
        conn.close()
    return counts


def _core_universe_fingerprint(tickers: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(tickers)).encode("utf-8")).hexdigest() if tickers else ""


CORE_COMPLETION_POLICY = "per_ticker_latest_sessions_v1"


def _core_window_fingerprint(
    universe_fingerprint: str, universe_date: str | None, dates: list[str]
) -> str:
    if not universe_fingerprint:
        return ""
    latest = str(dates[-1] if dates else universe_date or "")
    token = f"{CORE_COMPLETION_POLICY}|{universe_fingerprint}|{universe_date or ''}|{latest}"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _core_overlap_counts_for_dates(tickers: list[str], dates: list[str]) -> dict[str, int]:
    if not tickers or not dates or not config.SCANNER_DB.exists():
        return {}
    counts: dict[str, int] = {}
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    try:
        for start in range(0, len(tickers), 300):
            chunk = tickers[start:start + 300]
            ticker_ph = ",".join(["?"] * len(chunk))
            date_ph = ",".join(["?"] * len(dates))
            rows = conn.execute(
                f"""SELECT s.ticker,COUNT(DISTINCT s.history_date)
                    FROM ticker_signal_daily s
                    LEFT JOIN ticker_ohlc_daily p
                      ON p.ticker=s.ticker AND p.price_date=s.history_date
                    WHERE s.ticker IN ({ticker_ph}) AND s.history_date IN ({date_ph})
                      AND COALESCE(s.close_price,p.close,p.price) IS NOT NULL
                    GROUP BY s.ticker""",
                [*chunk, *dates],
            ).fetchall()
            for ticker, n in rows:
                counts[str(ticker).upper()] = int(n or 0)
    finally:
        conn.close()
    return counts


def _sync_core_bootstrap_states(tickers: list[str], universe: dict) -> dict:
    """Build a fair, resumable queue for the exact Scanner + canonical window."""
    target = max(1, int(config.CORE_HISTORY_TARGET_SESSIONS))
    coverage = core_history_coverage()
    dates = list(coverage.get("dates") or [])
    universe_fingerprint = _core_universe_fingerprint(tickers)
    window_fingerprint = _core_window_fingerprint(universe_fingerprint, universe.get("snapshot_date"), dates)
    # Completion is based on each ticker's own 30 valid sessions. Request
    # batches may straddle a market close and therefore need not share the same
    # exact date set.
    overlap = _core_overlap_counts(tickers)
    now_utc = ts_utc_str(); now_mad = ts_madrid_str()
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        existing: dict[str, dict] = {}
        for start in range(0, len(tickers), 350):
            chunk = tickers[start:start + 350]
            placeholders = ",".join(["?"] * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM core_bootstrap_ticker_state WHERE ticker IN ({placeholders})",
                chunk,
            ).fetchall():
                existing[str(row["ticker"]).upper()] = dict(row)
        states: dict[str, dict] = {}
        for ticker in tickers:
            row = existing.get(ticker) or {}
            same_universe = bool(row.get("universe_fingerprint") == universe_fingerprint)
            same_window = bool(same_universe and row.get("window_fingerprint") == window_fingerprint)
            carry_attempts = bool(
                same_window
                or (same_universe and not str(row.get("window_fingerprint") or ""))
            )
            count = int(overlap.get(ticker, 0))
            if count >= target:
                status = "complete"
                exclusion_reason = None
            elif same_window and str(row.get("status") or "") == "excluded":
                status = "excluded"
                exclusion_reason = row.get("exclusion_reason")
            else:
                status = "pending"
                exclusion_reason = None
            attempts = int(row.get("attempts") or 0) if carry_attempts else 0
            successful_payloads = int(row.get("successful_payloads") or 0) if carry_attempts else 0
            last_attempt = row.get("last_attempt_at_utc") if carry_attempts else None
            last_error = row.get("last_error") if carry_attempts else None
            payload = {
                "ticker": ticker,
                "universe_fingerprint": universe_fingerprint,
                "window_fingerprint": window_fingerprint,
                "universe_run_id": int(universe.get("run_id") or 0),
                "universe_date": universe.get("snapshot_date"),
                "status": status,
                "attempts": attempts,
                "successful_payloads": successful_payloads,
                "overlap_dates": count,
                "last_error": last_error,
                "exclusion_reason": exclusion_reason,
                "last_attempt_at_utc": last_attempt,
                "updated_at_utc": now_utc,
                "updated_at_madrid": now_mad,
            }
            conn.execute(
                """INSERT INTO core_bootstrap_ticker_state(
                       ticker,universe_fingerprint,window_fingerprint,universe_run_id,universe_date,
                       status,attempts,successful_payloads,overlap_dates,last_error,exclusion_reason,
                       last_attempt_at_utc,updated_at_utc,updated_at_madrid
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     universe_fingerprint=excluded.universe_fingerprint,
                     window_fingerprint=excluded.window_fingerprint,
                     universe_run_id=excluded.universe_run_id,universe_date=excluded.universe_date,
                     status=excluded.status,attempts=excluded.attempts,
                     successful_payloads=excluded.successful_payloads,
                     overlap_dates=excluded.overlap_dates,last_error=excluded.last_error,
                     exclusion_reason=excluded.exclusion_reason,
                     last_attempt_at_utc=excluded.last_attempt_at_utc,
                     updated_at_utc=excluded.updated_at_utc,updated_at_madrid=excluded.updated_at_madrid""",
                tuple(payload.values()),
            )
            states[ticker] = payload
        conn.commit()
    finally:
        conn.close()
    return {
        "dates": dates,
        "target": target,
        "universe_fingerprint": universe_fingerprint,
        "window_fingerprint": window_fingerprint,
        "states": states,
    }


def _update_core_bootstrap_ticker_state(
    ticker: str, queue: dict, *, status: str, overlap_dates: int,
    last_error: str | None = None, exclusion_reason: str | None = None,
    successful_payload: bool = False,
) -> None:
    state = dict((queue.get("states") or {}).get(ticker) or {})
    attempts = int(state.get("attempts") or 0) + 1
    successful_payloads = int(state.get("successful_payloads") or 0) + (1 if successful_payload else 0)
    now_utc = ts_utc_str(); now_mad = ts_madrid_str()
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    try:
        conn.execute(
            """UPDATE core_bootstrap_ticker_state SET status=?,attempts=?,successful_payloads=?,
                   overlap_dates=?,last_error=?,exclusion_reason=?,last_attempt_at_utc=?,
                   updated_at_utc=?,updated_at_madrid=? WHERE ticker=? AND universe_fingerprint=?
                   AND window_fingerprint=?""",
            (status, attempts, successful_payloads, int(overlap_dates), last_error, exclusion_reason,
             now_utc, now_utc, now_mad, ticker, queue.get("universe_fingerprint"), queue.get("window_fingerprint")),
        )
        conn.commit()
    finally:
        conn.close()
    state.update({
        "status": status, "attempts": attempts, "successful_payloads": successful_payloads,
        "overlap_dates": int(overlap_dates), "last_error": last_error,
        "exclusion_reason": exclusion_reason, "last_attempt_at_utc": now_utc,
    })
    queue.setdefault("states", {})[ticker] = state


def _write_core_exclusions_report(coverage: dict) -> None:
    try:
        payload = {
            "generated_at_madrid": now_madrid().isoformat(),
            "universe_tickers": int(coverage.get("universe_tickers") or 0),
            "eligible_tickers": int(coverage.get("complete_tickers") or 0),
            "excluded_tickers": int(coverage.get("excluded_tickers") or 0),
            "pending_tickers": int(coverage.get("pending_tickers") or coverage.get("remaining_tickers") or 0),
            "excluded_sample": coverage.get("excluded_ticker_sample") or [],
            "excluded_reasons": coverage.get("excluded_ticker_reasons") or {},
            "pending_sample": coverage.get("pending_ticker_sample") or [],
            "universe_fingerprint": coverage.get("universe_fingerprint"),
            "window_fingerprint": coverage.get("window_fingerprint"),
            "quality": coverage.get("universe_quality"),
        }
        path = config.OUTPUT_DIR / "core_bootstrap_resolution.json"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        cprint(f"⚠️ No se pudo escribir core_bootstrap_resolution.json: {exc}")


def _write_core_bootstrap_state(payload: dict) -> None:
    try:
        path = config.CORE_BOOTSTRAP_STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        cprint(f"⚠️ No se pudo persistir core_bootstrap_state.json: {exc}")


def run_core_history_bootstrap(max_tickers: int | None = None, reason: str = "startup") -> dict:
    """Fetch TMAD detail for the full Scanner with a fair, auditable queue.

    Every current Scanner ticker is attempted. Tickers that cannot supply the
    30 valid sessions are excluded only for the exact daily universe/policy and
    are retried automatically when either changes. Transient API failures remain
    pending and cannot silently count as resolved.
    """
    limit = max(1, int(max_tickers or config.CORE_BOOTSTRAP_STARTUP_MAX_TICKERS))
    stats = {
        "reason": reason,
        "enabled": bool(config.CORE_BOOTSTRAP_FETCH_ON_START),
        "universe_run_id": 0,
        "universe_date": None,
        "universe_tickers": 0,
        "already_complete": 0,
        "already_excluded": 0,
        "selected": 0,
        "attempted": 0,
        "successful": 0,
        "failed": 0,
        "excluded_now": 0,
        "signal_rows": 0,
        "price_rows": 0,
        "remaining": 0,
        "errors": [],
        "started_at_madrid": now_madrid().isoformat(),
    }
    if not config.CORE_BOOTSTRAP_FETCH_ON_START:
        stats["status"] = "disabled"
        stats["finished_at_madrid"] = now_madrid().isoformat()
        _write_core_bootstrap_state(stats)
        return stats

    universe = _latest_successful_scanner_universe()
    tickers = list(universe.get("tickers") or [])
    stats["universe_run_id"] = int(universe.get("run_id") or 0)
    stats["universe_date"] = universe.get("snapshot_date")
    stats["universe_tickers"] = len(tickers)
    if not tickers:
        stats["status"] = "waiting_for_scanner_universe"
        stats["finished_at_madrid"] = now_madrid().isoformat()
        _write_core_bootstrap_state(stats)
        return stats

    queue = _sync_core_bootstrap_states(tickers, universe)
    states = queue.get("states") or {}
    complete = [ticker for ticker in tickers if str((states.get(ticker) or {}).get("status")) == "complete"]
    excluded = [ticker for ticker in tickers if str((states.get(ticker) or {}).get("status")) == "excluded"]
    pending = [ticker for ticker in tickers if ticker not in set(complete) | set(excluded)]
    order_index = {ticker: idx for idx, ticker in enumerate(tickers)}
    pending.sort(key=lambda ticker: (
        int((states.get(ticker) or {}).get("attempts") or 0),
        str((states.get(ticker) or {}).get("last_attempt_at_utc") or ""),
        order_index.get(ticker, 10**9),
    ))
    selected = pending[:limit]
    stats["already_complete"] = len(complete)
    stats["already_excluded"] = len(excluded)
    stats["selected"] = len(selected)
    stats["remaining"] = len(pending)
    stats["window_dates"] = len(queue.get("dates") or [])
    stats["window_fingerprint"] = queue.get("window_fingerprint")

    if not selected:
        coverage = core_history_coverage()
        _write_core_exclusions_report(coverage)
        stats["status"] = "complete" if coverage.get("ready") else "waiting_resolution"
        stats["remaining"] = int(coverage.get("remaining_tickers") or 0)
        stats["complete_tickers"] = int(coverage.get("complete_tickers") or 0)
        stats["excluded_tickers"] = int(coverage.get("excluded_tickers") or 0)
        stats["coverage"] = coverage
        stats["finished_at_madrid"] = now_madrid().isoformat()
        _write_core_bootstrap_state(stats)
        cprint(
            "🧱 [CORE BOOTSTRAP END] "
            f"reason={reason} status={stats['status']} attempted=0 ok=0 failed=0 excluded_now=0 "
            f"complete={stats['complete_tickers']} excluded={stats['excluded_tickers']} remaining={stats['remaining']} "
            f"ready={bool(coverage.get('ready'))} | pending={','.join((coverage.get('pending_ticker_sample') or [])[:8]) or 'none'} "
            f"excluded_sample={','.join((coverage.get('excluded_ticker_sample') or [])[:8]) or 'none'}"
        )
        return stats

    cprint(
        f"🧱 CORE bootstrap independiente | reason={reason} | selected={len(selected)}/{len(tickers)} "
        f"| eligible={len(complete)} | excluded={len(excluded)} | pending={len(pending)} "
        f"| source_run={stats['universe_run_id']}"
    )
    selected_diag = []
    for selected_ticker in selected[:8]:
        selected_state = states.get(selected_ticker) or {}
        last_error = " ".join(str(selected_state.get('last_error') or '').split())
        if len(last_error) > 55:
            last_error = last_error[:54] + '…'
        selected_diag.append(
            f"{selected_ticker}(attempts={int(selected_state.get('attempts') or 0)},"
            f"overlap={int(selected_state.get('overlap_dates') or 0)}/{int(queue.get('target') or 0)},"
            f"last={last_error or 'none'})"
        )
    cprint(
        "🧱 [CORE BOOTSTRAP START] "
        f"reason={reason} source_run={stats['universe_run_id']} window_dates={len(queue.get('dates') or [])} "
        f"selected={len(selected)} pending={len(pending)} | sample={';'.join(selected_diag) or 'none'}"
    )
    client = make_client()
    target = int(queue.get("target") or config.CORE_HISTORY_TARGET_SESSIONS)
    dates = list(queue.get("dates") or [])
    try:
        for idx, ticker in enumerate(selected, start=1):
            stats["attempted"] += 1
            signal_rows = price_rows = 0
            try:
                url = config.STOCK_DETAIL_URL_TEMPLATE.format(ticker=ticker)
                payload, status = fetch_stock_detail(client, ticker)
                data = (payload or {}).get("data") or {}
                history = data.get("history") or []
                price_history = data.get("priceHistory") or []
                if not isinstance(history, list) or not history:
                    raise ValueError("detail sin data.history")
                if not isinstance(price_history, list) or not price_history:
                    raise ValueError("detail sin data.priceHistory")
                phash = raw_and_discover(
                    "core_bootstrap", int(stats["universe_run_id"] or 0), "detail",
                    ticker, url, status, payload,
                )
                signal_rows = int(insert_detail_history(
                    int(stats["universe_run_id"] or 0), "core_history_bootstrap",
                    ticker, payload, phash,
                ) or 0)
                price_rows = int(insert_price_history(
                    int(stats["universe_run_id"] or 0), "core_history_bootstrap",
                    ticker, payload, phash,
                ) or 0)
                if signal_rows <= 0 or price_rows <= 0:
                    raise ValueError(f"materialización vacía signal={signal_rows} price={price_rows}")
                overlap = _core_overlap_counts([ticker]).get(ticker, 0)
                previous = (queue.get("states") or {}).get(ticker) or {}
                next_attempt = int(previous.get("attempts") or 0) + 1
                if int(overlap) >= target:
                    ticker_status = "complete"
                    exclusion_reason = None
                elif len(dates) >= target and next_attempt >= 2:
                    ticker_status = "excluded"
                    exclusion_reason = f"insufficient_per_ticker_history_after_{next_attempt}_attempts:{int(overlap)}/{target}"
                    stats["excluded_now"] += 1
                else:
                    ticker_status = "retry"
                    exclusion_reason = None
                _update_core_bootstrap_ticker_state(
                    ticker, queue, status=ticker_status, overlap_dates=int(overlap),
                    last_error=None, exclusion_reason=exclusion_reason, successful_payload=True,
                )
                stats["successful"] += 1
                stats["signal_rows"] += signal_rows
                stats["price_rows"] += price_rows
            except Exception as exc:
                stats["failed"] += 1
                message = f"{ticker}: {exc}"
                if len(stats["errors"]) < 25:
                    stats["errors"].append(message)
                previous = (queue.get("states") or {}).get(ticker) or {}
                next_attempt = int(previous.get("attempts") or 0) + 1
                text = str(exc).lower()
                deterministic = any(fragment in text for fragment in (
                    "sin data.history", "sin data.pricehistory", "materialización vacía",
                ))
                overlap = _core_overlap_counts([ticker]).get(ticker, 0)
                isolated_transient = bool(
                    not deterministic
                    and int(config.CORE_BOOTSTRAP_ISOLATED_QUARANTINE_MAX_PENDING) > 0
                    and len(pending) <= int(config.CORE_BOOTSTRAP_ISOLATED_QUARANTINE_MAX_PENDING)
                    and len(tickers) >= 100
                    and int(overlap) >= max(0, target - int(config.CORE_BOOTSTRAP_ISOLATED_QUARANTINE_MAX_GAP))
                )
                terminal = bool(
                    (deterministic and next_attempt >= 2 and len(dates) >= target)
                    or isolated_transient
                )
                ticker_status = "excluded" if terminal else "retry"
                if isolated_transient:
                    exclusion_reason = f"isolated_transient_quarantine:{type(exc).__name__}:{int(overlap)}/{target}"
                    cprint(
                        f"🟨 CORE ticker aislado en cuarentena | ticker={ticker} overlap={int(overlap)}/{target} "
                        f"pending={len(pending)} error={type(exc).__name__}; no bloquea CORE global"
                    )
                elif terminal:
                    exclusion_reason = f"deterministic_detail_failure_after_{next_attempt}_attempts:{exc}"
                else:
                    exclusion_reason = None
                if terminal:
                    stats["excluded_now"] += 1
                _update_core_bootstrap_ticker_state(
                    ticker, queue, status=ticker_status, overlap_dates=int(overlap),
                    last_error=str(exc), exclusion_reason=exclusion_reason, successful_payload=False,
                )
                try:
                    raw_and_discover(
                        "core_bootstrap", int(stats["universe_run_id"] or 0), "detail",
                        ticker, config.STOCK_DETAIL_URL_TEMPLATE.format(ticker=ticker),
                        None, None, str(exc),
                    )
                except Exception:
                    pass
            if idx == 1 or idx % 25 == 0 or idx == len(selected):
                cprint(
                    f"   CORE bootstrap {idx}/{len(selected)} | ok={stats['successful']} "
                    f"| failed={stats['failed']} | excluded_now={stats['excluded_now']} | ticker={ticker}"
                )
                partial = dict(stats)
                partial["status"] = "running"
                partial["finished_at_madrid"] = None
                _write_core_bootstrap_state(partial)
            if config.CORE_BOOTSTRAP_REQUEST_PAUSE_SECONDS:
                time.sleep(config.CORE_BOOTSTRAP_REQUEST_PAUSE_SECONDS)
    finally:
        try:
            client.close()
        except Exception:
            pass

    # Reconcile per-ticker session coverage and preserve only current-policy exclusions.
    final_queue = _sync_core_bootstrap_states(tickers, universe)
    coverage = core_history_coverage()
    _write_core_exclusions_report(coverage)
    stats["remaining"] = int(coverage.get("remaining_tickers") or 0)
    stats["complete_tickers"] = int(coverage.get("complete_tickers") or 0)
    stats["excluded_tickers"] = int(coverage.get("excluded_tickers") or 0)
    stats["resolved_tickers"] = int(coverage.get("resolved_tickers") or 0)
    stats["coverage"] = coverage
    stats["status"] = (
        "complete" if coverage.get("ready")
        else ("progress" if stats["successful"] > 0 or stats["excluded_now"] > 0 else "error")
    )
    stats["finished_at_madrid"] = now_madrid().isoformat()
    _write_core_bootstrap_state(stats)
    final_states = final_queue.get("states") or {}
    pending_diag = []
    excluded_diag = []
    target_sessions = int(final_queue.get("target") or config.CORE_HISTORY_TARGET_SESSIONS)
    for ticker in tickers:
        state = final_states.get(ticker) or {}
        status_value = str(state.get("status") or "")
        last_error = " ".join(str(state.get("last_error") or "").split())
        exclusion_reason = " ".join(str(state.get("exclusion_reason") or "").split())
        if len(last_error) > 45:
            last_error = last_error[:44] + "…"
        if len(exclusion_reason) > 45:
            exclusion_reason = exclusion_reason[:44] + "…"
        item = (
            f"{ticker}(a={int(state.get('attempts') or 0)},"
            f"overlap={int(state.get('overlap_dates') or 0)}/{target_sessions},"
            f"reason={exclusion_reason or last_error or 'pending'})"
        )
        if status_value == "excluded" and len(excluded_diag) < 8:
            excluded_diag.append(item)
        elif status_value not in {"complete", "excluded"} and len(pending_diag) < 8:
            pending_diag.append(item)
    cprint(
        "🧱 [CORE BOOTSTRAP END] "
        f"reason={reason} status={stats['status']} attempted={stats['attempted']} ok={stats['successful']} "
        f"failed={stats['failed']} excluded_now={stats['excluded_now']} complete={stats['complete_tickers']} "
        f"excluded={stats['excluded_tickers']} remaining={stats['remaining']} ready={bool(coverage.get('ready'))} | "
        f"pending={';'.join(pending_diag) or 'none'} | excluded_sample={';'.join(excluded_diag) or 'none'}"
    )
    return stats


def _has_valid_deep_for_date(snapshot_date: str) -> bool:
    if not snapshot_date or not config.SCANNER_DB.exists():
        return False
    conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
    try:
        row = conn.execute(
            """SELECT COUNT(*) FROM scanner_runs
               WHERE snapshot_date=? AND status='success'
                 AND LOWER(COALESCE(session_type,'')) LIKE '%deep%'
                 AND MAX(COALESCE(base_unique_tickers,0),COALESCE(total_tickers_ok,0))>=?""",
            (snapshot_date, int(config.MIN_VALID_RUN_SNAPSHOTS)),
        ).fetchone()
        return int(row[0] or 0) > 0
    finally:
        conn.close()


def startup_deep_repair_plan(status_before: dict, now_value=None) -> dict:
    """Plan a truthful deep repair for the most recent completed US session."""
    now_value = now_value or now_madrid()
    completed = latest_completed_market_date(now_value).isoformat()
    current_market_date = market_session_date_str(now_value)
    latest = status_before.get("latest_scanner_success") or {}
    latest_date = str(latest.get("snapshot_date") or "")
    plan = {
        "completed_market_date": completed,
        "current_market_date": current_market_date,
        "latest_scanner_success_date": latest_date or None,
        "target_date": None,
        "repairable_now": False,
        "deep_missing": False,
        "reason": "No hay cierre deep pendiente.",
    }
    if not latest_date or latest_date > completed:
        plan["reason"] = "No existe un Scanner SUCCESS de una sesión ya cerrada para reparar."
        return plan
    if _has_valid_deep_for_date(latest_date):
        plan["target_date"] = latest_date
        plan["reason"] = f"La sesión {latest_date} ya tiene cierre deep válido."
        return plan
    plan["target_date"] = latest_date
    plan["deep_missing"] = True
    if latest_date == completed and current_market_date == completed:
        plan["repairable_now"] = True
        plan["reason"] = f"Falta deep de {latest_date} y Nueva York continúa en esa misma fecha tras el cierre."
    else:
        plan["reason"] = (
            f"Falta deep de {latest_date}, pero ya no puede capturarse honestamente como esa fecha "
            f"(market_date actual={current_market_date}). No se fabricará un cierre retroactivo."
        )
    return plan


def run_scanner_capture(session_type: str = 'manual', enrichment_profile: str | None = None):
    start = time.time()
    profile = _resolve_enrichment_profile(session_type, enrichment_profile)
    cprint(f'▶️ Iniciando captura SCANNER | session_type={session_type} | enrichment_profile={profile}')
    reset_schema_alert_buffer()
    client = make_client()
    run_id = create_scanner_run(session_type=session_type, total_expected=0)
    cprint(f'🧾 Scanner run creado | run_id={run_id}')
    cprint(
        "▶️ [DATA RUN START] "
        f"run={run_id} session={session_type} profile={profile} market_date={market_session_date_str()} | "
        f"hourly_cap={config.HOURLY_ENRICH_MAX_TICKERS} deep_cap={config.DEEP_ENRICH_MAX_TICKERS} "
        f"fetch_detail={bool(config.FETCH_DETAIL)} fetch_intraday={bool(config.FETCH_INTRADAY)} fetch_gex={bool(config.FETCH_GEX)}"
    )
    ok = failed = 0
    paired_gex_run_id = None
    paired_gex_ok = paired_gex_failed = paired_gex_cached = 0
    gex_persist_seconds = 0.0
    paired_gex_failures: list[dict] = []
    paired_levels_count = paired_expirations_count = paired_buckets_count = 0
    scanner_meta = {}
    intraday_result: dict = {}
    intraday_error: str | None = None
    profile_archive_result: dict = {}
    profile_archive_error: str | None = None
    try:
        cprint('🌐 Descargando /api/scanner...')
        scanner_payload, scanner_status, rows = get_scanner_rows(client)
        scanner_meta = scanner_payload.get('data', {}) if scanner_payload else {}
        pagination_debug = scanner_meta.get('_pagination_debug') or []
        best_rows = scanner_meta.get('_pagination_best_rows')
        cprint(f'📥 Scanner recibido | status={scanner_status} | rows={len(rows)} | best_rows={best_rows}')
        if pagination_debug:
            # Imprime un resumen breve de paginación para ver si TMAD está capando a 500.
            shown = 0
            for d in pagination_debug:
                if d.get('page') in (0, 1) or d.get('added_unique', 0) > 0:
                    cprint(f"   Página scanner | strategy={d.get('strategy')} | page={d.get('page')} | rows={d.get('rows')} | added={d.get('added_unique')} | unique={d.get('unique_total')} | api_total={d.get('api_total_count')}")
                    shown += 1
                if shown >= 12:
                    cprint('   ... pagination debug truncado en consola')
                    break
        raw_scanner_hash = raw_and_discover('scanner', run_id, 'scanner', None, config.SCANNER_URL, scanner_status, scanner_payload)
        integrity = scanner_download_integrity(scanner_meta, rows)
        total_expected = int(integrity["expected"] or len(rows))
        update_scanner_run(run_id, 'running', ok=0, failed=0, total_expected=total_expected, scanner_meta=scanner_meta, completion_stage='scanner_downloaded')
        if integrity.get("near_complete"):
            scanner_meta["_integrity_status"] = "accepted_near_complete"
            scanner_meta["_integrity_shortfall"] = int(integrity.get("shortfall") or 0)
            scanner_meta["_integrity_coverage_ratio"] = float(integrity.get("coverage_ratio") or 0.0)
            message = (
                "Scanner casi completo aceptado con tolerancia acotada | "
                f"unique={integrity['unique_tickers']}/{total_expected} | "
                f"shortfall={integrity['shortfall']} | coverage={integrity['coverage_ratio']*100:.2f}%"
            )
            cprint(f"🟨 {message}. El run continúa y quedará marcado en metadata.")
            send_quality(build_group(
                "🟨 DATA · SCANNER RECONCILIADO",
                ["\n".join([
                        f"**Run:** `{run_id}` · **Cobertura:** `{integrity['unique_tickers']}/{total_expected}` (`{integrity['coverage_ratio']*100:.2f}%`)",
                    f"**Diferencia:** `{integrity['shortfall']}` tickers · máximo permitido `{getattr(config, 'SCANNER_NEAR_COMPLETE_MAX_SHORTFALL', 3)}`",
                    "**Acción:** captura aceptada con tolerancia controlada; continúa el enriquecimiento y el handoff queda marcado como reconciliado.",
                ])],
            ))
        if getattr(config, "SCANNER_STRICT_TOTAL_VALIDATION", True) and not integrity.get("accepted", integrity["complete"]):
            message = (
                "Scanner incompleto tras reintentos de paginación | "
                f"unique={integrity['unique_tickers']}/{total_expected} | "
                f"shortfall={integrity['shortfall']} | aggregate={integrity['aggregate_rows']}"
            )
            update_scanner_run(
                run_id, 'partial', ok=int(integrity['unique_tickers']),
                failed=int(integrity['shortfall']), total_expected=total_expected,
                duration_seconds=time.time()-start, error_message=message,
                scanner_meta=scanner_meta, base_rows_saved=0,
                base_unique_tickers=int(integrity['unique_tickers']),
                enriched_ok=0, enriched_failed=0,
                completion_stage='scanner_pagination_incomplete',
            )
            cprint(f"🟧 {message}. Se conserva el último Scanner SUCCESS y no se entrega este run a Alerts.")
            send_quality(build_group(
                "🟧 DATA · SCANNER PARCIAL",
                ["\n".join([
                        f"**Run:** `{run_id}` · **Cobertura:** `{integrity['unique_tickers']}/{total_expected}`",
                    f"**Diferencia:** `{integrity['shortfall']}` tickers; supera la tolerancia permitida.",
                    "**Acción:** se conserva el último Scanner válido; este run no se enriquece ni se entrega a Alerts.",
                ])],
            ))
            log_quality(
                'scanner_pagination', 'warning', message, scanner_run_id=run_id,
                metadata={**integrity, 'session_type': session_type, 'profile': profile},
            )
            try:
                risk_refresh = build_partial_risk_refresh(
                    scanner_run_id=run_id,
                    session_type=session_type,
                    rows=rows,
                    integrity=integrity,
                    client=client,
                )
                cprint(
                    "🛡️ [DATA RISK REFRESH] "
                    f"partial_run={run_id} open={len(risk_refresh.get('open_tickers') or [])} "
                    f"refreshed={len(risk_refresh.get('items') or [])} "
                    f"missing={len(risk_refresh.get('missing_open_tickers') or [])} "
                    "entries=DISABLED learning=DISABLED"
                )
            except Exception as risk_exc:
                cprint(f"⚠️ Position risk refresh no disponible para partial run={run_id}: {risk_exc}")
                log_quality(
                    'position_risk_refresh', 'warning', str(risk_exc), scanner_run_id=run_id,
                    metadata={'session_type': session_type, 'trace': traceback.format_exc()[:1800]},
                )
            write_output_json("latest_scanner_run.json", {
                "run_id": run_id, "session_type": session_type,
                "enrichment_profile": profile, "stored_ok": 0,
                "enriched_ok": 0, "failed": int(integrity['shortfall']),
                "expected": total_expected, "received_unique": int(integrity['unique_tickers']),
                "status": "partial", "reason": "scanner_pagination_incomplete",
                "finished_at_madrid": ts_madrid_str(),
            })
            cprint(
                "🟧 [DATA RUN END] "
                f"scanner_run={run_id} session={session_type} profile={profile} elapsed={time.time()-start:.1f}s "
                f"status=PARTIAL_PAGINATION scanner={integrity['unique_tickers']}/{total_expected} "
                f"shortfall={integrity['shortfall']} handoff=NOT_PUBLISHED"
            )
            cprint(console_health_line(context=f"partial run={run_id}"))
            flush_schema_alerts(f'scanner pagination incomplete run_id={run_id} session={session_type}')
            return run_id

        # El Scanner completo se persiste siempre. Las llamadas caras detail/GEX se limitan
        # a un universo explicable cada hora; una captura deep diaria refresca todo el universo.
        scanner_only_saved = 0
        all_tickers: list[tuple[str, dict]] = []
        seen_tickers: set[str] = set()
        for row in rows:
            ticker = ticker_from_row(row)
            if not ticker:
                continue
            ticker = str(ticker).upper()
            if ticker not in seen_tickers:
                all_tickers.append((ticker, row))
                seen_tickers.add(ticker)

        all_tickers = all_tickers[:config.DETAIL_TOP_N]
        _write_full_scanner_universe(run_id, session_type, rows)

        previous_scanner_run_id, previous_scanner_rows = _previous_scanner_snapshot_rows(run_id)
        if getattr(config, "HOURLY_SCANNER_DIFF_ENABLED", True):
            change_map, change_report, change_summary = compute_scanner_changes(
                [row for _ticker, row in all_tickers],
                previous_scanner_rows,
                previous_run_id=previous_scanner_run_id,
            )
        else:
            change_map, change_report, change_summary = {}, [], {
                "previous_run_id": previous_scanner_run_id,
                "baseline_checkpoint": not bool(previous_scanner_rows),
                "current_tickers": len(all_tickers),
                "previous_tickers": len(previous_scanner_rows),
                "changed_tickers": 0, "new_tickers": 0,
                "unchanged_tickers": len(all_tickers), "removed_tickers": 0,
            }
        _write_scanner_changes(run_id, session_type, change_report, change_summary)
        delta_captured_utc = ts_utc_str()
        delta_captured_madrid = ts_madrid_str()
        delta_rows = [
            {
                "scanner_run_id": int(run_id),
                "previous_scanner_run_id": int(previous_scanner_run_id or 0) or None,
                "captured_at_utc": delta_captured_utc,
                "captured_at_madrid": delta_captured_madrid,
                "snapshot_date": market_session_date_str(),
                "session_type": session_type,
                "ticker": item.get("ticker"),
                "change_kind": item.get("change_kind") or "unknown",
                "meaningful_change": 1 if item.get("meaningful_change") else 0,
                "change_score": float(item.get("change_score") or 0.0),
                "changed_fields_json": json.dumps(item.get("changed_fields") or [], ensure_ascii=False),
                "field_deltas_json": json.dumps(item.get("field_deltas") or {}, ensure_ascii=False, sort_keys=True),
                "created_at_utc": delta_captured_utc,
            }
            for item in change_report
        ]
        insert_scanner_snapshot_deltas(delta_rows)
        update_scanner_run(
            run_id, 'running', ok=0, failed=0, total_expected=total_expected, scanner_meta=scanner_meta,
            completion_stage='scanner_diff_computed',
            previous_scanner_run_id=previous_scanner_run_id or None,
            scanner_changed_tickers=int(change_summary.get('changed_tickers') or 0),
            scanner_new_tickers=int(change_summary.get('new_tickers') or 0),
            scanner_removed_tickers=int(change_summary.get('removed_tickers') or 0),
            scanner_unchanged_tickers=int(change_summary.get('unchanged_tickers') or 0),
        )
        cprint(
            f"🔄 Diff Scanner completo | previous_run={previous_scanner_run_id or 'none'} "
            f"| changed={change_summary.get('changed_tickers', 0)} "
            f"| new={change_summary.get('new_tickers', 0)} "
            f"| unchanged={change_summary.get('unchanged_tickers', 0)} "
            f"| removed={change_summary.get('removed_tickers', 0)}"
        )

        open_tickers, recent_alert_tickers = _alerts_priority_tickers()
        previous_enriched = _previous_enriched_tickers(run_id)
        if profile == "deep":
            tickers = all_tickers[:config.DEEP_ENRICH_MAX_TICKERS]
            selection_report = [
                {
                    "rank": idx,
                    "ticker": ticker,
                    "priority_score": None,
                    "selection_reasons": "daily_deep",
                    "scanner_change_kind": (change_map.get(ticker) or {}).get("change_kind"),
                    "scanner_change_score": (change_map.get(ticker) or {}).get("change_score"),
                    "scanner_changed_fields": ",".join((change_map.get(ticker) or {}).get("changed_fields") or []),
                    "scanner_gex_change": to_float(row.get("gexChange")),
                    "nearest_wall_pct": None,
                    "rvol": to_float(row.get("rvol")),
                    "option_signal": row.get("optionSignal"),
                    "unusual_flow_signal": row.get("unusualFlowSignal"),
                    "long_score": to_float(row.get("longScore")),
                    "momentum_score": to_float(row.get("momentumScore")),
                    "option_volume_z": None,
                }
                for idx, (ticker, row) in enumerate(tickers, start=1)
            ]
            selection_summary = {
                "scanner_unique_tickers": len(all_tickers),
                "selected_tickers": len(tickers),
                "configured_cap": config.DEEP_ENRICH_MAX_TICKERS,
                "mandatory_tickers": len((open_tickers | recent_alert_tickers | config.ALWAYS_ENRICH_TICKERS) & seen_tickers),
                "open_trade_tickers": len(open_tickers & seen_tickers),
                "recent_alert_tickers": len(recent_alert_tickers & seen_tickers),
                "always_tickers": len(config.ALWAYS_ENRICH_TICKERS & seen_tickers),
                "previous_enriched_candidates": len(previous_enriched & seen_tickers),
                "changed_candidates": int(change_summary.get("changed_tickers") or 0),
                "new_ticker_candidates": int(change_summary.get("new_tickers") or 0),
                "selected_changed": sum(1 for ticker, _row in tickers if (change_map.get(ticker) or {}).get("meaningful_change")),
                "reason_counts": {"daily_deep": len(tickers)},
            }
        else:
            selective_cap = config.FIRST_RUN_ENRICH_MAX_TICKERS if session_type in {"first_run", "startup_bootstrap"} else config.HOURLY_ENRICH_MAX_TICKERS
            tickers, selection_report, selection_summary = select_hourly_enrichment(
                [row for _ticker, row in all_tickers],
                max_tickers=selective_cap,
                always_tickers=config.ALWAYS_ENRICH_TICKERS,
                open_trade_tickers=open_tickers,
                recent_alert_tickers=recent_alert_tickers,
                previous_enriched_tickers=previous_enriched,
                change_map=change_map,
                top_changed=selective_cap,
                top_gex_change=config.HOURLY_TOP_GEX_CHANGE,
                top_near_wall=config.HOURLY_TOP_NEAR_WALL,
                top_rvol=config.HOURLY_TOP_RVOL,
                top_signals=config.HOURLY_TOP_SIGNALS,
                continuity_max=config.HOURLY_CONTINUITY_TICKERS,
            )

        # Hard safety guard: a selective hourly run may never silently fall back to the
        # full scanner universe. Mandatory open/recent tickers may exceed the configured cap,
        # but otherwise the cap is enforced before any expensive endpoint is called.
        mandatory_count = int(selection_summary.get('mandatory_tickers') or 0)
        expected_cap = max(int(config.HOURLY_ENRICH_MAX_TICKERS), mandatory_count) if profile == 'selective' else int(config.DEEP_ENRICH_MAX_TICKERS)
        if profile == 'selective' and len(tickers) > expected_cap:
            cprint(f'🟥 SAFETY: selección horaria excede cap | selected={len(tickers)} | cap={expected_cap}. Recortando antes de llamadas detail/GEX.')
            tickers = tickers[:expected_cap]
            selection_report = selection_report[:expected_cap]
            selection_summary['selected_tickers'] = len(tickers)
            selection_summary['safety_cap_applied'] = True

        selection_summary["scanner_diff"] = change_summary
        selected_set = {ticker for ticker, _row in tickers}
        _write_enrichment_selection(run_id, session_type, profile, selection_report, selection_summary)
        reasons_short = ", ".join(
            f"{key}={value}" for key, value in list((selection_summary.get("reason_counts") or {}).items())[:8]
        ) or "none"
        cprint(
            f"🎯 Universo de enriquecimiento | profile={profile} | selected={len(tickers)}/{len(all_tickers)} "
            f"| open={selection_summary.get('open_trade_tickers', 0)} | recent_alerts={selection_summary.get('recent_alert_tickers', 0)} "
            f"| reasons={reasons_short}"
        )

        if config.SAVE_SCANNER_ROWS_BEFORE_ENRICH:
            cprint(f'💾 Guardando snapshot base scanner-only para {len(rows)} filas antes de enriquecer...')
        for row in rows:
            ticker = ticker_from_row(row)
            if not ticker:
                continue
            ticker = str(ticker).upper()
            if config.SAVE_SCANNER_ROWS_BEFORE_ENRICH:
                selected = ticker in selected_set
                statuses0 = {
                    'detail_status': 'pending' if selected and config.FETCH_DETAIL else 'skipped',
                    'intraday_status': 'pending' if selected and config.FETCH_INTRADAY else 'skipped',
                    'gex_status': 'pending' if selected and config.FETCH_GEX else ('not_selected' if config.FETCH_GEX else 'skipped'),
                    'error_message': None if selected else f'not_selected:{profile}',
                }
                snap0 = build_scanner_snapshot(run_id, session_type, ticker, row, None, None, {}, {'scanner': raw_scanner_hash}, statuses0)
                insert_scanner_snapshot(snap0)
                if profile == "deep":
                    upsert_daily_history_from_snapshot(snap0)
                scanner_only_saved += 1
        if config.SAVE_SCANNER_ROWS_BEFORE_ENRICH:
            cprint(f'✅ Snapshot base guardado | rows={scanner_only_saved}')
            update_scanner_run(run_id, 'running', ok=scanner_only_saved, failed=0, total_expected=total_expected, scanner_meta=scanner_meta, base_rows_saved=scanner_only_saved, base_unique_tickers=len(seen_tickers), enriched_ok=0, enriched_failed=0, completion_stage=f'base_snapshot_saved_{profile}')

        if config.PERSIST_GEX_FROM_SCANNER and config.FETCH_GEX:
            paired_gex_run_id = create_gex_run(
                session_type=f"{session_type}_paired",
                total_tickers=len(tickers),
            )
            cprint(f'🔗 GEX emparejado con Scanner | gex_run_id={paired_gex_run_id} | tickers={len(tickers)}')
        cprint(f'🔎 Tickers a enriquecer con detail/intraday/GEX: {len(tickers)} de {len(seen_tickers)} únicos')
        gex_cache_decisions = {}
        gex_prefetch = {}
        if config.FETCH_GEX:
            for gex_ticker, gex_scanner_row in tickers:
                gex_cache_decisions[gex_ticker] = gex_cache_should_skip(gex_ticker, gex_scanner_row)
            gex_prefetch = _prefetch_selective_gex(
                [gex_ticker for gex_ticker, _row in tickers if not gex_cache_decisions[gex_ticker][0]],
                profile,
            )
        for idx, (ticker, scanner_row) in enumerate(tickers, start=1):
            if idx == 1 or idx % 25 == 0 or idx == len(tickers):
                cprint(f'   Procesando ticker {idx}/{len(tickers)}: {ticker}')
            detail_payload = intraday_payload = gex_payload = None
            hashes = {'scanner': raw_scanner_hash}
            statuses = {'detail_status': 'skipped', 'intraday_status': 'skipped', 'gex_status': 'skipped'}
            errors = []
            try:
                if config.FETCH_DETAIL:
                    try:
                        url = config.STOCK_DETAIL_URL_TEMPLATE.format(ticker=ticker)
                        detail_payload, st = fetch_stock_detail(client, ticker)
                        hashes['detail'] = raw_and_discover('scanner', run_id, 'detail', ticker, url, st, detail_payload)
                        insert_detail_zones(run_id, session_type, ticker, detail_payload, hashes.get('detail'))
                        insert_detail_history(run_id, session_type, ticker, detail_payload, hashes.get('detail'))
                        if config.FETCH_PRICE_HISTORY:
                            insert_price_history(run_id, session_type, ticker, detail_payload, hashes.get('detail'))
                        statuses['detail_status'] = 'ok'
                    except Exception as e:
                        statuses['detail_status'] = 'error'; errors.append(f'detail={e}')
                        raw_and_discover('scanner', run_id, 'detail', ticker, config.STOCK_DETAIL_URL_TEMPLATE.format(ticker=ticker), None, None, str(e))
                if config.FETCH_INTRADAY:
                    try:
                        url = config.STOCK_INTRADAY_URL_TEMPLATE.format(ticker=ticker)
                        intraday_payload, st = fetch_stock_intraday(client, ticker)
                        hashes['intraday'] = raw_and_discover('scanner', run_id, 'intraday', ticker, url, st, intraday_payload)
                        statuses['intraday_status'] = 'ok'
                    except Exception as e:
                        statuses['intraday_status'] = 'error'; errors.append(f'intraday={e}')
                        raw_and_discover('scanner', run_id, 'intraday', ticker, config.STOCK_INTRADAY_URL_TEMPLATE.format(ticker=ticker), None, None, str(e))
                current_price = None
                if detail_payload:
                    current_price = to_float((detail_payload.get('data') or {}).get('currentPrice'))
                if current_price is None:
                    current_price = to_float(scanner_row.get('currentPrice'))
                gsum = {}
                if config.FETCH_GEX:
                    cached_skip, cached_entry = gex_cache_decisions.get(ticker, (False, None))
                    if cached_skip:
                        paired_gex_failed += 1
                        paired_gex_cached += 1
                        cache_code = cached_entry.get('status_code') if cached_entry else None
                        paired_gex_failures.append({
                            'ticker': ticker,
                            'kind': f'negative_cache_{cache_code or "unknown"}',
                            'status_code': cache_code,
                            'message': 'endpoint omitido por caché negativa',
                        })
                        statuses['gex_status'] = 'unavailable_cached'
                        statuses['gex_cache_status_code'] = cache_code
                        statuses['gex_cache_retry_after'] = cached_entry.get('retry_after_date') if cached_entry else None
                    else:
                      try:
                        url = config.STOCK_GEX_URL_TEMPLATE.format(ticker=ticker)
                        prefetched = gex_prefetch.get(ticker)
                        if prefetched is not None:
                            gex_payload, st, prefetch_error = prefetched
                            if prefetch_error is not None:
                                raise prefetch_error
                        else:
                            gex_payload, st = _fetch_gex_with_profile_retry(client, ticker, profile)
                        gex_cache_clear_success(ticker)
                        hashes['gex'] = raw_and_discover('scanner', run_id, 'gex', ticker, url, st, gex_payload)
                        statuses['gex_status'] = 'ok'
                        gsum, paired_levels, paired_expirations, paired_buckets = summarize_gex_payload(
                            gex_payload,
                            current_price=current_price,
                            ticker=ticker,
                            gex_run_id=paired_gex_run_id,
                        )
                        if paired_gex_run_id:
                            utc = ts_utc_str(); mad = ts_madrid_str()
                            change_pct = to_float(scanner_row.get('changePercent'))
                            profile_event = record_gex_profile_event(
                                paired_gex_run_id,
                                f"{session_type}_paired",
                                ticker,
                                gex_payload,
                                captured_at_utc=utc,
                                captured_at_madrid=mad,
                            )
                            summary_row = {
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'current_price': current_price,
                                'change_percent': change_pct,
                                **gsum,
                                'profile_event_kind': profile_event.get('event_kind'),
                                'profile_hash': profile_event.get('profile_hash'),
                                'profile_changed_fields': profile_event.get('changed_fields', 0),
                                'profile_changed_strikes': profile_event.get('changed_strikes', 0),
                                'profile_changed_expiries': profile_event.get('changed_expiries', 0),
                                'profile_material_change': 1 if profile_event.get('material_change') else 0,
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                            }
                            level_rows = [{
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'current_price': current_price,
                                'change_percent': change_pct,
                                'distance_to_price_pct': distance_to_price_pct(lv.get('strike'), current_price),
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                                **lv,
                            } for lv in paired_levels]
                            expiration_rows = [{
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'current_price': current_price,
                                'change_percent': change_pct,
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                                **exp,
                            } for exp in paired_expirations]
                            bucket_rows = [{
                                'gex_run_id': paired_gex_run_id,
                                'captured_at_utc': utc,
                                'captured_at_madrid': mad,
                                'snapshot_date': market_date_from_madrid_iso(mad),
                                'session_type': f"{session_type}_paired",
                                'ticker': ticker,
                                'raw_gex_hash': hashes.get('gex'),
                                'created_at_utc': utc,
                                **bucket,
                            } for bucket in paired_buckets]
                            persist_started = time.perf_counter()
                            persisted = persist_gex_bundle(summary_row, level_rows, expiration_rows, bucket_rows)
                            gex_persist_seconds += time.perf_counter() - persist_started
                            paired_levels_count += int(persisted.get('levels') or 0)
                            paired_expirations_count += int(persisted.get('expirations') or 0)
                            paired_buckets_count += int(persisted.get('buckets') or 0)
                            paired_gex_ok += 1
                      except Exception as e:
                        paired_gex_failed += 1
                        statuses['gex_status'] = 'error'; errors.append(f'gex={e}')
                        raw_and_discover('scanner', run_id, 'gex', ticker, config.STOCK_GEX_URL_TEMPLATE.format(ticker=ticker), None, None, str(e))
                        status_code = None
                        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                            status_code = e.response.status_code
                        error_text = str(e)
                        if status_code == 404:
                            error_kind = 'http_404_not_available'
                        elif status_code == 429:
                            error_kind = 'http_429_rate_limit'
                        elif status_code and int(status_code) >= 500:
                            error_kind = 'http_5xx_upstream'
                        elif status_code:
                            error_kind = f'http_{status_code}'
                        elif isinstance(e, (httpx.TimeoutException, TimeoutError)):
                            error_kind = 'timeout'
                        elif 'json' in error_text.lower() or 'decode' in error_text.lower():
                            error_kind = 'payload_parse'
                        else:
                            error_kind = type(e).__name__.lower()
                        paired_gex_failures.append({
                            'ticker': ticker,
                            'kind': error_kind,
                            'status_code': status_code,
                            'message': error_text[:180],
                        })
                        if paired_gex_run_id:
                            gex_cache_record_failure(ticker, status_code, market_session_date_str())
                            utc = ts_utc_str(); mad = ts_madrid_str()
                            try:
                                insert_gex_failure({
                                    'gex_run_id': paired_gex_run_id,
                                    'captured_at_utc': utc,
                                    'captured_at_madrid': mad,
                                    'snapshot_date': market_date_from_madrid_iso(mad),
                                    'session_type': f"{session_type}_paired",
                                    'ticker': ticker,
                                    'url': config.STOCK_GEX_URL_TEMPLATE.format(ticker=ticker),
                                    'status_code': status_code,
                                    'error_type': 'not_available' if status_code == 404 else 'error',
                                    'error_message': str(e)[:1000],
                                    'created_at_utc': utc,
                                })
                            except Exception:
                                pass
                statuses['error_message'] = '; '.join(errors) if errors else None
                snap = build_scanner_snapshot(run_id, session_type, ticker, scanner_row, detail_payload, intraday_payload, gsum, hashes, statuses)
                insert_scanner_snapshot(snap)
                if profile == "deep":
                    upsert_daily_history_from_snapshot(snap)
                ok += 1
                if ok % 25 == 0 or idx == len(tickers):
                    cprint(f'   Avance SCANNER | ok={ok} | failed={failed} | último={ticker}')
                    update_scanner_run(run_id, 'running', ok=max(ok, scanner_only_saved), failed=failed, total_expected=total_expected, scanner_meta=scanner_meta, base_rows_saved=scanner_only_saved, base_unique_tickers=len(seen_tickers), enriched_ok=ok, enriched_failed=failed, completion_stage='enriching')
            except Exception as e:
                failed += 1
                cprint(f'⚠️ Error capturando ticker {ticker}: {e}')
                log_quality('ticker_capture', 'error', str(e), scanner_run_id=run_id, ticker=ticker, metadata={'trace': traceback.format_exc()[:4000]})
        stored_ok = max(ok, scanner_only_saved if 'scanner_only_saved' in locals() else 0)
        ratio = stored_ok / max(total_expected, 1)
        status = 'success' if stored_ok >= config.MIN_VALID_RUN_SNAPSHOTS and ratio >= config.MIN_VALID_RUN_SNAPSHOT_RATIO else 'partial'
        update_scanner_run(run_id, status, ok=stored_ok, failed=failed, total_expected=total_expected, duration_seconds=time.time()-start, scanner_meta=scanner_meta, base_rows_saved=scanner_only_saved if 'scanner_only_saved' in locals() else None, base_unique_tickers=len(seen_tickers) if 'seen_tickers' in locals() else None, enriched_ok=ok, enriched_failed=failed, completion_stage='finished')
        paired_gex_status = None
        if paired_gex_run_id:
            paired_ratio = paired_gex_ok / max(1, len(tickers))
            if profile == 'selective':
                minimum_ok = min(max(1, config.HOURLY_MIN_GEX_OK_TICKERS), max(1, len(tickers)))
                paired_gex_status = 'success' if paired_gex_ok >= minimum_ok and paired_ratio >= config.HOURLY_MIN_GEX_OK_RATIO else ('partial' if paired_gex_ok > 0 else 'failed')
            else:
                paired_gex_status = 'success' if paired_gex_ok > 0 and paired_ratio >= config.BOOTSTRAP_MIN_GEX_OK_RATIO else ('partial' if paired_gex_ok > 0 else 'failed')
            update_gex_run(
                paired_gex_run_id,
                paired_gex_status,
                ok=paired_gex_ok,
                failed=paired_gex_failed,
                levels_count=paired_levels_count,
                duration_seconds=time.time()-start,
            )
            cprint(
                f'🔗 GEX emparejado finalizado | run_id={paired_gex_run_id} | status={paired_gex_status} '
                f'| ok={paired_gex_ok} | failed={paired_gex_failed} | cache={paired_gex_cached} | strikes={paired_levels_count} '
                f'| expiries={paired_expirations_count} | buckets={paired_buckets_count}'
            )
            cprint(
                f"🗄️ [GEX PERSIST] scanner_run={run_id} gex_run={paired_gex_run_id} "
                f"sqlite_seconds={gex_persist_seconds:.2f} mode=atomic_per_ticker"
            )
            sample_text = ','.join(
                f"{item.get('ticker')}:{item.get('kind')}" for item in paired_gex_failures[:12]
            ) or 'none'
            failed_open = sorted({str(item.get('ticker') or '') for item in paired_gex_failures} & set(open_tickers))
            gex_quality_state = gex_quality_summary(
                ok=paired_gex_ok, selected=len(tickers), failures=paired_gex_failures, failed_open=failed_open,
            )
            gex_quality = str(gex_quality_state['quality'])
            gex_coverage_pct = float(gex_quality_state['universe_coverage_pct'])
            reason_text = ','.join(f'{key}:{value}' for key, value in sorted(gex_quality_state['counts'].items())) or 'none'
            technical_text = ','.join(f'{key}:{value}' for key, value in sorted(gex_quality_state['technical_counts'].items())) or 'none'
            cprint(
                '🧲 [DATA GEX QUALITY] '
                f'run={run_id} gex_run={paired_gex_run_id} quality={gex_quality} '
                f'universe={paired_gex_ok}/{len(tickers)} ({gex_coverage_pct:.1f}%) '
                f'supported={paired_gex_ok}/{gex_quality_state["supported"]} ({gex_quality_state["supported_coverage_pct"]:.1f}%) '
                f'unavailable={gex_quality_state["unavailable"]} technical_failed={gex_quality_state["technical_failed"]} cached={paired_gex_cached} '
                f'| reasons={reason_text} technical={technical_text} '
                f'failed_open={len(failed_open)}:{",".join(failed_open[:8]) or "none"} | sample={sample_text}'
            )
            if (
                int(gex_quality_state.get("technical_failed") or 0) > 0
                or bool(failed_open)
                or gex_quality in {"DEGRADED", "PARTIAL", "DEGRADED_OPEN_POSITION", "HEALTHY_WITH_TECHNICAL_GAPS"}
            ):
                send_quality(build_group(
                    "🧲 DATA · CALIDAD GEX",
                    ["\n".join([
                        f"**Run:** `{run_id}` · **GEX:** `{paired_gex_run_id}` · **Estado:** `{gex_quality}`",
                        f"**Cobertura total:** `{paired_gex_ok}/{len(tickers)}` (`{gex_coverage_pct:.1f}%`) · **soportada:** `{paired_gex_ok}/{gex_quality_state['supported']}` (`{gex_quality_state['supported_coverage_pct']:.1f}%`)",
                        f"**No disponible:** `{gex_quality_state['unavailable']}` · **fallos técnicos:** `{gex_quality_state['technical_failed']}` · **posiciones afectadas:** `{len(failed_open)}`",
                        f"**Detalle técnico:** `{technical_text}` · muestra `{sample_text}`",
                    ])],
                ))
        if status == 'success':
            run_cleanup_invalid_runs_after_success('after scanner success')

        if status == 'success':
            if config.FETCH_SCORE:
                try:
                    run_score_capture(f'{session_type}_score', archive_after=False)
                except Exception as e:
                    cprint(f'⚠️ Captura SCORE post-scanner falló: {e}')
            if config.FETCH_WATCHLIST:
                try:
                    run_watchlist_capture(f'{session_type}_watchlist', archive_after=False)
                except Exception as e:
                    cprint(f'⚠️ Captura WATCHLIST post-scanner falló: {e}')
        if status == 'success' and profile == 'deep' and config.INTRADAY_ARCHIVE_ENABLED and config.INTRADAY_ARCHIVE_AFTER_DEEP:
            try:
                close_date = market_date_from_madrid_iso(ts_madrid_str())
                intraday_result = archive_intraday_date(close_date)
                cprint(
                    f"📦 Intradía archivado | date={close_date} | status={intraday_result.get('status')} "
                    f"| scanner_runs={intraday_result.get('scanner_runs', 0)} | gex_runs={intraday_result.get('gex_runs', 0)} "
                    f"| zip={intraday_result.get('zip')}"
                )
            except Exception as e:
                intraday_error = str(e)
                cprint(f'⚠️ No se pudo archivar/purgar la capa intradía: {e}')
                log_quality('intraday_archive', 'warning', str(e), scanner_run_id=run_id, metadata={'trace': traceback.format_exc()[:4000]})

        if status == 'success' and profile == 'deep' and config.GEX_PROFILE_ARCHIVE_ENABLED and config.GEX_PROFILE_ARCHIVE_AFTER_DEEP:
            try:
                close_date = market_date_from_madrid_iso(ts_madrid_str())
                profile_archive_result = archive_gex_profile_date(close_date)
                cprint(
                    f"🧬 Perfiles GEX archivados | date={close_date} | status={profile_archive_result.get('status')} "
                    f"| events={profile_archive_result.get('counts', {}).get('events', 0)} "
                    f"| tickers={profile_archive_result.get('counts', {}).get('tickers', 0)} "
                    f"| zip={profile_archive_result.get('zip')}"
                )
            except Exception as e:
                profile_archive_error = str(e)
                cprint(f'⚠️ No se pudo archivar/purgar la cadena de perfiles GEX: {e}')
                log_quality('gex_profile_archive', 'warning', str(e), scanner_run_id=run_id, metadata={'trace': traceback.format_exc()[:4000]})

        cprint('🗄️ Actualizando cobertura histórica y archivos semanales...')
        coverage = ensure_history_archives(bootstrap_complete=True)
        cprint(
            f"📚 Cobertura histórica | status={coverage['status']} | "
            f"daily_closes={coverage['paired_trading_dates']}/{coverage['target_daily_closes']} | "
            f"calendar_span={coverage['coverage_calendar_days']} días | zips={coverage['contributing_zip_count']}"
        )
        cprint('🧹 Aplicando retención live...')
        purge_live_retention()
        capture_kind = "CIERRE DEEP" if profile == "deep" else "INTRADÍA"
        scanner_reconciled = bool(integrity.get("near_complete"))
        scanner_stage = record_stage(
            "D20", "WARN" if scanner_reconciled else ("OK" if status == "success" else "ERROR"),
            f"Scanner {'reconciliado' if scanner_reconciled else 'completo'} · {capture_kind}",
            expected={"rows": total_expected, "minimum_valid": config.MIN_VALID_RUN_SNAPSHOTS},
            found={
                "saved": stored_ok, "expected": total_expected, "status": status, "failed": failed,
                "near_complete": scanner_reconciled, "shortfall": integrity.get("shortfall", 0),
                "coverage_ratio": integrity.get("coverage_ratio"),
            },
            action=(
                "Universo persistido con tolerancia mínima documentada."
                if scanner_reconciled else
                ("Universo completo persistido." if status == "success" else "No entregar el run a Alerts; conservar diagnóstico.")
            ),
            reason=(
                f"TMAD declaró {total_expected} pero devolvió {stored_ok}; diferencia dentro de tolerancia near-complete."
                if scanner_reconciled else
                (None if status == "success" else "La captura Scanner no alcanzó el mínimo de validez.")
            ),
            details={"scanner_run_id": run_id, "session_type": session_type},
        )
        gex_stage_status = "OK" if paired_gex_status == "success" else ("WARN" if paired_gex_ok else "ERROR")
        gex_stage = record_stage(
            "D21", gex_stage_status, f"GEX granular · {capture_kind}",
            expected={"selected": len(tickers), "profile": profile},
            found={
                "ok": paired_gex_ok,
                "unavailable_or_failed": paired_gex_failed,
                "negative_cache_skipped": paired_gex_cached,
                "status": paired_gex_status,
                "strikes": paired_levels_count,
                "expirations": paired_expirations_count,
            },
            action=("GEX listo para reglas." if paired_gex_status == "success" else "Alerts usará solo tickers con GEX válido."),
            reason=(None if paired_gex_status == "success" else "Algunos endpoints GEX no respondieron o no existen."),
            details={"gex_run_id": paired_gex_run_id},
        )
        if profile == "deep":
            archive_state = str(intraday_result.get("status") or "")
            if intraday_error:
                archive_stage_status = "ERROR"
                archive_reason = intraday_error
                archive_action = "No purgar filas intradía; conservar live para evitar pérdida."
            elif archive_state == "archived":
                archive_stage_status = "OK"
                archive_reason = None
                archive_action = "ZIP verificado; capa horaria retirada de live."
            elif archive_state == "nothing_to_archive":
                archive_stage_status = "SKIP"
                archive_reason = "No había runs horarios de esa sesión."
                archive_action = "Mantener el cierre deep; no hay ZIP horario nuevo."
            else:
                archive_stage_status = "WARN"
                archive_reason = f"Estado de archivo no confirmado: {archive_state or 'vacío'}."
                archive_action = "Revisar intraday_archive antes de purgar."
        else:
            archive_stage_status = "SKIP"
            archive_reason = "El ZIP intradía se crea después del cierre deep."
            archive_action = "Mantener este run en live hasta el cierre."
        archive_stage = record_stage(
            "D22", archive_stage_status, "Archivo ZIP de la capa intradía",
            expected="Tras el deep: ZIP verificado antes de borrar runs horarios",
            found={
                "status": intraday_result.get("status"),
                "scanner_runs": intraday_result.get("scanner_runs", 0),
                "gex_runs": intraday_result.get("gex_runs", 0),
                "zip": intraday_result.get("zip"),
                "gex_profile_archive_status": profile_archive_result.get("status"),
                "gex_profile_events": profile_archive_result.get("counts", {}).get("events", 0),
                "gex_profile_zip": profile_archive_result.get("zip"),
            },
            action=archive_action, reason=archive_reason,
        )
        coverage_stage = record_stage(
            "D23", "OK" if coverage.get("bootstrap_complete") else "WARN", "Cobertura diaria publicada",
            expected=f"Manifest con rolling {config.DAILY_CLOSE_WINDOW_SESSIONS} cierres deep",
            found={
                "daily_closes": int(coverage.get("paired_trading_dates") or 0),
                "target": int(coverage.get("target_daily_closes") or config.DAILY_CLOSE_WINDOW_SESSIONS),
                "coverage_end": coverage.get("coverage_end_date"),
                "weekly_zips": int(coverage.get("contributing_zip_count") or 0),
            },
            action="Alerts Bot conciliará este número con sus observaciones.",
        )
        handoff_ok = bool(
            status == "success"
            and paired_gex_status == "success"
            and coverage.get("bootstrap_complete")
            and (profile != "deep" or (not intraday_error and not profile_archive_error))
        )
        handoff_stage = record_stage(
            "D24", "OK" if handoff_ok else "WARN", "Entrega del run al Alerts Bot",
            expected="Scanner válido + GEX válido + manifest final; en deep, archivo intradía sin error",
            found={
                "scanner_run_id": run_id,
                "gex_run_id": paired_gex_run_id,
                "scanner_status": status,
                "gex_status": paired_gex_status,
                "manifest_complete": coverage.get("bootstrap_complete"),
                "intraday_archive_error": intraday_error,
                "gex_profile_archive_error": profile_archive_error,
            },
            action=("Alerts puede procesar el run." if handoff_ok else "Alerts procesará solo datos válidos y mantendrá gates de seguridad."),
        )
        archive_found = archive_stage.get("found") if isinstance(archive_stage.get("found"), dict) else {}
        core_dates = int(coverage.get("core_history_dates") or 0)
        core_target = int(coverage.get("core_history_target_sessions") or config.CORE_HISTORY_TARGET_SESSIONS)
        full_dates = int(coverage.get("paired_trading_dates") or 0)
        full_target = int(coverage.get("target_daily_closes") or config.DAILY_CLOSE_WINDOW_SESSIONS)
        scanner_note = " · reconciliado" if scanner_reconciled else ""
        archive_line = (
            f"**Archivo:** intradía `{archive_found.get('scanner_runs',0)}` Scanner + `{archive_found.get('gex_runs',0)}` GEX"
            f" · perfiles GEX `{_fmt_count_es(archive_found.get('gex_profile_events',0))}` eventos"
            if profile == "deep" else
            "**Archivo:** este run permanece en live hasta el cierre deep"
        )
        msg = build_group(
            f"📥 DATA · {str(capture_kind).upper()}",
            ["\n".join([
                f"**Scanner:** `{stored_ok}/{total_expected}` tickers{scanner_note} · enriquecidos `{ok}` · fallos `{failed}`.",
                f"**GEX:** `{paired_gex_ok}/{len(tickers)}` disponibles · sin GEX `{paired_gex_failed}` · caché `{paired_gex_cached}` · strikes `{_fmt_count_es(paired_levels_count)}` · expiraciones `{_fmt_count_es(paired_expirations_count)}`.",
                f"{archive_line}",
                f"**Cobertura:** CORE `{core_dates}/{core_target}` · FULL DEEP `{full_dates}/{full_target}`.",
                f"**Handoff:** `{'PUBLICADO' if handoff_ok else 'CON AVISOS'}` · pendiente de ACK de Alerts.",
            ])],
            context=f"**TMAD:** `v{config.BOT_VERSION}` · **Sesión:** `{session_type}` · **Scanner run:** `{run_id}` · **GEX run:** `{paired_gex_run_id or 'pendiente'}`",
            footer="El detalle técnico y las rutas permanecen en pipeline_diagnostics.jsonl; Discord muestra solo el estado operativo.",
)
        cprint(msg.replace("\n", " | "))
        cprint(run_completion_line(
            run_id=run_id, session_type=session_type, profile=profile,
            elapsed_seconds=time.time()-start, scanner_ok=stored_ok, scanner_expected=total_expected,
            scanner_failed=failed, changed_tickers=int(change_summary.get("changed_tickers") or 0),
            new_tickers=int(change_summary.get("new_tickers") or 0), selected_tickers=len(tickers),
            gex_run_id=paired_gex_run_id, gex_ok=paired_gex_ok, gex_selected=len(tickers),
            gex_failed=paired_gex_failed, strikes=paired_levels_count, expirations=paired_expirations_count,
            gex_failures=paired_gex_failures, gex_failed_open=failed_open if paired_gex_run_id else [],
            coverage=coverage, handoff_ok=handoff_ok,
        ))
        cprint(auditor_feed_console_line(context=f"run={run_id} {session_type}"))
        cprint(console_health_line(context=f"run end={run_id}"))
        if config.SEND_RUN_SUMMARY:
            delivered = log_info(msg)
            if config.DISCORD_WEBHOOK_URL_DATA_LOG and not delivered:
                cprint("⚠️ [D24] Falló la entrega al canal Data Log; revisa tmad_data_discord_delivery_error_*.log.")
        write_output_json("latest_scanner_run.json", {
            "run_id": run_id,
            "session_type": session_type,
            "enrichment_profile": profile,
            "enrichment_selected": len(tickers),
            "enrichment_universe": len(all_tickers),
            "selection_summary": selection_summary,
            "stored_ok": stored_ok,
            "enriched_ok": ok,
            "failed": failed,
            "expected": total_expected,
            "status": status,
            "paired_gex_run_id": paired_gex_run_id,
            "paired_gex_status": paired_gex_status,
            "paired_gex_ok": paired_gex_ok,
            "paired_gex_failed": paired_gex_failed,
            "paired_gex_failures": paired_gex_failures,
            "paired_gex_levels": paired_levels_count,
            "paired_gex_expirations": paired_expirations_count,
            "paired_gex_buckets": paired_buckets_count,
            "finished_at_madrid": ts_madrid_str(),
        })
        flush_schema_alerts(f'scanner run_id={run_id} session={session_type}')
        return run_id
    except KeyboardInterrupt:
        stored_ok = max(ok, scanner_only_saved if 'scanner_only_saved' in locals() else 0)
        stage = 'interrupted_after_base_snapshot' if stored_ok > 0 else 'interrupted_before_base_snapshot'
        status = 'partial_base_saved' if stored_ok > 0 else 'interrupted'
        update_scanner_run(run_id, status, ok=stored_ok, failed=failed, duration_seconds=time.time()-start, error_message='Interrupted by user', scanner_meta=scanner_meta, base_rows_saved=scanner_only_saved if 'scanner_only_saved' in locals() else None, base_unique_tickers=len(seen_tickers) if 'seen_tickers' in locals() else None, enriched_ok=ok, enriched_failed=failed, completion_stage=stage)
        if paired_gex_run_id:
            update_gex_run(paired_gex_run_id, 'partial' if paired_gex_ok else 'interrupted', ok=paired_gex_ok, failed=paired_gex_failed, levels_count=paired_levels_count, duration_seconds=time.time()-start, error_message='Interrupted by user')
        cprint(f'🟧 TMAD scanner capture interrumpido | run_id={run_id} | stored_ok={stored_ok} | enriched_ok={ok} | failed={failed} | stage={stage}')
        cprint(
            "🟧 [DATA RUN END] "
            f"scanner_run={run_id} session={session_type} profile={profile} elapsed={time.time()-start:.1f}s "
            f"status=INTERRUPTED stage={stage} stored={stored_ok} enriched={ok} failed={failed} handoff=NOT_PUBLISHED"
        )
        cprint(console_health_line(context=f"interrupted run={run_id}"))
        flush_schema_alerts(f'scanner interrupted run_id={run_id} session={session_type}')
        raise
    except Exception as e:
        update_scanner_run(run_id, 'failed', ok=max(ok, scanner_only_saved if 'scanner_only_saved' in locals() else 0), failed=failed, duration_seconds=time.time()-start, error_message=str(e), scanner_meta=scanner_meta, base_rows_saved=scanner_only_saved if 'scanner_only_saved' in locals() else None, base_unique_tickers=len(seen_tickers) if 'seen_tickers' in locals() else None, enriched_ok=ok, enriched_failed=failed, completion_stage='failed')
        if paired_gex_run_id:
            update_gex_run(paired_gex_run_id, 'failed', ok=paired_gex_ok, failed=paired_gex_failed, levels_count=paired_levels_count, duration_seconds=time.time()-start, error_message=str(e))
        cprint(f'🟥 TMAD scanner capture failed | run_id={run_id} | {e}')
        cprint(
            "🟥 [DATA RUN END] "
            f"scanner_run={run_id} session={session_type} profile={profile} elapsed={time.time()-start:.1f}s "
            f"status=FAILED stage=failed stored={max(ok, scanner_only_saved if 'scanner_only_saved' in locals() else 0)} "
            f"enriched={ok} failed={failed} handoff=NOT_PUBLISHED error={type(e).__name__}:{e}"
        )
        cprint(console_health_line(context=f"failed run={run_id}"))
        log_error(f"🟥 TMAD scanner capture failed | run_id={run_id} | {e}\n```{traceback.format_exc()[-5000:]}```")
        flush_schema_alerts(f'scanner failed run_id={run_id} session={session_type}')
        raise
    finally:
        client.close()



def run_score_capture(session_type: str = 'manual_score', archive_after: bool = True):
    if not config.FETCH_SCORE:
        cprint('⏭️ FETCH_SCORE=false: se omite captura Score')
        return None
    start=time.time(); cprint(f'▶️ Iniciando captura SCORE | session_type={session_type}')
    reset_schema_alert_buffer()
    client=make_client(); run_id=create_score_run(session_type)
    top_count=basket_days=basket_positions=0
    try:
        url=config.SCORE_TOP_LONGS_URL; payload, st=fetch_score_top_longs(client)
        ph=raw_and_discover('score', run_id, 'score_top_longs', None, url, st, payload)
        top_count=insert_score_top_longs(run_id, payload, ph)
        cprint(f'   Score top-longs guardado | rows={top_count}')

        url=config.SCORE_BACKTEST_URL; payload, st=fetch_score_backtest(client)
        ph=raw_and_discover('score', run_id, 'score_backtest', None, url, st, payload)
        bt_counts=insert_score_backtest(run_id, payload, ph)
        cprint(f"   Score backtest guardado | equity={bt_counts.get('equity')} | mc={bt_counts.get('mc')}")

        url=config.SCORE_BASKET_HISTORY_URL; payload, st=fetch_score_basket_history(client)
        ph=raw_and_discover('score', run_id, 'score_basket_history', None, url, st, payload)
        bh_counts=insert_score_basket_history(run_id, payload, ph)
        basket_days=bh_counts.get('days',0); basket_positions=bh_counts.get('positions',0)
        cprint(f'   Score basket-history guardado | days={basket_days} | positions={basket_positions}')

        update_score_run(run_id, 'success', top_count, basket_days, basket_positions)
        msg=f"✅ TMAD SCORE capture `{session_type}` finalizado | run_id={run_id} | top_longs={top_count} | basket_days={basket_days} | basket_positions={basket_positions}"
        cprint(msg)
        if config.SEND_RUN_SUMMARY: log_info(msg)
        write_output_json('latest_score_run.json', {'run_id':run_id,'session_type':session_type,'top_longs':top_count,'basket_days':basket_days,'basket_positions':basket_positions,'status':'success','finished_at_madrid':ts_madrid_str()})
        flush_schema_alerts(f'score run_id={run_id} session={session_type}')
        if archive_after:
            archive_recent_weeks(); purge_live_retention()
        return run_id
    except Exception as e:
        update_score_run(run_id, 'failed', top_count, basket_days, basket_positions, str(e))
        cprint(f'🟥 TMAD SCORE capture failed | run_id={run_id} | {e}')
        log_error(f"🟥 TMAD SCORE capture failed | run_id={run_id} | {e}\n```{traceback.format_exc()[-1800:]}```")
        flush_schema_alerts(f'score failed run_id={run_id} session={session_type}')
        raise
    finally:
        client.close()


def run_watchlist_capture(session_type: str = 'manual_watchlist', archive_after: bool = True):
    if not config.FETCH_WATCHLIST:
        cprint('⏭️ FETCH_WATCHLIST=false: se omite captura Watchlist')
        return None
    cprint(f'▶️ Iniciando captura WATCHLIST | session_type={session_type}')
    reset_schema_alert_buffer()
    client=make_client(); run_id=create_watchlist_run(session_type)
    watchlists_count=dashboards_count=rows_count=0
    try:
        payload, st=fetch_watchlists(client)
        ph=raw_and_discover('watchlist', run_id, 'watchlists', None, config.WATCHLISTS_URL, st, payload)
        watchlists_count=insert_watchlists(run_id, payload, ph)
        items=[]
        data=payload.get('data') if isinstance(payload, dict) else None
        if isinstance(data, list): items=data
        elif isinstance(data, dict):
            for k in ('watchlists','rows','items','data'):
                if isinstance(data.get(k), list): items=data.get(k); break
        cprint(f'   Watchlists guardadas | count={watchlists_count}')
        for idx,w in enumerate(items[:config.WATCHLIST_MAX_DASHBOARDS], start=1):
            if not isinstance(w, dict): continue
            wid=w.get('id') or w.get('watchlistId') or w.get('watchlist_id') or w.get('name')
            if wid is None: continue
            try:
                url=config.WATCHLIST_DASHBOARD_URL_TEMPLATE.format(watchlist_id=wid)
                dash, st=fetch_watchlist_dashboard(client, wid)
                dph=raw_and_discover('watchlist', run_id, 'watchlist_dashboard', str(wid), url, st, dash)
                counts=insert_watchlist_dashboard(run_id, str(wid), dash, dph)
                dashboards_count+=1; rows_count += counts.get('rows',0)
                cprint(f"   Dashboard watchlist {idx}/{len(items)} id={wid} | rows={counts.get('rows')} | sections={counts.get('sections')}")
            except Exception as e:
                cprint(f'⚠️ Error dashboard watchlist id={wid}: {e}')
                log_quality('watchlist_dashboard', 'warning', str(e), ticker=str(wid), metadata={'trace': traceback.format_exc()[:2000]})
        status='success' if watchlists_count or dashboards_count else 'partial'
        update_watchlist_run(run_id, status, watchlists_count, dashboards_count, rows_count)
        msg=f"✅ TMAD WATCHLIST capture `{session_type}` finalizado | run_id={run_id} | watchlists={watchlists_count} | dashboards={dashboards_count} | rows={rows_count} | status={status}"
        cprint(msg)
        if config.SEND_RUN_SUMMARY: log_info(msg)
        write_output_json('latest_watchlist_run.json', {'run_id':run_id,'session_type':session_type,'watchlists':watchlists_count,'dashboards':dashboards_count,'rows':rows_count,'status':status,'finished_at_madrid':ts_madrid_str()})
        flush_schema_alerts(f'watchlist run_id={run_id} session={session_type}')
        if archive_after:
            archive_recent_weeks(); purge_live_retention()
        return run_id
    except Exception as e:
        update_watchlist_run(run_id, 'failed', watchlists_count, dashboards_count, rows_count, str(e))
        cprint(f'🟥 TMAD WATCHLIST capture failed | run_id={run_id} | {e}')
        log_error(f"🟥 TMAD WATCHLIST capture failed | run_id={run_id} | {e}\n```{traceback.format_exc()[-1800:]}```")
        flush_schema_alerts(f'watchlist failed run_id={run_id} session={session_type}')
        raise
    finally:
        client.close()


def run_gex_capture(session_type: str = 'manual_gex'):
    start = time.time()
    cprint(f'▶️ Iniciando captura GEX | session_type={session_type}')
    reset_schema_alert_buffer()
    client = make_client()
    gex_run_id = None
    ok = failed = cached = levels_count = expirations_count = buckets_count = 0
    failure_counts: dict[str, int] = {}
    failure_samples: list[str] = []
    try:
        cprint('🌐 Descargando scanner base para universo GEX...')
        scanner_payload, scanner_status, rows = get_scanner_rows(client)
        cprint(f'📥 Scanner base GEX recibido | status={scanner_status} | rows={len(rows)}')
        raw_and_discover('gex', None, 'scanner', None, config.SCANNER_URL, scanner_status, scanner_payload)
        tickers = []
        for row in rows[:config.DETAIL_TOP_N]:
            ticker = ticker_from_row(row)
            if ticker: tickers.append((ticker, row))
        gex_run_id = create_gex_run(session_type=session_type, total_tickers=len(tickers))
        cprint(f'🧾 GEX run creado | run_id={gex_run_id} | tickers={len(tickers)}')
        for idx, (ticker, scanner_row) in enumerate(tickers, start=1):
            if idx == 1 or idx % 25 == 0 or idx == len(tickers):
                cprint(f'   Procesando GEX {idx}/{len(tickers)}: {ticker}')
            cached_skip, cached_entry = gex_cache_should_skip(ticker, scanner_row)
            if cached_skip:
                failed += 1
                cached += 1
                if len(failure_samples) < config.GEX_FAILURE_SAMPLE_LIMIT:
                    failure_samples.append(f"{ticker}:CACHE-{cached_entry.get('status_code') if cached_entry else 'NA'}")
                continue
            try:
                url = config.STOCK_GEX_URL_TEMPLATE.format(ticker=ticker)
                gex_payload, st = fetch_stock_gex(client, ticker)
                gex_cache_clear_success(ticker)
                phash = raw_and_discover('gex', gex_run_id, 'gex', ticker, url, st, gex_payload)
                current_price = to_float(scanner_row.get('currentPrice'))
                change_pct = to_float(scanner_row.get('changePercent'))
                summary, levels, expirations, buckets = summarize_gex_payload(gex_payload, current_price=current_price, ticker=ticker, gex_run_id=gex_run_id)
                utc = ts_utc_str(); mad = ts_madrid_str()
                profile_event = record_gex_profile_event(
                    gex_run_id,
                    session_type,
                    ticker,
                    gex_payload,
                    captured_at_utc=utc,
                    captured_at_madrid=mad,
                )
                summary_row = {'gex_run_id': gex_run_id, 'captured_at_utc': utc, 'captured_at_madrid': mad, 'snapshot_date': market_date_from_madrid_iso(mad), 'session_type': session_type, 'ticker': ticker, 'current_price': current_price, 'change_percent': change_pct, **summary, 'profile_event_kind': profile_event.get('event_kind'), 'profile_hash': profile_event.get('profile_hash'), 'profile_changed_fields': profile_event.get('changed_fields', 0), 'profile_changed_strikes': profile_event.get('changed_strikes', 0), 'profile_changed_expiries': profile_event.get('changed_expiries', 0), 'profile_material_change': 1 if profile_event.get('material_change') else 0, 'raw_gex_hash': phash, 'created_at_utc': utc}
                insert_gex_summary(summary_row)
                for lv in levels:
                    insert_gex_level({'gex_run_id': gex_run_id, 'captured_at_utc': utc, 'captured_at_madrid': mad, 'snapshot_date': market_date_from_madrid_iso(mad), 'session_type': session_type, 'ticker': ticker, 'current_price': current_price, 'change_percent': change_pct, 'distance_to_price_pct': distance_to_price_pct(lv.get('strike'), current_price), 'raw_gex_hash': phash, 'created_at_utc': utc, **lv})
                    levels_count += 1
                for exp in expirations:
                    insert_gex_expiration({'gex_run_id': gex_run_id, 'captured_at_utc': utc, 'captured_at_madrid': mad, 'snapshot_date': market_date_from_madrid_iso(mad), 'session_type': session_type, 'ticker': ticker, 'current_price': current_price, 'change_percent': change_pct, 'raw_gex_hash': phash, 'created_at_utc': utc, **exp})
                    expirations_count += 1
                for bucket in buckets:
                    insert_gex_expiry_bucket({'gex_run_id': gex_run_id, 'captured_at_utc': utc, 'captured_at_madrid': mad, 'snapshot_date': market_date_from_madrid_iso(mad), 'session_type': session_type, 'ticker': ticker, 'raw_gex_hash': phash, 'created_at_utc': utc, **bucket})
                    buckets_count += 1
                ok += 1
                if ok % 25 == 0 or idx == len(tickers):
                    cprint(f'   Avance GEX | ok={ok} | failed={failed} | cache={cached} | último={ticker}')
            except Exception as e:
                failed += 1
                status_code = None
                if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                    status_code = e.response.status_code
                gex_cache_record_failure(ticker, status_code, market_session_date_str())
                error_type = "not_available" if status_code == 404 else "error"
                failure_counts[error_type] = failure_counts.get(error_type, 0) + 1
                if len(failure_samples) < config.GEX_FAILURE_SAMPLE_LIMIT:
                    failure_samples.append(f"{ticker}:{status_code or 'ERR'}")
                utc = ts_utc_str(); mad = ts_madrid_str()
                try:
                    insert_gex_failure({
                        'gex_run_id': gex_run_id,
                        'captured_at_utc': utc,
                        'captured_at_madrid': mad,
                        'snapshot_date': market_date_from_madrid_iso(mad),
                        'session_type': session_type,
                        'ticker': ticker,
                        'url': config.STOCK_GEX_URL_TEMPLATE.format(ticker=ticker),
                        'status_code': status_code,
                        'error_type': error_type,
                        'error_message': str(e)[:1000],
                        'created_at_utc': utc,
                    })
                except Exception:
                    pass

                # Los 404 son esperables en tickers OTC/futuros/fondos sin endpoint GEX.
                if status_code == 404:
                    if config.GEX_LOG_404_EACH:
                        cprint(f'⚠️ GEX no disponible para {ticker} (404)')
                    if config.GEX_404_AS_ERROR:
                        log_quality('gex_capture', 'warning', str(e), ticker=ticker, metadata={'status_code': status_code})
                else:
                    cprint(f'⚠️ Error capturando GEX {ticker}: {e}')
                    log_quality('gex_capture', 'error', str(e), ticker=ticker, metadata={'trace': traceback.format_exc()[:4000], 'status_code': status_code})

        status = 'success' if ok > 0 and (ok / max(1, len(tickers))) >= config.BOOTSTRAP_MIN_GEX_OK_RATIO else ('partial' if ok > 0 else 'failed')
        update_gex_run(gex_run_id, status, ok=ok, failed=failed, levels_count=levels_count, duration_seconds=time.time()-start)
        if status == 'success':
            run_cleanup_invalid_runs_after_success('after GEX success')
        cprint('🗄️ Actualizando cobertura histórica y archivos semanales...')
        coverage = ensure_history_archives(bootstrap_complete=True)
        cprint(
            f"📚 Cobertura histórica | status={coverage['status']} | "
            f"daily_closes={coverage['paired_trading_dates']}/{coverage['target_daily_closes']} | "
            f"calendar_span={coverage['coverage_calendar_days']} días | zips={coverage['contributing_zip_count']}"
        )
        if config.GEX_PROFILE_ARCHIVE_ENABLED and config.GEX_PROFILE_ARCHIVE_AFTER_DEEP and ("deep" in session_type.lower() or "postmarket" in session_type.lower() or "close" in session_type.lower()):
            try:
                profile_archive = archive_gex_profile_date(market_date_from_madrid_iso(ts_madrid_str()))
                cprint(f"🧬 Perfiles GEX archivados | status={profile_archive.get('status')} | zip={profile_archive.get('zip')}")
            except Exception as archive_exc:
                cprint(f"⚠️ Archivo de perfiles GEX pendiente: {archive_exc}")
                log_quality('gex_profile_archive', 'warning', str(archive_exc), metadata={'trace': traceback.format_exc()[:4000]})
        cprint('🧹 Aplicando retención live...')
        purge_live_retention()

        failure_summary = ", ".join([f"{k}={v}" for k, v in sorted(failure_counts.items())]) or "none"
        msg = f"✅ TMAD GEX capture `{session_type}` finalizado | run_id={gex_run_id} | ok={ok} | failed={failed} | strikes={levels_count} | expiries={expirations_count} | buckets={buckets_count} | status={status} | failures={failure_summary}"
        cprint(msg)
        if failure_samples:
            cprint(f"   GEX failure samples: {', '.join(failure_samples)}")
        if config.SEND_RUN_SUMMARY:
            log_info(msg + (f"\nFailure samples: {', '.join(failure_samples)}" if failure_samples else ""))

        write_output_json("latest_gex_run.json", {
            "run_id": gex_run_id,
            "session_type": session_type,
            "ok": ok,
            "failed": failed,
            "levels": levels_count,
            "expirations": expirations_count,
            "expiry_buckets": buckets_count,
            "status": status,
            "failure_counts": failure_counts,
            "failure_samples": failure_samples,
            "finished_at_madrid": ts_madrid_str(),
        })
        flush_schema_alerts(f'gex run_id={gex_run_id} session={session_type}')
        return gex_run_id
    except Exception as e:
        if gex_run_id: update_gex_run(gex_run_id, 'failed', ok=ok, failed=failed, levels_count=levels_count, duration_seconds=time.time()-start, error_message=str(e))
        cprint(f'🟥 TMAD GEX capture failed | run_id={gex_run_id} | {e}')
        log_error(f"🟥 TMAD GEX capture failed | run_id={gex_run_id} | {e}\n```{traceback.format_exc()[-1800:]}```")
        flush_schema_alerts(f'gex failed run_id={gex_run_id} session={session_type}')
        raise
    finally:
        client.close()



def _safe_scalar(db_path: Path, sql: str, params: tuple = ()): 
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        cprint(f'⚠️ No se pudo consultar {db_path.name}: {e}')
        return None


def _safe_row(db_path: Path, sql: str, params: tuple = ()): 
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        cprint(f'⚠️ No se pudo consultar {db_path.name}: {e}')
        return None


def _table_count(db_path: Path, table: str) -> int:
    val = _safe_scalar(db_path, f'SELECT COUNT(*) FROM {table}')
    return int(val or 0)


def get_latest_scanner_run(status_filter: str | None = None):
    if status_filter:
        return _safe_row(config.SCANNER_DB, "SELECT * FROM scanner_runs WHERE status=? ORDER BY captured_at_madrid DESC, id DESC LIMIT 1", (status_filter,))
    return _safe_row(config.SCANNER_DB, "SELECT * FROM scanner_runs ORDER BY captured_at_madrid DESC, id DESC LIMIT 1")


def get_latest_gex_run(status_filter: str | None = None):
    if status_filter:
        return _safe_row(config.GEX_DB, "SELECT * FROM gex_runs WHERE status=? ORDER BY captured_at_madrid DESC, id DESC LIMIT 1", (status_filter,))
    return _safe_row(config.GEX_DB, "SELECT * FROM gex_runs ORDER BY captured_at_madrid DESC, id DESC LIMIT 1")




def _scanner_run_actual_counts(run_id: int) -> dict:
    """Return actual persisted rows for a scanner run, independent from scanner_runs counters."""
    snap_count = int(_safe_scalar(config.SCANNER_DB, "SELECT COUNT(*) FROM scanner_snapshots WHERE scanner_run_id=?", (run_id,)) or 0)
    unique_count = int(_safe_scalar(config.SCANNER_DB, "SELECT COUNT(DISTINCT ticker) FROM scanner_snapshots WHERE scanner_run_id=?", (run_id,)) or 0)
    pending_detail = int(_safe_scalar(config.SCANNER_DB, "SELECT COUNT(*) FROM scanner_snapshots WHERE scanner_run_id=? AND COALESCE(detail_status,'')='pending'", (run_id,)) or 0)
    pending_intraday = int(_safe_scalar(config.SCANNER_DB, "SELECT COUNT(*) FROM scanner_snapshots WHERE scanner_run_id=? AND COALESCE(intraday_status,'')='pending'", (run_id,)) or 0)
    pending_gex = int(_safe_scalar(config.SCANNER_DB, "SELECT COUNT(*) FROM scanner_snapshots WHERE scanner_run_id=? AND COALESCE(gex_status,'')='pending'", (run_id,)) or 0)
    return {
        'snap_count': snap_count,
        'unique_count': unique_count,
        'pending_detail': pending_detail,
        'pending_intraday': pending_intraday,
        'pending_gex': pending_gex,
    }


def _gex_run_actual_counts(run_id: int) -> dict:
    """Return actual persisted GEX rows for a run, independent from gex_runs counters."""
    summary_count = int(_safe_scalar(config.GEX_DB, "SELECT COUNT(*) FROM gex_summary WHERE gex_run_id=?", (run_id,)) or 0)
    unique_count = int(_safe_scalar(config.GEX_DB, "SELECT COUNT(DISTINCT ticker) FROM gex_summary WHERE gex_run_id=?", (run_id,)) or 0)
    levels_count = int(_safe_scalar(config.GEX_DB, "SELECT COUNT(*) FROM gex_levels WHERE gex_run_id=?", (run_id,)) or 0)
    return {'summary_count': summary_count, 'unique_count': unique_count, 'levels_count': levels_count}


def _scanner_run_is_valid_success(run: dict | None) -> bool:
    if not run or run.get('status') != 'success':
        return False
    expected = int(run.get('total_tickers_expected') or 0)
    ok = int(run.get('total_tickers_ok') or 0)
    if expected > 0:
        return ok >= config.BOOTSTRAP_MIN_SCANNER_OK and (ok / max(expected, 1)) >= config.MIN_VALID_RUN_SNAPSHOT_RATIO
    return ok >= config.BOOTSTRAP_MIN_SCANNER_OK


def _gex_run_is_valid_success(run: dict | None) -> bool:
    if not run or run.get('status') != 'success':
        return False
    total = int(run.get('total_tickers') or 0)
    ok = int(run.get('ok_tickers') or 0)
    if total > 0:
        return ok > 0 and (ok / max(total, 1)) >= config.BOOTSTRAP_MIN_GEX_OK_RATIO
    return ok > 0


def get_today_valid_scanner_success(today: str):
    rows = []
    try:
        conn = sqlite3.connect(config.SCANNER_DB, timeout=30)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM scanner_runs WHERE snapshot_date=? AND status='success' ORDER BY captured_at_madrid DESC, id DESC", (today,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        cprint(f'⚠️ No se pudo validar scanner success de hoy: {e}')
    for r in rows:
        if _scanner_run_is_valid_success(r):
            return r
    return None


def get_today_valid_gex_success(today: str):
    rows = []
    try:
        conn = sqlite3.connect(config.GEX_DB, timeout=30)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM gex_runs WHERE snapshot_date=? AND status='success' ORDER BY captured_at_madrid DESC, id DESC", (today,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        cprint(f'⚠️ No se pudo validar GEX success de hoy: {e}')
    for r in rows:
        if _gex_run_is_valid_success(r):
            return r
    return None


def repair_incomplete_run_metadata():
    """Reconcile run headers with the actual detail rows stored in SQLite.

    This solves the case where a Ctrl+C happens after data has been inserted but before
    the run header counters were updated. It does NOT delete partial data; it marks it
    as partial and lets bootstrap decide whether to launch a fresh complete run.
    """
    if not config.BOOTSTRAP_REPAIR_RUN_METADATA_ON_START:
        return
    utc = ts_utc_str()
    scanner_updates = 0
    gex_updates = 0

    try:
        conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT * FROM scanner_runs
            WHERE status IS NULL OR status NOT IN ('success')
            ORDER BY id ASC
        """).fetchall()
        for row in rows:
            run = dict(row)
            rid = int(run['id'])
            counts = _scanner_run_actual_counts(rid)
            actual = counts['snap_count']
            unique = counts['unique_count']
            expected = int(run.get('total_tickers_expected') or 0)
            if actual <= 0:
                if run.get('status') == 'running':
                    new_status = 'interrupted'
                    stage = 'startup_repaired_no_rows'
                else:
                    continue
            else:
                ratio = actual / max(expected, actual, 1)
                # If the process was interrupted, never promote to success automatically.
                # A fresh successful run is required to consider the day complete.
                new_status = 'partial_base_saved' if ratio >= config.MIN_VALID_RUN_SNAPSHOT_RATIO else 'partial'
                stage = 'startup_repaired_partial_rows'
                if counts['pending_detail'] or counts['pending_intraday'] or counts['pending_gex']:
                    stage = 'startup_repaired_base_pending_enrichment'
            cur.execute("""
                UPDATE scanner_runs
                SET status=?,
                    total_tickers_ok=MAX(COALESCE(total_tickers_ok,0), ?),
                    base_rows_saved=MAX(COALESCE(base_rows_saved,0), ?),
                    base_unique_tickers=MAX(COALESCE(base_unique_tickers,0), ?),
                    enriched_ok=COALESCE(enriched_ok,0),
                    enriched_failed=COALESCE(enriched_failed,0),
                    completion_stage=COALESCE(completion_stage, ?),
                    error_message=COALESCE(error_message, ?),
                    updated_at_utc=?
                WHERE id=?
            """, (new_status, actual, actual, unique, stage, f'Run metadata reconciled at startup: actual_rows={actual}, unique={unique}', utc, rid))
            scanner_updates += 1
        conn.commit(); conn.close()
    except Exception as e:
        cprint(f'⚠️ No se pudo reparar metadata de scanner_runs: {e}')

    try:
        conn = sqlite3.connect(config.GEX_DB, timeout=60)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT * FROM gex_runs
            WHERE status IS NULL OR status NOT IN ('success')
            ORDER BY id ASC
        """).fetchall()
        for row in rows:
            run = dict(row)
            rid = int(run['id'])
            counts = _gex_run_actual_counts(rid)
            actual = counts['summary_count']
            levels = counts['levels_count']
            if actual <= 0:
                if run.get('status') == 'running':
                    new_status = 'interrupted'
                else:
                    continue
            else:
                new_status = 'partial'
            cur.execute("""
                UPDATE gex_runs
                SET status=?,
                    ok_tickers=MAX(COALESCE(ok_tickers,0), ?),
                    levels_count=MAX(COALESCE(levels_count,0), ?),
                    error_message=COALESCE(error_message, ?),
                    updated_at_utc=?
                WHERE id=?
            """, (new_status, actual, levels, f'GEX run metadata reconciled at startup: summary={actual}, levels={levels}', utc, rid))
            gex_updates += 1
        conn.commit(); conn.close()
    except Exception as e:
        cprint(f'⚠️ No se pudo reparar metadata de gex_runs: {e}')

    if scanner_updates or gex_updates:
        cprint(f'🧩 Metadata de runs recalibrada contra datos reales | scanner={scanner_updates} | gex={gex_updates}')
        try:
            log_quality('startup_repair', 'warning', 'Run metadata reconciled against actual row counts', metadata={'scanner': scanner_updates, 'gex': gex_updates})
        except Exception:
            pass

def get_today_scanner_success_count(today: str) -> int:
    return int(_safe_scalar(config.SCANNER_DB, "SELECT COUNT(*) FROM scanner_runs WHERE snapshot_date=? AND status='success'", (today,)) or 0)


def get_today_gex_success_count(today: str) -> int:
    return int(_safe_scalar(config.GEX_DB, "SELECT COUNT(*) FROM gex_runs WHERE snapshot_date=? AND status='success'", (today,)) or 0)


def print_bootstrap_status():
    today = market_session_date_str()
    cprint('🔎 Preflight DB status')
    cprint(f'   DB live scanner: {config.SCANNER_DB}')
    cprint(f'   DB live GEX:     {config.GEX_DB}')
    cprint(f'   DB raw:          {config.RAW_DB}')
    cprint(f'   DB meta:         {config.META_DB}')
    cprint(f'   Archive dir:     {config.ARCHIVE_DIR}')

    scanner_runs = _table_count(config.SCANNER_DB, 'scanner_runs')
    scanner_snapshots = _table_count(config.SCANNER_DB, 'scanner_snapshots')
    daily_history = _table_count(config.SCANNER_DB, 'ticker_daily_history')
    gex_runs = _table_count(config.GEX_DB, 'gex_runs')
    gex_summary = _table_count(config.GEX_DB, 'gex_summary')
    gex_levels = _table_count(config.GEX_DB, 'gex_levels')
    gex_expirations = _table_count(config.GEX_DB, 'gex_expirations')
    gex_expiry_buckets = _table_count(config.GEX_DB, 'gex_expiry_buckets')
    raw_payloads = _table_count(config.RAW_DB, 'raw_api_payloads')
    extra_fields = _table_count(config.META_DB, 'extra_fields')
    schema_fields = _table_count(config.META_DB, 'schema_registry')

    cprint(f'   Counts | scanner_runs={scanner_runs} | scanner_snapshots={scanner_snapshots} | daily_history={daily_history}')
    cprint(f'   Counts | gex_runs={gex_runs} | gex_summary={gex_summary} | strikes={gex_levels} | expirations={gex_expirations} | expiry_buckets={gex_expiry_buckets}')
    cprint(f'   Counts | raw_payloads={raw_payloads} | schema_fields={schema_fields} | extra_fields={extra_fields}')

    latest_scanner = get_latest_scanner_run()
    latest_scanner_success = get_latest_scanner_run('success')
    latest_gex = get_latest_gex_run()
    latest_gex_success = get_latest_gex_run('success')

    if latest_scanner:
        cprint(f"   Último scanner run: id={latest_scanner.get('id')} | date={latest_scanner.get('snapshot_date')} | session={latest_scanner.get('session_type')} | status={latest_scanner.get('status')} | ok={latest_scanner.get('total_tickers_ok')} | expected={latest_scanner.get('total_tickers_expected')} | base={latest_scanner.get('base_rows_saved')} | unique={latest_scanner.get('base_unique_tickers')} | enriched={latest_scanner.get('enriched_ok')}/{latest_scanner.get('enriched_failed')} | stage={latest_scanner.get('completion_stage')}")
    else:
        cprint('   Último scanner run: ninguno')
    if latest_scanner_success:
        cprint(f"   Último scanner SUCCESS: id={latest_scanner_success.get('id')} | date={latest_scanner_success.get('snapshot_date')} | session={latest_scanner_success.get('session_type')} | ok={latest_scanner_success.get('total_tickers_ok')} | base={latest_scanner_success.get('base_rows_saved')} | enriched={latest_scanner_success.get('enriched_ok')} | stage={latest_scanner_success.get('completion_stage')}")
    else:
        cprint('   Último scanner SUCCESS: ninguno')

    if latest_gex:
        cprint(f"   Último GEX run: id={latest_gex.get('id')} | date={latest_gex.get('snapshot_date')} | session={latest_gex.get('session_type')} | status={latest_gex.get('status')} | ok={latest_gex.get('ok_tickers')} | levels={latest_gex.get('levels_count')}")
    else:
        cprint('   Último GEX run: ninguno')
    if latest_gex_success:
        cprint(f"   Último GEX SUCCESS: id={latest_gex_success.get('id')} | date={latest_gex_success.get('snapshot_date')} | session={latest_gex_success.get('session_type')} | ok={latest_gex_success.get('ok_tickers')} | levels={latest_gex_success.get('levels_count')}")
    else:
        cprint('   Último GEX SUCCESS: ninguno')

    today_valid_scanner_success = get_today_valid_scanner_success(today)
    today_valid_gex_success = get_today_valid_gex_success(today)
    raw_scanner_success_count = get_today_scanner_success_count(today)
    raw_gex_success_count = get_today_gex_success_count(today)
    cprint(f"   Runs success hoy ({today}) | scanner={raw_scanner_success_count} valid={1 if today_valid_scanner_success else 0} | gex={raw_gex_success_count} valid={1 if today_valid_gex_success else 0}")
    if not today_valid_scanner_success and (scanner_snapshots or scanner_runs):
        cprint('   ⚠️ Scanner no tiene SUCCESS válido hoy: si hay datos, se consideran parciales hasta completar una nueva captura.')
    if not today_valid_gex_success and (gex_summary or gex_runs):
        cprint('   ⚠️ GEX no tiene SUCCESS válido hoy: si hay datos, se consideran parciales hasta completar una nueva captura.')
    return {
        'scanner_runs': scanner_runs,
        'scanner_snapshots': scanner_snapshots,
        'daily_history': daily_history,
        'gex_runs': gex_runs,
        'gex_summary': gex_summary,
        'gex_levels': gex_levels,
        'gex_expirations': gex_expirations,
        'gex_expiry_buckets': gex_expiry_buckets,
        'raw_payloads': raw_payloads,
        'latest_scanner_success': latest_scanner_success,
        'latest_gex_success': latest_gex_success,
        'today': today,
        'today_scanner_success': raw_scanner_success_count,
        'today_gex_success': raw_gex_success_count,
        'today_valid_scanner_success': today_valid_scanner_success,
        'today_valid_gex_success': today_valid_gex_success,
    }


def mark_stale_running_runs():
    """Mark runs left in 'running' by a previous killed process as interrupted.

    If this process has acquired the lock, any existing running row belongs to an
    older process or to a Ctrl+C interruption. Keeping it as running makes the
    healthcheck misleading and can block bootstrap decisions.
    """
    utc = ts_utc_str()
    scanner_count = 0
    gex_count = 0
    try:
        conn = sqlite3.connect(config.SCANNER_DB, timeout=60)
        cur = conn.cursor()
        running_rows = cur.execute("SELECT id FROM scanner_runs WHERE status='running'").fetchall()
        for rr in running_rows:
            rid = rr[0]
            snap_count = cur.execute("SELECT COUNT(*) FROM scanner_snapshots WHERE scanner_run_id=?", (rid,)).fetchone()[0]
            unique_count = cur.execute("SELECT COUNT(DISTINCT ticker) FROM scanner_snapshots WHERE scanner_run_id=?", (rid,)).fetchone()[0]
            if snap_count > 0:
                cur.execute("""
                    UPDATE scanner_runs
                    SET status='partial_base_saved',
                        total_tickers_ok=MAX(COALESCE(total_tickers_ok,0), ?),
                        base_rows_saved=COALESCE(NULLIF(base_rows_saved,0), ?),
                        base_unique_tickers=COALESCE(NULLIF(base_unique_tickers,0), ?),
                        completion_stage=COALESCE(completion_stage, 'startup_recovered_after_base_snapshot'),
                        error_message=COALESCE(error_message, 'Stale run recovered at startup after base snapshots were saved'),
                        updated_at_utc=?
                    WHERE id=?
                """, (snap_count, snap_count, unique_count, utc, rid))
            else:
                cur.execute("""
                    UPDATE scanner_runs
                    SET status='interrupted',
                        completion_stage=COALESCE(completion_stage, 'startup_recovered_before_base_snapshot'),
                        error_message=COALESCE(error_message, 'Interrupted/stale run detected at startup'),
                        updated_at_utc=?
                    WHERE id=?
                """, (utc, rid))
        scanner_count = len(running_rows)
        conn.commit(); conn.close()
    except Exception as e:
        cprint(f'⚠️ No se pudieron cerrar scanner_runs running antiguos: {e}')

    try:
        conn = sqlite3.connect(config.GEX_DB, timeout=60)
        cur = conn.cursor()
        running_rows = cur.execute("SELECT id FROM gex_runs WHERE status='running'").fetchall()
        for rr in running_rows:
            rid = rr[0]
            summary_count = cur.execute("SELECT COUNT(*) FROM gex_summary WHERE gex_run_id=?", (rid,)).fetchone()[0]
            levels_count = cur.execute("SELECT COUNT(*) FROM gex_levels WHERE gex_run_id=?", (rid,)).fetchone()[0]
            new_status = 'partial' if summary_count > 0 else 'interrupted'
            cur.execute("""
                UPDATE gex_runs
                SET status=?,
                    ok_tickers=MAX(COALESCE(ok_tickers,0), ?),
                    levels_count=MAX(COALESCE(levels_count,0), ?),
                    error_message=COALESCE(error_message, 'Interrupted/stale GEX run detected at startup'),
                    updated_at_utc=?
                WHERE id=?
            """, (new_status, summary_count, levels_count, utc, rid))
        gex_count = len(running_rows)
        conn.commit(); conn.close()
    except Exception as e:
        cprint(f'⚠️ No se pudieron cerrar gex_runs running antiguos: {e}')

    if scanner_count or gex_count:
        cprint(f'🧯 Runs antiguos en running marcados como interrupted | scanner={scanner_count} | gex={gex_count}')
        try:
            log_quality('startup_recovery', 'warning', 'Marked stale running runs as interrupted', metadata={'scanner': scanner_count, 'gex': gex_count})
        except Exception:
            pass


def run_bootstrap_checks():
    if not config.RUN_BOOTSTRAP_ON_START:
        payload = record_stage(
            "D00", "SKIP", "Bootstrap desactivado",
            expected="RUN_BOOTSTRAP_ON_START=true", found=False,
            action="El daemon arrancará sin reparar ni revisar archivos.",
        )
        cprint(friendly_stage_line(payload))
        return

    bootstrap_id = f"data-{now_madrid().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    bootstrap_started = now_madrid().isoformat()
    mark_history_bootstrap_started(bootstrap_id, bootstrap_started)

    cprint(f'🧭 DATA PIPELINE STARTUP v{config.BOT_VERSION} | CORE TMAD + FULL DEEP + INTRADÍA')
    hooks = webhook_status()
    hook_status = "OK" if hooks.get("data_log") else "WARN"
    hook_stage = record_stage(
        "D01", hook_status, "Canales Discord del Data Bot",
        expected="DATA_LOG configurado",
        found={k: ("configured" if v else "missing") for k, v in hooks.items()},
        action=("Se enviarán resúmenes a Data Log." if hooks.get("data_log") else "Se guardarán logs locales; revisar configuracion_canales.env."),
        reason=(None if hooks.get("data_log") else "No hay webhook DATA_LOG utilizable."),
    )
    cprint(friendly_stage_line(hook_stage))
    if hooks.get("data_log"):
        log_info(build_group(
            "🟡 DATA · ARRANQUE",
            ["**Secuencia:** bases live → archivos pendientes → histórico CORE → cobertura FULL/GEX → handoff a Alerts."],
            context=f"**Versión:** `v{config.BOT_VERSION}`",
        ), send_discord=True)


    status_before = print_bootstrap_status()
    db_stage = record_stage(
        "D02", "OK", "Lectura de bases live",
        expected="DB accesibles y schemas válidos",
        found={
            "scanner_runs": status_before.get("scanner_runs", 0),
            "scanner_rows": status_before.get("scanner_snapshots", 0),
            "gex_runs": status_before.get("gex_runs", 0),
            "gex_rows": status_before.get("gex_summary", 0),
        },
        action="Continuar con limpieza y reconstrucción de capas.",
    )
    cprint(friendly_stage_line(db_stage))

    run_cleanup_invalid_runs_after_success('at bootstrap start')

    pending_results: list[dict] = []
    intraday_status = "OK"
    intraday_reason = None
    try:
        pending_results = archive_pending_intraday(before_date=current_market_date()) if config.INTRADAY_ARCHIVE_ENABLED else []
        archived = [row for row in pending_results if row.get("status") == "archived"]
        no_data = [row for row in pending_results if row.get("status") == "nothing_to_archive"]
        found = {
            "days_checked": len(pending_results),
            "days_archived": len(archived),
            "days_without_rows": len(no_data),
            "scanner_runs_archived": sum(int(row.get("scanner_runs") or 0) for row in archived),
            "gex_runs_archived": sum(int(row.get("gex_runs") or 0) for row in archived),
        }
        action = "ZIP intradía verificado y filas horarias purgadas de live." if archived else "No había sesiones horarias antiguas pendientes."
    except Exception as exc:
        intraday_status = "ERROR"
        intraday_reason = str(exc)
        found = {"days_checked": 0}
        action = "No se purgó intradía; se conserva live para evitar pérdida."
        log_quality('bootstrap_capture_layers', 'warning', str(exc), metadata={'trace': traceback.format_exc()[:4000]})
    intraday_stage = record_stage(
        "D03", intraday_status, "Archivo de capa intradía anterior",
        expected="Todo run no-deep anterior guardado en ZIP antes de borrarse",
        found=found, action=action, reason=intraday_reason,
        details={"results": pending_results[:10]},
    )
    cprint(friendly_stage_line(intraday_stage))

    profile_pending_results: list[dict] = []
    try:
        profile_pending_results = archive_pending_gex_profiles(before_date=current_market_date())
        profile_stage_status = "OK"
        profile_stage_reason = None
        profile_stage_action = "Perfiles GEX antiguos guardados en ZIP y purgados de live." if profile_pending_results else "No había perfiles GEX antiguos pendientes."
    except Exception as exc:
        profile_stage_status = "ERROR"
        profile_stage_reason = str(exc)
        profile_stage_action = "No se borró ningún evento de perfil GEX; se conserva live."
        log_quality('bootstrap_gex_profile_archive', 'warning', str(exc), metadata={'trace': traceback.format_exc()[:4000]})
    profile_stage = record_stage(
        "D03B", profile_stage_status, "Archivo diario de perfiles GEX",
        expected="Baseline + deltas por ticker en ZIP verificado",
        found={
            "days_archived": len([x for x in profile_pending_results if x.get('status') == 'archived']),
            "events": sum(int(x.get('counts', {}).get('events') or 0) for x in profile_pending_results),
            "tickers": sum(int(x.get('counts', {}).get('tickers') or 0) for x in profile_pending_results),
        },
        action=profile_stage_action,
        reason=profile_stage_reason,
        details={"results": profile_pending_results[:10]},
    )
    cprint(friendly_stage_line(profile_stage))

    core_fetch: dict = {}
    try:
        core_fetch = run_core_history_bootstrap(
            max_tickers=config.CORE_BOOTSTRAP_STARTUP_MAX_TICKERS,
            reason="startup",
        )
        if core_fetch.get("status") == "error":
            core_fetch_status = "ERROR"
        elif int(core_fetch.get("remaining") or 0) > 0:
            core_fetch_status = "WAIT"
        elif core_fetch.get("status") in {"disabled", "waiting_for_scanner_universe"}:
            core_fetch_status = "WAIT"
        else:
            core_fetch_status = "OK"
        core_fetch_reason = (
            None if core_fetch_status == "OK"
            else (core_fetch.get("errors") or [core_fetch.get("status")])[0]
        )
    except Exception as exc:
        core_fetch = {"status": "error", "error": str(exc), "remaining": None}
        core_fetch_status = "ERROR"
        core_fetch_reason = str(exc)
    core_fetch_stage = record_stage(
        "D04B", core_fetch_status, "Bootstrap CORE_HISTORY por red",
        expected=(
            f"Descargar history + priceHistory de hasta "
            f"{config.CORE_BOOTSTRAP_STARTUP_MAX_TICKERS} ticker sin depender del horario"
        ),
        found={
            "universe": int(core_fetch.get("universe_tickers") or 0),
            "already_complete": int(core_fetch.get("already_complete") or 0),
            "attempted": int(core_fetch.get("attempted") or 0),
            "successful": int(core_fetch.get("successful") or 0),
            "failed": int(core_fetch.get("failed") or 0),
            "signal_rows": int(core_fetch.get("signal_rows") or 0),
            "price_rows": int(core_fetch.get("price_rows") or 0),
            "remaining": core_fetch.get("remaining"),
        },
        action=(
            "CORE ya completo para el universo Scanner actual; no hacen falta llamadas adicionales."
            if bool((core_fetch.get("coverage") or {}).get("ready"))
            else (
                "CORE materializado; continuar el resto del universo en daemon."
                if int(core_fetch.get("successful") or 0) > 0
                else "Reintentar cuando exista un Scanner SUCCESS y una sesión TMAD válida."
            )
        ),
        reason=core_fetch_reason,
    )
    cprint(friendly_stage_line(core_fetch_stage))

    cprint("🔄 [5/8] Reconstruyendo histórico CORE desde los payloads guardados…")
    core_rebuild = {}
    try:
        def _core_rebuild_progress(done, total, ticker, stats):
            cprint(
                f"   ↳ CORE compacto {done:,}/{total:,} | ticker={ticker} | "
                f"señales={int(stats.get('signal_rows') or 0):,} | precios={int(stats.get('price_rows') or 0):,}"
            )
        core_rebuild = rebuild_compact_history_from_raw_details(progress_callback=_core_rebuild_progress)
        if core_rebuild.get("cached"):
            cprint(
                f"⚡ [5/8] Histórico CORE ya materializado | "
                f"payloads={int(core_rebuild.get('payloads') or 0):,} | sin reescritura"
            )
        core_rebuild_status = "OK" if int(core_rebuild.get("tickers") or 0) > 0 else "SKIP"
        core_rebuild_reason = None if core_rebuild_status == "OK" else "No había payload detail pendiente de reconstrucción."
    except Exception as exc:
        core_rebuild = {"error": str(exc)}
        core_rebuild_status = "WARN"
        core_rebuild_reason = str(exc)
    core_rebuild_stage = record_stage(
        "D04C", core_rebuild_status, "Reconstrucción CORE_HISTORY desde TMAD detail",
        expected="Materializar history + priceHistory ya guardados, sin llamadas de red",
        found=core_rebuild,
        action="Mantener ticker_signal_daily y ticker_ohlc_daily listos para calibración core.",
        reason=core_rebuild_reason,
    )
    cprint(friendly_stage_line(core_rebuild_stage))

    rebuilt: dict = {}
    try:
        rebuilt = rebuild_canonical_daily_history()
        daily_status = "OK" if int(rebuilt.get("dates") or 0) > 0 else "WAIT"
        daily_reason = None if daily_status == "OK" else "Todavía no existe un cierre deep válido."
    except Exception as exc:
        rebuilt = {"dates": 0, "rows": 0, "error": str(exc)}
        daily_status = "ERROR"
        daily_reason = str(exc)
        log_quality('canonical_daily_rebuild', 'warning', str(exc), metadata={'trace': traceback.format_exc()[:4000]})
    daily_stage = record_stage(
        "D04F", daily_status, "FULL DEEP diario canónico",
        expected=f"{config.DAILY_CLOSE_WINDOW_SESSIONS} cierres deep completos",
        found={"daily_dates": int(rebuilt.get("dates") or 0), "scanner_rows": int(rebuilt.get("rows") or 0)},
        action="Conservar solo deep como capa FULL; los runs horarios no cuentan como cierre diario.",
        reason=daily_reason,
    )
    cprint(friendly_stage_line(daily_stage))

    coverage: dict = {}
    if config.BOOTSTRAP_ARCHIVE_ON_START:
        try:
            coverage = ensure_history_archives(
                bootstrap_complete=False,
                bootstrap_id=bootstrap_id,
                bootstrap_started_at=bootstrap_started,
            )
            coverage_status = "OK" if coverage.get("core_history_ready") else "WAIT"
            coverage_reason = (
                None if coverage.get("core_history_ready")
                else (
                    f"CORE TMAD pendiente: fechas {coverage.get('core_history_dates', 0)}/"
                    f"{coverage.get('core_history_target_sessions', config.CORE_HISTORY_TARGET_SESSIONS)} · "
                    f"eligible={coverage.get('core_history_complete_tickers', 0)} · "
                    f"excluded={coverage.get('core_history_excluded_tickers', 0)} · "
                    f"pending={coverage.get('core_history_pending_tickers', coverage.get('core_history_remaining_tickers', 0))}/"
                    f"{coverage.get('core_history_universe_tickers', 0)}."
                )
            )
        except Exception as exc:
            coverage = {"paired_trading_dates": 0, "target_daily_closes": config.DAILY_CLOSE_WINDOW_SESSIONS, "contributing_zip_count": 0}
            coverage_status = "ERROR"
            coverage_reason = str(exc)
            log_quality('bootstrap_archive', 'warning', str(exc), metadata={'trace': traceback.format_exc()[:4000]})
    else:
        coverage_status = "SKIP"
        coverage_reason = "BOOTSTRAP_ARCHIVE_ON_START=false"
    coverage_stage = record_stage(
        "D05", coverage_status, "Coberturas CORE / FULL DEEP",
        expected={
            "core_tmad": f"{config.CORE_HISTORY_TARGET_SESSIONS}/{config.CORE_HISTORY_TARGET_SESSIONS}",
            "full_deep": f"{config.DAILY_CLOSE_WINDOW_SESSIONS}/{config.DAILY_CLOSE_WINDOW_SESSIONS}",
        },
        found={
            "core_history": f"{int(coverage.get('core_history_dates') or 0)}/{int(coverage.get('core_history_target_sessions') or config.CORE_HISTORY_TARGET_SESSIONS)}",
            "core_ready": bool(coverage.get("core_history_ready")),
            "core_rows": int(coverage.get("core_history_rows") or 0),
            "core_tickers": int(coverage.get("core_history_tickers") or 0),
            "core_universe": int(coverage.get("core_history_universe_tickers") or 0),
            "core_eligible": int(coverage.get("core_history_complete_tickers") or 0),
            "core_excluded": int(coverage.get("core_history_excluded_tickers") or 0),
            "core_pending": int(coverage.get("core_history_pending_tickers") or coverage.get("core_history_remaining_tickers") or 0),
            "full_deep": f"{int(coverage.get('paired_trading_dates') or 0)}/{int(coverage.get('target_daily_closes') or config.DAILY_CLOSE_WINDOW_SESSIONS)}",
            "weekly_zips_with_deep": int(coverage.get("contributing_zip_count") or 0),
        },
        action=(
            "CORE listo para calibración; FULL DEEP seguirá como challenger."
            if coverage.get("core_history_ready")
            else "Seguir refrescando /api/stock/{ticker} y capturando un deep por sesión."
        ),
        reason=coverage_reason,
    )
    cprint(friendly_stage_line(coverage_stage))

    retention_status = "OK"
    retention_reason = None
    if config.BOOTSTRAP_RETENTION_ON_START:
        try:
            purge_live_retention()
            retention_action = f"Retención aplicada; cierres deep conservados hasta {config.LIVE_RETENTION_DAYS} días."
        except Exception as exc:
            retention_status = "WARN"
            retention_reason = str(exc)
            retention_action = "No se borró nada por seguridad."
            log_quality('bootstrap_retention', 'warning', str(exc), metadata={'trace': traceback.format_exc()[:4000]})
    else:
        retention_status = "SKIP"
        retention_action = "Retención de arranque desactivada."
    retention_stage = record_stage(
        "D06", retention_status, "Retención live",
        expected="No borrar cierres rolling ni datos sin ZIP",
        found=f"retention_days={config.LIVE_RETENTION_DAYS}",
        action=retention_action, reason=retention_reason,
    )
    cprint(friendly_stage_line(retention_stage))

    today = status_before['today']
    first_slot_minutes = min(
        [int(h.split(':')[0]) * 60 + int(h.split(':')[1]) for h in config.SCANNER_CAPTURE_TIMES if ':' in h] or [24 * 60]
    )
    current_minutes = now_madrid().hour * 60 + now_madrid().minute
    before_first_slot = current_minutes < first_slot_minutes
    deep_plan = startup_deep_repair_plan(status_before)
    should_repair_deep = bool(deep_plan.get("repairable_now"))
    should_capture_scanner = False
    should_capture_gex = False
    scanner_profile = "selective"
    scanner_session = "startup_bootstrap"
    capture_reason = str(deep_plan.get("reason") or "No hace falta captura de reparación.")

    if should_repair_deep:
        should_capture_scanner = True
        should_capture_gex = False  # el deep crea su GEX emparejado
        scanner_profile = "deep"
        scanner_session = f"{deep_plan.get('target_date')}_deep_repair"
    elif config.BOOTSTRAP_CAPTURE_IF_EMPTY and status_before['scanner_snapshots'] == 0:
        should_capture_scanner = True
        capture_reason = "Scanner vacío: captura inicial necesaria."
    elif not before_first_slot and config.BOOTSTRAP_CAPTURE_SCANNER_IF_INCOMPLETE and not status_before.get('today_valid_scanner_success'):
        should_capture_scanner = True
        capture_reason = f"Después del primer slot no existe scanner SUCCESS válido para {today}."
    elif not before_first_slot and config.BOOTSTRAP_CAPTURE_SCANNER_IF_NO_SUCCESS_TODAY and status_before['today_scanner_success'] == 0:
        should_capture_scanner = True
        capture_reason = f"Después del primer slot no existe scanner SUCCESS para {today}."

    if not should_repair_deep:
        if config.BOOTSTRAP_RUN_GEX_IF_EMPTY and status_before['gex_summary'] == 0:
            should_capture_gex = True
        elif not before_first_slot and config.BOOTSTRAP_CAPTURE_GEX_IF_INCOMPLETE and not status_before.get('today_valid_gex_success'):
            should_capture_gex = True
        elif not before_first_slot and config.BOOTSTRAP_CAPTURE_GEX_IF_NO_SUCCESS_TODAY and status_before['today_gex_success'] == 0:
            should_capture_gex = True

    market_weekend = now_madrid().astimezone(MARKET_TZ).weekday() >= 5
    weekend_block = (not config.RUN_WEEKENDS) and (not config.BOOTSTRAP_CAPTURE_ON_WEEKENDS) and market_weekend
    if weekend_block and not should_repair_deep:
        should_capture_scanner = False
        should_capture_gex = False
        capture_reason = "Fin de semana de mercado y RUN_WEEKENDS=false."
    elif before_first_slot and status_before['scanner_snapshots'] > 0 and not should_repair_deep:
        capture_reason = (
            capture_reason if deep_plan.get("deep_missing")
            else "Antes del primer horario programado: no se crea un run artificial de arranque."
        )

    capture_status = "WARN" if (should_capture_scanner or should_capture_gex or deep_plan.get("deep_missing")) else "OK"
    capture_stage = record_stage(
        "D07", capture_status, "Decisión de captura de reparación",
        expected="Reparar el último deep solo en la misma fecha NY; nunca fabricar un cierre retroactivo",
        found={
            "scanner": should_capture_scanner,
            "scanner_profile": scanner_profile if should_capture_scanner else None,
            "gex": should_capture_gex,
            "before_first_slot": before_first_slot,
            "completed_market_date": deep_plan.get("completed_market_date"),
            "current_market_date": deep_plan.get("current_market_date"),
            "deep_target": deep_plan.get("target_date"),
            "deep_missing": bool(deep_plan.get("deep_missing")),
            "deep_repairable_now": bool(deep_plan.get("repairable_now")),
        },
        action=(
            f"Lanzar cierre deep de reparación para {deep_plan.get('target_date')}."
            if should_repair_deep
            else ("Lanzar reparación selectiva." if (should_capture_scanner or should_capture_gex) else "Esperar al siguiente horario normal.")
        ),
        reason=capture_reason,
    )
    cprint(friendly_stage_line(capture_stage))

    if should_capture_scanner:
        try:
            run_scanner_capture(scanner_session, scanner_profile)
        except Exception as exc:
            cprint(f'🟥 [D07] Captura scanner de reparación falló: {exc}')
    if should_capture_gex:
        try:
            run_gex_capture('startup_bootstrap_gex')
        except Exception as exc:
            cprint(f'🟥 [D07] Captura GEX de reparación falló: {exc}')

    run_cleanup_invalid_runs_after_success('after bootstrap captures')

    try:
        final_coverage = ensure_history_archives(
            bootstrap_complete=True,
            bootstrap_id=bootstrap_id,
            bootstrap_started_at=bootstrap_started,
        )
    except Exception as exc:
        final_coverage = coverage
        final_coverage["bootstrap_complete"] = True
        final_coverage["final_error"] = str(exc)
    final_status = print_bootstrap_status()
    write_output_json("latest_bootstrap_status.json", final_status)

    final_stage = record_stage(
        "D08", "OK" if final_coverage.get("bootstrap_complete") else "WARN", "Data Bot listo",
        expected="Manifest actual y daemon preparado",
        found={
            "bootstrap_id": bootstrap_id,
            "core_history": f"{int(final_coverage.get('core_history_dates') or 0)}/{int(final_coverage.get('core_history_target_sessions') or config.CORE_HISTORY_TARGET_SESSIONS)}",
            "core_ready": bool(final_coverage.get("core_history_ready")),
            "core_universe": int(final_coverage.get("core_history_universe_tickers") or 0),
            "core_eligible": int(final_coverage.get("core_history_complete_tickers") or 0),
            "core_excluded": int(final_coverage.get("core_history_excluded_tickers") or 0),
            "core_pending": int(final_coverage.get("core_history_pending_tickers") or final_coverage.get("core_history_remaining_tickers") or 0),
            "full_deep": f"{int(final_coverage.get('paired_trading_dates') or 0)}/{int(final_coverage.get('target_daily_closes') or config.DAILY_CLOSE_WINDOW_SESSIONS)}",
            "scanner_live_runs": final_status.get("scanner_runs", 0),
            "gex_live_runs": final_status.get("gex_runs", 0),
        },
        action=(
            "Alerts Bot puede calibrar CORE inmediatamente si history/priceHistory alcanzan 30 sesiones; "
            "FULL DEEP e intradía continúan acumulándose como challengers."
        ),
    )
    cprint(friendly_stage_line(final_stage))
    cprint(auditor_feed_console_line(context="startup listo"))
    cprint(console_health_line(context="startup listo"))

    summary_message = build_group(
        "✅ DATA · LISTO",
        ["\n".join([
            f"**Bases live:** Scanner `{_fmt_count_es(final_status.get('scanner_snapshots',0))}` filas · GEX `{_fmt_count_es(final_status.get('gex_summary',0))}` resúmenes.",
            f"**CORE:** `{int(final_coverage.get('core_history_dates') or 0)}/{int(final_coverage.get('core_history_target_sessions') or config.CORE_HISTORY_TARGET_SESSIONS)}` · universo `{int(final_coverage.get('core_history_complete_tickers') or 0)} + {int(final_coverage.get('core_history_excluded_tickers') or 0)} / {int(final_coverage.get('core_history_universe_tickers') or 0)}` · pendientes `{int(final_coverage.get('core_history_pending_tickers') or 0)}`.",
            f"**FULL DEEP:** `{int(final_coverage.get('paired_trading_dates') or 0)}/{int(final_coverage.get('target_daily_closes') or config.DAILY_CLOSE_WINDOW_SESSIONS)}` · continúa acumulando histórico granular.",
            f"**Archivos:** intradía `{(intraday_stage.get('found') or {}).get('days_archived',0)}` días · perfiles GEX `{(profile_stage.get('found') or {}).get('days_archived',0)}` días.",
            "**Handoff:** manifest publicado a Alerts mediante Coordination Bus.",
        ])],
        context=f"**Versión:** `v{config.BOT_VERSION}` · **Bootstrap:** `{bootstrap_id}`",
        footer="Data está listo. El detalle técnico permanece en pipeline_diagnostics.jsonl.",
    )
    delivered = log_info(summary_message) if config.BOOTSTRAP_NOTIFY_DISCORD else False
    if not hooks.get("data_log"):
        cprint("⚠️ [D01] No llegó nada a Discord Data Log porque el webhook no está configurado. Revisa DATA_LOG_WEBHOOK en 02_CONFIG\\configuracion_canales.env.")
    elif config.BOOTSTRAP_NOTIFY_DISCORD and not delivered:
        cprint("⚠️ [D01] El webhook Data Log existe, pero Discord rechazó o no recibió el mensaje. Revisa tmad_data_discord_delivery_error_*.log.")
    try:
        send_storage_summary()
    except Exception as report_exc:
        cprint(f"⚠️ Resumen de bases/archivo no entregado: {report_exc}")

def _has_successful_deep_capture(snapshot_date: str) -> bool:
    """Return True only when today's deep Scanner capture finished successfully.

    This is used solely to decide whether an elapsed deep slot may be considered
    satisfied after a restart. An interrupted/partial deep must be retried.
    """
    try:
        conn = sqlite3.connect(config.SCANNER_DB, timeout=5)
        row = conn.execute(
            """SELECT 1
               FROM scanner_runs
               WHERE snapshot_date=?
                 AND status='success'
                 AND session_type LIKE '%deep%'
               ORDER BY id DESC
               LIMIT 1""",
            (str(snapshot_date),),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as exc:
        cprint(f"⚠️ No se pudo verificar deep SUCCESS del día | date={snapshot_date} error={type(exc).__name__}")
        # Fail safe: keep the deep slot eligible rather than silently losing EOD data.
        return False


def _scanner_run_completion(run_id: int) -> dict:
    """Read the terminal state of one Scanner run without changing pipeline state."""
    conn = None
    try:
        conn = sqlite3.connect(config.SCANNER_DB, timeout=5)
        row = conn.execute(
            """SELECT status,completion_stage,error_message
               FROM scanner_runs WHERE id=? LIMIT 1""",
            (int(run_id),),
        ).fetchone()
        if not row:
            return {"status": "unknown", "completion_stage": "missing", "error_message": "run_not_found"}
        return {
            "status": str(row[0] or "unknown").lower(),
            "completion_stage": str(row[1] or ""),
            "error_message": str(row[2] or ""),
        }
    except Exception as exc:
        return {"status": "unknown", "completion_stage": "read_error", "error_message": f"{type(exc).__name__}: {exc}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_daemon():
    # Acquire the liveness lock before schemas and maintenance. A large persistent
    # database may need more than the deploy timeout, but the process is already
    # healthy and must not trigger a false rollback while it initializes.
    acquire_lock()
    cprint(f'⚙️ Inicializando schemas SQLite | TMAD Data Bot v{config.BOT_VERSION}')
    ensure_all_schemas()
    normalize_existing_snapshot_dates_v49()
    try:
        from maintenance_v620 import main as run_maintenance_v620
        run_maintenance_v620()
    except Exception as exc:
        log_error(f"🟥 Mantenimiento operativo v6.1.25 al arrancar: {exc}")
    mark_stale_running_runs()
    repair_incomplete_run_metadata()

    # Bootstrap may launch a FULL DEEP repair lasting hours. Publish liveness
    # before entering that blocking stage so Alerts never mistakes healthy Data
    # processing for a dead peer.
    bootstrap_heartbeat_stop = threading.Event()

    def _bootstrap_coordination_heartbeat_worker() -> None:
        interval = max(5, min(30, int(config.COORDINATION_HEARTBEAT_SECONDS)))
        while not bootstrap_heartbeat_stop.is_set():
            try:
                ack = read_ack(config.COORDINATION_DB, "alerts_bot")
                ack_generation = int(ack.get("generation") or 0)
                write_heartbeat(
                    config.COORDINATION_DB, "data_bot", "running",
                    generation_seen=ack_generation,
                    details={
                        "alerts_ack_status": ack.get("status"),
                        "alerts_ack_generation": ack_generation,
                        "worker": "bootstrap",
                        "stage": "bootstrap_or_deep_repair",
                    },
                )
            except Exception:
                pass
            bootstrap_heartbeat_stop.wait(interval)

    bootstrap_heartbeat_thread = threading.Thread(
        target=_bootstrap_coordination_heartbeat_worker,
        name="tmad-data-bootstrap-heartbeat",
        daemon=True,
    )
    bootstrap_heartbeat_thread.start()
    try:
        run_bootstrap_checks()
    finally:
        bootstrap_heartbeat_stop.set()
        try:
            bootstrap_heartbeat_thread.join(timeout=2)
        except Exception:
            pass

    already_run = set(); last_heartbeat = 0
    deep_retry_attempts: dict[str, int] = {}
    deep_retry_not_before: dict[str, float] = {}
    last_discord_heartbeat = 0.0
    last_coordination_heartbeat = 0.0
    last_peer_warning = False
    heartbeat_stop = threading.Event()

    def _coordination_heartbeat_worker() -> None:
        # Scanner/GEX/archive work can block the daemon loop for more than the
        # stale threshold. Keep liveness independent, as Alerts already does.
        interval = max(5, min(30, int(config.COORDINATION_HEARTBEAT_SECONDS)))
        while not heartbeat_stop.is_set():
            try:
                ack = read_ack(config.COORDINATION_DB, "alerts_bot")
                ack_generation = int(ack.get("generation") or 0)
                write_heartbeat(
                    config.COORDINATION_DB, "data_bot", "running",
                    generation_seen=ack_generation,
                    details={
                        "alerts_ack_status": ack.get("status"),
                        "alerts_ack_generation": ack_generation,
                        "worker": "independent",
                    },
                )
            except Exception:
                pass
            heartbeat_stop.wait(interval)

    heartbeat_thread = threading.Thread(
        target=_coordination_heartbeat_worker,
        name="tmad-data-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    last_ack_generation_logged = 0
    last_core_bootstrap = time.time()
    last_operational_state = None
    # Bootstrap/first-run may already have captured the latest state. Mark elapsed slots so
    # daemon startup does not immediately repeat every scheduled capture from earlier today.
    # Exception: never mark an elapsed DAILY DEEP slot as satisfied unless a SUCCESS
    # deep capture exists for the same market date. This recovers interrupted close runs
    # after deployments/restarts without replaying obsolete selective slots.
    startup_now = now_madrid()
    startup_date = startup_now.strftime('%Y-%m-%d')
    startup_minutes = startup_now.hour * 60 + startup_now.minute
    deep_success_today = _has_successful_deep_capture(startup_date)
    deep_catchup_armed = False
    for job_type, times in [('scanner', config.SCANNER_CAPTURE_TIMES), ('gex', config.GEX_CAPTURE_TIMES)]:
        for hhmm in times:
            try:
                hour, minute = [int(x) for x in hhmm.split(':', 1)]
            except Exception:
                continue
            if hour * 60 + minute <= startup_minutes:
                if (
                    job_type == 'scanner'
                    and hhmm in config.DAILY_DEEP_CAPTURE_TIMES
                    and not deep_success_today
                ):
                    deep_catchup_armed = True
                    continue
                already_run.add(f"{startup_date}|{job_type}|{hhmm}")
    if deep_catchup_armed:
        cprint(
            f"🧯 DEEP CATCH-UP armado | date={startup_date} "
            f"deep_times={config.DAILY_DEEP_CAPTURE_TIMES} reason=no_successful_deep_after_restart"
        )
    cprint(f'🚀 TMAD Core Data Bot v{config.BOT_VERSION} iniciado en modo daemon')
    cprint(f'📅 Scanner times={config.SCANNER_CAPTURE_TIMES} | deep_times={config.DAILY_DEEP_CAPTURE_TIMES} | hourly_cap={config.HOURLY_ENRICH_MAX_TICKERS} | GEX separate times={config.GEX_CAPTURE_TIMES} | sleep={config.LOOP_SLEEP_SECONDS}s | run_weekends={config.RUN_WEEKENDS}')
    log_info(f'🚀 TMAD Core Data Bot v{config.BOT_VERSION} iniciado', send_discord=False)
    try:
        while True:
            now = now_madrid()
            operational_now = is_operational_window(now)
            if last_operational_state is None or operational_now != last_operational_state:
                if operational_now:
                    cprint("🟢 MODO OPERATIVO · L-V 12:00-00:00 Madrid | capturas y mantenimiento habilitados")
                else:
                    next_start = next_operational_start(now)
                    cprint(
                        f"🌙 MODO DESCANSO · próximo inicio {next_start.strftime('%Y-%m-%d %H:%M')} | "
                        "startup ya completado; solo heartbeat/coordinación"
                    )
                last_operational_state = operational_now
                cprint(console_health_line(context="operating window change"))
            if time.time() - last_heartbeat > config.HEARTBEAT_MINUTES * 60:
                insert_heartbeat('running', f'Bot vivo {now.isoformat()}')
                cprint(f'💓 [DATA HEARTBEAT] time={now.strftime("%Y-%m-%d %H:%M:%S")}')
                cprint(console_health_line(context="heartbeat"))
                log_info(f'💓 TMAD Core Data Bot heartbeat | {now.strftime("%Y-%m-%d %H:%M:%S")}', send_discord=False)
                last_heartbeat = time.time()
                discord_interval = 3600 if operational_now else 10800
                if time.time() - last_discord_heartbeat >= discord_interval:
                    try:
                        send_data_heartbeat()
                    except Exception as hb_exc:
                        cprint(f"⚠️ Heartbeat Discord Data no entregado: {hb_exc}")
                    last_discord_heartbeat = time.time()
            if time.time() - last_coordination_heartbeat >= config.COORDINATION_HEARTBEAT_SECONDS:
                try:
                    ack = read_ack(config.COORDINATION_DB, "alerts_bot")
                    ack_generation = int(ack.get("generation") or 0)
                    write_heartbeat(
                        config.COORDINATION_DB, "data_bot", "running",
                        generation_seen=ack_generation,
                        details={"alerts_ack_status": ack.get("status"), "alerts_ack_generation": ack_generation},
                    )
                    if ack_generation > last_ack_generation_logged:
                        cprint(
                            f"✅ Alerts Bot confirmó generación {ack_generation} "
                            f"| status={ack.get('status', 'unknown')} | attempts={ack.get('attempt_count', 0)}"
                        )
                        last_ack_generation_logged = ack_generation
                    peer = read_heartbeat(config.COORDINATION_DB, "alerts_bot")
                    peer_age = heartbeat_age_seconds(peer)
                    stale = peer_age is not None and peer_age > config.COORDINATION_STALE_SECONDS
                    if stale and not last_peer_warning:
                        cprint(f"⚠️ Alerts Bot sin heartbeat reciente | age={int(peer_age)}s")
                        last_peer_warning = True
                    elif not stale and last_peer_warning:
                        cprint("✅ Heartbeat del Alerts Bot recuperado")
                        last_peer_warning = False
                except Exception as exc:
                    cprint(f"⚠️ Coordination heartbeat Data falló: {exc}")
                last_coordination_heartbeat = time.time()

            # Startup/bootstrap above always runs once, including weekends. After
            # publication to Alerts, off-window operation is heartbeat-only.
            if bool(config.REST_MODE_HEARTBEAT_ONLY) and not operational_now:
                time.sleep(config.LOOP_SLEEP_SECONDS)
                continue

            for job_type, hhmm, session_type in due_jobs(already_run):
                key = f'{now.strftime("%Y-%m-%d")}|{job_type}|{hhmm}'
                retry_not_before = float(deep_retry_not_before.get(key) or 0.0)
                if retry_not_before > time.time():
                    continue
                cprint(f'⏰ Job debido detectado | type={job_type} | hhmm={hhmm} | session={session_type}')
                try:
                    if job_type == 'scanner':
                        profile = 'deep' if hhmm in config.DAILY_DEEP_CAPTURE_TIMES else 'selective'
                        run_id = run_scanner_capture(session_type + ('_deep' if profile == 'deep' else '_selective'), profile)
                        if profile == 'deep':
                            terminal = _scanner_run_completion(run_id)
                            if terminal.get('status') == 'success':
                                already_run.add(key)
                                deep_retry_attempts.pop(key, None)
                                deep_retry_not_before.pop(key, None)
                            else:
                                attempts = int(deep_retry_attempts.get(key) or 0) + 1
                                deep_retry_attempts[key] = attempts
                                max_attempts = int(config.SCANNER_DEEP_SLOT_RETRY_ATTEMPTS)
                                if attempts < max_attempts:
                                    delay = float(config.SCANNER_DEEP_SLOT_RETRY_BACKOFF_SECONDS) * attempts
                                    deep_retry_not_before[key] = time.time() + delay
                                    cprint(
                                        f"🧯 DEEP retry programado | run={run_id} status={terminal.get('status')} "
                                        f"stage={terminal.get('completion_stage')} attempt={attempts + 1}/{max_attempts} "
                                        f"in={int(delay)}s"
                                    )
                                else:
                                    already_run.add(key)
                                    deep_retry_not_before.pop(key, None)
                                    cprint(
                                        f"🟧 DEEP retry agotado | run={run_id} status={terminal.get('status')} "
                                        f"stage={terminal.get('completion_stage')} attempts={attempts}/{max_attempts} | "
                                        "se conserva último SUCCESS; catch-up tras restart sigue disponible"
                                    )
                        else:
                            already_run.add(key)
                    elif job_type == 'gex':
                        run_gex_capture(session_type)
                        already_run.add(key)
                except Exception:
                    already_run.add(key)
            # Limpia keys de días anteriores para que el set no crezca indefinidamente
            today = now.strftime('%Y-%m-%d')
            already_run = {k for k in already_run if k.startswith(today)}
            deep_retry_attempts = {k: v for k, v in deep_retry_attempts.items() if k.startswith(today)}
            deep_retry_not_before = {k: v for k, v in deep_retry_not_before.items() if k.startswith(today)}
            if (
                config.CORE_BOOTSTRAP_CONTINUE_IN_DAEMON
                and time.time() - last_core_bootstrap >= config.CORE_BOOTSTRAP_DAEMON_INTERVAL_MINUTES * 60
            ):
                try:
                    core_progress = run_core_history_bootstrap(
                        max_tickers=config.CORE_BOOTSTRAP_DAEMON_BATCH_SIZE,
                        reason="daemon_resume",
                    )
                    if int(core_progress.get("attempted") or 0) > 0:
                        refreshed = ensure_history_archives(bootstrap_complete=True)
                        cprint(
                            f"🧱 CORE daemon resume | ok={core_progress.get('successful', 0)} "
                            f"| failed={core_progress.get('failed', 0)} | remaining={core_progress.get('remaining', 0)} "
                            f"| ready={bool(refreshed.get('core_history_ready'))} "
                            f"| eligible={int(refreshed.get('core_history_complete_tickers') or 0)}/"
                            f"{int(refreshed.get('core_history_universe_tickers') or 0)} "
                            f"| excluded={int(refreshed.get('core_history_excluded_tickers') or 0)}"
                        )
                        write_output_json("core_daemon_resume_status.json", refreshed)
                except Exception as exc:
                    cprint(f"⚠️ CORE daemon resume falló: {exc}")
                finally:
                    last_core_bootstrap = time.time()
            time.sleep(config.LOOP_SLEEP_SECONDS)
    finally:
        heartbeat_stop.set()
        try:
            heartbeat_thread.join(timeout=2)
        except Exception:
            pass
        try:
            write_heartbeat(config.COORDINATION_DB, "data_bot", "stopped")
        except Exception:
            pass
        release_lock()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', choices=['scanner','gex','score','watchlist'], help='Ejecuta una captura manual y termina')
    parser.add_argument('--session', default='manual')
    parser.add_argument('--profile', choices=['auto','selective','deep'], default='auto', help='Perfil de enriquecimiento para --once scanner')
    args = parser.parse_args()
    ensure_all_schemas()
    if args.once == 'scanner': run_scanner_capture(args.session, args.profile)
    elif args.once == 'gex': run_gex_capture(args.session)
    elif args.once == 'score': run_score_capture(args.session)
    elif args.once == 'watchlist': run_watchlist_capture(args.session)
    else: run_daemon()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        release_lock(); cprint('Interrumpido por usuario'); sys.exit(0)
