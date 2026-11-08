import argparse
import json
import os
import sys


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
import traceback
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from archive_bootstrap import bootstrap_from_archives
from alerts_engine import run_alert_scan
from layer_history import ensure_layer_history_schema, materialize_layer_outcomes
from layer_archive import archive_completed_layer_dates
from layer_calibration import refresh_layer_calibrations
from market_outcome_auditor import ensure_market_outcome_audit_schema, maybe_run_market_outcome_audit, run_market_outcome_audit
from adaptive_learning import ensure_adaptive_learning_schema, maybe_run_adaptive_learning, run_adaptive_learning
from backtesting import run_backtest
from db import (
    activate_due_model, data_dbs_available, ensure_alerts_schema, insert_heartbeat,
    purge_research_retention, latest_processed_scanner_run_id, latest_successful_scanner_run, successful_scanner_runs_after,
    latest_scanner_run_any, purge_intraday_observations_through, get_runtime_state, set_runtime_state, history_coverage,
    canonical_materialization_reconciliation, get_active_model, core_feature_date_inventory,
)
from discord_utils import log_error, log_info, log_warning, send_daily
from optimizer import run_optimizer
from core_history import materialize_core_history_if_needed, core_source_coverage
from core_reconciliation import reconcile_core_universe, reconcile_core_dates
from outcomes import materialize_outcomes
from research import export_research_tables
from daily_summary import send_daily_operating_summary, send_rules_config
from setup_reports import maybe_send_for_scanner_run, send_recovery_snapshot, send_setup_phase
from scheduler import due_jobs
from time_utils import now_madrid, is_operational_window, next_operational_start
from startup_calibration import run_startup_warmup, decide_operational_mode
from performance_report import build_performance_report
from intraday_archive_bootstrap import bootstrap_intraday_archives_if_ready
from relationship_learning import refresh_relationship_graph
from calibration_state import build_core_calibration_signature
from pipeline_state import transition_pipeline_state, readiness_state, get_pipeline_state, resume_after_rest
from discord_presentation import build_group
from positions_overview import maybe_send_open_positions_overview
from risk_refresh import maybe_process_partial_risk_refresh
from lifecycle import retry_pending_exit_deliveries
from reporting_v621 import send_heartbeat as send_alerts_heartbeat, emit_console_snapshot
from coordination_bus import (
    acknowledge_generation, heartbeat_age_seconds, read_ack, read_data_state,
    read_heartbeat, write_heartbeat,
)

LOCK_FILE = config.LOCK_DIR / "tmad_alerts_adaptive_bot.lock"


_LOG_THROTTLE: dict[str, float] = {}


def _log_info_throttled(key: str, message: str, *, interval_seconds: int = 300) -> bool:
    now_tick = time.monotonic()
    last_tick = _LOG_THROTTLE.get(str(key))
    if last_tick is not None and now_tick - last_tick < max(1, int(interval_seconds)):
        return False
    _LOG_THROTTLE[str(key)] = now_tick
    log_info(message, send_discord=False)
    return True


def _pid_is_running(pid: int) -> bool:
    """Comprueba un PID sin usar os.kill(pid, 0) en Windows.

    En Windows, os.kill(pid, 0) puede devolver WinError 87 incluso para un lock
    perfectamente recuperable. OpenProcess + GetExitCodeProcess distingue un
    proceso vivo de un PID antiguo sin enviar ninguna señal.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Fallback no destructivo: tasklist está disponible en Windows Server.
            try:
                import subprocess
                completed = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                output = (completed.stdout or "").lower()
                return str(pid) in output and "no tasks" not in output and "ninguna tarea" not in output
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
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = 0
        if _pid_is_running(pid):
            raise RuntimeError(f"El Alerts Bot ya parece activo con PID {pid}.")
        LOCK_FILE.unlink(missing_ok=True)
        print(f"🧹 Lock antiguo del Alerts Bot eliminado | pid={pid or 'inválido'}", flush=True)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _read_data_history_manifest() -> dict:
    """Read the transactional coordination bus first, with JSON fallback."""
    try:
        state = read_data_state(config.COORDINATION_DB)
        manifest = state.get("manifest") if isinstance(state, dict) else None
        if isinstance(manifest, dict) and manifest:
            manifest = dict(manifest)
            manifest["_coordination_generation"] = int(state.get("generation") or 0)
            manifest["_coordination_updated_at_utc"] = state.get("updated_at_utc")
            return manifest
    except Exception:
        pass
    try:
        if config.DATA_HISTORY_STATUS_FILE.exists():
            return json.loads(config.DATA_HISTORY_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _coordination_result_status(result: dict) -> str:
    status = str(result.get("status") or "unknown")
    if status in {
        "calibrated", "calibrated_with_warnings", "optimizer_executed_no_champion",
        "already_processed", "outcomes_pending", "calibration_deferred_operational",
    }:
        return "processed"
    if status in {"data_bootstrap_in_progress", "partial_universe"}:
        return "waiting"
    if status in {"manifest_missing_or_stale", "universe_mismatch", "materialization_incomplete"}:
        return "blocked"
    return "processed" if result.get("executed") else "waiting"


def _coordination_state_ready(state: dict) -> bool:
    """True only for the semantic LISTO state published by the current Data Bot.

    remaining=0 alone is not enough: during startup Data can already know the full
    eligible/excluded universe while bootstrap_complete is still false. Alerts must
    never touch CORE until Data has atomically published the final manifest.
    """
    universe = int(state.get("universe_tickers") or 0)
    eligible = int(state.get("eligible_tickers") or 0)
    excluded = int(state.get("excluded_tickers") or 0)
    pending = int(state.get("pending_tickers") or 0)
    remaining = int(state.get("remaining_tickers") or 0)
    return bool(
        str(state.get("bot_version") or "") == str(config.BOT_VERSION)
        and state.get("bootstrap_complete")
        and state.get("core_ready")
        and universe > 0
        and eligible + excluded == universe
        and pending == 0
        and remaining == 0
    )


def _core_watch_allowed(*, operational_now: bool, startup_training_pending: bool, startup_recovery_sent: bool) -> bool:
    """Independent CORE watcher is a daytime maintenance path, never startup handoff.

    During restart, Coordination Bus exclusively owns the first calibration. This
    prevents an incomplete generation or weekend rest loop from starting a second
    materialization in parallel.
    """
    return bool(operational_now and not startup_training_pending and startup_recovery_sent)


def _hhmm_minutes(value: str) -> int:
    try:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
        return hour * 60 + minute
    except Exception:
        return 0


def _entry_window_open(now_market: datetime | None = None) -> bool:
    """Return True only inside the configured US entry window.

    Heavy CORE materialisation/optimisation is intentionally forbidden here: fresh
    Scanner runs must always have priority because TMAD is an anticipation system.
    """
    now_market = now_market or datetime.now(ZoneInfo(config.ENTRY_MARKET_TIMEZONE))
    if now_market.weekday() >= 5:
        return False
    current = now_market.hour * 60 + now_market.minute
    start = _hhmm_minutes(config.ENTRY_WINDOW_START)
    end = _hhmm_minutes(config.ENTRY_WINDOW_END)
    return bool(start <= current <= end)


def _valid_pending_scanner_runs(limit: int = 1000) -> list[dict]:
    try:
        processed = int(latest_processed_scanner_run_id() or 0)
        return list(successful_scanner_runs_after(processed, limit=max(1, int(limit))) or [])
    except Exception:
        return []


def _data_run_in_progress() -> dict:
    """Return the latest Data run when Scanner/deep is still running."""
    try:
        row = latest_scanner_run_any() or {}
    except Exception:
        return {}
    status = str(row.get("status") or "").strip().lower()
    stage = str(row.get("stage") or "").strip().lower()
    if status == "running" or stage in {"created", "running", "fetching", "enriching", "saving"}:
        return row
    return {}


def _coordination_gap() -> dict:
    """Describe unconsumed Data generations without confusing run-id gaps."""
    try:
        state = read_data_state(config.COORDINATION_DB) or {}
        ack = read_ack(config.COORDINATION_DB, "alerts_bot") or {}
        data_generation = int(state.get("generation") or 0)
        ack_generation = int(ack.get("generation") or 0)
        gap = max(0, data_generation - ack_generation)
        return {
            "gap": gap,
            "data_generation": data_generation,
            "ack_generation": ack_generation,
            "ack_status": str(ack.get("status") or ""),
        }
    except Exception:
        return {"gap": 0, "data_generation": 0, "ack_generation": 0, "ack_status": ""}


def _rest_completion_pending() -> bool:
    """Allow post-midnight drain of a deep/handoff before heartbeat-only REST."""
    if _valid_pending_scanner_runs(limit=1000):
        return True
    if _data_run_in_progress():
        return True
    return bool(int(_coordination_gap().get("gap") or 0) > 0)


def _outcome_rows_new(value) -> int:
    """Normalize legacy int and newer dict returns from materialize_outcomes()."""
    if isinstance(value, dict):
        raw = value.get("rows") or value.get("inserted") or value.get("new_rows") or 0
    else:
        raw = value or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _restore_ready_pipeline(
    operational: dict,
    *,
    stage: str,
    generation: int = 0,
    details: dict | None = None,
) -> None:
    """Never leave Alerts stuck in BUILDING_CORE after a no-op/deferred path."""
    active = get_active_model() or {}
    entry_mode = str(operational.get("entry_mode") or "shadow")
    transition_pipeline_state(
        readiness_state(entry_mode, safe=True),
        stage=stage,
        generation=int(generation or 0),
        entry_mode=entry_mode,
        model_id=int(active.get("id") or 0) or None,
        details=dict(details or {}),
    )


def _set_core_learning_queue(
    pending: bool,
    *,
    reason: str = "",
    context: str = "",
    generation: int = 0,
    pending_runs: list[int] | None = None,
) -> None:
    set_runtime_state("core_learning_deferred", "1" if pending else "0")
    set_runtime_state("core_learning_deferred_reason", str(reason or ""))
    set_runtime_state("core_learning_deferred_context", str(context or ""))
    set_runtime_state("core_learning_deferred_generation", str(int(generation or 0)))
    set_runtime_state("core_learning_deferred_pending_runs", json.dumps(list(pending_runs or [])))
    if pending:
        set_runtime_state("core_learning_deferred_at", now_madrid().isoformat())
    else:
        set_runtime_state("core_learning_completed_at", now_madrid().isoformat())


def _core_learning_defer_reason(*, context: str = "", force: bool = False) -> dict:
    """Protect operational freshness from expensive learning work.

    `force` means bypass model idempotency, not bypass operational safety. Even an
    adaptive retrain request waits until the entry window is closed and all valid
    Scanner runs have been consumed.
    """
    running = _data_run_in_progress()
    if running:
        return {
            "reason": "data_run_running",
            "pending_runs": [],
            "pending_count": 0,
            "data_run_id": int(running.get("id") or 0),
            "data_session": str(running.get("session_type") or ""),
            "data_stage": str(running.get("stage") or running.get("status") or "running"),
            "context": context,
            "force_requested": bool(force),
        }
    pending = _valid_pending_scanner_runs(limit=1000)
    pending_ids = [int(row.get("id") or 0) for row in pending if int(row.get("id") or 0)]
    if pending_ids:
        return {
            "reason": "fresh_scanner_backlog",
            "pending_runs": pending_ids,
            "pending_count": len(pending_ids),
            "context": context,
            "force_requested": bool(force),
        }
    coordination = _coordination_gap()
    if int(coordination.get("gap") or 0) > 0:
        return {
            "reason": "data_generation_gap",
            "pending_runs": [],
            "pending_count": 0,
            **coordination,
            "context": context,
            "force_requested": bool(force),
        }
    now_market = datetime.now(ZoneInfo(config.ENTRY_MARKET_TIMEZONE))
    if now_market.weekday() < 5:
        current = now_market.hour * 60 + now_market.minute
        start = _hhmm_minutes(config.ENTRY_WINDOW_START)
        end = _hhmm_minutes(config.ENTRY_WINDOW_END)
        guard = int(getattr(config, "CORE_LEARNING_ENTRY_GUARD_MINUTES", 180))
        if start <= current <= end:
            return {
                "reason": "entry_window_open",
                "pending_runs": [],
                "pending_count": 0,
                "market_time": now_market.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "window": f"{config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END}",
                "context": context,
                "force_requested": bool(force),
            }
        minutes_to_entry = start - current
        if 0 < minutes_to_entry <= guard:
            return {
                "reason": "entry_window_guard",
                "pending_runs": [],
                "pending_count": 0,
                "market_time": now_market.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "minutes_to_entry": minutes_to_entry,
                "guard_minutes": guard,
                "window": f"{config.ENTRY_WINDOW_START}-{config.ENTRY_WINDOW_END}",
                "context": context,
                "force_requested": bool(force),
            }
    return {}


def _run_audit_learning_after_core(*, context: str, core_result: dict, send_discord: bool = True) -> tuple[dict, dict]:
    """Refresh validation/learning only when CORE work actually changed evidence."""
    if bool(core_result.get("deferred")):
        return (
            {"status": "deferred_operational", "executed": False},
            {"status": "deferred_operational", "executed": False},
        )
    audit = maybe_run_market_outcome_audit(
        context=context,
        send_discord=send_discord,
        console_if_unchanged=True,
    )
    learning = maybe_run_adaptive_learning(
        context=context,
        audit_payload=audit if audit.get("metrics") else None,
        send_discord=send_discord,
        console_if_unchanged=True,
    )
    return audit, learning


def _process_coordination_generation(*, force: bool = False, startup_context: bool = False) -> dict:
    coordination_started = time.time()
    """Process and ACK one Data generation; safe across either bot restarting.

    A generation marked EN PROGRESO is acknowledged WAITING without invoking any
    CORE/outcome/optimizer work. Only the atomic LISTO state may enter calibration.
    """
    state = read_data_state(config.COORDINATION_DB)
    generation = int(state.get("generation") or 0)
    if generation <= 0:
        return {"status": "no_generation", "executed": False, "generation": 0}

    ready = _coordination_state_ready(state)
    ack = read_ack(config.COORDINATION_DB, "alerts_bot")
    ack_generation = int(ack.get("generation") or 0)
    ack_status = str(ack.get("status") or "")
    attempts = int(ack.get("attempt_count") or 0) if ack_generation == generation else 0

    # A processed generation is final. WAITING/BLOCKED are final only while the
    # current state is still incomplete; a semantic LISTO publish normally creates
    # a new generation, but this guard also tolerates a repaired state in place.
    if not force and ack_generation == generation:
        if ack_status == "processed":
            return {"status": "already_acknowledged", "executed": False, "generation": generation, "ack_status": ack_status}
        if ack_status == "blocked":
            return {"status": "already_acknowledged", "executed": False, "generation": generation, "ack_status": ack_status}
        if ack_status == "waiting" and not ready:
            return {"status": "already_acknowledged", "executed": False, "generation": generation, "ack_status": ack_status}

    if attempts >= config.COORDINATION_MAX_RETRIES and ack_status == "error":
        return {"status": "retry_limit", "executed": False, "generation": generation, "attempts": attempts}

    attempt = attempts + 1
    if not ready:
        version_matches = str(state.get("bot_version") or "") == str(config.BOT_VERSION)
        transition_pipeline_state(
            "WAITING_DATA", stage="coordination_waiting", generation=generation,
            entry_mode="shadow" if version_matches else "off",
            error_code=(None if version_matches else "VERSION_MISMATCH"),
            details={"bootstrap_complete": bool(state.get("bootstrap_complete")), "core_ready": bool(state.get("core_ready"))},
        )
        wait_status = "waiting" if version_matches else "blocked"
        result = {
            "status": "data_bootstrap_in_progress" if version_matches else "manifest_missing_or_stale",
            "executed": False,
            "generation": generation,
            "bootstrap_complete": bool(state.get("bootstrap_complete")),
            "core_ready": bool(state.get("core_ready")),
            "universe_tickers": int(state.get("universe_tickers") or 0),
            "eligible_tickers": int(state.get("eligible_tickers") or 0),
            "excluded_tickers": int(state.get("excluded_tickers") or 0),
            "pending_tickers": int(state.get("pending_tickers") or 0),
            "remaining_tickers": int(state.get("remaining_tickers") or 0),
        }
        acknowledge_generation(
            config.COORDINATION_DB, "alerts_bot", generation, wait_status,
            message=str(result["status"]), details=result, attempt_count=attempt,
        )
        wait_signature = "|".join([
            str(wait_status), str(state.get("bot_version") or ""),
            str(state.get("eligible_tickers") or 0), str(state.get("excluded_tickers") or 0),
            str(state.get("universe_tickers") or 0), str(state.get("remaining_tickers") or 0),
        ])
        last_wait_signature = str(get_runtime_state("discord_coordination_wait_signature", "") or "")
        if (ack_generation != generation or ack_status != wait_status) and wait_signature != last_wait_signature:
            set_runtime_state("discord_coordination_wait_signature", wait_signature)
            log_info(build_group(
                f"{'🟡' if version_matches else '🔴'} DATA → ALERTS · {'ESPERANDO' if version_matches else 'BLOQUEADO'}",
                ["\n".join([
                    f"**Generación:** `{generation}` · **Data:** `{'EN PROGRESO' if version_matches else 'VERSIÓN NO VÁLIDA'}`",
                    f"**Universo:** `{state.get('eligible_tickers', 0)} elegibles + {state.get('excluded_tickers', 0)} excluidos / {state.get('universe_tickers', 0)}` · pendientes `{state.get('remaining_tickers', 0)}`",
                    f"**Alerts:** `{'ESPERANDO DATA' if version_matches else 'BLOQUEADO'}`",
                ])],
            ), send_discord=True)
        return {**result, "ack_status": wait_status, "attempts": attempt}

    log_info(
        "▶️ [ALERTS GENERATION START] "
        f"generation={generation} force={bool(force)} startup={bool(startup_context)} "
        f"data_version={state.get('bot_version') or 'n/a'} bootstrap={bool(state.get('bootstrap_complete'))} "
        f"core_ready={bool(state.get('core_ready'))} universe={state.get('eligible_tickers',0)}+{state.get('excluded_tickers',0)}/{state.get('universe_tickers',0)} "
        f"coverage_end={state.get('coverage_end_date') or 'n/a'} ack_before={ack_generation}:{ack_status or 'n/a'}",
        send_discord=False,
    )
    try:
        set_runtime_state("discord_coordination_wait_signature", "")
        previous_pipeline = get_pipeline_state()
        transition_pipeline_state(
            "BUILDING_CORE", stage="coordination_processing", generation=generation, entry_mode="off",
            details={"startup_context": bool(startup_context)},
        )
        acknowledge_generation(
            config.COORDINATION_DB, "alerts_bot", generation, "processing",
            message="Data LISTO; evaluación CORE/aprendizaje",
            details={"generation": generation, "startup_context": bool(startup_context)},
            attempt_count=attempt,
        )
        announce_received = ack_generation != generation or ack_status != "processing"
        result = _attempt_core_recalibration(
            context=(f"reinicio · handoff Data generation {generation}" if startup_context else f"handoff Data generation {generation}"),
            force=False,
        )
        # v6.1.24 V2: validation/learning follows actual evidence changes, but
        # a deferred generation remains lightweight so it cannot delay fresh scans.
        generation_audit, generation_learning = _run_audit_learning_after_core(
            context=f"generation {generation} · outcomes/calibration",
            core_result=result,
            send_discord=True,
        )
        if bool(result.get("executed")):
            try:
                set_runtime_state("adaptive_last_retrain_source_dates", str(int((history_coverage() or {}).get("core_feature_dates") or 0)))
                set_runtime_state("adaptive_retrain_requested", "0")
            except Exception:
                pass
        raw_coordination_result = str(result.get("status") or "")
        if announce_received:
            if raw_coordination_result == "already_processed":
                log_info(build_group(
                    "🟢 DATA → ALERTS · RECONOCIDO",
                    ["\n".join([
                        f"**Generación:** `{generation}` · **Data:** `LISTO`",
                        f"**Universo:** `{state.get('eligible_tickers', 0)} elegibles + {state.get('excluded_tickers', 0)} excluidos / {state.get('universe_tickers', 0)}`",
                        "**Resultado:** ya procesada · sin materialización ni calibración duplicadas.",
                    ])],
                ), send_discord=True)
            elif raw_coordination_result == "calibration_deferred_operational":
                log_info(build_group(
                    "🟢 DATA → ALERTS · RECONOCIDO",
                    ["\n".join([
                        f"**Generación:** `{generation}` · **Data:** `LISTO`",
                        f"**Universo:** `{state.get('eligible_tickers', 0)} elegibles + {state.get('excluded_tickers', 0)} excluidos / {state.get('universe_tickers', 0)}`",
                        f"**Operativa:** `PRIORIDAD` · aprendizaje diferido por `{result.get('defer_reason') or 'protección operativa'}`.",
                        "**Siguiente:** procesar primero runs frescos; CORE/adaptive learning se ejecutará fuera de la ventana de entradas.",
                    ])],
                ), send_discord=True)
            else:
                log_info(build_group(
                    "🟢 DATA → ALERTS · RECIBIDO",
                    ["\n".join([
                        f"**Generación:** `{generation}` · **Data:** `LISTO`",
                        f"**Universo:** `{state.get('eligible_tickers', 0)} elegibles + {state.get('excluded_tickers', 0)} excluidos / {state.get('universe_tickers', 0)}`",
                        "**Siguiente:** materializar CORE, completar outcomes y recalibrar solo si existe evidencia nueva.",
                    ])],
                ), send_discord=True)
        ack_state = _coordination_result_status(result)
        current_pipeline = get_pipeline_state()
        if ack_state == "blocked" and str(current_pipeline.get("state") or "").upper() != "ERROR":
            transition_pipeline_state(
                "ERROR", stage="coordination_result_blocked", generation=generation,
                entry_mode="off", error_code=str(result.get("status") or "GENERATION_BLOCKED").upper(),
                details={"result_status": result.get("status")},
            )
            current_pipeline = get_pipeline_state()
        if str(previous_pipeline.get("state") or "").upper() == "ERROR" and str(current_pipeline.get("state") or "").upper() in {"SHADOW_READY", "LIVE_READY"}:
            log_info(build_group(
                "✅ ALERTS · ERROR RESUELTO",
                [f"### Pipeline recuperado\n**Generación:** `{generation}` · **Estado:** {current_pipeline.get('state')}"],
            ), send_discord=True)
        acknowledge_generation(
            config.COORDINATION_DB, "alerts_bot", generation, ack_state,
            message=str(result.get("status") or ack_state), details=result, attempt_count=attempt,
        )
        icon = "✅" if ack_state == "processed" else ("⏳" if ack_state == "waiting" else "🟥")
        heading = "COMPLETADO" if ack_state == "processed" else ("ESPERANDO" if ack_state == "waiting" else "BLOQUEADO")
        raw_result = str(result.get("status") or ack_state)
        result_label = {
            "calibrated": "calibración completada",
            "calibrated_with_warnings": "calibración completada con avisos",
            "optimizer_executed_no_champion": "modelo provisional; Live continúa apagado",
            "outcomes_pending": "pendiente de outcomes",
            "manifest_missing_or_stale": "manifest no válido",
            "already_processed": "generación ya procesada; sin trabajo duplicado",
            "calibration_deferred_operational": "handoff procesado; aprendizaje diferido para priorizar runs frescos",
        }.get(raw_result, raw_result.replace("_", " "))
        action = (
            ("procesar operativa; aprendizaje queda en cola" if raw_result == "calibration_deferred_operational" else "configuración disponible")
            if ack_state == "processed"
            else ("esperar nuevas sesiones" if ack_state == "waiting" else "revisar diagnóstico CORE")
        )
        if raw_result != "already_processed":
            log_info(build_group(
                f"{icon} DATA → ALERTS · {heading}",
                ["\n".join([
                    f"**Generación:** `{generation}` · **Resultado:** {result_label}.",
                    f"**Modo:** `{str(result.get('entry_mode') or get_runtime_state('entry_mode','off')).upper()}` · **Acción:** {action}.",
                ])],
            ), send_discord=(ack_state != "waiting"))
        log_info(
            "✅ [ALERTS GENERATION END] "
            f"generation={generation} elapsed={time.time()-coordination_started:.1f}s ack={ack_state} "
            f"result={raw_result} entry={str(result.get('entry_mode') or get_runtime_state('entry_mode','off')).upper()} "
            f"executed={bool(result.get('executed'))} model={result.get('effective_model_id') or result.get('model_id') or 'n/a'} | "
            f"audit={generation_audit.get('status','n/a')} audit_executed={bool(generation_audit.get('executed'))} "
            f"learning={generation_learning.get('status','n/a')} learning_executed={bool(generation_learning.get('executed'))}",
            send_discord=False,
        )
        emit_console_snapshot(context=f"coordination generation={generation} status={ack_state}")
        return {**result, "generation": generation, "ack_status": ack_state, "attempts": attempt}
    except Exception as exc:
        acknowledge_generation(
            config.COORDINATION_DB, "alerts_bot", generation, "error",
            message=str(exc), details={"trace": traceback.format_exc()[-2500:]}, attempt_count=attempt,
        )
        raise


def _ack_startup_warmup_generation(startup: dict) -> dict:
    """ACK a LISTO generation already consumed by the synchronous startup pipeline.

    Without this ACK the daemon sees the same generation as new and starts a second
    CORE materialisation. A blocked startup is also final for that generation; it
    may be retried only after a new Data generation or in the next operating window.
    """
    state = read_data_state(config.COORDINATION_DB)
    generation = int(state.get("generation") or 0)
    if generation <= 0 or not _coordination_state_ready(state):
        return {"acknowledged": False, "generation": generation, "status": "not_ready"}
    current = read_ack(config.COORDINATION_DB, "alerts_bot")
    current_generation = int(current.get("generation") or 0)
    current_status = str(current.get("status") or "")
    startup_status = str(startup.get("status") or "").lower()
    processed = bool(
        startup.get("entries_enabled")
        or startup.get("core_optimizer_completed")
        or startup_status in {"calibrated", "bootstrap_shadow"}
    )
    ack_status = "processed" if processed else "blocked"
    if current_generation == generation and current_status == ack_status:
        return {"acknowledged": False, "generation": generation, "status": ack_status}
    attempts = int(current.get("attempt_count") or 0) + 1 if current_generation == generation else 1
    acknowledge_generation(
        config.COORDINATION_DB, "alerts_bot", generation, ack_status,
        message=f"startup_warmup:{startup_status or 'unknown'}",
        details={
            "startup_status": startup_status,
            "entry_mode": startup.get("entry_mode"),
            "core_optimizer_completed": bool(startup.get("core_optimizer_completed")),
            "heavy_pipeline_attempted": bool(startup.get("heavy_pipeline_attempted")),
            "core_reconciliation_warning": bool(startup.get("core_reconciliation_warning")),
        },
        attempt_count=attempts,
    )
    log_info(
        f"{'✅' if ack_status == 'processed' else '🟥'} DATA → ALERTS · generación {generation} · "
        f"**{ack_status.upper()}** por startup warmup",
        send_discord=False,
    )
    return {"acknowledged": True, "generation": generation, "status": ack_status}


def _strict_core_universe_status(manifest: dict, materialization: dict, *, feature_tickers: int = 0) -> dict:
    return reconcile_core_universe(
        manifest, materialization, feature_tickers=int(feature_tickers or 0)
    )


def _set_safe_core_mode(core_consistent: bool, champion_ready: bool = False) -> dict:
    operational = decide_operational_mode(
        champion_ready=bool(champion_ready),
        core_consistent=bool(core_consistent),
        manifest_ok=True,
    )
    set_runtime_state("entry_mode", str(operational["entry_mode"]))
    return operational


def _core_outcome_fingerprint(coverage: dict) -> str:
    """Scientific fingerprint of mature CORE outcomes, independent of new raw captures."""
    return "|".join([
        str(int(coverage.get("core_outcome_rows") or 0)),
        str(int(coverage.get("core_outcome_dates") or 0)),
        str(coverage.get("core_outcome_start_date") or ""),
        str(coverage.get("core_outcome_end_date") or ""),
    ])


def _core_engine_key() -> str:
    # Engine compatibility is semantic, not tied to the package version. A bot
    # upgrade must not retrain CORE merely because VERSION.txt changed.
    return str(getattr(config, "CORE_CALIBRATION_ENGINE_SIGNATURE", "core"))


def _core_engine_matches(stored: str | None, current: str | None = None) -> bool:
    stored = str(stored or "")
    current = str(current or _core_engine_key())
    return bool(stored == current or stored.endswith("|" + current))


def _attempt_core_recalibration(context: str = "CORE completion watch", force: bool = False) -> dict:
    recalibration_started = time.time()
    """Promote CORE only when all tickers in the current Scanner are complete.

    This watcher is intentionally independent from the daily deep close. The Data
    Bot may finish the resumable /api/stock/{ticker} bootstrap overnight, between
    Scanner slots; the Alerts Bot must react without waiting for another session.
    """
    manifest = _read_data_history_manifest()
    generation_ok = bool(
        str(manifest.get("bot_version") or "") == str(config.BOT_VERSION)
        and manifest.get("bootstrap_id")
    )
    manifest_complete = bool(generation_ok and manifest.get("bootstrap_complete"))
    if not generation_ok:
        operational = _set_safe_core_mode(core_consistent=False, champion_ready=False)
        return {
            "status": "manifest_missing_or_stale",
            "executed": False,
            "entry_mode": operational["entry_mode"],
        }
    if not manifest_complete:
        # A current partial manifest means Data is still bootstrapping/deep-repairing.
        # This is safe for shadow, but never for optimizer/live promotion.
        operational = _set_safe_core_mode(core_consistent=True, champion_ready=False)
        signature = "|".join([
            str(manifest.get("bootstrap_id") or ""),
            str(manifest.get("core_history_complete_tickers") or 0),
            str(manifest.get("core_history_universe_tickers") or 0),
        ])
        if get_runtime_state("last_data_bootstrap_wait_signature") != signature:
            log_info(
                "⏳ DATA WATCH · bootstrap actual en progreso | "
                f"CORE {manifest.get('core_history_complete_tickers', 0)}/"
                f"{manifest.get('core_history_universe_tickers', 0)} | "
                f"entry_mode={str(operational['entry_mode']).upper()}",
                send_discord=False,
            )
            set_runtime_state("last_data_bootstrap_wait_signature", signature)
        return {
            "status": "data_bootstrap_in_progress",
            "executed": False,
            "entry_mode": operational["entry_mode"],
        }

    # Fast idempotency check before touching CORE tables. Data publishes a
    # semantic window fingerprint; if source + outcomes are unchanged, there is
    # nothing to materialize or optimize again.
    if bool(manifest.get("core_history_universe_complete")) and not force:
        quick_coverage = history_coverage()
        quick_source = core_source_coverage()
        quick_universe = _strict_core_universe_status(
            manifest, quick_source,
            feature_tickers=int(quick_coverage.get("core_feature_tickers") or 0),
        )
        quick_signature = build_core_calibration_signature(
            universe_fingerprint=str(quick_universe.get("fingerprint") or manifest.get("core_history_universe_fingerprint") or ""),
            excluded_fingerprint=str(quick_universe.get("excluded_fingerprint") or manifest.get("core_history_excluded_fingerprint") or ""),
            window_fingerprint=str(manifest.get("core_history_window_fingerprint") or quick_source.get("window_fingerprint") or ""),
            end_date=str(quick_source.get("end_date") or manifest.get("core_history_end_date") or ""),
            feature_dates=int(quick_coverage.get("core_feature_dates") or 0),
            outcome_dates=int(quick_coverage.get("core_outcome_dates") or 0),
            outcome_rows=int(quick_coverage.get("core_outcome_rows") or 0),
        )
        if get_runtime_state("last_core_recalibration_signature") == quick_signature:
            quick_feature_date_values = [row["date"] for row in core_feature_date_inventory()]
            quick_dates = reconcile_core_dates(
                source_dates=int(quick_source.get("date_count") or 0),
                feature_dates=int(quick_coverage.get("core_feature_dates") or 0),
                target_dates=int(quick_source.get("target") or config.CORE_HISTORY_TARGET_SESSIONS),
                source_end_date=quick_source.get("end_date"),
                feature_end_date=quick_coverage.get("core_feature_end_date"),
                errors=0,
                nonfatal_limit=max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25))),
                source_date_values=list(quick_source.get("dates") or []),
                feature_date_values=quick_feature_date_values,
            )
            core_consistent = bool(quick_universe.get("complete") and quick_dates.get("complete"))
            active = get_active_model()
            champion_ready = bool(
                active.get("status") == "champion"
                and quick_universe.get("live_safe")
                and quick_dates.get("live_safe")
            )
            operational = _set_safe_core_mode(core_consistent=core_consistent, champion_ready=champion_ready)
            _set_core_learning_queue(False, context=context)
            _restore_ready_pipeline(
                operational,
                stage="core_signature_already_processed",
                generation=int(manifest.get("_coordination_generation") or 0),
                details={"context": context, "quick_signature": True},
            )
            log_info(
                "⚡ [CORE FAST SKIP] reason=signature_already_processed "
                f"context={context} source_dates={int(quick_source.get('date_count') or 0)} "
                f"feature_dates={int(quick_coverage.get('core_feature_dates') or 0)} "
                f"outcome_dates={int(quick_coverage.get('core_outcome_dates') or 0)} "
                f"model=#{int((active or {}).get('id') or 0)}",
                send_discord=False,
            )
            return {
                "status": "already_processed",
                "executed": False,
                "entry_mode": operational["entry_mode"],
                "reconciliation_warning": bool(
                    quick_universe.get("warning") or quick_dates.get("warning")
                ),
            }

    # A partial universe is a normal bootstrap state, never a calibration input.
    if not bool(manifest.get("core_history_universe_complete")):
        source = core_source_coverage()
        strict_partial = _strict_core_universe_status(manifest, source)
        partial_coherent = bool(
            strict_partial["coherent"]
            or (
                strict_partial["source_count"] == 0
                and strict_partial["manifest_count"] == 0
            )
        )
        operational = _set_safe_core_mode(core_consistent=partial_coherent, champion_ready=False)
        transition_pipeline_state(
            "WAITING_DATA",
            stage="core_universe_partial",
            generation=int(manifest.get("_coordination_generation") or 0),
            entry_mode=str(operational.get("entry_mode") or "shadow"),
            details={"context": context, "partial_universe": True},
        )
        wait_signature = "|".join([
            str(manifest.get("core_history_universe_fingerprint") or "pending"),
            str(manifest.get("core_history_complete_tickers") or 0),
            str(manifest.get("core_history_universe_tickers") or 0),
        ])
        if get_runtime_state("last_core_wait_signature") != wait_signature:
            log_info(
                "⏳ CORE WATCH · universo parcial | "
                f"eligible={manifest.get('core_history_complete_tickers', 0)} · "
                f"excluded={manifest.get('core_history_excluded_tickers', 0)} · "
                f"pending={manifest.get('core_history_pending_tickers', manifest.get('core_history_remaining_tickers', 0))}/"
                f"{manifest.get('core_history_universe_tickers', 0)} | "
                f"entry_mode={str(operational['entry_mode']).upper()}",
                send_discord=False,
            )
            set_runtime_state("last_core_wait_signature", wait_signature)
        return {
            "status": "partial_universe" if partial_coherent else "universe_mismatch",
            "executed": False,
            "entry_mode": operational["entry_mode"],
            **strict_partial,
        }

    manifest_for_state = _read_data_history_manifest()
    defer = _core_learning_defer_reason(context=context, force=force)
    if defer:
        active = get_active_model() or {}
        champion_ready = bool(str(active.get("status") or "").lower() == "champion")
        operational = _set_safe_core_mode(core_consistent=True, champion_ready=champion_ready)
        generation = int(manifest_for_state.get("_coordination_generation") or manifest.get("_coordination_generation") or 0)
        pending_ids = list(defer.get("pending_runs") or [])
        _set_core_learning_queue(
            True,
            reason=str(defer.get("reason") or "operational_priority"),
            context=context,
            generation=generation,
            pending_runs=pending_ids,
        )
        _restore_ready_pipeline(
            operational,
            stage="core_learning_deferred_operational",
            generation=generation,
            details={
                "context": context,
                "defer_reason": defer.get("reason"),
                "pending_runs": pending_ids,
                "market_time": defer.get("market_time"),
            },
        )
        _log_info_throttled(
            f"core_learning_deferred:{defer.get('reason')}:{generation}",
            "⏸️ [CORE LEARNING DEFERRED] "
            f"reason={defer.get('reason')} context={context} priority=fresh_scans "
            f"pending_valid={len(pending_ids)} ids={pending_ids or 'none'} "
            f"market_time={defer.get('market_time') or 'outside_entry_window'} "
            f"entry={str(operational.get('entry_mode') or 'shadow').upper()} force_requested={bool(force)}",
        )
        _log_info_throttled(
            f"learning_queue:{defer.get('reason')}:{generation}",
            "📚 [LEARNING QUEUE] pending=yes "
            f"reason={defer.get('reason')} generation={generation} "
            f"pending_runs={pending_ids or 'none'} execute_when=entry_window_closed_and_backlog_zero",
        )
        return {
            "status": "calibration_deferred_operational",
            "executed": False,
            "deferred": True,
            "defer_reason": defer.get("reason"),
            "pending_run_ids": pending_ids,
            "entry_mode": operational["entry_mode"],
            "generation": generation,
        }

    materialization_started = time.time()
    transition_pipeline_state(
        "BUILDING_CORE", stage="core_materialization",
        generation=int(manifest_for_state.get("_coordination_generation") or 0), entry_mode="off",
        details={"context": context},
    )
    log_info("🔄 CORE · verificando cache/materialización…", send_discord=False)
    def _core_progress(stage, current, total, detail):
        if stage == "relationships":
            log_info(f"   ↳ CORE {current}/{total} fechas | {detail}", send_discord=False)
        elif stage in {"loaded", "calendar", "saving"}:
            log_info(f"   ↳ CORE · {detail}", send_discord=False)
    core_materialization = (
        materialize_core_history_if_needed(progress_callback=_core_progress)
        if config.CORE_HISTORY_ENABLED else core_source_coverage()
    )
    materialization_elapsed = time.time() - materialization_started
    if core_materialization.get("cached"):
        log_info("⚡ CORE · cache coherente reutilizada; sin reconstrucción", send_discord=False)
    core_dates = int(core_materialization.get("date_count") or 0)
    core_target = int(core_materialization.get("target") or config.CORE_HISTORY_TARGET_SESSIONS)
    core_errors = int(core_materialization.get("errors") or 0)
    materialized = history_coverage()
    feature_dates_now = int(materialized.get("core_feature_dates") or 0)
    feature_tickers_now = int(materialized.get("core_feature_tickers") or 0)
    strict = _strict_core_universe_status(
        manifest, core_materialization, feature_tickers=feature_tickers_now
    )
    nonfatal_limit = max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25)))
    manifest_universe = int(manifest.get("core_history_universe_tickers") or 0)
    manifest_complete_tickers = int(manifest.get("core_history_complete_tickers") or 0)
    manifest_excluded_tickers = int(manifest.get("core_history_excluded_tickers") or 0)
    manifest_authoritative_reconciliation = bool(strict.get("authoritative_reconciliation"))
    feature_date_inventory_now = core_feature_date_inventory()
    feature_date_values_now = [row["date"] for row in feature_date_inventory_now]
    source_date_values_now = list(core_materialization.get("dates") or [])
    date_reconciliation = reconcile_core_dates(
        source_dates=len(source_date_values_now) or core_dates,
        feature_dates=len(feature_date_values_now) or feature_dates_now,
        target_dates=core_target,
        source_end_date=(source_date_values_now[-1] if source_date_values_now else core_materialization.get("end_date")),
        feature_end_date=(feature_date_values_now[-1] if feature_date_values_now else materialized.get("core_feature_end_date")),
        errors=core_errors,
        nonfatal_limit=nonfatal_limit,
        source_date_values=source_date_values_now,
        feature_date_values=feature_date_values_now,
    )
    strict_usable = bool(strict["complete"])
    coverage_usable = bool(
        date_reconciliation["complete"]
        and feature_tickers_now >= manifest_complete_tickers
    )
    if not strict_usable or not coverage_usable or core_errors > nonfatal_limit:
        operational = _set_safe_core_mode(core_consistent=False, champion_ready=False)
        mismatch = {
            "status": "materialization_incomplete" if strict_usable else "universe_mismatch",
            "executed": False,
            "entry_mode": operational["entry_mode"],
            "materialization_errors": core_errors,
            "materialization_error_samples": core_materialization.get("error_samples") or [],
            "nonmaterializable_tickers": int(core_materialization.get("nonmaterializable_tickers") or 0),
            "nonmaterializable_ticker_sample": core_materialization.get("nonmaterializable_ticker_sample") or [],
            "feature_dates_now": feature_dates_now,
            "feature_tickers_now": feature_tickers_now,
            "nonfatal_limit": nonfatal_limit,
            "manifest_authoritative_reconciliation": manifest_authoritative_reconciliation,
            "date_reconciliation": date_reconciliation,
            **strict,
        }
        transition_pipeline_state(
            "ERROR", stage="core_reconciliation_blocked",
            generation=int(manifest.get("_coordination_generation") or 0), entry_mode="off",
            error_code=str(mismatch.get("status") or "CORE_RECONCILIATION_BLOCKED").upper(),
            details={
                "core_errors": core_errors,
                "feature_tickers": feature_tickers_now,
                "feature_dates": feature_dates_now,
                "source_tickers": strict.get("complete_tickers"),
                "source_count": strict.get("source_count"),
                "date_reconciliation": date_reconciliation,
            },
        )
        missing_feature_sample = list(core_materialization.get("nonmaterializable_ticker_sample") or [])[:12]
        diagnostic_sections = [
            "\n".join([
                "### Reconciliación no válida",
                f"**Universo conciliado:** `{strict.get('complete')}` · **Errores:** `{core_errors}/{nonfatal_limit}`",
                f"**Features:** `{feature_tickers_now}/{manifest_complete_tickers}` tickers · `{feature_dates_now}` sesiones",
                f"**Fuente:** `{strict.get('complete_tickers')}` elegibles + `{manifest_excluded_tickers}` excluidos / `{strict.get('source_count')}`",
            ]),
            "\n".join([
                "### Diferencias",
                f"**Faltan features:** `{strict.get('feature_ticker_shortfall', 0)}` · **No materializables:** `{int(core_materialization.get('nonmaterializable_tickers') or 0)}`",
                f"**Drift de fechas:** `{date_reconciliation.get('drift')}` · **Fecha final presente:** `{date_reconciliation.get('end_match')}`",
                f"**Fechas requeridas ausentes:** `{len(date_reconciliation.get('missing_required_dates') or [])}`",
                f"**Excluidos efectivos:** `{strict.get('excluded_tickers')}` · **Pendientes:** `{strict.get('pending_tickers')}`",
            ]),
        ]
        if missing_feature_sample:
            diagnostic_sections.append(
                "### Muestra sin materializar\n" + " · ".join(f"`{ticker}`" for ticker in missing_feature_sample)
            )
        log_info(build_group(
            "🟥 CORE · BLOQUEADO",
            diagnostic_sections,
            footer="Entradas bloqueadas hasta reparar CORE. El detalle completo queda en el log local.",
        ), send_discord=True)
        return mismatch

    pruned_core_rows = int(core_materialization.get("pruned_observations") or 0)
    pruned_core_outcomes = int(core_materialization.get("pruned_outcomes") or 0)
    if pruned_core_rows or pruned_core_outcomes:
        log_info(
            "🧹 CORE · calendario local reconciliado | "
            f"observaciones antiguas={pruned_core_rows} · outcomes antiguos={pruned_core_outcomes} · "
            f"calendario={len(source_date_values_now)} sesiones",
            send_discord=False,
        )

    materialization_warning = bool(
        core_errors > 0 or strict.get("warning") or date_reconciliation.get("warning")
    )
    if manifest_authoritative_reconciliation:
        log_info(
            "⚠️ CORE reconciliado por conteos Data autoritativos | "
            f"eligible={manifest_complete_tickers} · excluded={manifest_excluded_tickers} · universe={manifest_universe} · "
            "se mantiene SHADOW hasta reconciliar huellas locales",
            send_discord=False,
        )
    if materialization_warning:
        log_info(
            "⚠️ CORE materializado con incidencias no fatales | "
            f"errores={core_errors}/{nonfatal_limit} · cobertura={feature_tickers_now} tickers/{feature_dates_now}d · "
            f"drift_fechas={date_reconciliation.get('drift')} · "
            "se mantiene SHADOW y se conserva la cache para evitar reconstrucciones repetidas",
            send_discord=False,
        )

    log_info("🔄 CORE · calculando outcomes de 1 a 5 sesiones…", send_discord=False)
    outcomes_started = time.time()
    outcome_materialization = materialize_outcomes() or {}
    outcomes_elapsed = time.time() - outcomes_started
    coverage = history_coverage()
    core_feature_dates = int(coverage.get("core_feature_dates") or 0)
    core_outcomes = int(coverage.get("core_outcome_dates") or 0)
    minimum_outcomes = int(config.CORE_HISTORY_MIN_FULLY_LABELED_DATES)
    eligible = bool(
        core_dates >= core_target
        and core_feature_dates >= core_target
        and core_outcomes >= minimum_outcomes
    )
    signature = build_core_calibration_signature(
        universe_fingerprint=str(strict.get("fingerprint") or ""),
        excluded_fingerprint=str(strict.get("excluded_fingerprint") or ""),
        window_fingerprint=str(manifest.get("core_history_window_fingerprint") or core_materialization.get("window_fingerprint") or ""),
        end_date=str(core_materialization.get("end_date") or manifest.get("core_history_end_date") or ""),
        feature_dates=core_feature_dates,
        outcome_dates=core_outcomes,
        outcome_rows=int(coverage.get("core_outcome_rows") or 0),
    )
    if not force and get_runtime_state("last_core_recalibration_signature") == signature:
        active = get_active_model()
        champion_ready = bool(active.get("status") == "champion" and strict["complete"])
        operational = _set_safe_core_mode(core_consistent=True, champion_ready=champion_ready)
        _set_core_learning_queue(False, context=context)
        _restore_ready_pipeline(
            operational,
            stage="core_signature_already_processed",
            generation=int(manifest.get("_coordination_generation") or 0),
            details={"context": context, "materialization_elapsed": round(materialization_elapsed, 3), "outcomes_elapsed": round(outcomes_elapsed, 3)},
        )
        log_info(
            "⏭️ [CORE CALIBRATION SKIP] reason=signature_already_processed "
            f"context={context} source_dates={core_dates} feature_dates={core_feature_dates} "
            f"outcome_dates={core_outcomes} model=#{int((active or {}).get('id') or 0)}",
            send_discord=False,
        )
        return {
            "status": "already_processed",
            "executed": False,
            "entry_mode": operational["entry_mode"],
            "materialization_elapsed": materialization_elapsed,
            "outcomes_elapsed": outcomes_elapsed,
            **strict,
        }

    if not eligible:
        operational = _set_safe_core_mode(core_consistent=True, champion_ready=False)
        log_info(
            "⏭️ [CORE CALIBRATION SKIP] reason=outcomes_pending "
            f"context={context} source_dates={core_dates}/{core_target} "
            f"feature_dates={core_feature_dates}/{core_target} outcome_dates={core_outcomes}/{minimum_outcomes}",
            send_discord=False,
        )
        _set_core_learning_queue(False, context=context)
        _restore_ready_pipeline(
            operational,
            stage="core_outcomes_pending",
            generation=int(manifest.get("_coordination_generation") or 0),
            details={
                "core_dates": core_feature_dates,
                "core_outcomes": core_outcomes,
                "materialization_elapsed": round(materialization_elapsed, 3),
                "outcomes_elapsed": round(outcomes_elapsed, 3),
            },
        )
        return {
            "status": "outcomes_pending",
            "executed": False,
            "entry_mode": operational["entry_mode"],
            "core_dates": core_feature_dates,
            "core_outcomes": core_outcomes,
            "materialization_elapsed": materialization_elapsed,
            "outcomes_elapsed": outcomes_elapsed,
            **strict,
        }

    outcome_fingerprint = _core_outcome_fingerprint(coverage)
    engine_key = _core_engine_key()
    if (
        not force
        and _core_engine_matches(get_runtime_state("last_core_calibration_engine", ""), engine_key)
        and str(get_runtime_state("last_core_calibrated_outcomes", "") or "") == outcome_fingerprint
    ):
        active = get_active_model()
        champion_ready = bool(active.get("status") == "champion" and strict["complete"] and not materialization_warning)
        operational = _set_safe_core_mode(core_consistent=True, champion_ready=champion_ready)
        _set_core_learning_queue(False, context=context)
        _restore_ready_pipeline(
            operational,
            stage="core_no_new_mature_outcomes",
            generation=int(manifest.get("_coordination_generation") or 0),
            details={
                "context": context,
                "outcome_fingerprint": outcome_fingerprint,
                "materialization_elapsed": round(materialization_elapsed, 3),
                "outcomes_elapsed": round(outcomes_elapsed, 3),
            },
        )
        log_info(
            "⏭️ [CORE CALIBRATION SKIP] reason=no_new_mature_outcomes "
            f"context={context} engine={engine_key} outcome_fingerprint={outcome_fingerprint} "
            f"outcome_dates={core_outcomes} outcome_rows={int(coverage.get('core_outcome_rows') or 0)} "
            f"model=#{int((active or {}).get('id') or 0)}",
            send_discord=False,
        )
        return {
            "status": "already_processed",
            "executed": False,
            "entry_mode": operational["entry_mode"],
            "skip_reason": "no_new_mature_outcomes",
            "core_outcome_fingerprint": outcome_fingerprint,
            "materialization_elapsed": materialization_elapsed,
            "outcomes_elapsed": outcomes_elapsed,
            **strict,
        }

    transition_pipeline_state(
        "CALIBRATING", stage="core_walkforward", generation=int(manifest.get("_coordination_generation") or 0), entry_mode="off",
        details={"context": context},
    )
    base_model = get_active_model()
    base_model_label = (
        f"#{int(base_model.get('id') or 0)} · {base_model.get('model_key') or 'sin-clave'}"
        if base_model else "sin modelo base"
    )
    log_info(
        "▶️ [CORE CALIBRATION START] "
        f"context={context} force={bool(force)} source_dates={core_dates} feature_dates={core_feature_dates} "
        f"outcome_dates={core_outcomes} min_outcomes={minimum_outcomes} "
        f"universe={strict.get('complete_tickers',0)}/{strict.get('source_count',0)} base_model={base_model_label}",
        send_discord=False,
    )
    send_daily(build_group(
        "🧪 CALIBRACIÓN · INICIADA",
        ["\n".join([
            f"**TMAD:** `v{config.BOT_VERSION}` · **Modelo base:** `{base_model_label}`",
            "**Capa:** `CORE` · selección purgada y orden temporal.",
            "**Estado:** walk-forward en curso; el resultado final se publicará en este canal.",
        ])],
    ))
    optimizer_started = time.time()
    model_id = run_optimizer(calibration_layer="core")
    optimizer_elapsed = time.time() - optimizer_started
    activate_due_model()
    active = get_active_model()
    champion_ready = bool(active.get("status") == "champion" and strict["complete"] and not materialization_warning)
    operational = _set_safe_core_mode(core_consistent=True, champion_ready=champion_ready)
    transition_pipeline_state(
        readiness_state(str(operational["entry_mode"]), safe=True),
        stage="core_calibration_complete", generation=int(manifest.get("_coordination_generation") or 0),
        entry_mode=str(operational["entry_mode"]), model_id=active.get("id"),
        details={"materialization_warning": materialization_warning, "context": context},
    )
    set_runtime_state("last_core_recalibration_signature", signature)
    set_runtime_state("last_core_calibration_engine", engine_key)
    set_runtime_state("last_core_calibrated_outcomes", outcome_fingerprint)
    set_runtime_state("last_core_wait_signature", "")
    _set_core_learning_queue(False, context=context)
    effective_model_id = int(active.get("id") or 0) if active else 0
    challenger_model_id = int(model_id or 0)
    promoted = bool(challenger_model_id and challenger_model_id == effective_model_id)
    decision_text = (
        f"promovido como efectivo `#{effective_model_id}`"
        if promoted else
        f"challenger `#{challenger_model_id or 'n/a'}` conservado sin activar · efectivo `#{effective_model_id or 'n/a'}`"
    )
    log_info(build_group(
        "✅ CORE · RECALIBRADO",
        ["\n".join([
            f"**Datos:** `{strict['complete_tickers']}/{strict['source_count']}` tickers · **Histórico:** `{core_feature_dates}` sesiones · outcomes `{core_outcomes}`.",
            f"**Decisión:** {decision_text}.",
            f"**Modo:** `{str(operational['entry_mode']).upper()}`.",
        ])],
    ), send_discord=True)
    # During a restart the dedicated recovery block below sends one coherent
    # config + setup snapshot. Other recalibrations refresh both immediately.
    if "reinicio" not in str(context or "").lower():
        send_rules_config(context=context)
        try:
            from setup_reports import current_operating_phase
            send_setup_phase(current_operating_phase(), force=True)
        except Exception as exc:
            log_error(f"🟥 Error refrescando setups tras calibración: {exc}")
    total_elapsed = time.time() - recalibration_started
    log_info(
        "⏱️ [CORE CALIBRATION PERFORMANCE] "
        f"total={total_elapsed:.1f}s materialize={materialization_elapsed:.1f}s "
        f"outcomes={outcomes_elapsed:.1f}s optimizer={optimizer_elapsed:.1f}s "
        f"source_dates={core_dates} feature_dates={core_feature_dates} outcome_dates={core_outcomes} "
        f"outcome_rows_new={_outcome_rows_new(outcome_materialization)}",
        send_discord=False,
    )
    log_info(
        "✅ [CORE CALIBRATION END] "
        f"elapsed={total_elapsed:.1f}s materialize={materialization_elapsed:.1f}s "
        f"outcomes_phase={outcomes_elapsed:.1f}s optimizer={optimizer_elapsed:.1f}s context={context} "
        f"challenger={challenger_model_id or 'n/a'} effective={effective_model_id or 'n/a'} promoted={promoted} "
        f"entry={str(operational['entry_mode']).upper()} feature_dates={core_feature_dates} outcomes={core_outcomes} "
        f"materialization_errors={core_errors} warning={materialization_warning}",
        send_discord=False,
    )
    return {
        "status": ("calibrated_with_warnings" if materialization_warning else ("calibrated" if champion_ready else "optimizer_executed_no_champion")),
        "executed": True,
        "materialization_errors": core_errors,
        "materialization_warning": materialization_warning,
        "manifest_authoritative_reconciliation": manifest_authoritative_reconciliation,
        "model_id": model_id,
        "effective_model_id": int(active.get("id") or 0) if active else 0,
        "entry_mode": operational["entry_mode"],
        "materialization_elapsed": materialization_elapsed,
        "outcomes_elapsed": outcomes_elapsed,
        "optimizer_elapsed": optimizer_elapsed,
        "total_elapsed": total_elapsed,
        **strict,
    }


def _run_training_cycle(context: str, *, force_core: bool = False) -> dict:
    """Train CORE without ever taking priority over fresh operational scans."""
    result = _attempt_core_recalibration(context=context, force=force_core)
    if result.get("executed"):
        _run_audit_learning_after_core(
            context=f"{context} · training completed",
            core_result=result,
            send_discord=True,
        )
    if result.get("status") == "outcomes_pending":
        log_info(
            "⏳ TRAIN CORE pendiente | "
            f"fechas={result.get('core_dates', 0)} · outcomes={result.get('core_outcomes', 0)}",
            send_discord=False,
        )
    return result


def _wait_for_daily_close_handoff(trade_date: str) -> tuple[bool, dict]:
    """Wait until Data Bot has archived the intraday layer and published the deep close."""
    deadline = time.time() + max(0, int(config.DAILY_CLOSE_HANDOFF_TIMEOUT_SECONDS))
    last: dict = {}
    while True:
        last = _read_data_history_manifest()
        ready = bool(
            last.get("bootstrap_complete")
            and str(last.get("bot_version") or "") == str(config.BOT_VERSION)
            and str(last.get("coverage_end_date") or "") >= trade_date
        )
        if ready:
            return True, last
        if time.time() >= deadline:
            return False, last
        time.sleep(2)


def _daily_recalibration(close_run: dict, context: str = "nuevo cierre diario") -> None:
    run_id = int(close_run.get("id") or 0)
    trade_date = str(close_run.get("snapshot_date") or "")
    if not run_id or not trade_date:
        return
    state_key = "last_recalibrated_daily_close_run"
    if get_runtime_state(state_key) == str(run_id):
        return

    handoff_ok, manifest = _wait_for_daily_close_handoff(trade_date)
    if not handoff_ok:
        log_error(
            "🟥 DAILY CLOSE HANDOFF TIMEOUT\n"
            f"Deep run `{run_id}` date `{trade_date}` detectado, pero Data Bot no publicó el manifest final.\n"
            "Acción: no se recalibra ni se purga nada; se reintentará en el siguiente ciclo.\n"
            f"Manifest encontrado: version `{manifest.get('bot_version')}` · end `{manifest.get('coverage_end_date')}`"
        )
        return

    # The deep run is authoritative for the FULL daily layer. Materialise it in
    # historical mode before comparing Data vs Alerts; this cannot create entries,
    # update positions or publish an open-book overview.
    close_materialized = run_alert_scan(
        scanner_run_id=run_id,
        historical=True,
        capture_prospective_architecture=True,
    )

    # v6.1.107: global layer maintenance is deliberately outside run_alert_scan.
    # This keeps fresh Scanner consumption fast while preserving the same daily
    # outcome/archive/calibration/audit/learning work once the close is authoritative.
    layer_maintenance_started = time.time()
    layer_outcomes = materialize_layer_outcomes()
    prospective_architecture = {"status": "not_run", "matured_now": 0}
    try:
        from prospective_architecture_journal import mature_prospective_architecture_journal
        prospective_architecture = mature_prospective_architecture_journal()
    except Exception as architecture_exc:
        prospective_architecture = {
            "status": "error",
            "error": str(architecture_exc),
            "matured_now": 0,
            "live_mutation": False,
        }
        log_info(
            f"⚠️ [ARCHITECTURE JOURNAL] maturation skipped: {architecture_exc}",
            send_discord=False,
        )
    layer_archives = archive_completed_layer_dates()
    layer_calibrations = refresh_layer_calibrations()
    layer_audit = maybe_run_market_outcome_audit(
        context=f"{context} · layer maintenance",
        send_discord=True,
        console_if_unchanged=True,
    )
    layer_learning = maybe_run_adaptive_learning(
        context=f"{context} · layer maintenance",
        audit_payload=layer_audit if layer_audit.get("metrics") else None,
        send_discord=True,
        console_if_unchanged=True,
    )
    log_info(
        "🧹 [DAILY LAYER MAINTENANCE] "
        f"date={trade_date} outcomes={layer_outcomes} "
        f"architecture_matured={int(prospective_architecture.get('matured_now') or 0)} "
        f"archives={len(layer_archives)} "
        f"calibrations={len(layer_calibrations)} audit={layer_audit.get('status')} "
        f"learning={layer_learning.get('status')} elapsed={time.time()-layer_maintenance_started:.1f}s",
        send_discord=False,
    )

    intraday_status = bootstrap_intraday_archives_if_ready()
    purged = purge_intraday_observations_through(trade_date)
    log_info(
        "DAILY CLOSE · HANDOFF ✅\n"
        f"Fecha `{trade_date}` · deep run `{run_id}`\n"
        f"Data coverage `{manifest.get('paired_trading_dates', 0)}/{manifest.get('target_daily_closes', 30)}` · "
        f"FULL materializado `{close_materialized}` filas nuevas\n"
        f"Intradía ZIP `{intraday_status.get('available_sessions', 0)}/{intraday_status.get('required_sessions', 30)}` · "
        f"observaciones live purgadas `{purged.get('observations', 0)}`",
        send_discord=False,
    )

    core_materialization = (
        materialize_core_history_if_needed(progress_callback=lambda stage, current, total, detail: (
            log_info(f"   ↳ CORE {current}/{total} fechas | {detail}", send_discord=False)
            if stage == "relationships" else None
        )) if config.CORE_HISTORY_ENABLED else {}
    )
    materialize_outcomes()
    if config.RELATIONSHIP_REFRESH_AFTER_TRAIN:
        refresh_relationship_graph(force=True, effective_date=trade_date)

    coverage = history_coverage()
    daily_closes = int(coverage.get("feature_dates") or 0)
    target = int(config.DAILY_CLOSE_WINDOW_SESSIONS)
    data_closes = int(manifest.get("paired_trading_dates") or 0)
    core_dates = int(coverage.get("core_feature_dates") or 0)
    core_target = int(config.CORE_HISTORY_TARGET_SESSIONS)
    core_outcomes = int(coverage.get("core_outcome_dates") or 0)
    full_outcomes = int(coverage.get("outcome_dates") or 0)
    cutoff = (now_madrid().date() - timedelta(days=config.FEATURE_RETENTION_DAYS)).isoformat()
    reconciliation = canonical_materialization_reconciliation(cutoff, config.STARTUP_MAX_HISTORICAL_RUNS)
    full_consistent = data_closes == daily_closes and int(reconciliation.get("missing_rows") or 0) == 0
    core_materialized_dates = int(core_materialization.get("date_count") or 0)
    core_universe = _strict_core_universe_status(
        manifest, core_materialization,
        feature_tickers=int(coverage.get("core_feature_tickers") or 0),
    )
    daily_nonfatal_limit = max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25)))
    daily_source_date_values = list(core_materialization.get("dates") or [])
    daily_feature_date_values = [row["date"] for row in core_feature_date_inventory()]
    core_date_reconciliation = reconcile_core_dates(
        source_dates=len(daily_source_date_values) or core_materialized_dates,
        feature_dates=len(daily_feature_date_values) or core_dates,
        target_dates=core_target,
        source_end_date=(daily_source_date_values[-1] if daily_source_date_values else core_materialization.get("end_date")),
        feature_end_date=(daily_feature_date_values[-1] if daily_feature_date_values else coverage.get("core_feature_end_date")),
        errors=int(core_materialization.get("errors") or 0),
        nonfatal_limit=daily_nonfatal_limit,
        source_date_values=daily_source_date_values,
        feature_date_values=daily_feature_date_values,
    )
    core_layer_consistent = bool(core_date_reconciliation["complete"])
    core_pipeline_consistent = bool(core_layer_consistent and core_universe["complete"])
    core_warning = bool(core_date_reconciliation.get("warning") or core_universe.get("warning"))
    core_eligible = bool(
        core_pipeline_consistent
        and core_dates >= core_target
        and core_outcomes >= int(config.CORE_HISTORY_MIN_FULLY_LABELED_DATES)
    )
    full_eligible = bool(
        full_consistent
        and daily_closes >= target
        and full_outcomes >= int(config.WALKFORWARD_MIN_FULLY_LABELED_DATES)
    )

    calibration_parts: list[str] = []
    if core_eligible:
        core_result = _attempt_core_recalibration(context=f"{context} · CORE", force=False)
        if core_result.get("executed"):
            calibration_parts.append(f"CORE model_id `{core_result.get('model_id') or 'sin promoción'}`")
        else:
            calibration_parts.append("CORE sin outcomes maduros nuevos · no recalibrado")
    else:
        calibration_parts.append(
            f"CORE pendiente `{core_dates}/{core_target}` · outcomes `{core_outcomes}/{config.CORE_HISTORY_MIN_FULLY_LABELED_DATES}`"
        )
    if full_eligible:
        full_outcome_fingerprint = "|".join([
            str(int(coverage.get("outcome_rows") or 0)),
            str(int(coverage.get("outcome_dates") or 0)),
            str(coverage.get("outcome_start_date") or ""),
            str(coverage.get("outcome_end_date") or ""),
        ])
        full_engine_key = f"{config.BOT_VERSION}|FULL"
        if (
            str(get_runtime_state("last_full_calibration_engine", "") or "") == full_engine_key
            and str(get_runtime_state("last_full_calibrated_outcomes", "") or "") == full_outcome_fingerprint
        ):
            calibration_parts.append("FULL sin outcomes maduros nuevos · no recalibrado")
        else:
            full_model_id = run_optimizer(calibration_layer="full")
            activate_due_model()
            set_runtime_state("last_full_calibration_engine", full_engine_key)
            set_runtime_state("last_full_calibrated_outcomes", full_outcome_fingerprint)
            calibration_parts.append(f"FULL model_id `{full_model_id or 'sin promoción'}`")
    else:
        calibration_parts.append(
            f"FULL pendiente `{daily_closes}/{target}` · outcomes `{full_outcomes}/{config.WALKFORWARD_MIN_FULLY_LABELED_DATES}`"
        )
    calibration_result = " | ".join(calibration_parts)

    active = get_active_model()
    champion_ready = bool(
        active.get("status") == "champion"
        and core_universe["complete"]
        and not core_warning
    )
    operational = decide_operational_mode(
        champion_ready=champion_ready,
        core_consistent=core_pipeline_consistent,
        manifest_ok=True,
    )
    shadow_ready = bool(operational["shadow_ready"])
    entry_mode = str(operational["entry_mode"])
    set_runtime_state("entry_mode", entry_mode)

    purge_research_retention()
    if champion_ready:
        send_rules_config(context=context)
    if config.SEND_BACKTEST_RESULTS_AFTER_TRAIN:
        build_performance_report(f"daily_backtest_{trade_date}")
    send_daily_operating_summary()
    send_daily(build_group(
        "🧪 CALIBRACIÓN · CIERRE DIARIO",
        ["\n".join([
            f"**CORE:** `{core_dates}/{core_target}` sesiones · universo `{core_universe['complete_tickers']}/{core_universe['source_count']}` · outcomes `{core_outcomes}`",
            f"**FULL:** Data `{data_closes}` · Alerts `{daily_closes}` · pendientes `{reconciliation.get('missing_rows', 0)}`",
            f"**Resultado:** {calibration_result}",
            f"**Modo siguiente:** `{entry_mode.upper()}` · {'Live validado' if champion_ready else ('Research activo; Live apagado' if shadow_ready else 'pipeline bloqueado')}",
        ])],
        footer="Los detalles técnicos y la reconciliación por filas quedan en los diagnósticos locales.",
    ))
    set_runtime_state(state_key, str(run_id))


def run_daemon():
    # The deployment lock is a liveness signal, not a readiness signal. Acquire it
    # before schema checks, migrations or storage maintenance, which can take more
    # than the deploy timeout on the persistent Alerts database.
    acquire_lock()
    ensure_alerts_schema()
    ensure_layer_history_schema()
    ensure_market_outcome_audit_schema()
    ensure_adaptive_learning_schema()
    maintenance_retry_pending = False
    maintenance_retry_reason = ""
    try:
        from maintenance_v620 import main as run_maintenance_v620
        maintenance_result = run_maintenance_v620() or {}
        maintenance_retry_pending = bool(maintenance_result.get("pending"))
        maintenance_retry_reason = str(maintenance_result.get("warning") or "")
        if maintenance_retry_pending:
            log_warning(
                "Mantenimiento SQLite pospuesto: "
                f"{maintenance_retry_reason}. Pipeline operativo no afectado; "
                "se reintentará con cooldown fuera de calibración."
            )
    except Exception as exc:
        maintenance_retry_pending = True
        maintenance_retry_reason = str(exc)
        log_warning(f"Mantenimiento SQLite pospuesto: {exc}. Pipeline operativo no afectado.")
    # Storage maintenance also runs on weekend/night restarts: the last closed
    # session moves to ZIP and matured outcomes are merged before startup recovery.
    try:
        materialize_layer_outcomes()
        try:
            from prospective_architecture_journal import mature_prospective_architecture_journal
            mature_prospective_architecture_journal()
        except Exception as architecture_exc:
            log_info(
                f"⚠️ [ARCHITECTURE JOURNAL] startup maturation skipped: {architecture_exc}",
                send_discord=False,
            )
        archive_completed_layer_dates()
        refresh_layer_calibrations()
        startup_audit = maybe_run_market_outcome_audit(
            context="startup recovery", send_discord=True, console_if_unchanged=True
        )
        maybe_run_adaptive_learning(
            context="startup recovery",
            audit_payload=startup_audit if startup_audit.get("metrics") else None,
            send_discord=True, console_if_unchanged=True,
        )
    except Exception as exc:
        log_error(f"🟥 Mantenimiento ZIP/auditor v6 al arrancar: {exc}")
    already_run: set[str] = set()
    last_heartbeat = 0.0
    last_discord_heartbeat = 0.0
    last_coordination_poll = 0.0
    last_coordination_heartbeat = 0.0
    last_coordination_retry = 0.0
    next_maintenance_retry_at = time.time() + 900
    last_maintenance_notice_at = 0.0
    local_heartbeat_degraded = False
    last_peer_warning = False
    auto_seen_runs: set[int] = set()
    last_deferred_run_id = 0
    startup_cycle_started = time.time()
    peer_heartbeat_grace_until = startup_cycle_started + max(120, int(config.COORDINATION_STALE_SECONDS))
    startup_recovery_sent = False
    startup_wait_notice_sent = False
    last_operational_state = None
    heartbeat_stop = threading.Event()

    def _coordination_heartbeat_worker() -> None:
        interval = max(5, min(30, int(config.COORDINATION_HEARTBEAT_SECONDS)))
        while not heartbeat_stop.is_set():
            try:
                data_state = read_data_state(config.COORDINATION_DB)
                write_heartbeat(
                    config.COORDINATION_DB, "alerts_bot", "running",
                    generation_seen=int(data_state.get("generation") or 0),
                    details={
                        "entry_mode": get_runtime_state("entry_mode") or "unknown",
                        "phase": get_runtime_state("alerts_phase") or "startup",
                    },
                )
            except Exception:
                pass
            heartbeat_stop.wait(interval)

    heartbeat_thread = threading.Thread(
        target=_coordination_heartbeat_worker,
        name="tmad-alerts-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    def _send_startup_recovery_once(reason: str) -> bool:
        nonlocal startup_recovery_sent
        if startup_recovery_sent:
            return False
        pipeline = get_pipeline_state()
        state = str(pipeline.get("state") or "ERROR").upper()
        if state == "REST":
            state = str((pipeline.get("details") or {}).get("resume_state") or "ERROR").upper()
        if state == "ERROR":
            # The startup cycle is complete, but there is no coherent snapshot to
            # publish. Mark it consumed so off-hours mode cannot loop or leak
            # candidates from stale local data.
            startup_recovery_sent = True
            set_runtime_state("last_startup_recovery_at", now_madrid().isoformat())
            log_info(
                "⏭️ RECUPERACIÓN no publicada · pipeline ERROR; Research y Live permanecen bloqueados",
                send_discord=False,
            )
            return False
        if state not in {"SHADOW_READY", "LIVE_READY"}:
            return False
        try:
            send_rules_config(context=f"arranque · {reason}")
            recovery_message = send_recovery_snapshot()
            positions_message = maybe_send_open_positions_overview(force=True)
            startup_recovery_sent = True
            set_runtime_state("last_startup_recovery_at", now_madrid().isoformat())
            log_info(
                "📨 RECUPERACIÓN DE ARRANQUE completada · configuración enviada a Calibration · "
                + ("watchlist enviada a Research" if recovery_message else "sin candidatos publicables")
                + (" · overview enviado a Alerts Status" if positions_message else " · overview no generado"),
                send_discord=False,
            )
            return True
        except Exception as exc:
            log_error(f"🟥 Error enviando estado de recuperación: {exc}")
            return False

    log_info(f"🚀 TMAD Alerts Adaptive Bot v{config.BOT_VERSION} iniciado", send_discord=False)
    try:
        activate_due_model()
        startup = run_startup_warmup()
        try:
            retry_pending_exit_deliveries()
        except Exception as exc:
            log_info(f'⚠️ Lifecycle CLOSE retry de arranque pospuesto: {exc}', send_discord=False)
        # Coordination exclusively owns a deferred startup. When the synchronous
        # warmup already consumed a LISTO generation, ACK it and never launch an
        # immediate second materialisation, even if the result was blocked.
        startup_deferred = bool(startup.get("deferred_to_coordination"))
        startup_heavy_attempted = bool(startup.get("heavy_pipeline_attempted"))
        startup_training_pending = bool(startup_deferred)
        last_core_watch = time.time() if startup_heavy_attempted else 0.0
        if startup_heavy_attempted:
            _ack_startup_warmup_generation(startup)
            startup_training_pending = False
            if str(startup.get("status") or "").lower() == "error":
                log_info(
                    "🟥 Startup CORE bloqueado; la generación queda cerrada y no se reconstruirá en bucle. "
                    "Se reintentará con una nueva generación Data o en la siguiente ventana operativa.",
                    send_discord=False,
                )
        elif startup_deferred:
            log_info(
                "🟡 Arranque diferido completado | esperando Data LISTO sin ejecutar trabajo CORE duplicado",
                send_discord=False,
            )
        elif not bool(startup.get("core_optimizer_completed")):
            # Warmup disabled/short-circuited without consuming a Data generation.
            # One explicit restart train remains valid in this exceptional path.
            try:
                restart_train = _run_training_cycle("entrenamiento por reinicio", force_core=False)
                startup_training_pending = not bool(
                    restart_train.get("executed")
                    or restart_train.get("status") in {
                        "already_processed", "calibrated", "calibrated_with_warnings",
                        "optimizer_executed_no_champion", "outcomes_pending"
                    }
                )
            except Exception as exc:
                log_error(f"🟥 Error entrenamiento por reinicio: {exc}\n```{traceback.format_exc()[-1600:]}```")
        # Startup recovery always runs once, even nights/weekends. If Data is
        # still publishing, wait non-blockingly for LISTO; after the recovery
        # snapshot the bot may enter heartbeat-only rest mode.
        if not startup_training_pending:
            _send_startup_recovery_once("estado inicial listo")

        ok, msg = data_dbs_available()
        if not ok:
            log_warning(build_group("🟧 ALERTS · ARRANQUE CON AVISO", [f"{msg}. El bot seguirá vivo y reintentará."]))
        else:
            coverage = startup.get("coverage") or {}
            startup_label = "ESPERANDO DATA" if startup_training_pending else "OPERATIVO"
            startup_processing = bool(startup.get('entries_enabled')) and not startup_training_pending
            log_info(
                f"ALERTS BOT · {startup_label} | mode={str(startup.get('status') or 'unknown').upper()} "
                f"| cierres={coverage.get('feature_dates', 0)}/{config.DAILY_CLOSE_WINDOW_SESSIONS} "
                f"| outcomes={coverage.get('core_outcome_dates', coverage.get('outcome_dates', 0))} "
                f"| entry_mode={str(startup.get('entry_mode') or 'off').upper()} "
                f"| processing={'ON' if startup_processing else 'OFF'} "
                f"| live_alerts={'ON' if (startup.get('live_alerts_enabled') and not startup_training_pending) else 'OFF'} "
                f"| shadow={'ON' if startup.get('shadow_scoring_enabled') else 'OFF'}", 
                send_discord=False,
            )
        emit_console_snapshot(context="startup complete")

        while True:
            now = now_madrid()
            operational_now = is_operational_window(now)
            if last_operational_state is None or operational_now != last_operational_state:
                if operational_now:
                    resumed = resume_after_rest()
                    log_info(
                        f"🟢 MODO OPERATIVO · estado {resumed.get('state')} | capturas, calibración y Research habilitados",
                        send_discord=False,
                    )
                else:
                    next_start = next_operational_start(now)
                    if startup_recovery_sent:
                        transition_pipeline_state(
                            "REST", stage="off_hours", preserve_entry_mode=True,
                            details={"next_start": next_start.isoformat()},
                        )
                    log_info(
                        f"🌙 MODO DESCANSO · próximo inicio {next_start.strftime('%Y-%m-%d %H:%M')} | solo heartbeat/coordinación",
                        send_discord=False,
                    )
                last_operational_state = operational_now
                emit_console_snapshot(context="operating window change")
            if time.time() - last_heartbeat > config.HEARTBEAT_MINUTES * 60:
                try:
                    activate_due_model()
                    persisted = insert_heartbeat("running", f"Bot vivo {now.isoformat()}")
                    if not persisted:
                        if not local_heartbeat_degraded:
                            log_info(
                                "⚠️ Heartbeat SQLite local pospuesto por bloqueo; "
                                "el heartbeat de coordinación continúa activo.",
                                send_discord=False,
                            )
                        local_heartbeat_degraded = True
                    elif local_heartbeat_degraded:
                        log_info("✅ Heartbeat SQLite local recuperado.", send_discord=False)
                        local_heartbeat_degraded = False
                except Exception as heartbeat_exc:
                    local_heartbeat_degraded = True
                    log_info(
                        f"⚠️ Heartbeat SQLite local degradado: {heartbeat_exc}. "
                        "Alerts continúa mediante Coordination Bus.",
                        send_discord=False,
                    )
                finally:
                    last_heartbeat = time.time()
                discord_interval = 3600 if operational_now else 10800
                if time.time() - last_discord_heartbeat >= discord_interval:
                    try:
                        send_alerts_heartbeat()
                    except Exception as hb_exc:
                        log_info(f"⚠️ Heartbeat Discord Alerts no entregado: {hb_exc}", send_discord=False)
                    last_discord_heartbeat = time.time()
                emit_console_snapshot(context="heartbeat")

            if (
                maintenance_retry_pending
                and startup_recovery_sent
                and not operational_now
                and time.time() >= next_maintenance_retry_at
            ):
                try:
                    from maintenance_v620 import main as retry_maintenance_v620
                    maintenance_result = retry_maintenance_v620() or {}
                    maintenance_retry_pending = bool(maintenance_result.get("pending"))
                    maintenance_retry_reason = str(maintenance_result.get("warning") or "")
                    if not maintenance_retry_pending:
                        log_info("✅ Mantenimiento SQLite recuperado fuera de calibración.", send_discord=True)
                    elif time.time() - last_maintenance_notice_at >= 1800:
                        log_info(
                            f"⚠️ Mantenimiento SQLite pendiente: {maintenance_retry_reason}; "
                            "nuevo intento en 30 minutos.",
                            send_discord=False,
                        )
                        last_maintenance_notice_at = time.time()
                except Exception as maintenance_exc:
                    maintenance_retry_pending = True
                    maintenance_retry_reason = str(maintenance_exc)
                    if time.time() - last_maintenance_notice_at >= 1800:
                        log_info(
                            f"⚠️ Mantenimiento SQLite pendiente: {maintenance_exc}; "
                            "nuevo intento en 30 minutos.",
                            send_discord=False,
                        )
                        last_maintenance_notice_at = time.time()
                finally:
                    next_maintenance_retry_at = time.time() + 1800

            if time.time() - last_coordination_heartbeat >= config.COORDINATION_HEARTBEAT_SECONDS:
                try:
                    data_state = read_data_state(config.COORDINATION_DB)
                    generation_seen = int(data_state.get("generation") or 0)
                    write_heartbeat(
                        config.COORDINATION_DB, "alerts_bot", "running",
                        generation_seen=generation_seen,
                        details={"entry_mode": get_runtime_state("entry_mode") or "unknown"},
                    )
                    peer = read_heartbeat(config.COORDINATION_DB, "data_bot")
                    peer_age = heartbeat_age_seconds(peer)
                    stale = peer_age is not None and peer_age > config.COORDINATION_STALE_SECONDS
                    if stale and time.time() >= peer_heartbeat_grace_until and not last_peer_warning:
                        log_info(f"⚠️ Data Bot sin heartbeat reciente | age={int(peer_age)}s", send_discord=False)
                        last_peer_warning = True
                    elif not stale and last_peer_warning:
                        log_info("✅ Heartbeat del Data Bot recuperado", send_discord=False)
                        last_peer_warning = False
                except Exception as exc:
                    log_error(f"🟥 Error heartbeat coordinación Alerts: {exc}")
                last_coordination_heartbeat = time.time()

            coordination_needed = bool(operational_now or startup_training_pending or not startup_recovery_sent)
            if coordination_needed and time.time() - last_coordination_poll >= config.COORDINATION_POLL_SECONDS:
                try:
                    ack = read_ack(config.COORDINATION_DB, "alerts_bot")
                    force_retry = bool(
                        str(ack.get("status") or "") == "error"
                        and int(ack.get("attempt_count") or 0) < config.COORDINATION_MAX_RETRIES
                        and time.time() - last_coordination_retry >= config.COORDINATION_RETRY_SECONDS
                    )
                    handoff = _process_coordination_generation(force=force_retry, startup_context=not startup_recovery_sent)
                    if force_retry:
                        last_coordination_retry = time.time()
                    if (
                        handoff.get("executed")
                        or handoff.get("status") in {
                            "already_processed", "calibrated", "calibrated_with_warnings",
                            "optimizer_executed_no_champion", "outcomes_pending",
                            "calibration_deferred_operational",
                        }
                        or (handoff.get("status") == "already_acknowledged" and handoff.get("ack_status") == "processed")
                    ):
                        startup_training_pending = False
                        last_core_watch = time.time()
                    elif handoff.get("ack_status") == "blocked":
                        # A blocked LISTO generation is final. Consume the startup
                        # recovery promise without publishing stale Research/Live and
                        # without repeating a warning on every coordination poll.
                        if not startup_recovery_sent:
                            transition_pipeline_state(
                                "ERROR", stage="coordination_generation_blocked",
                                generation=int(handoff.get("generation") or generation_seen or 0),
                                entry_mode="off", error_code=str(handoff.get("status") or "GENERATION_BLOCKED").upper(),
                                details={"ack_status": "blocked"},
                            )
                            startup_recovery_sent = True
                            set_runtime_state("last_startup_recovery_at", now_madrid().isoformat())
                            set_runtime_state(
                                "last_blocked_recovery_generation",
                                str(int(handoff.get("generation") or generation_seen or 0)),
                            )
                            log_info(
                                "⏭️ RECUPERACIÓN no publicada · generación Data bloqueada; "
                                "Research y Live permanecen cerrados hasta una nueva generación",
                                send_discord=False,
                            )
                        startup_training_pending = False
                        last_core_watch = time.time()
                except Exception as exc:
                    log_error(f"🟥 Error DATA HANDOFF: {exc}\n```{traceback.format_exc()[-1600:]}```")
                    last_coordination_retry = time.time()
                last_coordination_poll = time.time()

            if not startup_recovery_sent and not startup_training_pending:
                _send_startup_recovery_once("Data LISTO / calibración disponible")

            # Fallback: a disconnected Data Bot must not prevent the user from
            # receiving the last known market snapshot on restart. It is still
            # labelled recovery and never creates an entry.
            if (
                not startup_recovery_sent
                and time.time() - startup_cycle_started >= int(config.STARTUP_RECOVERY_MAX_WAIT_SECONDS)
            ):
                latest_state = read_data_state(config.COORDINATION_DB)
                if not _coordination_state_ready(latest_state):
                    # A long FULL DEEP repair can legitimately keep Data in progress
                    # for well over the startup timeout. Do not emit the same warning
                    # every LOOP_SLEEP_SECONDS and do not publish stale Research.
                    if not startup_wait_notice_sent:
                        log_info(
                            "⏳ Recuperación de arranque en espera · Data continúa procesando; "
                            "Research y Live permanecen cerrados hasta recibir LISTO",
                            send_discord=False,
                        )
                        startup_wait_notice_sent = True
                else:
                    _send_startup_recovery_once("fallback coordinado")

            # Before the one startup recovery is available, off-hours mode may
            # poll coordination and heartbeat only. The independent CORE watcher
            # must not bypass an incomplete Data generation.
            if bool(config.REST_MODE_HEARTBEAT_ONLY) and not operational_now and not startup_recovery_sent:
                time.sleep(config.LOOP_SLEEP_SECONDS)
                continue

            # Outside the broad Madrid window, the one startup recovery above is
            # preserved. From that point onward only local/coordination heartbeats
            # run; no CORE, outcomes, setups, scans or Discord operational traffic.
            if (
                bool(config.REST_MODE_HEARTBEAT_ONLY)
                and not operational_now
                and startup_recovery_sent
            ):
                if not _rest_completion_pending():
                    time.sleep(config.LOOP_SLEEP_SECONDS)
                    continue
                _log_info_throttled(
                    "rest_drain",
                    "🌙 [REST DRAIN] Data/deep pendiente detectado · se completa handoff/backlog antes del descanso",
                )

            # Independent of Scanner slots and deep runs. A forced restart train
            # is attempted once; later checks are idempotent by versioned state.
            core_watch_interval = (
                int(getattr(config, "CORE_RESTART_TRAIN_POLL_SECONDS", 30))
                if startup_training_pending else min(300, max(60, int(config.HEARTBEAT_MINUTES) * 60))
            )
            if _core_watch_allowed(
                operational_now=operational_now,
                startup_training_pending=startup_training_pending,
                startup_recovery_sent=startup_recovery_sent,
            ) and time.time() - last_core_watch >= core_watch_interval:
                try:
                    adaptive_force = str(get_runtime_state("adaptive_retrain_requested", "0") or "0") == "1"
                    adaptive_reason = str(get_runtime_state("adaptive_retrain_reason", "") or "")
                    watch = _attempt_core_recalibration(
                        context=("aprendizaje adaptativo · " + adaptive_reason if adaptive_force else "CORE completado durante daemon"),
                        force=adaptive_force,
                    )
                    if watch.get("executed"):
                        _run_audit_learning_after_core(
                            context="daemon CORE learning completion",
                            core_result=watch,
                            send_discord=True,
                        )
                    if watch.get("executed") or watch.get("status") in {"already_processed", "calibration_deferred_operational"}:
                        # A deferred learning request must never keep startup_training_pending
                        # true because that flag intentionally blocks fresh Scanner processing.
                        startup_training_pending = False
                        if adaptive_force and watch.get("executed"):
                            set_runtime_state("adaptive_retrain_requested", "0")
                            set_runtime_state("adaptive_last_retrain_source_dates", str(int((history_coverage() or {}).get("core_feature_dates") or 0)))
                except Exception as exc:
                    log_error(f"🟥 Error CORE completion watch: {exc}\n```{traceback.format_exc()[-1400:]}```")
                last_core_watch = time.time()

            # Position safety remains active even when global Scanner
            # integrity blocks the entry/research handoff. This path is lifecycle-only.
            try:
                maybe_process_partial_risk_refresh()
            except Exception as risk_exc:
                _log_info_throttled(
                    "partial_risk_refresh_error",
                    f"⚠️ Position risk refresh pospuesto: {risk_exc}",
                    interval_seconds=300,
                )

            if config.AUTO_SCAN_ON_NEW_DATA:
                try:
                    processed_id = latest_processed_scanner_run_id()
                    pending_runs = successful_scanner_runs_after(processed_id, limit=12)
                    if pending_runs:
                        latest_pending_id = int(pending_runs[-1].get("id") or 0)
                        if startup_training_pending:
                            if latest_pending_id != last_deferred_run_id:
                                log_info(
                                    f"⏳ Runs pendientes `{len(pending_runs)}` · último `{latest_pending_id}`: "
                                    "primero se completa el entrenamiento por reinicio.",
                                    send_discord=False,
                                )
                                last_deferred_run_id = latest_pending_id
                        else:
                            if len(pending_runs) > 1:
                                log_info(
                                    f"🔄 Recuperando backlog Alerts · runs `{pending_runs[0].get('id')}→{pending_runs[-1].get('id')}` "
                                    f"· total `{len(pending_runs)}`",
                                    send_discord=False,
                                )
                            for index, scanner_run in enumerate(pending_runs):
                                run_id = int(scanner_run.get("id") or 0)
                                if not run_id or run_id in auto_seen_runs:
                                    continue
                                auto_seen_runs.add(run_id)
                                log_info(f"🆕 Captura Data Bot detectada | scanner_run={run_id}", send_discord=False)
                                run_alert_scan(scanner_run_id=run_id)
                                if index == len(pending_runs) - 1:
                                    try:
                                        maybe_send_for_scanner_run(scanner_run)
                                    except Exception as exc:
                                        log_error(f"🟥 Error informe operativo de setups: {exc}")
                                if config.AUTO_RECALIBRATE_ON_DAILY_CLOSE and "deep" in str(scanner_run.get("session_type") or "").lower():
                                    _daily_recalibration(scanner_run, context="cierre diario expansivo")
                            post_scan_core = _attempt_core_recalibration(context="CORE completado tras backlog/captura intradía")
                            if post_scan_core.get("executed"):
                                _run_audit_learning_after_core(
                                    context="CORE post-scan/backlog",
                                    core_result=post_scan_core,
                                    send_discord=True,
                                )
                            if len(pending_runs) > 1:
                                log_info(
                                    f"✅ Backlog Alerts recuperado · último scanner_run `{pending_runs[-1].get('id')}`",
                                    send_discord=False,
                                )
                                emit_console_snapshot(context=f"backlog recovered through run={pending_runs[-1].get('id')}")
                except Exception as exc:
                    log_error(f"🟥 Error auto-scan/backlog por nuevo dato: {exc}\n```{traceback.format_exc()[-1600:]}```")

            for job_type, hhmm in due_jobs(already_run):
                key = f"{now.strftime('%Y-%m-%d')}|{job_type}|{hhmm}"
                mark_job_complete = True
                try:
                    if job_type == "premarket_setup":
                        send_setup_phase("premarket")
                    elif job_type == "alerts":
                        if startup_training_pending:
                            log_info("⏳ ALERTS job aplazado hasta completar entrenamiento por reinicio.", send_discord=False)
                            mark_job_complete = False
                        else:
                            run_alert_scan()
                    elif job_type == "labels":
                        defer = _core_learning_defer_reason(context="scheduled labels")
                        if defer:
                            mark_job_complete = False
                            _log_info_throttled(
                                f"labels_deferred:{defer.get('reason')}",
                                f"⏸️ LABELS aplazado | reason={defer.get('reason')} · prioridad Data/deep",
                            )
                        else:
                            materialize_outcomes()
                            materialize_layer_outcomes()
                            archive_completed_layer_dates()
                            labels_audit = maybe_run_market_outcome_audit(context="scheduled labels", send_discord=True)
                            maybe_run_adaptive_learning(
                                context="scheduled labels",
                                audit_payload=labels_audit if labels_audit.get("metrics") else None,
                                send_discord=True, console_if_unchanged=True,
                            )
                    elif job_type == "train":
                        train_result = _run_training_cycle("calibración diaria programada", force_core=False)
                        if train_result.get("status") == "calibration_deferred_operational":
                            mark_job_complete = False
                    elif job_type == "export":
                        export_research_tables()
                except Exception as exc:
                    log_error(f"🟥 Error en job {job_type}: {exc}\n```{traceback.format_exc()[-1800:]}```")
                finally:
                    if mark_job_complete:
                        already_run.add(key)
            today = now.strftime('%Y-%m-%d')
            already_run = {key for key in already_run if key.startswith(today)}
            time.sleep(config.LOOP_SLEEP_SECONDS)
    finally:
        heartbeat_stop.set()
        try:
            heartbeat_thread.join(timeout=2)
        except Exception:
            pass
        try:
            state = read_data_state(config.COORDINATION_DB)
            write_heartbeat(config.COORDINATION_DB, "alerts_bot", "stopped", generation_seen=int(state.get("generation") or 0))
        except Exception:
            pass
        release_lock()



def _publish_alerts_db_ready() -> None:
    """Signal auxiliary observers only after the production schema is usable."""
    path = config.RUNTIME_ROOT / "alerts_db_ready.json"
    tmp = path.with_suffix(".json.tmp")
    payload = {
        "pid": os.getpid(),
        "bot_version": config.BOT_VERSION,
        "ready_at_madrid": now_madrid().isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        choices=["alerts", "labels", "train", "backtest", "research", "bootstrap", "baseline", "startup", "daily", "status", "sync", "setup-premarket", "setup-open", "setup-next", "setup-recovery", "positions", "audit"],
        help="Ejecuta una tarea manual y termina",
    )
    args = parser.parse_args()
    ensure_alerts_schema()
    _publish_alerts_db_ready()
    if args.once == "alerts":
        run_alert_scan()
    elif args.once == "labels":
        materialize_outcomes()
    elif args.once == "train":
        bootstrap_intraday_archives_if_ready()
        materialize_outcomes()
        if config.RELATIONSHIP_REFRESH_AFTER_TRAIN:
            refresh_relationship_graph(force=True)
        _run_training_cycle("calibración manual", force_core=True)
    elif args.once == "backtest":
        run_backtest()
    elif args.once == "research":
        export_research_tables()
    elif args.once == "bootstrap":
        bootstrap_from_archives(); materialize_outcomes()
    elif args.once == "baseline":
        from initial_baseline import main as initial_baseline_main
        raise SystemExit(initial_baseline_main())
    elif args.once == "startup":
        print(run_startup_warmup())
    elif args.once == "daily":
        send_daily_operating_summary()
    elif args.once == "status":
        from db import get_active_model
        print({"active_model": get_active_model(), "coordination": read_data_state(config.COORDINATION_DB), "ack": read_ack(config.COORDINATION_DB, "alerts_bot")})
    elif args.once == "sync":
        print(_process_coordination_generation(force=True))
    elif args.once == "setup-premarket":
        send_setup_phase("premarket", force=True)
    elif args.once == "setup-open":
        send_setup_phase("market_open", force=True)
    elif args.once == "setup-next":
        send_setup_phase("next_day", force=True)
    elif args.once == "setup-recovery":
        send_recovery_snapshot()
    elif args.once == "positions":
        maybe_send_open_positions_overview(force=True)
    elif args.once == "audit":
        run_market_outcome_audit(context="manual CLI", force=True, send_discord=True, console_if_unchanged=True)
    else:
        run_daemon()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        release_lock(); print("Interrumpido por usuario"); sys.exit(0)
    except RuntimeError as exc:
        if str(exc).startswith("El Alerts Bot ya parece activo con PID"):
            # Never remove the lock owned by the process that is actually alive.
            print(f"⏭️ {exc} No se inicia una segunda instancia.", flush=True)
            raise SystemExit(0)
        release_lock()
        log_error(f"🟥 Fatal: {exc}\n```{traceback.format_exc()[-1800:]}```")
        raise
    except Exception as exc:
        release_lock(); log_error(f"🟥 Fatal: {exc}\n```{traceback.format_exc()[-1800:]}```"); raise
