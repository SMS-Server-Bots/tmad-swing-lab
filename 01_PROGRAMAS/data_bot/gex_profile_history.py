from __future__ import annotations

import hashlib
import json
import zlib
from typing import Any

import config
from db_core import gex_conn
from time_utils import market_date_from_madrid_iso, ts_madrid_str, ts_utc_str


def _num(value: Any):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # Removes irrelevant binary-float noise while preserving the API value
        # at a precision far above what is useful for GEX research.
        return round(float(value), int(config.GEX_PROFILE_FLOAT_DECIMALS))
    return value


def _clean_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean_mapping(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, list):
        return [_clean_mapping(v) for v in value]
    return _num(value)


def _strike_key(row: dict) -> str:
    value = row.get("strike")
    try:
        return f"{float(value):.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def _expiry_key(row: dict) -> str:
    return str(row.get("expiration") or row.get("expirationDate") or row.get("expiry") or "")[:10]


def canonical_profile(payload: dict | None, ticker: str | None = None) -> dict:
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = {}
    strikes = [dict(x) for x in (data.get("gexByStrike") or []) if isinstance(x, dict)]
    expiries = [dict(x) for x in (data.get("gexByExpiry") or []) if isinstance(x, dict)]
    strikes.sort(key=lambda x: float(x.get("strike") or 0))
    expiries.sort(key=lambda x: _expiry_key(x))
    profile = {
        "symbol": str(data.get("symbol") or ticker or "").upper(),
        "spot": data.get("spot"),
        "totalGex": data.get("totalGex"),
        "gammaFlip": data.get("gammaFlip"),
        "contractCount": data.get("contractCount"),
        "dataSource": data.get("dataSource"),
        "horizonDefinitions": data.get("horizonDefinitions") or [],
        "summaryByHorizon": data.get("summaryByHorizon") or {},
        "gexByStrike": strikes,
        "gexByExpiry": expiries,
    }
    return _clean_mapping(profile)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(profile: dict) -> str:
    return hashlib.sha256(_json_bytes(profile)).hexdigest()


def _compress(value: Any) -> bytes:
    return zlib.compress(_json_bytes(value), level=int(config.GEX_PROFILE_ZLIB_LEVEL))


def _decompress(blob: bytes | None) -> dict | None:
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _mapping_diff(previous: dict, current: dict) -> dict:
    changes: dict[str, list[Any]] = {}
    for key in sorted(set(previous) | set(current)):
        old = previous.get(key)
        new = current.get(key)
        if old != new:
            changes[key] = [old, new]
    return changes


def _flatten_patch(previous: Any, current: Any, prefix: str = "") -> tuple[dict[str, Any], list[str]]:
    sets: dict[str, Any] = {}
    removes: list[str] = []
    if isinstance(previous, dict) and isinstance(current, dict):
        for key in sorted(set(previous) | set(current)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in current:
                removes.append(path)
            elif key not in previous:
                sets[path] = current[key]
            else:
                child_sets, child_removes = _flatten_patch(previous[key], current[key], path)
                sets.update(child_sets)
                removes.extend(child_removes)
        return sets, removes
    if previous != current:
        sets[prefix] = current
    return sets, removes


def _rows_diff(previous_rows: list[dict], current_rows: list[dict], key_func) -> tuple[list[dict], list[dict], list[str]]:
    previous = {key_func(row): row for row in previous_rows if key_func(row)}
    current = {key_func(row): row for row in current_rows if key_func(row)}
    new_rows: list[dict] = []
    patches: list[dict] = []
    for key in sorted(current):
        if key not in previous:
            new_rows.append(current[key])
            continue
        if previous[key] == current[key]:
            continue
        sets, removes = _flatten_patch(previous[key], current[key])
        patches.append({"key": key, "set": sets, "remove": removes})
    removed = [key for key in sorted(previous) if key not in current]
    return new_rows, patches, removed


def build_delta(previous: dict, current: dict) -> dict:
    strike_new, strike_patches, strike_removed = _rows_diff(
        previous.get("gexByStrike") or [], current.get("gexByStrike") or [], _strike_key
    )
    expiry_new, expiry_patches, expiry_removed = _rows_diff(
        previous.get("gexByExpiry") or [], current.get("gexByExpiry") or [], _expiry_key
    )
    top_previous = {k: v for k, v in previous.items() if k not in {"gexByStrike", "gexByExpiry", "summaryByHorizon"}}
    top_current = {k: v for k, v in current.items() if k not in {"gexByStrike", "gexByExpiry", "summaryByHorizon"}}
    top_set, top_remove = _flatten_patch(top_previous, top_current)
    horizon_set, horizon_remove = _flatten_patch(previous.get("summaryByHorizon") or {}, current.get("summaryByHorizon") or {})
    return {
        "topSet": top_set,
        "topRemove": top_remove,
        "horizonSet": horizon_set,
        "horizonRemove": horizon_remove,
        "strikeNew": strike_new,
        "strikePatches": strike_patches,
        "strikeRemoved": strike_removed,
        "expiryNew": expiry_new,
        "expiryPatches": expiry_patches,
        "expiryRemoved": expiry_removed,
    }


def _set_path(target: dict, path: str, value: Any) -> None:
    parts = [part for part in str(path).split(".") if part]
    cursor = target
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    if parts:
        cursor[parts[-1]] = value


def _remove_path(target: dict, path: str) -> None:
    parts = [part for part in str(path).split(".") if part]
    cursor = target
    for part in parts[:-1]:
        cursor = cursor.get(part)
        if not isinstance(cursor, dict):
            return
    if parts and isinstance(cursor, dict):
        cursor.pop(parts[-1], None)


def _apply_patch(target: dict, sets: dict, removes: list[str]) -> None:
    for path in removes or []:
        _remove_path(target, path)
    for path, value in (sets or {}).items():
        _set_path(target, path, value)


def apply_delta(previous: dict, delta: dict) -> dict:
    """Reconstruct the next canonical profile from one archived delta."""
    current = json.loads(json.dumps(previous))
    top = {k: v for k, v in current.items() if k not in {"gexByStrike", "gexByExpiry", "summaryByHorizon"}}
    _apply_patch(top, delta.get("topSet") or {}, delta.get("topRemove") or [])
    for key in list(current):
        if key not in {"gexByStrike", "gexByExpiry", "summaryByHorizon"}:
            current.pop(key, None)
    current.update(top)
    horizons = current.setdefault("summaryByHorizon", {})
    _apply_patch(horizons, delta.get("horizonSet") or {}, delta.get("horizonRemove") or [])

    def apply_rows(name: str, new_key: str, patch_key: str, removed_key: str, key_func) -> None:
        rows = {key_func(row): row for row in (current.get(name) or []) if key_func(row)}
        for key in delta.get(removed_key) or []:
            rows.pop(str(key), None)
        for row in delta.get(new_key) or []:
            rows[key_func(row)] = row
        for patch in delta.get(patch_key) or []:
            key = str(patch.get("key") or "")
            row = rows.get(key)
            if row is None:
                continue
            _apply_patch(row, patch.get("set") or {}, patch.get("remove") or [])
        current[name] = list(rows.values())
        if name == "gexByStrike":
            current[name].sort(key=lambda row: float(row.get("strike") or 0))
        else:
            current[name].sort(key=_expiry_key)

    apply_rows("gexByStrike", "strikeNew", "strikePatches", "strikeRemoved", _strike_key)
    apply_rows("gexByExpiry", "expiryNew", "expiryPatches", "expiryRemoved", _expiry_key)
    return _clean_mapping(current)

def _changed_fields(delta: dict) -> list[str]:
    fields = [f"top.{key}" for key in (delta.get("topSet") or {})]
    fields += [f"top.remove.{key}" for key in (delta.get("topRemove") or [])]
    fields += [f"horizon.{key}" for key in (delta.get("horizonSet") or {})]
    fields += [f"horizon.remove.{key}" for key in (delta.get("horizonRemove") or [])]
    if delta.get("strikeNew"):
        fields.append("gexByStrike.new")
    if delta.get("strikePatches"):
        fields.append("gexByStrike.patches")
    if delta.get("strikeRemoved"):
        fields.append("gexByStrike.removed")
    if delta.get("expiryNew"):
        fields.append("gexByExpiry.new")
    if delta.get("expiryPatches"):
        fields.append("gexByExpiry.patches")
    if delta.get("expiryRemoved"):
        fields.append("gexByExpiry.removed")
    return fields


def _material_flags(previous: dict | None, current: dict, delta: dict) -> dict:
    previous = previous or {}
    flags: dict[str, bool] = {}
    old_h = previous.get("summaryByHorizon") or {}
    new_h = current.get("summaryByHorizon") or {}
    spot = current.get("spot") if isinstance(current.get("spot"), (int, float)) else previous.get("spot")
    for horizon in ("all", "near", "swing", "long"):
        old = old_h.get(horizon) or {}
        new = new_h.get(horizon) or {}
        for field in ("callWallStrike", "putWallStrike"):
            flags[f"{horizon}_{field}"] = old.get(field) != new.get(field)
        old_flip = old.get("gammaFlip")
        new_flip = new.get("gammaFlip")
        if isinstance(old_flip, (int, float)) and isinstance(new_flip, (int, float)) and isinstance(spot, (int, float)) and spot:
            flags[f"{horizon}_gammaFlip"] = abs(float(new_flip) - float(old_flip)) / abs(float(spot)) * 100 >= config.GEX_PROFILE_MATERIAL_LEVEL_MOVE_PCT
        else:
            flags[f"{horizon}_gammaFlip"] = old_flip != new_flip
        old_net = old.get("gex")
        new_net = new.get("gex")
        if isinstance(old_net, (int, float)) and isinstance(new_net, (int, float)):
            denom = max(abs(float(old_net)), 1.0)
            flags[f"{horizon}_gex_move"] = abs(float(new_net) - float(old_net)) / denom * 100 >= config.GEX_PROFILE_MATERIAL_GEX_PCT
            flags[f"{horizon}_sign_flip"] = old_net != 0 and new_net != 0 and (old_net > 0) != (new_net > 0)
    previous_strikes = {_strike_key(row) for row in (previous.get("gexByStrike") or [])}
    current_strikes = {_strike_key(row) for row in (current.get("gexByStrike") or [])}
    previous_expiries = {_expiry_key(row) for row in (previous.get("gexByExpiry") or [])}
    current_expiries = {_expiry_key(row) for row in (current.get("gexByExpiry") or [])}
    flags["strike_membership_change"] = previous_strikes != current_strikes
    flags["expiry_membership_change"] = previous_expiries != current_expiries
    return flags


def record_gex_profile_event(
    gex_run_id: int,
    session_type: str,
    ticker: str,
    payload: dict,
    captured_at_utc: str | None = None,
    captured_at_madrid: str | None = None,
) -> dict:
    """Persist one daily baseline plus compressed deltas for every later profile.

    The latest full state remains in gex_profile_state so restarts do not break the
    delta chain. Daily events are safe to archive and purge after ZIP verification.
    """
    if not config.GEX_PROFILE_HISTORY_ENABLED:
        return {"stored": False, "reason": "disabled"}
    utc = captured_at_utc or ts_utc_str()
    mad = captured_at_madrid or ts_madrid_str()
    trade_date = market_date_from_madrid_iso(mad)
    ticker = str(ticker).upper()
    current = canonical_profile(payload, ticker)
    current_hash = _hash(current)

    conn = gex_conn()
    try:
        row = conn.execute("SELECT * FROM gex_profile_state WHERE ticker=?", (ticker,)).fetchone()
        previous = _decompress(row["profile_blob"]) if row else None
        previous_hash = str(row["profile_hash"]) if row else None
        previous_run_id = int(row["gex_run_id"]) if row and row["gex_run_id"] is not None else None
        is_daily_baseline = row is None or str(row["snapshot_date"] or "") != trade_date

        if is_daily_baseline:
            kind = "baseline"
            body = current
            delta = {
                "topSet": {}, "topRemove": [], "horizonSet": {}, "horizonRemove": [],
                "strikeNew": current.get("gexByStrike") or [], "strikePatches": [], "strikeRemoved": [],
                "expiryNew": current.get("gexByExpiry") or [], "expiryPatches": [], "expiryRemoved": [],
            }
            changed_fields = ["daily_baseline"]
            material_flags = {"daily_baseline": True}
        elif previous_hash == current_hash:
            # Exact profile duplicate: update state timestamp but do not grow event storage.
            conn.execute(
                """UPDATE gex_profile_state SET gex_run_id=?,captured_at_utc=?,captured_at_madrid=?,updated_at_utc=? WHERE ticker=?""",
                (gex_run_id, utc, mad, utc, ticker),
            )
            conn.commit()
            return {"stored": False, "reason": "unchanged", "profile_hash": current_hash}
        else:
            kind = "delta"
            delta = build_delta(previous or {}, current)
            body = delta
            changed_fields = _changed_fields(delta)
            material_flags = _material_flags(previous, current, delta)

        body_blob = _compress(body)
        changed_strikes = len(delta.get("strikeNew") or []) + len(delta.get("strikePatches") or []) + len(delta.get("strikeRemoved") or [])
        changed_expiries = len(delta.get("expiryNew") or []) + len(delta.get("expiryPatches") or []) + len(delta.get("expiryRemoved") or [])
        material = 1 if any(material_flags.values()) else 0
        conn.execute(
            """INSERT OR REPLACE INTO gex_profile_events
               (gex_run_id,previous_gex_run_id,captured_at_utc,captured_at_madrid,snapshot_date,session_type,ticker,
                event_kind,previous_profile_hash,profile_hash,spot,previous_spot,changed_field_count,
                changed_strike_count,changed_expiry_count,material_change,changed_fields_json,material_flags_json,
                profile_blob,compressed,created_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                gex_run_id, previous_run_id, utc, mad, trade_date, session_type, ticker,
                kind, previous_hash, current_hash, current.get("spot"), (previous or {}).get("spot"),
                len(changed_fields), changed_strikes, changed_expiries, material,
                json.dumps(changed_fields, ensure_ascii=False), json.dumps(material_flags, ensure_ascii=False, sort_keys=True),
                body_blob, 1, utc,
            ),
        )
        conn.execute(
            """INSERT INTO gex_profile_state
               (ticker,gex_run_id,captured_at_utc,captured_at_madrid,snapshot_date,profile_hash,spot,profile_blob,compressed,updated_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 gex_run_id=excluded.gex_run_id,captured_at_utc=excluded.captured_at_utc,
                 captured_at_madrid=excluded.captured_at_madrid,snapshot_date=excluded.snapshot_date,
                 profile_hash=excluded.profile_hash,spot=excluded.spot,profile_blob=excluded.profile_blob,
                 compressed=excluded.compressed,updated_at_utc=excluded.updated_at_utc""",
            (ticker, gex_run_id, utc, mad, trade_date, current_hash, current.get("spot"), _compress(current), 1, utc),
        )
        conn.commit()
        return {
            "stored": True,
            "event_kind": kind,
            "profile_hash": current_hash,
            "changed_fields": len(changed_fields),
            "changed_strikes": changed_strikes,
            "changed_expiries": changed_expiries,
            "material_change": bool(material),
        }
    finally:
        conn.close()
