# TMAD Swing Lab - ejecucion diaria en el VPS (solo lectura sobre 03_DATOS).
# Programar despues del postmarket_deep (p. ej. 23:30 Madrid). No envia ordenes ni mensajes.
param(
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [string]$Python = "python",
    [string]$WorkDir = (Join-Path $PSScriptRoot "..\swing_lab_runtime")
)
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
Push-Location $WorkDir
try {
    $env:PYTHONPATH = $RepoRoot
    & $Python -m swing_lab.run list --data-root $DataRoot
    $listExit = $LASTEXITCODE
    & $Python -m swing_lab.run track --data-root $DataRoot
    $trackExit = $LASTEXITCODE
    Write-Host "SWING_LAB_LIST_EXIT_CODE=$listExit"
    Write-Host "SWING_LAB_TRACK_EXIT_CODE=$trackExit"
    Write-Host "SWING_LAB_OUTPUT=$WorkDir"
} finally {
    Pop-Location
}
