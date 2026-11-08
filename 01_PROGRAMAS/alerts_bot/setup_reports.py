from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
from db import get_runtime_state, history_coverage, set_runtime_state, get_open_trades, latest_processed_scanner_run_id, successful_scanner_runs_after, latest_successful_scanner_run
from discord_utils import send_setup_report, log_info
from intraday_archive_bootstrap import intraday_archive_status
from model_metrics import normalized_oos_metrics
from rules import evaluate_features, native_feature_sections
from layer_history import latest_layer_signals
from layer_calibration import get_layer_calibrations
from fusion_engine import build_fusion_signals
from time_utils import now_madrid
from pipeline_state import get_pipeline_state
from coordination_bus import read_data_state
from discord_presentation import build_group, signal_source_label, signal_model_lineage, ticker_bullet_header, shown_count_line, status_display, state_display, gex_intent_display

STATUS_PRIORITY = {
    "champion": 0,
    "scheduled_champion": 1,
    "provisional_champion": 2,
    "challenger": 3,
    "rejected": 8,
    "retired": 9,
    "bootstrap": 10,
}

PHASE_TITLES = {
    "premarket": "🧪 RESEARCH · PREMARKET",
    "market_open": "🧪 RESEARCH · APERTURA",
    "next_day": "🧪 RESEARCH · PRÓXIMA SESIÓN",
}


def _json(value, default=None):
    try:
        return json.loads(value or "{}")
    except Exception:
        return {} if default is None else default


def _fmt(value, decimals=1, signed=False):
    try:
        number=float(value)
        prefix="+" if signed and number>0 else ""
        return f"{prefix}{number:.{decimals}f}"
    except Exception:
        return "n/a"


def _next_weekday(value: date) -> date:
    value += timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def _market_open_source_stale(phase: str, source_date: date, today: date | None = None) -> bool:
    today = today or now_madrid().date()
    return bool(str(phase) == "market_open" and source_date != today)


def _market_now() -> datetime:
    return datetime.now(ZoneInfo(config.ENTRY_MARKET_TIMEZONE))


def _minutes(hhmm: str) -> int:
    h,m=[int(x) for x in hhmm.split(':',1)]
    return h*60+m


def current_operating_phase() -> str:
    now_ny=_market_now()
    if now_ny.weekday() >= 5:
        return "next_day"
    current=now_ny.hour*60+now_ny.minute
    open_min=_minutes(config.ENTRY_WINDOW_START)
    close_min=_minutes(config.ENTRY_WINDOW_END)
    preclose=_minutes(config.SETUP_REPORT_PRECLOSE_MIN_NY)
    if current < open_min:
        return "premarket"
    if current >= preclose or current > close_min:
        return "next_day"
    return "market_open"


def _models() -> list[dict]:
    conn=sqlite3.connect(config.ALERTS_DB)
    conn.row_factory=sqlite3.Row
    rows=conn.execute("SELECT * FROM model_versions ORDER BY id DESC").fetchall()
    conn.close()
    out=[]
    for row in rows:
        item=dict(row)
        item['params']=_json(item.get('params_json'))
        item['metrics']=_json(item.get('metrics_json'))
        item['tier']=str(item['params'].get('feature_tier') or item['metrics'].get('feature_tier') or 'BOOTSTRAP').upper()
        out.append(item)
    return out


def _best_model(tier: str, *, usable_only: bool=True) -> dict|None:
    tier=tier.upper()
    candidates=[m for m in _models() if m.get('tier')==tier]
    if usable_only:
        candidates=[m for m in candidates if str(m.get('status') or '').lower() in {'champion','scheduled_champion','provisional_champion','challenger'}]
    if not candidates:
        return None
    candidates.sort(key=lambda m:(STATUS_PRIORITY.get(str(m.get('status') or '').lower(),50),-int(m.get('id') or 0)))
    return candidates[0]


def _oos(model: dict|None) -> dict:
    if not model:
        return {}
    return normalized_oos_metrics(model.get('metrics') or {})


def _model_line(label: str, model: dict|None, coverage_text: str) -> str:
    if not model:
        return f"• **{label}:** {coverage_text} · sin modelo utilizable todavía"
    oos=_oos(model)
    status=str(model.get('status') or 'unknown').upper()
    return (
        f"• **{label}:** `{model.get('model_key')}` · **{status}** · {coverage_text} · "
        f"OOS `{int(oos.get('sample') or 0)}/{int(oos.get('market_dates') or 0)}d` · "
        f"Hit ±5% `{_fmt(oos.get('target_hit_rate'))}%` · lift `{_fmt(oos.get('discovery_lift'),2)}` · "
        f"MFE/MAE `{_fmt(oos.get('avg_mfe_pct'),1,signed=True)}%/{_fmt(oos.get('avg_mae_pct'),1,signed=True)}%`"
    )


def _latest_rows(layer: str, phase: str) -> tuple[list[dict], str, int]:
    conn=sqlite3.connect(config.ALERTS_DB)
    conn.row_factory=sqlite3.Row
    rows=[]; run_id=0
    # After the open and before the close, reports must use the latest actual
    # market run. On a later restart those rows may already be archived/purged,
    # so the function falls back to the latest canonical daily/core snapshot.
    if phase in {'market_open','next_day'}:
        row=conn.execute(
            "SELECT MAX(source_scanner_run_id) run_id FROM feature_observations WHERE observation_layer='intraday'"
        ).fetchone()
        run_id=int(row['run_id'] or 0)
        if run_id:
            rows=conn.execute(
                "SELECT * FROM feature_observations WHERE observation_layer='intraday' AND source_scanner_run_id=? ORDER BY ticker,id",
                (run_id,),
            ).fetchall()
    if not rows and layer == 'CORE':
        row=conn.execute(
            "SELECT MAX(snapshot_date) d FROM feature_observations WHERE observation_layer='core_history' AND feature_tier='CORE'"
        ).fetchone()
        d=str(row['d'] or '')
        rows=conn.execute(
            "SELECT * FROM feature_observations WHERE observation_layer='core_history' AND feature_tier='CORE' AND snapshot_date=? ORDER BY ticker,id",
            (d,),
        ).fetchall() if d else []
        run_id=max((int(r['source_scanner_run_id'] or 0) for r in rows),default=0)
    elif not rows:
        row=conn.execute(
            "SELECT MAX(snapshot_date) d FROM feature_observations WHERE observation_layer='daily_close' AND is_canonical_daily_close=1"
        ).fetchone()
        d=str(row['d'] or '')
        rows=conn.execute(
            "SELECT * FROM feature_observations WHERE observation_layer='daily_close' AND is_canonical_daily_close=1 AND snapshot_date=? ORDER BY ticker,id",
            (d,),
        ).fetchall() if d else []
        run_id=max((int(r['source_scanner_run_id'] or 0) for r in rows),default=0)
    conn.close()
    items=[]
    for row in rows:
        item=dict(row)
        features=_json(item.get('features_json'))
        if features:
            features.setdefault('ticker',item.get('ticker'))
            features.setdefault('price',item.get('current_price'))
            items.append(features)
    source_date=str(rows[0]['snapshot_date']) if rows else ''
    return items,source_date,run_id

def _confirmations(features: dict, direction: str) -> list[str]:
    sign=1.0 if direction=='long' else -1.0
    mapping={
        'mercado':features.get('market_context_score'),
        'sector':features.get('sector_etf_score'),
        'breadth':features.get('sector_breadth_score'),
        'leveraged':features.get('leveraged_confirmation_score'),
        'grafo':features.get('relationship_context_score'),
        'lead-lag':features.get('relationship_lead_lag_score'),
    }
    out=[]
    for name,value in mapping.items():
        try:
            if sign*float(value)>=10:
                out.append(name)
        except Exception:
            pass
    return out


def _candidate_rationale(candidate: dict, features: dict, layer: str) -> str:
    bits=[str(candidate.get('reason') or candidate.get('rule_name') or 'patrón del modelo')]
    flow=str(features.get('option_signal') or features.get('unusual_flow_signal') or '').strip()
    trend=str(features.get('trend') or '').strip()
    if flow and flow.lower() not in {'neutral','none','n/a'}: bits.append(flow)
    if trend and trend.lower() not in {'neutral','none','n/a'}: bits.append(f"trend {trend}")
    try:
        gex=float(features.get('total_gex_change_pct'))
        bits.append(f"ΔGEX total {gex:+.1f}%")
    except Exception:
        pass
    if layer=='FULL':
        try: bits.append(f"Call Wall {float(features.get('call_wall')):.2f}")
        except Exception: pass
        try: bits.append(f"Put Wall {float(features.get('put_wall')):.2f}")
        except Exception: pass
    return '; '.join(dict.fromkeys(bits))


def _rank_candidates(model: dict|None, rows: list[dict], mode: str, layer: str) -> list[dict]:
    if not model:
        return []
    params=model.get('params') or {}
    by_ticker={}
    for features in rows:
        ticker=str(features.get('ticker') or '').upper()
        if not ticker: continue
        for candidate in evaluate_features(features,params,mode=mode):
            projection=float(candidate.get('daily_pattern_projection_pct') or 0.0)
            if projection < float(config.SETUP_REPORT_MIN_PROJECTION):
                continue
            confirms=_confirmations(features,str(candidate.get('direction') or ''))
            score=float(candidate.get('score') or 0.0)
            strong=(score>=float(config.PRECISION_FIRST_MIN_LIVE_SCORE) and projection>=float(config.PRECISION_FIRST_MIN_LIVE_PROJECTION) and len(confirms)>=int(config.PRECISION_FIRST_MIN_CONFIRMING_BLOCKS))
            item={
                'ticker':ticker,'direction':str(candidate.get('direction') or ''),'score':score,
                'projection':projection,'rule':candidate.get('rule_name'),'reason':_candidate_rationale(candidate,features,layer),
                'confirmations':confirms,'strong':strong,'price':features.get('price'),
                'model':model,'layer':layer,
            }
            rank=score+projection*0.12+len(confirms)*3+(8 if strong else 0)
            item['rank']=rank
            if ticker not in by_ticker or rank>by_ticker[ticker]['rank']:
                by_ticker[ticker]=item
    return sorted(by_ticker.values(),key=lambda x:x['rank'],reverse=True)[:max(1,int(config.SETUP_REPORT_MAX_CANDIDATES))]


def _candidate_lines(candidates: list[dict], source: str) -> list[str]:
    if not candidates:
        return [f"• Sin candidatos que superen los filtros del modelo {source}."]
    lines=[]
    for idx,item in enumerate(candidates,1):
        side='CALL' if item['direction']=='long' else 'PUT'
        state='🟢 preparado' if item['strong'] else '🟡 watch'
        lines.append(
            f"• {ticker_source_label(item['ticker'], source=('CORE' if str(source).upper() == 'CORE' else 'GEX'))} · **{side}** · {state} · score `{_fmt(item['score'])}` · "
            f"proyección `{_fmt(item['projection'],0)}%` · precio `{_fmt(item.get('price'),2)}`\n"
            f"   Racional: {item['reason']} · confirma `{', '.join(item['confirmations']) or 'pendiente'}` · regla `{item['rule']}`"
        )
    return lines


def _short(text: str, limit: int = 150) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _target_zone_text(low, high) -> str:
    if low is None or high is None:
        return "`pendiente de GEX`"
    try:
        if abs(float(low) - float(high)) < 1e-9:
            return f"`{_fmt(low,2)}`"
    except Exception:
        pass
    return f"`{_fmt(low,2)}–{_fmt(high,2)}`"


def _layer_candidate_lines(rows: list[dict], core_model: dict | None = None, *, fresh_timing: bool = True) -> list[str]:
    """Compact shortlist: one readable block per ticker, no technical dump."""
    if not rows:
        return ["_Sin candidatos nuevos._"]
    cards: list[str] = []
    for row in rows:
        item = dict(row.get("payload") or _json(row.get("payload_json")))
        ticker = str(row.get("ticker") or item.get("ticker") or "N/A").upper()
        direction = str(item.get("direction") or row.get("direction") or "short")
        side = "CALL" if direction == "long" else "PUT"
        raw_state = str(item.get("opportunity_state") or item.get("state") or row.get("state") or "WATCH").upper()
        state = raw_state
        blockers = [str(x) for x in (item.get("blockers") or []) if str(x).strip()]
        blockers += [str(x) for x in (item.get("intraday_adverse") or []) if str(x).strip()]
        if not fresh_timing and raw_state == "ENTER":
            state = "READY"
            blockers.insert(0, "timing de la sesión objetivo pendiente")
        if raw_state == "ENTER" and fresh_timing:
            state_label = "CANDIDATA ENTER · CON RIESGOS" if blockers else "CANDIDATA ENTER · FUSION"
        else:
            state_label = state_display(state)
        source = signal_source_label(item, rule_name=item.get("rule_name"))
        price = item.get("price")
        low, high = item.get("target_zone_low"), item.get("target_zone_high")
        gex_diagnostic = str(item.get("gex_diagnostic_code") or "").strip().upper()
        target = _target_zone_text(low, high).strip("`") if low is not None and high is not None else "pendiente de GEX"
        target_pct = abs(float(item.get("target_pct") or 0.0))
        score = float(item.get("score") or row.get("score") or 0.0)
        rationale = _short(item.get("rationale") or item.get("reason") or item.get("signal_origin"), 190)
        observations = ", ".join(dict.fromkeys(blockers))
        if raw_state == "ENTER" and fresh_timing:
            status_detail = (
                f"Riesgos/avisos: {observations}. Sigue sin ser una entrada operativa hasta registrarse en Alerts Live."
                if observations else
                "FUSION cumple su estado teórico; sigue sin ser una entrada operativa hasta registrarse en Alerts Live."
            )
        else:
            status_detail = observations or "confirmación pendiente"
        if gex_diagnostic and gex_diagnostic not in {"GEX_READY", "TIMING_PENDING"}:
            status_detail = f"{gex_diagnostic} · {status_detail}"
        cards.append("\n".join([
            f"**${ticker} · {side} · [{source}] · {state_label} · prioridad {score:.0f}**",
            f"`{_fmt(price,2) if price is not None else 'n/a'} → {target}`" + (f" · recorrido `{target_pct:.1f}%`" if target_pct else ""),
            f"{rationale or 'Sin lectura adicional.'}",
            f"**Estado previo a operativa:** {status_detail}",
        ]))
    return cards


def build_setup_report(phase: str, *, recovery: bool=False) -> tuple[str,dict]:
    phase = phase if phase in PHASE_TITLES else current_operating_phase()
    coverage = history_coverage(); intraday = intraday_archive_status()
    try: data_state = read_data_state(config.COORDINATION_DB)
    except Exception: data_state = {}
    data_full_dates = max(int(data_state.get('paired_trading_dates') or 0), int(coverage.get('daily_dates') or 0), int(coverage.get('feature_dates') or 0))
    core_model = _best_model('CORE', usable_only=True); full_model = _best_model('FULL', usable_only=True)
    layer_rows = latest_layer_signals(limit=1000)
    if not layer_rows:
        fallback_rows, fallback_date, fallback_run = _latest_rows('CORE', phase)
        features_map = {str(item.get('ticker') or '').upper(): item for item in fallback_rows if item.get('ticker')}
        generated, _ = build_fusion_signals(features_map, core_model, full_model, mode=('intraday' if phase == 'market_open' else 'daily_close')) if features_map else ([], {})
        layer_rows = [{**item, 'payload': item, 'snapshot_date': fallback_date, 'source_scanner_run_id': fallback_run} for item in generated]
    fusion_rows = [r for r in layer_rows if str(r.get('layer') or '').upper() == 'FUSION']
    open_tickers = {str(t.get('ticker') or '').upper() for t in get_open_trades() if t.get('ticker')}
    fusion_rows = [r for r in fusion_rows if str(r.get('ticker') or (r.get('payload') or {}).get('ticker') or '').upper() not in open_tickers]

    def payload(row): return row.get('payload') or _json(row.get('payload_json'))
    def state(row): return str(payload(row).get('opportunity_state') or payload(row).get('state') or row.get('state') or 'WATCH').upper()
    def origin(row): return str(payload(row).get('signal_origin') or row.get('signal_origin') or '')
    priority = {'ENTER':9,'READY':8,'RETEST':7,'EARLY':6,'DISCOVERED':5,'WATCH':4,'DIVERGENCE':2,'CAPPED':1,'EXTENDED':0,'MARKET_BLOCKED':0}
    by_ticker: dict[str,dict] = {}
    for row in fusion_rows:
        p = payload(row); ticker = str(row.get('ticker') or p.get('ticker') or '').upper()
        if not ticker: continue
        rank = priority.get(state(row),3)*1000 + float(p.get('score') or row.get('score') or 0)
        if ticker not in by_ticker or rank > by_ticker[ticker]['_rank']:
            by_ticker[ticker] = {**row, 'payload':p, '_rank':rank}
    all_candidates = sorted(by_ticker.values(), key=lambda r:r['_rank'], reverse=True)

    source_date = max([str(r.get('snapshot_date') or '') for r in layer_rows] or ['n/a'])
    try: base = date.fromisoformat(source_date)
    except Exception: base = now_madrid().date()
    today = now_madrid().date(); target = today if phase == 'market_open' else _next_weekday(base)
    stale_market_open = _market_open_source_stale(phase, base, today)
    latest_data_run = latest_successful_scanner_run() or {}
    latest_data_run_id = int(latest_data_run.get("id") or 0)
    processed_run_id = int(latest_processed_scanner_run_id() or 0)
    try:
        pending_successful_runs = successful_scanner_runs_after(processed_run_id, limit=1000)
    except Exception:
        pending_successful_runs = []
    pending_run_ids = [int(row.get("id") or 0) for row in pending_successful_runs if int(row.get("id") or 0)]
    alerts_backlog = len(pending_run_ids)
    stale_pipeline = bool(alerts_backlog > 0)
    fresh_timing = bool(phase == 'market_open' and base == today and not recovery and not stale_market_open)
    candidate_total_before_block = len(all_candidates)
    if stale_market_open or stale_pipeline: all_candidates = []

    counts = {name:0 for name in ('ENTER','READY','EARLY','DIVERGENCE','WATCH')}
    for row in fusion_rows:
        s = state(row); key = 'READY' if s == 'RETEST' else 'EARLY' if s in {'DISCOVERED','CAPPED'} else 'DIVERGENCE' if s == 'DIVERGENCE' else 'WATCH' if s not in counts else s
        counts[key] = counts.get(key,0)+1
    max_candidates = max(1, int(getattr(config, 'RESEARCH_COMPACT_MAX_CANDIDATES', 5)))
    selected = all_candidates[:max_candidates]
    core_oos = _oos(core_model)
    overview = "\n".join([
        f"**Datos:** `{source_date}` · **Sesión objetivo:** `{target.isoformat()}` · `RESEARCH/SHADOW`",
        f"**READY:** `{counts.get('READY',0)}` · **EARLY:** `{counts.get('EARLY',0)}` · **DIVERGENCIA:** `{counts.get('DIVERGENCE',0)}` · **WATCH:** `{counts.get('WATCH',0)}`",
        f"**Mostradas:** `{len(selected)}/{candidate_total_before_block}` · **ENTER FUSION teóricos:** `{counts.get('ENTER',0) if fresh_timing else 0}` · operativos `solo Alerts Live`",
        f"**Modelos:** CORE `#{int((core_model or {}).get('id') or 0)}` · hit ±5% `{_fmt(core_oos.get('target_hit_rate'))}%` · GEX `{coverage.get('feature_dates',0)}/{config.DAILY_CLOSE_WINDOW_SESSIONS}` · intradía `{intraday.get('available_sessions',0)}/{intraday.get('required_sessions',config.INTRADAY_CALIBRATION_MIN_ARCHIVED_SESSIONS)}`",
    ])
    cards = [overview]
    if stale_pipeline:
        cards.append(
            f"🔵 Alerts tiene `{alerts_backlog}` run(s) SUCCESS pendientes `{pending_run_ids}` · último Data `{latest_data_run_id}` / procesado `{processed_run_id}`. "
            "No se publican candidatos hasta recuperar la sincronización."
        )
    elif stale_market_open:
        cards.append(f"🔵 Últimos datos `{source_date}`; se espera Scanner SUCCESS de `{today.isoformat()}`. No se publican candidatos con timing antiguo.")
    elif selected:
        cards.extend(_layer_candidate_lines(selected, core_model=core_model, fresh_timing=fresh_timing))
    else:
        cards.append("_Sin candidatos que superen los filtros._")
    message = build_group(PHASE_TITLES[phase], cards, footer="Research no es una entrada. ENTER/UPDATE/HIT/RUNNER/CLOSE se publican solo en Alerts Live.")
    return message, {
        'phase':phase,'recovery':recovery,'source_date':source_date,'target_date':target.isoformat(),
        'core_model_id':(core_model or {}).get('id'),'full_model_id':(full_model or {}).get('id'),
        'selected_candidates':selected,'source_run_id':max([int(r.get('source_scanner_run_id') or 0) for r in layer_rows] or [0]),
        'suppressed_reason':('alerts_backlog' if stale_pipeline else 'stale_market_open_source' if stale_market_open else None),'excluded_open_tickers':sorted(open_tickers),
        'fresh_timing':fresh_timing,
        'fusion_state_counts':counts,
        'fusion_enter_theoretical':counts.get('ENTER',0) if fresh_timing else 0,
        'candidate_total_before_block':candidate_total_before_block,
    }

def send_setup_phase(phase: str, *, recovery: bool=False, force: bool=False) -> str:
    if not config.SETUP_REPORTS_ENABLED:
        return ''
    pipeline = get_pipeline_state()
    state = str(pipeline.get("state") or "ERROR").upper()
    stage = str(pipeline.get("stage") or "")
    # Research only exists after a coherent calibration. Never publish candidates
    # while the global pipeline is waiting, building, calibrating or in ERROR.
    # legacy_fallback is allowed solely for manual/backward-compatible utilities.
    allowed = state in {"SHADOW_READY", "LIVE_READY", "REST"} or stage == "legacy_fallback"
    if not allowed:
        log_info(
            f"⏭️ Discord Research bloqueado | pipeline={state} · phase={phase} · no se publican candidatos",
            send_discord=False,
        )
        return ''
    if state == "REST":
        resume_state = str((pipeline.get("details") or {}).get("resume_state") or "").upper()
        if resume_state not in {"SHADOW_READY", "LIVE_READY"}:
            log_info(
                f"⏭️ Discord Research bloqueado | REST reanuda en {resume_state or 'ERROR'}",
                send_discord=False,
            )
            return ''
    message,payload=build_setup_report(phase,recovery=recovery)
    suppressed_reason = str(payload.get('suppressed_reason') or '')
    state_counts = payload.get('fusion_state_counts') or {}
    log_info(
        "🧪 [RESEARCH SUMMARY] "
        f"phase={phase} recovery={bool(recovery)} source={payload.get('source_date') or 'n/a'} "
        f"target={payload.get('target_date') or 'n/a'} source_run={int(payload.get('source_run_id') or 0)} "
        f"fresh_timing={bool(payload.get('fresh_timing'))} suppressed={suppressed_reason or 'none'} | "
        f"fusion_enter_theoretical={int(payload.get('fusion_enter_theoretical') or 0)} "
        f"ready={int(state_counts.get('READY') or 0)} early={int(state_counts.get('EARLY') or 0)} "
        f"divergence={int(state_counts.get('DIVERGENCE') or 0)} watch={int(state_counts.get('WATCH') or 0)} "
        f"shown={len(payload.get('selected_candidates') or [])}/{int(payload.get('candidate_total_before_block') or 0)} | "
        "operational_entries=Alerts_Live_only",
        send_discord=False,
    )
    key_prefix = 'setup_report_waiting' if suppressed_reason else 'setup_report_sent'
    key=f"{key_prefix}|{phase}|{payload.get('target_date')}"
    signature=f"{payload.get('source_run_id')}|{payload.get('core_model_id')}|{payload.get('full_model_id')}|{suppressed_reason}"
    # One normal delivery per market phase/session. A later Scanner run must not
    # spam another opening/pre-close report. Recalibration and explicit restart
    # recovery may force a refreshed snapshot.
    if not force and get_runtime_state(key):
        return ''
    delivered = send_setup_report(message)
    log_info(
        f"📨 Discord Research · informe {phase} " + ("entregado" if delivered else "NO entregado; revisar webhook/log local"),
        send_discord=False,
    )
    set_runtime_state(key,signature)
    out=config.OUTPUT_DIR/f"setup_report_{phase}.txt"
    out.write_text(message,encoding='utf-8')
    (config.OUTPUT_DIR/f"setup_report_{phase}.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    return message


def maybe_send_for_scanner_run(run: dict) -> str:
    if not config.SETUP_REPORTS_ENABLED or not run:
        return ''
    now_ny=_market_now(); current=now_ny.hour*60+now_ny.minute
    if current>=_minutes(config.SETUP_REPORT_PRECLOSE_MIN_NY):
        return send_setup_phase('next_day')
    if current<=_minutes(config.SETUP_REPORT_OPEN_MAX_NY):
        return send_setup_phase('market_open')
    return ''


def send_recovery_snapshot() -> str:
    if not config.SETUP_REPORT_RECOVERY_ON_START:
        return ''
    return send_setup_phase(current_operating_phase(),recovery=True,force=True)
