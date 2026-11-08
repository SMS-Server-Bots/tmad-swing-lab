from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from alerts_engine import run_alert_scan
from archive_bootstrap import bootstrap_from_archives, get_last_archive_bootstrap_status
from intraday_archive_bootstrap import bootstrap_intraday_archives_if_ready
from db import (
    activate_due_model,
    backfill_capture_layers_from_live_runs,
    get_active_model,
    ensure_alerts_schema,
    history_coverage,
    latest_canonical_daily_dates,
    latest_successful_scanner_run,
    processed_scanner_run_ids,
    successful_scanner_runs_since,
    sync_live_price_cache,
    canonical_materialization_reconciliation,
    core_feature_date_inventory,
    set_runtime_state, get_runtime_state, latest_scanner_run_any,
)
from discord_utils import log_error, log_info, send_daily
from optimizer import run_optimizer
from core_history import materialize_core_history_if_needed, core_source_coverage
from core_reconciliation import reconcile_core_universe, reconcile_core_dates
from outcomes import materialize_outcomes
from performance_report import build_performance_report
from pipeline_diagnostics import record_stage, stage_line, friendly_stage_line, webhook_status
from relationship_learning import backfill_relational_feature_history, refresh_relationship_graph
from time_utils import now_madrid, is_operational_window
from calibration_state import build_core_calibration_signature
from coordination_bus import read_data_state
from pipeline_state import transition_pipeline_state, readiness_state
from discord_presentation import build_group


def _write_status(payload: dict) -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config.STARTUP_STATUS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def classify_data_manifest(manifest: dict) -> dict:
    """Separate a current in-progress handoff from a stale/broken manifest."""
    version_ok = str(manifest.get("bot_version") or "") == str(config.BOT_VERSION)
    generation_ok = bool(version_ok and manifest.get("bootstrap_id"))
    complete = bool(generation_ok and manifest.get("bootstrap_complete"))
    return {
        "version_ok": version_ok,
        "generation_ok": generation_ok,
        "complete": complete,
        "pipeline_safe_for_shadow": generation_ok,
        "status": "OK" if complete else ("WAIT" if generation_ok else "ERROR"),
    }


def _wait_for_data_history_manifest() -> dict:
    """Read the latest Data state once and continue immediately.

    Since v5.7.2 the Coordination Bus is the live handoff. Alerts never blocks
    startup waiting for Data: an incomplete current generation is accepted for
    safe SHADOW processing and later generations trigger recalibration in daemon.
    The atomic JSON manifest remains a fallback if the shared SQLite is missing.
    """
    manifest: dict = {}
    source = "none"
    try:
        state = read_data_state(config.COORDINATION_DB)
        candidate = state.get("manifest") if isinstance(state, dict) else None
        if isinstance(candidate, dict) and candidate:
            manifest = dict(candidate)
            manifest["_coordination_generation"] = int(state.get("generation") or 0)
            manifest["_coordination_updated_at_utc"] = state.get("updated_at_utc")
            source = "coordination_bus"
    except Exception as exc:
        log_info(f"⚠️ [A01] Coordination Bus no disponible al arrancar: {exc}", send_discord=False)

    if not manifest and config.DATA_HISTORY_STATUS_FILE.exists():
        try:
            candidate = json.loads(config.DATA_HISTORY_STATUS_FILE.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                manifest = candidate
                source = "atomic_json"
        except Exception as exc:
            log_info(f"⚠️ [A01] Manifest JSON no legible al arrancar: {exc}", send_discord=False)

    state = classify_data_manifest(manifest)
    if state["generation_ok"] and not state["complete"]:
        manifest["_accepted_partial_manifest"] = True
    manifest["_startup_handoff_source"] = source
    manifest["_startup_nonblocking"] = True
    log_info(
        f"ℹ️ [A01] Handoff no bloqueante | source={source} | "
        f"complete={bool(state['complete'])} | generation={manifest.get('_coordination_generation', 0)}",
        send_discord=False,
    )
    return manifest


def _stage_summary(stages: list[dict]) -> str:
    lines: list[str] = []
    for stage in stages:
        status = str(stage.get("status") or "").upper()
        icon = {"OK": "✅", "WAIT": "⏳", "WARN": "⚠️", "ERROR": "🟥", "SKIP": "⏭️"}.get(status, "•")
        lines.append(f"{icon} `{stage.get('code')}` {stage.get('title')}: **{status}**")
    return "\n".join(lines)


def classify_layer_reconciliation(
    core_data_dates: int,
    core_alert_dates: int,
    core_errors: int,
    full_consistent: bool,
    core_universe_complete: bool = True,
) -> dict:
    """Classify empty-but-equal CORE as WAIT, never as an inconsistency."""
    core_consistent = bool(
        int(core_data_dates) == int(core_alert_dates)
        and int(core_errors) == 0
    )
    core_available = int(core_alert_dates) > 0
    if not core_consistent:
        return {
            "status": "ERROR",
            "core_consistent": False,
            "core_available": core_available,
            "reason": "CORE Data/Alerts inconsistente.",
            "action": "Bloquear entradas y reparar CORE_HISTORY.",
        }
    if core_available and not core_universe_complete:
        return {
            "status": "WAIT",
            "core_consistent": True,
            "core_available": True,
            "reason": "CORE Data/Alerts coincide, pero el universo Scanner aún está incompleto.",
            "action": "Mantener shadow y continuar el bootstrap hasta completar todos los tickers del Scanner.",
        }
    if core_available and full_consistent:
        return {
            "status": "OK",
            "core_consistent": True,
            "core_available": True,
            "reason": None,
            "action": "CORE puede calibrarse; FULL DEEP seguirá acumulándose.",
        }
    if core_available:
        return {
            "status": "WARN",
            "core_consistent": True,
            "core_available": True,
            "reason": "FULL DEEP aún no está conciliado; CORE permanece utilizable.",
            "action": "Mantener CORE operativo y reparar la capa FULL por separado.",
        }
    return {
        "status": "WAIT",
        "core_consistent": True,
        "core_available": False,
        "reason": "CORE todavía no está materializado en ninguna de las dos capas (0=0).",
        "action": "Mantener bootstrap shadow mientras Data Bot completa CORE_HISTORY.",
    }


def decide_operational_mode(
    champion_ready: bool,
    core_consistent: bool,
    manifest_ok: bool,
) -> dict:
    pipeline_safe = bool(
        core_consistent
        and (manifest_ok or not config.PRECALIBRATION_REQUIRE_PIPELINE_OK)
    )
    shadow_ready = bool(
        not champion_ready
        and pipeline_safe
        and config.PRECALIBRATION_SHADOW_ENABLED
    )
    entry_mode = "live" if champion_ready else ("shadow" if shadow_ready else "off")
    status = "calibrated" if champion_ready else ("bootstrap_shadow" if shadow_ready else "error")
    return {
        "pipeline_safe": pipeline_safe,
        "shadow_ready": shadow_ready,
        "entry_mode": entry_mode,
        "status": status,
    }


def _hhmm_minutes(value: str) -> int:
    try:
        hour, minute = [int(part) for part in str(value).split(":", 1)]
        return hour * 60 + minute
    except Exception:
        return 0


def _startup_heavy_defer_reason() -> dict:
    """Use the same freshness principle as daemon learning before A04-A09."""
    try:
        latest = latest_scanner_run_any() or {}
    except Exception:
        latest = {}
    status = str(latest.get("status") or "").lower()
    stage = str(latest.get("stage") or "").lower()
    if status == "running" or stage in {"created", "running", "fetching", "enriching", "saving"}:
        return {
            "reason": "data_run_running",
            "run_id": int(latest.get("id") or 0),
            "session": str(latest.get("session_type") or ""),
        }
    market_now = datetime.now(ZoneInfo(config.ENTRY_MARKET_TIMEZONE))
    if market_now.weekday() < 5:
        current = market_now.hour * 60 + market_now.minute
        start = _hhmm_minutes(config.ENTRY_WINDOW_START)
        end = _hhmm_minutes(config.ENTRY_WINDOW_END)
        guard = int(getattr(config, "CORE_LEARNING_ENTRY_GUARD_MINUTES", 180))
        if start <= current <= end:
            return {"reason": "entry_window_open", "market_time": market_now.isoformat()}
        minutes_to_entry = start - current
        if 0 < minutes_to_entry <= guard:
            return {
                "reason": "entry_window_guard",
                "minutes_to_entry": minutes_to_entry,
                "guard_minutes": guard,
                "market_time": market_now.isoformat(),
            }
    return {}


def _engine_signature() -> str:
    return str(getattr(config, "CORE_CALIBRATION_ENGINE_SIGNATURE", "core"))


def _engine_matches(stored: str | None, current: str | None = None) -> bool:
    stored = str(stored or "")
    current = str(current or _engine_signature())
    return bool(stored == current or stored.endswith("|" + current))


def _outcome_fingerprint(coverage: dict, *, prefix: str = "core_") -> str:
    return "|".join([
        str(int(coverage.get(prefix + "outcome_rows") or 0)),
        str(int(coverage.get(prefix + "outcome_dates") or 0)),
        str(coverage.get(prefix + "outcome_start_date") or ""),
        str(coverage.get(prefix + "outcome_end_date") or ""),
    ])


def run_startup_warmup() -> dict:
    started = now_madrid()
    result: dict = {
        "started_at_madrid": started.isoformat(),
        "status": "running",
        "stages": [],
        "coverage": {},
        "active_model": {},
        "core_optimizer_attempted": False,
        "core_optimizer_completed": False,
        "heavy_pipeline_attempted": False,
    }
    if not config.STARTUP_WARMUP_ENABLED:
        result["status"] = "disabled"
        _write_status(result)
        return result

    # Keep this entry point safe when invoked directly by diagnostics/tests.
    # main.py also calls it before startup, so this is idempotent in production.
    ensure_alerts_schema()

    def emit(code: str, status: str, title: str, **kwargs) -> dict:
        payload = record_stage(code, status, title, **kwargs)
        result["stages"].append(payload)
        # Detailed stage output stays in console/local JSONL. Discord receives one
        # compact pipeline summary at the end, plus the calibration status channel.
        log_info(friendly_stage_line(payload), send_discord=False)
        return payload

    try:
        hooks = webhook_status()
        emit(
            "A00", "OK" if hooks.get("alerts_status") and hooks.get("calibration") else "WARN",
            "Canales del Alerts Bot",
            expected="Alerts Status y Calibration configurados",
            found={k: ("configured" if v else "missing") for k, v in hooks.items()},
            action=(
                "Enviar resumen operativo y estado de calibración."
                if hooks.get("alerts_status") and hooks.get("calibration")
                else "Guardar localmente y mostrar qué webhook falta."
            ),
            reason=(None if hooks.get("alerts_status") and hooks.get("calibration") else "Falta al menos un canal de diagnóstico."),
        )

        data_history = _wait_for_data_history_manifest()
        result["data_history_manifest"] = data_history
        manifest_state = classify_data_manifest(data_history)
        manifest_complete = bool(manifest_state["complete"])
        manifest_pipeline_safe = bool(manifest_state["pipeline_safe_for_shadow"])
        pipeline_generation = int(data_history.get("_coordination_generation") or 0)
        transition_pipeline_state(
            "BUILDING_CORE" if manifest_complete else "WAITING_DATA",
            stage="startup_manifest_ready" if manifest_complete else "startup_waiting_data",
            generation=pipeline_generation,
            entry_mode="off" if manifest_complete else "shadow",
            details={"manifest_complete": manifest_complete, "source": data_history.get("_startup_handoff_source")},
        )
        data_daily_closes = int(data_history.get("paired_trading_dates") or 0)
        emit(
            "A01", str(manifest_state["status"]), "Sincronización con Data Bot",
            expected="Estado actual del Data Bot; completo para calibrar o parcial seguro para shadow",
            found={
                "version": data_history.get("bot_version"),
                "bootstrap_complete": data_history.get("bootstrap_complete"),
                "bootstrap_id": data_history.get("bootstrap_id"),
                "daily_closes": data_daily_closes,
                "coverage_end": data_history.get("coverage_end_date"),
                "partial_accepted": bool(data_history.get("_accepted_partial_manifest")),
                "generation": int(data_history.get("_coordination_generation") or 0),
            },
            action=(
                "Usar la cobertura final publicada por Data Bot." if manifest_complete
                else (
                    "Iniciar shadow mientras Data Bot termina; mantener calibración y live bloqueados."
                    if manifest_pipeline_safe else
                    "Bloquear y revisar que Data Bot haya iniciado la generación actual."
                )
            ),
            reason=(
                None if manifest_complete
                else (
                    "Bootstrap actual todavía en progreso; no es una inconsistencia."
                    if manifest_pipeline_safe else
                    "Manifest ausente, de otra versión o sin bootstrap_id actual."
                )
            ),
        )

        archive_features, archive_prices = bootstrap_from_archives()
        archive_status = get_last_archive_bootstrap_status()
        result["archive_features_imported"] = archive_features
        result["archive_prices_imported"] = archive_prices
        result["archive_status"] = archive_status
        archive_stage_status = "OK" if archive_status.get("usable", 0) else ("SKIP" if archive_status.get("new", 0) == 0 else "WARN")
        archive_reason = None
        if archive_status.get("new", 0) == 0:
            archive_reason = "No había ZIP semanal nuevo; se continúa con DB live y ZIP ya importados."
        elif archive_status.get("usable", 0) == 0:
            archive_reason = "Los ZIP revisados no contenían cierres deep canónicos utilizables."
        emit(
            "A02", archive_stage_status, "Importación de ZIP semanales deep",
            expected="Importar únicamente cierres deep completos aún no materializados",
            found={
                "selected": archive_status.get("selected", 0),
                "new": archive_status.get("new", 0),
                "usable": archive_status.get("usable", 0),
                "without_deep": archive_status.get("skipped", 0),
                "rows_imported": archive_features,
            },
            action="Mantener el histórico diario; ignorar explícitamente ZIP antiguos sin deep.",
            reason=archive_reason,
            details={"zips": archive_status.get("details", [])},
        )

        intraday_status = bootstrap_intraday_archives_if_ready()
        result["intraday_archive_status"] = intraday_status
        emit(
            "A03", "OK" if intraday_status.get("ready") else "WAIT", "Archivo intradía para timing futuro",
            expected=f"{intraday_status.get('required_sessions', config.INTRADAY_CALIBRATION_MIN_ARCHIVED_SESSIONS)} sesiones archivadas",
            found={
                "available": intraday_status.get("available_sessions", 0),
                "archives": intraday_status.get("archives", 0),
                "imported_now": intraday_status.get("imported_archives", 0),
            },
            action=("Usar ZIP intradía para calibrar timing." if intraday_status.get("ready") else "Conservar ZIP; todavía no influye en la configuración."),
            reason=(None if intraday_status.get("ready") else "Muestra intradía insuficiente."),
        )

        # Immediate Discord acknowledgement: the bot is alive even while the
        # heavier CORE materialisation/calibration continues.
        startup_next = (
            "reconstrucción CORE → outcomes → calibración"
            if manifest_complete
            else "esperar la generación Data LISTO sin trabajo duplicado; después CORE → outcomes → calibración"
        )
        startup_discord = build_group(
            f"{'🟢' if manifest_complete else '🟡'} ALERTS · ARRANQUE",
            ["\n".join([
                f"**Versión:** `v{config.BOT_VERSION}` · **Data:** `{'LISTO' if manifest_complete else 'EN PROGRESO'}` · **Generación:** `{data_history.get('_coordination_generation', 0)}`",
                f"**Modo temporal:** `{'CALIBRANDO' if manifest_complete else 'SHADOW'}` · **Siguiente:** {startup_next}.",
                "**Canales:** Alerts Status = estado · Calibration = modelo · Learning Audit = validación · Research = shortlist · Shadow/Live = lifecycle.",
            ])],
            footer=(None if is_operational_window() else "Fuera de ventana operativa: después de la recuperación quedará solo el heartbeat."),
        )
        log_info(startup_discord, send_discord=True)

        # A current but incomplete Data generation must stop the heavy startup
        # pipeline here. Alerts remains alive in SHADOW and the Coordination Bus
        # resumes CORE -> outcomes -> optimizer exactly once when Data publishes
        # a semantic LISTO generation. Continuing below would reuse stale local
        # coverage and run A04-A09 concurrently with Data.
        if config.CORE_HISTORY_ENABLED and not manifest_complete:
            cached_coverage = history_coverage()
            cached_core = core_source_coverage()
            operational = decide_operational_mode(
                champion_ready=False, core_consistent=True, manifest_ok=True
            )
            set_runtime_state("entry_mode", str(operational["entry_mode"]))
            transition_pipeline_state(
                "WAITING_DATA", stage="startup_deferred",
                generation=int(data_history.get("_coordination_generation") or 0),
                entry_mode="shadow", details={"deferred": True},
            )
            result.update({
                "status": "waiting_data",
                "deferred_to_coordination": True,
                "entries_enabled": True,
                "live_alerts_enabled": False,
                "shadow_scoring_enabled": True,
                "entry_mode": str(operational["entry_mode"]),
                "pipeline_safe": True,
                "coverage": cached_coverage,
                "core_history_source": cached_core,
                "core_history_materialization": {
                    **cached_core,
                    "deferred": True,
                    "errors": 0,
                    "inserted_or_updated": 0,
                },
                "active_model": get_active_model(),
                "finished_at_madrid": now_madrid().isoformat(),
            })
            log_info(
                "⏭️ [5/9] CORE aplazado de verdad | no se ejecutan A04-A09 con Data en progreso",
                send_discord=False,
            )
            log_info(
                "✅ [9/9] Alerts operativo en SHADOW | Coordination Bus activo; "
                "reanudarán CORE, outcomes y calibración al recibir Data LISTO",
                send_discord=False,
            )
            _write_status(result)
            return result

        startup_defer = _startup_heavy_defer_reason()
        if startup_defer:
            cached_coverage = history_coverage()
            cached_core = core_source_coverage()
            operational = decide_operational_mode(
                champion_ready=False, core_consistent=True, manifest_ok=True
            )
            set_runtime_state("entry_mode", str(operational["entry_mode"]))
            transition_pipeline_state(
                readiness_state(str(operational["entry_mode"]), safe=True),
                stage="startup_learning_deferred_operational",
                generation=int(data_history.get("_coordination_generation") or 0),
                entry_mode=str(operational["entry_mode"]),
                details={"deferred": True, **startup_defer},
            )
            result.update({
                "status": "bootstrap_shadow",
                "deferred_to_coordination": True,
                "defer_reason": startup_defer.get("reason"),
                "entries_enabled": True,
                "live_alerts_enabled": False,
                "shadow_scoring_enabled": True,
                "entry_mode": str(operational["entry_mode"]),
                "pipeline_safe": True,
                "coverage": cached_coverage,
                "core_history_source": cached_core,
                "active_model": get_active_model(),
                "finished_at_madrid": now_madrid().isoformat(),
            })
            log_info(
                "⏸️ [STARTUP LEARNING DEFERRED] "
                f"reason={startup_defer.get('reason')} · prioridad scans/Data; Coordination Bus reanudará después",
                send_discord=False,
            )
            _write_status(result)
            return result

        result["heavy_pipeline_attempted"] = True
        transition_pipeline_state(
            "BUILDING_CORE", stage="startup_materializing_core",
            generation=int(data_history.get("_coordination_generation") or 0), entry_mode="off",
        )
        core_source = core_source_coverage()
        if config.CORE_HISTORY_ENABLED:
            log_info("🔄 [5/9] Preparando histórico CORE…", send_discord=False)
            def _startup_core_progress(stage, current, total, detail):
                if stage == "relationships":
                    log_info(f"   ↳ CORE {current}/{total} fechas | {detail}", send_discord=False)
                elif stage in {"loaded", "calendar", "saving"}:
                    log_info(f"   ↳ CORE · {detail}", send_discord=False)
            core_materialization = materialize_core_history_if_needed(progress_callback=_startup_core_progress)
            if core_materialization.get("cached"):
                log_info("⚡ [5/9] Histórico CORE ya estaba actualizado; reutilizando cache", send_discord=False)
        else:
            core_materialization = {**core_source, "errors": 0, "inserted_or_updated": 0}
        result["core_history_source"] = core_source
        result["core_history_materialization"] = core_materialization
        core_dates = int(core_materialization.get("date_count") or 0)
        core_target = int(core_materialization.get("target") or config.CORE_HISTORY_TARGET_SESSIONS)
        core_errors = int(core_materialization.get("errors") or 0)
        core_cache_coverage = history_coverage()
        core_feature_tickers_now = int(core_cache_coverage.get("core_feature_tickers") or 0)
        universe_reconciliation = reconcile_core_universe(
            data_history, core_materialization, feature_tickers=core_feature_tickers_now
        )
        result["core_universe_reconciliation"] = universe_reconciliation
        source_universe_complete = bool(core_materialization.get("universe_complete"))
        manifest_universe_complete = bool(data_history.get("core_history_universe_complete"))
        source_universe_tickers = int(core_materialization.get("universe_tickers") or 0)
        manifest_universe_tickers = int(data_history.get("core_history_universe_tickers") or 0)
        source_universe_fingerprint = str(universe_reconciliation.get("fingerprint") or "")
        manifest_universe_fingerprint = str(data_history.get("core_history_universe_fingerprint") or "")
        core_universe_counts_match = bool(universe_reconciliation.get("counts_match"))
        core_universe_fingerprint_match = bool(universe_reconciliation.get("fingerprint_match"))
        source_excluded_tickers = int(universe_reconciliation.get("excluded_tickers") or 0)
        manifest_excluded_tickers = int(data_history.get("core_history_excluded_tickers") or 0)
        source_excluded_fingerprint = str(universe_reconciliation.get("excluded_fingerprint") or "")
        manifest_excluded_fingerprint = str(data_history.get("core_history_excluded_fingerprint") or "")
        core_exclusions_match = bool(universe_reconciliation.get("exclusions_match"))
        core_universe_complete = bool(universe_reconciliation.get("complete"))
        core_universe_warning = bool(universe_reconciliation.get("warning"))
        effective_pending_tickers = int(universe_reconciliation.get("pending_tickers") or 0)
        core_stage_status = (
            ("WARN" if core_universe_warning else "OK")
            if core_dates >= core_target and core_errors <= max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25))) and core_universe_complete
            else ("WARN" if core_dates > 0 else "WAIT")
        )
        emit(
            "A04C", core_stage_status, "CORE HISTORY TMAD",
            expected=f"{core_target} sesiones de history + priceHistory para calibración inicial",
            found={
                "dates": core_dates,
                "target": core_target,
                "source_rows": core_materialization.get("rows", 0),
                "tickers": core_materialization.get("tickers", 0),
                "universe_tickers": source_universe_tickers,
                "eligible_tickers": core_materialization.get("complete_tickers", 0),
                "excluded_tickers": source_excluded_tickers,
                "pending_tickers": effective_pending_tickers,
                "resolved_tickers": universe_reconciliation.get("resolved_tickers", 0),
                "remaining_tickers": effective_pending_tickers,
                "authoritative_reconciliation": universe_reconciliation.get("authoritative_reconciliation", False),
                "reconciliation_warning": core_universe_warning,
                "universe_complete": core_universe_complete,
                "manifest_universe_tickers": manifest_universe_tickers,
                "universe_fingerprint_match": core_universe_fingerprint_match,
                "exclusions_match": core_exclusions_match,
                "observations_upserted": core_materialization.get("inserted_or_updated", 0),
                "materializable_tickers": core_materialization.get("materializable_tickers", 0),
                "nonmaterializable_tickers": core_materialization.get("nonmaterializable_tickers", 0),
                "nonmaterializable_ticker_sample": core_materialization.get("nonmaterializable_ticker_sample", []),
                "with_total_gex": core_materialization.get("with_total_gex", 0),
                "relational_dates": core_materialization.get("relational_dates", 0),
                "errors": core_errors,
                "universe_quality": core_materialization.get("universe_quality"),
            },
            action=(
                "Autorizar backtest CORE purgado; FULL DEEP e intradía continúan como challengers."
                if core_dates >= core_target and core_errors <= max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25))) and core_universe_complete
                else "Seguir refrescando /api/stock/{ticker}; no fabricar variables históricas granulares."
            ),
            reason=(
                None if core_dates >= core_target and core_errors <= max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25))) and core_universe_complete and not core_universe_warning
                else (
                    f"CORE parcial: fechas {core_dates}/{core_target}; universo "
                    f"eligible={core_materialization.get('complete_tickers', 0)} · "
                    f"excluded={source_excluded_tickers} · pending={effective_pending_tickers}/"
                    f"{source_universe_tickers}; errores={core_errors}."
                )
            ),
        )

        if core_universe_warning:
            log_info(
                "⚠️ CORE startup reconciliado por universo Data autoritativo | "
                f"eligible={int(core_materialization.get('complete_tickers') or 0)} · "
                f"excluded={source_excluded_tickers} · pending=0/{source_universe_tickers} · "
                "modo SHADOW hasta converger huellas locales",
                send_discord=False,
            )

        result["capture_layer_backfill"] = backfill_capture_layers_from_live_runs()
        latest = latest_successful_scanner_run()
        latest_id = int(latest.get("id") or 0) if latest else 0
        latest_is_fresh = False
        if latest:
            try:
                captured = datetime.fromisoformat(str(latest.get("captured_at_madrid") or ""))
                latest_is_fresh = (now_madrid() - captured).total_seconds() / 60.0 <= config.MAX_SCANNER_AGE_MINUTES
            except Exception:
                latest_is_fresh = False

        imported_runs = imported_rows = 0
        if config.STARTUP_IMPORT_LIVE_HISTORY:
            cutoff = (now_madrid().date() - timedelta(days=config.FEATURE_RETENTION_DAYS)).isoformat()
            processed = processed_scanner_run_ids()
            runs = successful_scanner_runs_since(cutoff, config.STARTUP_MAX_HISTORICAL_RUNS)
            reconciliation_before = canonical_materialization_reconciliation(
                cutoff, config.STARTUP_MAX_HISTORICAL_RUNS
            )
            incomplete_ids = {
                int(item.get("run_id") or 0)
                for item in reconciliation_before.get("incomplete_runs", [])
            }
            historical_runs = [
                run for run in runs
                if "deep" in str(run.get("session_type") or "").lower()
                and (int(run.get("id") or 0) not in processed or int(run.get("id") or 0) in incomplete_ids)
                and not (latest_is_fresh and int(run.get("id") or 0) == latest_id)
            ]
            for run in historical_runs:
                inserted = run_alert_scan(int(run["id"]), historical=True)
                imported_runs += 1
                imported_rows += int(inserted or 0)
        result["live_runs_materialized"] = imported_runs
        result["live_observations_inserted"] = imported_rows

        materialized_coverage = history_coverage()
        alerts_daily_after_import = int(materialized_coverage.get("feature_dates") or 0)
        alerts_rows_after_import = int(materialized_coverage.get("feature_rows") or 0)
        cutoff_for_reconciliation = (now_madrid().date() - timedelta(days=config.FEATURE_RETENTION_DAYS)).isoformat()
        reconciliation = canonical_materialization_reconciliation(
            cutoff_for_reconciliation, config.STARTUP_MAX_HISTORICAL_RUNS
        )
        result["canonical_reconciliation"] = reconciliation
        missing_rows = int(reconciliation.get("missing_rows") or 0)
        expected_rows = int(reconciliation.get("expected_rows") or 0)
        reconciled_rows = int(reconciliation.get("actual_rows") or 0)
        if data_daily_closes > 0 and alerts_daily_after_import == 0:
            materialization_status = "ERROR"
            materialization_reason = "Data Bot tiene cierres deep, pero Alerts no pudo convertirlos en observaciones."
        elif missing_rows > 0:
            materialization_status = "ERROR"
            materialization_reason = f"Faltan {missing_rows} filas Scanner del cierre deep en Alerts."
        elif data_daily_closes != alerts_daily_after_import:
            materialization_status = "WARN"
            materialization_reason = "La cobertura diaria de Data y Alerts no coincide todavía."
        elif alerts_daily_after_import > 0:
            materialization_status = "OK"
            materialization_reason = None
        else:
            materialization_status = "WAIT"
            materialization_reason = "Todavía no existe un cierre deep."
        emit(
            "A04F", materialization_status, "FULL DEEP · Scanner y GEX granular",
            expected={
                "daily_dates": data_daily_closes,
                "scanner_rows": expected_rows,
                "rule": "100% del Scanner; GEX puede faltar sin excluir ticker",
            },
            found={
                "live_deep_runs_read": imported_runs,
                "rows_upserted_now": imported_rows,
                "alerts_daily_dates": alerts_daily_after_import,
                "alerts_scanner_rows": reconciled_rows,
                "alerts_gex_rows": reconciliation.get("gex_rows", 0),
                "missing_scanner_rows": missing_rows,
                "incomplete_runs": reconciliation.get("incomplete_runs", []),
                "archive_rows_inserted": archive_features,
            },
            action=(
                "Scanner diario completo conciliado; los ticker sin GEX permanecen como Scanner-only."
                if materialization_status == "OK"
                else (
                    "Seguir acumulando un cierre FULL DEEP por sesión; CORE puede operar de forma independiente."
                    if materialization_status == "WAIT"
                    else "Bloquear únicamente la capa afectada; reparar/reimportar el deep incompleto y reintentar."
                )
            ),
            reason=materialization_reason,
        )

        result["prices_synced"] = sync_live_price_cache()
        canonical_dates = latest_canonical_daily_dates(1)
        full_graph_date = canonical_dates[-1] if canonical_dates else None
        core_graph_date = core_materialization.get("end_date")
        graph_date = max(
            [day for day in (full_graph_date, core_graph_date, data_history.get("coverage_end_date")) if day],
            default=None,
        )
        if config.RELATIONSHIP_REFRESH_ON_START:
            result["relationship_graph"] = refresh_relationship_graph(force=True, effective_date=graph_date)
        else:
            result["relationship_graph"] = {}
        result["relational_backfill"] = backfill_relational_feature_history(config.STARTUP_MAX_HISTORICAL_RUNS)
        graph = result.get("relationship_graph") or {}
        graph_status = "OK" if graph.get("relationships", 0) else ("ERROR" if alerts_daily_after_import > 0 else "WAIT")
        emit(
            "A05", graph_status, "Contexto mercado/ETF/sector",
            expected=f"Todo ticker del Scanner como nodo; relaciones ETF/leveraged y dinámicas a fecha {graph_date or 'del último deep'}, sin información futura",
            found={
                "effective_date": graph.get("effective_date"),
                "scanner_nodes": graph.get("nodes", 0),
                "tickers_with_edges": graph.get("tickers", 0),
                "edges": graph.get("relationships", 0),
                "structural_etf_leveraged_edges": graph.get("structural_edges", 0),
                "dynamic_edges": graph.get("with_correlation", 0),
                "nodes_csv": graph.get("nodes_csv"),
                "edges_csv": graph.get("edges_csv"),
            },
            action=(
                "Usar el Scanner completo como grafo vivo; TSLA↔TSLL/TSLQ y pares broad/sector se firman por polaridad; correlación/lead-lag se aprende con cierres."
                if graph.get("relationships", 0)
                else "Revisar A04 si había datos; sin nodos no existe contexto relacional."
            ),
            reason=(None if graph.get("with_correlation", 0) else "Aún no hay lookback suficiente para relaciones dinámicas."),
        )

        result["outcomes_materialized"] = materialize_outcomes()
        coverage_before = history_coverage()
        result["coverage_before_optimizer"] = coverage_before
        any_outcomes = bool(
            coverage_before.get("core_outcome_dates", 0)
            or coverage_before.get("outcome_dates", 0)
        )
        emit(
            "A06", "OK" if any_outcomes else "WAIT", "Outcomes de 1 a 5 sesiones",
            expected="Etiquetas futuras completas y conocidas, sin mirar hacia delante",
            found={
                "core_dates": coverage_before.get("core_feature_dates", 0),
                "core_outcome_dates": coverage_before.get("core_outcome_dates", 0),
                "core_outcome_rows": coverage_before.get("core_outcome_rows", 0),
                "full_deep_dates": coverage_before.get("feature_dates", 0),
                "full_outcome_dates": coverage_before.get("outcome_dates", 0),
                "full_outcome_rows": coverage_before.get("outcome_rows", 0),
            },
            action=("Etiquetas disponibles para walk-forward." if any_outcomes else "Esperar sesiones futuras; no fabricar resultados."),
        )

        daily_closes = int(coverage_before.get("feature_dates") or 0)
        target_closes = int(config.DAILY_CLOSE_WINDOW_SESSIONS)
        core_dates_materialized = int(coverage_before.get("core_feature_dates") or 0)
        core_target = int(config.CORE_HISTORY_TARGET_SESSIONS)
        core_outcome_dates = int(coverage_before.get("core_outcome_dates") or 0)
        full_outcome_dates = int(coverage_before.get("outcome_dates") or 0)

        full_consistent = bool(
            manifest_pipeline_safe
            and data_daily_closes == daily_closes
            and int(result.get("canonical_reconciliation", {}).get("missing_rows") or 0) == 0
        )
        core_data_dates = int(core_materialization.get("date_count") or 0)
        startup_nonfatal_limit = max(0, int(getattr(config, "CORE_MATERIALIZATION_MAX_NONFATAL_ERRORS", 25)))
        startup_source_date_values = list(core_materialization.get("dates") or [])
        startup_feature_date_values = [row["date"] for row in core_feature_date_inventory()]
        date_reconciliation = reconcile_core_dates(
            source_dates=len(startup_source_date_values) or core_data_dates,
            feature_dates=len(startup_feature_date_values) or core_dates_materialized,
            target_dates=core_target,
            source_end_date=(startup_source_date_values[-1] if startup_source_date_values else core_materialization.get("end_date")),
            feature_end_date=(startup_feature_date_values[-1] if startup_feature_date_values else coverage_before.get("core_feature_end_date")),
            errors=int(core_materialization.get("errors") or 0),
            nonfatal_limit=startup_nonfatal_limit,
            source_date_values=startup_source_date_values,
            feature_date_values=startup_feature_date_values,
        )
        result["core_date_reconciliation"] = date_reconciliation
        normalized_core_data_dates = core_dates_materialized if date_reconciliation.get("complete") else core_data_dates
        reconciliation = classify_layer_reconciliation(
            core_data_dates=normalized_core_data_dates,
            core_alert_dates=core_dates_materialized,
            core_errors=0 if date_reconciliation.get("complete") else int(core_materialization.get("errors") or 0),
            full_consistent=full_consistent,
            core_universe_complete=core_universe_complete,
        )
        if reconciliation.get("status") in {"OK", "WARN"} and (core_universe_warning or date_reconciliation.get("warning")):
            reconciliation["status"] = "WARN"
            reconciliation["reason"] = (
                "CORE utilizable por ventana/universo equivalentes; se mantiene SHADOW hasta converger metadata local."
            )
            reconciliation["action"] = "Calibrar CORE en SHADOW; no habilitar live con reconciliación tolerada."
        core_consistent = bool(reconciliation["core_consistent"])
        core_available = bool(reconciliation["core_available"])
        reconciliation_status = str(reconciliation["status"])
        emit(
            "A07", reconciliation_status, "Conciliación por capas",
            expected={
                "core": f"{core_data_dates} fechas Data = Alerts",
                "full_deep": f"{data_daily_closes} fechas Data = Alerts",
            },
            found={
                "core_data": core_data_dates,
                "core_alerts": core_dates_materialized,
                "core_empty_equal": bool(core_data_dates == 0 and core_dates_materialized == 0),
                "core_universe_tickers": source_universe_tickers,
                "core_eligible_tickers": int(core_materialization.get("complete_tickers") or 0),
                "core_excluded_tickers": source_excluded_tickers,
                "core_pending_tickers": effective_pending_tickers,
                "core_remaining_tickers": effective_pending_tickers,
                "core_universe_complete": core_universe_complete,
                "core_authoritative_reconciliation": universe_reconciliation.get("authoritative_reconciliation", False),
                "core_date_drift": date_reconciliation.get("drift", 0),
                "core_date_end_match": date_reconciliation.get("end_match", False),
                "full_data": data_daily_closes,
                "full_alerts": daily_closes,
                "full_missing_rows": int(result.get("canonical_reconciliation", {}).get("missing_rows") or 0),
            },
            action=str(reconciliation["action"]),
            reason=reconciliation.get("reason"),
        )

        core_eligible = bool(
            manifest_complete
            and core_consistent
            and core_universe_complete
            and core_dates_materialized >= core_target
            and core_outcome_dates >= int(config.CORE_HISTORY_MIN_FULLY_LABELED_DATES)
        )
        full_eligible = bool(
            full_consistent
            and daily_closes >= target_closes
            and full_outcome_dates >= int(config.WALKFORWARD_MIN_FULLY_LABELED_DATES)
        )
        core_model_id = None
        full_model_id = None
        core_outcome_fingerprint = _outcome_fingerprint(coverage_before, prefix="core_")
        core_engine = _engine_signature()
        core_already_current = bool(
            _engine_matches(get_runtime_state("last_core_calibration_engine", ""), core_engine)
            and str(get_runtime_state("last_core_calibrated_outcomes", "") or "") == core_outcome_fingerprint
        )
        if core_already_current:
            log_info(
                "⏭️ [STARTUP CORE CALIBRATION SKIP] reason=no_new_mature_outcomes "
                f"engine={core_engine} fingerprint={core_outcome_fingerprint}",
                send_discord=False,
            )
        if (
            config.STARTUP_CALIBRATE_ENABLED
            and config.CORE_HISTORY_AUTO_CALIBRATE
            and core_eligible
            and not core_already_current
        ):
            result["core_optimizer_attempted"] = True
            transition_pipeline_state(
                "CALIBRATING", stage="startup_core_walkforward",
                generation=int(data_history.get("_coordination_generation") or 0), entry_mode="off",
            )
            core_model_id = run_optimizer(startup=True, calibration_layer="core")
            result["core_optimizer_completed"] = True
            activate_due_model()
            set_runtime_state(
                "last_core_recalibration_signature",
                build_core_calibration_signature(
                    universe_fingerprint=source_universe_fingerprint,
                    excluded_fingerprint=source_excluded_fingerprint,
                    end_date=str(core_materialization.get("end_date") or data_history.get("core_history_end_date") or ""),
                    feature_dates=core_dates_materialized,
                    outcome_dates=core_outcome_dates,
                    outcome_rows=int(coverage_before.get("core_outcome_rows") or 0),
                ),
            )
            set_runtime_state("last_core_calibration_engine", core_engine)
            set_runtime_state("last_core_calibrated_outcomes", core_outcome_fingerprint)
        if config.STARTUP_CALIBRATE_ENABLED and full_eligible:
            full_model_id = run_optimizer(startup=True, calibration_layer="full")
            activate_due_model()
            set_runtime_state("last_full_calibration_engine", "FULL")
            set_runtime_state(
                "last_full_calibrated_outcomes",
                "|".join([
                    str(int(coverage_before.get("outcome_rows") or 0)),
                    str(int(coverage_before.get("outcome_dates") or 0)),
                    str(coverage_before.get("outcome_start_date") or ""),
                    str(coverage_before.get("outcome_end_date") or ""),
                ]),
            )
        result["core_optimizer_model_id"] = core_model_id
        result["full_optimizer_model_id"] = full_model_id

        if full_eligible:
            calibration_status = "OK"
            calibration_reason = None
            calibration_action = "Backtest FULL DEEP ejecutado; compite contra el champion CORE/FULL."
        elif core_eligible:
            calibration_status = "OK"
            calibration_reason = None
            calibration_action = "Backtest CORE ejecutado; FULL DEEP e intradía siguen acumulándose."
        else:
            calibration_status = "WAIT" if core_consistent else "ERROR"
            if not core_consistent:
                calibration_reason = "CORE Data/Alerts inconsistente."
            elif not core_available:
                calibration_reason = "CORE Data/Alerts vacío pero conciliado (0=0)."
            elif not manifest_complete:
                calibration_reason = "Data Bot todavía no ha publicado el manifest final de este reinicio; el watcher entrenará en cuanto aparezca."
            elif not core_universe_complete:
                calibration_reason = (
                    f"Universo CORE sin resolver: eligible={core_materialization.get('complete_tickers', 0)} · "
                    f"excluded={source_excluded_tickers} · pending={effective_pending_tickers}/"
                    f"{source_universe_tickers}; no se autoriza backtest ni champion."
                )
            elif core_dates_materialized < core_target:
                calibration_reason = f"CORE TMAD {core_dates_materialized}/{core_target}."
            else:
                calibration_reason = (
                    f"Outcomes CORE conocibles {core_outcome_dates}/"
                    f"{config.CORE_HISTORY_MIN_FULLY_LABELED_DATES}."
                )
            calibration_action = "Mantener bootstrap shadow; completar CORE y outcomes sin usar futuro."

        emit(
            "A08", calibration_status, "Backtesting y elección de champion",
            expected="CORE 30 sesiones + walk-forward purgado; FULL DEEP e intradía como challengers posteriores",
            found={
                "core_dates": core_dates_materialized,
                "core_target": core_target,
                "core_universe_tickers": source_universe_tickers,
                "core_eligible_tickers": int(core_materialization.get("complete_tickers") or 0),
                "core_excluded_tickers": source_excluded_tickers,
                "core_pending_tickers": effective_pending_tickers,
                "core_remaining_tickers": effective_pending_tickers,
                "core_universe_complete": core_universe_complete,
                "core_outcome_dates": core_outcome_dates,
                "core_executed": bool(core_model_id),
                "full_deep_dates": daily_closes,
                "full_deep_target": target_closes,
                "full_outcome_dates": full_outcome_dates,
                "full_executed": bool(full_model_id),
            },
            action=calibration_action,
            reason=calibration_reason,
        )

        active = get_active_model()
        result["active_model"] = {
            "id": active.get("id"),
            "model_key": active.get("model_key"),
            "status": active.get("status"),
            "effective_from_date": active.get("effective_from_date"),
            "train_sample": active.get("train_sample"),
            "validation_sample": active.get("validation_sample"),
        }
        result["coverage"] = history_coverage()
        # A champion trained before the complete-universe gate is not allowed to
        # reactivate live alerts while the current Scanner universe is partial.
        champion_ready = bool(
            active.get("status") == "champion"
            and core_consistent
            and core_universe_complete
            and manifest_complete
            and not core_universe_warning
            and not bool(date_reconciliation.get("warning"))
        )
        operational = decide_operational_mode(
            champion_ready=champion_ready,
            core_consistent=core_consistent,
            manifest_ok=manifest_pipeline_safe,
        )
        shadow_ready = bool(operational["shadow_ready"])
        entry_mode = str(operational["entry_mode"])
        set_runtime_state("entry_mode", entry_mode)
        result["status"] = str(operational["status"])
        result["entries_enabled"] = bool(champion_ready or shadow_ready)
        result["live_alerts_enabled"] = bool(champion_ready)
        result["shadow_scoring_enabled"] = bool(shadow_ready)
        result["entry_mode"] = entry_mode
        final_pipeline_state = readiness_state(entry_mode, safe=bool(champion_ready or shadow_ready))
        state_payload = transition_pipeline_state(
            final_pipeline_state, stage="startup_complete",
            generation=int(data_history.get("_coordination_generation") or 0),
            entry_mode=entry_mode,
            error_code=(None if final_pipeline_state != "ERROR" else "STARTUP_RECONCILIATION"),
            model_id=active.get("id"),
            reason=(None if final_pipeline_state != "ERROR" else "A07/A08/A09 no conciliado"),
            details={
                "core_dates": core_dates_materialized, "core_outcomes": core_outcome_dates,
                "full_dates": daily_closes, "warning": bool(core_universe_warning or date_reconciliation.get("warning")),
            },
        )
        result["global_state"] = state_payload["state"]
        result["core_reconciliation_warning"] = bool(core_universe_warning or date_reconciliation.get("warning"))
        result["note"] = (
            "Champion validado: alertas live habilitadas." if champion_ready
            else (
                "Sin champion todavía: pipeline completo activo en modo PRELIMINAR/SHADOW."
                if shadow_ready else
                "Pipeline no conciliado: procesamiento bloqueado hasta reparar la conciliación."
            )
        )
        emit(
            "A09", "OK" if (champion_ready or shadow_ready) else "ERROR",
            "Estado operativo del Alerts Bot",
            expected="Procesar cada run en live validado o shadow provisional; bloquear solo si falla el pipeline",
            found={
                "mode": result["status"],
                "entry_mode": entry_mode,
                "model": active.get("model_key"),
                "model_status": active.get("status"),
                "entries_enabled": result["entries_enabled"],
                "core_universe_complete": core_universe_complete,
            },
            action=(
                "Procesar entradas, updates y salidas VALIDADA." if champion_ready
                else (
                    "Procesar entradas, updates y salidas PRELIMINARES; guardar performance para calibración futura."
                    if shadow_ready else
                    "No procesar entradas; reparar A04/A07."
                )
            ),
        )

        if config.SEND_BACKTEST_RESULTS_ON_START:
            report = build_performance_report("startup_backtest")
            result["backtest_summary"] = report.get("summary")

        missing = max(0, target_closes - daily_closes)
        core_missing = max(0, core_target - core_dates_materialized)
        pipeline_errors = [s.get("code") for s in result["stages"] if str(s.get("status")).upper() == "ERROR"]
        active_tier = str((active.get("params") or {}).get("feature_tier") or "BOOTSTRAP")
        calibration_message = build_group(
            "🧪 ALERTS · ESTADO DE CALIBRACIÓN",
            [
                "\n".join([
                    "### Estado operativo",
                    f"**Pipeline:** {state_payload['state']} · **Generación:** `{int(data_history.get('_coordination_generation') or 0)}`",
                    f"**Live:** {'ON' if champion_ready else 'OFF'} · **Research/Shadow:** {'ON' if shadow_ready else 'OFF'}",
                ]),
                "\n".join([
                    "### CORE",
                    f"**Sesiones:** `{core_dates_materialized}/{core_target}` · **Outcomes:** `{core_outcome_dates}`",
                    f"**Backtest:** {'EJECUTADO' if core_eligible else 'PENDIENTE'}",
                    f"**Universo:** `{int(core_materialization.get('complete_tickers') or 0)} elegibles + {source_excluded_tickers} excluidos / {source_universe_tickers}` · pendientes `{effective_pending_tickers}`",
                ]),
                "\n".join([
                    "### GEX",
                    f"**Diario:** `{daily_closes}/{target_closes}` · **Intradía:** `{intraday_status.get('available_sessions', 0)}/{intraday_status.get('required_sessions', 30)}`",
                ]),
            ],
            footer="El diagnóstico técnico completo queda guardado localmente.",
        )
        calibration_delivered = send_daily(calibration_message)

        if final_pipeline_state == "ERROR":
            operational_message = build_group(
                "🟥 ALERTS · ERROR",
                [
                    "\n".join([
                        "### Pipeline bloqueado",
                        f"**Generación:** `{int(data_history.get('_coordination_generation') or 0)}`",
                        f"**Códigos:** `{', '.join(pipeline_errors) or 'STARTUP_RECONCILIATION'}`",
                        "No se generan señales ni se publican candidatos hasta reparar la conciliación.",
                    ])
                ],
                footer="El diagnóstico técnico completo queda guardado localmente.",
            )
        else:
            operational_message = build_group(
                f"✅ ALERTS · {'LIVE LISTO' if champion_ready else 'SHADOW LISTO'}",
                ["\n".join([
                    f"**Generación:** `{int(data_history.get('_coordination_generation') or 0)}` · **Modo:** `{'LIVE' if champion_ready else 'SHADOW'}`.",
                    f"**CORE:** `{core_dates_materialized}` sesiones · outcomes `{core_outcome_dates}`.",
                    f"**Canales:** Live `{'ON' if champion_ready else 'OFF'}` · Research `{'ON' if shadow_ready else 'OFF'}`.",
                    "**Publicación:** Research = shortlist preliminar · Live = ENTER / UPDATE / CLOSE validados.",
                ])],
            )
        log_delivered = log_info(operational_message, send_discord=True)
        result["discord_delivery"] = {
            "alerts_status": bool(log_delivered),
            "calibration": bool(calibration_delivered),
        }

        result["finished_at_madrid"] = now_madrid().isoformat()
        _write_status(result)
        log_info(
            f"✅ [A10] Startup terminado | mode={result['status']} | core={core_dates_materialized}/{core_target} "
            f"| full={daily_closes}/{target_closes} | core_outcomes={core_outcome_dates} | entry_mode={entry_mode}", 
            send_discord=False,
        )
        return result
    except Exception as exc:
        transition_pipeline_state(
            "ERROR", stage="startup_exception", entry_mode="off",
            error_code=type(exc).__name__, reason=str(exc),
        )
        result["status"] = "error"
        result["entries_enabled"] = False
        result["note"] = str(exc)
        result["traceback"] = traceback.format_exc()[-4000:]
        result["finished_at_madrid"] = now_madrid().isoformat()
        _write_status(result)
        payload = record_stage(
            "A99", "ERROR", "Fallo fatal del warm-up",
            expected="Completar todas las etapas sin habilitar entradas prematuramente",
            found=type(exc).__name__, action="Mantener entradas bloqueadas.", reason=str(exc),
        )
        log_error(f"{stage_line(payload)}\n```{traceback.format_exc()[-1800:]}```")
        return result
