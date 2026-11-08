[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [switch]$NoStart
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BackupBase = Join-Path ([Environment]::GetFolderPath('Desktop')) 'TMAD_DEPLOY_BACKUPS'
$DeploymentLogDir = Join-Path $ProductionRoot '04_RUNTIME\deployments'
$BackupDir = $null
$DeploymentLog = $null
$BotsStopped = $false
$rootFiles = @()
$ProductionPrefix = $ProductionRoot.TrimEnd('\') + '\'

function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO','OK','WARN','ERROR')][string]$Level = 'INFO'
    )
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $color = switch ($Level) {
        'OK' { 'Green' }
        'WARN' { 'Yellow' }
        'ERROR' { 'Red' }
        default { 'Cyan' }
    }
    Write-Host $line -ForegroundColor $color
    if ($DeploymentLog) {
        try {
            New-Item -ItemType Directory -Path $DeploymentLogDir -Force | Out-Null
            Add-Content -Path $DeploymentLog -Value $line -Encoding UTF8
        } catch {}
    }
}

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = & git -C $RepoRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Git fallo: git -C `"$RepoRoot`" $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return @($output)
}

function Get-EnvValue {
    param([string]$Path,[string[]]$Names)
    if (-not (Test-Path $Path -PathType Leaf)) { return '' }
    $values = @{}
    foreach ($line in Get-Content $Path -Encoding UTF8) {
        $trim = $line.Trim()
        if (-not $trim -or $trim.StartsWith('#') -or -not $trim.Contains('=')) { continue }
        $parts = $trim.Split('=',2)
        $values[$parts[0].Trim()] = $parts[1].Trim()
    }
    foreach ($name in $Names) {
        if ($values.ContainsKey($name) -and [string]$values[$name]) {
            return ([string]$values[$name]).Trim()
        }
    }
    return ''
}

function Show-ChannelConfig {
    $envFile = Join-Path $ProductionRoot '02_CONFIG\configuracion_canales.env'
    $channels = @(
        @{Name='DATA_LOG'; Names=@('DATA_LOG_WEBHOOK')},
        @{Name='DATA_QUALITY'; Names=@('DATA_QUALITY_WEBHOOK','DATA_SCHEMA_WEBHOOK')},
        @{Name='DATA_ERROR'; Names=@('DATA_ERROR_WEBHOOK')},
        @{Name='ALERTS_RESEARCH'; Names=@('ALERTS_RESEARCH_WEBHOOK')},
        @{Name='ALERTS_SHADOW'; Names=@('ALERTS_SHADOW_WEBHOOK')},
        @{Name='ALERTS_LIVE'; Names=@('ALERTS_LIVE_WEBHOOK')},
        @{Name='ALERTS_POSITIONS'; Names=@('ALERTS_POSITIONS_WEBHOOK')},
        @{Name='ALERTS_LOG'; Names=@('ALERTS_LOG_WEBHOOK')},
        @{Name='ALERTS_ERROR'; Names=@('ALERTS_ERROR_WEBHOOK')},
        @{Name='MODEL_CALIBRATION'; Names=@('MODEL_CALIBRATION_WEBHOOK','DAILY_CALIBRATION_WEBHOOK')},
        @{Name='MODEL_LEARNING'; Names=@('MODEL_LEARNING_WEBHOOK','LEARNING_AUDIT_WEBHOOK')},
        @{Name='SYSTEM_STATUS'; Names=@('SYSTEM_STATUS_WEBHOOK','ALERTS_STATUS_WEBHOOK')}
    )
    $ok = 0
    foreach ($channel in $channels) {
        $value = Get-EnvValue -Path $envFile -Names $channel.Names
        $configured = [bool]($value -and $value.StartsWith('http'))
        if ($configured) { $ok++ }
        $state = if ($configured) { 'OK' } else { 'MISSING' }
        $level = if ($configured) { 'OK' } else { 'WARN' }
        Write-Log ("Canal {0}: {1}" -f $channel.Name,$state) $level
    }
    $summaryLevel = if ($ok -eq 12) { 'OK' } else { 'WARN' }
    Write-Log ("Webhooks semanticos configurados: {0}/12" -f $ok) $summaryLevel
}

function Send-SystemStatus {
    param([string]$Text)
    try {
        $envFile = Join-Path $ProductionRoot '02_CONFIG\configuracion_canales.env'
        $url = Get-EnvValue -Path $envFile -Names @('SYSTEM_STATUS_WEBHOOK','ALERTS_STATUS_WEBHOOK')
        if ($url) {
            Invoke-RestMethod -Uri $url -Method Post -ContentType 'application/json' -Body (@{content=$Text} | ConvertTo-Json -Compress) -TimeoutSec 10 | Out-Null
        }
    } catch {
        Write-Log ('No se pudo publicar System Status: ' + $_.Exception.Message) 'WARN'
    }
}

function Invoke-Validator {
    param([string]$Root,[string]$ValidatorName)
    $validator = Join-Path $Root $ValidatorName
    if (-not (Test-Path $validator -PathType Leaf)) { throw "Falta validador: $validator" }
    Write-Log "Ejecutando $ValidatorName en $Root"
    Push-Location $Root
    try {
        & cmd.exe /d /c "`"$validator`""
        if ($LASTEXITCODE -ne 0) { throw "Validacion fallo con codigo $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
    Write-Log 'Validacion correcta.' 'OK'
}

function Get-CimProcessSafe {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Get-TmadConsoleTarget {
    param([int]$DaemonPid)
    $current = Get-CimProcessSafe -ProcessId $DaemonPid
    if (-not $current) { return $null }
    for ($depth = 0; $depth -lt 6; $depth++) {
        $parentId = [int]$current.ParentProcessId
        if ($parentId -le 0) { break }
        $parent = Get-CimProcessSafe -ProcessId $parentId
        if (-not $parent) { break }
        $cmd = [string]$parent.CommandLine
        if ($parent.Name -match 'powershell' -and $cmd -like '*start-bot.ps1*' -and $cmd -like '*-Mode*daemon*') {
            return $parent
        }
        $current = $parent
    }
    return Get-CimProcessSafe -ProcessId $DaemonPid
}

function Get-LingeringTmadDaemonShells {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match 'powershell' -and
        $_.CommandLine -and
        ([string]$_.CommandLine) -like '*start-bot.ps1*' -and
        ([string]$_.CommandLine) -like '*-Mode*daemon*'
    })
}

function Get-TmadProcessTreeIds {
    param([int]$RootPid)
    if ($RootPid -le 0) { return @() }
    $snapshot = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $ids = @($RootPid)
    do {
        $added = $false
        foreach ($proc in $snapshot) {
            $pidValue = [int]$proc.ProcessId
            $parentValue = [int]$proc.ParentProcessId
            if (($ids -contains $parentValue) -and -not ($ids -contains $pidValue)) {
                $ids += $pidValue
                $added = $true
            }
        }
    } while ($added)
    return @($ids | Sort-Object -Unique)
}

function Stop-TmadProcessTreeVerified {
    param(
        [Parameter(Mandatory = $true)][int]$RootPid,
        [string]$Label = 'TMAD process tree'
    )
    if (-not (Get-CimProcessSafe -ProcessId $RootPid)) { return $true }

    $treeIds = @(Get-TmadProcessTreeIds -RootPid $RootPid)
    if ($treeIds.Count -eq 0) { $treeIds = @($RootPid) }

    # Windows taskkill can return a non-zero code when one descendant exits
    # during /T traversal.  With ErrorActionPreference=Stop, native stderr used
    # to abort the whole deploy before we could verify the actual process state.
    # Run it behind cmd redirection, then decide on process liveness instead.
    $taskkillCode = 0
    try {
        & cmd.exe /d /c "taskkill.exe /PID $RootPid /T /F >nul 2>&1" | Out-Null
        $taskkillCode = [int]$LASTEXITCODE
    } catch {
        $taskkillCode = -1
        Write-Log ("taskkill exception for {0} PID={1}: {2}" -f $Label,$RootPid,$_.Exception.Message) 'WARN'
    }

    Start-Sleep -Milliseconds 300
    $remaining = @($treeIds | Where-Object { Get-CimProcessSafe -ProcessId ([int]$_) })
    if ($remaining.Count -gt 0) {
        Write-Log ("taskkill dejo {0} PID(s) vivos en {1}; reintento individual: {2}" -f $remaining.Count,$Label,($remaining -join ',')) 'WARN'
        foreach ($pidValue in @($remaining | Sort-Object -Descending)) {
            try {
                Stop-Process -Id ([int]$pidValue) -Force -ErrorAction SilentlyContinue
            } catch {}
        }
        Start-Sleep -Milliseconds 700
        $remaining = @($treeIds | Where-Object { Get-CimProcessSafe -ProcessId ([int]$_) })
    }

    if ($remaining.Count -gt 0) {
        Write-Log ("No se pudo cerrar {0}; PID(s) persistentes: {1}" -f $Label,($remaining -join ',')) 'ERROR'
        return $false
    }

    if ($taskkillCode -ne 0) {
        Write-Log ("taskkill devolvio codigo {0} para {1}, pero la verificacion confirma el arbol cerrado; carrera tolerada." -f $taskkillCode,$Label) 'WARN'
    }
    return $true
}

function Stop-TmadBots {
    Write-Log 'Deteniendo la pareja TMAD activa mediante locks + arbol de procesos...'
    $lockPaths = @(
        (Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_core_data_bot.lock'),
        (Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_alerts_adaptive_bot.lock')
    )
    $targets = @{}
    foreach ($lockPath in $lockPaths) {
        if (-not (Test-Path $lockPath -PathType Leaf)) { continue }
        $raw = (Get-Content $lockPath -Raw -ErrorAction SilentlyContinue).Trim()
        if ($raw -notmatch '^\d+$') { continue }
        $daemonPid = [int]$raw
        $target = Get-TmadConsoleTarget -DaemonPid $daemonPid
        if ($target) { $targets[[string]$target.ProcessId] = $target }
    }

    foreach ($target in @($targets.Values)) {
        Write-Log ("Cerrando arbol TMAD: PID {0} - {1}" -f $target.ProcessId,$target.Name) 'WARN'
        if (-not (Stop-TmadProcessTreeVerified -RootPid ([int]$target.ProcessId) -Label ("TMAD {0}" -f $target.Name))) {
            throw "No se pudo cerrar de forma verificada el arbol TMAD PID=$($target.ProcessId)."
        }
    }

    Start-Sleep -Milliseconds 750
    foreach ($shell in @(Get-LingeringTmadDaemonShells)) {
        Write-Log ("Cerrando consola TMAD huerfana: PID {0}" -f $shell.ProcessId) 'WARN'
        if (-not (Stop-TmadProcessTreeVerified -RootPid ([int]$shell.ProcessId) -Label 'consola TMAD huerfana')) {
            throw "No se pudo cerrar consola TMAD huerfana PID=$($shell.ProcessId)."
        }
    }

    Start-Sleep -Seconds 2
    $remaining = @(Get-LingeringTmadDaemonShells)
    if ($remaining.Count -gt 0) {
        throw "Persisten $($remaining.Count) consolas TMAD daemon despues de taskkill."
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $lockPaths

    $historyPidFile = Join-Path $ProductionRoot '04_RUNTIME\history_query_worker.pid'
    if (Test-Path $historyPidFile -PathType Leaf) {
        $historyPidRaw = (Get-Content $historyPidFile -Raw -ErrorAction SilentlyContinue).Trim()
        if ($historyPidRaw -match '^\d+$') {
            $historyProc = Get-CimProcessSafe -ProcessId ([int]$historyPidRaw)
            if ($historyProc) {
                Write-Log ("Cerrando History Query Worker PID={0}..." -f $historyPidRaw) 'WARN'
                if (-not (Stop-TmadProcessTreeVerified -RootPid ([int]$historyPidRaw) -Label 'History Query Worker')) {
                    throw "No se pudo cerrar History Query Worker PID=$historyPidRaw."
                }
            }
        }
        Remove-Item $historyPidFile -Force -ErrorAction SilentlyContinue
    }

    $historyTunnelPidFile = Join-Path $ProductionRoot '04_RUNTIME\history_query_tunnel.pid'
    if (Test-Path $historyTunnelPidFile -PathType Leaf) {
        $historyTunnelPidRaw = (Get-Content $historyTunnelPidFile -Raw -ErrorAction SilentlyContinue).Trim()
        if ($historyTunnelPidRaw -match '^\d+$') {
            $historyTunnelProc = Get-CimProcessSafe -ProcessId ([int]$historyTunnelPidRaw)
            if ($historyTunnelProc) {
                Write-Log ("Cerrando History Query Tunnel PID={0}..." -f $historyTunnelPidRaw) 'WARN'
                if (-not (Stop-TmadProcessTreeVerified -RootPid ([int]$historyTunnelPidRaw) -Label 'History Query Tunnel')) {
                    throw "No se pudo cerrar History Query Tunnel PID=$historyTunnelPidRaw."
                }
            }
        }
        Remove-Item $historyTunnelPidFile -Force -ErrorAction SilentlyContinue
    }

    foreach ($cloudflaredProc in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq 'cloudflared.exe' -and
        $_.CommandLine -and
        ([string]$_.CommandLine).IndexOf('127.0.0.1:8787',[System.StringComparison]::OrdinalIgnoreCase) -ge 0
    })) {
        Write-Log ("Cerrando History Query Tunnel huerfano PID={0}..." -f $cloudflaredProc.ProcessId) 'WARN'
        if (-not (Stop-TmadProcessTreeVerified -RootPid ([int]$cloudflaredProc.ProcessId) -Label 'History Query Tunnel huerfano')) {
            throw "No se pudo cerrar History Query Tunnel huerfano PID=$($cloudflaredProc.ProcessId)."
        }
    }

    Write-Log ("Pareja anterior cerrada; arboles iniciales={0}; locks limpiados." -f $targets.Count) 'OK'
}

function Invoke-Mirror {
    param([string]$Source,[string]$Destination,[string]$Label)
    if (-not (Test-Path $Source -PathType Container)) { throw "No existe origen: $Source" }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    try { & attrib.exe -R (Join-Path $Destination '*') /S /D 2>$null | Out-Null } catch {}
    Write-Log "$Label mediante robocopy /MIR..."
    $output = & robocopy.exe $Source $Destination /MIR /COPY:DAT /DCOPY:DAT /R:12 /W:2 /XJ /FFT /NP /NFL /NDL 2>&1
    $code = $LASTEXITCODE
    if ($code -gt 7) {
        throw "$Label fallo; Robocopy=$code`n$(@($output | Select-Object -Last 20) -join [Environment]::NewLine)"
    }
    Write-Log "$Label completado - Robocopy=$code" 'OK'
}

function Read-LockPid {
    param([string]$Path)
    try {
        if (Test-Path $Path -PathType Leaf) {
            $raw = (Get-Content $Path -Raw).Trim()
            if ($raw -match '^\d+$') { return [int]$raw }
        }
    } catch {}
    return 0
}

function Get-TmadDaemonState {
    param(
        [Parameter(Mandatory = $true)][string]$LockPath,
        [Parameter(Mandatory = $true)][datetime]$StartedAt,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$ExpectedVenvFragment
    )
    $pidValue = Read-LockPid $LockPath
    $proc = Get-CimProcessSafe -ProcessId $pidValue
    $name = if ($proc) { [string]$proc.Name } else { '' }
    $creation = if ($proc) { $proc.CreationDate } else { $null }
    $cmd = if ($proc) { [string]$proc.CommandLine } else { '' }
    $exe = if ($proc) { [string]$proc.ExecutablePath } else { '' }

    $live = [bool]$proc
    $fresh = [bool]($proc -and $creation -and $creation -ge $StartedAt.AddSeconds(-10))
    $python = [bool]($proc -and $name -match '^python(w)?\.exe$')
    # Path identity is diagnostic only. Win32_Process.CommandLine/ExecutablePath
    # can be blank under Windows permission boundaries even for a valid process.
    # Provenance comes from a newly-created lock + fresh live Python PID.
    $pathIdentity = [bool](
        ($cmd -and $cmd.IndexOf($ExpectedVenvFragment,[System.StringComparison]::OrdinalIgnoreCase) -ge 0) -or
        ($exe -and $exe.IndexOf($ExpectedVenvFragment,[System.StringComparison]::OrdinalIgnoreCase) -ge 0)
    )
    [pscustomobject]@{
        Label = $Label
        Valid = [bool]($live -and $fresh -and $python)
        LockExists = [bool](Test-Path $LockPath -PathType Leaf)
        Pid = [int]$pidValue
        Live = $live
        Fresh = $fresh
        Python = $python
        PathIdentityAdvisory = $pathIdentity
        Name = $name
        CreationDate = $creation
        CommandLine = $cmd
        ExecutablePath = $exe
    }
}

function Write-TmadDaemonState {
    param([Parameter(Mandatory = $true)]$State,[string]$Level = 'INFO')
    $cmdCompact = ([string]$State.CommandLine).Replace([string][char]13,' ').Replace([string][char]10,' ')
    if ($cmdCompact.Length -gt 220) { $cmdCompact = $cmdCompact.Substring(0,220) + '...' }
    Write-Log (
        "{0} health | lock={1} pid={2} live={3} fresh={4} python={5} venv_path_advisory={6} name={7} created={8} cmd={9}" -f
        $State.Label,$State.LockExists,$State.Pid,$State.Live,$State.Fresh,$State.Python,
        $State.PathIdentityAdvisory,$State.Name,$State.CreationDate,$cmdCompact
    ) $Level
}

function Restart-MissingTmadDaemon {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('data','alerts')][string]$Bot
    )
    $subdir = if ($Bot -eq 'data') { 'data_bot' } else { 'alerts_bot' }
    $script = Join-Path $ProductionRoot ("01_PROGRAMAS\{0}\start-bot.ps1" -f $subdir)
    if (-not (Test-Path $script -PathType Leaf)) {
        Write-Log ("No se puede reintentar {0}: falta {1}" -f $Bot,$script) 'WARN'
        return
    }
    $scriptFull = [System.IO.Path]::GetFullPath($script)
    foreach ($shell in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match 'powershell' -and $_.CommandLine -and
        ([string]$_.CommandLine).IndexOf($scriptFull,[System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        ([string]$_.CommandLine) -like '*-Mode*daemon*'
    })) {
        Write-Log ("Reintento {0}: cerrando wrapper sin daemon valido PID={1}" -f $Bot,$shell.ProcessId) 'WARN'
        [void](Stop-TmadProcessTreeVerified -RootPid ([int]$shell.ProcessId) -Label ("TMAD {0} retry" -f $Bot))
    }
    $lockPath = if ($Bot -eq 'data') {
        Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_core_data_bot.lock'
    } else {
        Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_alerts_adaptive_bot.lock'
    }
    $lockPid = Read-LockPid $lockPath
    if ($lockPid -le 0 -or -not (Get-CimProcessSafe -ProcessId $lockPid)) {
        Remove-Item -Force -ErrorAction SilentlyContinue $lockPath
    }
    $programDir = Split-Path -Parent $scriptFull
    Start-Process -FilePath 'powershell.exe' -WorkingDirectory $programDir -ArgumentList @(
        '-NoExit','-NoProfile','-ExecutionPolicy','Bypass','-File',$scriptFull,'-Mode','daemon'
    ) | Out-Null
    Write-Log ("Reintento dirigido lanzado para {0}." -f $Bot) 'WARN'
}

function Start-TmadBots {
    $bat = Join-Path $ProductionRoot 'INICIAR_TMAD_DUAL_BOT.bat'
    if (-not (Test-Path $bat -PathType Leaf)) { throw 'Falta INICIAR_TMAD_DUAL_BOT.bat en produccion.' }
    $dataLock = Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_core_data_bot.lock'
    $alertsLock = Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_alerts_adaptive_bot.lock'
    if ((Test-Path $dataLock) -or (Test-Path $alertsLock)) {
        throw 'No se arrancara TMAD con locks preexistentes; la parada anterior no quedo limpia.'
    }

    $startedAt = Get-Date
    Write-Log 'Arrancando una unica pareja Data Bot + Alerts Bot...'
    & cmd.exe /d /c ('"' + $bat + '"')
    if ($LASTEXITCODE -ne 0) { throw "Launcher devolvio $LASTEXITCODE" }

    $deadline = (Get-Date).AddSeconds(180)
    $nextDiagnostic = (Get-Date).AddSeconds(15)
    $retryAt = (Get-Date).AddSeconds(55)
    $retriedData = $false
    $retriedAlerts = $false
    while ((Get-Date) -lt $deadline) {
        $dataState = Get-TmadDaemonState -LockPath $dataLock -StartedAt $startedAt -Label 'Data' -ExpectedVenvFragment '\venvs\data_bot\Scripts\python.exe'
        $alertsState = Get-TmadDaemonState -LockPath $alertsLock -StartedAt $startedAt -Label 'Alerts' -ExpectedVenvFragment '\venvs\alerts_bot\Scripts\python.exe'

        if ($dataState.Valid -and $alertsState.Valid) {
            Write-Log ("Pareja nueva confirmada: Data PID={0}; Alerts PID={1}; path advisory Data={2} Alerts={3}." -f $dataState.Pid,$alertsState.Pid,$dataState.PathIdentityAdvisory,$alertsState.PathIdentityAdvisory) 'OK'
            return
        }

        if ((Get-Date) -ge $nextDiagnostic) {
            Write-TmadDaemonState -State $dataState -Level $(if ($dataState.Valid) { 'INFO' } else { 'WARN' })
            Write-TmadDaemonState -State $alertsState -Level $(if ($alertsState.Valid) { 'INFO' } else { 'WARN' })
            $nextDiagnostic = (Get-Date).AddSeconds(15)
        }

        if ((Get-Date) -ge $retryAt) {
            if (-not $dataState.Valid -and -not $retriedData) {
                Restart-MissingTmadDaemon -Bot data
                $retriedData = $true
            }
            if (-not $alertsState.Valid -and -not $retriedAlerts) {
                Restart-MissingTmadDaemon -Bot alerts
                $retriedAlerts = $true
            }
            $retryAt = [datetime]::MaxValue
        }
        Start-Sleep -Seconds 3
    }

    $dataState = Get-TmadDaemonState -LockPath $dataLock -StartedAt $startedAt -Label 'Data' -ExpectedVenvFragment '\venvs\data_bot\Scripts\python.exe'
    $alertsState = Get-TmadDaemonState -LockPath $alertsLock -StartedAt $startedAt -Label 'Alerts' -ExpectedVenvFragment '\venvs\alerts_bot\Scripts\python.exe'
    Write-TmadDaemonState -State $dataState -Level 'ERROR'
    Write-TmadDaemonState -State $alertsState -Level 'ERROR'
    throw 'No se confirmo una pareja NUEVA de Data + Alerts mediante locks/PIDs vivos y recientes.'
}

function Restore-Backup {
    param([string[]]$RootFiles)
    Write-Log 'Iniciando rollback automatico...' 'WARN'
    try { Stop-TmadBots } catch { Write-Log $_.Exception.Message 'WARN' }
    Invoke-Mirror -Source (Join-Path $BackupDir '01_PROGRAMAS') -Destination (Join-Path $ProductionRoot '01_PROGRAMAS') -Label 'Rollback 01_PROGRAMAS'
    $rootBackupDir = Join-Path $BackupDir 'root_files'
    foreach ($name in $RootFiles) {
        $src = Join-Path $rootBackupDir $name
        $dst = Join-Path $ProductionRoot $name
        if (Test-Path $src -PathType Leaf) { Copy-Item $src $dst -Force }
        else { Remove-Item $dst -Force -ErrorAction SilentlyContinue }
    }
    Write-Log 'Rollback completado.' 'OK'
    if (-not $NoStart) {
        Start-TmadBots
        Write-Log 'Version anterior arrancada despues del rollback.' 'OK'
    }
}

try {
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host ' TMAD - DESPLIEGUE DIRECTO DESDE REPO _2' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git no esta disponible en PATH.' }
    if (-not (Test-Path (Join-Path $RepoRoot '.git') -PathType Container)) { throw "Repo invalido: $RepoRoot" }
    if (-not (Test-Path (Join-Path $ProductionRoot '01_PROGRAMAS') -PathType Container)) { throw "Produccion invalida: $ProductionRoot" }

    $branch = (Invoke-Git @('branch','--show-current') | Select-Object -First 1).Trim()
    if ($branch -ne 'main') { throw "La carpeta _2 debe estar en main; rama actual=$branch" }
    $dirty = @(Invoke-Git @('status','--porcelain'))
    if ($dirty.Count -gt 0) { throw "Working tree no limpio en _2. No se desplegara.`n$($dirty -join [Environment]::NewLine)" }

    $version = (Get-Content (Join-Path $RepoRoot 'VERSION.txt') -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION.txt invalido: $version" }
    $versionUnderscore = $version.Replace('.','_')
    $validator = 'VALIDAR_TMAD_CURRENT.bat'
    $postCopyValidator = 'VALIDAR_TMAD_POSTCOPY.bat'
    $BackupDir = Join-Path $BackupBase ("{0}_v{1}" -f $Timestamp,$version)
    $DeploymentLog = Join-Path $DeploymentLogDir ("deploy_{0}_v{1}.log" -f $Timestamp,$versionUnderscore)
    $rootFiles = @(
        'ABRIR_REPORTES.bat',
        'CONFIGURAR_CANALES.bat',
        'DETENER_TMAD_DUAL_BOT.bat',
        'DIAGNOSTICO_ESTRUCTURA.bat',
        'DIAGNOSTICO_LANZADOR.bat',
        'ESTADO_COORDINACION.bat',
        'INICIAR_TMAD_DUAL_BOT.bat',
        'REINTENTAR_PRIMERA_EJECUCION.bat',
        'VERSION.txt',
        $validator,
        $postCopyValidator
    )

    Write-Log "Repo: $RepoRoot"
    Write-Log "Produccion: $ProductionRoot"
    Write-Log "Version: v$version"
    Write-Log ("Commit _2: {0}" -f ((Invoke-Git @('rev-parse','--short','HEAD') | Select-Object -First 1).Trim()))
    Show-ChannelConfig

    Invoke-Validator -Root $RepoRoot -ValidatorName $validator

    $runtimeLogInstaller = Join-Path $RepoRoot 'INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1'
    if (Test-Path $runtimeLogInstaller -PathType Leaf) {
        Write-Log 'Actualizando observabilidad TMAD (best-effort, fuera del camino critico)...'
        try {
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $runtimeLogInstaller -RepoPath $RepoRoot -ProductionPath $ProductionRoot -RetentionDays 7 -FromDeploy
            if ($LASTEXITCODE -ne 0) { throw "Instalador observabilidad devolvio $LASTEXITCODE" }
            Write-Log 'Observabilidad TMAD actualizada.' 'OK'
        } catch {
            Write-Log ('Observabilidad no pudo actualizarse; el deploy continua: ' + $_.Exception.Message) 'WARN'
        }
    } else {
        Write-Log 'Instalador de observabilidad no presente; el deploy continua.' 'WARN'
    }

    $dataBackupInstaller = Join-Path $RepoRoot 'INSTALAR_BACKUP_TMAD_DATA_GITHUB.ps1'
    if (-not (Test-Path $dataBackupInstaller -PathType Leaf)) { throw "Falta instalador TMAD data backup: $dataBackupInstaller" }
    Write-Log 'Actualizando tareas GitHub backup de 03_DATOS (upload-only; sin purge)...'
    try {
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $dataBackupInstaller -RepoPath $RepoRoot -ProductionPath $ProductionRoot -FromDeploy
        if ($LASTEXITCODE -ne 0) { throw "Instalador TMAD data backup devolvio $LASTEXITCODE" }
        Write-Log 'Tareas backup 03_DATOS instaladas; purga local desactivada.' 'OK'
    } catch {
        Write-Log ('No se pudieron instalar tareas backup 03_DATOS: ' + $_.Exception.Message) 'WARN'
    }

    $rootBackupDir = Join-Path $BackupDir 'root_files'
    New-Item -ItemType Directory -Path $rootBackupDir -Force | Out-Null
    Invoke-Mirror -Source (Join-Path $ProductionRoot '01_PROGRAMAS') -Destination (Join-Path $BackupDir '01_PROGRAMAS') -Label 'Backup 01_PROGRAMAS'
    foreach ($name in $rootFiles) {
        $src = Join-Path $ProductionRoot $name
        if (Test-Path $src -PathType Leaf) { Copy-Item $src (Join-Path $rootBackupDir $name) -Force }
    }
    Write-Log "Backup: $BackupDir" 'OK'

    Stop-TmadBots
    $BotsStopped = $true

    Invoke-Mirror -Source (Join-Path $RepoRoot '01_PROGRAMAS') -Destination (Join-Path $ProductionRoot '01_PROGRAMAS') -Label 'Actualizacion 01_PROGRAMAS'
    foreach ($name in $rootFiles) {
        $src = Join-Path $RepoRoot $name
        if (Test-Path $src -PathType Leaf) { Copy-Item $src (Join-Path $ProductionRoot $name) -Force }
    }
    Write-Log '02_CONFIG, 03_DATOS y 04_RUNTIME preservados.' 'OK'

    Invoke-Validator -Root $ProductionRoot -ValidatorName $postCopyValidator

    # No detached historical replay/launcher may compete with production startup.
    $preStartReplayOrphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match "^python(w)?[.]exe$" -and
        $_.CommandLine -and
        ([string]$_.CommandLine) -match "historical_replay_once[.]py"
    })
    foreach ($proc in $preStartReplayOrphans) {
        Write-Log ("Cerrando replay historico previo PID={0} antes del arranque productivo." -f $proc.ProcessId) 'WARN'
        Stop-Process -Id ([int]$proc.ProcessId) -Force -ErrorAction SilentlyContinue
    }

    $preStartReplayLaunchers = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match "powershell" -and
        $_.CommandLine -and
        ([string]$_.CommandLine) -match "deferred_historical_replay[.]ps1"
    })
    foreach ($proc in $preStartReplayLaunchers) {
        Write-Log ("Cerrando launcher historico diferido previo PID={0}." -f $proc.ProcessId) 'WARN'
        Stop-Process -Id ([int]$proc.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if (($preStartReplayOrphans.Count + $preStartReplayLaunchers.Count) -gt 0) {
        Start-Sleep -Seconds 2
    }

    # Production always gets resource priority. Historical replay is launched
    # later by a detached gate only after current startup finishes successfully
    # and any previous adaptive-research process has exited.
    $replayEligibilityEpoch = [DateTimeOffset]::Now.ToUnixTimeSeconds()
    if (-not $NoStart) { Start-TmadBots }

    # Rehydrate verified cold Alerts layer archives before the deferred replay.
    $coldMaterializer = Join-Path $RepoRoot 'MATERIALIZE_TMAD_ALERTS_COLD_HISTORY.ps1'
    if (Test-Path $coldMaterializer -PathType Leaf) {
        try {
            & $coldMaterializer -ProductionPath $ProductionRoot -GitHubRepo 'SMS-Server-Bots/tmad-dual-bot' -WarmDays 45 -MaxFiles 120
            if ($LASTEXITCODE -ne 0) { throw "Materializador cold Alerts devolvio $LASTEXITCODE" }
            Write-Log 'Cold history Alerts verificado/materializado antes del replay diferido.' 'OK'
        } catch {
            Write-Log ('Cold history Alerts no pudo materializarse; replay sigue no critico: ' + $_.Exception.Message) 'WARN'
        }
    } else {
        Write-Log 'Materializador cold Alerts no presente; replay seguira con warm/cache disponible.' 'WARN'
    }

    # v6.2.65 is a research-only historical stress launch. Reuse the last
    # successfully frozen Rolling + Fixed caches when both exactly match one
    # completed valid replay; a redundant multi-hour FULL is not justified.
    # If evidence is incomplete, retain the usual deferred historical replay.
    $reuseFrozenHistoryForStress = $false
    if ($version -eq '6.2.65' -and -not $NoStart) {
        try {
            $researchDir = Join-Path $ProductionRoot '03_DATOS\alerts_bot\output\historical_validation'
            $statusFile = Join-Path $researchDir 'latest_fast_research_cache_status.json'
            $perfFile = Join-Path $researchDir 'latest_historical_replay_performance.json'
            $cacheDir = Join-Path $ProductionRoot '04_RUNTIME\autonomous_research\cache'
            if ((Test-Path $statusFile -PathType Leaf) -and (Test-Path $perfFile -PathType Leaf)) {
                $cache = Get-Content $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
                $perf = Get-Content $perfFile -Raw -Encoding UTF8 | ConvertFrom-Json
                $r = $cache.lanes.ROLLING_LATEST
                $f = $cache.lanes.FIXED_BENCHMARK
                $reuseFrozenHistoryForStress = (
                    [string]$perf.status -eq 'ok' -and
                    [string]$perf.producer_version -eq [string]$cache.producer_version -and
                    [string]$r.status -eq 'OK' -and
                    [string]$f.status -eq 'OK' -and
                    -not [string]::IsNullOrWhiteSpace([string]$r.dataset_fingerprint) -and
                    -not [string]::IsNullOrWhiteSpace([string]$f.dataset_fingerprint) -and
                    -not [string]::IsNullOrWhiteSpace([string]$r.contract_hash) -and
                    [string]$r.contract_hash -eq [string]$f.contract_hash -and
                    (Test-Path (Join-Path $cacheDir 'rolling_latest.v1.json.gz') -PathType Leaf) -and
                    (Test-Path (Join-Path $cacheDir 'fixed_benchmark.v1.json.gz') -PathType Leaf)
                )
            }
        } catch {
            Write-Log ('No se pudo verificar reutilizacion historica: ' + $_.Exception.Message) 'WARN'
            $reuseFrozenHistoryForStress = $false
        }
    }

    $deferredReplay = Join-Path $ProductionRoot '01_PROGRAMAS\alerts_bot\deferred_historical_replay.ps1'
    $replayRuntime = Join-Path $ProductionRoot '04_RUNTIME\historical_replay'
    if ($NoStart) {
        Write-Log 'Replay historico diferido omitido: despliegue solicitado con NoStart.' 'WARN'
    } elseif ($reuseFrozenHistoryForStress) {
        Write-Log 'Replay FULL omitido en 6.2.65: Rolling y Fixed congelados + último FULL válido. Se ejecutará stress detached.' 'OK'
    } elseif (-not (Test-Path $deferredReplay -PathType Leaf)) {
        Write-Log ("Replay historico diferido omitido: falta {0}" -f $deferredReplay) 'WARN'
    } else {
        try {
            New-Item -ItemType Directory -Force -Path $replayRuntime | Out-Null
            $launcherStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
            $launcherStdout = Join-Path $replayRuntime ("deferred_launcher_{0}.log" -f $launcherStamp)
            $launcherStderr = Join-Path $replayRuntime ("deferred_launcher_{0}.err.log" -f $launcherStamp)
            $launcherArgs = @(
                '-NoLogo','-NoProfile','-ExecutionPolicy','Bypass',
                '-File',$deferredReplay,
                '-ProductionRoot',$ProductionRoot,
                '-NotBeforeEpoch',([string]$replayEligibilityEpoch)
            )
            $launcherProc = Start-Process -FilePath 'powershell.exe' -ArgumentList $launcherArgs -WorkingDirectory (Split-Path -Parent $deferredReplay) -WindowStyle Hidden -RedirectStandardOutput $launcherStdout -RedirectStandardError $launcherStderr -PassThru
            Write-Log (
                "Replay historico diferido armado PID={0}; esperara warm-up sano y fin de adaptive research. Status={1}" -f
                $launcherProc.Id,(Join-Path $replayRuntime 'deferred_launcher_status.json')
            ) 'OK'
        } catch {
            Write-Log ("Launcher historico diferido no critico no pudo arrancar; produccion sigue valida: {0}" -f $_.Exception.Message) 'WARN'
        }
    }

    $historyInstaller = Join-Path $RepoRoot 'INSTALAR_HISTORICO_TMAD.ps1'
    if (Test-Path $historyInstaller -PathType Leaf) {
        Write-Log 'Instalando gestion activa de historico + migracion segura old_2...'
        try {
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $historyInstaller -RepoRoot $RepoRoot -ProductionRoot $ProductionRoot -FromDeploy
            if ($LASTEXITCODE -ne 0) { throw "Instalador historico devolvio $LASTEXITCODE" }
            Write-Log 'Gestion de historico instalada; migracion old_2 lanzada en background.' 'OK'
        } catch {
            Write-Log ('Gestion de historico no pudo instalarse; deploy principal sigue valido: ' + $_.Exception.Message) 'WARN'
        }
    } else {
        Write-Log 'INSTALAR_HISTORICO_TMAD.ps1 no presente; se omite gestion historica.' 'WARN'
    }

    # The detached stress worker is strictly research-only, keeps an OS lock,
    # waits until production startup/replay/adaptive research are idle, and
    # never prevents deployment success when its own launch fails.
    if ($version -eq '6.2.65' -and -not $NoStart) {
        try {
            $autoStress = Join-Path $ProductionRoot '01_PROGRAMAS\alerts_bot\auto_joint_stress.py'
            $stressPython = Join-Path $ProductionRoot '04_RUNTIME\venvs\alerts_bot\Scripts\python.exe'
            $stressRuntime = Join-Path $ProductionRoot '04_RUNTIME\autonomous_research\joint_entry_exit_stress'
            if (-not (Test-Path $autoStress -PathType Leaf)) { throw 'Auto stress runner missing' }
            if (-not (Test-Path $stressPython -PathType Leaf)) { throw 'Persistent Alerts Python missing' }
            New-Item -ItemType Directory -Path $stressRuntime -Force | Out-Null
            $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
            $stdout = Join-Path $stressRuntime ('auto_stress_' + $stamp + '.log')
            $stderr = Join-Path $stressRuntime ('auto_stress_' + $stamp + '.err.log')
            $proc = Start-Process -FilePath $stressPython -ArgumentList @('-u', $autoStress) -WorkingDirectory (Split-Path -Parent $autoStress) -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
            Write-Log ('Joint stress histórico autónomo armado PID=' + $proc.Id + '; status en 04_RUNTIME/autonomous_research/joint_entry_exit_stress/latest_status.json') 'OK'
        } catch {
            Write-Log ('Joint stress no pudo lanzarse (no crítico): ' + $_.Exception.Message) 'WARN'
        }
    }

    $startState = if ($NoStart) { 'NO START solicitado' } else { 'arranque confirmado' }
    Send-SystemStatus "TMAD SYSTEM - DEPLOY OK`nVersion v$version - fuente main/_2 - Data/Alerts $startState"
    Write-Log "TMAD v$version desplegado correctamente." 'OK'
    exit 0
}
catch {
    $message = $_.Exception.Message
    Write-Log $message 'ERROR'
    if ($BotsStopped -and $BackupDir -and (Test-Path $BackupDir)) {
        try { Restore-Backup -RootFiles $rootFiles }
        catch { Write-Log ('ROLLBACK FALLO: ' + $_.Exception.Message) 'ERROR' }
    }
    Send-SystemStatus ("TMAD SYSTEM - DEPLOY FALLIDO`n" + $message)
    exit 1
}
