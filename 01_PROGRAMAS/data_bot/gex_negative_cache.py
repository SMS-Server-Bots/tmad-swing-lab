from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

VERSION = "6.2.67"
_MADRID = ZoneInfo("Europe/Madrid")


def _data_root() -> Path:
    return Path(os.getenv("TMAD_DATA_DIR", Path(__file__).resolve().parents[2] / "03_DATOS")).resolve()


def cache_path() -> Path:
    return _data_root() / "data_bot" / "output" / "gex_negative_cache.json"


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "generated_at": datetime.now(_MADRID).isoformat(), "tickers": {}}


def load_cache() -> dict[str, Any]:
    path = cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return _empty()
        if not isinstance(payload.get("tickers"), dict):
            payload["tickers"] = {}
        return payload
    except Exception:
        return _empty()


def save_cache(payload: dict[str, Any]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["version"] = VERSION
    payload["generated_at"] = datetime.now(_MADRID).isoformat()
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def _iso_day(value: object | None = None) -> str:
    if value:
        text = str(value)[:10]
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            pass
    return datetime.now(_MADRID).date().isoformat()


def _retry_after(status_code: int, failure_day: str) -> str:
    base = date.fromisoformat(_iso_day(failure_day))
    days = int(os.getenv("GEX_404_BACKOFF_DAYS", "5")) if int(status_code) == 404 else int(os.getenv("GEX_400_BACKOFF_DAYS", "2"))
    return (base + timedelta(days=max(1, days))).isoformat()


def record_failure(ticker: str, status_code: int | None, failure_day: object | None = None) -> None:
    if status_code not in (400, 404):
        return
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return
    payload = load_cache()
    tickers = payload.setdefault("tickers", {})
    day = _iso_day(failure_day)
    old = tickers.get(symbol) if isinstance(tickers.get(symbol), dict) else {}
    tickers[symbol] = {
        "status_code": int(status_code),
        "failures": int(old.get("failures") or 0) + 1,
        "last_failure_date": day,
        "retry_after_date": _retry_after(int(status_code), day),
        "last_error_source": "runtime",
    }
    save_cache(payload)


def clear_success(ticker: str) -> None:
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return
    payload = load_cache()
    tickers = payload.setdefault("tickers", {})
    if symbol in tickers:
        tickers.pop(symbol, None)
        save_cache(payload)


def get_active_entry(ticker: str, *, today: object | None = None) -> dict[str, Any] | None:
    symbol = str(ticker or "").upper().strip()
    item = (load_cache().get("tickers") or {}).get(symbol)
    if not isinstance(item, dict):
        return None
    retry_after = str(item.get("retry_after_date") or "")[:10]
    if not retry_after:
        return None
    try:
        active = date.fromisoformat(retry_after) >= date.fromisoformat(_iso_day(today))
    except ValueError:
        return None
    return item if active else None


def scanner_override(row: dict[str, Any] | None) -> bool:
    """Allow a fresh call when Scanner contains new, potentially actionable evidence."""
    row = row or {}
    option_signal = str(row.get("optionSignal") or row.get("option_signal") or row.get("signal") or "").strip().lower()
    flow_signal = str(row.get("unusualFlowSignal") or row.get("unusual_flow_signal") or row.get("flowSignal") or "").strip().lower()
    meaningful = {"", "none", "neutral", "n/a", "na", "null", "false", "0", "no signal"}
    if option_signal not in meaningful or flow_signal not in meaningful:
        return True
    for key in ("gexChange", "gexChangePct", "gex_change", "totalGexChangePct"):
        try:
            if row.get(key) not in (None, "") and abs(float(row.get(key))) >= float(os.getenv("GEX_NEGATIVE_CACHE_OVERRIDE_CHANGE_PCT", "15")):
                return True
        except (TypeError, ValueError):
            continue
    return False


def should_skip(ticker: str, scanner_row: dict[str, Any] | None = None, *, force: bool = False) -> tuple[bool, dict[str, Any] | None]:
    item = get_active_entry(ticker)
    if not item or force or scanner_override(scanner_row):
        return False, item
    return True, item
