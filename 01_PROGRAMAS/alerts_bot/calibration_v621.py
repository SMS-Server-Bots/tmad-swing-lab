from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

import config
from discord_presentation import build_group
from discord_utils import send_alert, send_daily
from gex_intelligence import project_gex
from layer_archive import archived_feature_rows_with_outcomes, archived_joined_signal_outcomes
from model_metrics import normalized_oos_metrics
from time_utils import ts_madrid_str, ts_utc_str

CORE_INTERPRETATIONS = {
    "momentum_score": "fuerza del impulso del ticker",
    "scanner_trend_score": "dirección y persistencia detectadas por Scanner",
    "rsi": "posición del precio dentro de su impulso reciente",
    "rvol": "participación de volumen frente a su nivel habitual",
    "change_percent": "movimiento de precio de la sesión",
    "momentum_5d_pct": "continuidad del movimiento durante cinco sesiones",
    "price_vs_sma20_pct": "distancia frente a la tendencia de 20 sesiones",
    "price_vs_vwap_pct": "posición frente al precio medio negociado",
    "atr_pct": "amplitud habitual del movimiento del ticker",
    "realized_volatility_20d": "volatilidad realizada de las últimas 20 sesiones",
    "flow_direction": "sesgo direccional del flujo de opciones",
    "trend_direction": "dirección técnica dominante",
    "market_context_score": "alineación del ticker con el mercado general",
    "market_breadth_score": "participación del conjunto del mercado",
    "vix_stress_score": "presión del régimen de volatilidad",
    "credit_risk_on_score": "apetito o aversión al riesgo del crédito",
    "market_deterioration_score": "deterioro agregado del mercado",
    "sector_etf_score": "dirección del ETF sectorial",
    "sector_breadth_score": "participación interna del sector",
    "relative_strength_vs_market": "fortaleza o debilidad frente al mercado",
    "relative_strength_vs_sector": "fortaleza o debilidad frente al sector",
}

GEX_INTERPRETATIONS = {
    "target_pct": "distancia desde la entrada hasta el wall objetivo",
    "wall_asymmetry": "relación entre recorrido favorable y riesgo del wall contrario",
    "total_gex_change_pct": "desplazamiento del GEX agregado por strike",
    "near_gex_change_pct": "cambio del GEX de vencimientos próximos, 0–7 días",
    "swing_gex_change_pct": "cambio del GEX de vencimientos intermedios",
    "expiry_regime": "alineación o conflicto entre vencimientos",
    "wall_persistence": "estabilidad del wall durante capturas consecutivas",
    "gex_intent": "lectura operativa derivada de flow, walls y vencimientos",
    "target_zone_hit_rate": "frecuencia con la que el precio alcanzó el target GEX",
    "target_mae_pct": "error medio entre el target previsto y el recorrido observado",
    "calibrated_close_session": "sesión OOS que mejor conserva el movimiento antes del agotamiento",
    "exit_gex_reversal_pct": "reversión mínima del GEX por strike/expiry que aporta evidencia de cierre",
    "exit_pattern_score": "puntuación agregada mínima de invalidación para cerrar la posición",
    "soft_exit_confirmation_runs": "número de capturas consecutivas necesarias para confirmar un deterioro no extremo",
}


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _payload(row: dict) -> dict:
    value = row.get("payload")
    if isinstance(value, dict):
        return value
    try:
        return json.loads(row.get("payload_json") or "{}")
    except Exception:
        return {}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ALERTS_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def ensure_v621_schema() -> None:
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS layer_model_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL,
            model_key TEXT NOT NULL UNIQUE,
            parent_model_id INTEGER,
            status TEXT NOT NULL,
            selection_score REAL,
            sample INTEGER NOT NULL DEFAULT 0,
            complete_dates INTEGER NOT NULL DEFAULT 0,
            metrics_json TEXT NOT NULL,
            attributes_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_at_madrid TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_layer_model_versions_layer
            ON layer_model_versions(layer,status,id);
        CREATE TABLE IF NOT EXISTS layer_model_state (
            layer TEXT PRIMARY KEY,
            model_version_id INTEGER NOT NULL,
            updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calibration_trace_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            source_scanner_run_id INTEGER,
            core_model_id INTEGER,
            gex_model_id INTEGER,
            trace_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(ticker,direction,source_scanner_run_id,core_model_id,gex_model_id)
        );
        """
    )
    conn.commit()
    conn.close()


def _all_signal_outcomes() -> list[dict]:
    rows = list(archived_joined_signal_outcomes())
    conn = _connect()
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='layer_signal_history'").fetchone()
        if exists:
            rows.extend(dict(r) for r in conn.execute(
                """SELECT s.*,o.entry_price,o.max_horizon_sessions,o.movement_5pct_hit,o.target_zone_hit,
                          o.first_5pct_session,o.first_target_session,o.max_favorable_pct,o.max_adverse_pct,
                          o.target_error_pct,o.sessions_json,o.close_return_pct,o.resolved_reason,o.complete
                   FROM layer_signal_history s JOIN layer_signal_outcomes o ON o.layer_signal_id=s.id
                   WHERE o.complete=1 ORDER BY s.snapshot_date,s.id"""
            ).fetchall())
    finally:
        conn.close()
    dedup: dict[tuple, dict] = {}
    for row in rows:
        key = (str(row.get("snapshot_date") or row.get("signal_date") or ""), str(row.get("ticker") or ""), str(row.get("layer") or ""), str(row.get("direction") or ""))
        dedup[key] = row
    return list(dedup.values())


def _normalize_gex_direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"long", "bull", "bullish", "call", "up", "upside"}:
        return "long"
    if text in {"short", "bear", "bearish", "put", "down", "downside"}:
        return "short"
    return ""


def _gex_feature_rows() -> list[dict]:
    """Point-in-time GEX feature availability, independent of signal emission.

    v6.1.74 treated "no GEX_DAILY signal" as "no GEX data", which produced
    0% historical coverage even when a paired GEX snapshot existed. This loader
    keeps same-date/no-lookahead semantics but can use the underlying feature.
    """
    rows = list(archived_feature_rows_with_outcomes())
    conn = _connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='layer_feature_history'"
        ).fetchone()
        if exists:
            rows.extend(dict(row) for row in conn.execute(
                """SELECT * FROM layer_feature_history
                   WHERE COALESCE(source_gex_run_id,0)>0
                      OR COALESCE(gex_direction,'')<>''
                      OR COALESCE(gex_intent,'')<>''"""
            ).fetchall())
    finally:
        conn.close()
    return rows


def _gex_index() -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}

    # Fallback first: data availability must not depend on whether the GEX layer
    # itself emitted a standalone signal.
    for row in _gex_feature_rows():
        day = str(row.get("snapshot_date") or row.get("signal_date") or "")[:10]
        ticker = str(row.get("ticker") or "").upper()
        if not day or not ticker:
            continue
        payload = _payload(row)
        features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
        direction = _normalize_gex_direction(
            row.get("gex_direction") or payload.get("gex_direction") or payload.get("direction")
        )
        reprojection = None
        if not direction and features:
            try:
                reprojection = project_gex(features, core_direction=row.get("core_direction"))
            except Exception:
                reprojection = None
            direction = _normalize_gex_direction((reprojection or {}).get("direction"))
        pseudo = {
            **row,
            "direction": direction,
            "target_price_low": (
                row.get("gex_target_price")
                or (reprojection or {}).get("target_zone_low")
            ),
            "target_price_high": (
                row.get("gex_target_price")
                or (reprojection or {}).get("target_zone_high")
            ),
            "target_pct": row.get("gex_target_pct") or (reprojection or {}).get("target_pct"),
            "wall_asymmetry": (
                row.get("wall_asymmetry")
                or payload.get("wall_asymmetry")
                or (reprojection or {}).get("wall_asymmetry")
            ),
            "expiry_regime": (
                row.get("expiry_regime")
                or payload.get("expiry_regime")
                or (reprojection or {}).get("expiry_regime")
            ),
            "gex_intent": (
                row.get("gex_intent")
                or payload.get("gex_intent")
                or (reprojection or {}).get("gex_intent")
            ),
            "_gex_source": (
                "feature_reprojected_current_rules"
                if reprojection is not None
                else "feature_point_in_time"
            ),
        }
        key = (day, ticker)
        current = index.get(key)
        if current is None or int(pseudo.get("source_scanner_run_id") or 0) >= int(current.get("source_scanner_run_id") or 0):
            index[key] = pseudo

    # A real GEX_DAILY signal is stronger evidence and always overrides a
    # feature-only fallback on the same date/ticker.
    for row in _all_signal_outcomes():
        if str(row.get("layer") or "").upper() != "GEX_DAILY":
            continue
        key = (
            str(row.get("snapshot_date") or row.get("signal_date") or "")[:10],
            str(row.get("ticker") or "").upper(),
        )
        if not key[0] or not key[1]:
            continue
        row = {**row, "_gex_source": "gex_daily_signal"}
        current = index.get(key)
        if (
            current is None
            or str(current.get("_gex_source") or "") != "gex_daily_signal"
            or int(row.get("source_scanner_run_id") or 0) >= int(current.get("source_scanner_run_id") or 0)
        ):
            index[key] = row
    return index

def enrich_core_trades_with_gex(trades: list[dict]) -> tuple[list[dict], dict]:
    index = _gex_index()
    groups: dict[str, list[dict]] = defaultdict(list)
    enriched: list[dict] = []
    for trade in trades:
        item = dict(trade)
        row = index.get((str(trade.get("entry_date") or ""), str(trade.get("ticker") or "").upper()))
        if not row:
            status = "NOT_AVAILABLE"
            payload = {}
        else:
            payload = _payload(row)
            core_direction = _normalize_gex_direction(trade.get("direction"))
            gex_direction = _normalize_gex_direction(
                row.get("direction") or row.get("gex_direction") or payload.get("gex_direction") or payload.get("direction")
            )
            status = ("CONFIRMED" if gex_direction == core_direction else "DIVERGENT") if gex_direction else "NEUTRAL"
            if str(payload.get("gex_intent") or row.get("gex_intent") or "").upper() in {"UNCLEAR", "HEDGING", "STABILIZING"}:
                status = "NEUTRAL" if status == "CONFIRMED" else status
        item["gex_validation_status"] = status
        item["gex_validation"] = {
            "status": status,
            "source": (row or {}).get("_gex_source"),
            "source_gex_run_id": (row or {}).get("source_gex_run_id"),
            "direction": row.get("direction") if row else None,
            "target_low": (row or {}).get("target_price_low"),
            "target_high": (row or {}).get("target_price_high"),
            "target_pct": (row or {}).get("target_pct"),
            "wall_asymmetry": (row or {}).get("wall_asymmetry") or payload.get("wall_asymmetry"),
            "expiry_regime": (row or {}).get("expiry_regime") or payload.get("expiry_regime"),
            "gex_intent": (row or {}).get("gex_intent") or payload.get("gex_intent"),
            "level_name": payload.get("level_name"),
            "level_price": payload.get("level_price"),
            "expiry_date": payload.get("expiry_date"),
            "near_gex_change_pct": payload.get("near_gex_change_pct"),
            "swing_gex_change_pct": payload.get("swing_gex_change_pct"),
            "total_gex_change_pct": payload.get("total_gex_change_pct"),
        }
        groups[status].append(item)
        enriched.append(item)

    def stats(items: list[dict]) -> dict:
        if not items:
            return {"sample": 0, "hit_rate": None, "avg_return": None}
        return {
            "sample": len(items),
            "hit_rate": round(sum(bool(i.get("target_5pct_hit")) for i in items) / len(items) * 100.0, 3),
            "avg_return": round(mean(float(i.get("return_pct") or 0.0) for i in items), 4),
        }

    summary = {key.lower(): stats(value) for key, value in groups.items()}
    available = [i for i in enriched if i.get("gex_validation_status") != "NOT_AVAILABLE"]
    confirmed = groups.get("CONFIRMED", [])
    baseline_hit = (sum(bool(i.get("target_5pct_hit")) for i in available) / len(available) * 100.0) if available else 0.0
    confirmed_hit = (sum(bool(i.get("target_5pct_hit")) for i in confirmed) / len(confirmed) * 100.0) if confirmed else 0.0
    source_counts = Counter(str((i.get("gex_validation") or {}).get("source") or "none") for i in enriched)
    summary.update({
        "available": len(available),
        "source_counts": dict(source_counts),
        "coverage_pct": round(len(available) / max(1, len(enriched)) * 100.0, 3),
        "confirmed_lift_pp": round(confirmed_hit - baseline_hit, 3) if available and confirmed else None,
    })
    return enriched, summary


def core_gex_selection_bonus(summary: dict) -> float:
    sample = int(summary.get("available") or 0)
    lift = _f(summary.get("confirmed_lift_pp"), 0.0) or 0.0
    if sample < 8:
        return 0.0
    reliability = min(1.0, sample / 50.0)
    return round(max(-0.20, min(0.20, lift / 100.0 * reliability)), 6)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def _mode_numeric(values: list[Any], default: float | None = None) -> float | None:
    clean: list[float] = []
    for value in values:
        number = _f(value)
        if number is not None:
            clean.append(number)
    if not clean:
        return default
    counter = Counter(round(value, 8) for value in clean)
    return float(counter.most_common(1)[0][0])


def build_gex_close_policy_attributes(core_model: dict | None, metrics: dict | None = None) -> list[dict]:
    """Expose the calibrated exit policy embedded in the active CORE rules."""
    params = (core_model or {}).get("params") or {}
    rules = [rule for name, rule in (params.get("rules") or {}).items() if not str(name).startswith("_") and isinstance(rule, dict)]
    reversal = _mode_numeric([rule.get("exit_gex_reversal_pct") for rule in rules], float(config.DEFAULT_EXIT_GEX_REVERSAL_PCT))
    exit_score = _mode_numeric([rule.get("exit_pattern_score") for rule in rules], float(config.DEFAULT_EXIT_PATTERN_SCORE))
    close_session = _f((metrics or {}).get("calibrated_close_session"))
    attrs: list[dict] = []
    if close_session is not None:
        attrs.append({"feature": "calibrated_close_session", "op": "≈", "threshold": int(round(close_session)), "interpretation": GEX_INTERPRETATIONS["calibrated_close_session"]})
    attrs.extend([
        {"feature": "exit_gex_reversal_pct", "op": ">=", "threshold": round(float(reversal or config.DEFAULT_EXIT_GEX_REVERSAL_PCT), 2), "interpretation": GEX_INTERPRETATIONS["exit_gex_reversal_pct"]},
        {"feature": "exit_pattern_score", "op": ">=", "threshold": round(float(exit_score or config.DEFAULT_EXIT_PATTERN_SCORE), 2), "interpretation": GEX_INTERPRETATIONS["exit_pattern_score"]},
        {"feature": "soft_exit_confirmation_runs", "op": ">=", "threshold": int(config.SOFT_EXIT_CONFIRMATION_RUNS), "interpretation": GEX_INTERPRETATIONS["soft_exit_confirmation_runs"]},
    ])
    return attrs


def _row_close_stats(row: dict) -> dict:
    try:
        sessions = json.loads(row.get("sessions_json") or "[]")
    except Exception:
        sessions = []
    entry = _f(row.get("entry_price")); direction = str(row.get("direction") or "").lower()
    returns: list[float] = []
    if entry and entry > 0:
        for item in sessions:
            close = _f((item or {}).get("close"))
            if not close or close <= 0:
                continue
            returns.append((close / entry - 1.0) * 100.0 if direction == "long" else (entry / close - 1.0) * 100.0)
    close_ret = _f(row.get("close_return_pct"), returns[-1] if returns else None)
    max_fav = _f(row.get("max_favorable_pct"), max(returns) if returns else 0.0) or 0.0
    capture = (close_ret / max_fav * 100.0) if close_ret is not None and max_fav > 0 else None
    peak_session = (max(range(len(returns)), key=lambda i: returns[i]) + 1) if returns else None
    return {
        "close_return_pct": close_ret,
        "movement_capture_pct": max(-100.0, min(100.0, capture)) if capture is not None else None,
        "giveback_pct": max(0.0, max_fav - float(close_ret or 0.0)),
        "peak_close_session": peak_session,
    }


def build_gex_attributes(rows: list[dict]) -> list[dict]:
    """Derive transparent GEX rules from successful point-in-time observations."""
    hits = [r for r in rows if int(r.get("target_zone_hit") or 0) == 1]
    source = hits if len(hits) >= 5 else rows
    payloads = [_payload(r) for r in source]
    attrs: list[dict] = []

    target = [abs(x) for x in (_f(r.get("target_pct") or p.get("target_pct")) for r, p in zip(source, payloads)) if x is not None]
    asym = [x for x in (_f(r.get("wall_asymmetry") or p.get("wall_asymmetry")) for r, p in zip(source, payloads)) if x is not None]
    total = [abs(x) for x in (_f(p.get("total_gex_change_pct")) for p in payloads) if x is not None]
    near = [abs(x) for x in (_f(p.get("near_gex_change_pct")) for p in payloads) if x is not None]
    persistence = [x for x in (_f(p.get("wall_persistence_runs") or p.get("target_persistence_runs")) for p in payloads) if x is not None]

    if target:
        attrs.append({"feature": "target_pct", "op": "between", "threshold": [round(_quantile(target, .20) or 0, 2), round(_quantile(target, .80) or 0, 2)], "interpretation": GEX_INTERPRETATIONS["target_pct"]})
    if asym:
        attrs.append({"feature": "wall_asymmetry", "op": ">=", "threshold": round(_quantile(asym, .25) or 0, 2), "interpretation": GEX_INTERPRETATIONS["wall_asymmetry"]})
    if total:
        attrs.append({"feature": "total_gex_change_pct", "op": "directional_abs>=", "threshold": round(_quantile(total, .25) or 0, 2), "interpretation": GEX_INTERPRETATIONS["total_gex_change_pct"]})
    if near:
        attrs.append({"feature": "near_gex_change_pct", "op": "directional_abs>=", "threshold": round(_quantile(near, .25) or 0, 2), "interpretation": GEX_INTERPRETATIONS["near_gex_change_pct"]})
    if persistence:
        attrs.append({"feature": "wall_persistence", "op": ">=", "threshold": max(1, int(round(_quantile(persistence, .25) or 1))), "interpretation": GEX_INTERPRETATIONS["wall_persistence"]})

    regimes: dict[str, list[dict]] = defaultdict(list)
    for row in source:
        p = _payload(row)
        regimes[str(row.get("expiry_regime") or p.get("expiry_regime") or "UNKNOWN")].append(row)
    if regimes:
        best = max(regimes, key=lambda k: (sum(int(r.get("target_zone_hit") or 0) for r in regimes[k]) / max(1, len(regimes[k])), len(regimes[k])))
        attrs.append({"feature": "expiry_regime", "op": "=", "threshold": best, "interpretation": GEX_INTERPRETATIONS["expiry_regime"]})
    return attrs[:6]


def _exit_price(row: dict) -> float | None:
    entry = _f(row.get("entry_price"))
    direction = str(row.get("direction") or "")
    ret = _f(row.get("close_return_pct"))
    if entry and ret is not None:
        return entry * (1.0 + ret / 100.0) if direction == "long" else entry * (1.0 - ret / 100.0)
    try:
        sessions = json.loads(row.get("sessions_json") or "[]")
    except Exception:
        sessions = []
    first = int(row.get("first_target_session") or row.get("first_5pct_session") or 0)
    if first and len(sessions) >= first:
        return _f(sessions[first - 1].get("close"))
    return None


def _evidence_row(row: dict) -> dict:
    p = _payload(row)
    return {
        "ticker": row.get("ticker"), "direction": row.get("direction"),
        "entry_date": row.get("snapshot_date") or row.get("signal_date"),
        "entry_price": row.get("entry_price"), "exit_price": _exit_price(row),
        "target_low": row.get("target_price_low"), "target_high": row.get("target_price_high"),
        "target_hit": bool(int(row.get("target_zone_hit") or 0)),
        "movement_hit": bool(int(row.get("movement_5pct_hit") or 0)),
        "first_target_session": row.get("first_target_session"),
        "first_5pct_session": row.get("first_5pct_session"),
        "max_favorable_pct": row.get("max_favorable_pct"), "max_adverse_pct": row.get("max_adverse_pct"),
        "gex_intent": row.get("gex_intent") or p.get("gex_intent"),
        "expiry_regime": row.get("expiry_regime") or p.get("expiry_regime"),
        "wall_asymmetry": row.get("wall_asymmetry") or p.get("wall_asymmetry"),
        "level_name": p.get("level_name"), "level_price": p.get("level_price"),
        "opposite_level_name": p.get("opposite_level_name"), "opposite_level_price": p.get("opposite_level_price"),
        "expiry_date": p.get("expiry_date"), "expiry_bucket": p.get("expiry_bucket"),
        "near_gex_change_pct": p.get("near_gex_change_pct"), "swing_gex_change_pct": p.get("swing_gex_change_pct"),
        "total_gex_change_pct": p.get("total_gex_change_pct"),
        "wall_persistence_runs": p.get("wall_persistence_runs") or p.get("target_persistence_runs"),
        "graph_source": p.get("graph_source") or p.get("signal_origin"),
        "graph_score": p.get("graph_score") or p.get("score"),
        "resolved_reason": row.get("resolved_reason"),
        **_row_close_stats(row),
    }


def build_layer_evidence(rows: list[dict], limit: int = 3) -> list[dict]:
    ranked = sorted(rows, key=lambda r: (int(r.get("target_zone_hit") or r.get("movement_5pct_hit") or 0), abs(_f(r.get("max_favorable_pct"), 0.0) or 0.0)), reverse=True)
    wins = [r for r in ranked if int(r.get("target_zone_hit") or r.get("movement_5pct_hit") or 0) == 1][: max(1, limit - 1)]
    losses = [r for r in ranked if int(r.get("target_zone_hit") or r.get("movement_5pct_hit") or 0) == 0][:1]
    return [_evidence_row(r) for r in (wins + losses)[:limit]]


def build_graph_attributes(rows: list[dict]) -> list[dict]:
    hits = [r for r in rows if int(r.get("movement_5pct_hit") or 0) == 1]
    source = hits if len(hits) >= 5 else rows
    payloads = [_payload(r) for r in source]
    scores = [x for x in (_f(p.get("graph_score") or p.get("score")) for p in payloads) if x is not None]
    attrs: list[dict] = []
    if scores:
        attrs.append({"feature": "graph_score", "op": ">=", "threshold": round(_quantile(scores, .25) or 0, 1), "interpretation": "intensidad de propagación entre ETF, sector y ticker"})
    sources: dict[str, int] = defaultdict(int)
    for p in payloads:
        sources[str(p.get("graph_source") or p.get("signal_origin") or "UNKNOWN")] += 1
    if sources:
        attrs.append({"feature": "graph_source", "op": "in", "threshold": [k for k, _ in sorted(sources.items(), key=lambda kv: kv[1], reverse=True)[:3]], "interpretation": "tipo de relación que anticipa la expansión general"})
    attrs.extend([
        {"feature": "lead_lag_confirmation", "op": "=", "threshold": True, "interpretation": "el ETF o nodo líder se mueve antes que el ticker"},
        {"feature": "ticker_confirmation", "op": "=", "threshold": True, "interpretation": "el ticker confirma después la señal agregada"},
    ])
    return attrs[:6]


def layer_selection_score(layer: str, metrics: dict) -> float:
    layer = str(layer).upper()
    if layer == "GEX_DAILY":
        hit = _f(metrics.get("target_zone_hit_rate"), 0.0) or 0.0
        movement = _f(metrics.get("movement_5pct_hit_rate"), 0.0) or 0.0
        capture = _f(metrics.get("calibrated_close_capture_pct"), 0.0) or 0.0
        close_win = _f(metrics.get("calibrated_close_win_rate"), 0.0) or 0.0
        error = _f(metrics.get("target_mae_pct"), 25.0) or 25.0
        giveback = _f(metrics.get("calibrated_close_giveback_pct"), 25.0) or 25.0
        duration_error = _f(metrics.get("close_duration_mae_sessions"), 5.0) or 5.0
        return round(hit * 0.35 + movement * 0.15 + capture * 0.20 + close_win * 0.15 - error * 1.2 - giveback * 1.5 - duration_error * 3.0, 6)
    if layer == "GRAPH":
        return round((_f(metrics.get("movement_5pct_hit_rate"), 0.0) or 0.0) - abs(_f(metrics.get("avg_max_adverse_pct"), 0.0) or 0.0) * 0.5, 6)
    if layer == "GEX_INTRADAY":
        return round((_f(metrics.get("timing_uplift"), 0.0) or 0.0) + (_f(metrics.get("enter_target_hit_rate"), 0.0) or 0.0) * 0.25, 6)
    return round(_f(metrics.get("movement_5pct_hit_rate"), 0.0) or 0.0, 6)


def _write_layer_artifacts(layer: str, model_id: int, metrics: dict, attrs: list[dict], rows: list[dict]) -> None:
    folder = Path(config.OUTPUT_DIR) / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    slug = layer.lower()
    card = {"layer": layer, "model_id": model_id, "metrics": metrics, "attributes": attrs, "generated_at_madrid": ts_madrid_str()}
    (folder / f"{slug}_model_card.json").write_text(json.dumps(card, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    ledger = [_evidence_row(r) for r in rows]
    (folder / f"{slug}_trade_ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def register_layer_model(layer: str, metrics: dict, rows: list[dict]) -> dict:
    ensure_v621_schema()
    layer = str(layer).upper()
    attrs = build_gex_attributes(rows) if layer == "GEX_DAILY" else build_graph_attributes(rows) if layer == "GRAPH" else []
    if layer == "GEX_DAILY":
        try:
            from db import get_active_model
            core_model = get_active_model()
        except Exception:
            core_model = None
        attrs = (attrs[:4] + build_gex_close_policy_attributes(core_model, metrics))[:8]
        metrics = dict(metrics)
        metrics["core_exit_policy_model_id"] = int((core_model or {}).get("id") or 0)
    evidence = build_layer_evidence(rows)
    score = layer_selection_score(layer, metrics)
    fingerprint = json.dumps({"sample": metrics.get("sample"), "dates": metrics.get("complete_dates"), "score": score, "attrs": attrs}, sort_keys=True, default=str)
    conn = _connect()
    active_row = conn.execute("""SELECT mv.* FROM layer_model_state ms JOIN layer_model_versions mv ON mv.id=ms.model_version_id WHERE ms.layer=?""", (layer,)).fetchone()
    same = conn.execute("SELECT * FROM layer_model_versions WHERE layer=? AND fingerprint=? ORDER BY id DESC LIMIT 1", (layer, fingerprint)).fetchone()
    if same:
        result = dict(same); conn.close()
        result["attributes"] = json.loads(result.get("attributes_json") or "[]")
        result["evidence"] = json.loads(result.get("evidence_json") or "[]")
        result["metrics"] = json.loads(result.get("metrics_json") or "{}")
        return result
    active_score = _f(active_row["selection_score"], -999.0) if active_row else -999.0
    min_sample = 5
    selected = bool(int(metrics.get("sample") or 0) >= min_sample and (not active_row or score > float(active_score or -999.0) + 1e-9))
    status = "active" if selected else "challenger"
    key = f"{layer.lower()}-{ts_madrid_str().replace(':','').replace('-','').replace('T','')[:14]}"
    cur = conn.execute(
        """INSERT INTO layer_model_versions(layer,model_key,parent_model_id,status,selection_score,sample,complete_dates,metrics_json,attributes_json,evidence_json,fingerprint,created_at_utc,created_at_madrid)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (layer, key, int(active_row["id"]) if active_row else None, status, score, int(metrics.get("sample") or 0), int(metrics.get("complete_dates") or 0),
         json.dumps(metrics, ensure_ascii=False, default=str), json.dumps(attrs, ensure_ascii=False, default=str), json.dumps(evidence, ensure_ascii=False, default=str), fingerprint, ts_utc_str(), ts_madrid_str()),
    )
    model_id = int(cur.lastrowid)
    if selected:
        if active_row:
            conn.execute("UPDATE layer_model_versions SET status='retired' WHERE id=?", (int(active_row["id"]),))
        conn.execute("INSERT OR REPLACE INTO layer_model_state(layer,model_version_id,updated_at_utc) VALUES(?,?,?)", (layer, model_id, ts_utc_str()))
    conn.commit(); row = conn.execute("SELECT * FROM layer_model_versions WHERE id=?", (model_id,)).fetchone(); conn.close()
    _write_layer_artifacts(layer, model_id, metrics, attrs, rows)
    result = dict(row); result.update({"metrics": metrics, "attributes": attrs, "evidence": evidence, "selected": selected, "previous_id": int(active_row["id"]) if active_row else None, "previous_score": float(active_score or -999.0)})
    return result


def active_layer_model(layer: str) -> dict | None:
    ensure_v621_schema(); conn = _connect()
    row = conn.execute("""SELECT mv.* FROM layer_model_state ms JOIN layer_model_versions mv ON mv.id=ms.model_version_id WHERE ms.layer=?""", (str(layer).upper(),)).fetchone()
    if not row:
        conn.close(); return None
    item = dict(row)
    parent = conn.execute("SELECT * FROM layer_model_versions WHERE id=?", (item.get("parent_model_id"),)).fetchone() if item.get("parent_model_id") else None
    conn.close()
    for key in ("metrics", "attributes", "evidence"):
        try: item[key] = json.loads(item.get(f"{key}_json") or ("[]" if key != "metrics" else "{}"))
        except Exception: item[key] = [] if key != "metrics" else {}
    if parent:
        item["previous_id"] = int(parent["id"])
        try: item["previous_metrics"] = json.loads(parent["metrics_json"] or "{}")
        except Exception: item["previous_metrics"] = {}
        item["previous_score"] = parent["selection_score"]
    return item


def _core_attrs(model: dict | None) -> list[dict]:
    if not model:
        return []
    params = model.get("params") or {}
    grouped: dict[str, list[dict]] = {"LONG": [], "SHORT": [], "MIXED": []}
    for rule_name, rule in (params.get("rules") or {}).items():
        if str(rule_name).startswith("_"):
            continue
        direction = "LONG" if "LONG" in str(rule_name).upper() else "SHORT" if "SHORT" in str(rule_name).upper() else "MIXED"
        for cond in rule.get("conditions") or []:
            feature = str(cond.get("feature") or "")
            grouped[direction].append({
                "rule_name": str(rule_name),
                "direction": direction,
                "feature": feature,
                "op": cond.get("op"),
                "threshold": cond.get("threshold"),
                "interpretation": CORE_INTERPRETATIONS.get(
                    feature,
                    "condición point-in-time que separa movimientos ±5% de controles",
                ),
            })
    limit = int(getattr(config, "CALIBRATION_ATTRIBUTE_LIMIT", 6))
    rows: list[dict] = []
    # Keep the compact card representative of both sides of the calibrated model.
    for direction in ("LONG", "SHORT", "MIXED"):
        quota = max(1, limit // 2) if direction in {"LONG", "SHORT"} else limit
        rows.extend(grouped[direction][:quota])
    return rows[:limit]

def _core_ledger(model: dict | None) -> list[dict]:
    if not model: return []
    metrics = model.get("metrics") or {}
    report_dir = metrics.get("report_dir") or (metrics.get("walk_forward") or {}).get("report_dir")
    if not report_dir: return []
    path = Path(str(report_dir)) / "oos_trades.csv"
    if not path.exists(): return []
    import csv
    return list(csv.DictReader(path.open(encoding="utf-8-sig")))


def _daily_price(ticker: str, trade_date: str) -> float | None:
    if not ticker or not trade_date or not Path(config.SCANNER_DB).exists():
        return None
    conn = sqlite3.connect(config.SCANNER_DB, timeout=20)
    try:
        row = conn.execute(
            "SELECT current_price FROM ticker_daily_history WHERE ticker=? AND trade_date=? LIMIT 1",
            (str(ticker).upper(), str(trade_date)),
        ).fetchone()
        return _f(row[0]) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _enrich_core_trade_prices(row: dict) -> dict:
    item = dict(row)
    entry = _f(item.get("entry_price")) or _daily_price(str(item.get("ticker") or ""), str(item.get("entry_date") or ""))
    exit_price = _f(item.get("exit_price")) or _daily_price(str(item.get("ticker") or ""), str(item.get("exit_date") or ""))
    item["entry_price"] = entry
    item["exit_price"] = exit_price
    if not item.get("exit_session") and item.get("entry_date") and item.get("exit_date"):
        try:
            from datetime import date
            start_date = date.fromisoformat(str(item["entry_date"]))
            end_date = date.fromisoformat(str(item["exit_date"]))
            business_days = 0
            cursor = start_date
            while cursor < end_date:
                cursor = cursor.fromordinal(cursor.toordinal() + 1)
                if cursor.weekday() < 5:
                    business_days += 1
            item["exit_session"] = max(1, business_days)
        except Exception:
            pass
    return item


def _core_available_dates() -> int:
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT COUNT(DISTINCT snapshot_date) FROM feature_observations
               WHERE observation_layer='core_history' AND feature_tier='CORE'"""
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _layer_availability() -> dict:
    output = {
        "gex_daily_captured": 0,
        "gex_intraday_captured": 0,
        "gex_mature_dates": 0,
        "gex_mature_operations": 0,
        "graph_mature_dates": 0,
        "graph_mature_operations": 0,
    }
    try:
        from db import history_coverage
        coverage = history_coverage()
        output["gex_daily_captured"] = int(coverage.get("feature_dates") or 0)
    except Exception:
        pass
    try:
        from intraday_archive_bootstrap import intraday_archive_status
        intraday = intraday_archive_status()
        output["gex_intraday_captured"] = int(intraday.get("available_sessions") or 0)
    except Exception:
        pass
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT layer,sample,complete_dates FROM layer_model_calibration WHERE layer IN ('GEX_DAILY','GRAPH')"
        ).fetchall()
        for row in rows:
            layer = str(row[0] or "")
            if layer == "GEX_DAILY":
                output["gex_mature_operations"] = int(row[1] or 0)
                output["gex_mature_dates"] = int(row[2] or 0)
            elif layer == "GRAPH":
                output["graph_mature_operations"] = int(row[1] or 0)
                output["graph_mature_dates"] = int(row[2] or 0)
    except Exception:
        pass
    finally:
        conn.close()
    return output

def _delta(old: float | None, new: float | None, suffix: str = "", decimals: int = 1, lower_is_better: bool = False) -> str:
    if old is None or new is None:
        return "n/a"
    change = new - old
    improved = change < 0 if lower_is_better else change > 0
    symbol = "+" if change >= 0 else ""
    quality = "mejora" if improved else "empeora" if change else "igual"
    return f"{old:.{decimals}f}{suffix} → {new:.{decimals}f}{suffix} ({symbol}{change:.{decimals}f}{suffix}; {quality})"


def _core_evidence(model: dict | None, limit: int = 3) -> list[dict]:
    rows = _core_ledger(model)
    winners = sorted([r for r in rows if str(r.get("target_5pct_hit")).lower() in {"true", "1"}], key=lambda r: float(r.get("return_pct") or 0), reverse=True)[:2]
    failures = sorted([r for r in rows if str(r.get("target_5pct_hit")).lower() not in {"true", "1"}], key=lambda r: float(r.get("return_pct") or 0))[:1]
    return [_enrich_core_trade_prices(r) for r in (winners + failures)[:limit]]


def _metric(model: dict | None, key: str) -> float | None:
    return _f(normalized_oos_metrics((model or {}).get("metrics") or {}).get(key)) if model else None


def _arrow(old: float | None, new: float | None, suffix: str = "", decimals: int = 1) -> str:
    if old is None or new is None:
        value = new if new is not None else old
        return "n/a" if value is None else f"{value:.{decimals}f}{suffix}"
    return f"{old:.{decimals}f}{suffix} → {new:.{decimals}f}{suffix}"


def _threshold_text(attr: dict) -> str:
    threshold = attr.get("threshold")
    if isinstance(threshold, list):
        return f"{threshold[0]}–{threshold[-1]}"
    return str(threshold)


def _fmt_price(value: Any) -> str:
    v = _f(value)
    return "n/a" if v is None else f"{v:.2f}"


def calibration_messages(previous_core: dict | None, candidate_core: dict | None, effective_core: dict | None, layer_models: dict[str, dict | None]) -> list[str]:
    effective = effective_core or candidate_core
    active_before_m = normalized_oos_metrics((previous_core or {}).get("metrics") or {})
    candidate_m = normalized_oos_metrics((candidate_core or {}).get("metrics") or {})
    effective_m = normalized_oos_metrics((effective or {}).get("metrics") or {})
    gex = layer_models.get("GEX_DAILY") or {}
    graph = layer_models.get("GRAPH") or {}
    availability = _layer_availability()

    active_id = int((previous_core or {}).get("id") or 0)
    candidate_id = int((candidate_core or {}).get("id") or 0)
    effective_id = int((effective or {}).get("id") or 0)
    candidate_selected = bool(candidate_id and candidate_id == effective_id)
    decision = (
        f"CORE #{active_id} → #{candidate_id} · NUEVO MEJOR MODELO"
        if candidate_selected and candidate_id != active_id
        else f"CORE #{active_id} se mantiene · challenger #{candidate_id} no mejora el resultado global"
    )

    core_dates = _core_available_dates()
    result_lines = [
        f"**Datos CORE:** `{core_dates}` sesiones disponibles · mínimo `{int(getattr(config, 'CORE_HISTORY_TARGET_SESSIONS', 30))}` · ventana `EXPANSIVA`.",
        f"**Muestras:** activo `#{active_id}` `{int(active_before_m.get('sample') or 0)}` OOS · challenger `#{candidate_id}` `{int(candidate_m.get('sample') or 0)}` OOS.",
        f"**Hit ±5%:** `{_delta(_f(active_before_m.get('target_hit_rate')), _f(candidate_m.get('target_hit_rate')), '%')}`.",
        f"**Factor de beneficio (PF):** `{_delta(_f(active_before_m.get('profit_factor')), _f(candidate_m.get('profit_factor')), '', 2)}`.",
        f"**Caída máxima:** `{_delta(_f(active_before_m.get('max_drawdown')), _f(candidate_m.get('max_drawdown')), '%', 1, lower_is_better=True)}`.",
        f"**Tiempo medio al hit:** `{_delta(_f(active_before_m.get('avg_target_session')), _f(candidate_m.get('avg_target_session')), ' ses.', 1, lower_is_better=True)}`.",
        f"**Decisión:** {decision}.",
    ]
    if gex:
        gm = gex.get("metrics") or {}
        result_lines.append(
            f"**GEX:** modelo `#{int(gex.get('id') or 0)}` · `{int(gm.get('sample') or 0)}` operaciones maduras · target `{_f(gm.get('target_zone_hit_rate'),0):.1f}%`."
        )
    else:
        result_lines.append(
            f"**GEX:** `SIN MODELO · ACUMULANDO` · diario `{availability['gex_daily_captured']}` sesiones · "
            f"intradía `{availability['gex_intraday_captured']}` · outcomes maduros `{availability['gex_mature_operations']}`."
        )
    if graph:
        xm = graph.get("metrics") or {}
        result_lines.append(
            f"**EXPANSIÓN:** modelo `#{int(graph.get('id') or 0)}` · `{int(xm.get('sample') or 0)}` eventos maduros · hit `{_f(xm.get('movement_5pct_hit_rate'),0):.1f}%`."
        )
    else:
        result_lines.append(
            f"**EXPANSIÓN:** `SIN MODELO · ACUMULANDO` · eventos maduros `{availability['graph_mature_operations']}`."
        )
    result_lines.append(
        "**Glosario:** Factor de beneficio (PF) = beneficio bruto/pérdida bruta · "
        "excursión favorable máxima (MFE) = máximo avance a favor · "
        "excursión adversa máxima (MAE) = máximo movimiento en contra."
    )
    messages = [build_group("🧪 CALIBRACIÓN · RESULTADO", ["\n".join(result_lines)])]

    core_lines = [
        f"**CORE #{effective_id}** · anticipa dirección y ±5% · hit `{_f(effective_m.get('target_hit_rate'),0):.1f}%` · "
        f"PF `{_f(effective_m.get('profit_factor'),0):.2f}` · MFE `{_f(effective_m.get('avg_mfe_pct'),0):+.1f}%` · "
        f"MAE `{_f(effective_m.get('avg_mae_pct'),0):+.1f}%`."
    ]
    last_rule = None
    for attr in _core_attrs(effective):
        if attr.get("rule_name") != last_rule:
            core_lines.append(f"**{attr.get('rule_name')} · {attr.get('direction')}**")
            last_rule = attr.get("rule_name")
        core_lines.append(
            f"• `{attr['feature']} {attr['op']} {_threshold_text(attr)}` ({attr['interpretation']})."
        )
    evidence = _core_evidence(effective)
    if evidence:
        core_lines.append("**Operaciones base:**")
        for row in evidence:
            core_lines.append(
                f"• ${row.get('ticker')} {str(row.get('direction') or '').upper()} · "
                f"`{_fmt_price(row.get('entry_price'))} → {_fmt_price(row.get('exit_price'))}` · "
                f"`{float(row.get('return_pct') or 0):+.1f}%` · `{row.get('exit_session') or 'n/a'} ses.`"
            )
    messages.append(build_group("🧠 CORE · ATRIBUTOS Y EVIDENCIA", ["\n".join(core_lines)]))

    if not gex:
        gex_lines = [
            "**SIN MODELO · ACUMULANDO EVIDENCIA**",
            f"• Diario capturado: `{availability['gex_daily_captured']}` sesiones.",
            f"• Intradía capturado: `{availability['gex_intraday_captured']}` sesiones.",
            f"• Outcomes maduros: `{availability['gex_mature_operations']}` operaciones / `{availability['gex_mature_dates']}` fechas.",
            "• Se crearán atributos y operaciones GEX cuando existan outcomes futuros suficientes para evaluar walls, strikes, expiries, duración y cierre.",
        ]
    else:
        gex_m = gex.get("metrics") or {}
        gex_lines = [
            f"**GEX #{int(gex.get('id') or 0)}** · recorrido/duración/cierre · target `{_f(gex_m.get('target_zone_hit_rate'),0):.1f}%` · "
            f"cierre calibrado `sesión {_f(gex_m.get('calibrated_close_session'),0):.0f}` · captura `{_f(gex_m.get('calibrated_close_capture_pct'),0):.1f}%` · "
            f"devolución `{_f(gex_m.get('calibrated_close_giveback_pct'),0):.1f}%`."
        ]
        for attr in gex.get("attributes") or []:
            gex_lines.append(f"• `{attr.get('feature')} {attr.get('op')} {_threshold_text(attr)}` ({attr.get('interpretation')}).")
        if gex.get("evidence"):
            gex_lines.append("**Operaciones GEX base:**")
            for row in gex.get("evidence")[:3]:
                target = _fmt_price(row.get("target_low")) if row.get("target_low") == row.get("target_high") else f"{_fmt_price(row.get('target_low'))}–{_fmt_price(row.get('target_high'))}"
                gex_lines.append(
                    f"• ${row.get('ticker')} {str(row.get('direction') or '').upper()} · `{_fmt_price(row.get('entry_price'))} → {_fmt_price(row.get('exit_price'))}` · "
                    f"target `{target}` · wall `{row.get('level_name') or 'n/a'} {_fmt_price(row.get('level_price'))}` / opuesto `{row.get('opposite_level_name') or 'n/a'} {_fmt_price(row.get('opposite_level_price'))}` · "
                    f"asim. `{_f(row.get('wall_asymmetry'),0):.1f}x` · Δstrike `{_f(row.get('total_gex_change_pct'),0):+.1f}%` · "
                    f"Δ0–7d `{_f(row.get('near_gex_change_pct'),0):+.1f}%` · expiry `{row.get('expiry_regime') or 'n/a'}` · "
                    f"captura `{_f(row.get('movement_capture_pct'),0):.0f}%` / devolución `{_f(row.get('giveback_pct'),0):.1f}%` · {'HIT' if row.get('target_hit') else 'FALLO'}."
                )
    messages.append(build_group("🧲 GEX · ATRIBUTOS Y EVIDENCIA", ["\n".join(gex_lines)]))

    if not graph:
        graph_lines = [
            "**SIN MODELO · ACUMULANDO EVIDENCIA**",
            f"• Eventos maduros: `{availability['graph_mature_operations']}` / `{availability['graph_mature_dates']}` fechas.",
            "• Se calibrará cuando pueda medirse qué señales generales anticiparon posteriormente CORE y GEX.",
        ]
    else:
        graph_m = graph.get("metrics") or {}
        graph_lines = [
            f"**EXPANSIÓN #{int(graph.get('id') or 0)}** · hit ±5% `{_f(graph_m.get('movement_5pct_hit_rate'),0):.1f}%` · "
            f"MFE `{_f(graph_m.get('avg_max_favorable_pct'),0):+.1f}%` / MAE `{_f(graph_m.get('avg_max_adverse_pct'),0):+.1f}%`."
        ]
        for attr in graph.get("attributes") or []:
            graph_lines.append(f"• `{attr.get('feature')} {attr.get('op')} {_threshold_text(attr)}` ({attr.get('interpretation')}).")
    messages.append(build_group("🌐 EXPANSIÓN · ATRIBUTOS Y EVIDENCIA", ["\n".join(graph_lines)]))
    return messages

def publish_calibration(previous_core: dict | None, candidate_core: dict | None, effective_core: dict | None, layer_models: dict[str, dict | None]) -> list[str]:
    messages = calibration_messages(previous_core, candidate_core, effective_core, layer_models)
    for message in messages: send_daily(message)
    return messages


def _condition_lines(model: dict | None, features: dict) -> tuple[list[str], int]:
    lines: list[str] = []; matches = 0
    for attr in _core_attrs(model):
        value = _f(features.get(attr["feature"])); threshold = _f(attr.get("threshold"))
        if value is None or threshold is None or attr.get("op") not in {">=", "<="}: continue
        ok = value >= threshold if attr.get("op") == ">=" else value <= threshold
        matches += int(ok)
        lines.append(f"• `{attr['feature']} {value:.2f} {attr['op']} {threshold:.2f}` ({attr['interpretation']}) {'✅' if ok else '❌'}")
    return lines[:6], matches


def _gex_condition_lines(gex_model: dict | None, features: dict, direction: str) -> tuple[list[str], int]:
    lines: list[str] = []; matches = 0
    sign = 1.0 if str(direction).lower() == "long" else -1.0
    for attr in (gex_model or {}).get("attributes") or []:
        feature = str(attr.get("feature") or ""); op = str(attr.get("op") or ""); threshold = attr.get("threshold")
        if feature in {"exit_gex_reversal_pct", "exit_pattern_score", "soft_exit_confirmation_runs", "calibrated_close_session"}:
            continue
        value = features.get(feature)
        number = _f(value)
        ok = False; rendered = str(value)
        if op == ">=" and number is not None and _f(threshold) is not None:
            ok = number >= float(threshold); rendered = f"{number:.2f} >= {float(threshold):.2f}"
        elif op == "directional_abs>=" and number is not None and _f(threshold) is not None:
            directional = sign * number
            ok = directional >= float(threshold); rendered = f"dir {directional:.2f} >= {float(threshold):.2f}"
        elif op == "between" and number is not None and isinstance(threshold, list) and len(threshold) >= 2:
            low, high = float(threshold[0]), float(threshold[-1]); actual = abs(number)
            ok = low <= actual <= high; rendered = f"{actual:.2f} in [{low:.2f},{high:.2f}]"
        elif op == "=":
            ok = str(value) == str(threshold); rendered = f"{value} = {threshold}"
        else:
            continue
        matches += int(ok)
        lines.append(f"• `{feature}: {rendered}` ({attr.get('interpretation')}) {'✅' if ok else '❌'}")
    return lines[:4], matches


def _core_comparable(model: dict | None, event: dict) -> dict:
    rows = _core_ledger(model); direction = str(event.get("direction") or ""); rule = str(event.get("rule_name") or "")
    comparable = [r for r in rows if str(r.get("direction") or "") == direction and (not rule or str(r.get("rule_name") or "") == rule)]
    if not comparable: comparable = [r for r in rows if str(r.get("direction") or "") == direction]
    hits = sum(str(r.get("target_5pct_hit")).lower() in {"true", "1"} for r in comparable)
    durations = [_f(r.get("target_session")) for r in comparable if _f(r.get("target_session")) is not None]
    entries = [_f(r.get("entry_price")) for r in comparable if _f(r.get("entry_price")) is not None]
    exits = [_f(r.get("exit_price")) for r in comparable if _f(r.get("exit_price")) is not None]
    return {"sample": len(comparable), "hit_rate": hits/max(1,len(comparable))*100.0, "duration": mean(durations) if durations else None, "entry": mean(entries) if entries else None, "exit": mean(exits) if exits else None}


def build_alert_traceability(event: dict, model: dict | None) -> str:
    try: features = json.loads(event.get("features_json") or "{}")
    except Exception: features = {}
    core_lines, matches = _condition_lines(model, features)
    gex = active_layer_model("GEX_DAILY") or {}; ticker = str(event.get("ticker") or "").upper(); direction = str(event.get("direction") or "")
    core_cmp = _core_comparable(model, event)
    all_gex = [r for r in _all_signal_outcomes() if str(r.get("layer") or "").upper() == "GEX_DAILY" and str(r.get("direction") or "") == direction]
    current_intent = str(features.get("gex_intent") or "")
    comparable = [r for r in all_gex if not current_intent or str(r.get("gex_intent") or _payload(r).get("gex_intent") or "") == current_intent] or all_gex
    gex_hits = sum(int(r.get("target_zone_hit") or 0) for r in comparable)
    gex_durations = [_f(r.get("first_target_session")) for r in comparable if _f(r.get("first_target_session")) is not None]
    gex_close_stats = [_row_close_stats(r) for r in comparable]
    gex_captures = [_f(item.get("movement_capture_pct")) for item in gex_close_stats if _f(item.get("movement_capture_pct")) is not None]
    gex_givebacks = [_f(item.get("giveback_pct")) for item in gex_close_stats if _f(item.get("giveback_pct")) is not None]
    gex_rule_lines, gex_matches = _gex_condition_lines(gex, features, direction)
    target = features.get("target_price") or features.get("target_zone_low")
    lines = [
        f"**${ticker} · {direction.upper()} · CORE #{int((model or {}).get('id') or 0)} / GEX #{int(gex.get('id') or 0)}**",
        f"**CORE:** `{matches}` reglas activadas · `{core_cmp['sample']}` casos comparables · hit ±5% `{core_cmp['hit_rate']:.0f}%`" + (f" · entrada/salida media `{core_cmp['entry']:.2f} → {core_cmp['exit']:.2f}` · `{core_cmp['duration']:.1f} ses.`" if core_cmp['entry'] is not None and core_cmp['exit'] is not None and core_cmp['duration'] is not None else "") + ".",
        *core_lines,
        f"**GEX actual:** `{gex_matches}` reglas · target `{_fmt_price(target)}` · wall `{features.get('level_name') or 'n/a'} {_fmt_price(features.get('level_price'))}` / opuesto `{features.get('opposite_level_name') or 'n/a'} {_fmt_price(features.get('opposite_level_price'))}` · asim. `{_f(features.get('wall_asymmetry'),0):.1f}x` · Δstrike `{_f(features.get('total_gex_change_pct'),0):+.1f}%` · Δ0–7d `{_f(features.get('near_gex_change_pct'),0):+.1f}%` · expiry `{features.get('expiry_regime') or 'n/a'}`.",
        *gex_rule_lines,
        f"**Evidencia GEX:** `{len(comparable)}` casos · target `{gex_hits/max(1,len(comparable))*100:.0f}%`" + (f" · duración `{mean(gex_durations):.1f} ses.`" if gex_durations else "") + (f" · captura `{mean(gex_captures):.0f}%`" if gex_captures else "") + (f" · devolución `{mean(gex_givebacks):.1f}%`" if gex_givebacks else "") + ".",
    ]
    return build_group("🧬 TRAZABILIDAD · ALERTA → CALIBRACIÓN", ["\n".join(lines)])


def send_alert_traceability(event: dict, model: dict | None) -> bool:
    message = build_alert_traceability(event, model); ensure_v621_schema(); gex = active_layer_model("GEX_DAILY") or {}; conn = _connect()
    cur = conn.execute(
        """INSERT OR IGNORE INTO calibration_trace_history(ticker,direction,source_scanner_run_id,core_model_id,gex_model_id,trace_json,created_at_utc)
           VALUES(?,?,?,?,?,?,?)""",
        (str(event.get("ticker") or ""), str(event.get("direction") or ""), event.get("source_scanner_run_id"), (model or {}).get("id"), gex.get("id"), json.dumps({"message": message}, ensure_ascii=False), ts_utc_str()),
    )
    inserted = cur.rowcount > 0; conn.commit(); conn.close()
    return True if not inserted else bool(send_alert(message))
