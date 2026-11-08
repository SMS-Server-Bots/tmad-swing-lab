@echo off
if exist "%~dp004_RUNTIME\launcher\first_run_complete.json" del /q "%~dp004_RUNTIME\launcher\first_run_complete.json"
if exist "%~dp004_RUNTIME\launcher\first_run_last_attempt.json" del /q "%~dp004_RUNTIME\launcher\first_run_last_attempt.json"
call "%~dp0INICIAR_TMAD_DUAL_BOT.bat"
exit /b %ERRORLEVEL%
