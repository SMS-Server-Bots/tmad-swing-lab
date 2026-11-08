param(
    [int]$RetentionDays = 7,
    [string]$ProductionPath = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$RepoPath = "",
    [switch]$RunNow,
    [switch]$FromDeploy
)

$ErrorActionPreference = "Stop"
if (-not $RepoPath) { $RepoPath = $PSScriptRoot }
$RepoPath = (Resolve-Path $RepoPath).Path
$ProductionPath = (Resolve-Path $ProductionPath).Path
$TaskName = "Trumppetta_RuntimeLogs_TMAD"
$EventTaskName = "Trumppetta_RuntimeLogs_TMAD_EventWatch"
$WatchdogTaskName = "Trumppetta_RuntimePublication_TMAD_Watchdog"
$MaintenanceTaskName = "Trumppetta_TMAD_Storage_Maintenance"
$SyncScript = Join-Path $RepoPath "SYNC_RUNTIME_LOGS_TMAD.ps1"
$WatchScript = Join-Path $RepoPath "WATCH_RUNTIME_LOGS_TMAD.ps1"
$WatchdogScript = Join-Path $RepoPath "01_PROGRAMAS\observability\runtime_publication_watchdog.ps1"
$MaintenanceScript = Join-Path $RepoPath "RUN_TMAD_STORAGE_MAINTENANCE.ps1"

if (-not (Test-Path $SyncScript -PathType Leaf)) { throw "Falta $SyncScript" }
if (-not (Test-Path $WatchScript -PathType Leaf)) { throw "Falta $WatchScript" }
if (-not (Test-Path $WatchdogScript -PathType Leaf)) { throw "Falta $WatchdogScript" }
if (-not (Test-Path $MaintenanceScript -PathType Leaf)) { throw "Falta $MaintenanceScript" }
if (-not (Test-Path (Join-Path $RepoPath ".git") -PathType Container)) { throw "Repo de control inválido: $RepoPath" }
if (-not (Test-Path $ProductionPath -PathType Container)) { throw "Producción TMAD inexistente: $ProductionPath" }

$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path $PowerShellExe -PathType Leaf)) { throw "No se encontró Windows PowerShell" }

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentIdentity -LogonType Interactive -RunLevel Highest

# -----------------------------------------------------------------------------
# 1) Watcher event-driven: fuente primaria. Se ejecuta de forma continua y
# publica cuando cualquier *.log de producción cambia, también noches/weekends.
# -----------------------------------------------------------------------------
$watchArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-WindowStyle', 'Hidden',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $WatchScript),
    '-RetentionDays', [string]$RetentionDays,
    '-ProductionPath', ('"{0}"' -f $ProductionPath),
    '-RepoPath', ('"{0}"' -f $RepoPath)
) -join ' '

$watchAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $watchArguments -WorkingDirectory $RepoPath
$watchTriggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn -User $currentIdentity)
)
$watchSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 100 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

try { Stop-ScheduledTask -TaskName $EventTaskName -ErrorAction SilentlyContinue } catch {}
Register-ScheduledTask `
    -TaskName $EventTaskName `
    -Action $watchAction `
    -Trigger $watchTriggers `
    -Settings $watchSettings `
    -Principal $principal `
    -Description "Observa eventos/logs TMAD y publica runtime-logs en GitHub tras cada ráfaga de actividad, 24/7." `
    -Force | Out-Null

$WatcherLog = Join-Path (Join-Path $RepoPath "runtime_observability") "tmad_event_watcher.log"
Start-ScheduledTask -TaskName $EventTaskName
Start-Sleep -Seconds 4
$watchTask = Get-ScheduledTask -TaskName $EventTaskName -ErrorAction Stop
$watchInfo = Get-ScheduledTaskInfo -TaskName $EventTaskName -ErrorAction Stop
if ($watchTask.State -ne 'Running') {
    throw "Watcher event-driven no quedo Running. state=$($watchTask.State) last_result=$($watchInfo.LastTaskResult)"
}
if (-not (Test-Path $WatcherLog -PathType Leaf)) {
    throw "Watcher event-driven esta Running pero no creo su log de salud: $WatcherLog"
}
$watchTail = @(Get-Content $WatcherLog -Tail 20 -ErrorAction SilentlyContinue)
if (-not ($watchTail -match 'suscripciones activas')) {
    throw "Watcher event-driven no confirmo suscripciones activas. Revisa: $WatcherLog"
}
Write-Host "[OK] Watcher event-driven instalado, Running y con suscripciones activas: $EventTaskName" -ForegroundColor Green
Write-Host "[INFO] Los cambios de logs TMAD disparan GitHub 24/7 con debounce; reinicios incluidos." -ForegroundColor Cyan

# -----------------------------------------------------------------------------
# 2) Snapshot horario: red de seguridad. Mantiene la ventana histórica actual
# para recuperar cualquier evento que el watcher no pudiera publicar.
# -----------------------------------------------------------------------------
$arguments = @(
    '-NoLogo',
    '-NoProfile',
    '-WindowStyle', 'Hidden',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $SyncScript),
    '-RetentionDays', [string]$RetentionDays,
    '-ProductionPath', ('"{0}"' -f $ProductionPath),
    '-RepoPath', ('"{0}"' -f $RepoPath),
    '-Scheduled'
) -join ' '

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $arguments -WorkingDirectory $RepoPath
$now = Get-Date
$nextHour = $now.Date.AddHours($now.Hour + 1)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $nextHour `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Fallback horario de logs TMAD sanitizados; watcher event-driven es la fuente primaria." `
    -Force | Out-Null

Write-Host "[OK] Tarea fallback instalada: $TaskName" -ForegroundColor Green
Write-Host "[INFO] Fallback horario: L-V 12:00-23:00 + cierre 00:00 Europe/Madrid." -ForegroundColor Cyan
Write-Host "[INFO] Destino GitHub: rama runtime-logs; retención: $RetentionDays días." -ForegroundColor Cyan

# -----------------------------------------------------------------------------
# 3) Watchdog independiente: tercer nivel. Cada 30 minutos compara actividad
# local de producción con las ramas remotas runtime-logs/runtime-data. Si la
# publicación lleva >3h congelada y producción tiene datos más nuevos, ejecuta
# el sync correspondiente y comprueba convergencia. No depende del watcher.
# -----------------------------------------------------------------------------
$watchdogArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-WindowStyle', 'Hidden',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $WatchdogScript),
    '-MaxStaleHours', '3',
    '-WriteGraceSeconds', '120',
    '-ProductionPath', ('"{0}"' -f $ProductionPath),
    '-RepoPath', ('"{0}"' -f $RepoPath)
) -join ' '
$watchdogAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $watchdogArguments -WorkingDirectory $RepoPath
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(2)) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 25)

Register-ScheduledTask `
    -TaskName $WatchdogTaskName `
    -Action $watchdogAction `
    -Trigger $watchdogTrigger `
    -Settings $watchdogSettings `
    -Principal $principal `
    -Description "Watchdog independiente TMAD: detecta runtime-logs/runtime-data congelados y fuerza reparación verificada." `
    -Force | Out-Null

Write-Host "[OK] Watchdog publicación instalado: $WatchdogTaskName" -ForegroundColor Green
Write-Host "[INFO] Frecuencia 30 min; repara sólo si producción local es más reciente y la rama lleva >3h stale." -ForegroundColor Cyan

# La observabilidad nunca debe convertirse en un punto único de fallo del
# despliegue. En deploy se intenta reparar inmediatamente y se deja WARN si no
# converge; la tarea independiente seguirá reintentando cada 30 min. En RunNow
# manual sí mantenemos semántica estricta para diagnóstico explícito.
if ($FromDeploy) {
    Write-Host "[INFO] Observabilidad instalada fuera del camino critico; el watchdog empezara en background." -ForegroundColor Cyan
} elseif ($RunNow) {
    & $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $WatchdogScript -MaxStaleHours 3 -WriteGraceSeconds 120 -ProductionPath $ProductionPath -RepoPath $RepoPath
    $watchdogExit = $LASTEXITCODE
    if ($watchdogExit -ne 0) { throw "Watchdog manual TMAD no consiguió convergencia; código $watchdogExit" }
    Write-Host "[OK] Watchdog manual confirma publicación convergente." -ForegroundColor Green
}

# -----------------------------------------------------------------------------
# 4) Mantenimiento de almacenamiento: trigger horario independiente de la zona
# horaria del VPS. El runner se auto-limita a una ejecución por día entre
# 03:00-05:59 Europe/Madrid, ya terminado el deep y antes de la operativa.
# -----------------------------------------------------------------------------
$maintenanceArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-WindowStyle', 'Hidden',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $MaintenanceScript),
    '-ProductionPath', ('"{0}"' -f $ProductionPath),
    '-RepoPath', ('"{0}"' -f $RepoPath)
) -join ' '
$maintenanceAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $maintenanceArguments -WorkingDirectory $RepoPath
$maintenanceTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At $nextHour `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$maintenanceSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask `
    -TaskName $MaintenanceTaskName `
    -Action $maintenanceAction `
    -Trigger $maintenanceTrigger `
    -Settings $maintenanceSettings `
    -Principal $principal `
    -Description "Mantenimiento SQLite TMAD seguro: compacta metadata antigua, optimiza DB y VACUUM sólo en fin de semana si el espacio reclamable lo justifica." `
    -Force | Out-Null
Write-Host "[OK] Mantenimiento storage instalado: $MaintenanceTaskName" -ForegroundColor Green
Write-Host "[INFO] Ventana efectiva: 03:00-05:59 Europe/Madrid, máximo una ejecución limpia al día." -ForegroundColor Cyan

if ($RunNow) {
    & $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $SyncScript -RetentionDays $RetentionDays -ProductionPath $ProductionPath -RepoPath $RepoPath
    if ($LASTEXITCODE -ne 0) { throw "Snapshot inicial TMAD falló con código $LASTEXITCODE" }
}
