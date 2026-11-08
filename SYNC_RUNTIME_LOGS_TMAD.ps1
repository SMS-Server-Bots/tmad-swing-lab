param(
    [int]$RetentionDays = 7,
    [string]$ProductionPath = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$RepoPath = "",
    [switch]$Scheduled
)

$ErrorActionPreference = "Stop"
if (-not $RepoPath) { $RepoPath = $PSScriptRoot }
$RepoPath = (Resolve-Path $RepoPath).Path
$ProductionPath = (Resolve-Path $ProductionPath).Path
Set-Location $RepoPath

try {
    $madridTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Romance Standard Time")
} catch {
    throw "No se pudo resolver Europe/Madrid (Romance Standard Time): $($_.Exception.Message)"
}
$madridNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([datetime]::UtcNow, $madridTz)

# TMAD operates Mon-Fri 12:00-00:00 Europe/Madrid. The 00:00 snapshot belongs
# to the market day that has just ended, so it is allowed Tue-Sat at hour 00.
if ($Scheduled) {
    $weekday = $madridNow.DayOfWeek -notin @([System.DayOfWeek]::Saturday, [System.DayOfWeek]::Sunday)
    $regularWindow = $weekday -and $madridNow.Hour -ge 12 -and $madridNow.Hour -le 23
    $previousDay = $madridNow.AddDays(-1).DayOfWeek
    $closingMidnight = $madridNow.Hour -eq 0 -and $previousDay -notin @([System.DayOfWeek]::Saturday, [System.DayOfWeek]::Sunday)
    if (-not ($regularWindow -or $closingMidnight)) {
        Write-Host ("[INFO] TMAD runtime logs omitidos fuera de ventana Europe/Madrid. Ahora: {0:yyyy-MM-dd HH:mm:ss}" -f $madridNow) -ForegroundColor DarkGray
        exit 0
    }
}

$Branch = "runtime-logs"
$RuntimeDir = Join-Path $RepoPath "runtime_observability"
$Worktree = Join-Path $RuntimeDir "runtime-logs-worktree"
$SnapshotRoot = Join-Path $Worktree "runtime_logs"
$SnapshotLogs = Join-Path $SnapshotRoot "logs"
$CutoffDate = $madridNow.Date.AddDays(-1 * [Math]::Max(0, $RetentionDays - 1))

function Redact-Line([string]$Line) {
    if ($null -eq $Line) { return "" }
    $s = $Line
    $s = [regex]::Replace($s, 'https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]+', '[REDACTED_DISCORD_WEBHOOK]', 'IgnoreCase')
    $s = [regex]::Replace($s, '(?i)(Authorization\s*:\s*(?:Bearer|Bot)\s+)[A-Za-z0-9._~+\-/=]+', '$1[REDACTED]')
    $s = [regex]::Replace($s, '(?i)\b(?:gho_|ghp_|github_pat_)[A-Za-z0-9_]+\b', '[REDACTED_GITHUB_TOKEN]')
    $s = [regex]::Replace($s, '(?i)\bsk-ant-[A-Za-z0-9_-]+\b', '[REDACTED_ANTHROPIC_KEY]')
    $s = [regex]::Replace($s, '(?i)\bsk-[A-Za-z0-9_-]{20,}\b', '[REDACTED_API_KEY]')
    $s = [regex]::Replace($s, '(?i)(DISCORD_TOKEN|API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|PASS|TOKEN|SECRET|COOKIE)\s*[=:]\s*[^\s,;]+', '$1=[REDACTED]')
    return $s
}

function Open-SharedLogReader([string]$Path) {
    $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
    $stream = [System.IO.FileStream]::new(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        $share
    )
    try {
        return [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $true)
    } catch {
        $stream.Dispose()
        throw
    }
}

function Write-SevenDayLog([System.IO.FileInfo]$Source, [string]$Destination) {
    $destDir = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    $writer = [System.IO.StreamWriter]::new($Destination, $false, [System.Text.UTF8Encoding]::new($false))
    $reader = $null
    try {
        $reader = Open-SharedLogReader $Source.FullName
        $keepContinuation = $false
        $sawDatedLine = $false
        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            $lineDate = $null
            $m = [regex]::Match($line, '^\s*\[?(\d{4}-\d{2}-\d{2})')
            if ($m.Success) {
                $parsed = [datetime]::MinValue
                if ([datetime]::TryParseExact($m.Groups[1].Value, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$parsed)) {
                    $lineDate = $parsed.Date
                    $sawDatedLine = $true
                }
            }
            if ($null -ne $lineDate) {
                $keepContinuation = ($lineDate -ge $CutoffDate)
                if ($keepContinuation) { $writer.WriteLine((Redact-Line $line)) }
                continue
            }
            if ($keepContinuation) { $writer.WriteLine((Redact-Line $line)) }
        }

        if (-not $sawDatedLine -and $Source.LastWriteTime.Date -ge $CutoffDate) {
            try { $reader.Dispose() } catch {}
            $reader = $null
            try { $writer.Dispose() } catch {}
            $writer = $null
            $fallbackWriter = [System.IO.StreamWriter]::new($Destination, $false, [System.Text.UTF8Encoding]::new($false))
            $fallbackReader = $null
            try {
                $fallbackReader = Open-SharedLogReader $Source.FullName
                while (-not $fallbackReader.EndOfStream) {
                    $fallbackWriter.WriteLine((Redact-Line $fallbackReader.ReadLine()))
                }
            } finally {
                try { $fallbackReader.Dispose() } catch {}
                try { $fallbackWriter.Dispose() } catch {}
            }
        }
    } finally {
        try { $reader.Dispose() } catch {}
        try { $writer.Dispose() } catch {}
    }
}

function Read-LockPid([string]$Path) {
    try {
        if (Test-Path $Path -PathType Leaf) {
            $raw = (Get-Content $Path -Raw).Trim()
            if ($raw -match '^\d+$') { return [int]$raw }
        }
    } catch {}
    return 0
}

function Get-ProcessStartMadrid([int]$ProcessId) {
    if ($ProcessId -le 0) { return [datetime]::MinValue }
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        $startLocal = [datetime]$process.StartTime
        $startUtc = $startLocal.ToUniversalTime()
        return [System.TimeZoneInfo]::ConvertTimeFromUtc($startUtc, $madridTz)
    } catch {
        return [datetime]::MinValue
    }
}

if (-not (Test-Path (Join-Path $RepoPath ".git"))) { throw "RepoPath no es un repositorio Git: $RepoPath" }
if (-not (Test-Path $ProductionPath)) { throw "No existe producción TMAD: $ProductionPath" }
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

# Serialize all runtime-logs publishers (watcher/fallback/watchdog). A named
# mutex is released by Windows automatically if a publisher crashes.
$syncMutex = New-Object System.Threading.Mutex($false, "Global\TMAD_RuntimeLogs_GitSync")
$mutexAcquired = $false
try { $mutexAcquired = $syncMutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $mutexAcquired = $true }
if (-not $mutexAcquired) {
    Write-Host "[INFO] runtime-logs sync ya esta en curso; ejecucion omitida." -ForegroundColor DarkGray
    exit 0
}

$excluded = '\\(?:venvs|\.venv|venv|__pycache__)\\'
$productionLogs = @(
    Get-ChildItem $ProductionPath -Recurse -File -Filter "*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch $excluded }
)
$controlLogs = @(
    Get-ChildItem $RuntimeDir -File -Filter "*.log" -ErrorAction SilentlyContinue
)

try { git -C $RepoPath worktree remove --force $Worktree 2>$null | Out-Null } catch {}
if (Test-Path $Worktree) { Remove-Item $Worktree -Recurse -Force -ErrorAction SilentlyContinue }
git -C $RepoPath worktree prune --expire now 2>$null | Out-Null

$originUrl = (git -C $RepoPath remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or -not $originUrl) { throw "No se pudo resolver origin para runtime-logs" }
$runtimeMainRef = "refs/tmad/runtime-logs-main"
git -C $RepoPath fetch $originUrl "+refs/heads/main:$runtimeMainRef" --quiet --no-tags
if ($LASTEXITCODE -ne 0) { throw "git fetch main aislado runtime-logs falló" }
git -C $RepoPath worktree add --detach --force $Worktree $runtimeMainRef | Out-Null
if ($LASTEXITCODE -ne 0) { throw "No se pudo preparar worktree detached runtime-logs" }

Push-Location $Worktree
try {
    if (Test-Path $SnapshotRoot) { Remove-Item $SnapshotRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $SnapshotLogs | Out-Null

    foreach ($file in $productionLogs) {
        $relative = $file.FullName.Substring($ProductionPath.Length).TrimStart('\')
        if (-not $relative) { $relative = $file.Name }
        Write-SevenDayLog $file (Join-Path $SnapshotLogs (Join-Path "production" $relative))
    }
    foreach ($file in $controlLogs) {
        Write-SevenDayLog $file (Join-Path $SnapshotLogs (Join-Path "control" $file.Name))
    }

    $version = "unknown"
    $versionFile = Join-Path $ProductionPath "VERSION.txt"
    if (Test-Path $versionFile) { $version = (Get-Content $versionFile -Raw).Trim() }
    $dataPid = Read-LockPid (Join-Path $ProductionPath "04_RUNTIME\locks\tmad_core_data_bot.lock")
    $alertsPid = Read-LockPid (Join-Path $ProductionPath "04_RUNTIME\locks\tmad_alerts_adaptive_bot.lock")

    $status = [ordered]@{
        bot = "tmad-dual-bot"
        generated_at_madrid = $madridNow.ToString("yyyy-MM-ddTHH:mm:ss")
        retention_days = $RetentionDays
        scheduled_window = "Mon-Fri 12:00-00:00 Europe/Madrid; hourly"
        version = $version
        production_path = $ProductionPath
        production_log_files = $productionLogs.Count
        control_log_files = $controlLogs.Count
        data_pid = $dataPid
        alerts_pid = $alertsPid
    }
    $status | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $SnapshotRoot "status.json") -Encoding UTF8

    function Get-LatestSnapshotLine([string]$FilePattern, [string]$Pattern) {
        $files = @(
            Get-ChildItem $SnapshotLogs -Recurse -File -Filter $FilePattern -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending
        )
        foreach ($file in $files) {
            $lines = @(Get-Content $file.FullName -Encoding UTF8 -ErrorAction SilentlyContinue)
            for ($i = $lines.Count - 1; $i -ge 0; $i--) {
                if ([string]$lines[$i] -match $Pattern) { return [string]$lines[$i] }
            }
        }
        return ""
    }

    function Get-LatestSnapshotEvent(
        [string]$FilePattern,
        [string]$Pattern,
        [int]$CurrentProcessPid = 0,
        [datetime]$CurrentProcessStartMadrid = [datetime]::MinValue
    ) {
        $empty = [ordered]@{
            status = "NONE"
            text = ""
            event_date = ""
            event_time_madrid = ""
            age_days = $null
            age_minutes = $null
            source_file = ""
            current_version = $version
            event_version = ""
            event_version_source = "unavailable"
            current_process_pid = $CurrentProcessPid
            current_process_start_madrid = if ($CurrentProcessStartMadrid -ne [datetime]::MinValue) { $CurrentProcessStartMadrid.ToString('yyyy-MM-ddTHH:mm:ss') } else { "" }
            event_after_process_start = $null
            classification_reason = "no_matching_event"
        }
        $files = @(
            Get-ChildItem $SnapshotLogs -Recurse -File -Filter $FilePattern -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending
        )
        foreach ($file in $files) {
            $lines = @(Get-Content $file.FullName -Encoding UTF8 -ErrorAction SilentlyContinue)
            for ($i = $lines.Count - 1; $i -ge 0; $i--) {
                $line = [string]$lines[$i]
                if ($line -notmatch $Pattern) { continue }
                $eventDate = $null
                $eventTime = $null
                $timeMatch = [regex]::Match($line, '^\s*\[?(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})')
                $lineMatch = [regex]::Match($line, '^\s*\[?(\d{4}-\d{2}-\d{2})')
                $fileMatch = [regex]::Match($file.Name, '(\d{4}-\d{2}-\d{2})')
                $dateText = if ($lineMatch.Success) { $lineMatch.Groups[1].Value } elseif ($fileMatch.Success) { $fileMatch.Groups[1].Value } else { '' }
                if ($timeMatch.Success) {
                    $parsedTime = [datetime]::MinValue
                    $timeText = $timeMatch.Groups[1].Value + ' ' + $timeMatch.Groups[2].Value
                    if ([datetime]::TryParseExact($timeText, 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$parsedTime)) {
                        # Python control-plane logs use the VPS system wall clock,
                        # while runtime health is expressed in Europe/Madrid.
                        # Convert only control logs; production bot logs already
                        # carry Madrid/offset-aware timestamps in their own lines.
                        $isControlLog = $file.FullName -match '[\\/]logs[\\/]control[\\/]'
                        if ($isControlLog) {
                            try {
                                $localWall = [datetime]::SpecifyKind($parsedTime, [DateTimeKind]::Unspecified)
                                $eventUtc = [System.TimeZoneInfo]::ConvertTimeToUtc($localWall, [System.TimeZoneInfo]::Local)
                                $eventTime = [System.TimeZoneInfo]::ConvertTimeFromUtc($eventUtc, $madridTz)
                            } catch {
                                $eventTime = $parsedTime
                            }
                        } else {
                            $eventTime = $parsedTime
                        }
                        $eventDate = $eventTime.Date
                    }
                }
                if ($null -eq $eventDate -and $dateText) {
                    $parsed = [datetime]::MinValue
                    if ([datetime]::TryParseExact($dateText, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$parsed)) {
                        $eventDate = $parsed.Date
                    }
                }
                $ageDays = $null
                $ageMinutes = $null
                $classification = "HISTORICAL"
                $classificationReason = "event_from_prior_calendar_day"
                $eventAfterProcessStart = $null
                if ($null -ne $eventDate) {
                    $ageDays = [Math]::Max(0, [int](($madridNow.Date - $eventDate).TotalDays))
                    if ($eventDate -eq $madridNow.Date) {
                        if ($CurrentProcessPid -gt 0 -and $CurrentProcessStartMadrid -ne [datetime]::MinValue -and $null -ne $eventTime) {
                            # Allow a few seconds of timestamp/process-start skew. A
                            # same-day error that predates the live PID is stale and
                            # must never be attributed to the current deployment.
                            $comparisonStart = $CurrentProcessStartMadrid.AddSeconds(-5)
                            $eventAfterProcessStart = [bool]($eventTime -ge $comparisonStart)
                            if ($eventAfterProcessStart) {
                                $classification = "CURRENT"
                                $classificationReason = "event_at_or_after_current_process_start"
                            } else {
                                $classification = "STALE"
                                $classificationReason = "event_before_current_process_start"
                            }
                        } elseif ($CurrentProcessPid -gt 0 -and $CurrentProcessStartMadrid -eq [datetime]::MinValue) {
                            $classification = "UNKNOWN"
                            $classificationReason = "current_process_start_unavailable"
                        } else {
                            $classification = "CURRENT"
                            $classificationReason = "same_day_event_without_process_context"
                        }
                    }
                } else {
                    $classification = "UNKNOWN"
                    $classificationReason = "event_timestamp_unavailable"
                }
                if ($null -ne $eventTime) {
                    $ageMinutes = [Math]::Max(0, [Math]::Round(($madridNow - $eventTime).TotalMinutes, 2))
                }
                $eventVersion = ""
                $eventVersionSource = "unavailable"
                $versionMatch = [regex]::Match($line, '(?i)(?:version|versi[oó]n|\bv)\s*[:=]?\s*(\d+\.\d+\.\d+)')
                if ($versionMatch.Success) {
                    $eventVersion = $versionMatch.Groups[1].Value
                    $eventVersionSource = "event_line"
                }
                return [ordered]@{
                    status = $classification
                    text = $line
                    event_date = if ($null -ne $eventDate) { $eventDate.ToString('yyyy-MM-dd') } else { '' }
                    event_time_madrid = if ($null -ne $eventTime) { $eventTime.ToString('yyyy-MM-ddTHH:mm:ss') } else { '' }
                    age_days = $ageDays
                    age_minutes = $ageMinutes
                    source_file = $file.Name
                    current_version = $version
                    event_version = $eventVersion
                    event_version_source = $eventVersionSource
                    current_process_pid = $CurrentProcessPid
                    current_process_start_madrid = if ($CurrentProcessStartMadrid -ne [datetime]::MinValue) { $CurrentProcessStartMadrid.ToString('yyyy-MM-ddTHH:mm:ss') } else { "" }
                    event_after_process_start = $eventAfterProcessStart
                    classification_reason = $classificationReason
                }
            }
        }
        return $empty
    }

    $dataProcessStartMadrid = Get-ProcessStartMadrid $dataPid
    $alertsProcessStartMadrid = Get-ProcessStartMadrid $alertsPid
    $alertsError = Get-LatestSnapshotEvent "tmad_alerts_error_*.log" "(?i)(error|exception|traceback|🟥)" $alertsPid $alertsProcessStartMadrid
    $dataError = Get-LatestSnapshotEvent "tmad_data_error_*.log" "(?i)(error|exception|traceback|🟥)" $dataPid $dataProcessStartMadrid
    $controllerHeartbeat = Get-LatestSnapshotEvent "tmad_discord_update_controller.log" "(?i)TMAD controller heartbeat"
    $updateEvent = Get-LatestSnapshotEvent "tmad_update_once.log" "(?i)(%update success|%update failed|%update started)"
    $controllerAgeMinutes = if ($null -ne $controllerHeartbeat.age_minutes) { [double]$controllerHeartbeat.age_minutes } else { $null }
    $controllerStatus = if ($null -eq $controllerAgeMinutes) { "MISSING" } elseif ($controllerAgeMinutes -le 5.0) { "OK" } else { "STALE" }

    $health = [ordered]@{
        schema_version = 2
        bot = "tmad-dual-bot"
        generated_at_madrid = $madridNow.ToString("yyyy-MM-ddTHH:mm:ss")
        version = $version
        data_pid = $dataPid
        alerts_pid = $alertsPid
        data_process_start_madrid = if ($dataProcessStartMadrid -ne [datetime]::MinValue) { $dataProcessStartMadrid.ToString('yyyy-MM-ddTHH:mm:ss') } else { "" }
        alerts_process_start_madrid = if ($alertsProcessStartMadrid -ne [datetime]::MinValue) { $alertsProcessStartMadrid.ToString('yyyy-MM-ddTHH:mm:ss') } else { "" }
        data_health = Get-LatestSnapshotLine "tmad_core_data_*.log" "\[DATA HEALTH\]"
        auditor_feed = Get-LatestSnapshotLine "tmad_core_data_*.log" "\[AUDITOR FEED\]"
        alerts_state = Get-LatestSnapshotLine "tmad_alerts_log_*.log" "\[ALERTS STATE\]"
        alerts_sync = Get-LatestSnapshotLine "tmad_alerts_log_*.log" "\[ALERTS SYNC\]"
        alerts_evidence = Get-LatestSnapshotLine "tmad_alerts_log_*.log" "\[ALERTS EVIDENCE\]"
        alerts_book_audit = Get-LatestSnapshotLine "tmad_alerts_log_*.log" "\[ALERTS BOOK/AUDIT\]"
        learning_state = Get-LatestSnapshotLine "tmad_alerts_log_*.log" "\[LEARNING STATE\]"
        research_summary = Get-LatestSnapshotLine "tmad_alerts_log_*.log" "\[RESEARCH SUMMARY\]"
        update_controller = $controllerHeartbeat
        update_controller_status = $controllerStatus
        update_controller_age_minutes = $controllerAgeMinutes
        last_update_event = $updateEvent
        alerts_error = $alertsError
        data_error = $dataError
        latest_alerts_error = [string]$alertsError.text
        latest_data_error = [string]$dataError.text
    }
    $health | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $SnapshotRoot "health.json") -Encoding UTF8

    git add -f runtime_logs
    $changes = git status --porcelain -- runtime_logs
    if ($changes) {
        git -c user.name="Trumppetta Runtime" -c user.email="runtime@local" commit -m "runtime logs snapshot: TMAD" --quiet
        if ($LASTEXITCODE -ne 0) { throw "No se pudo crear snapshot runtime-logs" }
    }
    git push --force origin "HEAD:$Branch" --quiet
    if ($LASTEXITCODE -ne 0) { throw "No se pudo publicar $Branch" }
} finally {
    Pop-Location
    try { git -C $RepoPath worktree remove --force $Worktree | Out-Null } catch {}
}

Write-Host "[OK] TMAD runtime logs publicados en $Branch. Producción: $($productionLogs.Count) archivos; control: $($controlLogs.Count). Retención: $RetentionDays días." -ForegroundColor Green
