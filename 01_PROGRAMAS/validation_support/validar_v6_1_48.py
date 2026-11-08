from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL_LAYOUT = (ROOT / ".git").exists()

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
parts = tuple(int(x) for x in version.split("."))
assert parts >= (6, 1, 48), version

if CONTROL_LAYOUT:
    logs = (ROOT / "SYNC_RUNTIME_LOGS_TMAD.ps1").read_text(encoding="utf-8-sig")
    data = (ROOT / "SYNC_RUNTIME_DATA_TMAD.ps1").read_text(encoding="utf-8-sig")
    installer = (ROOT / "INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1").read_text(encoding="utf-8-sig")
    deploy = (ROOT / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8-sig")
    current = (ROOT / "VALIDAR_TMAD_CURRENT.bat").read_text(encoding="utf-8-sig")

    for token in ("Global\\TMAD_RuntimeLogs_GitSync", "worktree add --detach --force", "worktree prune --expire now"):
        assert token in logs, f"runtime-logs missing {token}"
    for token in ("Global\\TMAD_RuntimeData_GitSync", '"worktree","add","--detach","--force"', '"worktree","prune","--expire","now"'):
        assert token in data, f"runtime-data missing {token}"
    assert "if ($FromDeploy)" in installer
    assert "fuera del camino critico" in installer
    assert "Observabilidad no pudo actualizarse; el deploy continua" in deploy
    assert "validar_v6_1_48.py" in current
    assert "if(Test-Path $optional)" in current

print("OK v6.1.48 stable updater" + (" control-layout" if CONTROL_LAYOUT else " production-layout"))
