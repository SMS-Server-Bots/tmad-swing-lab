from __future__ import annotations

import json
from datetime import datetime

import config
from db import get_active_model, get_open_trades, get_runtime_state, set_runtime_state
from discord_utils import log_info
from lifecycle import update_trade_lifecycle
from time_utils import now_madrid

RISK_REFRESH_FILE = config.DATA_BOT_DIR / "output" / "risk_refresh_latest.json"
RISK_REFRESH_MAX_AGE_MINUTES = 20.0


def _age_minutes(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now_madrid().tzinfo)
        return (now_madrid() - parsed).total_seconds() / 60.0
    except Exception:
        return None


def maybe_process_partial_risk_refresh() -> dict:
    """Apply a partial Scanner sidecar only to lifecycle of already-open trades.

    No entry engine, Research layer, calibration table or Data generation is
    touched here. This is an idempotent risk-control path for the exact case in
    which global Scanner pagination is incomplete but an open ticker is present.
    """
    if not RISK_REFRESH_FILE.exists():
        return {"status": "missing", "executed": False}
    try:
        payload = json.loads(RISK_REFRESH_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"status": "invalid", "executed": False, "error": str(exc)}

    if str(payload.get("kind") or "") != "POSITION_RISK_ONLY":
        return {"status": "wrong_kind", "executed": False}
    if bool(payload.get("entries_allowed")) or bool(payload.get("research_allowed")) or bool(payload.get("learning_allowed")):
        return {"status": "unsafe_flags", "executed": False}

    run_id = int(payload.get("scanner_run_id") or 0)
    if run_id <= 0:
        return {"status": "invalid_run", "executed": False}
    if str(get_runtime_state("risk_refresh_last_run_id", "") or "") == str(run_id):
        return {"status": "already_processed", "executed": False, "run_id": run_id}

    age = _age_minutes(payload.get("generated_at_madrid"))
    if age is None or age < -2.0 or age > RISK_REFRESH_MAX_AGE_MINUTES:
        return {"status": "stale", "executed": False, "run_id": run_id, "age_minutes": age}

    open_tickers = {str(row.get("ticker") or "").upper() for row in get_open_trades()}
    if not open_tickers:
        set_runtime_state("risk_refresh_last_run_id", str(run_id))
        return {"status": "no_open_positions", "executed": False, "run_id": run_id}

    snapshots: list[dict] = []
    gex_map: dict[str, dict] = {}
    expiry_map: dict[str, dict[str, dict]] = {}
    expirations_map: dict[str, list[dict]] = {}
    for item in payload.get("items") or []:
        ticker = str((item or {}).get("ticker") or "").upper()
        if not ticker or ticker not in open_tickers:
            continue
        snapshot = (item or {}).get("snapshot")
        if isinstance(snapshot, dict):
            snapshots.append(snapshot)
        gex = (item or {}).get("gex")
        if isinstance(gex, dict) and str((item or {}).get("gex_status") or "").lower() == "ok":
            gex_map[ticker] = gex
        buckets = (item or {}).get("expiry_buckets")
        if isinstance(buckets, dict):
            expiry_map[ticker] = buckets
        expirations = (item or {}).get("expirations")
        if isinstance(expirations, list):
            expirations_map[ticker] = [row for row in expirations if isinstance(row, dict)]

    if not snapshots:
        # Do not mark consumed when an open ticker was absent: a corrected sidecar
        # for the same partial run may still arrive during its freshness window.
        return {
            "status": "open_tickers_missing",
            "executed": False,
            "run_id": run_id,
            "missing": sorted(open_tickers),
        }

    closed = update_trade_lifecycle(
        snapshots,
        gex_map=gex_map,
        expiry_map=expiry_map,
        expirations_map=expirations_map,
        model=get_active_model(),
        features_by_ticker=None,
    )
    set_runtime_state("risk_refresh_last_run_id", str(run_id))
    set_runtime_state("risk_refresh_last_processed_at", now_madrid().isoformat())
    set_runtime_state("risk_refresh_last_closed", str(int(closed or 0)))
    log_info(
        "🛡️ [RISK REFRESH] "
        f"partial_scanner_run={run_id} age={age:.1f}m positions={len(snapshots)} "
        f"gex_valid={len(gex_map)} closed={int(closed or 0)} entries=DISABLED learning=DISABLED",
        send_discord=False,
    )
    return {
        "status": "processed",
        "executed": True,
        "run_id": run_id,
        "positions": len(snapshots),
        "gex_valid": len(gex_map),
        "closed": int(closed or 0),
    }
