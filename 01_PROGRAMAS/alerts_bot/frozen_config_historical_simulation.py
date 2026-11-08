from __future__ import annotations

"""Historical simulation of the exact 6.2.22 prospective architecture config.

Research-only. This module replays the frozen CORE Direction V2 and Research
Override Top-1 ideas over trusted historical rows so implementation mistakes
can be detected before prospective evidence matures.

Important: this is a diagnostic backfill over the same historical universe from
which the hypotheses were discovered. It is NOT independent OOS evidence and
never authorizes a LIVE change.
"""

from collections import defaultdict
from statistics import mean
from typing import Any

from daily_reverse_alpha import _stage_path
from economic_architecture_challengers import _build_rows
from economic_waterfall_calibration import _population_metrics
from prospective_architecture_journal import (
    CORE_DIRECTION_RULES,
    RESEARCH_PASS_STATES,
    STATE_PRIORITY,
    core_direction_v2,
)


MODE = "FROZEN_CONFIG_HISTORICAL_SIMULATION_V1"
TARGET_PCT = 5.0
HORIZON_SESSIONS = 5


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 0:
        return None if gains <= 0 else float("inf")
    return round(gains / losses, 4)


def _path_row(source: dict, direction: str) -> dict:
    path = source["long_path"] if direction == "LONG" else source["short_path"]
    return {
        "date": source["date"],
        "ticker": source["ticker"],
        "direction": direction,
        "mfe_pct": float(path.get("mfe_pct") or 0.0),
        "mae_pct": float(path.get("mae_pct") or 0.0),
        "first_hit_session": path.get("first_hit_session"),
        "horizon_close_return_pct": path.get("horizon_close_return_pct"),
        "mover_5d": bool(float(path.get("mfe_pct") or 0.0) >= TARGET_PCT),
        "best_direction": source.get("best_direction"),
        "best_mover_5d": bool(source.get("best_mover_5d")),
    }


def _metrics_for_variant(source_rows: list[dict], direction_key: str) -> dict:
    rows = _variant_rows(source_rows, direction_key)
    total_best_movers = sum(bool(row.get("best_mover_5d")) for row in source_rows)
    return _population_metrics(rows, total_movers=total_best_movers)


def _direction_accuracy(source_rows: list[dict], direction_key: str) -> dict:
    rows = [
        row for row in source_rows
        if row.get("best_mover_5d")
        and row.get(direction_key) in {"LONG", "SHORT"}
    ]
    if not rows:
        return {"sample": 0, "accuracy_pct": None}
    correct = sum(row.get(direction_key) == row.get("best_direction") for row in rows)
    return {
        "sample": len(rows),
        "accuracy_pct": round(100.0 * correct / len(rows), 3),
    }


def _vote(feature: str, value: float) -> str:
    rule = next(row for row in CORE_DIRECTION_RULES if row["feature"] == feature)
    high = str(rule["high_direction"])
    if float(value) >= float(rule["threshold"]):
        return high
    return "LONG" if high == "SHORT" else "SHORT"


def _consensus_direction(features: dict, current_direction: str | None) -> dict:
    current = str(current_direction or "").upper()
    if current not in {"LONG", "SHORT"}:
        return {
            "direction": None,
            "changed": False,
            "both_features_available": False,
            "votes_agree": False,
        }
    votes: list[str] = []
    values: dict[str, float] = {}
    for rule in CORE_DIRECTION_RULES:
        value = _f(features.get(rule["feature"]))
        if value is None:
            continue
        values[rule["feature"]] = float(value)
        votes.append(_vote(rule["feature"], float(value)))
    both = len(votes) == len(CORE_DIRECTION_RULES)
    agree = both and len(set(votes)) == 1
    direction = votes[0] if agree else current
    return {
        "direction": direction,
        "changed": direction != current,
        "both_features_available": both,
        "votes_agree": agree,
        "votes": votes,
        "values": values,
    }


def _stage_for(
    signal_index: dict[tuple[str, str, str], list[dict]],
    date_value: str,
    ticker: str,
    direction: str,
) -> tuple[dict, int]:
    rows = list(signal_index.get((date_value, ticker, direction.lower()), []) or [])
    return _stage_path(rows, direction.lower()), len(rows)


def _downstream_profile(
    source_rows: list[dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    direction_key: str,
) -> dict:
    changed = [
        row for row in source_rows
        if row.get("current_direction") in {"LONG", "SHORT"}
        and row.get(direction_key) in {"LONG", "SHORT"}
        and row.get(direction_key) != row.get("current_direction")
    ]
    counts = defaultdict(int)
    current_fusion_to_missing_alt = 0
    for row in changed:
        stage, signal_rows = _stage_for(
            signal_index,
            row["date"],
            row["ticker"],
            row[direction_key],
        )
        if signal_rows:
            counts["any_signal_rows"] += 1
        for name in ("core", "research", "gex", "timing", "fusion"):
            if stage.get(name):
                counts[name] += 1
        if stage.get("gex") and stage.get("timing") and stage.get("fusion"):
            counts["gex_timing_fusion"] += 1
        if bool((row.get("stage") or {}).get("fusion")) and not stage.get("fusion"):
            current_fusion_to_missing_alt += 1
    sample = len(changed)
    return {
        "changed_direction_sample": sample,
        "any_alt_signal_rows": int(counts["any_signal_rows"]),
        "alt_signal_coverage_pct": (
            round(100.0 * counts["any_signal_rows"] / sample, 3) if sample else None
        ),
        "alt_research_pass": int(counts["research"]),
        "alt_gex_pass": int(counts["gex"]),
        "alt_timing_pass": int(counts["timing"]),
        "alt_fusion_pass": int(counts["fusion"]),
        "alt_gex_timing_fusion_pass": int(counts["gex_timing_fusion"]),
        "current_fusion_to_missing_alt_fusion": current_fusion_to_missing_alt,
        "interpretation": (
            "Opposite-direction downstream rows are observational coverage only. "
            "Missing rows make the fully combined historical replay non-identifiable; "
            "they must not be imputed from future outcomes."
        ),
    }


def _core_simulation(
    rows: list[dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
) -> dict:
    usable = [row for row in rows if row.get("current_direction_known")]
    enriched: list[dict] = []
    loose_single_feature_flips = 0
    for row in usable:
        features = row.get("direction_features") or {}
        loose = core_direction_v2(features, row.get("current_direction"))
        consensus = _consensus_direction(features, row.get("current_direction"))
        item = dict(row)
        item["loose_direction"] = loose.get("challenger_direction")
        item["consensus_direction"] = consensus.get("direction")
        item["loose_vote_count"] = len(loose.get("votes") or [])
        item["consensus_both_available"] = consensus.get("both_features_available")
        item["consensus_votes_agree"] = consensus.get("votes_agree")
        if loose.get("changed") and len(loose.get("votes") or []) < len(CORE_DIRECTION_RULES):
            loose_single_feature_flips += 1
        enriched.append(item)

    current = [_path_row(row, row["current_direction"]) for row in enriched]
    loose = [_path_row(row, row["loose_direction"]) for row in enriched]
    consensus = [_path_row(row, row["consensus_direction"]) for row in enriched]

    for row in enriched:
        row["current_direction_eval"] = row.get("current_direction")
        row["loose_direction_eval"] = row.get("loose_direction")
        row["consensus_direction_eval"] = row.get("consensus_direction")

    current_acc = _direction_accuracy(enriched, "current_direction_eval")
    loose_acc = _direction_accuracy(enriched, "loose_direction_eval")
    consensus_acc = _direction_accuracy(enriched, "consensus_direction_eval")

    loose_changed = [
        row for row in enriched if row.get("loose_direction") != row.get("current_direction")
    ]
    consensus_changed = [
        row for row in enriched if row.get("consensus_direction") != row.get("current_direction")
    ]
    both_available = sum(bool(row.get("consensus_both_available")) for row in enriched)
    agreement = sum(bool(row.get("consensus_votes_agree")) for row in enriched)

    return {
        "sample": len(enriched),
        "frozen_rules": list(CORE_DIRECTION_RULES),
        "feature_coverage": {
            "both_features_available": both_available,
            "both_features_available_pct": (
                round(100.0 * both_available / len(enriched), 3) if enriched else None
            ),
            "both_votes_agree": agreement,
            "both_votes_agree_pct": (
                round(100.0 * agreement / len(enriched), 3) if enriched else None
            ),
        },
        "current": {
            "economics": _population_metrics(
                current,
                total_movers=sum(bool(row.get("best_mover_5d")) for row in enriched),
            ),
            "direction_accuracy_on_5d_movers": current_acc,
        },
        "v2_loose_current_622": {
            "economics": _population_metrics(
                loose,
                total_movers=sum(bool(row.get("best_mover_5d")) for row in enriched),
            ),
            "direction_accuracy_on_5d_movers": loose_acc,
            "changed_direction": len(loose_changed),
            "single_feature_flips": loose_single_feature_flips,
            "downstream_reproducibility": _downstream_profile(
                enriched, signal_index, "loose_direction"
            ),
        },
        "v2_strict_consensus": {
            "economics": _population_metrics(
                consensus,
                total_movers=sum(bool(row.get("best_mover_5d")) for row in enriched),
            ),
            "direction_accuracy_on_5d_movers": consensus_acc,
            "changed_direction": len(consensus_changed),
            "single_feature_flips": 0,
            "downstream_reproducibility": _downstream_profile(
                enriched, signal_index, "consensus_direction"
            ),
        },
    }


def _rank_key(row: dict) -> tuple:
    rank = tuple(row.get("research_rank") or ())
    return (*rank, str(row.get("ticker") or ""))


def _per_date_top1(rows: list[dict]) -> list[dict]:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_date[str(row.get("date") or "")].append(row)
    return [
        max(day_rows, key=_rank_key)
        for _day, day_rows in sorted(by_date.items())
        if day_rows
    ]


def _variant_rows(source_rows: list[dict], direction_key: str) -> list[dict]:
    out = []
    for row in source_rows:
        direction = row.get(direction_key)
        if direction not in {"LONG", "SHORT"}:
            continue
        out.append(_path_row(row, direction))
    return out


def _same_direction_downstream_safe(row: dict) -> bool:
    stage = row.get("stage") or {}
    return bool(stage.get("gex") and stage.get("timing") and stage.get("fusion"))


def _variant_downstream_safe(
    row: dict,
    signal_index: dict[tuple[str, str, str], list[dict]],
    direction_key: str,
) -> bool:
    direction = row.get(direction_key)
    if direction not in {"LONG", "SHORT"}:
        return False
    if direction == row.get("current_direction"):
        return _same_direction_downstream_safe(row)
    stage, _n = _stage_for(signal_index, row["date"], row["ticker"], direction)
    return bool(stage.get("gex") and stage.get("timing") and stage.get("fusion"))


def _alert_conflicts(
    rows: list[dict],
    alerts: set[tuple[str, str, str]] | None,
    direction_key: str,
) -> dict:
    alert_set = alerts or set()
    exact = 0
    ticker_any = 0
    opposite = 0
    for row in rows:
        direction = str(row.get(direction_key) or "").lower()
        key = (str(row.get("date") or ""), str(row.get("ticker") or "").upper(), direction)
        same_ticker = [
            item for item in alert_set
            if item[0] == key[0] and item[1] == key[1]
        ]
        if key in alert_set:
            exact += 1
        if same_ticker:
            ticker_any += 1
        if any(item[2] != direction for item in same_ticker):
            opposite += 1
    return {
        "exact_direction_overlap": exact,
        "same_ticker_any_direction_overlap": ticker_any,
        "opposite_direction_overlap": opposite,
    }


def _research_override_simulation(
    rows: list[dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    alerts: set[tuple[str, str, str]] | None,
) -> dict:
    usable = [row for row in rows if row.get("current_direction_known")]
    rejects = [
        row for row in usable
        if bool((row.get("stage") or {}).get("core"))
        and not bool((row.get("stage") or {}).get("research"))
    ]
    hard_pass = [
        row for row in usable
        if bool((row.get("stage") or {}).get("core"))
        and bool((row.get("stage") or {}).get("research"))
    ]

    override = _per_date_top1(rejects)
    control = _per_date_top1(hard_pass)

    enriched_override: list[dict] = []
    for row in override:
        item = dict(row)
        features = item.get("direction_features") or {}
        loose = core_direction_v2(features, item.get("current_direction"))
        consensus = _consensus_direction(features, item.get("current_direction"))
        item["loose_direction"] = loose.get("challenger_direction")
        item["consensus_direction"] = consensus.get("direction")
        enriched_override.append(item)

    current_rows = _variant_rows(enriched_override, "current_direction")
    loose_rows = _variant_rows(enriched_override, "loose_direction")
    consensus_rows = _variant_rows(enriched_override, "consensus_direction")
    control_rows = _variant_rows(control, "current_direction")

    current_safe_src = [row for row in enriched_override if _same_direction_downstream_safe(row)]
    loose_safe_src = [
        row for row in enriched_override
        if _variant_downstream_safe(row, signal_index, "loose_direction")
    ]
    consensus_safe_src = [
        row for row in enriched_override
        if _variant_downstream_safe(row, signal_index, "consensus_direction")
    ]

    return {
        "reject_pool": {
            "sample": len(rejects),
            "dates": len({row["date"] for row in rejects}),
        },
        "top1_override": {
            "identities": len(enriched_override),
            "dates": len({row["date"] for row in enriched_override}),
            "current_direction": _metrics_for_variant(enriched_override, "current_direction"),
            "core_v2_loose_direction": _metrics_for_variant(enriched_override, "loose_direction"),
            "core_v2_consensus_direction": _metrics_for_variant(enriched_override, "consensus_direction"),
            "current_hard_pass_top1_control": _metrics_for_variant(control, "current_direction"),
            "current_alert_conflicts": _alert_conflicts(
                enriched_override, alerts, "current_direction"
            ),
        },
        "downstream_safe_only": {
            "definition": "Research is bypassed only; GEX + Timing + FUSION must still pass.",
            "current_direction": {
                "eligible_identities": len(current_safe_src),
                "economics": _metrics_for_variant(
                    current_safe_src, "current_direction"
                ),
            },
            "core_v2_loose_direction": {
                "eligible_identities": len(loose_safe_src),
                "economics": _metrics_for_variant(
                    loose_safe_src, "loose_direction"
                ),
            },
            "core_v2_consensus_direction": {
                "eligible_identities": len(consensus_safe_src),
                "economics": _metrics_for_variant(
                    consensus_safe_src, "consensus_direction"
                ),
            },
        },
    }


def _error_flags(core: dict, research: dict) -> list[dict]:
    flags: list[dict] = []
    loose = core.get("v2_loose_current_622") or {}
    strict = core.get("v2_strict_consensus") or {}
    current = core.get("current") or {}

    if int(loose.get("single_feature_flips") or 0) > 0:
        flags.append({
            "code": "CORE_SINGLE_FEATURE_FLIP",
            "severity": "WARN",
            "count": int(loose.get("single_feature_flips") or 0),
            "detail": (
                "6.2.22 can flip direction with only one frozen feature available. "
                "Compare strict-consensus economics before any promotion."
            ),
        })

    cur_e = current.get("economics") or {}
    loose_e = loose.get("economics") or {}
    cur_ret = _f(cur_e.get("avg_horizon_close_return_pct"))
    loose_ret = _f(loose_e.get("avg_horizon_close_return_pct"))
    cur_pf = _f(cur_e.get("horizon_close_profit_factor"))
    loose_pf = _f(loose_e.get("horizon_close_profit_factor"))
    if (
        cur_ret is not None and loose_ret is not None and loose_ret < cur_ret
        and cur_pf is not None and loose_pf is not None and loose_pf < cur_pf
    ):
        flags.append({
            "code": "CORE_V2_ECONOMIC_REGRESSION",
            "severity": "FAIL",
            "detail": "Loose CORE V2 worsens both 5D close return and PF versus current direction.",
        })

    loose_down = loose.get("downstream_reproducibility") or {}
    changed = int(loose_down.get("changed_direction_sample") or 0)
    alt = int(loose_down.get("any_alt_signal_rows") or 0)
    if changed > 0 and alt < changed:
        flags.append({
            "code": "CORE_FLIP_DOWNSTREAM_PARTIAL_COVERAGE",
            "severity": "INFO",
            "count": changed - alt,
            "detail": (
                "Some direction flips have no opposite-direction historical signal rows. "
                "Their full downstream counterfactual is not identifiable and is not imputed."
            ),
        })

    override = ((research.get("top1_override") or {}).get("current_direction") or {})
    avg_ret = _f(override.get("avg_horizon_close_return_pct"))
    pf = _f(override.get("horizon_close_profit_factor"))
    if int(override.get("sample") or 0) > 0 and (
        avg_ret is None or avg_ret <= 0 or pf is None or pf <= 1.0
    ):
        flags.append({
            "code": "RESEARCH_OVERRIDE_RAW_ECONOMIC_FAIL",
            "severity": "FAIL",
            "detail": "Raw Top-1 Research rejects do not clear positive return + PF>1 historically.",
        })

    override_block = research.get("top1_override") or {}
    research_current = override_block.get("current_direction") or {}
    research_core_v2 = override_block.get("core_v2_consensus_direction") or {}
    current_ret = _f(research_current.get("avg_horizon_close_return_pct"))
    v2_ret = _f(research_core_v2.get("avg_horizon_close_return_pct"))
    current_pf = _f(research_current.get("horizon_close_profit_factor"))
    v2_pf = _f(research_core_v2.get("horizon_close_profit_factor"))
    if (
        current_ret is not None and v2_ret is not None and v2_ret < current_ret
        and current_pf is not None and v2_pf is not None and v2_pf < current_pf
    ):
        flags.append({
            "code": "CORE_V2_RESEARCH_INTERACTION_REGRESSION",
            "severity": "WARN",
            "detail": (
                "Do not stack CORE V2 onto Research Override Top-1: the historical "
                "interaction worsens both close return and profit factor."
            ),
        })

    downstream = research.get("downstream_safe_only") or {}
    current_downstream = downstream.get("current_direction") or {}
    if int((research.get("top1_override") or {}).get("identities") or 0) > 0 and int(
        current_downstream.get("eligible_identities") or 0
    ) == 0:
        flags.append({
            "code": "RESEARCH_OVERRIDE_DOWNSTREAM_NOT_IDENTIFIABLE",
            "severity": "WARN",
            "detail": (
                "Historical Research rejects have no fully reproducible downstream "
                "GEX+Timing+FUSION pass. A prospective shadow continuation is required "
                "before any deployable override can be assessed."
            ),
        })

    conflicts = ((research.get("top1_override") or {}).get("current_alert_conflicts") or {})
    if int(conflicts.get("same_ticker_any_direction_overlap") or 0) > 0:
        flags.append({
            "code": "RESEARCH_OVERRIDE_EXISTING_ALERT_CONFLICT",
            "severity": "WARN",
            "count": int(conflicts.get("same_ticker_any_direction_overlap") or 0),
            "detail": "Some override identities already collide with a current-policy alert on the same ticker/date.",
        })

    return flags


def build_frozen_config_historical_simulation(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    alerts: set[tuple[str, str, str]] | None = None,
    lane_label: str = "ROLLING_LATEST",
) -> dict:
    rows = _build_rows(
        canonical,
        signal_index,
        trusted_price_series,
        threshold_pct=TARGET_PCT,
        horizon_sessions=HORIZON_SESSIONS,
    )
    core = _core_simulation(rows, signal_index)
    research = _research_override_simulation(rows, signal_index, alerts)
    flags = _error_flags(core, research)
    return {
        "mode": MODE,
        "lane": str(lane_label or "ROLLING_LATEST").upper(),
        "status": "EVALUABLE" if rows else "NO_EVALUABLE_CANDIDATES",
        "live_mutation": False,
        "promotion_allowed": False,
        "historical_independence": "DIAGNOSTIC_BACKFILL_NOT_INDEPENDENT_OOS",
        "economic_truth": {
            "threshold_pct": TARGET_PCT,
            "horizon_sessions": HORIZON_SESSIONS,
        },
        "candidate_population": {
            "evaluable": len(rows),
            "dates": len({row["date"] for row in rows}),
        },
        "core_direction": core,
        "research_override_top1": research,
        "error_flags": flags,
        "policy": {
            "prospective_journal_unchanged": True,
            "gex_unchanged": True,
            "timing_unchanged": True,
            "fusion_unchanged": True,
            "lifecycle_unchanged": True,
            "future_prices_labels_only": True,
            "no_missing_downstream_imputation": True,
        },
    }


def _metric(payload: dict, *path: str) -> float | None:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _f(value)


def combine_frozen_config_historical_simulations(
    rolling: dict,
    fixed: dict,
) -> dict:
    lanes = [rolling, fixed]
    critical = [
        flag
        for lane in lanes
        for flag in (lane.get("error_flags") or [])
        if str(flag.get("severity") or "").upper() == "FAIL"
    ]

    def direction_acc(lane: dict, variant: str) -> float | None:
        return _metric(
            lane,
            "core_direction",
            variant,
            "direction_accuracy_on_5d_movers",
            "accuracy_pct",
        )

    rolling_loose = direction_acc(rolling, "v2_loose_current_622")
    rolling_strict = direction_acc(rolling, "v2_strict_consensus")
    fixed_loose = direction_acc(fixed, "v2_loose_current_622")
    fixed_strict = direction_acc(fixed, "v2_strict_consensus")

    strict_not_worse = bool(
        rolling_loose is not None and rolling_strict is not None
        and fixed_loose is not None and fixed_strict is not None
        and rolling_strict >= rolling_loose
        and fixed_strict >= fixed_loose
    )

    return {
        "mode": "FROZEN_CONFIG_HISTORICAL_DUAL_LANE_DECISION_V1",
        "status": (
            "EVALUABLE"
            if str(rolling.get("status")) == "EVALUABLE"
            and str(fixed.get("status")) == "EVALUABLE"
            else "PARTIAL"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "critical_failures": critical,
        "critical_failure_count": len(critical),
        "strict_consensus_not_worse_than_loose_on_direction_accuracy": strict_not_worse,
        "recommended_core_variant_for_continued_shadow": (
            "V2_STRICT_CONSENSUS"
            if strict_not_worse else "KEEP_6_2_22_LOOSE_PENDING_PROSPECTIVE"
        ),
        "research_override_direction_policy": "CURRENT_CORE_ONLY",
        "research_override_do_not_combine_with_core_v2": True,
        "research_override_requires_prospective_downstream_continuation": True,
        "historical_simulation_clean_for_prospective_continuation": len(critical) == 0,
        "interpretation": (
            "This backfill is for implementation-error detection and variant calibration. "
            "It cannot replace the frozen prospective journal as promotion evidence."
        ),
    }
