[CmdletBinding()]
param(
    [int]$MinIntervalSeconds = 300,
    [int]$MaxRowsPerTable = 250,
    [int]$MaxTotalMb = 30,
    [string]$ProductionPath = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$RepoPath = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
if (-not $RepoPath) { $RepoPath = $PSScriptRoot }
$RepoPath = (Resolve-Path $RepoPath).Path
$ProductionPath = (Resolve-Path $ProductionPath).Path

$Branch = "runtime-data"
$RuntimeDir = Join-Path $RepoPath "runtime_observability"
$Worktree = Join-Path $RuntimeDir "runtime-data-worktree"
$ExportRoot = Join-Path $RuntimeDir "runtime-data-export"
$StateFile = Join-Path $RuntimeDir "runtime-data.state.json"
$LogFile = Join-Path $RuntimeDir "tmad_runtime_data_sync.log"
$Exporter = Join-Path $RepoPath "01_PROGRAMAS\observability\runtime_data_export.py"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

# Serialize runtime-data publishers to avoid overlapping worktree/export jobs.
$syncMutex = New-Object System.Threading.Mutex($false, "Global\TMAD_RuntimeData_GitSync")
$mutexAcquired = $false
try { $mutexAcquired = $syncMutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $mutexAcquired = $true }
if (-not $mutexAcquired) {
    Log "INFO" "runtime-data sync ya esta en curso; ejecucion omitida."
    exit 0
}

try {
    $MadridTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Romance Standard Time")
} catch {
    throw "No se pudo resolver Europe/Madrid (Romance Standard Time): $($_.Exception.Message)"
}

function Madrid-Now {
    return [System.TimeZoneInfo]::ConvertTimeFromUtc([datetime]::UtcNow, $MadridTz)
}

function Log([string]$Level, [string]$Message) {
    $line = "[{0}] [{1}] {2}" -f (Madrid-Now).ToString("yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try { Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 } catch {}
    Write-Host $line
}

function Invoke-Git([string[]]$Arguments, [switch]$AllowFailure) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = @(& git -C $RepoPath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw "git -C $RepoPath $($Arguments -join ' ') fallo ($code): $($out -join ' ')"
    }
    return [pscustomobject]@{ Code=$code; Output=$out }
}

if (-not (Test-Path $Exporter -PathType Leaf)) { throw "Falta exporter runtime-data: $Exporter" }
if (-not (Test-Path (Join-Path $ProductionPath "03_DATOS") -PathType Container)) { throw "Falta 03_DATOS en produccion: $ProductionPath" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git no disponible" }

if (-not $Force -and $MinIntervalSeconds -gt 0 -and (Test-Path $StateFile -PathType Leaf)) {
    try {
        $state = Get-Content $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $last = [datetime]::Parse([string]$state.last_success_utc).ToUniversalTime()
        $age = ([datetime]::UtcNow - $last).TotalSeconds
        if ($age -lt $MinIntervalSeconds) {
            Log "INFO" ("runtime-data omitido por throttle: age={0:N0}s < min={1}s" -f $age,$MinIntervalSeconds)
            exit 0
        }
    } catch {
        Log "WARN" ("Estado throttle ilegible; se publicara: " + $_.Exception.Message)
    }
}

Remove-Item -LiteralPath $ExportRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $ExportRoot | Out-Null

try {
    $venvPython = Join-Path $ProductionPath "venvs\data_bot\Scripts\python.exe"
    if (Test-Path $venvPython -PathType Leaf) {
        & $venvPython $Exporter $ProductionPath $ExportRoot --max-rows $MaxRowsPerTable --max-total-mb $MaxTotalMb
    } elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
        & py.exe -3 $Exporter $ProductionPath $ExportRoot --max-rows $MaxRowsPerTable --max-total-mb $MaxTotalMb
    } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
        & python.exe $Exporter $ProductionPath $ExportRoot --max-rows $MaxRowsPerTable --max-total-mb $MaxTotalMb
    } else {
        throw "No se encontro Python para exportar runtime-data"
    }
    if ($LASTEXITCODE -ne 0) { throw "runtime_data_export.py termino con codigo $LASTEXITCODE" }

    if (-not (Test-Path (Join-Path $ExportRoot "status.json") -PathType Leaf)) {
        throw "El exporter no genero status.json"
    }
    if (-not (Test-Path (Join-Path $ExportRoot "catalog.json") -PathType Leaf)) {
        throw "El exporter no genero catalog.json"
    }

    $originUrl = [string]((Invoke-Git @("remote","get-url","origin")).Output | Select-Object -First 1)
    $originUrl = $originUrl.Trim()
    if (-not $originUrl) { throw "No se pudo resolver origin para runtime-data" }
    $runtimeMainRef = "refs/tmad/runtime-data-main"
    [void](Invoke-Git @("fetch",$originUrl,"+refs/heads/main:$runtimeMainRef","--quiet","--no-tags"))
    if (Test-Path $Worktree) {
        [void](Invoke-Git @("worktree","remove","--force",$Worktree) -AllowFailure)
        Remove-Item -LiteralPath $Worktree -Recurse -Force -ErrorAction SilentlyContinue
    }
    [void](Invoke-Git @("worktree","prune","--expire","now") -AllowFailure)
    [void](Invoke-Git @("worktree","add","--detach","--force",$Worktree,$runtimeMainRef))

    Push-Location $Worktree
    try {

        $snapshotRoot = Join-Path $Worktree "runtime_data"
        if (Test-Path $snapshotRoot) { Remove-Item $snapshotRoot -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $snapshotRoot | Out-Null
        Copy-Item -Path (Join-Path $ExportRoot "*") -Destination $snapshotRoot -Recurse -Force

        & git add -f runtime_data
        if ($LASTEXITCODE -ne 0) { throw "git add runtime_data fallo" }
        $changes = & git status --porcelain -- runtime_data
        if ($changes) {
            & git -c user.name="Trumppetta Runtime" -c user.email="runtime@local" commit -m "runtime data snapshot: TMAD" --quiet
            if ($LASTEXITCODE -ne 0) { throw "No se pudo crear commit runtime-data" }
        }
        & git push --force origin "HEAD:$Branch" --quiet
        if ($LASTEXITCODE -ne 0) { throw "No se pudo publicar $Branch" }
    } finally {
        Pop-Location
    }

    $status = Get-Content (Join-Path $ExportRoot "status.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    [ordered]@{
        last_success_utc = [datetime]::UtcNow.ToString("o")
        last_success_madrid = (Madrid-Now).ToString("o")
        version = [string]$status.version
        database_count = [int]$status.database_count
        exported_table_count = [int]$status.exported_table_count
        exported_json_bytes = [long]$status.exported_json_bytes
        branch = $Branch
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StateFile -Encoding UTF8

    Log "OK" ("runtime-data publicado | version={0} dbs={1} tables={2} bytes={3}" -f $status.version,$status.database_count,$status.exported_table_count,$status.exported_json_bytes)
    exit 0
} catch {
    Log "ERROR" $_.Exception.Message
    exit 1
} finally {
    try { [void](Invoke-Git @("worktree","remove","--force",$Worktree) -AllowFailure) } catch {}
    Remove-Item -LiteralPath $Worktree -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ExportRoot -Recurse -Force -ErrorAction SilentlyContinue
}
