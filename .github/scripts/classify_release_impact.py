from __future__ import annotations

import fnmatch
import subprocess
import sys

CONTROL_ONLY = (
    ".github/**",
    "TMAD_UPDATE_ONCE.py",
    "SYNC_RUNTIME_LOGS_TMAD.ps1",
    "01_PROGRAMAS/validation_support/**",
    "01_PROGRAMAS/validar_*.py",
    "VALIDAR_TMAD_CURRENT.bat",
    "CAMBIOS_V*.txt",
    "README.md",
    "docs/**",
)

def changed(base: str, head: str) -> list[str]:
    out = subprocess.check_output(["git", "diff", "--name-only", f"{base}..{head}"], text=True)
    return [line.strip() for line in out.splitlines() if line.strip()]

def control_only(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in CONTROL_ONLY)

if len(sys.argv) != 3:
    raise SystemExit("usage: classify_release_impact.py <base> <head>")

paths = changed(sys.argv[1], sys.argv[2])
runtime = [p for p in paths if not control_only(p)]
print("changed_files=" + str(len(paths)))
for p in paths:
    print(("RUNTIME " if p in runtime else "CONTROL ") + p)
print("runtime_changed=" + ("true" if runtime else "false"))
raise SystemExit(1 if runtime else 0)
