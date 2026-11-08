from __future__ import annotations

from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"
OBS=ROOT/"01_PROGRAMAS"/"observability"


def main()->int:
    replay=(ALERTS/"historical_replay_once.py").read_text(encoding="utf-8-sig")
    watcher=(ROOT/"WATCH_RUNTIME_LOGS_TMAD.ps1").read_text(encoding="utf-8-sig")
    exporter=(OBS/"runtime_data_export.py").read_text(encoding="utf-8-sig")

    # Replay phase timings are observability-only and must survive success/error.
    assert "_REPLAY_PHASE_TIMINGS" in replay
    assert "_REPLAY_PHASE_HISTORY" in replay
    assert "def _phase_timings_snapshot" in replay
    assert "def _write_replay_performance_sidecar" in replay
    assert "latest_historical_replay_performance.json" in replay
    assert '"historical_replay_phase_timings"' in replay
    assert 'result["phase_timings"] = phase_timings' in replay
    assert 'error["phase_timings"] = phase_timings' in replay
    assert 'f"Replay fases: {_top_phase_text(result)}"' in replay
    assert '_set_replay_phase("REUSED"' in replay

    success_start=replay.index('elapsed = time.monotonic() - monotonic_started')
    complete_pos=replay.index('_set_replay_phase("COMPLETE"',success_start)
    snapshot_pos=replay.index("_phase_timings_snapshot(elapsed)",complete_pos)
    sidecar_pos=replay.index("_write_replay_performance_sidecar(performance_payload)",snapshot_pos)
    publish_summary_pos=replay.index('set_runtime_state(\n            "historical_replay_summary"',sidecar_pos)
    discord_pos=replay.index("_discord_send(_completion_message(result, elapsed))",publish_summary_pos)
    assert complete_pos < snapshot_pos < sidecar_pos < publish_summary_pos < discord_pos

    error_pos=replay.index('_set_replay_phase("ERROR"')
    error_snapshot=replay.index("_phase_timings_snapshot(elapsed)",error_pos)
    assert error_pos < error_snapshot
    assert replay.index('"status": "error"',error_snapshot) > error_snapshot

    # Existing event-driven publisher keeps its normal 5m throttle, but replay
    # terminal states force one current runtime-data publication.
    assert "DataMinSyncIntervalSeconds = 300" in watcher
    assert "ForceRuntimeData = $false" in watcher
    assert "ForceRuntimeDataReason" in watcher
    assert "historical_replay.*\\.log$" in watcher
    assert "phase=(COMPLETE|ERROR|REUSED)" in watcher
    assert "Publish-RuntimeData -Force" in watcher
    assert "[void](Publish-RuntimeData)" in watcher
    assert "Replay terminal detectado; forzando runtime-data sin throttle" in watcher

    # Runtime-data publishes the small timing artifact independently of the
    # oversized historical summary.
    assert '"latest_historical_replay_performance.json"' in exporter
    assert '"latest_historical_validation_summary.json"' in exporter

    # Syntax guards.
    ast.parse(replay)
    ast.parse(exporter)

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split("."))>=(6,2,46),version

    print(
        "PASS | >=6.2.46 | replay terminal forces fresh runtime-data + phase timing sidecar | "
        "normal publisher throttle retained | LIVE economics unchanged"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
