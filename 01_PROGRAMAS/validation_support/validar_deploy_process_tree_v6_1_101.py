from __future__ import annotations

from pathlib import Path

root = Path(__file__).resolve().parents[2]
deploy = (root / "DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8", errors="replace")

assert "function Get-TmadProcessTreeIds" in deploy
assert "function Stop-TmadProcessTreeVerified" in deploy
assert 'taskkill.exe /PID $RootPid /T /F >nul 2>&1' in deploy
assert "Stop-Process -Id ([int]$pidValue) -Force" in deploy
assert "PID(s) persistentes" in deploy
assert "la verificacion confirma el arbol cerrado; carrera tolerada" in deploy
assert "& taskkill.exe" not in deploy
assert "Stop-TmadProcessTreeVerified -RootPid ([int]$target.ProcessId)" in deploy
assert "Stop-TmadProcessTreeVerified -RootPid ([int]$historyPidRaw)" in deploy
assert "Stop-TmadProcessTreeVerified -RootPid ([int]$historyTunnelPidRaw)" in deploy
assert "Stop-TmadProcessTreeVerified -RootPid ([int]$cloudflaredProc.ProcessId)" in deploy

# The regression being fixed was native stderr becoming terminating under
# $ErrorActionPreference='Stop' before liveness verification could run.
assert "$ErrorActionPreference = 'Stop'" in deploy
assert ">nul 2>&1" in deploy

print("PASS | v6.1.101 | TMAD process-tree stop is race tolerant and liveness verified")
