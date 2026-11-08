[CmdletBinding()]
param(
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [long]$NotBeforeEpoch = 0,
    [int]$StartupTimeoutSeconds = 21600,
    [int]$AdaptiveTimeoutSeconds = 21600,
    [int]$PollSeconds = 10
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$runtimeDir = Join-Path $ProductionRoot '04_RUNTIME\historical_replay'
$statusPath = Join-Path $runtimeDir 'deferred_launcher_status.json'
$startupStatusPath = Join-Path $ProductionRoot '03_DATOS\alerts_bot\output\startup_calibration_status.json'
$replayScript = Join-Path $ProductionRoot '01_PROGRAMAS\alerts_bot\historical_replay_once.py'
$alertsPython = Join-Path $ProductionRoot '04_RUNTIME\venvs\alerts_bot\Scripts\python.exe'
$startedAt = Get-Date

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

function Write-DeferredStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Detail = '',
        [int]$ReplayPid = 0
    )
    $payload = [ordered]@{
        schema_version = 1
        mode = 'DEFERRED_HISTORICAL_REPLAY_LAUNCHER_V1'
        status = $Status
        detail = $Detail
        launcher_pid = $PID
        replay_pid = $ReplayPid
        started_at_madrid = $startedAt.ToString('o')
        updated_at_madrid = (Get-Date).ToString('o')
        live_mutation = $false
    }
    $tmp = $statusPath + '.tmp'
    $payload | ConvertTo-Json -Depth 5 | Set-Content -Path $tmp -Encoding UTF8
    Move-Item -Path $tmp -Destination $statusPath -Force
}

function Get-MatchingPythonProcesses {
    param([Parameter(Mandatory = $true)][string]$Pattern)
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^python(w)?[.]exe$' -and
        $_.CommandLine -and
        ([string]$_.CommandLine) -match $Pattern
    })
}

try {
    Write-DeferredStatus -Status 'WAITING_STARTUP' -Detail 'Production warm-up has priority over historical replay.'

    $startupDeadline = (Get-Date).AddSeconds([Math]::Max(60, $StartupTimeoutSeconds))
    $startupPayload = $null
    while ((Get-Date) -lt $startupDeadline) {
        if (Test-Path $startupStatusPath -PathType Leaf) {
            try {
                $payload = Get-Content $startupStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $status = ([string]$payload.status).ToLowerInvariant()
                $finished = [string]$payload.finished_at_madrid
                $startupEpoch = 0L
                try {
                    $startupEpoch = [DateTimeOffset]::Parse([string]$payload.started_at_madrid).ToUnixTimeSeconds()
                } catch {}
                $fresh = ($NotBeforeEpoch -le 0 -or $startupEpoch -ge ($NotBeforeEpoch - 5))
                if ($fresh -and $finished -and $status -ne 'running') {
                    $startupPayload = $payload
                    break
                }
            } catch {}
        }
        Start-Sleep -Seconds ([Math]::Max(2, $PollSeconds))
    }

    if (-not $startupPayload) {
        Write-DeferredStatus -Status 'SKIPPED_STARTUP_TIMEOUT' -Detail 'Startup did not finish inside the bounded wait; replay not launched.'
        exit 0
    }

    $startupStatus = ([string]$startupPayload.status).ToLowerInvariant()
    if ($startupStatus -eq 'error') {
        Write-DeferredStatus -Status 'SKIPPED_STARTUP_ERROR' -Detail 'Production warm-up ended in ERROR; replay suppressed to preserve resources.'
        exit 0
    }

    Write-DeferredStatus -Status 'WAITING_ADAPTIVE' -Detail 'Startup finished; waiting for any earlier adaptive research process.'

    $adaptiveDeadline = (Get-Date).AddSeconds([Math]::Max(60, $AdaptiveTimeoutSeconds))
    while ((Get-Date) -lt $adaptiveDeadline) {
        $adaptive = @(Get-MatchingPythonProcesses -Pattern 'adaptive_research_cycle[.]py')
        if ($adaptive.Count -eq 0) { break }
        Start-Sleep -Seconds ([Math]::Max(5, $PollSeconds))
    }

    $adaptiveRemaining = @(Get-MatchingPythonProcesses -Pattern 'adaptive_research_cycle[.]py')
    if ($adaptiveRemaining.Count -gt 0) {
        Write-DeferredStatus -Status 'SKIPPED_ADAPTIVE_TIMEOUT' -Detail ('Adaptive research still active after bounded wait; pids=' + (($adaptiveRemaining | ForEach-Object { $_.ProcessId }) -join ','))
        exit 0
    }

    $existingReplay = @(Get-MatchingPythonProcesses -Pattern 'historical_replay_once[.]py')
    if ($existingReplay.Count -gt 0) {
        Write-DeferredStatus -Status 'SKIPPED_REPLAY_ALREADY_RUNNING' -Detail ('Replay already active; pids=' + (($existingReplay | ForEach-Object { $_.ProcessId }) -join ','))
        exit 0
    }

    if (-not (Test-Path $replayScript -PathType Leaf)) {
        Write-DeferredStatus -Status 'SKIPPED_REPLAY_SCRIPT_MISSING' -Detail $replayScript
        exit 0
    }
    if (-not (Test-Path $alertsPython -PathType Leaf)) {
        Write-DeferredStatus -Status 'SKIPPED_ALERTS_PYTHON_MISSING' -Detail $alertsPython
        exit 0
    }

    $replayStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $stdout = Join-Path $runtimeDir ("historical_replay_{0}.log" -f $replayStamp)
    $stderr = Join-Path $runtimeDir ("historical_replay_{0}.err.log" -f $replayStamp)
    $pidFile = Join-Path $runtimeDir 'current.pid'

    $proc = Start-Process -FilePath $alertsPython -ArgumentList @('-u', $replayScript) -WorkingDirectory (Split-Path -Parent $replayScript) -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    try { $proc.PriorityClass = 'BelowNormal' } catch {}
    Set-Content -Path $pidFile -Value ([string]$proc.Id) -Encoding ASCII

    Write-DeferredStatus -Status 'LAUNCHED' -Detail ('Replay launched after production startup/adaptive isolation. log=' + $stdout) -ReplayPid ([int]$proc.Id)
    exit 0
}
catch {
    Write-DeferredStatus -Status 'ERROR' -Detail ($_.Exception.GetType().Name + ': ' + $_.Exception.Message)
    exit 1
}
