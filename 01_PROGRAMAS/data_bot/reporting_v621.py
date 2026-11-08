from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import config
from coordination_bus import read_ack, read_data_state, read_heartbeat, heartbeat_age_seconds
from discord_presentation import build_group
from discord_utils import log_info, log_schema
from time_utils import now_madrid, is_operational_window, next_operational_start


def _count(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    conn = sqlite3.connect(path, timeout=15)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except Exception:
        return 0
    finally:
        conn.close()


def _integrity(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    conn = sqlite3.connect(path, timeout=15)
    try:
        return "OK" if str(conn.execute("PRAGMA quick_check").fetchone()[0]).lower() == "ok" else "FAIL"
    except Exception:
        return "BUSY"
    finally:
        conn.close()


def _size_mb(path: Path) -> float:
    try:
        return path.stat().st_size / 1024 / 1024
    except Exception:
        return 0.0


def _dir_size_mb(path: Path) -> float:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except Exception:
        pass
    return total / 1024 / 1024


def _fmt(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def _scalar(path: Path, sql: str, params: tuple = ()) -> int:
    if not path.exists():
        return 0
    conn = sqlite3.connect(path, timeout=15)
    try:
        row = conn.execute(sql, params).fetchone()
        return int((row or [0])[0] or 0)
    except Exception:
        return 0
    finally:
        conn.close()


def _text_scalar(path: Path, sql: str, params: tuple = ()) -> str | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=15)
    try:
        row = conn.execute(sql, params).fetchone()
        return str((row or [None])[0]) if row and row[0] is not None else None
    except Exception:
        return None
    finally:
        conn.close()


def _latest_run(path: Path, table: str, *, success_only: bool = False) -> dict:
    if not path.exists():
        return {}
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        where = "WHERE status='success'" if success_only else ""
        row = conn.execute(f"SELECT * FROM {table} {where} ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        conn.close()


def _age_label(age: float | None) -> str:
    if age is None:
        return "n/a"
    if age < 60:
        return f"{int(age)}s"
    return f"{int(age // 60)}m{int(age % 60):02d}s"


def gex_quality_summary(
    *,
    ok: int,
    selected: int,
    failures: list[dict] | None = None,
    failed_open: list[str] | None = None,
) -> dict:
    failures = list(failures or [])
    failed_open = list(failed_open or [])
    counts: dict[str, int] = {}
    for item in failures:
        kind = str(item.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    unavailable_kinds = {"http_404_not_available", "negative_cache_404", "not_available"}
    unavailable = sum(value for key, value in counts.items() if key in unavailable_kinds)
    technical_failed = max(0, int(selected or 0) - int(ok or 0) - unavailable)
    supported = max(0, int(selected or 0) - unavailable)
    universe_coverage = 100.0 * int(ok or 0) / max(1, int(selected or 0))
    supported_coverage = 100.0 * int(ok or 0) / max(1, supported) if supported else 100.0
    if failed_open:
        quality = "DEGRADED_OPEN_POSITION"
    elif technical_failed == 0 and unavailable > 0 and supported_coverage >= 99.0:
        quality = "HEALTHY_WITH_UNAVAILABLE"
    elif technical_failed == 0 and int(ok or 0) == int(selected or 0):
        quality = "COMPLETE"
    elif supported_coverage >= 98.0:
        quality = "HEALTHY_WITH_TECHNICAL_GAPS"
    elif supported_coverage >= 90.0:
        quality = "PARTIAL"
    else:
        quality = "DEGRADED"
    technical_counts = {k: v for k, v in counts.items() if k not in unavailable_kinds}
    return {
        "quality": quality,
        "universe_coverage_pct": round(universe_coverage, 4),
        "supported_coverage_pct": round(supported_coverage, 4),
        "available": int(ok or 0),
        "selected": int(selected or 0),
        "supported": supported,
        "unavailable": unavailable,
        "technical_failed": technical_failed,
        "failed_open": sorted(set(failed_open)),
        "counts": counts,
        "technical_counts": technical_counts,
    }


def auditor_feed_status() -> dict:
    """Canonical Data inputs required by MARKET OUTCOME AUDITOR."""
    latest_deep = {}
    if config.SCANNER_DB.exists():
        conn = sqlite3.connect(config.SCANNER_DB, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT id,snapshot_date,session_type,status,total_tickers_ok,total_tickers_expected
                   FROM scanner_runs WHERE status='success' AND lower(session_type) LIKE '%deep%'
                   ORDER BY snapshot_date DESC,id DESC LIMIT 1"""
            ).fetchone()
            latest_deep = dict(row) if row else {}
        except Exception:
            latest_deep = {}
        finally:
            conn.close()
    price_dates = _scalar(config.SCANNER_DB, "SELECT COUNT(DISTINCT trade_date) FROM ticker_daily_history")
    maturity_horizon = 5
    try:
        coverage_payload = json.loads(config.HISTORY_COVERAGE_FILE.read_text(encoding="utf-8")) if config.HISTORY_COVERAGE_FILE.exists() else {}
    except Exception:
        coverage_payload = {}
    feed_status = "READY_FOR_LABELS" if max(0, price_dates - maturity_horizon) > 0 else "ACCUMULATING"
    return {
        "status": feed_status,
        "daily_price_rows": _count(config.SCANNER_DB, "ticker_daily_history"),
        "daily_price_dates": price_dates,
        "latest_price_date": _text_scalar(config.SCANNER_DB, "SELECT MAX(trade_date) FROM ticker_daily_history"),
        "potential_mature_dates": max(0, price_dates - maturity_horizon),
        "deep_scanner_dates": _scalar(config.SCANNER_DB, "SELECT COUNT(DISTINCT snapshot_date) FROM scanner_runs WHERE status='success' AND lower(session_type) LIKE '%deep%'"),
        "deep_gex_dates": _scalar(config.GEX_DB, "SELECT COUNT(DISTINCT snapshot_date) FROM gex_runs WHERE status='success' AND lower(session_type) LIKE '%deep%'"),
        "full_coverage_dates": int(coverage_payload.get("paired_trading_dates") or 0),
        "full_live_dates": int(coverage_payload.get("live_paired_dates") or 0),
        "full_archive_dates": int(coverage_payload.get("archive_paired_dates") or 0),
        "latest_deep_run_id": int(latest_deep.get("id") or 0),
        "latest_deep_date": latest_deep.get("snapshot_date"),
        "latest_deep_ok": int(latest_deep.get("total_tickers_ok") or 0),
        "latest_deep_expected": int(latest_deep.get("total_tickers_expected") or 0),
        "intraday_zips": sum(1 for _ in config.INTRADAY_ARCHIVE_DIR.rglob("*.zip")) if config.INTRADAY_ARCHIVE_DIR.exists() else 0,
        "profile_zips": sum(1 for _ in config.GEX_PROFILE_ARCHIVE_DIR.rglob("*.zip")) if config.GEX_PROFILE_ARCHIVE_DIR.exists() else 0,
        "maturity_horizon": maturity_horizon,
    }


def auditor_feed_console_line(context: str = "status") -> str:
    state = auditor_feed_status()
    feed_status = str(state.get("status") or ("READY_FOR_LABELS" if state["potential_mature_dates"] > 0 else "ACCUMULATING"))
    return (
        "📊 [AUDITOR FEED] "
        f"context={context} status={feed_status} | "
        f"deep_live_scanner={state['deep_scanner_dates']} deep_live_gex={state['deep_gex_dates']} "
        f"full_coverage={state['full_coverage_dates']} (live={state['full_live_dates']},archive={state['full_archive_dates']}) | "
        f"latest_deep={state.get('latest_deep_date') or 'n/a'} run={state['latest_deep_run_id']} "
        f"universe={state['latest_deep_ok']}/{state['latest_deep_expected']} | "
        f"price_history={state['daily_price_rows']}rows/{state['daily_price_dates']}dates "
        f"latest_price={state.get('latest_price_date') or 'n/a'} potentially_mature={state['potential_mature_dates']}dates | "
        f"intraday_zips={state['intraday_zips']} gex_profile_zips={state['profile_zips']} | "
        f"maturity={state['maturity_horizon']}_future_sessions"
    )


def console_health_line(context: str = "status") -> str:
    """Stable console-only Data telemetry for diagnosis from copied console logs."""
    now = now_madrid()
    operational = is_operational_window(now)
    try:
        state = read_data_state(config.COORDINATION_DB) or {}
    except Exception:
        state = {}
    try:
        ack = read_ack(config.COORDINATION_DB, "alerts_bot") or {}
    except Exception:
        ack = {}
    try:
        peer = read_heartbeat(config.COORDINATION_DB, "alerts_bot") or {}
        peer_age = heartbeat_age_seconds(peer)
    except Exception:
        peer = {}
        peer_age = None
    latest_scanner = _latest_run(config.SCANNER_DB, "scanner_runs", success_only=True)
    current_scanner = _latest_run(config.SCANNER_DB, "scanner_runs", success_only=False)
    latest_gex = _latest_run(config.GEX_DB, "gex_runs", success_only=True)
    manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else {}
    generation = int(state.get("generation") or 0)
    ack_generation = int(ack.get("generation") or 0)
    pending = max(0, generation - ack_generation)
    peer_ok = peer_age is not None and peer_age < int(config.COORDINATION_STALE_SECONDS) and str(peer.get("status") or "").lower() == "running"
    return (
        "🧭 [DATA HEALTH] "
        f"context={context} time={now.strftime('%Y-%m-%d %H:%M:%S')} mode={'OPERATIVE' if operational else 'REST'} | "
        f"generation={generation} bootstrap={'LISTO' if state.get('bootstrap_complete') else 'EN_PROGRESO'} "
        f"core={manifest.get('core_history_dates',0)}/{manifest.get('core_history_target_sessions',config.CORE_HISTORY_TARGET_SESSIONS)} "
        f"full={state.get('paired_trading_dates', manifest.get('paired_trading_dates',0))}/{manifest.get('target_daily_closes',config.DAILY_CLOSE_WINDOW_SESSIONS)} | "
        f"scanner_success={latest_scanner.get('id') or 0}:{latest_scanner.get('snapshot_date') or 'n/a'}:{latest_scanner.get('session_type') or 'n/a'} "
        f"current={current_scanner.get('id') or 0}:{current_scanner.get('status') or 'n/a'}:{current_scanner.get('completion_stage') or 'n/a'} | "
        f"gex_success={latest_gex.get('id') or 0}:{latest_gex.get('snapshot_date') or 'n/a'}:{latest_gex.get('status') or 'n/a'} | "
        f"alerts_ack={ack_generation}:{ack.get('status') or 'n/a'} pending_generations={pending} "
        f"alerts_hb={'OK' if peer_ok else 'STALE'} age={_age_label(peer_age)}"
    )


def run_completion_line(
    *,
    run_id: int,
    session_type: str,
    profile: str,
    elapsed_seconds: float,
    scanner_ok: int,
    scanner_expected: int,
    scanner_failed: int,
    changed_tickers: int,
    new_tickers: int,
    selected_tickers: int,
    gex_run_id: int | None,
    gex_ok: int,
    gex_selected: int,
    gex_failed: int,
    strikes: int,
    expirations: int,
    gex_failures: list[dict] | None = None,
    gex_failed_open: list[str] | None = None,
    coverage: dict,
    handoff_ok: bool,
) -> str:
    try:
        state = read_data_state(config.COORDINATION_DB) or {}
    except Exception:
        state = {}
    try:
        ack = read_ack(config.COORDINATION_DB, "alerts_bot") or {}
    except Exception:
        ack = {}
    generation = int(state.get("generation") or 0)
    ack_generation = int(ack.get("generation") or 0)
    gex_failures = list(gex_failures or [])
    gex_failed_open = list(gex_failed_open or [])
    gex_quality_state = gex_quality_summary(
        ok=int(gex_ok or 0), selected=int(gex_selected or 0),
        failures=gex_failures, failed_open=gex_failed_open,
    )
    gex_coverage = float(gex_quality_state["universe_coverage_pct"])
    gex_quality = str(gex_quality_state["quality"])
    failure_summary = ",".join(f"{key}:{value}" for key, value in sorted(gex_quality_state["counts"].items())) or "none"
    return (
        "✅ [DATA RUN END] "
        f"scanner_run={run_id} gex_run={gex_run_id or 0} session={session_type} profile={profile} "
        f"elapsed={elapsed_seconds:.1f}s status={'SUCCESS' if handoff_ok else 'WARN'} | "
        f"scanner={scanner_ok}/{scanner_expected} failed={scanner_failed} changed={changed_tickers} new={new_tickers} "
        f"enriched={selected_tickers} | "
        f"gex={gex_ok}/{gex_selected} universe_coverage={gex_coverage:.1f}% supported_coverage={gex_quality_state['supported_coverage_pct']:.1f}% "
        f"quality={gex_quality} unavailable={gex_quality_state['unavailable']} technical_failed={gex_quality_state['technical_failed']} "
        f"failed_open={len(gex_failed_open)} reasons={failure_summary} strikes={strikes} expirations={expirations} | "
        f"CORE={coverage.get('core_history_dates',0)}/{coverage.get('core_history_target_sessions',config.CORE_HISTORY_TARGET_SESSIONS)} "
        f"FULL={coverage.get('paired_trading_dates',0)}/{coverage.get('target_daily_closes',config.DAILY_CLOSE_WINDOW_SESSIONS)} | "
        f"handoff={'PUBLICADO' if handoff_ok else 'CON_AVISOS'} generation={generation} "
        f"ack={ack_generation}:{ack.get('status') or 'n/a'} pending_generations={max(0,generation-ack_generation)}"
    )


def storage_message() -> str:
    scanner_state = _integrity(config.SCANNER_DB)
    gex_state = _integrity(config.GEX_DB)
    raw_state = _integrity(config.RAW_DB)
    meta_state = _integrity(config.META_DB)
    states = [scanner_state, gex_state, raw_state, meta_state]

    scanner_rows = _count(config.SCANNER_DB, "scanner_snapshots")
    gex_rows = _count(config.GEX_DB, "gex_summary")
    raw_payloads = _count(config.RAW_DB, "raw_api_payloads")
    schema_fields = _count(config.META_DB, "schema_registry")
    extra_fields = _count(config.META_DB, "extra_fields")

    archive_mb = _dir_size_mb(config.ARCHIVE_DIR)
    intraday_mb = _dir_size_mb(config.INTRADAY_ARCHIVE_DIR)
    profiles_mb = _dir_size_mb(config.GEX_PROFILE_ARCHIVE_DIR)
    zips = sum(1 for _ in config.ARCHIVE_DIR.rglob("*.zip")) if config.ARCHIVE_DIR.exists() else 0

    cards = ["\n".join([
        "**Bases live**",
        f"• **Scanner:** `{_size_mb(config.SCANNER_DB):.1f} MB` · `{_fmt(scanner_rows)}` snapshots · `{scanner_state}`",
        f"• **GEX:** `{_size_mb(config.GEX_DB):.1f} MB` · `{_fmt(gex_rows)}` resúmenes · `{gex_state}`",
        f"• **Raw:** `{_size_mb(config.RAW_DB):.1f} MB` · `{_fmt(raw_payloads)}` payloads · `{raw_state}`",
        f"• **Meta:** `{_size_mb(config.META_DB):.1f} MB` · `{_fmt(schema_fields)}` campos + `{_fmt(extra_fields)}` extras · `{meta_state}`",
        "",
        f"**Archivo:** `{archive_mb:.1f} MB` · ZIP `{zips}` · intradía `{intraday_mb:.1f} MB` · perfiles GEX `{profiles_mb:.1f} MB`",
        f"**Integridad:** `{sum(state == 'OK' for state in states)}/4 OK` · retención/mantenimiento fuera de mercado.",
    ])]
    return build_group("🗄️ DATA · BASES Y ARCHIVO", cards)


def send_storage_summary() -> bool:
    return bool(log_schema(storage_message()))


def heartbeat_message() -> str:
    now = now_madrid()
    operational = is_operational_window(now)
    try:
        ack = read_ack(config.COORDINATION_DB, "alerts_bot")
    except Exception:
        ack = {}
    try:
        state_payload = read_data_state(config.COORDINATION_DB)
    except Exception:
        state_payload = {}
    try:
        peer = read_heartbeat(config.COORDINATION_DB, "alerts_bot")
        peer_age = heartbeat_age_seconds(peer)
    except Exception:
        peer = {}
        peer_age = None

    stale_limit = int(config.COORDINATION_STALE_SECONDS)
    peer_status = str(peer.get("status") or "").lower()
    if peer_age is not None and peer_age < stale_limit and peer_status == "running":
        alerts_state = "OK"
    elif peer_age is None:
        alerts_state = "SIN HEARTBEAT"
    else:
        alerts_state = "CAÍDO / SIN HEARTBEAT"

    data_generation = int(state_payload.get("generation") or 0)
    ack_generation = int(ack.get("generation") or 0)
    pending = max(0, data_generation - ack_generation)
    state = "OPERATIVO" if operational else "DESCANSO"
    next_event = "capturas según scheduler" if operational else next_operational_start(now).strftime("%Y-%m-%d %H:%M")
    latest_scanner = _latest_run(config.SCANNER_DB, "scanner_runs", success_only=True)
    latest_gex = _latest_run(config.GEX_DB, "gex_runs", success_only=True)
    feed = auditor_feed_status()

    cards = ["\n".join([
        f"**Estado:** `{state}` · **Alerts:** `{alerts_state}` · edad `{_age_label(peer_age)}`",
        f"**Último ACK:** generación `{ack_generation}` · `{ack.get('status') or 'n/a'}` · pendientes `{pending}`",
        f"**Generación Data:** `{data_generation}` · Scanner SUCCESS `{latest_scanner.get('id') or 0}` · GEX SUCCESS `{latest_gex.get('id') or 0}`",
        f"**Bases:** Scanner `{_fmt(_count(config.SCANNER_DB, 'scanner_snapshots'))}` · GEX `{_fmt(_count(config.GEX_DB, 'gex_summary'))}`",
        f"**Auditor feed:** deep `{feed['deep_scanner_dates']}` · precios `{feed['daily_price_dates']}` fechas · estado `{feed['status']}`",
        f"**Próximo evento:** `{next_event}`",
    ])]
    return build_group("💓 DATA · HEARTBEAT", cards)


def send_heartbeat() -> bool:
    return bool(log_info(heartbeat_message(), send_discord=True))
