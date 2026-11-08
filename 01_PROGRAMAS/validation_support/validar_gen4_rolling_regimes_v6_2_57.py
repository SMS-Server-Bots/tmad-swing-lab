from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

import config
from core_context_precision_optimizer import _fit_train_models
from regime_failure_research import run_gen4_failure_research


def _rows() -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    dates = [f"2026-04-{day:02d}" for day in range(1, 19)]
    for day_idx, day in enumerate(dates):
        regime = "RISK_ON_TREND" if day_idx % 2 == 0 else "RISK_OFF_VOLATILITY"
        best_direction = "LONG" if regime == "RISK_ON_TREND" else "SHORT"
        vol_mag = 1.0 + float(day_idx % 3)
        for ticker_idx in range(40):
            ticker = f"G{ticker_idx:02d}"
            mover = ticker_idx < 10
            # Alternate CORE pass/reject inside the mover cohort so TOP5 tie
            # ordering cannot make the failure-map assertion tautological.
            core_pass = (ticker_idx % 2 == 0)
            if mover and best_direction == "LONG":
                long_path = {"mfe_pct": 8.0, "mae_pct": 1.0, "horizon_close_return_pct": 5.0}
                short_path = {"mfe_pct": 1.0, "mae_pct": 4.0, "horizon_close_return_pct": -2.0}
            elif mover:
                long_path = {"mfe_pct": 1.0, "mae_pct": 4.0, "horizon_close_return_pct": -2.0}
                short_path = {"mfe_pct": 8.0, "mae_pct": 1.0, "horizon_close_return_pct": 5.0}
            else:
                long_path = {"mfe_pct": 1.0, "mae_pct": 3.0, "horizon_close_return_pct": -1.0}
                short_path = {"mfe_pct": 1.0, "mae_pct": 3.0, "horizon_close_return_pct": -1.0}

            for direction in ("LONG", "SHORT"):
                correct_hypothesis = direction == best_direction
                aligned = (
                    3.0 if mover and correct_hypothesis
                    else (-3.0 if mover else -1.0)
                )
                broad = 2.0 if correct_hypothesis else -2.0
                features = {}
                for block, count in {
                    "node": 6,
                    "market": 8,
                    "sector": 5,
                    "relation": 5,
                    "interaction": 10,
                }.items():
                    for idx in range(count):
                        features[f"{block}.signal_{idx}"] = aligned * (1.0 - idx * 0.01)
                features.update({
                    "market.spy_aligned": broad,
                    "market.qqq_aligned": broad * 0.95,
                    "market.iwm_aligned": broad * 0.90,
                    "market.dia_aligned": broad * 0.85,
                    "market.vix_stress_score_aligned": (
                        vol_mag if direction == "SHORT" else -vol_mag
                    ),
                    "market.uvix_aligned": (
                        vol_mag if direction == "SHORT" else -vol_mag
                    ),
                    "sector.sector_etf_explicit_aligned": broad * 0.9,
                })
                rows.append({
                    "date": day,
                    "ticker": ticker,
                    "stage": {"core": core_pass},
                    "current_direction": best_direction if core_pass else "",
                    "research_rank": [100 - ticker_idx],
                    "long_path": long_path,
                    "short_path": short_path,
                    "best_mover_5d": mover,
                    "best_direction": best_direction if mover else "",
                    "context_direction": direction,
                    "context_features": features,
                    "context_diagnostics": {
                        "explicit_sector_etf": True,
                        "explicit_main_etf_count": 4,
                        "explicit_vol_count": 3,
                        "market_regime": regime,
                    },
                    "context_usable": True,
                    "directional_mover_5d": bool(mover and correct_hypothesis),
                })
    train_dates = set(dates[:12])
    validation_dates = set(dates[12:])
    return (
        [row for row in rows if row["date"] in train_dates],
        [row for row in rows if row["date"] in validation_dates],
    )


def main() -> int:
    train, validation = _rows()
    fitted = _fit_train_models(train)
    spec = {
        "generation": 1,
        "parent": "GEN4_TEST",
        "limits": {
            "node": 2,
            "market": 2,
            "sector": 1,
            "relation": 1,
            "interaction": 2,
        },
        "daily_capacity": 5,
        "drop_top": {},
    }
    leaderboard = [{
        "candidate_name": "TEST",
        "spec": spec,
        "validation": {
            "challenger": {
                "precision_pct": 50.0,
                "horizon_close_profit_factor": 1.5,
            }
        },
    }]
    base_results = {
        "ROLLING_LATEST": {
            "selected_spec": {},
            "leaderboard": leaderboard,
        },
        "FIXED_BENCHMARK": {
            "selected_spec": spec,
            "selected_candidate": "TEST_FIXED_SURVIVOR",
            "leaderboard": leaderboard,
        },
    }
    contexts = {
        lane: {
            "train": train,
            "validation": validation,
            "fitted": fitted,
        }
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK")
    }

    old_values = {
        "RUNTIME_ROOT": config.RUNTIME_ROOT,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "GEN4_MIN_SPECIALIST_ROWS": getattr(config, "GEN4_MIN_SPECIALIST_ROWS", None),
        "GEN4_MIN_CLASS_ROWS": getattr(config, "GEN4_MIN_CLASS_ROWS", None),
        "GEN4_MAX_BASE_SPECS": getattr(config, "GEN4_MAX_BASE_SPECS", None),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="tmad-gen4-v6257-") as tmp:
            root = Path(tmp)
            config.RUNTIME_ROOT = root / "04_RUNTIME"
            config.OUTPUT_DIR = root / "03_DATOS" / "alerts_bot" / "output"
            config.RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
            config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            config.GEN4_MIN_SPECIALIST_ROWS = 100
            config.GEN4_MIN_CLASS_ROWS = 10
            config.GEN4_MAX_BASE_SPECS = 2

            result = run_gen4_failure_research(
                lane_contexts=contexts,
                base_results=base_results,
                candidate_records={
                    "ROLLING_LATEST": {},
                    "FIXED_BENCHMARK": {},
                },
                persist=True,
            )

            assert result["status"] == "EVALUABLE"
            assert result["final_test_status"] == "LOCKED"
            assert result["final_test"] == {}
            assert result["policy"]["final_test_never_read"] is True
            assert result["policy"]["same_base_specs_both_lanes"] is True
            assert result["policy"]["regime_router_fit_on_train_only"] is True
            assert result["base_spec_count"] >= 1
            assert result["lane_summary"]["ROLLING_LATEST"]["candidate_count"] >= 2
            assert result["lane_summary"]["FIXED_BENCHMARK"]["candidate_count"] >= 2

            rolling = result["diagnostics"]["ROLLING_LATEST"]
            assert rolling["status"] == "EVALUABLE"
            assert rolling["reference_source"] == "FIXED_SURVIVOR_APPLIED_TO_ROLLING"
            assert rolling["true_mover_opportunities"] > 0
            assert rolling["missed_true_movers"] > 0
            assert rolling["missed_true_movers_core_rejected"] > 0
            assert rolling["fold_stability"]["fold_count"] == 3
            assert rolling["failure_map"]["market_regime"]
            assert rolling["failure_map"]["volatility_bucket"]
            assert rolling["failure_map"]["broad_etf_alignment"]
            assert rolling["failure_map"]["sector_etf_alignment"]
            assert rolling["failure_map"]["direction"]
            assert rolling["failure_map"]["core_stage"]

            published = (
                config.OUTPUT_DIR
                / "historical_validation"
                / "latest_gen4_failure_research.json"
            )
            assert published.is_file()
    finally:
        config.RUNTIME_ROOT = old_values["RUNTIME_ROOT"]
        config.OUTPUT_DIR = old_values["OUTPUT_DIR"]
        for key in ("GEN4_MIN_SPECIALIST_ROWS", "GEN4_MIN_CLASS_ROWS", "GEN4_MAX_BASE_SPECS"):
            value = old_values[key]
            if value is None:
                try:
                    delattr(config, key)
                except Exception:
                    pass
            else:
                setattr(config, key, value)

    adaptive = (ALERTS / "adaptive_research_cycle.py").read_text(encoding="utf-8-sig")
    gen4 = (ALERTS / "regime_failure_research.py").read_text(encoding="utf-8-sig")
    snapshot = (ALERTS / "research_snapshot.py").read_text(encoding="utf-8-sig")
    exporter = (
        ROOT / "01_PROGRAMAS" / "observability" / "runtime_data_export.py"
    ).read_text(encoding="utf-8-sig")

    assert "consensus is None" in adaptive
    assert "run_gen4_failure_research" in adaptive
    assert '"gen4_failure_driven_after_no_consensus": True' in adaptive
    assert "FINAL TEST is never read here" in gen4
    assert '"final_test_never_read": True' in gen4
    assert '"latest_gen4_failure_research.json"' in exporter

    row_verify = snapshot.index('_progress("row_count_verification", "running")')
    index_build = snapshot.index('index_started = time.perf_counter()')
    assert row_verify < index_build
    assert "EXACT_ROW_COUNTS_PLUS_SQLITE_INDEX_BUILD" in snapshot
    assert "FULL_INTEGRITY_CHECK_POST_INDEX" in snapshot
    # >=6.2.58 removes the routine full-file quick_check while preserving
    # diagnostic/forensic opt-ins; Gen4 must not depend on verification cost.
    assert "routine_quick_check=false" in snapshot

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 57), version

    print(
        "PASS | >=6.2.57 | Gen4 failure-driven Rolling diagnostics + "
        "regime/vol specialist routers | dual-lane TRAIN+VALIDATION only | "
        "FINAL TEST locked | snapshot verification forward-compatible | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
