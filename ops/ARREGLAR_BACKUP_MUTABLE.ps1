# TMAD - Desatasca el backup mutable y libera el WAL de Alerts.
# Por defecto SOLO MUESTRA lo que haria. Con -Apply actua. No toca los bots ni sus tareas.
#   1) Para la tarea Trumppetta_TMAD_Data_Mutable_Backup si esta atascada y la DESACTIVA
#      (se sustituye por BACKUP_ALERTS_DB_STREAM.ps1).
#   2) Termina procesos python que ejecuten TMAD_DATA_SNAPSHOT.py (solo ese script).
#   3) Borra carpetas temporales tmad_data_backup_* de %TEMP% (solo ese patron).
#   4) Pide a SQLite un checkpoint del WAL de Alerts: PASSIVE (no bloquea al bot) y despues
#      TRUNCATE con espera maxima de 5 s (si el bot esta escribiendo, se omite sin bloquearlo).
# Uso:  powershell -ExecutionPolicy Bypass -File .\ARREGLAR_BACKUP_MUTABLE.ps1          (simulacion)
#       powershell -ExecutionPolicy Bypass -File .\ARREGLAR_BACKUP_MUTABLE.ps1 -Apply   (aplicar)
param(
    [string]$ProductionPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$TaskName = 'Trumppetta_TMAD_Data_Mutable_Backup',
    [switch]$Apply
)
$ErrorActionPreference = "Continue"
$db = Join-Path $ProductionPath "03_DATOS\alerts_bot\db\live\tmad_alerts_adaptive.db"
$wal = "$db-wal"
function Free { "{0:N2} GB" -f ((Get-PSDrive C).Free / 1GB) }
function WalGB { if (Test-Path $wal) { "{0:N2} GB" -f ((Get-Item $wal).Length / 1GB) } else { "sin WAL" } }
$mode = if ($Apply) { "APLICAR" } else { "SIMULACION (usa -Apply para actuar)" }
Write-Host "=== MODO: $mode"
Write-Host "Libre en C: $(Free) | WAL Alerts: $(WalGB)"

Write-Host "`n=== 1) Tarea $TaskName"
$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($t) {
    $i = $t | Get-ScheduledTaskInfo
    Write-Host ("Estado={0} Ultima={1} Resultado=0x{2:X}" -f $t.State, $i.LastRunTime, $i.LastTaskResult)
    if ($Apply) {
        if ($t.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName; Write-Host "Parada." }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null; Write-Host "Desactivada."
    } else { Write-Host "Se pararia (si esta en ejecucion) y se desactivaria." }
} else { Write-Host "No existe." }

Write-Host "`n=== 2) Procesos TMAD_DATA_SNAPSHOT.py"
$procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'TMAD_DATA_SNAPSHOT\.py' }
if ($procs) {
    foreach ($p in $procs) {
        Write-Host ("PID {0} desde {1}: {2}" -f $p.ProcessId, $p.CreationDate, $p.CommandLine)
        if ($Apply) { Stop-Process -Id $p.ProcessId -Force; Write-Host "  terminado" }
    }
} else { Write-Host "Ninguno." }
$sevenZ = Get-Process 7z -ErrorAction SilentlyContinue
if ($sevenZ) { Write-Host "Aviso: hay procesos 7z en ejecucion (PID $($sevenZ.Id -join ','))."; if ($Apply) { $sevenZ | Stop-Process -Force; Write-Host "  terminados" } }

Write-Host "`n=== 3) Temporales tmad_data_backup_* en $env:TEMP"
$tmp = Get-ChildItem $env:TEMP -Directory -Filter 'tmad_data_backup_*' -ErrorAction SilentlyContinue
if ($tmp) {
    foreach ($d in $tmp) {
        $s = (Get-ChildItem $d.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        Write-Host ("{0,10:N2} GB  {1}" -f ($s / 1GB), $d.FullName)
        if ($Apply) { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction Continue; Write-Host "  borrada" }
    }
} else { Write-Host "Ninguna." }

Write-Host "`n=== 4) Checkpoint del WAL de Alerts"
$walBytes = if (Test-Path $wal) { (Get-Item $wal).Length } else { 0 }
if ($walBytes -eq 0) { Write-Host "Sin WAL." }
elseif ((Get-PSDrive C).Free -lt $walBytes) { Write-Host "Espacio libre menor que el WAL: no se hace checkpoint (podria crecer la base)." }
elseif ($Apply) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    $code = @"
import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=5)
c.execute('PRAGMA busy_timeout=5000')
print('PASSIVE',c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
try: print('TRUNCATE',c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
except Exception as e: print('TRUNCATE omitido:',e)
c.close()
"@
    & $py -c $code $db
    Write-Host "(resultado = busy, paginas_wal, paginas_copiadas; busy=1 significa que el bot estaba escribiendo)"
} else { Write-Host "Se haria checkpoint PASSIVE y TRUNCATE (espera maxima 5 s)." }

Write-Host "`n=== RESULTADO: libre en C: $(Free) | WAL Alerts: $(WalGB)"
