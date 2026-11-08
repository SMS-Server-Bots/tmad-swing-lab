@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0INSTALAR_CONTROL_UPDATE_TMAD.ps1"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [ERROR] Instalacion del controlador %%update TMAD fallo con codigo %RC%.
) else (
  echo [OK] Controlador %%update TMAD instalado y arrancado.
)
pause
exit /b %RC%
