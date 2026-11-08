from pathlib import Path

root = Path(__file__).resolve().parents[2]
bat = (root / "VALIDAR_TMAD_CURRENT.bat").read_text(encoding="utf-8", errors="replace")

mandatory = "$files=@('01_PROGRAMAS\\observability\\runtime_publication_watchdog.ps1','01_PROGRAMAS\\launcher\\launcher.ps1');"
optional = "foreach($optional in @('DESPLEGAR_TMAD.ps1'"

assert mandatory in bat
assert optional in bat
assert "$files=@('01_PROGRAMAS\\observability\\runtime_publication_watchdog.ps1','01_PROGRAMAS\\launcher\\launcher.ps1','DESPLEGAR_TMAD.ps1')" not in bat

print("PASS | v6.1.98 | production validation does not require control-plane root scripts")
