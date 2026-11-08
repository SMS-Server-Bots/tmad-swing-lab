from __future__ import annotations

"""Adaptive, validation-only TMAD research over frozen fast-research caches.

The cycle expands the 6.2.52 stability search without rebuilding historical
replay. It NEVER reads FINAL TEST. Exact same candidate specs are evaluated in
ROLLING_LATEST and FIXED_BENCHMARK, and progress is ranked by worst-case
cross-lane stability.

This is a bounded deterministic search, not an unconstrained self-modifier.
"""

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

import config
from autonomous_research_cache import (
    cache_filename,
    load_fast_research_cache,
    split_cached_rows,
)
from autonomous_research_lab import (
    BLOCKS,
    DAILY_CAPACITIES,
    _attach_stability,
    _build_models,
    _candidate_name,
    _evaluate,
    _models_usable,
    _stability_score,
    _temporal_critic,
    run_autonomous_research_lane,
)
from core_context_precision_optimizer import (
    _comparison,
    _fast_cache_contract_hash,
    _fit_train_models,
)

MODE = "ADAPTIVE_VALIDATION_SEARCH_V1"
LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")
LIMIT_BOUNDS = {
    "node": (1, 6),
    "market": (1, 8),
    "sector": (0, 6),
    "relation": (0, 6),
    "interaction": (0, 10),
}


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


def _load_cache(lane: str) -> dict:
    path = (
        config.RUNTIME_ROOT
        / "autonomous_research"
        / "cache"
        / cache_filename(lane)
    )
    return load_fast_research_cache(
        path,
        expected_contract_hash=_fast_cache_contract_hash(),
        verify_fingerprint=bool(
            getattr(config, "FAST_RESEARCH_CACHE_VERIFY_ON_LOAD", True)
        ),
    )


def _lane_context(cache: dict) -> dict:
    train, validation, test = split_cached_rows(cache)
    return {
        "cache": cache,
        "train": train,
        "validation": validation,
        # retained only to prove the frozen split exists; never passed to
        # adaptive candidate evaluation in this module.
        "test_row_count": len(test),
        "fitted": _fit_train_models(train),
    }


def _canonical_spec(spec: dict, *, generation: int, parent: str) -> dict:
    return {
        "generation": int(generation),
        "parent": str(parent),
        "limits": {
            block: int((spec.get("limits") or {}).get(block) or 0)
            for block in BLOCKS
        },
        "daily_capacity": int(spec.get("daily_capacity") or 1),
        "drop_top": {
            str(block): int(value or 0)
            for block, value in sorted((spec.get("drop_top") or {}).items())
            if int(value or 0) > 0
        },
    }


def _spec_key(spec: dict) -> str:
    return _candidate_name(spec)


def _mutate(parent: dict, *, generation: int, max_children: int) -> list[dict]:
    base = parent.get("spec") or {}
    parent_name = str(parent.get("candidate_name") or _candidate_name(base))
    limits = {
        block: int((base.get("limits") or {}).get(block) or 0)
        for block in BLOCKS
    }
    capacity = int(base.get("daily_capacity") or 1)
    base_drop = dict(base.get("drop_top") or {})
    raw: list[dict] = []

    # Local architecture neighborhood: one limit step up/down.
    for block in BLOCKS:
        low, high = LIMIT_BOUNDS[block]
        current = limits[block]
        for delta in (-1, 1):
            nxt = current + delta
            if nxt < low or nxt > high:
                continue
            changed = dict(limits)
            changed[block] = nxt
            raw.append({
                "generation": generation,
                "parent": parent_name,
                "limits": changed,
                "daily_capacity": capacity,
                "drop_top": dict(base_drop),
            })

    # Capacity neighborhood.
    if capacity in DAILY_CAPACITIES:
        idx = DAILY_CAPACITIES.index(capacity)
        for next_idx in (idx - 1, idx + 1):
            if 0 <= next_idx < len(DAILY_CAPACITIES):
                raw.append({
                    "generation": generation,
                    "parent": parent_name,
                    "limits": dict(limits),
                    "daily_capacity": int(DAILY_CAPACITIES[next_idx]),
                    "drop_top": dict(base_drop),
                })

    # Feature-rank shift: drop one more top feature in one block.
    for block in BLOCKS:
        drop = dict(base_drop)
        drop[block] = int(drop.get(block) or 0) + 1
        if drop[block] <= 2:
            raw.append({
                "generation": generation,
                "parent": parent_name,
                "limits": dict(limits),
                "daily_capacity": capacity,
                "drop_top": drop,
            })

    # Two-block simplification around complex survivors.
    nonzero = [block for block in BLOCKS if limits[block] > LIMIT_BOUNDS[block][0]]
    for idx, left in enumerate(nonzero):
        for right in nonzero[idx + 1:]:
            changed = dict(limits)
            changed[left] -= 1
            changed[right] -= 1
            raw.append({
                "generation": generation,
                "parent": parent_name,
                "limits": changed,
                "daily_capacity": capacity,
                "drop_top": dict(base_drop),
            })

    out: list[dict] = []
    seen: set[str] = set()
    for spec in raw:
        key = _spec_key(spec)
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
        if len(out) >= max(1, int(max_children)):
            break
    return out


def _eval_spec(spec: dict, lane_ctx: dict) -> dict | None:
    fitted = lane_ctx["fitted"]
    if not _models_usable(_build_models(fitted, spec)):
        return None
    record = _evaluate(
        spec,
        fitted=fitted,
        train=lane_ctx["train"],
        validation=lane_ctx["validation"],
        comparison_fn=_comparison,
    )
    if record is None:
        return None
    _attach_stability(
        [record],
        fitted=fitted,
        validation=lane_ctx["validation"],
        comparison_fn=_comparison,
    )
    if record.get("stable_train_validation") and (record.get("stability") or {}).get("pass"):
        critic = _temporal_critic(
            record,
            fitted=fitted,
            validation=lane_ctx["validation"],
            comparison_fn=_comparison,
        )
    else:
        critic = {
            "status": "NOT_ELIGIBLE",
            "pass": False,
            "reason": "train_validation_or_stability_failed",
        }
    record["critic"] = critic
    record["screen_survivor"] = bool(
        record.get("stable_train_validation")
        and (record.get("stability") or {}).get("pass")
        and critic.get("pass")
    )
    # Explicit guarantee: this module never computes FINAL TEST.
    record["final_test_status"] = "LOCKED"
    record["final_test"] = {}
    return record


def _cross_lane_score(name: str, lane_maps: dict[str, dict[str, dict]]) -> tuple:
    records = [lane_maps[lane][name] for lane in LANES]
    stabilities = [record.get("stability") or {} for record in records]
    validations = [(record.get("validation") or {}).get("challenger") or {} for record in records]

    def vals(key: str, source: list[dict], default: float) -> list[float]:
        out = []
        for row in source:
            try:
                out.append(float(row.get(key)))
            except Exception:
                out.append(default)
        return out

    min_precision_lifts = vals("min_precision_lift_pp", stabilities, -1e12)
    min_pfs = vals("min_profit_factor", stabilities, -1e12)
    min_returns = vals("min_return_pct", stabilities, -1e12)
    ranges = vals("precision_lift_range_pp", stabilities, 1e12)
    headline_precision = vals("precision_pct", validations, -1e12)
    headline_pf = vals("horizon_close_profit_factor", validations, -1e12)
    headline_return = vals("avg_horizon_close_return_pct", validations, -1e12)
    spec = records[0].get("spec") or {}
    complexity = sum(int(v or 0) for v in (spec.get("limits") or {}).values())

    return (
        min(min_precision_lifts),
        min(min_pfs),
        min(min_returns),
        -max(ranges),
        min(headline_precision),
        min(headline_pf),
        min(headline_return),
        -float(complexity),
        -float(int(spec.get("daily_capacity") or 999)),
        name,
    )


def _seed_specs(base_results: dict[str, dict], max_parents: int) -> list[dict]:
    pool: dict[str, dict] = {}
    for lane in LANES:
        result = base_results.get(lane) or {}
        for row in result.get("stability_candidates") or []:
            name = str(row.get("candidate_name") or "")
            if name:
                pool[name] = row

    common = []
    rolling_names = {
        str(row.get("candidate_name") or "")
        for row in (base_results.get("ROLLING_LATEST") or {}).get("stability_candidates") or []
    }
    fixed_names = {
        str(row.get("candidate_name") or "")
        for row in (base_results.get("FIXED_BENCHMARK") or {}).get("stability_candidates") or []
    }
    common_names = rolling_names.intersection(fixed_names)
    for name in sorted(common_names):
        if name and name in pool:
            common.append(pool[name])

    # Prefer already common stability survivors. If none exist, expand around
    # best local survivors from both lanes so the next round can search for
    # convergence.
    source = common if common else list(pool.values())
    source = sorted(source, key=_stability_score, reverse=True)
    return source[: max(1, int(max_parents))]


def run_adaptive_validation_search(
    *,
    max_rounds: int | None = None,
    max_total_candidates: int | None = None,
    parents_per_round: int | None = None,
    children_per_parent: int | None = None,
    persist: bool = True,
) -> dict:
    max_rounds = int(
        max_rounds
        if max_rounds is not None
        else getattr(config, "ADAPTIVE_RESEARCH_MAX_ROUNDS", 3)
    )
    max_total_candidates = int(
        max_total_candidates
        if max_total_candidates is not None
        else getattr(config, "ADAPTIVE_RESEARCH_MAX_TOTAL_CANDIDATES", 240)
    )
    parents_per_round = int(
        parents_per_round
        if parents_per_round is not None
        else getattr(config, "ADAPTIVE_RESEARCH_PARENTS_PER_ROUND", 4)
    )
    children_per_parent = int(
        children_per_parent
        if children_per_parent is not None
        else getattr(config, "ADAPTIVE_RESEARCH_CHILDREN_PER_PARENT", 24)
    )

    lane_ctx: dict[str, dict] = {}
    base_results: dict[str, dict] = {}
    cache_meta: dict[str, dict] = {}

    for lane in LANES:
        cache = _load_cache(lane)
        ctx = _lane_context(cache)
        lane_ctx[lane] = ctx
        cache_meta[lane] = {
            "producer_version": cache.get("producer_version"),
            "dataset_fingerprint": cache.get("dataset_fingerprint"),
            "contract_hash": cache.get("contract_hash"),
            "row_count": cache.get("row_count"),
            "test_row_count": ctx["test_row_count"],
        }
        # Base 6.2.52 screen seeds the adaptive neighborhood. FINAL TEST locked.
        base_results[lane] = run_autonomous_research_lane(
            train=ctx["train"],
            validation=ctx["validation"],
            test=[{"date": "__LOCKBOX_PRESENT__"}],
            fitted=ctx["fitted"],
            comparison_fn=_comparison,
            lane_label=lane,
            registry_path=None,
            max_candidates=int(getattr(config, "AUTONOMOUS_RESEARCH_MAX_CANDIDATES", 48)),
            top_report=int(getattr(config, "AUTONOMOUS_RESEARCH_TOP_REPORT", 20)),
            touch_final_test=False,
        )

    evaluated_names: set[str] = set()
    all_lane_records: dict[str, dict[str, dict]] = {lane: {} for lane in LANES}
    rounds: list[dict] = []

    # Bring base stability candidates into the common comparison universe.
    for lane in LANES:
        for row in base_results[lane].get("stability_candidates") or []:
            name = str(row.get("candidate_name") or "")
            if name:
                all_lane_records[lane][name] = row

    parents = _seed_specs(base_results, parents_per_round)
    generation = 4
    best_common_name: str | None = None
    best_common_score: tuple | None = None

    for round_idx in range(1, max(1, max_rounds) + 1):
        specs: list[dict] = []
        spec_seen: set[str] = set()
        for parent in parents[: max(1, parents_per_round)]:
            for spec in _mutate(
                parent,
                generation=generation,
                max_children=children_per_parent,
            ):
                name = _spec_key(spec)
                if name in evaluated_names or name in spec_seen:
                    continue
                spec_seen.add(name)
                specs.append(spec)
                if len(evaluated_names) + len(specs) >= max_total_candidates:
                    break
            if len(evaluated_names) + len(specs) >= max_total_candidates:
                break

        if not specs:
            rounds.append({
                "round": round_idx,
                "generation": generation,
                "status": "NO_NEW_SPECS",
                "evaluated": 0,
                "common_survivors": 0,
            })
            break

        lane_round: dict[str, dict[str, dict]] = {lane: {} for lane in LANES}
        for spec in specs:
            name = _spec_key(spec)
            evaluated_names.add(name)
            for lane in LANES:
                record = _eval_spec(spec, lane_ctx[lane])
                if record is not None:
                    lane_round[lane][name] = record
                    all_lane_records[lane][name] = record

        common = sorted(
            name
            for name in set(lane_round["ROLLING_LATEST"]).intersection(
                lane_round["FIXED_BENCHMARK"]
            )
            if lane_round["ROLLING_LATEST"][name].get("screen_survivor")
            and lane_round["FIXED_BENCHMARK"][name].get("screen_survivor")
        )
        ranked_common = sorted(
            common,
            key=lambda name: _cross_lane_score(name, lane_round),
            reverse=True,
        )

        round_best = ranked_common[0] if ranked_common else None
        round_best_score = (
            _cross_lane_score(round_best, lane_round)
            if round_best is not None
            else None
        )
        improved = bool(
            round_best_score is not None
            and (best_common_score is None or round_best_score > best_common_score)
        )
        if improved:
            best_common_name = round_best
            best_common_score = round_best_score

        rounds.append({
            "round": round_idx,
            "generation": generation,
            "status": "EVALUATED",
            "candidate_specs": len(specs),
            "evaluated_total": len(evaluated_names),
            "common_survivors": len(ranked_common),
            "round_best_candidate": round_best,
            "improved_global_best": improved,
            "top_common": [
                {
                    "candidate_name": name,
                    "spec": lane_round["ROLLING_LATEST"][name].get("spec") or {},
                    "rolling_stability": lane_round["ROLLING_LATEST"][name].get("stability") or {},
                    "fixed_stability": lane_round["FIXED_BENCHMARK"][name].get("stability") or {},
                    "rolling_validation": lane_round["ROLLING_LATEST"][name].get("validation") or {},
                    "fixed_validation": lane_round["FIXED_BENCHMARK"][name].get("validation") or {},
                }
                for name in ranked_common[:10]
            ],
        })

        # Next round expands around best common survivors. If this round has no
        # common survivor, use strongest local survivors to search convergence.
        next_parents: list[dict] = []
        if ranked_common:
            for name in ranked_common[:parents_per_round]:
                next_parents.append(lane_round["ROLLING_LATEST"][name])
        else:
            local_pool = []
            for lane in LANES:
                local_pool.extend(
                    record
                    for record in lane_round[lane].values()
                    if record.get("screen_survivor")
                )
            local_pool.sort(key=_stability_score, reverse=True)
            dedup: set[str] = set()
            for row in local_pool:
                name = str(row.get("candidate_name") or "")
                if not name or name in dedup:
                    continue
                dedup.add(name)
                next_parents.append(row)
                if len(next_parents) >= parents_per_round:
                    break

        if not next_parents:
            break
        parents = next_parents
        generation += 1
        if len(evaluated_names) >= max_total_candidates:
            break

    # Include common survivors across ALL explored candidates, not only the last round.
    all_common = sorted(
        name
        for name in set(all_lane_records["ROLLING_LATEST"]).intersection(
            all_lane_records["FIXED_BENCHMARK"]
        )
        if all_lane_records["ROLLING_LATEST"][name].get("screen_survivor")
        and all_lane_records["FIXED_BENCHMARK"][name].get("screen_survivor")
    )
    all_common = sorted(
        all_common,
        key=lambda name: _cross_lane_score(name, all_lane_records),
        reverse=True,
    )
    consensus = all_common[0] if all_common else None
    consensus_spec = (
        dict(all_lane_records["ROLLING_LATEST"][consensus].get("spec") or {})
        if consensus else {}
    )

    # Generation 4 is failure-driven by design: do not broaden the search when
    # the existing grammar already produced a robust dual-lane survivor.
    gen4_failure_research = {
        "mode": "GEN4_FAILURE_DRIVEN_REGIME_RESEARCH_V1",
        "status": "SKIPPED_EXISTING_CONSENSUS" if consensus else "DISABLED",
        "consensus_candidate": None,
        "final_test_status": "LOCKED",
        "final_test": {},
        "live_mutation": False,
    }
    if (
        consensus is None
        and bool(getattr(config, "GEN4_FAILURE_RESEARCH_ENABLED", True))
    ):
        try:
            from regime_failure_research import run_gen4_failure_research

            gen4_failure_research = run_gen4_failure_research(
                lane_contexts={
                    lane: {
                        "train": lane_ctx[lane]["train"],
                        "validation": lane_ctx[lane]["validation"],
                        "fitted": lane_ctx[lane]["fitted"],
                    }
                    for lane in LANES
                },
                base_results=base_results,
                candidate_records=all_lane_records,
                persist=persist,
            )
        except Exception as exc:
            gen4_failure_research = {
                "mode": "GEN4_FAILURE_DRIVEN_REGIME_RESEARCH_V1",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {exc}",
                "consensus_candidate": None,
                "final_test_status": "LOCKED",
                "final_test": {},
                "live_mutation": False,
            }

    payload = {
        "schema_version": 1,
        "mode": MODE,
        "producer_version": config.BOT_VERSION,
        "generated_at": _now(),
        "status": "EVALUABLE",
        "cache": cache_meta,
        "budget": {
            "max_rounds": max_rounds,
            "max_total_candidates": max_total_candidates,
            "parents_per_round": parents_per_round,
            "children_per_parent": children_per_parent,
            "evaluated_adaptive_candidates": len(evaluated_names),
        },
        "base": {
            lane: {
                "candidate_count": base_results[lane].get("candidate_count"),
                "stability_survivor_count": base_results[lane].get("stability_survivor_count"),
                "selected_candidate": base_results[lane].get("selected_candidate"),
                "final_test_status": "LOCKED",
            }
            for lane in LANES
        },
        "rounds": rounds,
        "consensus_candidate": consensus,
        "consensus_spec": consensus_spec,
        "common_survivor_count": len(all_common),
        "gen4_failure_research": gen4_failure_research,
        "top_common_survivors": [
            {
                "rank": idx,
                "candidate_name": name,
                "spec": all_lane_records["ROLLING_LATEST"][name].get("spec") or {},
                "rolling_stability": all_lane_records["ROLLING_LATEST"][name].get("stability") or {},
                "fixed_stability": all_lane_records["FIXED_BENCHMARK"][name].get("stability") or {},
                "rolling_validation": all_lane_records["ROLLING_LATEST"][name].get("validation") or {},
                "fixed_validation": all_lane_records["FIXED_BENCHMARK"][name].get("validation") or {},
            }
            for idx, name in enumerate(all_common[:20], start=1)
        ],
        "final_test_status": "LOCKED",
        "final_test": {},
        "next_step": (
            "REVIEW_CONSENSUS_FOR_EXPLICIT_LOCKBOX"
            if consensus
            else (
                "REVIEW_GEN4_CONSENSUS_FOR_EXPLICIT_LOCKBOX"
                if gen4_failure_research.get("consensus_candidate")
                else "RETHINK_FEATURE_ARCHITECTURE_FROM_ROLLING_FAILURE_MAP"
            )
        ),
        "policy": {
            "frozen_cache_only": True,
            "validation_only": True,
            "final_test_never_read": True,
            "same_specs_evaluated_both_lanes": True,
            "worst_case_cross_lane_ranking": True,
            "bounded_deterministic_mutation": True,
            "gen4_failure_driven_after_no_consensus": True,
            "gen4_regime_router_train_validation_only": True,
            "source_code_self_mutation": False,
            "live_mutation": False,
            "auto_deploy": False,
            "auto_promote_live": False,
        },
    }

    if persist:
        runtime_root = config.RUNTIME_ROOT / "autonomous_research" / "adaptive_cycles"
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        _atomic_json(runtime_root / f"adaptive_cycle_{stamp}.json", payload)
        _atomic_json(runtime_root / "latest_adaptive_cycle.json", payload)
        _atomic_json(
            config.OUTPUT_DIR
            / "historical_validation"
            / "latest_adaptive_research_cycle.json",
            payload,
        )
    return payload


def main() -> int:
    payload = run_adaptive_validation_search(persist=True)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
