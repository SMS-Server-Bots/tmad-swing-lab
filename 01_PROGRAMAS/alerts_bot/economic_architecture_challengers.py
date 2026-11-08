from __future__ import annotations

"""TMAD 6.2.19 economic architecture challengers.

Research-only / SHADOW-only.

The module tests three narrowly-defined hypotheses on the same <=5-session
economic truth used by 6.2.18:

1) CORE direction: can point-in-time features improve LONG/SHORT selection?
2) Research: does using the existing Research evidence as a ranker outperform
   the same-capacity hard Research gate?
3) FUSION: does using existing FUSION/tradeability evidence as a ranker on
   timing-ready candidates outperform the same-capacity hard FUSION gate?

No threshold is changed, no LIVE rule is mutated, and no candidate is promoted
from hindsight. Future prices are labels only.
"""

from collections import defaultdict
import math
from statistics import mean, median
from typing import Any

from daily_reverse_alpha import _stage_path
from economic_waterfall_calibration import (
    DEFAULT_HORIZON_SESSIONS,
    DEFAULT_THRESHOLD_PCT,
    _best_direction,
    _direction_path,
    _entry_price,
    _f,
    _feature_payload,
    _future_same_segment,
    _population_metrics,
)


MODE = "ECONOMIC_ARCHITECTURE_CHALLENGERS_V1"
STATE_PRIORITY = {
    "BLOCKED": 0,
    "WATCH": 1,
    "DISCOVERED": 2,
    "EARLY": 3,
    "CAPPED": 4,
    "READY": 5,
    "RETEST": 6,
    "ENTER": 7,
}
DIRECTION_FEATURE_TOKENS = (
    "momentum", "trend", "change", "breadth", "relative_strength",
    "flow", "rvol", "score", "strength", "lead_lag",
)
EXCLUDED_FEATURE_TOKENS = (
    "price", "strike", "expiry", "target", "stop", "distance",
    "_id", "timestamp", "observed", "created", "updated",
)


def _numeric(value: Any) -> float | None:
    return _f(value)


def _direction_features(row: dict) -> dict[str, float]:
    payload = _feature_payload(row)
    out: dict[str, float] = {}
    for key, value in payload.items():
        name = str(key)
        low = name.lower()
        if any(token in low for token in EXCLUDED_FEATURE_TOKENS):
            continue
        if not any(token in low for token in DIRECTION_FEATURE_TOKENS):
            continue
        number = _numeric(value)
        if number is not None:
            out[name] = float(number)
    for key in ("core_score", "core_probability_pct"):
        number = _numeric(row.get(key))
        if number is not None:
            out[key] = float(number)
    return out


def _layer_rows(signal_rows: list[dict], layer: str) -> list[dict]:
    target = str(layer).upper()
    return [
        row for row in signal_rows
        if str(row.get("layer") or "").upper() == target
    ]


def _state(row: dict) -> str:
    return str(row.get("state") or row.get("opportunity_state") or "WATCH").upper()


def _max_field(rows: list[dict], keys: tuple[str, ...]) -> float | None:
    values: list[float] = []
    for row in rows:
        for key in keys:
            value = _numeric(row.get(key))
            if value is not None:
                values.append(float(value))
    return max(values) if values else None


def _research_rank_tuple(signal_rows: list[dict], canonical_row: dict) -> tuple:
    core = _layer_rows(signal_rows, "CORE")
    priority = max((STATE_PRIORITY.get(_state(row), 1) for row in core), default=0)
    score = _max_field(
        core,
        ("research_score", "score", "probability_pct", "core_score"),
    )
    if score is None:
        score = _numeric(canonical_row.get("core_score")) or 0.0
    probability = _max_field(core, ("probability_pct", "core_probability_pct"))
    if probability is None:
        probability = _numeric(canonical_row.get("core_probability_pct")) or 0.0
    return (float(score), float(probability), int(priority))


def _fusion_rank_tuple(signal_rows: list[dict]) -> tuple:
    fusion = _layer_rows(signal_rows, "FUSION")
    priority = max((STATE_PRIORITY.get(_state(row), 1) for row in fusion), default=0)
    fusion_score = _max_field(fusion, ("score", "fusion_score")) or 0.0
    tradeability = _max_field(fusion, ("tradeability_score",)) or 0.0
    alpha = _max_field(fusion, ("underlying_alpha_score", "alpha_score")) or 0.0
    return (float(fusion_score), float(tradeability), float(alpha), int(priority))


def _build_rows(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    threshold_pct: float,
    horizon_sessions: int,
) -> list[dict]:
    rows: list[dict] = []
    for (date_value, ticker_value), source in sorted(canonical.items()):
        date_key = str(date_value)[:10]
        ticker = str(ticker_value).upper()
        entry = _entry_price(source)
        if entry is None or entry <= 0:
            continue
        future = _future_same_segment(
            trusted_price_series.get(ticker, []),
            date_key,
            horizon_sessions,
        )
        if not future:
            continue

        best_direction, best_path = _best_direction(
            float(entry), future, float(threshold_pct)
        )
        long_path = _direction_path(float(entry), "LONG", future, threshold_pct)
        short_path = _direction_path(float(entry), "SHORT", future, threshold_pct)

        current_direction = str(source.get("core_direction") or "").upper()
        current_known = current_direction in {"LONG", "SHORT"}
        if current_known:
            current_path = long_path if current_direction == "LONG" else short_path
            signals = list(
                signal_index.get(
                    (date_key, ticker, current_direction.lower()), []
                ) or []
            )
            stage = _stage_path(signals, current_direction.lower())
            research_rank = _research_rank_tuple(signals, source)
            fusion_rank = _fusion_rank_tuple(signals)
        else:
            current_path = {
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "first_hit_session": None,
                "horizon_close_return_pct": None,
            }
            signals = []
            stage = {
                "core": False,
                "research": False,
                "gex": False,
                "timing": False,
                "fusion": False,
            }
            research_rank = (0.0, 0.0, 0)
            fusion_rank = (0.0, 0.0, 0.0, 0)

        rows.append({
            "date": date_key,
            "ticker": ticker,
            "entry_price": float(entry),
            "best_direction": best_direction,
            "best_mfe_pct": float(best_path.get("mfe_pct") or 0.0),
            "best_mover_5d": bool(
                float(best_path.get("mfe_pct") or 0.0) >= float(threshold_pct)
            ),
            "long_path": long_path,
            "short_path": short_path,
            "current_direction": current_direction if current_known else None,
            "current_direction_known": current_known,
            "current_direction_correct": bool(
                current_known and current_direction == best_direction
            ),
            "mfe_pct": float(current_path.get("mfe_pct") or 0.0),
            "mae_pct": float(current_path.get("mae_pct") or 0.0),
            "first_hit_session": current_path.get("first_hit_session"),
            "horizon_close_return_pct": current_path.get(
                "horizon_close_return_pct"
            ),
            "mover_5d": bool(
                current_known
                and float(current_path.get("mfe_pct") or 0.0)
                >= float(threshold_pct)
            ),
            "stage": stage,
            "research_rank": research_rank,
            "fusion_rank": fusion_rank,
            "direction_features": _direction_features(source),
        })
    return rows


def _direction_split_dates(rows: list[dict]) -> tuple[set[str], set[str], set[str]]:
    dates = sorted({
        str(row.get("date") or "")
        for row in rows
        if row.get("date") and row.get("best_mover_5d")
    })
    if len(dates) < 8:
        return set(dates), set(), set()
    n = len(dates)
    train_end = max(1, int(math.floor(n * 0.50)))
    valid_end = max(train_end + 1, int(math.floor(n * 0.75)))
    valid_end = min(valid_end, n - 1)
    return (
        set(dates[:train_end]),
        set(dates[train_end:valid_end]),
        set(dates[valid_end:]),
    )


def _direction_rule(
    rows: list[dict],
    feature: str,
) -> dict | None:
    values = [
        (float((row.get("direction_features") or {})[feature]), row["best_direction"])
        for row in rows
        if row.get("best_mover_5d")
        and _numeric((row.get("direction_features") or {}).get(feature)) is not None
        and row.get("best_direction") in {"LONG", "SHORT"}
    ]
    longs = [value for value, direction in values if direction == "LONG"]
    shorts = [value for value, direction in values if direction == "SHORT"]
    if len(values) < 20 or len(longs) < 5 or len(shorts) < 5:
        return None
    long_med = median(longs)
    short_med = median(shorts)
    if abs(long_med - short_med) < 1e-12:
        return None
    threshold = (float(long_med) + float(short_med)) / 2.0
    high_direction = "LONG" if long_med > short_med else "SHORT"

    def predict(value: float) -> str:
        if value >= threshold:
            return high_direction
        return "SHORT" if high_direction == "LONG" else "LONG"

    correct = sum(predict(value) == direction for value, direction in values)
    return {
        "feature": feature,
        "threshold": round(threshold, 8),
        "high_direction": high_direction,
        "sample": len(values),
        "train_accuracy_pct": round(100.0 * correct / len(values), 3),
    }


def _rule_accuracy(rows: list[dict], rule: dict) -> dict:
    feature = str(rule["feature"])
    threshold = float(rule["threshold"])
    high_direction = str(rule["high_direction"])
    evaluated = []
    for row in rows:
        if not row.get("best_mover_5d"):
            continue
        value = _numeric((row.get("direction_features") or {}).get(feature))
        if value is None:
            continue
        predicted = (
            high_direction
            if float(value) >= threshold
            else ("SHORT" if high_direction == "LONG" else "LONG")
        )
        evaluated.append(predicted == row.get("best_direction"))
    return {
        "sample": len(evaluated),
        "accuracy_pct": (
            round(100.0 * sum(evaluated) / len(evaluated), 3)
            if evaluated else None
        ),
    }


def _direction_metrics(rows: list[dict], predicted_key: str) -> dict:
    evaluated = []
    for row in rows:
        if not row.get("best_mover_5d"):
            continue
        direction = row.get(predicted_key)
        if direction not in {"LONG", "SHORT"}:
            continue
        path = row["long_path"] if direction == "LONG" else row["short_path"]
        evaluated.append({
            "date": row["date"],
            "correct": direction == row["best_direction"],
            "mover": float(path.get("mfe_pct") or 0.0) >= DEFAULT_THRESHOLD_PCT,
            "mfe_pct": float(path.get("mfe_pct") or 0.0),
            "mae_pct": float(path.get("mae_pct") or 0.0),
        })
    return {
        "sample": len(evaluated),
        "dates": len({row["date"] for row in evaluated}),
        "direction_accuracy_pct": (
            round(
                100.0 * sum(bool(row["correct"]) for row in evaluated)
                / len(evaluated),
                3,
            )
            if evaluated else None
        ),
        "mover_capture_pct": (
            round(
                100.0 * sum(bool(row["mover"]) for row in evaluated)
                / len(evaluated),
                3,
            )
            if evaluated else None
        ),
        "avg_mfe_pct": (
            round(mean(row["mfe_pct"] for row in evaluated), 4)
            if evaluated else None
        ),
        "avg_mae_pct": (
            round(mean(row["mae_pct"] for row in evaluated), 4)
            if evaluated else None
        ),
    }


def _direction_challenger(rows: list[dict]) -> dict:
    train_dates, valid_dates, test_dates = _direction_split_dates(rows)
    if not valid_dates or not test_dates:
        return {
            "status": "INSUFFICIENT_DATES",
            "live_mutation": False,
            "promotion_allowed": False,
        }
    train = [row for row in rows if row.get("date") in train_dates]
    valid = [row for row in rows if row.get("date") in valid_dates]
    test = [dict(row) for row in rows if row.get("date") in test_dates]

    feature_counts: dict[str, int] = defaultdict(int)
    for row in train:
        for feature in (row.get("direction_features") or {}):
            feature_counts[feature] += 1

    rules = []
    for feature in sorted(feature_counts):
        rule = _direction_rule(train, feature)
        if not rule:
            continue
        validation = _rule_accuracy(valid, rule)
        rule["validation"] = validation
        accuracy = _numeric(validation.get("accuracy_pct"))
        if (
            rule["train_accuracy_pct"] >= 55.0
            and validation.get("sample", 0) >= 10
            and accuracy is not None
            and accuracy >= 52.0
        ):
            rules.append(rule)

    rules.sort(
        key=lambda rule: (
            -float((rule.get("validation") or {}).get("accuracy_pct") or 0.0),
            -float(rule.get("train_accuracy_pct") or 0.0),
            str(rule.get("feature") or ""),
        )
    )
    selected_rules = rules[:7]

    for row in test:
        if not row.get("current_direction_known"):
            row["challenger_direction"] = None
            continue
        votes: list[str] = []
        for rule in selected_rules:
            value = _numeric(
                (row.get("direction_features") or {}).get(rule["feature"])
            )
            if value is None:
                continue
            high_direction = str(rule["high_direction"])
            predicted = (
                high_direction
                if float(value) >= float(rule["threshold"])
                else ("SHORT" if high_direction == "LONG" else "LONG")
            )
            votes.append(predicted)
        if votes:
            long_votes = sum(vote == "LONG" for vote in votes)
            short_votes = len(votes) - long_votes
            if long_votes == short_votes:
                row["challenger_direction"] = row.get("current_direction")
            else:
                row["challenger_direction"] = (
                    "LONG" if long_votes > short_votes else "SHORT"
                )
        else:
            row["challenger_direction"] = row.get("current_direction")

    current_metrics = _direction_metrics(test, "current_direction")
    challenger_metrics = _direction_metrics(test, "challenger_direction")
    current_acc = _numeric(current_metrics.get("direction_accuracy_pct"))
    challenger_acc = _numeric(challenger_metrics.get("direction_accuracy_pct"))
    current_capture = _numeric(current_metrics.get("mover_capture_pct"))
    challenger_capture = _numeric(challenger_metrics.get("mover_capture_pct"))

    return {
        "status": "EVALUABLE" if selected_rules else "NO_STABLE_DIRECTION_RULES",
        "live_mutation": False,
        "promotion_allowed": False,
        "split": {
            "train_dates": len(train_dates),
            "validation_dates": len(valid_dates),
            "test_dates": len(test_dates),
        },
        "selected_rules": selected_rules,
        "current_core_direction_test": current_metrics,
        "feature_vote_challenger_test": challenger_metrics,
        "lift": {
            "direction_accuracy_pp": (
                round(challenger_acc - current_acc, 3)
                if challenger_acc is not None and current_acc is not None else None
            ),
            "mover_capture_pp": (
                round(challenger_capture - current_capture, 3)
                if challenger_capture is not None and current_capture is not None
                else None
            ),
        },
        "historical_followup_candidate": bool(
            selected_rules
            and challenger_metrics.get("sample", 0) >= 20
            and challenger_acc is not None
            and current_acc is not None
            and challenger_capture is not None
            and current_capture is not None
            and challenger_acc > current_acc
            and challenger_capture >= current_capture
        ),
        "methodology": {
            "train_pct": 50,
            "validation_pct": 25,
            "test_pct": 25,
            "validation_selects_rules": True,
            "test_is_final_holdout": True,
            "same_current_core_identities_on_test": True,
            "future_prices_are_labels_only": True,
        },
    }


def _ranked_daily(
    rows: list[dict],
    *,
    eligibility_key: str,
    rank_key: str,
    top_k: int,
) -> list[dict]:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if bool((row.get("stage") or {}).get(eligibility_key)):
            by_date[str(row.get("date") or "")].append(row)
    selected: list[dict] = []
    for _date, day_rows in sorted(by_date.items()):
        selected.extend(
            sorted(
                day_rows,
                key=lambda row: (
                    tuple(row.get(rank_key) or ()),
                    str(row.get("ticker") or ""),
                ),
                reverse=True,
            )[: max(1, int(top_k))]
        )
    return selected


def _soft_rank_daily_equal_capacity(
    rows: list[dict],
    *,
    universe_key: str,
    rank_key: str,
    capacity_by_date: dict[str, int],
) -> list[dict]:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if bool((row.get("stage") or {}).get(universe_key)):
            by_date[str(row.get("date") or "")].append(row)
    selected: list[dict] = []
    for date_value, day_rows in sorted(by_date.items()):
        capacity = int(capacity_by_date.get(date_value) or 0)
        if capacity <= 0:
            continue
        selected.extend(
            sorted(
                day_rows,
                key=lambda row: (
                    tuple(row.get(rank_key) or ()),
                    str(row.get("ticker") or ""),
                ),
                reverse=True,
            )[:capacity]
        )
    return selected


def _economic_ranker_followup(top5: dict) -> bool:
    """Predeclared v6.2.22 follow-up gate: selection quality AND economics."""
    hard = top5.get("same_capacity_hard_gate") or {}
    soft = top5.get("soft_ranker") or {}
    lift = top5.get("lift") or {}
    hard_n = int(hard.get("sample") or 0)
    soft_n = int(soft.get("sample") or 0)
    precision = _numeric(lift.get("precision_pp"))
    recall = _numeric(lift.get("recall_pp"))
    hard_return = _numeric(hard.get("avg_horizon_close_return_pct"))
    soft_return = _numeric(soft.get("avg_horizon_close_return_pct"))
    hard_pf = _numeric(hard.get("horizon_close_profit_factor"))
    soft_pf = _numeric(soft.get("horizon_close_profit_factor"))
    return bool(
        hard_n >= 20
        and soft_n >= 20
        and precision is not None and precision > 0.0
        and recall is not None and recall >= 0.0
        and hard_return is not None and soft_return is not None
        and soft_return >= hard_return
        and hard_pf is not None and soft_pf is not None
        and soft_pf >= hard_pf
    )


def _stage_ranker(
    rows: list[dict],
    *,
    name: str,
    universe_key: str,
    hard_gate_key: str,
    rank_key: str,
) -> dict:
    universe = [
        row for row in rows
        if bool((row.get("stage") or {}).get(universe_key))
        and row.get("current_direction_known")
    ]
    total_movers = sum(bool(row.get("mover_5d")) for row in universe)
    by_k: dict[str, dict] = {}
    for top_k in range(1, 6):
        current = _ranked_daily(
            universe,
            eligibility_key=hard_gate_key,
            rank_key=rank_key,
            top_k=top_k,
        )
        capacity_by_date: dict[str, int] = defaultdict(int)
        for row in current:
            capacity_by_date[str(row.get("date") or "")] += 1
        soft = _soft_rank_daily_equal_capacity(
            universe,
            universe_key=universe_key,
            rank_key=rank_key,
            capacity_by_date=capacity_by_date,
        )
        current_metrics = _population_metrics(current, total_movers=total_movers)
        soft_metrics = _population_metrics(soft, total_movers=total_movers)
        current_precision = _numeric(current_metrics.get("mover_precision_pct"))
        soft_precision = _numeric(soft_metrics.get("mover_precision_pct"))
        current_recall = _numeric(current_metrics.get("mover_recall_pct"))
        soft_recall = _numeric(soft_metrics.get("mover_recall_pct"))
        current_keys = {
            (row["date"], row["ticker"]) for row in current
        }
        recovered = [
            row for row in soft
            if (row["date"], row["ticker"]) not in current_keys
            and not bool((row.get("stage") or {}).get(hard_gate_key))
        ]
        by_k[str(top_k)] = {
            "same_capacity_hard_gate": current_metrics,
            "soft_ranker": soft_metrics,
            "lift": {
                "precision_pp": (
                    round(soft_precision - current_precision, 3)
                    if soft_precision is not None and current_precision is not None
                    else None
                ),
                "recall_pp": (
                    round(soft_recall - current_recall, 3)
                    if soft_recall is not None and current_recall is not None
                    else None
                ),
            },
            "recovered_hard_rejects": _population_metrics(
                recovered,
                total_movers=total_movers,
            ),
        }

    top5 = by_k["5"]
    top5_followup = _economic_ranker_followup(top5)
    return {
        "mode": name,
        "status": "EVALUABLE" if universe else "NO_EVALUABLE_CANDIDATES",
        "live_mutation": False,
        "promotion_allowed": False,
        "universe": {
            "sample": len(universe),
            "dates": len({row["date"] for row in universe}),
            "movers_5d": total_movers,
        },
        "top_k_equal_capacity": by_k,
        "top5_historical_followup_candidate": bool(top5_followup),
        "top5_followup_gate": {
            "requires_positive_precision_lift": True,
            "requires_nonnegative_recall_lift": True,
            "requires_no_return_deterioration": True,
            "requires_no_profit_factor_deterioration": True,
        },
        "policy": {
            "existing_scores_only": True,
            "no_new_score_formula": True,
            "no_threshold_optimization": True,
            "hard_gate_is_control": True,
            "soft_ranker_uses_exact_daily_hard_gate_capacity": True,
        },
    }


def build_economic_architecture_challengers(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    horizon_sessions: int = DEFAULT_HORIZON_SESSIONS,
    lane_label: str = "ROLLING_LATEST",
) -> dict:
    rows = _build_rows(
        canonical,
        signal_index,
        trusted_price_series,
        threshold_pct=float(threshold_pct),
        horizon_sessions=int(horizon_sessions),
    )
    return {
        "mode": MODE,
        "lane": str(lane_label or "ROLLING_LATEST").upper(),
        "status": "EVALUABLE" if rows else "NO_EVALUABLE_CANDIDATES",
        "live_mutation": False,
        "promotion_allowed": False,
        "economic_truth": {
            "threshold_pct": float(threshold_pct),
            "horizon_sessions": int(horizon_sessions),
            "definition": "favourable excursion >= threshold within <= horizon sessions",
        },
        "candidate_population": {
            "evaluable": len(rows),
            "dates": len({row["date"] for row in rows}),
            "best_direction_movers": sum(
                bool(row.get("best_mover_5d")) for row in rows
            ),
            "current_direction_known": sum(
                bool(row.get("current_direction_known")) for row in rows
            ),
            "current_direction_movers": sum(
                bool(row.get("mover_5d")) for row in rows
            ),
        },
        "core_direction_challenger": _direction_challenger(rows),
        "research_soft_gate_challenger": _stage_ranker(
            rows,
            name="RESEARCH_SOFT_GATE_EQUAL_CAPACITY_V1",
            universe_key="core",
            hard_gate_key="research",
            rank_key="research_rank",
        ),
        "fusion_ranker_challenger": _stage_ranker(
            rows,
            name="FUSION_AS_RANKER_EQUAL_CAPACITY_V1",
            universe_key="timing",
            hard_gate_key="fusion",
            rank_key="fusion_rank",
        ),
        "policy": {
            "research_only": True,
            "future_prices_are_labels_only": True,
            "gex_unchanged": True,
            "timing_unchanged": True,
            "lifecycle_unchanged": True,
            "eligibility_remains_separate_abstention_evidence": True,
            "no_live_mutation": True,
        },
    }


def combine_economic_architecture_challengers(
    rolling: dict,
    fixed: dict,
) -> dict:
    def followup(payload: dict, key: str) -> bool:
        block = payload.get(key) or {}
        if key == "core_direction_challenger":
            return bool(block.get("historical_followup_candidate"))
        return bool(block.get("top5_historical_followup_candidate"))

    keys = (
        "core_direction_challenger",
        "research_soft_gate_challenger",
        "fusion_ranker_challenger",
    )
    agreement = {
        key: {
            "rolling_followup": followup(rolling, key),
            "fixed_followup": followup(fixed, key),
            "dual_lane_followup": (
                followup(rolling, key) and followup(fixed, key)
            ),
        }
        for key in keys
    }
    return {
        "mode": "ECONOMIC_ARCHITECTURE_DUAL_LANE_DECISION_V1",
        "status": (
            "EVALUABLE"
            if str(rolling.get("status")) == "EVALUABLE"
            and str(fixed.get("status")) == "EVALUABLE"
            else "PARTIAL"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "agreement": agreement,
        "dual_lane_followup_components": [
            key for key, value in agreement.items()
            if value["dual_lane_followup"]
        ],
        "eligibility_role": (
            "Use existing Eligibility evidence as abstention/risk-control layer; "
            "6.2.19 does not retune Eligibility."
        ),
        "next_step": (
            "Only dual-lane components may proceed to prospective SHADOW journal; "
            "no historical result changes LIVE."
        ),
    }
