from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

import config
from autonomous_research_cache import write_fast_research_cache
from core_context_precision_optimizer import _fast_cache_contract_hash
import adaptive_research_cycle as cycle


def _rows() -> tuple[list[dict], set[str], set[str], set[str]]:
    rows: list[dict] = []
    dates = [f"2026-03-{day:02d}" for day in range(1, 17)]
    for day in dates:
        for ticker_idx in range(30):
            ticker = f"A{ticker_idx:02d}"
            mover = ticker_idx < 8
            noisy = 8 <= ticker_idx < 10
            value = 2.0 if mover else (1.0 if noisy else -1.0)
            long_path = {
                "mfe_pct": 8.0 if mover else (2.0 if noisy else 1.0),
                "mae_pct": 1.0 if mover else (2.0 if noisy else 3.0),
                "horizon_close_return_pct": 5.0 if mover else -1.0,
            }
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
                    "node": 6,
                    "market": 8,
                    "sector": 5,
                    "relation": 5,
                    "interaction": 10,
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


def main() -> int:
    rows, train_dates, validation_dates, test_dates = _rows()
    contract = _fast_cache_contract_hash()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = root / "04_RUNTIME"
        output = root / "03_DATOS" / "alerts_bot" / "output"
        cache_root = runtime / "autonomous_research" / "cache"
        runtime.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)

        config.RUNTIME_ROOT = runtime
        config.OUTPUT_DIR = output
        cycle.config.RUNTIME_ROOT = runtime
        cycle.config.OUTPUT_DIR = output

        for lane in cycle.LANES:
            status = write_fast_research_cache(
                rows=rows,
                train_dates=train_dates,
                validation_dates=validation_dates,
                test_dates=test_dates,
                lane_label=lane,
                producer_version="6.2.53",
                contract_hash=contract,
                cache_root=cache_root,
                compression_level=6,
            )
            assert status["status"] == "OK"

        result = cycle.run_adaptive_validation_search(
            max_rounds=2,
            max_total_candidates=100,
            parents_per_round=3,
            children_per_parent=18,
            persist=True,
        )

        assert result["status"] == "EVALUABLE"
        assert result["final_test_status"] == "LOCKED"
        assert result["final_test"] == {}
        assert result["policy"]["final_test_never_read"] is True
        assert result["policy"]["same_specs_evaluated_both_lanes"] is True
        assert result["policy"]["worst_case_cross_lane_ranking"] is True
        assert result["budget"]["evaluated_adaptive_candidates"] > 0
        assert len(result["rounds"]) >= 1
        assert result["consensus_candidate"]
        assert result["common_survivor_count"] >= 1
        assert result["top_common_survivors"]
        for row in result["top_common_survivors"]:
            assert row["rolling_stability"]["pass"] is True
            assert row["fixed_stability"]["pass"] is True

        latest = (
            runtime
            / "autonomous_research"
            / "adaptive_cycles"
            / "latest_adaptive_cycle.json"
        )
        published = (
            output
            / "historical_validation"
            / "latest_adaptive_research_cycle.json"
        )
        assert latest.is_file()
        assert published.is_file()

    source = (ALERTS / "adaptive_research_cycle.py").read_text(encoding="utf-8-sig")
    assert "FINAL TEST" in source
    assert '"final_test_never_read": True' in source
    exporter = (
        ROOT / "01_PROGRAMAS" / "observability" / "runtime_data_export.py"
    ).read_text(encoding="utf-8-sig")
    assert '"latest_adaptive_research_cycle.json"' in exporter
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 53), version

    print(
        "PASS | >=6.2.53 | adaptive validation-only cache search | "
        "bounded multi-round mutations + same-spec dual-lane evaluation + "
        "worst-case consensus | FINAL TEST locked | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
