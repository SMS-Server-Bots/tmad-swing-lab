from __future__ import annotations
"""Offline evidence of full-universe, direction-safe calibration with no slots."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "alerts_bot"))
from research_full_universe_calibration import _universe, _selected, evaluate_period, _frozen_validation_audit, _validation_lane_evidence, _dual_lane_validation_audit, ARMS

def _rows():
    result = []
    for date in ("2026-09-01", "2026-09-02", "2026-09-03"):
        for i in range(12):
            for side in ("LONG", "SHORT"):
                result.append({
                    "date": date, "ticker": "SYM" + str(i),
                    "context_direction": side,
                    "current_direction": "SHORT" if i % 2 else "LONG",
                    "stage": {"core": i < 4},
                    "research_rank": [100 - i],
                    "best_mover_5d": i % 3 == 0,
                    "directional_mover_5d": side == "LONG" and i % 3 == 0,
                    "context_usable": True,
                    "context_features": {"node.test": float(12 - i) if side == "LONG" else float(i),
                                         "market.test": 2.0, "relation.test": 2.0,
                                         "sector.test": 1.0, "interaction.test": .5},
                    "context_diagnostics": {"explicit_sector_etf": i % 2 == 0,
                                            "explicit_main_etf_count": 2,
                                            "explicit_vol_count": 1},
                    "gex_context": {"status": "SAME_RUN_DATE_ONLY" if i < 5 else "NO_SAME_RUN_GEX",
                                    "same_run": i < 5, "same_direction": i < 3,
                                    "opposite_direction": i == 3,
                                    "exact_timestamp_verified": False},
                    "long_path": {"mfe_pct": 7., "mae_pct": 2., "horizon_close_return_pct": 3.},
                    "short_path": {"mfe_pct": 2., "mae_pct": 6., "horizon_close_return_pct": -3.},
                })
    return result

def _models():
    return {group: [{"feature": feature, "cuts": [3., 6., 9.],
                     "bins": [{"score": -1.}, {"score": 0.},
                              {"score": 0.5}, {"score": 1.}]}]
            for group, feature in (
                ("node", "node.test"), ("market", "market.test"),
                ("relation", "relation.test"), ("sector", "sector.test"),
                ("interaction", "interaction.test")
            )}

def main():
    replay = (Path(__file__).resolve().parents[1] / "alerts_bot" / "historical_replay_once.py").read_text(encoding="utf-8-sig")
    for file in ("core_context_precision_optimizer.py", "autonomous_research_cache.py"):
        assert ('"' + file + '"') in replay, "New research cache contract must invalidate old FULL replay: " + file
    assert 'run_full_universe()' in replay
    assert "def _refresh_full_universe_calibration_if_stale()" in replay
    assert 'research_refresh = _refresh_full_universe_calibration_if_stale()' in replay, (
        "A REUSED economic replay must refresh stale research from cached rows"
    )
    assert 'if replay_mode in {"FULL", "PARTIAL_OPERATIONAL"}:' in replay
    assert "research_code_sha256" in replay and "REUSED_FROZEN_RESEARCH" in replay
    rows = _rows()
    pairs, population = _universe(rows)
    assert population["paired_ticker_dates"] == 36
    assert population["best_direction_movers_hindsight"] == 12
    assert population["gex_same_run_any_side"] == 15
    full, _ = _selected(pairs, _models(), "CONTEXT_FULL")
    assert len(full) == 36, len(full)
    assert {x["direction"] for x in full} <= {"LONG", "SHORT"}
    top5 = [x for x in full if x["daily_rank"] <= 5]
    assert len(top5) == 15, "Top5 selection must be independent of 5-slot concurrent swing cap"
    gex, _ = _selected(pairs, _models(), "GEX_SAME_RUN_SUPPORT")
    assert len(gex) <= 15 and len(gex) > 0
    veto, _ = _selected(pairs, _models(), "GEX_SAME_RUN_VETO")
    assert len(veto) > len(gex), (
        "A GEX contradiction-only veto must NOT reject candidates for missing GEX; "
        "previously both GEX arms were logically equivalent"
    )
    no_gex_keys = {("2026-09-01", "SYM" + str(i)) for i in range(5, 12)}
    assert no_gex_keys <= {(r["date"], r["ticker"]) for r in veto}
    assert not (no_gex_keys & {(r["date"], r["ticker"]) for r in gex})
    report = evaluate_period(rows, _models())
    assert set(report["arms"]) == set(ARMS)
    assert report["arms"]["CONTEXT_FULL"]["top_k_without_portfolio"]["5"]["selected"] == 15
    # Training winner loses on both validation lanes: unequivocal research rejection.
    def arm_result(precision, return_label):
        return {"top_k_without_portfolio": {"5": {
            "selected": 45, "dates": 9,
            "directional_precision_pct": precision,
            "average_gross_h5_close_pct": return_label,
        }}}
    lanes = {
        lane: {"validation": {"arms": {
            "CORE_CURRENT": arm_result(30., -1.),
            "CONTEXT_FULL": arm_result(60., 4.),
            "DROP_INTERACTION": arm_result(44., 2.),
        }}} for lane in ("ROLLING_LATEST", "FIXED_BENCHMARK")
    }
    rejected = _frozen_validation_audit(lanes, "DROP_INTERACTION")
    assert rejected["status"] == "REJECTED_VALIDATION", rejected
    assert all("WORSE_THAN_CONTEXT_FULL_DIRECTIONAL_PRECISION" in x["reasons"]
               for x in rejected["per_lane"].values())
    assert all("WORSE_THAN_CONTROL_H5_GROSS_LABEL" in x["reasons"]
               for x in rejected["per_lane"].values())
    for lane in lanes:
        lanes[lane]["validation"]["arms"]["DROP_INTERACTION"] = arm_result(70., 5.)
    survived = _frozen_validation_audit(lanes, "DROP_INTERACTION")
    assert survived["status"] == "SURVIVED_VALIDATION_FOR_FURTHER_RESEARCH_ONLY"
    assert not survived["promotion_allowed"]
    assert _frozen_validation_audit(lanes, None)["status"] == "NO_TRAIN_ELIGIBLE_CHALLENGER"
    # The same validation rows across Rolling/Fixed must be recognized as
    # overlapping, never counted as independent out-of-sample evidence.
    same_lane = _validation_lane_evidence(_rows(), _models(), {"2026-09-01"})
    twin_lane = _validation_lane_evidence(_rows(), _models(), {"2026-08-28"})
    dual = _dual_lane_validation_audit({
        "ROLLING_LATEST": same_lane, "FIXED_BENCHMARK": twin_lane,
    })
    assert dual["validation_overlap_dates"] == 3
    assert dual["overlap_ticker_date_pairs"] == 36
    assert dual["top5_common_selected_ticker_dates"] == 15
    assert dual["top5_direction_disagreements_for_common_selected"] == 0
    assert dual["overlap_label_disagreements"] == 0
    assert not dual["rolling_fixed_are_independent_oos"]
    assert dual["per_lane"]["ROLLING_LATEST"]["training_dates_also_in_other_validation"] == 1
    assert sum(x["selected"] for x in dual["per_lane"]["ROLLING_LATEST"]["top5_all_dates"]["by_direction"].values()) == 15
    # Different valid labeled samples must be counted as population changes
    # rather than wrongly interpreted as model deterioration alone.
    shorter_rows = [r for r in _rows() if r["ticker"] != "SYM0"]
    shorter = _validation_lane_evidence(shorter_rows, _models(), set())
    mixed = _dual_lane_validation_audit({
        "ROLLING_LATEST": same_lane, "FIXED_BENCHMARK": shorter,
    })
    assert mixed["overlap_ticker_date_pairs"] == 33
    assert mixed["union_ticker_date_pairs"] == 36
    assert mixed["validation_overlap_dates"] == 3
    loss = report["arms"]["CONTEXT_FULL"]["mover_loss_attribution_top5"]
    counts = loss["counts"]
    assert counts["all_paired"] == 36
    assert counts["all_hindsight_movers"] == 12
    assert counts["all_hindsight_movers"] == sum(counts.get(k, 0) for k in (
        "MISSED_UNSCOREABLE", "MISSED_BELOW_TOP5",
        "SELECTED_WRONG_DIRECTION", "CAPTURED_CORRECT_DIRECTION",
    )), "Hindsight movers must be accounted for once"
    assert sum(v["all_paired"] for v in loss["by_market_regime"].values()) == 36
    assert loss["by_context_availability"]["SECTOR_PRESENT"]["all_paired"] == 18
    assert loss["by_context_availability"]["GEX_SAME_RUN"]["all_paired"] == 15
    base_delta = report["arms"]["CONTEXT_FULL"]["top5_delta_vs_context_full"]
    assert base_delta["added_ticker_dates"] == 0
    assert base_delta["removed_ticker_dates"] == 0
    assert base_delta["directional_hit_delta"] == 0
    for arm in ARMS:
        assert "top5_delta_vs_context_full" in report["arms"][arm]
        assert "mover_loss_attribution_top5" in report["arms"][arm]
    assert report["arms"]["CORE_CURRENT"]["all_scoreable_ticker_dates"] <= 12
    core, _ = _selected(pairs, _models(), "CORE_CURRENT")
    assert all(x["daily_rank"] == int(x["ticker"][3:]) + 1 for x in core), (
        "Historical CORE control must use its own recorded research_rank, not contextual re-ranking"
    )
    # Replace future labels only; point-in-time rank and direction must not move.
    other = _rows()
    for row in other:
        row["best_mover_5d"] = not row["best_mover_5d"]
        row["directional_mover_5d"] = not row["directional_mover_5d"]
        row["long_path"]["horizon_close_return_pct"] = -27.
        row["short_path"]["horizon_close_return_pct"] = 61.
    changed, _ = _universe(other)
    independent, _ = _selected(changed, _models(), "CONTEXT_FULL")
    assert [(x["date"], x["ticker"], x["direction"], x["daily_rank"])
            for x in full] == [
                (x["date"], x["ticker"], x["direction"], x["daily_rank"])
                for x in independent], "Future labels have leaked into selection"
    assert all(
        arm["top_k_without_portfolio"]["5"]["no_portfolio_slot_filter"]
        for arm in report["arms"].values()
    )
    print("PASS | 6.2.66 | full ticker/date calibration, module ablation, GEX provenance, top5 without slots, future label isolation")

if __name__ == "__main__":
    main()
