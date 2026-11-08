from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import sys

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def _load():
    sys.path.insert(0,str(ALERTS))
    path=ALERTS/"core_context_precision_optimizer.py"
    spec=importlib.util.spec_from_file_location("core_context_precision_optimizer_v648",path)
    assert spec and spec.loader
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _source(run_id: int, ticker: str, features: dict) -> dict:
    return {
        "snapshot_date":"2026-09-01",
        "ticker":ticker,
        "source_scanner_run_id":run_id,
        "features_json":json.dumps(features,separators=(",",":")),
    }


def _context_fixture(m):
    run=10
    candidate={
        "momentum_score":80,
        "scanner_trend_score":60,
        "change_percent":3.0,
        "flow_direction":1,
        "long_score":82,
        "rvol":2.0,
        "rvol_percentile":85,
        "change_percentile":80,
        "market_regime":"RISK_ON_TREND",
        "market_context_score":40,
        "market_breadth_score":30,
        "credit_risk_on_score":20,
        "market_deterioration_score":4,
        "vix_stress_score":8,
        "sector":"Technology",
        "industry":"Software",
        "sector_etf":"XLK",
        "sector_etf_score":45,
        "sector_breadth_score":25,
        "sector_deterioration_score":2,
        "relative_strength_vs_sector":18,
        "relationship_context_score":22,
        "relationship_lead_lag_score":16,
        "leveraged_confirmation_score":12,
        "graph_context_score":28,
        "relational_deterioration_score":3,
        "relative_strength_vs_market":15,
        "related_nodes":[
            {"ticker":"QQQ","score":35,"adjusted_score":35,"lead_lag_corr":0.6,"polarity":1},
            {"ticker":"UVIX","score":-30,"adjusted_score":30,"lead_lag_corr":-0.5,"polarity":-1},
        ],
    }
    def market(score):
        return {
            "scanner_trend_score":score,
            "flow_direction":1 if score>0 else -1,
            "change_percent":1.0 if score>0 else -1.0,
            "momentum_5d_pct":2.0 if score>0 else -2.0,
            "price_vs_vwap_pct":1.0 if score>0 else -1.0,
            "price_vs_sma20_pct":2.0 if score>0 else -2.0,
        }
    canonical={
        ("2026-09-01","AAA"):_source(run,"AAA",candidate),
        ("2026-09-01","SPY"):_source(run,"SPY",market(30)),
        ("2026-09-01","QQQ"):_source(run,"QQQ",market(35)),
        ("2026-09-01","IWM"):_source(run,"IWM",market(20)),
        ("2026-09-01","DIA"):_source(run,"DIA",market(15)),
        ("2026-09-01","UVIX"):_source(run,"UVIX",market(-40)),
        ("2026-09-01","XLK"):_source(run,"XLK",market(40)),
    }
    row={
        "date":"2026-09-01",
        "ticker":"AAA",
        "current_direction":"LONG",
        "current_direction_known":True,
        "direction_features":{},
    }
    features,diag=m._context_features(row,canonical,"LONG")
    assert diag["usable"] is True
    assert diag["same_run_context_required"] is True
    assert diag["explicit_main_etf_count"]==4
    assert diag["explicit_vol_count"]==1
    assert diag["explicit_sector_etf"] is True
    assert features["market.uvix_aligned"]>0
    assert features["sector.sector_etf_explicit_aligned"]>0

    # Same date but wrong run must not be accepted as contemporaneous context.
    canonical[("2026-09-01","UVIX")]=_source(run+1,"UVIX",market(-40))
    features2,diag2=m._context_features(row,canonical,"LONG")
    assert diag2["explicit_vol_count"]==0
    assert diag2["vol_context_available"] is False
    assert diag2["usable"] is False
    assert features2=={}


def _synthetic_rows():
    rows=[]
    for day_i in range(40):
        day=f"2026-07-{day_i+1:02d}" if day_i<31 else f"2026-08-{day_i-30:02d}"
        for i in range(10):
            mover=i<4
            signal=2.0 if mover else -2.0
            # Deliberately rank non-movers highest in current CORE control.
            current_rank=(100.0-i if not mover else float(i),50.0,1)
            long_path={
                "mfe_pct":7.0 if mover else 1.0,
                "mae_pct":1.0 if mover else 3.0,
                "horizon_close_return_pct":4.0 if mover else -2.0,
            }
            short_path={
                "mfe_pct":1.0,
                "mae_pct":3.0,
                "horizon_close_return_pct":-2.0,
            }
            rows.append({
                "date":day,
                "ticker":f"T{i:02d}",
                "best_mover_5d":mover,
                "best_direction":"LONG",
                "current_direction":"LONG",
                "current_direction_known":True,
                "context_direction":"LONG",
                "context_usable":True,
                "stage":{"core":True},
                "research_rank":current_rank,
                "long_path":long_path,
                "short_path":short_path,
                "context_features":{
                    "node.momentum_score_aligned":signal,
                    "node.change_percent_aligned":signal,
                    "node.rvol":3.0 if mover else 0.8,
                    "market.spy_aligned":signal,
                    "market.qqq_aligned":signal,
                    "market.uvix_aligned":signal,
                    "market.vix_stress_score_aligned":signal,
                    "sector.sector_etf_explicit_aligned":signal,
                    "sector.relative_strength_vs_sector_aligned":signal,
                    "relation.relationship_context_score_aligned":signal,
                    "relation.related_aligned_mean":signal,
                    "interaction.momentum_x_market_context":signal,
                    "interaction.momentum_x_uvix":signal,
                    "interaction.momentum_x_sector_etf":signal,
                },
                "context_diagnostics":{
                    "explicit_main_etf_count":4,
                    "explicit_vol_count":2,
                    "explicit_sector_etf":True,
                },
            })
    return rows


def main()->int:
    m=_load()
    _context_fixture(m)

    # Lower synthetic thresholds only for the unit fixture; production constants
    # remain fixed in the module and are checked below.
    m.MIN_FEATURE_SAMPLE=10
    m.MIN_FEATURE_COVERAGE_PCT=10.0

    rows=_synthetic_rows()
    train_dates,valid_dates,test_dates=m._split_dates(rows)
    train=[r for r in rows if r["date"] in train_dates]
    valid=[r for r in rows if r["date"] in valid_dates]
    test=[r for r in rows if r["date"] in test_dates]
    assert len(train_dates)==20 and len(valid_dates)==10 and len(test_dates)==10

    fitted=m._fit_train_models(train)
    models=m._family_models(fitted,"CONTEXT_BALANCED")
    assert models["node"] and models["market"] and models["sector"] and models["relation"]
    assert models["interaction"]

    train_view=m._comparison(train,models,5)
    valid_view=m._comparison(valid,models,5)
    final_view=m._comparison(test,models,5)
    for view in (train_view,valid_view,final_view):
        assert view["gate"]["pass"] is True
        assert view["challenger"]["precision_pct"]>view["control"]["precision_pct"]
        assert view["challenger"]["horizon_close_profit_factor"]>1.0
        assert view["selection"]["daily_capacity"]==5
        period_dates=(
            train_dates if view is train_view else
            valid_dates if view is valid_view else
            test_dates
        )
        assert view["selection"]["capacity_total"]==5*len(period_dates)
        assert "challenger_selection_current_direction" in view
        assert "selection_only_precision_pp" in view["attribution"]
        assert "direction_increment_precision_pp" in view["attribution"]

    rolling={
        "status":"EVALUABLE",
        "selected_candidate":"CONTEXT_BALANCED@TOP5",
        "historical_followup_candidate":True,
    }
    fixed=dict(rolling)
    decision=m.combine_core_context_precision_optimizers(rolling,fixed)
    assert decision["dual_lane_followup"] is True
    assert decision["next_step"]=="FREEZE_CONTEXT_RULE_AND_PROSPECTIVE_SHADOW"

    source=(ALERTS/"core_context_precision_optimizer.py").read_text(encoding="utf-8-sig")
    assert 'DAILY_CAPACITIES = (1, 3, 5, 10)' in source
    assert '"precision_prioritized_over_recall": True' in source
    assert '"current_core_capacity_is_not_a_constraint": True' in source
    assert '"main_etf_context_required": True' in source
    assert '"volatility_context_included": True' in source
    assert '"current_core_direction_is_control_only": True' in source
    assert '"long_short_hypotheses_scored_independently": True' in source
    assert '"new_context_interactions_allowed": True' in source
    assert "momentum_x_uvix" in source
    assert "momentum_x_sector_etf" in source
    assert "expected_run_id=run_id" in source
    assert "explicit_main_count >= 2" in source
    assert "explicit_vol_count >= 1" in source

    historical=(ALERTS/"historical_operational_validation.py").read_text(encoding="utf-8-sig")
    assert "build_core_context_precision_optimizer" in historical
    assert '"core_context_precision_optimizer": core_context_precision_optimizer' in historical
    assert "latest_core_context_precision_optimizer.json" in historical

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,48),version

    print(
        "PASS | >=6.2.48 | context-aware precision-first CORE optimizer | "
        "same-run SPY/QQQ/IWM/DIA + VIX-family + sector/relations required | "
        "train/validation/final-test | LIVE unchanged"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
