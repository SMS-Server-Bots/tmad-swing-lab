[CmdletBinding()]
param(
    [int]$MaxStaleHours = 3,
    [int]$WriteGraceSeconds = 120,
    [string]$ProductionPath = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$RepoPath = "",
    [switch]$NoRepair
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $RepoPath) {
    $RepoPath = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$RepoPath = (Resolve-Path $RepoPath).Path
$ProductionPath = (Resolve-Path $ProductionPath).Path

$RuntimeDir = Join-Path $RepoPath "runtime_observability"
$StatusPath = Join-Path $RuntimeDir "runtime_publication_status.json"
$LogPath = Join-Path $RuntimeDir "tmad_runtime_publication_watchdog.log"
$LogsSync = Join-Path $RepoPath "SYNC_RUNTIME_LOGS_TMAD.ps1"
$DataSync = Join-Path $RepoPath "SYNC_RUNTIME_DATA_TMAD.ps1"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Write-WatchLog {
    param([string]$Level,[string]$Message)
    $line = "[{0}] [{1}] {2}" -f ([datetime]::UtcNow.ToString("yyyy-MM-dd HH:mm:ss'Z'")),$Level,$Message
    try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 } catch {}
    Write-Host $line
}

function Invoke-GitSafe {
    param([string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = @(& git -C $RepoPath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    return [pscustomobject]@{ Code=$code; Output=$out }
}

function Refresh-RemoteBranch {
    param([string]$Branch,[int]$Attempts = 3)
    $lastError = ""
    for ($attempt = 1; $attempt -le [Math]::Max(1,$Attempts); $attempt++) {
        $fetch = Invoke-GitSafe @("fetch","origin",$Branch,"--quiet","--force")
        if ($fetch.Code -eq 0) {
            $log = Invoke-GitSafe @("log","-1","--format=%cI","origin/$Branch")
            if ($log.Code -eq 0 -and ($log.Output -join '').Trim()) {
                try {
                    return [pscustomobject]@{
                        CommitUtc = [DateTimeOffset]::Parse(($log.Output | Select-Object -First 1)).ToUniversalTime()
                        Error = $null
                    }
                } catch {
                    $lastError = "invalid origin/$Branch commit timestamp"
                }
            } else {
                $lastError = "cannot read origin/$Branch commit timestamp"
            }
        } else {
            $lastError = "git fetch origin $Branch failed: $($fetch.Output -join ' ')"
        }
        if ($attempt -lt $Attempts) { Start-Sleep -Seconds 2 }
    }
    return [pscustomobject]@{ CommitUtc=$null; Error=$lastError }
}

function Get-LatestWriteUtc {
    param([string[]]$Paths)
    $latest = $null
    foreach ($path in $Paths) {
        if (-not (Test-Path $path)) { continue }
        $item = Get-Item $path
        if ($item.PSIsContainer) {
            $candidate = Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTimeUtc -Descending |
                Select-Object -First 1
            $stamp = if ($candidate) { $candidate.LastWriteTimeUtc } else { $item.LastWriteTimeUtc }
        } else {
            $stamp = $item.LastWriteTimeUtc
        }
        if ($stamp -and (($null -eq $latest) -or ($stamp -gt $latest))) { $latest = $stamp }
    }
    return $latest
}

function Get-StreamState {
    param([string]$Name,[DateTimeOffset]$Now,$RemoteResult,$LocalUtc)
    $branchUtc = $RemoteResult.CommitUtc
    $ageHours = if ($branchUtc) { ($Now - $branchUtc).TotalHours } else { $null }
    $graceCutoff = if ($branchUtc) { $branchUtc.UtcDateTime.AddSeconds([Math]::Max(0,$WriteGraceSeconds)) } else { $null }
    $localNewer = [bool]($LocalUtc -and ((-not $branchUtc) -or ($LocalUtc -gt $graceCutoff)))
    $stale = [bool]($localNewer -and ((-not $branchUtc) -or ($ageHours -gt $MaxStaleHours)))
    return [ordered]@{
        stream = $Name
        remote_branch_commit_utc = if ($branchUtc) { $branchUtc.ToString("o") } else { $null }
        local_latest_write_utc = if ($LocalUtc) { ([DateTimeOffset]$LocalUtc).ToUniversalTime().ToString("o") } else { $null }
        branch_age_hours = if ($null -ne $ageHours) { [math]::Round($ageHours,2) } else { $null }
        write_grace_seconds = $WriteGraceSeconds
        local_newer_than_branch = $localNewer
        stale_publication = $stale
        remote_error = $RemoteResult.Error
        repair_attempted = $false
        repair_exit_code = $null
        repair_result = "NOT_NEEDED"
    }
}

function Invoke-Repair {
    param([string]$Stream)
    if ($Stream -eq "runtime-logs") {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $LogsSync -RetentionDays 7 -ProductionPath $ProductionPath -RepoPath $RepoPath
        return $LASTEXITCODE
    }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $DataSync -ProductionPath $ProductionPath -RepoPath $RepoPath -Force
    return $LASTEXITCODE
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git no disponible" }
if (-not (Test-Path (Join-Path $RepoPath ".git") -PathType Container)) { throw "RepoPath no es repo Git: $RepoPath" }
if (-not (Test-Path $ProductionPath -PathType Container)) { throw "ProductionPath no existe: $ProductionPath" }
if (-not (Test-Path $LogsSync -PathType Leaf)) { throw "Falta $LogsSync" }
if (-not (Test-Path $DataSync -PathType Leaf)) { throw "Falta $DataSync" }

# Only production-owned sources that are actually represented by each snapshot.
# Do not use 04_RUNTIME here: deploy/status/lock files change while the watchdog
# itself is running and previously produced false 'local newer' convergence misses.
$logPaths = @(
    (Join-Path $ProductionPath "03_DATOS\data_bot\logs"),
    (Join-Path $ProductionPath "03_DATOS\alerts_bot\logs")
)
$dataPaths = @(
    (Join-Path $ProductionPath "03_DATOS\data_bot\db\live"),
    (Join-Path $ProductionPath "03_DATOS\alerts_bot\db\live"),
    (Join-Path $ProductionPath "03_DATOS\data_bot\output"),
    (Join-Path $ProductionPath "03_DATOS\alerts_bot\output")
)

$now = [DateTimeOffset]::UtcNow
$logsRemote = Refresh-RemoteBranch "runtime-logs"
$dataRemote = Refresh-RemoteBranch "runtime-data"
$logsState = Get-StreamState "runtime-logs" $now $logsRemote (Get-LatestWriteUtc $logPaths)
$dataState = Get-StreamState "runtime-data" $now $dataRemote (Get-LatestWriteUtc $dataPaths)
$streams = @($logsState,$dataState)

foreach ($stream in $streams) {
    if (-not $stream.stale_publication) { continue }
    Write-WatchLog "WARN" ("{0} stale: branch_age={1}h local_newer={2}" -f $stream.stream,$stream.branch_age_hours,$stream.local_newer_than_branch)
    if ($NoRepair) {
        $stream.repair_result = "DISABLED"
        continue
    }
    $stream.repair_attempted = $true
    try {
        $code = Invoke-Repair $stream.stream
        $stream.repair_exit_code = $code
        $stream.repair_result = if ($code -eq 0) { "SYNC_EXITED_OK" } else { "SYNC_FAILED" }
        Write-WatchLog (if ($code -eq 0) { "OK" } else { "ERROR" }) ("repair {0} exit={1}" -f $stream.stream,$code)
    } catch {
        $stream.repair_exit_code = -1
        $stream.repair_result = "EXCEPTION"
        Write-WatchLog "ERROR" ("repair {0}: {1}" -f $stream.stream,$_.Exception.Message)
    }
}

# Re-fetch after repairs. A small write grace avoids declaring failure because a
# live SQLite/log file changed seconds after the snapshot commit was created.
$verifyNow = [DateTimeOffset]::UtcNow
foreach ($stream in $streams) {
    if (-not $stream.repair_attempted) { continue }
    $remote = Refresh-RemoteBranch $stream.stream 3
    $local = if ($stream.stream -eq "runtime-logs") { Get-LatestWriteUtc $logPaths } else { Get-LatestWriteUtc $dataPaths }
    $verified = Get-StreamState $stream.stream $verifyNow $remote $local
    $stream.remote_branch_commit_utc = $verified.remote_branch_commit_utc
    $stream.local_latest_write_utc = $verified.local_latest_write_utc
    $stream.branch_age_hours = $verified.branch_age_hours
    $stream.local_newer_than_branch = $verified.local_newer_than_branch
    $stream.stale_publication = $verified.stale_publication
    $stream.remote_error = $verified.remote_error
    if (-not $stream.stale_publication -and -not $stream.remote_error) {
        $stream.repair_result = "VERIFIED_REMOTE_CONVERGENCE"
    } elseif ($stream.repair_result -eq "SYNC_EXITED_OK") {
        $stream.repair_result = "SYNC_OK_BUT_REMOTE_STILL_STALE"
    }
}

$healthy = -not ($streams | Where-Object { $_.stale_publication -or $_.remote_error })
$status = [ordered]@{
    schema_version = 3
    generated_at_utc = $verifyNow.ToString("o")
    max_stale_hours = $MaxStaleHours
    write_grace_seconds = $WriteGraceSeconds
    healthy = [bool]$healthy
    repair_enabled = -not [bool]$NoRepair
    repo_path = $RepoPath
    production_path = $ProductionPath
    streams = $streams
}
$status | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $StatusPath -Encoding UTF8

if ($healthy) {
    Write-WatchLog "OK" "runtime publication healthy"
    exit 0
}
Write-WatchLog "ERROR" "runtime publication remains unhealthy after watchdog evaluation"
exit 2
