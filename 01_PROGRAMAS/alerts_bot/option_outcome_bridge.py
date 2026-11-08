from __future__ import annotations

"""Targeted mature underlying outcomes for journalled EOD option contracts.

EOD selections are not feature_observation records, so matching them against
observation_outcomes by observation id/date is semantically wrong.  The journal
already contains the point-in-time entry spot.  Mature underlying outcomes are
therefore reconstructed from the canonical price_cache trading sessions that
occurred strictly after the journal date.

This is SHADOW research evidence only; it does not create option market marks.
"""

import json
import sqlite3
from pathlib import Path

HORIZONS = (1, 2, 3, 5)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def journal_contract_rows(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except Exception:
            continue
        signal_date = str(row.get("asof") or "")[:10]
        ticker = str(row.get("ticker") or "").upper().strip()
        key = str(row.get("key") or f"{signal_date}|{ticker}|{row.get('direction') or ''}")
        try:
            spot = float(row.get("spot") or 0.0)
        except Exception:
            spot = 0.0
        if signal_date and ticker and spot > 0 and key not in seen:
            rows.append({**row, "asof": signal_date, "ticker": ticker, "spot": spot})
            seen.add(key)
    return rows


def journal_contract_keys(path: str | Path) -> list[tuple[str, str]]:
    return sorted({(str(row["asof"]), str(row["ticker"])) for row in journal_contract_rows(path)})


def load_targeted_option_outcomes(
    conn: sqlite3.Connection,
    journal_path: str | Path,
) -> list[dict]:
    """Build 1/2/3/5-session outcomes from price_cache for EOD selections."""
    if not _table_exists(conn, "price_cache"):
        return []

    journal = journal_contract_rows(journal_path)
    if not journal:
        return []

    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    for ordinal, item in enumerate(journal, start=1):
        ticker = str(item["ticker"])
        signal_date = str(item["asof"])
        spot0 = float(item["spot"])
        future = [
            dict(row)
            for row in conn.execute(
                """SELECT trade_date,open,high,low,close
                   FROM price_cache
                   WHERE ticker=? AND trade_date>?
                   ORDER BY trade_date
                   LIMIT ?""",
                (ticker, signal_date, max(HORIZONS)),
            ).fetchall()
        ]
        if not future:
            continue

        first_session_date = str(future[0].get("trade_date") or "")
        for horizon in HORIZONS:
            if len(future) < horizon:
                continue
            exit_row = future[horizon - 1]
            try:
                exit_price = float(exit_row.get("close") or 0.0)
            except Exception:
                exit_price = 0.0
            if exit_price <= 0:
                continue
            return_long_pct = (exit_price / spot0 - 1.0) * 100.0
            rows.append({
                # Stable synthetic id is used only for deterministic de-duplication
                # inside the SHADOW proxy. It is not an observation DB id.
                "observation_id": -(ordinal * 10 + horizon),
                "ticker": ticker,
                "observation_date": signal_date,
                "snapshot_date": signal_date,
                "horizon_sessions": horizon,
                "entry_price": spot0,
                "first_session_date": first_session_date,
                "exit_date": str(exit_row.get("trade_date") or ""),
                "exit_price": exit_price,
                "return_long_pct": return_long_pct,
                "mfe_long_pct": None,
                "mae_long_pct": None,
                "complete": 1,
                "features_json": "{}",
                "observation_layer": "eod_journal_price_cache",
                "is_canonical_daily_close": 1,
                "outcome_source": "PRICE_CACHE_FUTURE_SESSIONS",
            })

    return rows
