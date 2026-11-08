from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import config
from db import get_open_trades, get_pending_exit_deliveries, get_runtime_state, set_runtime_state, mark_exit_sent, update_open_trade
from discord_utils import log_info, send_alert, send_shadow, send_research, send_preliminary_alert
from rules import (
    build_features,
    exit_pattern_signal,
    format_exit_message,
    format_position_update_message,
    params_for_rule,
    position_update_assessment,
    structural_runner_exit_signal,
)
from time_utils import now_madrid, ts_madrid_str
from discord_presentation import build_group
from market_regime import broad_signal_local_exit_gate


def _directional_return(price: float, entry: float, direction: str) -> float:
    raw = (price / entry - 1.0) * 100.0
    return raw if direction == "long" else -raw


def _minutes_since(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return (now_madrid() - datetime.fromisoformat(str(value))).total_seconds() / 60.0
    except Exception:
        return None


def _is_shadow_trade(trade: dict) -> bool:
    return str(trade.get("alert_status") or "").lower().startswith("shadow")


def _shadow_delivery_allowed() -> bool:
    # Pipeline/reconciliation is authoritative. OFF/ERROR may keep tracking the
    # trade locally, but must not publish preliminary UPDATE/CLOSE messages.
    return str(get_runtime_state("entry_mode", "off") or "off").lower() == "shadow"


def _send_update(trade: dict, message: str) -> bool:
    if _is_shadow_trade(trade):
        if not _shadow_delivery_allowed():
            return False
        return bool(send_shadow(message))
    if not int(trade.get("sent_to_discord") or 0):
        return False
    if not config.SEND_POSITION_UPDATES_TO_DISCORD:
        return False
    return bool(send_alert(message))




def _delivery_target(trade: dict, *, is_update: bool) -> str | None:
    if _is_shadow_trade(trade):
        if not _shadow_delivery_allowed():
            return None
        return "shadow"
    if not int(trade.get("sent_to_discord") or 0):
        return None
    if is_update and not config.SEND_POSITION_UPDATES_TO_DISCORD:
        return None
    return "live"


def _deliver_group(target: str, message: str) -> bool:
    if target == "live":
        return bool(send_alert(message))
    if target == "preliminary":
        return bool(send_preliminary_alert(message))
    if target == "research":
        return bool(send_research(message))
    if target == "shadow":
        return bool(send_shadow(message))
    return False


def _update_allowed(trade: dict, current_date: str, kind: str, current_return: float) -> tuple[bool, int]:
    last_date = str(trade.get("last_update_date") or "")
    sent_today = int(trade.get("updates_sent_today") or 0) if last_date == current_date else 0
    if sent_today >= max(1, config.POSITION_UPDATE_MAX_PER_DAY):
        return False, sent_today
    elapsed = _minutes_since(trade.get("last_update_at_madrid"))
    if elapsed is not None and elapsed < config.POSITION_UPDATE_COOLDOWN_MINUTES:
        return False, sent_today
    last_kind = str(trade.get("last_update_kind") or "")
    last_ret = trade.get("last_update_return_pct")
    try:
        last_ret_value = float(last_ret) if last_ret is not None else None
    except Exception:
        last_ret_value = None
    if kind == "favorable" and last_kind == "favorable" and last_ret_value is not None:
        if current_return < last_ret_value + config.POSITION_UPDATE_MIN_STEP_PCT:
            return False, sent_today
    return True, sent_today


def retry_pending_exit_deliveries(limit: int = 20) -> int:
    """Retry recent CLOSE cards without reopening or mutating trading decisions."""
    try:
        pending = list(get_pending_exit_deliveries(limit=limit))
    except Exception as exc:
        log_info(f"⚠️ Lifecycle CLOSE retry · lectura pendiente falló: {exc}", send_discord=False)
        return 0
    grouped: dict[str, list[dict]] = {"live": [], "shadow": []}
    for trade in pending:
        target = _delivery_target(trade, is_update=False)
        if target not in grouped:
            continue
        try:
            grouped[target].append({
                "trade_id": int(trade["id"]),
                "return_pct": float(trade.get("exit_return_pct") or 0.0),
                "card": format_exit_message(trade, as_card=True),
            })
        except Exception as exc:
            log_info(
                f"⚠️ Lifecycle CLOSE retry · trade_id={trade.get('id')} no serializable: {exc}",
                send_discord=False,
            )
    delivered = 0
    for target in ("live", "shadow"):
        items = grouped[target]
        if not items:
            continue
        items.sort(key=lambda item: (float(item.get("return_pct") or 0.0), int(item.get("trade_id") or 0)), reverse=True)
        shadow = target == "shadow"
        message = build_group(
            "CLOSE · SHADOW" if shadow else "CLOSE · LIVE",
            [item["card"] for item in items],
            context=f"`{len(items)}` cierres recuperados · entrega pendiente",
            footer=("⚠️ SHADOW: aprendizaje; no equivale a operativa LIVE." if shadow else "Lifecycle operativo validado."),
        )
        if not _deliver_group(target, message):
            continue
        for item in items:
            mark_exit_sent(int(item["trade_id"]))
            delivered += 1
    if delivered:
        log_info(f"✅ Lifecycle CLOSE retry · entregados={delivered}", send_discord=False)
    return delivered


def update_trade_lifecycle(
    snapshots: list[dict],
    gex_map: dict[str, dict] | None = None,
    expiry_map: dict[str, dict] | None = None,
    expirations_map: dict[str, list[dict]] | None = None,
    model: dict | None = None,
    features_by_ticker: dict[str, dict] | None = None,
) -> int:
    """Evaluate every open position on every fresh run, independently of entry gates.

    v6.1.23 separates statistical success, calibrated broad warnings and execution:
    - reaching directional ±5% marks the setup as won and starts OPEN_RUNNER;
    - touching the original GEX target produces an update, never an automatic close;
    - broad FULL CLOSE LONGS/SHORTS warnings never close an individual trade;
    - runners close only after their own CORE/GEX/ticker structure confirms exhaustion/reversal;
    - the original discovery horizon closes only setups that never reached ±5%.
    """
    retry_pending_exit_deliveries()
    by_ticker = {str(row.get("ticker")): row for row in snapshots if row.get("ticker")}
    gex_map = gex_map or {}
    expiry_map = expiry_map or {}
    expirations_map = expirations_map or {}
    model_params = (model or {}).get("params") or {}
    features_by_ticker = features_by_ticker or {}
    closed = 0
    updates = 0
    pending_updates: list[dict] = []
    pending_exits: list[dict] = []
    open_trades = list(get_open_trades())

    for trade in open_trades:
        ticker = str(trade.get("ticker") or "")
        snapshot = by_ticker.get(ticker)
        if not snapshot:
            continue
        try:
            price = float(snapshot.get("current_price"))
            entry = float(trade.get("entry_price"))
        except Exception:
            continue
        if price <= 0 or entry <= 0:
            continue

        current_date = str(snapshot.get("snapshot_date") or "")
        last_seen_date = str(trade.get("last_seen_date") or "")
        sessions_seen = int(trade.get("sessions_seen") or 0)
        if current_date and current_date != last_seen_date and current_date > str(trade.get("entry_date") or ""):
            sessions_seen += 1
            last_seen_date = current_date

        direction = str(trade.get("direction") or "long")
        ret = _directional_return(price, entry, direction)
        previous_best = float(trade.get("best_return_pct") or 0.0)
        best = max(previous_best, ret)
        worst = min(float(trade.get("worst_return_pct") or 0.0), ret)
        reason: str | None = None
        reason_detail: str | None = None
        update_signal: dict | None = None
        deterioration_streak = int(trade.get("deterioration_streak") or 0)
        favorable_streak = int(trade.get("favorable_streak") or 0)
        existing_hit = bool(int(trade.get("setup_hit") or 0))
        setup_hit_now = bool(not existing_hit and ret >= float(config.SETUP_HIT_THRESHOLD_PCT))
        setup_hit = bool(existing_hit or setup_hit_now)
        lifecycle_state = "OPEN_RUNNER" if setup_hit else "OPEN"

        gex = gex_map.get(ticker)
        if ticker in features_by_ticker:
            features = dict(features_by_ticker[ticker])
        elif gex and str(snapshot.get("gex_status") or "").lower() == "ok":
            features = build_features(snapshot, gex, expiry_map.get(ticker), expirations_map.get(ticker))
        else:
            features = {}
        if features:
            features.setdefault("source_scanner_run_id", snapshot.get("scanner_run_id"))
            features.setdefault("source_gex_run_id", (gex or {}).get("gex_run_id"))

        try:
            entry_features = json.loads(trade.get("entry_features_json") or "{}")
        except Exception:
            entry_features = {}
        if features and entry_features:
            # Make point-in-time entry GEX available to every downstream exit gate.
            # This allows true entry→current reversal detection without rewriting
            # the historical entry snapshot.
            features["_entry_features"] = entry_features
        target_source = str(entry_features.get("target_source") or "")
        try:
            target_low = float(entry_features.get("target_zone_low"))
            target_high = float(entry_features.get("target_zone_high"))
        except Exception:
            target_low = target_high = None
        target_touched = bool(
            target_source.startswith("GEX")
            and target_low is not None and target_high is not None
            and ((direction == "long" and price >= min(target_low, target_high))
                 or (direction == "short" and price <= max(target_low, target_high)))
        )
        prior_target_count = int(trade.get("target_touch_count") or 0)
        target_touch_now = bool(target_touched and prior_target_count == 0)
        target_touch_count = prior_target_count + (1 if target_touched else 0)

        # The normal pattern engine and runner-specific CORE/GEX exhaustion engine
        # are both structural. Neither closes merely because price reached ±5% or
        # touched a target zone.
        exit_signal = None
        if features:
            rule_params = params_for_rule(model_params, str(trade.get("rule_name") or ""))
            exit_signal = exit_pattern_signal(direction, features, rule_params, ret, best)
            runner_signal = structural_runner_exit_signal(
                direction, entry_features, features,
                target_touched=bool(target_touched or prior_target_count > 0),
            ) if setup_hit else None
            if runner_signal and (not exit_signal or float(runner_signal.get("score") or 0) >= float(exit_signal.get("score") or 0)):
                exit_signal = runner_signal

        if exit_signal:
            exit_reason = str(exit_signal.get("reason") or "PATTERN_REVERSAL")
            exit_detail = str(exit_signal.get("detail") or "")
            confirmation_runs = (
                int(config.GEX_EXHAUSTION_CONFIRMATION_RUNS)
                if exit_reason in {"GEX_EXHAUSTION", "CORE_REVERSAL"}
                else int(config.SOFT_EXIT_CONFIRMATION_RUNS)
            )
            confirmation_runs = max(1, confirmation_runs)
            if bool(exit_signal.get("hard")):
                reason = exit_reason
                reason_detail = exit_detail
                deterioration_streak = max(deterioration_streak + 1, confirmation_runs)
            else:
                deterioration_streak += 1
                if deterioration_streak >= confirmation_runs:
                    reason = exit_reason
                    reason_detail = f"confirmado {deterioration_streak}/{confirmation_runs} runs; {exit_detail}".strip("; ")
                else:
                    update_signal = {
                        "kind": "deterioration",
                        "detail": f"{exit_reason} {deterioration_streak}/{confirmation_runs} runs; {exit_detail}".strip("; "),
                    }
        elif features:
            update_signal = position_update_assessment(direction, features, ret)
            if update_signal and update_signal.get("kind") == "deterioration":
                deterioration_streak += 1
                confirmation_runs = max(1, int(config.SOFT_EXIT_CONFIRMATION_RUNS))
                detail = str(update_signal.get("detail") or "")
                local_exit_gate = broad_signal_local_exit_gate(direction, features)
                close_blocked = bool(
                    local_exit_gate.get("broad_active")
                    and local_exit_gate.get("local_gex_required")
                    and not local_exit_gate.get("exit_allowed")
                )
                if close_blocked:
                    # Keep a warning streak, but never let scanner/price/sector alone
                    # cross the CLOSE threshold while ticker GEX remains unconfirmed.
                    deterioration_streak = min(deterioration_streak, max(0, confirmation_runs - 1))
                    local_state = (local_exit_gate.get("local_gex_state") or {}).get("state") or "NO_GEX"
                    update_signal = {
                        "kind": "deterioration",
                        "detail": (
                            f"vigilancia {deterioration_streak}/{confirmation_runs} runs; {detail}; "
                            f"cierre bloqueado: GEX ticker {local_state} aún no confirma"
                        ).strip("; "),
                        "local_gex_state": local_state,
                        "close_blocked_by_local_gex": True,
                    }
                elif deterioration_streak >= confirmation_runs:
                    reason = "PATTERN_REVERSAL"
                    reason_detail = f"deterioro confirmado {deterioration_streak}/{confirmation_runs} runs; {detail}".strip("; ")
                    update_signal = None
                else:
                    update_signal = {"kind": "deterioration", "detail": f"deterioro {deterioration_streak}/{confirmation_runs} runs; {detail}".strip("; ")}
            else:
                deterioration_streak = 0

        max_hold = min(int(trade.get("max_hold_sessions") or config.MAX_PATTERN_HORIZON_SESSIONS), int(config.MAX_PATTERN_HORIZON_SESSIONS))
        if not reason and not setup_hit and sessions_seen >= max_hold:
            reason = "SETUP_HORIZON_EXPIRED"
            reason_detail = f"no alcanzó ±{float(config.SETUP_HIT_THRESHOLD_PCT):.1f}% en {sessions_seen} sesiones"

        # High-priority milestone messages bypass normal update cooldowns.
        milestone_kind: str | None = None
        milestone_detail = ""
        setup_hit_pending_delivery = bool(setup_hit and not int(trade.get("setup_hit_sent") or 0))
        target_touch_pending_delivery = bool((target_touched or prior_target_count > 0) and not int(trade.get("target_touch_sent") or 0))
        if not reason and setup_hit_pending_delivery and bool(config.SETUP_HIT_MESSAGE_ENABLED):
            milestone_kind = "setup_hit"
            milestone_detail = f"hit estadístico {ret:+.1f}% alcanzado"
            if target_touched or prior_target_count > 0:
                milestone_detail += "; zona objetivo GEX original también alcanzada"
        elif not reason and target_touch_pending_delivery and bool(config.TARGET_TOUCH_MESSAGE_ENABLED):
            milestone_kind = "target_touch"
            milestone_detail = "zona objetivo GEX original alcanzada; la posición continúa hasta salida estructural"
        elif not reason and setup_hit and best >= previous_best + float(config.RUNNER_UPDATE_MIN_STEP_PCT):
            milestone_kind = "runner"
            milestone_detail = f"nuevo MFE {best:+.1f}%"

        if update_signal and update_signal.get("kind") == "favorable":
            favorable_streak += 1
            if setup_hit:
                update_signal = {"kind": "runner", "detail": str(update_signal.get("detail") or "")}
        elif update_signal and update_signal.get("kind") == "deterioration":
            favorable_streak = 0
        elif milestone_kind in {"setup_hit", "target_touch", "runner"}:
            favorable_streak += 1
        elif not reason:
            favorable_streak = 0

        fields: dict[str, Any] = {
            "last_price": price,
            "best_return_pct": round(best, 6),
            "worst_return_pct": round(worst, 6),
            "sessions_seen": sessions_seen,
            "last_seen_date": last_seen_date,
            "deterioration_streak": deterioration_streak,
            "favorable_streak": favorable_streak,
            "lifecycle_state": lifecycle_state,
            "target_touch_count": target_touch_count,
            "last_features_json": json.dumps(features, ensure_ascii=False) if features else trade.get("last_features_json"),
        }
        if setup_hit_now:
            fields.update({
                "setup_hit": 1,
                "setup_hit_at_madrid": ts_madrid_str(),
                "setup_hit_date": current_date,
                "setup_hit_price": price,
                "setup_hit_return_pct": round(ret, 6),
                "setup_hit_sessions": sessions_seen,
                "lifecycle_state": "OPEN_RUNNER",
            })
        if target_touch_now:
            fields.update({"target_touch_at_madrid": ts_madrid_str(), "target_touch_price": price})

        if reason:
            full_reason = reason if not reason_detail else f"{reason}: {reason_detail}"
            giveback = max(0.0, best - ret)
            capture = (ret / best * 100.0) if best > 0 else None
            fields.update({
                "status": "closed",
                "lifecycle_state": f"CLOSED_{reason}",
                "exit_at_madrid": ts_madrid_str(),
                "exit_date": current_date,
                "exit_price": price,
                "exit_return_pct": round(ret, 6),
                "exit_reason": full_reason,
                "exit_giveback_pct": round(giveback, 6),
                "exit_capture_ratio_pct": round(capture, 6) if capture is not None else None,
            })
        else:
            event_kind = milestone_kind or (str(update_signal.get("kind") or "") if update_signal else "")
            event_detail = milestone_detail or (str(update_signal.get("detail") or "") if update_signal else "")
            if event_kind and features:
                bypass = event_kind in {"setup_hit", "target_touch"}
                allowed, sent_today = (True, int(trade.get("updates_sent_today") or 0)) if bypass else _update_allowed(trade, current_date, event_kind, ret)
                if allowed:
                    target = _delivery_target(trade, is_update=True)
                    if target:
                        card_trade = {**trade, **fields}
                        delivery_fields = {
                            "last_update_at_madrid": ts_madrid_str(),
                            "last_update_return_pct": round(ret, 6),
                            "last_update_kind": event_kind,
                            "last_update_date": current_date,
                            "updates_sent_today": sent_today + 1,
                        }
                        if event_kind == "setup_hit":
                            delivery_fields["setup_hit_sent"] = 1
                            if target_touched:
                                delivery_fields["target_touch_sent"] = 1
                        if event_kind == "target_touch":
                            delivery_fields["target_touch_sent"] = 1
                        pending_updates.append({
                            "trade_id": int(trade["id"]),
                            "target": target,
                            "kind": event_kind,
                            "return_pct": float(ret),
                            "card": format_position_update_message(card_trade, features, event_kind, event_detail, as_card=True),
                            "delivery_fields": delivery_fields,
                        })
                elif str(trade.get("last_update_date") or "") != current_date:
                    fields.update({"last_update_date": current_date, "updates_sent_today": 0})

        update_open_trade(int(trade["id"]), **fields)
        if reason:
            closed += 1
            trade_for_exit = {**trade, **fields}
            target = _delivery_target(trade_for_exit, is_update=False)
            if target:
                pending_exits.append({
                    "trade_id": int(trade["id"]),
                    "target": target,
                    "reason": str(reason),
                    "return_pct": float(ret),
                    "card": format_exit_message(trade_for_exit, as_card=True),
                })

    # Broad FULL CLOSE signals are published independently by alerts_engine.
    # This lifecycle only closes a position when that ticker's own structural
    # engine confirms loss of advantage.

    # Stable operational order in Alerts Live: exits, statistical milestones,
    # deterioration, then continuing runners. Research remains watchlist-only.
    group_specs = [
        ("gex_exit", "CLOSE · AGOTAMIENTO GEX", lambda item: item in pending_exits and str(item.get("reason") or "") == "GEX_EXHAUSTION", False),
        ("core_exit", "CLOSE · CAMBIO CORE", lambda item: item in pending_exits and str(item.get("reason") or "") == "CORE_REVERSAL", False),
        ("invalid_exit", "CLOSE · ESTRUCTURA INVALIDADA", lambda item: item in pending_exits and str(item.get("reason") or "") in {"PATTERN_REVERSAL", "TRAILING_PATTERN"}, False),
        ("horizon_exit", "CLOSE · HORIZONTE AGOTADO", lambda item: item in pending_exits and str(item.get("reason") or "") == "SETUP_HORIZON_EXPIRED", False),
        ("setup_hit", "SETUP GANADO · RUNNER ACTIVO", lambda item: item in pending_updates and item.get("kind") == "setup_hit", True),
        ("target_touch", "OBJETIVO GEX ALCANZADO · RUNNER ACTIVO", lambda item: item in pending_updates and item.get("kind") == "target_touch", True),
        ("deterioration", "UPDATE DETERIORO", lambda item: item in pending_updates and item.get("kind") == "deterioration", False),
        ("runner", "UPDATE RUNNER", lambda item: item in pending_updates and item.get("kind") == "runner", True),
        ("favorable", "UPDATE POSITION", lambda item: item in pending_updates and item.get("kind") == "favorable", True),
    ]
    all_pending = pending_exits + pending_updates
    for target in ("live", "shadow", "research", "preliminary"):
        for _group_key, title, predicate, reverse in group_specs:
            items = [item for item in all_pending if item.get("target") == target and predicate(item)]
            if not items:
                continue
            items.sort(key=lambda item: (float(item.get("return_pct") or 0.0), int(item.get("trade_id") or 0)), reverse=reverse)
            shadow = target in {"shadow", "research", "preliminary"}
            message = build_group(
                title + (" · SHADOW" if shadow else " · LIVE"),
                [item["card"] for item in items],
                context=f"`{len(items)}` posiciones · ordenadas por impacto" + (" · seguimiento no operativo" if shadow else ""),
                footer=("⚠️ SHADOW: aprendizaje; no equivale a operativa LIVE." if shadow else "Lifecycle operativo validado."),
            )
            if not _deliver_group(target, message):
                continue
            for item in items:
                if item in pending_exits:
                    mark_exit_sent(int(item["trade_id"]))
                else:
                    update_open_trade(int(item["trade_id"]), **dict(item.get("delivery_fields") or {}))
                    updates += 1

    if closed or updates:
        log_info(f"Lifecycle procesado · cierres `{closed}` · updates entregados `{updates}`", send_discord=False)
    return closed

