from __future__ import annotations

import hashlib
import json
from pathlib import Path
import traceback

import config
import historical_replay_once as historical
from db import connect, set_runtime_state
from fusion_gate_research_runner import run_fusion_gate_research
from research_benchmark import (
    ensure_economic_benchmark,
    ensure_feature_observation_benchmark,
)
from time_utils import ts_madrid_str

HERE=Path(__file__).resolve().parent
BASIS_KEY="fusion_gate_calibration_basis"
SNAPSHOT_KEY="fusion_gate_calibration_snapshot"
PHASE_KEY="fusion_gate_calibration_phase"
FUSION_ARTIFACT_SCHEMA_VERSION=4
FUSION_OUTCOME_MODE="TRUSTED_REVERSE_SERIES_V1"
FUSION_SIDECAR_NAME="latest_fusion_gate_calibration.json"


def _json_state(key: str) -> dict:
    return historical._json_state(key)


def _source_fingerprint() -> str:
    h=hashlib.sha256()
    for name in ("fusion_gate_calibration.py","fusion_gate_research_runner.py","historical_replay_pipeline.py"):
        p=HERE/name
        h.update(name.encode("utf-8")); h.update(b"\0")
        h.update(p.read_bytes()); h.update(b"\0")
    return h.hexdigest()


def _stable_input_watermark() -> dict:
    conn = connect(config.ALERTS_DB, readonly=True)
    try:
        mature = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0), COALESCE(MAX(observation_date),''),
                      COALESCE(SUM(LENGTH(COALESCE(sessions_json,''))),0)
               FROM observation_outcomes
               WHERE complete=1 AND horizon_sessions=5"""
        ).fetchone()
        mature_date = str(mature[2] or "") if mature else ""
        if mature_date:
            features = conn.execute(
                """SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0),
                          COALESCE(SUM(COALESCE(source_scanner_run_id,0)),0),
                          COALESCE(SUM(CAST(ROUND(COALESCE(current_price,0)*10000) AS INTEGER)),0),
                          COALESCE(SUM(COALESCE(is_canonical_daily_close,0)),0)
                   FROM feature_observations
                   WHERE snapshot_date<=?""",
                (mature_date,),
            ).fetchone()
        else:
            features = (0,0,0,0,0)

        def state_value(key: str) -> str:
            try:
                row = conn.execute(
                    "SELECT state_value FROM runtime_state WHERE state_key=?",
                    (key,),
                ).fetchone()
                return str(row[0] or "") if row else ""
            except Exception:
                return ""

        entry_mode = state_value("entry_mode").lower() or "shadow"
        override_id = int(state_value("adaptive_shadow_model_id") or 0)
        model = None
        if entry_mode != "live" and override_id > 0:
            model = conn.execute(
                "SELECT id,model_key,status,params_json FROM model_versions WHERE id=?",
                (override_id,),
            ).fetchone()
        if model is None:
            model = conn.execute(
                """SELECT mv.id,mv.model_key,mv.status,mv.params_json
                   FROM model_versions mv
                   JOIN model_state ms ON ms.model_version_id=mv.id
                   WHERE ms.state_key='active'
                   ORDER BY mv.id DESC LIMIT 1"""
            ).fetchone()
        if model is None:
            model = conn.execute(
                "SELECT id,model_key,status,params_json FROM model_versions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        model_payload = tuple(model) if model else ()
        model_hash = hashlib.sha256(
            json.dumps(model_payload, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        return {
            "mature_outcomes": {
                "rows": int(mature[0] or 0) if mature else 0,
                "max_rowid": int(mature[1] or 0) if mature else 0,
                "max_date": mature_date,
                "sessions_bytes": int(mature[3] or 0) if mature else 0,
            },
            "mature_feature_rows": {
                "rows": int(features[0] or 0),
                "max_rowid": int(features[1] or 0),
                "scanner_run_sum": int(features[2] or 0),
                "price_signature": int(features[3] or 0),
                "canonical_sum": int(features[4] or 0),
            },
            "effective_model_hash": model_hash,
            "entry_mode": entry_mode,
            "shadow_override_id": override_id,
        }
    finally:
        conn.close()


def _basis() -> dict:
    return {
        "source_fingerprint": _source_fingerprint(),
        "stable_input_watermark": _stable_input_watermark(),
    }


def _can_reuse(basis: dict) -> bool:
    prev=_json_state(BASIS_KEY)
    snap=_json_state(SNAPSHOT_KEY)
    return bool(
        prev==basis
        and str(((snap.get("decision") or {}).get("status") or "")).upper() in {"EVALUABLE","PARTIAL"}
    )


def _write_fusion_sidecar(result: dict, *, reused: bool = False) -> Path:
    now=ts_madrid_str()
    previous_meta=result.get("artifact_metadata") or {}
    validity=result.get("research_validity") or {}
    result["artifact_metadata"]={
        **previous_meta,
        "schema_version":FUSION_ARTIFACT_SCHEMA_VERSION,
        "producer_version":historical._production_version(),
        "generated_at_madrid":str(
            previous_meta.get("generated_at_madrid")
            or validity.get("finished_at_madrid")
            or now
        ),
        "published_at_madrid":now,
        "outcome_mode":FUSION_OUTCOME_MODE,
        "reused":bool(reused),
        "source_fingerprint":_source_fingerprint(),
    }
    out=Path(config.OUTPUT_DIR)/"historical_validation"
    out.mkdir(parents=True,exist_ok=True)
    target=out/FUSION_SIDECAR_NAME
    tmp=target.with_suffix(target.suffix+".tmp")
    tmp.write_text(
        json.dumps(result,ensure_ascii=False,indent=2,default=str),
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def mark_fusion_sidecar_pending(reason: str) -> Path:
    target=Path(config.OUTPUT_DIR)/"historical_validation"/FUSION_SIDECAR_NAME
    if target.exists():
        try:
            existing=json.loads(target.read_text(encoding="utf-8"))
            meta=(existing or {}).get("artifact_metadata") or {}
            if (
                str(meta.get("producer_version") or "")==historical._production_version()
                and str(meta.get("outcome_mode") or "")==FUSION_OUTCOME_MODE
                and str(meta.get("source") or "")=="FULL_HISTORICAL_REPLAY"
            ):
                return target
        except Exception:
            pass
    now=ts_madrid_str()
    pending={
        "mode":"FUSION_GATE_CALIBRATION_V1",
        "status":"PENDING_FRESH_RUN",
        "live_mutation":False,
        "promotion_allowed":False,
        "decision":{
            "status":"PENDING",
            "promotion_allowed":False,
            "live_mutation":False,
            "next_step":"WAIT_FOR_FRESH_FULL_REPLAY_FUSION_CALIBRATION",
        },
        "research_validity":{
            "status":"PENDING_FRESH_RUN",
            "strategic_conclusion_allowed":False,
            "started_at_madrid":now,
            "finished_at_madrid":None,
            "reason":str(reason),
            "live_mutation":False,
        },
    }
    return _write_fusion_sidecar(pending,reused=False)


def _lane_label(lane: dict) -> str:
    status=str(lane.get("status") or "UNKNOWN").upper()
    if status!="EVALUABLE":
        return status
    return "PASS" if lane.get("historical_followup_candidate") else "FAIL"


def _summary(result: dict) -> str:
    roll=result.get("rolling_latest") or {}
    fixed=result.get("fixed_benchmark") or {}
    decision=result.get("decision") or {}
    states=((roll.get("state_decomposition") or {}).get("rows_by_state") or {})
    coverage=(roll.get("coverage_reconciliation") or {})
    return (
        "🧪 **FUSION gate calibration v6.2.44 completada.**\n"
        f"Rolling states: {states}\n"
        f"Coverage: {coverage.get('status','n/a')} · "
        f"H5={coverage.get('complete_h5_fusion_signals','n/a')}/"
        f"{coverage.get('fusion_signals','n/a')} · "
        f"post-Timing={coverage.get('post_timing_rows','n/a')}\n"
        f"EARLY_RECOVERY: rolling={_lane_label(roll)} · "
        f"fixed={_lane_label(fixed)} · "
        f"dual={decision.get('dual_lane_followup','n/a')}\n"
        f"Next: {decision.get('next_step','n/a')} · LIVE sin cambios"
    )


def _load_full_replay_fusion_result() -> tuple[dict|None,str]:
    target=Path(config.OUTPUT_DIR)/"historical_validation"/FUSION_SIDECAR_NAME
    if not target.exists():
        return None,"MISSING_SIDECAR"
    try:
        result=json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return None,f"INVALID_JSON:{type(exc).__name__}"
    if not isinstance(result,dict):
        return None,"INVALID_PAYLOAD"
    meta=result.get("artifact_metadata") or {}
    if str(meta.get("producer_version") or "")!=historical._production_version():
        return None,"PRODUCER_VERSION_MISMATCH"
    if str(meta.get("outcome_mode") or "")!=FUSION_OUTCOME_MODE:
        return None,"OUTCOME_MODE_MISMATCH"
    if str(meta.get("source") or "")!="FULL_HISTORICAL_REPLAY":
        return None,"NOT_FULL_REPLAY_ARTIFACT"
    rolling=result.get("rolling_latest") or {}
    fixed=result.get("fixed_benchmark") or {}
    if str(((rolling.get("economic_truth") or {}).get("outcome_mode") or ""))!=FUSION_OUTCOME_MODE:
        return None,"ROLLING_OUTCOME_MODE_MISMATCH"
    fixed_mode=str(((fixed.get("economic_truth") or {}).get("outcome_mode") or ""))
    if fixed_mode not in {FUSION_OUTCOME_MODE,""}:
        return None,"FIXED_OUTCOME_MODE_MISMATCH"
    replay_summary=historical._json_state("historical_replay_summary")
    if str(replay_summary.get("status") or "").lower() not in {"ok","reused"}:
        return None,"HISTORICAL_REPLAY_NOT_VALID"
    validity=replay_summary.get("research_validity") or {}
    if validity and validity.get("strategic_conclusion_allowed") is not True:
        result["research_validity"]={
            **(result.get("research_validity") or {}),
            "status":"INVALID_PARENT_REPLAY",
            "strategic_conclusion_allowed":False,
            "parent_replay_validity":validity,
            "live_mutation":False,
        }
    return result,"OK"


def _set_phase(phase: str) -> None:
    set_runtime_state(PHASE_KEY, phase)
    print(f"PHASE | FUSION gate calibration | {phase}", flush=True)


def run_fusion_only(*, notify_reuse: bool = False) -> int:
    _set_phase("load_full_replay_artifact")
    result,reason=_load_full_replay_fusion_result()
    if result is None:
        mark_fusion_sidecar_pending(f"FULL_REPLAY_ARTIFACT_UNAVAILABLE:{reason}")
        set_runtime_state("fusion_gate_calibration_status","skipped_no_full_replay_artifact")
        _set_phase("skipped_no_full_replay_artifact")
        print(
            f"SKIP | FUSION gate calibration | full replay artifact unavailable | {reason}",
            flush=True,
        )
        return 0

    _set_phase("republish_full_replay_artifact")
    _write_fusion_sidecar(result,reused=True)
    set_runtime_state(SNAPSHOT_KEY,json.dumps(result,ensure_ascii=False,default=str))
    set_runtime_state(
        BASIS_KEY,
        json.dumps({
            "source":"FULL_HISTORICAL_REPLAY",
            "producer_version":historical._production_version(),
            "source_fingerprint":_source_fingerprint(),
            "artifact_generated_at_madrid":(
                (result.get("artifact_metadata") or {}).get("generated_at_madrid")
            ),
        },ensure_ascii=False,default=str),
    )
    set_runtime_state("fusion_gate_calibration_status","reused_full_replay")
    _set_phase("complete")
    if notify_reuse:
        historical._discord_send(_summary(result))
    print(json.dumps(result,ensure_ascii=False,default=str),flush=True)
    return 0


def main() -> int:
    rc=historical.main()
    if rc!=0:
        return int(rc)
    return run_fusion_only(notify_reuse=True)


if __name__=="__main__":
    raise SystemExit(main())
