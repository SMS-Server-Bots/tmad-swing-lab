@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS_EXE%" (
  echo [ERROR] No se encontro Windows PowerShell.
  exit /b 9009
)
"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0DESPLEGAR_RELEASE_TMAD.ps1" %*
exit /b %ERRORLEVEL%
