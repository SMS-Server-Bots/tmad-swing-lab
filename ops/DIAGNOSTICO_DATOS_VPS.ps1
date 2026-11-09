# TMAD - Diagnostico de datos y backups en el VPS. SOLO LECTURA: no modifica, borra ni para nada.
# Uso:  powershell -ExecutionPolicy Bypass -File DIAGNOSTICO_DATOS_VPS.ps1 -ProductionPath "C:\...\TMAD"
# Genera diagnostico_datos_<fecha>.txt junto al script. Pega su contenido en el chat o subelo al repo.
param(
    [Parameter(Mandatory = $true)][string]$ProductionPath,
    [string]$RepoPath = $ProductionPath
)
$ErrorActionPreference = "Continue"
$out = Join-Path $PSScriptRoot ("diagnostico_datos_{0}.txt" -f (Get-Date -Format "yyyyMMdd_HHmm"))
$DataRoot = Join-Path $ProductionPath "03_DATOS"
function W($t) { $t | Out-File -FilePath $out -Append -Encoding utf8; Write-Host $t }
function GB($b) { "{0:N2} GB" -f ($b / 1GB) }

W "=== TMAD DIAGNOSTICO DATOS $(Get-Date -Format o)"
W "ProductionPath=$ProductionPath  DataRoot=$DataRoot"

W "`n=== DISCOS"
Get-PSDrive -PSProvider FileSystem | ForEach-Object {
    if ($_.Used -ne $null) { W ("{0}: usado {1}  libre {2}  ({3:N1}% libre)" -f $_.Name, (GB $_.Used), (GB $_.Free), (100 * $_.Free / ($_.Used + $_.Free))) }
}
W "TEMP=$env:TEMP"

W "`n=== TAMANO POR CARPETA EN 03_DATOS (2 niveles)"
Get-ChildItem $DataRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $lvl1 = $_
    Get-ChildItem $lvl1.FullName -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $s = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        W ("{0,10}  {1}\{2}" -f (GB $s), $lvl1.Name, $_.Name)
    }
}

W "`n=== BASES SQLITE Y WAL"
Get-ChildItem $DataRoot -Recurse -File -Include *.db, *.db-wal, *.db-shm, *.sqlite -ErrorAction SilentlyContinue |
    Sort-Object Length -Descending | Select-Object -First 25 | ForEach-Object {
        W ("{0,10}  {1}  {2}" -f (GB $_.Length), $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm"), $_.FullName.Replace($DataRoot, "03_DATOS"))
    }

W "`n=== ARCHIVOS ZIP LOCALES (recuento y rango)"
foreach ($pat in @("data_bot\db\archive\20??-W*.zip", "data_bot\db\archive\intraday\*\intraday_*.zip", "alerts_bot\db\archive\layers\*\layers_*.zip")) {
    $f = Get-ChildItem (Join-Path $DataRoot $pat) -ErrorAction SilentlyContinue | Sort-Object Name
    if ($f) { W ("{0,4} ficheros  {1} -> {2}  ({3})  patron {4}" -f $f.Count, $f[0].Name, $f[-1].Name, (GB ($f | Measure-Object Length -Sum).Sum), $pat) }
    else { W "   0 ficheros  patron $pat" }
}

W "`n=== TAREAS PROGRAMADAS TMAD / TRUMPPETTA"
Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match "TMAD|Trumppetta" } | ForEach-Object {
    $i = $_ | Get-ScheduledTaskInfo
    W ("{0,-55} estado={1,-8} ultima={2}  resultado=0x{3:X}  proxima={4}" -f $_.TaskName, $_.State, $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime)
}

W "`n=== LOG BACKUP GITHUB (ultimas 60 lineas)"
$log = Join-Path $RepoPath "runtime_observability\tmad_data_backup\tmad_data_backup.log"
if (Test-Path $log) { Get-Content $log -Tail 60 | ForEach-Object { W $_ } } else { W "No existe $log" }
W "`n=== ERRORES/MUTABLE EN EL LOG (ultimas 20 coincidencias)"
if (Test-Path $log) { Select-String -Path $log -Pattern "ERROR|mutable|Mutable|espacio|space|7-Zip" | Select-Object -Last 20 | ForEach-Object { W $_.Line } }

W "`n=== HERRAMIENTAS"
foreach ($c in @("gh", "python", "py", "7z", "rclone", "aws")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    W ("{0,-8} {1}" -f $c, $(if ($cmd) { $cmd.Source } else { "NO" }))
}
if (Get-Command gh -ErrorAction SilentlyContinue) { W (gh auth status 2>&1 | Out-String) }
W "`nFIN. Fichero: $out"
