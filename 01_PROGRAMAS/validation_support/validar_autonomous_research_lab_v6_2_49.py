from __future__ import annotations

from pathlib import Path
import importlib.util
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"


def _load(name: str, filename: str):
    path = ALERTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(prefix: str, dates: int = 12) -> list[dict]:
    out = []
    for i in range(dates):
        day = f"{prefix}-{i+1:02d}"
        for j in range(20):
            out.append({
                "date": day,
                "ticker": f"T{j:02d}",
                "directional_mover_5d": j < 8,
            })
    return out


def _fitted() -> dict:
    counts = {"node": 6, "market": 8, "sector": 5, "relation": 5, "interaction": 10}
    return {
        block: [{"feature": f"{block}.f{i}"} for i in range(count)]
        for block, count in counts.items()
    }


def _comparison(rows, models, daily_capacity):
    complexity = sum(len(models.get(block) or []) for block in ("node","market","sector","relation","interaction"))
    sample = max(30, len({r["date"] for r in rows}) * int(daily_capacity))
    precision = min(92.0, 62.0 + complexity * 0.75 - abs(int(daily_capacity) - 3) * 1.5)
    control_precision = 58.0
    avg_return = 2.0 + complexity * 0.08
    control_return = 1.5
    pf = 1.5 + complexity * 0.08
    control_pf = 1.3
    pass_gate = (
        sample >= 30
        and precision >= control_precision + 2.0
        and avg_return >= control_return
        and avg_return > 0
        and pf >= control_pf
        and pf > 1.0
    )
    return {
        "selection": {
            "daily_capacity": int(daily_capacity),
            "dates": len({r["date"] for r in rows}),
            "scoreable_rows": len(rows),
            "context_rejects": 0,
        },
        "control": {
            "sample": sample,
            "precision_pct": control_precision,
            "avg_horizon_close_return_pct": control_return,
            "horizon_close_profit_factor": control_pf,
            "avg_mae_pct": 2.0,
        },
        "challenger_selection_current_direction": {},
        "challenger": {
            "sample": sample,
            "precision_pct": round(precision, 3),
            "avg_horizon_close_return_pct": round(avg_return, 4),
            "horizon_close_profit_factor": round(pf, 4),
            "avg_mae_pct": 1.8,
        },
        "attribution": {"selection_only_precision_pp": precision-control_precision},
        "lift": {
            "precision_pp": round(precision-control_precision, 3),
            "return_pp": round(avg_return-control_return, 4),
            "pf": round(pf-control_pf, 4),
            "mae_pp": -0.2,
        },
        "gate": {
            "sample": sample >= 30,
            "precision_positive_lift": precision >= control_precision + 2.0,
            "return_not_worse": avg_return >= control_return,
            "return_positive": avg_return > 0,
            "pf_not_worse": pf >= control_pf,
            "pf_above_one": pf > 1.0,
            "mae_controlled": True,
            "pass": pass_gate,
        },
    }


def main() -> int:
    lab = _load("autonomous_research_lab_v6249", "autonomous_research_lab.py")
    train = _rows("2026-07", 12)
    valid = _rows("2026-08", 8)
    test = _rows("2026-09", 8)
    with tempfile.TemporaryDirectory() as tmp:
        registry = Path(tmp) / "experiments.sqlite"
        result = lab.run_autonomous_research_lane(
            train=train,
            validation=valid,
            test=test,
            fitted=_fitted(),
            comparison_fn=_comparison,
            lane_label="ROLLING_LATEST",
            registry_path=registry,
            max_candidates=48,
            top_report=10,
        )
        assert result["status"] == "EVALUABLE"
        assert result["candidate_count"] >= 40
        assert result["generation_counts"]["generation_1"] >= 40
        assert result["selected_candidate"]
        assert result["critic"]["pass"] is True
        assert result["final_test"]["gate"]["pass"] is True
        assert result["historical_followup_candidate"] is True
        assert result["registry"]["status"] == "OK"
        assert registry.exists()
        assert result["policy"]["final_test_touched_only_for_selected_candidate"] is True
        dual = lab.combine_autonomous_research_lanes(result, dict(result))
        assert dual["dual_lane_survivor"] is True
        assert dual["promotion_allowed"] is False
        assert dual["live_mutation"] is False

    source = (ALERTS / "core_context_precision_optimizer.py").read_text(encoding="utf-8-sig")
    assert "run_autonomous_research_lane" in source
    assert '"autonomous_research": autonomous_research' in source
    assert "combine_autonomous_research_lanes" in source
    config = (ALERTS / "config.py").read_text(encoding="utf-8-sig")
    assert "AUTONOMOUS_RESEARCH_ENABLED" in config
    assert "AUTONOMOUS_RESEARCH_MAX_CANDIDATES" in config
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 49), version

    print(
        "PASS | >=6.2.49 | autonomous research tournament | "
        "bounded candidates + generation-2 ablation + temporal critic + registry | "
        "FINAL TEST one winner only | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
