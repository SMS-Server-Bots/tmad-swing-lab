from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

from post_replay_adaptive_handoff import launch_adaptive_after_full


class FakeProcess:
    pid = 4321


def fake_popen(*args, **kwargs):
    return FakeProcess()


def _write_status(output: Path, *, fixed: bool = True) -> None:
    path = output / "historical_validation" / "latest_fast_research_cache_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    lanes = {
        "ROLLING_LATEST": {
            "status": "OK",
            "dataset_fingerprint": "rolling-fp",
            "row_count": 100,
            "producer_version": "6.2.54",
        },
    }
    if fixed:
        lanes["FIXED_BENCHMARK"] = {
            "status": "OK",
            "dataset_fingerprint": "fixed-fp",
            "row_count": 100,
            "producer_version": "6.2.54",
        }
    path.write_text(
        json.dumps(
            {
                "producer_version": "6.2.54",
                "generated_at": "2026-10-08T01:00:00+02:00",
                "lanes": lanes,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        output = root / "03_DATOS" / "alerts_bot" / "output"
        runtime = root / "04_RUNTIME"
        program = root / "01_PROGRAMAS" / "alerts_bot"
        output.mkdir(parents=True, exist_ok=True)
        runtime.mkdir(parents=True, exist_ok=True)
        program.mkdir(parents=True, exist_ok=True)
        (program / "adaptive_research_cycle.py").write_text(
            "print('ok')\n", encoding="utf-8"
        )
        _write_status(output)

        launched = launch_adaptive_after_full(
            replay_mode="FULL",
            strategic_conclusion_allowed=True,
            output_dir=output,
            runtime_root=runtime,
            program_dir=program,
            python_executable=sys.executable,
            producer_version="6.2.54",
            replay_started_at="2026-10-08T00:00:00+02:00",
            enabled=True,
            popen_fn=fake_popen,
        )
        assert launched["status"] == "LAUNCHED", launched
        assert launched["pid"] == 4321
        assert launched["final_test_locked"] is True
        assert launched["live_mutation"] is False
        assert launched["cache_fingerprints"] == {
            "ROLLING_LATEST": "rolling-fp",
            "FIXED_BENCHMARK": "fixed-fp",
        }

        latest = (
            runtime
            / "autonomous_research"
            / "adaptive_cycles"
            / "latest_adaptive_cycle.json"
        )
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(
            json.dumps(
                {
                    "cache": {
                        "ROLLING_LATEST": {
                            "dataset_fingerprint": "rolling-fp"
                        },
                        "FIXED_BENCHMARK": {
                            "dataset_fingerprint": "fixed-fp"
                        },
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        duplicate = launch_adaptive_after_full(
            replay_mode="FULL",
            strategic_conclusion_allowed=True,
            output_dir=output,
            runtime_root=runtime,
            program_dir=program,
            python_executable=sys.executable,
            producer_version="6.2.54",
            replay_started_at="2026-10-08T00:00:00+02:00",
            enabled=True,
            popen_fn=fake_popen,
        )
        assert duplicate["status"] == "SKIPPED_ALREADY_EVALUATED", duplicate

        partial = launch_adaptive_after_full(
            replay_mode="PARTIAL_OPERATIONAL",
            strategic_conclusion_allowed=True,
            output_dir=output,
            runtime_root=runtime,
            program_dir=program,
            python_executable=sys.executable,
            producer_version="6.2.54",
            replay_started_at="2026-10-08T00:00:00+02:00",
            enabled=True,
            popen_fn=fake_popen,
        )
        assert partial["status"] == "SKIPPED_NOT_FULL"

        invalid = launch_adaptive_after_full(
            replay_mode="FULL",
            strategic_conclusion_allowed=False,
            output_dir=output,
            runtime_root=runtime,
            program_dir=program,
            python_executable=sys.executable,
            producer_version="6.2.54",
            replay_started_at="2026-10-08T00:00:00+02:00",
            enabled=True,
            popen_fn=fake_popen,
        )
        assert invalid["status"] == "SKIPPED_INVALID_RESEARCH_BASIS"

        latest.unlink()
        _write_status(output, fixed=False)
        missing = launch_adaptive_after_full(
            replay_mode="FULL",
            strategic_conclusion_allowed=True,
            output_dir=output,
            runtime_root=runtime,
            program_dir=program,
            python_executable=sys.executable,
            producer_version="6.2.54",
            replay_started_at="2026-10-08T00:00:00+02:00",
            enabled=True,
            popen_fn=fake_popen,
        )
        assert missing["status"] == "SKIPPED_CACHE_PAIR_NOT_READY"

    replay = (ALERTS / "historical_replay_once.py").read_text(
        encoding="utf-8-sig"
    )
    config = (ALERTS / "config.py").read_text(encoding="utf-8-sig")
    module = (ALERTS / "post_replay_adaptive_handoff.py").read_text(
        encoding="utf-8-sig"
    )
    assert "launch_adaptive_after_full" in replay
    assert '"adaptive_research_auto_launch"' in replay
    assert "ADAPTIVE_RESEARCH_AUTO_RUN_AFTER_FULL" in config
    assert "adaptive_research_cycle.py" in module
    assert '"final_test_locked": True' in module
    assert '"live_mutation": False' in module

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 54), version

    print(
        "PASS | >=6.2.54 | valid FULL -> detached adaptive validation search | "
        "cache-pair gated + duplicate-safe | FINAL TEST locked | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
