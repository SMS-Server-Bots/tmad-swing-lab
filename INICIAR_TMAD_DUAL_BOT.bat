@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" (echo [ERROR] No se encontro Windows PowerShell.& pause & exit /b 9009)
"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp001_PROGRAMAS\launcher\launcher.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (echo. & echo [ERROR] Lanzador finalizado con codigo %RC%. & echo Revisa 04_RUNTIME\launcher\launcher_error.log & pause)
exit /b %RC%
