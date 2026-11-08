$ErrorActionPreference = 'Stop'
$LauncherErrorLog = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '.runtime\launcher_error.log'
trap {
    New-Item -ItemType Directory -Path (Split-Path -Parent $LauncherErrorLog) -Force | Out-Null
    $msg = ('[' + (Get-Date).ToString('o') + '] ' + $_.Exception.Message + [Environment]::NewLine + $_.ScriptStackTrace)
    Set-Content -Path $LauncherErrorLog -Value $msg -Encoding UTF8
    Write-Host ('[ERROR] ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (!(Test-Path $PowerShellExe)) {
    $resolvedPowerShell = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if ($resolvedPowerShell) { $PowerShellExe = $resolvedPowerShell.Source }
    else { throw 'No se encontro Windows PowerShell.' }
}
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = Join-Path $Root 'tmad_bot_data_v3'
$AlertsDir = Join-Path $Root 'tmad_bot_alerts_v3'
$RuntimeDir = Join-Path $Root '.runtime'
$ChannelsFile = Join-Path $Root 'configuracion_canales.env'
$FirstRunMarker = Join-Path $RuntimeDir 'first_run_complete.json'

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

function Read-EnvFile([string]$Path) {
    $values = @{}
    if (!(Test-Path $Path)) { return $values }
    foreach ($line in Get-Content $Path -Encoding UTF8) {
        $trim = $line.Trim()
        if (!$trim -or $trim.StartsWith('#') -or !$trim.Contains('=')) { continue }
        $parts = $trim.Split('=', 2)
        $values[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $values
}

function Write-Secrets($channels) {
    $dataSecret = @"
DISCORD_WEBHOOK_URL_DATA_LOG=$($channels['DATA_LOG_WEBHOOK'])
DISCORD_WEBHOOK_URL_DATA_ERROR=$($channels['DATA_ERROR_WEBHOOK'])
DISCORD_WEBHOOK_URL_DATA_SCHEMA=$($channels['DATA_SCHEMA_WEBHOOK'])
"@
    Set-Content -Path (Join-Path $DataDir '.env.secret') -Value $dataSecret -Encoding UTF8

    $alertsSecret = @"
DISCORD_WEBHOOK_URL_ALERTS=$($channels['ALERTS_LIVE_WEBHOOK'])
DISCORD_WEBHOOK_URL_ALERTS_LOG=$($channels['ALERTS_LOG_WEBHOOK'])
DISCORD_WEBHOOK_URL_ALERTS_ERROR=$($channels['ALERTS_ERROR_WEBHOOK'])
DISCORD_WEBHOOK_URL_RESEARCH=$($channels['ALERTS_RESEARCH_WEBHOOK'])
DISCORD_WEBHOOK_URL_DAILY=$($channels['DAILY_CALIBRATION_WEBHOOK'])
"@
    Set-Content -Path (Join-Path $AlertsDir '.env.secret') -Value $alertsSecret -Encoding UTF8
}

Write-Host '============================================================'
Write-Host ' TMAD DUAL BOT - DATA + ALERTAS ADAPTATIVAS'
Write-Host '============================================================'

if (!(Test-Path $ChannelsFile)) {
    throw "No existe $ChannelsFile"
}

$channels = Read-EnvFile $ChannelsFile
$configured = @($channels.Values | Where-Object { $_ -and $_.StartsWith('http') }).Count
$PromptMarker = Join-Path $RuntimeDir 'channels_prompted.flag'
if ($configured -eq 0 -and !(Test-Path $PromptMarker)) {
    Write-Host ''
    Write-Host '[CONFIG] No hay webhooks configurados.' -ForegroundColor Yellow
    Write-Host 'Se abrirá el fichero de canales. Pega los webhooks, guarda y cierra Notepad.'
    Start-Process notepad.exe -ArgumentList ('"' + $ChannelsFile + '"') -Wait
    Set-Content -Path $PromptMarker -Value (Get-Date).ToString('o') -Encoding UTF8
    $channels = Read-EnvFile $ChannelsFile
    $configured = @($channels.Values | Where-Object { $_ -and $_.StartsWith('http') }).Count
}
Write-Secrets $channels
Write-Host "[CONFIG] Webhooks configurados: $configured/8"

$SessionFile = Join-Path $DataDir 'tmad_session.json'
if (!(Test-Path $SessionFile)) {
    throw 'Falta tmad_session.json. Copia una sesión válida de TMAD en tmad_bot_data_v3.'
}

Write-Host '[SETUP] Preparando Data Bot...'
& $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $DataDir 'start-bot.ps1') -Mode setup-only
if ($LASTEXITCODE -ne 0) { throw 'Falló el setup del Data Bot.' }

Write-Host '[SETUP] Preparando Alerts Bot...'
& $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AlertsDir 'start-bot.ps1') -Mode setup-only
if ($LASTEXITCODE -ne 0) { throw 'Falló el setup del Alerts Bot.' }

if (!(Test-Path $FirstRunMarker)) {
    Write-Host ''
    Write-Host '[FIRST RUN] Captura real inicial Scanner + GEX...' -ForegroundColor Cyan
    Push-Location $DataDir
    & '.\.venv\Scripts\python.exe' -u main.py --once scanner --session first_run
    $dataExit = $LASTEXITCODE
    Pop-Location

    $baselineExit = 1
    if ($dataExit -eq 0) {
        Write-Host '[FIRST RUN] Importando histórico, etiquetando outcomes y creando baseline...' -ForegroundColor Cyan
        Push-Location $AlertsDir
        & '.\.venv\Scripts\python.exe' -u main.py --once baseline
        $baselineExit = $LASTEXITCODE
        Pop-Location
    } else {
        Write-Host '[FIRST RUN] La captura inicial falló. Los daemons arrancarán y volverán a intentarlo.' -ForegroundColor Yellow
    }

    $marker = @{
        attempted_at = (Get-Date).ToString('o')
        data_exit_code = $dataExit
        baseline_exit_code = $baselineExit
        completed = ($dataExit -eq 0 -and $baselineExit -eq 0)
    } | ConvertTo-Json
    if ($dataExit -eq 0 -and $baselineExit -eq 0) {
        Set-Content -Path $FirstRunMarker -Value $marker -Encoding UTF8
    } else {
        Set-Content -Path (Join-Path $RuntimeDir 'first_run_last_attempt.json') -Value $marker -Encoding UTF8
    }

    $BaselineHtml = Join-Path $AlertsDir 'output\initial_baseline\baseline_report.html'
    if (Test-Path $BaselineHtml) {
        Start-Process $BaselineHtml
    }
}

Write-Host ''
Write-Host '[START] Abriendo Data Bot y Alerts Bot en ventanas separadas...' -ForegroundColor Green
Start-Process -FilePath $PowerShellExe -WorkingDirectory $DataDir -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File','.\start-bot.ps1','-Mode','daemon'
Start-Sleep -Seconds 3
Start-Process -FilePath $PowerShellExe -WorkingDirectory $AlertsDir -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File','.\start-bot.ps1','-Mode','daemon'

Write-Host '[OK] Los dos bots están arrancados.' -ForegroundColor Green
Write-Host 'Data Bot: captura horaria combinada Scanner + GEX.'
Write-Host 'Alerts Bot: escanea cada nuevo run y calibra diariamente al cierre.'
