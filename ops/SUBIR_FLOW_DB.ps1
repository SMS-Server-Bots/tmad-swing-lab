# Sube una copia de la base de flujo (mapas de calor de opciones + velas 1 min) a la release privada
# "tmad-flow-snapshot" de tmad-swing-lab, para analizarla sin esperar al ZIP semanal. No para el bot.
# Uso: powershell -ExecutionPolicy Bypass -File .\SUBIR_FLOW_DB.ps1
param(
    [string]$InstallRoot = "C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick",
    [string]$Repo = "SMS-Server-Bots/tmad-swing-lab",
    [string]$Tag = "tmad-flow-snapshot"
)
$py = Join-Path $InstallRoot "04_RUNTIME\venvs\data_bot\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$out = Join-Path $env:TEMP ("tmad_flow_" + (Get-Date -Format "yyyyMMdd_HHmm"))
$ErrorActionPreference = "Continue"
& $py (Join-Path $PSScriptRoot "subir_flow_db.py") --db (Join-Path $InstallRoot "03_DATOS\data_bot\db\live\tmad_flow_live.db") --out $out 2>&1 | ForEach-Object { "$_" }
function Invoke-Gh([string[]]$GhArgs) { $o = & gh @GhArgs 2>&1 | ForEach-Object { "$_" }; [pscustomobject]@{ Code = $LASTEXITCODE; Text = ($o -join " ") } }
$f = Get-ChildItem -LiteralPath $out -Filter "*.gz" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $f) { Write-Host "No se generó la copia (ver mensajes de arriba)."; exit 1 }
if ((Invoke-Gh @("release", "view", $Tag, "--repo", $Repo)).Code -ne 0) {
    Invoke-Gh @("release", "create", $Tag, "--repo", $Repo, "--title", "Copia base de flujo (privado)", "--notes", "tmad_flow_live.db para análisis.") | Out-Null
}
$u = Invoke-Gh @("release", "upload", $Tag, $f.FullName, "--repo", $Repo, "--clobber")
if ($u.Code -eq 0) { Write-Host ("FLOW_DB_SUBIDA {0} ({1:N0} MB)" -f $f.Name, ($f.Length / 1MB)); Remove-Item -LiteralPath $out -Recurse -Force }
else { Write-Host "Fallo al subir: $($u.Text). Copia conservada en $out" }
