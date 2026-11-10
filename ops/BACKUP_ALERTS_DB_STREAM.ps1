# TMAD - Backup remoto de la base de Alerts SIN copia completa en disco.
# Vuelca en streaming (solo lectura, instantanea coherente) a gzip por partes, verifica los SHA y sube
# a la release "tmad-alerts-db-latest" de tmad-dual-bot, descarga cada parte y comprueba su SHA.
# Espacio necesario: solo la salida comprimida (aprox. 10-20 % de la base).
# Uso:  powershell -ExecutionPolicy Bypass -File BACKUP_ALERTS_DB_STREAM.ps1 [-NoUpload] [-UploadOnly <carpeta>] [-WorkDir D:\tmp]
param(
    [string]$ProductionPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$WorkDir = $env:TEMP,
    [string]$GitHubRepo = "SMS-Server-Bots/tmad-dual-bot",
    [string]$ReleaseTag = "tmad-alerts-db-latest",
    [int]$PartMB = 1800,
    [switch]$NoUpload,
    [string]$UploadOnly = ""   # carpeta de un volcado ya hecho: solo sube y verifica
)
$ErrorActionPreference = "Stop"
$logDir = Join-Path $PSScriptRoot "logs"; New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir ("alerts_stream_backup_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmm"))) | Out-Null
Get-ChildItem $logDir -Filter "alerts_stream_backup_*.log" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue
# Restos de ejecuciones anteriores interrumpidas
Get-ChildItem $WorkDir -Directory -Filter "tmad_alerts_stream_*" -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-6) } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
function Invoke-Gh([string[]]$GhArgs) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $o = & gh @GhArgs 2>&1 | ForEach-Object { "$_" }; $c = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
    [pscustomobject]@{ Code = $c; Lines = @($o | Where-Object { $_ -and $_ -notmatch "^\s*$" }); Text = ($o -join " ") }
}
$keep = $false
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

$out = if ($UploadOnly) { $UploadOnly } else { Join-Path $WorkDir ("tmad_alerts_stream_{0}" -f (Get-Date -Format "yyyyMMdd_HHmmss")) }
New-Item -ItemType Directory -Force -Path $out | Out-Null
try {
    $t0 = Get-Date
    if ($UploadOnly) {
        $stem = (Get-ChildItem $out -Filter "*.manifest.json" | Select-Object -First 1).Name -replace "\.manifest\.json$", ""
        if (-not $stem) { throw "No hay manifest en $out" }
    } else {
    $stem = "tmad_alerts_adaptive_{0}" -f (Get-Date -Format "yyyyMMdd_HHmm")
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & $py $helper dump $db $out --part-mb $PartMB --stem $stem 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE; $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "dump fallo ($code)" }
    }
    & $py $helper verify $out
    if ($LASTEXITCODE -ne 0) { throw "verify fallo ($LASTEXITCODE)" }
    $files = Get-ChildItem $out -File | Sort-Object Name
    $sum = ($files | Measure-Object Length -Sum).Sum / 1GB
    Write-Host ("Volcado OK: {0} ficheros, {1:N2} GB comprimidos, {2:N0} min" -f $files.Count, $sum, ((Get-Date) - $t0).TotalMinutes)
    if ($NoUpload) { $keep = $true; Write-Host "NoUpload: salida en $out"; return }

    $exists = Invoke-Gh @("release", "view", $ReleaseTag, "--repo", $GitHubRepo, "--json", "tagName")
    if ($exists.Code -ne 0) {
        Write-Host "Creando release $ReleaseTag"
        $r = Invoke-Gh @("release", "create", $ReleaseTag, "--repo", $GitHubRepo, "--title", "TMAD Alerts DB latest (stream dump)",
                         "--notes", "Volcado SQL gzip por partes de tmad_alerts_adaptive.db. Restaurar con ops/tmad_sqlite_stream_backup.py restore (repo tmad-swing-lab).")
        if ($r.Code -ne 0) { throw "No se pudo crear la release: $($r.Text)" }
    }
    $old = @((Invoke-Gh @("release", "view", $ReleaseTag, "--repo", $GitHubRepo, "--json", "assets", "--jq", ".assets[].name")).Lines)
    foreach ($f in $files) {
        Write-Host "Subiendo $($f.Name) ($([math]::Round($f.Length/1MB)) MB)"
        $u = Invoke-Gh @("release", "upload", $ReleaseTag, $f.FullName, "--repo", $GitHubRepo, "--clobber")
        if ($u.Code -ne 0) { $keep = $true; throw "upload fallo $($f.Name): $($u.Text)" }
    }

    # Verificacion remota del juego NUEVO antes de retirar el anterior
    $chk = Join-Path $out "_verify_remote"; New-Item -ItemType Directory -Force -Path $chk | Out-Null
    $d1 = Invoke-Gh @("release", "download", $ReleaseTag, "--repo", $GitHubRepo, "--pattern", "$stem.manifest.json", "--dir", $chk, "--clobber")
    if ($d1.Code -ne 0) { $keep = $true; throw "No se pudo descargar el manifest: $($d1.Text)" }
    $man = Get-Content (Join-Path $chk "$stem.manifest.json") -Raw | ConvertFrom-Json
    foreach ($p in $man.parts) {
        $d2 = Invoke-Gh @("release", "download", $ReleaseTag, "--repo", $GitHubRepo, "--pattern", $p.name, "--dir", $chk, "--clobber")
        if ($d2.Code -ne 0) { $keep = $true; throw "No se pudo descargar $($p.name): $($d2.Text)" }
        $h = (Get-FileHash -Algorithm SHA256 (Join-Path $chk $p.name)).Hash.ToLowerInvariant()
        if ($h -ne $p.sha256) { $keep = $true; throw "SHA remoto distinto en $($p.name)" }
        Remove-Item -LiteralPath (Join-Path $chk $p.name) -Force
        Write-Host "Verificado en remoto: $($p.name)"
    }
    $newNames = $files.Name
    foreach ($n in $old) { if ($n -and ($newNames -notcontains $n)) { Invoke-Gh @("release", "delete-asset", $ReleaseTag, $n, "--repo", $GitHubRepo, "--yes") | Out-Null; Write-Host "Retirado juego anterior: $n" } }
    Write-Host "BACKUP_ALERTS_STREAM_OK release=$ReleaseTag partes=$($man.parts.Count)"
} finally {
    if ($keep) { Write-Host "Se conserva el volcado para reintentar: -UploadOnly `"$out`"" -ForegroundColor Yellow }
    else { Remove-Item -LiteralPath $out -Recurse -Force -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
}
