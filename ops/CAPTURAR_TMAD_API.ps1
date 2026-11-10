# Captura puntual (solo lectura) de endpoints de tmad.app que el bot no guarda (Score, watchlists, detalle por ticker)
# y la sube a la release privada "tmad-app-api-capture" de tmad-swing-lab. Usa la sesion del propio bot.
# Uso: powershell -ExecutionPolicy Bypass -File .\CAPTURAR_TMAD_API.ps1
param(
    [string]$InstallRoot = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$Repo = "SMS-Server-Bots/tmad-swing-lab",
    [string]$Tag = "tmad-app-api-capture"
)
$py = Join-Path $InstallRoot "04_RUNTIME\venvs\data_bot\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No encuentro el Python del Data Bot: $py" }
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$out = Join-Path $env:TEMP "tmad_api_capture_$stamp"
$ErrorActionPreference = "Continue"
& $py (Join-Path $PSScriptRoot "capturar_tmad_api.py") --install-root $InstallRoot --out $out 2>&1 | ForEach-Object { "$_" }
$zip = Join-Path $env:TEMP "tmad_api_capture_$stamp.zip"
Compress-Archive -Path (Join-Path $out "*") -DestinationPath $zip -Force
function Invoke-Gh([string[]]$GhArgs) { $o = & gh @GhArgs 2>&1 | ForEach-Object { "$_" }; [pscustomobject]@{ Code = $LASTEXITCODE; Text = ($o -join " ") } }
if ((Invoke-Gh @("release", "view", $Tag, "--repo", $Repo)).Code -ne 0) {
    Invoke-Gh @("release", "create", $Tag, "--repo", $Repo, "--title", "Captura API tmad.app (privado)", "--notes", "Respuestas JSON de endpoints no capturados por el bot. Sin cookies.") | Out-Null
}
$u = Invoke-Gh @("release", "upload", $Tag, $zip, "--repo", $Repo, "--clobber")
if ($u.Code -eq 0) { Write-Host ("SUBIDO {0} ({1:N1} MB)" -f (Split-Path $zip -Leaf), ((Get-Item $zip).Length / 1MB)) } else { Write-Host "Fallo al subir: $($u.Text)" }
Remove-Item -LiteralPath $out -Recurse -Force -ErrorAction SilentlyContinue
