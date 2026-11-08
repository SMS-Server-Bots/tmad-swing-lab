from __future__ import annotations

"""TMAD 6.2.33 GEX gate calibration.

Research-only, point-in-time historical calibration of the GEX layer after the
current CORE + Research path. The purpose is to separate three effects that the
generic waterfall previously conflated:

1) no historical GEX coverage / no identifiable GEX input;
2) GEX data existed but produced no same-direction projection;
3) an explicit opposite-direction GEX contradiction existed.

The only challenger is fixed ex ante: use GEX as an abstention layer that blocks
only an explicit strong opposite-direction DIRECTIONAL/SQUEEZE projection with
an operational target. Neutral/no-projection rows are allowed. No continuous
threshold is optimized and LIVE behavior is never changed here.

Future prices are outcome labels only.
"""

from collections import Counter
from typing import Any

from economic_architecture_challengers import _build_rows as _build_architecture_rows
from economic_waterfall_calibration import _feature_payload, _population_metrics

MODE = "GEX_GATE_CALIBRATION_V1"
DECISION_MODE = "GEX_GATE_DUAL_LANE_DECISION_V1"
TARGET_PCT = 5.0
HORIZON_SESSIONS = 5
CHALLENGER = "CONTRADICTION_ONLY_ABSTENTION_V1"
STRONG_CONTRADICTION_INTENTS = frozenset({"DIRECTIONAL", "SQUEEZE"})


def _num(value: Any) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    try:
        return bool(int(float(value)))
    except Exception:
        return str(value).strip().lower() in {"true", "yes", "y", "on"}


def _gex_signal(signals: list[dict]) -> dict | None:
    rows = [
        row for row in signals
        if str(row.get("layer") or "").upper() == "GEX_DAILY"
    ]
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            float(_num(row.get("score")) or 0.0),
            int(_truthy(row.get("operational_target"))),
            str(row.get("gex_intent") or ""),
        ),
    )


def _opposite(direction: str) -> str:
    return "SHORT" if str(direction).upper() == "LONG" else "LONG"


def _strong_contradiction(signal: dict | None) -> bool:
    if not signal:
        return False
    intent = str(signal.get("gex_intent") or "").upper()
    return bool(
        intent in STRONG_CONTRADICTION_INTENTS
        and _truthy(signal.get("operational_target"))
        and not _truthy(signal.get("capped"))
    )


def _data_available(source: dict, same: dict | None, opposite: dict | None) -> bool:
    features = _feature_payload(source)
    for value in (
        source.get("has_gex"),
        features.get("has_gex"),
        source.get("gex_run_id"),
        features.get("gex_run_id"),
        source.get("source_gex_run_id"),
        features.get("source_gex_run_id"),
    ):
        if _truthy(value):
            return True
    return bool(same or opposite)


def _coverage_reason(row: dict) -> str:
    if not row.get("gex_data_available"):
        return "DATA_UNAVAILABLE"
    if row.get("same_direction_gex"):
        return "SAME_DIRECTION_PROJECTION"
    if row.get("strong_opposite_contradiction"):
        return "STRONG_OPPOSITE_CONTRADICTION"
    if row.get("opposite_direction_gex"):
        return "NON_STRONG_OPPOSITE_PROJECTION"
    return "DATA_AVAILABLE_NO_PROJECTION"


def _enriched_rows(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
) -> list[dict]:
    base = _build_architecture_rows(
        canonical,
        signal_index,
        trusted_price_series,
        threshold_pct=TARGET_PCT,
        horizon_sessions=HORIZON_SESSIONS,
    )
    out: list[dict] = []
    for row in base:
        if not row.get("current_direction_known"):
            continue
        if not bool((row.get("stage") or {}).get("research")):
            continue

        day = str(row.get("date") or "")[:10]
        ticker = str(row.get("ticker") or "").upper()
        direction = str(row.get("current_direction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            continue

        same_rows = list(
            signal_index.get((day, ticker, direction.lower()), []) or []
        )
        opposite_direction = _opposite(direction)
        opposite_rows = list(
            signal_index.get((day, ticker, opposite_direction.lower()), []) or []
        )
        same = _gex_signal(same_rows)
        opposite = _gex_signal(opposite_rows)
        source = canonical.get((day, ticker)) or {}
        available = _data_available(source, same, opposite)
        strong_opposite = _strong_contradiction(opposite)

        item = {
            **row,
            "gex_data_available": bool(available),
            "same_direction_gex": bool(same),
            "opposite_direction_gex": bool(opposite),
            "same_gex_score": _num((same or {}).get("score")),
            "same_gex_intent": str((same or {}).get("gex_intent") or "").upper() or None,
            "same_gex_operational_target": _truthy((same or {}).get("operational_target")),
            "same_gex_capped": _truthy((same or {}).get("capped")),
            "opposite_gex_score": _num((opposite or {}).get("score")),
            "opposite_gex_intent": str((opposite or {}).get("gex_intent") or "").upper() or None,
            "opposite_gex_operational_target": _truthy((opposite or {}).get("operational_target")),
            "opposite_gex_capped": _truthy((opposite or {}).get("capped")),
            "strong_opposite_contradiction": bool(strong_opposite),
            # Exact current waterfall semantics for the current CORE direction:
            # a same-direction GEX_DAILY row must exist.
            "current_gate_pass": bool((row.get("stage") or {}).get("gex")),
            # Fixed challenger: block only an explicit strong opposite GEX view.
            "challenger_pass": bool(available and not strong_opposite),
        }
        item["coverage_reason"] = _coverage_reason(item)
        out.append(item)
    return out


def _split_dates(rows: list[dict]) -> tuple[set[str], set[str], set[str]]:
    dates = sorted({
        str(row.get("date") or "")
        for row in rows
        if row.get("gex_data_available")
    })
    if len(dates) < 8:
        return set(dates), set(), set()
    n = len(dates)
    train_end = max(1, int(n * 0.50))
    valid_end = max(train_end + 1, int(n * 0.75))
    valid_end = min(valid_end, n - 1)
    return (
        set(dates[:train_end]),
        set(dates[train_end:valid_end]),
        set(dates[valid_end:]),
    )


def _not_worse(challenger: Any, control: Any) -> bool:
    c = _num(control)
    n = _num(challenger)
    if c is None and n is None:
        return True
    if c is None and n is not None:
        return True
    if c is not None and n is None:
        return False
    return bool(float(n) >= float(c))


def _not_higher(challenger: Any, control: Any) -> bool:
    c = _num(control)
    n = _num(challenger)
    if c is None and n is None:
        return True
    if c is None and n is not None:
        return True
    if c is not None and n is None:
        return False
    return bool(float(n) <= float(c))


def _comparison(rows: list[dict]) -> dict:
    identifiable = [row for row in rows if row.get("gex_data_available")]
    total_movers = sum(bool(row.get("mover_5d")) for row in identifiable)
    current = [row for row in identifiable if row.get("current_gate_pass")]
    challenger = [row for row in identifiable if row.get("challenger_pass")]
    recovered = [
        row for row in challenger
        if not row.get("current_gate_pass")
    ]
    current_rejects = [
        row for row in identifiable
        if not row.get("current_gate_pass")
    ]
    contradictions = [
        row for row in identifiable
        if row.get("strong_opposite_contradiction")
    ]

    current_metrics = _population_metrics(current, total_movers=total_movers)
    challenger_metrics = _population_metrics(challenger, total_movers=total_movers)
    recovered_metrics = _population_metrics(recovered, total_movers=total_movers)
    reject_metrics = _population_metrics(current_rejects, total_movers=total_movers)
    contradiction_metrics = _population_metrics(contradictions, total_movers=total_movers)

    current_recall = _num(current_metrics.get("mover_recall_pct"))
    challenger_recall = _num(challenger_metrics.get("mover_recall_pct"))
    gates = {
        "minimum_control_sample": int(current_metrics.get("sample") or 0) >= 20,
        "minimum_challenger_sample": int(challenger_metrics.get("sample") or 0) >= 20,
        "recovers_additional_rows": int(recovered_metrics.get("sample") or 0) > 0,
        "recall_improves": bool(
            current_recall is not None
            and challenger_recall is not None
            and challenger_recall > current_recall
        ),
        "precision_not_worse": _not_worse(
            challenger_metrics.get("mover_precision_pct"),
            current_metrics.get("mover_precision_pct"),
        ),
        "return_not_worse": _not_worse(
            challenger_metrics.get("avg_horizon_close_return_pct"),
            current_metrics.get("avg_horizon_close_return_pct"),
        ),
        "profit_factor_not_worse": _not_worse(
            challenger_metrics.get("horizon_close_profit_factor"),
            current_metrics.get("horizon_close_profit_factor"),
        ),
        "mae_not_worse": _not_higher(
            challenger_metrics.get("avg_mae_pct"),
            current_metrics.get("avg_mae_pct"),
        ),
    }
    gates["pass"] = all(bool(value) for key, value in gates.items() if key != "pass")

    return {
        "identifiable_universe": _population_metrics(
            identifiable, total_movers=total_movers
        ),
        "current_hard_alignment_gate": current_metrics,
        "challenger_contradiction_only": challenger_metrics,
        "recovered_current_rejects": recovered_metrics,
        "all_current_rejects": reject_metrics,
        "strong_opposite_contradictions": contradiction_metrics,
        "gate": gates,
    }


def build_gex_gate_calibration(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    lane_label: str = "ROLLING_LATEST",
) -> dict:
    rows = _enriched_rows(canonical, signal_index, trusted_price_series)
    identifiable = [row for row in rows if row.get("gex_data_available")]
    unavailable = [row for row in rows if not row.get("gex_data_available")]
    train_dates, valid_dates, test_dates = _split_dates(rows)

    def choose(dates: set[str]) -> list[dict]:
        return [row for row in rows if str(row.get("date") or "") in dates]

    coverage = Counter(str(row.get("coverage_reason") or "") for row in rows)
    coverage_movers = Counter(
        str(row.get("coverage_reason") or "")
        for row in rows if row.get("mover_5d")
    )
    train = _comparison(choose(train_dates)) if train_dates else {}
    validation = _comparison(choose(valid_dates)) if valid_dates else {}
    test = _comparison(choose(test_dates)) if test_dates else {}

    validation_pass = bool((validation.get("gate") or {}).get("pass"))
    test_pass = bool((test.get("gate") or {}).get("pass"))
    followup = bool(validation_pass and test_pass)

    total_post_research_movers = sum(bool(row.get("mover_5d")) for row in rows)
    unavailable_movers = sum(bool(row.get("mover_5d")) for row in unavailable)

    return {
        "mode": MODE,
        "lane": str(lane_label or "ROLLING_LATEST").upper(),
        "status": (
            "EVALUABLE"
            if identifiable and valid_dates and test_dates
            else "INSUFFICIENT_IDENTIFIABLE_GEX_HISTORY"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "candidate_family": CHALLENGER,
        "economic_truth": {
            "threshold_pct": TARGET_PCT,
            "horizon_sessions": HORIZON_SESSIONS,
            "future_prices_are_labels_only": True,
            "direction_is_current_core_direction": True,
        },
        "post_research_population": {
            "sample": len(rows),
            "dates": len({str(row.get("date") or "") for row in rows}),
            "movers_5d": total_post_research_movers,
            "gex_identifiable_sample": len(identifiable),
            "gex_identifiable_dates": len({
                str(row.get("date") or "") for row in identifiable
            }),
            "gex_unavailable_sample": len(unavailable),
            "gex_unavailable_movers": unavailable_movers,
            "unavailable_mover_share_pct": (
                round(100.0 * unavailable_movers / total_post_research_movers, 3)
                if total_post_research_movers else None
            ),
        },
        "coverage_decomposition": {
            "rows_by_reason": dict(coverage),
            "movers_by_reason": dict(coverage_movers),
            "data_unavailable": _population_metrics(
                unavailable,
                total_movers=total_post_research_movers,
            ),
            "interpretation": (
                "Rows without identifiable point-in-time GEX data are reported "
                "separately and excluded from the gate-vs-challenger decision."
            ),
        },
        "split": {
            "train_dates": len(train_dates),
            "validation_dates": len(valid_dates),
            "test_dates": len(test_dates),
        },
        "train": train,
        "validation": validation,
        "final_test": test,
        "historical_followup_candidate": followup,
        "policy": {
            "fixed_candidate_family_only": True,
            "candidate_definition": (
                "Pass a post-Research row when GEX data is identifiable unless "
                "there is an opposite-direction DIRECTIONAL/SQUEEZE GEX projection "
                "with operational_target=true and capped=false."
            ),
            "continuous_threshold_optimization": False,
            "missing_gex_data_imputed": False,
            "coverage_gap_excluded_from_decision": True,
            "current_core_direction_preserved": True,
            "core_live_unchanged": True,
            "research_live_unchanged": True,
            "gex_live_unchanged": True,
            "timing_live_unchanged": True,
            "fusion_live_unchanged": True,
            "lifecycle_live_unchanged": True,
            "prospective_required_before_any_live_change": True,
        },
    }


def combine_gex_gate_calibrations(rolling: dict, fixed: dict) -> dict:
    rolling_ok = bool(rolling.get("historical_followup_candidate"))
    fixed_ok = bool(fixed.get("historical_followup_candidate"))
    dual = bool(rolling_ok and fixed_ok)
    return {
        "mode": DECISION_MODE,
        "status": (
            "EVALUABLE"
            if str(rolling.get("status")) == "EVALUABLE"
            and str(fixed.get("status")) == "EVALUABLE"
            else "PARTIAL"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "candidate_family": CHALLENGER,
        "rolling_followup": rolling_ok,
        "fixed_followup": fixed_ok,
        "dual_lane_followup": dual,
        "next_step": (
            "PROSPECTIVE_SHADOW_GEX_CONTRADICTION_ONLY"
            if dual
            else "KEEP_GEX_LIVE_UNCHANGED_AND_REVIEW_COVERAGE_OR_NEXT_MODULE"
        ),
        "policy": {
            "same_fixed_candidate_required_both_lanes": True,
            "no_live_change_from_historical_result": True,
            "coverage_gap_is_not_treated_as_gex_model_failure": True,
        },
    }
