from __future__ import annotations

"""Compact Discord reporting for the v6 learning layers."""

import hashlib
import json
from collections import Counter
from typing import Any

from db import get_runtime_state, set_runtime_state
from discord_utils import send_daily
from time_utils import now_madrid
from discord_presentation import build_group, ticker_bullet_header, status_display


def _f(value: Any, decimals: int = 1) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except Exception:
        return "n/a"


def _pct(value: Any, decimals: int = 1) -> str:
    formatted = _f(value, decimals)
    return formatted if formatted == "n/a" else formatted + "%"


def _layer_line(label: str, row: dict) -> str:
    status = str(row.get("status") or "ACCUMULATING")
    sample = int(row.get("sample") or 0)
    dates = int(row.get("complete_dates") or 0)
    if label in {"GEX D", "GEX DIARIO", "FUSION", "FUSIÓN"}:
        metric = f"Target `{_f(row.get('target_zone_hit_rate'))}%` · MAE `{_f(row.get('target_mae_pct'))}%`"
    elif label == "TIMING":
        metric = f"ENTER `{int(row.get('enter_sample') or 0)}` · uplift `{_f(row.get('timing_uplift'))}pp`"
    else:
        metric = f"Hit ±5 `{_f(row.get('movement_5pct_hit_rate'))}%`"
    return f"• **{label}:** {status} · n `{sample}` / `{dates}d` · {metric}"


def _top_lines(signals: list[dict], limit: int = 5) -> list[str]:
    rows = [s for s in signals if str(s.get("layer") or "").upper() == "FUSION"]
    priority = {"ENTER": 9, "READY": 8, "RETEST": 7, "EARLY": 6, "DISCOVERED": 5, "WATCH": 4}
    best: dict[str, dict] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        state = str(row.get("opportunity_state") or row.get("state") or "WATCH").upper()
        rank = priority.get(state, 1) * 1000 + float(row.get("score") or 0)
        if ticker and rank > float((best.get(ticker) or {}).get("_rank") or -1):
            best[ticker] = {**row, "_rank": rank}
    selected = sorted(best.values(), key=lambda r: float(r.get("_rank") or 0), reverse=True)[:limit]
    if not selected:
        return ["### Top del run\n_Sin setups en el último run._"]
    output: list[str] = []
    for row in selected:
        direction = "CALL" if str(row.get("direction")) == "long" else "PUT"
        state = str(row.get("opportunity_state") or row.get("state") or "WATCH").upper()
        target = row.get("target_pct")
        target_text = f"`{float(target):+.1f}%`" if target is not None else "pendiente"
        timing = f"{int(row.get('timing_min_sessions') or 1)}–{int(row.get('timing_max_sessions') or 5)} sesiones"
        intent = str(row.get("gex_intent") or "").strip()
        output.append("\n".join([
            ticker_bullet_header(row.get('ticker'), row, rule_name=row.get('rule_name'), side=direction, state=state),
            f"**Prioridad:** `{_f(row.get('score'),0)} pts` · **Objetivo GEX:** {target_text}",
            f"**Horizonte:** `{timing}`" + (f" · **Lectura:** {intent}" if intent else ""),
        ]))
    return output


def build_layer_calibration_message(calibrations: dict[str, dict], signals: list[dict], run: dict, market_summary: dict | None = None) -> str:
    state_counts = Counter(
        str(s.get("opportunity_state") or s.get("state") or "UNKNOWN").upper()
        for s in signals if str(s.get("layer") or "").upper() == "FUSION"
    )
    states = " · ".join(f"**{key}:** `{value}`" for key, value in state_counts.most_common(5)) or "sin setups"
    layers = "\n".join([
        "### Estado de las capas",
        _layer_line("CORE", calibrations.get("CORE") or {}),
        _layer_line("GRAFO", calibrations.get("GRAPH") or {}),
        _layer_line("GEX DIARIO", calibrations.get("GEX_DAILY") or {}),
        _layer_line("TIMING", calibrations.get("GEX_INTRADAY") or {}),
        _layer_line("FUSIÓN", calibrations.get("FUSION") or {}),
    ])
    cards = [layers]
    market = market_summary or {}
    if market:
        close_cal = market.get("market_close_all_calibration") or {}
        long_cal = close_cal.get("long") or {}
        short_cal = close_cal.get("short") or {}
        market_core_coverage = int(market.get('market_core_coverage') or 0)
        market_probability_line = (
            f"• **Probabilidad:** risk-off `{_f(market.get('market_core_risk_off_probability'))}%` · risk-on `{_f(market.get('market_core_risk_on_probability'))}%` · acuerdo `{float(max(market.get('market_core_risk_off_agreement') or 0, market.get('market_core_risk_on_agreement') or 0)):.0%}`"
            if market_core_coverage > 0 else
            "• **Probabilidad:** `NO DISPONIBLE` · el 50/50 interno es un placeholder neutral porque MARKET CORE tiene cobertura 0/4."
        )
        market_card = "\n".join([
            "### MARKET CORE / GEX / VOL · CALIDAD",
            f"• **MARKET CORE:** modelo `#{market.get('market_core_model_id') or 'n/a'} {market.get('market_core_model_status') or 'n/a'}` · cobertura `{market_core_coverage}/4` / peso `{float(market.get('market_core_weight_coverage') or 0):.0%}`",
            market_probability_line,
            f"• **GEX main ETFs:** risk-on `{_f(market.get('main_etf_gex_risk_on_score'))}` · cobertura `{int(market.get('main_etf_gex_coverage') or 0)}/4` / peso `{float(market.get('main_etf_gex_weight_coverage') or 0):.0%}`",
            f"• **Complejo VIX:** GEX stress `{_f(market.get('vol_gex_stress_score'))}` · `{int(market.get('vol_gex_coverage') or 0)}` productos (`{int(market.get('vol_gex_long_coverage') or 0)}L/{int(market.get('vol_gex_short_coverage') or 0)}S`) · scanner `{_f(market.get('vol_scanner_stress_score'))}`",
            f"• **CLOSE ALL LONGS:** `{long_cal.get('status') or 'ACCUMULATING'}` · n `{int(long_cal.get('sample') or 0)}` · hit `{_pct(long_cal.get('hit_rate_pct'))}`",
            f"• **CLOSE ALL SHORTS:** `{short_cal.get('status') or 'ACCUMULATING'}` · n `{int(short_cal.get('sample') or 0)}` · hit `{_pct(short_cal.get('hit_rate_pct'))}`",
            "• **Uso:** señal general paralela; no cierra posiciones. El CLOSE individual exige reversión del GEX propio del ticker mientras la señal esté activa.",
        ])
        cards.append(market_card)
    return build_group(
        "🧪 CALIBRACIÓN · CAPAS",
        cards,
        context=f"**Sesión:** `{run.get('snapshot_date') or now_madrid().date().isoformat()}`",
        footer="Calibration muestra salud y muestra de las capas; los tickers concretos se publican únicamente en Research.",
    )


def maybe_send_layer_calibration(calibrations: dict[str, dict], signals: list[dict], run: dict, *, market_summary: dict | None = None, force: bool = False) -> str:
    message = build_layer_calibration_message(calibrations, signals, run, market_summary=market_summary)
    signature = hashlib.sha256(message.encode("utf-8")).hexdigest()
    key = "v6_layer_calibration_signature"
    if not force and str(get_runtime_state(key) or "") == signature:
        return ""
    delivered = send_daily(message)
    # Only acknowledge a Discord state after a successful delivery. A temporary
    # webhook failure must be retried on the next daemon pass rather than silently
    # suppressing the same operational message forever.
    if delivered:
        set_runtime_state(key, signature)
    return message
