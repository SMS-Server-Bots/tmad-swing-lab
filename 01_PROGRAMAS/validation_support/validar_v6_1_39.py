from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
data_main = (ROOT / "01_PROGRAMAS/data_bot/main.py").read_text(encoding="utf-8-sig")
data_cfg = (ROOT / "01_PROGRAMAS/data_bot/config.py").read_text(encoding="utf-8-sig")
alerts_cfg = (ROOT / "01_PROGRAMAS/alerts_bot/config.py").read_text(encoding="utf-8-sig")
alerts_main = (ROOT / "01_PROGRAMAS/alerts_bot/main.py").read_text(encoding="utf-8-sig")
lifecycle = (ROOT / "01_PROGRAMAS/alerts_bot/lifecycle.py").read_text(encoding="utf-8-sig")
db = (ROOT / "01_PROGRAMAS/alerts_bot/db.py").read_text(encoding="utf-8-sig")
validator_bat = (ROOT / "VALIDAR_TMAD_CURRENT.bat").read_text(encoding="utf-8-sig")
version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()

version_tuple = tuple(int(part) for part in version.split("."))
assert version_tuple >= (6, 1, 39), version
assert f'BOT_VERSION = "{version}"' in data_cfg
assert f'BOT_VERSION = "{version}"' in alerts_cfg

# Strategy/gates are intentionally unchanged by this release.
assert 'CORE_READY_RANK_SCORE = _float("CORE_READY_RANK_SCORE", 82.0)' in alerts_cfg
assert 'FUSION_MIN_ACTIONABLE_SCORE = _float("FUSION_MIN_ACTIONABLE_SCORE", 82.0)' in alerts_cfg
assert 'DEEP_RUN_RESEARCH_ONLY = _bool("DEEP_RUN_RESEARCH_ONLY", True)' in alerts_cfg
assert 'MARKET_CLOSE_ALL_REQUIRE_LOCAL_GEX_FOR_EXIT = _bool("MARKET_CLOSE_ALL_REQUIRE_LOCAL_GEX_FOR_EXIT", True)' in alerts_cfg

# Shadow lifecycle route regression: producer and consumer use the same target.
assert 'return "shadow"' in lifecycle
assert 'for target in ("live", "shadow", "research", "preliminary")' in lifecycle
assert '"shadow_live"' not in lifecycle
assert 'def get_pending_exit_deliveries(' in db
assert 'retry_pending_exit_deliveries()' in lifecycle
assert 'from lifecycle import retry_pending_exit_deliveries' in alerts_main

# Parallelism is bounded and network-only. Persistence must remain on main loop.
assert 'GEX_SELECTIVE_FETCH_WORKERS = max(1, min(4,' in data_cfg
assert 'ThreadPoolExecutor' in data_main
start = data_main.index('def _fetch_gex_chunk(')
end = data_main.index('def normalize_existing_snapshot_dates_v49(')
network_helper = data_main[start:end]
for forbidden in (
    'insert_gex_summary(', 'insert_gex_level(', 'insert_gex_expiration(',
    'insert_gex_expiry_bucket(', 'insert_scanner_snapshot(', 'raw_and_discover(',
    'gex_cache_record_failure(', 'gex_cache_clear_success(',
):
    assert forbidden not in network_helper, forbidden
assert 'str(profile).lower() != "selective"' in network_helper

# Technical noise is throttled but work remains scheduled/retried.
assert 'def _log_info_throttled(' in alerts_main
assert 'labels_deferred:' in alerts_main
assert 'rest_drain' in alerts_main
assert 'mark_job_complete = False' in alerts_main

# Windows deployment regressions:
# CURRENT validation must not depend on CALL :label and the Python fallback stays safe.
assert 'call :run' not in validator_bat.lower()
assert '\n:run\n' not in validator_bat.lower().replace('\r\n', '\n')
assert 'validation_runner.py' in validator_bat
assert 'where python >nul 2>nul || set "PY=py -3 -B"' in validator_bat
assert 'where py >nul 2>nul && set "PY=py -3 -B"' not in validator_bat

print('PASS | TMAD v6.1.39+ latency/lifecycle/deploy invariants')
