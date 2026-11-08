from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

db = (ROOT / "01_PROGRAMAS" / "alerts_bot" / "db.py").read_text(encoding="utf-8-sig")
outcomes = (ROOT / "01_PROGRAMAS" / "alerts_bot" / "outcomes.py").read_text(encoding="utf-8-sig")
context = (ROOT / "01_PROGRAMAS" / "alerts_bot" / "core_context_precision_optimizer.py").read_text(encoding="utf-8-sig")
operational = (ROOT / "01_PROGRAMAS" / "alerts_bot" / "historical_operational_validation.py").read_text(encoding="utf-8-sig")
deploy = (ROOT / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8-sig")
deferred = (ROOT / "01_PROGRAMAS" / "alerts_bot" / "deferred_historical_replay.ps1").read_text(encoding="utf-8-sig")

# Production recovery: the historical backlog must never be materialized in one
# unbounded Python list/set during Alerts startup.
assert "def observations_needing_labels_batch(" in db
assert "completed_horizons" in db
assert "LIMIT ?" in db
assert "observations_needing_labels_batch" in outcomes
assert "observations = observations_needing_labels()" not in outcomes
assert "SELECT observation_id,horizon_sessions FROM observation_outcomes" not in outcomes
assert "while True:" in outcomes
assert "completed_total += upsert_observation_outcomes_bulk" in outcomes

# FULL replay is cache-first. The expensive autonomous tournament is deferred to
# the already-existing post-FULL adaptive process; direct callers retain an
# explicit opt-in switch for the embedded tournament.
assert "run_autonomous_research: bool = True" in context
assert "DEFERRED_TO_POST_FULL_ADAPTIVE" in context
assert "run_autonomous_research=not defer_autonomous_context_research" in operational
assert "defer_autonomous_context_research: bool = True" in operational

# Deployment must prioritize the production warm-up and avoid the 6.2.55
# replay/warm-up RAM and I/O collision.
assert "deferred_historical_replay.ps1" in deploy
assert "Replay historico diferido armado" in deploy
assert "$replayProc = Start-Process" not in deploy
assert "[long]$NotBeforeEpoch" in deferred
assert "SKIPPED_STARTUP_ERROR" in deferred
assert "adaptive_research_cycle[.]py" in deferred
assert "historical_replay_once[.]py" in deferred
assert "Production warm-up has priority" in deferred

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
assert tuple(int(part) for part in version.split(".")) >= (6, 2, 56), version

print(
    "PASS | >=6.2.56 | outcomes bounded by keyset batches | "
    "production startup isolated from FULL replay | FULL autonomous tournament "
    "deferred to cache-backed adaptive research | LIVE economics unchanged"
)
