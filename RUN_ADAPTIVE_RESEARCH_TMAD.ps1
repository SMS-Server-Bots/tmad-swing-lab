param(
    [int]$MaxRounds = 0,
    [int]$MaxCandidates = 0,
    [int]$ParentsPerRound = 0,
    [int]$ChildrenPerParent = 0
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$ProgramDir = Join-Path $Root "01_PROGRAMAS\alerts_bot"
$RuntimeRoot = Join-Path $Root "04_RUNTIME"
$PythonExe = Join-Path $RuntimeRoot "venvs\alerts_bot\Scripts\python.exe"
$Runner = Join-Path $ProgramDir "adaptive_research_cycle.py"

if (-not (Test-Path $PythonExe -PathType Leaf)) {
    throw "No existe el Python persistente de Alerts Bot: $PythonExe"
}
if (-not (Test-Path $Runner -PathType Leaf)) {
    throw "Falta adaptive_research_cycle.py: $Runner"
}

$env:TMAD_INSTALL_ROOT = $Root
$env:TMAD_CONFIG_DIR = Join-Path $Root "02_CONFIG"
$env:TMAD_DATA_DIR = Join-Path $Root "03_DATOS"
$env:TMAD_RUNTIME_DIR = $RuntimeRoot
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if ($MaxRounds -gt 0) { $env:ADAPTIVE_RESEARCH_MAX_ROUNDS = "$MaxRounds" }
if ($MaxCandidates -gt 0) { $env:ADAPTIVE_RESEARCH_MAX_TOTAL_CANDIDATES = "$MaxCandidates" }
if ($ParentsPerRound -gt 0) { $env:ADAPTIVE_RESEARCH_PARENTS_PER_ROUND = "$ParentsPerRound" }
if ($ChildrenPerParent -gt 0) { $env:ADAPTIVE_RESEARCH_CHILDREN_PER_PARENT = "$ChildrenPerParent" }

Write-Host "[TMAD ADAPTIVE RESEARCH] validation-only | FINAL TEST locked"
& $PythonExe -u $Runner
exit $LASTEXITCODE
