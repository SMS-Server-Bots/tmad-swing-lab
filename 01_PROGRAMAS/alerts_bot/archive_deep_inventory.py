from __future__ import annotations

"""Read-only FULL-DEEP inventory from immutable weekly Data Bot archives.

Used only by economic/reverse-alpha SHADOW evidence. It never changes LIVE
selection, thresholds or execution.
"""

import csv
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from research_snapshot import research_input_path


def _i(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def weekly_deep_inventory(
    archive_dir: Path,
    *,
    archive_names: set[str],
    dates: set[str],
) -> dict[str, dict]:
    """Return canonical expected Scanner rows by market date from weekly ZIPs.

    Weekly archives contain canonical deep runs only after finalisation, but a
    date may still contain more than one deep repair run. We therefore choose the
    latest run per date and count unique tickers for that exact run.
    """
    wanted_dates = {str(x) for x in dates if str(x)}
    if not wanted_dates or not archive_names:
        return {}

    grouped: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    latest_captured: dict[tuple[str, int, str], str] = {}
    for archive_name in sorted({str(x).strip() for x in archive_names if str(x).strip()}):
        path = research_input_path(Path(archive_dir) / archive_name)
        if not path.is_file():
            continue
        try:
            with ZipFile(path) as archive:
                members = [
                    name for name in archive.namelist()
                    if "/csv/scanner_snapshots_" in name and name.endswith(".csv")
                ]
                for member in members:
                    text = archive.read(member).decode("utf-8-sig")
                    for row in csv.DictReader(text.splitlines()):
                        date = str(row.get("snapshot_date") or "")[:10]
                        session = str(row.get("session_type") or "").lower()
                        ticker = str(row.get("ticker") or "").upper().strip()
                        if date not in wanted_dates or "deep" not in session or not ticker:
                            continue
                        run_id = _i(row.get("scanner_run_id") or row.get("run_id"))
                        captured = str(row.get("captured_at_madrid") or "")
                        key = (date, run_id, archive_name)
                        grouped[key].add(ticker)
                        if captured >= latest_captured.get(key, ""):
                            latest_captured[key] = captured
        except Exception:
            # Evidence degradation must remain non-fatal for the trading bot.
            continue

    by_date: dict[str, dict] = {}
    for (date, run_id, archive_name), tickers in grouped.items():
        captured = latest_captured.get((date, run_id, archive_name), "")
        candidate = {
            "snapshot_date": date,
            "run_id": run_id,
            "captured_at_madrid": captured,
            "expected_rows": len(tickers),
            "source_archive": archive_name,
        }
        old = by_date.get(date)
        if old is None or (captured, run_id) >= (
            str(old.get("captured_at_madrid") or ""),
            _i(old.get("run_id")),
        ):
            by_date[date] = candidate
    return by_date
