from __future__ import annotations

import json
from typing import Any

import config
from db import get_open_trades, get_runtime_state, set_runtime_state
from discord_presentation import build_group, signal_source_label, signal_model_lineage, ticker_source_label
from discord_utils import send_status, send_research
from rules import position_update_assessment
from time_utils import now_madrid

STATUS_META = {
    "red": ("🔴", 0, "tesis cerca de cierre"),
    "yellow": ("🟡", 1, "cambio relevante"),
    "green": ("🟢", 2, "avanza en la tesis"),
    "gray": ("⚪", 3, "sin cambios"),
}


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _directional_return(current: float, entry: float, direction: str) -> float:
    if current <= 0 or entry <= 0:
        return 0.0
    raw = (current / entry - 1.0) * 100.0
    return raw if str(direction).lower() == "long" else -raw


def _is_live_trade(trade: dict) -> bool:
    return bool(
        int(trade.get("sent_to_discord") or 0)
        and not str(trade.get("alert_status") or "").lower().startswith("shadow")
    )


def _is_shadow_trade(trade: dict) -> bool:
    return str(trade.get("alert_status") or "").lower().startswith("shadow")


def _current_features(trade: dict, features_by_ticker: dict[str, dict]) -> dict:
    ticker = str(trade.get("ticker") or "").upper()
    return dict(features_by_ticker.get(ticker) or _json(trade.get("last_features_json")))


def _material_change(entry_features: dict, current_features: dict, directional_return: float, direction: str) -> str | None:
    if abs(directional_return) >= float(config.POSITION_UPDATE_MIN_STEP_PCT):
        return f"movimiento `{directional_return:+.1f}%` todavía sin confirmación completa"

    entry_signal = str(
        entry_features.get("option_signal")
        or entry_features.get("unusual_flow_signal")
        or entry_features.get("flow")
        or ""
    ).strip().lower()
    current_signal = str(
        current_features.get("option_signal")
        or current_features.get("unusual_flow_signal")
        or current_features.get("flow")
        or ""
    ).strip().lower()
    neutral = {"", "neutral", "none", "n/a"}
    if entry_signal not in neutral and current_signal not in neutral and entry_signal != current_signal:
        return "la señal de flujo ha cambiado respecto a la entrada"

    price = _number(current_features.get("price"), _number(current_features.get("current_price"), 0.0))
    vwap = _number(current_features.get("vwap"), 0.0)
    if price > 0 and vwap > 0:
        adverse_vwap = (str(direction).lower() == "long" and price < vwap) or (
            str(direction).lower() == "short" and price > vwap
        )
        if adverse_vwap:
            return "precio al lado adverso del VWAP, todavía sin invalidación"
    return None


def assess_open_position(trade: dict, features_by_ticker: dict[str, dict] | None = None) -> dict:
    """Classify one open LIVE position for the Discord overview."""
    features_by_ticker = features_by_ticker or {}
    features = _current_features(trade, features_by_ticker)
    entry_features = _json(trade.get("entry_features_json"))
    direction = str(trade.get("direction") or "long").lower()
    entry = _number(trade.get("entry_price"), 0.0)
    current = _number(features.get("price"), _number(trade.get("last_price"), entry))
    directional_return = _directional_return(current, entry, direction)
    assessment = position_update_assessment(direction, features, directional_return) if features else None
    deterioration_streak = int(trade.get("deterioration_streak") or 0)
    favorable_streak = int(trade.get("favorable_streak") or 0)
    soft_runs = max(1, int(config.SOFT_EXIT_CONFIRMATION_RUNS))
    last_kind = str(trade.get("last_update_kind") or "").lower()
    setup_hit = bool(int(trade.get("setup_hit") or 0))
    best_return = _number(trade.get("best_return_pct"), directional_return)
    giveback = max(0.0, best_return - directional_return)
    lifecycle_state = str(trade.get("lifecycle_state") or ("OPEN_RUNNER" if setup_hit else "OPEN"))

    severe_adverse = bool(
        assessment and assessment.get("kind") == "deterioration"
        and directional_return <= -max(8.0, float(config.POSITION_UPDATE_MIN_STEP_PCT) * 2.0)
    )
    if assessment and assessment.get("kind") == "deterioration" and (deterioration_streak >= max(1, soft_runs - 1) or severe_adverse):
        status = "red"
        effective_streak = max(1, deterioration_streak)
        remaining = max(0, soft_runs - effective_streak)
        detail = (
            f"cierre pendiente · deterioro `{effective_streak}/{soft_runs}`; "
            + ("confirmación final en el próximo run" if remaining <= 1 else f"faltan {remaining} confirmaciones")
        )
    elif assessment and assessment.get("kind") == "deterioration":
        status = "yellow"
        detail = str(assessment.get("detail") or "cambio adverso aún no confirmado")
    elif assessment and assessment.get("kind") == "favorable":
        status = "green"
        detail = (
            f"runner `{directional_return:+.1f}%` · MFE `{best_return:+.1f}%` · giveback `{giveback:.1f} pp`; estructura vigente"
            if setup_hit else f"avanza `{directional_return:+.1f}%`; confirmaciones de la tesis vigentes"
        )
    elif setup_hit:
        status = "green"
        detail = f"setup ganado · runner `{directional_return:+.1f}%` · MFE `{best_return:+.1f}%` · giveback `{giveback:.1f} pp`"
    elif favorable_streak > 0 or (directional_return >= float(config.POSITION_UPDATE_MIN_FAVORABLE_PCT) and last_kind != "deterioration"):
        status = "green"
        detail = f"avanza `{directional_return:+.1f}%`; sin deterioro estructural"
    else:
        change = _material_change(entry_features, features, directional_return, direction)
        if change or last_kind == "deterioration":
            status = "yellow"
            detail = change or "hubo un cambio reciente, pero la tesis sigue abierta"
        else:
            status = "gray"
            detail = "sin cambios relevantes desde el último run"

    source = signal_source_label(
        entry_features,
        rule_name=str(trade.get("rule_name") or ""),
        feature_tier=str(entry_features.get("feature_tier") or ""),
    )
    entry_lineage = signal_model_lineage(entry_features, model_id=trade.get("model_version_id"), model_key=trade.get("model_key"))
    has_active_lineage = any(
        features.get(key) not in (None, "", [], {})
        for key in (
            "signal_origin", "core_model_id", "core_model_key", "core_rule",
            "target_source", "contributor_engines", "model_lineage",
            "gex_daily_model_status", "gex_intraday_model_status", "graph_source",
        )
    ) if features else False
    active_lineage = signal_model_lineage(
        features,
        model_id=features.get("core_model_id"),
        model_key=features.get("core_model_key"),
    ) if has_active_lineage else ""
    lineage = f"entrada {entry_lineage}"
    if active_lineage and active_lineage != entry_lineage:
        lineage += f" · activo {active_lineage}"
    side = "CALL" if direction == "long" else "PUT"
    icon, priority, label = STATUS_META[status]
    return {
        "trade": trade,
        "ticker": str(trade.get("ticker") or "").upper(),
        "source": source,
        "lineage": lineage,
        "side": side,
        "status": status,
        "icon": icon,
        "priority": priority,
        "status_label": label,
        "directional_return": directional_return,
        "current": current,
        "entry": entry,
        "detail": " ".join(str(detail).split())[:230],
        "sessions_seen": int(trade.get("sessions_seen") or 0),
        "max_hold_sessions": int(trade.get("max_hold_sessions") or config.MAX_PATTERN_HORIZON_SESSIONS),
        "setup_hit": setup_hit,
        "lifecycle_state": lifecycle_state,
        "best_return_pct": best_return,
        "giveback_pct": giveback,
    }


def build_open_positions_overview(
    trades: list[dict],
    *,
    features_by_ticker: dict[str, dict] | None = None,
    run: dict | None = None,
    book: str = "live",
) -> str:
    """Build one bullet overview for the LIVE or SHADOW open book."""
    book_key = str(book or "live").lower()
    selected = [
        trade for trade in trades
        if (_is_live_trade(trade) if book_key == "live" else _is_shadow_trade(trade))
    ]
    if not selected:
        return ""
    assessed = [assess_open_position(trade, features_by_ticker) for trade in selected]
    assessed.sort(
        key=lambda item: (
            int(item["priority"]),
            float(item["directional_return"]) if item["status"] in {"red", "yellow"} else -float(item["directional_return"]),
            item["ticker"],
        )
    )
    counts = {key: sum(1 for item in assessed if item["status"] == key) for key in STATUS_META}
    lines = [
        (
            f"• {item['icon']} {ticker_source_label(item['ticker'], source=item['source'])}: "
            f"**{item['side']} · {item['directional_return']:+.1f}%** · entry `{item['entry']:.2f}` → now `{item['current']:.2f}`\n"
            f"   {item['detail'].rstrip('.')} · session `{item['sessions_seen']}/{item['max_hold_sessions']}`\n"
            f"   `model: {item['lineage']} · lifecycle: {item['lifecycle_state']}`"
        )
        for item in assessed
    ]
    run_id = str((run or {}).get("id") or "manual")
    session = str((run or {}).get("session_type") or "estado actual")
    context = (
        f"Run `{run_id}` · `{session}` · abiertas `{len(assessed)}` · "
        f"🔴 `{counts['red']}` · 🟡 `{counts['yellow']}` · 🟢 `{counts['green']}` · ⚪ `{counts['gray']}`"
    )
    if book_key == "live":
        title = "📌 POSICIONES ABIERTAS · LIVE"
        footer = "🔴 cerca de cierre · 🟡 cambio relevante · 🟢 avanza · ⚪ estable. Libro operativo validado."
    else:
        title = "📌 POSICIONES ABIERTAS · SHADOW"
        footer = "⚠️ Seguimiento preliminar para aprendizaje. No equivale a posiciones LIVE ni a operativa validada."
    return build_group(title, ["\n".join(lines)], context=context, footer=footer)



def build_positions_dashboard(
    trades: list[dict],
    *,
    features_by_ticker: dict[str, dict] | None = None,
    run: dict | None = None,
) -> str:
    """Build the central Discord dashboard for every open position.

    Alerts Status is the central operational overview channel. LIVE and SHADOW are
    separated inside the same dashboard while their lifecycle cards remain in
    their dedicated channels. An explicit empty state prevents a silent status.
    """
    features_by_ticker = features_by_ticker or {}
    assessed_by_book: dict[str, list[dict]] = {"live": [], "shadow": []}
    for trade in trades:
        if _is_live_trade(trade):
            assessed_by_book["live"].append(assess_open_position(trade, features_by_ticker))
        elif _is_shadow_trade(trade):
            assessed_by_book["shadow"].append(assess_open_position(trade, features_by_ticker))

    for items in assessed_by_book.values():
        items.sort(
            key=lambda item: (
                int(item["priority"]),
                float(item["directional_return"]) if item["status"] in {"red", "yellow"} else -float(item["directional_return"]),
                item["ticker"],
            )
        )

    def section(title: str, items: list[dict], empty: str) -> str:
        lines = [f"### {title}"]
        if not items:
            lines.append(f"_{empty}_")
            return "\n".join(lines)
        for item in items:
            lines.append(
                f"• {item['icon']} {ticker_source_label(item['ticker'], source=item['source'])}: "
                f"**{item['side']} · {item['directional_return']:+.1f}%** · entry `{item['entry']:.2f}` → now `{item['current']:.2f}`\n"
                f"   {item['detail'].rstrip('.')} · session `{item['sessions_seen']}/{item['max_hold_sessions']}`\n"
                f"   `model: {item['lineage']} · lifecycle: {item['lifecycle_state']}`"
            )
        return "\n".join(lines)

    live = assessed_by_book["live"]
    shadow = assessed_by_book["shadow"]
    all_items = live + shadow
    counts = {key: sum(1 for item in all_items if item["status"] == key) for key in STATUS_META}
    run_id = str((run or {}).get("id") or "estado actual")
    session = str((run or {}).get("session_type") or "recuperación / estado actual")
    context = (
        f"**TMAD:** `v{config.BOT_VERSION}` · **Run:** `{run_id}` · **Sesión:** `{session}`\n"
        f"**Abiertas:** LIVE `{len(live)}` · SHADOW `{len(shadow)}` · "
        f"🔴 `{counts['red']}` · 🟡 `{counts['yellow']}` · 🟢 `{counts['green']}` · ⚪ `{counts['gray']}`"
    )
    cards = [
        section("LIVE · OPERATIVAS", live, "Sin posiciones LIVE abiertas"),
        section("SHADOW · SIMULADAS / NO OPERATIVAS", shadow, "Sin posiciones SHADOW abiertas"),
    ]
    return build_group(
        "📌 OPEN POSITIONS | TMAD",
        cards,
        context=context,
        footer=(
            "🔴 cerca de cierre · 🟡 cambio relevante · 🟢 avanza · ⚪ estable. "
            "Las posiciones SHADOW son seguimiento de aprendizaje y no representan operativa LIVE."
        ),
    )

def maybe_send_open_positions_overview(
    *,
    features_by_ticker: dict[str, dict] | None = None,
    run: dict | None = None,
    force: bool = False,
) -> str:
    if not bool(getattr(config, "OPEN_POSITIONS_OVERVIEW_ENABLED", True)):
        return ""
    run_key = str((run or {}).get("id") or "manual")
    state_key = "open_positions_overview_last_run_all"
    if not force and run_key != "manual" and str(get_runtime_state(state_key, "") or "") == run_key:
        return ""
    message = build_positions_dashboard(
        get_open_trades(),
        features_by_ticker=features_by_ticker or {},
        run=run or {},
    )
    delivered = send_status(message)
    if delivered and run_key != "manual":
        set_runtime_state(state_key, run_key)
    return message

