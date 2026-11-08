from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALERTS = ROOT / "01_PROGRAMAS" / "alerts_bot"
if str(ALERTS) not in sys.path:
    sys.path.insert(0, str(ALERTS))

from research_snapshot import _sqlite_backup

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
assert tuple(map(int, version.split("."))) >= (6, 2, 6), version

snapshot_text = (ALERTS / "research_snapshot.py").read_text(encoding="utf-8-sig")
replay_text = (ALERTS / "historical_replay_once.py").read_text(encoding="utf-8-sig")
alpha_text = (ALERTS / "alpha_pnl_attribution.py").read_text(encoding="utf-8-sig")
config_text = (ALERTS / "config.py").read_text(encoding="utf-8-sig")

# Snapshot performance: one SQLite backup step pins a single read view instead
# of yielding between many page batches while production keeps changing.
assert 'src.backup(dst, pages=-1, sleep=0.0)' in snapshot_text
assert 'PRAGMA journal_mode=OFF' in snapshot_text
assert 'PRAGMA synchronous=OFF' in snapshot_text
assert '"mode": "SQLITE_BACKUP_ONE_SHOT"' in snapshot_text
assert 'progress_callback: Callable | None = None' in snapshot_text

# Replay must expose the exact database/archive subphase to the watchdog.
assert 'def _snapshot_progress(component: str, detail: str)' in replay_text
assert 'SNAPSHOT_PREP/{component}' in replay_text
assert 'frozen_research_snapshot(config, progress_callback=_snapshot_progress)' in replay_text

# Functional T0 check on a real SQLite database.
with tempfile.TemporaryDirectory(prefix="tmad-v626-snapshot-") as tmp:
    root = Path(tmp)
    source = root / "source.db"
    frozen = root / "frozen.db"

    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO sample(value) VALUES(?)", [(f"v{i}",) for i in range(100)])
    conn.commit()
    conn.close()

    result = _sqlite_backup(source, frozen)
    assert result["mode"] == "SQLITE_BACKUP_ONE_SHOT"
    assert result["bytes"] > 0

    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO sample(value) VALUES('after_t0')")
    conn.commit()
    conn.close()

    frozen_conn = sqlite3.connect(frozen)
    try:
        assert frozen_conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 100
    finally:
        frozen_conn.close()

# Attribution performance: compact discovery values and point-in-time regime
# are reused across the three components that dominated v6.2.4 runtime.
assert '_DISCOVERY_VALUES_CACHE_KEY = "__tmad_attr_discovery_values"' in alpha_text
assert '_REGIME_CACHE_KEY = "__tmad_attr_regime"' in alpha_text
assert "def _discovery_values(row: dict)" in alpha_text
assert "def _row_regime(row: dict)" in alpha_text
assert "return _score_feature_values(_discovery_values(row), scheme)" in alpha_text

functions = (
    "def regime_horizon_daily_pair_challenger",
    "def regime_router_daily_pair_challenger",
    "def rolling_regime_stability_challenger",
)
for index, name in enumerate(functions):
    start = alpha_text.index(name)
    next_def = alpha_text.find("\ndef ", start + 4)
    block = alpha_text[start:] if next_def < 0 else alpha_text[start:next_def]
    assert "_row_regime(row)" in block
    assert '_json(row.get("features_json"))' not in block

# No economic/sample shortcut.
assert "FEATURE_SAMPLE_LIMIT = 0" in alpha_text
assert "SIGNAL_SAMPLE_LIMIT = 0" in alpha_text
assert 'CORE_LEARNING_ENTRY_GUARD_MINUTES = max(300' in config_text

print("PASS | v6.2.6 | frozen T0 snapshot one-shot + compact regime attribution caches")
