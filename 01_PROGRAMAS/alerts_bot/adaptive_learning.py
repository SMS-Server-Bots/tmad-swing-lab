from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import config
from db import connect, get_active_model, get_runtime_state, set_runtime_state
from discord_presentation import build_group
from discord_utils import log_info, send_daily
from historical_evidence import history_maturity_snapshot
from lifecycle_research import run_lifecycle_research
from model_metrics import normalized_oos_metrics
from reverse_alpha_learning import extract_reverse_learning_evidence, reverse_specialist_plans
from time_utils import now_madrid, ts_madrid_str, ts_utc_str


# v6.1.25: controlled continuous-learning coordinator.
# It never edits source code and never promotes LIVE automatically. It consumes
# the point-in-time auditor/model registry, proposes bounded changes, promotes
# only a SHADOW override and can remove that override automatically.


def _f(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return default


def _pct(value: Any) -> str:
    number = _f(value, None)
    return "n/a" if number is None else f"{number:.1f}%"


def _num(value: Any, digits: int = 2) -> str:
    number = _f(value, None)
    return "n/a" if number is None else f"{number:.{digits}f}"


def _json_load(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return {} if default is None else default


def ensure_adaptive_learning_schema() -> None:
    conn = connect(config.ALERTS_DB)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS adaptive_learning_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at_utc TEXT NOT NULL,
                created_at_madrid TEXT NOT NULL,
                context TEXT,
                state TEXT NOT NULL,
                active_model_id INTEGER,
                shadow_model_id INTEGER,
                regime TEXT,
                audit_key TEXT,
                source_feature_dates INTEGER DEFAULT 0,
                mature_movements INTEGER DEFAULT 0,
                core_signal_sample INTEGER DEFAULT 0,
                drift_status TEXT,
                dominant_failure TEXT,
                research_gate_current REAL,
                research_gate_proposed REAL,
                live_gate_current REAL,
                live_gate_proposed REAL,
                retrain_requested INTEGER DEFAULT 0,
                promotion_action TEXT,
                rollback_action TEXT,
                payload_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_adaptive_learning_created
                ON adaptive_learning_runs(created_at_utc);

            CREATE TABLE IF NOT EXISTS adaptive_model_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learning_run_id INTEGER NOT NULL,
                model_version_id INTEGER NOT NULL,
                model_key TEXT,
                model_status TEXT,
                role TEXT,
                sample INTEGER DEFAULT 0,
                market_dates INTEGER DEFAULT 0,
                hit_rate REAL,
                avg_return REAL,
                profit_factor REAL,
                max_drawdown REAL,
                avg_hit_sessions REAL,
                utility REAL,
                safety_pass INTEGER DEFAULT 0,
                selected_for_shadow INTEGER DEFAULT 0,
                metrics_json TEXT NOT NULL,
                UNIQUE(learning_run_id,model_version_id)
            );
            CREATE INDEX IF NOT EXISTS idx_adaptive_model_metrics_model
                ON adaptive_model_metrics(model_version_id,learning_run_id);

            CREATE TABLE IF NOT EXISTS adaptive_threshold_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learning_run_id INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,
                threshold_name TEXT NOT NULL,
                current_value REAL,
                proposed_value REAL,
                applied_value REAL,
                scope TEXT NOT NULL,
                reason TEXT,
                evidence_json TEXT,
                UNIQUE(learning_run_id,threshold_name,scope)
            );

            CREATE TABLE IF NOT EXISTS adaptive_promotion_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learning_run_id INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,
                candidate_model_id INTEGER,
                previous_model_id INTEGER,
                decision TEXT NOT NULL,
                scope TEXT NOT NULL,
                candidate_utility REAL,
                previous_utility REAL,
                evidence_json TEXT
            );

            CREATE TABLE IF NOT EXISTS adaptive_rollback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learning_run_id INTEGER NOT NULL,
                created_at_utc TEXT NOT NULL,
                from_model_id INTEGER,
                to_model_id INTEGER,
                reason TEXT NOT NULL,
                evidence_json TEXT
            );

            CREATE TABLE IF NOT EXISTS adaptive_regime_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learning_run_id INTEGER NOT NULL,
                model_version_id INTEGER,
                regime TEXT NOT NULL,
                movement_events INTEGER DEFAULT 0,
                core_detected INTEGER DEFAULT 0,
                enter_generated INTEGER DEFAULT 0,
                coverage_pct REAL,
                enter_recall_pct REAL,
                UNIQUE(learning_run_id,model_version_id,regime)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _latest_audit() -> dict:
    path = config.MARKET_OUTCOME_AUDITOR_OUTPUT_DIR / "latest_market_outcome_audit.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _base_active_model() -> dict:
    """Return the validated model_state model, ignoring any SHADOW router override."""
    conn = connect(config.ALERTS_DB)
    try:
        row = conn.execute(
            """SELECT mv.* FROM model_versions mv
               JOIN model_state ms ON ms.model_version_id=mv.id AND ms.state_key='active'
               ORDER BY mv.id DESC LIMIT 1"""
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM model_versions WHERE status IN ('champion','provisional_champion','bootstrap') ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return {}
        item = dict(row)
        item["params"] = _json_load(item.get("params_json"), {})
        item["metrics"] = _json_load(item.get("metrics_json"), {})
        return item
    finally:
        conn.close()


def _model_rows(limit: int | None = None) -> list[dict]:
    ensure_adaptive_learning_schema()
    conn = connect(config.ALERTS_DB)
    try:
        sql = "SELECT * FROM model_versions ORDER BY id DESC"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (max(1, int(limit)),)
        rows = []
        for raw in conn.execute(sql, params).fetchall():
            item = dict(raw)
            item["params"] = _json_load(item.get("params_json"), {})
            item["metrics"] = _json_load(item.get("metrics_json"), {})
            rows.append(item)
        return rows
    finally:
        conn.close()


def _latest_regime() -> dict:
    conn = connect(config.ALERTS_DB)
    try:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "market_regime_history" not in tables:
            return {"regime": "UNKNOWN", "run_id": 0}
        row = conn.execute(
            "SELECT * FROM market_regime_history ORDER BY source_scanner_run_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"regime": "UNKNOWN", "run_id": 0}
        item = dict(row)
        return {
            "regime": str(item.get("market_regime") or item.get("correlation_regime") or "UNKNOWN").upper(),
            "run_id": _i(item.get("source_scanner_run_id")),
            "snapshot_date": item.get("snapshot_date"),
            "market_context": _f(item.get("market_context_score"), None),
            "breadth": _f(item.get("market_breadth_score"), None),
            "vix_stress": _f(item.get("vix_stress_score"), None),
            "entry_policy": item.get("entry_policy"),
        }
    except Exception:
        return {"regime": "UNKNOWN", "run_id": 0}
    finally:
        conn.close()


def _model_metrics(model: dict) -> dict:
    oos = normalized_oos_metrics(model.get("metrics") or {})
    hit = _f(oos.get("target_hit_rate") if oos.get("target_hit_rate") is not None else oos.get("win_rate"), 0.0) or 0.0
    avg_return = _f(oos.get("avg_return"), 0.0) or 0.0
    pf = _f(oos.get("profit_factor"), 0.0) or 0.0
    dd = abs(_f(oos.get("max_drawdown"), 999.0) or 999.0)
    sample = _i(oos.get("sample"))
    dates = _i(oos.get("market_dates"))
    avg_hit_sessions = _f(
        oos.get("avg_sessions_to_target")
        if oos.get("avg_sessions_to_target") is not None
        else oos.get("avg_hit_sessions"),
        None,
    )
    # Stable 0..100 utility. Coverage is evaluated prospectively by the auditor;
    # candidate comparison uses only purged OOS metrics available for every model.
    hit_component = max(0.0, min(1.0, hit / 100.0))
    pf_component = max(0.0, min(1.0, pf / 4.0))
    avg_component = max(0.0, min(1.0, (avg_return + 1.0) / 6.0))
    dd_component = max(0.0, min(1.0, 1.0 - dd / max(1.0, float(config.ADAPTIVE_MAX_SHADOW_DRAWDOWN_PCT))))
    sample_component = max(0.0, min(1.0, sample / max(1.0, float(config.ADAPTIVE_TARGET_OOS_SAMPLE))))
    lead_component = 0.5 if avg_hit_sessions is None else max(0.0, min(1.0, (6.0 - avg_hit_sessions) / 5.0))
    utility = 100.0 * (
        0.30 * hit_component
        + 0.22 * pf_component
        + 0.13 * avg_component
        + 0.15 * dd_component
        + 0.10 * sample_component
        + 0.10 * lead_component
    )
    safety = bool(
        sample >= int(config.ADAPTIVE_MIN_OOS_SAMPLE)
        and dates >= int(config.ADAPTIVE_MIN_OOS_DATES)
        and hit >= float(config.ADAPTIVE_MIN_SHADOW_HIT_RATE)
        and pf >= float(config.ADAPTIVE_MIN_SHADOW_PROFIT_FACTOR)
        and avg_return > 0.0
        and dd <= float(config.ADAPTIVE_MAX_SHADOW_DRAWDOWN_PCT)
    )
    return {
        "sample": sample,
        "market_dates": dates,
        "hit_rate": round(hit, 4),
        "avg_return": round(avg_return, 6),
        "profit_factor": round(pf, 6),
        "max_drawdown": round(dd, 6),
        "avg_hit_sessions": avg_hit_sessions,
        "utility": round(utility, 4),
        "safety_pass": safety,
        "raw": oos,
    }



RESEARCH_MODEL_STATUSES = {
    "champion", "provisional_champion", "challenger",
    "discovery_shadow", "retired", "shadow_bridge",
}


def select_adaptive_model_pool(
    all_models: list[dict],
    active_model: dict | None,
    recent_limit: int,
) -> list[dict]:
    """Bounded model memory: recent models + strongest older OOS evidence + active.

    A fixed "last N ids" window can forget an economically strong challenger
    simply because several later experiments were written to the registry. This
    selector keeps recency while retaining a small research memory ranked by the
    same OOS utility used by Adaptive Learning. Promotion rules are unchanged.
    """
    recent_limit = max(4, int(recent_limit))
    recent = list(all_models[:recent_limit])
    recent_ids = {_i(model.get("id")) for model in recent}
    historical_cap = max(4, min(12, recent_limit))

    historical: list[tuple[float, int, int, int, dict]] = []
    for model in all_models[recent_limit:]:
        model_id = _i(model.get("id"))
        status = str(model.get("status") or "").lower()
        if model_id <= 0 or model_id in recent_ids or status not in RESEARCH_MODEL_STATUSES:
            continue
        metrics = _model_metrics(model)
        if (
            _i(metrics.get("sample")) < int(config.ADAPTIVE_MIN_OOS_SAMPLE)
            or _i(metrics.get("market_dates")) < int(config.ADAPTIVE_MIN_OOS_DATES)
            or (_f(metrics.get("avg_return"), 0.0) or 0.0) <= 0.0
            or (_f(metrics.get("profit_factor"), 0.0) or 0.0) <= 0.0
        ):
            continue
        historical.append((
            _f(metrics.get("utility"), 0.0) or 0.0,
            _i(metrics.get("sample")),
            _i(metrics.get("market_dates")),
            model_id,
            model,
        ))
    historical.sort(key=lambda item: item[:4], reverse=True)

    selected = recent + [item[4] for item in historical[:historical_cap]]
    active = dict(active_model or {})
    active_id = _i(active.get("id"))
    if active_id > 0 and all(_i(model.get("id")) != active_id for model in selected):
        selected.append(active)

    dedup: dict[int, dict] = {}
    ordered: list[dict] = []
    for model in selected:
        model_id = _i(model.get("id"))
        if model_id <= 0 or model_id in dedup:
            continue
        dedup[model_id] = model
        ordered.append(model)
    return ordered[: recent_limit + historical_cap + 1]


def _adaptive_model_pool(limit: int) -> list[dict]:
    recent_limit = max(4, int(limit))
    history_limit = max(80, min(250, recent_limit * 12))
    all_models = _model_rows(limit=history_limit)
    active = _base_active_model() or {}
    return select_adaptive_model_pool(all_models, active, recent_limit)

def _assign_roles(models: list[dict]) -> dict[int, str]:
    if not models:
        return {}
    metrics = {int(model["id"]): _model_metrics(model) for model in models}
    roles: dict[int, set[str]] = defaultdict(set)
    active_id = _i((_base_active_model() or {}).get("id"))
    if active_id:
        roles[active_id].add("GLOBAL_CHAMPION")
    candidates = [m for m in models if _i(m.get("id")) != active_id]
    if candidates:
        roles[max(candidates, key=lambda m: metrics[_i(m["id"])]["hit_rate"])["id"]].add("PRECISION")
        roles[max(candidates, key=lambda m: metrics[_i(m["id"])]["sample"])["id"]].add("COVERAGE_PROXY")
        roles[min(candidates, key=lambda m: metrics[_i(m["id"])]["max_drawdown"])["id"]].add("LOW_DRAWDOWN")
        lead_candidates = [m for m in candidates if metrics[_i(m["id"])]["avg_hit_sessions"] is not None]
        if lead_candidates:
            roles[min(lead_candidates, key=lambda m: metrics[_i(m["id"])]["avg_hit_sessions"])["id"]].add("EARLY_DETECTION")
    return {model_id: "+".join(sorted(values)) or "BALANCED" for model_id, values in roles.items()}


def _audit_evidence(audit: dict) -> dict:
    metrics = audit.get("metrics") or {}
    funnel = metrics.get("funnel") or {}
    quality = metrics.get("signal_quality") or {}
    core_quality = quality.get("core") or {}
    gex = metrics.get("gex_value") or {}
    drift = metrics.get("drift") or {}
    anticipation = metrics.get("anticipation") or {}
    diagnostics = audit.get("diagnostics") or {}
    classes = diagnostics.get("classifications") or {}
    reverse_alpha = extract_reverse_learning_evidence(audit)
    movements = _i(funnel.get("movements"))
    core_sample = _i(core_quality.get("sample"))
    enough_movements = movements >= int(config.ADAPTIVE_MIN_MATURE_MOVEMENTS)
    enough_signals = core_sample >= int(config.ADAPTIVE_MIN_MATURE_SIGNALS)
    precision_evaluable = enough_signals
    coverage_evaluable = enough_movements
    dominant_failure = "NONE"
    if classes:
        dominant_failure = max(classes.items(), key=lambda item: (_i(item[1]), str(item[0])))[0]
    return {
        "movements": movements,
        "core_signal_sample": core_sample,
        "source_feature_dates": _i(audit.get("source_feature_dates")),
        "coverage": _f(funnel.get("core_detected_pct"), None) if coverage_evaluable else None,
        "research_recall": _f(funnel.get("research_reached_pct"), None) if coverage_evaluable else None,
        "enter_recall": _f(funnel.get("enter_generated_pct"), None) if coverage_evaluable else None,
        "precision": _f(core_quality.get("hit_rate"), None) if precision_evaluable else None,
        "lead": _f(anticipation.get("median_lead_sessions"), None) if coverage_evaluable else None,
        "late_pct": _f(anticipation.get("late_pct"), None) if coverage_evaluable else None,
        "gex_lift": _f(gex.get("lift_pp"), None) if _i(gex.get("confirmed_sample")) >= int(config.ADAPTIVE_MIN_GEX_SAMPLE) else None,
        "good_blocked": _i(gex.get("good_opportunities_blocked")),
        "blocking_precision": _f(gex.get("blocking_precision"), None) if _i(gex.get("blocked_sample")) >= int(config.ADAPTIVE_MIN_GEX_SAMPLE) else None,
        "drift_status": str(drift.get("status") or "ACCUMULATING").upper(),
        "drift_warnings": list(drift.get("warnings") or []),
        "recent_coverage": _f(drift.get("recent_coverage"), None),
        "previous_coverage": _f(drift.get("previous_coverage"), None),
        "oos_hit": _f(drift.get("oos_hit_rate"), None),
        "prospective_hit": _f(drift.get("prospective_hit_rate"), None) if precision_evaluable else None,
        "classes": {str(k): _i(v) for k, v in classes.items()},
        "dominant_failure": str(dominant_failure),
        "enough_movements": enough_movements,
        "enough_signals": enough_signals,
        "audit_key": str(audit.get("audit_key") or ""),
        "status": str(audit.get("status") or "ACCUMULATING").upper(),
        "probability_calibration": dict(core_quality.get("probability_calibration") or quality.get("probability_calibration") or {}),
        "reverse_alpha": reverse_alpha,
    }


def _threshold_proposal(evidence: dict) -> dict:
    current_research = float(effective_research_gate(use_runtime=False))
    proposed_research = current_research
    reasons: list[str] = []
    coverage = evidence.get("coverage")
    precision = evidence.get("precision")
    oos = evidence.get("oos_hit")
    dominant = str(evidence.get("dominant_failure") or "")
    if evidence.get("enough_movements"):
        if dominant in {"BELOW_RESEARCH_GATE", "NO_CORE_DIRECTION", "DETECTED_LATE"} and (
            precision is None or oos is None or precision >= oos - float(config.ADAPTIVE_ACCEPTABLE_PRECISION_DROP_PP)
        ):
            proposed_research -= float(config.ADAPTIVE_RESEARCH_GATE_STEP)
            reasons.append(f"coverage/miss={dominant}")
        if precision is not None and oos is not None and precision < oos - float(config.ADAPTIVE_PRECISION_RAISE_GATE_DROP_PP):
            proposed_research += float(config.ADAPTIVE_RESEARCH_GATE_STEP)
            reasons.append("precision_below_oos")
        if evidence.get("late_pct") is not None and float(evidence["late_pct"]) >= float(config.ADAPTIVE_LATE_MOVE_WARNING_PCT):
            proposed_research -= float(config.ADAPTIVE_RESEARCH_GATE_STEP)
            reasons.append("late_detection")
    proposed_research = round(max(float(config.ADAPTIVE_RESEARCH_GATE_MIN), min(float(config.ADAPTIVE_RESEARCH_GATE_MAX), proposed_research)), 2)

    current_live = float(config.PRECISION_FIRST_MIN_LIVE_SCORE)
    proposed_live = current_live
    live_reasons: list[str] = []
    if evidence.get("enough_signals") and precision is not None and oos is not None:
        if precision < oos - float(config.ADAPTIVE_PRECISION_RAISE_GATE_DROP_PP):
            proposed_live = min(float(config.ADAPTIVE_LIVE_GATE_MAX), current_live + 1.0)
            live_reasons.append("prospective_precision_warning")
        elif coverage is not None and coverage < float(config.ADAPTIVE_COVERAGE_WARNING_PCT) and precision >= oos:
            proposed_live = max(float(config.ADAPTIVE_LIVE_GATE_MIN), current_live - 1.0)
            live_reasons.append("coverage_low_precision_stable")
    # LIVE proposals are never applied automatically in v6.1.25.
    return {
        "research_current": current_research,
        "research_proposed": proposed_research,
        "research_reason": ",".join(reasons) or "no_material_change",
        "live_current": current_live,
        "live_proposed": proposed_live,
        "live_reason": ",".join(live_reasons) or "no_material_change",
    }


def effective_research_gate(*, use_runtime: bool = True) -> float:
    base = float(config.FUSION_MIN_ACTIONABLE_SCORE)
    if not use_runtime or not bool(getattr(config, "ADAPTIVE_LEARNING_ENABLED", True)):
        return base
    entry_mode = str(get_runtime_state("entry_mode", "shadow") or "shadow").lower()
    if entry_mode == "live":
        return base
    value = _f(get_runtime_state("adaptive_research_gate_shadow", str(base)), base)
    return float(max(float(config.ADAPTIVE_RESEARCH_GATE_MIN), min(float(config.ADAPTIVE_RESEARCH_GATE_MAX), value or base)))


def calibrated_probability(raw_probability: Any) -> float | None:
    raw = _f(raw_probability, None)
    if raw is None:
        return None
    mapping = _json_load(get_runtime_state("adaptive_probability_map", "{}"), {})
    if not mapping:
        return raw
    if raw < 50:
        label = "<50"
    elif raw < 60:
        label = "50-59"
    elif raw < 70:
        label = "60-69"
    elif raw < 80:
        label = "70-79"
    elif raw < 90:
        label = "80-89"
    else:
        label = "90+"
    row = mapping.get(label) or {}
    sample = _i(row.get("sample"))
    observed = _f(row.get("observed_hit_rate"), None)
    if sample < int(config.ADAPTIVE_PROBABILITY_MIN_BIN_SAMPLE) or observed is None:
        return raw
    # Conservative shrinkage: never jump fully to a small empirical bin.
    weight = min(0.75, sample / max(float(config.ADAPTIVE_PROBABILITY_FULL_WEIGHT_SAMPLE), 1.0))
    return round(raw * (1.0 - weight) + observed * weight, 3)


def _regime_metrics(active_model_id: int) -> list[dict]:
    conn = connect(config.ALERTS_DB)
    try:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"market_outcome_audit_events", "market_outcome_audit_runs", "market_regime_history"}.issubset(tables):
            return []
        rows = conn.execute(
            """
            SELECT COALESCE(m.market_regime,'UNKNOWN') AS regime,
                   COUNT(*) AS movements,
                   SUM(e.core_detected) AS core_detected,
                   SUM(e.enter_generated) AS enter_generated
            FROM market_outcome_audit_events e
            JOIN market_outcome_audit_runs a ON a.id=e.audit_run_id
            LEFT JOIN market_regime_history m ON m.snapshot_date=e.signal_date
            WHERE a.id=(SELECT MAX(id) FROM market_outcome_audit_runs)
            GROUP BY COALESCE(m.market_regime,'UNKNOWN')
            ORDER BY movements DESC
            """
        ).fetchall()
        result = []
        for row in rows:
            movements = _i(row["movements"])
            core = _i(row["core_detected"])
            enter = _i(row["enter_generated"])
            result.append({
                "model_version_id": active_model_id,
                "regime": str(row["regime"] or "UNKNOWN").upper(),
                "movements": movements,
                "core_detected": core,
                "enter_generated": enter,
                "coverage_pct": round(core * 100.0 / movements, 3) if movements else None,
                "enter_recall_pct": round(enter * 100.0 / movements, 3) if movements else None,
            })
        return result
    except Exception:
        return []
    finally:
        conn.close()


def _specialist_plan(evidence: dict) -> list[str]:
    classes = evidence.get("classes") or {}
    plans: list[str] = []
    lifecycle = evidence.get("lifecycle_counterfactual") or {}
    if _i(classes.get("NO_CORE_DIRECTION")):
        plans.append("HIGH_COVERAGE")
    if _i(classes.get("DETECTED_LATE")):
        plans.append("EARLY_DETECTION")
    if _i(classes.get("WRONG_DIRECTION")):
        plans.append("DIRECTIONAL_PRECISION")
    if _i(classes.get("GEX_NOT_CONFIRMED")) or evidence.get("good_blocked", 0):
        plans.append("GEX_VALUE")
    if _i(classes.get("BELOW_RESEARCH_GATE")) or _i(classes.get("FUSION_GATE")):
        plans.append("ADAPTIVE_GATE")
    for plan in reverse_specialist_plans(evidence.get("reverse_alpha") or {}):
        if plan not in plans:
            plans.append(plan)
    reversal_sample = _i(lifecycle.get("pattern_reversal_sample"))
    false_exit_pct = _f(lifecycle.get("target_after_pattern_reversal_pct"), None)
    hold2_edge = _f(((lifecycle.get("fixed_hold_comparison") or {}).get("2") or {}).get("fixed_hold_edge_pp"), None)
    if reversal_sample >= 5 and (
        (false_exit_pct is not None and false_exit_pct >= 15.0)
        or (hold2_edge is not None and hold2_edge >= 0.50)
    ):
        plans.append("EXIT_POLICY")
    if not plans:
        plans.append("BALANCED_GLOBAL")
    return plans[:4]


def _shadow_override_model(models: list[dict]) -> dict | None:
    override_id = _i(get_runtime_state("adaptive_shadow_model_id", "0"))
    return next((m for m in models if _i(m.get("id")) == override_id), None)


def _promotion_and_rollback(models: list[dict], evidence: dict, threshold: dict, fingerprint: str) -> dict:
    active = _base_active_model() or {}
    active_id = _i(active.get("id"))
    active_metrics = _model_metrics(active) if active else {}
    override = _shadow_override_model(models)
    action = "NONE"
    rollback = "NONE"
    candidate: dict | None = None
    decision_reason = "insufficient_evidence"

    # Rollback watch first. The override is SHADOW-only; clearing it restores the
    # regular champion/provisional selection immediately.
    if override:
        baseline = _json_load(get_runtime_state("adaptive_promotion_baseline", "{}"), {})
        baseline_dates = _i(baseline.get("source_feature_dates"))
        new_dates = max(0, _i(evidence.get("source_feature_dates")) - baseline_dates)
        baseline_precision = _f(baseline.get("precision"), None)
        baseline_coverage = _f(baseline.get("coverage"), None)
        precision = _f(evidence.get("precision"), None)
        coverage = _f(evidence.get("coverage"), None)
        reasons: list[str] = []
        if new_dates >= int(config.ADAPTIVE_ROLLBACK_MIN_NEW_DATES):
            if baseline_precision is not None and precision is not None and precision < baseline_precision - float(config.ADAPTIVE_ROLLBACK_PRECISION_DROP_PP):
                reasons.append(f"precision {precision:.1f}<{baseline_precision:.1f}-{float(config.ADAPTIVE_ROLLBACK_PRECISION_DROP_PP):.1f}")
            if baseline_coverage is not None and coverage is not None and coverage < baseline_coverage - float(config.ADAPTIVE_ROLLBACK_COVERAGE_DROP_PP):
                reasons.append(f"coverage {coverage:.1f}<{baseline_coverage:.1f}-{float(config.ADAPTIVE_ROLLBACK_COVERAGE_DROP_PP):.1f}")
            if evidence.get("drift_status") == "DRIFT" and evidence.get("enough_movements"):
                reasons.append("auditor_drift")
        if reasons:
            previous_id = _i(get_runtime_state("adaptive_shadow_previous_model_id", "0"))
            set_runtime_state("adaptive_shadow_model_id", "0")
            set_runtime_state("adaptive_shadow_previous_model_id", "0")
            set_runtime_state("adaptive_promotion_baseline", "{}")
            rollback = f"ROLLED_BACK_TO_{previous_id or 'DEFAULT'}"
            action = "ROLLBACK"
            decision_reason = ";".join(reasons)
            return {
                "action": action, "rollback": rollback, "candidate": override,
                "candidate_metrics": _model_metrics(override), "active_metrics": active_metrics,
                "reason": decision_reason, "previous_id": previous_id,
            }
        return {
            "action": "SHADOW_VALIDATION", "rollback": "NONE", "candidate": override,
            "candidate_metrics": _model_metrics(override), "active_metrics": active_metrics,
            "reason": f"watching_new_dates={new_dates}/{int(config.ADAPTIVE_ROLLBACK_MIN_NEW_DATES)}",
            "previous_id": _i(get_runtime_state("adaptive_shadow_previous_model_id", "0")),
        }

    if not bool(config.ADAPTIVE_AUTO_PROMOTE_SHADOW):
        return {"action": action, "rollback": rollback, "candidate": None, "active_metrics": active_metrics, "reason": "auto_promote_shadow_disabled"}
    if not (evidence.get("enough_movements") and evidence.get("enough_signals")):
        return {"action": action, "rollback": rollback, "candidate": None, "active_metrics": active_metrics, "reason": decision_reason}

    allowed = {"challenger", "discovery_shadow", "retired", "provisional_champion"}
    candidates = []
    for model in models:
        if _i(model.get("id")) == active_id or str(model.get("status") or "").lower() not in allowed:
            continue
        metrics = _model_metrics(model)
        if metrics["safety_pass"]:
            candidates.append((metrics["utility"], _i(model.get("id")), model, metrics))
    if not candidates:
        return {"action": action, "rollback": rollback, "candidate": None, "active_metrics": active_metrics, "reason": "no_safe_candidate"}
    _utility, _model_id, candidate, candidate_metrics = max(candidates, key=lambda item: (item[0], item[1]))
    active_utility = _f(active_metrics.get("utility"), 0.0) or 0.0
    improvement = float(candidate_metrics["utility"]) - active_utility
    if improvement < float(config.ADAPTIVE_MIN_UTILITY_IMPROVEMENT):
        return {
            "action": "NOT_PROMOTED", "rollback": "NONE", "candidate": candidate,
            "candidate_metrics": candidate_metrics, "active_metrics": active_metrics,
            "reason": f"utility_improvement={improvement:.2f}<{float(config.ADAPTIVE_MIN_UTILITY_IMPROVEMENT):.2f}",
        }

    set_runtime_state("adaptive_shadow_previous_model_id", str(active_id))
    set_runtime_state("adaptive_shadow_model_id", str(_i(candidate.get("id"))))
    set_runtime_state("adaptive_promotion_fingerprint", fingerprint)
    set_runtime_state("adaptive_promotion_baseline", json.dumps({
        "source_feature_dates": evidence.get("source_feature_dates"),
        "precision": evidence.get("precision"),
        "coverage": evidence.get("coverage"),
        "audit_key": evidence.get("audit_key"),
        "created_at_madrid": ts_madrid_str(),
    }, ensure_ascii=False))
    action = "PROMOTED_SHADOW"
    decision_reason = f"utility_improvement={improvement:.2f}; safety_pass"
    return {
        "action": action, "rollback": "NONE", "candidate": candidate,
        "candidate_metrics": candidate_metrics, "active_metrics": active_metrics,
        "reason": decision_reason, "previous_id": active_id,
    }


def _learning_state(evidence: dict, promotion: dict, retrain: bool) -> str:
    if promotion.get("action") == "ROLLBACK":
        return "ROLLED_BACK"
    if promotion.get("action") == "PROMOTED_SHADOW":
        return "PROMOTED_SHADOW"
    if promotion.get("action") == "SHADOW_VALIDATION":
        return "SHADOW_VALIDATION"
    if not evidence.get("enough_movements") or not evidence.get("enough_signals"):
        maturity = evidence.get("history_maturity") or {}
        if maturity.get("history_ready"):
            return "PROSPECTIVE_VALIDATION"
        return "ACCUMULATING"
    if retrain:
        return "RETRAIN_REQUIRED"
    if evidence.get("drift_status") == "DRIFT":
        return "DRIFT_WARNING"
    return "STABLE"


def _retrain_decision(evidence: dict) -> tuple[bool, str]:
    if not bool(config.ADAPTIVE_AUTO_RETRAIN_ENABLED):
        return False, "disabled"
    last_dates = _i(get_runtime_state("adaptive_last_retrain_source_dates", "0"))
    new_dates = max(0, _i(evidence.get("source_feature_dates")) - last_dates)
    dominant = str(evidence.get("dominant_failure") or "")
    miss_count = _i((evidence.get("classes") or {}).get(dominant))
    drift = evidence.get("drift_status") == "DRIFT"
    systematic_misses = bool(
        evidence.get("enough_movements")
        and miss_count >= int(config.ADAPTIVE_MIN_DOMINANT_MISSES)
        and dominant in {"NO_CORE_DIRECTION", "WRONG_DIRECTION", "DETECTED_LATE", "BELOW_RESEARCH_GATE", "GEX_NOT_CONFIRMED"}
    )
    enough_new = new_dates >= int(config.ADAPTIVE_RETRAIN_MIN_NEW_DATES)
    requested = bool(enough_new and (drift or systematic_misses))
    reason = (
        f"new_dates={new_dates}; drift={drift}; dominant={dominant}:{miss_count}"
        if requested else f"waiting new_dates={new_dates}/{int(config.ADAPTIVE_RETRAIN_MIN_NEW_DATES)} drift={drift} dominant={dominant}:{miss_count}"
    )
    if requested:
        set_runtime_state("adaptive_retrain_requested", "1")
        set_runtime_state("adaptive_retrain_reason", reason)
    return requested, reason


def _persist(payload: dict, models: list[dict], roles: dict[int, str], promotion: dict, threshold: dict, regime_rows: list[dict]) -> int:
    ensure_adaptive_learning_schema()
    conn = connect(config.ALERTS_DB)
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO adaptive_learning_runs(
                   created_at_utc,created_at_madrid,context,state,active_model_id,shadow_model_id,regime,
                   audit_key,source_feature_dates,mature_movements,core_signal_sample,drift_status,dominant_failure,
                   research_gate_current,research_gate_proposed,live_gate_current,live_gate_proposed,
                   retrain_requested,promotion_action,rollback_action,payload_json,fingerprint
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["created_at_utc"], payload["created_at_madrid"], payload.get("context"), payload["state"],
                payload.get("active_model_id"), payload.get("shadow_model_id"), payload.get("regime"),
                payload.get("audit_key"), payload.get("source_feature_dates", 0), payload.get("mature_movements", 0),
                payload.get("core_signal_sample", 0), payload.get("drift_status"), payload.get("dominant_failure"),
                threshold["research_current"], threshold["research_proposed"], threshold["live_current"], threshold["live_proposed"],
                1 if payload.get("retrain_requested") else 0, promotion.get("action"), promotion.get("rollback"),
                json.dumps(payload, ensure_ascii=False, default=str), payload["fingerprint"],
            ),
        )
        if cur.lastrowid:
            learning_id = int(cur.lastrowid)
        else:
            row = conn.execute("SELECT id FROM adaptive_learning_runs WHERE fingerprint=?", (payload["fingerprint"],)).fetchone()
            learning_id = _i(row[0] if row else 0)
        selected_id = _i((promotion.get("candidate") or {}).get("id")) if promotion.get("action") in {"PROMOTED_SHADOW", "SHADOW_VALIDATION"} else 0
        for model in models:
            model_id = _i(model.get("id"))
            m = _model_metrics(model)
            conn.execute(
                """INSERT OR REPLACE INTO adaptive_model_metrics(
                       learning_run_id,model_version_id,model_key,model_status,role,sample,market_dates,hit_rate,
                       avg_return,profit_factor,max_drawdown,avg_hit_sessions,utility,safety_pass,selected_for_shadow,metrics_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    learning_id, model_id, model.get("model_key"), model.get("status"), roles.get(model_id, "BALANCED"),
                    m["sample"], m["market_dates"], m["hit_rate"], m["avg_return"], m["profit_factor"],
                    m["max_drawdown"], m["avg_hit_sessions"], m["utility"], 1 if m["safety_pass"] else 0,
                    1 if model_id == selected_id else 0, json.dumps(m, ensure_ascii=False, default=str),
                ),
            )
        for name, current, proposed, applied, scope, reason in (
            ("research_fusion_score", threshold["research_current"], threshold["research_proposed"], effective_research_gate(), "SHADOW_AUTO", threshold["research_reason"]),
            ("enter_live_score", threshold["live_current"], threshold["live_proposed"], threshold["live_current"], "LIVE_PROPOSAL_ONLY", threshold["live_reason"]),
        ):
            conn.execute(
                """INSERT OR REPLACE INTO adaptive_threshold_history(
                       learning_run_id,created_at_utc,threshold_name,current_value,proposed_value,applied_value,scope,reason,evidence_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (learning_id, ts_utc_str(), name, current, proposed, applied, scope, reason, json.dumps(payload.get("evidence") or {}, ensure_ascii=False)),
            )
        candidate = promotion.get("candidate") or {}
        if promotion.get("action") not in {None, "NONE"}:
            conn.execute(
                """INSERT INTO adaptive_promotion_decisions(
                       learning_run_id,created_at_utc,candidate_model_id,previous_model_id,decision,scope,
                       candidate_utility,previous_utility,evidence_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    learning_id, ts_utc_str(), _i(candidate.get("id")) or None, promotion.get("previous_id"),
                    promotion.get("action"), "SHADOW_ONLY",
                    _f((promotion.get("candidate_metrics") or {}).get("utility"), None),
                    _f((promotion.get("active_metrics") or {}).get("utility"), None),
                    json.dumps({"reason": promotion.get("reason"), "evidence": payload.get("evidence")}, ensure_ascii=False, default=str),
                ),
            )
        if promotion.get("action") == "ROLLBACK":
            conn.execute(
                """INSERT INTO adaptive_rollback_events(
                       learning_run_id,created_at_utc,from_model_id,to_model_id,reason,evidence_json
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    learning_id, ts_utc_str(), _i(candidate.get("id")) or None, promotion.get("previous_id"),
                    str(promotion.get("reason") or "adaptive_guard"), json.dumps(payload.get("evidence") or {}, ensure_ascii=False),
                ),
            )
        for row in regime_rows:
            conn.execute(
                """INSERT OR REPLACE INTO adaptive_regime_metrics(
                       learning_run_id,model_version_id,regime,movement_events,core_detected,enter_generated,coverage_pct,enter_recall_pct
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    learning_id, row.get("model_version_id"), row.get("regime"), row.get("movements"),
                    row.get("core_detected"), row.get("enter_generated"), row.get("coverage_pct"), row.get("enter_recall_pct"),
                ),
            )
        conn.commit()
        return learning_id
    finally:
        conn.close()


def _write_artifacts(payload: dict) -> dict[str, str]:
    out_dir = config.ADAPTIVE_LEARNING_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest_adaptive_learning.json"
    history = out_dir / "adaptive_learning_history.jsonl"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return {"json": str(latest), "history": str(history)}


def _console_report(payload: dict) -> None:
    evidence = payload.get("evidence") or {}
    threshold = payload.get("thresholds") or {}
    promotion = payload.get("promotion") or {}
    models = payload.get("models") or []
    log_info(
        "🧠 [LEARNING STATE] "
        f"id={payload.get('learning_run_id') or 'pending'} state={payload.get('state')} context={payload.get('context')} | "
        f"champion=#{payload.get('active_model_id') or 0} shadow_override=#{payload.get('shadow_model_id') or 0} "
        f"regime={payload.get('regime')} | mature_moves={evidence.get('movements',0)} "
        f"core_signals={evidence.get('core_signal_sample',0)} source_dates={evidence.get('source_feature_dates',0)}",
        send_discord=False,
    )
    log_info(
        "🧠 [DRIFT DETECTOR] "
        f"status={evidence.get('drift_status')} coverage={_pct(evidence.get('coverage'))} "
        f"precision={_pct(evidence.get('precision'))} OOS={_pct(evidence.get('oos_hit'))} "
        f"lead={_num(evidence.get('lead'))}s late={_pct(evidence.get('late_pct'))} | "
        f"dominant={evidence.get('dominant_failure')} warnings={';'.join(evidence.get('drift_warnings') or []) or 'none'}",
        send_discord=False,
    )
    reverse = evidence.get("reverse_alpha") or {}
    log_info(
        "🧠 [REVERSE ALPHA LEARNING] "
        f"scope={reverse.get('scope','n/a')} strategic={bool(reverse.get('strategic_conclusion_allowed'))} "
        f"top={reverse.get('evaluated_top_opportunities',0)} dominant={reverse.get('dominant_miss','NONE')} "
        f"alert_recall={_pct(reverse.get('alert_recall_pct'))} eod_recall={_pct(reverse.get('eod_alpha_recall_pct'))} "
        f"plans={','.join(reverse_specialist_plans(reverse)) or 'none'}",
        send_discord=False,
    )
    log_info(
        "🧠 [CHALLENGER FACTORY] "
        f"plans={','.join(payload.get('specialist_plan') or [])} models_evaluated={len(models)} "
        f"retrain_requested={bool(payload.get('retrain_requested'))} reason={payload.get('retrain_reason')}",
        send_discord=False,
    )
    for row in models[: int(config.ADAPTIVE_MAX_MODELS_CONSOLE)]:
        log_info(
            "   ↳ [CHALLENGER RESULT] "
            f"model=#{row.get('id')} status={row.get('status')} role={row.get('role')} "
            f"n={row.get('sample')}/{row.get('market_dates')}d hit={_pct(row.get('hit_rate'))} "
            f"PF={_num(row.get('profit_factor'))} DD={_pct(row.get('max_drawdown'))} "
            f"utility={_num(row.get('utility'))} safe={row.get('safety_pass')}",
            send_discord=False,
        )
    log_info(
        "🧠 [THRESHOLD PROPOSAL] "
        f"Research/Fusion {threshold.get('research_current')}→{threshold.get('research_proposed')} "
        f"applied_shadow={threshold.get('research_applied')} reason={threshold.get('research_reason')} | "
        f"ENTER_LIVE {threshold.get('live_current')}→{threshold.get('live_proposed')} "
        f"applied={threshold.get('live_current')} proposal_only reason={threshold.get('live_reason')}",
        send_discord=False,
    )
    log_info(
        "🧠 [PROBABILITY CALIBRATION] "
        f"status={'ACTIVE_SHADOW' if payload.get('probability_map') else 'ACCUMULATING'} "
        f"bins={json.dumps(payload.get('probability_map') or {}, ensure_ascii=False, separators=(',',':'))[:900]}",
        send_discord=False,
    )
    log_info(
        "🧠 [REGIME ROUTER] "
        f"current={payload.get('regime')} selected=#{payload.get('shadow_model_id') or payload.get('active_model_id') or 0} "
        f"mode={'SPECIALIST_SHADOW' if payload.get('shadow_model_id') else 'GLOBAL_FALLBACK'} "
        f"regime_metrics={json.dumps(payload.get('regime_metrics') or [], ensure_ascii=False, separators=(',',':'))[:700]}",
        send_discord=False,
    )
    log_info(
        "🧠 [PROMOTION DECISION] "
        f"action={promotion.get('action')} candidate=#{_i((promotion.get('candidate') or {}).get('id'))} "
        f"previous=#{promotion.get('previous_id') or payload.get('active_model_id') or 0} scope=SHADOW_ONLY "
        f"reason={promotion.get('reason')} | AUTO_PROMOTE_LIVE=false",
        send_discord=False,
    )
    if promotion.get("action") == "ROLLBACK":
        log_info(
            "🧠 [AUTO ROLLBACK] "
            f"from=#{_i((promotion.get('candidate') or {}).get('id'))} to=#{promotion.get('previous_id') or 0} "
            f"reason={promotion.get('reason')}",
            send_discord=False,
        )
    files = payload.get("files") or {}
    log_info(f"📁 [LEARNING OUTPUT] json={files.get('json')} history={files.get('history')}", send_discord=False)


def _discord_report(payload: dict) -> str:
    evidence = payload.get("evidence") or {}
    threshold = payload.get("thresholds") or {}
    promotion = payload.get("promotion") or {}
    return build_group("🧠 APRENDIZAJE ADAPTATIVO · ESTADO", ["\n".join([
        f"**Estado:** `{payload.get('state')}` · **régimen:** `{payload.get('regime')}` · champion `#{payload.get('active_model_id') or 0}` · shadow `#{payload.get('shadow_model_id') or 0}`.",
        f"**Histórico:** `{(payload.get('history_maturity') or {}).get('status','n/a')}` · OOS/CORE `{(payload.get('history_maturity') or {}).get('historical_sample',0)}` obs · `{(payload.get('history_maturity') or {}).get('historical_dates',0)}` fechas.",
        f"**Auditor prospectivo:** movimientos `{evidence.get('movements',0)}` · señales CORE `{evidence.get('core_signal_sample',0)}` · cobertura `{_pct(evidence.get('coverage'))}` · precisión `{_pct(evidence.get('precision'))}`.",
        f"**Diagnóstico:** `{evidence.get('dominant_failure')}` · drift `{evidence.get('drift_status')}` · challengers `{','.join(payload.get('specialist_plan') or [])}`.",
        f"**Reverse Alpha:** scope `{(evidence.get('reverse_alpha') or {}).get('scope','n/a')}` · top `{(evidence.get('reverse_alpha') or {}).get('evaluated_top_opportunities',0)}` · miss `{(evidence.get('reverse_alpha') or {}).get('dominant_miss','NONE')}` · recall alertas `{_pct((evidence.get('reverse_alpha') or {}).get('alert_recall_pct'))}` · EOD `{_pct((evidence.get('reverse_alpha') or {}).get('eod_alpha_recall_pct'))}`.",
        f"**Gate SHADOW:** Research/Fusion `{threshold.get('research_current')}→{threshold.get('research_applied')}` · LIVE permanece fijo en `{threshold.get('live_current')}`.",
        f"**Decisión:** `{promotion.get('action')}` · `{promotion.get('reason')}`.",
        "Las promociones y adaptaciones automáticas de v6.1.25 afectan únicamente a SHADOW; LIVE no cambia automáticamente.",
    ])])


def run_adaptive_learning(
    context: str,
    *,
    audit_payload: dict | None = None,
    force: bool = False,
    send_discord: bool = True,
    console_if_unchanged: bool = False,
) -> dict:
    if not bool(config.ADAPTIVE_LEARNING_ENABLED):
        return {"status": "disabled", "executed": False}
    ensure_adaptive_learning_schema()
    audit = dict(audit_payload or _latest_audit())
    evidence = _audit_evidence(audit)
    maturity = history_maturity_snapshot(audit)
    try:
        lifecycle = run_lifecycle_research()
    except Exception as exc:
        lifecycle = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}", "pattern_reversal_sample": 0}
    lifecycle_summary = {k: v for k, v in lifecycle.items() if k != "trades"}
    evidence["history_maturity"] = maturity
    evidence["lifecycle_counterfactual"] = lifecycle_summary
    models = _adaptive_model_pool(int(config.ADAPTIVE_MODEL_LOOKBACK))
    active = _base_active_model() or {}
    active_id = _i(active.get("id"))
    roles = _assign_roles(models)
    threshold = _threshold_proposal(evidence)

    # Bounded, automatic SHADOW adaptation. No file/config mutation.
    set_runtime_state("adaptive_research_gate_shadow", str(threshold["research_proposed"]))
    probability_map = evidence.get("probability_calibration") or {}
    usable_probability_map = {
        key: value for key, value in probability_map.items()
        if _i((value or {}).get("sample")) >= int(config.ADAPTIVE_PROBABILITY_MIN_BIN_SAMPLE)
    }
    set_runtime_state("adaptive_probability_map", json.dumps(usable_probability_map, ensure_ascii=False))

    fingerprint_payload = {
        "audit_key": evidence.get("audit_key"),
        "active_model": active_id,
        "models": [(m.get("id"), m.get("status"), m.get("objective_score")) for m in models],
        "research_gate": threshold["research_proposed"],
        "probability_map": usable_probability_map,
        "override": get_runtime_state("adaptive_shadow_model_id", "0"),
        "history_status": maturity.get("status"),
        "historical_sample": maturity.get("historical_sample"),
        "historical_dates": maturity.get("historical_dates"),
        "lifecycle_fingerprint": lifecycle.get("fingerprint"),
        "reverse_alpha": {
            "scope": (evidence.get("reverse_alpha") or {}).get("scope"),
            "evaluated": (evidence.get("reverse_alpha") or {}).get("evaluated_top_opportunities"),
            "dominant": (evidence.get("reverse_alpha") or {}).get("dominant_miss"),
            "miss_reasons": (evidence.get("reverse_alpha") or {}).get("miss_reasons"),
            "alert_recall_pct": (evidence.get("reverse_alpha") or {}).get("alert_recall_pct"),
            "eod_alpha_recall_pct": (evidence.get("reverse_alpha") or {}).get("eod_alpha_recall_pct"),
        },
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, default=str).encode()).hexdigest()
    previous = str(get_runtime_state("adaptive_learning_fingerprint", "") or "")
    if not force and previous == fingerprint:
        cached_path = config.ADAPTIVE_LEARNING_OUTPUT_DIR / "latest_adaptive_learning.json"
        try:
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
        except Exception:
            cached = {}
        if console_if_unchanged:
            log_info(
                "🧠 [LEARNING CACHE] no_new_evidence | "
                f"context={context} state={cached.get('state','unchanged')} "
                f"champion=#{cached.get('active_model_id',active_id)} shadow=#{cached.get('shadow_model_id',0)}",
                send_discord=False,
            )
        return {**cached, "executed": False, "status": cached.get("state", "unchanged") if cached else "unchanged"}

    retrain, retrain_reason = _retrain_decision(evidence)
    promotion = _promotion_and_rollback(models, evidence, threshold, fingerprint)
    shadow_model_id = _i(get_runtime_state("adaptive_shadow_model_id", "0"))
    state = _learning_state(evidence, promotion, retrain)
    regime = _latest_regime()
    regime_rows = _regime_metrics(active_id)
    specialist_plan = _specialist_plan(evidence)

    model_payload = []
    for model in models:
        m = _model_metrics(model)
        model_payload.append({
            "id": _i(model.get("id")), "key": model.get("model_key"), "status": model.get("status"),
            "role": roles.get(_i(model.get("id")), "BALANCED"), **{k: v for k, v in m.items() if k != "raw"},
        })
    model_payload.sort(key=lambda row: (float(row.get("utility") or 0), row.get("id") or 0), reverse=True)
    # A challenger can have strong precision/expectancy but fail only the drawdown
    # safety gate (as observed in production). Make that an explicit research
    # objective instead of silently waiting for more calendar time.
    for row in model_payload:
        if _i(row.get("id")) == active_id:
            continue
        if (
            _i(row.get("sample")) >= int(config.ADAPTIVE_MIN_OOS_SAMPLE)
            and (_f(row.get("hit_rate"), 0.0) or 0.0) >= float(config.ADAPTIVE_MIN_SHADOW_HIT_RATE)
            and (_f(row.get("profit_factor"), 0.0) or 0.0) >= float(config.ADAPTIVE_MIN_SHADOW_PROFIT_FACTOR)
            and (_f(row.get("avg_return"), 0.0) or 0.0) > 0.0
            and (_f(row.get("max_drawdown"), 999.0) or 999.0) > float(config.ADAPTIVE_MAX_SHADOW_DRAWDOWN_PCT)
        ):
            if "LOW_DRAWDOWN" not in specialist_plan:
                specialist_plan.append("LOW_DRAWDOWN")
            break

    payload = {
        "version": config.BOT_VERSION,
        "created_at_utc": ts_utc_str(), "created_at_madrid": ts_madrid_str(),
        "context": context, "state": state, "fingerprint": fingerprint,
        "active_model_id": active_id, "shadow_model_id": shadow_model_id,
        "regime": regime.get("regime"), "regime_snapshot": regime,
        "audit_key": evidence.get("audit_key"), "source_feature_dates": evidence.get("source_feature_dates", 0),
        "mature_movements": evidence.get("movements", 0), "core_signal_sample": evidence.get("core_signal_sample", 0),
        "drift_status": evidence.get("drift_status"), "dominant_failure": evidence.get("dominant_failure"),
        "retrain_requested": retrain, "retrain_reason": retrain_reason,
        "specialist_plan": specialist_plan,
        "model_pool": {
            "evaluated": len(models),
            "ids": [_i(model.get("id")) for model in models],
            "recent_limit": int(config.ADAPTIVE_MODEL_LOOKBACK),
            "historical_memory": True,
        },
        "history_maturity": maturity,
        "lifecycle_counterfactual": lifecycle_summary,
        "thresholds": {
            **threshold,
            "research_applied": effective_research_gate(),
            "live_applied": float(config.PRECISION_FIRST_MIN_LIVE_SCORE),
        },
        "probability_map": usable_probability_map,
        "promotion": promotion,
        "evidence": evidence,
        "models": model_payload,
        "regime_metrics": regime_rows,
        "safety": {
            "auto_promote_shadow": bool(config.ADAPTIVE_AUTO_PROMOTE_SHADOW),
            "auto_promote_live": False,
            "source_code_mutation": False,
            "bounded_research_gate": [float(config.ADAPTIVE_RESEARCH_GATE_MIN), float(config.ADAPTIVE_RESEARCH_GATE_MAX)],
        },
    }
    files = _write_artifacts(payload)
    payload["files"] = files
    learning_id = _persist(payload, models, roles, promotion, payload["thresholds"], regime_rows)
    payload["learning_run_id"] = learning_id
    Path(files["json"]).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    set_runtime_state("adaptive_learning_fingerprint", fingerprint)
    set_runtime_state("adaptive_learning_state", state)
    set_runtime_state("adaptive_learning_run_id", str(learning_id))
    set_runtime_state(
        "adaptive_reverse_alpha_evidence",
        json.dumps(evidence.get("reverse_alpha") or {}, ensure_ascii=False, default=str),
    )
    _console_report(payload)

    if send_discord and bool(config.ADAPTIVE_LEARNING_DISCORD_ENABLED):
        discord_key = hashlib.sha256(json.dumps({
            "state": state, "shadow": shadow_model_id, "retrain": retrain,
            "promotion": promotion.get("action"), "gate": payload["thresholds"]["research_applied"],
            "dominant": evidence.get("dominant_failure"),
        }, sort_keys=True, default=str).encode()).hexdigest()
        if force or str(get_runtime_state("adaptive_learning_discord_fingerprint", "") or "") != discord_key:
            send_daily(_discord_report(payload))
            set_runtime_state("adaptive_learning_discord_fingerprint", discord_key)
    return {**payload, "executed": True, "status": state}


def maybe_run_adaptive_learning(
    context: str,
    *,
    audit_payload: dict | None = None,
    send_discord: bool = True,
    console_if_unchanged: bool = False,
) -> dict:
    try:
        return run_adaptive_learning(
            context=context, audit_payload=audit_payload, force=False,
            send_discord=send_discord, console_if_unchanged=console_if_unchanged,
        )
    except sqlite3.OperationalError as exc:
        log_info(f"⚠️ [LEARNING DEFERRED] reason=SQLITE_BUSY | aprendizaje aplazado sin afectar Alerts: {exc}", send_discord=False)
        return {"status": "deferred_sqlite", "executed": False, "error": str(exc)}
    except Exception as exc:
        log_info(f"⚠️ [LEARNING DEFERRED] reason=NON_FATAL_ERROR | aprendizaje aplazado: {type(exc).__name__}: {exc}", send_discord=False)
        return {"status": "deferred_error", "executed": False, "error": str(exc)}
