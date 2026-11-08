@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ACTUALIZAR_Y_DESPLEGAR_TMAD.ps1"
set "ERR=%ERRORLEVEL%"
echo.
if not "%ERR%"=="0" (
  echo ACTUALIZACION O DESPLIEGUE TMAD FALLIDO - codigo %ERR%
  pause
  exit /b %ERR%
)
echo TMAD ACTUALIZADO Y DESPLEGADO CORRECTAMENTE
pause
exit /b 0
