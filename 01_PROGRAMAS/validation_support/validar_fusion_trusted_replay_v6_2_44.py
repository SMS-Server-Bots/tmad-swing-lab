from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def _load():
    sys.path.insert(0,str(ALERTS))
    path=ALERTS/"fusion_gate_calibration.py"
    spec=importlib.util.spec_from_file_location("fusion_gate_calibration_v644",path)
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _series(start: str, closes: list[float], *, segment: int = 0):
    # Dates are fixed and ordered; first row is the signal-date anchor.
    days=[
        "2026-09-01","2026-09-02","2026-09-03",
        "2026-09-04","2026-09-05","2026-09-06",
    ]
    assert start=="2026-09-01"
    out=[]
    for day,close in zip(days,closes):
        out.append({
            "trade_date":day,
            "open":close,
            "high":close+1.0,
            "low":max(0.01,close-1.0),
            "close":close,
            "reverse_segment":segment,
        })
    return out


def main()->int:
    m=_load()

    canonical={
        ("2026-09-01","EARLY"):{
            "ticker":"EARLY","snapshot_date":"2026-09-01",
            "price":100.0,"core_direction":"LONG",
        },
        ("2026-09-01","ENTER"):{
            "ticker":"ENTER","snapshot_date":"2026-09-01",
            "price":100.0,"core_direction":"LONG",
        },
    }
    signals={
        ("2026-09-01","EARLY","long"):[
            {"layer":"GEX_INTRADAY","direction":"long","state":"READY"},
            {
                "layer":"FUSION","direction":"long","state":"EARLY",
                "score":88.0,"actionable":0,"signal_origin":"CORE_DIRECTIONAL",
            },
        ],
        ("2026-09-01","ENTER","short"):[
            {"layer":"GEX_INTRADAY","direction":"short","state":"ENTER"},
            {
                "layer":"FUSION","direction":"short","state":"ENTER",
                "score":96.0,"actionable":1,"signal_origin":"GEX_STANDALONE",
            },
        ],
    }
    series={
        "EARLY":_series("2026-09-01",[100,102,103,104,105,106]),
        "ENTER":_series("2026-09-01",[100,99,98,97,96,95]),
    }

    rows,coverage=m._rows_from_trusted_series_with_coverage(
        canonical,signals,series,
        expected_generation={"fusion_signals":2,"fusion_enter_state":1},
    )
    assert len(rows)==2
    assert coverage["status"]=="VALID_RECONCILED"
    assert coverage["label_source"]=="TRUSTED_REVERSE_SERIES_V1"
    assert coverage["fusion_signals"]==2
    assert coverage["complete_h5_fusion_signals"]==2
    assert coverage["post_timing_rows"]==2
    assert coverage["generation_reconciliation"]["valid"] is True
    assert coverage["generation_reconciliation"]["actual"]["fusion_enter_state"]==1
    assert coverage["gex_standalone_signals"]==1
    assert coverage["core_direction_disagreements"]==1
    assert coverage["required_timing_invariant"]["fusion_enter_expected"]==1
    assert coverage["required_timing_invariant"]["fusion_enter_reconciled"]==1
    assert coverage["required_timing_invariant"]["fusion_enter_h5_complete"]==1

    by_ticker={row["ticker"]:row for row in rows}
    early=by_ticker["EARLY"]
    assert early["fusion_state"]=="EARLY"
    assert early["challenger_pass"] is True
    assert early["outcome_source"]=="trusted_reverse_series_h5"
    assert early["mover_5d"] is True

    enter=by_ticker["ENTER"]
    assert enter["current_direction"]=="SHORT"
    assert enter["source_core_direction"]=="LONG"
    assert enter["current_gate_pass"] is True
    assert enter["fusion_signal_origin"]=="GEX_STANDALONE"
    assert enter["horizon_close_return_pct"]>0

    result=m.build_fusion_gate_calibration_from_trusted_series(
        canonical,signals,series,
        lane_label="ROLLING_LATEST",
        expected_generation={"fusion_signals":2,"fusion_enter_actionable":1},
    )
    assert result["economic_truth"]["outcome_mode"]=="TRUSTED_REVERSE_SERIES_V1"
    assert result["economic_truth"]["direction_source"]=="FUSION_SIGNAL"
    assert result["coverage_reconciliation"]["status"]=="VALID_RECONCILED"

    broken=m.build_fusion_gate_calibration_from_trusted_series(
        canonical,signals,series,
        lane_label="ROLLING_LATEST",
        expected_generation={"fusion_signals":3,"fusion_enter_state":1},
    )
    assert broken["status"]=="INVALID_COVERAGE_RECONCILIATION"
    assert broken["historical_followup_candidate"] is False
    assert broken["coverage_reconciliation"]["generation_reconciliation"]["valid"] is False

    # A continuity break cannot be silently crossed to manufacture H5.
    broken_series={"EARLY":_series("2026-09-01",[100,102,103,104,105,106])}
    for row in broken_series["EARLY"][3:]:
        row["reverse_segment"]=1
    one={("2026-09-01","EARLY"):canonical[("2026-09-01","EARLY")]}
    one_signals={("2026-09-01","EARLY","long"):signals[("2026-09-01","EARLY","long")]}
    no_rows,no_cov=m._rows_from_trusted_series_with_coverage(
        one,one_signals,broken_series,
        expected_generation={"fusion_signals":1,"fusion_enter_state":0},
    )
    assert no_rows==[]
    assert no_cov["complete_h5_fusion_signals"]==0
    assert no_cov["incomplete_h5_fusion_signals"]==1
    assert no_cov["policy"]["same_continuity_segment_required"] is True

    historical=(ALERTS/"historical_operational_validation.py").read_text(encoding="utf-8-sig")
    assert "build_fusion_gate_calibration_from_trusted_series" in historical
    assert "trusted_reverse_series" in historical
    assert "fixed_benchmark_trusted_series" in historical
    assert '"fusion_gate_calibration": fusion_gate_calibration' in historical
    assert 'latest_fusion_gate_calibration.json' in historical

    pipeline=(ALERTS/"historical_replay_pipeline.py").read_text(encoding="utf-8-sig")
    assert 'FUSION_OUTCOME_MODE="TRUSTED_REVERSE_SERIES_V1"' in pipeline
    assert "def _load_full_replay_fusion_result" in pipeline
    assert '"FULL_HISTORICAL_REPLAY"' in pipeline
    assert "skipped_no_full_replay_artifact" in pipeline
    run_block=pipeline.split("def run_fusion_only",1)[1].split("def main()",1)[0]
    assert "run_fusion_gate_research(" not in run_block
    assert "_load_full_replay_fusion_result()" in run_block

    replay=(ALERTS/"historical_replay_once.py").read_text(encoding="utf-8-sig")
    assert '"fusion_gate_calibration.py"' in replay

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,44),version

    print(
        "PASS | >=6.2.44 | FUSION uses FULL trusted replay H5 labels + actual "
        "FUSION direction + exact generation/timing reconciliation | post-lab republishes only | LIVE unchanged"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
