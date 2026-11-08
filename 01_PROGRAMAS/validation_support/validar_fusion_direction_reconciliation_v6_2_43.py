from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def _load():
    sys.path.insert(0,str(ALERTS))
    path=ALERTS/"fusion_gate_calibration.py"
    spec=importlib.util.spec_from_file_location("fusion_gate_calibration_v643",path)
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _outcome():
    return {
        "entry_price":100.0,
        "exit_price":110.0,
        "return_long_pct":10.0,
        "mfe_long_pct":12.0,
        "mae_long_pct":-6.0,
    }


def main()->int:
    m=_load()

    # FUSION direction must win over source CORE direction. This represents a
    # real GEX_STANDALONE / GEX-owned-direction path that 6.2.42 discarded.
    canonical={
        ("2026-09-01","GEXS"):{
            "id":101,
            "ticker":"GEXS",
            "snapshot_date":"2026-09-01",
            "core_direction":"LONG",
        },
        ("2026-09-02","DIV"):{
            "id":102,
            "ticker":"DIV",
            "snapshot_date":"2026-09-02",
            "core_direction":"LONG",
        },
    }
    signals={
        ("2026-09-01","GEXS","short"):[
            {"layer":"GEX_INTRADAY","direction":"short","state":"ENTER"},
            {
                "layer":"FUSION","direction":"short","state":"EARLY","score":88.0,
                "actionable":0,"signal_origin":"GEX_STANDALONE",
                "gex_intent":"STABILIZING",
            },
        ],
        ("2026-09-02","DIV","short"):[
            {"layer":"GEX_INTRADAY","direction":"short","state":"ENTER"},
            {
                "layer":"FUSION","direction":"short","state":"DIVERGENCE","score":81.0,
                "actionable":0,"signal_origin":"CORE_GEX_DIVERGENCE",
                "blockers":["CORE/GEX divergen"],
            },
        ],
    }
    outcomes={101:_outcome(),102:_outcome()}
    rows,coverage=m._rows_from_outcomes_with_coverage(canonical,signals,outcomes)

    assert len(rows)==2
    by_ticker={row["ticker"]:row for row in rows}
    gexs=by_ticker["GEXS"]
    assert gexs["current_direction"]=="SHORT"
    assert gexs["direction_source"]=="FUSION_SIGNAL"
    assert gexs["source_core_direction"]=="LONG"
    assert gexs["fusion_signal_origin"]=="GEX_STANDALONE"
    assert gexs["fusion_state"]=="EARLY"
    assert gexs["challenger_pass"] is True
    assert gexs["mfe_pct"]==6.0
    assert gexs["mae_pct"]==12.0
    assert gexs["horizon_close_return_pct"]==-10.0

    div=by_ticker["DIV"]
    assert div["current_direction"]=="SHORT"
    assert div["fusion_state"]=="DIVERGENCE"
    assert div["explicit_safety_block"] is True

    assert coverage["status"]=="VALID_RECONCILED"
    assert coverage["gex_standalone_signals"]==1
    assert coverage["core_direction_disagreements"]==2
    assert coverage["post_timing_rows"]==2
    assert coverage["pre_timing_states"]=={"EARLY":1,"DIVERGENCE":1}

    # Exact invariant: current engine can only create FUSION ENTER after
    # same-direction intraday ENTER. A mature ENTER without Timing is invalid
    # historical reconstruction, never negative strategy evidence.
    broken_canonical={
        ("2026-09-03","BROKEN"):{
            "id":103,"ticker":"BROKEN","snapshot_date":"2026-09-03",
            "core_direction":"LONG",
        }
    }
    broken_signals={
        ("2026-09-03","BROKEN","short"):[
            {
                "layer":"FUSION","direction":"short","state":"ENTER","score":95.0,
                "actionable":1,"signal_origin":"GEX_STANDALONE",
            }
        ]
    }
    broken_rows,broken_coverage=m._rows_from_outcomes_with_coverage(
        broken_canonical,broken_signals,{103:_outcome()}
    )
    assert broken_rows==[]
    assert broken_coverage["status"]=="INVALID_COVERAGE_RECONCILIATION"
    invariant=broken_coverage["required_timing_invariant"]
    assert invariant["fusion_enter_expected"]==1
    assert invariant["fusion_enter_reconciled"]==0
    assert invariant["missing"]==1

    broken_result=m.build_fusion_gate_calibration_from_outcomes(
        broken_canonical,broken_signals,{103:_outcome()},lane_label="ROLLING_LATEST"
    )
    assert broken_result["status"]=="INVALID_COVERAGE_RECONCILIATION"
    assert broken_result["historical_followup_candidate"] is False
    assert broken_result["economic_truth"]["direction_source"]=="FUSION_SIGNAL"
    assert broken_result["economic_truth"]["direction_is_current_fusion_direction"] is True

    combined=m.combine_fusion_gate_calibrations(broken_result,broken_result)
    assert combined["status"]=="INVALID_COVERAGE_RECONCILIATION"
    assert combined["dual_lane_followup"] is False
    assert combined["next_step"]=="REPAIR_FUSION_HISTORY_COVERAGE_BEFORE_INTERPRETATION"

    # Engine semantics that justify the exact reconciliation rule.
    engine=(ALERTS/"fusion_engine.py").read_text(encoding="utf-8-sig")
    assert 'fusion_direction = str((gex or core or graph or {}).get("direction") or "")' in engine
    assert 'str((timing or {}).get("state") or "WAIT") == "ENTER"' in engine
    assert '"signal_origin": origin' in engine

    calibration=(ALERTS/"fusion_gate_calibration.py").read_text(encoding="utf-8-sig")
    assert "def _fusion_signal_for_key" in calibration
    assert "def _rows_from_outcomes_with_coverage" in calibration
    assert '"direction_source":"FUSION_SIGNAL"' in calibration
    assert '"coverage_reconciliation_required"' in calibration

    runner=(ALERTS/"fusion_gate_research_runner.py").read_text(encoding="utf-8-sig")
    assert '"counterfactual_generation"' in runner
    assert '"fusion_enter_actionable"' in runner
    assert '"coverage_reconciliation"' in runner

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,43),version

    print(
        "PASS | >=6.2.43 | FUSION direction linkage uses actual FUSION signal | "
        "GEX_STANDALONE preserved | ENTER/RETEST timing reconciliation exact | LIVE unchanged"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
