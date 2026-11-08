from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import config
from db import connect
from fusion_gate_calibration import (
    build_fusion_gate_calibration_from_outcomes,
    combine_fusion_gate_calibrations,
)
from historical_operational_validation import (
    _counterfactual_current_policy_inputs,
    _signals_index,
)
from research_benchmark import operational_run_key_set, feature_observation_id_set


def _mature_h5_outcomes(canonical: dict[tuple[str, str], dict]) -> tuple[dict[int, dict], dict]:
    observation_ids=sorted({
        int(row.get("id") or row.get("observation_id") or 0)
        for row in canonical.values()
        if int(row.get("id") or row.get("observation_id") or 0)>0
    })
    if not observation_ids:
        return {}, {"requested_observations":0,"matched_observations":0,"matched_pct":None,"horizon_sessions":5}

    selected:dict[int,dict]={}
    conn=connect(config.ALERTS_DB,readonly=True)
    try:
        for start in range(0,len(observation_ids),400):
            batch=observation_ids[start:start+400]
            placeholders=",".join("?" for _ in batch)
            rows=conn.execute(
                f"""SELECT id,observation_id,ticker,observation_date,horizon_sessions,
                           entry_price,exit_date,exit_price,return_long_pct,
                           mfe_long_pct,mae_long_pct,complete,labeled_at_utc
                    FROM observation_outcomes
                    WHERE complete=1 AND horizon_sessions=5
                      AND observation_id IN ({placeholders})
                    ORDER BY observation_id,id DESC""",
                batch,
            ).fetchall()
            for raw in rows:
                row=dict(raw)
                oid=int(row.get("observation_id") or 0)
                if oid>0 and oid not in selected:
                    selected[oid]=row
    finally:
        conn.close()

    return selected,{
        "requested_observations":len(observation_ids),
        "matched_observations":len(selected),
        "matched_pct":round(100.0*len(selected)/len(observation_ids),3) if observation_ids else None,
        "horizon_sessions":5,
        "source":"mature_observation_outcomes_aggregate_columns",
        "future_prices_are_labels_only":True,
    }


def run_fusion_gate_research(
    research_benchmark: dict | None,
    research_feature_benchmark: dict | None,
    *,
    progress: Callable[[str], None] | None = None,
    write_sidecar: bool = True,
) -> dict:
    def mark(phase: str) -> None:
        if progress is not None:
            progress(phase)

    mark("inputs_rolling")
    rolling_signals, rolling_features, rolling_diag = _counterfactual_current_policy_inputs(
        lane_label="ROLLING_LATEST"
    )

    mark("inputs_benchmark")
    fixed_keys = operational_run_key_set(research_benchmark or {})
    fixed_ids = feature_observation_id_set(research_feature_benchmark or {})
    rolling_keys = {
        (str(r.get("date") or "")[:10], int(r.get("source_scanner_run_id") or 0))
        for r in (rolling_diag.get("selected_run_keys") or [])
        if str(r.get("date") or "")[:10] and int(r.get("source_scanner_run_id") or 0) > 0
    }
    rolling_ids = {
        int(r.get("id") or r.get("observation_id") or 0)
        for r in rolling_features
        if int(r.get("id") or r.get("observation_id") or 0) > 0
    }
    reuse = bool(
        fixed_keys and fixed_keys == rolling_keys
        and (not fixed_ids or fixed_ids == rolling_ids)
    )
    if reuse:
        fixed_signals, fixed_features = rolling_signals, rolling_features
        fixed_diag = dict(rolling_diag)
        fixed_diag["lane"] = "FIXED_BENCHMARK"
        fixed_diag["reuses_rolling"] = True
    elif fixed_keys:
        fixed_signals, fixed_features, fixed_diag = _counterfactual_current_policy_inputs(
            fixed_keys, fixed_ids, lane_label="FIXED_BENCHMARK"
        )
    else:
        fixed_signals, fixed_features = [], []
        fixed_diag = {
            "status":"BENCHMARK_NOT_AVAILABLE",
            "lane":"FIXED_BENCHMARK",
            "fusion_signals":0,
            "fusion_enter_actionable":0,
        }

    mark("canonical_universe")
    def canonicalize(rows: list[dict]) -> dict[tuple[str, str], dict]:
        out: dict[tuple[str, str], dict] = {}
        for row in rows:
            day = str(row.get("snapshot_date") or row.get("signal_date") or "")[:10]
            ticker = str(row.get("ticker") or "").upper()
            if not day or not ticker:
                continue
            key = (day, ticker)
            old = out.get(key)
            run_id = int(row.get("source_scanner_run_id") or 0)
            if old is None or run_id >= int(old.get("source_scanner_run_id") or 0):
                out[key] = row
        return out

    rolling_canonical = canonicalize(rolling_features)
    fixed_canonical = canonicalize(fixed_features)

    mark("mature_outcomes")
    rolling_outcomes, rolling_outcome_diag = _mature_h5_outcomes(rolling_canonical)
    fixed_outcomes, fixed_outcome_diag = (
        _mature_h5_outcomes(fixed_canonical)
        if fixed_canonical else ({}, {"requested_observations":0,"matched_observations":0,"horizon_sessions":5})
    )

    mark("rolling_lane")
    rolling = build_fusion_gate_calibration_from_outcomes(
        rolling_canonical,
        _signals_index(rolling_signals),
        rolling_outcomes,
        lane_label="ROLLING_LATEST",
    )
    mark("fixed_lane")
    fixed = (
        build_fusion_gate_calibration_from_outcomes(
            fixed_canonical,
            _signals_index(fixed_signals),
            fixed_outcomes,
            lane_label="FIXED_BENCHMARK",
        )
        if fixed_canonical and fixed_signals
        else {
            "mode": "FUSION_GATE_CALIBRATION_V1",
            "lane": "FIXED_BENCHMARK",
            "status": "BENCHMARK_NOT_AVAILABLE",
            "live_mutation": False,
            "promotion_allowed": False,
        }
    )
    result = {
        "rolling_latest": rolling,
        "fixed_benchmark": fixed,
        "decision": combine_fusion_gate_calibrations(rolling, fixed),
        "input_diagnostics": {
            "rolling_feature_rows": len(rolling_features),
            "rolling_signal_rows": len(rolling_signals),
            "fixed_feature_rows": len(fixed_features),
            "fixed_signal_rows": len(fixed_signals),
            "fixed_reuses_rolling": reuse,
            "rolling_outcome_source": rolling_outcome_diag,
            "fixed_outcome_source": fixed_outcome_diag,
            "counterfactual_generation": {
                "rolling": {
                    "selected_dates": int(rolling_diag.get("selected_dates") or 0),
                    "feature_rows": int(rolling_diag.get("feature_rows") or len(rolling_features)),
                    "all_component_signals": int(rolling_diag.get("all_component_signals") or len(rolling_signals)),
                    "fusion_signals": int(rolling_diag.get("fusion_signals") or 0),
                    "fusion_enter_actionable": int(rolling_diag.get("fusion_enter_actionable") or 0),
                },
                "fixed": {
                    "selected_dates": int(fixed_diag.get("selected_dates") or 0),
                    "feature_rows": int(fixed_diag.get("feature_rows") or len(fixed_features)),
                    "all_component_signals": int(fixed_diag.get("all_component_signals") or len(fixed_signals)),
                    "fusion_signals": int(fixed_diag.get("fusion_signals") or 0),
                    "fusion_enter_actionable": int(fixed_diag.get("fusion_enter_actionable") or 0),
                },
            },
            "coverage_reconciliation": {
                "rolling": rolling.get("coverage_reconciliation") or {},
                "fixed": fixed.get("coverage_reconciliation") or {},
            },
            "fixed_feature_benchmark_is_input_freeze_only": True,
            "outcomes_may_mature_after_feature_benchmark": True,
        },
    }

    if write_sidecar:
        mark("write_sidecar")
        out = Path(config.OUTPUT_DIR) / "historical_validation"
        out.mkdir(parents=True, exist_ok=True)
        (out / "latest_fusion_gate_calibration.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    mark("lanes_complete")
    return result
