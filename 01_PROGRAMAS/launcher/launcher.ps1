$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try {
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $Utf8NoBom
    [Console]::InputEncoding = $Utf8NoBom
    $OutputEncoding = $Utf8NoBom
} catch {}
try {
    $ProgramLauncherDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $Root = (Resolve-Path (Join-Path $ProgramLauncherDir '..\..')).Path
    $ConfigDir = Join-Path $Root '02_CONFIG'
    $DataRoot = Join-Path $Root '03_DATOS'
    $RuntimeRoot = Join-Path $Root '04_RUNTIME'
    $DataDir = Join-Path $Root '01_PROGRAMAS\data_bot'
    $AlertsDir = Join-Path $Root '01_PROGRAMAS\alerts_bot'
    $HistoryDir = Join-Path $Root '01_PROGRAMAS\history_query_worker'
    $LauncherRuntime = Join-Path $RuntimeRoot 'launcher'
    $ChannelsFile = Join-Path $ConfigDir 'configuracion_canales.env'
    $FirstRunMarker = Join-Path $LauncherRuntime 'first_run_complete.json'
    $VersionFile = Join-Path $Root 'VERSION.txt'
    $DisplayVersion = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { 'desconocida' }
    $PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (!(Test-Path $PowerShellExe)) { throw 'No se encontro Windows PowerShell.' }
    New-Item -ItemType Directory -Path $LauncherRuntime -Force | Out-Null

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
    function Get-FirstValue($map, [string[]]$names, [string]$fallback = '') {
        foreach ($name in $names) {
            if ($map.ContainsKey($name)) {
                $value = [string]$map[$name]
                if ($value -and $value.Trim()) { return $value.Trim() }
            }
        }
        return $fallback
    }
    function Write-Secrets($channels) {
        $dataSecretPath = Join-Path $ConfigDir 'data_bot.secret.env'
        $alertsSecretPath = Join-Path $ConfigDir 'alerts_bot.secret.env'
        $oldData = Read-EnvFile $dataSecretPath
        $oldAlerts = Read-EnvFile $alertsSecretPath

        $dataLog = Get-FirstValue $channels @('DATA_LOG_WEBHOOK','DATA_BOT_LOG_WEBHOOK','DISCORD_WEBHOOK_URL_DATA_LOG','DISCORD_DATA_LOG_WEBHOOK','WEBHOOK_DATA_LOG') (Get-FirstValue $oldData @('DISCORD_WEBHOOK_URL_DATA_LOG'))
        $dataError = Get-FirstValue $channels @('DATA_ERROR_WEBHOOK','DATA_BOT_ERROR_WEBHOOK','DISCORD_WEBHOOK_URL_DATA_ERROR','DISCORD_DATA_ERROR_WEBHOOK','WEBHOOK_DATA_ERROR') (Get-FirstValue $oldData @('DISCORD_WEBHOOK_URL_DATA_ERROR'))
        $dataQuality = Get-FirstValue $channels @('DATA_QUALITY_WEBHOOK','DATA_SCHEMA_WEBHOOK','DISCORD_WEBHOOK_URL_DATA_QUALITY','DISCORD_WEBHOOK_URL_DATA_SCHEMA','WEBHOOK_DATA_QUALITY','WEBHOOK_DATA_SCHEMA') (Get-FirstValue $oldData @('DISCORD_WEBHOOK_URL_DATA_QUALITY','DISCORD_WEBHOOK_URL_DATA_SCHEMA'))

        $alertsLive = Get-FirstValue $channels @('ALERTS_LIVE_WEBHOOK','ALERTS_WEBHOOK','DISCORD_WEBHOOK_URL_ALERTS','WEBHOOK_ALERTS_LIVE') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_ALERTS'))
        $alertsLog = Get-FirstValue $channels @('ALERTS_LOG_WEBHOOK','DISCORD_WEBHOOK_URL_ALERTS_LOG','WEBHOOK_ALERTS_LOG') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_ALERTS_LOG'))
        $alertsError = Get-FirstValue $channels @('ALERTS_ERROR_WEBHOOK','DISCORD_WEBHOOK_URL_ALERTS_ERROR','WEBHOOK_ALERTS_ERROR') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_ALERTS_ERROR'))
        $alertsResearch = Get-FirstValue $channels @('ALERTS_RESEARCH_WEBHOOK','DISCORD_WEBHOOK_URL_RESEARCH','WEBHOOK_ALERTS_RESEARCH') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_RESEARCH'))
        $alertsShadow = Get-FirstValue $channels @('ALERTS_SHADOW_WEBHOOK','DISCORD_WEBHOOK_URL_ALERTS_SHADOW','WEBHOOK_ALERTS_SHADOW') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_ALERTS_SHADOW'))
        $alertsPositions = Get-FirstValue $channels @('ALERTS_POSITIONS_WEBHOOK','DISCORD_WEBHOOK_URL_ALERTS_POSITIONS','WEBHOOK_ALERTS_POSITIONS') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_ALERTS_POSITIONS'))
        $calibration = Get-FirstValue $channels @('MODEL_CALIBRATION_WEBHOOK','DAILY_CALIBRATION_WEBHOOK','ALERTS_CALIBRATION_WEBHOOK','DISCORD_WEBHOOK_URL_MODEL_CALIBRATION','DISCORD_WEBHOOK_URL_DAILY','WEBHOOK_CALIBRATION') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_MODEL_CALIBRATION','DISCORD_WEBHOOK_URL_DAILY'))
        $learning = Get-FirstValue $channels @('MODEL_LEARNING_WEBHOOK','LEARNING_AUDIT_WEBHOOK','ALERTS_LEARNING_AUDIT_WEBHOOK','DISCORD_WEBHOOK_URL_MODEL_LEARNING','DISCORD_WEBHOOK_URL_LEARNING_AUDIT','WEBHOOK_LEARNING_AUDIT') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_MODEL_LEARNING','DISCORD_WEBHOOK_URL_LEARNING_AUDIT'))
        $systemStatus = Get-FirstValue $channels @('SYSTEM_STATUS_WEBHOOK','ALERTS_STATUS_WEBHOOK','DISCORD_WEBHOOK_URL_SYSTEM_STATUS','WEBHOOK_SYSTEM_STATUS') (Get-FirstValue $oldAlerts @('DISCORD_WEBHOOK_URL_SYSTEM_STATUS','DISCORD_WEBHOOK_URL_ALERTS_STATUS'))

        $dataSecret = @"
DISCORD_WEBHOOK_URL_DATA_LOG=$dataLog
DISCORD_WEBHOOK_URL_DATA_ERROR=$dataError
DISCORD_WEBHOOK_URL_DATA_QUALITY=$dataQuality
DISCORD_WEBHOOK_URL_DATA_SCHEMA=$dataQuality
"@
        Set-Content -Path $dataSecretPath -Value $dataSecret -Encoding UTF8

        $alertsSecret = @"
DISCORD_WEBHOOK_URL_ALERTS=$alertsLive
DISCORD_WEBHOOK_URL_ALERTS_LOG=$alertsLog
DISCORD_WEBHOOK_URL_ALERTS_ERROR=$alertsError
DISCORD_WEBHOOK_URL_RESEARCH=$alertsResearch
DISCORD_WEBHOOK_URL_ALERTS_SHADOW=$alertsShadow
DISCORD_WEBHOOK_URL_ALERTS_POSITIONS=$alertsPositions
DISCORD_WEBHOOK_URL_MODEL_CALIBRATION=$calibration
DISCORD_WEBHOOK_URL_DAILY=$calibration
DISCORD_WEBHOOK_URL_MODEL_LEARNING=$learning
DISCORD_WEBHOOK_URL_LEARNING_AUDIT=$learning
DISCORD_WEBHOOK_URL_SYSTEM_STATUS=$systemStatus
DISCORD_WEBHOOK_URL_ALERTS_STATUS=$systemStatus
"@
        Set-Content -Path $alertsSecretPath -Value $alertsSecret -Encoding UTF8

        return @{
            DATA_LOG = [bool]$dataLog
            DATA_QUALITY = [bool]$dataQuality
            DATA_ERROR = [bool]$dataError
            ALERTS_RESEARCH = [bool]$alertsResearch
            ALERTS_SHADOW = [bool]$alertsShadow
            ALERTS_LIVE = [bool]$alertsLive
            ALERTS_POSITIONS = [bool]$alertsPositions
            ALERTS_LOG = [bool]$alertsLog
            ALERTS_ERROR = [bool]$alertsError
            MODEL_CALIBRATION = [bool]$calibration
            MODEL_LEARNING = [bool]$learning
            SYSTEM_STATUS = [bool]$systemStatus
        }
    }

    function Ensure-HistoryQueryConfig {
        $result = @{ Enabled=$false; Python='' }
        if (!(Test-Path $HistoryDir -PathType Container)) { return $result }

        $dataPython = Join-Path $RuntimeRoot 'venvs\data_bot\Scripts\python.exe'
        if (!(Test-Path $dataPython -PathType Leaf)) { return $result }

        $historyEnv = Join-Path $ConfigDir 'tmad_history_query.env'
        $historyExample = Join-Path $HistoryDir 'history_query_example.txt'
        if (!(Test-Path $historyEnv -PathType Leaf) -and (Test-Path $historyExample -PathType Leaf)) {
            Copy-Item $historyExample $historyEnv -Force
        }

        $historySecret = Join-Path $ConfigDir 'tmad_history_query.secret.env'
        $secretValues = Read-EnvFile $historySecret
        $token = Get-FirstValue $secretValues @('TMAD_HISTORY_QUERY_TOKEN')
        if (-not $token) {
            $bytes = New-Object byte[] 32
            $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
            try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
            $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
            $secretLines = @(
                '# Generated locally by TMAD launcher; do not commit'
                ('TMAD_HISTORY_QUERY_TOKEN=' + $token)
            )
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllLines($historySecret, $secretLines, $utf8NoBom)
            Write-Host '[CONFIG] History Query token creado localmente (valor no mostrado).' -ForegroundColor Green
        }

        Push-Location $HistoryDir
        try {
            & $dataPython -c "import server; server.validate_startup(); s=server.HistoryStore(); x=s.catalog_summary(); print('[HISTORY] catalog_objects={0}'.format(x['objects']))"
            if ($LASTEXITCODE -ne 0) {
                Write-Host '[WARN] History Query Worker no paso setup; Data/Alerts continuaran.' -ForegroundColor Yellow
                return $result
            }
        } finally {
            Pop-Location
        }

        $result.Enabled = $true
        $result.Python = $dataPython
        return $result
    }

    function Start-HistoryQuickTunnel {
        param([bool]$WorkerHealthy)

        if (-not $WorkerHealthy) { return }

        $historyEnvPath = Join-Path $ConfigDir 'tmad_history_query.env'
        $historyValues = Read-EnvFile $historyEnvPath
        $tunnelEnabled = (Get-FirstValue $historyValues @('TMAD_HISTORY_TUNNEL_ENABLED') '1').Trim().ToLowerInvariant()
        if ($tunnelEnabled -in @('0','false','no','off')) {
            Write-Host '[HISTORY] Quick Tunnel desactivado por configuracion.' -ForegroundColor Yellow
            return
        }

        $cloudflared = Get-FirstValue $historyValues @('TMAD_HISTORY_CLOUDFLARED_EXE') 'C:\Tools\cloudflared\cloudflared.exe'
        $registerUrl = Get-FirstValue $historyValues @('TMAD_HISTORY_REGISTER_URL') 'https://dzkbljrwvcbknozolrgw.supabase.co/functions/v1/tmad-history-worker-register'
        if (!(Test-Path $cloudflared -PathType Leaf)) {
            Write-Host ("[WARN] cloudflared no encontrado en {0}; History Worker queda solo local." -f $cloudflared) -ForegroundColor Yellow
            return
        }

        $secretValues = Read-EnvFile (Join-Path $ConfigDir 'tmad_history_query.secret.env')
        $historyToken = Get-FirstValue $secretValues @('TMAD_HISTORY_QUERY_TOKEN')
        if (-not $historyToken) {
            Write-Host '[WARN] No se encontro token History Query; no se inicia tunnel.' -ForegroundColor Yellow
            return
        }

        $historyLogDir = Join-Path $RuntimeRoot 'history_query'
        New-Item -ItemType Directory -Path $historyLogDir -Force | Out-Null
        $tunnelPidFile = Join-Path $RuntimeRoot 'history_query_tunnel.pid'

        if (Test-Path $tunnelPidFile -PathType Leaf) {
            $oldTunnelPid = (Get-Content $tunnelPidFile -Raw -ErrorAction SilentlyContinue).Trim()
            if ($oldTunnelPid -match '^\d+$') {
                $oldTunnelProc = Get-Process -Id ([int]$oldTunnelPid) -ErrorAction SilentlyContinue
                if ($oldTunnelProc) {
                    Stop-Process -Id ([int]$oldTunnelPid) -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Milliseconds 500
                }
            }
            Remove-Item $tunnelPidFile -Force -ErrorAction SilentlyContinue
        }

        try {
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Name -eq 'cloudflared.exe' -and
                    $_.CommandLine -and
                    ([string]$_.CommandLine).IndexOf('127.0.0.1:8787',[System.StringComparison]::OrdinalIgnoreCase) -ge 0
                } |
                ForEach-Object {
                    Stop-Process -Id ([int]$_.ProcessId) -Force -ErrorAction SilentlyContinue
                }
        } catch {}
        Start-Sleep -Milliseconds 500

        $tunnelOut = Join-Path $historyLogDir 'cloudflared_quick_stdout.log'
        $tunnelErr = Join-Path $historyLogDir 'cloudflared_quick_stderr.log'
        Remove-Item $tunnelOut,$tunnelErr -Force -ErrorAction SilentlyContinue

        $proc = Start-Process -FilePath $cloudflared -ArgumentList @(
            'tunnel',
            '--url','http://127.0.0.1:8787',
            '--no-autoupdate'
        ) -WindowStyle Hidden -PassThru -RedirectStandardOutput $tunnelOut -RedirectStandardError $tunnelErr

        Set-Content -Path $tunnelPidFile -Value ([string]$proc.Id) -Encoding ASCII

        $publicUrl = ''
        $deadline = (Get-Date).AddSeconds(35)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 1
            if ($proc.HasExited) { break }
            $combined = ''
            if (Test-Path $tunnelOut -PathType Leaf) { $combined += (Get-Content $tunnelOut -Raw -ErrorAction SilentlyContinue) }
            if (Test-Path $tunnelErr -PathType Leaf) { $combined += [Environment]::NewLine + (Get-Content $tunnelErr -Raw -ErrorAction SilentlyContinue) }
            $match = [regex]::Match($combined, 'https://[a-z0-9-]+\.trycloudflare\.com', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
            if ($match.Success) {
                $publicUrl = $match.Value.TrimEnd('/')
                break
            }
        }

        if (-not $publicUrl) {
            Write-Host '[WARN] cloudflared no devolvio URL publica; Worker sigue local.' -ForegroundColor Yellow
            return
        }

        try {
            $headers = @{ 'x-tmad-history-token' = $historyToken }
            $body = @{ url = $publicUrl } | ConvertTo-Json -Compress
            $registration = Invoke-RestMethod -Uri $registerUrl -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 30
            if ($registration.ok -ne $true) { throw 'registro Supabase devolvio ok=false' }
            @{
                public_url = $publicUrl
                tunnel_pid = $proc.Id
                registered_at = (Get-Date).ToString('o')
                catalog_objects = $registration.catalog_objects
            } | ConvertTo-Json | Set-Content (Join-Path $historyLogDir 'tunnel_state.json') -Encoding UTF8
            Write-Host ("[OK] History Query Tunnel registrado. catalog_objects={0}" -f $registration.catalog_objects) -ForegroundColor Green
        } catch {
            Write-Host ('[WARN] Tunnel activo pero no se pudo registrar en Supabase: ' + $_.Exception.Message) -ForegroundColor Yellow
        }
    }

    Write-Host '============================================================'
    Write-Host (" TMAD DUAL BOT v{0} - ESTRUCTURA PERSISTENTE" -f $DisplayVersion)
    Write-Host '============================================================'
    Write-Host '[SAFE] Solo 01_PROGRAMAS se sustituye en actualizaciones.' -ForegroundColor Green
    Write-Host '[SAFE] 02_CONFIG, 03_DATOS y 04_RUNTIME se preservan.' -ForegroundColor Green

    if (!(Test-Path $ChannelsFile)) { throw "No existe $ChannelsFile" }
    $channels = Read-EnvFile $ChannelsFile
    $configured = @($channels.Values | Where-Object { $_ -and $_.StartsWith('http') }).Count
    $PromptMarker = Join-Path $LauncherRuntime 'channels_prompted.flag'
    if ($configured -eq 0 -and !(Test-Path $PromptMarker)) {
        Write-Host '[CONFIG] No hay webhooks configurados. Se abrira el fichero.' -ForegroundColor Yellow
        Start-Process notepad.exe -ArgumentList ('"' + $ChannelsFile + '"') -Wait
        Set-Content -Path $PromptMarker -Value (Get-Date).ToString('o') -Encoding UTF8
        $channels = Read-EnvFile $ChannelsFile
    }

    $webhookStatus = Write-Secrets $channels
    $configured = @($webhookStatus.Values | Where-Object { $_ }).Count
    Write-Host "[CONFIG] Webhooks configurados: $configured/12"
    foreach ($name in @(
        'DATA_LOG','DATA_QUALITY','DATA_ERROR',
        'ALERTS_RESEARCH','ALERTS_SHADOW','ALERTS_LIVE','ALERTS_POSITIONS','ALERTS_LOG','ALERTS_ERROR',
        'MODEL_CALIBRATION','MODEL_LEARNING','SYSTEM_STATUS'
    )) {
        $state = if ($webhookStatus[$name]) { 'OK' } else { 'MISSING' }
        $color = if ($webhookStatus[$name]) { 'Green' } else { 'Yellow' }
        Write-Host ("[CONFIG] {0}: {1}" -f $name,$state) -ForegroundColor $color
    }

    if (!(Test-Path (Join-Path $ConfigDir 'tmad_session.json'))) { throw 'Falta 02_CONFIG\tmad_session.json.' }

    Write-Host '[SETUP] Preparando Data Bot...'
    & $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $DataDir 'start-bot.ps1') -Mode setup-only
    if ($LASTEXITCODE -ne 0) { throw 'Fallo el setup del Data Bot.' }
    Write-Host '[SETUP] Preparando Alerts Bot...'
    & $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AlertsDir 'start-bot.ps1') -Mode setup-only
    if ($LASTEXITCODE -ne 0) { throw 'Fallo el setup del Alerts Bot.' }

    Write-Host '[SETUP] Preparando History Query Worker...'
    $historySetup = Ensure-HistoryQueryConfig

    if (!(Test-Path $FirstRunMarker)) {
        $scannerDb = Join-Path $DataRoot 'data_bot\db\live\tmad_scanner_live.db'
        if (Test-Path $scannerDb) {
            Write-Host '[FIRST RUN] Datos migrados detectados; se conserva el run anterior.' -ForegroundColor Cyan
        } else {
            Write-Host '[FIRST RUN] Captura real inicial selectiva...' -ForegroundColor Cyan
            Push-Location $DataDir
            & (Join-Path $RuntimeRoot 'venvs\data_bot\Scripts\python.exe') -u main.py --once scanner --session first_run --profile selective
            $dataExit = $LASTEXITCODE
            Pop-Location
            if ($dataExit -ne 0) { Write-Host '[FIRST RUN] Captura inicial pendiente; el daemon reintentara.' -ForegroundColor Yellow }
        }
        Write-Host '[FIRST RUN] Generando baseline con el historico disponible...' -ForegroundColor Cyan
        Push-Location $AlertsDir
        & (Join-Path $RuntimeRoot 'venvs\alerts_bot\Scripts\python.exe') -u main.py --once baseline
        $baselineExit = $LASTEXITCODE
        Pop-Location
        if ($baselineExit -eq 0) {
            @{ completed=$true; completed_at=(Get-Date).ToString('o'); preserved_existing_data=(Test-Path $scannerDb) } | ConvertTo-Json | Set-Content $FirstRunMarker -Encoding UTF8
        }
    }

    Write-Host '[START] Abriendo Data Bot y Alerts Bot...' -ForegroundColor Green
    Start-Process -FilePath $PowerShellExe -WorkingDirectory $DataDir -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $DataDir 'start-bot.ps1'),'-Mode','daemon'
    Start-Sleep -Seconds 3
    Start-Process -FilePath $PowerShellExe -WorkingDirectory $AlertsDir -ArgumentList '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $AlertsDir 'start-bot.ps1'),'-Mode','daemon'
    Write-Host '[OK] Ambos bots arrancados.' -ForegroundColor Green

    if ($historySetup.Enabled) {
        $historyPidFile = Join-Path $RuntimeRoot 'history_query_worker.pid'
        if (Test-Path $historyPidFile -PathType Leaf) {
            $oldPidRaw = (Get-Content $historyPidFile -Raw -ErrorAction SilentlyContinue).Trim()
            if ($oldPidRaw -match '^\d+$') {
                $oldProc = Get-Process -Id ([int]$oldPidRaw) -ErrorAction SilentlyContinue
                if ($oldProc) {
                    Write-Host ("[HISTORY] Cerrando worker anterior PID={0}..." -f $oldPidRaw) -ForegroundColor Yellow
                    Stop-Process -Id ([int]$oldPidRaw) -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Milliseconds 500
                }
            }
            Remove-Item $historyPidFile -Force -ErrorAction SilentlyContinue
        }

        $historyLogDir = Join-Path $RuntimeRoot 'history_query'
        New-Item -ItemType Directory -Path $historyLogDir -Force | Out-Null
        $historyOut = Join-Path $historyLogDir 'worker_stdout.log'
        $historyErr = Join-Path $historyLogDir 'worker_stderr.log'
        Start-Process -FilePath $historySetup.Python -WorkingDirectory $HistoryDir -ArgumentList '-u','server.py' -WindowStyle Hidden -RedirectStandardOutput $historyOut -RedirectStandardError $historyErr | Out-Null

        $historyHealthy = $false
        $historyDeadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $historyDeadline) {
            Start-Sleep -Seconds 1
            try {
                $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/health' -Method Get -TimeoutSec 3
                if ($health.ok -eq $true) {
                    $historyHealthy = $true
                    Write-Host ("[OK] History Query Worker saludable. catalog_objects={0}" -f $health.catalog_objects) -ForegroundColor Green
                    break
                }
            } catch {}
        }
        if (-not $historyHealthy) {
            Write-Host '[WARN] History Query Worker no paso health-check; Data/Alerts siguen activos. Revisa 04_RUNTIME\history_query.' -ForegroundColor Yellow
        } else {
            Start-HistoryQuickTunnel -WorkerHealthy $true
        }
    }

    exit 0
} catch {
    try {
        $fallbackRoot = (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) '..\..')).Path
        $logDir = Join-Path $fallbackRoot '04_RUNTIME\launcher'
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $_ | Out-String | Set-Content (Join-Path $logDir 'launcher_error.log') -Encoding UTF8
    } catch {}
    Write-Host ('[ERROR] ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
