from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def _load():
    sys.path.insert(0,str(ALERTS))
    path=ALERTS/"fusion_gate_calibration.py"
    spec=importlib.util.spec_from_file_location("fusion_gate_calibration_v634",path)
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(idx:int,*,current:bool,state:str,mover:bool,ret:float,mae:float)->dict:
    return {
        "date":f"2026-09-{(idx%20)+1:02d}",
        "ticker":f"F{idx:03d}",
        "fusion_available":True,
        "fusion_state":state,
        "current_gate_pass":current,
        "challenger_pass":bool(current or state=="EARLY"),
        "explicit_safety_block":state in {"DIVERGENCE","MARKET_BLOCKED","EXTENDED","CAPPED"},
        "blockers":[],
        "mover_5d":mover,
        "mfe_pct":7.0 if mover else 2.0,
        "mae_pct":mae,
        "horizon_close_return_pct":ret,
        "first_hit_session":2 if mover else None,
    }


def main()->int:
    m=_load()
    assert m.MODE=="FUSION_GATE_CALIBRATION_V1"
    assert m.CHALLENGER=="EARLY_RECOVERY_V1"
    assert m.PASS_STATES=={"READY","RETEST","ENTER"}
    assert "DIVERGENCE" in m.BLOCK_STATES
    assert "MARKET_BLOCKED" in m.BLOCK_STATES

    rows=[]
    for i in range(20):
        mover=i<10
        rows.append(_row(i,current=True,state="READY",mover=mover,ret=2.0 if mover else -1.0,mae=1.2))
    for i in range(20,30):
        rows.append(_row(i,current=False,state="EARLY",mover=True,ret=3.0,mae=.8))
    for i in range(30,35):
        rows.append(_row(i,current=False,state="DIVERGENCE",mover=False,ret=-2.0,mae=2.0))

    result=m._comparison(rows)
    gate=result["gate"]
    assert gate["minimum_control_sample"]
    assert gate["minimum_challenger_sample"]
    assert gate["recovers_early_rows"]
    assert gate["recall_improves"]
    assert gate["precision_not_worse"]
    assert gate["return_not_worse"]
    assert gate["profit_factor_not_worse"]
    assert gate["mae_not_worse"]
    assert gate["pass"]
    assert result["state_economics"]["EARLY"]["sample"]==10

    module=(ALERTS/"fusion_gate_calibration.py").read_text(encoding="utf-8-sig")
    assert '"equal_capacity_ranker_retried":False' in module
    assert '"continuous_threshold_optimization":False' in module
    assert '"unknown_states_auto_admitted":False' in module
    assert '"fusion_live_unchanged":True' in module

    runner=(ALERTS/"fusion_gate_research_runner.py").read_text(encoding="utf-8-sig")
    assert "frozen" not in runner.lower() or "trusted" in runner.lower()
    assert "latest_fusion_gate_calibration.json" in runner
    assert "FIXED_BENCHMARK" in runner

    pipeline=(ALERTS/"historical_replay_pipeline.py").read_text(encoding="utf-8-sig")
    assert ("frozen_research_snapshot" in pipeline) or ("VALID_STABLE_READ_WINDOW" in pipeline) or ("FULL_HISTORICAL_REPLAY" in pipeline)
    assert "run_fusion_gate_research" in pipeline
    assert "fusion_gate_calibration_basis" in pipeline

    startup=(ALERTS/"fusion_gate_startup_lab.py").read_text(encoding="utf-8-sig")
    assert "historical_replay_status" in startup
    assert "run_fusion_only(notify_reuse=False)" in startup
    assert "_can_reuse" in pipeline
    assert "_basis()" in pipeline
    assert "def run_fusion_only(" in pipeline

    ps=(ALERTS/"start-bot.ps1").read_text(encoding="utf-8-sig")
    assert "fusion_gate_startup_lab.py" in ps
    assert "--ready-file" in ps
    assert "--replay-timeout" in ps
    assert "[FUSION LAB]" in ps

    exporter=(ROOT/"01_PROGRAMAS"/"observability"/"runtime_data_export.py").read_text(encoding="utf-8-sig")
    assert '"latest_fusion_gate_calibration.json"' in exporter

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,34),version

    print("PASS | >=6.2.34 | FUSION EARLY recovery fixed challenger | isolated research lab | LIVE unchanged")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
