from __future__ import annotations

"""Read-only forensic study of the intraday Scanner observations omitted by
the representative-run operational counterfactual.

NOT a full exchange market universe, NOT a backtest, and NOT a prospective
entry signal. The live research DB is queried readonly after the replay; its
watermark may differ from the frozen operational T0 snapshot.
"""

from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from db import connect
from research_streaming_io import atomic_stream_json

MODE = "SCANNER_INTRADAY_OBSERVED_COVERAGE_V1"
MAX_DAILY_ROWS = 100


def _f(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        n = float(value)
        return n if n == n and abs(n) != float("inf") else None
    except (ValueError, TypeError, OverflowError):
        return None


def _i(value) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError, OverflowError):
        return 0


def _choose_representative_run(stats: dict[int, int]) -> int | None:
    """Exactly reproduce the existing rolling counterfactual selection."""
    if not stats:
        return None
    max_rows = max(stats.values())
    minimum = max(4, (max_rows + 1) // 2)
    eligible = [run for run, count in stats.items() if run > 0 and count >= minimum]
    return max(eligible) if eligible else None


def _label(row: dict) -> str:
    """Per-observation H5 excursion, never share outcomes across entry times."""
    mfe = _f(row.get("mfe_long_pct"))
    mae = _f(row.get("mae_long_pct"))
    if not bool(row.get("complete_5d")) or (mfe is None and mae is None):
        return "UNLABELLED"
    if mfe is None or mae is None:
        return "PARTIAL_LABEL"
    long = mfe >= 5.0
    short = mae <= -5.0
    if long and short:
        return "BOTH_SIDES_HIT"
    if long:
        return "LONG_HIT"
    if short:
        return "SHORT_HIT"
    return "NO_5PCT_MOVER"


def _cohort(rows: list[dict], name: str) -> dict:
    labelled = Counter(_label(row) for row in rows)
    return {
        "name": name,
        "observations": len(rows),
        "ticker_dates": len({(str(row.get("date")), str(row.get("ticker"))) for row in rows}),
        "observations_by_5d_label": dict(labelled),
        "valid_complete_directional_excursion_labels": sum(
            labelled.get(key, 0) for key in
            ("BOTH_SIDES_HIT", "LONG_HIT", "SHORT_HIT", "NO_5PCT_MOVER")
        ),
        "observations_mover_5d": sum(
            labelled.get(key, 0) for key in ("BOTH_SIDES_HIT", "LONG_HIT", "SHORT_HIT")
        ),
        "observations_missing_exact_time": sum(
            not bool(str(row.get("observed_at_madrid") or "").strip()) for row in rows
        ),
        "daily_close_or_canonical": sum(
            str(row.get("layer") or "").lower() == "daily_close"
            or bool(row.get("is_canonical_daily_close")) for row in rows
        ),
    }


def audit_rows(rows: list[dict], *, generated_at: str = "", watermark: dict | None = None, run_stats: dict[str, dict[int, int]] | None = None) -> dict:
    """Pure deterministic calculation, easy to test without production DB."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        day = str(row.get("date") or "")[:10]
        ticker = str(row.get("ticker") or "").upper().strip()
        run_id = _i(row.get("run_id"))
        if not day or not ticker or run_id <= 0:
            continue
        by_day[day].append({**row, "date": day, "ticker": ticker, "run_id": run_id})

    representative = []
    excluded = []
    excluded_unique = []
    repeated_across_runs = Counter()
    selected_runs = []
    date_details = []
    days_without_representative = 0
    for day, day_rows in sorted(by_day.items()):
        # Include empty tickers and run_id=0 in the selection denominator.
        # The existing counterfactual uses full per-run COUNT(*).
        run_sizes = Counter((run_stats or {}).get(day) or Counter(int(row["run_id"]) for row in day_rows))
        winner = _choose_representative_run(run_sizes)
        if winner is None:
            days_without_representative += 1
        chosen = [row for row in day_rows if row["run_id"] == winner] if winner is not None else []
        other = [row for row in day_rows if row["run_id"] != winner]
        chosen_tickers = {row["ticker"] for row in chosen}
        other_tickers = {row["ticker"] for row in other}
        seen_by_ticker = defaultdict(set)
        for row in day_rows:
            seen_by_ticker[row["ticker"]].add(row["run_id"])
        unique = [row for row in other if row["ticker"] not in chosen_tickers]
        representative.extend(chosen)
        excluded.extend(other)
        excluded_unique.extend(unique)
        for ticker, run_set in seen_by_ticker.items():
            if len(run_set) > 1:
                repeated_across_runs[day] += 1
        selected_runs.append((day, winner))
        date_details.append({
            "date": day,
            "total_runs": len(run_sizes),
            "representative_run_id": winner,
            "representative_observations": len(chosen),
            "other_observations": len(other),
            "unique_tickers_in_representative": len(chosen_tickers),
            "unique_tickers_across_all_runs": len(set(seen_by_ticker)),
            "exclusive_tickers_from_other_runs": len(other_tickers - chosen_tickers),
            "repeated_tickers_across_runs": repeated_across_runs[day],
            "max_cross_section_size": max(run_sizes.values()),
            "run_count_distribution": dict(Counter(run_sizes.values())),
        })
    all_kept = representative + excluded
    all_ticker_dates = {(row["date"], row["ticker"]) for row in all_kept}
    representative_ticker_dates = {(row["date"], row["ticker"]) for row in representative}
    exclusive_ticker_dates = all_ticker_dates - representative_ticker_dates
    unique_excluded_labelled = {
        (row["date"], row["ticker"]) for row in excluded_unique
        if _label(row) not in {"UNLABELLED", "PARTIAL_LABEL"}
    }
    unique_excluded_movers = {
        (row["date"], row["ticker"]) for row in excluded_unique
        if _label(row) in {"LONG_HIT", "SHORT_HIT", "BOTH_SIDES_HIT"}
    }
    return {
        "mode": MODE,
        "generated_at_madrid": generated_at,
        "input_origin": "READONLY_LIVE_RESEARCH_DB_MATURE_H5_DATES",
        "not_frozen_operational_snapshot": True,
        "not_full_raw_scanner_archive_or_market_universe": True,
        "no_live_mutation": True,
        "no_entry_promotion": True,
        "outcome_type": "PER_OBSERVATION_5D_EXCURSION_NOT_EXECUTABLE_RETURN",
        "selection_rule": "latest positive run with >=4 observations and >=50% daily maximum",
        "watermark": watermark or {},
        "dates": len(date_details),
        "dates_with_representative_run": len(date_details) - days_without_representative,
        "dates_without_representative_run": days_without_representative,
        "total_observed_ticker_dates": len(all_ticker_dates),
        "representative_ticker_dates": len(representative_ticker_dates),
        "exclusive_ticker_dates_in_other_runs": len(exclusive_ticker_dates),
        "fraction_ticker_dates_absent_from_representative_pct": (
            round(100.0 * len(exclusive_ticker_dates) / len(all_ticker_dates), 3)
            if all_ticker_dates else None
        ),
        "unique_excluded_ticker_dates_with_complete_h5_labels": len(unique_excluded_labelled),
        "unique_excluded_ticker_dates_with_h5_5pct_move": len(unique_excluded_movers),
        "unique_excluded_ticker_dates_without_complete_h5_labels": (
            len(exclusive_ticker_dates) - len(unique_excluded_labelled)
        ),
        "cohorts": {
            "all_observed": _cohort(all_kept, "ALL_OBSERVED_RUNS"),
            "representative_run": _cohort(representative, "SELECTED_OPERATIONAL_RUN"),
            "other_runs": _cohort(excluded, "OTHER_RUN_OBSERVATIONS"),
            "exclusive_other_runs": _cohort(excluded_unique, "EXCLUSIVE_OTHER_RUN_OBSERVATIONS"),
        },
        "daily": date_details[-MAX_DAILY_ROWS:],
        "daily_details_truncated": len(date_details) > MAX_DAILY_ROWS,
        "scientific_blockers": [
            "LIVE_DB_WATERMARK_MAY_DIFFER_FROM_FROZEN_T0",
            "H5_OUTCOME_SPARSE_AND_ENTRY_TIME_SPECIFIC",
            "MARKET_OUTSIDE_OBSERVED_FEATURE_DB_UNKNOWN",
            "ONE_TICKER_SEEN_MULTIPLE_RUNS_IS_NOT_MULTIPLE_INDEPENDENT_MOVERS",
            "GEX_AND_SECTOR_CONTEXT_NOT_PROVEN_POINT_IN_TIME_FOR_ALL_OBSERVATIONS",
            "NOT_AN_EXECUTABLE_LONG_SHORT_BACKTEST",
        ],
    }


def load_observed_rows() -> tuple[list[dict], dict, dict[str, dict[int, int]]]:
    """One aggregate label per observation, no read/write side effects."""
    conn = connect(config.ALERTS_DB, readonly=True, live=True)
    try:
        conn.row_factory = __import__("sqlite3").Row
        # This is intentionally the full observed feature store, not the
        # compact immutable replay snapshot (which omits some intraday runs).
        sql = """
        SELECT f.id, f.snapshot_date AS date, f.source_scanner_run_id AS run_id,
               UPPER(TRIM(f.ticker)) AS ticker, f.observed_at_madrid,
               f.observation_layer AS layer, f.is_canonical_daily_close,
               CASE WHEN o.observation_id IS NOT NULL THEN 1 ELSE 0 END AS complete_5d,
               o.mfe_long_pct, o.mae_long_pct
        FROM feature_observations f
        LEFT JOIN (
            SELECT observation_id, MAX(id) AS latest_id
            FROM observation_outcomes
            WHERE complete=1 AND horizon_sessions=5
            GROUP BY observation_id
        ) h5 ON h5.observation_id=f.id
        LEFT JOIN observation_outcomes o ON o.id=h5.latest_id
        WHERE f.snapshot_date IN (
            SELECT DISTINCT observation_date FROM observation_outcomes
            WHERE complete=1 AND horizon_sessions=5
        )
          AND COALESCE(f.source_scanner_run_id,0)>0
          AND TRIM(COALESCE(f.ticker,''))<>''
        ORDER BY f.snapshot_date, f.source_scanner_run_id, f.id
        """
        run_sql = """
        SELECT f.snapshot_date AS date,
               COALESCE(f.source_scanner_run_id,0) AS run_id,
               COUNT(*) AS n
        FROM feature_observations f
        WHERE f.snapshot_date IN (
            SELECT DISTINCT observation_date FROM observation_outcomes
            WHERE complete=1 AND horizon_sessions=5
        )
        GROUP BY f.snapshot_date, f.source_scanner_run_id
        """
        run_stats: dict[str, dict[int, int]] = defaultdict(dict)
        for raw in conn.execute(run_sql):
            run_stats[str(raw["date"])[:10]][_i(raw["run_id"])] = _i(raw["n"])
        rows = [dict(row) for row in conn.execute(sql)]
        watermark = {
            "rows_loaded": len(rows),
            "max_observation_id": max((_i(r.get("id")) for r in rows), default=0),
            "date_first": min((str(r["date"]) for r in rows), default=""),
            "date_last": max((str(r["date"]) for r in rows), default=""),
        }
        return rows, watermark, dict(run_stats)
    finally:
        conn.close()


def _quick_input_watermark() -> dict:
    conn = connect(config.ALERTS_DB, readonly=True, live=True)
    try:
        features = conn.execute(
            "SELECT COUNT(*),COALESCE(MAX(id),0) FROM feature_observations"
        ).fetchone()
        outcomes = conn.execute(
            "SELECT COUNT(*),COALESCE(MAX(id),0) FROM observation_outcomes "
            "WHERE complete=1 AND horizon_sessions=5"
        ).fetchone()
        return {
            "feature_rows": int(features[0] or 0),
            "feature_max_id": int(features[1] or 0),
            "mature_h5_rows": int(outcomes[0] or 0),
            "mature_h5_max_id": int(outcomes[1] or 0),
        }
    finally:
        conn.close()


def run(*, write: bool = True) -> dict:
    rows, watermark, run_stats = load_observed_rows()
    result = audit_rows(
        rows,
        generated_at=datetime.now(ZoneInfo("Europe/Madrid")).isoformat(),
        watermark=watermark,
        run_stats=run_stats,
    )
    result["source_code_sha256"] = sha256(Path(__file__).read_bytes()).hexdigest()
    result["input_watermark"] = _quick_input_watermark()
    if write:
        path = Path(config.OUTPUT_DIR) / "historical_validation" / "latest_scanner_intraday_coverage_audit.json"
        atomic_stream_json(path, result)
    return result


def run_if_stale() -> dict:
    # No full SQL scan if source code + append-only input signatures
    # match the last report. Known in-place label repairs require a forced audit.
    path = Path(config.OUTPUT_DIR) / "historical_validation" / "latest_scanner_intraday_coverage_audit.json"
    code_sha = sha256(Path(__file__).read_bytes()).hexdigest()
    watermark = _quick_input_watermark()
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8-sig"))
            if (previous.get("source_code_sha256") == code_sha
                    and previous.get("input_watermark") == watermark):
                return {"status": "REUSED_READONLY_SCANNER_AUDIT", "no_live_mutation": True}
        except (OSError, ValueError, TypeError):
            pass
    result = run()
    return {
        "status": "RECOMPUTED_READONLY_SCANNER_AUDIT",
        "exclusive_ticker_dates_in_other_runs": result["exclusive_ticker_dates_in_other_runs"],
        "unique_excluded_ticker_dates_with_h5_5pct_move": result["unique_excluded_ticker_dates_with_h5_5pct_move"],
        "no_live_mutation": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, default=str))
