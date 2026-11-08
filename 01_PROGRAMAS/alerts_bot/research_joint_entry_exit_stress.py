from __future__ import annotations

"""TMAD detached joint entry x direction x exit stress test (no FULL, no LIVE).

Train-only model fit and bounded rule search; rolling and fixed must share a
candidate BEFORE their validation outcomes are inspected. FINAL TEST is NEVER
read for scoring. Every exit is replayed on the same next-open OHLC path.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import gc
from itertools import product
import json
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import config
from autonomous_research_cache import cache_filename, load_fast_research_cache, split_cached_rows
from core_context_precision_optimizer import _family_models, _fast_cache_contract_hash, _fit_train_models, _score_row
from research_streaming_io import atomic_stream_json
from research_swing_exit_policy import _future_path, _load_db_price_paths, _profit_factor, simulate_exit

MODE = "JOINT_ENTRY_DIRECTION_EXIT_STRESS_V1"
LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")
SELECTIONS = ("ENTER_RECORDED", "CORE_RANK", "CORE_CONTEXT", "SCANNER_CONTEXT", "SCANNER_HIGH_CONF")
SIDES = ("CORE", "CONTEXT")
CAPACITIES = (5, 10, 20)
STOPS_PCT = (3.0, 6.0)
POLICIES = ("TIME_5", "TRAIL_CLOSE_1R", "TRAIL_CLOSE_2R", "TIME_10")
HORIZONS = (5, 10)
MIN_TRAIN_TRADES = 30
MIN_TRAIN_DATES = 12
MIN_VALIDATION_TRADES = 20
MIN_VALIDATION_DATES = 8


def _key(item: dict) -> tuple[str, str]:
    return (str(item.get("date") or ""), str(item.get("ticker") or "").upper())


def _entry_keys() -> dict[tuple[str, str], set[str]]:
    path = Path(config.OUTPUT_DIR) / "historical_validation" / "latest_all_actionable_enter_candidates.json"
    if not path.is_file():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {}
    found: dict[tuple[str, str], set[str]] = defaultdict(set)
    if not isinstance(entries, list):
        return {}
    for row in entries:
        if not isinstance(row, dict):
            continue
        date = str(row.get("signal_date") or "")[:10]
        ticker = str(row.get("ticker") or "").upper()
        side = str(row.get("direction") or "").upper()
        if date and ticker and side in {"LONG", "SHORT"}:
            found[(date, ticker)].add(side)
    return dict(found)


def _daily(rows: list[dict], models: dict) -> tuple[dict, dict]:
    pairs: dict[tuple[str, str], dict] = defaultdict(dict)
    for row in rows:
        side = str(row.get("context_direction") or "")
        if side in {"LONG", "SHORT"}:
            pairs[_key(row)][side] = row
    dates: dict[str, list[dict]] = defaultdict(list)
    missing = Counter()
    scores = []
    mover_population: set[tuple[str, str]] = set()
    core_ticker_dates = 0
    core_mover_dates = 0
    for (day, ticker), sides in sorted(pairs.items()):
        if len(sides) != 2:
            missing["MISSING_LONG_OR_SHORT"] += 1
            continue
        row = sides["LONG"]
        if bool(row.get("best_mover_5d")):
            mover_population.add((day, ticker))
        if bool((row.get("stage") or {}).get("core")):
            core_ticker_dates += 1
            core_mover_dates += int(bool(row.get("best_mover_5d")))
        ranked = []
        for side in ("LONG", "SHORT"):
            score, _ = _score_row(sides[side], models)
            if score is not None:
                ranked.append((float(score), side))
        best = max(ranked) if ranked else None
        current = str(row.get("current_direction") or "").upper()
        if current not in {"LONG", "SHORT"}:
            missing["CORE_DIRECTION_UNKNOWN"] += 1
        if best:
            scores.append(best[0])
        else:
            missing["CONTEXT_NOT_SCOREABLE"] += 1
        dates[day].append({
            "date": day, "ticker": ticker, "current": current,
            "context": best[1] if best else None,
            "context_score": best[0] if best else None,
            "core": bool((row.get("stage") or {}).get("core")),
            "research_rank": tuple(row.get("research_rank") or ()),
            "mover5d_best_direction_label": bool(row.get("best_mover_5d")),
            "mover_by_side": {side: bool(sides[side].get("directional_mover_5d")) for side in ("LONG", "SHORT")},
            "sector_explicit": bool((row.get("context_diagnostics") or {}).get("explicit_sector_etf")),
        })
    threshold = sorted(scores)[int((len(scores) - 1) * .75)] if scores else None
    return dates, {
        "population_ticker_dates": len(pairs), "mover5d_best_direction_label": len(mover_population),
        "core_pass_ticker_dates": core_ticker_dates,
        "core_pass_mover_labels_hindsight": core_mover_dates,
        "missing": dict(missing), "high_conf_threshold_from_this_split": threshold,
    }


def _select(
    by_date: dict, spec: dict, recorded: dict, *, high_conf_threshold: float | None
) -> list[dict]:
    out = []
    for day, candidates in sorted(by_date.items()):
        candidates_ranked = []
        for item in candidates:
            selection = spec["selection"]
            side = spec["side"]
            if selection == "ENTER_RECORDED":
                matched = recorded.get((day, item["ticker"])) or set()
                if not matched:
                    continue
                # STRICT means historical recorded side. Do not claim an
                # unissued opposite-side hypothesis was a FUSION ENTER.
                direction = "LONG" if "LONG" in matched else "SHORT"
            else:
                direction = item["current"] if side == "CORE" else item["context"]
            if direction not in {"LONG", "SHORT"}:
                continue
            if selection in {"CORE_RANK", "CORE_CONTEXT"} and not item["core"]:
                continue
            if selection in {"CORE_CONTEXT", "SCANNER_CONTEXT", "SCANNER_HIGH_CONF"} and item["context_score"] is None:
                continue
            if selection == "SCANNER_HIGH_CONF" and (
                high_conf_threshold is None or item["context_score"] < high_conf_threshold
            ):
                continue
            # Rank/score fixed from point-in-time; never by future mover label.
            rank = (
                item["research_rank"] if selection in {"CORE_RANK", "ENTER_RECORDED"}
                else (float(item["context_score"]),)
            )
            candidates_ranked.append((rank, item["ticker"], item, direction))
        candidates_ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for _, _, item, direction in candidates_ranked[:spec["capacity"]]:
            out.append({
                "ticker": item["ticker"], "signal_date": day, "direction": direction,
                "mover5d_best_direction_label": item["mover5d_best_direction_label"],
                "mover5d_selected_direction_label": item["mover_by_side"][direction],
                "sector_explicit": item["sector_explicit"],
            })
    return out


def _simulate(
    entries: list[dict], price_paths: dict, spec: dict, *, cost_bps: float
) -> dict:
    rejections = Counter()
    potential = []
    unique = {_key({"date": x["signal_date"], "ticker": x["ticker"]}): x for x in entries}
    for (day, ticker), entry in unique.items():
        path, reason = _future_path(entry, price_paths.get((ticker, day), []), sessions=spec["horizon"])
        if reason and reason != "INSUFFICIENT_FORWARD_SESSIONS":
            rejections[reason] += 1
            continue
        if len(path) < spec["horizon"]:
            rejections["NOT_MATURE_OR_NO_TRUSTED_OHLC"] += 1
            continue
        opening = path[0]["open"]
        risk = float(spec["stop_pct"]) / 100.0
        stop = opening * (1.0 - risk if entry["direction"] == "LONG" else 1.0 + risk)
        signal = {
            **entry, "invalidation_price": stop, "target_price": None
        }
        trade = simulate_exit(
            signal, path, spec["policy"], roundtrip_cost_bps=cost_bps,
            observation_cap=spec["horizon"]
        )
        if trade is None:
            rejections["INVALID_SCRATCH_STOP"] += 1
            continue
        potential.append({**trade, "mover5d_best_direction_label": entry["mover5d_best_direction_label"],
                       "mover5d_selected_direction_label": entry["mover5d_selected_direction_label"],
                       "sector_explicit": entry["sector_explicit"]})

    # Actual admission: cap concurrent swing slots, not merely signals per day.
    # Stops and exits are computed from each entry's future path, but future
    # outcomes are NEVER used to rank or reorder candidates entering at T0.
    trades = []
    occupied_until = []
    peak_open = 0
    for trade in sorted(potential, key=lambda x: (x["entry_date"], x["signal_date"])):
        occupied_until = [day for day in occupied_until if day >= trade["entry_date"]]
        if len(occupied_until) >= 5:
            rejections["PORTFOLIO_SLOTS_OCCUPIED"] += 1
            continue
        occupied_until.append(trade["exit_date"])
        peak_open = max(peak_open, len(occupied_until))
        trades.append(trade)

    values = [float(x["net_return_pct"]) for x in trades]
    sessions = [int(x["held_sessions"]) for x in trades]
    n = len(trades)
    date_count = len({x["signal_date"] for x in trades})
    # Slot accounting includes positions that close later in the same session.
    captured = sum(x["max_observed_favorable_excursion_pct"] >= 5 for x in trades)
    return {
        "selected": len(entries), "evaluable": n, "dates": date_count,
        "trusted_ohlc_candidates": len(potential),
        "mature_trusted_coverage_pct": round(100 * len(potential) / len(entries), 2) if entries else None,
        "portfolio_slot_admission_pct": round(100 * n / len(potential), 2) if potential else None,
        "labelled_best_direction_movers_hindsight": sum(x["mover5d_best_direction_label"] for x in trades),
        "labelled_movers_selected_side_signal_entry": sum(x["mover5d_selected_direction_label"] for x in trades),
        "reached_5pct_before_exit": captured,
        "captured_rate_pct": round(100 * captured / n, 3) if n else None,
        "net_win_pct": round(100 * sum(x > 0 for x in values) / n, 3) if n else None,
        "net_avg_pct": round(mean(values), 4) if n else None,
        "net_profit_factor": _profit_factor(values),
        "avg_sessions_held": round(mean(sessions), 3) if n else None,
        "max_concurrent_positions_enforced": peak_open,
        "max_portfolio_slots": 5,
        "portfolio_risk_notional_not_modelled": True,
        "net_per_capital_session_pct": round(mean(x["capital_session_pct"] for x in trades), 4) if n else None,
        "avg_mae_observed_pct": round(mean(x["max_observed_adverse_excursion_pct"] for x in trades), 4) if n else None,
        "worst_trade_net_pct": round(min(values), 4) if values else None,
        "explicit_sector_etf_trades": sum(x["sector_explicit"] for x in trades),
        "exit_reasons": dict(Counter(x["exit_reason"] for x in trades)),
        "rejected": dict(rejections),
        "assumptions": "Next-session open, fixed synthetic stop, roundtrip cost; no option quotes or intraday tick order",
    }


def _specs() -> list[dict]:
    result = []
    for selection, side, cap, stop, horizon, policy in product(
        SELECTIONS, SIDES, CAPACITIES, STOPS_PCT, HORIZONS, POLICIES
    ):
        if selection == "ENTER_RECORDED" and side != "CORE":
            continue  # Record the actual emitted direction, not a variant.
        if policy == "TIME_10" and horizon < 10:
            continue
        result.append({
            "selection": selection, "side": side, "capacity": cap, "stop_pct": stop,
            "horizon": horizon, "policy": policy,
        })
    return result


def _id(spec: dict) -> str:
    return "|".join(str(spec[k]) for k in
                    ("selection", "side", "capacity", "stop_pct", "horizon", "policy"))


def _data(lane: str) -> tuple[list[dict], list[dict], dict]:
    payload = load_fast_research_cache(
        config.RUNTIME_ROOT / "autonomous_research" / "cache" / cache_filename(lane),
        expected_contract_hash=_fast_cache_contract_hash(),
        verify_fingerprint=True,
    )
    train, validation, _holdout = split_cached_rows(payload)
    # A 5-session purge prevents TRAIN's future mover labels from reaching
    # VALIDATION, and VALIDATION's last labels from reaching the final lockbox.
    # The cached source stays unchanged: purge happens only in this reader.
    train_days = sorted({str(row.get("date") or "") for row in train})
    validation_days = sorted({str(row.get("date") or "") for row in validation})
    if len(train_days) <= 5 or len(validation_days) <= 5:
        raise ValueError("INSUFFICIENT_PURGED_CHRONOLOGICAL_DATES")
    keep_train = set(train_days[:-5])
    keep_validation = set(validation_days[:-5])
    train = [row for row in train if str(row.get("date") or "") in keep_train]
    validation = [row for row in validation if str(row.get("date") or "") in keep_validation]
    return train, validation, {
        "version": payload.get("producer_version"),
        "fingerprint": payload.get("dataset_fingerprint"),
        "raw_train_dates": len(train_days), "purged_train_dates": len(keep_train),
        "raw_validation_dates": len(validation_days),
        "purged_validation_dates": len(keep_validation),
        "embargo_sessions_between_lanes": 5,
        "final_test_locked": True,
    }


def _lane(lane: str, *, phase: str, specs: list[dict], cost_bps: float, recorded: dict) -> dict:
    print(f"[STRESS] lane={lane} phase={phase} loading frozen cache", flush=True)
    train, validation, cache = _data(lane)
    models = _family_models(_fit_train_models(train), "CONTEXT_LEAN")
    train_by_date, train_diag = _daily(train, models)
    high_conf = train_diag["high_conf_threshold_from_this_split"]
    active, diag = (train_by_date, train_diag) if phase == "TRAIN" else _daily(validation, models)
    del train, validation, train_by_date
    gc.collect()
    selections = {}
    entries_by_key = {}
    for spec in specs:
        selkey = (spec["selection"], spec["side"], spec["capacity"])
        if selkey not in selections:
            selections[selkey] = _select(active, spec, recorded, high_conf_threshold=high_conf)
        for item in selections[selkey]:
            entries_by_key[(item["ticker"], item["signal_date"])] = {
                "ticker": item["ticker"], "signal_date": item["signal_date"]
            }
    print(f"[STRESS] {lane}/{phase}: {len(entries_by_key)} distinct paths; querying OHLC read-only", flush=True)
    paths, coverage = _load_db_price_paths(
        list(entries_by_key.values()),
        progress=lambda message: print("[STRESS] " + message, flush=True)
    )
    values = {}
    for spec in specs:
        selkey = (spec["selection"], spec["side"], spec["capacity"])
        values[_id(spec)] = _simulate(selections[selkey], paths, spec, cost_bps=cost_bps)
    return {"status": "EVALUATED", "phase": phase, "lane": lane, "cache": cache,
            "population": diag, "price_coverage": coverage, "specs": values}


def _train_score(row: dict) -> tuple:
    if row.get("evaluable", 0) < MIN_TRAIN_TRADES or row.get("dates", 0) < MIN_TRAIN_DATES:
        return (-1, -1e6, -1e6)
    return (
        1 if (row.get("net_avg_pct") or -999) > 0 and (row.get("net_profit_factor") or 0) > 1 else 0,
        float(row.get("net_per_capital_session_pct") or -999),
        float(row.get("captured_rate_pct") or -999),
    )


def _train_consensus(rolling: dict, fixed: dict, specs: list[dict]) -> list[dict]:
    candidates = []
    for spec in specs:
        ident = _id(spec)
        r, f = rolling["specs"][ident], fixed["specs"][ident]
        if min(_train_score(r)[0], _train_score(f)[0]) < 1:
            continue
        # One Pareto representative per entry selection and side; avoids a
        # post-hoc search of hundreds of VALIDATION outcomes.
        candidates.append((
            min(_train_score(r)[1], _train_score(f)[1]),
            min(int(r["evaluable"]), int(f["evaluable"])),
            ident, spec
        ))
    out = []
    seen = set()
    for _, _, ident, spec in sorted(candidates, reverse=True):
        k = (spec["selection"], spec["side"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"id": ident, "spec": spec})
    return out


def run(*, cost_bps: float = 20.0) -> dict:
    if not 0 <= cost_bps <= 200:
        raise ValueError("cost bps must be between 0 and 200")
    specs = _specs()
    recorded = _entry_keys()
    train_results = {}
    for lane in LANES:
        train_results[lane] = _lane(lane, phase="TRAIN", specs=specs, cost_bps=cost_bps, recorded=recorded)
        gc.collect()
    shortlist = _train_consensus(*(train_results[x] for x in LANES), specs)
    selected_specs = [x["spec"] for x in shortlist]
    validation = {}
    if selected_specs:
        for lane in LANES:
            validation[lane] = _lane(
                lane, phase="VALIDATION", specs=selected_specs, cost_bps=cost_bps, recorded=recorded
            )
            gc.collect()
    # Bind results to the research OHLC revision without changing immutable
    # scanner/GEX historical snapshots or unlocking the final test.
    price_manifest = config.OUTPUT_DIR / "historical_validation" / "latest_research_ohlc_backfill.json"
    try:
        price_report = json.loads(price_manifest.read_text(encoding="utf-8-sig"))
        research_ohlc_revision = str((price_report.get("outcomes") or {}).get("revision") or "NONE")
    except (OSError, ValueError, TypeError):
        research_ohlc_revision = "NONE"
    # Neither the best validation score nor a repeated search on validation
    # may select a new rule. Prospective observations are required to proceed.
    return {
        "mode": MODE,
        "generated_at_madrid": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        "objective": "Joint entry quantity, direction, exit capture and capital time",
        "train_search": {
            "candidate_variants": len(specs),
            "rolling": train_results[LANES[0]],
            "fixed": train_results[LANES[1]],
        },
        "train_frozen_shortlist": shortlist,
        "validation": validation,
        "decision": {
            "status": "INSUFFICIENT_EVIDENCE" if not shortlist else "EXPLORATORY_JOINT_FRONTIER",
            "validating_shortlist_not_reoptimizing_validation": True,
            "promotion_allowed": False, "live_mutation": False,
            "final_test": "LOCKED",
            "cost_bps": cost_bps,
            "next_step": "Inspect frozen shortlist validation and blocked/missing OHLC; require prospective SHADOW before LIVE",
        },
        "methodology": {
            "train_only_fit_and_grid_search": True,
            "rolling_and_fixed_train_consensus_before_validation": True,
            "all_scanner_vs_core_vs_recorded_enter": True,
            "context_high_conf_threshold_fit_on_train_only": True,
            "same_point_in_time_selection_rules": True,
            "synthetic_fixed_percent_stops_not_current_live_stops": True,
            "requires_trusted_forward_OHLC": True,
            "research_ohlc_revision": research_ohlc_revision,
            "research_ohlc_segregated_from_live_cache": True,
            "all_rows_price_mature_5d_labels_from_frozen_cache": True,
            "counterfactual_exits_use_next_session_open": True,
            "no_lookahead_rule_execution": True,
            "no_final_test_evaluation": True,
            "single_snapshot_historical_data_not_independent_out_of_sample": True,
            "not_a_true_5d_universe_recall_rate": True,
            "overlapping_trades_not_real_portfolio_equity_or_executable_options": True,
            "max_open_positions_enforced": 5,
            "capital_risk_and_option_sizing_not_yet_modelled": True,
            "no_live_strategy_change": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cost-bps", type=float, default=20.0)
    args = parser.parse_args()
    result = run(cost_bps=args.cost_bps)
    output = Path(config.OUTPUT_DIR) / "historical_validation" / "latest_joint_entry_exit_stress.json"
    atomic_stream_json(output, result)
    print(json.dumps({
        "artifact": str(output),
        "status": result["decision"]["status"],
        "train_candidates": result["train_search"]["candidate_variants"],
        "shortlisted": len(result["train_frozen_shortlist"]),
        "validation_completed": len(result["validation"]),
        "no_full_required": True, "live_unchanged": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
