from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import time

import config

from db import get_runtime_state, set_runtime_state
from historical_replay_pipeline import mark_fusion_sidecar_pending, run_fusion_only
from time_utils import ts_madrid_str

STARTUP_STATUS_KEY="fusion_gate_startup_status"
STARTUP_META_KEY="fusion_gate_startup_snapshot"
DEFAULT_REPLAY_TIMEOUT_SECONDS=28800


def _record_startup_status(status: str, **details) -> None:
    payload={
        "status":str(status),
        "recorded_at_madrid":ts_madrid_str(),
        "producer_version":str(getattr(config,"BOT_VERSION","")),
        "live_mutation":False,
        **details,
    }
    try:
        set_runtime_state(STARTUP_STATUS_KEY,str(status))
        set_runtime_state(STARTUP_META_KEY,json.dumps(payload,ensure_ascii=False,default=str))
    except Exception as exc:
        print(f"WARN | FUSION startup status write failed | {type(exc).__name__}: {exc}",flush=True)
    print(f"STATUS | FUSION startup lab | {status} | {json.dumps(details,ensure_ascii=False,default=str)}",flush=True)


def _cleanup_orphaned_research_snapshots() -> tuple[int, int]:
    root = Path(config.RUNTIME_ROOT) / "research_snapshots"
    if not root.exists():
        return 0, 0
    removed = 0
    freed = 0
    for child in list(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            freed += sum(
                item.stat().st_size
                for item in child.rglob("*")
                if item.is_file()
            )
        except Exception:
            pass
        shutil.rmtree(child, ignore_errors=True)
        if not child.exists():
            removed += 1
    return removed, freed


def _wait_ready(path: Path, timeout: int) -> bool:
    deadline=time.time()+max(1,timeout)
    while time.time()<deadline:
        if path.exists():
            return True
        time.sleep(2)
    return path.exists()


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--ready-file",required=True)
    p.add_argument("--ready-timeout",type=int,default=180)
    p.add_argument("--replay-timeout",type=int,default=DEFAULT_REPLAY_TIMEOUT_SECONDS)
    p.add_argument("--grace",type=int,default=75)
    args=p.parse_args()

    if not _wait_ready(Path(args.ready_file),args.ready_timeout):
        _record_startup_status(
            "SKIPPED_DB_READY_TIMEOUT",
            ready_file=str(args.ready_file),
            ready_timeout_seconds=int(args.ready_timeout),
        )
        return 0

    mark_fusion_sidecar_pending("WAITING_FOR_HISTORICAL_REPLAY")
    _record_startup_status(
        "WAITING_FOR_HISTORICAL_REPLAY",
        replay_timeout_seconds=int(args.replay_timeout),
    )
    time.sleep(max(0,args.grace))
    deadline=time.time()+max(60,args.replay_timeout)
    while str(get_runtime_state("historical_replay_status") or "").lower()=="running":
        if time.time()>=deadline:
            _record_startup_status(
                "SKIPPED_REPLAY_TIMEOUT",
                replay_timeout_seconds=int(args.replay_timeout),
                historical_replay_status=str(get_runtime_state("historical_replay_status") or ""),
            )
            return 0
        time.sleep(30)

    removed, freed = _cleanup_orphaned_research_snapshots()
    if removed:
        print(
            f"CLEANUP | orphan research snapshots | dirs={removed} freed_mb={freed/(1024*1024):.1f}",
            flush=True,
        )

    _record_startup_status("RUNNING_FUSION_ONLY")
    rc=run_fusion_only(notify_reuse=False)
    _record_startup_status(
        "COMPLETED" if int(rc)==0 else "FAILED",
        return_code=int(rc),
    )
    print(f"DONE | FUSION startup lab | rc={rc}", flush=True)
    return int(rc)


if __name__=="__main__":
    raise SystemExit(main())
