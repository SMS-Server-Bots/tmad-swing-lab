@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp001_PROGRAMAS\launcher\detener-bots.ps1"
pause
exit /b %ERRORLEVEL%
