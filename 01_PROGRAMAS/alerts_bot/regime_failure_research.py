from __future__ import annotations

"""Generation-4 failure-driven research for contextual mover selection.

This module is deliberately invoked only after the bounded adaptive search has
failed to produce a common ROLLING_LATEST + FIXED_BENCHMARK survivor.

It does two things:
1. explain *where* the current family fails in chronological Rolling validation;
2. test a small deterministic regime-aware router using the same frozen TRAIN
   and VALIDATION rows in both lanes.

FINAL TEST is never read here. LIVE/config/source promotion is impossible.
"""

from collections import defaultdict
from datetime import datetime
import json
import math
import os
from pathlib import Path
from statistics import mean
from typing import Any, Callable

import config
from autonomous_research_lab import (
    _build_models,
    _candidate_name,
    _contiguous_date_folds,
    _models_usable,
)
from core_context_precision_optimizer import (
    TARGET_PCT,
    _comparison,
    _context_profile,
    _fit_train_models,
    _metrics,
    _path,
    _same_capacity_control,
    _score_row,
    _select_period,
)

MODE = "GEN4_FAILURE_DRIVEN_REGIME_RESEARCH_V1"
LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")
ROUTER_MODES = ("REGIME", "REGIME_VOL")
DEFAULT_MAX_BASE_SPECS = 8
DEFAULT_MIN_SPECIALIST_ROWS = 600
DEFAULT_MIN_CLASS_ROWS = 50


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass


def _f(value: Any) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _quantile(values: list[float], q: float) -> float | None:
    ordered = sorted(float(v) for v in values if _f(v) is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    pos = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    weight = pos - lo
    return float(ordered[lo]) * (1.0 - weight) + float(ordered[hi]) * weight


def _regime(row: dict) -> str:
    value = str((row.get("context_diagnostics") or {}).get("market_regime") or "").upper()
    return value or "UNKNOWN"


def _vol_value(row: dict) -> float | None:
    features = row.get("context_features") or {}
    for key in (
        "market.vix_stress_score_aligned",
        "market.uvix_aligned",
        "market.vix_aligned",
        "market.vvix_aligned",
    ):
        value = _f(features.get(key))
        if value is not None:
            return abs(float(value))
    return None


def _vol_thresholds(train: list[dict]) -> tuple[float | None, float | None]:
    values = [value for row in train if (value := _vol_value(row)) is not None]
    return _quantile(values, 1.0 / 3.0), _quantile(values, 2.0 / 3.0)


def _vol_bucket(row: dict, thresholds: tuple[float | None, float | None]) -> str:
    value = _vol_value(row)
    lo, hi = thresholds
    if value is None or lo is None or hi is None:
        return "VOL_UNKNOWN"
    if value <= lo:
        return "VOL_LOW"
    if value <= hi:
        return "VOL_MID"
    return "VOL_HIGH"


def _broad_alignment(row: dict) -> str:
    features = row.get("context_features") or {}
    values = [
        value
        for key in (
            "market.spy_aligned",
            "market.qqq_aligned",
            "market.iwm_aligned",
            "market.dia_aligned",
        )
        if (value := _f(features.get(key))) is not None
    ]
    if not values:
        return "BROAD_UNKNOWN"
    avg = mean(values)
    if avg > 0:
        return "BROAD_SUPPORTIVE"
    if avg < 0:
        return "BROAD_AGAINST"
    return "BROAD_NEUTRAL"


def _sector_alignment(row: dict) -> str:
    value = _f(
        (row.get("context_features") or {}).get(
            "sector.sector_etf_explicit_aligned"
        )
    )
    if value is None:
        return "SECTOR_UNKNOWN"
    if value > 0:
        return "SECTOR_SUPPORTIVE"
    if value < 0:
        return "SECTOR_AGAINST"
    return "SECTOR_NEUTRAL"


def _mover(row: dict) -> bool:
    direction = str(row.get("context_direction") or "").upper()
    return bool(float((_path(row, direction) or {}).get("mfe_pct") or 0.0) >= TARGET_PCT)


def _row_return(row: dict) -> float | None:
    direction = str(row.get("context_direction") or "").upper()
    return _f((_path(row, direction) or {}).get("horizon_close_return_pct"))


def _segment(rows: list[dict], key_fn: Callable[[dict], str]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)
    out = []
    for key, group in groups.items():
        returns = [value for row in group if (value := _row_return(row)) is not None]
        movers = sum(_mover(row) for row in group)
        out.append({
            "segment": key,
            "sample": len(group),
            "movers": movers,
            "precision_pct": round(100.0 * movers / len(group), 3) if group else None,
            "avg_return_pct": round(mean(returns), 4) if returns else None,
        })
    return sorted(out, key=lambda row: (-int(row["sample"]), str(row["segment"])))


def _correct_mover_universe(rows: list[dict]) -> list[dict]:
    unique: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not row.get("best_mover_5d"):
            continue
        if str(row.get("context_direction") or "").upper() != str(row.get("best_direction") or "").upper():
            continue
        key = (str(row.get("date") or ""), str(row.get("ticker") or ""))
        unique.setdefault(key, row)
    return list(unique.values())


def _failure_reasons(view: dict) -> list[str]:
    gate = view.get("gate") or {}
    return sorted(
        key
        for key, value in gate.items()
        if key != "pass" and value is False
    )


def _fold_profile(
    validation: list[dict],
    evaluate: Callable[[list[dict]], dict],
) -> dict:
    folds = _contiguous_date_folds(validation, 3)
    rows = []
    for idx, dates in enumerate(folds, start=1):
        subset = [row for row in validation if str(row.get("date") or "") in dates]
        view = evaluate(subset)
        challenger = view.get("challenger") or {}
        control = view.get("control") or {}
        lift = view.get("lift") or {}
        sample = int(challenger.get("sample") or 0)
        precision_lift = _f(lift.get("precision_pp"))
        ret = _f(challenger.get("avg_horizon_close_return_pct"))
        pf = _f(challenger.get("horizon_close_profit_factor"))
        mae = _f(challenger.get("avg_mae_pct"))
        control_mae = _f(control.get("avg_mae_pct"))
        evaluable = sample >= 10
        passed = bool(
            evaluable
            and precision_lift is not None and precision_lift >= 0.0
            and ret is not None and ret > 0.0
            and pf is not None and pf > 1.0
            and mae is not None and control_mae is not None
            and mae <= control_mae + 0.5
        )
        rows.append({
            "fold": idx,
            "dates": sorted(dates),
            "sample": sample,
            "evaluable": evaluable,
            "pass": passed,
            "precision_lift_pp": precision_lift,
            "challenger_precision_pct": _f(challenger.get("precision_pct")),
            "challenger_return_pct": ret,
            "challenger_profit_factor": pf,
            "challenger_mae_pct": mae,
            "control_mae_pct": control_mae,
            "failed_gate_checks": _failure_reasons(view),
        })
    evaluable = [row for row in rows if row["evaluable"]]
    precision = [float(row["precision_lift_pp"]) for row in evaluable if row["precision_lift_pp"] is not None]
    returns = [float(row["challenger_return_pct"]) for row in evaluable if row["challenger_return_pct"] is not None]
    pfs = [float(row["challenger_profit_factor"]) for row in evaluable if row["challenger_profit_factor"] is not None]
    passed = bool(
        len(evaluable) >= min(2, len(folds))
        and evaluable
        and all(bool(row["pass"]) for row in evaluable)
        and precision and returns and pfs
    )
    return {
        "status": "PASS" if passed else ("FAIL" if evaluable else "INSUFFICIENT"),
        "pass": passed,
        "fold_count": len(folds),
        "evaluable_folds": len(evaluable),
        "pass_folds": sum(bool(row["pass"]) for row in evaluable),
        "min_precision_lift_pp": round(min(precision), 4) if precision else None,
        "min_return_pct": round(min(returns), 4) if returns else None,
        "min_profit_factor": round(min(pfs), 4) if pfs else None,
        "precision_lift_range_pp": round(max(precision) - min(precision), 4) if precision else None,
        "folds": rows,
    }


def _reference_spec(
    lane: str,
    base_results: dict[str, dict],
    candidate_records: dict[str, dict[str, dict]],
) -> tuple[dict, str]:
    # Rolling is diagnosed first against the Fixed survivor because that is the
    # exact architecture that generalized in the static lane but failed to
    # generalize in Rolling.
    if lane == "ROLLING_LATEST":
        fixed = base_results.get("FIXED_BENCHMARK") or {}
        spec = dict(fixed.get("selected_spec") or {})
        if spec:
            return spec, "FIXED_SURVIVOR_APPLIED_TO_ROLLING"
    result = base_results.get(lane) or {}
    selected_spec = dict(result.get("selected_spec") or {})
    if selected_spec:
        return selected_spec, "LANE_SELECTED_SURVIVOR"
    leaderboard = list(result.get("leaderboard") or [])
    if leaderboard:
        return dict((leaderboard[0] or {}).get("spec") or {}), "LANE_TOP_NEAR_MISS"
    records = list((candidate_records.get(lane) or {}).values())
    if records:
        records.sort(
            key=lambda row: (
                _f(((row.get("validation") or {}).get("challenger") or {}).get("precision_pct")) or -1e12,
                _f(((row.get("validation") or {}).get("challenger") or {}).get("horizon_close_profit_factor")) or -1e12,
            ),
            reverse=True,
        )
        return dict((records[0] or {}).get("spec") or {}), "ADAPTIVE_TOP_NEAR_MISS"
    return {}, "NONE"


def _diagnose_reference(
    lane: str,
    lane_ctx: dict,
    base_results: dict[str, dict],
    candidate_records: dict[str, dict[str, dict]],
) -> dict:
    spec, source = _reference_spec(lane, base_results, candidate_records)
    if not spec:
        return {"status": "NO_REFERENCE_SPEC", "source": source}
    models = _build_models(lane_ctx["fitted"], spec)
    if not _models_usable(models):
        return {"status": "UNUSABLE_REFERENCE_MODELS", "source": source, "spec": spec}
    capacity = int(spec.get("daily_capacity") or 1)
    train = lane_ctx["train"]
    validation = lane_ctx["validation"]
    thresholds = _vol_thresholds(train)
    control, selected, selection = _select_period(validation, models, capacity)
    selected_keys = {(str(row.get("date") or ""), str(row.get("ticker") or "")) for row in selected}
    opportunities = _correct_mover_universe(validation)
    missed = [
        row for row in opportunities
        if (str(row.get("date") or ""), str(row.get("ticker") or "")) not in selected_keys
    ]
    false_positive = [row for row in selected if not _mover(row)]
    correct_selected = [row for row in selected if _mover(row)]

    def core_key(row: dict) -> str:
        return "CORE_PASS" if bool((row.get("stage") or {}).get("core")) else "CORE_REJECT"

    dims = {
        "market_regime": lambda row: _regime(row),
        "volatility_bucket": lambda row: _vol_bucket(row, thresholds),
        "broad_etf_alignment": _broad_alignment,
        "sector_etf_alignment": _sector_alignment,
        "direction": lambda row: str(row.get("context_direction") or "UNKNOWN"),
        "core_stage": core_key,
    }
    failure_map = {}
    for name, fn in dims.items():
        failure_map[name] = {
            "selected": _segment(selected, fn),
            "false_positives": _segment(false_positive, fn),
            "missed_true_movers": _segment(missed, fn),
        }

    view = _comparison(validation, models, capacity)
    folds = _fold_profile(
        validation,
        lambda subset: _comparison(subset, models, capacity),
    )
    return {
        "status": "EVALUABLE",
        "reference_source": source,
        "candidate_name": _candidate_name(spec),
        "spec": spec,
        "validation": view,
        "validation_failed_gate_checks": _failure_reasons(view),
        "fold_stability": folds,
        "selection": selection,
        "selected": len(selected),
        "selected_true_movers": len(correct_selected),
        "selected_false_positives": len(false_positive),
        "true_mover_opportunities": len(opportunities),
        "missed_true_movers": len(missed),
        "missed_true_movers_core_rejected": sum(
            not bool((row.get("stage") or {}).get("core"))
            for row in missed
        ),
        "volatility_thresholds": {
            "low_mid": thresholds[0],
            "mid_high": thresholds[1],
        },
        "failure_map": failure_map,
        "interpretation_limits": {
            "cache_rows_are_scanner_universe": True,
            "core_reject_is_observable": True,
            "scanner_miss_is_not_observable_inside_context_cache": True,
            "capacity_constrained_misses_are_diagnostics_not_recall_target": True,
        },
    }


def _route_key(row: dict, mode: str, thresholds: tuple[float | None, float | None]) -> str:
    regime = _regime(row)
    if mode == "REGIME":
        return regime
    return f"{regime}|{_vol_bucket(row, thresholds)}"


def _fit_router(train: list[dict], fitted: dict, spec: dict, mode: str) -> dict:
    thresholds = _vol_thresholds(train)
    global_models = _build_models(fitted, spec)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in train:
        grouped[_route_key(row, mode, thresholds)].append(row)

    min_rows = int(getattr(config, "GEN4_MIN_SPECIALIST_ROWS", DEFAULT_MIN_SPECIALIST_ROWS))
    min_class = int(getattr(config, "GEN4_MIN_CLASS_ROWS", DEFAULT_MIN_CLASS_ROWS))
    specialists: dict[str, dict] = {}
    specialist_meta: dict[str, dict] = {}
    for key, rows in sorted(grouped.items()):
        movers = sum(bool(row.get("directional_mover_5d")) for row in rows)
        nonmovers = len(rows) - movers
        if len(rows) < min_rows or movers < min_class or nonmovers < min_class:
            specialist_meta[key] = {
                "status": "INSUFFICIENT",
                "rows": len(rows),
                "movers": movers,
                "nonmovers": nonmovers,
            }
            continue
        local_fitted = _fit_train_models(rows)
        models = _build_models(local_fitted, spec)
        if not _models_usable(models):
            specialist_meta[key] = {
                "status": "UNUSABLE_MODELS",
                "rows": len(rows),
                "movers": movers,
                "nonmovers": nonmovers,
            }
            continue
        specialists[key] = models
        specialist_meta[key] = {
            "status": "READY",
            "rows": len(rows),
            "movers": movers,
            "nonmovers": nonmovers,
            "features": {
                block: [str(item.get("feature") or "") for item in values]
                for block, values in models.items()
            },
        }
    return {
        "mode": mode,
        "thresholds": thresholds,
        "global_models": global_models,
        "specialists": specialists,
        "specialist_meta": specialist_meta,
    }


def _router_models(router: dict, row: dict) -> tuple[dict, str]:
    mode = str(router.get("mode") or "REGIME")
    thresholds = tuple(router.get("thresholds") or (None, None))
    key = _route_key(row, mode, thresholds)
    models = (router.get("specialists") or {}).get(key)
    if models:
        return models, key
    if mode == "REGIME_VOL":
        fallback = _regime(row)
        models = (router.get("specialists") or {}).get(fallback)
        if models:
            return models, fallback
    return router.get("global_models") or {}, "GLOBAL_FALLBACK"


def _select_router(
    rows: list[dict],
    router: dict,
    daily_capacity: int,
) -> tuple[list[dict], list[dict], dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("date") or "")].append(row)

    control: list[dict] = []
    challenger: list[dict] = []
    scoreable = 0
    context_rejects = 0
    route_counts: dict[str, int] = defaultdict(int)

    for day, day_rows in sorted(grouped.items()):
        capacity = max(1, int(daily_capacity))
        control.extend(_same_capacity_control(day_rows, capacity))
        scored = []
        for row in day_rows:
            if not row.get("context_usable"):
                context_rejects += 1
                continue
            models, route = _router_models(router, row)
            score, explanation = _score_row(row, models)
            if score is None:
                context_rejects += 1
                continue
            scoreable += 1
            route_counts[route] += 1
            scored.append({
                **row,
                "context_precision_score": score,
                "context_precision_explanation": explanation,
                "gen4_route": route,
            })

        best_by_ticker: dict[str, dict] = {}
        for item in scored:
            ticker = str(item.get("ticker") or "")
            old = best_by_ticker.get(ticker)
            if old is None or (
                float(item.get("context_precision_score") or -1e18),
                str(item.get("context_direction") or ""),
            ) > (
                float(old.get("context_precision_score") or -1e18),
                str(old.get("context_direction") or ""),
            ):
                best_by_ticker[ticker] = item
        ranked = sorted(
            best_by_ticker.values(),
            key=lambda row: (
                float(row.get("context_precision_score") or -1e18),
                str(row.get("ticker") or ""),
            ),
            reverse=True,
        )
        challenger.extend(ranked[:capacity])

    return control, challenger, {
        "daily_capacity": int(daily_capacity),
        "dates": len(grouped),
        "scoreable_rows": scoreable,
        "context_rejects": context_rejects,
        "route_counts": dict(sorted(route_counts.items())),
    }


def _comparison_router(rows: list[dict], router: dict, daily_capacity: int) -> dict:
    control, challenger, selection = _select_router(rows, router, daily_capacity)
    control_metrics = _metrics(control, use_context_direction=False)
    challenger_metrics = _metrics(challenger, use_context_direction=True)
    cp = _f(control_metrics.get("precision_pct"))
    np = _f(challenger_metrics.get("precision_pct"))
    cr = _f(control_metrics.get("avg_horizon_close_return_pct"))
    nr = _f(challenger_metrics.get("avg_horizon_close_return_pct"))
    cpf = _f(control_metrics.get("horizon_close_profit_factor"))
    npf = _f(challenger_metrics.get("horizon_close_profit_factor"))
    cmae = _f(control_metrics.get("avg_mae_pct"))
    nmae = _f(challenger_metrics.get("avg_mae_pct"))
    checks = {
        "sample": int(challenger_metrics.get("sample") or 0) >= 30,
        "precision_positive_lift": cp is not None and np is not None and np >= cp + 2.0,
        "return_not_worse": cr is not None and nr is not None and nr >= cr,
        "return_positive": nr is not None and nr > 0.0,
        "pf_not_worse": cpf is not None and npf is not None and npf >= cpf,
        "pf_above_one": npf is not None and npf > 1.0,
        "mae_controlled": cmae is not None and nmae is not None and nmae <= cmae + 0.5,
    }
    checks["pass"] = all(checks.values())
    return {
        "selection": selection,
        "control": control_metrics,
        "challenger": challenger_metrics,
        "lift": {
            "precision_pp": round(np - cp, 3) if np is not None and cp is not None else None,
            "return_pp": round(nr - cr, 4) if nr is not None and cr is not None else None,
            "pf": round(npf - cpf, 4) if npf is not None and cpf is not None else None,
            "mae_pp": round(nmae - cmae, 4) if nmae is not None and cmae is not None else None,
        },
        "gate": checks,
        "true_positive_context": _context_profile([row for row in challenger if _mover(row)]),
        "false_positive_context": _context_profile([row for row in challenger if not _mover(row)]),
    }


def _spec_pool(
    base_results: dict[str, dict],
    candidate_records: dict[str, dict[str, dict]],
    max_specs: int,
) -> list[dict]:
    weighted: list[tuple[tuple, dict]] = []
    seen: set[str] = set()

    def add(spec: dict, priority: int, precision: float = -1e12, pf: float = -1e12) -> None:
        if not spec:
            return
        try:
            name = _candidate_name(spec)
        except Exception:
            return
        if name in seen:
            return
        seen.add(name)
        weighted.append(((priority, precision, pf, name), dict(spec)))

    fixed = base_results.get("FIXED_BENCHMARK") or {}
    add(dict(fixed.get("selected_spec") or {}), 100)
    for lane in LANES:
        result = base_results.get(lane) or {}
        for rank, row in enumerate(result.get("leaderboard") or [], start=1):
            metrics = (row.get("validation") or {}).get("challenger") or {}
            add(
                dict(row.get("spec") or {}),
                90 - rank,
                _f(metrics.get("precision_pct")) or -1e12,
                _f(metrics.get("horizon_close_profit_factor")) or -1e12,
            )
            if rank >= 6:
                break
        records = list((candidate_records.get(lane) or {}).values())
        for row in records:
            metrics = (row.get("validation") or {}).get("challenger") or {}
            add(
                dict(row.get("spec") or {}),
                50,
                _f(metrics.get("precision_pct")) or -1e12,
                _f(metrics.get("horizon_close_profit_factor")) or -1e12,
            )
    weighted.sort(key=lambda item: item[0], reverse=True)
    return [spec for _, spec in weighted[: max(1, int(max_specs))]]


def _cross_lane_score(name: str, records: dict[str, dict[str, dict]]) -> tuple:
    lane_rows = [records[lane][name] for lane in LANES]
    stability = [row.get("stability") or {} for row in lane_rows]
    validation = [(row.get("validation") or {}).get("challenger") or {} for row in lane_rows]
    return (
        min(_f(row.get("min_precision_lift_pp")) or -1e12 for row in stability),
        min(_f(row.get("min_profit_factor")) or -1e12 for row in stability),
        min(_f(row.get("min_return_pct")) or -1e12 for row in stability),
        min(_f(row.get("precision_pct")) or -1e12 for row in validation),
        min(_f(row.get("horizon_close_profit_factor")) or -1e12 for row in validation),
        min(_f(row.get("avg_horizon_close_return_pct")) or -1e12 for row in validation),
        name,
    )


def run_gen4_failure_research(
    *,
    lane_contexts: dict[str, dict],
    base_results: dict[str, dict],
    candidate_records: dict[str, dict[str, dict]],
    persist: bool = True,
) -> dict:
    max_specs = int(getattr(config, "GEN4_MAX_BASE_SPECS", DEFAULT_MAX_BASE_SPECS))
    diagnostics = {
        lane: _diagnose_reference(lane, lane_contexts[lane], base_results, candidate_records)
        for lane in LANES
    }
    specs = _spec_pool(base_results, candidate_records, max_specs)
    lane_records: dict[str, dict[str, dict]] = {lane: {} for lane in LANES}

    for lane in LANES:
        ctx = lane_contexts[lane]
        for spec in specs:
            for router_mode in ROUTER_MODES:
                router = _fit_router(ctx["train"], ctx["fitted"], spec, router_mode)
                if not _models_usable(router.get("global_models") or {}):
                    continue
                capacity = int(spec.get("daily_capacity") or 1)
                train_view = _comparison_router(ctx["train"], router, capacity)
                validation_view = _comparison_router(ctx["validation"], router, capacity)
                stability = _fold_profile(
                    ctx["validation"],
                    lambda subset, _r=router, _c=capacity: _comparison_router(subset, _r, _c),
                )
                name = f"GEN4_{router_mode}::{_candidate_name(spec)}"
                screen = bool(
                    (train_view.get("gate") or {}).get("pass")
                    and (validation_view.get("gate") or {}).get("pass")
                    and stability.get("pass")
                )
                lane_records[lane][name] = {
                    "candidate_name": name,
                    "router_mode": router_mode,
                    "base_spec": spec,
                    "train": train_view,
                    "validation": validation_view,
                    "stability": stability,
                    "screen_survivor": screen,
                    "specialists_ready": sum(
                        (meta or {}).get("status") == "READY"
                        for meta in (router.get("specialist_meta") or {}).values()
                    ),
                    "specialist_meta": router.get("specialist_meta") or {},
                    "final_test_status": "LOCKED",
                    "final_test": {},
                }

    common = sorted(
        name
        for name in set(lane_records["ROLLING_LATEST"]).intersection(lane_records["FIXED_BENCHMARK"])
        if lane_records["ROLLING_LATEST"][name].get("screen_survivor")
        and lane_records["FIXED_BENCHMARK"][name].get("screen_survivor")
    )
    common.sort(key=lambda name: _cross_lane_score(name, lane_records), reverse=True)
    consensus = common[0] if common else None

    payload = {
        "schema_version": 1,
        "mode": MODE,
        "producer_version": config.BOT_VERSION,
        "generated_at": _now(),
        "status": "EVALUABLE",
        "objective": "EXPLAIN_ROLLING_FAILURE_AND_TEST_SMALL_REGIME_SPECIALIST_ROUTER",
        "diagnostics": diagnostics,
        "base_spec_count": len(specs),
        "router_modes": list(ROUTER_MODES),
        "lane_summary": {
            lane: {
                "candidate_count": len(lane_records[lane]),
                "survivor_count": sum(bool(row.get("screen_survivor")) for row in lane_records[lane].values()),
                "top_candidates": [
                    {
                        "candidate_name": name,
                        "screen_survivor": bool(row.get("screen_survivor")),
                        "stability": row.get("stability") or {},
                        "validation": row.get("validation") or {},
                        "specialists_ready": row.get("specialists_ready"),
                    }
                    for name, row in sorted(
                        lane_records[lane].items(),
                        key=lambda item: (
                            bool(item[1].get("screen_survivor")),
                            _f(((item[1].get("stability") or {}).get("min_precision_lift_pp"))) or -1e12,
                            _f((((item[1].get("validation") or {}).get("challenger") or {}).get("precision_pct"))) or -1e12,
                            item[0],
                        ),
                        reverse=True,
                    )[:10]
                ],
            }
            for lane in LANES
        },
        "common_survivor_count": len(common),
        "consensus_candidate": consensus,
        "top_common_survivors": [
            {
                "rank": idx,
                "candidate_name": name,
                "rolling": lane_records["ROLLING_LATEST"][name],
                "fixed": lane_records["FIXED_BENCHMARK"][name],
            }
            for idx, name in enumerate(common[:10], start=1)
        ],
        "final_test_status": "LOCKED",
        "final_test": {},
        "next_step": (
            "REVIEW_GEN4_CONSENSUS_FOR_EXPLICIT_LOCKBOX"
            if consensus
            else "RETHINK_FEATURE_ARCHITECTURE_FROM_ROLLING_FAILURE_MAP"
        ),
        "policy": {
            "frozen_cache_only": True,
            "train_validation_only": True,
            "same_base_specs_both_lanes": True,
            "regime_router_fit_on_train_only": True,
            "chronological_three_fold_validation": True,
            "final_test_never_read": True,
            "live_mutation": False,
            "source_code_self_mutation": False,
            "auto_promote_live": False,
            "auto_deploy": False,
        },
    }

    if persist:
        runtime = config.RUNTIME_ROOT / "autonomous_research" / "gen4_failure_research"
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        _atomic_json(runtime / f"gen4_failure_research_{stamp}.json", payload)
        _atomic_json(runtime / "latest_gen4_failure_research.json", payload)
        _atomic_json(
            config.OUTPUT_DIR
            / "historical_validation"
            / "latest_gen4_failure_research.json",
            payload,
        )
    return payload
