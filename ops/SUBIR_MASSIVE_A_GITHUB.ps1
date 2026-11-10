# Sube los CSV.gz descargados por massive_grouped_daily_fetch.py a la release privada "massive-grouped-daily"
# de tmad-swing-lab, para analizarlos fuera del VPS. Uso:
#   powershell -ExecutionPolicy Bypass -File .\SUBIR_MASSIVE_A_GITHUB.ps1 -Dir C:\Users\Administrator\Desktop\massive_daily
param(
    [string]$Dir = "C:\Users\Administrator\Desktop\massive_daily",
    [string]$Repo = "SMS-Server-Bots/tmad-swing-lab",
    [string]$Tag = "massive-grouped-daily"
)
function Invoke-Gh([string[]]$GhArgs) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $o = & gh @GhArgs 2>&1 | ForEach-Object { "$_" }; $c = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
    [pscustomobject]@{ Code = $c; Text = ($o -join " ") }
}
$files = Get-ChildItem $Dir -File | Where-Object { $_.Name -like "grouped_daily_*.csv.gz" -or $_.Name -eq "status.json" } | Sort-Object Name
if (-not $files) { throw "No hay ficheros en $Dir" }
if ((Invoke-Gh @("release", "view", $Tag, "--repo", $Repo)).Code -ne 0) {
    $r = Invoke-Gh @("release", "create", $Tag, "--repo", $Repo, "--title", "Massive grouped daily (privado)", "--notes", "OHLCV diario oficial EE. UU. descargado desde el VPS. Uso interno.")
    if ($r.Code -ne 0) { throw "No se pudo crear la release: $($r.Text)" }
}
foreach ($f in $files) {
    $u = Invoke-Gh @("release", "upload", $Tag, $f.FullName, "--repo", $Repo, "--clobber")
    if ($u.Code -ne 0) { throw "Fallo subiendo $($f.Name): $($u.Text)" }
    Write-Host ("Subido {0} ({1:N1} MB)" -f $f.Name, ($f.Length / 1MB))
}
Write-Host "MASSIVE_UPLOAD_OK ficheros=$($files.Count)"
