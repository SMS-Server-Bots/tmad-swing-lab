from __future__ import annotations

from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def main()->int:
    startup=(ALERTS/"fusion_gate_startup_lab.py").read_text(encoding="utf-8-sig")
    pipeline=(ALERTS/"historical_replay_pipeline.py").read_text(encoding="utf-8-sig")
    start_ps=(ALERTS/"start-bot.ps1").read_text(encoding="utf-8-sig")

    # 6.2.41 FULL took 4h49m51s, longer than the old four-hour wait.
    assert "DEFAULT_REPLAY_TIMEOUT_SECONDS=28800" in startup
    assert "'--replay-timeout','28800'" in start_ps
    assert '"SKIPPED_REPLAY_TIMEOUT"' in startup
    assert "set_runtime_state(STARTUP_STATUS_KEY" in startup
    assert 'STARTUP_META_KEY="fusion_gate_startup_snapshot"' in startup
    assert "mark_fusion_sidecar_pending" in startup
    assert '"status":"PENDING_FRESH_RUN"' in pipeline
    assert '"strategic_conclusion_allowed":False' in pipeline

    # Sidecars must identify the active producer and freshness. 6.2.44+
    # supersedes the retained-row direct-H5 lab with the FULL trusted replay lane.
    assert "FUSION_ARTIFACT_SCHEMA_VERSION=" in pipeline
    schema=int(pipeline.split("FUSION_ARTIFACT_SCHEMA_VERSION=",1)[1].splitlines()[0].strip())
    assert schema>=2,schema
    assert "FUSION_OUTCOME_MODE=" in pipeline
    assert '"producer_version":historical._production_version()' in pipeline
    assert '"generated_at_madrid"' in pipeline
    assert '"published_at_madrid"' in pipeline
    assert '"outcome_mode":FUSION_OUTCOME_MODE' in pipeline
    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    version_tuple=tuple(int(x) for x in version.split("."))
    if version_tuple>=(6,2,44):
        assert "_write_fusion_sidecar(result,reused=True)" in pipeline
    else:
        assert "_write_fusion_sidecar(snap,reused=True)" in pipeline

    if version_tuple>=(6,2,44):
        assert 'FUSION_OUTCOME_MODE="TRUSTED_REVERSE_SERIES_V1"' in pipeline
        assert "def _load_full_replay_fusion_result" in pipeline
        assert '"source") or "")!="FULL_HISTORICAL_REPLAY"' in pipeline
        assert 'set_runtime_state("fusion_gate_calibration_status","reused_full_replay")' in pipeline
        assert "skipped_no_full_replay_artifact" in pipeline
        assert "result,reason=_load_full_replay_fusion_result()" in pipeline
    else:
        assert 'FUSION_OUTCOME_MODE="DIRECT_MATURE_OBSERVATION_OUTCOMES_V1"' in pipeline
        validity_pos=pipeline.index(
            'result["research_validity"]={\n'
            '            "status":"VALID_STABLE_READ_WINDOW"'
        )
        publish_pos=pipeline.index("_write_fusion_sidecar(result,reused=False)",validity_pos)
        assert validity_pos < publish_pos

    # Atomic replacement prevents runtime-data from reading partial JSON.
    assert 'tmp=target.with_suffix(target.suffix+".tmp")' in pipeline
    assert "tmp.replace(target)" in pipeline

    # Candidate/economic semantics remain untouched.
    calibration=(ALERTS/"fusion_gate_calibration.py").read_text(encoding="utf-8-sig")
    assert 'CHALLENGER="EARLY_RECOVERY_V1"' in calibration
    runner=(ALERTS/"fusion_gate_research_runner.py").read_text(encoding="utf-8-sig")
    assert "_mature_h5_outcomes" in runner
    assert "mature_observation_outcomes_aggregate_columns" in runner
    assert "_observation_outcome_price_series" not in runner

    tree=ast.parse(pipeline)
    assert any(
        isinstance(node,ast.FunctionDef) and node.name=="_write_fusion_sidecar"
        for node in ast.walk(tree)
    )

    assert version_tuple>=(6,2,42),version

    print(
        "PASS | >=6.2.42 | FUSION 8h orchestration + explicit timeout + "
        "self-identifying complete sidecar | LIVE unchanged"
    )
    return 0


if __name__=="__main__":
    raise SystemExit(main())
