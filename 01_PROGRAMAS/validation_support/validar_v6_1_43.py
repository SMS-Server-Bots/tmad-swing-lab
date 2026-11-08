from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
assert tuple(int(part) for part in version.split(".")) >= (6, 1, 43), version


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def assert_contains(relative: str, *needles: str) -> None:
    source = text(relative)
    for needle in needles:
        assert needle in source, f"{relative}: missing {needle!r}"


def test_atomic_gex_bundle() -> None:
    with tempfile.TemporaryDirectory(prefix="tmad_v643_gex_") as td:
        db = Path(td) / "gex.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE gex_summary(gex_run_id INTEGER,ticker TEXT,value REAL,PRIMARY KEY(gex_run_id,ticker));
            CREATE TABLE gex_levels(gex_run_id INTEGER,ticker TEXT,strike REAL,PRIMARY KEY(gex_run_id,ticker,strike));
            CREATE TABLE gex_expirations(gex_run_id INTEGER,ticker TEXT,expiry TEXT,PRIMARY KEY(gex_run_id,ticker,expiry));
            CREATE TABLE gex_expiry_buckets(gex_run_id INTEGER,ticker TEXT,expiry_bucket TEXT,PRIMARY KEY(gex_run_id,ticker,expiry_bucket));
            """
        )
        conn.close()

        fake_db_core = types.ModuleType("db_core")
        fake_db_core.gex_conn = lambda: sqlite3.connect(db)
        previous = sys.modules.get("db_core")
        sys.modules["db_core"] = fake_db_core
        try:
            path = ROOT / "01_PROGRAMAS/data_bot/gex_persistence.py"
            spec = importlib.util.spec_from_file_location("tmad_v643_gex_persistence", path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.persist_gex_bundle(
                {"gex_run_id": 1, "ticker": "AAA", "value": 7.0},
                [
                    {"gex_run_id": 1, "ticker": "AAA", "strike": 100.0},
                    {"gex_run_id": 1, "ticker": "AAA", "strike": 105.0},
                ],
                [{"gex_run_id": 1, "ticker": "AAA", "expiry": "2026-09-18"}],
                [{"gex_run_id": 1, "ticker": "AAA", "expiry_bucket": "front"}],
            )
        finally:
            if previous is None:
                sys.modules.pop("db_core", None)
            else:
                sys.modules["db_core"] = previous
        assert result == {"summary": 1, "levels": 2, "expirations": 1, "buckets": 1}, result
        conn = sqlite3.connect(db)
        counts = tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("gex_summary", "gex_levels", "gex_expirations", "gex_expiry_buckets")
        )
        conn.close()
        assert counts == (1, 2, 1, 1), counts


def test_source_wiring() -> None:
    assert_contains(
        "01_PROGRAMAS/data_bot/tmad_client.py",
        '"strategy": "reverse_offset_recovery"',
        "complete = bool(expected_total > 0 and len(chosen_rows) == expected_total)",
        "data[\"_pagination_complete\"] = complete",
    )
    assert_contains(
        "01_PROGRAMAS/data_bot/main.py",
        "from gex_persistence import persist_gex_bundle",
        "from partial_risk_refresh import build_partial_risk_refresh",
        "persisted = persist_gex_bundle(summary_row, level_rows, expiration_rows, bucket_rows)",
        "[GEX PERSIST] scanner_run=",
        "[DATA RISK REFRESH]",
        "handoff=NOT_PUBLISHED",
    )
    assert_contains(
        "01_PROGRAMAS/alerts_bot/alerts_engine.py",
        "upsert_feature_observations_bulk(observation_rows)",
        "upsert_relational_contexts_bulk(relational_rows)",
        "[ALERTS PERSIST]",
        "closed = 0 if historical else update_trade_lifecycle(",
    )
    assert_contains(
        "01_PROGRAMAS/alerts_bot/main.py",
        "from risk_refresh import maybe_process_partial_risk_refresh",
        "maybe_process_partial_risk_refresh()",
    )


def test_risk_path_isolation() -> None:
    data_risk = text("01_PROGRAMAS/data_bot/partial_risk_refresh.py")
    alerts_risk = text("01_PROGRAMAS/alerts_bot/risk_refresh.py")
    for literal in (
        '"entries_allowed": False',
        '"research_allowed": False',
        '"learning_allowed": False',
        '"kind": "POSITION_RISK_ONLY"',
    ):
        assert literal in data_risk, literal
    for forbidden in (
        "insert_alert_event(",
        "run_alert_scan(",
        "persist_layer_signals(",
        "maybe_run_market_outcome_audit(",
        "maybe_run_adaptive_learning(",
    ):
        assert forbidden not in alerts_risk, forbidden
    assert "update_trade_lifecycle(" in alerts_risk
    assert "RISK_REFRESH_MAX_AGE_MINUTES = 20.0" in alerts_risk
    assert 'get_runtime_state("risk_refresh_last_run_id"' in alerts_risk


def test_strategy_unchanged() -> None:
    cfg = text("01_PROGRAMAS/alerts_bot/config.py")
    for invariant in (
        'CORE_READY_RANK_SCORE = _float("CORE_READY_RANK_SCORE", 82.0)',
        'FUSION_MIN_ACTIONABLE_SCORE = _float("FUSION_MIN_ACTIONABLE_SCORE", 82.0)',
        'PRECISION_FIRST_MIN_LIVE_SCORE = _float("PRECISION_FIRST_MIN_LIVE_SCORE", 85.0)',
        'PRECISION_FIRST_MIN_LIVE_PROJECTION = _float("PRECISION_FIRST_MIN_LIVE_PROJECTION", 100.0)',
        'PRECISION_FIRST_MIN_CONFIRMING_BLOCKS = _int("PRECISION_FIRST_MIN_CONFIRMING_BLOCKS", 3)',
        'DEEP_RUN_RESEARCH_ONLY = _bool("DEEP_RUN_RESEARCH_ONLY", True)',
        'MARKET_CLOSE_ALL_REQUIRE_LOCAL_GEX_FOR_EXIT = _bool("MARKET_CLOSE_ALL_REQUIRE_LOCAL_GEX_FOR_EXIT", True)',
    ):
        assert invariant in cfg, invariant


def main() -> int:
    test_atomic_gex_bundle()
    test_source_wiring()
    test_risk_path_isolation()
    test_strategy_unchanged()
    validator = text("VALIDAR_TMAD_CURRENT.bat").lower()
    assert "validar_v6_1_43.py" in validator
    workflow_path = ROOT / ".github/workflows/sync-tmad-version.yml"
    if workflow_path.exists():
        workflow = workflow_path.read_text(encoding="utf-8-sig").lower()
        assert "apply_v6_1_43_patch.py" in workflow
        assert "validate-tmad.sh" in workflow
        shared_runner = text(".github/scripts/validate-tmad.sh").lower()
        assert "validar_v6_1_43.py" in shared_runner
    print("PASS | TMAD v6.1.43+ Scanner recovery + atomic/bulk persistence + isolated position risk refresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
