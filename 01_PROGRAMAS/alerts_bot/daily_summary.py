from __future__ import annotations

import json
import sqlite3

import config
from adaptive_learning import effective_research_gate
from db import history_coverage, latest_canonical_daily_dates, get_asset_universe_nodes, get_asset_relationships, get_runtime_state
from discord_utils import send_daily
from time_utils import now_madrid, market_date_from_madrid_iso
from intraday_archive_bootstrap import intraday_archive_status
from model_metrics import normalized_oos_metrics
from layer_calibration import get_layer_calibrations
from discord_presentation import build_group, status_display


RULE_LABELS = {
    "CORE_GEX_TREND_LONG": "CALL · GEX total + tendencia",
    "CORE_GEX_TREND_SHORT": "PUT · GEX total + tendencia",
    "CORE_FLOW_MOMENTUM_LONG": "CALL · flow + momentum",
    "CORE_FLOW_MOMENTUM_SHORT": "PUT · flow + momentum",
    "CORE_RELATIVE_STRENGTH_LONG": "CALL · fortaleza relativa",
    "CORE_RELATIVE_STRENGTH_SHORT": "PUT · debilidad relativa",
    "PUT_WALL_DEFENSE_LONG": "CALL · defensa Put Wall",
    "CALL_WALL_REJECTION_SHORT": "PUT · rechazo Call Wall",
    "CALL_WALL_BREAK_LONG": "CALL · ruptura Call Wall",
    "PUT_WALL_BREAK_SHORT": "PUT · pérdida Put Wall",
    "EXPIRY_GEX_SHIFT_LONG": "CALL · giro GEX expiry",
    "EXPIRY_GEX_SHIFT_SHORT": "PUT · giro GEX expiry",
    "DISCOVERED_MOVER_LONG_1": "CALL · patrón descubierto #1",
    "DISCOVERED_MOVER_LONG_2": "CALL · patrón descubierto #2",
    "DISCOVERED_MOVER_LONG_3": "CALL · patrón descubierto #3",
    "DISCOVERED_MOVER_LONG_6": "CALL · patrón descubierto #6",
    "DISCOVERED_MOVER_LONG_5": "CALL · patrón descubierto #5",
    "DISCOVERED_MOVER_LONG_4": "CALL · patrón descubierto #4",
    "DISCOVERED_MOVER_SHORT_1": "PUT · patrón descubierto #1",
    "DISCOVERED_MOVER_SHORT_2": "PUT · patrón descubierto #2",
    "DISCOVERED_MOVER_SHORT_3": "PUT · patrón descubierto #3",
    "DISCOVERED_MOVER_SHORT_6": "PUT · patrón descubierto #6",
    "DISCOVERED_MOVER_SHORT_5": "PUT · patrón descubierto #5",
    "DISCOVERED_MOVER_SHORT_4": "PUT · patrón descubierto #4",
}

CORE_RULES = {
    name for name in RULE_LABELS
    if name.startswith("CORE_") or name.startswith("DISCOVERED_MOVER_")
}


def _active_model_row(conn: sqlite3.Connection):
    return conn.execute(
        """SELECT mv.* FROM model_versions mv
           JOIN model_state ms ON ms.model_version_id=mv.id AND ms.state_key='active'"""
    ).fetchone()


def _json(value) -> dict:
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def _fmt(value, decimals: int = 1, signed: bool = False) -> str:
    try:
        number = float(value)
        if decimals <= 0:
            return str(int(round(number)))
        rendered = f"{number:.{decimals}f}"
        return rendered.rstrip("0").rstrip(".")
    except Exception:
        return "n/a"


def _fmt_context(context: str) -> str:
    return context[:1].upper() + context[1:] if context else "Estado"




def _flatten_relationship_edges(value) -> list[dict]:
    """Normalize DB relationship maps/lists into a flat list of edge dictionaries.

    get_asset_relationships() intentionally returns {ticker: [edges]}. Older
    summary code iterated the mapping keys and crashed when calling .get() on
    ticker strings. Keep this boundary defensive so malformed rows cannot break
    the daily close handoff or recalibration.
    """
    edges: list[dict] = []
    if isinstance(value, dict):
        candidates = value.values()
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        candidates = []
    for item in candidates:
        if isinstance(item, dict):
            edges.append(item)
        elif isinstance(item, (list, tuple)):
            edges.extend(edge for edge in item if isinstance(edge, dict))
    return edges

def _model_payload(active) -> tuple[dict, dict, str]:
    params = _json(active["params_json"]) if active else {}
    metrics = _json(active["metrics_json"]) if active else {}
    tier = str(
        params.get("feature_tier")
        or metrics.get("feature_tier")
        or "BOOTSTRAP"
    ).upper()
    return params, metrics, tier


def _oos_metrics(metrics: dict) -> dict:
    return normalized_oos_metrics(metrics)


def build_daily_summary() -> str:
    market_date = market_date_from_madrid_iso(now_madrid().isoformat())
    coverage = history_coverage()
    daily_dates = latest_canonical_daily_dates(config.DAILY_CLOSE_WINDOW_SESSIONS)
    window_start = daily_dates[0] if daily_dates else str(coverage.get("core_feature_start_date") or market_date)

    conn = sqlite3.connect(config.ALERTS_DB)
    conn.row_factory = sqlite3.Row
    active = _active_model_row(conn)
    tuning = conn.execute("SELECT * FROM tuning_runs ORDER BY id DESC LIMIT 1").fetchone()
    today_alerts = conn.execute("SELECT COUNT(*) FROM alert_events WHERE snapshot_date=?", (market_date,)).fetchone()[0]
    today_live = conn.execute(
        "SELECT COUNT(*) FROM alert_events WHERE snapshot_date=? AND sent_to_discord=1 "
        "AND status NOT LIKE 'shadow%'", (market_date,),
    ).fetchone()[0]
    today_shadow = conn.execute(
        "SELECT COUNT(*) FROM alert_events WHERE snapshot_date=? AND status LIKE 'shadow%'", (market_date,),
    ).fetchone()[0]
    # sent_to_discord and the alert status belong to alert_events, not to
    # open_trades. Join through alert_event_id so existing production DBs do
    # not fail with "no such column: sent_to_discord" during the daily summary.
    active_position_counts = conn.execute(
        """SELECT
               SUM(CASE WHEN COALESCE(ae.sent_to_discord,0)=1
                         AND COALESCE(ae.status,'') NOT LIKE 'shadow%'
                        THEN 1 ELSE 0 END) AS live_n,
               SUM(CASE WHEN COALESCE(ae.status,'') LIKE 'shadow%'
                        THEN 1 ELSE 0 END) AS shadow_n
           FROM open_trades ot
           LEFT JOIN alert_events ae ON ae.id=ot.alert_event_id
           WHERE ot.status='open'"""
    ).fetchone()
    active_live_positions = int(active_position_counts[0] or 0)
    active_shadow_positions = int(active_position_counts[1] or 0)

    # Restored databases and validation fixtures may still expose the pre-runner
    # open_trades schema. Keep the report readable while startup migrations add
    # the v6.1.15 columns; never fail the daily handoff over an optional metric.
    trade_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(open_trades)").fetchall()}
    setup_hit_expr = "setup_hit" if "setup_hit" in trade_columns else "0"
    best_return_expr = "best_return_pct" if "best_return_pct" in trade_columns else "NULL"
    capture_expr = "exit_capture_ratio_pct" if "exit_capture_ratio_pct" in trade_columns else "NULL"
    closed_metrics_sql = (
        "SELECT COUNT(*), AVG(exit_return_pct), "
        "SUM(CASE WHEN exit_return_pct>0 THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN {setup_hit_expr}=1 THEN 1 ELSE 0 END), "
        f"AVG({best_return_expr}), AVG({capture_expr}) FROM open_trades "
    )
    calibration_filter = (
        " AND calibration_excluded_reason IS NULL"
        if "calibration_excluded_reason" in trade_columns else ""
    )
    closed_today = conn.execute(
        closed_metrics_sql + "WHERE status='closed' AND exit_date=?" + calibration_filter, (market_date,),
    ).fetchone()
    closed_window = conn.execute(
        closed_metrics_sql + "WHERE status='closed' AND entry_date>=?" + calibration_filter, (window_start,),
    ).fetchone()
    if {"setup_hit", "setup_hit_date"}.issubset(trade_columns):
        setup_hits_today = conn.execute(
            "SELECT COUNT(*) FROM open_trades WHERE setup_hit=1 AND setup_hit_date=?", (market_date,),
        ).fetchone()[0]
    else:
        setup_hits_today = 0
    if "setup_hit" in trade_columns:
        open_runners = conn.execute(
            "SELECT COUNT(*) FROM open_trades WHERE status='open' AND setup_hit=1"
        ).fetchone()[0]
    else:
        open_runners = 0
    intraday_live = conn.execute(
        "SELECT COUNT(*) FROM feature_observations WHERE observation_layer='intraday' AND source_archive IS NULL"
    ).fetchone()[0]
    conn.close()

    _params, metrics, tier = _model_payload(active)
    intraday = intraday_archive_status()
    oos = _oos_metrics(metrics)
    graph_nodes = get_asset_universe_nodes()
    graph_edges = _flatten_relationship_edges(get_asset_relationships())
    structural_edges = sum(
        1 for edge in graph_edges
        if str(edge.get("relation_type") or "") in {
            "leveraged_parent", "leveraged_confirmation", "single_stock_parent", "single_stock_confirmation",
        }
    )

    today_closed_n = int(closed_today[0] or 0)
    today_avg = float(closed_today[1] or 0.0)
    today_wins = int(closed_today[2] or 0)
    today_win_rate = today_wins / today_closed_n * 100 if today_closed_n else 0.0
    today_setup_wins = int(closed_today[3] or 0)
    today_mfe = float(closed_today[4] or 0.0)
    today_capture = float(closed_today[5] or 0.0)
    closed_n = int(closed_window[0] or 0)
    avg_ret = float(closed_window[1] or 0.0)
    wins = int(closed_window[2] or 0)
    setup_wins = int(closed_window[3] or 0)
    avg_mfe = float(closed_window[4] or 0.0)
    avg_capture = float(closed_window[5] or 0.0)
    win_rate = wins / closed_n * 100 if closed_n else 0.0
    setup_hit_rate = setup_wins / closed_n * 100 if closed_n else 0.0
    model_key = active["model_key"] if active else "sin modelo"
    model_status = str(active["status"] if active else "none").upper()
    tuning_status = str(tuning["status"] if tuning else "sin calibración").upper()
    entry_mode = str(get_runtime_state("entry_mode", "off") or "off").upper()

    cards = [
        "\n".join([
            "### Operativa del día",
            f"**Entradas:** live `{today_live}` · shadow `{today_shadow}` · eventos totales `{today_alerts}`",
            f"**Posiciones abiertas:** live `{active_live_positions}` · shadow `{active_shadow_positions}` · runners `{int(open_runners or 0)}` · **cerradas hoy:** `{today_closed_n}`",
            f"**Setup hit hoy:** nuevos `{int(setup_hits_today or 0)}` · cerrados que habían ganado `{today_setup_wins}`",
            f"**Gestión de cierres:** retorno positivo `{today_win_rate:.1f}%` · retorno medio `{today_avg:+.2f}%` · MFE medio `{today_mfe:+.2f}%` · captura `{today_capture:.1f}%`",
        ]),
        "\n".join([
            "### Modelo y validación",
            f"**Activo:** `{model_key}` · `{model_status}/{tier}` · modo `{entry_mode}`",
            f"**OOS:** trades `{int(oos.get('sample') or 0)}` · fechas `{int(oos.get('market_dates') or 0)}` · "
            f"win `{_fmt(oos.get('win_rate'),1)}%` · avg `{_fmt(oos.get('avg_return'),2)}%` · "
            f"PF `{_fmt(oos.get('profit_factor'),2)}` · MaxDD `{_fmt(oos.get('max_drawdown'),2)}`",
            f"**Última recalibración:** `{tuning_status}`",
        ]),
        "\n".join([
            "### Cobertura de aprendizaje",
            f"**CORE:** `{coverage.get('core_feature_dates', 0)}/{config.CORE_HISTORY_TARGET_SESSIONS}` sesiones · "
            f"outcomes `{coverage.get('core_outcome_dates', 0)}`",
            f"**FULL DEEP:** `{coverage.get('feature_dates', 0)}/{config.DAILY_CLOSE_WINDOW_SESSIONS}`",
            f"**Intradía:** live `{intraday_live}` obs · ZIP `{intraday.get('available_sessions', 0)}/"
            f"{intraday.get('required_sessions', config.INTRADAY_CALIBRATION_MIN_ARCHIVED_SESSIONS)}` · "
            f"`{'VALIDABLE' if intraday.get('ready') else 'ACUMULANDO'}`",
            f"**Grafo:** nodos `{len(graph_nodes)}` · edges `{len(graph_edges)}` · estructurales `{structural_edges}`",
        ]),
        "\n".join([
            f"### Ventana evaluada · desde `{window_start}`",
            f"Cierres `{closed_n}` · setup hit ±5% `{setup_hit_rate:.1f}%` · retorno final positivo `{win_rate:.1f}%`",
            f"Retorno final medio `{avg_ret:+.2f}%` · MFE medio `{avg_mfe:+.2f}%` · capture ratio `{avg_capture:.1f}%`",
            "El perfil **precision-first** puede cerrar una sesión con cero entradas cuando ningún setup supera todos los gates.",
        ]),
    ]
    return build_group(f"🌙 CIERRE DE SESIÓN · {market_date}", cards)


def _core_rule_line(label: str, rule: dict) -> str:
    conditions = rule.get("conditions") or []
    if conditions:
        rendered=[]
        for condition in conditions[:5]:
            feature=str(condition.get("feature") or "variable")
            op=str(condition.get("op") or condition.get("operator") or "")
            value=condition.get("value")
            rendered.append(f"{feature} {op} {_fmt(value,2)}".strip())
        stats=rule.get("discovery_stats") or {}
        return (
            f"• {label}: " + "; ".join(rendered) +
            f" · score≥`{_fmt(rule.get('min_score'),0)}`"
            f" · muestra patrón `{int(stats.get('selected') or 0)}`"
            f" · precisión patrón `{_fmt((stats.get('precision') or 0)*100,1)}%`"
            f" · lift `{_fmt(stats.get('lift'),2)}`"
        )
    return (
        f"• {label}: score≥`{_fmt(rule.get('min_score'),0)}` "
        f"· momentum≥`{_fmt(rule.get('core_min_momentum'),1)}` "
        f"· RVOL≥`{_fmt(rule.get('min_rvol'),2)}` "
        f"· contexto≥`{_fmt(rule.get('min_context_score'),1)}` "
        f"· flow `{'obligatorio' if int(rule.get('core_require_flow') or 0) else 'opcional'}`"
    )


def _full_rule_line(label: str, rule: dict) -> str:
    return (
        f"• {label}: score≥`{_fmt(rule.get('min_score'),0)}` "
        f"· RVOL≥`{_fmt(rule.get('min_rvol'),2)}` "
        f"· Δexpiry≥`{_fmt(rule.get('expiry_change_pct'),1)}%` "
        f"· Δstrike≥`{_fmt(rule.get('strike_change_pct'),1)}%` "
        f"· proyección≥`{_fmt(rule.get('min_daily_pattern_projection'),0)}%`"
    )


def _layer_calibration_stats() -> dict:
    layers = get_layer_calibrations()
    gex = layers.get("GEX_DAILY") or {}
    timing = layers.get("GEX_INTRADAY") or {}
    graph = layers.get("GRAPH") or {}
    fusion = layers.get("FUSION") or {}
    return {
        "layers": layers,
        "gex_signals": int(gex.get("sample") or 0),
        "gex_complete": int(gex.get("sample") or 0),
        "gex_hits": round((float(gex.get("target_zone_hit_rate") or 0.0) / 100.0) * int(gex.get("sample") or 0)),
        "gex_hit_rate": gex.get("target_zone_hit_rate"),
        "gex_avg_timing": gex.get("avg_first_target_session"),
        "gex_target_mae": gex.get("target_mae_pct"),
        "gex_status": gex.get("status") or "ACCUMULATING",
        "timing_status": timing.get("status") or "ACCUMULATING",
        "timing_uplift": timing.get("timing_uplift"),
        "graph_signals": int(graph.get("sample") or 0),
        "graph_status": graph.get("status") or "ACCUMULATING",
        "fusion_status": fusion.get("status") or "ACCUMULATING",
    }


def _condition_text(rule: dict) -> str:
    conditions = rule.get("conditions") or []
    if not conditions:
        return "regla heredada"
    parts=[]
    for item in conditions[:3]:
        parts.append(f"{item.get('feature')} {item.get('op')} {_fmt(item.get('threshold', item.get('value')), 1)}")
    stats=rule.get("discovery_stats") or {}
    suffix=f" · precisión `{_fmt((stats.get('precision') or 0)*100,1)}%` · lift `{_fmt(stats.get('lift'),2)}`" if stats else ""
    return "; ".join(parts)+suffix



def _unique_core_rule_families(rules: dict) -> list[tuple[str, dict]]:
    selected: dict[str, tuple[str, dict, float]] = {}
    for name, rule in rules.items():
        if str(name).startswith("_") or not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        family = str(rule.get("family") or "OTHER")
        stats = rule.get("discovery_stats") or {}
        quality = float(stats.get("precision") or 0.0) * 3 + float(stats.get("lift") or 0.0) + min(float(stats.get("selected") or 0.0), 1000.0) / 10000.0
        if family not in selected or quality > selected[family][2]:
            selected[family] = (name, rule, quality)
    return [(name, rule) for name, rule, _ in sorted(selected.values(), key=lambda item: item[2], reverse=True)]

def build_rules_config_message(context: str = "estado") -> tuple[str, dict]:
    conn = sqlite3.connect(config.ALERTS_DB); conn.row_factory = sqlite3.Row
    active = _active_model_row(conn); conn.close()
    model_key = active["model_key"] if active else "sin-modelo"
    model_id = int(active["id"] or 0) if active else 0
    model_status = str(active["status"] if active else "none").upper()
    params, metrics, tier = _model_payload(active)
    coverage = history_coverage(); intraday = intraday_archive_status(); oos = _oos_metrics(metrics)
    entry_mode = str(get_runtime_state("entry_mode", "off") or "off").upper()
    layer_stats = _layer_calibration_stats()
    research_gate = float(effective_research_gate())
    live_gate = float(config.PRECISION_FIRST_MIN_LIVE_SCORE)
    cards = ["\n".join([
        f"**CORE:** `#{model_id} · {model_key}` · `{status_display(model_status)}` · modo `{status_display(entry_mode)}`",
        f"**Gates:** Research/Fusion `≥{research_gate:.0f}` · ENTER LIVE score `≥{live_gate:.0f}` + proyección `≥{config.PRECISION_FIRST_MIN_LIVE_PROJECTION:.0f}%` + `{config.PRECISION_FIRST_MIN_CONFIRMING_BLOCKS}` confirmaciones",
        f"**Evidencia:** CORE `{coverage.get('core_feature_dates',0)}` sesiones · OOS `{int(oos.get('sample') or 0)}` · hit ±5% `{_fmt(oos.get('target_hit_rate'),1)}%` · PF `{_fmt(oos.get('profit_factor'),2)}`",
        f"**Capas:** GEX diario `{coverage.get('feature_dates',0)}/{config.DAILY_CLOSE_WINDOW_SESSIONS}` · intradía `{intraday.get('available_sessions',0)}/{intraday.get('required_sessions',config.INTRADAY_CALIBRATION_MIN_ARCHIVED_SESSIONS)}` · EXP `{status_display(layer_stats.get('graph_status'))}`",
        "**Lifecycle:** ±5%/target son milestone; cierre solo por reversión propia CORE/GEX/estructura confirmada.",
    ])]
    message = build_group(f"📘 CONFIGURACIÓN {'ACTIVA' if model_status == 'CHAMPION' else 'PROVISIONAL'} · v{config.BOT_VERSION}", cards)
    return message, {"generated_at_madrid": now_madrid().isoformat(), "context": context, "version": config.BOT_VERSION,
        "model_id": model_id, "model_key": model_key, "model_status": model_status, "feature_tier": tier,
        "entry_mode": entry_mode, "coverage": coverage, "intraday_archive_status": intraday,
        "layer_stats": layer_stats, "params": params, "metrics": metrics,
        "research_gate": research_gate, "live_gate": live_gate}

def send_rules_config(context: str = "estado") -> str:
    enabled = (
        config.SEND_RULES_CONFIG_ON_START
        if context == "arranque"
        else config.SEND_RULES_CONFIG_AFTER_TRAIN
    )
    message, payload = build_rules_config_message(context)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (config.OUTPUT_DIR / "active_rules_config.txt").write_text(message, encoding="utf-8")
    (config.OUTPUT_DIR / "active_rules_config.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if enabled:
        send_daily(message)
    print(
        f"📄 Config efectiva guardada | {config.OUTPUT_DIR / 'active_rules_config.txt'}",
        flush=True,
    )
    return message


def send_daily_operating_summary() -> str:
    message = build_daily_summary()
    send_daily(message)
    return message
