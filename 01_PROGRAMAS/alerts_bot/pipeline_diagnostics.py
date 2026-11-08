from __future__ import annotations

import json
from typing import Any

import config
from time_utils import now_madrid


def _safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def record_stage(
    code: str,
    status: str,
    title: str,
    *,
    expected: Any = None,
    found: Any = None,
    action: str | None = None,
    reason: str | None = None,
    details: dict | None = None,
) -> dict:
    payload = {
        "timestamp_madrid": now_madrid().isoformat(),
        "bot": "alerts",
        "version": config.BOT_VERSION,
        "code": code,
        "status": status.upper(),
        "title": title,
        "expected": _safe(expected),
        "found": _safe(found),
        "action": action,
        "reason": reason,
        "details": {k: _safe(v) for k, v in (details or {}).items()},
    }
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with config.PIPELINE_DIAGNOSTICS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    reset_codes = {"D01", "A00"}
    status_doc: dict = {}
    if code not in reset_codes and config.PIPELINE_STATUS_FILE.exists():
        try:
            status_doc = json.loads(config.PIPELINE_STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            status_doc = {}
    stages = status_doc.get("stages") if isinstance(status_doc.get("stages"), dict) else {}
    stages[code] = payload
    status_doc = {
        "bot": payload["bot"],
        "version": payload["version"],
        "updated_at_madrid": payload["timestamp_madrid"],
        "latest": payload,
        "stages": stages,
    }
    config.PIPELINE_STATUS_FILE.write_text(
        json.dumps(status_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def stage_line(payload: dict) -> str:
    icon = {"OK": "✅", "WAIT": "⏳", "WARN": "⚠️", "ERROR": "🟥", "SKIP": "⏭️"}.get(
        str(payload.get("status") or "").upper(), "ℹ️"
    )
    line = f"{icon} [{payload.get('code')}] {payload.get('title')}"
    if payload.get("found") is not None:
        line += f" | encontrado={payload.get('found')}"
    if payload.get("expected") is not None:
        line += f" | esperado={payload.get('expected')}"
    if payload.get("action"):
        line += f" | acción={payload.get('action')}"
    if payload.get("reason"):
        line += f" | motivo={payload.get('reason')}"
    return line



def friendly_stage_line(payload: dict) -> str:
    """Compact human-readable stage line; full payload remains in JSONL diagnostics."""
    status = str(payload.get("status") or "").upper()
    icon = {"OK": "✅", "WAIT": "🟡", "WARN": "⚠️", "ERROR": "🟥", "SKIP": "⏭️"}.get(status, "ℹ️")
    code = str(payload.get("code") or "")
    title = str(payload.get("title") or "")
    found = payload.get("found") if isinstance(payload.get("found"), dict) else {}
    action = str(payload.get("action") or "")
    reason = str(payload.get("reason") or "")

    if code == "A00":
        configured = sum(1 for value in found.values() if value == "configured")
        return f"{icon} [1/9] Discord Alerts conectado | canales configurados={configured}/{len(found) or 7}"
    if code == "A01":
        ready = bool(found.get("bootstrap_complete"))
        gen = found.get("generation", found.get("coordination_generation", 0))
        state = "Data Bot listo" if ready else "Data Bot preparando datos; Alerts continúa en SHADOW"
        return f"{icon} [2/9] {state} | generación={gen or '-'} | cierres={found.get('daily_closes', 0)}"
    if code == "A02":
        return f"{icon} [3/9] Histórico deep revisado | ZIP nuevos={found.get('new', 0)} | filas importadas={found.get('rows_imported', 0)}"
    if code == "A03":
        return f"{icon} [4/9] GEX intradía | sesiones={found.get('available', 0)}/{payload.get('expected', '?')} | archivos={found.get('archives', 0)}"
    if code == "A04C":
        return (f"{icon} [5/9] Histórico CORE | días={found.get('dates', 0)}/{found.get('target', '?')} | "
                f"universo={found.get('eligible_tickers', 0)} elegibles + {found.get('excluded_tickers', 0)} excluidos | "
                f"pendientes={found.get('pending_tickers', 0)}")
    if code == "A04F":
        return f"{icon} [6/9] GEX diario granular | cierres={found.get('alerts_daily_dates', found.get('daily_dates', 0))} | filas Scanner={found.get('alerts_scanner_rows', 0)}"
    if code == "A05":
        return f"{icon} [7/9] Contexto mercado/ETF listo | nodos={found.get('scanner_nodes', 0)} | relaciones={found.get('edges', 0)}"
    if code == "A06":
        return f"{icon} [8/9] Outcomes preparados | CORE={found.get('core_outcome_dates', 0)} días | FULL={found.get('full_outcome_dates', 0)} días"
    if code == "A07":
        return f"{icon} Conciliación Data↔Alerts | CORE={found.get('core_data', 0)} días | FULL={found.get('full_data', 0)} días"
    if code == "A08":
        executed = bool(found.get('core_executed'))
        return f"{icon} Calibración CORE | {'ejecutada' if executed else 'pendiente'} | CORE={found.get('core_dates', 0)}/{found.get('core_target', '?')}"
    if code == "A09":
        return f"{icon} [9/9] Alerts operativo | modo={str(found.get('mode', 'unknown')).upper()} | alertas={str(found.get('entry_mode', 'off')).upper()}"

    if code == "D01":
        configured = sum(1 for value in found.values() if value == "configured")
        return f"{icon} [1/8] Discord Data conectado | canales configurados={configured}/{len(found) or 3}"
    if code == "D02":
        return (f"{icon} [2/8] Bases abiertas | Scanner={found.get('scanner_rows', 0):,} filas | "
                f"GEX={found.get('gex_rows', 0):,} resúmenes")
    if code == "D03":
        return f"{icon} [3/8] Intradía anterior archivado | días={found.get('days_archived', 0)} | runs Scanner={found.get('scanner_runs_archived', 0)} | runs GEX={found.get('gex_runs_archived', 0)}"
    if code == "D03B":
        return f"{icon} Perfiles GEX archivados | días={found.get('days_archived', 0)} | eventos={found.get('events', 0)} | tickers={found.get('tickers', 0)}"
    if code == "D04B":
        return f"{icon} [4/8] Descarga CORE | completos={found.get('already_complete', 0)} | intentados={found.get('attempted', 0)} | pendientes={found.get('remaining', 0)}"
    if code == "D04C":
        return f"{icon} [5/8] Histórico CORE reconstruido | tickers={found.get('tickers', 0)} | señales={found.get('signal_rows', 0):,} | precios={found.get('price_rows', 0):,}"
    if code == "D04F":
        return f"{icon} [6/8] Cierres FULL DEEP | días={found.get('alerts_daily_dates', found.get('daily_dates', 0))} | filas={found.get('scanner_rows', 0):,}"
    if code == "D05":
        return (f"{icon} [7/8] Cobertura | CORE={found.get('core_history', '?')} {'LISTO' if found.get('core_ready') else 'ACUMULANDO'} | "
                f"FULL GEX={found.get('full_deep', '?')} | pendientes CORE={found.get('core_pending', 0)}")
    if code == "D06":
        return f"{icon} Retención aplicada | {found}"
    if code == "D07":
        return f"{icon} Captura de reparación | Scanner={'sí' if found.get('scanner') else 'no'} | GEX={'sí' if found.get('gex') else 'no'} | {reason or action}"
    if code == "D08":
        return (f"{icon} [8/8] Data Bot listo | CORE={found.get('core_history', '?')} | "
                f"universo={found.get('core_eligible', 0)}+{found.get('core_excluded', 0)}/{found.get('core_universe', 0)} | "
                f"FULL GEX={found.get('full_deep', '?')}")

    detail = reason or action
    return f"{icon} [{code}] {title}" + (f" | {detail}" if detail else "")


def webhook_status() -> dict:
    return {
        "alerts_status": bool(config.DISCORD_WEBHOOK_URL_ALERTS_STATUS),
        "alerts_error": bool(config.DISCORD_WEBHOOK_URL_ALERTS_ERROR),
        "research": bool(config.DISCORD_WEBHOOK_URL_RESEARCH),
        "alerts_shadow": bool(config.DISCORD_WEBHOOK_URL_ALERTS_SHADOW),
        "alerts_live": bool(config.DISCORD_WEBHOOK_URL_ALERTS),
        "calibration": bool(config.DISCORD_WEBHOOK_URL_DAILY),
        "learning_audit": bool(config.DISCORD_WEBHOOK_URL_LEARNING_AUDIT),
    }
