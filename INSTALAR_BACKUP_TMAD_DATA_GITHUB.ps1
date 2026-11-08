[CmdletBinding()]
param(
    [string]$RepoPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [switch]$EnablePurge,
    [switch]$FromDeploy
)

$ErrorActionPreference='Stop'
$backup=Join-Path $RepoPath 'BACKUP_TMAD_DATA_GITHUB.ps1'
if (-not (Test-Path $backup -PathType Leaf)) { throw "Falta $backup" }

$archiveArgs = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$backup`" -RepoPath `"$RepoPath`" -ProductionPath `"$ProductionPath`" -ArchiveOnly"
if ($EnablePurge) { $archiveArgs += ' -PurgeVerifiedArchives' }
$archiveAction=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $archiveArgs -WorkingDirectory $RepoPath
$archiveTrigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName 'Trumppetta_TMAD_Data_Archive_Backup' -Action $archiveAction -Trigger $archiveTrigger -Settings $settings -RunLevel Highest -Force | Out-Null

$mutableArgs = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$backup`" -RepoPath `"$RepoPath`" -ProductionPath `"$ProductionPath`" -MutableOnly"
$mutableAction=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $mutableArgs -WorkingDirectory $RepoPath
$mutableTrigger=New-ScheduledTaskTrigger -Daily -At '04:00'
Register-ScheduledTask -TaskName 'Trumppetta_TMAD_Data_Mutable_Backup' -Action $mutableAction -Trigger $mutableTrigger -Settings $settings -RunLevel Highest -Force | Out-Null

$mode=if($EnablePurge){'upload + purge verified ZIPs'}else{'upload only; NO local purge'}
Write-Host "[OK] TMAD archive backup cada 2h ($mode)." -ForegroundColor Green
Write-Host '[OK] TMAD mutable snapshot diario 04:00 hora local del VPS.' -ForegroundColor Green
if ($FromDeploy) {
    try { Start-ScheduledTask -TaskName 'Trumppetta_TMAD_Data_Archive_Backup' } catch {}
}
