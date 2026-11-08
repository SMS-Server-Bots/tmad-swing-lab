from __future__ import annotations

import config
from coordination_bus import read_ack, read_data_state, read_heartbeat, heartbeat_age_seconds
from db import (
    get_active_model,
    get_open_trades,
    get_runtime_state,
    history_coverage,
    latest_processed_scanner_run_id,
    latest_successful_scanner_run,
    successful_scanner_runs_after,
)
from model_metrics import normalized_oos_metrics
from pipeline_state import get_pipeline_state
from discord_presentation import build_group
from discord_utils import log_info
from time_utils import now_madrid, is_operational_window, next_operational_start


def _age_label(age: float | None) -> str:
    if age is None:
        return "n/a"
    if age < 60:
        return f"{int(age)}s"
    return f"{int(age // 60)}m{int(age % 60):02d}s"


def _fmt_pct(value, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except Exception:
        return "n/a"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def _snapshot_payload() -> dict:
    now = now_madrid()
    pipeline = get_pipeline_state() or {}
    active = get_active_model() or {}
    oos = normalized_oos_metrics(active.get("metrics") or {})
    try:
        data = read_data_state(config.COORDINATION_DB) or {}
    except Exception:
        data = {}
    try:
        ack = read_ack(config.COORDINATION_DB, "alerts_bot") or {}
    except Exception:
        ack = {}
    try:
        data_heartbeat = read_heartbeat(config.COORDINATION_DB, "data_bot") or {}
        data_age = heartbeat_age_seconds(data_heartbeat)
    except Exception:
        data_heartbeat = {}
        data_age = None
    try:
        latest_scanner = latest_successful_scanner_run() or {}
    except Exception:
        latest_scanner = {}
    try:
        processed_run = latest_processed_scanner_run_id()
    except Exception:
        processed_run = 0
    try:
        coverage = history_coverage() or {}
    except Exception:
        coverage = {}
    try:
        trades = get_open_trades()
    except Exception:
        trades = []

    latest_run = _safe_int(latest_scanner.get("id"))
    try:
        pending_successful = successful_scanner_runs_after(processed_run, limit=1000)
    except Exception:
        pending_successful = []
    pending_run_ids = [_safe_int(row.get("id")) for row in pending_successful if _safe_int(row.get("id"))]
    live_positions = sum(str(row.get("alert_status") or "").lower() in {"open", "sent"} for row in trades)
    shadow_positions = sum(str(row.get("alert_status") or "").lower().startswith("shadow") for row in trades)
    data_generation = _safe_int(data.get("generation"))
    ack_generation = _safe_int(ack.get("generation"))
    return {
        "now": now,
        "pipeline": pipeline,
        "active": active,
        "oos": oos,
        "data": data,
        "ack": ack,
        "data_heartbeat": data_heartbeat,
        "data_age": data_age,
        "latest_scanner": latest_scanner,
        "latest_run": latest_run,
        "processed_run": processed_run,
        "backlog_runs": len(pending_run_ids),
        "pending_run_ids": pending_run_ids,
        "coverage": coverage,
        "live_positions": live_positions,
        "shadow_positions": shadow_positions,
        "data_generation": data_generation,
        "ack_generation": ack_generation,
        "generation_gap": max(0, data_generation - ack_generation),
        "audit_status": str(get_runtime_state("market_outcome_audit_status", "ACCUMULATING") or "ACCUMULATING"),
        "audit_events": _safe_int(get_runtime_state("market_outcome_audit_last_events", "0")),
        "learning_state": str(get_runtime_state("adaptive_learning_state", "ACCUMULATING") or "ACCUMULATING"),
        "learning_run_id": _safe_int(get_runtime_state("adaptive_learning_run_id", "0")),
        "shadow_model_id": _safe_int(get_runtime_state("adaptive_shadow_model_id", "0")),
        "research_gate": get_runtime_state("adaptive_research_gate_shadow", str(config.FUSION_MIN_ACTIONABLE_SCORE)),
        "core_learning_deferred": str(get_runtime_state("core_learning_deferred", "0") or "0") == "1",
        "core_learning_reason": str(get_runtime_state("core_learning_deferred_reason", "") or ""),
        "core_learning_context": str(get_runtime_state("core_learning_deferred_context", "") or ""),
        "core_learning_generation": _safe_int(get_runtime_state("core_learning_deferred_generation", "0")),
        "core_learning_pending_runs": str(get_runtime_state("core_learning_deferred_pending_runs", "[]") or "[]"),
    }


def console_snapshot_lines(context: str = "status") -> list[str]:
    """Console-only operational telemetry sufficient for remote diagnosis.

    These lines are intentionally stable and machine-readable. They never publish
    to Discord and must never interrupt Alerts if a diagnostic query is busy.
    """
    try:
        snap = _snapshot_payload()
        pipeline = snap["pipeline"]
        active = snap["active"]
        oos = snap["oos"]
        data = snap["data"]
        ack = snap["ack"]
        latest = snap["latest_scanner"]
        coverage = snap["coverage"]
        peer_fresh = snap["data_age"] is not None and snap["data_age"] < int(config.COORDINATION_STALE_SECONDS)
        model_id = _safe_int(active.get("id"))
        model_key = str(active.get("model_key") or "sin_modelo")
        model_status = str(active.get("status") or "none").upper()
        entry_mode = str(get_runtime_state("entry_mode", pipeline.get("entry_mode") or "off") or "off").upper()
        core_dates = _safe_int(coverage.get("core_feature_dates"))
        core_target = _safe_int(coverage.get("core_history_target"), int(config.CORE_HISTORY_TARGET_SESSIONS))
        core_outcome_dates = _safe_int(coverage.get("core_outcome_dates"))
        full_dates = _safe_int(coverage.get("feature_dates"))
        full_outcomes = _safe_int(coverage.get("outcome_dates"))
        return [
            (
                "🧭 [ALERTS STATE] "
                f"context={context} | time={snap['now'].strftime('%Y-%m-%d %H:%M:%S')} | "
                f"pipeline={str(pipeline.get('state') or 'UNKNOWN')} stage={str(pipeline.get('stage') or 'n/a')} "
                f"entry={entry_mode} generation={_safe_int(pipeline.get('generation'))} | "
                f"model=#{model_id} status={model_status} key={model_key}"
            ),
            (
                "🔗 [ALERTS SYNC] "
                f"data_gen={snap['data_generation']} bootstrap={'LISTO' if data.get('bootstrap_complete') else 'EN_PROGRESO'} "
                f"core_ready={bool(data.get('core_ready'))} coverage_end={data.get('coverage_end_date') or 'n/a'} | "
                f"latest_scanner={snap['latest_run']}:{latest.get('snapshot_date') or 'n/a'}:{latest.get('session_type') or 'n/a'} "
                f"rows={_safe_int(latest.get('snapshot_count'))} | processed={snap['processed_run']} backlog_valid={snap['backlog_runs']} "
                f"pending_ids={snap['pending_run_ids'] or 'none'} | "
                f"handoff_ack={snap['ack_generation']}:{ack.get('status') or 'n/a'} gen_gap={snap['generation_gap']} | "
                f"data_hb={'OK' if peer_fresh else 'STALE'} age={_age_label(snap['data_age'])}"
            ),
            (
                "🧪 [ALERTS EVIDENCE] "
                f"CORE={core_dates}/{core_target} dates outcomes={core_outcome_dates} "
                f"rows={_safe_int(coverage.get('core_feature_rows'))} tickers={_safe_int(coverage.get('core_feature_tickers'))} | "
                f"FULL_ALERTS={full_dates}/{_safe_int(coverage.get('daily_close_target'), int(config.DAILY_CLOSE_WINDOW_SESSIONS))} "
                f"FULL_DATA={_safe_int(data.get('paired_trading_dates') or (data.get('manifest') or {}).get('paired_trading_dates'))}/{_safe_int((data.get('manifest') or {}).get('target_daily_closes'), int(config.DAILY_CLOSE_WINDOW_SESSIONS))} "
                f"pending_deep={snap['pending_run_ids']} outcomes={full_outcomes} intraday={_safe_int(coverage.get('intraday_dates'))}d/{_safe_int(coverage.get('intraday_runs'))}r | "
                f"OOS_n={_safe_int(oos.get('sample'))} hit={_fmt_pct(oos.get('target_hit_rate') or oos.get('win_rate'))} "
                f"PF={oos.get('profit_factor') if oos.get('profit_factor') is not None else 'n/a'}"
            ),
            (
                "📌 [ALERTS BOOK/AUDIT] "
                f"positions_live={snap['live_positions']} positions_shadow={snap['shadow_positions']} | "
                f"auditor={snap['audit_status']} mature_events={snap['audit_events']} | "
                f"learning={snap['learning_state']} run={snap['learning_run_id']} shadow_model={snap['shadow_model_id']} research_gate={snap['research_gate']} | "
                f"coord_peer_pid={snap['data_heartbeat'].get('pid') or 'n/a'}"
            ),
            (
                "📚 [LEARNING QUEUE] "
                f"pending={'yes' if snap['core_learning_deferred'] else 'no'} "
                f"reason={snap['core_learning_reason'] or 'none'} generation={snap['core_learning_generation']} "
                f"pending_runs={snap['core_learning_pending_runs']} context={snap['core_learning_context'] or 'n/a'}"
            ),
        ]
    except Exception as exc:
        return [f"⚠️ [ALERTS STATE] context={context} | diagnóstico no disponible: {type(exc).__name__}: {exc}"]


def emit_console_snapshot(context: str = "status") -> None:
    for line in console_snapshot_lines(context):
        log_info(line, send_discord=False)


def heartbeat_message() -> str:
    snap = _snapshot_payload()
    now = snap["now"]
    pipeline = snap["pipeline"]
    active = snap["active"]
    operational = is_operational_window(now)
    next_event = "próximo run Data" if operational else next_operational_start(now).strftime("%Y-%m-%d %H:%M")
    data_ok = snap["data_age"] is not None and snap["data_age"] < int(config.COORDINATION_STALE_SECONDS)
    return build_group("💓 ALERTS · HEARTBEAT", ["\n".join([
        f"**Pipeline:** `{str(pipeline.get('state') or 'UNKNOWN')}` · **generación:** `{snap['data_generation']}` · **Data:** `{'OK' if data_ok else 'PENDIENTE'}`",
        f"**Sincronización operativa:** último Scanner `{snap['latest_run']}` · procesado `{snap['processed_run']}` · pendientes válidos `{snap['backlog_runs']}` `{snap['pending_run_ids'] or ''}`",
        f"**Handoff/modelo:** ACK generación `{snap['ack_generation']} / {snap['ack'].get('status') or 'n/a'}` · gap generaciones `{snap['generation_gap']}`",
        f"**Modo:** `{str(get_runtime_state('entry_mode','off') or 'off').upper()}` · **CORE:** `#{_safe_int(active.get('id'))}` · posiciones LIVE `{snap['live_positions']}` / SHADOW `{snap['shadow_positions']}`",
        f"**Auditor:** `{snap['audit_status']}` · movimientos maduros `{snap['audit_events']}` · **Learning:** `{snap['learning_state']}` · shadow `#{snap['shadow_model_id']}`",
        f"**Cola aprendizaje CORE:** `{'PENDIENTE' if snap['core_learning_deferred'] else 'LIBRE'}`"
        + (f" · motivo `{snap['core_learning_reason']}`" if snap['core_learning_deferred'] else ""),
        f"**Estado horario:** `{'OPERATIVO' if operational else 'DESCANSO'}` · **próximo evento:** `{next_event}`",
    ])])


def send_heartbeat() -> bool:
    return bool(log_info(heartbeat_message(), send_discord=True))
