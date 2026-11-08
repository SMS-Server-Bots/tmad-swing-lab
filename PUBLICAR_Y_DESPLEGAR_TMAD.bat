@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo [INFO] PUBLICAR_Y_DESPLEGAR_TMAD es ahora un alias del flujo GitHub directo.
call "%~dp0ACTUALIZAR_Y_DESPLEGAR_TMAD.bat"
exit /b %ERRORLEVEL%
