from __future__ import annotations

from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"


def _load_module():
    path = ALERTS / "gex_gate_calibration.py"
    sys.path.insert(0, str(ALERTS))
    spec = importlib.util.spec_from_file_location("gex_gate_calibration_v633", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(
    idx: int,
    *,
    current: bool,
    challenger: bool,
    mover: bool,
    ret: float,
    mae: float,
    contradiction: bool = False,
) -> dict:
    return {
        "date": f"2026-09-{(idx % 20) + 1:02d}",
        "ticker": f"T{idx:03d}",
        "gex_data_available": True,
        "current_gate_pass": current,
        "challenger_pass": challenger,
        "strong_opposite_contradiction": contradiction,
        "mover_5d": mover,
        "mfe_pct": 7.0 if mover else 2.0,
        "mae_pct": mae,
        "horizon_close_return_pct": ret,
        "first_hit_session": 2 if mover else None,
    }


def main() -> int:
    module = _load_module()

    assert module.MODE == "GEX_GATE_CALIBRATION_V1"
    assert module.CHALLENGER == "CONTRADICTION_ONLY_ABSTENTION_V1"

    assert module._strong_contradiction({
        "gex_intent": "DIRECTIONAL",
        "operational_target": True,
        "capped": False,
    })
    assert module._strong_contradiction({
        "gex_intent": "SQUEEZE",
        "operational_target": 1,
        "capped": 0,
    })
    assert not module._strong_contradiction({
        "gex_intent": "UNCLEAR",
        "operational_target": True,
        "capped": False,
    })
    assert not module._strong_contradiction({
        "gex_intent": "DIRECTIONAL",
        "operational_target": False,
        "capped": False,
    })

    # Control: 20 rows, 10 movers, mixed returns.
    rows = []
    for i in range(20):
        mover = i < 10
        rows.append(_row(
            i,
            current=True,
            challenger=True,
            mover=mover,
            ret=2.0 if mover else -1.0,
            mae=1.2,
        ))

    # Challenger recovers 10 current rejects; all are economically useful.
    for i in range(20, 30):
        rows.append(_row(
            i,
            current=False,
            challenger=True,
            mover=True,
            ret=3.0,
            mae=0.8,
        ))

    # Explicit strong contradictions remain blocked by the challenger.
    for i in range(30, 35):
        rows.append(_row(
            i,
            current=False,
            challenger=False,
            mover=False,
            ret=-2.0,
            mae=2.0,
            contradiction=True,
        ))

    comparison = module._comparison(rows)
    gate = comparison["gate"]
    assert gate["minimum_control_sample"]
    assert gate["minimum_challenger_sample"]
    assert gate["recovers_additional_rows"]
    assert gate["recall_improves"]
    assert gate["precision_not_worse"]
    assert gate["return_not_worse"]
    assert gate["profit_factor_not_worse"]
    assert gate["mae_not_worse"]
    assert gate["pass"]

    source = (ALERTS / "gex_gate_calibration.py").read_text(encoding="utf-8-sig")
    assert '"continuous_threshold_optimization": False' in source
    assert '"missing_gex_data_imputed": False' in source
    assert '"coverage_gap_excluded_from_decision": True' in source
    assert '"gex_live_unchanged": True' in source
    assert '"timing_live_unchanged": True' in source
    assert '"fusion_live_unchanged": True' in source
    assert '"lifecycle_live_unchanged": True' in source

    hist = (ALERTS / "historical_operational_validation.py").read_text(
        encoding="utf-8-sig"
    )
    assert "build_gex_gate_calibration" in hist
    assert "combine_gex_gate_calibrations" in hist
    assert "latest_gex_gate_calibration.json" in hist
    assert '"gex_gate_calibration": gex_gate_calibration' in hist

    replay = (ALERTS / "historical_replay_once.py").read_text(encoding="utf-8-sig")
    assert '"gex_gate_calibration.py"' in replay
    assert "GEX gate v6.2.33" in replay

    exporter = (
        ROOT / "01_PROGRAMAS" / "observability" / "runtime_data_export.py"
    ).read_text(encoding="utf-8-sig")
    assert '"latest_gex_gate_calibration.json"' in exporter

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 33), version

    print(
        "PASS | >=6.2.33 | GEX coverage separated from gate logic | "
        "fixed contradiction-only challenger | dual-lane research only | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
