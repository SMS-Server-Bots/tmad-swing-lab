from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
manager = (ROOT / "MANAGE_TMAD_HISTORY_STORAGE.ps1").read_text(encoding="utf-8-sig")
deploy = (ROOT / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8-sig")
helper = (ROOT / "MATERIALIZE_TMAD_ALERTS_COLD_HISTORY.ps1").read_text(encoding="utf-8-sig")
layer = (ROOT / "01_PROGRAMAS" / "alerts_bot" / "layer_archive.py").read_text(encoding="utf-8-sig")
installer = (ROOT / "INSTALAR_HISTORICO_TMAD.ps1").read_text(encoding="utf-8-sig")

# Purge must reuse the existing validated deletion path and only widen its
# candidate set after additional Alerts-specific checks.
assert "AlertsLayerArchiveRoot" in manager
assert "$ArchiveRoots = @($ArchiveRoot,$AlertsLayerArchiveRoot)" in manager
assert "layer_archive_catalog WHERE verified=1" in manager
assert "$layerCatalog[$name]" in manager
assert "$hash -ne $layerCatalog[$importKey]" in manager
assert "$manifest.source_counts.complete" in manager
assert "$hash -ne ([string]$entry.sha256).ToLowerInvariant()" in manager
assert "Where-Object { $_.LastWriteTime -lt $cutoff }" in manager
assert manager.count("Remove-Item -LiteralPath $file.FullName -Force") == 1
assert "Trumppetta_TMAD_History_Storage_Manager" in installer
assert "-WarmDays 45 -CacheHours 24" in installer
assert "New-ScheduledTaskTrigger -Daily -At '05:15'" in installer

# Cold recovery is control-plane only, SHA+size verified, and must happen before
# historical replay is launched.
assert "tmad-data-index" in helper
assert "archive-index.json" in helper
assert "Get-FileHash -Algorithm SHA256" in helper
assert "$remoteHash -ne $expectedHash" in helper
assert "$expectedBytes" in helper
assert r"history_cache\alerts_layers" in helper

materialize_pos = deploy.index("MATERIALIZE_TMAD_ALERTS_COLD_HISTORY.ps1")
replay_pos = (
    deploy.index("deferred_historical_replay.ps1")
    if "deferred_historical_replay.ps1" in deploy
    else deploy.index("$historicalReplay = Join-Path")
)
assert materialize_pos < replay_pos
assert "Cold history Alerts verificado/materializado antes del replay" in deploy

# Historical Python runtime never talks to GitHub/network directly.
assert '"history_cache" / "alerts_layers"' in layer
assert "for path in cold:" in layer
assert "for path in local:" in layer
for forbidden in ("subprocess", "urllib", "requests"):
    assert forbidden not in layer

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
assert tuple(int(x) for x in version.split(".")) >= (6, 2, 39), version

print(
    "PASS | >=6.2.39 | Alerts warm->GitHub verified->local purge->cold cache "
    "| existing deletion path reused | replay hydration first | LIVE unchanged"
)
