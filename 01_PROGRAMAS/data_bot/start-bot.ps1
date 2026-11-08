param(
    [ValidateSet('daemon','setup-only','scanner-once','gex-once')]
    [string]$Mode = 'daemon'
)

$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'TMAD - DATA BOT'
$ProgramDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ProgramDir '..\..')).Path
$ConfigDir = Join-Path $Root '02_CONFIG'
$DataRoot = Join-Path $Root '03_DATOS'
$RuntimeRoot = Join-Path $Root '04_RUNTIME'
$VenvDir = Join-Path $RuntimeRoot 'venvs\data_bot'
$RequirementsMarker = Join-Path $RuntimeRoot 'data_bot_requirements_ok.txt'

$env:TMAD_INSTALL_ROOT = $Root
$env:TMAD_CONFIG_DIR = $ConfigDir
$env:TMAD_DATA_DIR = $DataRoot
$env:TMAD_RUNTIME_DIR = $RuntimeRoot
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try {
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $Utf8NoBom
    [Console]::InputEncoding = $Utf8NoBom
    $OutputEncoding = $Utf8NoBom
} catch {}
Set-Location $ProgramDir

$ConsoleTranscriptStarted = $false
if ($Mode -eq 'daemon') {
    try {
        $ConsoleLogDir = Join-Path $DataRoot 'data_bot\logs'
        New-Item -ItemType Directory -Path $ConsoleLogDir -Force | Out-Null
        $ConsoleLogPath = Join-Path $ConsoleLogDir ("tmad_data_console_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
        Start-Transcript -Path $ConsoleLogPath -Append -Force | Out-Null
        $ConsoleTranscriptStarted = $true
        Write-Host "[LOG] Consola completa: $ConsoleLogPath"
    } catch {
        Write-Host ('[WARN] No se pudo iniciar transcript de consola: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

Write-Host '========================================='
Write-Host ' TMAD Core Data Bot v6.2.67'
Write-Host '========================================='
Write-Host "[PATH] Programas: $ProgramDir"
Write-Host "[PATH] Config:    $ConfigDir"
Write-Host "[PATH] Datos:     $DataRoot"

if (!(Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {
    Write-Host '[SETUP] Creando entorno virtual persistente...'
    New-Item -ItemType Directory -Path (Split-Path $VenvDir -Parent) -Force | Out-Null
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv $VenvDir }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { python -m venv $VenvDir }
    else { throw 'No se encontro Python 3. Instalalo y marca Add Python to PATH.' }
}
$PythonExe = Join-Path $VenvDir 'Scripts\python.exe'
$RequirementsFile = Join-Path $ProgramDir 'requirements.txt'
$RequirementsHash = (Get-FileHash $RequirementsFile -Algorithm SHA256).Hash
$InstalledRequirementsHash = if (Test-Path $RequirementsMarker) { (Get-Content $RequirementsMarker -Raw).Trim() } else { '' }
$NeedsDependencies = $InstalledRequirementsHash -ne $RequirementsHash
if ($NeedsDependencies) {
    Write-Host '[SETUP] Instalando dependencias del Data Bot...'
    & $PythonExe -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Fallo actualizando pip del Data Bot.' }
    & $PythonExe -m pip install --disable-pip-version-check -r $RequirementsFile
    if ($LASTEXITCODE -ne 0) { throw 'Fallo instalando requirements del Data Bot.' }
    Set-Content -Path $RequirementsMarker -Value $RequirementsHash -Encoding ASCII
} else {
    Write-Host '[SETUP] Dependencias del Data Bot ya preparadas; instalacion omitida.'
}
if (!(Test-Path (Join-Path $ConfigDir 'data_bot.env'))) { throw 'Falta 02_CONFIG\data_bot.env' }
if (!(Test-Path (Join-Path $ConfigDir 'data_bot.secret.env'))) { Copy-Item (Join-Path $ConfigDir 'data_bot.secret.env.example') (Join-Path $ConfigDir 'data_bot.secret.env') }
if (!(Test-Path (Join-Path $ConfigDir 'tmad_session.json'))) { throw 'Falta 02_CONFIG\tmad_session.json' }
& $PythonExe -c "import config; print('[CONFIG DATA] scanner_times={0} | deep_times={1} | hourly_cap={2} deep_cap={3} | strict_total={4} weekends={5} | heartbeat={6}m peer_stale={7}s'.format(config.SCANNER_CAPTURE_TIMES,config.DAILY_DEEP_CAPTURE_TIMES,config.HOURLY_ENRICH_MAX_TICKERS,config.DEEP_ENRICH_MAX_TICKERS,config.SCANNER_STRICT_TOTAL_VALIDATION,config.RUN_WEEKENDS,config.HEARTBEAT_MINUTES,config.COORDINATION_STALE_SECONDS))"
if ($LASTEXITCODE -ne 0) { throw 'No se pudo leer la configuracion efectiva de Data.' }
if ($Mode -eq 'setup-only') { Write-Host '[OK] Data Bot preparado.'; exit 0 }
$RunExitCode = 0
try {
    if ($Mode -eq 'scanner-once') { & $PythonExe -u main.py --once scanner --session manual_once }
    elseif ($Mode -eq 'gex-once') { & $PythonExe -u main.py --once gex --session manual_gex_once }
    else { & $PythonExe -u main.py }
    $RunExitCode = $LASTEXITCODE
} finally {
    if ($ConsoleTranscriptStarted) { try { Stop-Transcript | Out-Null } catch {} }
}
exit $RunExitCode
