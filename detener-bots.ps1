$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stopped = 0
Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and
    $_.CommandLine -and
    $_.CommandLine.Contains($Root) -and
    ($_.Name -match 'python|powershell')
} | ForEach-Object {
    try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
        $stopped++
    } catch {}
}
Remove-Item -Force -ErrorAction SilentlyContinue `
    (Join-Path $Root 'tmad_bot_data_v3\tmad_core_data_bot.lock'), `
    (Join-Path $Root 'tmad_bot_alerts_v3\tmad_alerts_adaptive_bot.lock')
Write-Host "Bots detenidos: $stopped. Locks limpiados."
exit 0
