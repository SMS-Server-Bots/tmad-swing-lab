from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
version_tuple = tuple(int(part) for part in version.split("."))
assert version_tuple >= (6, 1, 42), version

alerts_cfg = (ROOT / "01_PROGRAMAS/alerts_bot/config.py").read_text(encoding="utf-8-sig")
data_cfg = (ROOT / "01_PROGRAMAS/data_bot/config.py").read_text(encoding="utf-8-sig")
v641 = ROOT / "01_PROGRAMAS/validation_support/validar_v6_1_41.py"
validator_bat = (ROOT / "VALIDAR_TMAD_CURRENT.bat").read_text(encoding="utf-8-sig")
workflow = ROOT / ".github/workflows/sync-tmad-version.yml"

assert f'BOT_VERSION = "{version}"' in alerts_cfg
assert f'BOT_VERSION = "{version}"' in data_cfg

# Strategy/gates remain untouched by the storage release.
assert 'CORE_READY_RANK_SCORE = _float("CORE_READY_RANK_SCORE", 82.0)' in alerts_cfg
assert 'FUSION_MIN_ACTIONABLE_SCORE = _float("FUSION_MIN_ACTIONABLE_SCORE", 82.0)' in alerts_cfg
assert 'DEEP_RUN_RESEARCH_ONLY = _bool("DEEP_RUN_RESEARCH_ONLY", True)' in alerts_cfg
assert 'MARKET_CLOSE_ALL_REQUIRE_LOCAL_GEX_FOR_EXIT = _bool("MARKET_CLOSE_ALL_REQUIRE_LOCAL_GEX_FOR_EXIT", True)' in alerts_cfg

# v6.1.41 must explicitly distinguish the dual layouts.
v641_text = v641.read_text(encoding="utf-8-sig")
assert 'control_only = [' in v641_text
assert 'if any(path.exists() for path in control_only):' in v641_text
assert 'partial control-only storage infrastructure' in v641_text

# Reproduce the production POST-check layout: 01_PROGRAMAS exists, while the
# control-only root scripts and .github workflow do not. The full v6.1.41
# storage regression must pass in that layout, not merely skip its functional tests.
with tempfile.TemporaryDirectory(prefix="tmad_v642_production_layout_") as td:
    prod = Path(td) / "production"
    copies = [
        (ROOT / "01_PROGRAMAS/validation_support/validar_v6_1_41.py", prod / "01_PROGRAMAS/validation_support/validar_v6_1_41.py"),
        (ROOT / "01_PROGRAMAS/observability/runtime_data_export.py", prod / "01_PROGRAMAS/observability/runtime_data_export.py"),
        (ROOT / "01_PROGRAMAS/data_bot/maintenance_v620.py", prod / "01_PROGRAMAS/data_bot/maintenance_v620.py"),
        (ROOT / "01_PROGRAMAS/alerts_bot/maintenance_v620.py", prod / "01_PROGRAMAS/alerts_bot/maintenance_v620.py"),
    ]
    for source, destination in copies:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    completed = subprocess.run(
        [sys.executable, str(prod / "01_PROGRAMAS/validation_support/validar_v6_1_41.py")],
        cwd=prod,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "v6.1.41 production-layout regression failed\n"
            + completed.stdout + "\n" + completed.stderr
        )
    assert "dual-layout safe" in completed.stdout

assert 'validar_v6_1_42.py' in validator_bat.lower()
if workflow.exists():
    workflow_text = workflow.read_text(encoding="utf-8-sig")
    assert 'validate-tmad.sh' in workflow_text
    shared_runner = (ROOT / ".github/scripts/validate-tmad.sh").read_text(encoding="utf-8-sig")
    assert 'validar_v6_1_42.py' in shared_runner
    assert 'Create exact version tag' in workflow_text

print("PASS | TMAD v6.1.42+ production POST-check layout + strategy invariants")
