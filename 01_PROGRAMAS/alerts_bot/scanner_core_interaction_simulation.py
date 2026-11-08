from __future__ import annotations

"""TMAD 6.2.27 mandatory-preserving Scanner -> CORE interaction simulation.

Research-only historical simulation of the proposed Scanner ranking combined
with strict CORE Direction V2. It exists to detect configuration interactions
before any LIVE change.

The deployable-style comparison reserves the frozen static mandatory universe
(ALWAYS_ENRICH / relational-core names) for BOTH current and challenger ranking.
The pure challenger is retained only as a diagnostic.

Dynamic open/recent-alert mandatory names, same-day change-map boosts,
continuity, relationship-seed expansion and negative-cache state are not
reconstructed or imputed. Missing opposite-direction downstream rows are also
not imputed. Future prices are outcome labels only.
"""

from collections import defaultdict
from statistics import mean
from typing import Any

from economic_architecture_challengers import _build_rows as _build_architecture_rows
from economic_waterfall_calibration import _population_metrics
from frozen_config_historical_simulation import _consensus_direction, _stage_for
from scanner_recall_ranking_calibration import (
    CAPACITY,
    _build_rows as _build_scanner_rows,
    _challenger_rank,
    _current_rank,
    _split_dates,
)

MODE = "SCANNER_CORE_INTERACTION_SIMULATION_V1"
TARGET_PCT = 5.0
HORIZON_SESSIONS = 5
FROZEN_SCANNER_FEATURES = ("abs_change_percent", "rvol")

STATIC_MANDATORY_TICKERS = frozenset(
    """
SPY QQQ IWM DIA SPX VIX VVIX VIXY VIXM VXZ UVIX UVXY VXX SVIX SVXY ZIVB
XLK XLF XLE XLV XLY XLP XLI XLB XLU XLRE XLC SMH SOXX IGV HACK CIBR
KBE KRE XBI IBB OIH XOP XRT XHB ITB JETS ITA XAR XME SLX GDX GDXJ TAN
ARKK ARKG ARKF BOTZ ROBO URA COPX LIT REMX MJ TLT HYG LQD GLD SLV IBIT
ETHA TQQQ SQQQ SPXL SPXS TNA TZA UDOW SDOW SOXL SOXS FAS FAZ DPST WDRW
LABU LABD ERX ERY RETL EMTY TARK SARK NUGT DUST JNUG JDST
""".split()
)


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _mandatory_preserving_rank(
    day_rows: list[dict],
    ranked_rows: list[dict],
    *,
    capacity: int = CAPACITY,
) -> list[dict]:
    """Keep every frozen static mandatory identity, then fill by supplied rank."""
    by_ticker = {str(row["ticker"]).upper(): row for row in day_rows}
    ranked_map = {str(row["ticker"]).upper(): row for row in ranked_rows}
    mandatory = sorted(STATIC_MANDATORY_TICKERS & set(by_ticker))
    cap = max(int(capacity), len(mandatory))
    selected: list[dict] = []
    used: set[str] = set()

    for ticker in mandatory:
        selected.append(dict(ranked_map.get(ticker) or by_ticker[ticker]))
        used.add(ticker)

    for row in ranked_rows:
        ticker = str(row["ticker"]).upper()
        if ticker in used:
            continue
        selected.append(dict(row))
        used.add(ticker)
        if len(selected) >= cap:
            break

    # Missing challenger feature scores cannot remove a mandatory or leave an
    # artificial hole. Fill deterministically from point-in-time identities.
    if len(selected) < min(cap, len(day_rows)):
        for ticker in sorted(by_ticker):
            if ticker in used:
                continue
            selected.append(dict(by_ticker[ticker]))
            used.add(ticker)
            if len(selected) >= cap:
                break

    return [{**row, "rank": idx} for idx, row in enumerate(selected, start=1)]


def _ranked_selections(rows: list[dict]) -> dict[str, dict[str, list[dict]]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row["date"])].append(row)

    current_ranked = _current_rank(rows)
    challenger_ranked = _challenger_rank(rows, list(FROZEN_SCANNER_FEATURES))
    out: dict[str, dict[str, list[dict]]] = {
        "current_mandatory": {},
        "challenger_mandatory": {},
        "challenger_pure": {},
    }
    for day, day_rows in grouped.items():
        current = current_ranked.get(day, [])
        challenger = challenger_ranked.get(day, [])
        out["current_mandatory"][day] = _mandatory_preserving_rank(day_rows, current)
        out["challenger_mandatory"][day] = _mandatory_preserving_rank(day_rows, challenger)
        out["challenger_pure"][day] = [
            dict(row) for row in challenger
            if int(row.get("rank") or 0) <= CAPACITY
        ]
    return out


def _flatten(by_date: dict[str, list[dict]]) -> list[dict]:
    return [row for day in sorted(by_date) for row in by_date[day]]


def _selection_metrics(rows: list[dict], *, all_movers: int) -> dict:
    movers = [row for row in rows if bool(row.get("best_mover_5d"))]
    first_hits = [
        float(row["first_hit_session"]) for row in movers
        if row.get("first_hit_session") is not None
    ]
    return {
        "sample": len(rows),
        "dates": len({str(row.get("date") or "") for row in rows}),
        "selected_movers": len(movers),
        "precision_pct": round(100.0 * len(movers) / len(rows), 3) if rows else None,
        "recall_pct": round(100.0 * len(movers) / all_movers, 3) if all_movers else None,
        "avg_best_mfe_pct": (
            round(mean(float(row.get("best_mfe_pct") or 0.0) for row in movers), 4)
            if movers else None
        ),
        "avg_first_hit_session": round(mean(first_hits), 3) if first_hits else None,
    }


def _path_row(source: dict, direction: str) -> dict:
    path = source["long_path"] if direction == "LONG" else source["short_path"]
    return {
        "date": source["date"],
        "ticker": source["ticker"],
        "mover_5d": bool(float(path.get("mfe_pct") or 0.0) >= TARGET_PCT),
        "mfe_pct": float(path.get("mfe_pct") or 0.0),
        "mae_pct": float(path.get("mae_pct") or 0.0),
        "first_hit_session": path.get("first_hit_session"),
        "horizon_close_return_pct": path.get("horizon_close_return_pct"),
    }


def _direction_metrics(
    selected: list[dict],
    architecture_by_key: dict[tuple[str, str], dict],
    *,
    total_movers: int,
) -> dict:
    current_rows: list[dict] = []
    strict_rows: list[dict] = []
    best_movers = current_correct = strict_correct = changed = missing = 0
    for row in selected:
        source = architecture_by_key.get((str(row["date"]), str(row["ticker"])))
        if not source:
            missing += 1
            continue
        current = str(source.get("current_direction") or "").upper()
        if current not in {"LONG", "SHORT"}:
            continue
        consensus = _consensus_direction(source.get("direction_features") or {}, current)
        strict = str(consensus.get("direction") or current).upper()
        if strict not in {"LONG", "SHORT"}:
            strict = current
        changed += int(strict != current)
        current_rows.append(_path_row(source, current))
        strict_rows.append(_path_row(source, strict))
        if source.get("best_mover_5d"):
            best_movers += 1
            current_correct += int(current == source.get("best_direction"))
            strict_correct += int(strict == source.get("best_direction"))

    current_metrics = _population_metrics(current_rows, total_movers=total_movers)
    strict_metrics = _population_metrics(strict_rows, total_movers=total_movers)
    current_metrics["direction_accuracy_on_best_movers_pct"] = (
        round(100.0 * current_correct / best_movers, 3) if best_movers else None
    )
    strict_metrics["direction_accuracy_on_best_movers_pct"] = (
        round(100.0 * strict_correct / best_movers, 3) if best_movers else None
    )
    strict_metrics["changed_direction"] = changed
    return {
        "current_core": current_metrics,
        "core_v2_strict": strict_metrics,
        "core_known": len(current_rows),
        "core_coverage_pct": (
            round(100.0 * len(current_rows) / len(selected), 3) if selected else None
        ),
        "architecture_missing": missing,
    }


def _stage_profile(
    selected: list[dict],
    architecture_by_key: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    *,
    strict_v2: bool,
) -> dict:
    names = ("core", "research", "gex", "timing", "fusion")
    passes = {name: 0 for name in names}
    identifiable = changed = missing_alt = 0
    for row in selected:
        source = architecture_by_key.get((str(row["date"]), str(row["ticker"])))
        if not source:
            continue
        current = str(source.get("current_direction") or "").upper()
        if current not in {"LONG", "SHORT"}:
            continue
        stage = source.get("stage") or {}
        if strict_v2:
            consensus = _consensus_direction(source.get("direction_features") or {}, current)
            direction = str(consensus.get("direction") or current).upper()
            if direction != current:
                changed += 1
                stage, signal_rows = _stage_for(
                    signal_index, str(source["date"]), str(source["ticker"]), direction
                )
                if signal_rows <= 0:
                    missing_alt += 1
                    continue
        identifiable += 1
        for name in names:
            passes[name] += int(bool(stage.get(name)))
    return {
        "selected": len(selected),
        "identifiable": identifiable,
        "coverage_pct": round(100.0 * identifiable / len(selected), 3) if selected else None,
        "changed_direction": changed,
        "missing_opposite_direction_history": missing_alt,
        "passes": passes,
        "pass_rate_pct_on_identifiable": {
            name: round(100.0 * value / identifiable, 3) if identifiable else None
            for name, value in passes.items()
        },
        "missing_rows_are_not_imputed": True,
    }


def _research_override_top1(
    selected: list[dict],
    architecture_by_key: dict[tuple[str, str], dict],
    *,
    total_movers: int,
) -> dict:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in selected:
        source = architecture_by_key.get((str(row["date"]), str(row["ticker"])))
        if not source or not source.get("current_direction_known"):
            continue
        stage = source.get("stage") or {}
        if bool(stage.get("core")) and not bool(stage.get("research")):
            by_date[str(source["date"])].append(source)

    picked = [
        max(rows, key=lambda x: (tuple(x.get("research_rank") or ()), str(x.get("ticker") or "")))
        for _day, rows in sorted(by_date.items())
    ]
    directional = [
        _path_row(row, str(row["current_direction"]).upper()) for row in picked
    ]
    return {
        "pool_dates": len(by_date),
        "top1_identities": len(picked),
        "current_core_direction_only": True,
        "core_v2_stacking_forbidden": True,
        "economics": _population_metrics(directional, total_movers=total_movers),
    }


def _variant(
    label: str,
    selected: list[dict],
    *,
    all_movers: int,
    architecture_by_key: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
) -> dict:
    return {
        "label": label,
        "selection": _selection_metrics(selected, all_movers=all_movers),
        "direction": _direction_metrics(selected, architecture_by_key, total_movers=all_movers),
        "current_downstream": _stage_profile(
            selected, architecture_by_key, signal_index, strict_v2=False
        ),
        "core_v2_downstream": _stage_profile(
            selected, architecture_by_key, signal_index, strict_v2=True
        ),
        "research_override_top1": _research_override_top1(
            selected, architecture_by_key, total_movers=all_movers
        ),
    }


def _identity_comparison(
    current: list[dict],
    challenger: list[dict],
    architecture_by_key: dict[tuple[str, str], dict],
    *,
    all_movers: int,
) -> dict:
    cur = {(str(row["date"]), str(row["ticker"])): row for row in current}
    new = {(str(row["date"]), str(row["ticker"])): row for row in challenger}
    cur_keys, new_keys = set(cur), set(new)
    gained = [new[key] for key in sorted(new_keys - cur_keys)]
    lost = [cur[key] for key in sorted(cur_keys - new_keys)]
    return {
        "current_identities": len(cur_keys),
        "challenger_identities": len(new_keys),
        "overlap": len(cur_keys & new_keys),
        "gained": len(gained),
        "lost": len(lost),
        "replacement_pct": round(100.0 * len(gained) / len(new_keys), 3) if new_keys else None,
        "gained_selection": _selection_metrics(gained, all_movers=all_movers),
        "lost_selection": _selection_metrics(lost, all_movers=all_movers),
        "gained_direction": _direction_metrics(gained, architecture_by_key, total_movers=all_movers),
        "lost_direction": _direction_metrics(lost, architecture_by_key, total_movers=all_movers),
    }


def _lane(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    lane_label: str,
) -> dict:
    scanner_rows = _build_scanner_rows(canonical, trusted_price_series)
    _train, _valid, test_dates = _split_dates(scanner_rows)
    test = [row for row in scanner_rows if str(row["date"]) in test_dates]
    all_movers = sum(bool(row.get("best_mover_5d")) for row in test)

    architecture_rows = _build_architecture_rows(
        canonical,
        signal_index,
        trusted_price_series,
        threshold_pct=TARGET_PCT,
        horizon_sessions=HORIZON_SESSIONS,
    )
    architecture = {
        (str(row["date"]), str(row["ticker"])): row
        for row in architecture_rows if str(row["date"]) in test_dates
    }

    selections = _ranked_selections(test)
    current = _flatten(selections["current_mandatory"])
    challenger = _flatten(selections["challenger_mandatory"])
    pure = _flatten(selections["challenger_pure"])

    current_view = _variant(
        "CURRENT_STATIC_PLUS_STATIC_MANDATORY", current,
        all_movers=all_movers, architecture_by_key=architecture, signal_index=signal_index
    )
    challenger_view = _variant(
        "CHALLENGER_PLUS_STATIC_MANDATORY", challenger,
        all_movers=all_movers, architecture_by_key=architecture, signal_index=signal_index
    )
    pure_view = _variant(
        "PURE_CHALLENGER_DIAGNOSTIC", pure,
        all_movers=all_movers, architecture_by_key=architecture, signal_index=signal_index
    )
    replacement = _identity_comparison(
        current, challenger, architecture, all_movers=all_movers
    )

    flags: list[dict] = [{
        "code": "EXACT_DYNAMIC_SELECTIVE_CONTROL_NOT_IDENTIFIABLE",
        "severity": "INFO",
        "detail": (
            "Open/recent-alert mandatory names, same-day change-map boosts, continuity, "
            "relationship-seed expansion and negative-cache state are not reconstructed."
        ),
    }]

    pure_sel = pure_view["selection"]
    new_sel = challenger_view["selection"]
    pure_precision, new_precision = _num(pure_sel.get("precision_pct")), _num(new_sel.get("precision_pct"))
    pure_recall, new_recall = _num(pure_sel.get("recall_pct")), _num(new_sel.get("recall_pct"))
    if (
        pure_precision is not None and new_precision is not None
        and pure_recall is not None and new_recall is not None
        and (pure_precision - new_precision >= 3.0 or pure_recall - new_recall >= 2.0)
    ):
        flags.append({
            "code": "SCANNER_MANDATORY_RESERVATION_MATERIAL",
            "severity": "WARN",
            "detail": (
                "Pure Top-180 materially overstates the result once the frozen "
                "production mandatory universe is reserved."
            ),
        })

    baseline = current_view["direction"]["current_core"]
    new_stack = challenger_view["direction"]["core_v2_strict"]
    cur_ret, new_ret = _num(baseline.get("avg_horizon_close_return_pct")), _num(new_stack.get("avg_horizon_close_return_pct"))
    cur_pf, new_pf = _num(baseline.get("horizon_close_profit_factor")), _num(new_stack.get("horizon_close_profit_factor"))
    if (
        cur_ret is not None and new_ret is not None and cur_pf is not None and new_pf is not None
        and new_ret < cur_ret and new_pf < cur_pf
    ):
        flags.append({
            "code": "SCANNER_CORE_V2_COMBINED_ECONOMIC_REGRESSION",
            "severity": "FAIL",
            "detail": (
                "Mandatory-preserving Scanner challenger + strict CORE V2 worsens "
                "both 5D close return and profit factor versus current control."
            ),
        })

    cur_sel = current_view["selection"]
    selection_better = bool(
        _num(cur_sel.get("precision_pct")) is not None and new_precision is not None
        and _num(cur_sel.get("recall_pct")) is not None and new_recall is not None
        and float(new_sel["precision_pct"]) > float(cur_sel["precision_pct"])
        and float(new_sel["recall_pct"]) > float(cur_sel["recall_pct"])
    )
    directional_safe = bool(
        cur_ret is not None and new_ret is not None and new_ret >= cur_ret
        and cur_pf is not None and new_pf is not None and new_pf >= cur_pf
    )
    critical = [row for row in flags if row.get("severity") == "FAIL"]

    return {
        "mode": MODE,
        "lane": str(lane_label).upper(),
        "status": "EVALUABLE" if test else "INSUFFICIENT",
        "live_mutation": False,
        "promotion_allowed": False,
        "test_dates": len(test_dates),
        "test_rows": len(test),
        "test_best_direction_movers": all_movers,
        "capacity": CAPACITY,
        "frozen_scanner_features": list(FROZEN_SCANNER_FEATURES),
        "static_mandatory_universe_size": len(STATIC_MANDATORY_TICKERS),
        "current_mandatory_control": current_view,
        "challenger_mandatory": challenger_view,
        "pure_challenger_diagnostic": pure_view,
        "replacement_analysis": replacement,
        "error_flags": flags,
        "historical_chain_followup_candidate": bool(
            selection_better and directional_safe and not critical
        ),
        "identifiability": {
            "static_mandatory_preserved": True,
            "dynamic_mandatory_reconstructed": False,
            "dynamic_priority_overlays_reconstructed": False,
            "opposite_direction_missing_rows_imputed": False,
            "future_prices_labels_only": True,
        },
        "policy": {
            "scanner_live_selection_unchanged": True,
            "core_live_unchanged": True,
            "research_live_unchanged": True,
            "gex_unchanged": True,
            "timing_unchanged": True,
            "fusion_unchanged": True,
            "lifecycle_unchanged": True,
            "research_override_uses_current_core_direction_only": True,
            "prospective_required_before_any_live_change": True,
        },
    }


def build_scanner_core_interaction_simulation(
    canonical: dict[tuple[str, str], dict],
    signal_index: dict[tuple[str, str, str], list[dict]],
    trusted_price_series: dict[str, list[dict]],
    *,
    lane_label: str = "ROLLING_LATEST",
) -> dict:
    return _lane(canonical, signal_index, trusted_price_series, lane_label=lane_label)


def combine_scanner_core_interaction_simulations(rolling: dict, fixed: dict) -> dict:
    rolling_ok = bool(rolling.get("historical_chain_followup_candidate"))
    fixed_ok = bool(fixed.get("historical_chain_followup_candidate"))
    rolling_fails = [x for x in (rolling.get("error_flags") or []) if x.get("severity") == "FAIL"]
    fixed_fails = [x for x in (fixed.get("error_flags") or []) if x.get("severity") == "FAIL"]
    dual = bool(rolling_ok and fixed_ok and not rolling_fails and not fixed_fails)
    return {
        "mode": "SCANNER_CORE_INTERACTION_DUAL_LANE_DECISION_V1",
        "status": (
            "EVALUABLE"
            if rolling.get("status") == "EVALUABLE" and fixed.get("status") == "EVALUABLE"
            else "PARTIAL"
        ),
        "live_mutation": False,
        "promotion_allowed": False,
        "rolling_followup": rolling_ok,
        "fixed_followup": fixed_ok,
        "dual_lane_followup": dual,
        "critical_failures": {"rolling": rolling_fails, "fixed": fixed_fails},
        "next_step": (
            "PROSPECTIVE_SHADOW_SCANNER_SELECTION_WITH_STATIC_MANDATORY"
            if dual else "DO_NOT_PROMOTE_COMBINED_CONFIG_REVIEW_ERROR_FLAGS"
        ),
        "interpretation": (
            "Historical interaction test before changing LIVE. Static mandatory "
            "reservation is preserved; dynamic SELECTIVE overlays remain "
            "non-identifiable and require prospective shadow confirmation."
        ),
    }
