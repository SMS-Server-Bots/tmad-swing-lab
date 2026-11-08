from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.insert(0, str(PROGRAM_DIR))

PRODUCTION_ROOT = PROGRAM_DIR.parents[1]


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _production_version() -> str:
    try:
        return (PRODUCTION_ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip() or "n/a"
    except Exception:
        return "n/a"


def _execution_webhook() -> str:
    merged: dict[str, str] = {}
    config_dir = PRODUCTION_ROOT / "02_CONFIG"
    for path in (config_dir / "configuracion_canales.env", config_dir / "tmad_execution.secret.env"):
        merged.update(_read_env_file(path))
    return str(merged.get("EXECUTION_WEBHOOK") or os.getenv("EXECUTION_WEBHOOK") or "").strip()


def _discord_send(message: str) -> bool:
    url = _execution_webhook()
    if not url:
        return False
    payload = json.dumps({"content": str(message)[:1950]}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "TMAD-Historical-Replay/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
        return True
    except Exception:
        return False


def _duration_text(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


_PHASE_LOCK = threading.Lock()
_REPLAY_PHASE = {"phase": "STARTING", "changed_monotonic": time.monotonic(), "detail": ""}
_REPLAY_PHASE_TIMINGS: dict[str, float] = {}
_REPLAY_PHASE_HISTORY: list[dict] = []
_WATCHDOG_WARN_SECONDS = 90 * 60


def _set_replay_phase(phase: str, detail: str = "") -> None:
    now_text = ts_madrid_str()
    now_monotonic = time.monotonic()
    with _PHASE_LOCK:
        previous_phase = str(_REPLAY_PHASE.get("phase") or "")
        previous_detail = str(_REPLAY_PHASE.get("detail") or "")
        previous_changed = float(_REPLAY_PHASE.get("changed_monotonic") or now_monotonic)
        previous_elapsed = max(0.0, now_monotonic - previous_changed)
        if previous_phase and previous_phase not in {"STARTING", "COMPLETE", "ERROR", "REUSED"}:
            _REPLAY_PHASE_TIMINGS[previous_phase] = (
                float(_REPLAY_PHASE_TIMINGS.get(previous_phase) or 0.0)
                + previous_elapsed
            )
            _REPLAY_PHASE_HISTORY.append({
                "phase": previous_phase,
                "detail": previous_detail,
                "elapsed_seconds": round(previous_elapsed, 3),
                "finished_at_madrid": now_text,
            })
        _REPLAY_PHASE["phase"] = str(phase)
        _REPLAY_PHASE["detail"] = str(detail)
        _REPLAY_PHASE["changed_monotonic"] = now_monotonic
    payload = {
        "phase": str(phase),
        "detail": str(detail),
        "updated_at_madrid": now_text,
        "pid": os.getpid(),
    }
    try:
        set_runtime_state("historical_replay_phase", json.dumps(payload, ensure_ascii=False))
        set_runtime_state("historical_replay_heartbeat_at_madrid", now_text)
    except Exception:
        pass
    print(f"[HISTORICAL REPLAY] phase={phase} detail={detail}", flush=True)


def _phase_timings_snapshot(total_elapsed_seconds: float | None = None) -> dict:
    with _PHASE_LOCK:
        phase_seconds = {
            str(name): round(float(seconds), 3)
            for name, seconds in _REPLAY_PHASE_TIMINGS.items()
        }
        history = [dict(item) for item in _REPLAY_PHASE_HISTORY]
    top = sorted(
        (
            {"phase": name, "elapsed_seconds": seconds}
            for name, seconds in phase_seconds.items()
        ),
        key=lambda item: float(item.get("elapsed_seconds") or 0.0),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "total_elapsed_seconds": (
            round(float(total_elapsed_seconds), 3)
            if total_elapsed_seconds is not None
            else None
        ),
        "phase_seconds": phase_seconds,
        "top_phases": top[:12],
        "transitions": history[-200:],
    }


def _write_replay_performance_sidecar(payload: dict) -> Path:
    target = Path(config.OUTPUT_DIR) / "historical_validation" / "latest_historical_replay_performance.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def _top_phase_text(result: dict, limit: int = 4) -> str:
    timings = result.get("phase_timings") or {}
    items = list(timings.get("top_phases") or [])[:max(1, int(limit))]
    if not items:
        return "n/a"
    return " · ".join(
        f"{item.get('phase','?')} {_duration_text(float(item.get('elapsed_seconds') or 0.0))}"
        for item in items
    )


def _watchdog_loop(stop_event: threading.Event) -> None:
    warned_for = ""
    while not stop_event.wait(300):
        with _PHASE_LOCK:
            phase = str(_REPLAY_PHASE.get("phase") or "UNKNOWN")
            detail = str(_REPLAY_PHASE.get("detail") or "")
            age = max(
                0.0,
                time.monotonic() - float(_REPLAY_PHASE.get("changed_monotonic") or time.monotonic()),
            )
        print(
            f"[HISTORICAL REPLAY HEARTBEAT] pid={os.getpid()} phase={phase} "
            f"phase_age_min={age / 60.0:.1f} detail={detail}",
            flush=True,
        )
        if age >= _WATCHDOG_WARN_SECONDS and warned_for != phase:
            warned_for = phase
            _discord_send(
                "⚠️ **Reconstrucción histórica TMAD sigue viva pero lenta.**\n"
                f"PID: {os.getpid()} · fase: {phase} · sin checkpoint: {_duration_text(age)}\n"
                "Producción continúa operativa. El watchdog no mata el replay; sólo avisa."
            )


def _fmt(value: object, digits: int = 2) -> str:
    try:
        number = float(value)
    except Exception:
        return "n/a"
    return f"{number:.{digits}f}"

def _safe_float(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _completion_message(result: dict, elapsed_seconds: float) -> str:
    operational = result.get("operational_validation") or {}
    product = operational.get("product_replay") or {}
    current_book = product.get("current_policy_counterfactual") or {}
    emitted_book = product.get("historical_emitted_book") or {}
    metrics = current_book.get("metrics") or {}
    diagnostics = current_book.get("diagnostics") or {}
    compression = current_book.get("selection_compression") or {}
    overlap = current_book.get("reverse_trade_overlap") or {}
    reverse = operational.get("reverse_stress") or {}
    threshold = reverse.get("threshold_5pct_within_5_sessions") or {}
    reverse_cache = reverse.get("canonical_cache") or {}
    reverse_data_quality = reverse.get("data_quality") or {}
    trusted_lane = reverse_data_quality.get("trusted_lane") or {}
    return_math_sanity = reverse.get("return_math_sanity") or {}
    option_proxy = metrics.get("synthetic_option_proxy") or {}
    challengers = operational.get("research_challengers") or {}
    enter_first = challengers.get("actionable_enter_first_top5") or {}
    session_fallback = challengers.get("market_session_fallback") or {}
    quality_enter = challengers.get("quality_gated_enter_top5") or {}
    combined = challengers.get("actionable_enter_first_plus_market_session_fallback") or {}
    entry_quality = challengers.get("entry_quality_walkforward") or {}
    entry_quality_cross = challengers.get("entry_quality_cross_sectional_walkforward") or {}
    entry_eligibility = challengers.get("entry_quality_eligibility_walkforward") or {}
    eligibility_fixed = entry_eligibility.get("fixed_horizon_crosscheck_v4") or {}
    eligibility_falsification = challengers.get("entry_eligibility_historical_falsification") or {}
    eligibility_falsification_rolling = eligibility_falsification.get("rolling_latest") or {}
    eligibility_falsification_fixed = eligibility_falsification.get("fixed_benchmark") or {}
    eligibility_falsification_decision = eligibility_falsification.get("decision") or {}
    economic_waterfall = challengers.get("economic_waterfall_calibration") or {}
    economic_waterfall_rolling = economic_waterfall.get("rolling_latest") or {}
    economic_waterfall_fixed = economic_waterfall.get("fixed_benchmark") or {}
    economic_waterfall_decision = economic_waterfall.get("decision") or {}
    economic_architecture = challengers.get("economic_architecture_challengers") or {}
    economic_architecture_rolling = economic_architecture.get("rolling_latest") or {}
    economic_architecture_fixed = economic_architecture.get("fixed_benchmark") or {}
    economic_architecture_decision = economic_architecture.get("decision") or {}
    frozen_config_sim = challengers.get("frozen_config_historical_simulation") or {}
    frozen_config_rolling = frozen_config_sim.get("rolling_latest") or {}
    frozen_config_fixed = frozen_config_sim.get("fixed_benchmark") or {}
    frozen_config_decision = frozen_config_sim.get("decision") or {}
    scanner_calibration = challengers.get("scanner_recall_ranking_calibration") or {}
    scanner_calibration_rolling = scanner_calibration.get("rolling_latest") or {}
    scanner_calibration_fixed = scanner_calibration.get("fixed_benchmark") or {}
    scanner_calibration_decision = scanner_calibration.get("decision") or {}
    scanner_core = challengers.get("scanner_core_interaction_simulation") or {}
    scanner_core_rolling = scanner_core.get("rolling_latest") or {}
    scanner_core_fixed = scanner_core.get("fixed_benchmark") or {}
    scanner_core_decision = scanner_core.get("decision") or {}
    scanner_direction_aware = challengers.get("scanner_direction_aware_ranking") or {}
    scanner_direction_aware_rolling = scanner_direction_aware.get("rolling_latest") or {}
    scanner_direction_aware_fixed = scanner_direction_aware.get("fixed_benchmark") or {}
    scanner_direction_aware_decision = scanner_direction_aware.get("decision") or {}
    gex_gate = challengers.get("gex_gate_calibration") or {}
    gex_gate_rolling = gex_gate.get("rolling_latest") or {}
    gex_gate_fixed = gex_gate.get("fixed_benchmark") or {}
    gex_gate_decision = gex_gate.get("decision") or {}
    momentum_direction = challengers.get("momentum_direction_audit") or {}
    momentum_attrition = challengers.get("momentum_edge_attrition") or {}
    thesis_quality = challengers.get("thesis_quality_completion_walkforward") or {}
    quality = operational.get("semantic_quality") or {}
    price_integrity = quality.get("price_path_integrity") or {}
    sampled = not bool(result.get("history_not_truncated"))
    sample_note = " · attribution sample truncado" if sampled else ""
    lifecycle = result.get("lifecycle_research") or {}
    lifecycle_current = (
        lifecycle.get("current_policy")
        or ((lifecycle.get("historical_alpha_ledgers") or {}).get("current_policy") or {})
    )
    research_validity = result.get("research_validity") or {}
    swing_v2 = result.get("swing_v2") or {}
    swing_v2_hist = swing_v2.get("historical_gate") or {}
    swing_v2_forward = swing_v2.get("prospective_audit") or {}
    momentum_reconciliation = result.get("momentum_reconciliation") or {}
    trusted_momentum_reconciliation = momentum_reconciliation.get("trusted_point_in_time") or {}
    fixed_benchmark_momentum = momentum_reconciliation.get("fixed_benchmark_trusted_point_in_time") or {}
    research_benchmark = result.get("research_benchmark") or {}
    research_feature_benchmark = result.get("research_feature_benchmark") or {}
    replay_mode = str(result.get("replay_mode") or "FULL")
    attribution_cache_mode = str(result.get("attribution_cache_mode") or "MISS")
    lines = [
        "✅ **Reconstrucción histórica TMAD completada y validada.**",
        f"Versión: {result.get('producer_version') or 'n/a'} · duración: {_duration_text(elapsed_seconds)} · modo: {replay_mode} · attribution: {attribution_cache_mode} · semantic quality: {quality.get('status') or 'n/a'}",
        f"Replay fases: {_top_phase_text(result)}",
        (
            "Adaptive research: "
            + str((result.get("adaptive_research_auto_launch") or {}).get("status") or "n/a")
            + (
                " · PID "
                + str((result.get("adaptive_research_auto_launch") or {}).get("pid"))
                if (result.get("adaptive_research_auto_launch") or {}).get("pid")
                else ""
            )
        ),
        f"Current-policy: {diagnostics.get('selected_dates', 'n/a')} fechas · FUSION ENTER {diagnostics.get('fusion_enter_actionable', 'n/a')} · Top-5 ENTER {compression.get('selected_actionable_enter_occurrences', 'n/a')} · OPEN {metrics.get('opened', 'n/a')} · cerradas {metrics.get('closed', 'n/a')}",
        f"Subyacente: win rate {_fmt(metrics.get('win_rate_pct'))}% · retorno medio {_fmt(metrics.get('avg_underlying_return_pct'))}% · PF {_fmt(metrics.get('underlying_profit_factor'))}",
        f"Opciones proxy: {option_proxy.get('evaluable_trades', 'n/a')} evaluables ({option_proxy.get('normalized_synthetic_trades', 'n/a')} normalizadas) · retorno medio {_fmt(option_proxy.get('avg_return_pct'))}% · PF {_fmt(option_proxy.get('profit_factor'))}",
        f"Reverse limpio ≥5%/5D: {threshold.get('events', 'n/a')} oportunidades · capturadas {threshold.get('captured', 'n/a')} · OPENs que alcanzan ≥5%: {overlap.get('trades_reaching_5pct_within_5_sessions', 'n/a')} · matching errors: {overlap.get('threshold_matching_consistency_errors', 'n/a')}",
        f"Reverse RAW vs limpio: {reverse_data_quality.get('raw_threshold_events', 'n/a')} → {reverse_data_quality.get('trusted_threshold_events', 'n/a')} · cuarentena {reverse_data_quality.get('threshold_events_quarantined', 'n/a')}",
        f"Reverse canónico: {reverse_cache.get('mode', 'n/a')} · base {str((reverse_cache.get('basis') or {}).get('fingerprint') or 'n/a')[:12]} · Top hindsight {reverse_cache.get('top_opportunities', 'n/a')}",
        f"Return math: {return_math_sanity.get('formula_version', 'n/a')} · SHORT >100%: {return_math_sanity.get('short_top_return_over_100', 'n/a')}",
        f"Integridad precios RAW: OHLC inválidos {price_integrity.get('invalid_ohlc_rows', 'n/a')} · saltos ≥80% {(price_integrity.get('observed_close_jumps') or {}).get('ge_80pct', 'n/a')} · metadata ajuste {_fmt(price_integrity.get('adjustment_metadata_coverage_pct'))}%",
        f"Lane limpia: OHLC→close {trusted_lane.get('invalid_ohlc_downgraded_to_close', 'n/a')} · barreras continuidad {trusted_lane.get('continuity_barriers', 'n/a')} · estado {quality.get('data_quality_status', 'n/a')}",
        f"Libro emitido histórico: {emitted_book.get('trades', 'n/a')} operaciones (separado del contrafactual actual).",
        f"Attribution lab: {result.get('history_dates') or 'n/a'} fechas · {result.get('mature_rows') or 'n/a'} filas{sample_note}.",
        f"Lifecycle boundary: {lifecycle_current.get('evaluable', 'n/a')} evaluables · completion {lifecycle_current.get('movement_completed', 'n/a')} · invalidación {lifecycle_current.get('thesis_invalidated', 'n/a')} · PF {_fmt(lifecycle_current.get('boundary_profit_factor'))}.",
        f"TIME_STOP challenger: {((lifecycle_current.get('time_stop_counterfactual') or {}).get('current_time_stop_trades', 'n/a'))} actuales · completion posterior {((lifecycle_current.get('time_stop_counterfactual') or {}).get('later_completed', 'n/a'))} · invalidación posterior {((lifecycle_current.get('time_stop_counterfactual') or {}).get('later_invalidated', 'n/a'))}.",
        f"Validez research: {research_validity.get('status') or 'n/a'} · operacional {'sí' if research_validity.get('operational_conclusion_allowed') else 'no'} · attribution {'sí' if research_validity.get('attribution_conclusion_allowed') else 'no'}.",
        f"Freeze T0: {(research_validity.get('snapshot') or {}).get('snapshot_id','n/a')} · producción avanzó durante replay: {'sí' if research_validity.get('live_inputs_advanced_during_replay') else 'no'}.",
        (
            "Swing V2 6.2: "
            f"hist={swing_v2_hist.get('status','n/a')} · "
            f"WF n={((swing_v2_hist.get('aggregate') or {}).get('sample','n/a'))} · "
            f"avg {_fmt(((swing_v2_hist.get('aggregate') or {}).get('avg_return_pct')))}% · "
            f"PF {_fmt(((swing_v2_hist.get('aggregate') or {}).get('sample_weighted_profit_factor')))} · "
            f"forward={swing_v2_forward.get('status','n/a')} n={((swing_v2_forward.get('metrics') or {}).get('sample','n/a'))} · "
            f"promotion={'SÍ' if swing_v2.get('promotion_ready') else 'NO'}"
        ),
        (
            "Momentum baseline slots v8: "
            f"2D all5 n={((((swing_v2.get('by_horizon') or {}).get('2') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('sample','n/a')} "
            f"avg={_fmt(((((swing_v2.get('by_horizon') or {}).get('2') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt(((((swing_v2.get('by_horizon') or {}).get('2') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('profit_factor'))}/"
            f"cap5 n={((((swing_v2.get('by_horizon') or {}).get('2') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('sample','n/a')} "
            f"avg={_fmt(((((swing_v2.get('by_horizon') or {}).get('2') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt(((((swing_v2.get('by_horizon') or {}).get('2') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('profit_factor'))} · "
            f"3D all5 n={((((swing_v2.get('by_horizon') or {}).get('3') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('sample','n/a')} "
            f"avg={_fmt(((((swing_v2.get('by_horizon') or {}).get('3') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt(((((swing_v2.get('by_horizon') or {}).get('3') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('profit_factor'))}/"
            f"cap5 n={((((swing_v2.get('by_horizon') or {}).get('3') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('sample','n/a')} "
            f"avg={_fmt(((((swing_v2.get('by_horizon') or {}).get('3') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt(((((swing_v2.get('by_horizon') or {}).get('3') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('profit_factor'))} · "
            f"5D all5 n={((((swing_v2.get('by_horizon') or {}).get('5') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('sample','n/a')} "
            f"avg={_fmt(((((swing_v2.get('by_horizon') or {}).get('5') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt(((((swing_v2.get('by_horizon') or {}).get('5') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('all_test_days_fixed_top5') or {}).get('profit_factor'))}/"
            f"cap5 n={((((swing_v2.get('by_horizon') or {}).get('5') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('sample','n/a')} "
            f"avg={_fmt(((((swing_v2.get('by_horizon') or {}).get('5') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt(((((swing_v2.get('by_horizon') or {}).get('5') or {}).get('momentum_baseline_slot_audit_v8') or {}).get('matched_daily_slots_capped_at_5') or {}).get('profit_factor'))}"
        ),
        (
            "Momentum reconciliation v12 trusted: "
            f"status={momentum_reconciliation.get('status','n/a')} · "
            f"slots-ref={'OK' if momentum_reconciliation.get('reference_reproduced_all_horizons') else 'FAIL'} · "
            f"waterfall-ref={'OK' if trusted_momentum_reconciliation.get('waterfall_reference_reproduced_all_horizons') else 'FAIL'} · "
            f"2D canonOOT {_fmt((((momentum_reconciliation.get('by_horizon') or {}).get('2') or {}).get('canonical_oot') or {}).get('avg_return_pct'))}%/"
            f"trustedSameDays {_fmt((((trusted_momentum_reconciliation.get('by_horizon') or {}).get('2') or {}).get('trusted_point_in_time_same_oot_days') or {}).get('avg_return_pct'))}% · "
            f"3D canonOOT {_fmt((((momentum_reconciliation.get('by_horizon') or {}).get('3') or {}).get('canonical_oot') or {}).get('avg_return_pct'))}%/"
            f"trustedSameDays {_fmt((((trusted_momentum_reconciliation.get('by_horizon') or {}).get('3') or {}).get('trusted_point_in_time_same_oot_days') or {}).get('avg_return_pct'))}% · "
            f"5D canonOOT {_fmt((((momentum_reconciliation.get('by_horizon') or {}).get('5') or {}).get('canonical_oot') or {}).get('avg_return_pct'))}%/"
            f"trustedSameDays {_fmt((((trusted_momentum_reconciliation.get('by_horizon') or {}).get('5') or {}).get('trusted_point_in_time_same_oot_days') or {}).get('avg_return_pct'))}%"
        ),
        (
            "Research benchmark fijo: "
            f"id={research_benchmark.get('benchmark_id','n/a')} · "
            f"status={research_benchmark.get('runtime_status','n/a')} · "
            f"anchor={((research_benchmark.get('anchor') or {}).get('first_date') or 'n/a')}→{((research_benchmark.get('anchor') or {}).get('last_date') or 'n/a')} · "
            f"runs={research_benchmark.get('operational_run_keys','n/a')} · "
            f"canon2/3/5={((research_benchmark.get('canonical_rows_by_horizon') or {}).get('2','n/a'))}/"
            f"{((research_benchmark.get('canonical_rows_by_horizon') or {}).get('3','n/a'))}/"
            f"{((research_benchmark.get('canonical_rows_by_horizon') or {}).get('5','n/a'))} · "
            f"fixed trusted status={fixed_benchmark_momentum.get('status','n/a')}"
        ),
        (
            "Entry Quality OOT v1: "
            f"{entry_quality.get('oot_scored_trades', 'n/a')} scored · "
            f"acepta {entry_quality.get('accepted_trades', 'n/a')} · "
            f"avg aceptadas {_fmt((entry_quality.get('accepted') or {}).get('avg_return_pct'))}% · "
            f"PF {_fmt((entry_quality.get('accepted') or {}).get('profit_factor'))} · "
            f"lift {_fmt(entry_quality.get('accepted_avg_return_lift_vs_all_pp'))}pp"
        ),
        (
            "Entry Quality cross-sectional v2: "
            f"pool resuelto {entry_quality_cross.get('candidate_pool_resolved', 'n/a')} · "
            f"test {entry_quality_cross.get('test_dates', 'n/a')}d · "
            f"crowded {entry_quality_cross.get('crowded_test_dates', 'n/a')}d · "
            f"lift crowded {_fmt((((entry_quality_cross.get('crowded_dates_only') or {}).get('lift') or {}).get('avg_return_lift_pp')))}pp · "
            f"PF challenger {_fmt((((entry_quality_cross.get('crowded_dates_only') or {}).get('challenger_cross_sectional_rank') or {}).get('profit_factor')))}"
        ),
        (
            "Entry Eligibility OOT v3: "
            f"test {entry_eligibility.get('test_dates', 'n/a')}d · "
            f"selecciona {entry_eligibility.get('selected_trades', 'n/a')} · "
            f"abstiene {entry_eligibility.get('dates_abstained_completely', 'n/a')}d · "
            f"avg {_fmt((entry_eligibility.get('eligibility_v3_variable_n') or {}).get('avg_return_pct'))}% · "
            f"PF {_fmt((entry_eligibility.get('eligibility_v3_variable_n') or {}).get('profit_factor'))} · "
            f"lift vs v2 {_fmt((entry_eligibility.get('eligibility_lift_vs_v2') or {}).get('avg_return_lift_pp'))}pp"
        ),
        (
            "Eligibility fixed-horizon v4: "
            f"2D n={((eligibility_fixed.get('by_horizon') or {}).get('2') or {}).get('eligibility_v3_variable_n', {}).get('sample', 'n/a')} "
            f"avg={_fmt((((eligibility_fixed.get('by_horizon') or {}).get('2') or {}).get('eligibility_v3_variable_n') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt((((eligibility_fixed.get('by_horizon') or {}).get('2') or {}).get('eligibility_v3_variable_n') or {}).get('profit_factor'))} · "
            f"3D n={((eligibility_fixed.get('by_horizon') or {}).get('3') or {}).get('eligibility_v3_variable_n', {}).get('sample', 'n/a')} "
            f"avg={_fmt((((eligibility_fixed.get('by_horizon') or {}).get('3') or {}).get('eligibility_v3_variable_n') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt((((eligibility_fixed.get('by_horizon') or {}).get('3') or {}).get('eligibility_v3_variable_n') or {}).get('profit_factor'))} · "
            f"5D n={((eligibility_fixed.get('by_horizon') or {}).get('5') or {}).get('eligibility_v3_variable_n', {}).get('sample', 'n/a')} "
            f"avg={_fmt((((eligibility_fixed.get('by_horizon') or {}).get('5') or {}).get('eligibility_v3_variable_n') or {}).get('avg_return_pct'))}% "
            f"PF={_fmt((((eligibility_fixed.get('by_horizon') or {}).get('5') or {}).get('eligibility_v3_variable_n') or {}).get('profit_factor'))} · "
            f"screen={((eligibility_fixed.get('research_screen') or {}).get('passing_horizon_count', 'n/a'))}/3"
        ),
        (
            "Eligibility falsification v5: "
            f"decision={'FOLLOWUP' if eligibility_falsification_decision.get('historical_followup_candidate') else 'FAIL'} · "
            f"rolling={'PASS' if ((eligibility_falsification_rolling.get('historical_screen') or {}).get('survives_historical_falsification')) else 'FAIL'} "
            f"h={len(eligibility_falsification_rolling.get('economic_passing_horizons') or [])}/3 "
            f"LOO={_fmt(((eligibility_falsification_rolling.get('leave_one_feature_out') or {}).get('followup_candidate_pct')))}% · "
            f"fixed={'PASS' if ((eligibility_falsification_fixed.get('historical_screen') or {}).get('survives_historical_falsification')) else 'FAIL'} "
            f"h={len(eligibility_falsification_fixed.get('economic_passing_horizons') or [])}/3 "
            f"LOO={_fmt(((eligibility_falsification_fixed.get('leave_one_feature_out') or {}).get('followup_candidate_pct')))}% · "
            f"feature-benchmark rows={research_feature_benchmark.get('feature_observation_ids','n/a')} "
            f"runs={research_feature_benchmark.get('run_keys','n/a')}"
        ),
        (
            "Economic waterfall <=5D: "
            f"rolling n={((economic_waterfall_rolling.get('candidate_population') or {}).get('evaluable_candidates','n/a'))} "
            f"movers={((economic_waterfall_rolling.get('candidate_population') or {}).get('movers','n/a'))} "
            f"bottleneck={((economic_waterfall_rolling.get('attrition') or {}).get('largest_mover_loss_stage') or 'n/a')} · "
            f"fixed n={((economic_waterfall_fixed.get('candidate_population') or {}).get('evaluable_candidates','n/a'))} "
            f"bottleneck={((economic_waterfall_fixed.get('attrition') or {}).get('largest_mover_loss_stage') or 'n/a')} · "
            f"same bottleneck={'sí' if economic_waterfall_decision.get('same_largest_mover_loss_stage') else 'no'} · "
            f"shared stable features={economic_waterfall_decision.get('shared_stable_feature_count','n/a')} · "
            f"capture={_fmt((((economic_waterfall_rolling.get('exit_capture_calibration') or {}).get('overall') or {}).get('avg_capture_ratio_pct')))}% "
            f"under-monetized={((economic_waterfall_rolling.get('exit_capture_calibration') or {}).get('overall') or {}).get('under_monetized_movers','n/a')} · LIVE sin cambios"
        ),
        (
            "Architecture <=5D: "
            f"CORE-dir Δacc roll={_fmt((((economic_architecture_rolling.get('core_direction_challenger') or {}).get('lift') or {}).get('direction_accuracy_pp')))}pp/"
            f"fixed={_fmt((((economic_architecture_fixed.get('core_direction_challenger') or {}).get('lift') or {}).get('direction_accuracy_pp')))}pp · "
            f"Research soft Top5 Δprec roll={_fmt((((((economic_architecture_rolling.get('research_soft_gate_challenger') or {}).get('top_k_equal_capacity') or {}).get('5') or {}).get('lift') or {}).get('precision_pp')))}pp/"
            f"fixed={_fmt((((((economic_architecture_fixed.get('research_soft_gate_challenger') or {}).get('top_k_equal_capacity') or {}).get('5') or {}).get('lift') or {}).get('precision_pp')))}pp · "
            f"FUSION rank Top5 Δprec roll={_fmt((((((economic_architecture_rolling.get('fusion_ranker_challenger') or {}).get('top_k_equal_capacity') or {}).get('5') or {}).get('lift') or {}).get('precision_pp')))}pp/"
            f"fixed={_fmt((((((economic_architecture_fixed.get('fusion_ranker_challenger') or {}).get('top_k_equal_capacity') or {}).get('5') or {}).get('lift') or {}).get('precision_pp')))}pp · "
            f"dual followup={','.join(economic_architecture_decision.get('dual_lane_followup_components') or []) or 'none'} · LIVE sin cambios"
        ),
        (
            "Frozen config simulation <=5D: "
            f"CORE loose acc={_fmt((((frozen_config_rolling.get('core_direction') or {}).get('v2_loose_current_622') or {}).get('direction_accuracy_on_5d_movers') or {}).get('accuracy_pct'))}% "
            f"strict={_fmt((((frozen_config_rolling.get('core_direction') or {}).get('v2_strict_consensus') or {}).get('direction_accuracy_on_5d_movers') or {}).get('accuracy_pct'))}% · "
            f"Research Top1 n={((((frozen_config_rolling.get('research_override_top1') or {}).get('top1_override') or {}).get('current_direction') or {}).get('sample','n/a'))} "
            f"hit={_fmt(((((frozen_config_rolling.get('research_override_top1') or {}).get('top1_override') or {}).get('current_direction') or {}).get('mover_precision_pct')))}% "
            f"ret={_fmt(((((frozen_config_rolling.get('research_override_top1') or {}).get('top1_override') or {}).get('current_direction') or {}).get('avg_horizon_close_return_pct')))}% "
            f"PF={_fmt(((((frozen_config_rolling.get('research_override_top1') or {}).get('top1_override') or {}).get('current_direction') or {}).get('horizon_close_profit_factor')))} · "
            f"critical={frozen_config_decision.get('critical_failure_count','n/a')} "
            f"core={frozen_config_decision.get('recommended_core_variant_for_continued_shadow','n/a')} · LIVE sin cambios"
        ),
        (
            "Scanner recall/rank <=5D: "
            f"universe={((scanner_calibration_rolling.get('universe') or {}).get('rows','n/a'))} "
            f"names/day={_fmt((scanner_calibration_rolling.get('universe') or {}).get('avg_names_per_date'))} · "
            f"Top180 current recall={_fmt((((scanner_calibration_rolling.get('current_static_priority_proxy') or {}).get('all_dates') or {}).get('top_180') or {}).get('recall_pct'))}% "
            f"prec={_fmt((((scanner_calibration_rolling.get('current_static_priority_proxy') or {}).get('all_dates') or {}).get('top_180') or {}).get('precision_pct'))}% · "
            f"test challenger recall={_fmt((((scanner_calibration_rolling.get('challenger') or {}).get('final_test') or {}).get('top_180') or {}).get('recall_pct'))}% "
            f"prec={_fmt((((scanner_calibration_rolling.get('challenger') or {}).get('final_test') or {}).get('top_180') or {}).get('precision_pct'))}% · "
            f"dirRet current={_fmt((((scanner_calibration_rolling.get('current_static_priority_proxy') or {}).get('directional_final_test') or {}).get('top_180') or {}).get('avg_horizon_close_return_pct'))}%/"
            f"chall={_fmt((((scanner_calibration_rolling.get('challenger') or {}).get('directional_final_test') or {}).get('top_180') or {}).get('avg_horizon_close_return_pct'))}% · "
            f"dirPF current={_fmt((((scanner_calibration_rolling.get('current_static_priority_proxy') or {}).get('directional_final_test') or {}).get('top_180') or {}).get('horizon_close_profit_factor'))}/"
            f"chall={_fmt((((scanner_calibration_rolling.get('challenger') or {}).get('directional_final_test') or {}).get('top_180') or {}).get('horizon_close_profit_factor'))} · "
            f"features={','.join((scanner_calibration_decision.get('stable_feature_overlap') or [])) or 'none'} · "
            f"dirGuard={scanner_calibration_decision.get('rolling_directional_sanity','n/a')}/"
            f"{scanner_calibration_decision.get('fixed_directional_sanity','n/a')} · "
            f"dual={scanner_calibration_decision.get('dual_lane_followup','n/a')} · LIVE sin cambios"
        ),
        (
            "Scanner→CORE v6.2.27: "
            f"current mandatory prec={_fmt(((scanner_core_rolling.get('current_mandatory_control') or {}).get('selection') or {}).get('precision_pct'))}% "
            f"recall={_fmt(((scanner_core_rolling.get('current_mandatory_control') or {}).get('selection') or {}).get('recall_pct'))}% · "
            f"chall mandatory prec={_fmt(((scanner_core_rolling.get('challenger_mandatory') or {}).get('selection') or {}).get('precision_pct'))}% "
            f"recall={_fmt(((scanner_core_rolling.get('challenger_mandatory') or {}).get('selection') or {}).get('recall_pct'))}% · "
            f"COREv2 ret={_fmt(((((scanner_core_rolling.get('challenger_mandatory') or {}).get('direction') or {}).get('core_v2_strict') or {}).get('avg_horizon_close_return_pct')))}% "
            f"PF={_fmt(((((scanner_core_rolling.get('challenger_mandatory') or {}).get('direction') or {}).get('core_v2_strict') or {}).get('horizon_close_profit_factor')))} · "
            f"replace={_fmt((scanner_core_rolling.get('replacement_analysis') or {}).get('replacement_pct'))}% · "
            f"dual={scanner_core_decision.get('dual_lane_followup','n/a')} · LIVE sin cambios"
        ),
        (
            "Scanner direction-aware v6.2.29: "
            f"roll={scanner_direction_aware_rolling.get('selected_candidate') or 'none'} "
            f"pass={scanner_direction_aware_rolling.get('historical_followup_candidate','n/a')} · "
            f"fixed={scanner_direction_aware_fixed.get('selected_candidate') or 'none'} "
            f"pass={scanner_direction_aware_fixed.get('historical_followup_candidate','n/a')} · "
            f"dual={scanner_direction_aware_decision.get('dual_lane_followup','n/a')} · "
            f"next={scanner_direction_aware_decision.get('next_step','n/a')} · LIVE sin cambios"
        ),
        (
            "GEX gate v6.2.33: "
            f"roll cov={((gex_gate_rolling.get('post_research_population') or {}).get('gex_identifiable_dates','n/a'))}d "
            f"unavail movers={((gex_gate_rolling.get('post_research_population') or {}).get('gex_unavailable_movers','n/a'))} "
            f"followup={'PASS' if gex_gate_rolling.get('historical_followup_candidate') else 'FAIL'} · "
            f"fixed followup={'PASS' if gex_gate_fixed.get('historical_followup_candidate') else 'FAIL'} · "
            f"dual={gex_gate_decision.get('dual_lane_followup','n/a')} · "
            f"next={gex_gate_decision.get('next_step','n/a')} · LIVE sin cambios"
        ),
        (
            "Momentum direction audit v6: "
            f"coverage {_fmt(momentum_direction.get('coverage_pct'))}% · "
            f"agree {_fmt(momentum_direction.get('direction_agreement_pct'))}% · "
            f"2D curr {_fmt(((((momentum_direction.get('by_horizon') or {}).get('2') or {}).get('current_direction_same_identities') or {}).get('avg_return_pct')))}%/"
            f"mom {_fmt(((((momentum_direction.get('by_horizon') or {}).get('2') or {}).get('momentum_direction_same_identities') or {}).get('avg_return_pct')))}% · "
            f"3D curr {_fmt(((((momentum_direction.get('by_horizon') or {}).get('3') or {}).get('current_direction_same_identities') or {}).get('avg_return_pct')))}%/"
            f"mom {_fmt(((((momentum_direction.get('by_horizon') or {}).get('3') or {}).get('momentum_direction_same_identities') or {}).get('avg_return_pct')))}% · "
            f"5D curr {_fmt(((((momentum_direction.get('by_horizon') or {}).get('5') or {}).get('current_direction_same_identities') or {}).get('avg_return_pct')))}%/"
            f"mom {_fmt(((((momentum_direction.get('by_horizon') or {}).get('5') or {}).get('momentum_direction_same_identities') or {}).get('avg_return_pct')))}% · "
            f"screen={((momentum_direction.get('research_screen') or {}).get('passing_horizon_count', 'n/a'))}/3"
        ),
        (
            "Momentum edge waterfall v7 Top-5: "
            f"2D scanner {_fmt((((((((momentum_attrition.get('top_k_results') or {}).get('5') or {}).get('by_horizon') or {}).get('2') or {}).get('stages') or {}).get('SCANNER_MOMENTUM') or {}).get('metrics', {}).get('avg_return_pct')))}%/"
            f"ENTER {_fmt((((((((momentum_attrition.get('top_k_results') or {}).get('5') or {}).get('by_horizon') or {}).get('2') or {}).get('stages') or {}).get('ENTER') or {}).get('metrics', {}).get('avg_return_pct')))}% · "
            f"3D scanner {_fmt((((((((momentum_attrition.get('top_k_results') or {}).get('5') or {}).get('by_horizon') or {}).get('3') or {}).get('stages') or {}).get('SCANNER_MOMENTUM') or {}).get('metrics', {}).get('avg_return_pct')))}%/"
            f"ENTER {_fmt((((((((momentum_attrition.get('top_k_results') or {}).get('5') or {}).get('by_horizon') or {}).get('3') or {}).get('stages') or {}).get('ENTER') or {}).get('metrics', {}).get('avg_return_pct')))}% · "
            f"5D scanner {_fmt((((((((momentum_attrition.get('top_k_results') or {}).get('5') or {}).get('by_horizon') or {}).get('5') or {}).get('stages') or {}).get('SCANNER_MOMENTUM') or {}).get('metrics', {}).get('avg_return_pct')))}%/"
            f"ENTER {_fmt((((((((momentum_attrition.get('top_k_results') or {}).get('5') or {}).get('by_horizon') or {}).get('5') or {}).get('stages') or {}).get('ENTER') or {}).get('metrics', {}).get('avg_return_pct')))}% · "
            f"harmful={','.join(momentum_attrition.get('primary_harmful_stages_across_2plus_horizons') or []) or 'none'}"
        ),
        (
            "Thesis Quality OOT v5: "
            f"test {thesis_quality.get('test_dates', 'n/a')}d · "
            f"selecciona {thesis_quality.get('selected_trades', 'n/a')} · "
            f"abstiene {thesis_quality.get('dates_abstained_completely', 'n/a')}d · "
            f"completion {_fmt((thesis_quality.get('thesis_eligibility_variable_n') or {}).get('completion_rate_pct'))}% · "
            f"avg {_fmt((thesis_quality.get('thesis_eligibility_variable_n') or {}).get('avg_return_pct'))}% · "
            f"PF {_fmt((thesis_quality.get('thesis_eligibility_variable_n') or {}).get('profit_factor'))} · "
            f"eligible uncapped PF {_fmt(((thesis_quality.get('ranking_ablation') or {}).get('uncapped_eligible') or {}).get('profit_factor'))} · "
            f"alpha-rank PF {_fmt(((thesis_quality.get('ranking_ablation') or {}).get('top_n_by_current_alpha') or {}).get('profit_factor'))} · "
            f"sep completion {_fmt((thesis_quality.get('candidate_diagnostics') or {}).get('completion_rate_separation_pp'))}pp"
        ),
        (
            "Challenger ENTER-first: "
            f"{(enter_first.get('metrics') or {}).get('closed', 'n/a')} cerradas · "
            f"avg {_fmt((enter_first.get('metrics') or {}).get('avg_underlying_return_pct'))}% · "
            f"PF {_fmt((enter_first.get('metrics') or {}).get('underlying_profit_factor'))}"
        ),
        (
            "Challenger session-fallback: "
            f"{(session_fallback.get('metrics') or {}).get('closed', 'n/a')} cerradas · "
            f"avg {_fmt((session_fallback.get('metrics') or {}).get('avg_underlying_return_pct'))}% · "
            f"PF {_fmt((session_fallback.get('metrics') or {}).get('underlying_profit_factor'))}"
        ),
        (
            "Challenger quality-gated: "
            f"{(quality_enter.get('metrics') or {}).get('closed', 'n/a')} cerradas · "
            f"avg {_fmt((quality_enter.get('metrics') or {}).get('avg_underlying_return_pct'))}% · "
            f"PF {_fmt((quality_enter.get('metrics') or {}).get('underlying_profit_factor'))}"
        ),
        (
            "Challenger combinado: "
            f"{(combined.get('metrics') or {}).get('closed', 'n/a')} cerradas · "
            f"avg {_fmt((combined.get('metrics') or {}).get('avg_underlying_return_pct'))}% · "
            f"PF {_fmt((combined.get('metrics') or {}).get('underlying_profit_factor'))}"
        ),
        "📊 Ledger current-policy + emitted book + Reverse Stress + challengers disponibles para revisión.",
    ]
    return "\n".join(lines)

from alpha_pnl_attribution import (
    run_alpha_pnl_attribution,
    _attribution_cache_basis,
    adaptive_model_comparison,
    compact_economic_summary,
)
from historical_operational_validation import build_historical_operational_validation, write_historical_operational_artifacts
from db import connect, get_runtime_state, set_runtime_state
import config
from time_utils import ts_madrid_str
from research_snapshot import frozen_research_snapshot, compact_snapshot_summary, research_db_path
from post_replay_adaptive_handoff import launch_adaptive_after_full
from research_benchmark import (
    ensure_economic_benchmark,
    compact_benchmark_summary,
    ensure_feature_observation_benchmark,
    compact_feature_benchmark_summary,
)



_ECONOMIC_FINGERPRINT_FILES = (
    "historical_operational_validation.py",
    "fusion_engine.py",
    "eod_options_lab.py",
    "eod_options_runtime.py",
    "alpha_swing_engine.py",
    "synthetic_option_proxy.py",
    "market_outcome_auditor.py",
    "daily_reverse_alpha.py",
    "alpha_pnl_attribution.py",
    "lifecycle_research.py",
    "entry_quality_research.py",
    "entry_quality_cross_sectional.py",
    "entry_quality_eligibility.py",
    "entry_eligibility_falsification.py",
    "economic_waterfall_calibration.py",
    "economic_architecture_challengers.py",
    "frozen_config_historical_simulation.py",
    "scanner_recall_ranking_calibration.py",
    "scanner_core_interaction_simulation.py",
    "scanner_direction_aware_ranking.py",
    "core_context_precision_optimizer.py",
    "autonomous_research_cache.py",
    "gex_gate_calibration.py",
    "fusion_gate_calibration.py",
    "prospective_architecture_journal.py",
    "momentum_direction_audit.py",
    "momentum_edge_attrition.py",
    "momentum_reconciliation.py",
    "entry_thesis_quality.py",
    "swing_v2.py",
    "config.py",
    "research_snapshot.py",
    "research_benchmark.py",
)


def _economic_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in _ECONOMIC_FINGERPRINT_FILES:
        path = PROGRAM_DIR / name
        text_value = path.read_text(encoding="utf-8-sig", errors="ignore")
        if name == "config.py":
            text_value = "\n".join(
                line for line in text_value.splitlines()
                if not line.strip().startswith("BOT_VERSION =")
            )
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text_value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _effective_operational_model_state(conn) -> dict:
    """Fingerprint the model actually used by historical operational replay.

    Raw entry_mode is intentionally not a material replay input. A transition
    shadow -> off during off-hours must not invalidate research when the resolved
    model is unchanged. A transition that changes the effective model is caught
    by model id + parameter fingerprint.
    """
    entry_mode = "shadow"
    override_id = 0
    try:
        row = conn.execute(
            "SELECT state_value FROM runtime_state WHERE state_key='entry_mode'"
        ).fetchone()
        entry_mode = str(row[0] if row else "shadow").lower()
        row = conn.execute(
            "SELECT state_value FROM runtime_state WHERE state_key='adaptive_shadow_model_id'"
        ).fetchone()
        override_id = int(row[0] or 0) if row else 0
    except Exception:
        entry_mode = "shadow"
        override_id = 0

    model = None
    resolution = "ACTIVE"
    if entry_mode != "live" and override_id > 0:
        model = conn.execute(
            "SELECT id,model_key,status,params_json FROM model_versions WHERE id=?",
            (override_id,),
        ).fetchone()
        resolution = "SHADOW_OVERRIDE"
    if model is None:
        model = conn.execute(
            """SELECT mv.id,mv.model_key,mv.status,mv.params_json
               FROM model_versions mv
               JOIN model_state ms ON ms.model_version_id=mv.id
               WHERE ms.state_key='active'
               ORDER BY mv.id DESC LIMIT 1"""
        ).fetchone()
        resolution = "ACTIVE"
    if model is None:
        model = conn.execute(
            """SELECT id,model_key,status,params_json FROM model_versions
               WHERE status='bootstrap' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        resolution = "BOOTSTRAP"
    if model is None:
        return {"model_version_id": 0, "model_key": "", "status": "", "params_hash": "", "resolution": "NONE"}
    params = str(model[3] or "")
    return {
        "model_version_id": int(model[0] or 0),
        "model_key": str(model[1] or ""),
        "status": str(model[2] or ""),
        "params_hash": hashlib.sha256(params.encode("utf-8")).hexdigest(),
        "resolution": resolution,
    }


def _history_watermark() -> dict:
    conn = connect(research_db_path(config.ALERTS_DB), readonly=True)
    try:
        tables: dict[str, dict] = {}
        specs = (
            ("feature_observations", "snapshot_date"),
            ("observation_outcomes", "observation_date"),
            ("layer_signal_history", "snapshot_date"),
            ("price_cache", "trade_date"),
        )
        for table, date_col in specs:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS max_rowid, "
                    f"COALESCE(MAX({date_col}),'') AS max_date FROM {table}"
                ).fetchone()
                tables[table] = {
                    "rows": int(row[0] or 0),
                    "max_rowid": int(row[1] or 0),
                    "max_date": str(row[2] or ""),
                }
                if table == "price_cache":
                    try:
                        sig = conn.execute(
                            """SELECT
                                 COALESCE(SUM(CAST(ROUND(COALESCE(open,0)*10000) AS INTEGER)),0),
                                 COALESCE(SUM(CAST(ROUND(COALESCE(high,0)*10000) AS INTEGER)),0),
                                 COALESCE(SUM(CAST(ROUND(COALESCE(low,0)*10000) AS INTEGER)),0),
                                 COALESCE(SUM(CAST(ROUND(COALESCE(close,0)*10000) AS INTEGER)),0)
                               FROM price_cache"""
                        ).fetchone()
                        tables[table]["value_signature"] = [int(value or 0) for value in sig]
                    except Exception:
                        tables[table]["value_signature"] = []
            except Exception:
                tables[table] = {"rows": None, "max_rowid": None, "max_date": None}
        return {
            "tables": tables,
            "model_state": {
                "effective_model": _effective_operational_model_state(conn),
            },
        }
    finally:
        conn.close()


def _replay_basis() -> dict:
    return {
        "economic_fingerprint": _economic_fingerprint(),
        "history_watermark": _history_watermark(),
    }



def _refresh_full_universe_calibration_if_stale() -> dict:
    """Refresh research from immutable caches; never require FULL economic replay.

    The cached study is stale only if its own code or input cache fingerprints
    differ. This makes research-only code iterations inexpensive and prevents
    changing historical positions, reverse stress or LIVE execution.
    """
    try:
        import config as _config
        from research_full_universe_calibration import run as run_full_universe
        from research_streaming_io import atomic_stream_json

        report_path = (
            _config.OUTPUT_DIR / "historical_validation"
            / "latest_full_universe_calibration.json"
        )
        source_sha = hashlib.sha256(
            (PROGRAM_DIR / "research_full_universe_calibration.py").read_bytes()
        ).hexdigest()
        status_path = (
            _config.OUTPUT_DIR / "historical_validation"
            / "latest_fast_research_cache_status.json"
        )
        status = json.loads(status_path.read_text(encoding="utf-8-sig"))
        cache_fingerprints = {
            name: str((status.get("lanes") or {}).get(name, {}).get("dataset_fingerprint") or "")
            for name in ("ROLLING_LATEST", "FIXED_BENCHMARK")
        }
        if not all(cache_fingerprints.values()):
            raise ValueError("FAST_RESEARCH_CACHE_FINGERPRINTS_MISSING")
        if report_path.is_file():
            try:
                last = json.loads(report_path.read_text(encoding="utf-8-sig"))
                if last.get("research_code_sha256") == source_sha and all(
                    ((last.get("lanes") or {}).get(name, {}).get("cache") or {}).get("cache_fingerprint")
                    == fp for name, fp in cache_fingerprints.items()
                ):
                    set_runtime_state("full_universe_calibration_status", "REUSED_FROZEN_RESEARCH")
                    return {"status": "REUSED_FROZEN_RESEARCH", "live_mutation": False}
            except (ValueError, OSError, TypeError):
                pass
        _set_replay_phase("FULL_UNIVERSE_CALIBRATION", "research-only frozen-cache recalculation")
        full_universe = run_full_universe()
        # Fingerprint belongs to the diagnostic code, not the economic replay.
        full_universe["research_code_sha256"] = source_sha
        atomic_stream_json(report_path, full_universe)
        set_runtime_state("full_universe_calibration_status", "DIAGNOSTIC_ONLY")
        return {"status": "RECOMPUTED_FROM_FROZEN_CACHE", "live_mutation": False}
    except Exception as exc:
        set_runtime_state(
            "full_universe_calibration_status",
            "ERROR: " + type(exc).__name__ + ": " + str(exc)[:300],
        )
        _set_replay_phase("FULL_UNIVERSE_CALIBRATION_WARNING", type(exc).__name__)
        return {
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "live_mutation": False,
        }



def _refresh_readonly_scanner_intraday_audit() -> dict:
    """Audit ALL observed Scanner runs from the live source DB, never from the
    compact frozen snapshot. A research-only read; operational T0 is separate.
    """
    try:
        from scanner_intraday_coverage_audit import run_if_stale
        outcome = run_if_stale()
        set_runtime_state("scanner_intraday_coverage_audit_status", str(outcome["status"]))
        return outcome
    except Exception as exc:
        error = f"ERROR: {type(exc).__name__}: {str(exc)[:300]}"
        set_runtime_state("scanner_intraday_coverage_audit_status", error)
        return {"status": "ERROR", "error": error, "no_live_mutation": True}


def _can_reuse_previous_replay(current_basis: dict) -> bool:
    previous = _json_state("historical_replay_basis")
    previous_summary = _json_state("historical_replay_summary")
    if str(previous_summary.get("status") or "").lower() not in {"ok", "reused"}:
        return False
    validity = previous_summary.get("research_validity") or {}
    # v6.1.138: never reuse a run that explicitly failed strategic validity.
    if validity and validity.get("strategic_conclusion_allowed") is not True:
        return False
    return (
        previous.get("economic_fingerprint") == current_basis.get("economic_fingerprint")
        and previous.get("history_watermark") == current_basis.get("history_watermark")
    )


def _basis_diff(before: dict, after: dict) -> dict:
    """Compact diagnostics for mutable input drift."""
    result = {"changed": before != after, "tables": [], "state": [], "journal_changed": False}
    before_hist = before.get("history_watermark") or {}
    after_hist = after.get("history_watermark") or {}
    before_tables = before_hist.get("tables") or {}
    after_tables = after_hist.get("tables") or {}
    result["tables"] = sorted({
        key for key in set(before_tables) | set(after_tables)
        if before_tables.get(key) != after_tables.get(key)
    })
    before_state = before_hist.get("state") or before_hist.get("model_state") or {}
    after_state = after_hist.get("state") or after_hist.get("model_state") or {}
    result["state"] = sorted({
        key for key in set(before_state) | set(after_state)
        if before_state.get(key) != after_state.get(key)
    })
    result["journal_changed"] = before_hist.get("journal") != after_hist.get("journal")
    result["source_fingerprint_changed"] = before.get("source_fingerprint") != after.get("source_fingerprint")
    result["economic_fingerprint_changed"] = before.get("economic_fingerprint") != after.get("economic_fingerprint")
    return result

def _json_state(key: str) -> dict:
    raw = get_runtime_state(key, "") or ""
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


V6_1_131_ATTRIBUTION_SOURCE_FINGERPRINT = "0b358a2acbaaf69521eb36b7d8d51d6b90f4e295694402e1ababb060ac195608"


def _prepare_v6131_attribution_basis_migration() -> dict:
    """Safely migrate the v6.1.131 cache basis after fixing one bad watermark column.

    v6.1.131 stored adaptive_model_metrics as an error because created_at_madrid
    does not exist. v6.1.132 changes only that table watermark to learning_run_id
    inside alpha_pnl_attribution.py. Reuse is allowed only when:
    - the previous source fingerprint is the exact known v6.1.131 fingerprint,
    - every other attribution input/state/journal watermark is unchanged,
    - the cached adaptive-model comparison already used the same latest
      learning_run_id now present in adaptive_model_metrics.

    If any check fails, nothing is mutated and Attribution recomputes fully.
    """
    previous = _json_state("alpha_pnl_attribution_basis")
    cached = _json_state("alpha_pnl_attribution_snapshot")
    current = _attribution_cache_basis()
    status = {
        "status": "NOT_APPLICABLE",
        "safe_reuse": False,
        "previous_source_fingerprint": previous.get("source_fingerprint"),
        "current_source_fingerprint": current.get("source_fingerprint"),
    }
    if not previous or not cached:
        status["reason"] = "MISSING_PREVIOUS_BASIS_OR_SNAPSHOT"
        return status
    if previous == current:
        status.update({"status": "EXACT_BASIS", "safe_reuse": True, "reason": "ALREADY_CURRENT"})
        return status
    if str(previous.get("source_fingerprint") or "") != V6_1_131_ATTRIBUTION_SOURCE_FINGERPRINT:
        status["reason"] = "PREVIOUS_SOURCE_NOT_V6_1_131"
        return status

    previous_history = previous.get("history_watermark") or {}
    current_history = current.get("history_watermark") or {}
    previous_tables = dict(previous_history.get("tables") or {})
    current_tables = dict(current_history.get("tables") or {})
    previous_adaptive = previous_tables.pop("adaptive_model_metrics", {})
    current_adaptive = current_tables.pop("adaptive_model_metrics", {})

    if previous_tables != current_tables:
        status["reason"] = "OTHER_ATTRIBUTION_TABLES_CHANGED"
        return status
    if (previous_history.get("state") or {}) != (current_history.get("state") or {}):
        status["reason"] = "ATTRIBUTION_STATE_CHANGED"
        return status
    if (previous_history.get("journal") or {}) != (current_history.get("journal") or {}):
        status["reason"] = "ATTRIBUTION_JOURNAL_CHANGED"
        return status
    if "created_at_madrid" not in str(previous_adaptive.get("error") or ""):
        status["reason"] = "PREVIOUS_ADAPTIVE_WATERMARK_NOT_EXPECTED_ERROR"
        return status
    if current_adaptive.get("error"):
        status["reason"] = "CURRENT_ADAPTIVE_WATERMARK_ERROR"
        return status

    cached_learning_run = int(
        ((cached.get("adaptive_model_comparison") or {}).get("learning_run_id") or 0)
    )
    current_learning_run = int(current_adaptive.get("max_date") or 0)
    status["cached_learning_run_id"] = cached_learning_run
    status["current_learning_run_id"] = current_learning_run
    if cached_learning_run <= 0 or cached_learning_run != current_learning_run:
        status["reason"] = "ADAPTIVE_MODEL_METRICS_CHANGED_SINCE_CACHED_ATTRIBUTION"
        return status

    set_runtime_state(
        "alpha_pnl_attribution_basis",
        json.dumps(current, ensure_ascii=False, default=str),
    )
    status.update({
        "status": "MIGRATED_V6_1_131_BASIS",
        "safe_reuse": True,
        "reason": "ONLY_INVALID_ADAPTIVE_MODEL_WATERMARK_COLUMN_CHANGED",
    })
    set_runtime_state(
        "alpha_pnl_attribution_basis_migration_status",
        json.dumps(status, ensure_ascii=False, default=str),
    )
    return status


def _prepare_adaptive_metrics_only_refresh() -> dict:
    """Reuse expensive Attribution when only adaptive_model_metrics advanced.

    adaptive_model_metrics feeds adaptive_model_comparison, but it does not feed
    feature usefulness, Scanner discovery, horizon/regime routers, rolling
    stability or the full-history swing lab. When it is the sole watermark
    change, permit the existing strong cache and refresh only that component.

    Any source/state/journal/other-table change fails closed to normal FULL
    recomputation.
    """
    previous = _json_state("alpha_pnl_attribution_basis")
    cached = _json_state("alpha_pnl_attribution_snapshot")
    current = _attribution_cache_basis()
    result = {
        "status": "NOT_APPLICABLE",
        "safe_reuse": False,
        "previous_source_fingerprint": previous.get("source_fingerprint"),
        "current_source_fingerprint": current.get("source_fingerprint"),
    }
    if not previous or not cached:
        result["reason"] = "MISSING_PREVIOUS_BASIS_OR_SNAPSHOT"
        return result
    if previous == current:
        result.update({"status": "EXACT_BASIS", "safe_reuse": True, "refresh_required": False})
        return result
    if previous.get("schema") != current.get("schema"):
        result["reason"] = "CACHE_SCHEMA_CHANGED"
        return result
    if previous.get("source_fingerprint") != current.get("source_fingerprint"):
        result["reason"] = "ATTRIBUTION_SOURCE_CHANGED"
        return result

    previous_history = previous.get("history_watermark") or {}
    current_history = current.get("history_watermark") or {}
    if (previous_history.get("state") or {}) != (current_history.get("state") or {}):
        result["reason"] = "ATTRIBUTION_STATE_CHANGED"
        return result
    if (previous_history.get("journal") or {}) != (current_history.get("journal") or {}):
        result["reason"] = "ATTRIBUTION_JOURNAL_CHANGED"
        return result

    previous_tables = dict(previous_history.get("tables") or {})
    current_tables = dict(current_history.get("tables") or {})
    previous_adaptive = previous_tables.pop("adaptive_model_metrics", None)
    current_adaptive = current_tables.pop("adaptive_model_metrics", None)
    if previous_tables != current_tables:
        result["reason"] = "OTHER_ATTRIBUTION_TABLES_CHANGED"
        return result
    if previous_adaptive == current_adaptive:
        result["reason"] = "BASIS_DIFF_NOT_ADAPTIVE_MODEL_METRICS"
        return result
    if current_adaptive is None or (isinstance(current_adaptive, dict) and current_adaptive.get("error")):
        result["reason"] = "CURRENT_ADAPTIVE_WATERMARK_INVALID"
        return result

    # Temporarily advance only the persisted basis so run_alpha_pnl_attribution
    # can reuse the expensive snapshot. The component below is refreshed
    # immediately from the current DB before any result is published.
    set_runtime_state(
        "alpha_pnl_attribution_basis",
        json.dumps(current, ensure_ascii=False, default=str),
    )
    result.update({
        "status": "ADAPTIVE_MODEL_METRICS_ONLY",
        "safe_reuse": True,
        "refresh_required": True,
        "previous_adaptive_watermark": previous_adaptive,
        "current_adaptive_watermark": current_adaptive,
    })
    return result


def _refresh_adaptive_model_component(payload: dict) -> dict:
    conn = connect(config.ALERTS_DB, readonly=True)
    try:
        refreshed = adaptive_model_comparison(conn)
    finally:
        conn.close()
    patched = dict(payload)
    patched["adaptive_model_comparison"] = refreshed
    patched["runtime_cached"] = True
    patched["cache_mode"] = "BASIS_REUSE_TARGETED_ADAPTIVE_REFRESH"
    patched["targeted_refresh_components"] = ["adaptive_model_comparison"]
    set_runtime_state(
        "alpha_pnl_attribution_snapshot",
        json.dumps(patched, ensure_ascii=False, default=str),
    )
    set_runtime_state(
        "alpha_pnl_economic_summary",
        json.dumps(compact_economic_summary(patched), ensure_ascii=False, default=str),
    )
    return patched


def main() -> int:
    started = ts_madrid_str()
    monotonic_started = time.monotonic()
    current_basis = _replay_basis()
    if _can_reuse_previous_replay(current_basis):
        finished = ts_madrid_str()
        reused = {
            "status": "reused",
            "started_at_madrid": started,
            "finished_at_madrid": finished,
            "producer_version": _production_version(),
            "reason": "UNCHANGED_ECONOMIC_FINGERPRINT_AND_HISTORY_WATERMARK",
            "economic_fingerprint": current_basis.get("economic_fingerprint"),
        }
        set_runtime_state("historical_replay_status", "reused")
        set_runtime_state("historical_replay_started_at_madrid", started)
        set_runtime_state("historical_replay_finished_at_madrid", finished)
        set_runtime_state(
            "historical_replay_summary",
            json.dumps(reused, ensure_ascii=False, default=str),
        )
        research_refresh = _refresh_full_universe_calibration_if_stale()
        reused["full_universe_research_refresh"] = research_refresh
        reused["scanner_intraday_audit"] = _refresh_readonly_scanner_intraday_audit()
        _set_replay_phase("REUSED", "economic replay reused; research cache checked")
        _discord_send(
            "♻️ **Reconstrucción histórica TMAD reutilizada.**\n"
            f"Versión producción: {_production_version()}\n"
            "Sin cambios en lógica económica ni en el histórico relevante; se conserva el último ledger validado.\n"
            f"Calibración research desde caché: {research_refresh['status']}.\n"
            "No se ejecuta un full replay innecesario."
        )
        print(json.dumps(reused, ensure_ascii=False, default=str))
        return 0

    set_runtime_state("historical_replay_status", "running")
    set_runtime_state("historical_replay_started_at_madrid", started)
    set_runtime_state("historical_replay_finished_at_madrid", "")
    set_runtime_state(
        "historical_replay_summary",
        json.dumps({"status": "running", "started_at_madrid": started}, ensure_ascii=False),
    )
    _discord_send(
        "🧪 **Reconstrucción histórica TMAD iniciada.**\n"
        f"Versión producción: {_production_version()} · PID: {os.getpid()}\n"
        "Forward Replay + aproximador de opciones + Reverse Stress ejecutándose en background.\n"
        "Data Bot y Alerts Bot continúan operativos."
    )
    watchdog_stop = threading.Event()
    watchdog_thread = threading.Thread(target=_watchdog_loop, args=(watchdog_stop,), daemon=True)
    watchdog_thread.start()
    _set_replay_phase("SNAPSHOT_PREP", "capturing immutable T0 research inputs")
    try:
        live_attribution_basis_started = _attribution_cache_basis()
        live_operational_basis_started = _replay_basis()

        def _snapshot_progress(component: str, detail: str) -> None:
            _set_replay_phase(f"SNAPSHOT_PREP/{component}", detail)

        with frozen_research_snapshot(config, progress_callback=_snapshot_progress) as research_snapshot:
            _set_replay_phase("RESEARCH_BENCHMARK", "loading or creating immutable economic cohort")
            research_benchmark = ensure_economic_benchmark(
                snapshot_id=str(research_snapshot.get("snapshot_id") or ""),
                producer_version=_production_version(),
            )
            research_benchmark_summary = compact_benchmark_summary(research_benchmark)
            set_runtime_state(
                "economic_research_benchmark_summary",
                json.dumps(research_benchmark_summary, ensure_ascii=False, default=str),
            )

            research_feature_benchmark = ensure_feature_observation_benchmark(
                research_benchmark,
                snapshot_id=str(research_snapshot.get("snapshot_id") or ""),
                producer_version=_production_version(),
            )
            research_feature_benchmark_summary = compact_feature_benchmark_summary(
                research_feature_benchmark
            )
            set_runtime_state(
                "economic_feature_observation_benchmark_summary",
                json.dumps(research_feature_benchmark_summary, ensure_ascii=False, default=str),
            )

            _set_replay_phase("ATTRIBUTION_PREP", "using immutable T0 snapshot + fixed economic benchmark")
            attribution_basis_started = _attribution_cache_basis()
            basis_migration = {
                "status": "DISABLED_FROZEN_SNAPSHOT",
                "safe_reuse": False,
                "reason": "Frozen replay never rewrites cache basis before attribution.",
            }
            targeted_refresh = {
                "status": "DISABLED_FROZEN_SNAPSHOT",
                "safe_reuse": False,
                "refresh_required": False,
                "reason": "Exact frozen-basis cache reuse remains available; partial live refresh is disabled.",
            }

            def _attribution_progress(component: str, status: str, elapsed_seconds=None) -> None:
                suffix = status
                if elapsed_seconds is not None:
                    suffix += f" {float(elapsed_seconds):.1f}s"
                _set_replay_phase(f"ATTRIBUTION/{component}", suffix)

            payload = run_alpha_pnl_attribution(
                progress_callback=_attribution_progress,
                research_benchmark=research_benchmark,
            )
            _set_replay_phase("ATTRIBUTION_COMPLETE", str(payload.get("cache_mode") or "computed"))
            if targeted_refresh.get("refresh_required"):
                payload = _refresh_adaptive_model_component(payload)
            attribution_cache_mode = str(payload.get("cache_mode") or ("CACHE" if payload.get("runtime_cached") else "RECOMPUTED"))
            replay_mode = "PARTIAL_OPERATIONAL" if bool(payload.get("runtime_cached")) else "FULL"
            operational_basis_started = _replay_basis()
            _set_replay_phase("OPERATIONAL_VALIDATION", "building current-policy, lifecycle and Reverse Stress")

            def _operational_progress(component: str, detail: str) -> None:
                _set_replay_phase(
                    f"OPERATIONAL_VALIDATION/{component}",
                    detail,
                )

            operational = build_historical_operational_validation(
                max_positions=5,
                momentum_reconciliation_reference=payload.get("momentum_reconciliation") or {},
                research_benchmark=research_benchmark,
                research_feature_benchmark=research_feature_benchmark,
                progress_callback=_operational_progress,
            )
            _set_replay_phase("OPERATIONAL_ARTIFACTS", "writing validation artifacts")
            operational_artifacts = write_historical_operational_artifacts(operational)
            # Stage 1 of strategy calibration: score the ENTIRE frozen
            # historical universe before top-K/portfolio/exit restrictions.
            # This research can fail without disturbing operational replay.
            if replay_mode in {"FULL", "PARTIAL_OPERATIONAL"}:
                _refresh_full_universe_calibration_if_stale()
                _refresh_readonly_scanner_intraday_audit()
            operational_summary = operational.get("summary") or {}
            lifecycle_research = (
                (operational_summary.get("research_challengers") or {}).get("stateful_boundary_lifecycle")
                or {}
            )
            lifecycle_current = lifecycle_research.get("current_policy") or {}
            set_runtime_state(
                "lifecycle_research_snapshot",
                json.dumps(lifecycle_research, ensure_ascii=False, default=str),
            )
            set_runtime_state(
                "lifecycle_research_boundary_sample",
                str(lifecycle_current.get("evaluable") or 0),
            )
            entry_quality_cross_runtime = (
                (operational_summary.get("research_challengers") or {}).get("entry_quality_cross_sectional_walkforward")
                or {}
            )
            set_runtime_state(
                "entry_quality_cross_sectional_snapshot",
                json.dumps(entry_quality_cross_runtime, ensure_ascii=False, default=str),
            )
            entry_quality_eligibility_runtime = (
                (operational_summary.get("research_challengers") or {}).get("entry_quality_eligibility_walkforward")
                or {}
            )
            set_runtime_state(
                "entry_quality_eligibility_snapshot",
                json.dumps(entry_quality_eligibility_runtime, ensure_ascii=False, default=str),
            )
            entry_eligibility_falsification_runtime = (
                (operational_summary.get("research_challengers") or {}).get("entry_eligibility_historical_falsification")
                or {}
            )
            set_runtime_state(
                "entry_eligibility_falsification_snapshot",
                json.dumps(entry_eligibility_falsification_runtime, ensure_ascii=False, default=str),
            )
            set_runtime_state(
                "entry_eligibility_dual_lane_decision",
                json.dumps(
                    entry_eligibility_falsification_runtime.get("decision") or {},
                    ensure_ascii=False,
                    default=str,
                ),
            )
            momentum_direction_runtime = (
                (operational_summary.get("research_challengers") or {}).get("momentum_direction_audit")
                or {}
            )
            set_runtime_state(
                "momentum_direction_audit_snapshot",
                json.dumps(momentum_direction_runtime, ensure_ascii=False, default=str),
            )
            momentum_attrition_runtime = (
                (operational_summary.get("research_challengers") or {}).get("momentum_edge_attrition")
                or {}
            )
            set_runtime_state(
                "momentum_edge_attrition_snapshot",
                json.dumps(momentum_attrition_runtime, ensure_ascii=False, default=str),
            )
            momentum_reconciliation_runtime = dict(payload.get("momentum_reconciliation") or {})
            trusted_momentum_reconciliation_runtime = (
                (operational_summary.get("research_challengers") or {}).get("trusted_momentum_reconciliation")
                or {}
            )
            fixed_benchmark_momentum_runtime = (
                (operational_summary.get("research_challengers") or {}).get("fixed_benchmark_momentum_reconciliation")
                or {}
            )
            momentum_reconciliation_runtime["trusted_point_in_time"] = trusted_momentum_reconciliation_runtime
            momentum_reconciliation_runtime["fixed_benchmark_trusted_point_in_time"] = fixed_benchmark_momentum_runtime
            momentum_reconciliation_runtime["waterfall_reference_reproduced_all_horizons"] = (
                trusted_momentum_reconciliation_runtime.get("waterfall_reference_reproduced_all_horizons")
            )
            momentum_reconciliation_runtime["status"] = (
                "EVALUABLE"
                if str(momentum_reconciliation_runtime.get("status") or "").upper() == "EVALUABLE"
                and str(trusted_momentum_reconciliation_runtime.get("status") or "").upper() == "EVALUABLE"
                else "RECONCILIATION_INCOMPLETE"
            )
            set_runtime_state(
                "trusted_momentum_reconciliation_snapshot",
                json.dumps(trusted_momentum_reconciliation_runtime, ensure_ascii=False, default=str),
            )
            set_runtime_state(
                "fixed_benchmark_momentum_reconciliation_snapshot",
                json.dumps(fixed_benchmark_momentum_runtime, ensure_ascii=False, default=str),
            )
            set_runtime_state(
                "momentum_reconciliation_snapshot",
                json.dumps(momentum_reconciliation_runtime, ensure_ascii=False, default=str),
            )
            thesis_quality_runtime = (
                (operational_summary.get("research_challengers") or {}).get("thesis_quality_completion_walkforward")
                or {}
            )
            set_runtime_state(
                "thesis_quality_completion_snapshot",
                json.dumps(thesis_quality_runtime, ensure_ascii=False, default=str),
            )
            operational_basis_finished = _replay_basis()
            set_runtime_state(
                "historical_operational_validation_summary",
                json.dumps(operational_summary, ensure_ascii=False, default=str),
            )
            semantic_quality = operational_summary.get("semantic_quality") or {}
            if str(semantic_quality.get("status") or "").upper() != "VALID":
                raise RuntimeError(
                    "Historical operational replay semantic validation failed: "
                    + json.dumps(semantic_quality, ensure_ascii=False, default=str)
                )
            full_lab = payload.get("full_history_swing_lab") or {}
            stability = payload.get("rolling_regime_stability_challenger") or {}
            router = payload.get("regime_router_daily_pair_challenger") or {}
            coverage = full_lab.get("coverage") or {}
            if coverage.get("full_history_not_truncated") is not True:
                raise RuntimeError(
                    "Historical attribution remains truncated; full-history validation cannot publish success"
                )
            attribution_basis_finished = _attribution_cache_basis()
        research_snapshot_summary = compact_snapshot_summary(research_snapshot)

        # We are outside the frozen context here. Production may legitimately
        # have advanced for hours while research worked on T0. That is reported,
        # but it cannot invalidate a result whose own input view was immutable.
        live_attribution_basis_finished = _attribution_cache_basis()
        live_operational_basis_finished = _replay_basis()

        frozen_attribution_input_drift = attribution_basis_finished != attribution_basis_started
        frozen_operational_input_drift = operational_basis_finished != operational_basis_started
        frozen_attribution_drift_detail = _basis_diff(attribution_basis_started, attribution_basis_finished)
        frozen_operational_drift_detail = _basis_diff(operational_basis_started, operational_basis_finished)

        live_attribution_input_drift = live_attribution_basis_finished != live_attribution_basis_started
        live_operational_input_drift = live_operational_basis_finished != live_operational_basis_started
        live_attribution_drift_detail = _basis_diff(live_attribution_basis_started, live_attribution_basis_finished)
        live_operational_drift_detail = _basis_diff(live_operational_basis_started, live_operational_basis_finished)

        operational_conclusion_allowed = not frozen_operational_input_drift
        attribution_conclusion_allowed = not frozen_attribution_input_drift
        strategic_conclusion_allowed = operational_conclusion_allowed and attribution_conclusion_allowed

        if frozen_operational_input_drift:
            validity_status = "FROZEN_OPERATIONAL_INPUT_DRIFT_INTERNAL_ERROR"
        elif frozen_attribution_input_drift:
            validity_status = "FROZEN_ATTRIBUTION_INPUT_DRIFT_INTERNAL_ERROR"
        else:
            validity_status = "VALID_FROZEN_INPUTS"

        research_validity = {
            "status": validity_status,
            "strategic_conclusion_allowed": strategic_conclusion_allowed,
            "operational_conclusion_allowed": operational_conclusion_allowed,
            "attribution_conclusion_allowed": attribution_conclusion_allowed,
            "operational_basis_frozen": not frozen_operational_input_drift,
            "attribution_basis_frozen": not frozen_attribution_input_drift,
            "frozen_operational_drift": frozen_operational_drift_detail,
            "frozen_attribution_drift": frozen_attribution_drift_detail,
            "live_inputs_advanced_during_replay": bool(
                live_operational_input_drift or live_attribution_input_drift
            ),
            "live_operational_drift": live_operational_drift_detail,
            "live_attribution_drift": live_attribution_drift_detail,
            "snapshot": research_snapshot_summary,
            "reason": (
                "Frozen T0 inputs changed internally; research result is invalid."
                if not strategic_conclusion_allowed else
                "Research used an immutable T0 snapshot. Production could continue advancing independently without invalidating the replay."
            ),
        }

        # Backwards-compatible result fields now mean drift inside the frozen
        # research view; LIVE drift is exposed separately below.
        attribution_input_drift = frozen_attribution_input_drift
        operational_input_drift = frozen_operational_input_drift

        # Persist the exact basis observed before Attribution. If inputs advanced
        # during the long replay, the next run will miss cache rather than falsely
        # claiming the just-computed payload covered newer inputs.
        set_runtime_state(
            "alpha_pnl_attribution_basis",
            json.dumps(attribution_basis_started, ensure_ascii=False, default=str),
        )
        swing_v2_payload = payload.get("swing_v2") or {}
        legacy_reference = (
            (((operational_summary.get("product_replay") or {}).get("current_policy_counterfactual") or {}).get("metrics"))
            or {}
        )
        swing_v2_runtime = {
            "mode": swing_v2_payload.get("mode"),
            "model_version": swing_v2_payload.get("model_version"),
            "canonical_rows": swing_v2_payload.get("canonical_rows"),
            "canonical_dates": swing_v2_payload.get("canonical_dates"),
            "historical_gate": swing_v2_payload.get("historical_gate") or {},
            "prospective_audit": swing_v2_payload.get("prospective_audit") or {},
            "promotion_ready": bool(swing_v2_payload.get("promotion_ready")),
            "legacy_current_policy_reference": {
                "closed": legacy_reference.get("closed"),
                "avg_underlying_return_pct": legacy_reference.get("avg_underlying_return_pct"),
                "underlying_profit_factor": legacy_reference.get("underlying_profit_factor"),
                "win_rate_pct": legacy_reference.get("win_rate_pct"),
                "comparable_population": False,
                "note": "Reference only: current-policy Top-N/FUSION population differs from Swing V2 full-universe same-slot tests.",
            },
            "attribution_conclusion_allowed": attribution_conclusion_allowed,
            "strategic_conclusion_allowed": bool(
                strategic_conclusion_allowed and swing_v2_payload.get("promotion_ready")
            ),
            "by_horizon": {
                str(h): {
                    "status": ((swing_v2_payload.get("by_horizon") or {}).get(str(h)) or {}).get("status"),
                    "selected": ((swing_v2_payload.get("by_horizon") or {}).get(str(h)) or {}).get("selected") or {},
                    "baselines": ((swing_v2_payload.get("by_horizon") or {}).get(str(h)) or {}).get("baselines") or {},
                    "momentum_baseline_slot_audit_v8": ((swing_v2_payload.get("by_horizon") or {}).get(str(h)) or {}).get("momentum_baseline_slot_audit_v8") or {},
                    "median_validation_threshold_abs_score": ((swing_v2_payload.get("by_horizon") or {}).get(str(h)) or {}).get("median_validation_threshold_abs_score"),
                }
                for h in (2, 3, 5)
            },
        }
        set_runtime_state("swing_v2_replay_summary", json.dumps(swing_v2_runtime, ensure_ascii=False, default=str))

        result = {
            "status": "ok",
            "replay_mode": replay_mode,
            "attribution_cache_mode": attribution_cache_mode,
            "attribution_basis_migration": basis_migration,
            "attribution_targeted_refresh": targeted_refresh,
            "attribution_input_drift_during_run": attribution_input_drift,
            "operational_input_drift_during_run": operational_input_drift,
            "live_attribution_input_drift_during_run": live_attribution_input_drift,
            "live_operational_input_drift_during_run": live_operational_input_drift,
            "research_snapshot": research_snapshot_summary,
            "research_benchmark": research_benchmark_summary,
            "research_feature_benchmark": research_feature_benchmark_summary,
            "research_validity": research_validity,
            "lifecycle_research": lifecycle_research,
            "swing_v2": swing_v2_runtime,
            "momentum_reconciliation": momentum_reconciliation_runtime,
            "started_at_madrid": started,
            "finished_at_madrid": ts_madrid_str(),
            "producer_version": str(payload.get("producer_version") or _production_version()),
            "mature_rows": coverage.get("raw_mature_rows"),
            "history_dates": coverage.get("dates"),
            "history_not_truncated": coverage.get("full_history_not_truncated"),
            "full_history_status": full_lab.get("status"),
            "attribution_performance": payload.get("performance") or {},
            "regime_router_swing_2_5": router.get("swing_2_5") or {},
            "rolling_stability_swing_2_5": stability.get("swing_2_5") or {},
            "rolling_stability_eligible_lanes": stability.get("eligible_lane_count"),
            "operational_validation": operational_summary,
            "operational_artifacts": operational_artifacts,
            "full_history_views": {
                name: {
                    "status": block.get("status"),
                    "rows": block.get("rows"),
                    "dates": block.get("dates"),
                    "positive_routed_horizons": block.get("positive_routed_horizons") or [],
                    "router_swing_2_5": block.get("router_swing_2_5") or {},
                    "stability_swing_2_5": block.get("stability_swing_2_5") or {},
                }
                for name, block in (full_lab.get("views") or {}).items()
            },
        }
        replay_status = "ok" if research_validity["strategic_conclusion_allowed"] else "ok_input_drift"
        set_runtime_state("historical_replay_status", replay_status)
        set_runtime_state("historical_replay_research_validity", json.dumps(research_validity, ensure_ascii=False, default=str))
        set_runtime_state("historical_replay_summary", json.dumps(result, ensure_ascii=False, default=str))
        set_runtime_state("historical_replay_finished_at_madrid", result["finished_at_madrid"])
        set_runtime_state(
            "historical_replay_basis",
            json.dumps(
                {
                    **current_basis,
                    "validated_at_madrid": result["finished_at_madrid"],
                    "producer_version": result.get("producer_version"),
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        elapsed = time.monotonic() - monotonic_started
        _set_replay_phase("COMPLETE", _duration_text(elapsed))
        phase_timings = _phase_timings_snapshot(elapsed)
        result["phase_timings"] = phase_timings
        adaptive_handoff = launch_adaptive_after_full(
            replay_mode=replay_mode,
            strategic_conclusion_allowed=bool(
                (result.get("research_validity") or {}).get(
                    "strategic_conclusion_allowed"
                )
            ),
            output_dir=Path(config.OUTPUT_DIR),
            runtime_root=Path(config.RUNTIME_ROOT),
            program_dir=PROGRAM_DIR,
            python_executable=sys.executable,
            producer_version=str(result.get("producer_version") or _production_version()),
            replay_started_at=str(result.get("started_at_madrid") or ""),
            enabled=bool(
                getattr(config, "ADAPTIVE_RESEARCH_AUTO_RUN_AFTER_FULL", True)
            ),
        )
        result["adaptive_research_auto_launch"] = adaptive_handoff
        try:
            set_runtime_state(
                "adaptive_research_auto_launch",
                json.dumps(adaptive_handoff, ensure_ascii=False, default=str),
            )
        except Exception:
            pass
        performance_payload = {
            "schema_version": 1,
            "status": replay_status,
            "producer_version": result.get("producer_version"),
            "started_at_madrid": result.get("started_at_madrid"),
            "finished_at_madrid": result.get("finished_at_madrid"),
            "replay_mode": result.get("replay_mode"),
            "attribution_cache_mode": result.get("attribution_cache_mode"),
            "phase_timings": phase_timings,
            "live_mutation": False,
        }
        performance_path = _write_replay_performance_sidecar(performance_payload)
        result["replay_performance_artifact"] = str(performance_path)
        set_runtime_state(
            "historical_replay_phase_timings",
            json.dumps(phase_timings, ensure_ascii=False, default=str),
        )
        set_runtime_state(
            "historical_replay_summary",
            json.dumps(result, ensure_ascii=False, default=str),
        )
        watchdog_stop.set()
        _discord_send(_completion_message(result, elapsed))
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        error = {
            "status": "error",
            "started_at_madrid": started,
            "finished_at_madrid": ts_madrid_str(),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        }
        try:
            set_runtime_state("historical_replay_status", "error")
            set_runtime_state("historical_replay_summary", json.dumps(error, ensure_ascii=False, default=str))
            set_runtime_state("historical_replay_finished_at_madrid", error["finished_at_madrid"])
        except Exception:
            pass
        elapsed = time.monotonic() - monotonic_started
        _set_replay_phase("ERROR", str(error.get("error") or "unknown")[:500])
        phase_timings = _phase_timings_snapshot(elapsed)
        error["phase_timings"] = phase_timings
        try:
            performance_path = _write_replay_performance_sidecar({
                "schema_version": 1,
                "status": "error",
                "producer_version": _production_version(),
                "started_at_madrid": started,
                "finished_at_madrid": error.get("finished_at_madrid"),
                "phase_timings": phase_timings,
                "error": error.get("error"),
                "live_mutation": False,
            })
            error["replay_performance_artifact"] = str(performance_path)
            set_runtime_state(
                "historical_replay_phase_timings",
                json.dumps(phase_timings, ensure_ascii=False, default=str),
            )
            set_runtime_state(
                "historical_replay_summary",
                json.dumps(error, ensure_ascii=False, default=str),
            )
        except Exception:
            pass
        watchdog_stop.set()
        _discord_send(
            "❌ **Reconstrucción histórica TMAD fallida.**\n"
            f"Duración: {_duration_text(elapsed)}\n"
            f"Error: {str(error.get('error') or 'desconocido')[:1200]}\n"
            "Producción sigue operativa; el fallo afecta sólo al laboratorio histórico."
        )
        print(json.dumps(error, ensure_ascii=False, default=str), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
