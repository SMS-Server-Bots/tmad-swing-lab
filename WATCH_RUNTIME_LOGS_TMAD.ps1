param(
    [int]$RetentionDays = 7,
    [int]$DebounceSeconds = 12,
    [int]$MinSyncIntervalSeconds = 45,
    [int]$MaxPendingSeconds = 90,
    [int]$DataMinSyncIntervalSeconds = 300,
    [string]$ProductionPath = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$RepoPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepoPath) { $RepoPath = $PSScriptRoot }
$RepoPath = (Resolve-Path $RepoPath).Path
$ProductionPath = (Resolve-Path $ProductionPath).Path
$SyncScript = Join-Path $RepoPath "SYNC_RUNTIME_LOGS_TMAD.ps1"
$DataSyncScript = Join-Path $RepoPath "SYNC_RUNTIME_DATA_TMAD.ps1"
$RuntimeDir = Join-Path $RepoPath "runtime_observability"
$WatcherLog = Join-Path $RuntimeDir "tmad_event_watcher.log"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if (-not (Test-Path $SyncScript -PathType Leaf)) { throw "Falta $SyncScript" }
if (-not (Test-Path $DataSyncScript -PathType Leaf)) { throw "Falta $DataSyncScript" }
if (-not (Test-Path $ProductionPath -PathType Container)) { throw "Produccion TMAD inexistente: $ProductionPath" }
try {
    $MadridTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Romance Standard Time")
} catch {
    throw "No se pudo resolver Europe/Madrid (Romance Standard Time): $($_.Exception.Message)"
}

function Write-WatcherLog([string]$Message, [string]$Level = "INFO") {
    $madridLogNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([datetime]::UtcNow, $MadridTz)
    $line = "[{0}] [{1}] {2}" -f $madridLogNow.ToString("yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try { Add-Content -Path $WatcherLog -Value $line -Encoding UTF8 } catch {}
}

function Publish-RuntimeData([switch]$Force) {
    try {
        $args = @(
            '-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$DataSyncScript,
            '-MinIntervalSeconds',[string]$DataMinSyncIntervalSeconds,
            '-ProductionPath',$ProductionPath,
            '-RepoPath',$RepoPath
        )
        if ($Force) { $args += '-Force' }
        & powershell.exe @args
        if ($LASTEXITCODE -eq 0) {
            Write-WatcherLog "runtime-data comprobado/publicado." "OK"
            return $true
        }
        Write-WatcherLog "runtime-data fallo con codigo $LASTEXITCODE; runtime-logs sigue operativo." "WARN"
    } catch {
        Write-WatcherLog ("runtime-data fallo: " + $_.Exception.Message + "; runtime-logs sigue operativo.") "WARN"
    }
    return $false
}

$state = [hashtable]::Synchronized(@{
    Pending = $false
    FirstPendingAt = [datetime]::MinValue
    LastChangeAt = [datetime]::MinValue
    LastPath = ""
    ChangeCount = 0
    ForceRuntimeData = $false
    ForceRuntimeDataReason = ""
})

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $ProductionPath
$watcher.Filter = "*.log"
$watcher.IncludeSubdirectories = $true
$watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite -bor [System.IO.NotifyFilters]::Size -bor [System.IO.NotifyFilters]::CreationTime
$watcher.EnableRaisingEvents = $true

$onChange = {
    $now = Get-Date
    if (-not $state.Pending) {
        $state.Pending = $true
        $state.FirstPendingAt = $now
    }
    $state.LastChangeAt = $now
    $state.LastPath = $Event.SourceEventArgs.FullPath
    $state.ChangeCount = [int]$state.ChangeCount + 1
    $eventPath = [string]$Event.SourceEventArgs.FullPath
    if ($eventPath -match 'historical_replay.*\.log$') {
        try {
            $tail = @(Get-Content -LiteralPath $eventPath -Tail 20 -ErrorAction SilentlyContinue)
            $terminal = $tail | Select-String -Pattern '\[HISTORICAL REPLAY\] phase=(COMPLETE|ERROR|REUSED)' | Select-Object -Last 1
            if ($terminal) {
                $state.ForceRuntimeData = $true
                $state.ForceRuntimeDataReason = [string]$terminal.Line
            }
        } catch {}
    }
}

$subscriptions = @()
Write-WatcherLog "Watcher TMAD arrancando. production=$ProductionPath debounce=${DebounceSeconds}s min_interval=${MinSyncIntervalSeconds}s max_pending=${MaxPendingSeconds}s data_min_interval=${DataMinSyncIntervalSeconds}s"
try {
    $subscriptions += Register-ObjectEvent -InputObject $watcher -EventName Created -Action $onChange
    $subscriptions += Register-ObjectEvent -InputObject $watcher -EventName Changed -Action $onChange
    $subscriptions += Register-ObjectEvent -InputObject $watcher -EventName Renamed -Action $onChange
} catch {
    Write-WatcherLog ("No se pudieron registrar eventos FileSystemWatcher: " + $_.Exception.Message) "ERROR"
    throw
}

$lastSyncAt = [datetime]::MinValue
Write-WatcherLog "Watcher TMAD iniciado y suscripciones activas."

# Startup publishes logs immediately and forces a lightweight data projection.
# This captures VPS restarts and makes the current TMAD state remotely auditable.
try {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $SyncScript -RetentionDays $RetentionDays -ProductionPath $ProductionPath -RepoPath $RepoPath
    if ($LASTEXITCODE -eq 0) {
        $lastSyncAt = Get-Date
        Write-WatcherLog "Snapshot inicial runtime-logs publicado." "OK"
        [void](Publish-RuntimeData -Force)
    } else {
        Write-WatcherLog "Snapshot inicial runtime-logs fallo con codigo $LASTEXITCODE." "WARN"
    }
} catch {
    Write-WatcherLog ("Snapshot inicial runtime-logs fallo: " + $_.Exception.Message) "WARN"
}

try {
    while ($true) {
        Start-Sleep -Seconds 2
        if (-not $state.Pending) { continue }

        $now = Get-Date
        $quietFor = ($now - $state.LastChangeAt).TotalSeconds
        $pendingFor = ($now - $state.FirstPendingAt).TotalSeconds
        $sinceSync = if ($lastSyncAt -eq [datetime]::MinValue) { [double]::PositiveInfinity } else { ($now - $lastSyncAt).TotalSeconds }

        $readyByQuiet = $quietFor -ge $DebounceSeconds
        $readyByMaxWait = $pendingFor -ge $MaxPendingSeconds
        if (-not ($readyByQuiet -or $readyByMaxWait)) { continue }
        if ($sinceSync -lt $MinSyncIntervalSeconds) { continue }

        $count = [int]$state.ChangeCount
        $lastPath = [string]$state.LastPath
        $forceRuntimeData = [bool]$state.ForceRuntimeData
        $forceRuntimeDataReason = [string]$state.ForceRuntimeDataReason
        # Reset before syncing: changes that arrive during the push remain pending
        # for a second pass.
        $state.Pending = $false
        $state.FirstPendingAt = [datetime]::MinValue
        $state.LastChangeAt = [datetime]::MinValue
        $state.ChangeCount = 0
        $state.LastPath = ""
        $state.ForceRuntimeData = $false
        $state.ForceRuntimeDataReason = ""

        Write-WatcherLog "Evento TMAD detectado; cambios=$count; ultimo=$lastPath. Publicando runtime-logs..."
        try {
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $SyncScript -RetentionDays $RetentionDays -ProductionPath $ProductionPath -RepoPath $RepoPath
            if ($LASTEXITCODE -eq 0) {
                $lastSyncAt = Get-Date
                Write-WatcherLog "runtime-logs actualizado por evento." "OK"
                if ($forceRuntimeData) {
                    Write-WatcherLog ("Replay terminal detectado; forzando runtime-data sin throttle. " + $forceRuntimeDataReason) "INFO"
                    [void](Publish-RuntimeData -Force)
                } else {
                    [void](Publish-RuntimeData)
                }
            } else {
                Write-WatcherLog "Sync por evento fallo con codigo $LASTEXITCODE." "WARN"
                if (-not $state.Pending) {
                    $state.Pending = $true
                    $state.FirstPendingAt = Get-Date
                    $state.LastChangeAt = Get-Date
                }
            }
        } catch {
            Write-WatcherLog ("Sync por evento fallo: " + $_.Exception.Message) "WARN"
            if (-not $state.Pending) {
                $state.Pending = $true
                $state.FirstPendingAt = Get-Date
                $state.LastChangeAt = Get-Date
            }
        }
    }
} finally {
    foreach ($sub in $subscriptions) {
        try { Unregister-Event -SubscriptionId $sub.Id -ErrorAction SilentlyContinue } catch {}
    }
    try { $watcher.Dispose() } catch {}
    Write-WatcherLog "Watcher TMAD detenido." "WARN"
}
