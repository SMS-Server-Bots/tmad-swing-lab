param(
    [ValidateSet('daemon','setup-only','alerts-once','labels-once','train-once','baseline','daily','status')]
    [string]$Mode = 'daemon'
)

$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'TMAD - ALERTS BOT'
$ProgramDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ProgramDir '..\..')).Path
$ConfigDir = Join-Path $Root '02_CONFIG'
$DataRoot = Join-Path $Root '03_DATOS'
$RuntimeRoot = Join-Path $Root '04_RUNTIME'
$VenvDir = Join-Path $RuntimeRoot 'venvs\alerts_bot'
$RequirementsMarker = Join-Path $RuntimeRoot 'alerts_bot_requirements_ok.txt'
$AlertsDbReadyFile = Join-Path $RuntimeRoot 'alerts_db_ready.json'

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
        $ConsoleLogDir = Join-Path $DataRoot 'alerts_bot\logs'
        New-Item -ItemType Directory -Path $ConsoleLogDir -Force | Out-Null
        $ConsoleLogPath = Join-Path $ConsoleLogDir ("tmad_alerts_console_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
        Start-Transcript -Path $ConsoleLogPath -Append -Force | Out-Null
        $ConsoleTranscriptStarted = $true
        Write-Host "[LOG] Consola completa: $ConsoleLogPath"
    } catch {
        Write-Host ('[WARN] No se pudo iniciar transcript de consola: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

Write-Host '========================================='
Write-Host ' TMAD Alerts Adaptive Bot v6.2.67'
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
    Write-Host '[SETUP] Instalando dependencias del Alerts Bot...'
    & $PythonExe -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Fallo actualizando pip del Alerts Bot.' }
    & $PythonExe -m pip install --disable-pip-version-check -r $RequirementsFile
    if ($LASTEXITCODE -ne 0) { throw 'Fallo instalando requirements del Alerts Bot.' }
    Set-Content -Path $RequirementsMarker -Value $RequirementsHash -Encoding ASCII
} else {
    Write-Host '[SETUP] Dependencias del Alerts Bot ya preparadas; instalacion omitida.'
}
if (!(Test-Path (Join-Path $ConfigDir 'alerts_bot.env'))) { throw 'Falta 02_CONFIG\alerts_bot.env' }
if (!(Test-Path (Join-Path $ConfigDir 'alerts_bot.secret.env'))) { Copy-Item (Join-Path $ConfigDir 'alerts_bot.secret.env.example') (Join-Path $ConfigDir 'alerts_bot.secret.env') }
& $PythonExe -c "import config; s=config.EFFECTIVE_CONFIG_SUMMARY; print('[CONFIG ALERTS] Research/Fusion>={0} source={1} | ENTER_LIVE score>={2} projection>={3}% confirms>={4} | research_mode={5} shadow={6} | entry_window={7}-{8} | scanner_max_age={9}m gex_max_lag={10}m learning_guard={11}m'.format(s['FUSION_MIN_ACTIONABLE_SCORE'],s['FUSION_MIN_ACTIONABLE_SCORE_SOURCE'],config.PRECISION_FIRST_MIN_LIVE_SCORE,config.PRECISION_FIRST_MIN_LIVE_PROJECTION,config.PRECISION_FIRST_MIN_CONFIRMING_BLOCKS,config.RESEARCH_MODE,config.PRECALIBRATION_SHADOW_ENABLED,config.ENTRY_WINDOW_START,config.ENTRY_WINDOW_END,config.MAX_SCANNER_AGE_MINUTES,config.MAX_GEX_LAG_MINUTES,config.CORE_LEARNING_ENTRY_GUARD_MINUTES)); print('[CONFIG AUDITOR] enabled={0} discord={1} target=+-{2}% horizon={3}s min_events={4} min_signals={5} rolling_dates={6} top_k={7}'.format(config.MARKET_OUTCOME_AUDITOR_ENABLED,config.MARKET_OUTCOME_AUDITOR_DISCORD_ENABLED,config.MARKET_OUTCOME_AUDITOR_TARGET_PCT,config.MARKET_OUTCOME_AUDITOR_HORIZON_SESSIONS,config.MARKET_OUTCOME_AUDITOR_MIN_EVENTS,config.MARKET_OUTCOME_AUDITOR_MIN_SIGNAL_SAMPLE,config.MARKET_OUTCOME_AUDITOR_ROLLING_DATES,','.join(map(str,config.MARKET_OUTCOME_AUDITOR_TOP_K)))); print('[CONFIG LEARNING] enabled={0} auto_retrain={1} promote_shadow={2} promote_live={3} | min_moves={4} min_signals={5} OOS={6}/{7}d | research_gate_range={8}-{9} rollback={10}d'.format(config.ADAPTIVE_LEARNING_ENABLED,config.ADAPTIVE_AUTO_RETRAIN_ENABLED,config.ADAPTIVE_AUTO_PROMOTE_SHADOW,config.ADAPTIVE_AUTO_PROMOTE_LIVE,config.ADAPTIVE_MIN_MATURE_MOVEMENTS,config.ADAPTIVE_MIN_MATURE_SIGNALS,config.ADAPTIVE_MIN_OOS_SAMPLE,config.ADAPTIVE_MIN_OOS_DATES,config.ADAPTIVE_RESEARCH_GATE_MIN,config.ADAPTIVE_RESEARCH_GATE_MAX,config.ADAPTIVE_ROLLBACK_MIN_NEW_DATES))"
if ($LASTEXITCODE -ne 0) { throw 'No se pudo leer la configuracion efectiva de Alerts.' }
if ($Mode -eq 'setup-only') { Write-Host '[OK] Alerts Bot preparado.'; exit 0 }

if ($Mode -eq 'daemon') {
    try {
        $StorageCheckpointScript = Join-Path $ProgramDir 'storage_startup_checkpoint.py'
        if (Test-Path $StorageCheckpointScript) {
            & $PythonExe -u $StorageCheckpointScript
            if ($LASTEXITCODE -ne 0) {
                Write-Host "[WARN] Startup WAL checkpoint devolvio codigo $LASTEXITCODE; Alerts continua." -ForegroundColor Yellow
            }
        }
    } catch {
        Write-Host ('[WARN] Startup WAL checkpoint no pudo ejecutarse; Alerts continua: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

$AlphaLabProcess = $null
if ($Mode -eq 'daemon') {
    Remove-Item -Force -ErrorAction SilentlyContinue $AlertsDbReadyFile
    try {
        $AlphaLabScript = Join-Path $ProgramDir 'alpha_lab_daemon.py'
        if (Test-Path $AlphaLabScript) {
            $AlphaLabLogDir = Join-Path $DataRoot 'alerts_bot\logs'
            New-Item -ItemType Directory -Path $AlphaLabLogDir -Force | Out-Null
            $AlphaLabStdout = Join-Path $AlphaLabLogDir ("alpha_lab_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
            $AlphaLabStderr = Join-Path $AlphaLabLogDir ("alpha_lab_{0}.err.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
            # The observer process starts immediately but must not touch SQLite
            # until main.py has successfully opened/initialized the production DB.
            $AlphaLabProcess = Start-Process -FilePath $PythonExe -ArgumentList @(
                '-u','alpha_lab_daemon.py','--parent-pid',"$PID",'--interval','60',
                '--ready-file',$AlertsDbReadyFile,'--ready-timeout','180'
            ) -WorkingDirectory $ProgramDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $AlphaLabStdout -RedirectStandardError $AlphaLabStderr
            Write-Host "[ALPHA LAB] SHADOW iniciado en espera DB-ready PID=$($AlphaLabProcess.Id) log=$AlphaLabStdout"
        }
    } catch {
        $AlphaLabProcess = $null
        Write-Host ('[WARN] Alpha Lab no pudo arrancar; Alerts continua sin bloquearse: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

$FusionLabProcess = $null
if ($Mode -eq 'daemon') {
    try {
        $FusionLabScript = Join-Path $ProgramDir 'fusion_gate_startup_lab.py'
        if (Test-Path $FusionLabScript) {
            $FusionLabLogDir = Join-Path $DataRoot 'alerts_bot\logs'
            New-Item -ItemType Directory -Path $FusionLabLogDir -Force | Out-Null
            $FusionLabStdout = Join-Path $FusionLabLogDir ("fusion_gate_lab_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
            $FusionLabStderr = Join-Path $FusionLabLogDir ("fusion_gate_lab_{0}.err.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
            $FusionLabProcess = Start-Process -FilePath $PythonExe -ArgumentList @(
                '-u','fusion_gate_startup_lab.py',
                '--ready-file',$AlertsDbReadyFile,
                '--ready-timeout','180',
                '--replay-timeout','28800',
                '--grace','75'
            ) -WorkingDirectory $ProgramDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $FusionLabStdout -RedirectStandardError $FusionLabStderr
            Write-Host "[FUSION LAB] Research one-shot iniciado PID=$($FusionLabProcess.Id) log=$FusionLabStdout"
        }
    } catch {
        $FusionLabProcess = $null
        Write-Host ('[WARN] FUSION Lab no pudo arrancar; Alerts continua sin bloquearse: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

$RunExitCode = 0
try {
    switch ($Mode) {
        'alerts-once' { & $PythonExe -u main.py --once alerts }
        'labels-once' { & $PythonExe -u main.py --once labels }
        'train-once'  { & $PythonExe -u main.py --once train }
        'baseline'    { & $PythonExe -u main.py --once baseline }
        'daily'       { & $PythonExe -u main.py --once daily }
        'status'      { & $PythonExe -u main.py --once status }
        default       { & $PythonExe -u main.py }
    }
    $RunExitCode = $LASTEXITCODE
} finally {
    if ($AlphaLabProcess -and -not $AlphaLabProcess.HasExited) {
        try { Stop-Process -Id $AlphaLabProcess.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    if ($FusionLabProcess -and -not $FusionLabProcess.HasExited) {
        try { Stop-Process -Id $FusionLabProcess.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    if ($ConsoleTranscriptStarted) { try { Stop-Transcript | Out-Null } catch {} }
}
exit $RunExitCode
