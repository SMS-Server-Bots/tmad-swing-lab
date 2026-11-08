$ErrorActionPreference = 'Continue'
$ProgramLauncherDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ProgramLauncherDir '..\..')).Path.TrimEnd('\')
$RootPrefix = $Root + '\'
$Comparison = [System.StringComparison]::OrdinalIgnoreCase
$Stopped = 0

# Solo detiene procesos cuya línea de comandos apunta dentro de ESTA raíz exacta.
# Evita que ...\TMAD_Dual_Bot_OneClick coincida también con ...\TMAD_Dual_Bot_OneClick_2.
Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and
    $_.CommandLine -and
    $_.CommandLine.IndexOf($RootPrefix, $Comparison) -ge 0 -and
    ($_.Name -match '^(python|pythonw|powershell|pwsh|cmd)(\.exe)?$')
} | ForEach-Object {
    try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
        $Stopped++
    } catch {}
}

$LockDir = Join-Path $Root '04_RUNTIME\locks'
Remove-Item -Force -ErrorAction SilentlyContinue `
    (Join-Path $LockDir 'tmad_core_data_bot.lock'), `
    (Join-Path $LockDir 'tmad_alerts_adaptive_bot.lock')

Write-Host "Bots detenidos en $Root: $Stopped. Locks persistentes limpiados."
exit 0
