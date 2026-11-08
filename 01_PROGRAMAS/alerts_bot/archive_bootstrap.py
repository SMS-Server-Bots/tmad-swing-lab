from __future__ import annotations

import csv
import io
from bisect import bisect_right
from datetime import timedelta
from pathlib import Path
from zipfile import ZipFile

import config
from db import archive_imported, insert_feature_observation, mark_archive_imported, upsert_price_cache_rows
from discord_utils import log_error, log_info
from rules import build_features, feature_observation
from time_utils import now_madrid

LAST_BOOTSTRAP_STATUS: dict = {}

def get_last_archive_bootstrap_status() -> dict:
    return dict(LAST_BOOTSTRAP_STATUS)


def _member_for_table(archive: ZipFile, table: str) -> str | None:
    token = f"/csv/{table}_"
    for name in archive.namelist():
        if token in name and name.endswith(".csv"):
            return name
    return None


def _read_rows(archive: ZipFile, table: str) -> list[dict]:
    member = _member_for_table(archive, table)
    if not member:
        return []
    with archive.open(member) as raw:
        wrapper = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        return [dict(row) for row in csv.DictReader(wrapper)]


def _week_start(path: Path):
    try:
        year, week = path.stem.split("-W", 1)
        from datetime import date
        return date.fromisocalendar(int(year), int(week), 1)
    except Exception:
        return None


def _recent_archives() -> list[Path]:
    """Select enough immutable weekly ZIPs to cover the training window.

    The fixed ARCHIVE_BOOTSTRAP_WEEKS remains a minimum. More weeks are selected
    automatically (up to ARCHIVE_BOOTSTRAP_MAX_WEEKS) to preserve the growing adaptive history.
    """
    if not config.DATA_ARCHIVE_DIR.exists():
        return []
    paths = sorted(config.DATA_ARCHIVE_DIR.glob("????-W??.zip"))
    if not paths:
        return []
    selected: list[Path] = []
    latest_start = _week_start(paths[-1])
    target_days = max(1, int(config.STARTUP_HISTORY_TARGET_DAYS), int(config.MODEL_HISTORY_MAX_DAYS))
    min_weeks = max(1, int(config.ARCHIVE_BOOTSTRAP_WEEKS))
    max_weeks = max(min_weeks, int(config.ARCHIVE_BOOTSTRAP_MAX_WEEKS))
    for path in reversed(paths):
        selected.append(path)
        start = _week_start(path)
        covered = (latest_start - start).days + 7 if latest_start and start else 0
        if len(selected) >= min_weeks and covered >= target_days:
            break
        if len(selected) >= max_weeks:
            break
    return sorted(selected)


def bootstrap_from_archives() -> tuple[int, int]:
    global LAST_BOOTSTRAP_STATUS
    """Import compact features/prices from closed weekly Data Bot ZIPs.

    Weekly ZIPs contain only canonical deep closes. The full Scanner universe is
    imported even when a specific ticker lacks a valid granular GEX endpoint; such
    rows still contribute to breadth/sector context, while GEX rules require has_gex=1.
    Each ZIP is imported only once, tracked in archive_imports.
    """
    if not config.BOOTSTRAP_ARCHIVES_ON_START:
        LAST_BOOTSTRAP_STATUS = {"status":"disabled","selected":0,"new":0,"usable":0,"skipped":0,"features":0,"prices":0}
        return 0, 0
    selected_paths = _recent_archives()
    paths = [path for path in selected_paths if not archive_imported(path.name)]
    if not paths:
        LAST_BOOTSTRAP_STATUS = {"status":"nothing_new","selected":len(selected_paths),"new":0,"usable":0,"skipped":0,"features":0,"prices":0,"details":[]}
        return 0, 0
    cutoff = (now_madrid().date() - timedelta(days=config.FEATURE_RETENTION_DAYS)).isoformat()

    all_scanner: list[tuple[Path, dict]] = []
    gex_by_ticker_date: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    buckets_by_run: dict[tuple[str, str], dict[str, dict]] = {}
    price_rows_by_archive: dict[Path, list[dict]] = {}
    source_counts: dict[Path, dict] = {path: {"scanner_deep": 0, "gex_deep": 0, "prices": 0} for path in paths}

    for path in paths:
        try:
            with ZipFile(path) as archive:
                scanner_rows = _read_rows(archive, "scanner_snapshots")
                gex_rows = _read_rows(archive, "gex_summary")
                bucket_rows = _read_rows(archive, "gex_expiry_buckets")
                prices = _read_rows(archive, "ticker_ohlc_daily")
                if not prices:
                    # Compatibility fallback: daily close only, no optimistic high/low inference.
                    prices = _read_rows(archive, "ticker_daily_history")
                    for row in prices:
                        row["trade_date"] = row.get("trade_date")
                        row["close"] = row.get("current_price")
                        row["open"] = row.get("current_price")
                        row["high"] = row.get("current_price")
                        row["low"] = row.get("current_price")
                price_rows_by_archive[path] = prices
                source_counts[path]["prices"] = len(prices)
                for row in scanner_rows:
                    session = str(row.get("session_type") or "").lower()
                    if "deep" in session and str(row.get("snapshot_date") or "") >= cutoff:
                        all_scanner.append((path, row))
                        source_counts[path]["scanner_deep"] += 1
                for row in gex_rows:
                    session = str(row.get("session_type") or "").lower()
                    ticker = str(row.get("ticker") or "")
                    captured = str(row.get("captured_at_madrid") or "")
                    if "deep" in session and ticker and captured:
                        gex_by_ticker_date.setdefault((ticker, str(row.get("snapshot_date") or "")), []).append((captured, row))
                        source_counts[path]["gex_deep"] += 1
                for row in bucket_rows:
                    session = str(row.get("session_type") or "").lower()
                    if "deep" not in session:
                        continue
                    key = (str(row.get("ticker") or ""), str(row.get("gex_run_id") or ""))
                    buckets_by_run.setdefault(key, {})[str(row.get("expiry_bucket") or "")] = row
        except Exception as exc:
            log_error(f"🟥 No se pudo leer archive {path.name}: {exc}")

    for values in gex_by_ticker_date.values():
        values.sort(key=lambda item: item[0])

    inserted_by_archive: dict[Path, int] = {path: 0 for path in paths}
    for source_path, snapshot in all_scanner:
        ticker = str(snapshot.get("ticker") or "")
        captured = str(snapshot.get("captured_at_madrid") or "")
        candidates = gex_by_ticker_date.get((ticker, str(snapshot.get("snapshot_date") or ""))) or []
        times = [item[0] for item in candidates]
        index = bisect_right(times, captured) - 1
        gex = candidates[index][1] if index >= 0 else None
        bucket_map = {}
        if gex:
            bucket_map = buckets_by_run.get((ticker, str(gex.get("gex_run_id") or "")), {})
        features = build_features(snapshot, gex, bucket_map, [])
        features["source_archive"] = source_path.name
        features["observation_layer"] = "daily_close"
        features["is_canonical_daily_close"] = 1
        try:
            row = feature_observation(snapshot, gex, features)
            row["source_archive"] = source_path.name
            row["observation_layer"] = "daily_close"
            row["is_canonical_daily_close"] = 1
        except Exception:
            continue
        if insert_feature_observation(row):
            inserted_by_archive[source_path] += 1

    total_features = total_prices = 0
    for path in paths:
        prices = price_rows_by_archive.get(path) or []
        price_count = upsert_price_cache_rows(prices, f"archive:{path.name}")
        feature_count = inserted_by_archive.get(path, 0)
        mark_archive_imported(path.name, feature_count, price_count, "weekly_zip_bootstrap")
        total_features += feature_count
        total_prices += price_count
    details = []
    usable = skipped = 0
    for path in paths:
        item = {
            "zip": path.name,
            **source_counts.get(path, {}),
            "features_imported": inserted_by_archive.get(path, 0),
            "prices_imported": len(price_rows_by_archive.get(path) or []),
        }
        item["usable"] = bool(item["features_imported"] or item["prices_imported"])
        if item["usable"]:
            usable += 1
        else:
            skipped += 1
            item["skip_reason"] = "El ZIP no contiene cierres deep canónicos utilizables."
        details.append(item)
    LAST_BOOTSTRAP_STATUS = {
        "status": "ok" if usable else "no_usable_deep",
        "selected": len(selected_paths),
        "new": len(paths),
        "usable": usable,
        "skipped": skipped,
        "features": total_features,
        "prices": total_prices,
        "details": details,
    }
    log_info(
        f"📦 [A02] ZIP semanales | revisados={len(paths)} | utilizables={usable} | "
        f"sin deep={skipped} | features={total_features} | prices={total_prices}",
        send_discord=False,
    )
    return total_features, total_prices
