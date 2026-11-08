from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL_LAYOUT = (ROOT / ".git").exists()

required = [
    ROOT / "01_PROGRAMAS" / "observability" / "runtime_publication_watchdog.ps1",
    ROOT / "VERSION.txt",
]
if CONTROL_LAYOUT:
    required += [
        ROOT / "INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1",
        ROOT / "SYNC_RUNTIME_LOGS_TMAD.ps1",
        ROOT / "SYNC_RUNTIME_DATA_TMAD.ps1",
    ]
for path in required:
    assert path.exists(), f"missing: {path}"

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
parts = tuple(int(x) for x in version.split("."))
assert parts >= (6, 1, 45), version

watchdog = (ROOT / "01_PROGRAMAS" / "observability" / "runtime_publication_watchdog.ps1").read_text(encoding="utf-8")
for token in (
    "stale_publication",
    "local_newer_than_branch",
    "runtime_publication_status.json",
):
    assert token in watchdog, f"watchdog missing {token}"

if CONTROL_LAYOUT:
    for token in (
        "origin/$Branch",
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
    ):
        assert token in installer, f"installer missing {token}"

    changelog = ROOT / "CAMBIOS_V6_1_45.txt"
    if changelog.exists():
        assert "Sin cambios" in changelog.read_text(encoding="utf-8")

print("OK v6.1.45 runtime publication watchdog" + (" control-layout" if CONTROL_LAYOUT else " production-layout"))
