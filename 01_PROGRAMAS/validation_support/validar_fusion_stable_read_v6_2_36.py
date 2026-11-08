from __future__ import annotations

from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def main()->int:
    pipeline=(ALERTS/"historical_replay_pipeline.py").read_text(encoding="utf-8-sig")
    runner=(ALERTS/"fusion_gate_research_runner.py").read_text(encoding="utf-8-sig")
    startup=(ALERTS/"fusion_gate_startup_lab.py").read_text(encoding="utf-8-sig")
    ps=(ALERTS/"start-bot.ps1").read_text(encoding="utf-8-sig")
    checkpoint=(ALERTS/"storage_startup_checkpoint.py").read_text(encoding="utf-8-sig")

    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    version_tuple=tuple(int(x) for x in version.split("."))

    # The original 6.2.36 lab used a stable-read watermark rather than a second
    # multi-GB DB copy. 6.2.44+ moves the economic calibration into the already
    # frozen FULL replay, so the stronger T0 snapshot + trusted-price invariant
    # supersedes the detached stable-read economic pass.
    assert "frozen_research_snapshot" not in pipeline
    if version_tuple>=(6,2,44):
        replay=(ALERTS/"historical_replay_once.py").read_text(encoding="utf-8-sig")
        operational=(ALERTS/"historical_operational_validation.py").read_text(encoding="utf-8-sig")
        assert "with frozen_research_snapshot(config" in replay
        assert '"fusion_gate_calibration.py"' in replay
        assert "trusted_reverse_series" in operational
        assert "fixed_benchmark_trusted_series" in operational
        assert "build_fusion_gate_calibration_from_trusted_series" in operational
        assert 'FUSION_OUTCOME_MODE="TRUSTED_REVERSE_SERIES_V1"' in pipeline
        assert "def _load_full_replay_fusion_result" in pipeline
        assert "skipped_no_full_replay_artifact" in pipeline
    else:
        assert "VALID_STABLE_READ_WINDOW" in pipeline
        assert "INVALID_INPUT_DRIFT" in pipeline
        assert "write_sidecar=False" in pipeline
        assert "before != after" in pipeline
        assert "_stable_input_watermark" in pipeline
        assert "stable_input_watermark" in pipeline

        # Mature observation outcomes were the future-label source of the
        # detached 6.2.36-6.2.43 lab.
        assert ("_observation_outcome_price_series" in runner) or ("_mature_h5_outcomes" in runner)
        assert "_all_feature_rows" not in runner
        assert "\n    _price_series," not in runner
        assert "_price_series(tickers)" not in runner
        assert "_layer_outcome_price_series" not in runner
        assert "_full_deep_reverse_canonical" not in runner
        assert "write_sidecar: bool = True" in runner
        assert 'mark("lanes_complete")' in runner

    # Disposable research snapshots left by a killed prior lab are cleaned only
    # after the authoritative historical replay is no longer running.
    assert "_cleanup_orphaned_research_snapshots" in startup
    wait_pos=startup.index('historical_replay_status')
    cleanup_pos=startup.index('removed, freed = _cleanup_orphaned_research_snapshots()')
    assert cleanup_pos > wait_pos

    # Startup storage recovery is a WAL checkpoint only. Never VACUUM/delete DB.
    assert "PRAGMA wal_checkpoint(TRUNCATE)" in checkpoint
    assert "VACUUM" not in checkpoint.upper()
    assert "unlink(" not in checkpoint
    assert "rmtree" not in checkpoint
    assert "storage_startup_checkpoint.py" in ps
    assert "Startup WAL checkpoint" in ps

    # Economics remain unchanged.
    calibration=(ALERTS/"fusion_gate_calibration.py").read_text(encoding="utf-8-sig")
    assert 'CHALLENGER="EARLY_RECOVERY_V1"' in calibration
    assert '"fusion_live_unchanged":True' in calibration

    assert version_tuple>=(6,2,36),version

    print(
        "PASS | >=6.2.36 | FUSION stable-read watermark + mature outcome labels | "
        "no full Alerts snapshot | startup WAL TRUNCATE only | LIVE unchanged"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
