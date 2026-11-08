from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from daily_reverse_alpha import _stage_path
from economic_architecture_challengers import _build_rows
from economic_waterfall_calibration import (
    _direction_path,
    _entry_price,
    _future_same_segment,
    _population_metrics,
)

MODE="FUSION_GATE_CALIBRATION_V1"
DECISION_MODE="FUSION_GATE_DUAL_LANE_DECISION_V1"
CHALLENGER="EARLY_RECOVERY_V1"
PASS_STATES={"READY","RETEST","ENTER"}
BLOCK_STATES={"DIVERGENCE","MARKET_BLOCKED","EXTENDED","CAPPED"}


def _num(v: Any):
    try:
        if v in (None,"") or isinstance(v,bool): return None
        x=float(v)
        return x if x==x and x not in (float("inf"),float("-inf")) else None
    except Exception:
        return None


def _fusion_signal(rows: list[dict]) -> dict|None:
    xs=[r for r in rows if str(r.get("layer") or "").upper()=="FUSION"]
    if not xs: return None
    pr={"ENTER":6,"RETEST":5,"READY":4,"EARLY":3,"CAPPED":2,"EXTENDED":2,"DIVERGENCE":1,"MARKET_BLOCKED":1}
    return max(xs,key=lambda r:(pr.get(str(r.get("state") or r.get("opportunity_state") or "").upper(),0),float(_num(r.get("score")) or 0.0)))


def _state(r: dict|None) -> str:
    if not r: return "MISSING"
    return str(r.get("state") or r.get("opportunity_state") or "UNKNOWN").upper()


def _direct_path_from_outcome(outcome: dict, direction: str) -> dict:
    direction=str(direction or "").upper()
    ret=_num(outcome.get("return_long_pct"))
    mfe_long=_num(outcome.get("mfe_long_pct"))
    mae_long=_num(outcome.get("mae_long_pct"))
    if ret is None or mfe_long is None or mae_long is None:
        return {"mfe_pct":0.0,"mae_pct":0.0,"first_hit_session":None,"horizon_close_return_pct":None}
    if direction=="SHORT":
        mfe=max(0.0,-float(mae_long))
        mae=max(0.0,float(mfe_long))
        close=-float(ret)
    else:
        mfe=max(0.0,float(mfe_long))
        mae=max(0.0,-float(mae_long))
        close=float(ret)
    return {
        "mfe_pct":round(mfe,6),
        "mae_pct":round(mae,6),
        "first_hit_session":None,
        "horizon_close_return_pct":round(close,6),
    }


def _fusion_signal_for_key(signal_index, date_value: str, ticker_value: str) -> dict|None:
    """Return the actual FUSION signal regardless of whether CORE owns direction.

    FUSION direction is selected upstream as GEX -> CORE -> GRAPH. Restricting the
    research join to source.core_direction drops GEX_STANDALONE and divergence
    cases, so the calibration must anchor on the emitted FUSION signal itself.
    """
    candidates=[]
    for direction in ("long","short"):
        candidates.extend(list(signal_index.get((date_value,ticker_value,direction),[]) or []))
    return _fusion_signal(candidates)


def _fusion_direction(signal: dict|None) -> str:
    direction=str((signal or {}).get("direction") or "").upper()
    return direction if direction in {"LONG","SHORT"} else ""


def _rows_from_outcomes_with_coverage(canonical, signal_index, outcomes_by_observation):
    out=[]
    pre_states=Counter()
    post_states=Counter()
    origins=Counter()
    directions=Counter()
    matched_outcomes=0
    selected_fusion=0
    invalid_direction=0
    core_direction_disagreements=0
    expected_enter=0
    reconciled_enter=0
    expected_retest=0
    reconciled_retest=0
    missing_required_timing=[]

    for (date_value,ticker_value),source in sorted(canonical.items()):
        oid=int(_num(source.get("id")) or 0)
        outcome=outcomes_by_observation.get(oid)
        if not outcome:
            continue
        matched_outcomes+=1
        d=str(date_value)[:10]
        t=str(ticker_value).upper()

        sig=_fusion_signal_for_key(signal_index,d,t)
        if sig is None:
            continue
        selected_fusion+=1

        state=_state(sig)
        pre_states[state]+=1
        origin=str(sig.get("signal_origin") or sig.get("primary_engine") or "UNKNOWN").upper()
        origins[origin]+=1

        direction=_fusion_direction(sig)
        if not direction:
            invalid_direction+=1
            continue
        directions[direction]+=1

        core_direction=str(source.get("core_direction") or "").upper()
        if core_direction in {"LONG","SHORT"} and core_direction!=direction:
            core_direction_disagreements+=1

        signals=list(signal_index.get((d,t,direction.lower()),[]) or [])
        stage=_stage_path(signals,direction.lower())
        timing_ready=bool(stage.get("timing"))

        if state=="ENTER":
            expected_enter+=1
            if timing_ready:
                reconciled_enter+=1
            else:
                missing_required_timing.append({
                    "date":d,"ticker":t,"direction":direction,
                    "fusion_state":state,"signal_origin":origin,
                })
        elif state=="RETEST":
            expected_retest+=1
            if timing_ready:
                reconciled_retest+=1
            else:
                missing_required_timing.append({
                    "date":d,"ticker":t,"direction":direction,
                    "fusion_state":state,"signal_origin":origin,
                })

        if not timing_ready:
            continue

        post_states[state]+=1
        current_path=_direct_path_from_outcome(outcome,direction)
        current=bool(stage.get("fusion"))
        out.append({
            "date":d,
            "ticker":t,
            "entry_price":_num(outcome.get("entry_price")),
            "current_direction":direction,
            "current_direction_known":True,
            "direction_source":"FUSION_SIGNAL",
            "source_core_direction":core_direction if core_direction in {"LONG","SHORT"} else None,
            "fusion_signal_origin":origin,
            "mfe_pct":float(current_path.get("mfe_pct") or 0.0),
            "mae_pct":float(current_path.get("mae_pct") or 0.0),
            "first_hit_session":None,
            "horizon_close_return_pct":current_path.get("horizon_close_return_pct"),
            "mover_5d":bool(float(current_path.get("mfe_pct") or 0.0)>=5.0),
            "stage":stage,
            "fusion_available":True,
            "fusion_state":state,
            "fusion_score":_num(sig.get("score")),
            "fusion_actionable":bool(sig.get("actionable")),
            "gex_intent":str(sig.get("gex_intent") or "").upper() or None,
            "blockers":list(sig.get("blockers") or []),
            "current_gate_pass":current,
            "challenger_pass":bool(current or state=="EARLY"),
            "explicit_safety_block":state in BLOCK_STATES,
            "outcome_source":"observation_outcomes_h5",
        })

    invariant_expected=expected_enter+expected_retest
    invariant_reconciled=reconciled_enter+reconciled_retest
    coverage_status=(
        "VALID_RECONCILED"
        if invariant_expected==invariant_reconciled and invalid_direction==0
        else "INVALID_COVERAGE_RECONCILIATION"
    )
    coverage={
        "status":coverage_status,
        "direction_source":"FUSION_SIGNAL",
        "matched_mature_h5_observations":matched_outcomes,
        "mature_observations_with_fusion_signal":selected_fusion,
        "post_timing_rows":len(out),
        "pre_timing_states":dict(pre_states),
        "post_timing_states":dict(post_states),
        "fusion_signal_origins":dict(origins),
        "fusion_directions":dict(directions),
        "core_direction_disagreements":core_direction_disagreements,
        "invalid_fusion_direction":invalid_direction,
        "required_timing_invariant":{
            "states":["ENTER","RETEST"],
            "expected":invariant_expected,
            "reconciled":invariant_reconciled,
            "missing":max(0,invariant_expected-invariant_reconciled),
            "fusion_enter_expected":expected_enter,
            "fusion_enter_reconciled":reconciled_enter,
            "fusion_retest_expected":expected_retest,
            "fusion_retest_reconciled":reconciled_retest,
            "rule":"Every mature FUSION ENTER/RETEST must have same-direction GEX_INTRADAY timing ready by construction.",
        },
        "missing_required_timing_examples":missing_required_timing[:20],
        "missing_required_timing_examples_truncated":len(missing_required_timing)>20,
        "gex_standalone_signals":int(origins.get("GEX_STANDALONE",0)),
    }
    return out,coverage


def _rows_from_outcomes(canonical, signal_index, outcomes_by_observation):
    rows,_coverage=_rows_from_outcomes_with_coverage(
        canonical,signal_index,outcomes_by_observation
    )
    return rows


def _rows_from_trusted_series_with_coverage(
    canonical,
    signal_index,
    trusted_price_series,
    *,
    threshold_pct: float = 5.0,
    horizon_sessions: int = 5,
    expected_generation: dict | None = None,
):
    """Evaluate FUSION on the exact trusted price lane used by FULL replay.

    Point-in-time feature/signal rows decide the state and direction. Future
    trusted OHLC is label-only and is restricted to the same continuity segment.
    This avoids coupling economic coverage to retained physical observation_id
    labels, which can be sparse after historical compaction.
    """
    out=[]
    pre_states=Counter()
    h5_states=Counter()
    post_states=Counter()
    origins=Counter()
    directions=Counter()
    incomplete_states=Counter()
    selected_fusion=0
    complete_h5=0
    invalid_direction=0
    invalid_entry=0
    core_direction_disagreements=0
    expected_enter=0
    reconciled_enter=0
    h5_enter=0
    expected_retest=0
    reconciled_retest=0
    h5_retest=0
    missing_required_timing=[]
    missing_h5_examples=[]

    for (date_value,ticker_value),source in sorted(canonical.items()):
        d=str(date_value)[:10]
        t=str(ticker_value).upper()
        sig=_fusion_signal_for_key(signal_index,d,t)
        if sig is None:
            continue
        selected_fusion+=1
        state=_state(sig)
        pre_states[state]+=1
        origin=str(sig.get("signal_origin") or sig.get("primary_engine") or "UNKNOWN").upper()
        origins[origin]+=1

        direction=_fusion_direction(sig)
        if not direction:
            invalid_direction+=1
            continue
        directions[direction]+=1

        core_direction=str(source.get("core_direction") or "").upper()
        if core_direction in {"LONG","SHORT"} and core_direction!=direction:
            core_direction_disagreements+=1

        signals=list(signal_index.get((d,t,direction.lower()),[]) or [])
        stage=_stage_path(signals,direction.lower())
        timing_ready=bool(stage.get("timing"))

        if state=="ENTER":
            expected_enter+=1
            if timing_ready:
                reconciled_enter+=1
            else:
                missing_required_timing.append({
                    "date":d,"ticker":t,"direction":direction,
                    "fusion_state":state,"signal_origin":origin,
                })
        elif state=="RETEST":
            expected_retest+=1
            if timing_ready:
                reconciled_retest+=1
            else:
                missing_required_timing.append({
                    "date":d,"ticker":t,"direction":direction,
                    "fusion_state":state,"signal_origin":origin,
                })

        entry=_entry_price(source)
        if entry is None or float(entry)<=0:
            invalid_entry+=1
            incomplete_states[state]+=1
            if len(missing_h5_examples)<20:
                missing_h5_examples.append({
                    "date":d,"ticker":t,"direction":direction,
                    "fusion_state":state,"reason":"INVALID_ENTRY",
                })
            continue

        future=_future_same_segment(
            trusted_price_series.get(t,[]),
            d,
            int(horizon_sessions),
        )
        if len(future)<int(horizon_sessions):
            incomplete_states[state]+=1
            if len(missing_h5_examples)<20:
                missing_h5_examples.append({
                    "date":d,"ticker":t,"direction":direction,
                    "fusion_state":state,"reason":"INCOMPLETE_TRUSTED_H5",
                    "sessions":len(future),
                })
            continue

        complete_h5+=1
        h5_states[state]+=1
        if state=="ENTER":
            h5_enter+=1
        elif state=="RETEST":
            h5_retest+=1

        if not timing_ready:
            continue

        post_states[state]+=1
        current_path=_direction_path(
            float(entry),direction,future,float(threshold_pct)
        )
        current=bool(stage.get("fusion"))
        out.append({
            "date":d,
            "ticker":t,
            "entry_price":float(entry),
            "current_direction":direction,
            "current_direction_known":True,
            "direction_source":"FUSION_SIGNAL",
            "source_core_direction":core_direction if core_direction in {"LONG","SHORT"} else None,
            "fusion_signal_origin":origin,
            "mfe_pct":float(current_path.get("mfe_pct") or 0.0),
            "mae_pct":float(current_path.get("mae_pct") or 0.0),
            "first_hit_session":current_path.get("first_hit_session"),
            "horizon_close_return_pct":current_path.get("horizon_close_return_pct"),
            "mover_5d":bool(float(current_path.get("mfe_pct") or 0.0)>=float(threshold_pct)),
            "stage":stage,
            "fusion_available":True,
            "fusion_state":state,
            "fusion_score":_num(sig.get("score")),
            "fusion_actionable":bool(sig.get("actionable")),
            "gex_intent":str(sig.get("gex_intent") or "").upper() or None,
            "blockers":list(sig.get("blockers") or []),
            "current_gate_pass":current,
            "challenger_pass":bool(current or state=="EARLY"),
            "explicit_safety_block":state in BLOCK_STATES,
            "outcome_source":"trusted_reverse_series_h5",
        })

    invariant_expected=expected_enter+expected_retest
    invariant_reconciled=reconciled_enter+reconciled_retest
    actual_generation={
        "fusion_signals":selected_fusion,
        "fusion_enter_state":expected_enter,
    }
    expected_generation=dict(expected_generation or {})
    generation_mismatch={}
    for key in ("fusion_signals","fusion_enter_state"):
        if expected_generation.get(key) is None:
            continue
        expected_value=int(expected_generation.get(key) or 0)
        actual_value=int(actual_generation.get(key) or 0)
        if expected_value!=actual_value:
            generation_mismatch[key]={
                "expected":expected_value,
                "actual":actual_value,
            }

    coverage_status=(
        "VALID_RECONCILED"
        if (
            invariant_expected==invariant_reconciled
            and invalid_direction==0
            and not generation_mismatch
        )
        else "INVALID_COVERAGE_RECONCILIATION"
    )
    coverage={
        "status":coverage_status,
        "label_source":"TRUSTED_REVERSE_SERIES_V1",
        "direction_source":"FUSION_SIGNAL",
        "canonical_rows":len(canonical),
        "fusion_signals":selected_fusion,
        "complete_h5_fusion_signals":complete_h5,
        "incomplete_h5_fusion_signals":max(0,selected_fusion-complete_h5),
        "complete_h5_pct":(
            round(100.0*complete_h5/selected_fusion,3)
            if selected_fusion else None
        ),
        "post_timing_rows":len(out),
        "pre_timing_states":dict(pre_states),
        "complete_h5_states":dict(h5_states),
        "incomplete_h5_states":dict(incomplete_states),
        "post_timing_states":dict(post_states),
        "fusion_signal_origins":dict(origins),
        "fusion_directions":dict(directions),
        "core_direction_disagreements":core_direction_disagreements,
        "invalid_fusion_direction":invalid_direction,
        "invalid_entry_price":invalid_entry,
        "generation_reconciliation":{
            "expected":expected_generation,
            "actual":actual_generation,
            "mismatch":generation_mismatch,
            "valid":not bool(generation_mismatch),
        },
        "required_timing_invariant":{
            "states":["ENTER","RETEST"],
            "expected":invariant_expected,
            "reconciled":invariant_reconciled,
            "missing":max(0,invariant_expected-invariant_reconciled),
            "fusion_enter_expected":expected_enter,
            "fusion_enter_reconciled":reconciled_enter,
            "fusion_enter_h5_complete":h5_enter,
            "fusion_retest_expected":expected_retest,
            "fusion_retest_reconciled":reconciled_retest,
            "fusion_retest_h5_complete":h5_retest,
            "rule":"Every FUSION ENTER/RETEST must have same-direction GEX_INTRADAY timing ready by construction.",
        },
        "missing_required_timing_examples":missing_required_timing[:20],
        "missing_required_timing_examples_truncated":len(missing_required_timing)>20,
        "missing_h5_examples":missing_h5_examples,
        "missing_h5_examples_truncated":max(0,selected_fusion-complete_h5)>len(missing_h5_examples),
        "gex_standalone_signals":int(origins.get("GEX_STANDALONE",0)),
        "policy":{
            "future_prices_are_labels_only":True,
            "same_continuity_segment_required":True,
            "physical_observation_id_not_required":True,
            "full_replay_trusted_lane_required":True,
        },
    }
    return out,coverage

def _rows(canonical, signal_index, series):
    base=_build_rows(canonical,signal_index,series,threshold_pct=5.0,horizon_sessions=5)
    out=[]
    for row in base:
        if not row.get("current_direction_known") or not bool((row.get("stage") or {}).get("timing")):
            continue
        d=str(row.get("date") or "")[:10]; t=str(row.get("ticker") or "").upper()
        direction=str(row.get("current_direction") or "").upper()
        sig=_fusion_signal(list(signal_index.get((d,t,direction.lower()),[]) or []))
        state=_state(sig)
        current=bool((row.get("stage") or {}).get("fusion"))
        out.append({**row,
            "fusion_available":sig is not None,
            "fusion_state":state,
            "fusion_score":_num((sig or {}).get("score")),
            "fusion_actionable":bool((sig or {}).get("actionable")),
            "gex_intent":str((sig or {}).get("gex_intent") or "").upper() or None,
            "blockers":list((sig or {}).get("blockers") or []),
            "current_gate_pass":current,
            "challenger_pass":bool(current or state=="EARLY"),
            "explicit_safety_block":state in BLOCK_STATES,
        })
    return out


def _split(rows):
    dates=sorted({str(r.get("date") or "") for r in rows if r.get("fusion_available")})
    if len(dates)<8: return set(dates),set(),set()
    n=len(dates); a=max(1,int(n*.5)); b=min(max(a+1,int(n*.75)),n-1)
    return set(dates[:a]),set(dates[a:b]),set(dates[b:])


def _ge(new,old):
    n,c=_num(new),_num(old)
    if n is None and c is None:return True
    if n is None:return False
    if c is None:return True
    return n>=c


def _le(new,old):
    n,c=_num(new),_num(old)
    if n is None and c is None:return True
    if n is None:return False
    if c is None:return True
    return n<=c


def _state_metrics(rows,total):
    groups=defaultdict(list)
    for r in rows: groups[str(r.get("fusion_state") or "UNKNOWN")].append(r)
    return {k:_population_metrics(v,total_movers=total) for k,v in sorted(groups.items())}


def _comparison(rows):
    ids=[r for r in rows if r.get("fusion_available")]
    missing=[r for r in rows if not r.get("fusion_available")]
    total=sum(bool(r.get("mover_5d")) for r in ids)
    cur=[r for r in ids if r.get("current_gate_pass")]
    new=[r for r in ids if r.get("challenger_pass")]
    rec=[r for r in new if not r.get("current_gate_pass")]
    blocks=[r for r in ids if r.get("explicit_safety_block")]
    cm=_population_metrics(cur,total_movers=total); nm=_population_metrics(new,total_movers=total)
    rm=_population_metrics(rec,total_movers=total)
    cr,nr=_num(cm.get("mover_recall_pct")),_num(nm.get("mover_recall_pct"))
    gate={
        "minimum_control_sample":int(cm.get("sample") or 0)>=20,
        "minimum_challenger_sample":int(nm.get("sample") or 0)>=20,
        "recovers_early_rows":int(rm.get("sample") or 0)>0,
        "recall_improves":cr is not None and nr is not None and nr>cr,
        "precision_not_worse":_ge(nm.get("mover_precision_pct"),cm.get("mover_precision_pct")),
        "return_not_worse":_ge(nm.get("avg_horizon_close_return_pct"),cm.get("avg_horizon_close_return_pct")),
        "profit_factor_not_worse":_ge(nm.get("horizon_close_profit_factor"),cm.get("horizon_close_profit_factor")),
        "mae_not_worse":_le(nm.get("avg_mae_pct"),cm.get("avg_mae_pct")),
    }
    gate["pass"]=all(gate.values())
    counts=Counter()
    for r in ids:
        for b in r.get("blockers") or []:
            if str(b).strip(): counts[str(b).strip()]+=1
    return {
        "identifiable_timing_universe":_population_metrics(ids,total_movers=total),
        "fusion_unavailable":_population_metrics(missing,total_movers=sum(bool(r.get("mover_5d")) for r in rows)),
        "current_fusion_gate":cm,
        "challenger_current_plus_early":nm,
        "recovered_early_rows":rm,
        "explicit_safety_blocks":_population_metrics(blocks,total_movers=total),
        "state_economics":_state_metrics(ids,total),
        "blocker_counts":dict(counts),
        "gate":gate,
    }


def build_fusion_gate_calibration_from_outcomes(canonical,signal_index,outcomes_by_observation,*,lane_label="ROLLING_LATEST"):
    rows,coverage=_rows_from_outcomes_with_coverage(
        canonical,signal_index,outcomes_by_observation
    )
    return _build_result(
        rows,
        lane_label=lane_label,
        outcome_mode="DIRECT_MATURE_OBSERVATION_OUTCOMES_V1",
        coverage_reconciliation=coverage,
        direction_source="FUSION_SIGNAL",
    )


def build_fusion_gate_calibration_from_trusted_series(
    canonical,
    signal_index,
    trusted_price_series,
    *,
    lane_label="ROLLING_LATEST",
    expected_generation: dict | None = None,
):
    rows,coverage=_rows_from_trusted_series_with_coverage(
        canonical,
        signal_index,
        trusted_price_series,
        threshold_pct=5.0,
        horizon_sessions=5,
        expected_generation=expected_generation,
    )
    return _build_result(
        rows,
        lane_label=lane_label,
        outcome_mode="TRUSTED_REVERSE_SERIES_V1",
        coverage_reconciliation=coverage,
        direction_source="FUSION_SIGNAL",
    )


def build_fusion_gate_calibration(canonical,signal_index,trusted_price_series,*,lane_label="ROLLING_LATEST"):
    # Compatibility entrypoint. Since 6.2.44 the trusted replay path uses the
    # actual FUSION signal direction rather than source CORE direction.
    return build_fusion_gate_calibration_from_trusted_series(
        canonical,
        signal_index,
        trusted_price_series,
        lane_label=lane_label,
    )


def _build_result(rows,*,lane_label,outcome_mode,coverage_reconciliation=None,direction_source="FUSION_SIGNAL"):
    coverage=dict(coverage_reconciliation or {})
    coverage_invalid=str(coverage.get("status") or "").upper()=="INVALID_COVERAGE_RECONCILIATION"
    train_d,val_d,test_d=_split(rows)
    pick=lambda ds:[r for r in rows if str(r.get("date") or "") in ds]
    train=_comparison(pick(train_d)) if train_d else {}
    val=_comparison(pick(val_d)) if val_d else {}
    test=_comparison(pick(test_d)) if test_d else {}
    sc=Counter(str(r.get("fusion_state") or "UNKNOWN") for r in rows)
    sm=Counter(str(r.get("fusion_state") or "UNKNOWN") for r in rows if r.get("mover_5d"))
    if coverage_invalid:
        status="INVALID_COVERAGE_RECONCILIATION"
    else:
        status="EVALUABLE" if rows and val_d and test_d else "INSUFFICIENT_FUSION_HISTORY"
    followup=bool(
        status=="EVALUABLE"
        and (val.get("gate") or {}).get("pass")
        and (test.get("gate") or {}).get("pass")
    )
    return {
        "mode":MODE,"lane":str(lane_label).upper(),
        "status":status,
        "live_mutation":False,"promotion_allowed":False,"candidate_family":CHALLENGER,
        "economic_truth":{
            "threshold_pct":5.0,
            "horizon_sessions":5,
            "future_prices_are_labels_only":True,
            "direction_source":direction_source,
            "direction_is_current_fusion_direction":direction_source=="FUSION_SIGNAL",
            "outcome_mode":outcome_mode,
        },
        "coverage_reconciliation":coverage,
        "timing_population":{
            "sample":len(rows),
            "dates":len({str(r.get("date") or "") for r in rows}),
            "movers_5d":sum(bool(r.get("mover_5d")) for r in rows),
            "fusion_available_sample":sum(bool(r.get("fusion_available")) for r in rows),
            "fusion_missing_sample":sum(not bool(r.get("fusion_available")) for r in rows),
        },
        "state_decomposition":{
            "rows_by_state":dict(sc),
            "movers_by_state":dict(sm),
            "current_pass_states":sorted(PASS_STATES),
            "recovered_state":"EARLY",
            "explicit_block_states":sorted(BLOCK_STATES),
        },
        "split":{"train_dates":len(train_d),"validation_dates":len(val_d),"test_dates":len(test_d)},
        "train":train,"validation":val,"final_test":test,
        "historical_followup_candidate":followup,
        "policy":{
            "fixed_candidate_family_only":True,
            "equal_capacity_ranker_retried":False,
            "continuous_threshold_optimization":False,
            "unknown_states_auto_admitted":False,
            "fusion_live_unchanged":True,
            "prospective_required_before_any_live_change":True,
            "coverage_reconciliation_required":outcome_mode in {"DIRECT_MATURE_OBSERVATION_OUTCOMES_V1","TRUSTED_REVERSE_SERIES_V1"},
        },
    }


def combine_fusion_gate_calibrations(rolling,fixed):
    ro=bool(rolling.get("historical_followup_candidate"))
    fi=bool(fixed.get("historical_followup_candidate"))
    dual=ro and fi
    invalid=any(
        str(item.get("status") or "").upper()=="INVALID_COVERAGE_RECONCILIATION"
        for item in (rolling,fixed)
    )
    if invalid:
        status="INVALID_COVERAGE_RECONCILIATION"
        next_step="REPAIR_FUSION_HISTORY_COVERAGE_BEFORE_INTERPRETATION"
    else:
        status=(
            "EVALUABLE"
            if rolling.get("status")=="EVALUABLE" and fixed.get("status")=="EVALUABLE"
            else "PARTIAL"
        )
        next_step=(
            "PROSPECTIVE_SHADOW_FUSION_EARLY_RECOVERY"
            if dual
            else "KEEP_FUSION_LIVE_UNCHANGED_AND_USE_STATE_ECONOMICS_FOR_NEXT_MODULE"
        )
    return {
        "mode":DECISION_MODE,
        "status":status,
        "live_mutation":False,"promotion_allowed":False,"candidate_family":CHALLENGER,
        "rolling_followup":ro,"fixed_followup":fi,"dual_lane_followup":dual,
        "next_step":next_step,
        "policy":{
            "same_fixed_candidate_required_both_lanes":True,
            "no_live_change_from_historical_result":True,
            "prior_equal_capacity_fusion_ranker_remains_rejected":True,
            "coverage_reconciliation_must_be_valid":True,
        },
    }
