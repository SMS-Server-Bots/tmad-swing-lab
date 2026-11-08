from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from statistics import mean
from typing import Any

import config
from layer_archive import archived_feature_rows_with_outcomes, archived_joined_signal_outcomes
from time_utils import ts_madrid_str, ts_utc_str
from calibration_v621 import register_layer_model

LAYERS = ("CORE", "GRAPH", "GEX_DAILY", "GEX_INTRADAY", "FUSION")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ALERTS_DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA busy_timeout=60000")
    return conn


def ensure_layer_calibration_schema() -> None:
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS layer_model_calibration (
            layer TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            sample INTEGER NOT NULL DEFAULT 0,
            complete_dates INTEGER NOT NULL DEFAULT 0,
            movement_5pct_hit_rate REAL,
            target_zone_hit_rate REAL,
            avg_first_5pct_session REAL,
            avg_first_target_session REAL,
            avg_max_favorable_pct REAL,
            avg_max_adverse_pct REAL,
            target_mae_pct REAL,
            enter_sample INTEGER,
            wait_sample INTEGER,
            enter_target_hit_rate REAL,
            wait_target_hit_rate REAL,
            timing_uplift REAL,
            metrics_json TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            updated_at_madrid TEXT NOT NULL
        );
        """
    )
    conn.commit(); conn.close()


def _avg(values: list[float | int | None]) -> float | None:
    clean=[float(v) for v in values if v is not None]
    return round(mean(clean),6) if clean else None


def _rate(values: list[int | float | None]) -> float | None:
    clean=[int(v) for v in values if v is not None]
    return round(sum(clean)/len(clean)*100.0,4) if clean else None


def _directional_close_path(row: dict) -> list[float]:
    """Directional close-to-close returns available after the point-in-time signal."""
    try:
        sessions = json.loads(row.get("sessions_json") or "[]")
    except Exception:
        sessions = []
    try:
        entry = float(row.get("entry_price") or 0.0)
    except Exception:
        entry = 0.0
    if entry <= 0:
        return []
    direction = str(row.get("direction") or "").lower()
    output: list[float] = []
    for item in sessions:
        try:
            close = float((item or {}).get("close") or 0.0)
        except Exception:
            close = 0.0
        if close <= 0:
            continue
        value = (close / entry - 1.0) * 100.0 if direction == "long" else (entry / close - 1.0) * 100.0
        output.append(value)
    return output


def _calibrate_gex_close(rows: list[dict]) -> dict[str, float | int | None]:
    """Choose the best OOS close horizon for historical GEX signals.

    This does not turn a horizon into a mandatory close. It provides the
    calibrated duration prior used by GEX; live lifecycle still requires the
    ticker's own GEX/CORE reversal or hard invalidation.
    """
    paths = [(row, _directional_close_path(row)) for row in rows]
    paths = [(row, path) for row, path in paths if path]
    if not paths:
        return {
            "close_sample": 0, "calibrated_close_session": None,
            "calibrated_close_avg_return_pct": None, "calibrated_close_win_rate": None,
            "calibrated_close_capture_pct": None, "calibrated_close_giveback_pct": None,
            "close_duration_mae_sessions": None, "avg_peak_close_session": None,
        }
    max_session = max(len(path) for _, path in paths)
    candidates: list[dict] = []
    peak_sessions: list[int] = []
    for _row, path in paths:
        peak_sessions.append(max(range(len(path)), key=lambda i: path[i]) + 1)
    for session in range(1, max_session + 1):
        returns: list[float] = []
        captures: list[float] = []
        givebacks: list[float] = []
        duration_errors: list[float] = []
        for row, path in paths:
            if len(path) < session:
                continue
            close_ret = float(path[session - 1])
            max_fav = float(row.get("max_favorable_pct") or max(path) or 0.0)
            peak_session = max(range(len(path)), key=lambda i: path[i]) + 1
            returns.append(close_ret)
            duration_errors.append(abs(session - peak_session))
            givebacks.append(max(0.0, max_fav - close_ret))
            if max_fav > 0:
                captures.append(max(-100.0, min(100.0, close_ret / max_fav * 100.0)))
        if not returns:
            continue
        avg_return = mean(returns)
        win_rate = sum(value > 0 for value in returns) / len(returns) * 100.0
        capture = mean(captures) if captures else 0.0
        giveback = mean(givebacks) if givebacks else 0.0
        duration_mae = mean(duration_errors) if duration_errors else 0.0
        # Result-first selection with explicit penalties for giving back the move
        # and closing far from the observed favorable peak.
        objective = avg_return * 4.0 + win_rate * 0.20 + capture * 0.10 - giveback * 1.5 - duration_mae * 2.0
        candidates.append({
            "session": session, "sample": len(returns), "avg_return": avg_return,
            "win_rate": win_rate, "capture": capture, "giveback": giveback,
            "duration_mae": duration_mae, "objective": objective,
        })
    if not candidates:
        return {"close_sample": len(paths)}
    best = max(candidates, key=lambda item: (item["objective"], item["sample"], -item["session"]))
    return {
        "close_sample": int(best["sample"]),
        "calibrated_close_session": int(best["session"]),
        "calibrated_close_avg_return_pct": round(float(best["avg_return"]), 6),
        "calibrated_close_win_rate": round(float(best["win_rate"]), 4),
        "calibrated_close_capture_pct": round(float(best["capture"]), 4),
        "calibrated_close_giveback_pct": round(float(best["giveback"]), 6),
        "close_duration_mae_sessions": round(float(best["duration_mae"]), 6),
        "avg_peak_close_session": round(mean(peak_sessions), 6) if peak_sessions else None,
        "close_objective": round(float(best["objective"]), 6),
    }


def _status(layer: str, metrics: dict) -> str:
    sample=int(metrics.get("sample") or 0); dates=int(metrics.get("complete_dates") or 0); min_dates=int(config.LAYER_VALIDATION_MIN_DATES)
    if sample<=0: return "ACCUMULATING"
    if layer=="GEX_DAILY":
        if sample<int(config.GEX_DAILY_VALIDATION_MIN_SIGNALS) or dates<min_dates: return "CHALLENGER"
        hit=float(metrics.get("target_zone_hit_rate") or 0); mae=float(metrics.get("target_mae_pct") or 999)
        return "VALIDATED" if hit>=float(config.GEX_DAILY_MIN_TARGET_HIT_RATE) and mae<=float(config.GEX_DAILY_MAX_TARGET_MAE_PCT) else "CHALLENGER"
    if layer=="GEX_INTRADAY":
        if int(metrics.get("enter_sample") or 0)<int(config.GEX_INTRADAY_VALIDATION_MIN_ENTER_SIGNALS) or dates<min_dates: return "CHALLENGER"
        return "VALIDATED" if float(metrics.get("timing_uplift") or 0)>=float(config.GEX_INTRADAY_MIN_UPLIFT_PP) else "CHALLENGER"
    if layer=="GRAPH":
        if sample<int(config.GRAPH_VALIDATION_MIN_SIGNALS) or dates<min_dates: return "CHALLENGER"
        return "VALIDATED" if float(metrics.get("movement_5pct_hit_rate") or 0)>=float(config.GRAPH_MIN_5PCT_HIT_RATE) else "CHALLENGER"
    if layer=="FUSION":
        if sample<int(config.FUSION_VALIDATION_MIN_SIGNALS) or dates<min_dates: return "CHALLENGER"
        return "VALIDATED" if float(metrics.get("target_zone_hit_rate") or 0)>=float(config.FUSION_MIN_TARGET_HIT_RATE) else "CHALLENGER"
    return "MEASURED" if dates>=min_dates else "ACCUMULATING"


def _payload(row: dict) -> dict:
    try: return json.loads(row.get("payload_json") or "{}")
    except Exception: return {}


def _breakdown(rows: list[dict], field: str, outcome_field: str) -> dict[str, dict]:
    groups: dict[str,list[dict]]=defaultdict(list)
    for row in rows:
        value=str((_payload(row).get(field) if field not in row else row.get(field)) or "UNKNOWN")
        groups[value].append(row)
    return {
        key:{"sample":len(items),"hit_rate":_rate([r.get(outcome_field) for r in items]),"avg_mfe":_avg([r.get("max_favorable_pct") for r in items])}
        for key,items in groups.items()
    }


def _dedupe(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """Prefer live/newest copies when a revised daily ZIP overlaps SQLite live."""
    output: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row.get(name) for name in keys)
        if any(value is None for value in key):
            key = ("fallback", row.get("snapshot_date"), row.get("ticker"), row.get("layer"), row.get("signal_key"), len(output))
        output[key] = row
    return list(output.values())


def _feature_quality() -> dict:
    conn=_connect()
    live=[dict(r) for r in conn.execute(
        """SELECT f.*,o.long_5pct_hit,o.short_5pct_hit,o.first_long_5pct_session,o.first_short_5pct_session,
                  o.max_up_pct,o.max_down_pct,o.close_return_pct
           FROM layer_feature_history f JOIN layer_feature_outcomes o ON o.layer_feature_id=f.id WHERE o.complete=1"""
    ).fetchall()]
    conn.close()
    rows=_dedupe(archived_feature_rows_with_outcomes()+live, ("id", "snapshot_date", "ticker", "run_key"))
    if not rows: return {}
    core_rows=[r for r in rows if str(r.get("core_direction") or "") in {"long","short"}]
    for r in core_rows:
        r["direction_hit"] = r.get("long_5pct_hit") if str(r.get("core_direction"))=="long" else r.get("short_5pct_hit")
    bins: dict[str,list[dict]]=defaultdict(list)
    for r in core_rows:
        score=float(r.get("core_score") or 0); low=int(score//10)*10; bins[f"{low:02d}-{low+9:02d}"].append(r)
    core_bins={k:{"sample":len(v),"hit_rate":_rate([r.get("direction_hit") for r in v]),"avg_up":_avg([r.get("max_up_pct") for r in v]),"avg_down":_avg([r.get("max_down_pct") for r in v])} for k,v in sorted(bins.items())}
    gex_intents: dict[str,list[dict]]=defaultdict(list)
    for r in rows:
        if r.get("gex_direction"):
            hit=r.get("long_5pct_hit") if str(r.get("gex_direction"))=="long" else r.get("short_5pct_hit")
            r=dict(r); r["direction_hit"]=hit; gex_intents[str(r.get("gex_intent") or "UNKNOWN")].append(r)
    intent_metrics={k:{"sample":len(v),"movement_hit_rate":_rate([r.get("direction_hit") for r in v]),"avg_up":_avg([r.get("max_up_pct") for r in v]),"avg_down":_avg([r.get("max_down_pct") for r in v])} for k,v in gex_intents.items()}
    states: dict[str,list[dict]]=defaultdict(list)
    for r in rows:
        if r.get("fusion_state"):
            direction=str(r.get("gex_direction") or r.get("core_direction") or "")
            hit=r.get("long_5pct_hit") if direction=="long" else r.get("short_5pct_hit") if direction=="short" else None
            x=dict(r); x["direction_hit"]=hit; states[str(r.get("fusion_state"))].append(x)
    state_metrics={k:{"sample":len(v),"movement_hit_rate":_rate([r.get("direction_hit") for r in v]),"avg_close":_avg([r.get("close_return_pct") for r in v])} for k,v in states.items()}
    return {"sample":len(rows),"dates":len({r.get('snapshot_date') for r in rows}),"core_rank_bins":core_bins,"gex_intent":intent_metrics,"execution_states":state_metrics}


def refresh_layer_calibrations() -> dict[str,dict]:
    ensure_layer_calibration_schema(); conn=_connect()
    live=[dict(r) for r in conn.execute(
        """SELECT s.layer,s.snapshot_date,s.payload_json,o.* FROM layer_signal_history s
           JOIN layer_signal_outcomes o ON o.layer_signal_id=s.id WHERE o.complete=1 ORDER BY s.snapshot_date,s.id"""
    ).fetchall()]
    rows=_dedupe(archived_joined_signal_outcomes()+live, ("layer_signal_id", "snapshot_date", "layer"))
    grouped: dict[str,list[dict]]=defaultdict(list)
    for row in rows:
        layer=str(row.get("layer") or "").upper()
        if layer in LAYERS:
            row["payload"]=_payload(row); grouped[layer].append(row)
    feature_quality=_feature_quality(); result={}; now_utc=ts_utc_str(); now_mad=ts_madrid_str()
    for layer in LAYERS:
        layer_rows=grouped.get(layer,[]); target_rows=[r for r in layer_rows if (r.get("payload") or {}).get("target_source")]
        metrics: dict[str,Any]={
            "layer":layer,"sample":len(layer_rows),
            "complete_dates":len({str(r.get("snapshot_date") or r.get("signal_date") or "") for r in layer_rows if r.get("snapshot_date") or r.get("signal_date")}),
            "movement_5pct_hit_rate":_rate([r.get("movement_5pct_hit") for r in layer_rows]),
            "target_zone_hit_rate":_rate([r.get("target_zone_hit") for r in target_rows]),
            "avg_first_5pct_session":_avg([r.get("first_5pct_session") for r in layer_rows]),
            "avg_first_target_session":_avg([r.get("first_target_session") for r in target_rows]),
            "avg_max_favorable_pct":_avg([r.get("max_favorable_pct") for r in layer_rows]),
            "avg_max_adverse_pct":_avg([r.get("max_adverse_pct") for r in layer_rows]),
            "target_mae_pct":_avg([abs(float(r.get("target_error_pct"))) if r.get("target_error_pct") is not None else None for r in target_rows]),
            "state_breakdown":_breakdown(layer_rows,"state","movement_5pct_hit"),
            "origin_breakdown":_breakdown(layer_rows,"signal_origin","movement_5pct_hit"),
        }
        if layer in {"GEX_DAILY","FUSION"}:
            metrics["intent_breakdown"]=_breakdown(layer_rows,"gex_intent","target_zone_hit")
            metrics["expiry_breakdown"]=_breakdown(layer_rows,"expiry_regime","target_zone_hit")
        if layer == "GEX_DAILY":
            metrics.update(_calibrate_gex_close(layer_rows))
        if layer=="GEX_INTRADAY":
            enter=[r for r in layer_rows if str((r.get("payload") or {}).get("state") or "").upper()=="ENTER"]
            wait=[r for r in layer_rows if str((r.get("payload") or {}).get("state") or "").upper()=="WAIT"]
            enter_rate=_rate([r.get("target_zone_hit") for r in enter]); wait_rate=_rate([r.get("target_zone_hit") for r in wait])
            metrics.update({"enter_sample":len(enter),"wait_sample":len(wait),"enter_target_hit_rate":enter_rate,"wait_target_hit_rate":wait_rate,"timing_uplift":round((enter_rate or 0)-(wait_rate or 0),4)})
        if layer=="CORE": metrics["cross_section_quality"]=feature_quality.get("core_rank_bins",{})
        if layer=="GEX_DAILY": metrics["gex_intent_quality"]=feature_quality.get("gex_intent",{})
        if layer=="FUSION": metrics["execution_state_quality"]=feature_quality.get("execution_states",{})
        metrics["archived_feature_sample"]=feature_quality.get("sample",0); metrics["status"]=_status(layer,metrics)
        conn.execute(
            """INSERT INTO layer_model_calibration(layer,status,sample,complete_dates,movement_5pct_hit_rate,target_zone_hit_rate,
                   avg_first_5pct_session,avg_first_target_session,avg_max_favorable_pct,avg_max_adverse_pct,target_mae_pct,
                   enter_sample,wait_sample,enter_target_hit_rate,wait_target_hit_rate,timing_uplift,metrics_json,updated_at_utc,updated_at_madrid)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(layer) DO UPDATE SET status=excluded.status,sample=excluded.sample,complete_dates=excluded.complete_dates,
                   movement_5pct_hit_rate=excluded.movement_5pct_hit_rate,target_zone_hit_rate=excluded.target_zone_hit_rate,
                   avg_first_5pct_session=excluded.avg_first_5pct_session,avg_first_target_session=excluded.avg_first_target_session,
                   avg_max_favorable_pct=excluded.avg_max_favorable_pct,avg_max_adverse_pct=excluded.avg_max_adverse_pct,
                   target_mae_pct=excluded.target_mae_pct,enter_sample=excluded.enter_sample,wait_sample=excluded.wait_sample,
                   enter_target_hit_rate=excluded.enter_target_hit_rate,wait_target_hit_rate=excluded.wait_target_hit_rate,
                   timing_uplift=excluded.timing_uplift,metrics_json=excluded.metrics_json,updated_at_utc=excluded.updated_at_utc,
                   updated_at_madrid=excluded.updated_at_madrid""",
            (layer,metrics["status"],metrics["sample"],metrics["complete_dates"],metrics.get("movement_5pct_hit_rate"),metrics.get("target_zone_hit_rate"),
             metrics.get("avg_first_5pct_session"),metrics.get("avg_first_target_session"),metrics.get("avg_max_favorable_pct"),metrics.get("avg_max_adverse_pct"),
             metrics.get("target_mae_pct"),metrics.get("enter_sample"),metrics.get("wait_sample"),metrics.get("enter_target_hit_rate"),metrics.get("wait_target_hit_rate"),
             metrics.get("timing_uplift"),json.dumps(metrics,ensure_ascii=False),now_utc,now_mad),
        )
        result[layer]=metrics
    conn.commit(); conn.close()
    for layer in ("GEX_DAILY", "GEX_INTRADAY", "GRAPH"):
        try:
            model = register_layer_model(layer, result.get(layer) or {}, grouped.get(layer) or [])
            result[layer]["model_version_id"] = model.get("id")
            result[layer]["model_selected"] = bool(model.get("selected"))
            result[layer]["selection_score"] = model.get("selection_score")
        except Exception:
            pass
    return result


def get_layer_calibrations() -> dict[str,dict]:
    ensure_layer_calibration_schema(); conn=_connect(); rows=[dict(r) for r in conn.execute("SELECT * FROM layer_model_calibration").fetchall()]; conn.close(); out={}
    for row in rows:
        try: metrics=json.loads(row.get("metrics_json") or "{}")
        except Exception: metrics={}
        metrics.update({k:v for k,v in row.items() if k!="metrics_json"}); out[str(row.get("layer") or "").upper()]=metrics
    return out
