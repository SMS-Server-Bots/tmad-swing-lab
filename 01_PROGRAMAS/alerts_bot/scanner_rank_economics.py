from __future__ import annotations

"""SHADOW-only economic attribution for Scanner cross-sectional priority.

The observer answers one narrow question: does higher Scanner-native rank
concentrate better future directional returns?  It never changes Scanner
selection, CORE/FUSION thresholds, LIVE entries, sizing, stops or targets.
"""

import hashlib
import json
import math
import time
from pathlib import Path
from statistics import mean, median
from typing import Any

import config
from db import connect, get_runtime_state, set_runtime_state
from discord_utils import send_learning_audit
from time_utils import ts_madrid_str

HORIZONS = (1, 2, 3, 5)
CUMULATIVE_CUTOFFS = (10, 25, 50, 100)
EXCLUSIVE_BINS = ((1, 10), (11, 25), (26, 50), (51, 100), (101, 10**9))
MIN_RECOMPUTE_SECONDS = 30 * 60
QUERY_LIMIT = 50000

# Scanner-native inputs only.  GEX, FUSION and future outcomes are deliberately
# excluded from ranking so the attribution remains a clean discovery test.
DIRECTIONAL_FEATURES = (
    "long_score",
    "scanner_trend_score",
    "momentum_score",
    "change_percent",
    "flow_direction",
)
INTENSITY_FEATURES = ("rvol",)


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
        return parsed
    except Exception:
        return default


def _table_exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _features(row: dict) -> dict:
    payload = _json(row.get("payload_json"), {})
    nested = payload.get("features") if isinstance(payload, dict) else None
    if isinstance(nested, dict):
        return nested
    if isinstance(payload, dict) and payload:
        return payload
    direct = _json(row.get("features_json"), {})
    return direct if isinstance(direct, dict) else {}


def _future_sessions(row: dict) -> list[dict]:
    raw = _json(row.get("sessions_json"), [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _directional_close_return(entry: float, sessions: list[dict], horizon: int, direction: str) -> float | None:
    if entry <= 0 or len(sessions) < horizon:
        return None
    close = _f(sessions[horizon - 1].get("close"))
    if close in (None, 0):
        return None
    if direction == "long":
        return (close / entry - 1.0) * 100.0
    return (entry / close - 1.0) * 100.0


def _mfe_mae(entry: float, sessions: list[dict], horizon: int, direction: str) -> tuple[float | None, float | None]:
    if entry <= 0 or len(sessions) < horizon:
        return None, None
    favorable: list[float] = []
    adverse: list[float] = []
    for session in sessions[:horizon]:
        high = _f(session.get("high") or session.get("close"))
        low = _f(session.get("low") or session.get("close"))
        if high in (None, 0) or low in (None, 0):
            continue
        if direction == "long":
            favorable.append((high / entry - 1.0) * 100.0)
            adverse.append((low / entry - 1.0) * 100.0)
        else:
            favorable.append((entry / low - 1.0) * 100.0)
            adverse.append((entry / high - 1.0) * 100.0)
    return (max(favorable) if favorable else None, min(adverse) if adverse else None)


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    ranked = sorted(values.items(), key=lambda item: (item[1], item[0]), reverse=True)
    n = len(ranked)
    if not n:
        return {}
    if n == 1:
        return {ranked[0][0]: 100.0}
    return {key: 100.0 * (1.0 - idx / (n - 1)) for idx, (key, _value) in enumerate(ranked)}


def _scanner_rank_rows(rows: list[dict], direction: str) -> list[dict]:
    """Rank one Scanner run using only contemporaneous Scanner-native evidence."""
    prepared: list[dict] = []
    for row in rows:
        feat = _features(row)
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        values: dict[str, float] = {}
        for feature in DIRECTIONAL_FEATURES:
            value = _f(feat.get(feature))
            if value is None:
                continue
            # long_score is centred around 50; all other directional inputs are
            # already signed.  Negating them creates the SHORT mirror without
            # learning weights from future labels.
            if feature == "long_score":
                values[feature] = value if direction == "long" else 100.0 - value
            else:
                values[feature] = value if direction == "long" else -value
        for feature in INTENSITY_FEATURES:
            value = _f(feat.get(feature))
            if value is not None:
                values[feature] = value
        if len(values) < 3:
            continue
        prepared.append({**row, "_ticker": ticker, "_rank_inputs": values})

    feature_pcts: dict[str, dict[str, float]] = {}
    all_features = sorted({name for row in prepared for name in row["_rank_inputs"]})
    for feature in all_features:
        vals = {row["_ticker"]: row["_rank_inputs"][feature] for row in prepared if feature in row["_rank_inputs"]}
        feature_pcts[feature] = _percentiles(vals)

    scored: list[dict] = []
    for row in prepared:
        ticker = row["_ticker"]
        pcts = [feature_pcts[name][ticker] for name in row["_rank_inputs"] if ticker in feature_pcts.get(name, {})]
        if not pcts:
            continue
        scored.append({**row, "scanner_economic_score": mean(pcts)})
    scored.sort(key=lambda row: (float(row["scanner_economic_score"]), row["_ticker"]), reverse=True)
    for index, row in enumerate(scored, start=1):
        row["scanner_economic_rank"] = index
    return scored


def _metrics(rows: list[dict]) -> dict:
    returns = [x for x in (_f(row.get("directional_return_pct")) for row in rows) if x is not None]
    mfe = [x for x in (_f(row.get("mfe_pct")) for row in rows) if x is not None]
    mae = [x for x in (_f(row.get("mae_pct")) for row in rows) if x is not None]
    positives = [x for x in returns if x > 0]
    negatives = [x for x in returns if x < 0]
    return {
        "sample": len(returns),
        "win_rate_pct": round(100.0 * len(positives) / len(returns), 2) if returns else None,
        "avg_return_pct": round(mean(returns), 4) if returns else None,
        "median_return_pct": round(median(returns), 4) if returns else None,
        "profit_factor": round(sum(positives) / abs(sum(negatives)), 4) if negatives and sum(negatives) else None,
        "avg_mfe_pct": round(mean(mfe), 4) if mfe else None,
        "avg_mae_pct": round(mean(mae), 4) if mae else None,
    }


def _directional_metrics(row: dict, horizon: int, direction: str) -> tuple[float | None, float | None, float | None]:
    """Return close/MFE/MAE for either legacy session paths or direct mature outcomes."""
    direct_horizon = int(row.get("horizon_sessions") or 0)
    direct_return = _f(row.get("return_long_pct"))
    if direct_horizon == horizon and direct_return is not None:
        entry = _f(row.get("entry_price"))
        exit_price = _f(row.get("exit_price"))
        if direction == "long":
            return direct_return, _f(row.get("mfe_long_pct")), _f(row.get("mae_long_pct"))
        if entry not in (None, 0) and exit_price not in (None, 0):
            close_return = (entry / exit_price - 1.0) * 100.0
        else:
            close_return = -direct_return
        long_mfe = _f(row.get("mfe_long_pct"))
        long_mae = _f(row.get("mae_long_pct"))
        short_mfe = None
        short_mae = None
        if long_mae is not None and 1.0 + long_mae / 100.0 > 0:
            short_mfe = (1.0 / (1.0 + long_mae / 100.0) - 1.0) * 100.0
        if long_mfe is not None and 1.0 + long_mfe / 100.0 > 0:
            short_mae = (1.0 / (1.0 + long_mfe / 100.0) - 1.0) * 100.0
        return close_return, short_mfe, short_mae

    entry = _f(row.get("entry_price")) or 0.0
    sessions = _future_sessions(row)
    return (
        _directional_close_return(entry, sessions, horizon, direction),
        *_mfe_mae(entry, sessions, horizon, direction),
    )


def analyze_ranked_rows(rows: list[dict]) -> dict:
    """Pure analyzer for full Scanner cross-sections and legacy layer archives.

    v6.1.99 accepts canonical daily feature observations joined directly to
    observation_outcomes.  That removes the old one-observation-per-run blind
    spot while retaining the legacy sessions_json path for backwards safety.
    """
    base: dict[tuple[int, str], dict] = {}
    direct_outcomes: dict[tuple[int, str, int], dict] = {}

    for row in rows:
        if int(row.get("complete") or 0) != 1:
            continue
        run_id = int(row.get("source_scanner_run_id") or 0)
        ticker = str(row.get("ticker") or "").upper()
        if run_id == 0 or not ticker:
            continue

        horizon = int(row.get("horizon_sessions") or 0)
        is_direct = horizon in HORIZONS and _f(row.get("return_long_pct")) is not None
        sessions = _future_sessions(row)
        if not is_direct and len(sessions) < max(HORIZONS):
            continue

        key = (run_id, ticker)
        feature_id = int(row.get("layer_feature_id") or row.get("observation_id") or row.get("id") or 10**18)
        old = base.get(key)
        old_id = int((old or {}).get("layer_feature_id") or (old or {}).get("observation_id") or (old or {}).get("id") or 10**18)
        if old is None or feature_id < old_id:
            base[key] = row

        if is_direct:
            okey = (run_id, ticker, horizon)
            previous = direct_outcomes.get(okey)
            previous_id = int((previous or {}).get("observation_id") or (previous or {}).get("id") or 10**18)
            if previous is None or feature_id < previous_id:
                direct_outcomes[okey] = row

    by_run: dict[int, list[dict]] = {}
    for (run_id, _ticker), row in base.items():
        by_run.setdefault(run_id, []).append(row)

    run_sizes = sorted(len(run_rows) for run_rows in by_run.values())
    single_observation_runs = sum(1 for size in run_sizes if size == 1)
    runs_with_top10_comparison = sum(1 for size in run_sizes if size > 10)
    median_run_size = float(median(run_sizes)) if run_sizes else 0.0
    max_run_size = max(run_sizes) if run_sizes else 0
    cross_section_ready = bool(runs_with_top10_comparison > 0)

    result: dict[str, Any] = {
        "mode": "SHADOW_RESEARCH_ONLY",
        "method": "fixed equal-weight cross-sectional percentile rank from Scanner-native contemporaneous fields; canonical FULL-DEEP daily observations preferred; no future label enters ranking",
        "rank_inputs": list(DIRECTIONAL_FEATURES + INTENSITY_FEATURES),
        "mature_feature_observations": len(base),
        "direct_outcome_rows": len(direct_outcomes),
        "scanner_runs": len(by_run),
        "run_min": min(by_run) if by_run else None,
        "run_max": max(by_run) if by_run else None,
        "cross_section_quality": {
            "ready": cross_section_ready,
            "median_observations_per_run": median_run_size,
            "max_observations_per_run": max_run_size,
            "single_observation_runs": single_observation_runs,
            "runs_with_more_than_10_observations": runs_with_top10_comparison,
            "reason": (
                "usable_cross_section_present"
                if cross_section_ready
                else "no run contains >10 mature observations; Top10-vs-rest attribution is not identifiable"
            ),
        },
        "horizons": {},
        "policy": {
            "live_mutation": False,
            "scanner_selection_mutation": False,
            "threshold_mutation": False,
            "mature_outcomes_only": True,
            "full_deep_direct_outcomes_are_labels_only": True,
        },
    }

    for horizon in HORIZONS:
        horizon_result: dict[str, Any] = {}
        for direction in ("long", "short"):
            evaluated: list[dict] = []
            for run_id, run_rows in by_run.items():
                ranked = _scanner_rank_rows(run_rows, direction)
                for row in ranked:
                    ticker = row["_ticker"]
                    source = direct_outcomes.get((run_id, ticker, horizon)) or row
                    ret, mfe, mae = _directional_metrics(source, horizon, direction)
                    if ret is None:
                        continue
                    evaluated.append({
                        "ticker": ticker,
                        "source_scanner_run_id": run_id,
                        "rank": int(row["scanner_economic_rank"]),
                        "score": round(float(row["scanner_economic_score"]), 4),
                        "directional_return_pct": ret,
                        "mfe_pct": mfe,
                        "mae_pct": mae,
                    })

            cumulative = {}
            for cutoff in CUMULATIVE_CUTOFFS:
                cumulative[f"top_{cutoff}"] = _metrics([row for row in evaluated if row["rank"] <= cutoff])
            rest_100 = _metrics([row for row in evaluated if row["rank"] > 100])
            exclusive = {}
            ordered_bin_avgs: list[float] = []
            for start, end in EXCLUSIVE_BINS:
                key = f"rank_{start}_{end if end < 10**9 else 'plus'}"
                metrics = _metrics([row for row in evaluated if start <= row["rank"] <= end])
                exclusive[key] = metrics
                if metrics.get("sample", 0) >= 5 and metrics.get("avg_return_pct") is not None:
                    ordered_bin_avgs.append(float(metrics["avg_return_pct"]))
            monotonic_pairs = sum(1 for a, b in zip(ordered_bin_avgs, ordered_bin_avgs[1:]) if a >= b)
            comparable_pairs = max(0, len(ordered_bin_avgs) - 1)
            top10 = cumulative["top_10"]
            rest10 = _metrics([row for row in evaluated if row["rank"] > 10])
            horizon_result[direction] = {
                "sample": len(evaluated),
                "cumulative": cumulative,
                "rank_gt_100": rest_100,
                "exclusive_bins": exclusive,
                "top10_vs_rest_avg_return_edge_pp": (
                    round(float(top10["avg_return_pct"]) - float(rest10["avg_return_pct"]), 4)
                    if top10.get("avg_return_pct") is not None and rest10.get("avg_return_pct") is not None else None
                ),
                "top10_vs_rest_median_return_edge_pp": (
                    round(float(top10["median_return_pct"]) - float(rest10["median_return_pct"]), 4)
                    if top10.get("median_return_pct") is not None and rest10.get("median_return_pct") is not None else None
                ),
                "monotonic_pairs": monotonic_pairs,
                "comparable_pairs": comparable_pairs,
                "monotonicity_pct": round(100.0 * monotonic_pairs / comparable_pairs, 2) if comparable_pairs else None,
            }
        result["horizons"][str(horizon)] = horizon_result
    return result


def _load_rows() -> list[dict]:
    """Load mature Scanner economics from FULL-DEEP canonical observations + layer archives."""
    from layer_archive import archived_feature_rows_with_outcomes

    rows = list(archived_feature_rows_with_outcomes())
    conn = connect(config.ALERTS_DB)
    try:
        conn.row_factory = __import__("sqlite3").Row

        # Legacy/layer evidence remains available for continuity.
        if _table_exists(conn, "layer_feature_history") and _table_exists(conn, "layer_feature_outcomes"):
            sql = """
                SELECT h.id AS layer_feature_id,h.source_scanner_run_id,h.ticker,h.payload_json,
                       o.signal_date,o.entry_price,o.horizon_sessions,o.sessions_json,o.complete
                FROM layer_feature_history h
                JOIN layer_feature_outcomes o ON o.layer_feature_id=h.id
                WHERE o.complete=1
                ORDER BY h.id DESC
                LIMIT ?
            """
            rows.extend(dict(row) for row in conn.execute(sql, (QUERY_LIMIT,)).fetchall())

        # v6.1.99: canonical daily Scanner observations provide the actual
        # cross-section (hundreds/~1000 names per deep date).  Mature outcomes
        # are labels only and never feed the ranking inputs.
        if _table_exists(conn, "feature_observations") and _table_exists(conn, "observation_outcomes"):
            sql = """
                SELECT f.id AS observation_id,f.source_scanner_run_id,f.ticker,f.features_json,
                       f.snapshot_date AS signal_date,f.observation_layer,f.is_canonical_daily_close,
                       o.horizon_sessions,o.entry_price,o.exit_date,o.exit_price,o.return_long_pct,
                       o.mfe_long_pct,o.mae_long_pct,o.complete
                FROM feature_observations f
                JOIN observation_outcomes o ON o.observation_id=f.id
                WHERE o.complete=1
                  AND o.horizon_sessions IN (1,2,3,5)
                  AND f.observation_layer='daily_close'
                  AND f.is_canonical_daily_close=1
                ORDER BY f.id DESC,o.horizon_sessions
                LIMIT ?
            """
            rows.extend(dict(row) for row in conn.execute(sql, (QUERY_LIMIT,)).fetchall())
    finally:
        conn.close()

    # Archive/live overlap and repeated materialisation must not manufacture
    # statistical sample. Direct outcomes retain one row per horizon.
    dedup: dict[tuple[int, str, str, int], dict] = {}
    for row in rows:
        run_id = int(row.get("source_scanner_run_id") or 0)
        ticker = str(row.get("ticker") or "").upper()
        day = str(row.get("signal_date") or row.get("snapshot_date") or "")[:10]
        is_direct = _f(row.get("return_long_pct")) is not None
        horizon = int(row.get("horizon_sessions") or 0) if is_direct else 0
        if run_id == 0 or not ticker or not day:
            continue
        if not is_direct and not row.get("sessions_json"):
            continue
        key = (run_id, ticker, day, horizon)
        old = dedup.get(key)
        row_id = int(row.get("observation_id") or row.get("layer_feature_id") or row.get("id") or 0)
        old_id = int((old or {}).get("observation_id") or (old or {}).get("layer_feature_id") or (old or {}).get("id") or 0)
        if old is None or row_id >= old_id:
            normalized = dict(row)
            if normalized.get("entry_price") in (None, ""):
                normalized["entry_price"] = normalized.get("outcome_entry_price")
            normalized["signal_date"] = day
            dedup[key] = normalized

    values = list(dedup.values())
    values.sort(key=lambda row: (
        str(row.get("signal_date") or ""),
        int(row.get("source_scanner_run_id") or 0),
        str(row.get("ticker") or ""),
        int(row.get("horizon_sessions") or 0),
    ))
    return values[-QUERY_LIMIT:]


def _fmt(value: Any, decimals: int = 2) -> str:
    number = _f(value)
    return "n/a" if number is None else f"{number:.{decimals}f}"


def _discord_summary(snapshot: dict) -> str:
    lines = [
        "## 🧪 SCANNER RANK ECONOMICS · SHADOW",
        "**Objetivo:** comprobar si mayor prioridad Scanner concentra mejor retorno futuro. **No modifica LIVE ni los 180 seleccionados.**",
        f"**Cobertura:** `{snapshot.get('mature_feature_observations', 0)}` observaciones maduras · `{snapshot.get('scanner_runs', 0)}` runs · Scanner `#{snapshot.get('run_min') or 'n/a'}→#{snapshot.get('run_max') or 'n/a'}`",
        (
            f"**Calidad cross-section:** `{(snapshot.get('cross_section_quality') or {}).get('ready')}` · "
            f"mediana `{(snapshot.get('cross_section_quality') or {}).get('median_observations_per_run')}` obs/run · "
            f"máx `{(snapshot.get('cross_section_quality') or {}).get('max_observations_per_run')}`."
        ),
        "**Ranking:** percentiles cross-sectional fijos de `long_score / trend / momentum / change / flow / RVOL`; el outcome nunca entra en el rank.",
    ]
    for horizon in (1, 3, 5):
        block = snapshot.get("horizons", {}).get(str(horizon), {})
        for direction in ("long", "short"):
            item = block.get(direction) or {}
            top = ((item.get("cumulative") or {}).get("top_10") or {})
            if not top.get("sample"):
                continue
            lines.append(
                f"• **{horizon}s {direction.upper()}** · Top10 `n={top.get('sample')}` · win `{_fmt(top.get('win_rate_pct'),1)}%` · "
                f"avg `{_fmt(top.get('avg_return_pct'))}%` · med `{_fmt(top.get('median_return_pct'))}%` · PF `{_fmt(top.get('profit_factor'))}` · "
                f"edge vs resto `{_fmt(item.get('top10_vs_rest_avg_return_edge_pp'))} pp` · monotonía `{_fmt(item.get('monotonicity_pct'),1)}%`"
            )
    lines.extend([
        "**Lectura:** usar `edge vs resto` + monotonía + tamaño muestral; una cifra aislada no promueve reglas.",
        "**Trazabilidad:** `layer_feature_history → layer_feature_outcomes complete=1 → Scanner run → rank → retorno 1/2/3/5 sesiones`.",
        "`SHADOW_RESEARCH_ONLY · live_mutation=false · threshold_mutation=false`",
    ])
    return "\n".join(lines)


def run_scanner_rank_economics(*, force: bool = False, send_discord: bool = True) -> dict:
    now_epoch = time.time()
    last_epoch = _f(get_runtime_state("scanner_rank_economics_last_epoch", "0")) or 0.0
    if not force and now_epoch - last_epoch < MIN_RECOMPUTE_SECONDS:
        raw = get_runtime_state("scanner_rank_economics_snapshot", "") or ""
        cached = _json(raw, {})
        return cached if isinstance(cached, dict) and cached else {"status": "deferred"}

    rows = _load_rows()
    snapshot = analyze_ranked_rows(rows)
    snapshot["schema_version"] = 1
    snapshot["generated_at_madrid"] = ts_madrid_str()
    fingerprint_payload = {
        "mature": snapshot.get("mature_feature_observations"),
        "runs": snapshot.get("scanner_runs"),
        "run_max": snapshot.get("run_max"),
        "horizons": snapshot.get("horizons"),
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    snapshot["fingerprint"] = fingerprint
    quality = snapshot.get("cross_section_quality") or {}
    if not snapshot.get("mature_feature_observations"):
        snapshot["status"] = "accumulating"
    elif not bool(quality.get("ready")):
        snapshot["status"] = "insufficient_cross_section"
    else:
        snapshot["status"] = "ok"

    output_dir = Path(config.OUTPUT_DIR) / "alpha_lab"
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "scanner_rank_economics_latest.json"
    history = output_dir / "scanner_rank_economics_history.jsonl"
    latest.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")

    previous_fingerprint = str(get_runtime_state("scanner_rank_economics_fingerprint", "") or "")
    set_runtime_state("scanner_rank_economics_last_epoch", str(now_epoch))
    set_runtime_state("scanner_rank_economics_status", str(snapshot["status"]))
    set_runtime_state("scanner_rank_economics_sample", str(snapshot.get("mature_feature_observations") or 0))
    quality = snapshot.get("cross_section_quality") or {}
    set_runtime_state("scanner_rank_economics_cross_section_ready", "1" if quality.get("ready") else "0")
    set_runtime_state("scanner_rank_economics_median_run_size", str(quality.get("median_observations_per_run") or 0))
    set_runtime_state("scanner_rank_economics_fingerprint", fingerprint)
    set_runtime_state("scanner_rank_economics_snapshot", json.dumps(snapshot, ensure_ascii=False, default=str))

    if (
        send_discord
        and fingerprint != previous_fingerprint
        and snapshot.get("mature_feature_observations")
        and snapshot.get("status") == "ok"
    ):
        send_learning_audit(_discord_summary(snapshot))
    return snapshot
