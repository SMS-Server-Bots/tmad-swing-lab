from __future__ import annotations

from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

import config
from autonomous_research_cache import (
    load_fast_research_cache,
    write_fast_research_cache,
)
from core_context_precision_optimizer import _fast_cache_contract_hash
import run_fast_autonomous_research as runner


def _synthetic_rows() -> tuple[list[dict], set[str], set[str], set[str]]:
    rows: list[dict] = []
    dates = [f"2026-01-{day:02d}" for day in range(1, 17)]
    for day in dates:
        for ticker_idx in range(30):
            ticker = f"T{ticker_idx:02d}"
            # T00-T07 are clean LONG movers; T08-T09 are high-context false
            # positives; T20-T29 are deliberately ranked highest by CORE control.
            if ticker_idx < 8:
                long_feature = 2.0
                long_path = {
                    "mfe_pct": 8.0,
                    "mae_pct": 1.0,
                    "horizon_close_return_pct": 5.0,
                }
                mover = True
            elif ticker_idx < 10:
                long_feature = 1.0
                long_path = {
                    "mfe_pct": 2.0,
                    "mae_pct": 2.0,
                    "horizon_close_return_pct": -1.0,
                }
                mover = False
            else:
                long_feature = -1.0
                long_path = {
                    "mfe_pct": 1.0,
                    "mae_pct": 3.0,
                    "horizon_close_return_pct": -1.0,
                }
                mover = False
            short_path = {
                "mfe_pct": 1.0,
                "mae_pct": 4.0,
                "horizon_close_return_pct": -2.0,
            }

            common = {
                "date": day,
                "ticker": ticker,
                "stage": {"core": True},
                "current_direction": "LONG",
                "research_rank": [ticker_idx],
                "long_path": long_path,
                "short_path": short_path,
                "best_mover_5d": mover,
                "best_direction": "LONG" if mover else "",
            }
            for direction in ("LONG", "SHORT"):
                value = long_feature if direction == "LONG" else -2.0
                rows.append({
                    **common,
                    "context_direction": direction,
                    "context_features": {
                        "node.signal": value,
                        "market.signal": value,
                        "sector.signal": value,
                        "relation.signal": value,
                        "interaction.signal": value,
                    },
                    "context_diagnostics": {
                        "explicit_sector_etf": True,
                        "explicit_main_etf_count": 2,
                        "explicit_vol_count": 1,
                        "market_regime": "TEST",
                    },
                    "context_usable": True,
                    "directional_mover_5d": bool(mover and direction == "LONG"),
                })

    return rows, set(dates[:8]), set(dates[8:12]), set(dates[12:])


def main() -> int:
    rows, train_dates, validation_dates, test_dates = _synthetic_rows()
    contract = _fast_cache_contract_hash()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        runtime = tmp_root / "04_RUNTIME"
        output = tmp_root / "03_DATOS" / "alerts_bot" / "output"
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
                producer_version="6.2.51",
                contract_hash=contract,
                cache_root=cache_root,
                compression_level=6,
            )
            assert status["status"] == "OK"
            assert status["row_count"] == len(rows)
            assert status["train_dates"] == 8
            assert status["validation_dates"] == 4
            assert status["test_dates"] == 4
            payload = load_fast_research_cache(
                Path(status["path"]),
                expected_contract_hash=contract,
                verify_fingerprint=True,
            )
            assert payload["dataset_fingerprint"] == status["dataset_fingerprint"]

            mismatch_raised = False
            try:
                load_fast_research_cache(
                    Path(status["path"]),
                    expected_contract_hash="wrong-contract",
                    verify_fingerprint=True,
                )
            except ValueError as exc:
                mismatch_raised = "cache_contract_mismatch" in str(exc)
            assert mismatch_raised

        screen = runner.run_fast_research(
            lanes=["ROLLING_LATEST", "FIXED_BENCHMARK"],
            unlock_final_test=False,
            max_candidates=48,
            top_report=8,
            persist=True,
        )
        assert screen["run_mode"] == "SCREEN_ONLY"
        assert screen["decision"]["dual_lane_screen_survivor"] is True
        assert screen["decision"]["dual_lane_final_survivor"] is False
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            result = screen["lanes"][lane]
            assert result["screen_survivor"] is True
            assert result["final_test_status"] == "LOCKED"
            assert result["final_test"] == {}
            assert result["selected_candidate"]
            assert result["candidate_count"] >= 40

        unlocked = runner.run_fast_research(
            lanes=["ROLLING_LATEST", "FIXED_BENCHMARK"],
            unlock_final_test=True,
            max_candidates=48,
            top_report=8,
            persist=True,
        )
        assert unlocked["run_mode"] == "FINAL_TEST_UNLOCKED"
        assert unlocked["decision"]["dual_lane_final_survivor"] is True
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            result = unlocked["lanes"][lane]
            assert result["final_test_status"] == "EVALUATED"
            assert result["historical_followup_candidate"] is True
            assert result["final_test_access"]["selection_stable"] is True
            assert result["final_test_access"]["final_gate_pass"] is True

        # Same frozen lane cannot expose FINAL TEST a second time, even if the
        # researcher later changes candidate code or ranking.
        repeated = runner.run_fast_research(
            lanes=["ROLLING_LATEST", "FIXED_BENCHMARK"],
            unlock_final_test=True,
            max_candidates=48,
            top_report=8,
            persist=False,
        )
        for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK"):
            assert (
                repeated["lanes"][lane]["final_test_access"]["status"]
                == "DENIED_ALREADY_ACCESSED"
            )

        assert (runtime / "autonomous_research" / "final_test_access.jsonl").is_file()
        assert (runtime / "autonomous_research" / "fast_runs" / "latest_fast_research.json").is_file()
        assert (
            output
            / "historical_validation"
            / "latest_fast_autonomous_research.json"
        ).is_file()

    source = (ALERTS / "core_context_precision_optimizer.py").read_text(
        encoding="utf-8-sig"
    )
    assert "touch_final_test=False" in source
    assert "write_fast_research_cache" in source
    lab = (ALERTS / "autonomous_research_lab.py").read_text(encoding="utf-8-sig")
    assert "final_test_access_enabled_for_this_run" in lab
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 51), version

    print(
        "PASS | >=6.2.51 | frozen fast research cache | "
        "fingerprint+contract verified | screen lockbox protected | "
        "explicit one-time FINAL TEST | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
