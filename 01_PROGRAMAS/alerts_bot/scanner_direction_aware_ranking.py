from __future__ import annotations

"""TMAD 6.2.29 direction-aware Scanner calibration (research only).

6.2.27 proved that |change|+rvol improves mover recall but degrades directional
trade economics. This module tests a small fixed family of point-in-time
Scanner rankings that add directional coherence. No weights are optimized.
Chronological train+validation select a candidate; the final quarter is untouched.
Static mandatory names are preserved. Future prices are labels only. LIVE is unchanged.
"""

from collections import defaultdict
from statistics import mean
from typing import Any

from economic_architecture_challengers import _build_rows as _build_architecture_rows
from economic_waterfall_calibration import _feature_payload
from scanner_recall_ranking_calibration import (
    CAPACITY, _build_rows as _build_scanner_rows, _current_rank, _split_dates, _num,
)
from scanner_core_interaction_simulation import (
    _mandatory_preserving_rank, _selection_metrics, _direction_metrics,
)

MODE = "SCANNER_DIRECTION_AWARE_RANKING_V2"
TARGET_PCT = 5.0
HORIZON_SESSIONS = 5

CANDIDATE_FAMILIES = {
    "CHANGE_RVOL_COHERENCE": ("abs_change", "rvol", "coherence"),
    "BALANCED_COHERENCE": ("abs_change", "rvol", "abs_trend", "coherence"),
    "SIGNAL_COHERENCE": (
        "abs_change", "rvol", "abs_momentum", "abs_long_bias", "coherence",
    ),
    "ALIGNMENT_FOCUSED": (
        "abs_change", "rvol", "coherence", "change_momentum_agree", "change_trend_agree",
    ),
}


def _value(payload: dict, *keys: str) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def _sign(value: float | None) -> int:
    if value is None or abs(value) < 1e-12:
        return 0
    return 1 if value > 0 else -1


def _pit_features(payload: dict) -> dict[str, float | None]:
    change = _num(_value(payload, "change_percent", "changePercent"))
    momentum = _num(_value(payload, "momentum_score", "momentumScore"))
    trend = _num(_value(payload, "scanner_trend_score", "scannerTrendScore"))
    long_score = _num(_value(payload, "long_score", "longScore"))
    rvol = _num(_value(payload, "rvol", "relativeVolume", "relative_volume"))
    change = float(change) if change is not None else None
    momentum = float(momentum) if momentum is not None else None
    trend = float(trend) if trend is not None else None
    long_bias = float(long_score) - 50.0 if long_score is not None else None
    votes = [x for x in (_sign(change), _sign(momentum), _sign(trend), _sign(long_bias)) if x]
    coherence = abs(sum(votes)) / len(votes) if votes else None
    cs, ms, ts = _sign(change), _sign(momentum), _sign(trend)
    return {
        "abs_change": abs(change) if change is not None else None,
        "rvol": float(rvol) if rvol is not None else None,
        "abs_trend": abs(trend) if trend is not None else None,
        "abs_momentum": abs(momentum) if momentum is not None else None,
        "abs_long_bias": abs(long_bias) if long_bias is not None else None,
        "coherence": coherence,
        "change_momentum_agree": (
            1.0 if cs and ms and cs == ms else (0.0 if cs and ms else None)
        ),
        "change_trend_agree": (
            1.0 if cs and ts and cs == ts else (0.0 if cs and ts else None)
        ),
    }


def _rows(canonical: dict, series: dict) -> list[dict]:
    out = []
    for row in _build_scanner_rows(canonical, series):
        key = (str(row["date"]), str(row["ticker"]).upper())
        item = dict(row)
        item["dir_features"] = _pit_features(_feature_payload(canonical.get(key) or {}))
        out.append(item)
    return out


def _pct_map(rows: list[dict], feature: str) -> dict[str, float]:
    clean = []
    for row in rows:
        value = _num((row.get("dir_features") or {}).get(feature))
        if value is not None:
            clean.append((str(row["ticker"]), float(value)))
    if not clean:
        return {}
    unique = sorted({value for _ticker, value in clean})
    if len(unique) == 1:
        ranks = {unique[0]: 50.0}
    else:
        ranks = {value: 100.0 * i / (len(unique) - 1) for i, value in enumerate(unique)}
    return {ticker: ranks[value] for ticker, value in clean}


def _candidate_rank(rows: list[dict], features: tuple[str, ...]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["date"])].append(row)
    result = {}
    for day, day_rows in grouped.items():
        maps = {f: _pct_map(day_rows, f) for f in features}
        scored = []
        for row in day_rows:
            ticker = str(row["ticker"])
            vals = [maps[f][ticker] for f in features if ticker in maps.get(f, {})]
            if vals:
                scored.append({**row, "candidate_score": mean(vals)})
        scored.sort(key=lambda x: (float(x["candidate_score"]), str(x["ticker"])), reverse=True)
        result[day] = [{**row, "rank": i} for i, row in enumerate(scored, 1)]
    return result


def _selected(rows: list[dict], ranked: dict[str, list[dict]]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["date"])].append(row)
    out = []
    for day, day_rows in sorted(grouped.items()):
        out.extend(_mandatory_preserving_rank(day_rows, ranked.get(day, []), capacity=CAPACITY))
    return out


def _view(rows: list[dict], ranked: dict[str, list[dict]], arch: dict, all_movers: int) -> dict:
    selected = _selected(rows, ranked)
    return {
        "selection": _selection_metrics(selected, all_movers=all_movers),
        "direction": _direction_metrics(selected, arch, total_movers=all_movers),
    }


def _metric_compare(challenger_value, control_value, *, higher_is_better: bool, tol: float = 0.0) -> tuple[bool, str]:
    """Compare a challenger metric without treating missing baseline evidence as a regression."""
    challenger_num = _num(challenger_value)
    control_num = _num(control_value)
    if challenger_num is None and control_num is None:
        return True, "BOTH_UNDEFINED_NEUTRAL"
    if challenger_num is None:
        return False, "CHALLENGER_UNDEFINED_FAIL"
    if control_num is None:
        return True, "CONTROL_UNDEFINED_NEW_EVIDENCE"
    if higher_is_better:
        passed = float(challenger_num) + float(tol) >= float(control_num)
    else:
        passed = float(challenger_num) <= float(control_num) + float(tol)
    return bool(passed), ("COMPARABLE_PASS" if passed else "COMPARABLE_FAIL")


def _gate(control: dict, challenger: dict) -> dict:
    cs, ns = control["selection"], challenger["selection"]
    cc, nc = control["direction"]["current_core"], challenger["direction"]["current_core"]
    cv, nv = control["direction"]["core_v2_strict"], challenger["direction"]["core_v2_strict"]

    specs = {
        "precision": (ns.get("precision_pct"), cs.get("precision_pct"), True, 0.0),
        "recall": (ns.get("recall_pct"), cs.get("recall_pct"), True, 0.0),
        "current_return": (
            nc.get("avg_horizon_close_return_pct"),
            cc.get("avg_horizon_close_return_pct"), True, 0.0,
        ),
        "current_pf": (
            nc.get("horizon_close_profit_factor"),
            cc.get("horizon_close_profit_factor"), True, 0.0,
        ),
        "v2_return": (
            nv.get("avg_horizon_close_return_pct"),
            cv.get("avg_horizon_close_return_pct"), True, 0.0,
        ),
        "v2_pf": (
            nv.get("horizon_close_profit_factor"),
            cv.get("horizon_close_profit_factor"), True, 0.0,
        ),
        "v2_accuracy": (
            nv.get("direction_accuracy_on_best_movers_pct"),
            cv.get("direction_accuracy_on_best_movers_pct"), True, 0.5,
        ),
        "v2_mae": (nv.get("avg_mae_pct"), cv.get("avg_mae_pct"), False, 0.5),
    }
    checks = {}
    states = {}
    for name, (challenger_value, control_value, higher_is_better, tol) in specs.items():
        passed, state = _metric_compare(
            challenger_value, control_value,
            higher_is_better=higher_is_better, tol=tol,
        )
        checks[name] = passed
        states[name] = state

    mover_evidence = not (
        _num(cs.get("recall_pct")) is None and _num(ns.get("recall_pct")) is None
    )
    direction_evidence = (
        int(nc.get("sample") or 0) > 0
        and int(nv.get("sample") or 0) > 0
    )
    checks["mover_evidence"] = mover_evidence
    checks["direction_evidence"] = direction_evidence
    checks["pass"] = all(checks.values())
    return {
        **checks,
        "comparison_state": states,
        "missing_metric_policy": {
            "both_undefined": "neutral_not_a_regression",
            "control_undefined_challenger_defined": "new_evidence_not_a_regression",
            "challenger_undefined_control_defined": "fail",
            "period_without_target_movers": "fail",
        },
    }

def _period(rows: list[dict], arch: dict, features: tuple[str, ...] | None) -> dict:
    movers = sum(bool(row.get("best_mover_5d")) for row in rows)
    control = _view(rows, _current_rank(rows), arch, movers)
    if not features:
        return {"control": control}
    challenger = _view(rows, _candidate_rank(rows, features), arch, movers)
    return {"control": control, "challenger": challenger, "gate": _gate(control, challenger)}


def _lane(canonical: dict, signal_index: dict, series: dict, *, lane_label: str) -> dict:
    rows = _rows(canonical, series)
    train_dates, valid_dates, test_dates = _split_dates(rows)
    train = [r for r in rows if r["date"] in train_dates]
    valid = [r for r in rows if r["date"] in valid_dates]
    test = [r for r in rows if r["date"] in test_dates]

    architecture = _build_architecture_rows(
        canonical, signal_index, series,
        threshold_pct=TARGET_PCT, horizon_sessions=HORIZON_SESSIONS,
    )
    arch_all = {(str(r["date"]), str(r["ticker"])): r for r in architecture}
    def arch(dates):
        return {k: v for k, v in arch_all.items() if k[0] in dates}

    candidates = {}
    stable = []
    for name, features in CANDIDATE_FAMILIES.items():
        tr = _period(train, arch(train_dates), features)
        va = _period(valid, arch(valid_dates), features)
        ok = bool((tr.get("gate") or {}).get("pass") and (va.get("gate") or {}).get("pass"))
        candidates[name] = {
            "features": list(features), "train": tr, "validation": va,
            "stable_train_validation": ok,
        }
        if ok:
            stable.append(name)

    def score(name):
        view = candidates[name]["validation"]["challenger"]
        strict = view["direction"]["core_v2_strict"]
        current = view["direction"]["current_core"]
        return (
            float(_num(strict.get("horizon_close_profit_factor")) or -1e9),
            float(_num(strict.get("avg_horizon_close_return_pct")) or -1e9),
            float(_num(current.get("horizon_close_profit_factor")) or -1e9),
            float(_num(view["selection"].get("precision_pct")) or -1e9),
        )

    selected_name = max(stable, key=score) if stable else None
    if selected_name:
        final = _period(test, arch(test_dates), CANDIDATE_FAMILIES[selected_name])
        follow = bool((final.get("gate") or {}).get("pass"))
    else:
        final = _period(test, arch(test_dates), None)
        follow = False

    return {
        "mode": MODE, "lane": str(lane_label).upper(),
        "status": "EVALUABLE" if train and valid and test else "INSUFFICIENT",
        "live_mutation": False, "promotion_allowed": False,
        "split": {
            "train_dates": len(train_dates), "validation_dates": len(valid_dates),
            "test_dates": len(test_dates), "train_rows": len(train),
            "validation_rows": len(valid), "test_rows": len(test),
        },
        "candidate_families": candidates,
        "stable_candidates": stable,
        "selected_candidate": selected_name,
        "selected_features": list(CANDIDATE_FAMILIES.get(selected_name, ())),
        "final_test": final,
        "historical_followup_candidate": follow,
        "policy": {
            "fixed_candidate_families_only": True, "weights_optimized": False,
            "static_mandatory_preserved": True, "future_prices_labels_only": True,
            "scanner_live_unchanged": True, "core_live_unchanged": True,
            "research_live_unchanged": True, "gex_timing_fusion_lifecycle_unchanged": True,
            "prospective_required_before_any_live_change": True,
        },
    }


def build_scanner_direction_aware_ranking(canonical, signal_index, series, *, lane_label="ROLLING_LATEST"):
    return _lane(canonical, signal_index, series, lane_label=lane_label)


def combine_scanner_direction_aware_rankings(rolling: dict, fixed: dict) -> dict:
    rn, fn = rolling.get("selected_candidate"), fixed.get("selected_candidate")
    ro = bool(rolling.get("historical_followup_candidate"))
    fo = bool(fixed.get("historical_followup_candidate"))
    same = bool(rn and fn and rn == fn)
    dual = bool(ro and fo and same)
    return {
        "mode": "SCANNER_DIRECTION_AWARE_DUAL_LANE_DECISION_V2",
        "status": "EVALUABLE" if rolling.get("status") == fixed.get("status") == "EVALUABLE" else "PARTIAL",
        "live_mutation": False, "promotion_allowed": False,
        "rolling_selected_candidate": rn, "fixed_selected_candidate": fn,
        "same_candidate_across_lanes": same,
        "rolling_followup": ro, "fixed_followup": fo, "dual_lane_followup": dual,
        "next_step": (
            "PROSPECTIVE_SHADOW_DIRECTION_AWARE_SCANNER"
            if dual else "NO_DIRECTION_AWARE_SCANNER_SURVIVOR_REVISIT_GENERATOR"
        ),
    }
