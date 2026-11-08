from __future__ import annotations

from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[2]
ALERTS=ROOT/"01_PROGRAMAS"/"alerts_bot"


def main()->int:
    pipeline=(ALERTS/"historical_replay_pipeline.py").read_text(encoding="utf-8-sig")
    startup=(ALERTS/"fusion_gate_startup_lab.py").read_text(encoding="utf-8-sig")
    runner=(ALERTS/"fusion_gate_research_runner.py").read_text(encoding="utf-8-sig")
    version=(ROOT/"VERSION.txt").read_text(encoding="utf-8-sig").strip()
    version_tuple=tuple(int(x) for x in version.split("."))

    assert 'PHASE_KEY="fusion_gate_calibration_phase"' in pipeline
    assert "def run_fusion_only(" in pipeline
    assert "return run_fusion_only(notify_reuse=True)" in pipeline
    assert "flush=True" in pipeline

    if version_tuple>=(6,2,44):
        for phase in (
            "load_full_replay_artifact",
            "republish_full_replay_artifact",
            "complete",
            "skipped_no_full_replay_artifact",
        ):
            assert phase in pipeline,phase
        assert "def _load_full_replay_fusion_result" in pipeline
        assert 'FUSION_OUTCOME_MODE="TRUSTED_REVERSE_SERIES_V1"' in pipeline
        assert "progress=_set_phase" not in pipeline.split("def run_fusion_only",1)[1].split("def main()",1)[0]
    else:
        for phase in (
            "benchmark","inputs_rolling","inputs_benchmark",
            "canonical_universe","rolling_lane","fixed_lane",
            "write_sidecar","complete","error","reused",
        ):
            assert phase in pipeline or phase in runner, phase
        assert ("price_history" in runner) or ("mature_outcomes" in runner)
        assert ("freeze_snapshot" in pipeline) or ("stable_read_start" in pipeline)
        assert "progress=_set_phase" in pipeline

    # Startup path must not call historical.main()/pipeline main anymore.
    assert "run_fusion_only" in startup
    assert "run_pipeline_lab" not in startup
    assert "_can_reuse" not in startup
    assert "_basis" not in startup
    assert "run_fusion_only(notify_reuse=False)" in startup

    # Legacy runner remains testable, but 6.2.44+ no longer uses it for the
    # post-replay economic conclusion.
    tree=ast.parse(runner)
    defs=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="run_fusion_gate_research"]
    assert len(defs)==1
    args=[a.arg for a in defs[0].args.kwonlyargs]
    assert "progress" in args
    assert 'CHALLENGER="EARLY_RECOVERY_V1"' in (ALERTS/"fusion_gate_calibration.py").read_text(encoding="utf-8-sig")

    assert version_tuple>=(6,2,35),version
    print("PASS | >=6.2.35 | FUSION startup observability compatible with FULL replay republisher | LIVE unchanged")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
