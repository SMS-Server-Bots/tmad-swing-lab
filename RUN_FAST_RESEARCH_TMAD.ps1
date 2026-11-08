param(
    [ValidateSet("all","rolling_latest","fixed_benchmark")]
    [string]$Lane = "all",
    [switch]$UnlockFinalTest,
    [int]$MaxCandidates = 0,
    [int]$TopReport = 0
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$ProgramDir = Join-Path $Root "01_PROGRAMAS\alerts_bot"
$RuntimeRoot = Join-Path $Root "04_RUNTIME"
$PythonExe = Join-Path $RuntimeRoot "venvs\alerts_bot\Scripts\python.exe"
$Runner = Join-Path $ProgramDir "run_fast_autonomous_research.py"

if (-not (Test-Path $PythonExe -PathType Leaf)) {
    throw "No existe el Python persistente de Alerts Bot: $PythonExe"
}
if (-not (Test-Path $Runner -PathType Leaf)) {
    throw "Falta runner de investigacion rapida: $Runner"
}

$env:TMAD_INSTALL_ROOT = $Root
$env:TMAD_CONFIG_DIR = Join-Path $Root "02_CONFIG"
$env:TMAD_DATA_DIR = Join-Path $Root "03_DATOS"
$env:TMAD_RUNTIME_DIR = $RuntimeRoot
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$argsList = @("-u", $Runner, "--lane", $Lane)
if ($UnlockFinalTest) { $argsList += "--unlock-final-test" }
if ($MaxCandidates -gt 0) { $argsList += @("--max-candidates", "$MaxCandidates") }
if ($TopReport -gt 0) { $argsList += @("--top-report", "$TopReport") }

$mode = if ($UnlockFinalTest) { "FINAL_TEST_UNLOCKED" } else { "SCREEN_ONLY" }
Write-Host "[TMAD FAST RESEARCH] mode=$mode lane=$Lane"
& $PythonExe @argsList
exit $LASTEXITCODE
