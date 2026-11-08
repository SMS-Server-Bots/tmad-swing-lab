from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from db import get_runtime_state, set_runtime_state
from discord_utils import send_alpha_output

_MARKET_TZ = ZoneInfo("America/New_York")
_FINAL_MINUTE = 15 * 60 + 55


def _fmt(value, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "-"


def _trace_token(payload: dict) -> str:
    """Short stable identifier for Discord-to-runtime traceability."""
    fingerprint = str(payload.get("fingerprint") or "").strip()
    return fingerprint[:12] if fingerprint else "-"


def _short(value, limit: int = 170) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _price(value) -> str:
    try:
        v = float(value)
        return f"{v:.2f}" if abs(v) < 1000 else f"{v:.1f}"
    except Exception:
        return "-"


def _signed_pct(value) -> str:
    try:
        return f"{float(value):+.1f}%"
    except Exception:
        return "-"


def _swing_context(row: dict) -> dict:
    value = row.get("swing_context")
    return value if isinstance(value, dict) else {}


def format_alpha_options_list(payload: dict, *, phase: str = "UPDATE") -> str:
    asof = str(payload.get("asof") or "")
    source_run = int(payload.get("source_scanner_run_id") or 0)
    selected = list(payload.get("selected") or [])
    phase = str(phase or "UPDATE").upper()
    title = "CIERRE" if phase == "FINAL" else "ACTUALIZACIÓN"
    trace = _trace_token(payload)
    book_summary = payload.get("book_summary") if isinstance(payload.get("book_summary"), dict) else {}
    active_positions = list(book_summary.get("open_positions") or [])
    economics = book_summary.get("closed_economics") if isinstance(book_summary.get("closed_economics"), dict) else {}
    lines = [
        f"🎯 **TMAD ALPHA | SWING LIST · {title}**",
        f"🧪 **SHADOW · {asof}** · producto operativo en validación · **NO ORDERS**",
        f"🔎 `Scanner {source_run} · trace {trace}`",
        f"📌 **{len(selected)} swings** · horizonte 1–5 sesiones · ordenados por Alpha",
        "",
    ]
    if active_positions:
        lines.append(f"📒 **Libro Alpha activo ({len(active_positions)})**")
        for position in active_positions:
            p_ticker = str(position.get("ticker") or "?").upper()
            p_direction = str(position.get("direction") or "?").upper()
            p_icon = "🟢" if p_direction == "LONG" else "🔴"
            lines.append(
                f"{p_icon} **${p_ticker} {p_direction} | {str(position.get('status') or 'OPEN').upper()}** "
                f"@ `{_price(position.get('entry_price'))}` → `{_price(position.get('last_price'))}` "
                f"(`{_signed_pct(position.get('last_return_pct'))}`) · target `{_price(position.get('target_price'))}` "
                f"· inval. `{_price(position.get('invalidation_price'))}` · "
                f"`{position.get('sessions_seen', 0)}/{position.get('max_hold_sessions', 5)} sesiones`"
            )
        lines.append("")
    lines.append(f"🔭 **Radar Alpha Top {len(selected)}**")
    if not selected:
        lines.append("Sin oportunidades Alpha seleccionadas en este snapshot.")
    for idx, row in enumerate(selected, start=1):
        ticker = str(row.get("ticker") or "?").upper()
        direction = str(row.get("direction") or "?").upper()
        icon = "🟢" if direction == "LONG" else "🔴"
        context = _swing_context(row)
        expression = row.get("option_expression") or {}
        instrument = str(expression.get("instrument") or ("CALL" if direction == "LONG" else "PUT"))
        strike = expression.get("strike")
        expiry = expression.get("expiry")
        dte = expression.get("dte")
        state = str(row.get("opportunity_state") or context.get("state") or "-").upper()
        book = str(row.get("alpha_book_status") or "WATCH").upper()
        alpha = row.get("underlying_alpha_score", row.get("conviction"))
        target = context.get("target_price")
        target_pct = context.get("target_pct")
        invalidation = context.get("invalidation_price")
        max_hold = context.get("max_hold_sessions") or "5"
        core = context.get("core_score")
        gex = context.get("gex_score")
        source_gex_run_id = int(row.get("source_gex_run_id") or context.get("source_gex_run_id") or 0)
        model_version_id = int(row.get("model_version_id") or context.get("model_version_id") or 0)
        timing = str(context.get("intraday_state") or "-").upper()
        intent = str(context.get("gex_intent") or "-").upper()
        option_flow = context.get("option_signal") or context.get("unusual_flow_signal") or "-"
        lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
        persistence_obs = int(lifecycle.get("observations") or 0)
        persistence_consistency = lifecycle.get("direction_consistency_pct")
        rationale = _short(context.get("rationale"), 190)
        if strike is not None and expiry:
            contract = f"{instrument} {float(strike):g} · {expiry} ({dte if dte is not None else '-'} DTE)"
        else:
            contract = f"{instrument} · sin contrato adecuado todavía"
        status = "OPEN" if book in {"OPEN", "OPEN_RUNNER"} else state
        lines.extend([
            f"{idx}. {icon} **${ticker} {direction} | {status}** · Alpha **{_fmt(alpha)}/100** · `{max_hold} sesiones máx.`",
            f"   📍 **{_price(row.get('spot'))} → target {_price(target)}**" + (f" (`{_fmt(target_pct)}%`)" if target_pct is not None else "") + (f" · inval. ref. **{_price(invalidation)}**" if invalidation is not None else ""),
            f"   🧠 CORE `{_fmt(core)}` · GEX `{_fmt(gex)}`/`{intent}` · timing `{timing}` · flow `{_short(option_flow,70)}`",
            f"   🔗 GEX `{source_gex_run_id}` · Modelo `{model_version_id}`",
            f"   ⏱️ Persistence `{persistence_obs}` obs" + (f" · `{_fmt(persistence_consistency)}%` consistent" if persistence_consistency is not None else ""),
            f"   💡 {_short(rationale or 'Tesis compuesta por Scanner + CORE + GEX + timing + contexto.', 210)}",
            f"   🧩 {contract}",
        ])
        if book == "OPEN_RUNNER":
            lines.append(f"   🔁 Runner activo · retorno Alpha `{_signed_pct(row.get('alpha_return_pct'))}`")
        elif book == "OPEN":
            lines.append(f"   ✅ Libro Alpha abierto @ `{_price(row.get('alpha_entry_price'))}` · retorno `{_signed_pct(row.get('alpha_return_pct'))}`")
    lines.extend([
        "",
        "🧭 **Regla:** la lista rankea; OPEN sólo cuando FUSION ya confirma `ENTER + actionable`.",
        "📐 **Semántica:** Alpha scores the underlying; option P/L se mide por separado en SHADOW.",
        "🚪 **Salida:** nivel de invalidación / direction flip / DIVERGENCE / market block / time stop. Target GEX = milestone de runner, no cierre automático.",
        "🛡️ SHADOW does not change LIVE gates or sizing.",
    ])
    closed_sample = int(economics.get("sample") or 0)
    if closed_sample > 0:
        pf = economics.get("profit_factor")
        pf_text = _fmt(pf, 2) if pf is not None else "-"
        lines.append(
            f"📊 **OPEN→CLOSE SHADOW:** n=`{closed_sample}` · WR `{_fmt(economics.get('win_rate_pct'))}%` "
            f"· avg `{_signed_pct(economics.get('avg_return_pct'))}` · PF `{pf_text}`"
        )
    lines.append("🧪 Todo sigue SHADOW; la muestra cerrada se mide antes de cualquier promoción.")
    return "\n".join(lines)


def format_alpha_swing_event(event: dict) -> str:
    event_type = str(event.get("event_type") or "UPDATE").upper()
    ticker = str(event.get("ticker") or "?").upper()
    direction = str(event.get("direction") or "?").upper()
    icon = "🟢" if direction == "LONG" else "🔴"
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    selected = payload.get("selected") if isinstance(payload.get("selected"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    if not context:
        context = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
    thesis = payload.get("thesis") if isinstance(payload.get("thesis"), dict) else {}
    reason = str(event.get("reason") or "")
    state = str(event.get("fusion_state") or context.get("state") or "-").upper()
    price = event.get("price")
    ret = event.get("return_pct")
    alpha = event.get("alpha_score")
    target = position.get("target_price") if position.get("target_price") is not None else context.get("target_price")
    invalidation = position.get("invalidation_price") if position.get("invalidation_price") is not None else context.get("invalidation_price")
    max_hold = context.get("max_hold_sessions") or position.get("max_hold_sessions") or 5
    rationale = context.get("rationale") or thesis.get("rationale") or ""
    core = context.get("core_score") if context.get("core_score") is not None else thesis.get("core_score")
    gex = context.get("gex_score") if context.get("gex_score") is not None else thesis.get("gex_score")
    intent = context.get("gex_intent") or thesis.get("gex_intent") or "-"
    timing = context.get("intraday_state") or thesis.get("intraday_state") or "-"
    expression = selected.get("option_expression") if isinstance(selected.get("option_expression"), dict) else {}
    contract = ""
    if expression:
        strike = expression.get("strike"); expiry = expression.get("expiry")
        if strike is not None and expiry:
            contract = f"{expression.get('instrument') or ('CALL' if direction == 'LONG' else 'PUT')} {float(strike):g} · {expiry}"
    reason_text = {
        "FUSION_ENTER_ACTIONABLE": "FUSION confirma ENTER accionable con la evidencia actual.",
        "TARGET_REACHED_RUNNER": "Objetivo GEX alcanzado; la operación pasa a runner mientras la tesis siga válida.",
        "STATE_CHANGE": "Cambio material del estado de la oportunidad.",
        "MATERIAL_MOVE": "Movimiento material desde la última actualización.",
        "DIRECTION_FLIP": "La dirección de FUSION ha girado contra la tesis de entrada.",
        "PRICE_INVALIDATION": "El precio ha cruzado el nivel de invalidación fijado al abrir la tesis.",
        "DIVERGENCE": "CORE/GEX han pasado a divergencia; la tesis deja de estar alineada.",
        "MARKET_BLOCKED": "El contexto de mercado bloquea la tesis.",
        "TIME_STOP": "Se agotó el horizonte máximo sin una razón para mantener el swing.",
    }.get(reason, reason or "Actualización material del Alpha Swing.")
    title = f"{icon} **TMAD ALPHA · {event_type} ${ticker} {direction}**"
    lines = [
        title,
        f"📍 **Precio:** `{_price(price)}` · **Alpha:** `{_fmt(alpha)}/100` · **FUSION:** `{state}`",
    ]
    if event_type == "OPEN":
        lines.extend([
            f"🎯 **Target:** `{_price(target)}` · **Invalidación ref.:** `{_price(invalidation)}` · **Time stop:** `{max_hold} sesiones`",
            f"🧠 **Entrada justificada:** CORE `{_fmt(core)}` · GEX `{_fmt(gex)}`/`{str(intent).upper()}` · timing `{str(timing).upper()}`",
            f"💡 {_short(rationale or reason_text, 260)}",
        ])
        if contract:
            lines.append(f"🧩 **Expresión opción SHADOW:** {contract}")
    elif event_type == "UPDATE":
        lines.extend([
            f"📈 **Retorno swing:** `{_signed_pct(ret)}` · **Motivo:** {reason_text}",
            f"🎯 Target `{_price(target)}` · inval. ref. `{_price(invalidation)}` · sesiones `{payload.get('sessions_seen','-')}/{max_hold}`",
        ])
    else:
        lines.extend([
            f"📊 **Resultado final:** `{_signed_pct(ret)}` · **Salida:** {reason_text}",
            f"🧠 La salida está ligada a la invalidación de la tesis, no a desaparecer del Top 5.",
        ])
    lines.append(f"🔎 `Scanner {int(event.get('source_scanner_run_id') or 0)} · Alpha Swing #{int(event.get('position_id') or 0)} · SHADOW`")
    return "\n".join(lines)


def publish_alpha_swing_events(events: list[dict]) -> dict:
    sent = 0
    failed = 0
    for event in events or []:
        if send_alpha_output(format_alpha_swing_event(event)):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "total": sent + failed}


def publish_alpha_options(payload: dict, *, now_market: datetime | None = None) -> dict:
    """Publish Alpha product on meaningful change plus one final close snapshot."""
    if str(payload.get("status") or "").lower() != "ok":
        return {"status": "not_ready"}
    asof = str(payload.get("asof") or "")[:10]
    fingerprint = str(payload.get("fingerprint") or "")
    source_run = int(payload.get("source_scanner_run_id") or 0)
    if not asof or not fingerprint:
        return {"status": "missing_identity"}

    now_market = now_market or datetime.now(_MARKET_TZ)
    if now_market.tzinfo is None:
        now_market = now_market.replace(tzinfo=_MARKET_TZ)
    else:
        now_market = now_market.astimezone(_MARKET_TZ)
    minute = now_market.hour * 60 + now_market.minute
    phase = "FINAL" if minute >= _FINAL_MINUTE else "UPDATE"

    if phase == "FINAL":
        if str(get_runtime_state("alpha_output_final_date", "") or "") == asof:
            return {"status": "already_sent_final", "asof": asof}
    else:
        previous_fp = str(get_runtime_state("alpha_output_update_fingerprint", "") or "")
        if previous_fp == fingerprint:
            return {"status": "unchanged", "asof": asof, "source_scanner_run_id": source_run}

    msg = format_alpha_options_list(payload, phase=phase)
    ok = send_alpha_output(msg)
    if not ok:
        return {"status": "delivery_failed", "phase": phase, "asof": asof}

    if phase == "FINAL":
        set_runtime_state("alpha_output_final_date", asof)
        set_runtime_state("alpha_output_final_fingerprint", fingerprint)
    else:
        set_runtime_state("alpha_output_update_fingerprint", fingerprint)
    set_runtime_state("alpha_output_sent_source_run", str(source_run))
    return {"status": "sent", "phase": phase, "asof": asof, "source_scanner_run_id": source_run}
