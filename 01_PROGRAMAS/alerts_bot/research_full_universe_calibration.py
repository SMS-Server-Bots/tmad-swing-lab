from __future__ import annotations

"""Full contextual mover-universe research; NO portfolio slots or future-price features.

This is deliberately independent of the entry/exit OHLC backfill. Every cached
ticker/date is scored before daily top-K selection. Locked holdout is not read.
"""
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import config
from autonomous_research_cache import cache_filename, load_fast_research_cache, split_cached_rows
from core_context_precision_optimizer import (
    _family_models, _fast_cache_contract_hash, _fit_train_models, _score_row,
)
from research_streaming_io import atomic_stream_json

MODE = "TMAD_FULL_UNIVERSE_MOVER_CALIBRATION_V1"
LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")
K_VALUES = (1, 3, 5, 10)
ARMS = (
    "CORE_CURRENT", "CONTEXT_FULL", "DROP_NODE", "DROP_MARKET",
    "DROP_SECTOR", "DROP_RELATION", "DROP_INTERACTION",
    "EXPLICIT_ETF_CONTEXT", "GEX_SAME_RUN_SUPPORT", "GEX_SAME_RUN_VETO",
)
MIN_VALIDATION_DATES = 8
MIN_VALIDATION_TOP5 = 20


def _f(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        x = float(value)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _key(row):
    return str(row.get("date") or ""), str(row.get("ticker") or "").upper()


def _gex_status(row: dict) -> str:
    return str((row.get("gex_context") or {}).get("status") or "NOT_CAPTURED")


def _same_run_gex_filter(row: dict, arm: str) -> bool:
    gex = row.get("gex_context") or {}
    same_run = bool(gex.get("same_run"))
    if arm == "GEX_SAME_RUN_SUPPORT":
        # Positive evidence: require same-run GEX supporting that direction.
        return same_run and bool(gex.get("same_direction"))
    if arm == "GEX_SAME_RUN_VETO":
        # Contradiction-only: missing GEX is UNKNOWN, not negative evidence.
        # Reject only same-run opposite-only evidence, not missing provenance.
        return not (same_run and bool(gex.get("opposite_direction"))
                    and not bool(gex.get("same_direction")))
    raise ValueError("UNKNOWN_GEX_FILTER_ARM")


def _side_score(row: dict, models: dict, arm: str) -> float | None:
    full, detail = _score_row(row, models)
    if full is None:
        return None
    blocks = (detail or {}).get("block_scores") or {}
    if arm.startswith("DROP_"):
        dropped = arm[5:].lower()
        remaining = [float(v) for k, v in blocks.items() if k != dropped]
        return mean(remaining) if len(remaining) >= 2 else None
    return float(full)


def _universe(rows: list[dict]) -> tuple[dict, dict]:
    groups: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    duplicates = 0
    for row in rows:
        direction = str(row.get("context_direction") or "").upper()
        if direction not in {"LONG", "SHORT"} or not all(_key(row)):
            continue
        pair = groups[_key(row)]
        if direction in pair:
            duplicates += 1
        pair[direction] = row

    pairs = {key: pair for key, pair in groups.items() if set(pair) == {"LONG", "SHORT"}}
    coverage = Counter()
    for pair in pairs.values():
        long = pair["LONG"]
        coverage["paired_ticker_dates"] += 1
        coverage["best_direction_movers_hindsight"] += int(bool(long.get("best_mover_5d")))
        coverage["current_core_pass"] += int(bool((long.get("stage") or {}).get("core")))
        coverage["context_usable"] += int(bool(long.get("context_usable")))
        coverage["sector_explicit"] += int(bool((long.get("context_diagnostics") or {}).get("explicit_sector_etf")))
        coverage["main_etf_2plus"] += int(int((long.get("context_diagnostics") or {}).get("explicit_main_etf_count") or 0) >= 2)
        coverage["vol_etf_available"] += int(int((long.get("context_diagnostics") or {}).get("explicit_vol_count") or 0) >= 1)
        coverage["gex_same_run_any_side"] += int(any((r.get("gex_context") or {}).get("same_run") for r in pair.values()))
        coverage["gex_timestamp_verified"] += int(any((r.get("gex_context") or {}).get("exact_timestamp_verified") for r in pair.values()))
    coverage["unpaired_ticker_dates"] = len(groups) - len(pairs)
    coverage["duplicate_directional_rows"] = duplicates
    return pairs, dict(coverage)


def _selected(pairs: dict, models: dict, arm: str) -> tuple[list[dict], dict]:
    """Score all ticker/date hypotheses first; apply top-K only afterward."""
    dated = defaultdict(list)
    blocked = Counter()
    for (day, ticker), sides in pairs.items():
        current = str(sides["LONG"].get("current_direction") or "").upper()
        choices = []
        for direction, row in sides.items():
            if arm == "EXPLICIT_ETF_CONTEXT":
                diagnostics = row.get("context_diagnostics") or {}
                if not (
                    int(diagnostics.get("explicit_main_etf_count") or 0) >= 2
                    and int(diagnostics.get("explicit_vol_count") or 0) >= 1
                    and bool(diagnostics.get("explicit_sector_etf"))
                ):
                    continue
            if arm == "CORE_CURRENT" and (
                direction != current or not bool((row.get("stage") or {}).get("core"))
            ):
                continue
            if arm == "CORE_CURRENT":
                rank = tuple(
                    _f(value) or 0.0 for value in (row.get("research_rank") or ())
                )
                if not rank:
                    continue
                score = rank[0]
                priority = rank
            else:
                score = _side_score(row, models, arm)
                if score is None:
                    continue
                priority = (float(score),)
            if arm.startswith("GEX_") and not _same_run_gex_filter(row, arm):
                continue
            choices.append((score, direction, row, priority))
        if not choices:
            blocked["NO_SCORE_OR_CONTEXT"] += 1
            if arm.startswith("GEX_") and not any(
                (row.get("gex_context") or {}).get("same_run") for row in sides.values()
            ):
                blocked["GEX_SAME_RUN_MISSING"] += 1
            continue
        score, direction, row, priority = max(choices, key=lambda x: (x[3], x[1]))
        path = row.get("long_path") if direction == "LONG" else row.get("short_path")
        dated[day].append({
            "date": day, "ticker": ticker, "direction": direction,
            "score": float(score),
            "point_in_time_rank_priority": priority,
            "selected_direction_mover": bool(row.get("directional_mover_5d")),
            "any_direction_mover": bool(row.get("best_mover_5d")),
            "mfe_pct": _f((path or {}).get("mfe_pct")),
            "mae_pct": _f((path or {}).get("mae_pct")),
            "h5_close_pct": _f((path or {}).get("horizon_close_return_pct")),
        })
    all_ranked = []
    for day in sorted(dated):
        dated[day].sort(
            key=lambda x: (tuple(-v for v in x["point_in_time_rank_priority"]), x["ticker"])
        )
        for rank, item in enumerate(dated[day], start=1):
            all_ranked.append({**item, "daily_rank": rank})
    return all_ranked, {"rejected_ticker_dates": dict(blocked)}


def _metrics(items: list[dict], denominator: dict, dates: list[str]) -> dict:
    tickers = {(_key(x)) for x in items}
    direction_hits = sum(x["selected_direction_mover"] for x in items)
    best_hits = sum(x["any_direction_mover"] for x in items)
    returns = [x["h5_close_pct"] for x in items if x["h5_close_pct"] is not None]
    # Returns here are hypothetical T0-to-5D gross labels, NOT next-open trades.
    return {
        "dates": len(dates),
        "selected": len(items),
        "selected_direction_movers": direction_hits,
        "best_direction_movers_in_selected": best_hits,
        "directional_precision_pct": round(100 * direction_hits / len(items), 3) if items else None,
        "directional_wrong_side_count": best_hits - direction_hits,
        "any_direction_mover_capture_pct": (
            round(100 * best_hits / denominator["best_direction_movers_hindsight"], 3)
            if denominator["best_direction_movers_hindsight"] else None
        ),
        "average_gross_h5_close_pct": round(mean(returns), 4) if returns else None,
        "return_label_coverage": len(returns),
        "false_positives": len(items) - direction_hits,
        "no_portfolio_slot_filter": True,
    }



def _mover_loss_attribution_top5(pairs: dict, ranked: list[dict]) -> dict:
    """Exhaustive mover loss partition from PRE-TopK paired frozen candidates.

    A mover is accounted for exactly once as unscoreable, below Top5, wrong
    direction or captured. No future information influences the selection.
    """
    scored = {(x["date"], x["ticker"]): x for x in ranked}
    selected = {key: row for key, row in scored.items() if row["daily_rank"] <= 5}
    losses = Counter()
    regimes = defaultdict(Counter)
    context = defaultdict(Counter)
    for key, pair in pairs.items():
        source = pair["LONG"]
        mover = bool(source.get("best_mover_5d"))
        item = scored.get(key)
        top = selected.get(key)
        diag = source.get("context_diagnostics") or {}
        regime = str(diag.get("market_regime") or "UNKNOWN")
        etf = "SECTOR_PRESENT" if diag.get("explicit_sector_etf") else "SECTOR_MISSING"
        vol = "VOL_PRESENT" if int(diag.get("explicit_vol_count") or 0) else "VOL_MISSING"
        gex = "GEX_SAME_RUN" if any(
            bool(r.get("gex_context", {}).get("same_run")) for r in pair.values()
        ) else "GEX_NOT_SAME_RUN"
        buckets = [regimes[regime], context[etf], context[vol], context[gex]]
        losses["all_paired"] += 1
        for bucket in buckets:
            bucket["all_paired"] += 1
        if item is None:
            category = "MISSED_UNSCOREABLE"
        elif top is None:
            category = "MISSED_BELOW_TOP5"
        elif not top["selected_direction_mover"]:
            category = "SELECTED_WRONG_DIRECTION" if mover else "SELECTED_NO_MOVER"
        else:
            category = "CAPTURED_CORRECT_DIRECTION"
        if mover:
            losses["all_hindsight_movers"] += 1
            losses[category] += 1
            for bucket in buckets:
                bucket["all_hindsight_movers"] += 1
                bucket[category] += 1
        if top:
            losses["selected_top5"] += 1
            for bucket in buckets:
                bucket["selected_top5"] += 1
            if not mover:
                losses["selected_no_mover"] += 1
                for bucket in buckets:
                    bucket["selected_no_mover"] += 1
            elif top["selected_direction_mover"]:
                losses["selected_direction_hit"] += 1
                for bucket in buckets:
                    bucket["selected_direction_hit"] += 1
    assert sum(losses[category] for category in (
        "MISSED_UNSCOREABLE", "MISSED_BELOW_TOP5",
        "SELECTED_WRONG_DIRECTION", "CAPTURED_CORRECT_DIRECTION",
    )) == losses["all_hindsight_movers"], "Mover loss partition must reconcile"
    return {
        "counts": dict(losses),
        "by_market_regime": {k: dict(v) for k, v in sorted(regimes.items())},
        "by_context_availability": {k: dict(v) for k, v in sorted(context.items())},
        "note": "Paired cached ticker-dates with trustworthy 5D labels only; no raw Scanner coverage claimed",
    }


def _top5_paired_delta(base: list[dict], challenger: list[dict]) -> dict:
    """Diagnostic ablation delta on a common paired candidate denominator."""
    control = {(r["date"], r["ticker"]): r for r in base if r["daily_rank"] <= 5}
    other = {(r["date"], r["ticker"]): r for r in challenger if r["daily_rank"] <= 5}
    overlap = set(control) & set(other)
    gained = set(other) - set(control)
    lost = set(control) - set(other)
    def hits(items: dict, keys: set) -> int:
        return sum(bool(items[key]["selected_direction_mover"]) for key in keys)
    return {
        "common_ticker_dates": len(overlap),
        "added_ticker_dates": len(gained),
        "removed_ticker_dates": len(lost),
        "changed_direction_on_overlap": sum(
            control[key]["direction"] != other[key]["direction"] for key in overlap
        ),
        "directional_hit_delta": (
            hits(other, set(other)) - hits(control, set(control))
        ),
        "selected_count_delta": len(other) - len(control),
        "diagnostic_only_no_validation_arm_selection": True,
    }


def evaluate_period(rows: list[dict], models: dict) -> dict:
    pairs, coverage = _universe(rows)
    dates = sorted({day for day, ticker in pairs})
    report = {"population": coverage, "dates": len(dates), "arms": {}}
    arm_ranked = {}
    for arm in ARMS:
        ranked, gaps = _selected(pairs, models, arm)
        arm_ranked[arm] = ranked
        cohorts = {}
        for k in K_VALUES:
            top = [r for r in ranked if int(r["daily_rank"]) <= k]
            metrics = _metrics(top, coverage, dates)
            metrics["mover_labels_unselected_by_top_k"] = (
                coverage["best_direction_movers_hindsight"]
                - len({(x["date"], x["ticker"]) for x in top if x["any_direction_mover"]})
            )
            cohorts[str(k)] = metrics
        folds = []
        if len(dates) >= 3:
            for fold in range(3):
                fold_dates = set(dates[fold * len(dates) // 3:(fold + 1) * len(dates) // 3])
                fold_top = [x for x in ranked if x["date"] in fold_dates and x["daily_rank"] <= 5]
                denom = {
                    "best_direction_movers_hindsight": sum(
                        bool(pair["LONG"].get("best_mover_5d"))
                        for (day, _), pair in pairs.items() if day in fold_dates
                    )
                }
                folds.append(_metrics(fold_top, denom, sorted(fold_dates)))
        report["arms"][arm] = {
            "all_scoreable_ticker_dates": len(ranked),
            "population_filter": gaps,
            "top_k_without_portfolio": cohorts,
            "validation_chronological_folds_top5": folds,
            "mover_loss_attribution_top5": _mover_loss_attribution_top5(pairs, ranked),
        }
    baseline = arm_ranked["CONTEXT_FULL"]
    for arm in ARMS:
        report["arms"][arm]["top5_delta_vs_context_full"] = _top5_paired_delta(
            baseline, arm_ranked[arm]
        )
    return report


def _load_lane(lane: str) -> tuple[list[dict], list[dict], dict]:
    payload = load_fast_research_cache(
        config.RUNTIME_ROOT / "autonomous_research" / "cache" / cache_filename(lane),
        expected_contract_hash=_fast_cache_contract_hash(),
        verify_fingerprint=True,
    )
    train, validation, _holdout = split_cached_rows(payload)
    # Independent chronological research periods, explicitly purge 5 sessions
    # from TRAIN and VALIDATION to prevent forward labels touching next period.
    train_days = sorted({str(r.get("date") or "") for r in train})
    val_days = sorted({str(r.get("date") or "") for r in validation})
    if len(train_days) <= 5 or len(val_days) <= 5:
        raise ValueError("INSUFFICIENT_CHRONOLOGICAL_DATES_FOR_H5_PURGE")
    keep_train = set(train_days[:-5])
    keep_val = set(val_days[:-5])
    return (
        [row for row in train if row.get("date") in keep_train],
        [row for row in validation if row.get("date") in keep_val],
        {
            "cache_fingerprint": payload.get("dataset_fingerprint"),
            "contract_hash": payload.get("contract_hash"),
            "producer_version": payload.get("producer_version"),
            "train_dates": len(keep_train),
            "validation_dates": len(keep_val),
            "final_test": "LOCKED",
        }
    )



def _frozen_validation_audit(lanes: dict, frozen_arm: str | None) -> dict:
    """Evaluate a TRAIN-frozen ablation against unchanged contemporaneous controls.

    No search, winner selection, or weight tuning on validation is permitted.
    This decision NEVER authorizes LIVE; it only rejects or marks a challenger
    for further independent prospective research.
    """
    if not frozen_arm:
        return {
            "status": "NO_TRAIN_ELIGIBLE_CHALLENGER",
            "per_lane": {},
            "live_mutation": False,
            "promotion_allowed": False,
        }
    per_lane = {}
    for lane in LANES:
        data = (lanes.get(lane) or {}).get("validation") or {}
        arms = data.get("arms") or {}
        challenger = ((arms.get(frozen_arm) or {}).get("top_k_without_portfolio") or {}).get("5") or {}
        context = ((arms.get("CONTEXT_FULL") or {}).get("top_k_without_portfolio") or {}).get("5") or {}
        core = ((arms.get("CORE_CURRENT") or {}).get("top_k_without_portfolio") or {}).get("5") or {}
        n = int(challenger.get("selected") or 0)
        dates = int(challenger.get("dates") or 0)
        delta_context = (
            round(float(challenger["directional_precision_pct"]) - float(context["directional_precision_pct"]), 3)
            if challenger.get("directional_precision_pct") is not None
            and context.get("directional_precision_pct") is not None else None
        )
        delta_core = (
            round(float(challenger["directional_precision_pct"]) - float(core["directional_precision_pct"]), 3)
            if challenger.get("directional_precision_pct") is not None
            and core.get("directional_precision_pct") is not None else None
        )
        ret = challenger.get("average_gross_h5_close_pct")
        core_ret = core.get("average_gross_h5_close_pct")
        control_ret = context.get("average_gross_h5_close_pct")
        reasons = []
        if n < MIN_VALIDATION_TOP5 or dates < MIN_VALIDATION_DATES:
            reasons.append("INSUFFICIENT_VALIDATION_SAMPLE")
        if delta_context is None or delta_core is None:
            reasons.append("MISSING_PRECISION")
        else:
            if delta_context < 0:
                reasons.append("WORSE_THAN_CONTEXT_FULL_DIRECTIONAL_PRECISION")
            if delta_core < 0:
                reasons.append("WORSE_THAN_CURRENT_CORE_DIRECTIONAL_PRECISION")
        if ret is None or control_ret is None or core_ret is None:
            reasons.append("MISSING_GROSS_RETURN_LABEL")
        elif float(ret) < max(float(control_ret), float(core_ret)):
            reasons.append("WORSE_THAN_CONTROL_H5_GROSS_LABEL")
        per_lane[lane] = {
            "selected": n,
            "dates": dates,
            "challenger_directional_precision_pct": challenger.get("directional_precision_pct"),
            "context_full_directional_precision_pct": context.get("directional_precision_pct"),
            "core_current_directional_precision_pct": core.get("directional_precision_pct"),
            "directional_precision_delta_vs_context_pp": delta_context,
            "directional_precision_delta_vs_core_pp": delta_core,
            "challenger_h5_gross_label_pct": ret,
            "context_full_h5_gross_label_pct": control_ret,
            "core_current_h5_gross_label_pct": core_ret,
            "reasons": reasons,
        }
    return {
        "status": (
            "REJECTED_VALIDATION"
            if any(item["reasons"] for item in per_lane.values())
            else "SURVIVED_VALIDATION_FOR_FURTHER_RESEARCH_ONLY"
        ),
        "frozen_arm": frozen_arm,
        "per_lane": per_lane,
        "no_validation_selection": True,
        "final_test": "LOCKED",
        "live_mutation": False,
        "promotion_allowed": False,
    }



def _validation_lane_evidence(rows: list[dict], models: dict, train_dates: set[str]) -> dict:
    """Research-only audit input: exact validation identities and CONTEXT_FULL Top5.

    Identical ticker/date outcomes must reconcile between lanes before treating
    their precision differences as model/regime effects. No holdout is used.
    """
    pairs, _ = _universe(rows)
    ranked, _ = _selected(pairs, models, "CONTEXT_FULL")
    population = {}
    for key, sides in pairs.items():
        row = sides["LONG"]
        population[key] = {
            "mover": bool(row.get("best_mover_5d")),
            "regime": str((row.get("context_diagnostics") or {}).get("market_regime") or "UNKNOWN"),
        }
    top = {}
    for item in ranked:
        if int(item["daily_rank"]) > 5:
            continue
        key = _key(item)
        src = pairs[key][item["direction"]]
        diag = src.get("context_diagnostics") or {}
        top[key] = {
            "direction": item["direction"],
            "hit": bool(item["selected_direction_mover"]),
            "any_mover": bool(item["any_direction_mover"]),
            "regime": str(diag.get("market_regime") or "UNKNOWN"),
            "sector_available": bool(diag.get("explicit_sector_etf")),
            "same_run_gex": bool((src.get("gex_context") or {}).get("same_run")),
        }
    return {
        "train_dates": set(train_dates),
        "validation_dates": {key[0] for key in pairs},
        "population": population,
        "top5": top,
    }


def _selection_regime_breakdown(top: dict) -> dict:
    regimes = defaultdict(Counter)
    directions = defaultdict(Counter)
    for row in top.values():
        regime = str(row.get("regime") or "UNKNOWN")
        side = str(row.get("direction") or "UNKNOWN")
        for group in (regimes[regime], directions[side]):
            group["selected"] += 1
            group["directional_hits"] += int(bool(row.get("hit")))
            group["wrong_direction_movers"] += int(
                bool(row.get("any_mover")) and not bool(row.get("hit"))
            )
            group["no_mover"] += int(not bool(row.get("any_mover")))
    def summarize(grouped: dict) -> dict:
        return {
            key: {
                **dict(counts),
                "directional_precision_pct": (
                    round(100.0 * counts["directional_hits"] / counts["selected"], 3)
                    if counts["selected"] else None
                ),
            }
            for key, counts in sorted(grouped.items())
        }
    return {"by_regime": summarize(regimes), "by_direction": summarize(directions)}


def _dual_lane_validation_audit(evidence: dict) -> dict:
    """Quantify date/population overlap and same-key policy disagreement.

    Cross-lane samples may overlap and are NOT independent out-of-sample
    replications. This is analysis, never a validation model-selection step.
    """
    rolling = evidence["ROLLING_LATEST"]
    fixed = evidence["FIXED_BENCHMARK"]
    roll_dates, fixed_dates = rolling["validation_dates"], fixed["validation_dates"]
    roll_pop, fixed_pop = rolling["population"], fixed["population"]
    roll_top, fixed_top = rolling["top5"], fixed["top5"]
    shared_dates = roll_dates & fixed_dates
    shared_keys = set(roll_pop) & set(fixed_pop)
    shared_selected = set(roll_top) & set(fixed_top)
    label_mismatches = sum(
        roll_pop[key]["mover"] != fixed_pop[key]["mover"] for key in shared_keys
    )
    regime_mismatches = sum(
        roll_pop[key]["regime"] != fixed_pop[key]["regime"] for key in shared_keys
    )
    direction_disagreements = sum(
        roll_top[key]["direction"] != fixed_top[key]["direction"]
        for key in shared_selected
    )
    per_lane = {}
    for lane, payload in evidence.items():
        on_common_dates = {
            key: row for key, row in payload["top5"].items()
            if key[0] in shared_dates
        }
        per_lane[lane] = {
            "validation_dates": len(payload["validation_dates"]),
            "training_dates_also_in_other_validation": len(
                payload["train_dates"]
                & (fixed_dates if lane == "ROLLING_LATEST" else roll_dates)
            ),
            "unique_validation_ticker_dates": len(payload["population"]),
            "top5_all_dates": _selection_regime_breakdown(payload["top5"]),
            "top5_shared_dates": {
                "selected": len(on_common_dates),
                "directional_hits": sum(bool(row["hit"]) for row in on_common_dates.values()),
                "directional_precision_pct": (
                    round(100 * sum(bool(row["hit"]) for row in on_common_dates.values()) / len(on_common_dates), 3)
                    if on_common_dates else None
                ),
                "breakdown": _selection_regime_breakdown(on_common_dates),
            },
        }
    return {
        "validation_overlap_dates": len(shared_dates),
        "validation_union_dates": len(roll_dates | fixed_dates),
        "overlap_ticker_date_pairs": len(shared_keys),
        "union_ticker_date_pairs": len(set(roll_pop) | set(fixed_pop)),
        "overlap_label_disagreements": label_mismatches,
        "overlap_market_regime_disagreements": regime_mismatches,
        "top5_common_selected_ticker_dates": len(shared_selected),
        "top5_direction_disagreements_for_common_selected": direction_disagreements,
        "top5_shared_direction_agreement_pct": (
            round(100 * (len(shared_selected) - direction_disagreements) / len(shared_selected), 3)
            if shared_selected else None
        ),
        "per_lane": per_lane,
        "rolling_fixed_are_independent_oos": False,
        "validation_used_to_select_models": False,
        "interpretation": (
            "If shared-date precision converges, divergence is cohort composition; "
            "if not, inspect regime, source-run and model-fit differences. "
            "Selection remains TRAIN-frozen; no LIVE promotion."
        ),
    }


def run() -> dict:
    lanes = {}
    lane_validation_evidence = {}
    for lane in LANES:
        train, validation, cache = _load_lane(lane)
        models = _family_models(_fit_train_models(train), "CONTEXT_BALANCED")
        lane_validation_evidence[lane] = _validation_lane_evidence(
            validation, models, {str(x.get("date") or "") for x in train}
        )
        lanes[lane] = {
            "cache": cache,
            "train": evaluate_period(train, models),
            "validation": evaluate_period(validation, models),
        }
    dual_lane_validation_audit = _dual_lane_validation_audit(lane_validation_evidence)
    # Freeze exactly one configuration from TRAIN in BOTH lanes, never by
    # searching validation. A positive result only authorizes more research.
    common = []
    for arm in ARMS:
        train_summaries = [
            lanes[lane]["train"]["arms"][arm]["top_k_without_portfolio"]["5"]
            for lane in LANES
        ]
        if all(int(s["selected"]) >= 30 and int(s["dates"]) >= 12
               and s["directional_precision_pct"] is not None for s in train_summaries):
            common.append((
                min(float(s["directional_precision_pct"]) for s in train_summaries),
                min(float(s["average_gross_h5_close_pct"] or -999) for s in train_summaries),
                arm,
            ))
    train_frozen_arm = max(common)[2] if common else None
    frozen_validation = {
        lane: (
            lanes[lane]["validation"]["arms"][train_frozen_arm]["top_k_without_portfolio"]["5"]
            if train_frozen_arm else {}
        ) for lane in LANES
    }
    frozen_validation_audit = _frozen_validation_audit(lanes, train_frozen_arm)
    blockers = ["FINAL_TEST_LOCKED", "PROSPECTIVE_SHADOW_NOT_EVALUATED",
                "NOT_EXECUTABLE_RETURNS_OR_OPTION_QUOTES",
                "ROLLING_FIXED_LANES_MAY_OVERLAP"]
    for lane, data in lanes.items():
        valid = data["validation"]
        coverage = valid["population"]
        base = (((valid.get("arms") or {}).get("CONTEXT_FULL") or {})
                .get("top_k_without_portfolio") or {}).get("5") or {}
        if int(base.get("selected") or 0) < MIN_VALIDATION_TOP5 or int(base.get("dates") or 0) < MIN_VALIDATION_DATES:
            blockers.append(lane + "_SMALL_VALIDATION")
        if int(coverage.get("gex_timestamp_verified") or 0) == 0:
            blockers.append(lane + "_GEX_INTRADAY_ORDER_UNVERIFIED")
    if not train_frozen_arm:
        blockers.append("NO_SHARED_TRAIN_ELIGIBLE_ARM")
    if frozen_validation_audit["status"] == "REJECTED_VALIDATION":
        blockers.append("TRAIN_FROZEN_ARM_REJECTED_BY_VALIDATION")
    if dual_lane_validation_audit["overlap_label_disagreements"]:
        blockers.append("ROLLING_FIXED_SAME_KEY_LABEL_MISMATCH")
    return {
        "mode": MODE,
        "generated_at_madrid": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "scope": "All ticker/date directional hypotheses in frozen contextual Scanner/CORE/market cache",
        "not_full_reverse_market_universe": True,
        "live_mutation": False, "final_test": "LOCKED", "promotion_allowed": False,
        "order_of_work": ["ALL_UNIVERSE_DETECTION_DIRECTION", "TOP5_NO_PORTFOLIO",
                          "EXITS_ON_FROZEN_ENTRIES_LATER"],
        "modules_ablation": list(ARMS),
        "lanes": lanes,
        "dual_lane_validation_audit": dual_lane_validation_audit,
        "train_frozen_arm": train_frozen_arm,
        "train_selection_without_validation_lookahead": True,
        "frozen_arm_validation_top5": frozen_validation,
        "frozen_arm_validation_audit": frozen_validation_audit,
        "decision": {
            "status": "DIAGNOSTIC_ONLY",
            "train_frozen_challenger_result": frozen_validation_audit["status"],
            "blockers": blockers,
            "next_step": "Review loss-by-module and GEX timestamp joins; freeze a train-only challenger and evaluate prospectively",
        },
    }


def main() -> int:
    artifact = Path(config.OUTPUT_DIR) / "historical_validation" / "latest_full_universe_calibration.json"
    report = run()
    atomic_stream_json(artifact, report)
    print(json.dumps({
        "mode": MODE, "artifact": str(artifact),
        "rolling_validation_population": report["lanes"]["ROLLING_LATEST"]["validation"]["population"],
        "fixed_validation_population": report["lanes"]["FIXED_BENCHMARK"]["validation"]["population"],
        "live_unchanged": True, "final_test": "LOCKED",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
