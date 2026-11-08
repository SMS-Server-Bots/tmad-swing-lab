from __future__ import annotations

"""Immutable economic benchmark for TMAD research.

The rolling production history is allowed to grow and historical archives may be
repaired. That is useful for current-state research, but it makes version-to-
version economics difficult to compare. This module creates exactly one
benchmark manifest and never rewrites it:

* canonical observation IDs are frozen for 2/3/5-session research,
* point-in-time Scanner run keys are frozen with the same substantial-run rule
  used by historical operational replay,
* all identifiers are taken from the frozen T0 SQLite snapshot,
* the manifest is copied into future frozen research snapshots.

New research can therefore report two lanes independently:
  FIXED_BENCHMARK  -> same historical cohort across code versions.
  ROLLING_LATEST   -> newest evidence, allowed to change over time.
"""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import config
from db import connect
from research_snapshot import research_db_path, research_input_path

BENCHMARK_ID = "TMAD_ECONOMIC_BENCHMARK_V1"
BENCHMARK_SCHEMA_VERSION = 1
HORIZONS = (2, 3, 5)


def benchmark_live_path() -> Path:
    return Path(config.RUNTIME_ROOT) / "research_benchmarks" / "economic_benchmark_v1.json"


def benchmark_frozen_path() -> Path:
    return research_input_path(benchmark_live_path())


def _sha(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _validate_manifest(value: dict) -> dict:
    errors: list[str] = []
    if value.get("benchmark_id") != BENCHMARK_ID:
        errors.append("BENCHMARK_ID_MISMATCH")
    if int(value.get("schema_version") or 0) != BENCHMARK_SCHEMA_VERSION:
        errors.append("SCHEMA_VERSION_MISMATCH")
    canonical = value.get("canonical_observation_ids_by_horizon") or {}
    for horizon in HORIZONS:
        ids = canonical.get(str(horizon))
        if not isinstance(ids, list) or not ids:
            errors.append(f"MISSING_CANONICAL_IDS_{horizon}D")
    run_keys = value.get("operational_run_keys") or []
    if not isinstance(run_keys, list) or not run_keys:
        errors.append("MISSING_OPERATIONAL_RUN_KEYS")

    integrity = value.get("integrity") or {}
    if canonical:
        current = _sha({
            str(h): sorted(int(v) for v in (canonical.get(str(h)) or []))
            for h in HORIZONS
        })
        if integrity.get("canonical_ids_sha256") and current != integrity.get("canonical_ids_sha256"):
            errors.append("CANONICAL_ID_HASH_MISMATCH")
    if isinstance(run_keys, list) and run_keys:
        normalized = sorted(
            (
                str(row.get("date") or "")[:10],
                int(row.get("source_scanner_run_id") or 0),
            )
            for row in run_keys
        )
        current = _sha(normalized)
        if integrity.get("operational_run_keys_sha256") and current != integrity.get("operational_run_keys_sha256"):
            errors.append("RUN_KEY_HASH_MISMATCH")
    return {
        "valid": not errors,
        "errors": errors,
    }


def _canonical_observation_manifest(conn) -> tuple[dict[str, list[int]], dict[str, list[str]]]:
    """Mirror swing_v2._canonical_rows using a SQL window over completed outcomes."""
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT
                o.observation_id,
                o.horizon_sessions,
                o.observation_date,
                f.ticker,
                ROW_NUMBER() OVER (
                    PARTITION BY o.observation_date, UPPER(f.ticker), o.horizon_sessions
                    ORDER BY
                        CASE WHEN COALESCE(f.is_canonical_daily_close,0)=1 THEN 1 ELSE 0 END DESC,
                        CASE
                            WHEN LOWER(COALESCE(f.session_type,'')) LIKE '%deep%'
                              OR LOWER(COALESCE(f.session_type,'')) LIKE '%full%'
                              OR LOWER(COALESCE(f.session_type,'')) LIKE '%close%'
                            THEN 1 ELSE 0
                        END DESC,
                        COALESCE(f.source_scanner_run_id,0) DESC,
                        o.observation_id DESC
                ) AS rn
            FROM observation_outcomes o
            JOIN feature_observations f ON f.id=o.observation_id
            WHERE o.complete=1
              AND o.return_long_pct IS NOT NULL
              AND o.horizon_sessions IN (2,3,5)
              AND o.observation_date IS NOT NULL
              AND TRIM(COALESCE(f.ticker,''))<>''
        )
        SELECT observation_id,horizon_sessions,observation_date
        FROM ranked
        WHERE rn=1
        ORDER BY horizon_sessions,observation_date,observation_id
        """
    ).fetchall()

    ids: dict[str, list[int]] = {str(h): [] for h in HORIZONS}
    dates: dict[str, set[str]] = {str(h): set() for h in HORIZONS}
    for row in rows:
        horizon = int(row[1] or 0)
        if horizon not in HORIZONS:
            continue
        ids[str(horizon)].append(int(row[0]))
        dates[str(horizon)].add(str(row[2] or "")[:10])
    return ids, {key: sorted(values) for key, values in dates.items()}


def _operational_run_manifest(conn) -> tuple[list[dict], dict]:
    mature_rows = conn.execute(
        """SELECT DISTINCT observation_date
           FROM observation_outcomes
           WHERE complete=1 AND horizon_sessions=5
           ORDER BY observation_date"""
    ).fetchall()
    mature_dates = [str(row[0])[:10] for row in mature_rows if row[0]]
    if not mature_dates:
        return [], {"mature_5d_dates": 0, "selected_dates": 0}

    stats: list[dict] = []
    for start in range(0, len(mature_dates), 300):
        dates = mature_dates[start:start + 300]
        ph = ",".join("?" for _ in dates)
        stats.extend(
            dict(row)
            for row in conn.execute(
                f"""SELECT snapshot_date,source_scanner_run_id,COUNT(*) AS rows,
                           MAX(observed_at_madrid) AS observed_at_madrid
                    FROM feature_observations
                    WHERE snapshot_date IN ({ph})
                    GROUP BY snapshot_date,source_scanner_run_id
                    ORDER BY snapshot_date,source_scanner_run_id""",
                dates,
            ).fetchall()
        )

    by_date: dict[str, list[dict]] = defaultdict(list)
    for row in stats:
        day = str(row.get("snapshot_date") or "")[:10]
        run_id = int(row.get("source_scanner_run_id") or 0)
        if day and run_id > 0:
            by_date[day].append(row)

    selected: list[dict] = []
    for day, runs in sorted(by_date.items()):
        max_rows = max(int(row.get("rows") or 0) for row in runs)
        threshold = max(4, int(math.ceil(max_rows * 0.50)))
        eligible = [row for row in runs if int(row.get("rows") or 0) >= threshold]
        if not eligible:
            continue
        chosen = max(eligible, key=lambda row: int(row.get("source_scanner_run_id") or 0))
        selected.append({
            "date": day,
            "source_scanner_run_id": int(chosen.get("source_scanner_run_id") or 0),
            "rows_at_anchor": int(chosen.get("rows") or 0),
            "observed_at_madrid": chosen.get("observed_at_madrid"),
        })
    return selected, {
        "mature_5d_dates": len(mature_dates),
        "selected_dates": len(selected),
        "selection_rule": "latest raw Scanner run with >=50% of daily max row-count and >=4 rows",
    }


def _build_manifest(*, snapshot_id: str, producer_version: str) -> dict:
    conn = connect(research_db_path(config.ALERTS_DB), readonly=True)
    try:
        canonical_ids, canonical_dates = _canonical_observation_manifest(conn)
        run_keys, run_diag = _operational_run_manifest(conn)
    finally:
        conn.close()

    normalized_run_keys = sorted(
        (
            str(row.get("date") or "")[:10],
            int(row.get("source_scanner_run_id") or 0),
        )
        for row in run_keys
    )
    canonical_hash_payload = {
        str(h): sorted(int(v) for v in (canonical_ids.get(str(h)) or []))
        for h in HORIZONS
    }
    all_dates = sorted({
        date
        for values in canonical_dates.values()
        for date in values
    } | {day for day, _run in normalized_run_keys})
    manifest = {
        "benchmark_id": BENCHMARK_ID,
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "created_from_frozen_snapshot_id": str(snapshot_id or ""),
        "producer_version": str(producer_version or ""),
        "policy": {
            "immutable_after_creation": True,
            "live_strategy_mutation": False,
            "canonical_rows_frozen_by_observation_id": True,
            "operational_population_frozen_by_date_and_scanner_run_id": True,
            "new_history_never_changes_fixed_benchmark_membership": True,
            "rolling_latest_remains_separate": True,
        },
        "canonical_observation_ids_by_horizon": canonical_hash_payload,
        "canonical_dates_by_horizon": canonical_dates,
        "operational_run_keys": run_keys,
        "anchor": {
            "first_date": all_dates[0] if all_dates else None,
            "last_date": all_dates[-1] if all_dates else None,
            "all_dates": len(all_dates),
            "canonical_rows_by_horizon": {
                str(h): len(canonical_hash_payload.get(str(h)) or [])
                for h in HORIZONS
            },
            **run_diag,
        },
        "integrity": {
            "canonical_ids_sha256": _sha(canonical_hash_payload),
            "operational_run_keys_sha256": _sha(normalized_run_keys),
        },
    }
    manifest["integrity"]["manifest_payload_sha256"] = _sha({
        "benchmark_id": manifest["benchmark_id"],
        "schema_version": manifest["schema_version"],
        "canonical_ids_sha256": manifest["integrity"]["canonical_ids_sha256"],
        "operational_run_keys_sha256": manifest["integrity"]["operational_run_keys_sha256"],
        "anchor": manifest["anchor"],
    })
    return manifest


def ensure_economic_benchmark(*, snapshot_id: str, producer_version: str) -> dict:
    """Load the frozen benchmark, or create it once from the current frozen T0."""
    live = benchmark_live_path()
    frozen = benchmark_frozen_path()

    if frozen.exists():
        existing = _load(frozen)
        validation = _validate_manifest(existing)
        if not validation["valid"]:
            raise RuntimeError(
                "Economic benchmark exists but failed integrity validation: "
                + ",".join(validation["errors"])
            )
        return {
            **existing,
            "runtime_status": "REUSED_FROZEN_BENCHMARK",
            "runtime_validation": validation,
        }

    # Outside a research snapshot, or for a benchmark created earlier in the
    # same process, a valid LIVE manifest can still be reused. During the first
    # frozen run research_input_path points at a deliberately missing frozen
    # destination, so this branch only applies when there is no separate map.
    if live.exists() and frozen == live:
        existing = _load(live)
        validation = _validate_manifest(existing)
        if not validation["valid"]:
            raise RuntimeError(
                "LIVE economic benchmark failed integrity validation: "
                + ",".join(validation["errors"])
            )
        return {
            **existing,
            "runtime_status": "REUSED_LIVE_BENCHMARK",
            "runtime_validation": validation,
        }

    manifest = _build_manifest(snapshot_id=snapshot_id, producer_version=producer_version)
    validation = _validate_manifest(manifest)
    if not validation["valid"]:
        raise RuntimeError(
            "New economic benchmark failed integrity validation: "
            + ",".join(validation["errors"])
        )

    # Never overwrite an already-published benchmark. A race should fail closed.
    if live.exists():
        existing = _load(live)
        existing_validation = _validate_manifest(existing)
        if existing_validation["valid"]:
            manifest = existing
            validation = existing_validation
        else:
            raise RuntimeError("Refusing to overwrite invalid existing economic benchmark.")
    else:
        _atomic_write_json(live, manifest)

    # The first 6.2.16 replay starts after the frozen snapshot was created, so
    # the snapshot mapping initially points to a non-existent file. Copy the
    # manifest derived from that frozen DB into the mapped destination as well.
    # This keeps the replay internally frozen and makes cache watermarks honest.
    if frozen != live and not frozen.exists():
        _atomic_write_json(frozen, manifest)

    return {
        **manifest,
        "runtime_status": "CREATED_FROM_FROZEN_T0",
        "runtime_validation": validation,
    }


def compact_benchmark_summary(manifest: dict) -> dict:
    canonical = manifest.get("canonical_observation_ids_by_horizon") or {}
    dates = manifest.get("canonical_dates_by_horizon") or {}
    return {
        "benchmark_id": manifest.get("benchmark_id"),
        "schema_version": manifest.get("schema_version"),
        "runtime_status": manifest.get("runtime_status"),
        "created_at_utc": manifest.get("created_at_utc"),
        "created_from_frozen_snapshot_id": manifest.get("created_from_frozen_snapshot_id"),
        "producer_version": manifest.get("producer_version"),
        "anchor": manifest.get("anchor") or {},
        "canonical_rows_by_horizon": {
            str(h): len(canonical.get(str(h)) or [])
            for h in HORIZONS
        },
        "canonical_dates_by_horizon": {
            str(h): len(dates.get(str(h)) or [])
            for h in HORIZONS
        },
        "operational_run_keys": len(manifest.get("operational_run_keys") or []),
        "integrity": manifest.get("integrity") or {},
        "validation": manifest.get("runtime_validation") or _validate_manifest(manifest),
        "policy": manifest.get("policy") or {},
    }


def canonical_id_sets(manifest: dict) -> dict[int, set[int]]:
    source = manifest.get("canonical_observation_ids_by_horizon") or {}
    return {
        int(h): {int(value) for value in (source.get(str(h)) or [])}
        for h in HORIZONS
    }


def operational_run_key_set(manifest: dict) -> set[tuple[str, int]]:
    return {
        (
            str(row.get("date") or "")[:10],
            int(row.get("source_scanner_run_id") or 0),
        )
        for row in (manifest.get("operational_run_keys") or [])
        if str(row.get("date") or "")[:10] and int(row.get("source_scanner_run_id") or 0) > 0
    }


FEATURE_BENCHMARK_ID = "TMAD_ECONOMIC_FEATURE_OBSERVATION_BENCHMARK_V1"


def feature_benchmark_live_path() -> Path:
    return Path(config.RUNTIME_ROOT) / "research_benchmarks" / "economic_feature_observation_benchmark_v1.json"


def feature_benchmark_frozen_path() -> Path:
    return research_input_path(feature_benchmark_live_path())


def _validate_feature_benchmark(value: dict, parent_manifest: dict) -> dict:
    errors: list[str] = []
    if value.get("benchmark_id") != FEATURE_BENCHMARK_ID:
        errors.append("FEATURE_BENCHMARK_ID_MISMATCH")
    parent_hash = str((parent_manifest.get("integrity") or {}).get("manifest_payload_sha256") or "")
    if str(value.get("parent_manifest_payload_sha256") or "") != parent_hash:
        errors.append("PARENT_BENCHMARK_HASH_MISMATCH")
    ids = value.get("feature_observation_ids") or []
    if not isinstance(ids, list) or not ids:
        errors.append("MISSING_FEATURE_OBSERVATION_IDS")
    run_keys = value.get("run_keys") or []
    expected_run_keys = {
        (str(row.get("date") or "")[:10], int(row.get("source_scanner_run_id") or 0))
        for row in (parent_manifest.get("operational_run_keys") or [])
        if str(row.get("date") or "")[:10] and int(row.get("source_scanner_run_id") or 0) > 0
    }
    actual_run_keys = {
        (str(row.get("date") or "")[:10], int(row.get("source_scanner_run_id") or 0))
        for row in run_keys
        if str(row.get("date") or "")[:10] and int(row.get("source_scanner_run_id") or 0) > 0
    }
    if actual_run_keys != expected_run_keys:
        errors.append("RUN_KEY_SET_MISMATCH")
    if any(int(row.get("feature_observation_count") or 0) <= 0 for row in run_keys):
        errors.append("EMPTY_RUN_IN_FEATURE_BENCHMARK")
    run_hash_payload = [
        [
            str(row.get("date") or "")[:10],
            int(row.get("source_scanner_run_id") or 0),
            int(row.get("feature_observation_count") or 0),
            str(row.get("feature_observation_ids_sha256") or ""),
        ]
        for row in run_keys
    ]
    stored_run_hash = str((value.get("integrity") or {}).get("run_keys_sha256") or "")
    if stored_run_hash and _sha(run_hash_payload) != stored_run_hash:
        errors.append("FEATURE_RUN_HASH_MISMATCH")
    if sum(int(row.get("feature_observation_count") or 0) for row in run_keys) != len(set(int(v) for v in ids)):
        errors.append("FEATURE_ROW_COUNT_MISMATCH")
    expected_hash = str((value.get("integrity") or {}).get("feature_observation_ids_sha256") or "")
    current_hash = _sha(sorted(int(v) for v in ids)) if ids else ""
    if expected_hash and current_hash != expected_hash:
        errors.append("FEATURE_OBSERVATION_ID_HASH_MISMATCH")
    return {"valid": not errors, "errors": errors}


def _build_feature_benchmark(parent_manifest: dict, *, snapshot_id: str, producer_version: str) -> dict:
    wanted = {
        (str(row.get("date") or "")[:10], int(row.get("source_scanner_run_id") or 0))
        for row in (parent_manifest.get("operational_run_keys") or [])
        if str(row.get("date") or "")[:10] and int(row.get("source_scanner_run_id") or 0) > 0
    }
    run_ids = sorted({run_id for _day_value, run_id in wanted})
    ids: list[int] = []
    per_run: dict[tuple[str, int], list[int]] = defaultdict(list)
    conn = connect(research_db_path(config.ALERTS_DB), readonly=True)
    try:
        for start in range(0, len(run_ids), 300):
            batch = run_ids[start:start + 300]
            if not batch:
                continue
            ph = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""SELECT id,snapshot_date,source_scanner_run_id
                    FROM feature_observations
                    WHERE source_scanner_run_id IN ({ph})
                    ORDER BY snapshot_date,source_scanner_run_id,id""",
                batch,
            ).fetchall()
            for row in rows:
                day = str(row[1] or "")[:10]
                run_id = int(row[2] or 0)
                if (day, run_id) not in wanted:
                    continue
                obs_id = int(row[0])
                ids.append(obs_id)
                per_run[(day, run_id)].append(obs_id)
    finally:
        conn.close()

    ids = sorted(set(ids))
    run_keys = [
        {
            "date": day,
            "source_scanner_run_id": run_id,
            "feature_observation_count": len(sorted(set(per_run.get((day, run_id), [])))),
            "feature_observation_ids_sha256": _sha(sorted(set(per_run.get((day, run_id), [])))),
        }
        for day, run_id in sorted(wanted)
    ]
    payload = {
        "benchmark_id": FEATURE_BENCHMARK_ID,
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "created_from_frozen_snapshot_id": str(snapshot_id or ""),
        "producer_version": str(producer_version or ""),
        "parent_benchmark_id": parent_manifest.get("benchmark_id"),
        "parent_manifest_payload_sha256": (parent_manifest.get("integrity") or {}).get("manifest_payload_sha256"),
        "feature_observation_ids": ids,
        "run_keys": run_keys,
        "integrity": {
            "feature_observation_ids_sha256": _sha(ids),
            "run_keys_sha256": _sha([
                [row["date"], row["source_scanner_run_id"], row["feature_observation_count"], row["feature_observation_ids_sha256"]]
                for row in run_keys
            ]),
        },
        "policy": {
            "immutable_after_creation": True,
            "exact_point_in_time_feature_rows_frozen": True,
            "live_strategy_mutation": False,
        },
    }
    payload["integrity"]["manifest_payload_sha256"] = _sha({
        "benchmark_id": payload["benchmark_id"],
        "parent_manifest_payload_sha256": payload["parent_manifest_payload_sha256"],
        "feature_observation_ids_sha256": payload["integrity"]["feature_observation_ids_sha256"],
        "run_keys_sha256": payload["integrity"]["run_keys_sha256"],
    })
    return payload


def ensure_feature_observation_benchmark(
    parent_manifest: dict,
    *,
    snapshot_id: str,
    producer_version: str,
) -> dict:
    live = feature_benchmark_live_path()
    frozen = feature_benchmark_frozen_path()

    if frozen.exists():
        existing = _load(frozen)
        validation = _validate_feature_benchmark(existing, parent_manifest)
        if not validation["valid"]:
            raise RuntimeError(
                "Frozen feature benchmark failed integrity validation: "
                + ",".join(validation["errors"])
            )
        return {**existing, "runtime_status": "REUSED_FROZEN_FEATURE_BENCHMARK", "runtime_validation": validation}

    if live.exists() and frozen == live:
        existing = _load(live)
        validation = _validate_feature_benchmark(existing, parent_manifest)
        if not validation["valid"]:
            raise RuntimeError(
                "LIVE feature benchmark failed integrity validation: "
                + ",".join(validation["errors"])
            )
        return {**existing, "runtime_status": "REUSED_LIVE_FEATURE_BENCHMARK", "runtime_validation": validation}

    payload = _build_feature_benchmark(
        parent_manifest,
        snapshot_id=snapshot_id,
        producer_version=producer_version,
    )
    validation = _validate_feature_benchmark(payload, parent_manifest)
    if not validation["valid"]:
        raise RuntimeError(
            "New feature benchmark failed integrity validation: "
            + ",".join(validation["errors"])
        )

    if live.exists():
        existing = _load(live)
        existing_validation = _validate_feature_benchmark(existing, parent_manifest)
        if not existing_validation["valid"]:
            raise RuntimeError("Refusing to overwrite invalid existing feature benchmark.")
        payload = existing
        validation = existing_validation
    else:
        _atomic_write_json(live, payload)

    if frozen != live and not frozen.exists():
        _atomic_write_json(frozen, payload)

    return {
        **payload,
        "runtime_status": "CREATED_FROM_FROZEN_T0",
        "runtime_validation": validation,
    }


def compact_feature_benchmark_summary(manifest: dict) -> dict:
    return {
        "benchmark_id": manifest.get("benchmark_id"),
        "runtime_status": manifest.get("runtime_status"),
        "parent_benchmark_id": manifest.get("parent_benchmark_id"),
        "feature_observation_ids": len(manifest.get("feature_observation_ids") or []),
        "run_keys": len(manifest.get("run_keys") or []),
        "integrity": manifest.get("integrity") or {},
        "validation": manifest.get("runtime_validation") or {},
        "policy": manifest.get("policy") or {},
    }


def feature_observation_id_set(manifest: dict) -> set[int]:
    return {int(value) for value in (manifest.get("feature_observation_ids") or [])}
