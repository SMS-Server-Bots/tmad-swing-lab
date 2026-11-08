from __future__ import annotations

"""Fail-soft post-FULL handoff into validation-only adaptive research.

This module never evaluates candidates itself. It only verifies that a valid FULL
has produced both frozen fast-research caches, avoids duplicate work for an
already-consumed cache pair, and launches adaptive_research_cycle.py in a
separate process.

Safety invariants:
- only after a strategically valid FULL replay;
- requires both rolling/fixed cache status=OK;
- adaptive child is validation-only and keeps FINAL TEST locked;
- no LIVE mutation, deployment or promotion;
- replay completion never fails because this optional handoff fails.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Any

MODE = "POST_FULL_ADAPTIVE_HANDOFF_V1"
LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _cache_pair(
    output_dir: Path,
    *,
    producer_version: str,
    replay_started_at: str,
) -> tuple[dict[str, str], dict]:
    path = Path(output_dir) / "historical_validation" / "latest_fast_research_cache_status.json"
    payload = _read_json(path)
    lanes = payload.get("lanes") or {}
    fingerprints: dict[str, str] = {}
    diagnostics: dict[str, Any] = {
        "status_path": str(path),
        "status_file_exists": path.is_file(),
        "producer_version": payload.get("producer_version"),
        "generated_at": payload.get("generated_at"),
        "lane_status": {},
    }
    if str(payload.get("producer_version") or "") != str(producer_version):
        diagnostics["reason"] = "producer_version_mismatch"
        return {}, diagnostics
    try:
        generated = datetime.fromisoformat(str(payload.get("generated_at") or ""))
        started = datetime.fromisoformat(str(replay_started_at or ""))
        if generated < started:
            diagnostics["reason"] = "cache_status_predates_current_replay"
            return {}, diagnostics
    except Exception:
        diagnostics["reason"] = "cache_status_timestamp_unverifiable"
        return {}, diagnostics

    for lane in LANES:
        row = lanes.get(lane) or {}
        status = str(row.get("status") or "").upper()
        fingerprint = str(row.get("dataset_fingerprint") or "")
        diagnostics["lane_status"][lane] = {
            "status": status or None,
            "dataset_fingerprint": fingerprint or None,
            "row_count": row.get("row_count"),
            "producer_version": row.get("producer_version"),
        }
        if status != "OK" or not fingerprint:
            return {}, diagnostics
        fingerprints[lane] = fingerprint
    return fingerprints, diagnostics


def _latest_cycle_matches(runtime_root: Path, fingerprints: dict[str, str]) -> bool:
    path = (
        Path(runtime_root)
        / "autonomous_research"
        / "adaptive_cycles"
        / "latest_adaptive_cycle.json"
    )
    payload = _read_json(path)
    cache = payload.get("cache") or {}
    return bool(
        fingerprints
        and all(
            str((cache.get(lane) or {}).get("dataset_fingerprint") or "")
            == str(fingerprints.get(lane) or "")
            for lane in LANES
        )
    )


def launch_adaptive_after_full(
    *,
    replay_mode: str,
    strategic_conclusion_allowed: bool,
    output_dir: Path,
    runtime_root: Path,
    program_dir: Path,
    python_executable: str,
    producer_version: str,
    replay_started_at: str,
    enabled: bool = True,
    popen_fn: Callable[..., Any] = subprocess.Popen,
) -> dict:
    base = {
        "mode": MODE,
        "producer_version": str(producer_version),
        "replay_mode": str(replay_mode),
        "enabled": bool(enabled),
        "live_mutation": False,
        "final_test_locked": True,
        "auto_deploy": False,
        "auto_promote_live": False,
    }
    if not enabled:
        return {**base, "status": "DISABLED"}
    if str(replay_mode).upper() != "FULL":
        return {**base, "status": "SKIPPED_NOT_FULL"}
    if not bool(strategic_conclusion_allowed):
        return {**base, "status": "SKIPPED_INVALID_RESEARCH_BASIS"}

    fingerprints, cache_diag = _cache_pair(
        Path(output_dir),
        producer_version=str(producer_version),
        replay_started_at=str(replay_started_at),
    )
    if not fingerprints:
        return {
            **base,
            "status": "SKIPPED_CACHE_PAIR_NOT_READY",
            "cache": cache_diag,
        }
    if _latest_cycle_matches(Path(runtime_root), fingerprints):
        return {
            **base,
            "status": "SKIPPED_ALREADY_EVALUATED",
            "cache_fingerprints": fingerprints,
        }

    runner = Path(program_dir) / "adaptive_research_cycle.py"
    if not runner.is_file():
        return {
            **base,
            "status": "ERROR_RUNNER_MISSING",
            "runner": str(runner),
            "cache_fingerprints": fingerprints,
        }

    log_dir = Path(runtime_root) / "autonomous_research" / "adaptive_cycles"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"auto_adaptive_{stamp}.log"
    env = os.environ.copy()
    env["TMAD_POST_FULL_ADAPTIVE_HANDOFF"] = "1"

    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

    try:
        with log_path.open("ab", buffering=0) as handle:
            process = popen_fn(
                [str(python_executable), "-u", str(runner)],
                cwd=str(program_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                close_fds=True,
            )
        return {
            **base,
            "status": "LAUNCHED",
            "launched_at": _now(),
            "pid": int(getattr(process, "pid", 0) or 0),
            "runner": str(runner),
            "log_path": str(log_path),
            "cache_fingerprints": fingerprints,
            "cache": cache_diag,
        }
    except Exception as exc:
        return {
            **base,
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "runner": str(runner),
            "log_path": str(log_path),
            "cache_fingerprints": fingerprints,
        }
