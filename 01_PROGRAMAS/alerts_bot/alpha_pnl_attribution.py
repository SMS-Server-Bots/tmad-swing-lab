from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

import config
from return_math import normalize_signal_outcome_row
from db import connect, get_runtime_state, set_runtime_state
from time_utils import ts_madrid_str
from synthetic_option_proxy import evaluate_synthetic_option_proxy
from option_outcome_bridge import journal_contract_keys, load_targeted_option_outcomes
from swing_v2 import build_swing_v2_research
from momentum_reconciliation import build_momentum_reconciliation
from research_snapshot import research_db_path, research_input_path, research_snapshot_active
from research_benchmark import compact_benchmark_summary

STAGES = (
    ("CORE", "core_detected"),
    ("RESEARCH", "research_reached"),
    ("GEX", "gex_confirmed"),
    ("TIMING", "timing_ready"),
    ("FUSION", "fusion_ready"),
    ("ENTER", "enter_generated"),
)
FEATURE_KEYS = (
    "delta_relative_strength_vs_market",
    "delta_relative_strength_vs_sector",
    "delta_momentum_score",
    "delta_change_percent",
    "delta_rvol",
    "delta_total_gex_change_pct",
    "delta_market_breadth_score",
    "delta_sector_breadth_score",
    "delta_relationship_lead_lag_score",
)
DISCOVERY_FEATURES = (
    "delta_sector_breadth_score",
    "delta_total_gex_change_pct",
    "delta_momentum_score",
    "delta_rvol",
    "delta_relative_strength_vs_market",
)
MIN_RECOMPUTE_SECONDS = 30 * 60
# 0 means full-history read. Historical replay runs detached from production,
# so evidence quality takes precedence over a fixed sampling cap.
FEATURE_SAMPLE_LIMIT = 0
SIGNAL_SAMPLE_LIMIT = 0

# Component-level replay cache. Unlike BOT_VERSION, this basis changes only
# when attribution source code or attribution inputs change, so an operational
# replay change can reuse the expensive 181k+ row attribution laboratory.
ATTRIBUTION_CACHE_SCHEMA = 4
ATTRIBUTION_CACHE_SOURCE_FILES = (
    "alpha_pnl_attribution.py",
    "synthetic_option_proxy.py",
    "option_outcome_bridge.py",
    "historical_evidence.py",
    "economic_evidence.py",
    "daily_reverse_alpha.py",
    "swing_v2.py",
    "momentum_reconciliation.py",
    "research_benchmark.py",
    "research_snapshot.py",
)
ATTRIBUTION_CACHE_TABLES = (
    ("market_outcome_audit_events", "signal_date"),
    ("layer_signal_outcomes", "signal_date"),
    ("layer_signal_history", "snapshot_date"),
    ("observation_outcomes", "observation_date"),
    ("feature_observations", "snapshot_date"),
    ("adaptive_model_metrics", "learning_run_id"),
    ("mover_discovery_runs", "training_end_date"),
    ("price_cache", "trade_date"),
)


def _limit_clause(limit: int) -> str:
    return f"LIMIT {int(limit)}" if int(limit or 0) > 0 else ""
DIRECTION_RULE_MIN_TRAIN = 6
DIRECTION_FLIP_MIN_EDGE_PCT = 0.25


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _attribution_source_fingerprint() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for name in ATTRIBUTION_CACHE_SOURCE_FILES:
        path = root / name
        if not path.exists():
            digest.update(name.encode("utf-8"))
            digest.update(b"\0MISSING\0")
            continue
        text_value = path.read_text(encoding="utf-8-sig", errors="ignore")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text_value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _attribution_history_watermark() -> dict:
    conn = connect(research_db_path(config.ALERTS_DB), readonly=True)
    try:
        tables: dict[str, dict] = {}
        for table, date_col in ATTRIBUTION_CACHE_TABLES:
            try:
                if not _table_exists(conn, table):
                    tables[table] = {"rows": 0, "max_rowid": 0, "max_date": ""}
                    continue
                row = conn.execute(
                    f"SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS max_rowid, "
                    f"COALESCE(MAX({date_col}),'') AS max_date FROM {table}"
                ).fetchone()
                tables[table] = {
                    "rows": int(row[0] or 0),
                    "max_rowid": int(row[1] or 0),
                    "max_date": str(row[2] or ""),
                }
                # Rows such as price_cache are UPSERTed in place, so row count /
                # max(rowid) alone cannot detect corrected historical values.
                if table == "price_cache":
                    cols = {
                        str(col[1])
                        for col in conn.execute("PRAGMA table_info(price_cache)").fetchall()
                    }
                    adjusted_expr = "COALESCE(adjusted_close,0)" if "adjusted_close" in cols else "0"
                    split_expr = "COALESCE(split_factor,0)" if "split_factor" in cols else "0"
                    adjustment_len_expr = (
                        "LENGTH(COALESCE(adjustment_json,''))"
                        if "adjustment_json" in cols else "0"
                    )
                    sig = conn.execute(
                        f"""SELECT
                             COALESCE(SUM(CAST(ROUND(COALESCE(open,0)*10000) AS INTEGER)),0),
                             COALESCE(SUM(CAST(ROUND(COALESCE(high,0)*10000) AS INTEGER)),0),
                             COALESCE(SUM(CAST(ROUND(COALESCE(low,0)*10000) AS INTEGER)),0),
                             COALESCE(SUM(CAST(ROUND(COALESCE(close,0)*10000) AS INTEGER)),0),
                             COALESCE(SUM(CAST(ROUND(({adjusted_expr})*10000) AS INTEGER)),0),
                             COALESCE(SUM(CAST(ROUND(({split_expr})*1000000) AS INTEGER)),0),
                             COALESCE(SUM({adjustment_len_expr}),0)
                           FROM price_cache"""
                    ).fetchone()
                    tables[table]["value_signature"] = [int(value or 0) for value in sig]
                elif table == "layer_signal_outcomes":
                    sig = conn.execute(
                        """SELECT
                             COALESCE(SUM(CAST(ROUND(COALESCE(close_return_pct,0)*10000) AS INTEGER)),0),
                             COALESCE(SUM(CAST(ROUND(COALESCE(max_favorable_pct,0)*10000) AS INTEGER)),0),
                             COALESCE(SUM(CAST(ROUND(COALESCE(max_adverse_pct,0)*10000) AS INTEGER)),0),
                             COALESCE(SUM(COALESCE(movement_5pct_hit,0)),0)
                           FROM layer_signal_outcomes"""
                    ).fetchone()
                    tables[table]["value_signature"] = [int(value or 0) for value in sig]
            except Exception as exc:
                tables[table] = {"error": f"{type(exc).__name__}: {exc}"}

        state: dict[str, str] = {}
        for key in ("alpha_lab_eod_options_snapshot", "entry_mode", "adaptive_shadow_model_id"):
            try:
                row = conn.execute(
                    "SELECT state_value FROM runtime_state WHERE state_key=?",
                    (key,),
                ).fetchone()
                raw = str(row[0] if row else "")
                state[key] = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""
            except Exception:
                state[key] = ""
    finally:
        conn.close()

    def _file_meta(path: Path) -> dict:
        meta = {"exists": path.exists(), "size": 0, "mtime_ns": 0}
        if path.exists():
            try:
                stat = path.stat()
                meta = {
                    "exists": True,
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                }
            except Exception:
                pass
        return meta

    journal_meta = {
        "eod_options": _file_meta(research_input_path(config.OUTPUT_DIR / "alpha_pnl_attribution" / "eod_selection_journal.jsonl")),
        "swing_v2_prospective": _file_meta(research_input_path(config.OUTPUT_DIR / "swing_v2" / "swing_v2_prospective_journal.jsonl")),
        "economic_benchmark_v1": _file_meta(research_input_path(Path(config.RUNTIME_ROOT) / "research_benchmarks" / "economic_benchmark_v1.json")),
    }
    return {"tables": tables, "state": state, "journal": journal_meta}


def _attribution_cache_basis() -> dict:
    return {
        "schema": ATTRIBUTION_CACHE_SCHEMA,
        "source_fingerprint": _attribution_source_fingerprint(),
        "history_watermark": _attribution_history_watermark(),
    }


def _is_mature_outcome(row: dict) -> bool:
    """Only use completed outcomes when the schema exposes completion state.

    Legacy/synthetic rows without `complete` remain valid for backwards-compatible
    validation, while production rows with complete=0 are excluded from economic
    expectancy so an in-flight zero return is never counted as a finished trade.
    """
    if "complete" not in row or row.get("complete") is None:
        return True
    try:
        return int(row.get("complete") or 0) == 1
    except Exception:
        return False


def dedupe_audit_events(rows: list[dict]) -> tuple[list[dict], int]:
    unique: dict[tuple, dict] = {}
    for row in rows:
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("direction") or "").lower(),
            str(row.get("signal_date") or ""),
            str(row.get("hit_date") or ""),
        )
        if not key[0] or not key[2]:
            continue
        if key not in unique or int(row.get("id") or 0) > int(unique[key].get("id") or 0):
            unique[key] = row
    return list(unique.values()), max(0, len(rows) - len(unique))


def _move_stats(rows: list[dict]) -> dict:
    moves = [x for x in (_f(r.get("move_pct")) for r in rows) if x is not None]
    leads = [x for x in (_f(r.get("lead_sessions")) for r in rows) if x is not None]
    hits = [x for x in (_f(r.get("first_hit_session")) for r in rows) if x is not None]
    return {
        "count": len(rows),
        "avg_move_pct": round(mean(moves), 4) if moves else None,
        "median_move_pct": round(median(moves), 4) if moves else None,
        "avg_lead_sessions": round(mean(leads), 3) if leads else None,
        "avg_first_hit_session": round(mean(hits), 3) if hits else None,
    }


def audit_stage_attribution(rows: list[dict]) -> dict:
    opportunities, duplicates = dedupe_audit_events(rows)
    total = len(opportunities)
    absolute: list[dict] = []
    cumulative: list[dict] = []
    cumulative_rows = list(opportunities)
    previous_cumulative = list(opportunities)
    for stage, field in STAGES:
        captured = [r for r in opportunities if int(r.get(field) or 0) == 1]
        abs_stats = _move_stats(captured)
        abs_stats.update({"stage": stage, "capture_rate_pct": round(100.0 * len(captured) / total, 2) if total else None})
        absolute.append(abs_stats)

        cumulative_rows = [r for r in cumulative_rows if int(r.get(field) or 0) == 1]
        lost = [r for r in previous_cumulative if r not in cumulative_rows]
        cum_stats = _move_stats(cumulative_rows)
        lost_moves = [x for x in (_f(r.get("move_pct")) for r in lost) if x is not None]
        cum_stats.update({
            "stage": stage,
            "capture_rate_pct": round(100.0 * len(cumulative_rows) / total, 2) if total else None,
            "incremental_opportunities_lost": len(lost),
            "avg_move_pct_lost_at_gate": round(mean(lost_moves), 4) if lost_moves else None,
        })
        cumulative.append(cum_stats)
        previous_cumulative = list(cumulative_rows)

    blockers: dict[str, dict] = {}
    for row in opportunities:
        cls = str(row.get("classification") or "UNCLASSIFIED").upper()
        item = blockers.setdefault(cls, {"count": 0, "moves": []})
        item["count"] += 1
        move = _f(row.get("move_pct"))
        if move is not None:
            item["moves"].append(move)
    blocker_summary = [
        {"classification": key, "count": value["count"], "avg_move_pct": round(mean(value["moves"]), 4) if value["moves"] else None}
        for key, value in sorted(blockers.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    ]
    return {
        "economic_opportunities": total,
        "duplicates_removed": duplicates,
        "absolute_stage_capture": absolute,
        "strict_cumulative_funnel": cumulative,
        "stages": cumulative,
        "blockers": blocker_summary,
        "methodology_note": "Absolute flags are non-nested; strict cumulative funnel is reported separately to avoid conflating pathways.",
    }


def fusion_timing_challenger(rows: list[dict]) -> dict:
    opportunities, duplicates = dedupe_audit_events(rows)
    timing = [r for r in opportunities if int(r.get("timing_ready") or 0) == 1]
    fusion = [r for r in opportunities if int(r.get("fusion_ready") or 0) == 1]
    blocked = [r for r in timing if int(r.get("fusion_ready") or 0) != 1]
    return {
        "mode": "SHADOW_ONLY",
        "sample_economic_opportunities": len(opportunities),
        "duplicates_removed": duplicates,
        "timing_ready": _move_stats(timing),
        "fusion_ready": _move_stats(fusion),
        "timing_ready_but_fusion_blocked": _move_stats(blocked),
        "decision_rule": "Do not mutate LIVE. Promote only after enough mature out-of-sample outcomes show superior expectancy/risk.",
    }


def dedupe_signal_outcomes(rows: list[dict]) -> tuple[list[dict], int]:
    unique: dict[tuple, dict] = {}
    valid_rows: list[dict] = []
    for row in rows:
        if _f(row.get("close_return_pct")) is None or not _is_mature_outcome(row):
            continue
        valid_rows.append(row)
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("signal_date") or ""),
            str(row.get("direction") or "").lower(),
        )
        if not key[0] or not key[1] or key[2] not in {"long", "short"}:
            continue
        if key not in unique or int(row.get("layer_signal_id") or 10**18) < int(unique[key].get("layer_signal_id") or 10**18):
            unique[key] = row
    return list(unique.values()), max(0, len(valid_rows) - len(unique))


def _max_drawdown_return_points(returns: list[float]) -> float | None:
    if not returns:
        return None
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    return round(max_dd, 4)


def _economic_metrics(rows: list[dict], *, return_values: list[float] | None = None) -> dict:
    ordered = sorted(rows, key=lambda r: (str(r.get("signal_date") or ""), int(r.get("layer_signal_id") or 0)))
    returns = return_values if return_values is not None else [x for x in (_f(r.get("close_return_pct")) for r in ordered) if x is not None]
    pos = [x for x in returns if x > 0]
    neg = [x for x in returns if x < 0]
    dates = {str(r.get("signal_date") or "") for r in ordered if r.get("signal_date")}
    favorable = [x for x in (_f(r.get("max_favorable_pct")) for r in ordered) if x is not None] if return_values is None else []
    adverse = [x for x in (_f(r.get("max_adverse_pct")) for r in ordered) if x is not None] if return_values is None else []
    return {
        "sample": len(returns),
        "days": len(dates),
        "frequency_per_day": round(len(returns) / len(dates), 3) if dates else None,
        "win_rate_pct": round(100.0 * len(pos) / len(returns), 2) if returns else None,
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "median_return_pct": round(median(returns), 4) if returns else None,
        "profit_factor": round(sum(pos) / abs(sum(neg)), 4) if neg and sum(neg) else None,
        "max_drawdown_return_points": _max_drawdown_return_points(returns),
        "avg_mfe_pct": round(mean(favorable), 4) if favorable else None,
        "avg_mae_pct": round(mean(adverse), 4) if adverse else None,
    }


def signal_pnl_proxy(rows: list[dict]) -> dict:
    trades, duplicates = dedupe_signal_outcomes(rows)
    result = _economic_metrics(trades)
    result.update({
        "lane": "CONFIRMED_LAYER_SIGNALS_UNDERLYING_PROXY",
        "duplicates_removed": duplicates,
        "mature_outcomes_only_when_completion_flag_available": True,
    })
    return result


def _dedupe_joined(rows: list[dict], *, include_layer: bool = True, include_origin: bool = False) -> list[dict]:
    unique: dict[tuple, dict] = {}
    for row in rows:
        if _f(row.get("close_return_pct")) is None or not _is_mature_outcome(row):
            continue
        key_parts: list[Any] = [
            str(row.get("ticker") or "").upper(),
            str(row.get("signal_date") or ""),
            str(row.get("direction") or "").lower(),
        ]
        if include_layer:
            key_parts.append(str(row.get("layer") or "").upper())
        if include_origin:
            key_parts.append(str(row.get("signal_origin") or "").upper())
        key = tuple(key_parts)
        if not key[0] or not key[1]:
            continue
        signal_id = int(row.get("layer_signal_id") or 10**18)
        if key not in unique or signal_id < int(unique[key].get("layer_signal_id") or 10**18):
            unique[key] = row
    return list(unique.values())


def _is_timing_ready_fusion(row: dict) -> bool:
    if str(row.get("layer") or "").upper() != "FUSION":
        return False
    payload = _json(row.get("payload_json"))
    contributors = {str(x).upper() for x in (payload.get("contributor_engines") or [])}
    intraday_state = str(payload.get("intraday_state") or "").upper()
    opportunity_state = str(row.get("opportunity_state") or payload.get("opportunity_state") or row.get("state") or "").upper()
    return intraday_state == "ENTER" or "TIMING" in contributors or opportunity_state in {"READY", "ENTER"}


def economic_lane_analysis(rows: list[dict]) -> dict:
    mature = _dedupe_joined(rows, include_layer=True, include_origin=False)
    by_layer: dict[str, list[dict]] = {}
    for row in mature:
        layer = str(row.get("layer") or "UNKNOWN").upper()
        by_layer.setdefault(layer, []).append(row)
    layer_metrics = {
        layer: _economic_metrics(items)
        for layer, items in sorted(by_layer.items())
        if len(items) >= 2
    }

    fusion = [r for r in mature if str(r.get("layer") or "").upper() == "FUSION"]
    strict = [r for r in fusion if int(r.get("actionable") or 0) == 1]
    soft = [r for r in fusion if _is_timing_ready_fusion(r)]
    soft_blocked = [r for r in soft if int(r.get("actionable") or 0) != 1]
    strict_metrics = _economic_metrics(strict)
    soft_metrics = _economic_metrics(soft)
    blocked_metrics = _economic_metrics(soft_blocked)
    return {
        "mode": "SHADOW_ONLY",
        "mature_joined_signals": len(mature),
        "layer_metrics": layer_metrics,
        "strict_fusion": strict_metrics,
        "soft_fusion_timing_ready": soft_metrics,
        "timing_ready_but_strict_fusion_blocked": blocked_metrics,
        "soft_minus_strict_avg_return_pp": (
            round(float(soft_metrics["avg_return_pct"]) - float(strict_metrics["avg_return_pct"]), 4)
            if soft_metrics.get("avg_return_pct") is not None and strict_metrics.get("avg_return_pct") is not None else None
        ),
        "soft_minus_strict_profit_factor": (
            round(float(soft_metrics["profit_factor"]) - float(strict_metrics["profit_factor"]), 4)
            if soft_metrics.get("profit_factor") is not None and strict_metrics.get("profit_factor") is not None else None
        ),
        "policy": "FUSION remains unchanged in production; this lane measures whether converting the hard veto into a score would improve expectancy.",
    }


def directional_specialist_challenger(rows: list[dict]) -> dict:
    mature = _dedupe_joined(rows, include_layer=True, include_origin=True)
    core = [r for r in mature if str(r.get("layer") or "").upper() == "CORE"]
    base = core if len(core) >= 20 else mature
    dates = sorted({str(r.get("signal_date") or "") for r in base if r.get("signal_date")})
    if len(dates) < 5 or len(base) < 30:
        return {"mode": "SHADOW_ONLY", "status": "INSUFFICIENT", "sample": len(base), "dates": len(dates)}

    split_idx = max(1, int(len(dates) * 0.60))
    split_date = dates[min(split_idx, len(dates) - 1)]
    train = [r for r in base if str(r.get("signal_date") or "") < split_date]
    test = [r for r in base if str(r.get("signal_date") or "") >= split_date]

    grouped: dict[str, list[float]] = {}
    for row in train:
        origin = str(row.get("signal_origin") or "UNKNOWN").upper()
        value = _f(row.get("close_return_pct"))
        if value is not None:
            grouped.setdefault(origin, []).append(value)

    rules: dict[str, dict] = {}
    for origin, values in grouped.items():
        avg = mean(values) if values else 0.0
        flip = len(values) >= DIRECTION_RULE_MIN_TRAIN and avg <= -DIRECTION_FLIP_MIN_EDGE_PCT
        rules[origin] = {
            "train_sample": len(values),
            "train_avg_directional_return_pct": round(avg, 4),
            "action": "FLIP" if flip else "KEEP",
        }

    baseline_returns: list[float] = []
    specialist_returns: list[float] = []
    flipped = 0
    for row in sorted(test, key=lambda r: (str(r.get("signal_date") or ""), int(r.get("layer_signal_id") or 0))):
        value = _f(row.get("close_return_pct"))
        if value is None:
            continue
        origin = str(row.get("signal_origin") or "UNKNOWN").upper()
        flip = (rules.get(origin) or {}).get("action") == "FLIP"
        baseline_returns.append(value)
        specialist_returns.append(-value if flip else value)
        if flip:
            flipped += 1

    baseline_metrics = _economic_metrics(test, return_values=baseline_returns)
    specialist_metrics = _economic_metrics(test, return_values=specialist_returns)
    return {
        "mode": "SHADOW_ONLY",
        "status": "EVALUABLE" if len(specialist_returns) >= 30 else "EARLY",
        "method": "60/40 date walk-forward by signal_origin; flip direction only when earlier-date training has >=6 outcomes and mean directional return <= -0.25%",
        "split_date": split_date,
        "train_dates": len([d for d in dates if d < split_date]),
        "test_dates": len([d for d in dates if d >= split_date]),
        "train_sample": len(train),
        "test_sample": len(test),
        "rules": rules,
        "test_flipped": flipped,
        "test_flip_rate_pct": round(100.0 * flipped / len(specialist_returns), 2) if specialist_returns else None,
        "baseline": baseline_metrics,
        "directional_specialist": specialist_metrics,
        "avg_return_lift_pp": (
            round(float(specialist_metrics["avg_return_pct"]) - float(baseline_metrics["avg_return_pct"]), 4)
            if specialist_metrics.get("avg_return_pct") is not None and baseline_metrics.get("avg_return_pct") is not None else None
        ),
        "profit_factor_lift": (
            round(float(specialist_metrics["profit_factor"]) - float(baseline_metrics["profit_factor"]), 4)
            if specialist_metrics.get("profit_factor") is not None and baseline_metrics.get("profit_factor") is not None else None
        ),
        "policy": "Diagnostic only; no LONG/SHORT production mutation.",
    }


def _percentile_groups(pairs: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    if len(pairs) < 8:
        return [], []
    ordered = sorted(pairs, key=lambda x: x[0])
    q = max(1, len(ordered) // 4)
    return [y for _, y in ordered[-q:]], [y for _, y in ordered[:q]]


def _dedupe_feature_rows(rows: list[dict]) -> list[dict]:
    unique: dict[tuple, dict] = {}
    for row in rows:
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("observation_date") or row.get("snapshot_date") or ""),
            int(row.get("horizon_sessions") or 0),
        )
        if not key[0] or not key[1] or not key[2]:
            continue
        oid = int(row.get("observation_id") or row.get("feature_observation_id") or 10**18)
        if key not in unique or oid < int(unique[key].get("observation_id") or unique[key].get("feature_observation_id") or 10**18):
            unique[key] = row
    return list(unique.values())


_DISCOVERY_VALUES_CACHE_KEY = "__tmad_attr_discovery_values"
_REGIME_CACHE_KEY = "__tmad_attr_regime"


def _discovery_values(row: dict) -> tuple[float | None, ...]:
    cached = row.get(_DISCOVERY_VALUES_CACHE_KEY)
    if isinstance(cached, tuple) and len(cached) == len(DISCOVERY_FEATURES):
        return cached
    features = _json(row.get("features_json"))
    values = tuple(_f(features.get(key)) for key in DISCOVERY_FEATURES)
    row[_DISCOVERY_VALUES_CACHE_KEY] = values
    if _REGIME_CACHE_KEY not in row:
        value = row.get("market_regime") or features.get("market_regime") or "UNKNOWN"
        row[_REGIME_CACHE_KEY] = str(value).upper().strip() or "UNKNOWN"
    return values


def _row_regime(row: dict) -> str:
    cached = row.get(_REGIME_CACHE_KEY)
    if cached is not None:
        return str(cached)
    features = _json(row.get("features_json"))
    value = row.get("market_regime") or features.get("market_regime") or "UNKNOWN"
    regime = str(value).upper().strip() or "UNKNOWN"
    row[_REGIME_CACHE_KEY] = regime
    return regime


def _prepare_feature_rows(
    rows: list[dict],
    feature_keys: tuple[str, ...],
) -> list[tuple[dict, tuple[float | None, ...]]]:
    """Prepare compact feature vectors and reuse discovery values across challengers."""
    keys = tuple(feature_keys)
    if keys == DISCOVERY_FEATURES:
        return [(row, _discovery_values(row)) for row in rows]
    prepared: list[tuple[dict, tuple[float | None, ...]]] = []
    for row in rows:
        features = _json(row.get("features_json"))
        prepared.append((row, tuple(_f(features.get(key)) for key in keys)))
    return prepared


def feature_usefulness(rows: list[dict]) -> list[dict]:
    unique = _dedupe_feature_rows(rows)
    prepared = _prepare_feature_rows(unique, FEATURE_KEYS)
    result = []
    for index, feature in enumerate(FEATURE_KEYS):
        pairs: list[tuple[float, float]] = []
        for row, values in prepared:
            x, y = values[index], _f(row.get("return_long_pct"))
            if x is not None and y is not None:
                pairs.append((x, y))
        top, bottom = _percentile_groups(pairs)
        edge = mean(top) - mean(bottom) if top and bottom else None
        result.append({
            "feature": feature,
            "sample": len(pairs),
            "confidence": "EVALUABLE" if len(pairs) >= 30 else ("EARLY" if len(pairs) >= 12 else "INSUFFICIENT"),
            "top_quartile_avg_future_return_pct": round(mean(top), 4) if top else None,
            "bottom_quartile_avg_future_return_pct": round(mean(bottom), 4) if bottom else None,
            "directional_edge_pct": round(edge, 4) if edge is not None else None,
        })
    return sorted(result, key=lambda r: abs(r.get("directional_edge_pct") or 0.0), reverse=True)


def _percentile_rank(value: float, ordered: list[float]) -> float:
    if not ordered:
        return 0.5
    lo, hi = 0, len(ordered)
    while lo < hi:
        mid = (lo + hi) // 2
        if ordered[mid] <= value:
            lo = mid + 1
        else:
            hi = mid
    return lo / len(ordered)


def scanner_discovery_challenger(rows: list[dict]) -> dict:
    """Out-of-time discovery challenger with train-only feature distributions.

    v6.1.102 hardens the economic evidence before any LIVE mutation:
    - feature signs are learned only on earlier dates;
    - percentile distributions are also frozen on the earlier train window;
    - test results are reported separately for 1/2/3/5 sessions;
    - the mirror/inverse lane remains SHADOW-only.
    """
    unique = _dedupe_feature_rows(rows)
    dates = sorted({str(r.get("observation_date") or r.get("snapshot_date") or "") for r in unique})
    if len(dates) < 5 or len(unique) < 100:
        return {"mode": "SHADOW_ONLY", "status": "INSUFFICIENT", "sample": len(unique), "dates": len(dates)}

    split_idx = max(1, int(len(dates) * 0.60))
    split_date = dates[min(split_idx, len(dates) - 1)]
    prepared = _prepare_feature_rows(unique, DISCOVERY_FEATURES)
    train = [
        item for item in prepared
        if str(item[0].get("observation_date") or item[0].get("snapshot_date") or "") < split_date
    ]
    test = [
        item for item in prepared
        if str(item[0].get("observation_date") or item[0].get("snapshot_date") or "") >= split_date
    ]

    learned: dict[str, dict] = {}
    train_distributions: dict[str, list[float]] = {key: [] for key in DISCOVERY_FEATURES}
    for index, feature in enumerate(DISCOVERY_FEATURES):
        pairs = []
        for row, values in train:
            x, y = values[index], _f(row.get("return_long_pct"))
            if x is not None:
                train_distributions[feature].append(x)
            if x is not None and y is not None:
                pairs.append((x, y))
        train_distributions[feature].sort()
        top, bottom = _percentile_groups(pairs)
        edge = mean(top) - mean(bottom) if top and bottom else 0.0
        learned[feature] = {
            "train_edge_pct": round(edge, 4),
            "sign": 1 if edge > 0 else (-1 if edge < 0 else 0),
            "sample": len(pairs),
            "distribution_sample": len(train_distributions[feature]),
        }

    def _score_rows(
        input_rows: list[tuple[dict, tuple[float | None, ...]]],
    ) -> list[tuple[float, float, int]]:
        scored: list[tuple[float, float, int]] = []
        for row, values in input_rows:
            y = _f(row.get("return_long_pct"))
            horizon = int(row.get("horizon_sessions") or 0)
            if y is None or horizon not in (1, 2, 3, 5):
                continue
            pieces = []
            for index, feature in enumerate(DISCOVERY_FEATURES):
                value = values[index]
                sign = int(learned[feature]["sign"])
                distribution = train_distributions.get(feature) or []
                if value is None or sign == 0 or not distribution:
                    continue
                pieces.append(sign * ((_percentile_rank(value, distribution) - 0.5) * 2.0))
            if pieces:
                scored.append((sum(pieces) / len(pieces), y, horizon))
        return scored

    scored = _score_rows(test)

    def _panel(panel: list[tuple[float, float, int]]) -> dict:
        pairs = [(score, y) for score, y, _h in panel]
        top, bottom = _percentile_groups(pairs)
        edge = mean(top) - mean(bottom) if top and bottom else None
        inverse_pairs = [(-score, y) for score, y in pairs]
        inverse_top, inverse_bottom = _percentile_groups(inverse_pairs)
        inverse_edge = mean(inverse_top) - mean(inverse_bottom) if inverse_top and inverse_bottom else None
        return {
            "sample": len(pairs),
            "primary": {
                "top_quartile_avg_future_return_pct": round(mean(top), 4) if top else None,
                "bottom_quartile_avg_future_return_pct": round(mean(bottom), 4) if bottom else None,
                "out_of_time_directional_edge_pct": round(edge, 4) if edge is not None else None,
            },
            "inverse": {
                "top_quartile_avg_future_return_pct": round(mean(inverse_top), 4) if inverse_top else None,
                "bottom_quartile_avg_future_return_pct": round(mean(inverse_bottom), 4) if inverse_bottom else None,
                "out_of_time_directional_edge_pct": round(inverse_edge, 4) if inverse_edge is not None else None,
            },
        }

    aggregate = _panel(scored)
    by_horizon = {str(h): _panel([row for row in scored if row[2] == h]) for h in (1, 2, 3, 5)}
    evaluable_inverse_edges = [
        panel["inverse"]["out_of_time_directional_edge_pct"]
        for panel in by_horizon.values()
        if int(panel.get("sample") or 0) >= 30
        and panel.get("inverse", {}).get("out_of_time_directional_edge_pct") is not None
    ]
    positive_inverse_horizons = sum(1 for value in evaluable_inverse_edges if float(value) > 0)

    edge = aggregate["primary"]["out_of_time_directional_edge_pct"]
    inverse_edge = aggregate["inverse"]["out_of_time_directional_edge_pct"]
    return {
        "mode": "SHADOW_ONLY",
        "status": "EVALUABLE" if len(scored) >= 30 else "EARLY",
        "method": "60/40 date walk-forward; feature sign and percentile distributions frozen on earlier train dates; later outcomes scored out-of-time",
        "train_dates": len([d for d in dates if d < split_date]),
        "test_dates": len([d for d in dates if d >= split_date]),
        "split_date": split_date,
        "train_sample": len(train),
        "test_sample": len(scored),
        "learned_features": learned,
        "top_quartile_avg_future_return_pct": aggregate["primary"]["top_quartile_avg_future_return_pct"],
        "bottom_quartile_avg_future_return_pct": aggregate["primary"]["bottom_quartile_avg_future_return_pct"],
        "out_of_time_directional_edge_pct": edge,
        "by_horizon": by_horizon,
        "inverse_shadow_candidate": {
            "status": "EVALUABLE" if len(scored) >= 30 else "EARLY",
            "top_quartile_avg_future_return_pct": aggregate["inverse"]["top_quartile_avg_future_return_pct"],
            "bottom_quartile_avg_future_return_pct": aggregate["inverse"]["bottom_quartile_avg_future_return_pct"],
            "out_of_time_directional_edge_pct": inverse_edge,
            "evaluable_horizons": len(evaluable_inverse_edges),
            "positive_edge_horizons": positive_inverse_horizons,
            "robust_across_horizons": bool(evaluable_inverse_edges and positive_inverse_horizons == len(evaluable_inverse_edges)),
            "activation_rule": "research_only_when_primary_edge_is_negative; inspect per-horizon robustness before any prospective promotion",
            "active_for_research": bool(edge is not None and edge < 0),
        },
        "policy": "Diagnostic only; no Scanner/CORE/FUSION threshold mutation.",
    }



def _rank_scheme_from_prepared(
    prepared: list[tuple[dict, tuple[float | None, ...]]],
    *,
    horizon: int | None = None,
) -> dict:
    subset = [
        item for item in prepared
        if horizon is None or int(item[0].get("horizon_sessions") or 0) == int(horizon)
    ]
    learned: dict[str, dict] = {}
    for index, feature in enumerate(DISCOVERY_FEATURES):
        distribution: list[float] = []
        pairs: list[tuple[float, float]] = []
        for row, values in subset:
            x, y = values[index], _f(row.get("return_long_pct"))
            if x is not None:
                distribution.append(x)
            if x is not None and y is not None:
                pairs.append((x, y))
        distribution.sort()
        top, bottom = _percentile_groups(pairs)
        edge = mean(top) - mean(bottom) if top and bottom else 0.0
        learned[feature] = {
            "sign": 1 if edge > 0 else (-1 if edge < 0 else 0),
            "train_edge_pct": round(edge, 4),
            "sample": len(pairs),
            "distribution": distribution,
        }
    return learned


def _rank_scheme_from_train(rows: list[dict], *, horizon: int | None = None) -> dict:
    return _rank_scheme_from_prepared(
        _prepare_feature_rows(rows, DISCOVERY_FEATURES),
        horizon=horizon,
    )


def _score_feature_values(
    values: tuple[float | None, ...],
    scheme: dict,
) -> float | None:
    pieces: list[float] = []
    for index, feature in enumerate(DISCOVERY_FEATURES):
        item = scheme.get(feature) or {}
        value = values[index]
        sign = int(item.get("sign") or 0)
        distribution = item.get("distribution") or []
        if value is None or sign == 0 or not distribution:
            continue
        pieces.append(sign * ((_percentile_rank(value, distribution) - 0.5) * 2.0))
    return sum(pieces) / len(pieces) if pieces else None


def _score_with_scheme(row: dict, scheme: dict) -> float | None:
    return _score_feature_values(_discovery_values(row), scheme)


def _strategy_metrics(returns: list[tuple[str, float]]) -> dict:
    ordered = sorted(returns, key=lambda item: item[0])
    values = [float(value) for _day, value in ordered]
    positives = [value for value in values if value > 0]
    negatives = [value for value in values if value < 0]
    return {
        "sample": len(values),
        "days": len({day for day, _value in ordered}),
        "win_rate_pct": round(100.0 * len(positives) / len(values), 2) if values else None,
        "avg_return_pct": round(mean(values), 4) if values else None,
        "median_return_pct": round(median(values), 4) if values else None,
        "profit_factor": round(sum(positives) / abs(sum(negatives)), 4) if negatives and sum(negatives) else None,
        "max_drawdown_return_points": _max_drawdown_return_points(values),
    }


def horizon_specific_daily_pair_challenger(rows: list[dict]) -> dict:
    """Historical OOT comparison of pooled vs horizon-specific feature signs.

    Each scheme is learned only on the earlier 60% of market dates. On later
    unseen dates it selects at most one LONG (highest score) and one SHORT
    (lowest score) per horizon/day. This tests the product we actually want:
    a very short daily swing list, not a global quartile correlation.
    """
    unique = _dedupe_feature_rows(rows)
    dates = sorted({
        str(r.get("observation_date") or r.get("snapshot_date") or "")
        for r in unique
        if r.get("observation_date") or r.get("snapshot_date")
    })
    if len(dates) < 10 or len(unique) < 200:
        return {"mode": "SHADOW_RESEARCH_ONLY", "status": "INSUFFICIENT", "sample": len(unique), "dates": len(dates)}

    split_idx = max(1, int(len(dates) * 0.60))
    split_date = dates[min(split_idx, len(dates) - 1)]
    prepared = _prepare_feature_rows(unique, DISCOVERY_FEATURES)
    train = [
        item for item in prepared
        if str(item[0].get("observation_date") or item[0].get("snapshot_date") or "") < split_date
    ]
    test = [
        item for item in prepared
        if str(item[0].get("observation_date") or item[0].get("snapshot_date") or "") >= split_date
    ]
    pooled_scheme = _rank_scheme_from_prepared(train)
    horizon_schemes = {h: _rank_scheme_from_prepared(train, horizon=h) for h in (1, 2, 3, 5)}

    output: dict[str, dict] = {}
    for horizon in (1, 2, 3, 5):
        h_rows = [item for item in test if int(item[0].get("horizon_sessions") or 0) == horizon]
        by_day: dict[str, list[tuple[dict, tuple[float | None, ...]]]] = {}
        for row, values in h_rows:
            day = str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]
            if day:
                by_day.setdefault(day, []).append((row, values))

        schemes = {"pooled": pooled_scheme, "horizon_specific": horizon_schemes[horizon]}
        scheme_results: dict[str, dict] = {}
        for name, scheme in schemes.items():
            selected_returns: list[tuple[str, float]] = []
            selected_long = selected_short = eligible_days = 0
            for day, day_rows in sorted(by_day.items()):
                scored: list[tuple[float, dict]] = []
                for row, values in day_rows:
                    score = _score_feature_values(values, scheme)
                    y = _f(row.get("return_long_pct"))
                    if score is not None and y is not None:
                        scored.append((score, row))
                if len(scored) < 4:
                    continue
                eligible_days += 1
                scored.sort(key=lambda item: item[0])
                low_score, low_row = scored[0]
                high_score, high_row = scored[-1]
                high_return = _f(high_row.get("return_long_pct"))
                low_return = _f(low_row.get("return_long_pct"))
                if high_return is not None:
                    selected_returns.append((day, float(high_return)))
                    selected_long += 1
                if low_return is not None and str(low_row.get("ticker") or "").upper() != str(high_row.get("ticker") or "").upper():
                    selected_returns.append((day, -float(low_return)))
                    selected_short += 1
            metrics = _strategy_metrics(selected_returns)
            metrics.update({
                "eligible_days": eligible_days,
                "long_selections": selected_long,
                "short_selections": selected_short,
            })
            scheme_results[name] = metrics

        pooled = scheme_results["pooled"]
        specific = scheme_results["horizon_specific"]
        scheme_results["horizon_specific"]["avg_return_lift_vs_pooled_pp"] = (
            round(float(specific["avg_return_pct"]) - float(pooled["avg_return_pct"]), 4)
            if specific.get("avg_return_pct") is not None and pooled.get("avg_return_pct") is not None else None
        )
        scheme_results["horizon_specific"]["profit_factor_lift_vs_pooled"] = (
            round(float(specific["profit_factor"]) - float(pooled["profit_factor"]), 4)
            if specific.get("profit_factor") is not None and pooled.get("profit_factor") is not None else None
        )
        output[str(horizon)] = {
            "test_rows": len(h_rows),
            "pooled": pooled,
            "horizon_specific": specific,
            "learned_feature_signs": {
                feature: {
                    "sign": int((horizon_schemes[horizon].get(feature) or {}).get("sign") or 0),
                    "train_edge_pct": (horizon_schemes[horizon].get(feature) or {}).get("train_edge_pct"),
                    "sample": (horizon_schemes[horizon].get(feature) or {}).get("sample"),
                }
                for feature in DISCOVERY_FEATURES
            },
        }

    swing = [output[str(h)]["horizon_specific"] for h in (2, 3, 5)]
    evaluable = [m for m in swing if int(m.get("sample") or 0) >= 20]
    positive = [m for m in evaluable if float(m.get("avg_return_pct") or 0.0) > 0.0 and float(m.get("profit_factor") or 0.0) > 1.0]
    improved = [
        m for m in evaluable
        if float(m.get("avg_return_lift_vs_pooled_pp") or 0.0) > 0.0
    ]
    return {
        "mode": "SHADOW_RESEARCH_ONLY",
        "status": "EVALUABLE" if sum(int(x.get("sample") or 0) for x in swing) >= 60 else "EARLY",
        "method": "60/40 date OOT; train-only feature signs/distributions; per test day/horizon select <=1 LONG + <=1 SHORT; no future data enters ranking",
        "split_date": split_date,
        "train_dates": len([d for d in dates if d < split_date]),
        "test_dates": len([d for d in dates if d >= split_date]),
        "by_horizon": output,
        "swing_2_5": {
            "evaluable_horizons": len(evaluable),
            "positive_expectancy_horizons": len(positive),
            "horizon_specific_beats_pooled_horizons": len(improved),
            "prospective_candidate": bool(len(evaluable) == 3 and len(positive) == 3 and len(improved) >= 2),
        },
        "policy": {
            "live_mutation": False,
            "auto_promote": False,
            "historical_oot_is_diagnostic_not_prospective_proof": True,
            "daily_list_capacity_max": 2,
        },
    }



def model_generation_change_point(conn: sqlite3.Connection, *, window: int = 8) -> dict:
    """Locate the strongest historical deterioration between adjacent model eras.

    Generations are correlated and this is not a statistical proof of a regime
    break. It is a read-only diagnostic that prevents the bot from mistaking two
    already-degraded recent windows for evidence of stability.
    """
    if not _table_exists(conn, "model_versions"):
        return {"status": "UNAVAILABLE", "policy": {"live_mutation": False}}

    conn.row_factory = sqlite3.Row
    raw = [dict(r) for r in conn.execute(
        """SELECT id,model_key,created_at_madrid,training_end_date,status,
                  validation_sample,validation_win_rate,validation_avg_return_pct,
                  validation_profit_factor,params_json
           FROM model_versions
           WHERE model_key LIKE 'core-mover-discovery-%'
             AND validation_sample>=30
             AND validation_avg_return_pct IS NOT NULL
             AND validation_profit_factor IS NOT NULL
           ORDER BY created_at_madrid,id"""
    ).fetchall()]
    if len(raw) < max(12, window * 2):
        return {"status": "INSUFFICIENT", "models": len(raw), "policy": {"live_mutation": False}}

    def _main_thresholds(row: dict) -> dict:
        params = _json(row.get("params_json"))
        rules = params.get("rules") or {}
        def _condition(rule_name: str, feature: str) -> float | None:
            for item in (rules.get(rule_name) or {}).get("conditions") or []:
                if str(item.get("feature") or "") == feature:
                    return _f(item.get("threshold"))
            return None
        return {
            "long_atr": _condition("DISCOVERED_MOVER_LONG_1", "atr_pct"),
            "long_breadth": _condition("DISCOVERED_MOVER_LONG_2", "market_breadth_score"),
            "long_momentum_5d": _condition("DISCOVERED_MOVER_LONG_2", "momentum_5d_pct"),
            "short_atr": _condition("DISCOVERED_MOVER_SHORT_1", "atr_pct"),
        }

    rows = []
    for row in raw:
        rows.append({
            **row,
            "validation_avg_return_pct": float(row["validation_avg_return_pct"]),
            "validation_profit_factor": float(row["validation_profit_factor"]),
            "validation_win_rate": _f(row.get("validation_win_rate")),
            "thresholds": _main_thresholds(row),
        })

    def _block(block: list[dict]) -> dict:
        returns = [float(r["validation_avg_return_pct"]) for r in block]
        pfs = [float(r["validation_profit_factor"]) for r in block]
        wins = [float(r["validation_win_rate"]) for r in block if r.get("validation_win_rate") is not None]
        return {
            "models": len(block),
            "first_model_id": int(block[0]["id"]) if block else None,
            "last_model_id": int(block[-1]["id"]) if block else None,
            "start_date": str(block[0].get("created_at_madrid") or "")[:10] if block else None,
            "end_date": str(block[-1].get("created_at_madrid") or "")[:10] if block else None,
            "avg_return_pct": round(mean(returns), 4) if returns else None,
            "median_return_pct": round(median(returns), 4) if returns else None,
            "avg_profit_factor": round(mean(pfs), 4) if pfs else None,
            "median_profit_factor": round(median(pfs), 4) if pfs else None,
            "avg_win_rate_pct": round(mean(wins), 2) if wins else None,
            "negative_expectancy_models": sum(1 for value in returns if value < 0),
            "pf_below_one_models": sum(1 for value in pfs if value < 1),
        }

    candidates: list[dict] = []
    for split in range(window, len(rows) - window + 1):
        before_rows = rows[split-window:split]
        after_rows = rows[split:split+window]
        before = _block(before_rows)
        after = _block(after_rows)
        return_drop = float(before["avg_return_pct"]) - float(after["avg_return_pct"])
        pf_drop = float(before["avg_profit_factor"]) - float(after["avg_profit_factor"])
        win_drop = float(before["avg_win_rate_pct"] or 0.0) - float(after["avg_win_rate_pct"] or 0.0)
        candidates.append({
            "split_model_id": int(after_rows[0]["id"]),
            "split_date": str(after_rows[0].get("created_at_madrid") or "")[:10],
            "before": before,
            "after": after,
            "avg_return_drop_pp": round(return_drop, 4),
            "profit_factor_drop": round(pf_drop, 4),
            "win_rate_drop_pp": round(win_drop, 3),
            "deterioration_score": round(return_drop + 0.75 * pf_drop + 0.03 * win_drop, 4),
            "_before_rows": before_rows,
            "_after_rows": after_rows,
        })
    best = max(candidates, key=lambda item: item["deterioration_score"])

    def _threshold_block(block: list[dict]) -> dict:
        out: dict[str, dict] = {}
        for key in ("long_atr", "long_breadth", "long_momentum_5d", "short_atr"):
            values = [
                float((r.get("thresholds") or {}).get(key))
                for r in block
                if (r.get("thresholds") or {}).get(key) is not None
            ]
            if not values:
                out[key] = {"sample": 0, "mean": None}
                continue
            out[key] = {
                "sample": len(values),
                "mean": round(mean(values), 6),
                "min": round(min(values), 6),
                "max": round(max(values), 6),
            }
        return out

    before_thresholds = _threshold_block(best.pop("_before_rows"))
    after_thresholds = _threshold_block(best.pop("_after_rows"))
    threshold_shift: dict[str, dict] = {}
    for key in before_thresholds:
        left = before_thresholds[key].get("mean")
        right = after_thresholds[key].get("mean")
        relative = None
        if left is not None and right is not None and abs(float(left)) > 1e-9:
            relative = abs(float(right) - float(left)) / abs(float(left)) * 100.0
        threshold_shift[key] = {
            "before_mean": left,
            "after_mean": right,
            "relative_shift_pct": round(relative, 3) if relative is not None else None,
        }

    comparable_shifts = [
        float(item["relative_shift_pct"])
        for item in threshold_shift.values()
        if item.get("relative_shift_pct") is not None
    ]
    median_threshold_shift = median(comparable_shifts) if comparable_shifts else None
    performance_drop = float(best.get("avg_return_drop_pp") or 0.0) > 0 and float(best.get("profit_factor_drop") or 0.0) > 0
    main_rules_comparatively_stable = median_threshold_shift is not None and float(median_threshold_shift) <= 20.0

    latest = _block(rows[-window:])
    pre_break = _block(rows[max(0, rows.index(next(r for r in rows if int(r["id"]) == int(best["split_model_id"]))) - window):
                            rows.index(next(r for r in rows if int(r["id"]) == int(best["split_model_id"])))])
    return {
        "status": "EVALUABLE",
        "models": len(rows),
        "window_models": window,
        "best_historical_break": best,
        "threshold_shift_around_break": threshold_shift,
        "median_main_threshold_shift_pct": round(float(median_threshold_shift), 3) if median_threshold_shift is not None else None,
        "latest_window": latest,
        "pre_break_reference_window": pre_break,
        "diagnosis": (
            "OUTCOME_SHIFT_NOT_EXPLAINED_BY_MAIN_THRESHOLDS"
            if performance_drop and main_rules_comparatively_stable
            else "MIXED_OR_WEAK_BREAK_EVIDENCE"
        ),
        "interpretation": "Diagnostic change-point only; correlated model generations are not independent samples.",
        "policy": {
            "live_mutation": False,
            "threshold_auto_tune": False,
            "change_point_cannot_promote_or_demote_model": True,
        },
    }



def regime_horizon_daily_pair_challenger(rows: list[dict]) -> dict:
    """OOT daily-pair challenger with train-only horizon × market-regime schemes.

    The market regime is contemporaneous information. Feature signs and empirical
    distributions for each regime/horizon are fitted only on earlier dates.
    Sparse regimes fall back to the already-separated horizon-specific scheme.
    """
    unique = _dedupe_feature_rows(rows)
    dates = sorted({
        str(r.get("observation_date") or r.get("snapshot_date") or "")
        for r in unique
        if r.get("observation_date") or r.get("snapshot_date")
    })
    if len(dates) < 10 or len(unique) < 200:
        return {"mode": "SHADOW_RESEARCH_ONLY", "status": "INSUFFICIENT", "sample": len(unique), "dates": len(dates)}

    split_idx = max(1, int(len(dates) * 0.60))
    split_date = dates[min(split_idx, len(dates) - 1)]
    train = [r for r in unique if str(r.get("observation_date") or r.get("snapshot_date") or "") < split_date]
    test = [r for r in unique if str(r.get("observation_date") or r.get("snapshot_date") or "") >= split_date]

    def _regime(row: dict) -> str:
        return _row_regime(row)

    horizon_schemes = {h: _rank_scheme_from_train(train, horizon=h) for h in (1, 2, 3, 5)}
    regime_horizon_rows: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in train:
        horizon = int(row.get("horizon_sessions") or 0)
        if horizon in (1, 2, 3, 5):
            regime_horizon_rows[(horizon, _regime(row))].append(row)

    regime_schemes: dict[tuple[int, str], dict] = {}
    regime_support: dict[str, dict] = {}
    for (horizon, regime), subset in regime_horizon_rows.items():
        train_dates = {
            str(r.get("observation_date") or r.get("snapshot_date") or "")[:10]
            for r in subset
        }
        eligible = len(subset) >= 80 and len(train_dates) >= 8 and regime != "UNKNOWN"
        regime_support[f"{horizon}|{regime}"] = {
            "horizon_sessions": horizon,
            "regime": regime,
            "train_rows": len(subset),
            "train_dates": len(train_dates),
            "eligible": eligible,
        }
        if eligible:
            regime_schemes[(horizon, regime)] = _rank_scheme_from_train(subset, horizon=horizon)

    def _evaluate(horizon: int, *, regime_aware: bool) -> dict:
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in test:
            if int(row.get("horizon_sessions") or 0) != horizon:
                continue
            day = str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]
            if day:
                by_day[day].append(row)

        selected: list[tuple[str, float]] = []
        eligible_days = long_count = short_count = 0
        regime_scored_rows = fallback_rows = 0
        selected_by_regime: dict[str, list[tuple[str, float]]] = defaultdict(list)

        for day, day_rows in sorted(by_day.items()):
            scored: list[tuple[float, dict, str, bool]] = []
            for row in day_rows:
                regime = _regime(row)
                specific = regime_schemes.get((horizon, regime)) if regime_aware else None
                scheme = specific or horizon_schemes[horizon]
                score = _score_with_scheme(row, scheme)
                y = _f(row.get("return_long_pct"))
                if score is None or y is None:
                    continue
                used_regime = specific is not None
                regime_scored_rows += int(used_regime)
                fallback_rows += int(not used_regime)
                scored.append((score, row, regime, used_regime))
            if len(scored) < 4:
                continue
            eligible_days += 1
            scored.sort(key=lambda item: item[0])
            low_score, low_row, low_regime, _ = scored[0]
            high_score, high_row, high_regime, _ = scored[-1]
            high_return = _f(high_row.get("return_long_pct"))
            low_return = _f(low_row.get("return_long_pct"))
            if high_return is not None:
                item = (day, float(high_return))
                selected.append(item)
                selected_by_regime[high_regime].append(item)
                long_count += 1
            if low_return is not None and str(low_row.get("ticker") or "").upper() != str(high_row.get("ticker") or "").upper():
                item = (day, -float(low_return))
                selected.append(item)
                selected_by_regime[low_regime].append(item)
                short_count += 1

        metrics = _strategy_metrics(selected)
        metrics.update({
            "eligible_days": eligible_days,
            "long_selections": long_count,
            "short_selections": short_count,
            "regime_specific_scored_rows": regime_scored_rows,
            "fallback_scored_rows": fallback_rows,
            "regime_specific_usage_pct": round(
                100.0 * regime_scored_rows / max(1, regime_scored_rows + fallback_rows), 2
            ),
            "selected_by_regime": {
                regime: _strategy_metrics(values)
                for regime, values in sorted(selected_by_regime.items())
                if len(values) >= 4
            },
        })
        return metrics

    by_horizon: dict[str, dict] = {}
    for horizon in (1, 2, 3, 5):
        baseline = _evaluate(horizon, regime_aware=False)
        regime_aware = _evaluate(horizon, regime_aware=True)
        regime_aware["avg_return_lift_vs_horizon_only_pp"] = (
            round(float(regime_aware["avg_return_pct"]) - float(baseline["avg_return_pct"]), 4)
            if regime_aware.get("avg_return_pct") is not None and baseline.get("avg_return_pct") is not None else None
        )
        regime_aware["profit_factor_lift_vs_horizon_only"] = (
            round(float(regime_aware["profit_factor"]) - float(baseline["profit_factor"]), 4)
            if regime_aware.get("profit_factor") is not None and baseline.get("profit_factor") is not None else None
        )
        by_horizon[str(horizon)] = {
            "horizon_only": baseline,
            "regime_horizon": regime_aware,
        }

    swing_blocks = [by_horizon[str(h)]["regime_horizon"] for h in (2, 3, 5)]
    evaluable = [m for m in swing_blocks if int(m.get("sample") or 0) >= 20]
    positive = [
        m for m in evaluable
        if float(m.get("avg_return_pct") or 0.0) > 0.0
        and (m.get("profit_factor") is None or float(m.get("profit_factor") or 0.0) > 1.0)
    ]
    improved = [
        m for m in evaluable
        if float(m.get("avg_return_lift_vs_horizon_only_pp") or 0.0) > 0.0
    ]
    eligible_regimes = [item for item in regime_support.values() if item.get("eligible")]
    return {
        "mode": "SHADOW_RESEARCH_ONLY",
        "status": "EVALUABLE" if sum(int(x.get("sample") or 0) for x in swing_blocks) >= 60 else "EARLY",
        "method": "60/40 date OOT; train-only horizon×regime feature signs/distributions; sparse regimes fallback to horizon-only; <=1 LONG + <=1 SHORT per test day/horizon",
        "split_date": split_date,
        "train_dates": len([d for d in dates if d < split_date]),
        "test_dates": len([d for d in dates if d >= split_date]),
        "eligible_regime_horizon_schemes": len(eligible_regimes),
        "regime_support": sorted(
            regime_support.values(),
            key=lambda item: (int(item["horizon_sessions"]), str(item["regime"])),
        ),
        "by_horizon": by_horizon,
        "swing_2_5": {
            "evaluable_horizons": len(evaluable),
            "positive_expectancy_horizons": len(positive),
            "beats_horizon_only_horizons": len(improved),
            "prospective_candidate": bool(len(evaluable) == 3 and len(positive) >= 2 and len(improved) >= 2),
        },
        "policy": {
            "live_mutation": False,
            "auto_promote": False,
            "market_regime_is_point_in_time": True,
            "sparse_regimes_fallback_to_horizon_only": True,
            "historical_oot_is_diagnostic_not_prospective_proof": True,
        },
    }


def regime_router_daily_pair_challenger(rows: list[dict]) -> dict:
    """Nested OOT regime router over the stable horizon-specific scorer.

    Discipline:
    1) Fit horizon feature signs/distributions on the earliest 50% of dates.
    2) Use the next 25% only to decide which horizon x regime x direction
       lanes have positive economics with minimum support.
    3) Freeze those lane decisions and evaluate them on the final 25%.

    Market regime is contemporaneous. Final-test outcomes never decide which
    lanes are active. The router only removes selections from the stable
    horizon scorer; it never refits feature signs inside a regime.
    """
    unique = _dedupe_feature_rows(rows)
    dates = sorted({
        str(r.get("observation_date") or r.get("snapshot_date") or "")[:10]
        for r in unique
        if r.get("observation_date") or r.get("snapshot_date")
    })
    if len(dates) < 16 or len(unique) < 300:
        return {
            "mode": "SHADOW_RESEARCH_ONLY",
            "status": "INSUFFICIENT",
            "sample": len(unique),
            "dates": len(dates),
        }

    train_end = max(6, int(len(dates) * 0.50))
    validation_end = max(train_end + 4, int(len(dates) * 0.75))
    validation_end = min(validation_end, len(dates) - 4)
    if validation_end <= train_end:
        return {
            "mode": "SHADOW_RESEARCH_ONLY",
            "status": "INSUFFICIENT_SPLIT",
            "sample": len(unique),
            "dates": len(dates),
        }

    train_dates = dates[:train_end]
    validation_dates = dates[train_end:validation_end]
    test_dates = dates[validation_end:]
    train_set, validation_set, test_set = set(train_dates), set(validation_dates), set(test_dates)

    def _day(row: dict) -> str:
        return str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]

    def _regime(row: dict) -> str:
        return _row_regime(row)

    train = [r for r in unique if _day(r) in train_set]
    validation = [r for r in unique if _day(r) in validation_set]
    test = [r for r in unique if _day(r) in test_set]
    horizon_schemes = {h: _rank_scheme_from_train(train, horizon=h) for h in (1, 2, 3, 5)}

    def _select(window_rows: list[dict], horizon: int) -> list[dict]:
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in window_rows:
            if int(row.get("horizon_sessions") or 0) != horizon:
                continue
            day = _day(row)
            if day:
                by_day[day].append(row)

        selections: list[dict] = []
        scheme = horizon_schemes[horizon]
        for day, day_rows in sorted(by_day.items()):
            scored: list[tuple[float, dict]] = []
            for row in day_rows:
                score = _score_with_scheme(row, scheme)
                outcome = _f(row.get("return_long_pct"))
                if score is not None and outcome is not None:
                    scored.append((score, row))
            if len(scored) < 4:
                continue
            scored.sort(key=lambda item: item[0])
            low_row = scored[0][1]
            high_row = scored[-1][1]
            high_return = _f(high_row.get("return_long_pct"))
            low_return = _f(low_row.get("return_long_pct"))
            if high_return is not None:
                selections.append({
                    "day": day,
                    "ticker": str(high_row.get("ticker") or "").upper(),
                    "direction": "LONG",
                    "regime": _regime(high_row),
                    "return_pct": float(high_return),
                })
            if low_return is not None and str(low_row.get("ticker") or "").upper() != str(high_row.get("ticker") or "").upper():
                selections.append({
                    "day": day,
                    "ticker": str(low_row.get("ticker") or "").upper(),
                    "direction": "SHORT",
                    "regime": _regime(low_row),
                    "return_pct": -float(low_return),
                })
        return selections

    validation_selections: list[dict] = []
    test_selections: dict[int, list[dict]] = {}
    for horizon in (1, 2, 3, 5):
        for item in _select(validation, horizon):
            item["horizon_sessions"] = horizon
            validation_selections.append(item)
        test_selections[horizon] = _select(test, horizon)

    lanes: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
    for item in validation_selections:
        lanes[(int(item["horizon_sessions"]), str(item["regime"]), str(item["direction"]))].append(item)

    lane_validation: list[dict] = []
    eligible_lanes: set[tuple[int, str, str]] = set()
    for key, items in sorted(lanes.items()):
        metrics = _strategy_metrics([(str(x["day"]), float(x["return_pct"])) for x in items])
        pf = metrics.get("profit_factor")
        eligible = (
            int(metrics.get("sample") or 0) >= 5
            and int(metrics.get("days") or 0) >= 4
            and float(metrics.get("avg_return_pct") or 0.0) > 0.0
            and (pf is None or float(pf) > 1.0)
        )
        if eligible:
            eligible_lanes.add(key)
        lane_validation.append({
            "horizon_sessions": key[0],
            "regime": key[1],
            "direction": key[2],
            **metrics,
            "eligible": eligible,
        })

    by_horizon: dict[str, dict] = {}
    all_baseline_swing: list[tuple[str, float]] = []
    all_routed_swing: list[tuple[str, float]] = []
    for horizon in (1, 2, 3, 5):
        baseline_items = test_selections[horizon]
        routed_items = [
            item for item in baseline_items
            if (horizon, str(item["regime"]), str(item["direction"])) in eligible_lanes
        ]
        baseline_returns = [(str(x["day"]), float(x["return_pct"])) for x in baseline_items]
        routed_returns = [(str(x["day"]), float(x["return_pct"])) for x in routed_items]
        baseline_metrics = _strategy_metrics(baseline_returns)
        routed_metrics = _strategy_metrics(routed_returns)
        if horizon in (2, 3, 5):
            all_baseline_swing.extend(baseline_returns)
            all_routed_swing.extend(routed_returns)
        by_horizon[str(horizon)] = {
            "baseline_test": baseline_metrics,
            "routed_test": routed_metrics,
            "coverage_pct": round(
                100.0 * len(routed_returns) / max(1, len(baseline_returns)), 2
            ),
            "avg_return_lift_pp": (
                round(float(routed_metrics["avg_return_pct"]) - float(baseline_metrics["avg_return_pct"]), 4)
                if routed_metrics.get("avg_return_pct") is not None and baseline_metrics.get("avg_return_pct") is not None else None
            ),
            "profit_factor_lift": (
                round(float(routed_metrics["profit_factor"]) - float(baseline_metrics["profit_factor"]), 4)
                if routed_metrics.get("profit_factor") is not None and baseline_metrics.get("profit_factor") is not None else None
            ),
        }

    baseline_swing = _strategy_metrics(all_baseline_swing)
    routed_swing = _strategy_metrics(all_routed_swing)
    positive_test_horizons = 0
    beats_baseline_horizons = 0
    evaluable_test_horizons = 0
    for horizon in (2, 3, 5):
        block = by_horizon[str(horizon)]
        routed = block["routed_test"]
        baseline = block["baseline_test"]
        if int(routed.get("sample") or 0) >= 4:
            evaluable_test_horizons += 1
            if (
                float(routed.get("avg_return_pct") or 0.0) > 0.0
                and (routed.get("profit_factor") is None or float(routed.get("profit_factor") or 0.0) > 1.0)
            ):
                positive_test_horizons += 1
            if (
                routed.get("avg_return_pct") is not None
                and baseline.get("avg_return_pct") is not None
                and float(routed["avg_return_pct"]) > float(baseline["avg_return_pct"])
            ):
                beats_baseline_horizons += 1

    combined_coverage = round(
        100.0 * len(all_routed_swing) / max(1, len(all_baseline_swing)), 2
    )
    prospective_candidate = bool(
        int(routed_swing.get("sample") or 0) >= 12
        and float(routed_swing.get("avg_return_pct") or 0.0) > 0.0
        and (routed_swing.get("profit_factor") is None or float(routed_swing.get("profit_factor") or 0.0) > 1.0)
        and positive_test_horizons >= 2
        and beats_baseline_horizons >= 2
        and combined_coverage >= 20.0
    )

    return {
        "mode": "SHADOW_RESEARCH_ONLY",
        "status": "EVALUABLE" if len(test_dates) >= 4 and len(validation_dates) >= 4 else "EARLY",
        "method": "50/25/25 chronological nested OOT; fit horizon scorer on train, select eligible horizon×regime×direction lanes on validation only, freeze router, evaluate final test",
        "train_dates": len(train_dates),
        "validation_dates": len(validation_dates),
        "test_dates": len(test_dates),
        "train_end_date": train_dates[-1] if train_dates else None,
        "validation_start_date": validation_dates[0] if validation_dates else None,
        "validation_end_date": validation_dates[-1] if validation_dates else None,
        "test_start_date": test_dates[0] if test_dates else None,
        "lane_rule": {
            "min_validation_sample": 5,
            "min_validation_days": 4,
            "avg_return_pct_gt": 0.0,
            "profit_factor_gt": 1.0,
        },
        "eligible_lane_count": len(eligible_lanes),
        "lane_validation": lane_validation,
        "by_horizon": by_horizon,
        "swing_2_5": {
            "baseline_test": baseline_swing,
            "routed_test": routed_swing,
            "coverage_pct": combined_coverage,
            "evaluable_test_horizons": evaluable_test_horizons,
            "positive_test_horizons": positive_test_horizons,
            "beats_baseline_horizons": beats_baseline_horizons,
            "prospective_candidate": prospective_candidate,
        },
        "policy": {
            "live_mutation": False,
            "auto_promote": False,
            "market_regime_is_point_in_time": True,
            "regime_never_refits_feature_signs": True,
            "lane_eligibility_uses_validation_only": True,
            "final_test_never_selects_lanes": True,
            "historical_oot_is_diagnostic_not_prospective_proof": True,
        },
    }


def _latest_feature_rows(rows: list[dict]) -> list[dict]:
    """Keep the latest point-in-time observation per ticker/date/horizon."""
    unique: dict[tuple, dict] = {}
    for row in rows:
        key = (
            str(row.get("ticker") or "").upper(),
            str(row.get("observation_date") or row.get("snapshot_date") or "")[:10],
            int(row.get("horizon_sessions") or 0),
        )
        if not key[0] or not key[1] or not key[2]:
            continue
        oid = int(row.get("observation_id") or row.get("feature_observation_id") or 0)
        if key not in unique or oid > int(unique[key].get("observation_id") or unique[key].get("feature_observation_id") or 0):
            unique[key] = row
    return list(unique.values())


def _feature_history_coverage(rows: list[dict]) -> dict:
    mature = [row for row in rows if _is_mature_outcome(row)]
    dates = sorted({
        str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]
        for row in mature
        if row.get("observation_date") or row.get("snapshot_date")
    })
    canonical = [row for row in mature if int(row.get("is_canonical_daily_close") or 0) == 1]
    horizons: dict[str, int] = {}
    regimes: dict[str, int] = {}
    for row in mature:
        horizon = str(int(row.get("horizon_sessions") or 0))
        horizons[horizon] = horizons.get(horizon, 0) + 1
        regime = _row_regime(row)
        regimes[regime] = regimes.get(regime, 0) + 1
    return {
        "raw_mature_rows": len(mature),
        "unique_earliest_rows": len(_dedupe_feature_rows(mature)),
        "unique_latest_rows": len(_latest_feature_rows(mature)),
        "canonical_rows": len(canonical),
        "dates": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "horizon_rows": dict(sorted(horizons.items())),
        "regime_rows": dict(sorted(regimes.items(), key=lambda item: (-item[1], item[0]))),
        "feature_sample_limit": FEATURE_SAMPLE_LIMIT if FEATURE_SAMPLE_LIMIT > 0 else None,
        "full_history_not_truncated": FEATURE_SAMPLE_LIMIT <= 0 or len(mature) < FEATURE_SAMPLE_LIMIT,
    }


def rolling_regime_stability_challenger(rows: list[dict]) -> dict:
    """Multi-window stability router with a final untouched holdout.

    One validation window can select a lucky lane. This challenger freezes a
    horizon-specific scorer on the earliest history, evaluates lane economics
    across three sequential validation windows, and admits a
    horizon×regime×direction lane only when it is positive in at least two
    independent validation windows with sufficient aggregate support. The last
    20% of dates stays untouched until the final evaluation.
    """
    unique = _dedupe_feature_rows(rows)
    dates = sorted({
        str(r.get("observation_date") or r.get("snapshot_date") or "")[:10]
        for r in unique
        if r.get("observation_date") or r.get("snapshot_date")
    })
    if len(dates) < 30 or len(unique) < 500:
        return {
            "mode": "SHADOW_RESEARCH_ONLY",
            "status": "INSUFFICIENT",
            "sample": len(unique),
            "dates": len(dates),
        }

    train_end = max(10, int(len(dates) * 0.40))
    test_start = max(train_end + 9, int(len(dates) * 0.80))
    test_start = min(test_start, len(dates) - 6)
    validation_dates = dates[train_end:test_start]
    if len(validation_dates) < 9:
        return {
            "mode": "SHADOW_RESEARCH_ONLY",
            "status": "INSUFFICIENT_VALIDATION",
            "sample": len(unique),
            "dates": len(dates),
        }

    train_dates = dates[:train_end]
    test_dates = dates[test_start:]
    train_set, test_set = set(train_dates), set(test_dates)
    train = [r for r in unique if str(r.get("observation_date") or r.get("snapshot_date") or "")[:10] in train_set]
    test = [r for r in unique if str(r.get("observation_date") or r.get("snapshot_date") or "")[:10] in test_set]
    schemes = {h: _rank_scheme_from_train(train, horizon=h) for h in (1, 2, 3, 5)}

    fold_count = 3
    fold_bounds: list[tuple[int, int]] = []
    for fold in range(fold_count):
        start = int(round(len(validation_dates) * fold / fold_count))
        end = int(round(len(validation_dates) * (fold + 1) / fold_count))
        if end > start:
            fold_bounds.append((start, end))

    def _day(row: dict) -> str:
        return str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]

    def _regime(row: dict) -> str:
        return _row_regime(row)

    def _select(window_rows: list[dict], horizon: int) -> list[dict]:
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in window_rows:
            if int(row.get("horizon_sessions") or 0) != horizon:
                continue
            day = _day(row)
            if day:
                by_day[day].append(row)
        result: list[dict] = []
        for day, day_rows in sorted(by_day.items()):
            scored: list[tuple[float, dict]] = []
            for row in day_rows:
                score = _score_with_scheme(row, schemes[horizon])
                future = _f(row.get("return_long_pct"))
                if score is not None and future is not None:
                    scored.append((score, row))
            if len(scored) < 4:
                continue
            scored.sort(key=lambda item: item[0])
            low_row, high_row = scored[0][1], scored[-1][1]
            high_return, low_return = _f(high_row.get("return_long_pct")), _f(low_row.get("return_long_pct"))
            if high_return is not None:
                result.append({
                    "day": day,
                    "direction": "LONG",
                    "regime": _regime(high_row),
                    "ticker": str(high_row.get("ticker") or "").upper(),
                    "return_pct": float(high_return),
                })
            if low_return is not None and str(low_row.get("ticker") or "").upper() != str(high_row.get("ticker") or "").upper():
                result.append({
                    "day": day,
                    "direction": "SHORT",
                    "regime": _regime(low_row),
                    "ticker": str(low_row.get("ticker") or "").upper(),
                    "return_pct": -float(low_return),
                })
        return result

    fold_lane_metrics: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
    fold_lane_values: dict[tuple[int, str, str], list[tuple[str, float]]] = defaultdict(list)
    fold_summaries: list[dict] = []
    for fold_index, (start, end) in enumerate(fold_bounds, 1):
        fold_dates = validation_dates[start:end]
        fold_set = set(fold_dates)
        fold_rows = [r for r in unique if _day(r) in fold_set]
        selections = []
        for horizon in (1, 2, 3, 5):
            for item in _select(fold_rows, horizon):
                item["horizon_sessions"] = horizon
                selections.append(item)

        grouped: dict[tuple[int, str, str], list[dict]] = defaultdict(list)
        for item in selections:
            grouped[(int(item["horizon_sessions"]), str(item["regime"]), str(item["direction"]))].append(item)

        lane_count = 0
        for key, items in grouped.items():
            values = [(str(x["day"]), float(x["return_pct"])) for x in items]
            metrics = _strategy_metrics(values)
            fold_lane_values[key].extend(values)
            fold_lane_metrics[key].append({
                "fold": fold_index,
                "start_date": fold_dates[0] if fold_dates else None,
                "end_date": fold_dates[-1] if fold_dates else None,
                **metrics,
            })
            lane_count += 1
        fold_summaries.append({
            "fold": fold_index,
            "start_date": fold_dates[0] if fold_dates else None,
            "end_date": fold_dates[-1] if fold_dates else None,
            "dates": len(fold_dates),
            "selected": len(selections),
            "lanes": lane_count,
        })

    eligible_lanes: set[tuple[int, str, str]] = set()
    lane_stability: list[dict] = []
    for key, fold_metrics in sorted(fold_lane_metrics.items()):
        values = list(fold_lane_values.get(key) or [])
        positive_folds = 0
        supported_folds = 0
        for metric in fold_metrics:
            sample = int(metric.get("sample") or 0)
            avg = metric.get("avg_return_pct")
            pf = metric.get("profit_factor")
            if sample >= 3:
                supported_folds += 1
                if avg is not None and float(avg) > 0.0 and (pf is None or float(pf) > 1.0):
                    positive_folds += 1
        # Exact same aggregate as the previous implementation, but reuse the
        # selections already computed once per fold instead of recomputing every
        # fold×horizon for every lane.
        aggregate = _strategy_metrics(values)
        aggregate_pf = aggregate.get("profit_factor")
        eligible = bool(
            supported_folds >= 2
            and positive_folds >= 2
            and int(aggregate.get("sample") or 0) >= 8
            and float(aggregate.get("avg_return_pct") or 0.0) > 0.0
            and (aggregate_pf is None or float(aggregate_pf) > 1.0)
        )
        if eligible:
            eligible_lanes.add(key)
        lane_stability.append({
            "horizon_sessions": key[0],
            "regime": key[1],
            "direction": key[2],
            "supported_folds": supported_folds,
            "positive_folds": positive_folds,
            "fold_metrics": fold_metrics,
            "aggregate_validation": aggregate,
            "eligible": eligible,
        })

    by_horizon: dict[str, dict] = {}
    all_baseline: list[tuple[str, float]] = []
    all_stable: list[tuple[str, float]] = []
    for horizon in (1, 2, 3, 5):
        baseline_items = _select(test, horizon)
        stable_items = [
            item for item in baseline_items
            if (horizon, str(item["regime"]), str(item["direction"])) in eligible_lanes
        ]
        baseline_returns = [(str(x["day"]), float(x["return_pct"])) for x in baseline_items]
        stable_returns = [(str(x["day"]), float(x["return_pct"])) for x in stable_items]
        baseline_metrics = _strategy_metrics(baseline_returns)
        stable_metrics = _strategy_metrics(stable_returns)
        if horizon in (2, 3, 5):
            all_baseline.extend(baseline_returns)
            all_stable.extend(stable_returns)
        by_horizon[str(horizon)] = {
            "baseline_test": baseline_metrics,
            "stable_router_test": stable_metrics,
            "coverage_pct": round(100.0 * len(stable_returns) / max(1, len(baseline_returns)), 2),
            "avg_return_lift_pp": (
                round(float(stable_metrics["avg_return_pct"]) - float(baseline_metrics["avg_return_pct"]), 4)
                if stable_metrics.get("avg_return_pct") is not None and baseline_metrics.get("avg_return_pct") is not None else None
            ),
        }

    baseline_swing = _strategy_metrics(all_baseline)
    stable_swing = _strategy_metrics(all_stable)
    positive_horizons = 0
    beats_baseline_horizons = 0
    evaluable_horizons = 0
    for horizon in (2, 3, 5):
        block = by_horizon[str(horizon)]
        routed = block["stable_router_test"]
        baseline = block["baseline_test"]
        if int(routed.get("sample") or 0) >= 4:
            evaluable_horizons += 1
            if float(routed.get("avg_return_pct") or 0.0) > 0.0 and (
                routed.get("profit_factor") is None or float(routed.get("profit_factor") or 0.0) > 1.0
            ):
                positive_horizons += 1
            if routed.get("avg_return_pct") is not None and baseline.get("avg_return_pct") is not None and (
                float(routed["avg_return_pct"]) > float(baseline["avg_return_pct"])
            ):
                beats_baseline_horizons += 1

    coverage_pct = round(100.0 * len(all_stable) / max(1, len(all_baseline)), 2)
    prospective_candidate = bool(
        int(stable_swing.get("sample") or 0) >= 10
        and float(stable_swing.get("avg_return_pct") or 0.0) > 0.0
        and (stable_swing.get("profit_factor") is None or float(stable_swing.get("profit_factor") or 0.0) > 1.0)
        and positive_horizons >= 2
        and beats_baseline_horizons >= 2
        and coverage_pct >= 15.0
    )

    return {
        "mode": "SHADOW_RESEARCH_ONLY",
        "status": "EVALUABLE",
        "method": "40% frozen scorer train + 40% split into 3 sequential validation folds + 20% untouched final test",
        "train_dates": len(train_dates),
        "validation_dates": len(validation_dates),
        "test_dates": len(test_dates),
        "train_end_date": train_dates[-1] if train_dates else None,
        "test_start_date": test_dates[0] if test_dates else None,
        "folds": fold_summaries,
        "eligible_lane_count": len(eligible_lanes),
        "lane_stability": lane_stability,
        "by_horizon": by_horizon,
        "swing_2_5": {
            "baseline_test": baseline_swing,
            "stable_router_test": stable_swing,
            "coverage_pct": coverage_pct,
            "evaluable_test_horizons": evaluable_horizons,
            "positive_test_horizons": positive_horizons,
            "beats_baseline_horizons": beats_baseline_horizons,
            "prospective_candidate": prospective_candidate,
        },
        "policy": {
            "live_mutation": False,
            "auto_promote": False,
            "frozen_scorer": True,
            "lane_requires_multiple_positive_validation_windows": True,
            "final_test_never_selects_lanes": True,
            "historical_oot_is_diagnostic_not_prospective_proof": True,
        },
    }


def _compact_view_from_components(
    rows: list[dict],
    daily: dict,
    router: dict,
    stability: dict,
) -> dict:
    if not rows:
        return {"status": "INSUFFICIENT", "rows": 0}
    by_horizon: dict[str, dict] = {}
    for horizon in ("1", "2", "3", "5"):
        daily_block = (daily.get("by_horizon") or {}).get(horizon, {})
        router_block = (router.get("by_horizon") or {}).get(horizon, {})
        stability_block = (stability.get("by_horizon") or {}).get(horizon, {})
        by_horizon[horizon] = {
            "horizon_specific_test": daily_block.get("horizon_specific") or {},
            "router_baseline_test": router_block.get("baseline_test") or {},
            "router_routed_test": router_block.get("routed_test") or {},
            "router_coverage_pct": router_block.get("coverage_pct"),
            "router_avg_return_lift_pp": router_block.get("avg_return_lift_pp"),
            "router_profit_factor_lift": router_block.get("profit_factor_lift"),
            "stability_baseline_test": stability_block.get("baseline_test") or {},
            "stability_router_test": stability_block.get("stable_router_test") or {},
            "stability_coverage_pct": stability_block.get("coverage_pct"),
            "stability_avg_return_lift_pp": stability_block.get("avg_return_lift_pp"),
        }
    eligible_horizons = []
    for horizon in ("1", "2", "3", "5"):
        routed = by_horizon[horizon]["router_routed_test"]
        if (
            int(routed.get("sample") or 0) >= 4
            and float(routed.get("avg_return_pct") or 0.0) > 0.0
            and (routed.get("profit_factor") is None or float(routed.get("profit_factor") or 0.0) > 1.0)
        ):
            eligible_horizons.append(int(horizon))
    return {
        "status": router.get("status") or daily.get("status"),
        "rows": len(rows),
        "dates": len({
            str(row.get("observation_date") or row.get("snapshot_date") or "")[:10]
            for row in rows
            if row.get("observation_date") or row.get("snapshot_date")
        }),
        "daily_pair_swing_2_5": daily.get("swing_2_5") or {},
        "router_swing_2_5": router.get("swing_2_5") or {},
        "router_eligible_lane_count": router.get("eligible_lane_count"),
        "router_lane_validation": list(router.get("lane_validation") or [])[:40],
        "stability_swing_2_5": stability.get("swing_2_5") or {},
        "stability_eligible_lane_count": stability.get("eligible_lane_count"),
        "stability_lanes": list(stability.get("lane_stability") or [])[:40],
        "positive_routed_horizons": eligible_horizons,
        "by_horizon": by_horizon,
    }


def _compact_view_result(rows: list[dict]) -> dict:
    if not rows:
        return {"status": "INSUFFICIENT", "rows": 0}
    return _compact_view_from_components(
        rows,
        horizon_specific_daily_pair_challenger(rows),
        regime_router_daily_pair_challenger(rows),
        rolling_regime_stability_challenger(rows),
    )


def full_history_swing_lab(
    rows: list[dict],
    *,
    precomputed_earliest: dict | None = None,
) -> dict:
    """Compare point-in-time views on the complete mature feature history.

    This separates three different product questions without changing LIVE:
    - earliest_observation: how early could TMAD have found the setup?
    - canonical_eod: what would an explicit end-of-day Bishop-like list do?
    - latest_observation: latest available point-in-time snapshot for the day,
      useful when a canonical close is absent.

    Every view is evaluated with the same historical OOT challengers. Future
    outcome labels are evaluation only and never enter the same-date ranking.
    """
    mature = [row for row in rows if _is_mature_outcome(row)]
    canonical = [row for row in mature if int(row.get("is_canonical_daily_close") or 0) == 1]
    views = {
        "earliest_observation": mature,
        "canonical_eod": canonical,
        "latest_observation": _latest_feature_rows(mature),
    }
    results: dict[str, dict] = {}
    for name, view_rows in views.items():
        if name == "earliest_observation" and precomputed_earliest is not None:
            results[name] = dict(precomputed_earliest)
            results[name]["reused_precomputed_components"] = True
        else:
            results[name] = _compact_view_result(view_rows)
    evaluable = [
        (name, result)
        for name, result in results.items()
        if result.get("status") == "EVALUABLE"
    ]
    return {
        "mode": "SHADOW_RESEARCH_ONLY",
        "status": "EVALUABLE" if evaluable else "INSUFFICIENT",
        "coverage": _feature_history_coverage(mature),
        "views": results,
        "research_questions": {
            "earliest_vs_eod": "Does waiting for the canonical close improve 2-5D expectancy/PF enough to justify an EOD product?",
            "latest_vs_canonical": "Can the latest point-in-time snapshot substitute when no canonical close exists?",
            "horizon_selection": "Which of 1/2/3/5D remains positive after nested regime routing on the final OOT test?",
        },
        "policy": {
            "live_mutation": False,
            "auto_promote": False,
            "all_mature_rows_requested": True,
            "canonical_eod_is_point_in_time": True,
            "latest_observation_is_point_in_time": True,
            "future_outcomes_are_labels_only": True,
            "historical_results_require_new_unseen_dates_before_live_promotion": True,
        },
    }


def mover_generation_stability(conn: sqlite3.Connection) -> dict:
    if not _table_exists(conn, "mover_discovery_runs"):
        return {"status": "UNAVAILABLE", "live_mutation": False}
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT * FROM mover_discovery_runs
           WHERE model_version_id IS NOT NULL
           ORDER BY model_version_id"""
    ).fetchall()]
    parsed: list[dict] = []
    for row in rows:
        summary = _json(row.get("summary_json"))
        wf = summary.get("walk_forward") or {}
        discovery = summary.get("discovery") or {}
        patterns = discovery.get("patterns") or {}

        def threshold(side: str, feature: str) -> float | None:
            for rule in patterns.get(side) or []:
                for condition in rule.get("conditions") or []:
                    if str(condition.get("feature") or "") == feature:
                        return _f(condition.get("threshold"))
            return None

        parsed.append({
            "model_version_id": int(row.get("model_version_id") or 0),
            "training_end_date": str(row.get("training_end_date") or ""),
            "sample": int(wf.get("sample") or row.get("oos_trades") or 0),
            "market_dates": int(wf.get("market_dates") or row.get("oos_dates") or 0),
            "avg_return_pct": _f(wf.get("avg_return")),
            "profit_factor": _f(wf.get("profit_factor")),
            "max_drawdown": _f(wf.get("max_drawdown")),
            "long_atr_threshold": threshold("long", "atr_pct"),
            "long_breadth_threshold": threshold("long", "market_breadth_score"),
            "long_momentum_5d_threshold": threshold("long", "momentum_5d_pct"),
            "short_atr_threshold": threshold("short", "atr_pct"),
        })
    parsed = [row for row in parsed if row["model_version_id"] > 0]
    if len(parsed) < 6:
        return {"status": "INSUFFICIENT", "models": len(parsed), "live_mutation": False}

    def _range(values: list[float]) -> dict:
        if not values:
            return {"sample": 0, "min": None, "max": None, "mean": None, "relative_range_pct": None}
        avg = mean(values)
        return {
            "sample": len(values),
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "mean": round(avg, 6),
            "relative_range_pct": round((max(values) - min(values)) / max(abs(avg), 1e-9) * 100.0, 3),
        }

    recent = parsed[-5:]
    prior = parsed[-10:-5] if len(parsed) >= 10 else parsed[:-5]
    def _econ(block: list[dict]) -> dict:
        avgs = [float(r["avg_return_pct"]) for r in block if r.get("avg_return_pct") is not None]
        pfs = [float(r["profit_factor"]) for r in block if r.get("profit_factor") is not None]
        dds = [float(r["max_drawdown"]) for r in block if r.get("max_drawdown") is not None]
        return {
            "models": len(block),
            "avg_oos_return_pct": round(mean(avgs), 4) if avgs else None,
            "avg_profit_factor": round(mean(pfs), 4) if pfs else None,
            "avg_max_drawdown": round(mean(dds), 4) if dds else None,
        }

    keys = ("long_atr_threshold", "long_breadth_threshold", "long_momentum_5d_threshold")
    stability = {
        key: _range([float(r[key]) for r in parsed[-15:] if r.get(key) is not None])
        for key in keys
    }
    prior_econ, recent_econ = _econ(prior), _econ(recent)
    deterioration = (
        prior_econ.get("avg_oos_return_pct") is not None
        and recent_econ.get("avg_oos_return_pct") is not None
        and float(recent_econ["avg_oos_return_pct"]) < float(prior_econ["avg_oos_return_pct"]) - 0.75
    )
    core_rule_stable = all(
        (item.get("relative_range_pct") is None or float(item["relative_range_pct"]) <= 25.0)
        for item in stability.values()
    )
    return {
        "status": "EVALUABLE",
        "models": len(parsed),
        "latest_model": parsed[-1],
        "prior_5_models": prior_econ,
        "recent_5_models": recent_econ,
        "recent_15_rule_stability": stability,
        "diagnosis": "OUTCOME_DRIFT_WITH_STABLE_RULES" if deterioration and core_rule_stable else "NO_CLEAR_STRUCTURAL_DRIFT",
        "interpretation": (
            "OOS economics deteriorated while the main discovery thresholds remained comparatively stable; prioritize regime/direction segmentation over threshold chasing."
            if deterioration and core_rule_stable else
            "No strong evidence that stable rules alone explain recent outcome changes."
        ),
        "recent_models": parsed[-10:],
        "policy": {"live_mutation": False, "threshold_auto_tune": False},
    }


def horizon_conditioned_discovery_challenger(discovery: dict) -> dict:
    """Turn the existing OOT discovery panels into a next-window SHADOW hypothesis.

    This does NOT reuse the fitted rule as proof of edge. The same OOT window that
    reveals the horizon split is only used to define a frozen hypothesis for the
    next unseen window. No LIVE ranking, gates, sizing or execution are changed.
    """
    by_horizon = discovery.get("by_horizon") or {}
    rows: list[dict] = []
    swing_edges: list[float] = []
    positive_swing = 0
    evaluable_swing = 0

    for horizon in (1, 2, 3, 5):
        panel = by_horizon.get(str(horizon)) or {}
        sample = int(panel.get("sample") or 0)
        primary = _f((panel.get("primary") or {}).get("out_of_time_directional_edge_pct"))
        inverse = _f((panel.get("inverse") or {}).get("out_of_time_directional_edge_pct"))
        evaluable = sample >= 30 and primary is not None and inverse is not None

        if evaluable and inverse > primary:
            lane = "INVERSE"
            edge = inverse
        elif evaluable:
            lane = "PRIMARY"
            edge = primary
        else:
            lane = "INSUFFICIENT"
            edge = None

        rows.append({
            "horizon_sessions": horizon,
            "sample": sample,
            "historical_preferred_lane": lane,
            "historical_edge_pct": round(edge, 4) if edge is not None else None,
            "primary_edge_pct": round(primary, 4) if primary is not None else None,
            "inverse_edge_pct": round(inverse, 4) if inverse is not None else None,
            "next_window_status": "FREEZE_AND_TEST" if evaluable else "ACCUMULATING",
        })

        if horizon in (2, 3, 5) and evaluable:
            evaluable_swing += 1
            swing_edges.append(float(edge))
            if float(edge) > 0:
                positive_swing += 1

    preferred = {int(row["horizon_sessions"]): str(row["historical_preferred_lane"]) for row in rows}
    swing_consistent = (
        evaluable_swing == 3
        and positive_swing == 3
        and all(preferred.get(h) == "INVERSE" for h in (2, 3, 5))
    )
    one_day_primary = preferred.get(1) == "PRIMARY"
    return {
        "mode": "SHADOW_RESEARCH_ONLY",
        "status": "TEST_IN_NEXT_OOS_WINDOW" if swing_consistent else "ACCUMULATING",
        "hypothesis": (
            "PRIMARY_1D__INVERSE_2_3_5D"
            if swing_consistent and one_day_primary
            else "HORIZON_CONDITIONED_DISCOVERY"
        ),
        "by_horizon": rows,
        "swing_2_5": {
            "evaluable_horizons": evaluable_swing,
            "positive_horizons": positive_swing,
            "all_prefer_inverse": swing_consistent,
            "mean_historical_preferred_edge_pct": round(mean(swing_edges), 4) if swing_edges else None,
        },
        "one_day_prefers_primary": one_day_primary,
        "freeze_rule": "Freeze this horizon routing now and evaluate only on later unseen dates before any promotion.",
        "policy": {
            "live_mutation": False,
            "auto_promote": False,
            "historical_window_defines_hypothesis_not_validation": True,
        },
    }


def adaptive_model_comparison(conn: sqlite3.Connection) -> dict:
    """Compare the current champion with eligible historical challengers.

    This is an observability guard: more dates do not beat the champion unless
    risk-adjusted utility and safety also improve. It never changes model_state.
    """
    if not _table_exists(conn, "adaptive_model_metrics"):
        return {"status": "UNAVAILABLE", "decision": "KEEP_CHAMPION", "live_mutation": False}

    latest_row = conn.execute("SELECT MAX(learning_run_id) FROM adaptive_model_metrics").fetchone()
    latest_run = int((latest_row[0] if latest_row else 0) or 0)
    if not latest_run:
        return {"status": "UNAVAILABLE", "decision": "KEEP_CHAMPION", "live_mutation": False}

    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT * FROM adaptive_model_metrics
           WHERE learning_run_id=? ORDER BY utility DESC,id DESC""",
        (latest_run,),
    ).fetchall()]

    def _view(row: dict | None) -> dict | None:
        if not row:
            return None
        return {
            "model_version_id": int(row.get("model_version_id") or 0),
            "model_key": row.get("model_key"),
            "status": row.get("model_status"),
            "role": row.get("role"),
            "sample": int(row.get("sample") or 0),
            "market_dates": int(row.get("market_dates") or 0),
            "hit_rate": _f(row.get("hit_rate")),
            "avg_return": _f(row.get("avg_return")),
            "profit_factor": _f(row.get("profit_factor")),
            "max_drawdown": _f(row.get("max_drawdown")),
            "utility": _f(row.get("utility")),
            "safety_pass": bool(int(row.get("safety_pass") or 0)),
        }

    champion_row = next(
        (r for r in rows if str(r.get("role") or "").upper() == "GLOBAL_CHAMPION"),
        next((r for r in rows if str(r.get("model_status") or "").lower() in {"champion", "provisional_champion"}), None),
    )
    candidate_rows = [
        r for r in rows
        if str(r.get("model_status") or "").lower() in {"challenger", "discovery_shadow"}
        and (not champion_row or int(r.get("model_version_id") or 0) != int(champion_row.get("model_version_id") or 0))
    ]
    safe_rows = [r for r in candidate_rows if int(r.get("safety_pass") or 0) == 1]
    best_safe = max(safe_rows, key=lambda r: float(r.get("utility") or -1e9), default=None)
    best_broad = max(candidate_rows, key=lambda r: float(r.get("utility") or -1e9), default=None)

    champion = _view(champion_row)
    safe = _view(best_safe)
    broad = _view(best_broad)
    decision = "KEEP_CHAMPION"
    reason = "no_eligible_safe_challenger"
    if champion and safe:
        champion_utility = float(champion.get("utility") or -1e9)
        safe_utility = float(safe.get("utility") or -1e9)
        if safe_utility > champion_utility and bool(safe.get("safety_pass")):
            decision = "CHALLENGER_DESERVES_PROSPECTIVE_SHADOW"
            reason = "safe_challenger_utility_above_champion"
        else:
            reason = "champion_risk_adjusted_utility_not_beaten"

    return {
        "status": "EVALUABLE" if champion else "UNAVAILABLE",
        "learning_run_id": latest_run,
        "decision": decision,
        "reason": reason,
        "champion": champion,
        "best_safe_eligible_challenger": safe,
        "best_eligible_challenger_any_safety": broad,
        "policy": {
            "live_mutation": False,
            "more_history_alone_is_not_promotion_evidence": True,
            "prospective_confirmation_required": True,
        },
    }


def fusion_softening_assessment(lanes: dict) -> dict:
    soft = lanes.get("soft_fusion_timing_ready") or {}
    sample = int(soft.get("sample") or 0)
    avg_return = _f(soft.get("avg_return_pct"))
    pf = _f(soft.get("profit_factor"))
    adverse = sample >= 20 and (
        (avg_return is not None and avg_return <= 0.0)
        or (pf is not None and pf < 1.0)
    )
    return {
        "status": "REJECT_SOFTENING_FOR_NOW" if adverse else "ACCUMULATING",
        "sample": sample,
        "avg_return_pct": avg_return,
        "profit_factor": pf,
        "reason": "mature_soft_lane_negative_expectancy" if adverse else "insufficient_or_non_negative_evidence",
        "policy": {"live_mutation": False, "fusion_logic_unchanged": True},
    }


def _load_attribution_inputs(conn: sqlite3.Connection) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    audit = _rows(conn, "SELECT * FROM market_outcome_audit_events ORDER BY id") if _table_exists(conn, "market_outcome_audit_events") else []
    if _table_exists(conn, "layer_signal_outcomes"):
        if _table_exists(conn, "layer_signal_history"):
            signals = _rows(
                conn,
                f"""SELECT o.*,h.target_pct
                    FROM layer_signal_outcomes o
                    LEFT JOIN layer_signal_history h ON h.id=o.layer_signal_id
                    WHERE o.close_return_pct IS NOT NULL
                    ORDER BY o.layer_signal_id DESC {_limit_clause(SIGNAL_SAMPLE_LIMIT)}""",
            )
        else:
            signals = _rows(
                conn,
                f"SELECT * FROM layer_signal_outcomes WHERE close_return_pct IS NOT NULL "
                f"ORDER BY layer_signal_id DESC {_limit_clause(SIGNAL_SAMPLE_LIMIT)}",
            )
    else:
        signals = []
    signals = [normalize_signal_outcome_row(row) for row in signals]

    # v6.1.99: economic lane attribution must not go blind when mature signal
    # history has already moved to immutable archives.  The canonical historical
    # helper deduplicates archive + live evidence per date/ticker/layer/direction.
    joined: list[dict] = []
    try:
        from historical_evidence import canonical_signal_outcomes
        joined = list(canonical_signal_outcomes())
    except Exception:
        joined = []

    if not joined and _table_exists(conn, "layer_signal_outcomes") and _table_exists(conn, "layer_signal_history"):
        joined = _rows(conn, f"""
            SELECT o.*,h.layer,h.signal_origin,h.score AS layer_score,h.state,h.actionable,
                   h.opportunity_state,h.payload_json
            FROM layer_signal_outcomes o
            JOIN layer_signal_history h ON h.id=o.layer_signal_id
            WHERE o.close_return_pct IS NOT NULL
            ORDER BY o.layer_signal_id DESC
            {_limit_clause(SIGNAL_SAMPLE_LIMIT)}
        """)
    if joined:
        signals.extend(joined)

    feature_rows: list[dict] = []
    if _table_exists(conn, "observation_outcomes") and _table_exists(conn, "feature_observations"):
        feature_rows = _rows(conn, f"""
            SELECT o.observation_id,o.ticker,o.observation_date,o.horizon_sessions,
                   o.entry_price,o.first_session_date,o.exit_date,o.exit_price,
                   o.return_long_pct,o.mfe_long_pct,o.mae_long_pct,o.complete,
                   f.features_json,f.snapshot_date,f.observation_layer,f.is_canonical_daily_close,
                   f.market_regime,f.observed_at_madrid,f.source_scanner_run_id,f.session_type
            FROM observation_outcomes o
            JOIN feature_observations f ON f.id=o.observation_id
            WHERE o.return_long_pct IS NOT NULL
              AND o.complete = 1
              AND o.horizon_sessions IN (1,2,3,5)
            ORDER BY o.observation_id DESC
            {_limit_clause(FEATURE_SAMPLE_LIMIT)}
        """)
    return audit, signals, feature_rows, joined


def _option_quote_fields(expression: dict) -> dict:
    aliases = {
        "bid": ("bid", "option_bid"),
        "ask": ("ask", "option_ask"),
        "mid": ("mid", "mark", "option_mid"),
        "iv": ("iv", "implied_volatility", "impliedVolatility"),
        "delta": ("delta", "option_delta"),
    }
    captured: dict[str, Any] = {}
    for target, names in aliases.items():
        for name in names:
            value = expression.get(name)
            if value not in (None, ""):
                captured[target] = value
                break
    return captured


def option_mark_capture_status(snapshot: dict) -> dict:
    selected = list(snapshot.get("selected") or []) if isinstance(snapshot, dict) else []
    contracts = 0
    with_marks = 0
    fields: set[str] = set()
    for row in selected:
        expression = _json(row.get("option_expression"))
        if expression.get("instrument") and expression.get("strike") is not None and expression.get("expiry"):
            contracts += 1
        quote = _option_quote_fields(expression)
        if quote:
            with_marks += 1
            fields.update(quote.keys())
    coverage = round(100.0 * with_marks / contracts, 2) if contracts else 0.0
    return {
        "status": "CAPTURING" if with_marks else "WAITING_FOR_MARK_SOURCE",
        "contracts_referenced": contracts,
        "contracts_with_quote_fields": with_marks,
        "quote_coverage_pct": coverage,
        "captured_fields": sorted(fields),
        "required_upstream_fields": ["bid", "ask", "mid", "iv", "delta"],
        "note": "Contract references are journaled now. Real option PnL starts only when an upstream source supplies actual marks; no synthetic quote is invented.",
    }


def _journal_eod_selection(output_dir: Path) -> dict:
    try:
        raw = get_runtime_state("alpha_lab_eod_options_snapshot", "") or ""
        snapshot = json.loads(raw) if raw else {}
    except Exception:
        snapshot = {}
    if not snapshot or str(snapshot.get("status") or "ok").lower() not in {"ok", "ready"}:
        return {"status": "no_eod_snapshot", "mark_capture": option_mark_capture_status(snapshot)}
    selected = list(snapshot.get("selected") or [])
    if not selected:
        return {"status": "empty_eod_snapshot", "mark_capture": option_mark_capture_status(snapshot)}

    path = research_input_path(output_dir / "eod_selection_journal.jsonl")
    existing: set[str] = set()
    if research_snapshot_active():
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        existing.add(str(json.loads(line).get("key") or ""))
            except Exception:
                existing = set()
        return {
            "status": "frozen_replay_no_journal_mutation",
            "added": 0,
            "total_keys": len(existing),
            "path": str(path),
            "mark_capture": option_mark_capture_status(snapshot),
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "eod_selection_journal.jsonl"
    existing: set[str] = set()
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                existing.add(str(json.loads(line).get("key") or ""))
        except Exception:
            existing = set()
    added = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in selected:
            expression = _json(row.get("option_expression"))
            key = "|".join((str(snapshot.get("asof") or ""), str(row.get("ticker") or "").upper(), str(row.get("direction") or "").upper()))
            if not key.strip("|") or key in existing:
                continue
            quote = _option_quote_fields(expression)
            item = {
                "key": key,
                "asof": snapshot.get("asof"),
                "source_scanner_run_id": snapshot.get("source_scanner_run_id"),
                "ticker": row.get("ticker"),
                "direction": row.get("direction"),
                "spot": row.get("spot"),
                "underlying_alpha_score": row.get("underlying_alpha_score", row.get("conviction")),
                "tradeability_score": row.get("tradeability_score"),
                "option_expression": expression,
                "volatility_proxy_input": row.get("volatility_proxy_input") or {"source": "UNAVAILABLE", "daily_pct": None},
                "contract_reference": {
                    "instrument": expression.get("instrument"),
                    "strike": expression.get("strike"),
                    "expiry": expression.get("expiry"),
                    "dte": expression.get("dte"),
                },
                "quote_snapshot": quote,
                "option_pnl_status": "PENDING_MARK_SERIES" if quote else "UNAVAILABLE_NO_CHAIN_MARKS",
                "underlying_pnl_status": "PENDING_OUTCOME",
                "journaled_at_madrid": ts_madrid_str(),
            }
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            existing.add(key)
            added += 1
    return {
        "status": "journaled",
        "added": added,
        "total_keys": len(existing),
        "path": str(path),
        "mark_capture": option_mark_capture_status(snapshot),
    }


def _cached_snapshot() -> dict | None:
    try:
        raw = get_runtime_state("alpha_pnl_attribution_snapshot", "") or ""
        payload = json.loads(raw) if raw else {}
        if not payload:
            return None

        current_version = str(getattr(config, "BOT_VERSION", "") or "")
        producer_version = str(payload.get("producer_version") or "")
        current_basis = _attribution_cache_basis()
        previous_basis = _json(get_runtime_state("alpha_pnl_attribution_basis", "") or "")

        # Strong cache: source fingerprint + all attribution input watermarks.
        # This deliberately survives a BOT_VERSION bump when attribution logic
        # and its data are byte-for-byte / watermark-equivalent.
        if previous_basis and previous_basis == current_basis:
            payload = dict(payload)
            payload["runtime_cached"] = True
            payload["cache_mode"] = "BASIS_REUSE"
            payload["cache_source_producer_version"] = producer_version
            if current_version:
                payload["producer_version"] = current_version
                set_runtime_state("alpha_pnl_attribution_producer_version", current_version)
            return payload

        # Short TTL fallback retained for repeated calls inside the same version.
        last_epoch = float(get_runtime_state("alpha_pnl_attribution_last_epoch", "0") or 0)
        if time.time() - last_epoch >= MIN_RECOMPUTE_SECONDS:
            return None
        if not current_version or producer_version != current_version:
            return None
        payload = dict(payload)
        payload["runtime_cached"] = True
        payload["cache_mode"] = "TTL_REUSE"
        return payload
    except Exception:
        pass
    return None


def compact_economic_summary(payload: dict) -> dict:
    """Persist a bounded, parseable economic snapshot for external QA.

    The full attribution payload is intentionally rich and can exceed runtime-data
    string limits. This summary exposes only decision-relevant SHADOW metrics and
    never changes LIVE strategy, thresholds or execution.
    """
    audit = payload.get("audit_opportunity_cost") or {}
    fusion = payload.get("fusion_timing_challenger") or {}
    confirmed = payload.get("confirmed_signal_pnl_proxy") or {}
    lanes = payload.get("economic_lanes") or {}
    directional = payload.get("directional_specialist_challenger") or {}
    discovery = payload.get("scanner_discovery_challenger") or {}
    horizon_router = payload.get("horizon_conditioned_discovery_challenger") or {}
    daily_pair = payload.get("horizon_specific_daily_pair_challenger") or {}
    regime_horizon = payload.get("regime_horizon_daily_pair_challenger") or {}
    regime_router = payload.get("regime_router_daily_pair_challenger") or {}
    full_history_lab = payload.get("full_history_swing_lab") or {}
    stability_router = payload.get("rolling_regime_stability_challenger") or {}
    model_comparison = payload.get("adaptive_model_comparison") or {}
    generation_stability = payload.get("mover_generation_stability") or {}
    generation_change_point = payload.get("model_generation_change_point") or {}
    fusion_assessment = payload.get("fusion_softening_assessment") or {}
    eod = payload.get("eod_options_lane") or {}
    mark = eod.get("mark_capture") or {}
    synthetic = eod.get("synthetic_option_proxy") or {}
    top_features = []
    for row in list(payload.get("feature_usefulness") or [])[:8]:
        top_features.append({
            "feature": row.get("feature"),
            "sample": row.get("sample"),
            "confidence": row.get("confidence"),
            "directional_edge_pct": row.get("directional_edge_pct"),
        })
    return {
        "schema_version": 1,
        "producer_version": payload.get("producer_version"),
        "generated_at_madrid": payload.get("generated_at_madrid"),
        "mode": payload.get("mode"),
        "audit": {
            "economic_opportunities": audit.get("economic_opportunities"),
            "blockers": audit.get("blockers"),
        },
        "fusion_timing": {
            "sample_economic_opportunities": fusion.get("sample_economic_opportunities"),
            "timing_ready": fusion.get("timing_ready"),
            "fusion_ready": fusion.get("fusion_ready"),
            "timing_ready_but_fusion_blocked": fusion.get("timing_ready_but_fusion_blocked"),
        },
        "confirmed_signal_proxy": {
            key: confirmed.get(key) for key in (
                "sample", "days", "win_rate_pct", "avg_return_pct",
                "median_return_pct", "profit_factor",
                "max_drawdown_return_points", "avg_mfe_pct", "avg_mae_pct",
            )
        },
        "economic_lanes": {
            "mature_joined_signals": lanes.get("mature_joined_signals"),
            "strict_fusion": lanes.get("strict_fusion"),
            "soft_fusion_timing_ready": lanes.get("soft_fusion_timing_ready"),
            "timing_ready_but_strict_fusion_blocked": lanes.get("timing_ready_but_strict_fusion_blocked"),
            "soft_minus_strict_avg_return_pp": lanes.get("soft_minus_strict_avg_return_pp"),
            "soft_minus_strict_profit_factor": lanes.get("soft_minus_strict_profit_factor"),
        },
        "directional_specialist": {
            key: directional.get(key) for key in (
                "status", "sample", "dates", "train_sample", "test_sample",
                "test_flipped", "test_flip_rate_pct", "avg_return_lift_pp",
                "profit_factor_lift",
            )
        },
        "scanner_discovery": {
            **{
                key: discovery.get(key) for key in (
                    "status", "sample", "dates", "train_sample", "test_sample",
                    "split_date", "top_quartile_avg_future_return_pct",
                    "bottom_quartile_avg_future_return_pct",
                    "out_of_time_directional_edge_pct",
                )
            },
            "by_horizon": discovery.get("by_horizon") or {},
            "inverse_shadow_candidate": discovery.get("inverse_shadow_candidate") or {},
        },
        "horizon_conditioned_discovery": horizon_router,
        "historical_refinement": {
            "horizon_daily_status": daily_pair.get("status"),
            "swing_2_5": daily_pair.get("swing_2_5") or {},
            "regime_horizon_status": regime_horizon.get("status"),
            "regime_horizon_swing_2_5": regime_horizon.get("swing_2_5") or {},
            "eligible_regime_horizon_schemes": regime_horizon.get("eligible_regime_horizon_schemes"),
            "regime_router_status": regime_router.get("status"),
            "regime_router_swing_2_5": regime_router.get("swing_2_5") or {},
            "regime_router_eligible_lanes": regime_router.get("eligible_lane_count"),
            "full_history_coverage": full_history_lab.get("coverage") or {},
            "rolling_stability_status": stability_router.get("status"),
            "rolling_stability_swing_2_5": stability_router.get("swing_2_5") or {},
            "rolling_stability_eligible_lanes": stability_router.get("eligible_lane_count"),
            "full_history_views": {
                name: {
                    "status": block.get("status"),
                    "rows": block.get("rows"),
                    "dates": block.get("dates"),
                    "positive_routed_horizons": block.get("positive_routed_horizons") or [],
                    "router_swing_2_5": block.get("router_swing_2_5") or {},
                }
                for name, block in (full_history_lab.get("views") or {}).items()
            },
            "model_drift_diagnosis": generation_stability.get("diagnosis"),
            "prior_5_models": generation_stability.get("prior_5_models") or {},
            "recent_5_models": generation_stability.get("recent_5_models") or {},
            "change_point_diagnosis": generation_change_point.get("diagnosis"),
            "best_historical_break": generation_change_point.get("best_historical_break") or {},
            "median_main_threshold_shift_pct": generation_change_point.get("median_main_threshold_shift_pct"),
        },
        "adaptive_model_comparison": model_comparison,
        "fusion_softening_assessment": fusion_assessment,
        "eod_options": {
            "option_pnl_status": eod.get("option_pnl_status"),
            "option_outcome_bridge": eod.get("option_outcome_bridge") or {},
            "real_option_pnl_status": eod.get("real_option_pnl_status", eod.get("option_pnl_status")),
            "contracts_referenced": mark.get("contracts_referenced"),
            "contracts_with_quote_fields": mark.get("contracts_with_quote_fields"),
            "quote_coverage_pct": mark.get("quote_coverage_pct"),
            "synthetic_option_proxy": {
                "status": synthetic.get("status"),
                "contracts_journaled": synthetic.get("contracts_journaled"),
                "evaluated_contract_horizons": synthetic.get("evaluated_contract_horizons"),
                "volatility_sources": synthetic.get("volatility_sources"),
                "skipped": synthetic.get("skipped") or {},
                "maturity_diagnostics": synthetic.get("maturity_diagnostics") or {},
                "by_horizon": synthetic.get("by_horizon") or {},
            },
        },
        "top_features": top_features,
        "policy": {
            "live_mutation": False,
            "auto_tune": False,
            "purpose": "external economic QA observability",
            "horizon_router_shadow_only": True,
            "champion_comparison_observability_only": True,
        },
    }


def run_alpha_pnl_attribution(progress_callback=None, research_benchmark: dict | None = None) -> dict:
    def _progress(component: str, status: str, elapsed_seconds: float | None = None) -> None:
        payload = {
            "component": component,
            "status": status,
            "updated_at_madrid": ts_madrid_str(),
        }
        if elapsed_seconds is not None:
            payload["elapsed_seconds"] = round(float(elapsed_seconds), 4)
        try:
            set_runtime_state("alpha_pnl_attribution_progress", json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
        print(
            f"[ATTRIBUTION] component={component} status={status}"
            + (f" elapsed={elapsed_seconds:.1f}s" if elapsed_seconds is not None else ""),
            flush=True,
        )
        if callable(progress_callback):
            try:
                progress_callback(component, status, elapsed_seconds)
            except Exception:
                pass

    cached = _cached_snapshot()
    if cached is not None:
        _progress("cached_snapshot", "reused", 0.0)
        return cached
    output_dir = config.OUTPUT_DIR / "alpha_pnl_attribution"
    eod_journal = _journal_eod_selection(output_dir)
    journal_path = research_input_path(output_dir / "eod_selection_journal.jsonl")

    bridge_keys = journal_contract_keys(journal_path)
    _progress("input_load", "running")
    conn = connect(research_db_path(config.ALERTS_DB))
    try:
        attribution_inputs_started = time.perf_counter()
        audit_rows, signal_rows, feature_rows, joined_rows = _load_attribution_inputs(conn)
        model_comparison = adaptive_model_comparison(conn)
        generation_stability = mover_generation_stability(conn)
        generation_change_point = model_generation_change_point(conn)
        attribution_input_load_seconds = round(time.perf_counter() - attribution_inputs_started, 4)
        _progress("input_load", "done", attribution_input_load_seconds)

        _progress("option_bridge_load", "running")
        option_bridge_started = time.perf_counter()
        targeted_option_rows = load_targeted_option_outcomes(conn, journal_path)
        option_bridge_load_seconds = round(time.perf_counter() - option_bridge_started, 4)
        _progress("option_bridge_load", "done", option_bridge_load_seconds)
    finally:
        conn.close()

    component_seconds: dict[str, float] = {}

    def _timed(name: str, fn):
        _progress(name, "running")
        started_component = time.perf_counter()
        try:
            value = fn()
        except Exception:
            elapsed = time.perf_counter() - started_component
            _progress(name, "error", elapsed)
            raise
        component_seconds[name] = round(time.perf_counter() - started_component, 4)
        _progress(name, "done", component_seconds[name])
        return value

    option_feature_rows = targeted_option_rows or feature_rows
    synthetic_option_proxy = _timed(
        "synthetic_option_proxy",
        lambda: evaluate_synthetic_option_proxy(journal_path, option_feature_rows),
    )
    lanes = _timed("economic_lane_analysis", lambda: economic_lane_analysis(joined_rows))
    discovery = _timed("scanner_discovery", lambda: scanner_discovery_challenger(feature_rows))
    audit_attribution = _timed("audit_stage_attribution", lambda: audit_stage_attribution(audit_rows))
    fusion_timing = _timed("fusion_timing_challenger", lambda: fusion_timing_challenger(audit_rows))
    confirmed_signal = _timed("signal_pnl_proxy", lambda: signal_pnl_proxy(signal_rows))
    directional = _timed("directional_specialist", lambda: directional_specialist_challenger(joined_rows))
    usefulness = _timed("feature_usefulness", lambda: feature_usefulness(feature_rows))
    horizon_conditioned = _timed(
        "horizon_conditioned_discovery",
        lambda: horizon_conditioned_discovery_challenger(discovery),
    )
    daily_pair = _timed(
        "horizon_specific_daily_pair",
        lambda: horizon_specific_daily_pair_challenger(feature_rows),
    )
    regime_horizon = _timed(
        "regime_horizon_daily_pair",
        lambda: regime_horizon_daily_pair_challenger(feature_rows),
    )
    regime_router = _timed(
        "regime_router_daily_pair",
        lambda: regime_router_daily_pair_challenger(feature_rows),
    )
    stability_router = _timed(
        "rolling_regime_stability",
        lambda: rolling_regime_stability_challenger(feature_rows),
    )
    earliest_compact = _compact_view_from_components(
        feature_rows,
        daily_pair,
        regime_router,
        stability_router,
    )
    full_history_lab = _timed(
        "full_history_swing_lab",
        lambda: full_history_swing_lab(
            feature_rows,
            precomputed_earliest=earliest_compact,
        ),
    )
    swing_v2 = _timed(
        "swing_v2",
        lambda: build_swing_v2_research(feature_rows),
    )
    momentum_reconciliation = _timed(
        "momentum_reconciliation",
        lambda: build_momentum_reconciliation(
            feature_rows,
            swing_v2,
            research_benchmark=research_benchmark,
        ),
    )

    payload = {
        "schema_version": 2,
        "economic_schema_version": 10,
        "producer_version": str(getattr(config, "BOT_VERSION", "") or "unknown"),
        "mode": "SHADOW_RESEARCH_ONLY",
        "generated_at_madrid": ts_madrid_str(),
        "objective": "measure economic value and opportunity cost across Scanner/pipeline layers before changing strategy",
        "audit_opportunity_cost": audit_attribution,
        "fusion_timing_challenger": fusion_timing,
        "confirmed_signal_pnl_proxy": confirmed_signal,
        "economic_lanes": lanes,
        "fusion_softening_assessment": fusion_softening_assessment(lanes),
        "directional_specialist_challenger": directional,
        "feature_usefulness": usefulness,
        "scanner_discovery_challenger": discovery,
        "horizon_conditioned_discovery_challenger": horizon_conditioned,
        "horizon_specific_daily_pair_challenger": daily_pair,
        "regime_horizon_daily_pair_challenger": regime_horizon,
        "regime_router_daily_pair_challenger": regime_router,
        "full_history_swing_lab": full_history_lab,
        "rolling_regime_stability_challenger": stability_router,
        "swing_v2": swing_v2,
        "momentum_reconciliation": momentum_reconciliation,
        "research_benchmark": compact_benchmark_summary(research_benchmark) if research_benchmark else {},
        "adaptive_model_comparison": model_comparison,
        "mover_generation_stability": generation_stability,
        "model_generation_change_point": generation_change_point,
        "eod_options_lane": {
            "journal": eod_journal,
            "option_pnl_status": (
                "PENDING_MARK_SERIES"
                if (eod_journal.get("mark_capture") or {}).get("contracts_with_quote_fields", 0) else "UNAVAILABLE_NO_CHAIN_MARKS"
            ),
            "real_option_pnl_status": (
                "PENDING_MARK_SERIES"
                if (eod_journal.get("mark_capture") or {}).get("contracts_with_quote_fields", 0) else "UNAVAILABLE_NO_CHAIN_MARKS"
            ),
            "synthetic_option_proxy": synthetic_option_proxy,
            "option_outcome_bridge": {
                "mode": "TARGETED_PRICE_CACHE_FUTURE_SESSIONS",
                "requested_contract_keys": len(bridge_keys),
                "requested_dates": len({day for day, _ticker in bridge_keys}),
                "targeted_rows": len(targeted_option_rows),
                "load_seconds": option_bridge_load_seconds,
                "attribution_input_load_seconds": attribution_input_load_seconds,
                "fallback_to_general_sample": not bool(targeted_option_rows),
            },
            "underlying_proxy_only": True,
            "mark_capture": eod_journal.get("mark_capture") or {},
        },
        "sampling": {
            "feature_row_limit": FEATURE_SAMPLE_LIMIT if FEATURE_SAMPLE_LIMIT > 0 else None,
            "signal_row_limit": SIGNAL_SAMPLE_LIMIT if SIGNAL_SAMPLE_LIMIT > 0 else None,
            "recompute_seconds": MIN_RECOMPUTE_SECONDS,
            "economic_outcomes_require_complete_when_available": True,
            "archived_joined_signal_bridge": True,
            "option_outcomes_loaded_from_price_cache_by_journal_key": True,
        },
        "performance": {
            "component_seconds": component_seconds,
            "attribution_input_load_seconds": attribution_input_load_seconds,
            "option_bridge_load_seconds": option_bridge_load_seconds,
            "earliest_full_history_components_reused": True,
            "rolling_fold_selections_cached": True,
        },
        "policy": {
            "live_mutation": False,
            "auto_tune": False,
            "challengers_shadow_only": True,
            "deduplicate_economic_opportunities": True,
            "do_not_count_snapshots_as_independent_trades": True,
            "do_not_count_pending_outcomes_as_finished_trades": True,
            "no_lookahead_for_challenger_feature_direction": True,
            "no_lookahead_for_directional_specialist": True,
            "fusion_softening_is_counterfactual_only": True,
            "never_invent_option_quotes": True,
            "synthetic_option_proxy_is_model_scenario_not_real_pnl": True,
            "horizon_router_is_next_window_shadow_hypothesis_only": True,
            "model_comparison_cannot_change_model_state": True,
            "fusion_softening_rejected_when_mature_lane_has_negative_expectancy": True,
            "horizon_specific_daily_pair_is_shadow_only": True,
            "model_generation_drift_is_observability_only": True,
            "change_point_is_diagnostic_only": True,
            "regime_horizon_daily_pair_is_shadow_only": True,
            "regime_router_daily_pair_is_shadow_only": True,
            "full_history_lab_is_shadow_only": True,
            "rolling_regime_stability_is_shadow_only": True,
            "swing_v2_is_research_only": True,
            "swing_v2_requires_prospective_shadow_before_promotion": True,
            "momentum_reconciliation_is_diagnostic_only": True,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "latest_alpha_pnl_attribution.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (output_dir / "latest_momentum_reconciliation.json").write_text(json.dumps(momentum_reconciliation, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (output_dir / "alpha_pnl_attribution_history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    set_runtime_state("alpha_pnl_attribution_snapshot", json.dumps(payload, ensure_ascii=False, default=str))
    summary = compact_economic_summary(payload)
    set_runtime_state("alpha_pnl_economic_summary", json.dumps(summary, ensure_ascii=False, default=str))

    daily_pair = payload.get("horizon_specific_daily_pair_challenger") or {}
    daily_pair_external = {
        "mode": daily_pair.get("mode"),
        "status": daily_pair.get("status"),
        "method": daily_pair.get("method"),
        "split_date": daily_pair.get("split_date"),
        "train_dates": daily_pair.get("train_dates"),
        "test_dates": daily_pair.get("test_dates"),
        "swing_2_5": daily_pair.get("swing_2_5") or {},
        "by_horizon": {
            str(h): {
                "test_rows": (daily_pair.get("by_horizon") or {}).get(str(h), {}).get("test_rows"),
                "pooled": (daily_pair.get("by_horizon") or {}).get(str(h), {}).get("pooled") or {},
                "horizon_specific": (daily_pair.get("by_horizon") or {}).get(str(h), {}).get("horizon_specific") or {},
                "feature_signs": {
                    feature: ((daily_pair.get("by_horizon") or {}).get(str(h), {}).get("learned_feature_signs") or {}).get(feature, {}).get("sign")
                    for feature in DISCOVERY_FEATURES
                },
            }
            for h in (1, 2, 3, 5)
        },
        "policy": daily_pair.get("policy") or {},
    }
    set_runtime_state("horizon_specific_swing_summary", json.dumps(daily_pair_external, ensure_ascii=False, default=str))

    regime_horizon = payload.get("regime_horizon_daily_pair_challenger") or {}
    regime_horizon_external = {
        "mode": regime_horizon.get("mode"),
        "status": regime_horizon.get("status"),
        "method": regime_horizon.get("method"),
        "split_date": regime_horizon.get("split_date"),
        "train_dates": regime_horizon.get("train_dates"),
        "test_dates": regime_horizon.get("test_dates"),
        "eligible_regime_horizon_schemes": regime_horizon.get("eligible_regime_horizon_schemes"),
        "swing_2_5": regime_horizon.get("swing_2_5") or {},
        "regime_support": list(regime_horizon.get("regime_support") or [])[:40],
        "by_horizon": {
            str(h): {
                "horizon_only": (regime_horizon.get("by_horizon") or {}).get(str(h), {}).get("horizon_only") or {},
                "regime_horizon": (regime_horizon.get("by_horizon") or {}).get(str(h), {}).get("regime_horizon") or {},
            }
            for h in (1, 2, 3, 5)
        },
        "policy": regime_horizon.get("policy") or {},
    }
    set_runtime_state("regime_horizon_swing_summary", json.dumps(regime_horizon_external, ensure_ascii=False, default=str))

    regime_router = payload.get("regime_router_daily_pair_challenger") or {}
    regime_router_external = {
        "mode": regime_router.get("mode"),
        "status": regime_router.get("status"),
        "method": regime_router.get("method"),
        "train_dates": regime_router.get("train_dates"),
        "validation_dates": regime_router.get("validation_dates"),
        "test_dates": regime_router.get("test_dates"),
        "train_end_date": regime_router.get("train_end_date"),
        "validation_start_date": regime_router.get("validation_start_date"),
        "validation_end_date": regime_router.get("validation_end_date"),
        "test_start_date": regime_router.get("test_start_date"),
        "lane_rule": regime_router.get("lane_rule") or {},
        "eligible_lane_count": regime_router.get("eligible_lane_count"),
        "lane_validation": list(regime_router.get("lane_validation") or [])[:40],
        "by_horizon": {
            str(h): (regime_router.get("by_horizon") or {}).get(str(h), {})
            for h in (1, 2, 3, 5)
        },
        "swing_2_5": regime_router.get("swing_2_5") or {},
        "policy": regime_router.get("policy") or {},
    }
    set_runtime_state("regime_router_swing_summary", json.dumps(regime_router_external, ensure_ascii=False, default=str))

    stability_router = payload.get("rolling_regime_stability_challenger") or {}
    stability_external = {
        "mode": stability_router.get("mode"),
        "status": stability_router.get("status"),
        "method": stability_router.get("method"),
        "train_dates": stability_router.get("train_dates"),
        "validation_dates": stability_router.get("validation_dates"),
        "test_dates": stability_router.get("test_dates"),
        "train_end_date": stability_router.get("train_end_date"),
        "test_start_date": stability_router.get("test_start_date"),
        "folds": stability_router.get("folds") or [],
        "eligible_lane_count": stability_router.get("eligible_lane_count"),
        "lane_stability": list(stability_router.get("lane_stability") or [])[:40],
        "by_horizon": stability_router.get("by_horizon") or {},
        "swing_2_5": stability_router.get("swing_2_5") or {},
        "policy": stability_router.get("policy") or {},
    }
    set_runtime_state("rolling_regime_stability_summary", json.dumps(stability_external, ensure_ascii=False, default=str))

    full_history_lab = payload.get("full_history_swing_lab") or {}
    compact_views = {}
    for name, block in (full_history_lab.get("views") or {}).items():
        compact_views[name] = {
            "status": block.get("status"),
            "rows": block.get("rows"),
            "dates": block.get("dates"),
            "positive_routed_horizons": block.get("positive_routed_horizons") or [],
            "daily_pair_swing_2_5": block.get("daily_pair_swing_2_5") or {},
            "router_swing_2_5": block.get("router_swing_2_5") or {},
            "router_eligible_lane_count": block.get("router_eligible_lane_count"),
            "by_horizon": {
                str(h): {
                    "router_baseline_test": ((block.get("by_horizon") or {}).get(str(h), {}) or {}).get("router_baseline_test") or {},
                    "router_routed_test": ((block.get("by_horizon") or {}).get(str(h), {}) or {}).get("router_routed_test") or {},
                    "router_coverage_pct": ((block.get("by_horizon") or {}).get(str(h), {}) or {}).get("router_coverage_pct"),
                    "router_avg_return_lift_pp": ((block.get("by_horizon") or {}).get(str(h), {}) or {}).get("router_avg_return_lift_pp"),
                }
                for h in (1, 2, 3, 5)
            },
        }
    full_history_external = {
        "mode": full_history_lab.get("mode"),
        "status": full_history_lab.get("status"),
        "coverage": full_history_lab.get("coverage") or {},
        "views": compact_views,
        "research_questions": full_history_lab.get("research_questions") or {},
        "policy": full_history_lab.get("policy") or {},
    }
    set_runtime_state("full_history_swing_lab_summary", json.dumps(full_history_external, ensure_ascii=False, default=str))

    stability = payload.get("mover_generation_stability") or {}
    stability_external = {
        "status": stability.get("status"),
        "models": stability.get("models"),
        "diagnosis": stability.get("diagnosis"),
        "interpretation": stability.get("interpretation"),
        "latest_model": stability.get("latest_model") or {},
        "prior_5_models": stability.get("prior_5_models") or {},
        "recent_5_models": stability.get("recent_5_models") or {},
        "recent_15_rule_stability": stability.get("recent_15_rule_stability") or {},
        "recent_models": list(stability.get("recent_models") or [])[-5:],
        "policy": stability.get("policy") or {},
    }
    set_runtime_state("mover_generation_stability_summary", json.dumps(stability_external, ensure_ascii=False, default=str))

    change_point = payload.get("model_generation_change_point") or {}
    change_point_external = {
        "status": change_point.get("status"),
        "models": change_point.get("models"),
        "window_models": change_point.get("window_models"),
        "diagnosis": change_point.get("diagnosis"),
        "best_historical_break": change_point.get("best_historical_break") or {},
        "threshold_shift_around_break": change_point.get("threshold_shift_around_break") or {},
        "median_main_threshold_shift_pct": change_point.get("median_main_threshold_shift_pct"),
        "latest_window": change_point.get("latest_window") or {},
        "pre_break_reference_window": change_point.get("pre_break_reference_window") or {},
        "policy": change_point.get("policy") or {},
    }
    set_runtime_state("model_generation_change_point_summary", json.dumps(change_point_external, ensure_ascii=False, default=str))

    set_runtime_state("alpha_pnl_attribution_status", "ok")
    set_runtime_state("alpha_pnl_attribution_producer_version", payload.get("producer_version") or "unknown")
    set_runtime_state(
        "alpha_pnl_attribution_basis",
        json.dumps(_attribution_cache_basis(), ensure_ascii=False, default=str),
    )
    set_runtime_state("alpha_pnl_attribution_last_epoch", str(time.time()))
    return payload
