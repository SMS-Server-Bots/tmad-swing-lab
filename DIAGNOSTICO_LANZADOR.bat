@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
echo === TMAD DUAL BOT - DIAGNOSTICO ===
echo Carpeta: %CD%
echo Windows: %OS%
echo PowerShell esperado: %PS_EXE%
if exist "%PS_EXE%" (echo PowerShell: OK) else (echo PowerShell: NO ENCONTRADO)
where py 2>nul
where python 2>nul
if exist "tmad_bot_data_v3\tmad_session.json" (echo Sesion TMAD: OK) else (echo Sesion TMAD: FALTA)
if exist "launcher.ps1" (echo launcher.ps1: OK) else (echo launcher.ps1: FALTA)
echo.
pause
exit /b 0
