@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul || (
    echo ERROR: no se encontro Python en PATH.
    exit /b 1
  )
  py -3 PREPARAR_RELEASE_TMAD.py
) else (
  python PREPARAR_RELEASE_TMAD.py
)
if errorlevel 1 exit /b %ERRORLEVEL%

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CD%\INSTALAR_RELEASE_FINALIZER_TMAD.ps1" -RepoPath "%CD%" -EveryMinutes 5
if errorlevel 1 exit /b %ERRORLEVEL%

echo.
echo RELEASE TMAD PREPARADA Y FINALIZER AUTOMATICO INSTALADO.
exit /b 0
