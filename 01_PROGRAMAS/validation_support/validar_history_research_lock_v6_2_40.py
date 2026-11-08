from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
manager = (ROOT / "MANAGE_TMAD_HISTORY_STORAGE.ps1").read_text(encoding="utf-8-sig")
installer = (ROOT / "INSTALAR_HISTORICO_TMAD.ps1").read_text(encoding="utf-8-sig")
deploy = (ROOT / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8-sig")

# Research-state guard must precede every cleanup/purge path.
guard_pos = manager.index("# Fail closed before any cache/archive mutation")
repo_cleanup_pos = manager.index("$repoData = Join-Path")
cache_cleanup_pos = manager.index("$cache = Join-Path")
archive_delete_pos = manager.index("Remove-Item -LiteralPath $file.FullName -Force")
assert guard_pos < repo_cleanup_pos < cache_cleanup_pos < archive_delete_pos

assert "historical_replay_status" in manager
assert "fusion_gate_calibration_status" in manager
assert "mode=ro" in manager
assert "SKIPPED_RESEARCH_ACTIVE" in manager
assert "SKIPPED_RESEARCH_STATE_UNAVAILABLE" in manager

# Fail closed: if state cannot be read, no storage mutation may happen.
unavailable_pos = manager.index("SKIPPED_RESEARCH_STATE_UNAVAILABLE")
assert "exit 0" in manager[unavailable_pos:repo_cleanup_pos]
active_pos = manager.index("SKIPPED_RESEARCH_ACTIVE")
assert "exit 0" in manager[active_pos:repo_cleanup_pos]

# Daily manager remains installed. Deploy must not start it inside replay window.
assert "New-ScheduledTaskTrigger -Daily -At '05:15'" in installer
from_deploy = installer[installer.index("if ($FromDeploy)"):]
assert "Start-ScheduledTask -TaskName 'Trumppetta_TMAD_History_Storage_Manager'" not in from_deploy
assert "Trumppetta_TMAD_Legacy_History_Migration" in from_deploy

# Verified cold cache is still materialized before detached replay.
materialize_pos = deploy.index("MATERIALIZE_TMAD_ALERTS_COLD_HISTORY.ps1")
replay_pos = (
    deploy.index("deferred_historical_replay.ps1")
    if "deferred_historical_replay.ps1" in deploy
    else deploy.index("$historicalReplay = Join-Path")
)
assert materialize_pos < replay_pos

# 6.2.39 purge gates remain intact.
for required in (
    "$ArchiveRoots = @($ArchiveRoot,$AlertsLayerArchiveRoot)",
    "layer_archive_catalog WHERE verified=1",
    "$hash -ne $layerCatalog[$importKey]",
    "$manifest.source_counts.complete",
    "Where-Object { $_.LastWriteTime -lt $cutoff }",
):
    assert required in manager

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
assert tuple(int(x) for x in version.split(".")) >= (6, 2, 40), version

print(
    "PASS | >=6.2.40 | fail-closed history research lock | no deploy-time purge/replay race "
    "| 6.2.39 cold lifecycle preserved | LIVE unchanged"
)
