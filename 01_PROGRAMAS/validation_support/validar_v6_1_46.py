from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL_LAYOUT = (ROOT / ".git").exists()

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
parts = tuple(int(x) for x in version.split("."))
assert parts >= (6, 1, 46), version

watchdog = (ROOT / "01_PROGRAMAS" / "observability" / "runtime_publication_watchdog.ps1").read_text(encoding="utf-8")
for token in (
    "WriteGraceSeconds = 120",
    "Refresh-RemoteBranch",
    "03_DATOS\\data_bot\\db\\live",
    "03_DATOS\\alerts_bot\\db\\live",
    "VERIFIED_REMOTE_CONVERGENCE",
    "schema_version = 3",
):
    assert token in watchdog, f"watchdog missing {token}"
assert 'Join-Path $ProductionPath "04_RUNTIME"' not in watchdog, "04_RUNTIME must not drive freshness"

if CONTROL_LAYOUT:
    assert "Attempts = 3" in watchdog
    installer = (ROOT / "INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1").read_text(encoding="utf-8")
    for token in (
        "WriteGraceSeconds', '120'",
        "Trumppetta_RuntimePublication_TMAD_Watchdog",
    ):
        assert token in installer, f"installer missing {token}"

    legacy_nonblocking = (
        "El despliegue continúa" in installer
        and "$RunNow -and -not $FromDeploy" in installer
    )
    successor_nonblocking = (
        "if ($FromDeploy)" in installer
        and "fuera del camino critico" in installer
        and "elseif ($RunNow)" in installer
    )
    assert legacy_nonblocking or successor_nonblocking, "installer lost non-blocking deploy semantics"

    changes = ROOT / "CAMBIOS_V6_1_46.txt"
    if changes.exists():
        assert "Sin cambios en Scanner" in changes.read_text(encoding="utf-8")

print("OK v6.1.46 watchdog deploy safety" + (" control-layout" if CONTROL_LAYOUT else " production-layout"))
