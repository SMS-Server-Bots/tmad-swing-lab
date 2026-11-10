# Instala dependencias y programa TMAD Swing Lab en el VPS (lunes a viernes, despues del postmarket_deep).
# Uso: powershell -ExecutionPolicy Bypass -File .\INSTALAR_SWING_LAB_VPS.ps1 [-At "14:30"] [-RunNow]
# -At es hora del VPS (Pacifico): 14:30 = 23:30 Madrid, despues del cierre y del postmarket_deep.
param(
    [string]$At = "14:30",
    [string]$TaskName = "Trumppetta_TMAD_Swing_Lab_Paper",
    [string]$Python = "python",
    [switch]$RunNow
)
$ErrorActionPreference = "Continue"
& $Python -c "import pandas, numpy; print('pandas', pandas.__version__, 'numpy', numpy.__version__)" 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) {
    Write-Host "Instalando pandas y numpy..."
    & $Python -m pip install --quiet pandas numpy 2>&1 | ForEach-Object { "$_" }
    & $Python -c "import pandas, numpy" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar pandas/numpy" }
}
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "EJECUTAR_SWING_LAB_VPS.ps1"
$taskArgs = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArgs -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Host "[OK] $TaskName de lunes a viernes a las $At (hora del VPS)." -ForegroundColor Green
Write-Host "     Resultado: $(Resolve-Path (Join-Path $PSScriptRoot '..'))\swing_lab_runtime\ULTIMA_LISTA.md y MARCADOR.md"
if ($RunNow) { Start-ScheduledTask -TaskName $TaskName; Write-Host "[OK] Ejecucion lanzada ahora." }
