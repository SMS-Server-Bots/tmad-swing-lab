[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [switch]$FromDeploy
)

$ErrorActionPreference = 'Stop'
$migration = Join-Path $RepoRoot 'MIGRAR_HISTORICO_LEGACY_TMAD.ps1'
$manager = Join-Path $RepoRoot 'MANAGE_TMAD_HISTORY_STORAGE.ps1'
foreach ($p in @($migration,$manager)) {
    if (-not (Test-Path $p -PathType Leaf)) { throw "Falta $p" }
}

$settingsLong = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
$settingsShort = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$migArgs = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $migration + '" -RepoRoot "' + $RepoRoot + '" -ProductionRoot "' + $ProductionRoot + '"'
$migAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $migArgs -WorkingDirectory $RepoRoot
$migTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 6) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName 'Trumppetta_TMAD_Legacy_History_Migration' -Action $migAction -Trigger $migTrigger -Settings $settingsLong -RunLevel Highest -Force | Out-Null

$mgrArgs = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $manager + '" -RepoRoot "' + $RepoRoot + '" -ProductionRoot "' + $ProductionRoot + '" -WarmDays 45 -CacheHours 24'
$mgrAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $mgrArgs -WorkingDirectory $RepoRoot
$mgrTrigger = New-ScheduledTaskTrigger -Daily -At '05:15'
Register-ScheduledTask -TaskName 'Trumppetta_TMAD_History_Storage_Manager' -Action $mgrAction -Trigger $mgrTrigger -Settings $settingsShort -RunLevel Highest -Force | Out-Null

Write-Host '[OK] Migracion legacy old_2: reintento cada 6h, resumible, nunca borra sin import+backup verificados.' -ForegroundColor Green
Write-Host '[OK] Gestor de historico: diario 05:15, warm=45d, cache=24h.' -ForegroundColor Green

if ($FromDeploy) {
    # Storage manager stays scheduled daily at 05:15. Do not start it from
    # deploy because the detached historical replay may already be reading
    # warm/cold history. MANAGE_TMAD_HISTORY_STORAGE.ps1 also fails closed on
    # active/unverifiable research state.
    try { Start-ScheduledTask -TaskName 'Trumppetta_TMAD_Legacy_History_Migration' } catch {}
}
