from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from zipfile import ZipFile

import config
from db import archive_imported, get_asset_relationships, insert_feature_observation, mark_archive_imported
from discord_utils import log_error, log_info
from relational_features import attach_relational_features, build_relational_context
from rules import build_features, feature_observation


def intraday_archives() -> list[Path]:
    if not config.DATA_INTRADAY_ARCHIVE_DIR.exists():
        return []
    return sorted(config.DATA_INTRADAY_ARCHIVE_DIR.glob("*/intraday_????-??-??.zip"))


def intraday_archive_status() -> dict:
    paths = intraday_archives()
    dates = sorted({path.stem.replace("intraday_", "") for path in paths})
    ready = len(dates) >= config.INTRADAY_CALIBRATION_MIN_ARCHIVED_SESSIONS
    return {
        "available_sessions": len(dates),
        "required_sessions": config.INTRADAY_CALIBRATION_MIN_ARCHIVED_SESSIONS,
        "ready": ready,
        "start_date": dates[0] if dates else None,
        "end_date": dates[-1] if dates else None,
        "archives": len(paths),
    }


def _member(archive: ZipFile, prefix: str) -> str | None:
    return next((name for name in archive.namelist() if Path(name).name.startswith(prefix) and name.endswith(".sqlite")), None)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _load_archive(path: Path) -> tuple[list[dict], dict[str, list[dict]], dict[tuple[str, str], dict[str, dict]]]:
    with tempfile.TemporaryDirectory(prefix="tmad_intraday_import_") as tmp:
        root = Path(tmp)
        with ZipFile(path) as archive:
            scanner_member = _member(archive, "scanner_intraday_")
            gex_member = _member(archive, "gex_intraday_")
            if not scanner_member:
                return [], {}, {}
            scanner_db = root / "scanner.sqlite"
            scanner_db.write_bytes(archive.read(scanner_member))
            gex_db = root / "gex.sqlite"
            if gex_member:
                gex_db.write_bytes(archive.read(gex_member))

        scanner = sqlite3.connect(scanner_db)
        scanner.row_factory = sqlite3.Row
        try:
            scanner_rows = _rows(
                scanner,
                """SELECT s.* FROM scanner_snapshots s
                   JOIN scanner_runs r ON r.id=s.scanner_run_id
                   WHERE r.status='success' AND LOWER(COALESCE(r.session_type,'')) NOT LIKE '%deep%'
                   ORDER BY s.captured_at_madrid,s.scanner_run_id,s.ticker""",
            )
        finally:
            scanner.close()

        gex_by_ticker: dict[str, list[dict]] = {}
        buckets: dict[tuple[str, str], dict[str, dict]] = {}
        if gex_db.exists():
            gex = sqlite3.connect(gex_db); gex.row_factory = sqlite3.Row
            try:
                for row in _rows(
                    gex,
                    """SELECT gs.* FROM gex_summary gs JOIN gex_runs gr ON gr.id=gs.gex_run_id
                       WHERE gr.status='success' AND LOWER(COALESCE(gr.session_type,'')) NOT LIKE '%deep%'
                       ORDER BY gs.ticker,gs.captured_at_madrid""",
                ):
                    gex_by_ticker.setdefault(str(row.get("ticker") or ""), []).append(row)
                try:
                    bucket_rows = _rows(
                        gex,
                        """SELECT b.* FROM gex_expiry_buckets b JOIN gex_runs gr ON gr.id=b.gex_run_id
                           WHERE gr.status='success' AND LOWER(COALESCE(gr.session_type,'')) NOT LIKE '%deep%'""",
                    )
                except sqlite3.OperationalError:
                    bucket_rows = []
                for row in bucket_rows:
                    key = (str(row.get("ticker") or ""), str(row.get("gex_run_id") or ""))
                    buckets.setdefault(key, {})[str(row.get("expiry_bucket") or "")] = row
            finally:
                gex.close()
        return scanner_rows, gex_by_ticker, buckets


def bootstrap_intraday_archives_if_ready() -> dict:
    status = intraday_archive_status()
    if not config.INTRADAY_ARCHIVE_CALIBRATION_ENABLED or not status["ready"]:
        status.update({"imported_archives": 0, "imported_features": 0, "enabled_for_calibration": False})
        return status

    paths = intraday_archives()[-max(1, config.INTRADAY_ARCHIVE_IMPORT_MAX_SESSIONS):]
    paths = [path for path in paths if not archive_imported(f"intraday/{path.name}")]
    relationship_map = get_asset_relationships()
    total = imported_archives = 0

    for path in paths:
        try:
            scanner_rows, gex_by_ticker, buckets = _load_archive(path)
            by_run: dict[int, list[dict]] = {}
            for row in scanner_rows:
                by_run.setdefault(int(row.get("scanner_run_id") or 0), []).append(row)
            previous_features: dict[str, dict] = {}
            archive_count = 0
            for run_id in sorted(by_run):
                snapshots = by_run[run_id]
                base: dict[str, dict] = {}
                gex_for_ticker: dict[str, dict] = {}
                for snapshot in snapshots:
                    ticker = str(snapshot.get("ticker") or "")
                    if not ticker:
                        continue
                    captured = str(snapshot.get("captured_at_madrid") or "")
                    options = [row for row in gex_by_ticker.get(ticker, []) if str(row.get("captured_at_madrid") or "") <= captured]
                    gex = options[-1] if options and str(snapshot.get("gex_status") or "").lower() == "ok" else None
                    bucket_map = buckets.get((ticker, str((gex or {}).get("gex_run_id") or "")), {})
                    features = build_features(snapshot, gex, bucket_map, [])
                    features["source_archive"] = path.name
                    base[ticker] = features
                    if gex:
                        gex_for_ticker[ticker] = gex
                relational, _summary = build_relational_context(
                    base, previous_by_ticker=previous_features, relationship_map=relationship_map
                ) if base else ({}, {})
                for snapshot in snapshots:
                    ticker = str(snapshot.get("ticker") or "")
                    features = attach_relational_features(base.get(ticker) or {}, relational.get(ticker))
                    if not features:
                        continue
                    gex = gex_for_ticker.get(ticker)
                    row = feature_observation(snapshot, gex, features)
                    row["source_archive"] = path.name
                    row["observation_layer"] = "intraday"
                    row["is_canonical_daily_close"] = 0
                    if insert_feature_observation(row):
                        archive_count += 1
                    previous_features[ticker] = features
            mark_archive_imported(f"intraday/{path.name}", archive_count, 0, "intraday_zip_future_calibration")
            total += archive_count; imported_archives += 1
        except Exception as exc:
            log_error(f"🟥 No se pudo importar intradía {path.name}: {exc}")

    status.update({
        "imported_archives": imported_archives,
        "imported_features": total,
        "enabled_for_calibration": True,
    })
    if imported_archives:
        log_info(
            f"✅ Intraday archive bootstrap | zips={imported_archives} | features={total} "
            f"| sessions={status['available_sessions']}",
            send_discord=False,
        )
    return status
