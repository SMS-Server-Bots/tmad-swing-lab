from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

required = [
    ROOT / "01_PROGRAMAS" / "observability" / "runtime_publication_watchdog.ps1",
    ROOT / "INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1",
    ROOT / "SYNC_RUNTIME_LOGS_TMAD.ps1",
    ROOT / "SYNC_RUNTIME_DATA_TMAD.ps1",
    ROOT / "VERSION.txt",
]
for path in required:
    assert path.exists(), f"missing: {path}"

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
assert version == "6.1.44", version

watchdog = (ROOT / "01_PROGRAMAS" / "observability" / "runtime_publication_watchdog.ps1").read_text(encoding="utf-8")
for token in (
    "origin/$Branch",
    "stale_publication",
    "local_newer_than_branch",
    "runtime_publication_status.json",
    "SYNC_RUNTIME_LOGS_TMAD.ps1",
    "SYNC_RUNTIME_DATA_TMAD.ps1",
    "-Force",
    "VERIFIED_REMOTE_CONVERGENCE",
    "SYNC_OK_BUT_REMOTE_STILL_STALE",
):
    assert token in watchdog, f"watchdog missing {token}"

installer = (ROOT / "INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1").read_text(encoding="utf-8")
for token in (
    "Trumppetta_RuntimePublication_TMAD_Watchdog",
    "runtime_publication_watchdog.ps1",
    "RepetitionInterval (New-TimeSpan -Minutes 30)",
    "-MaxStaleHours', '3'",
    "if ($FromDeploy -or $RunNow)",
    "Watchdog inicial TMAD no consiguió convergencia",
):
    assert token in installer, f"installer missing {token}"

# Scope guard: v6.1.44 must not change the Scanner/strategy modules merely to
# solve publication observability. The release validator documents this intent.
changelog = (ROOT / "CAMBIOS_V6_1_44.txt").read_text(encoding="utf-8")
assert "Sin cambios" in changelog or "No trading logic" in changelog

print("OK v6.1.44 runtime publication watchdog + independent auto-repair")
