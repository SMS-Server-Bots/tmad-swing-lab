from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def _load():
    sys.path.insert(0,str(ALERTS))
    path=ALERTS/"fusion_gate_calibration.py"
    spec=importlib.util.spec_from_file_location("fusion_gate_calibration_v638",path)
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main()->int:
    m=_load()

    outcome={
        "entry_price":100.0,
        "exit_price":110.0,
        "return_long_pct":10.0,
        "mfe_long_pct":12.0,
        "mae_long_pct":-6.0,
    }
    long_path=m._direct_path_from_outcome(outcome,"LONG")
    short_path=m._direct_path_from_outcome(outcome,"SHORT")
    assert long_path["mfe_pct"]==12.0
    assert long_path["mae_pct"]==6.0
    assert long_path["horizon_close_return_pct"]==10.0
    assert short_path["mfe_pct"]==6.0
    assert short_path["mae_pct"]==12.0
    assert short_path["horizon_close_return_pct"]==-10.0

    canonical={
        ("2026-09-01","ABC"):{
            "id":101,
            "ticker":"ABC",
            "snapshot_date":"2026-09-01",
            "core_direction":"LONG",
        }
    }
    signals={
        ("2026-09-01","ABC","long"):[
            {"layer":"GEX_INTRADAY","direction":"long","state":"READY"},
            {"layer":"FUSION","direction":"long","state":"EARLY","score":87.0,"actionable":0},
        ]
    }
    rows=m._rows_from_outcomes(canonical,signals,{101:outcome})
    assert len(rows)==1
    row=rows[0]
    assert row["fusion_state"]=="EARLY"
    assert row["current_gate_pass"] is False
    assert row["challenger_pass"] is True
    assert row["mover_5d"] is True
    assert row["mfe_pct"]==12.0
    assert row["mae_pct"]==6.0
    assert row["horizon_close_return_pct"]==10.0
    assert row["outcome_source"]=="observation_outcomes_h5"

    runner=(ALERTS/"fusion_gate_research_runner.py").read_text(encoding="utf-8-sig")
    assert "_mature_h5_outcomes" in runner
    assert "horizon_sessions=5" in runner
    assert "mfe_long_pct" in runner
    assert "mae_long_pct" in runner
    assert "return_long_pct" in runner
    assert "_observation_outcome_price_series" not in runner
    assert "_trusted_reverse_path_series" not in runner
    assert "sessions_json" not in runner

    calibration=(ALERTS/"fusion_gate_calibration.py").read_text(encoding="utf-8-sig")
    assert "DIRECT_MATURE_OBSERVATION_OUTCOMES_V1" in calibration
    assert 'CHALLENGER="EARLY_RECOVERY_V1"' in calibration
    assert '"fusion_live_unchanged":True' in calibration

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,38),version

    print("PASS | >=6.2.38 | FUSION direct mature H5 outcomes | LONG/SHORT math verified | LIVE unchanged")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
