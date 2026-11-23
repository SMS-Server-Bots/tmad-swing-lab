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
    & $Python -m swing_lab.run top10 --data-root $DataRoot 2>&1 | ForEach-Object { "$_" }
    $top10Exit = $LASTEXITCODE
    $last = Get-ChildItem (Join-Path $WorkDir "out") -Filter "eod_list_*.md" -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
    $t10 = Get-ChildItem (Join-Path $WorkDir "out") -Filter "top10_20*.md" -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -Last 1
    $parts = @()
    if ($t10) { $parts += Get-Content $t10.FullName -Raw -Encoding UTF8 }
    if ($last) { $parts += Get-Content $last.FullName -Raw -Encoding UTF8 }
    if ($parts.Count) { ($parts -join "`r`n`r`n---`r`n`r`n") | Set-Content (Join-Path $WorkDir "ULTIMA_LISTA.md") -Encoding UTF8 }
    $sbParts = @()
    foreach ($f in @("out\top10_scoreboard.md", "out\scoreboard.md")) { $p = Join-Path $WorkDir $f; if (Test-Path $p) { $sbParts += Get-Content $p -Raw -Encoding UTF8 } }
    if ($sbParts.Count) { ($sbParts -join "`r`n`r`n---`r`n`r`n") | Set-Content (Join-Path $WorkDir "MARCADOR.md") -Encoding UTF8 }
    # Copia del resultado en GitHub (release privada del laboratorio) para revisarlo sin entrar al VPS.
    $rel = "swing-lab-runtime"; $repo = "SMS-Server-Bots/tmad-swing-lab"
    & gh release view $rel --repo $repo *> $null
    if ($LASTEXITCODE -ne 0) { & gh release create $rel --repo $repo --title "Swing Lab: resultado diario (papel)" --notes "ULTIMA_LISTA, MARCADOR y registros del laboratorio. Se sustituyen cada noche." *> $null }
    $up = @(Join-Path $WorkDir "ULTIMA_LISTA.md"; Join-Path $WorkDir "MARCADOR.md") + @(Get-ChildItem (Join-Path $WorkDir "ledger") -Filter "*.csv" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $up = @($up | Where-Object { Test-Path $_ })
    if ($up.Count) { & gh release upload $rel @up --repo $repo --clobber 2>&1 | ForEach-Object { "$_" }; Write-Host "SWING_LAB_UPLOAD_EXIT_CODE=$LASTEXITCODE" }
    Write-Host "SWING_LAB_LIST_EXIT_CODE=$listExit"
    Write-Host "SWING_LAB_TRACK_EXIT_CODE=$trackExit"
    Write-Host "SWING_LAB_TOP10_EXIT_CODE=$top10Exit"
    Write-Host "SWING_LAB_OUTPUT=$WorkDir"
} finally {
    Pop-Location
    Stop-Transcript | Out-Null
}
