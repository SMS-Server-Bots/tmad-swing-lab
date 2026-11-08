from __future__ import annotations

import json
from typing import Any

from db import get_runtime_state, set_runtime_state, record_pipeline_state
from time_utils import now_madrid

PIPELINE_STATES = {
    "WAITING_DATA", "BUILDING_CORE", "CALIBRATING", "SHADOW_READY",
    "LIVE_READY", "REST", "ERROR",
}
READY_STATES = {"SHADOW_READY", "LIVE_READY"}
RESUMABLE_STATES = READY_STATES | {"ERROR"}
_STATE_KEY = "pipeline_state_v6_1"


def _safe_get(key: str, default=None):
    try:
        return get_runtime_state(key, default)
    except Exception:
        return default


def _safe_set(key: str, value: str) -> None:
    try:
        set_runtime_state(key, value)
    except Exception:
        pass


def _fallback_state() -> dict:
    mode = str(_safe_get("entry_mode", "off") or "off").lower()
    state = "LIVE_READY" if mode == "live" else ("SHADOW_READY" if mode == "shadow" else "ERROR")
    return {
        "state": state,
        "stage": "legacy_fallback",
        "generation": 0,
        "entry_mode": mode,
        "error_code": None,
        "model_id": None,
        "reason": None,
        "details": {},
        "updated_at_madrid": now_madrid().isoformat(),
    }


def get_pipeline_state() -> dict:
    raw = _safe_get(_STATE_KEY)
    if raw:
        try:
            payload = json.loads(raw)
            if str(payload.get("state") or "").upper() in PIPELINE_STATES:
                return payload
        except Exception:
            pass
    return _fallback_state()


def transition_pipeline_state(
    state: str,
    *,
    stage: str | None = None,
    generation: int | None = None,
    entry_mode: str | None = None,
    error_code: str | None = None,
    model_id: int | str | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    preserve_entry_mode: bool = False,
) -> dict:
    state = str(state or "").upper()
    if state not in PIPELINE_STATES:
        raise ValueError(f"Estado pipeline no válido: {state}")
    previous = get_pipeline_state()
    previous_state = str(previous.get("state") or "ERROR").upper()
    previous_mode = str(previous.get("entry_mode") or _safe_get("entry_mode", "off") or "off").lower()
    if entry_mode is None:
        if preserve_entry_mode or state == "REST":
            entry_mode = previous_mode
        elif state == "LIVE_READY":
            entry_mode = "live"
        elif state == "SHADOW_READY":
            entry_mode = "shadow"
        else:
            entry_mode = "off"
    entry_mode = str(entry_mode or "off").lower()
    merged_details = dict(details or {})
    if state == "REST":
        resume = previous_state if previous_state in RESUMABLE_STATES else str(previous.get("details", {}).get("resume_state") or "SHADOW_READY")
        merged_details.setdefault("resume_state", resume)
    payload = {
        "state": state,
        "previous_state": previous_state,
        "stage": str(stage or state.lower()),
        "generation": int(generation if generation is not None else previous.get("generation") or 0),
        "entry_mode": entry_mode,
        "error_code": error_code,
        "model_id": model_id,
        "reason": reason,
        "details": merged_details,
        "updated_at_madrid": now_madrid().isoformat(),
    }
    _safe_set(_STATE_KEY, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    _safe_set("pipeline_global_state", state)
    _safe_set("alerts_phase", str(payload["stage"]))
    _safe_set("pipeline_generation", str(payload["generation"]))
    _safe_set("pipeline_error_code", str(error_code or ""))
    _safe_set("entry_mode", entry_mode)
    try:
        record_pipeline_state(payload)
    except Exception:
        pass
    return payload


def readiness_state(entry_mode: str, *, safe: bool = True) -> str:
    if not safe:
        return "ERROR"
    return "LIVE_READY" if str(entry_mode or "").lower() == "live" else "SHADOW_READY"


def resume_after_rest() -> dict:
    current = get_pipeline_state()
    if str(current.get("state") or "").upper() != "REST":
        return current
    resume = str((current.get("details") or {}).get("resume_state") or "SHADOW_READY").upper()
    if resume not in RESUMABLE_STATES:
        resume = "SHADOW_READY"
    return transition_pipeline_state(
        resume,
        stage="operating_window_resumed",
        generation=int(current.get("generation") or 0),
        entry_mode=str(current.get("entry_mode") or "shadow"),
        details={"resumed_from": "REST"},
    )


def compact_state_label(payload: dict | None = None) -> str:
    row = payload or get_pipeline_state()
    return str(row.get("state") or "ERROR").replace("_", " ")
