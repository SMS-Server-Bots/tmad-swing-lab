# TMAD - Backup remoto de la base de Alerts SIN copia completa en disco.
# Vuelca en streaming (solo lectura, instantanea coherente) a gzip por partes, verifica los SHA y sube
# a la release "tmad-alerts-db-latest" de tmad-dual-bot. Despues descarga una parte y comprueba su SHA.
# Espacio necesario: solo la salida comprimida (aprox. 10-20 % de la base).
# Uso:  powershell -ExecutionPolicy Bypass -File BACKUP_ALERTS_DB_STREAM.ps1 [-NoUpload] [-WorkDir D:\tmp]
param(
    [string]$ProductionPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$WorkDir = $env:TEMP,
    [string]$GitHubRepo = "SMS-Server-Bots/tmad-dual-bot",
    [string]$ReleaseTag = "tmad-alerts-db-latest",
    [int]$PartMB = 1800,
    [switch]$NoUpload
)
$ErrorActionPreference = "Stop"
$db = Join-Path $ProductionPath "03_DATOS\alerts_bot\db\live\tmad_alerts_adaptive.db"
$helper = Join-Path $PSScriptRoot "tmad_sqlite_stream_backup.py"
if (-not (Test-Path $db)) { throw "No existe $db" }
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw "Python no disponible" }

$dbGB = (Get-Item $db).Length / 1GB
$drive = (Get-Item $WorkDir).PSDrive
$freeGB = $drive.Free / 1GB
Write-Host ("Base {0:N2} GB | libre en {1}: {2:N2} GB" -f $dbGB, $drive.Name, $freeGB)
if ($freeGB -lt [Math]::Max(3, 0.3 * $dbGB)) { throw ("Espacio insuficiente en {0}: hacen falta ~{1:N1} GB" -f $drive.Name, [Math]::Max(3, 0.3 * $dbGB)) }

$out = Join-Path $WorkDir ("tmad_alerts_stream_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Force -Path $out | Out-Null
try {
    $t0 = Get-Date
    $stem = "tmad_alerts_adaptive_{0}" -f (Get-Date -Format "yyyyMMdd_HHmm")
    & $py $helper dump $db $out --part-mb $PartMB --stem $stem
    if ($LASTEXITCODE -ne 0) { throw "dump fallo ($LASTEXITCODE)" }
    & $py $helper verify $out
    if ($LASTEXITCODE -ne 0) { throw "verify fallo ($LASTEXITCODE)" }
    $files = Get-ChildItem $out -File | Sort-Object Name
    $sum = ($files | Measure-Object Length -Sum).Sum / 1GB
    Write-Host ("Volcado OK: {0} ficheros, {1:N2} GB comprimidos, {2:N0} min" -f $files.Count, $sum, ((Get-Date) - $t0).TotalMinutes)
    if ($NoUpload) { Write-Host "NoUpload: salida en $out"; return }

    $exists = gh release view $ReleaseTag --repo $GitHubRepo 2>$null
    if ($LASTEXITCODE -ne 0) {
        gh release create $ReleaseTag --repo $GitHubRepo --title "TMAD Alerts DB latest (stream dump)" --notes "Volcado SQL gzip por partes de tmad_alerts_adaptive.db. Restaurar con ops/tmad_sqlite_stream_backup.py restore." | Out-Null
    }
    $old = gh release view $ReleaseTag --repo $GitHubRepo --json assets --jq ".assets[].name" 2>$null
    foreach ($f in $files) { gh release upload $ReleaseTag $f.FullName --repo $GitHubRepo --clobber; if ($LASTEXITCODE -ne 0) { throw "upload fallo $($f.Name)" } }

    # Verificacion remota del juego NUEVO antes de retirar el anterior
    $chk = Join-Path $out "_verify_remote"; New-Item -ItemType Directory -Force -Path $chk | Out-Null
    gh release download $ReleaseTag --repo $GitHubRepo --pattern "$stem.manifest.json" --dir $chk --clobber
    $man = Get-Content (Join-Path $chk "$stem.manifest.json") -Raw | ConvertFrom-Json
    $p = $man.parts[0]
    gh release download $ReleaseTag --repo $GitHubRepo --pattern $p.name --dir $chk --clobber
    $h = (Get-FileHash -Algorithm SHA256 (Join-Path $chk $p.name)).Hash.ToLowerInvariant()
    if ($h -ne $p.sha256) { throw "SHA remoto distinto en $($p.name)" }
    $newNames = $files.Name
    foreach ($n in $old) { if ($n -and ($newNames -notcontains $n)) { gh release delete-asset $ReleaseTag $n --repo $GitHubRepo --yes | Out-Null } }
    Write-Host "BACKUP_ALERTS_STREAM_OK release=$ReleaseTag partes=$($man.parts.Count)"
} finally {
    if (-not $NoUpload) { Remove-Item -LiteralPath $out -Recurse -Force -ErrorAction SilentlyContinue }
}
