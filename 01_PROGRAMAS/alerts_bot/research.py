from __future__ import annotations

import csv
import json
import shutil
from datetime import timedelta
from pathlib import Path

import config
from db import connect, get_active_model
from discord_utils import log_info
from time_utils import now_madrid, ts_utc_str


def _week_bounds():
    today = now_madrid().date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}", monday.isoformat(), sunday.isoformat()


def _export_query(conn, path: Path, sql: str, params: tuple = ()) -> int:
    rows = conn.execute(sql, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows([dict(row) for row in rows])
    return len(rows)


def export_research_tables():
    week_key, week_start, week_end = _week_bounds()
    out_dir = config.OUTPUT_DIR / week_key
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(config.ALERTS_DB)
    counts = {
        "feature_observations": _export_query(
            conn, out_dir / "feature_observations.csv",
            "SELECT * FROM feature_observations WHERE snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date,ticker",
            (week_start, week_end),
        ),
        "observation_outcomes": _export_query(
            conn, out_dir / "observation_outcomes.csv",
            "SELECT * FROM observation_outcomes WHERE observation_date BETWEEN ? AND ? ORDER BY observation_date,ticker,horizon_sessions",
            (week_start, week_end),
        ),
        "alert_events": _export_query(
            conn, out_dir / "alert_events.csv",
            "SELECT * FROM alert_events WHERE snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date,score DESC",
            (week_start, week_end),
        ),
        "open_trades": _export_query(
            conn, out_dir / "trade_lifecycle.csv",
            "SELECT * FROM open_trades WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date,ticker",
            (week_start, week_end),
        ),
        "model_versions": _export_query(
            conn, out_dir / "model_versions.csv",
            "SELECT * FROM model_versions ORDER BY id",
        ),
        "tuning_runs": _export_query(
            conn, out_dir / "tuning_runs.csv",
            "SELECT * FROM tuning_runs ORDER BY id",
        ),
    }
    conn.close()
    active = get_active_model()
    manifest = {
        "generated_at_utc": ts_utc_str(),
        "week": week_key,
        "week_start": week_start,
        "week_end": week_end,
        "counts": counts,
        "active_model": {
            "id": active.get("id"),
            "model_key": active.get("model_key"),
            "status": active.get("status"),
            "effective_from_date": active.get("effective_from_date"),
        },
        "training_window_days": config.TRAINING_WINDOW_DAYS,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = shutil.make_archive(str(config.OUTPUT_DIR / f"tmad_adaptive_{week_key}"), "zip", out_dir)
    log_info(f"✅ Adaptive research export {week_key} | counts={counts} | zip={zip_path}", send_discord=False)
    return [str(path) for path in sorted(out_dir.iterdir())] + [zip_path]
