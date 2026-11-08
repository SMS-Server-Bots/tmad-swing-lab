@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1" -RunNow
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [ERROR] Instalacion runtime logs TMAD fallo con codigo %RC%.
  pause
  exit /b %RC%
)
echo.
echo [OK] Runtime logs TMAD instalados.
pause
exit /b 0
