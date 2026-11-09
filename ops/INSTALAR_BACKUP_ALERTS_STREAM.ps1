# TMAD - Programa el backup nocturno en streaming de la base de Alerts y retira el backup mutable antiguo.
# Uso:  powershell -ExecutionPolicy Bypass -File .\INSTALAR_BACKUP_ALERTS_STREAM.ps1 [-At "03:10"] [-RunNow]
param(
    [string]$At = "03:10",
    [string]$TaskName = "Trumppetta_TMAD_Alerts_Stream_Backup",
    [string]$OldTask = "Trumppetta_TMAD_Data_Mutable_Backup",
    [switch]$RunNow
)
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "BACKUP_ALERTS_DB_STREAM.ps1"
if (-not (Test-Path $script)) { throw "Falta $script" }
$args = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $args -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Host "[OK] $TaskName diario a las $At (hora del VPS). Logs en $PSScriptRoot\logs" -ForegroundColor Green
$old = Get-ScheduledTask -TaskName $OldTask -ErrorAction SilentlyContinue
if ($old) {
    if ($old.State -eq "Running") { Stop-ScheduledTask -TaskName $OldTask }
    Disable-ScheduledTask -TaskName $OldTask | Out-Null
    Write-Host "[OK] $OldTask desactivada (un despliegue de tmad-dual-bot puede reactivarla: vuelve a ejecutar este instalador)." -ForegroundColor Yellow
}
if ($RunNow) { Start-ScheduledTask -TaskName $TaskName; Write-Host "[OK] Ejecucion lanzada ahora." }
