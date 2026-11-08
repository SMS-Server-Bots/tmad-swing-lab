@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0DESPLEGAR_TMAD.ps1"
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" (
  echo DESPLIEGUE TMAD FALLIDO - codigo %ERR%
  pause
  exit /b %ERR%
)
echo DESPLIEGUE TMAD COMPLETADO
pause
exit /b 0
