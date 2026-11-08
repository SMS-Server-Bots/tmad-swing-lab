from __future__ import annotations

"""Auto stress launcher: offline/idempotency, existing history, no FULL."""

import importlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "alerts_bot"
sys.path.insert(0, str(ALERTS))
import auto_joint_stress as job


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def main() -> int:
    prior = (job.SOURCE, job.STATUS, job.RUNTIME)
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            job.SOURCE = root / "03_DATOS" / "alerts_bot" / "output" / "historical_validation"
            job.STATUS = root / "04_RUNTIME" / "autonomous_research" / "joint_entry_exit_stress"
            job.RUNTIME = root / "04_RUNTIME"
            cache = job.RUNTIME / "autonomous_research" / "cache"
            cache.mkdir(parents=True)
            for filename in job.FILES:
                (cache / filename).write_bytes(b"x" * 101)
            fingerprints = {"ROLLING_LATEST": "hash_roll", "FIXED_BENCHMARK": "hash_fixed"}
            _json(job.SOURCE / "latest_fast_research_cache_status.json", {
                "producer_version": "6.2.60",
                "lanes": {lane: {"status": "OK", "dataset_fingerprint": fingerprints[lane],
                                  "contract_hash": "samecontract"} for lane in job.LANES},
            })
            _json(job.SOURCE / "latest_historical_replay_performance.json", {
                "status": "ok", "producer_version": "6.2.60",
            })
            pair, error = job._cache_pair()
            assert error is None and pair == fingerprints, (pair, error)
            _json(job.SOURCE / "latest_joint_entry_exit_stress.json", {
                "mode": "JOINT_ENTRY_DIRECTION_EXIT_STRESS_V1",
                "decision": {"cost_bps": 20.0},
                "train_search": {
                    "rolling": {"cache": {"fingerprint": "hash_roll"}},
                    "fixed": {"cache": {"fingerprint": "hash_fixed"}},
                },
            })
            assert job._already_done(pair) is True
            _json(job.SOURCE / "latest_joint_entry_exit_stress.json", {
                "mode": "JOINT_ENTRY_DIRECTION_EXIT_STRESS_V1",
                "decision": {"cost_bps": 21.0},
            })
            assert job._already_done(pair) is False
            first = job._lock()
            assert first is not None
            second = job._lock()
            assert second is None, "Concurrent second launcher should not run"
            first.close()
            assert job._lock() is not None
            _json(job.SOURCE / "latest_historical_replay_performance.json", {
                "status": "error", "producer_version": "6.2.60",
            })
            assert job._cache_pair()[1] == "LAST_HISTORICAL_NOT_VALID"
            _json(job.SOURCE / "latest_historical_replay_performance.json", {
                "status": "ok", "producer_version": "6.2.59",
            })
            assert job._cache_pair()[1] == "HISTORICAL_PRODUCER_MISMATCH"
            _json(job.SOURCE.parent / "startup_calibration_status.json", {
                "status": "running",
                "finished_at_madrid": "",
            })
            assert not job._startup_ready()
            _json(job.SOURCE.parent / "startup_calibration_status.json", {
                "status": "calibrated",
                "finished_at_madrid": "2026-10-10T12:00:00+02:00",
            })
            assert job._startup_ready()
    finally:
        job.SOURCE, job.STATUS, job.RUNTIME = prior
    deploy = (ROOT.parent / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8-sig")
    assert "reuseFrozenHistoryForStress" in deploy
    assert "auto_joint_stress.py" in deploy
    assert "latest_fast_research_cache_status.json" in deploy
    assert "latest_historical_replay_performance.json" in deploy
    assert "Replay FULL omitido en 6.2.65" in deploy
    assert "Join-Path $ProductionRoot '04_RUNTIME\\venvs\\alerts_bot\\Scripts\\python.exe'" in deploy
    assert "if ($version -eq '6.2.65' -and -not $NoStart)" in deploy

    print("PASS TMAD 6.2.62: cache/replay validation; single worker lock; skip FULL; detached startup hook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
