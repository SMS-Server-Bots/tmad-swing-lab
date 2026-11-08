from __future__ import annotations

from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

import config
from autonomous_research_cache import write_fast_research_cache
from autonomous_research_lab import combine_autonomous_research_lanes
from core_context_precision_optimizer import _fast_cache_contract_hash
import run_fast_autonomous_research as runner


def _rows() -> tuple[list[dict], set[str], set[str], set[str]]:
    rows: list[dict] = []
    dates = [f"2026-02-{day:02d}" for day in range(1, 17)]
    for day in dates:
        for ticker_idx in range(30):
            ticker = f"S{ticker_idx:02d}"
            if ticker_idx < 8:
                value = 2.0
                long_path = {
                    "mfe_pct": 8.0,
                    "mae_pct": 1.0,
                    "horizon_close_return_pct": 5.0,
                }
                mover = True
            elif ticker_idx < 10:
                value = 1.0
                long_path = {
                    "mfe_pct": 2.0,
                    "mae_pct": 2.0,
                    "horizon_close_return_pct": -1.0,
                }
                mover = False
            else:
                value = -1.0
                long_path = {
                    "mfe_pct": 1.0,
                    "mae_pct": 3.0,
                    "horizon_close_return_pct": -1.0,
                }
                mover = False

            common = {
                "date": day,
                "ticker": ticker,
                "stage": {"core": True},
                "current_direction": "LONG",
                "research_rank": [ticker_idx],
                "long_path": long_path,
                "short_path": {
                    "mfe_pct": 1.0,
                    "mae_pct": 4.0,
                    "horizon_close_return_pct": -2.0,
                },
                "best_mover_5d": mover,
                "best_direction": "LONG" if mover else "",
            }
            for direction in ("LONG", "SHORT"):
                aligned = value if direction == "LONG" else -2.0
                features = {}
                for block, count in {
                    "node": 5,
                    "market": 7,
                    "sector": 4,
                    "relation": 4,
                    "interaction": 8,
                }.items():
                    for idx in range(count):
                        features[f"{block}.signal_{idx}"] = aligned * (1.0 - idx * 0.01)
                rows.append({
                    **common,
                    "context_direction": direction,
                    "context_features": features,
                    "context_diagnostics": {
                        "explicit_sector_etf": True,
                        "explicit_main_etf_count": 4,
                        "explicit_vol_count": 3,
                        "market_regime": "TEST",
                    },
                    "context_usable": True,
                    "directional_mover_5d": bool(mover and direction == "LONG"),
                })

    return rows, set(dates[:8]), set(dates[8:12]), set(dates[12:])

def _mock_lane(selected: str, shared: str, precision: float) -> dict:
    def row(name: str, worst: float) -> dict:
        return {
            "candidate_name": name,
            "spec": {
                "limits": {"node": 3, "market": 4, "sector": 2, "relation": 1, "interaction": 4},
                "daily_capacity": 5,
                "drop_top": {},
            },
            "stability": {
                "pass": True,
                "min_precision_lift_pp": worst,
                "min_profit_factor": 1.8,
                "min_return_pct": 1.5,
                "precision_lift_range_pp": 12.0,
            },
            "validation": {
                "challenger": {
                    "precision_pct": precision,
                    "horizon_close_profit_factor": 2.1,
                    "avg_horizon_close_return_pct": 2.2,
                }
            },
        }
    return {
        "status": "EVALUABLE",
        "selected_candidate": selected,
        "screen_survivor": True,
        "historical_followup_candidate": False,
        "stability_candidates": [
            row(selected, 4.0),
            row(shared, 8.0),
        ],
    }


def main() -> int:
    rows, train_dates, validation_dates, test_dates = _rows()
    contract = _fast_cache_contract_hash()

    # Cross-lane consensus must not require both lanes to have the same local winner.
    combined = combine_autonomous_research_lanes(
        _mock_lane("ROLLING_LOCAL", "SHARED_ROBUST", 55.0),
        _mock_lane("FIXED_LOCAL", "SHARED_ROBUST", 54.0),
    )
    assert combined["same_individual_winner_across_lanes"] is False
    assert combined["dual_lane_screen_survivor"] is True
    assert combined["dual_lane_consensus_candidate"] == "SHARED_ROBUST"
    assert combined["next_step"] == "EXPLICIT_FINAL_TEST_REQUIRED"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = root / "04_RUNTIME"
        output = root / "03_DATOS" / "alerts_bot" / "output"
        cache_root = runtime / "autonomous_research" / "cache"
        runtime.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)

        config.RUNTIME_ROOT = runtime
        config.OUTPUT_DIR = output
        runner.config.RUNTIME_ROOT = runtime
        runner.config.OUTPUT_DIR = output

        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            status = write_fast_research_cache(
                rows=rows,
                train_dates=train_dates,
                validation_dates=validation_dates,
                test_dates=test_dates,
                lane_label=lane,
                producer_version="6.2.52",
                contract_hash=contract,
                cache_root=cache_root,
                compression_level=6,
            )
            assert status["status"] == "OK"

        screen = runner.run_fast_research(
            lanes=["ROLLING_LATEST", "FIXED_BENCHMARK"],
            unlock_final_test=False,
            max_candidates=48,
            top_report=12,
            persist=True,
        )
        assert screen["run_mode"] == "SCREEN_ONLY"
        assert screen["decision"]["dual_lane_screen_survivor"] is True
        assert screen["decision"]["dual_lane_consensus_candidate"]
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            result = screen["lanes"][lane]
            assert result["final_test"] == {}
            assert result["final_test_status"] == "LOCKED"
            assert result["stability_survivor_count"] >= 1
            assert result["selected_stability"]["pass"] is True
            assert result["generation_counts"]["generation_3"] >= 1
            assert result["stability_candidates"]
            for candidate in result["stability_candidates"]:
                assert candidate["stability"]["pass"] is True

        consensus = screen["decision"]["dual_lane_consensus_candidate"]
        unlocked = runner.run_fast_research(
            lanes=["ROLLING_LATEST", "FIXED_BENCHMARK"],
            unlock_final_test=True,
            max_candidates=48,
            top_report=12,
            persist=True,
        )
        assert unlocked["decision"]["dual_lane_final_survivor"] is True
        assert unlocked["decision"]["dual_lane_consensus_candidate"] == consensus
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            result = unlocked["lanes"][lane]
            assert result["candidate_name"] == consensus
            assert result["final_test_status"] == "EVALUATED"
            assert result["historical_followup_candidate"] is True
            assert result["final_test_access"]["selection_stable"] is True
            assert result["final_test_access"]["final_gate_pass"] is True

        repeated = runner.run_fast_research(
            lanes=["ROLLING_LATEST", "FIXED_BENCHMARK"],
            unlock_final_test=True,
            max_candidates=48,
            top_report=12,
            persist=False,
        )
        assert repeated["decision"]["dual_lane_final_survivor"] is False
        assert repeated["decision"]["final_test_access"]["status"] == "DENIED_ALREADY_ACCESSED"
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            assert repeated["lanes"][lane]["final_test_access"]["status"] == "DENIED_ALREADY_ACCESSED"

        ledger = runtime / "autonomous_research" / "final_test_access.jsonl"
        assert ledger.is_file()
        ledger_rows = [
            line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(ledger_rows) >= 4  # RESERVED + COMPLETED for each lane.

    lab = (ALERTS / "autonomous_research_lab.py").read_text(encoding="utf-8-sig")
    assert "STABILITY_FOLDS = 3" in lab
    assert "def _generation_three(" in lab
    assert "def _dual_lane_consensus(" in lab
    assert "evaluate_explicit_candidate_final_test" in lab
    fast = (ALERTS / "run_fast_autonomous_research.py").read_text(encoding="utf-8-sig")
    assert "dual_lane_consensus_required_for_final_test" in fast
    assert "lockbox_reserved_before_read" in fast
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 52), version

    print(
        "PASS | >=6.2.52 | stability-driven Generation 3 | "
        "3-fold validation + cross-lane consensus + paired lockbox FINAL TEST | "
        "LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
