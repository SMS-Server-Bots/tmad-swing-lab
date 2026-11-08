from __future__ import annotations

"""Run TMAD autonomous research from a frozen contextual cache.

Default mode is SCREEN_ONLY. The runner evaluates TRAIN + VALIDATION + temporal
stability/critic without exposing FINAL TEST. When both rolling and fixed lanes
share one stability-surviving consensus candidate, --unlock-final-test can
consume both frozen lockboxes exactly once for that SAME candidate.

Research-only: no LIVE mutation, deployment or automatic promotion.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import config
from autonomous_research_cache import (
    cache_filename,
    load_fast_research_cache,
    split_cached_rows,
)
from autonomous_research_lab import (
    combine_autonomous_research_lanes,
    evaluate_explicit_candidate_final_test,
    run_autonomous_research_lane,
)
from core_context_precision_optimizer import (
    _comparison,
    _fast_cache_contract_hash,
    _fit_train_models,
)

MODE = "FAST_AUTONOMOUS_RESEARCH_RUNNER_V2"
DUAL_LANES = ("ROLLING_LATEST", "FIXED_BENCHMARK")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except Exception:
            pass


def _ledger_path() -> Path:
    return config.RUNTIME_ROOT / "autonomous_research" / "final_test_access.jsonl"


def _lockbox_key(cache_fingerprint: str, lane: str) -> str:
    return "|".join((str(cache_fingerprint), str(lane).upper()))


def _read_lockbox_keys() -> set[str]:
    path = _ledger_path()
    if not path.is_file():
        return set()
    keys: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                key = str(row.get("lockbox_key") or "")
                if key:
                    keys.add(key)
    except Exception:
        return keys
    return keys


def _append_lockbox_access(payload: dict) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except Exception:
            pass


def _load_lane(lane: str) -> dict:
    root = config.RUNTIME_ROOT / "autonomous_research" / "cache"
    path = root / cache_filename(lane)
    return load_fast_research_cache(
        path,
        expected_contract_hash=_fast_cache_contract_hash(),
        verify_fingerprint=bool(
            getattr(config, "FAST_RESEARCH_CACHE_VERIFY_ON_LOAD", True)
        ),
    )


def _screen_cache(
    cache_payload: dict,
    *,
    max_candidates: int,
    top_report: int,
) -> dict:
    train, validation, test = split_cached_rows(cache_payload)
    fitted = _fit_train_models(train)
    result = run_autonomous_research_lane(
        train=train,
        validation=validation,
        test=test,
        fitted=fitted,
        comparison_fn=_comparison,
        lane_label=str(cache_payload.get("lane") or ""),
        registry_path=(
            config.RUNTIME_ROOT / "autonomous_research" / "experiments.sqlite"
            if bool(getattr(config, "AUTONOMOUS_RESEARCH_REGISTRY_ENABLED", True))
            else None
        ),
        max_candidates=max_candidates,
        top_report=top_report,
        touch_final_test=False,
    )
    result["cache"] = {
        "producer_version": cache_payload.get("producer_version"),
        "dataset_fingerprint": cache_payload.get("dataset_fingerprint"),
        "contract_hash": cache_payload.get("contract_hash"),
        "row_count": cache_payload.get("row_count"),
        "split_dates": {
            key: len(values or [])
            for key, values in (cache_payload.get("split_dates") or {}).items()
        },
    }
    result["run_mode"] = "SCREEN_ONLY"
    result["live_mutation"] = False
    return result


def _screen_lane(
    lane: str,
    *,
    max_candidates: int,
    top_report: int,
) -> tuple[dict, dict | None]:
    try:
        cache_payload = _load_lane(lane)
    except Exception as exc:
        return ({
            "mode": MODE,
            "lane": str(lane).upper(),
            "status": "CACHE_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "live_mutation": False,
        }, None)
    return (
        _screen_cache(
            cache_payload,
            max_candidates=max_candidates,
            top_report=top_report,
        ),
        cache_payload,
    )


def _screen_decision(results: dict[str, dict]) -> dict:
    rolling = results.get("ROLLING_LATEST") or {}
    fixed = results.get("FIXED_BENCHMARK") or {}
    if not rolling or not fixed:
        return {
            "mode": "FAST_RESEARCH_SINGLE_LANE_SCREEN_V1",
            "status": "PARTIAL",
            "dual_lane_screen_survivor": False,
            "dual_lane_consensus_candidate": None,
            "next_step": "RUN_BOTH_LANES_BEFORE_FINAL_TEST",
            "promotion_allowed": False,
            "live_mutation": False,
        }
    decision = combine_autonomous_research_lanes(rolling, fixed)
    decision["final_test_requested"] = False
    decision["dual_lane_final_survivor"] = False
    return decision


def _reserve_dual_lockbox(
    cache_payloads: dict[str, dict],
    *,
    consensus_candidate: str,
    consensus_spec: dict,
    used_keys: set[str],
) -> tuple[bool, dict[str, str], dict]:
    keys: dict[str, str] = {}
    for lane in DUAL_LANES:
        cache = cache_payloads.get(lane) or {}
        fp = str(cache.get("dataset_fingerprint") or "")
        if not fp:
            return False, {}, {
                "status": "DENIED_CACHE_FINGERPRINT_MISSING",
                "lane": lane,
            }
        key = _lockbox_key(fp, lane)
        keys[lane] = key
        if key in used_keys:
            return False, keys, {
                "status": "DENIED_ALREADY_ACCESSED",
                "lane": lane,
                "lockbox_key": key,
                "reason": "this frozen lane FINAL TEST has already been consumed",
            }

    # Fail closed: reserve BOTH lockboxes before reading either FINAL TEST.
    # A crash after reservation still consumes the holdout rather than allowing
    # accidental repeated peeking.
    reservation_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    for lane in DUAL_LANES:
        cache = cache_payloads[lane]
        row = {
            "status": "RESERVED",
            "reservation_id": reservation_id,
            "reserved_at": _now(),
            "reserved_at_utc": datetime.now(timezone.utc).isoformat(),
            "lockbox_key": keys[lane],
            "cache_dataset_fingerprint": cache.get("dataset_fingerprint"),
            "lane": lane,
            "consensus_candidate": consensus_candidate,
            "consensus_spec": consensus_spec,
            "live_mutation": False,
        }
        _append_lockbox_access(row)
        used_keys.add(keys[lane])
    return True, keys, {
        "status": "RESERVED",
        "reservation_id": reservation_id,
        "lockbox_keys": keys,
    }


def _evaluate_consensus_lane(
    lane: str,
    cache_payload: dict,
    *,
    spec: dict,
) -> dict:
    train, validation, test = split_cached_rows(cache_payload)
    fitted = _fit_train_models(train)
    result = evaluate_explicit_candidate_final_test(
        train=train,
        validation=validation,
        test=test,
        fitted=fitted,
        comparison_fn=_comparison,
        lane_label=lane,
        spec=spec,
    )
    result["cache"] = {
        "producer_version": cache_payload.get("producer_version"),
        "dataset_fingerprint": cache_payload.get("dataset_fingerprint"),
        "contract_hash": cache_payload.get("contract_hash"),
        "row_count": cache_payload.get("row_count"),
    }
    result["run_mode"] = "FINAL_TEST_UNLOCKED"
    result["final_test_status"] = (
        "EVALUATED" if result.get("status") == "EVALUATED" else "NOT_ELIGIBLE"
    )
    result["live_mutation"] = False
    return result


def _unlock_consensus(
    screens: dict[str, dict],
    cache_payloads: dict[str, dict],
    *,
    used_keys: set[str],
) -> tuple[dict[str, dict], dict]:
    if any(lane not in screens or lane not in cache_payloads for lane in DUAL_LANES):
        return screens, {
            **_screen_decision(screens),
            "final_test_requested": True,
            "final_test_access": {
                "status": "DENIED_REQUIRES_BOTH_LANES",
            },
        }

    screen_decision = combine_autonomous_research_lanes(
        screens["ROLLING_LATEST"],
        screens["FIXED_BENCHMARK"],
    )
    consensus = screen_decision.get("dual_lane_consensus") or {}
    candidate = str(consensus.get("candidate") or "")
    spec = dict(consensus.get("spec") or {})
    if not candidate or not spec:
        return screens, {
            **screen_decision,
            "final_test_requested": True,
            "dual_lane_final_survivor": False,
            "final_test_access": {
                "status": "DENIED_NO_DUAL_LANE_CONSENSUS",
            },
        }

    ok, keys, reservation = _reserve_dual_lockbox(
        cache_payloads,
        consensus_candidate=candidate,
        consensus_spec=spec,
        used_keys=used_keys,
    )
    if not ok:
        denied = {}
        for lane, screen in screens.items():
            denied[lane] = {
                **screen,
                "final_test_access": dict(reservation),
            }
        return denied, {
            **screen_decision,
            "final_test_requested": True,
            "dual_lane_final_survivor": False,
            "final_test_access": reservation,
        }

    evaluated: dict[str, dict] = {}
    for lane in DUAL_LANES:
        result = _evaluate_consensus_lane(
            lane,
            cache_payloads[lane],
            spec=spec,
        )
        access = {
            "status": "COMPLETED",
            "reservation_id": reservation.get("reservation_id"),
            "completed_at": _now(),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "lockbox_key": keys[lane],
            "cache_dataset_fingerprint": cache_payloads[lane].get("dataset_fingerprint"),
            "lane": lane,
            "consensus_candidate": candidate,
            "evaluated_candidate": result.get("candidate_name"),
            "selection_stable": result.get("candidate_name") == candidate,
            "final_gate_pass": bool(
                ((result.get("final_test") or {}).get("gate") or {}).get("pass")
            ),
            "live_mutation": False,
        }
        _append_lockbox_access(access)
        result["final_test_access"] = access
        evaluated[lane] = result

    final_dual = bool(
        all(
            evaluated[lane].get("candidate_name") == candidate
            and evaluated[lane].get("historical_followup_candidate")
            for lane in DUAL_LANES
        )
    )
    decision = {
        **screen_decision,
        "final_test_requested": True,
        "dual_lane_final_survivor": final_dual,
        "final_test_access": reservation,
        "next_step": (
            "ELIGIBLE_FOR_PROSPECTIVE_SHADOW_REVIEW"
            if final_dual
            else "KEEP_RESEARCHING"
        ),
    }
    return evaluated, decision


def run_fast_research(
    *,
    lanes: list[str],
    unlock_final_test: bool = False,
    max_candidates: int | None = None,
    top_report: int | None = None,
    persist: bool = True,
) -> dict:
    max_candidates = int(
        max_candidates
        if max_candidates is not None
        else getattr(config, "AUTONOMOUS_RESEARCH_MAX_CANDIDATES", 48)
    )
    top_report = int(
        top_report
        if top_report is not None
        else getattr(config, "AUTONOMOUS_RESEARCH_TOP_REPORT", 20)
    )

    screens: dict[str, dict] = {}
    cache_payloads: dict[str, dict] = {}
    for lane in lanes:
        result, cache = _screen_lane(
            lane,
            max_candidates=max_candidates,
            top_report=top_report,
        )
        lane_key = str(lane).upper()
        screens[lane_key] = result
        if cache is not None:
            cache_payloads[lane_key] = cache

    used = _read_lockbox_keys()
    if unlock_final_test:
        results, decision = _unlock_consensus(
            screens,
            cache_payloads,
            used_keys=used,
        )
        run_mode = "FINAL_TEST_UNLOCKED"
    else:
        results = screens
        decision = _screen_decision(screens)
        run_mode = "SCREEN_ONLY"

    payload = {
        "schema_version": 2,
        "mode": MODE,
        "producer_version": config.BOT_VERSION,
        "generated_at": _now(),
        "run_mode": run_mode,
        "lanes": results,
        "decision": decision,
        "policy": {
            "frozen_cache_only": True,
            "full_replay_required": False,
            "screen_runs_never_touch_final_test": True,
            "explicit_unlock_required_for_final_test": True,
            "dual_lane_consensus_required_for_final_test": True,
            "same_consensus_candidate_tested_in_both_lanes": True,
            "each_frozen_lane_final_test_once": True,
            "lockbox_reserved_before_read": True,
            "live_mutation": False,
            "auto_deploy": False,
            "auto_promote_live": False,
        },
    }

    if persist:
        root = config.RUNTIME_ROOT / "autonomous_research" / "fast_runs"
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        _atomic_json(root / f"fast_research_{stamp}.json", payload)
        _atomic_json(root / "latest_fast_research.json", payload)
        _atomic_json(
            config.OUTPUT_DIR
            / "historical_validation"
            / "latest_fast_autonomous_research.json",
            payload,
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run autonomous TMAD research from frozen contextual caches."
    )
    parser.add_argument(
        "--lane",
        choices=("all", "rolling_latest", "fixed_benchmark"),
        default="all",
    )
    parser.add_argument(
        "--unlock-final-test",
        action="store_true",
        help="Consume rolling+fixed FINAL TEST once for a shared stability consensus.",
    )
    parser.add_argument("--max-candidates", type=int, default=None)
    parser.add_argument("--top-report", type=int, default=None)
    args = parser.parse_args()

    lanes = (
        list(DUAL_LANES)
        if args.lane == "all"
        else [args.lane.upper()]
    )
    payload = run_fast_research(
        lanes=lanes,
        unlock_final_test=bool(args.unlock_final_test),
        max_candidates=args.max_candidates,
        top_report=args.top_report,
        persist=True,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    hard_error = any(
        row.get("status") in {"CACHE_ERROR", "LOCKBOX_SELECTION_MISMATCH"}
        for row in (payload.get("lanes") or {}).values()
    )
    return 2 if hard_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
