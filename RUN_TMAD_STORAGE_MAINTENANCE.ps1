param(
    [string]$ProductionPath = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$RepoPath = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
if (-not $RepoPath) { $RepoPath = $PSScriptRoot }
$RepoPath = (Resolve-Path $RepoPath).Path
$ProductionPath = (Resolve-Path $ProductionPath).Path
$RuntimeDir = Join-Path $RepoPath "runtime_observability"
$LogPath = Join-Path $RuntimeDir "storage_maintenance.log"
$MarkerPath = Join-Path $RuntimeDir "storage_maintenance_last_success.txt"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Write-MaintLog([string]$Message, [string]$Level = "INFO") {
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

try {
    $madridTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("Romance Standard Time")
} catch {
    throw "No se pudo resolver Europe/Madrid: $($_.Exception.Message)"
}
$madridNow = [System.TimeZoneInfo]::ConvertTimeFromUtc([datetime]::UtcNow, $madridTz)
$today = $madridNow.ToString("yyyy-MM-dd")

# The scheduled task wakes hourly so VPS local timezone does not matter. Actual
# maintenance is allowed once per Madrid day between 03:00 and 05:59, safely
# after the post-market deep and before TMAD's 12:00 operating window.
if (-not $Force) {
    if ($madridNow.Hour -lt 3 -or $madridNow.Hour -ge 6) { exit 0 }
    if (Test-Path $MarkerPath -PathType Leaf) {
        $last = (Get-Content $MarkerPath -Raw -ErrorAction SilentlyContinue).Trim()
        if ($last -eq $today) { exit 0 }
    }
}

$DataPython = Join-Path $ProductionPath "04_RUNTIME\venvs\data_bot\Scripts\python.exe"
$AlertsPython = Join-Path $ProductionPath "04_RUNTIME\venvs\alerts_bot\Scripts\python.exe"
$DataScript = Join-Path $ProductionPath "01_PROGRAMAS\data_bot\maintenance_v620.py"
$AlertsScript = Join-Path $ProductionPath "01_PROGRAMAS\alerts_bot\maintenance_v620.py"
foreach ($path in @($DataPython,$AlertsPython,$DataScript,$AlertsScript)) {
    if (-not (Test-Path $path -PathType Leaf)) { throw "Falta componente de mantenimiento: $path" }
}

$env:TMAD_INSTALL_ROOT = $ProductionPath
$env:TMAD_CONFIG_DIR = Join-Path $ProductionPath "02_CONFIG"
$env:TMAD_DATA_DIR = Join-Path $ProductionPath "03_DATOS"
$env:TMAD_RUNTIME_DIR = Join-Path $ProductionPath "04_RUNTIME"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-MaintLog "Inicio mantenimiento TMAD v$(if(Test-Path (Join-Path $ProductionPath 'VERSION.txt')){(Get-Content (Join-Path $ProductionPath 'VERSION.txt') -Raw).Trim()}else{'unknown'}) | Madrid=$($madridNow.ToString('yyyy-MM-dd HH:mm:ss'))"

$failed = $false
foreach ($job in @(
    @{Name="DATA"; Python=$DataPython; Script=$DataScript},
    @{Name="ALERTS"; Python=$AlertsPython; Script=$AlertsScript}
)) {
    Write-MaintLog "Ejecutando mantenimiento $($job.Name)..."
    $output = @(& $job.Python -u $job.Script 2>&1)
    $code = $LASTEXITCODE
    foreach ($line in $output) { Write-MaintLog ([string]$line) $job.Name }
    $joined = $output -join "`n"
    if ($code -ne 0 -or $joined -match '(?i)status=PARTIAL|DEFERRED_LOCK|Traceback|sqlite_failed=[1-9]|reason=alerts_db_missing') {
        $failed = $true
        Write-MaintLog "$($job.Name) no terminó limpio; se reintentará en la siguiente ventana horaria. exit=$code" "WARN"
    }
}

if ($failed) {
    Write-MaintLog "Mantenimiento incompleto; marker diario NO actualizado." "WARN"
    exit 2
}

Set-Content -Path $MarkerPath -Value $today -Encoding ASCII
Write-MaintLog "Mantenimiento diario completado; marker=$today" "OK"
exit 0
