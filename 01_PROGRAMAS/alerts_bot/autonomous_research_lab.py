from __future__ import annotations

"""Autonomous research tournament for TMAD contextual mover discovery.

Research-only. This module never mutates LIVE strategy, sizing, lifecycle,
configuration files or source code. It reuses one frozen historical dataset to
evaluate many deterministic candidate architectures cheaply, then allows only
one TRAIN+VALIDATION winner to touch the lane FINAL TEST.

The search is intentionally bounded and interpretable:
- fixed candidate grammar (feature-family limits + one-step ablations);
- no hindsight-driven free weight optimization;
- reliability-first ranking;
- temporal critic before FINAL TEST;
- SQLite experiment memory under 04_RUNTIME;
- no automatic LIVE promotion.
"""

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Callable, Any

MODE = "AUTONOMOUS_RESEARCH_LAB_V1"
BLOCKS = ("node", "market", "sector", "relation", "interaction")
DAILY_CAPACITIES = (1, 3, 5, 10)
DEFAULT_MAX_CANDIDATES = 48
DEFAULT_TOP_REPORT = 20
STABILITY_FOLDS = 3
DEFAULT_GENERATION3_PARENTS = 3
DEFAULT_GENERATION3_MAX_CHILDREN = 24


def _f(value: Any) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _limit_key(limits: dict[str, int]) -> str:
    return "-".join(
        (
            f"N{int(limits.get('node') or 0)}",
            f"M{int(limits.get('market') or 0)}",
            f"S{int(limits.get('sector') or 0)}",
            f"R{int(limits.get('relation') or 0)}",
            f"I{int(limits.get('interaction') or 0)}",
        )
    )


def _candidate_name(spec: dict) -> str:
    base = f"{_limit_key(spec['limits'])}@TOP{int(spec['daily_capacity'])}"
    drop = spec.get("drop_top") or {}
    if drop:
        suffix = ",".join(f"{block}:{int(drop[block])}" for block in sorted(drop))
        return f"{base}|DROP[{suffix}]"
    return base


def _valid_limits(limits: dict[str, int]) -> bool:
    if int(limits.get("node") or 0) < 1:
        return False
    if int(limits.get("market") or 0) < 1:
        return False
    return any(int(limits.get(block) or 0) > 0 for block in ("sector", "relation", "interaction"))


def _candidate_limit_specs() -> list[dict[str, int]]:
    """Small fixed grammar around the 6.2.48 LEAN/BALANCED/RICH families."""
    raw = [
        {"node": 2, "market": 2, "sector": 1, "relation": 1, "interaction": 2},
        {"node": 3, "market": 4, "sector": 2, "relation": 2, "interaction": 4},
        {"node": 5, "market": 6, "sector": 4, "relation": 4, "interaction": 8},
        # Ablate one optional context family at a time.
        {"node": 3, "market": 4, "sector": 0, "relation": 2, "interaction": 4},
        {"node": 3, "market": 4, "sector": 2, "relation": 0, "interaction": 4},
        {"node": 3, "market": 4, "sector": 2, "relation": 2, "interaction": 0},
        # Context-emphasis mutations.
        {"node": 3, "market": 6, "sector": 2, "relation": 2, "interaction": 4},
        {"node": 3, "market": 4, "sector": 4, "relation": 2, "interaction": 4},
        {"node": 3, "market": 4, "sector": 2, "relation": 4, "interaction": 4},
        {"node": 3, "market": 4, "sector": 2, "relation": 2, "interaction": 6},
        # Slightly leaner/richer balanced neighborhoods.
        {"node": 2, "market": 3, "sector": 2, "relation": 2, "interaction": 3},
        {"node": 4, "market": 5, "sector": 3, "relation": 3, "interaction": 5},
    ]
    seen: set[tuple[int, ...]] = set()
    out: list[dict[str, int]] = []
    for limits in raw:
        key = tuple(int(limits.get(block) or 0) for block in BLOCKS)
        if key in seen or not _valid_limits(limits):
            continue
        seen.add(key)
        out.append(dict(limits))
    return out


def generate_generation_one(max_candidates: int = DEFAULT_MAX_CANDIDATES) -> list[dict]:
    specs: list[dict] = []
    for limits in _candidate_limit_specs():
        for capacity in DAILY_CAPACITIES:
            specs.append({
                "generation": 1,
                "parent": "6.2.48_CONTEXT_SEARCH",
                "limits": dict(limits),
                "daily_capacity": int(capacity),
                "drop_top": {},
            })
            if len(specs) >= max(1, int(max_candidates)):
                return specs
    return specs


def _build_models(fitted: dict, spec: dict) -> dict[str, list[dict]]:
    drop_top = spec.get("drop_top") or {}
    models: dict[str, list[dict]] = {}
    for block in BLOCKS:
        rows = list(fitted.get(block) or [])
        drop = max(0, int(drop_top.get(block) or 0))
        limit = max(0, int((spec.get("limits") or {}).get(block) or 0))
        models[block] = rows[drop: drop + limit]
    return models


def _models_usable(models: dict[str, list[dict]]) -> bool:
    if not models.get("node") or not models.get("market"):
        return False
    return bool(models.get("sector") or models.get("relation") or models.get("interaction"))


def _model_signature(models: dict[str, list[dict]]) -> dict[str, list[str]]:
    return {
        block: [str(item.get("feature") or "") for item in (models.get(block) or [])]
        for block in BLOCKS
    }


def _compact_view(view: dict) -> dict:
    return {
        "selection": {
            "daily_capacity": int((view.get("selection") or {}).get("daily_capacity") or 0),
            "dates": int((view.get("selection") or {}).get("dates") or 0),
            "scoreable_rows": int((view.get("selection") or {}).get("scoreable_rows") or 0),
            "context_rejects": int((view.get("selection") or {}).get("context_rejects") or 0),
        },
        "control": dict(view.get("control") or {}),
        "challenger": dict(view.get("challenger") or {}),
        "lift": dict(view.get("lift") or {}),
        "gate": dict(view.get("gate") or {}),
        "attribution": dict(view.get("attribution") or {}),
    }


def _stable(record: dict) -> bool:
    return bool(
        ((record.get("train") or {}).get("gate") or {}).get("pass")
        and ((record.get("validation") or {}).get("gate") or {}).get("pass")
    )



def _contiguous_date_folds(rows: list[dict], folds: int = STABILITY_FOLDS) -> list[set[str]]:
    dates = sorted({str(row.get("date") or "") for row in rows if row.get("date")})
    count = max(1, min(int(folds), len(dates)))
    if count <= 1:
        return [set(dates)] if dates else []
    base, extra = divmod(len(dates), count)
    out: list[set[str]] = []
    start = 0
    for idx in range(count):
        size = base + (1 if idx < extra else 0)
        part = dates[start: start + size]
        start += size
        if part:
            out.append(set(part))
    return out


def _stability_profile(
    record: dict,
    *,
    fitted: dict,
    validation: list[dict],
    comparison_fn: Callable[[list[dict], dict[str, list[dict]], int], dict],
    folds: int = STABILITY_FOLDS,
) -> dict:
    models = _build_models(fitted, record.get("spec") or {})
    capacity = int((record.get("spec") or {}).get("daily_capacity") or 1)
    parts = _contiguous_date_folds(validation, folds)
    fold_rows = []
    for idx, dates in enumerate(parts, start=1):
        rows = [row for row in validation if str(row.get("date") or "") in dates]
        view = _compact_view(comparison_fn(rows, models, capacity))
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
        fold_pass = bool(
            evaluable
            and precision_lift is not None and precision_lift >= 0.0
            and ret is not None and ret > 0.0
            and pf is not None and pf > 1.0
            and mae is not None and control_mae is not None
            and mae <= control_mae + 0.5
        )
        fold_rows.append({
            "fold": idx,
            "dates": len(dates),
            "sample": sample,
            "evaluable": evaluable,
            "pass": fold_pass,
            "precision_lift_pp": precision_lift,
            "challenger_precision_pct": _f(challenger.get("precision_pct")),
            "challenger_return_pct": ret,
            "challenger_profit_factor": pf,
            "challenger_mae_pct": mae,
            "control_mae_pct": control_mae,
        })

    evaluable = [row for row in fold_rows if row["evaluable"]]
    precision = [float(row["precision_lift_pp"]) for row in evaluable if row["precision_lift_pp"] is not None]
    returns = [float(row["challenger_return_pct"]) for row in evaluable if row["challenger_return_pct"] is not None]
    pfs = [float(row["challenger_profit_factor"]) for row in evaluable if row["challenger_profit_factor"] is not None]
    pass_count = sum(bool(row["pass"]) for row in evaluable)
    minimum_required = min(2, len(parts))
    passed = bool(
        len(evaluable) >= minimum_required
        and pass_count == len(evaluable)
        and precision
        and returns
        and pfs
    )
    return {
        "status": "PASS" if passed else ("FAIL" if evaluable else "INSUFFICIENT"),
        "pass": passed,
        "fold_count": len(parts),
        "evaluable_folds": len(evaluable),
        "pass_folds": pass_count,
        "min_precision_lift_pp": round(min(precision), 4) if precision else None,
        "mean_precision_lift_pp": round(sum(precision) / len(precision), 4) if precision else None,
        "precision_lift_range_pp": round(max(precision) - min(precision), 4) if precision else None,
        "min_return_pct": round(min(returns), 4) if returns else None,
        "mean_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
        "min_profit_factor": round(min(pfs), 4) if pfs else None,
        "folds": fold_rows,
        "rule": "all_evaluable_validation_folds_nonnegative_precision_lift_positive_return_pf_gt1_mae_controlled",
    }


def _stability_score(record: dict) -> tuple:
    stability = record.get("stability") or {}
    validation = (record.get("validation") or {}).get("challenger") or {}
    spec = record.get("spec") or {}
    complexity = sum(int(v or 0) for v in (spec.get("limits") or {}).values())
    return (
        float(_f(stability.get("min_precision_lift_pp")) or -1e12),
        float(_f(stability.get("min_profit_factor")) or -1e12),
        float(_f(stability.get("min_return_pct")) or -1e12),
        -float(_f(stability.get("precision_lift_range_pp")) or 1e12),
        float(_f(validation.get("precision_pct")) or -1e12),
        float(_f(validation.get("horizon_close_profit_factor")) or -1e12),
        float(_f(validation.get("avg_horizon_close_return_pct")) or -1e12),
        -float(complexity),
        -float(int(spec.get("daily_capacity") or 999)),
        str(record.get("candidate_name") or ""),
    )


def _attach_stability(
    records: list[dict],
    *,
    fitted: dict,
    validation: list[dict],
    comparison_fn: Callable[[list[dict], dict[str, list[dict]], int], dict],
) -> None:
    for record in records:
        if not record.get("stable_train_validation"):
            record["stability"] = {
                "status": "NOT_ELIGIBLE",
                "pass": False,
                "reason": "train_or_validation_gate_failed",
            }
            continue
        record["stability"] = _stability_profile(
            record,
            fitted=fitted,
            validation=validation,
            comparison_fn=comparison_fn,
        )


def _score(record: dict) -> tuple:
    validation = (record.get("validation") or {}).get("challenger") or {}
    lift = (record.get("validation") or {}).get("lift") or {}
    train = (record.get("train") or {}).get("challenger") or {}
    spec = record.get("spec") or {}
    complexity = sum(int(v or 0) for v in (spec.get("limits") or {}).values())
    return (
        float(_f(validation.get("precision_pct")) or -1e12),
        float(_f(validation.get("horizon_close_profit_factor")) or -1e12),
        float(_f(validation.get("avg_horizon_close_return_pct")) or -1e12),
        float(_f(lift.get("precision_pp")) or -1e12),
        float(_f(train.get("precision_pct")) or -1e12),
        -float(complexity),
        -float(int(spec.get("daily_capacity") or 999)),
        str(record.get("candidate_name") or ""),
    )


def _evaluate(
    spec: dict,
    *,
    fitted: dict,
    train: list[dict],
    validation: list[dict],
    comparison_fn: Callable[[list[dict], dict[str, list[dict]], int], dict],
) -> dict | None:
    models = _build_models(fitted, spec)
    if not _models_usable(models):
        return None
    capacity = int(spec["daily_capacity"])
    train_view = _compact_view(comparison_fn(train, models, capacity))
    validation_view = _compact_view(comparison_fn(validation, models, capacity))
    record = {
        "candidate_name": _candidate_name(spec),
        "generation": int(spec.get("generation") or 1),
        "parent": str(spec.get("parent") or ""),
        "spec": {
            "limits": dict(spec.get("limits") or {}),
            "daily_capacity": capacity,
            "drop_top": dict(spec.get("drop_top") or {}),
        },
        "selected_features": _model_signature(models),
        "train": train_view,
        "validation": validation_view,
    }
    record["stable_train_validation"] = _stable(record)
    return record


def _temporal_critic(
    record: dict,
    *,
    fitted: dict,
    validation: list[dict],
    comparison_fn: Callable[[list[dict], dict[str, list[dict]], int], dict],
) -> dict:
    dates = sorted({str(row.get("date") or "") for row in validation if row.get("date")})
    if len(dates) < 4:
        return {
            "status": "INSUFFICIENT",
            "pass": True,
            "reason": "validation_has_fewer_than_4_dates",
            "halves": [],
        }
    split = max(1, len(dates) // 2)
    parts = [set(dates[:split]), set(dates[split:])]
    models = _build_models(fitted, record.get("spec") or {})
    capacity = int((record.get("spec") or {}).get("daily_capacity") or 1)
    halves = []
    catastrophic = False
    evaluable = 0
    for idx, part in enumerate(parts, start=1):
        rows = [row for row in validation if str(row.get("date") or "") in part]
        view = _compact_view(comparison_fn(rows, models, capacity))
        challenger = view.get("challenger") or {}
        control = view.get("control") or {}
        lift = view.get("lift") or {}
        sample = int(challenger.get("sample") or 0)
        precision_lift = _f(lift.get("precision_pp"))
        challenger_return = _f(challenger.get("avg_horizon_close_return_pct"))
        control_return = _f(control.get("avg_horizon_close_return_pct"))
        if sample >= 10:
            evaluable += 1
            precision_bad = precision_lift is not None and precision_lift < -5.0
            return_bad = (
                challenger_return is not None
                and control_return is not None
                and challenger_return < min(-2.0, control_return - 3.0)
            )
            catastrophic = catastrophic or precision_bad or return_bad
        halves.append({
            "half": idx,
            "dates": len(part),
            "sample": sample,
            "precision_lift_pp": precision_lift,
            "challenger_return_pct": challenger_return,
            "control_return_pct": control_return,
        })
    return {
        "status": "PASS" if not catastrophic else "FAIL",
        "pass": not catastrophic,
        "evaluable_halves": evaluable,
        "rule": "no_validation_half_precision_collapse_gt5pp_or_return_collapse_gt3pp",
        "halves": halves,
    }


def _generation_two(
    preliminary: dict,
    *,
    fitted: dict,
) -> list[dict]:
    """One-step ablation children around the current TRAIN+VALIDATION champion."""
    children: list[dict] = []
    base_spec = preliminary.get("spec") or {}
    base_models = _build_models(fitted, base_spec)
    for block in BLOCKS:
        if len(base_models.get(block) or []) <= 1:
            continue
        child = {
            "generation": 2,
            "parent": str(preliminary.get("candidate_name") or ""),
            "limits": dict(base_spec.get("limits") or {}),
            "daily_capacity": int(base_spec.get("daily_capacity") or 1),
            "drop_top": {block: 1},
        }
        if _models_usable(_build_models(fitted, child)):
            children.append(child)
    return children



def _generation_three(
    parents: list[dict],
    *,
    fitted: dict,
    max_children: int = DEFAULT_GENERATION3_MAX_CHILDREN,
) -> list[dict]:
    """Compound ablations/capacity mutations around stability-surviving parents."""
    children: list[dict] = []
    for parent in parents[: max(1, int(DEFAULT_GENERATION3_PARENTS))]:
        base = parent.get("spec") or {}
        limits = dict(base.get("limits") or {})
        base_drop = dict(base.get("drop_top") or {})
        capacity = int(base.get("daily_capacity") or 1)
        models = _build_models(fitted, base)
        eligible = [block for block in BLOCKS if len(models.get(block) or []) > 1]

        # Two-block ablations target overfit interactions without changing the
        # fitted feature universe or looking at FINAL TEST.
        for i, left in enumerate(eligible):
            for right in eligible[i + 1:]:
                drop = dict(base_drop)
                drop[left] = int(drop.get(left) or 0) + 1
                drop[right] = int(drop.get(right) or 0) + 1
                child = {
                    "generation": 3,
                    "parent": str(parent.get("candidate_name") or ""),
                    "limits": limits,
                    "daily_capacity": capacity,
                    "drop_top": drop,
                }
                if _models_usable(_build_models(fitted, child)):
                    children.append(child)
                    if len(children) >= max(1, int(max_children)):
                        return children

        # Capacity sensitivity around the robust parent. This is useful because
        # 6.2.49 showed that TOP5/TOP10 can generalize very differently.
        if capacity in DAILY_CAPACITIES:
            idx = DAILY_CAPACITIES.index(capacity)
            neighbors = []
            if idx > 0:
                neighbors.append(DAILY_CAPACITIES[idx - 1])
            if idx + 1 < len(DAILY_CAPACITIES):
                neighbors.append(DAILY_CAPACITIES[idx + 1])
            for new_capacity in neighbors:
                child = {
                    "generation": 3,
                    "parent": str(parent.get("candidate_name") or ""),
                    "limits": limits,
                    "daily_capacity": int(new_capacity),
                    "drop_top": dict(base_drop),
                }
                if _models_usable(_build_models(fitted, child)):
                    children.append(child)
                    if len(children) >= max(1, int(max_children)):
                        return children
    return children


def _rank_score(record: dict) -> tuple:
    return (
        1 if bool((record.get("stability") or {}).get("pass")) else 0,
        1 if bool(record.get("stable_train_validation")) else 0,
        *_stability_score(record),
    )


def _fingerprint(rows: list[dict], lane_label: str) -> str:
    dates = sorted({str(row.get("date") or "") for row in rows if row.get("date")})
    tickers = {str(row.get("ticker") or "") for row in rows if row.get("ticker")}
    movers = sum(bool(row.get("directional_mover_5d")) for row in rows)
    payload = {
        "lane": str(lane_label).upper(),
        "dates": dates,
        "rows": len(rows),
        "tickers": len(tickers),
        "directional_movers": movers,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(raw).hexdigest()[:20]


def _persist_registry(
    registry_path: Path | None,
    *,
    dataset_fingerprint: str,
    lane_label: str,
    records: list[dict],
    selected_name: str | None,
    final_test: dict,
) -> dict:
    if registry_path is None:
        return {"status": "DISABLED"}
    try:
        registry_path = Path(registry_path)
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(registry_path), timeout=30)
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS experiments(
                    dataset_fingerprint TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    candidate_name TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    parent TEXT,
                    spec_json TEXT NOT NULL,
                    train_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    final_json TEXT,
                    created_at_utc TEXT NOT NULL,
                    PRIMARY KEY(dataset_fingerprint,lane,candidate_name)
                )"""
            )
            now = datetime.now(timezone.utc).isoformat()
            for record in records:
                is_selected = str(record.get("candidate_name") or "") == str(selected_name or "")
                con.execute(
                    """INSERT OR REPLACE INTO experiments(
                        dataset_fingerprint,lane,candidate_name,generation,parent,
                        spec_json,train_json,validation_json,selected,final_json,created_at_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        dataset_fingerprint,
                        str(lane_label).upper(),
                        str(record.get("candidate_name") or ""),
                        int(record.get("generation") or 1),
                        str(record.get("parent") or ""),
                        json.dumps(record.get("spec") or {}, sort_keys=True),
                        json.dumps(record.get("train") or {}, sort_keys=True, default=str),
                        json.dumps(record.get("validation") or {}, sort_keys=True, default=str),
                        1 if is_selected else 0,
                        json.dumps(final_test or {}, sort_keys=True, default=str) if is_selected else None,
                        now,
                    ),
                )
            con.commit()
        finally:
            con.close()
        return {
            "status": "OK",
            "path": str(registry_path),
            "rows_written": len(records),
            "dataset_fingerprint": dataset_fingerprint,
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "dataset_fingerprint": dataset_fingerprint,
        }


def run_autonomous_research_lane(
    *,
    train: list[dict],
    validation: list[dict],
    test: list[dict],
    fitted: dict,
    comparison_fn: Callable[[list[dict], dict[str, list[dict]], int], dict],
    lane_label: str,
    registry_path: Path | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    top_report: int = DEFAULT_TOP_REPORT,
    touch_final_test: bool = True,
) -> dict:
    """Run one bounded autonomous tournament on an already-frozen dataset.

    Callers choose whether FINAL TEST is visible. Since v6.2.51 the FULL
    contextual flow uses False, so repeated generations cannot adapt to the
    autonomous lockbox before an explicit research decision.
    """
    if not train or not validation or not test:
        return {
            "mode": MODE,
            "lane": str(lane_label).upper(),
            "status": "INSUFFICIENT",
            "live_mutation": False,
            "auto_promote_live": False,
        }

    started = perf_counter()
    generation_one_started = perf_counter()
    records: list[dict] = []
    seen: set[str] = set()
    for spec in generate_generation_one(max_candidates=max_candidates):
        record = _evaluate(
            spec,
            fitted=fitted,
            train=train,
            validation=validation,
            comparison_fn=comparison_fn,
        )
        if record is None:
            continue
        name = str(record["candidate_name"])
        if name in seen:
            continue
        seen.add(name)
        records.append(record)

    generation_one_seconds = perf_counter() - generation_one_started
    _attach_stability(
        records,
        fitted=fitted,
        validation=validation,
        comparison_fn=comparison_fn,
    )
    stable = [record for record in records if record.get("stable_train_validation")]
    stability_survivors = [
        record for record in stable if bool((record.get("stability") or {}).get("pass"))
    ]
    preliminary = (
        max(stability_survivors, key=_stability_score)
        if stability_survivors
        else (max(stable, key=_score) if stable else None)
    )

    generation_two_started = perf_counter()
    generation_two_records: list[dict] = []
    if preliminary is not None:
        for spec in _generation_two(preliminary, fitted=fitted):
            record = _evaluate(
                spec,
                fitted=fitted,
                train=train,
                validation=validation,
                comparison_fn=comparison_fn,
            )
            if record is None:
                continue
            name = str(record["candidate_name"])
            if name in seen:
                continue
            seen.add(name)
            generation_two_records.append(record)
    _attach_stability(
        generation_two_records,
        fitted=fitted,
        validation=validation,
        comparison_fn=comparison_fn,
    )
    records.extend(generation_two_records)
    generation_two_seconds = perf_counter() - generation_two_started

    robust_after_two = sorted(
        [
            record
            for record in records
            if bool((record.get("stability") or {}).get("pass"))
        ],
        key=_stability_score,
        reverse=True,
    )
    generation_three_started = perf_counter()
    generation_three_records: list[dict] = []
    for spec in _generation_three(
        robust_after_two[:DEFAULT_GENERATION3_PARENTS],
        fitted=fitted,
        max_children=DEFAULT_GENERATION3_MAX_CHILDREN,
    ):
        record = _evaluate(
            spec,
            fitted=fitted,
            train=train,
            validation=validation,
            comparison_fn=comparison_fn,
        )
        if record is None:
            continue
        name = str(record["candidate_name"])
        if name in seen:
            continue
        seen.add(name)
        generation_three_records.append(record)
    _attach_stability(
        generation_three_records,
        fitted=fitted,
        validation=validation,
        comparison_fn=comparison_fn,
    )
    records.extend(generation_three_records)
    generation_three_seconds = perf_counter() - generation_three_started

    stable = [record for record in records if record.get("stable_train_validation")]
    stability_survivors = [
        record for record in stable if bool((record.get("stability") or {}).get("pass"))
    ]
    selected = (
        max(stability_survivors, key=_stability_score)
        if stability_survivors
        else None
    )
    final_stage_started = perf_counter()
    critic = {
        "status": "NOT_RUN",
        "pass": False,
        "reason": "no_train_validation_survivor",
    }
    final_test: dict = {}
    follow = False
    if selected is not None:
        critic = _temporal_critic(
            selected,
            fitted=fitted,
            validation=validation,
            comparison_fn=comparison_fn,
        )
        if bool(critic.get("pass")) and bool(touch_final_test):
            models = _build_models(fitted, selected.get("spec") or {})
            final_test = _compact_view(
                comparison_fn(
                    test,
                    models,
                    int((selected.get("spec") or {}).get("daily_capacity") or 1),
                )
            )
            follow = bool((final_test.get("gate") or {}).get("pass"))

    screen_survivor = bool(
        selected is not None
        and bool((selected.get("stability") or {}).get("pass"))
        and bool(critic.get("pass"))
    )
    final_test_status = (
        "EVALUATED"
        if bool(touch_final_test) and screen_survivor
        else ("LOCKED" if screen_survivor else "NOT_ELIGIBLE")
    )
    final_stage_seconds = perf_counter() - final_stage_started
    ranked = sorted(records, key=_rank_score, reverse=True)
    selected_name = str(selected.get("candidate_name") or "") if selected is not None else None
    dataset_fingerprint = _fingerprint(train + validation + test, lane_label)
    registry_started = perf_counter()
    registry = _persist_registry(
        registry_path,
        dataset_fingerprint=dataset_fingerprint,
        lane_label=lane_label,
        records=records,
        selected_name=selected_name,
        final_test=final_test,
    )
    registry_seconds = perf_counter() - registry_started
    total_seconds = perf_counter() - started

    return {
        "mode": MODE,
        "lane": str(lane_label).upper(),
        "status": "EVALUABLE",
        "live_mutation": False,
        "promotion_allowed": False,
        "auto_promote_live": False,
        "dataset_fingerprint": dataset_fingerprint,
        "candidate_count": len(records),
        "generation_counts": {
            "generation_1": sum(int(row.get("generation") or 0) == 1 for row in records),
            "generation_2": sum(int(row.get("generation") or 0) == 2 for row in records),
            "generation_3": sum(int(row.get("generation") or 0) == 3 for row in records),
        },
        "stable_train_validation_count": len(stable),
        "stability_survivor_count": len(stability_survivors),
        "selected_candidate": selected_name,
        "selected_spec": dict((selected or {}).get("spec") or {}),
        "selected_features": dict((selected or {}).get("selected_features") or {}),
        "selected_stability": dict((selected or {}).get("stability") or {}),
        "critic": critic,
        "screen_survivor": screen_survivor,
        "final_test_status": final_test_status,
        "final_test": final_test,
        "historical_followup_candidate": follow,
        "performance": {
            "total_elapsed_seconds": round(total_seconds, 3),
            "generation_1_seconds": round(generation_one_seconds, 3),
            "generation_2_seconds": round(generation_two_seconds, 3),
            "generation_3_seconds": round(generation_three_seconds, 3),
            "critic_and_final_test_seconds": round(final_stage_seconds, 3),
            "registry_seconds": round(registry_seconds, 3),
            "candidates_per_second": round(len(records) / total_seconds, 4) if total_seconds > 0 else None,
        },
        "leaderboard": [
            {
                "rank": idx,
                "candidate_name": row.get("candidate_name"),
                "generation": row.get("generation"),
                "parent": row.get("parent"),
                "stable_train_validation": bool(row.get("stable_train_validation")),
                "stability": row.get("stability") or {},
                "spec": row.get("spec"),
                "train": row.get("train"),
                "validation": row.get("validation"),
            }
            for idx, row in enumerate(ranked[: max(1, int(top_report))], start=1)
        ],
        "stability_candidates": [
            {
                "candidate_name": row.get("candidate_name"),
                "generation": row.get("generation"),
                "parent": row.get("parent"),
                "spec": row.get("spec") or {},
                "stability": row.get("stability") or {},
                "train": row.get("train") or {},
                "validation": row.get("validation") or {},
            }
            for row in ranked
            if bool((row.get("stability") or {}).get("pass"))
        ],
        "registry": registry,
        "policy": {
            "frozen_dataset_reused_for_many_candidates": True,
            "candidate_grammar_fixed_before_test": True,
            "train_validation_selects_single_winner": True,
            "validation_stability_folds": STABILITY_FOLDS,
            "generation_3_stability_mutations": True,
            "worst_fold_reliability_prioritized": True,
            "temporal_critic_before_final_test": True,
            "final_test_touched_only_for_selected_candidate": True,
            "final_test_access_enabled_for_this_run": bool(touch_final_test),
            "future_prices_labels_only": True,
            "source_code_self_mutation": False,
            "live_mutation": False,
            "human_gate_required_for_any_live_change": True,
        },
    }


def evaluate_explicit_candidate_final_test(
    *,
    train: list[dict],
    validation: list[dict],
    test: list[dict],
    fitted: dict,
    comparison_fn: Callable[[list[dict], dict[str, list[dict]], int], dict],
    lane_label: str,
    spec: dict,
) -> dict:
    """Evaluate one pre-selected, stability-qualified candidate on FINAL TEST."""
    record = _evaluate(
        spec,
        fitted=fitted,
        train=train,
        validation=validation,
        comparison_fn=comparison_fn,
    )
    if record is None:
        return {
            "status": "INVALID_SPEC",
            "lane": str(lane_label).upper(),
            "final_test": {},
            "historical_followup_candidate": False,
        }
    _attach_stability(
        [record],
        fitted=fitted,
        validation=validation,
        comparison_fn=comparison_fn,
    )
    critic = _temporal_critic(
        record,
        fitted=fitted,
        validation=validation,
        comparison_fn=comparison_fn,
    )
    eligible = bool(
        record.get("stable_train_validation")
        and (record.get("stability") or {}).get("pass")
        and critic.get("pass")
    )
    if not eligible:
        return {
            "status": "NOT_ELIGIBLE",
            "lane": str(lane_label).upper(),
            "candidate_name": record.get("candidate_name"),
            "spec": record.get("spec") or {},
            "stability": record.get("stability") or {},
            "critic": critic,
            "final_test": {},
            "historical_followup_candidate": False,
        }

    models = _build_models(fitted, record.get("spec") or {})
    final_test = _compact_view(
        comparison_fn(
            test,
            models,
            int((record.get("spec") or {}).get("daily_capacity") or 1),
        )
    )
    return {
        "status": "EVALUATED",
        "lane": str(lane_label).upper(),
        "candidate_name": record.get("candidate_name"),
        "spec": record.get("spec") or {},
        "selected_features": record.get("selected_features") or {},
        "stability": record.get("stability") or {},
        "critic": critic,
        "train": record.get("train") or {},
        "validation": record.get("validation") or {},
        "final_test": final_test,
        "historical_followup_candidate": bool(
            (final_test.get("gate") or {}).get("pass")
        ),
    }


def _dual_lane_consensus(rolling: dict, fixed: dict) -> dict:
    rmap = {
        str(row.get("candidate_name") or ""): row
        for row in (rolling.get("stability_candidates") or [])
        if row.get("candidate_name")
    }
    fmap = {
        str(row.get("candidate_name") or ""): row
        for row in (fixed.get("stability_candidates") or [])
        if row.get("candidate_name")
    }
    common = sorted(set(rmap).intersection(fmap))
    if not common:
        return {
            "status": "NONE",
            "candidate": None,
            "spec": {},
            "common_candidate_count": 0,
        }

    def score(name: str) -> tuple:
        rr, ff = rmap[name], fmap[name]
        rs, fs = rr.get("stability") or {}, ff.get("stability") or {}
        rv = (rr.get("validation") or {}).get("challenger") or {}
        fv = (ff.get("validation") or {}).get("challenger") or {}
        return (
            min(float(_f(rs.get("min_precision_lift_pp")) or -1e12), float(_f(fs.get("min_precision_lift_pp")) or -1e12)),
            min(float(_f(rs.get("min_profit_factor")) or -1e12), float(_f(fs.get("min_profit_factor")) or -1e12)),
            min(float(_f(rs.get("min_return_pct")) or -1e12), float(_f(fs.get("min_return_pct")) or -1e12)),
            -max(float(_f(rs.get("precision_lift_range_pp")) or 1e12), float(_f(fs.get("precision_lift_range_pp")) or 1e12)),
            min(float(_f(rv.get("precision_pct")) or -1e12), float(_f(fv.get("precision_pct")) or -1e12)),
            min(float(_f(rv.get("horizon_close_profit_factor")) or -1e12), float(_f(fv.get("horizon_close_profit_factor")) or -1e12)),
            min(float(_f(rv.get("avg_horizon_close_return_pct")) or -1e12), float(_f(fv.get("avg_horizon_close_return_pct")) or -1e12)),
            name,
        )

    winner = max(common, key=score)
    return {
        "status": "FOUND",
        "candidate": winner,
        "spec": dict((rmap[winner].get("spec") or {})),
        "common_candidate_count": len(common),
        "rolling_stability": rmap[winner].get("stability") or {},
        "fixed_stability": fmap[winner].get("stability") or {},
        "rolling_validation": rmap[winner].get("validation") or {},
        "fixed_validation": fmap[winner].get("validation") or {},
    }


def combine_autonomous_research_lanes(rolling: dict, fixed: dict) -> dict:
    rn = rolling.get("selected_candidate")
    fn = fixed.get("selected_candidate")
    same = bool(rn and fn and rn == fn)
    consensus = _dual_lane_consensus(rolling, fixed)
    consensus_candidate = consensus.get("candidate")
    screen_dual = bool(consensus_candidate)
    ro = bool(rolling.get("historical_followup_candidate"))
    fo = bool(fixed.get("historical_followup_candidate"))
    dual = bool(same and ro and fo)
    return {
        "mode": "AUTONOMOUS_RESEARCH_DUAL_LANE_DECISION_V2",
        "status": (
            "EVALUABLE"
            if rolling.get("status") == fixed.get("status") == "EVALUABLE"
            else "PARTIAL"
        ),
        "rolling_selected_candidate": rn,
        "fixed_selected_candidate": fn,
        "same_individual_winner_across_lanes": same,
        "dual_lane_consensus": consensus,
        "dual_lane_consensus_candidate": consensus_candidate,
        "dual_lane_screen_survivor": screen_dual,
        "rolling_final_pass": ro,
        "fixed_final_pass": fo,
        "dual_lane_survivor": dual,
        "promotion_allowed": False,
        "live_mutation": False,
        "next_step": (
            "FREEZE_AUTONOMOUS_CANDIDATE_FOR_PROSPECTIVE_SHADOW"
            if dual
            else (
                "EXPLICIT_FINAL_TEST_REQUIRED"
                if screen_dual
                else "KEEP_RESEARCHING_NO_AUTONOMOUS_SURVIVOR"
            )
        ),
    }

