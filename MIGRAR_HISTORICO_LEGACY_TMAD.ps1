[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$LegacyRoot = 'C:\Users\Administrator\Desktop\old_2\old Bot Scanner_data',
    [string]$GitHubRepo = 'SMS-Server-Bots/tmad-dual-bot'
)

$ErrorActionPreference = 'Stop'
$RuntimeDir = Join-Path $ProductionRoot '04_RUNTIME\history_migration'
$DoneFile = Join-Path $RuntimeDir 'legacy_old2_migration.done.json'
$ImportStatus = Join-Path $ProductionRoot '03_DATOS\alerts_bot\output\legacy_history_import.json'
$ColdCatalog = Join-Path $ProductionRoot '03_DATOS\data_bot\output\legacy_history_catalog.json'
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Log([string]$Level,[string]$Message) {
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),$Level,$Message
    Write-Host $line
    Add-Content -LiteralPath (Join-Path $RuntimeDir 'legacy_migration.log') -Value $line -Encoding UTF8
}

if (Test-Path $DoneFile -PathType Leaf) {
    Log 'OK' 'La migracion legacy ya esta completada.'
    exit 0
}
if (-not (Test-Path $LegacyRoot -PathType Container)) {
    Log 'WARN' "No existe $LegacyRoot. No se borra nada y se reintentara."
    exit 0
}

$AlertsPython = Join-Path $ProductionRoot '04_RUNTIME\venvs\alerts_bot\Scripts\python.exe'
if (-not (Test-Path $AlertsPython -PathType Leaf)) { throw "No existe Python Alerts: $AlertsPython" }
$ImportScript = Join-Path $RepoRoot '01_PROGRAMAS\alerts_bot\legacy_history_import.py'
$ColdScript = Join-Path $RepoRoot '01_PROGRAMAS\alerts_bot\legacy_cold_store.py'
foreach ($p in @($ImportScript,$ColdScript)) {
    if (-not (Test-Path $p -PathType Leaf)) { throw "Falta $p" }
}

$env:TMAD_INSTALL_ROOT = $ProductionRoot
$env:TMAD_CONFIG_DIR = Join-Path $ProductionRoot '02_CONFIG'
$env:TMAD_DATA_DIR = Join-Path $ProductionRoot '03_DATOS'
$env:TMAD_RUNTIME_DIR = Join-Path $ProductionRoot '04_RUNTIME'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

Log 'INFO' 'Fase 1/3: importando old_2 como historico canonico para Alerts/Learning...'
& $AlertsPython $ImportScript --legacy-root $LegacyRoot
if ($LASTEXITCODE -ne 0) { throw "legacy_history_import.py fallo: $LASTEXITCODE" }

$import = Get-Content $ImportStatus -Raw -Encoding UTF8 | ConvertFrom-Json
if ($import.status -notin @('ok','already_imported')) {
    throw "Import legacy no confirmado: $($import.status)"
}
if ($import.status -eq 'ok' -and [int]$import.features_imported -le 0) {
    throw 'Import legacy no genero feature_observations; se conserva old_2.'
}
Log 'OK' ("Historico old_2 ya utilizable por el bot: fechas={0}; features={1}; precios={2}" -f $import.date_count,$import.features_imported,$import.price_rows_imported)

Log 'INFO' 'Fase 2/3: archivando datos originales en GitHub Releases con chunks comprimidos y verificados...'
$coldOutput = @(& $AlertsPython $ColdScript --legacy-root $LegacyRoot --production-root $ProductionRoot --repo $GitHubRepo --chunk-mb 128 2>&1)
$coldExit = $LASTEXITCODE
foreach ($line in $coldOutput) {
    if ($null -ne $line -and [string]$line -ne '') { Log 'COLD' ([string]$line) }
}
if ($coldExit -ne 0) {
    Log 'WARN' ("Cold-store aun no ha terminado (exit={0}). El estado es resumible; old_2 permanece intacto." -f $coldExit)
    exit 2
}
$cold = Get-Content $ColdCatalog -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not [bool]$cold.verified) {
    Log 'WARN' 'Cold-store no esta 100% verificado; old_2 permanece intacto.'
    exit 2
}

Log 'INFO' 'Fase 3/3: comprobaciones finales antes de liberar old_2...'
if (-not (Test-Path $ImportStatus -PathType Leaf)) { throw 'Falta evidencia de importacion Alerts.' }
if (-not (Test-Path $ColdCatalog -PathType Leaf)) { throw 'Falta catalogo cold-store.' }

$bytes = (Get-ChildItem $LegacyRoot -File -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
$record = [ordered]@{
    completed_at = (Get-Date).ToString('o')
    source = $LegacyRoot
    source_bytes_before_delete = [long]$bytes
    import_status = [string]$import.status
    imported_dates = [int]($import.date_count)
    features_imported = [int]($import.features_imported)
    price_rows_imported = [int]($import.price_rows_imported)
    cold_release = [string]$cold.release_tag
    cold_verified = [bool]$cold.verified
    cold_included_bytes = [long]$cold.included_bytes
    cold_excluded_bytes = [long]$cold.excluded_bytes
    source_deleted = $false
}

# Solo ahora: usable por Alerts + copia cold verificada.
Remove-Item -LiteralPath $LegacyRoot -Recurse -Force
if (Test-Path $LegacyRoot) { throw 'No se pudo eliminar completamente old Bot Scanner_data.' }
$record.source_deleted = $true
$record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $DoneFile -Encoding UTF8

Log 'OK' ("Migracion COMPLETA. old Bot Scanner_data eliminado tras verificar uso + backup. Liberados aprox {0:N2} GB." -f ($bytes/1GB))
exit 0
