@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

set "PY=%TEMP%\TMAD\validation_venv\Scripts\python.exe"
if not exist "%PY%" (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo POSTCOPY VALIDATION FAILED - Python unavailable
  exit /b 1
)

echo [POSTCOPY] production smoke v6.1.123
"%PY%" -B "01_PROGRAMAS\validar_postcopy_smoke_v6_1_123.py" || exit /b 1

set "PRODROOT=%CD%"
set "ALERTS_PY=%PRODROOT%\04_RUNTIME\venvs\alerts_bot\Scripts\python.exe"
set "DATA_PY=%PRODROOT%\04_RUNTIME\venvs\data_bot\Scripts\python.exe"
if not exist "%ALERTS_PY%" (
  echo POSTCOPY VALIDATION FAILED - Alerts runtime Python unavailable
  exit /b 1
)
if not exist "%DATA_PY%" (
  echo POSTCOPY VALIDATION FAILED - Data runtime Python unavailable
  exit /b 1
)

echo [POSTCOPY] import smoke with production venvs
pushd "01_PROGRAMAS\alerts_bot"
"%ALERTS_PY%" -B -c "import main; print('PASS ^| alerts main import ^| production venv')" || (popd & exit /b 1)
popd
pushd "01_PROGRAMAS\data_bot"
"%DATA_PY%" -B -c "import main; print('PASS ^| data main import ^| production venv')" || (popd & exit /b 1)
popd

echo [POSTCOPY] parse runtime PowerShell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$files=@('01_PROGRAMAS\launcher\launcher.ps1','01_PROGRAMAS\alerts_bot\start-bot.ps1','01_PROGRAMAS\data_bot\start-bot.ps1'); foreach($f in $files){$tokens=$null;$errors=$null;[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f),[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){$errors|%%{Write-Host $_.Message -ForegroundColor Red};exit 1}};Write-Host 'PASS ^| postcopy PowerShell parse'" || exit /b 1

echo POSTCOPY VALIDATION CORRECTA
exit /b 0
