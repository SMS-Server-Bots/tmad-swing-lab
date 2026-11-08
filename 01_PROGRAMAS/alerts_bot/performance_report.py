from __future__ import annotations

import csv
import html
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from model_metrics import normalized_oos_metrics
from discord_utils import send_daily
from rules import RULE_DIRECTIONS, evaluate_rule, exit_pattern_signal, params_for_rule
from time_utils import now_madrid
from discord_presentation import build_group


def _directional(raw: float, direction: str) -> float:
    return raw if direction == "long" else -raw


def _clock_minutes(value: str) -> int:
    hour, minute = [int(x) for x in value.split(":", 1)]
    return hour * 60 + minute


def _is_close_observation(row: dict) -> bool:
    try:
        if row.get("is_canonical_daily_close") is not None:
            return bool(int(row.get("is_canonical_daily_close") or 0))
    except Exception:
        pass
    if str(row.get("observation_layer") or "").lower():
        return str(row.get("observation_layer") or "").lower() == "daily_close"
    value = row.get("scanner_captured_at_madrid") or row.get("observed_at_madrid")
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(config.TIMEZONE_NAME))
        market = dt.astimezone(ZoneInfo(config.ENTRY_MARKET_TIMEZONE))
        return market.hour * 60 + market.minute >= _clock_minutes(config.CLOSE_SNAPSHOT_MARKET_TIME)
    except Exception:
        return True


def _select_model(conn: sqlite3.Connection):
    # Backtest reporting follows the latest calibrated candidate, including a
    # rejected challenger. The effective config is reported separately by
    # daily_summary from model_state.
    row = conn.execute(
        "SELECT * FROM model_versions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _walkforward_report_from_model(model: dict, label: str) -> dict | None:
    try:
        metrics_payload = json.loads(model.get("metrics_json") or "{}")
    except Exception:
        return None
    wf = metrics_payload.get("walk_forward")
    if not wf:
        return None
    nested_walkforward = bool(wf.get("walk_forward_oos") is not None)
    oos = (wf.get("walk_forward_oos") or {}) if nested_walkforward else wf
    baseline = wf.get("baseline_oos") or {}
    folds_payload = metrics_payload.get("folds") or []
    discovery_mode = bool(not nested_walkforward and oos.get("sample") is not None)
    output_root = config.OUTPUT_DIR / label
    output_root.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at_madrid": now_madrid().isoformat(),
        "model_key": model.get("model_key"),
        "model_status": model.get("status"),
        "method": ("mover_discovery_purged_walk_forward" if discovery_mode else "purged_expanding_walk_forward_relational_joint_policy"),
        "pattern_target_pct": config.PATTERN_TARGET_PCT,
        "max_horizon_sessions": config.MAX_PATTERN_HORIZON_SESSIONS,
        "market_dates": oos.get("market_dates", wf.get("market_dates", 0)),
        "fold_count": (wf.get("fold_count", 0) if nested_walkforward else len(folds_payload)),
        "alerts_closed": oos.get("sample", 0),
        "target_5pct_hit_rate": oos.get("target_hit_rate", 0.0),
        "win_rate_pct": oos.get("win_rate", 0.0),
        "avg_return_pct": oos.get("avg_return", 0.0),
        "profit_factor": oos.get("profit_factor", 0.0),
        "max_drawdown": oos.get("max_drawdown", 0.0),
        "oos_objective": oos.get("objective", -999.0),
        "baseline_objective": baseline.get("objective", -999.0),
        "objective_improvement": wf.get("objective_improvement_vs_baseline", 0.0),
        "relational_objective_lift": wf.get("relational_objective_lift"),
        "portfolio_policy": wf.get("portfolio_policy") or {},
        "enabled_rules": wf.get("enabled_rules", list((metrics_payload.get("precision_rule_oos_metrics") or {}).keys())),
        "disabled_rules": wf.get("disabled_rules", []),
        "status": ("discovery_oos" if discovery_mode else wf.get("status", "unknown")),
        "report_dir": metrics_payload.get("report_dir"),
        "ci90_low": oos.get("mean_daily_return_ci90_low"),
        "ci90_high": oos.get("mean_daily_return_ci90_high"),
    }
    (output_root / "walkforward_status.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    report = f"""<!doctype html><html><head><meta charset='utf-8'><title>TMAD Walk-forward status</title>
<style>body{{font-family:Segoe UI,Arial;margin:32px;color:#152238}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{border:1px solid #ccd6e0;padding:16px;border-radius:8px}}.value{{font-size:26px;font-weight:700}}</style></head><body>
<h1>TMAD — resultados fuera de muestra</h1><p>Método: purged expanding walk-forward relacional + política conjunta; objetivo +5% en ≤5 sesiones.</p>
<div class='grid'><div class='card'>Trades OOS<div class='value'>{summary['alerts_closed']}</div></div><div class='card'>Hit +5%<div class='value'>{summary['target_5pct_hit_rate']:.2f}%</div></div><div class='card'>Win rate<div class='value'>{summary['win_rate_pct']:.2f}%</div></div><div class='card'>Retorno medio<div class='value'>{summary['avg_return_pct']:+.3f}%</div></div></div>
<p>PF {summary['profit_factor']:.2f} · MaxDD {summary['max_drawdown']:.2f} · Folds {summary['fold_count']} · Estado {html.escape(str(summary['status']))}</p>
<p>Reporte completo: {html.escape(str(summary.get('report_dir') or 'n/a'))}</p></body></html>"""
    (output_root / "walkforward_status.html").write_text(report, encoding="utf-8")
    if not discovery_mode:
        send_daily(build_group(
            "🧪 BACKTEST · WALK-FORWARD",
            [
            "\n".join([
                "### Modelo",
                f"**Clave:** `{summary['model_key']}` · **Estado:** {str(summary['model_status']).upper()}",
                f"**Validación:** {str(summary['status']).upper()} · **Folds OOS:** `{summary['fold_count']}`",
            ]),
            "\n".join([
                "### Resultados fuera de muestra",
                f"**Trades:** `{summary['alerts_closed']}` · **Hit ±5%:** `{summary['target_5pct_hit_rate']:.2f}%` · **Win rate:** `{summary['win_rate_pct']:.2f}%`",
                f"**Retorno medio:** `{summary['avg_return_pct']:+.3f}%` · **PF:** `{summary['profit_factor']:.2f}` · **MaxDD:** `{summary['max_drawdown']:.2f}`",
            ]),
            "\n".join([
                "### Comparación",
                f"**Mejora vs bootstrap:** `{summary['objective_improvement']:+.4f}` · **Reglas activas:** `{len(summary['enabled_rules'])}`",
            ]),
            ],
            footer="El reporte técnico completo queda guardado localmente.",
        ))
    return {"summary": summary, "alerts": [], "by_rule": {}, "output_dir": str(output_root)}


def build_performance_report(label: str = "initial_baseline") -> dict:
    output_root = config.OUTPUT_DIR / label
    output_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.ALERTS_DB)
    conn.row_factory = sqlite3.Row
    model = _select_model(conn)
    # A CORE champion has a valid walk-forward report even while FULL DEEP is
    # still below 30 sessions. Report that calibrated model before applying the
    # FULL-only coverage gate.
    if model:
        walkforward = _walkforward_report_from_model(model, label)
        if walkforward is not None:
            conn.close()
            return walkforward
    daily_dates = [str(row[0]) for row in conn.execute(
        """SELECT DISTINCT snapshot_date FROM feature_observations
           WHERE observation_layer='daily_close' AND is_canonical_daily_close=1
           ORDER BY snapshot_date DESC LIMIT ?""",
        (int(config.DAILY_CLOSE_WINDOW_SESSIONS),),
    ).fetchall()]
    daily_dates = sorted(daily_dates)
    target_closes = int(config.DAILY_CLOSE_WINDOW_SESSIONS)
    if len(daily_dates) < target_closes:
        conn.close()
        summary = {
            "generated_at_madrid": now_madrid().isoformat(),
            "window_start": daily_dates[0] if daily_dates else None,
            "window_end": daily_dates[-1] if daily_dates else None,
            "window_daily_closes": len(daily_dates),
            "window_target_daily_closes": target_closes,
            "missing_daily_closes": max(0, target_closes-len(daily_dates)),
            "pattern_target_pct": config.PATTERN_TARGET_PCT,
            "max_horizon_sessions": config.MAX_PATTERN_HORIZON_SESSIONS,
            "model_key": (model or {}).get("model_key", "none"),
            "model_status": (model or {}).get("status", "none"),
            "alerts_total": 0,
            "alerts_closed": 0,
            "status": "collecting_daily_closes",
            "backtest_executed": False,
            "reason": "La configuración CORE requiere 30 sesiones TMAD; FULL DEEP requiere 30 cierres propios.",
        }
        (output_root / "baseline_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        with (output_root / "baseline_alerts.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["alert_at", "ticker", "rule_name", "simulated_return_pct"])
            writer.writeheader()
        with (output_root / "baseline_by_rule.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["rule_name", "alerts", "wins", "target_5pct_hits", "target_5pct_hit_rate", "win_rate_pct", "avg_return_pct"])
            writer.writeheader()
        (output_root / "baseline_report.html").write_text(
            f"<html><body><h1>TMAD Backtest</h1><p>Estado: COLLECTING</p><p>Cierres deep: {len(daily_dates)}/{target_closes}. No se ha ejecutado la selección de configuración.</p></body></html>",
            encoding="utf-8",
        )
        return {"summary": summary, "alerts": [], "by_rule": {}, "output_dir": str(output_root)}
    if daily_dates:
        ph = ",".join(["?"] * len(daily_dates))
        all_observations = [dict(row) for row in conn.execute(
            f"""SELECT * FROM feature_observations
                WHERE observation_layer='daily_close' AND is_canonical_daily_close=1
                  AND snapshot_date IN ({ph})
                ORDER BY observed_at_madrid,id""",
            daily_dates,
        )]
    else:
        all_observations = []
    prices = defaultdict(list)
    for row in conn.execute("SELECT ticker,trade_date,open,high,low,close FROM price_cache ORDER BY ticker,trade_date"):
        prices[str(row["ticker"])].append(dict(row))
    conn.close()

    if not model:
        model = {"model_key": "none", "status": "none", "params_json": "{}"}
    params = json.loads(model.get("params_json") or "{}")

    # Daily structural patterns are calibrated from the final snapshot of each ticker/date.
    daily: dict[tuple[str, str], dict] = {}
    close_features: dict[str, dict[str, dict]] = defaultdict(dict)
    for observation in all_observations:
        if not _is_close_observation(observation):
            continue
        key = (str(observation.get("ticker") or ""), str(observation.get("snapshot_date") or ""))
        previous = daily.get(key)
        if previous is None or str(observation.get("observed_at_madrid") or "") >= str(previous.get("observed_at_madrid") or ""):
            daily[key] = observation
    observations = sorted(daily.values(), key=lambda row: (str(row.get("snapshot_date")), str(row.get("ticker"))))
    for observation in observations:
        try:
            close_features[str(observation["ticker"])][str(observation["snapshot_date"])] = json.loads(observation.get("features_json") or "{}")
        except Exception:
            pass

    rule_order = {name: idx for idx, name in enumerate(RULE_DIRECTIONS)}
    blocked_until: dict[str, str] = {}
    alerts: list[dict] = []

    for observation in observations:
        ticker = str(observation.get("ticker") or "")
        obs_date = str(observation.get("snapshot_date") or "")
        if not ticker or not obs_date or blocked_until.get(ticker, "") >= obs_date:
            continue
        try:
            features = json.loads(observation.get("features_json") or "{}")
            entry = float(observation.get("current_price"))
        except Exception:
            continue
        candidates = []
        for rule_name, direction in RULE_DIRECTIONS.items():
            rp = params_for_rule(params, rule_name)
            candidate = evaluate_rule(rule_name, features, rp, mode="daily_close")
            if candidate:
                candidates.append((float(candidate.get("score") or 0.0), -rule_order[rule_name], rule_name, direction, candidate, rp))
        if not candidates:
            continue
        _score, _order, rule_name, direction, candidate, rule_params = max(candidates, key=lambda item: (item[0], item[1]))
        hold = int(config.MAX_PATTERN_HORIZON_SESSIONS)
        target = float(config.PATTERN_TARGET_PCT)
        stop = 0.0
        future = [row for row in prices.get(ticker, []) if str(row.get("trade_date")) > obs_date][:hold]
        item = {
            "alert_at": observation.get("observed_at_madrid"),
            "alert_date": obs_date,
            "ticker": ticker,
            "direction": direction,
            "rule_name": rule_name,
            "score": candidate.get("score"),
            "entry_price": entry,
            "target_pct": target,
            "stop_pct": stop,
            "hold_sessions": hold,
            "reason": candidate.get("reason"),
            "gex_strike_change_pct": features.get("total_gex_change_pct"),
            "gex_front_change_pct": features.get("front_gex_change_pct"),
            "gex_short_change_pct": features.get("short_gex_change_pct"),
            "put_wall_pct": features.get("put_wall_pct"),
            "call_wall_pct": features.get("call_wall_pct"),
            "rvol": features.get("rvol"),
            "exit_date": None,
            "exit_reason": "OPEN/NO FUTURE DATA",
            "exit_price": None,
            "mfe_directional_pct": None,
            "mae_directional_pct": None,
            "target_5pct_hit": False,
            "simulated_return_pct": None,
            "model_key": model.get("model_key"),
            "model_status": model.get("status"),
        }
        for horizon in range(1, 6):
            item[f"underlying_move_{horizon}d_pct"] = None
            item[f"directional_{horizon}d_pct"] = None
            if len(future) >= horizon:
                move = (float(future[horizon - 1]["close"]) / entry - 1.0) * 100.0
                item[f"underlying_move_{horizon}d_pct"] = move
                item[f"directional_{horizon}d_pct"] = _directional(move, direction)

        best = 0.0
        worst = 0.0
        final_return = None
        soft_exit_streak = 0
        for index, day in enumerate(future, start=1):
            close = float(day["close"])
            high = float(day.get("high") if day.get("high") is not None else close)
            low = float(day.get("low") if day.get("low") is not None else close)
            if direction == "long":
                favorable = (high / entry - 1.0) * 100.0
                adverse = (low / entry - 1.0) * 100.0
            else:
                favorable = -(low / entry - 1.0) * 100.0
                adverse = -(high / entry - 1.0) * 100.0
            best = max(best, favorable)
            worst = min(worst, adverse)
            close_return = _directional((close / entry - 1.0) * 100.0, direction)

            # v5.6.9: no fixed price stop. Close only on target, pattern invalidation, or horizon.
            if favorable >= target:
                final_return = target
                item["target_5pct_hit"] = True
                item.update({"exit_date": day["trade_date"], "exit_reason": "TARGET_5PCT", "exit_price": entry * (1+target/100 if direction == "long" else 1-target/100)})
                break

            day_features = close_features.get(ticker, {}).get(str(day["trade_date"]))
            if day_features:
                exit_signal = exit_pattern_signal(direction, day_features, rule_params, close_return, best)
                if exit_signal:
                    if bool(exit_signal.get("hard")):
                        soft_exit_streak = int(config.SOFT_EXIT_CONFIRMATION_RUNS)
                    else:
                        soft_exit_streak += 1
                    if soft_exit_streak >= int(config.SOFT_EXIT_CONFIRMATION_RUNS):
                        final_return = close_return
                        item.update({"exit_date": day["trade_date"], "exit_reason": exit_signal["reason"], "exit_price": close})
                        break
                else:
                    soft_exit_streak = 0
            if index >= hold:
                final_return = close_return
                item.update({"exit_date": day["trade_date"], "exit_reason": "MAX_5_SESSIONS", "exit_price": close})
                break

        item["mfe_directional_pct"] = best if future else None
        item["mae_directional_pct"] = worst if future else None
        item["simulated_return_pct"] = final_return
        if item["exit_date"]:
            blocked_until[ticker] = str(item["exit_date"])
        alerts.append(item)

    closed = [a for a in alerts if a["simulated_return_pct"] is not None]
    wins = [a for a in closed if float(a["simulated_return_pct"]) > 0]
    losses = [a for a in closed if float(a["simulated_return_pct"]) < 0]
    target_hits = [a for a in closed if a["target_5pct_hit"]]
    pf = sum(float(a["simulated_return_pct"]) for a in wins) / abs(sum(float(a["simulated_return_pct"]) for a in losses)) if losses else (5.0 if wins else 0.0)
    summary = {
        "generated_at_madrid": now_madrid().isoformat(),
        "window_start": daily_dates[0] if daily_dates else None,
        "window_end": daily_dates[-1] if daily_dates else None,
        "window_daily_closes": len(daily_dates),
        "window_target_daily_closes": config.DAILY_CLOSE_WINDOW_SESSIONS,
        "pattern_target_pct": config.PATTERN_TARGET_PCT,
        "max_horizon_sessions": config.MAX_PATTERN_HORIZON_SESSIONS,
        "model_key": model.get("model_key"),
        "model_status": model.get("status"),
        "daily_close_observations": len(observations),
        "alerts_total": len(alerts),
        "alerts_closed": len(closed),
        "target_5pct_hits": len(target_hits),
        "target_5pct_hit_rate": len(target_hits) / len(closed) * 100.0 if closed else 0.0,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(closed) * 100.0 if closed else 0.0,
        "avg_return_pct": sum(float(a["simulated_return_pct"]) for a in closed) / len(closed) if closed else 0.0,
        "sum_return_pct_points": sum(float(a["simulated_return_pct"]) for a in closed),
        "profit_factor": pf,
        "status": "ready" if closed else "insufficient_history",
        "minimum_required_market_dates": config.MAX_PATTERN_HORIZON_SESSIONS + 1,
        "available_market_dates": len({str(o.get("snapshot_date")) for o in observations}),
        "note": "Patrones estructurales calibrados al cierre para buscar ±5% direccional en un máximo de cinco sesiones. En live, el trigger horario intenta anticipar el patrón antes del cierre. La salida no usa stop fijo: cierre por objetivo, invalidez/reversión del patrón o agotamiento del horizonte.",
    }

    by_rule = {}
    for rule in RULE_DIRECTIONS:
        rows = [a for a in closed if a["rule_name"] == rule]
        rule_wins = [a for a in rows if float(a["simulated_return_pct"]) > 0]
        rule_hits = [a for a in rows if a["target_5pct_hit"]]
        by_rule[rule] = {
            "alerts": len(rows),
            "wins": len(rule_wins),
            "target_5pct_hits": len(rule_hits),
            "target_5pct_hit_rate": len(rule_hits) / len(rows) * 100 if rows else 0.0,
            "win_rate_pct": len(rule_wins) / len(rows) * 100 if rows else 0.0,
            "avg_return_pct": sum(float(a["simulated_return_pct"]) for a in rows) / len(rows) if rows else 0.0,
        }

    (output_root / "baseline_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_root / "baseline_alerts.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = list(alerts[0].keys()) if alerts else ["alert_at", "ticker", "rule_name", "reason", "simulated_return_pct"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        if alerts:
            writer.writerows(alerts)
    with (output_root / "baseline_by_rule.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["rule_name", "alerts", "wins", "target_5pct_hits", "target_5pct_hit_rate", "win_rate_pct", "avg_return_pct"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for rule, metrics in by_rule.items():
            writer.writerow({"rule_name": rule, **metrics})

    rows_html = "".join(
        f"<tr><td>{html.escape(rule)}</td><td>{m['alerts']}</td><td>{m['target_5pct_hit_rate']:.2f}%</td><td>{m['win_rate_pct']:.2f}%</td><td>{m['avg_return_pct']:+.3f}%</td></tr>"
        for rule, m in by_rule.items()
    )
    report = f"""<!doctype html><html><head><meta charset='utf-8'><title>TMAD Backtest v5.6</title>
<style>body{{font-family:Segoe UI,Arial;margin:32px;color:#152238}}h1{{color:#17365d}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{border:1px solid #ccd6e0;padding:16px;border-radius:8px}}.value{{font-size:26px;font-weight:700}}table{{border-collapse:collapse;width:100%;margin-top:24px}}th,td{{border:1px solid #ccd6e0;padding:8px;text-align:left}}th{{background:#1f4e78;color:white}}.warn{{background:#fff2cc;padding:14px;border-radius:8px;margin:20px 0}}</style></head><body>
<h1>TMAD — baseline diario + trigger intradía</h1><p>Generado: {html.escape(summary['generated_at_madrid'])}</p>
<div class='warn'>{html.escape(summary['note'])}<br>Estado: <b>{summary['status']}</b></div>
<div class='grid'><div class='card'>Alertas cerradas<div class='value'>{summary['alerts_closed']}</div></div><div class='card'>Objetivo +5%<div class='value'>{summary['target_5pct_hit_rate']:.2f}%</div></div><div class='card'>Win rate<div class='value'>{summary['win_rate_pct']:.2f}%</div></div><div class='card'>Retorno medio<div class='value'>{summary['avg_return_pct']:+.3f}%</div></div></div>
<h2>Modelo</h2><p><code>{html.escape(str(summary['model_key']))}</code> — {html.escape(str(summary['model_status']))}</p>
<h2>Resultado por regla</h2><table><tr><th>Regla</th><th>Alertas</th><th>Hit +5%</th><th>Win rate</th><th>Retorno medio</th></tr>{rows_html}</table>
<p>Ficheros asociados: baseline_alerts.csv, baseline_by_rule.csv y baseline_summary.json.</p></body></html>"""
    (output_root / "baseline_report.html").write_text(report, encoding="utf-8")

    if summary["alerts_closed"]:
        result_lines = (
            f"Alertas cerradas `{summary['alerts_closed']}` | Hit +5% **{summary['target_5pct_hit_rate']:.2f}%**\n"
            f"Win rate **{summary['win_rate_pct']:.2f}%** | Retorno medio **{summary['avg_return_pct']:+.3f}%** | PF **{summary['profit_factor']:.2f}**"
        )
    else:
        result_lines = (
            "Resultados de performance: **N/A todavía**\n"
            f"Cierres diarios disponibles `{summary['available_market_dates']}` | "
            f"mínimo técnico `{summary['minimum_required_market_dates']}` para observar cinco sesiones posteriores.\n"
            f"Replay generado `{summary['alerts_total']}` setups | cerrados `0`: faltan outcomes completos para medir performance."
        )
    send_daily(build_group(
        "🧪 BACKTEST · ESTADO",
        [
            "\n".join([
                "### Modelo y objetivo",
                f"**Modelo:** `{summary['model_key']}` · **Estado:** {summary['model_status']}",
                f"**Objetivo:** `+{summary['pattern_target_pct']:.0f}%` · **Horizonte máximo:** `{summary['max_horizon_sessions']} sesiones`",
            ]),
            "\n".join([
                "### Resultado",
                result_lines,
                f"**Validación:** {summary['status']}",
            ]),
        ],
        footer="Los archivos técnicos del backtest quedan guardados localmente.",
    ))
    return {"summary": summary, "alerts": alerts, "by_rule": by_rule, "output_dir": str(output_root)}
