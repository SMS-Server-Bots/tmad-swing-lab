from __future__ import annotations

"""Frozen cache for TMAD autonomous research.

The cache stores only the point-in-time contextual rows needed by the precision
research layer. It is intentionally independent from LIVE and lets later
research iterations reuse one scientifically frozen dataset without rebuilding
the full historical operational validation.

Safety:
- write-only from historical/research flows;
- no LIVE strategy/config mutation;
- no automatic deployment/promotion;
- cache consumers must validate the builder contract hash;
- FINAL TEST policy is enforced by the fast runner / research lab.
"""

from datetime import datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1
CACHE_MODE = "FAST_RESEARCH_CACHE_V1"


def _f(value: Any) -> float | None:
    try:
        if value in (None, "") or isinstance(value, bool):
            return None
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _compact_path(value: Any) -> dict:
    row = value if isinstance(value, dict) else {}
    return {
        "mfe_pct": _f(row.get("mfe_pct")),
        "mae_pct": _f(row.get("mae_pct")),
        "horizon_close_return_pct": _f(row.get("horizon_close_return_pct")),
    }


def _compact_diag(value: Any) -> dict:
    row = value if isinstance(value, dict) else {}
    return {
        "explicit_sector_etf": bool(row.get("explicit_sector_etf")),
        "explicit_main_etf_count": int(row.get("explicit_main_etf_count") or 0),
        "explicit_vol_count": int(row.get("explicit_vol_count") or 0),
        "market_regime": str(row.get("market_regime") or ""),
    }


def compact_context_row(row: dict) -> dict:
    """Keep exactly the fields required by fit, comparison and diagnostics."""
    features = {
        str(key): float(value)
        for key, value in sorted((row.get("context_features") or {}).items())
        if _f(value) is not None
    }
    rank = row.get("research_rank") or ()
    if not isinstance(rank, (list, tuple)):
        rank = (rank,)
    return {
        "date": str(row.get("date") or ""),
        "ticker": str(row.get("ticker") or ""),
        "stage": {"core": bool((row.get("stage") or {}).get("core"))},
        "current_direction": str(row.get("current_direction") or "").upper(),
        "research_rank": list(rank),
        "long_path": _compact_path(row.get("long_path")),
        "short_path": _compact_path(row.get("short_path")),
        "best_mover_5d": bool(row.get("best_mover_5d")),
        "best_direction": str(row.get("best_direction") or "").upper(),
        "context_direction": str(row.get("context_direction") or "").upper(),
        "context_features": features,
        "context_diagnostics": _compact_diag(row.get("context_diagnostics")),
        "gex_context": {
            "status": str((row.get("gex_context") or {}).get("status") or "NOT_CAPTURED"),
            "same_run": bool((row.get("gex_context") or {}).get("same_run")),
            "exact_timestamp_verified": bool((row.get("gex_context") or {}).get("exact_timestamp_verified")),
            "same_direction": bool((row.get("gex_context") or {}).get("same_direction")),
            "opposite_direction": bool((row.get("gex_context") or {}).get("opposite_direction")),
            "same_score": _f((row.get("gex_context") or {}).get("same_score")),
            "opposite_score": _f((row.get("gex_context") or {}).get("opposite_score")),
            "same_intent": str((row.get("gex_context") or {}).get("same_intent") or ""),
            "opposite_intent": str((row.get("gex_context") or {}).get("opposite_intent") or ""),
        },
        "context_usable": bool(row.get("context_usable")),
        "directional_mover_5d": bool(row.get("directional_mover_5d")),
    }


def _sorted_rows(rows: list[dict]) -> list[dict]:
    compact = [compact_context_row(row) for row in rows]
    compact.sort(
        key=lambda row: (
            str(row.get("date") or ""),
            str(row.get("ticker") or ""),
            str(row.get("context_direction") or ""),
        )
    )
    return compact


def _dataset_fingerprint(
    rows: list[dict],
    *,
    lane_label: str,
    split_dates: dict[str, list[str]],
    contract_hash: str,
) -> str:
    digest = sha256()
    header = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "lane": str(lane_label).upper(),
        "split_dates": {
            key: sorted(str(x) for x in values)
            for key, values in sorted(split_dates.items())
        },
        "contract_hash": str(contract_hash),
    }
    digest.update(
        json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    for row in rows:
        digest.update(b"\n")
        digest.update(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
    return digest.hexdigest()


def cache_filename(lane_label: str) -> str:
    lane = str(lane_label).strip().lower()
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in lane)
    return f"{safe}.v{CACHE_SCHEMA_VERSION}.json.gz"


def write_fast_research_cache(
    *,
    rows: list[dict],
    train_dates: set[str],
    validation_dates: set[str],
    test_dates: set[str],
    lane_label: str,
    producer_version: str,
    contract_hash: str,
    cache_root: Path,
    compression_level: int = 6,
) -> dict:
    started = datetime.now().astimezone()
    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    compact_rows = _sorted_rows(rows)
    split_dates = {
        "train": sorted(str(x) for x in train_dates),
        "validation": sorted(str(x) for x in validation_dates),
        "test": sorted(str(x) for x in test_dates),
    }
    fingerprint = _dataset_fingerprint(
        compact_rows,
        lane_label=lane_label,
        split_dates=split_dates,
        contract_hash=contract_hash,
    )
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "mode": CACHE_MODE,
        "producer_version": str(producer_version),
        "generated_at": started.isoformat(timespec="seconds"),
        "lane": str(lane_label).upper(),
        "contract_hash": str(contract_hash),
        "dataset_fingerprint": fingerprint,
        "split_dates": split_dates,
        "row_count": len(compact_rows),
        "rows": compact_rows,
        "policy": {
            "point_in_time_context_only": True,
            "future_prices_labels_only": True,
            "live_mutation": False,
            "cache_is_not_live_config": True,
            "final_test_is_lockbox": True,
        },
    }

    path = root / cache_filename(lane_label)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    level = max(1, min(9, int(compression_level)))
    try:
        with gzip.open(temp, "wt", encoding="utf-8", compresslevel=level) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass

    return {
        "status": "OK",
        "mode": CACHE_MODE,
        "lane": str(lane_label).upper(),
        "schema_version": CACHE_SCHEMA_VERSION,
        "path": str(path),
        "dataset_fingerprint": fingerprint,
        "contract_hash": str(contract_hash),
        "row_count": len(compact_rows),
        "train_dates": len(split_dates["train"]),
        "validation_dates": len(split_dates["validation"]),
        "test_dates": len(split_dates["test"]),
        "compressed_bytes": int(path.stat().st_size),
        "generated_at": payload["generated_at"],
        "live_mutation": False,
    }


def _fingerprint_payload(payload: dict) -> str:
    rows = list(payload.get("rows") or [])
    split_dates = payload.get("split_dates") or {}
    return _dataset_fingerprint(
        rows,
        lane_label=str(payload.get("lane") or ""),
        split_dates={
            "train": list(split_dates.get("train") or []),
            "validation": list(split_dates.get("validation") or []),
            "test": list(split_dates.get("test") or []),
        },
        contract_hash=str(payload.get("contract_hash") or ""),
    )


def load_fast_research_cache(
    path: Path,
    *,
    expected_contract_hash: str,
    verify_fingerprint: bool = True,
) -> dict:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)

    if int(payload.get("schema_version") or 0) != CACHE_SCHEMA_VERSION:
        raise ValueError(
            f"cache_schema_mismatch:{payload.get('schema_version')}!={CACHE_SCHEMA_VERSION}"
        )
    actual_contract = str(payload.get("contract_hash") or "")
    if actual_contract != str(expected_contract_hash):
        raise ValueError(
            f"cache_contract_mismatch:{actual_contract}!={expected_contract_hash}"
        )
    if verify_fingerprint:
        expected = str(payload.get("dataset_fingerprint") or "")
        actual = _fingerprint_payload(payload)
        if actual != expected:
            raise ValueError(f"cache_fingerprint_mismatch:{actual}!={expected}")
    return payload


def split_cached_rows(payload: dict) -> tuple[list[dict], list[dict], list[dict]]:
    split = payload.get("split_dates") or {}
    train_dates = set(str(x) for x in (split.get("train") or []))
    validation_dates = set(str(x) for x in (split.get("validation") or []))
    test_dates = set(str(x) for x in (split.get("test") or []))
    rows = list(payload.get("rows") or [])
    train = [row for row in rows if str(row.get("date") or "") in train_dates]
    validation = [
        row for row in rows if str(row.get("date") or "") in validation_dates
    ]
    test = [row for row in rows if str(row.get("date") or "") in test_dates]
    return train, validation, test


def update_cache_status_file(
    status_path: Path,
    *,
    lane_label: str,
    lane_status: dict,
    producer_version: str,
) -> dict:
    """Best-effort compact sidecar for GitHub/runtime-data observability."""
    path = Path(status_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        payload = {}
    lanes = dict(payload.get("lanes") or {})
    lanes[str(lane_label).upper()] = dict(lane_status)
    out = {
        "schema_version": 1,
        "mode": "FAST_RESEARCH_CACHE_STATUS_V1",
        "producer_version": str(producer_version),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "lanes": lanes,
        "live_mutation": False,
    }
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        temp.write_text(
            json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass
    return out
