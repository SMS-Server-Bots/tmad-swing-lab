@echo off
set "REPORT=%~dp003_DATOS\alerts_bot\output\initial_baseline\baseline_report.html"
if exist "%REPORT%" start "" "%REPORT%"
if not exist "%~dp003_DATOS\alerts_bot\output" mkdir "%~dp003_DATOS\alerts_bot\output"
start "" explorer.exe "%~dp003_DATOS\alerts_bot\output"
exit /b 0
