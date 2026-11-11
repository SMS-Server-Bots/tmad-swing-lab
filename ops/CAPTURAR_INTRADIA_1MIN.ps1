# Descarga las velas de 1 minuto (30 dias, con pre/post-market) de todo el universo de tmad.app y las sube
# a la release privada "tmad-intraday-1min" de tmad-swing-lab. Solo lectura sobre tmad.app; usa la sesion del bot.
# Tarda ~20-40 min. Basta con ejecutarlo cada 2-3 semanas (cada descarga cubre 30 dias).
# Uso: powershell -ExecutionPolicy Bypass -File .\CAPTURAR_INTRADIA_1MIN.ps1
param(
    [string]$InstallRoot = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$Repo = "SMS-Server-Bots/tmad-swing-lab",
    [string]$Tag = "tmad-intraday-1min"
)
$py = Join-Path $InstallRoot "04_RUNTIME\venvs\data_bot\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No encuentro el Python del Data Bot: $py" }
$out = Join-Path $env:TEMP ("tmad_intraday_" + (Get-Date -Format "yyyyMMdd_HHmm"))
$ErrorActionPreference = "Continue"
& $py (Join-Path $PSScriptRoot "capturar_intradia_1min.py") --install-root $InstallRoot --out $out 2>&1 | ForEach-Object { "$_" }
function Invoke-Gh([string[]]$GhArgs) { $o = & gh @GhArgs 2>&1 | ForEach-Object { "$_" }; [pscustomobject]@{ Code = $LASTEXITCODE; Text = ($o -join " ") } }
if ((Invoke-Gh @("release", "view", $Tag, "--repo", $Repo)).Code -ne 0) {
    Invoke-Gh @("release", "create", $Tag, "--repo", $Repo, "--title", "Velas 1 min tmad.app (privado)", "--notes", "Velas de 1 minuto con pre/post-market, 30 dias por descarga. Sin cookies.") | Out-Null
}
$files = Get-ChildItem -LiteralPath $out -File -ErrorAction SilentlyContinue
if (-not $files) { Write-Host "No se genero ningun archivo en $out"; exit 1 }
$allOk = $true
foreach ($f in $files) {
    $u = Invoke-Gh @("release", "upload", $Tag, $f.FullName, "--repo", $Repo, "--clobber")
    if ($u.Code -eq 0) { Write-Host ("SUBIDO {0} ({1:N1} MB)" -f $f.Name, ($f.Length / 1MB)) } else { $allOk = $false; Write-Host "Fallo al subir $($f.Name): $($u.Text)" }
}
if ($allOk) { Remove-Item -LiteralPath $out -Recurse -Force -ErrorAction SilentlyContinue; Write-Host "INTRADIA_1MIN_SUBIDO_OK" }
else { Write-Host "Se conservan los archivos en $out para reintentar" }
