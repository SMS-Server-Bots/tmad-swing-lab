from __future__ import annotations

from pathlib import Path
import importlib.util
import json
import sqlite3
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
OBS = ROOT / "01_PROGRAMAS" / "observability"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lane(name: str) -> dict:
    return {
        "status": "EVALUABLE",
        "dataset_fingerprint": f"fp-{name}",
        "candidate_count": 53,
        "generation_counts": {"generation_1": 48, "generation_2": 5},
        "stable_train_validation_count": 4,
        "selected_candidate": "N3-M4-S2-R2-I4@TOP3",
        "selected_spec": {
            "limits": {"node": 3, "market": 4, "sector": 2, "relation": 2, "interaction": 4},
            "daily_capacity": 3,
            "drop_top": {},
        },
        "selected_features": {"node": ["node.a"], "market": ["market.a"]},
        "critic": {"status": "PASS", "pass": True},
        "final_test": {
            "control": {
                "precision_pct": 60.0,
                "avg_horizon_close_return_pct": 2.0,
                "horizon_close_profit_factor": 1.5,
            },
            "challenger": {
                "sample": 40,
                "precision_pct": 78.0,
                "avg_horizon_close_return_pct": 5.0,
                "horizon_close_profit_factor": 3.2,
                "avg_mae_pct": 2.1,
            },
            "lift": {"precision_pp": 18.0, "return_pp": 3.0, "pf": 1.7},
            "gate": {"pass": True},
        },
        "historical_followup_candidate": True,
        "performance": {
            "total_elapsed_seconds": 12.5,
            "generation_1_seconds": 10.0,
            "generation_2_seconds": 1.0,
            "critic_and_final_test_seconds": 1.0,
            "registry_seconds": 0.5,
        },
        "registry": {"status": "OK", "rows_written": 53},
        "leaderboard": [{
            "rank": 1,
            "candidate_name": "N3-M4-S2-R2-I4@TOP3",
            "generation": 1,
            "parent": "6.2.48_CONTEXT_SEARCH",
            "stable_train_validation": True,
            "spec": {"daily_capacity": 3},
            "train": {
                "control": {"precision_pct": 59.0},
                "challenger": {"sample": 60, "precision_pct": 80.0},
                "lift": {"precision_pp": 21.0},
                "gate": {"pass": True},
            },
            "validation": {
                "control": {"precision_pct": 60.0},
                "challenger": {"sample": 30, "precision_pct": 79.0},
                "lift": {"precision_pp": 19.0},
                "gate": {"pass": True},
            },
        }],
    }


def main() -> int:
    report = _load("autonomous_report_v6250", ALERTS / "autonomous_research_report.py")
    runtime_export = _load("runtime_export_v6250", OBS / "runtime_data_export.py")

    core = {
        "rolling_latest": {"autonomous_research": _lane("rolling")},
        "fixed_benchmark": {"autonomous_research": _lane("fixed")},
        "decision": {
            "autonomous_research": {
                "status": "EVALUABLE",
                "rolling_selected_candidate": "N3-M4-S2-R2-I4@TOP3",
                "fixed_selected_candidate": "N3-M4-S2-R2-I4@TOP3",
                "same_candidate_across_lanes": True,
                "rolling_final_pass": True,
                "fixed_final_pass": True,
                "dual_lane_survivor": True,
                "next_step": "FREEZE_AUTONOMOUS_CANDIDATE_FOR_PROSPECTIVE_SHADOW",
            }
        },
    }
    summary = report.build_autonomous_research_summary(core, producer_version="6.2.50")
    assert summary["decision"]["dual_lane_survivor"] is True
    assert summary["rolling_latest"]["candidate_count"] == 53
    assert summary["rolling_latest"]["final_test"]["precision_pct"] == 78.0
    assert summary["rolling_latest"]["performance"]["total_elapsed_seconds"] == 12.5
    assert len(summary["rolling_latest"]["leaderboard"]) == 1
    assert summary["policy"]["live_mutation"] is False

    with tempfile.TemporaryDirectory() as tmp:
        data_root = Path(tmp)
        db = data_root / "alerts_bot" / "db" / "live" / "tmad_alerts_adaptive.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        try:
            con.execute(
                "CREATE TABLE runtime_state("
                "state_key TEXT PRIMARY KEY,state_value TEXT,updated_at_utc TEXT)"
            )
            rows = [
                ("historical_replay_status", "running", "2026-10-07T11:00:00+00:00"),
                ("historical_replay_started_at_madrid", "2026-10-07T10:25:29+02:00", "2026-10-07T08:25:29+00:00"),
                ("historical_replay_heartbeat_at_madrid", "2026-10-07T13:00:00+02:00", "2026-10-07T11:00:00+00:00"),
                ("historical_replay_phase", json.dumps({
                    "phase": "OPERATIONAL_VALIDATION",
                    "detail": "building current-policy, lifecycle and Reverse Stress",
                    "pid": 5544,
                }), "2026-10-07T11:00:00+00:00"),
            ]
            con.executemany("INSERT INTO runtime_state VALUES(?,?,?)", rows)
            con.commit()
        finally:
            con.close()
        progress = runtime_export.read_historical_replay_progress(data_root)
        assert progress["status"] == "RUNNING"
        assert progress["phase"]["phase"] == "OPERATIONAL_VALIDATION"
        assert progress["phase"]["pid"] == 5544
        assert progress["live_mutation"] is False

    historical = (ALERTS / "historical_operational_validation.py").read_text(encoding="utf-8-sig")
    assert "latest_autonomous_research_summary.json" in historical
    exporter = (OBS / "runtime_data_export.py").read_text(encoding="utf-8-sig")
    assert '"latest_autonomous_research_summary.json"' in exporter
    assert '"historical_replay_progress.json"' in exporter
    lab = (ALERTS / "autonomous_research_lab.py").read_text(encoding="utf-8-sig")
    assert '"total_elapsed_seconds"' in lab
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 50), version

    print(
        "PASS | >=6.2.50 | autonomous research compact observability | "
        "summary + timings + runtime replay progress | LIVE unchanged"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
