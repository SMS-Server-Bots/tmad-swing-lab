# TMAD Swing Lab - ejecucion diaria en el VPS (SOLO LECTURA sobre 03_DATOS). No envia ordenes ni mensajes.
# Genera: lista EOD (politicas en papel + recorrido esperado), seguimiento, operaciones de la regla del hueco y marcador.
# Resultado legible: <WorkDir>\ULTIMA_LISTA.md y <WorkDir>\MARCADOR.md
param(
    [string]$DataRoot = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick\03_DATOS",
    [string]$Python = "python",
    [string]$WorkDir = (Join-Path $PSScriptRoot "..\swing_lab_runtime")
)
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$logDir = Join-Path $WorkDir "logs"; New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir ("swing_lab_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmm"))) | Out-Null
Push-Location $WorkDir
try {
    $env:PYTHONPATH = $RepoRoot
    $env:PYTHONIOENCODING = "utf-8"
    $ErrorActionPreference = "Continue"
    & $Python -m swing_lab.run list --data-root $DataRoot 2>&1 | ForEach-Object { "$_" }
    $listExit = $LASTEXITCODE
    & $Python -m swing_lab.run track --data-root $DataRoot 2>&1 | ForEach-Object { "$_" }
    $trackExit = $LASTEXITCODE
    $last = Get-ChildItem (Join-Path $WorkDir "out") -Filter "eod_list_*.md" -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
    if ($last) { Copy-Item $last.FullName (Join-Path $WorkDir "ULTIMA_LISTA.md") -Force }
    $sb = Join-Path $WorkDir "out\scoreboard.md"
    if (Test-Path $sb) { Copy-Item $sb (Join-Path $WorkDir "MARCADOR.md") -Force }
    Write-Host "SWING_LAB_LIST_EXIT_CODE=$listExit"
    Write-Host "SWING_LAB_TRACK_EXIT_CODE=$trackExit"
    Write-Host "SWING_LAB_OUTPUT=$WorkDir"
} finally {
    Pop-Location
    Stop-Transcript | Out-Null
}
