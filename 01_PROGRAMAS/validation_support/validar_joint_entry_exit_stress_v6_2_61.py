from __future__ import annotations

"""Synthetic, fail-closed regressions for detached joint-entry-exit lab."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "alerts_bot"))

from research_joint_entry_exit_stress import (
    _daily, _select, _simulate, _specs, _train_consensus, _id, SELECTIONS
)


def _rows() -> list[dict]:
    rows = []
    for day in ("2026-09-01", "2026-09-02"):
        for i in range(12):
            for side in ("LONG", "SHORT"):
                rows.append({
                    "date": day, "ticker": f"T{i:02d}",
                    "context_direction": side,
                    "current_direction": "LONG" if i % 2 == 0 else "SHORT",
                    "stage": {"core": i < 6},
                    "research_rank": [100-i],
                    "best_mover_5d": i % 3 == 0,
                    "context_usable": True,
                    "context_features": {
                        "node.test": float(12 - i) if side == "LONG" else float(i),
                        "market.test": 1.0,
                        "relation.test": 1.0,
                    },
                    "context_diagnostics": {"explicit_sector_etf": i == 1},
                    "long_path": {"mfe_pct": 6., "mae_pct": 2., "horizon_close_return_pct": 4.},
                    "short_path": {"mfe_pct": 2., "mae_pct": 6., "horizon_close_return_pct": -4.},
                })
    return rows


def _models() -> dict:
    return {
        group: [{
            "feature": feature,
            "cuts": [3.0, 6.0, 9.0],
            "bins": [{"score": -1.}, {"score": 0.}, {"score": 0.5}, {"score": 1.}],
        }]
        for group, feature in (
            ("node", "node.test"), ("market", "market.test"), ("relation", "relation.test")
        )
    }


def _paths() -> dict:
    result = {}
    for day in ("2026-09-01", "2026-09-02"):
        for i in range(12):
            candles = []
            # 10 genuine sequential business sessions, consistent OHLC.
            dates = ["2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08", "2026-09-09",
                     "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16"]
            price = 100.0
            for date in dates:
                close = price * 1.01
                candles.append({
                    "trade_date": date, "open": price, "high": close * 1.001,
                    "low": price * .999, "close": close, "source": "PRICE_CACHE"
                })
                price = close
            result[(f"T{i:02d}", day)] = candles
    return result


def main() -> int:
    dates, diag = _daily(_rows(), _models())
    assert diag["population_ticker_dates"] == 24
    assert diag["mover5d_best_direction_label"] == 8
    recorded = {("2026-09-01", "T00"): {"LONG"}, ("2026-09-02", "T00"): {"LONG"}}
    template = {"capacity": 5, "side": "CORE", "selection": "CORE_RANK",
                "stop_pct": 6.0, "horizon": 5, "policy": "TIME_5"}
    current = _select(dates, template, recorded, high_conf_threshold=0.0)
    assert len(current) == 10 and all(int(x["ticker"][1:]) < 6 for x in current)
    broad = _select(dates, {**template, "selection": "SCANNER_CONTEXT", "side": "CONTEXT"},
                    recorded, high_conf_threshold=0.0)
    assert len(broad) == 10
    strict = _select(dates, {**template, "selection": "ENTER_RECORDED"},
                     recorded, high_conf_threshold=0.0)
    assert len(strict) == 2 and all(x["direction"] == "LONG" for x in strict)
    assert len(_specs()) > 100

    # Counterfactual exits use observed *future* OHLC only after rule selection.
    metrics = _simulate(current, _paths(), template, cost_bps=20.0)
    assert metrics["evaluable"] == 5 and metrics["rejected"].get("PORTFOLIO_SLOTS_OCCUPIED") == 5, metrics
    assert metrics["net_avg_pct"] is not None
    assert metrics["reached_5pct_before_exit"] >= 0
    no_path = _simulate(current, {}, template, cost_bps=20.0)
    assert no_path["evaluable"] == 0 and no_path["rejected"], no_path
    bad_path = _paths()
    bad_path[("T00", "2026-09-01")][0]["high"] = None
    lost = _simulate(current, bad_path, template, cost_bps=20.0)
    assert lost["rejected"].get("INVALID_OR_CLOSE_ONLY_OHLC") == 1, lost

    # Train-consensus only uses TRAIN aggregates. Validation data is not supplied.
    specs = [template]
    ident = _id(template)
    sample = {"evaluable": 35, "dates": 14, "net_avg_pct": 1.2, "net_profit_factor": 1.5,
              "net_per_capital_session_pct": .25, "captured_rate_pct": 45}
    lane = {"specs": {ident: sample}}
    consensus = _train_consensus(lane, lane, specs)
    assert len(consensus) == 1 and consensus[0]["id"] == ident

    print("PASS | TMAD 6.2.61 | multi-gate entry x contextual direction x maturity/cost-aware exits; train-only shortlisting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
