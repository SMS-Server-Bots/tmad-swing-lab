[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v\d+\.\d+\.\d+$')]
    [string]$Tag,

    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [switch]$NoStart,
    [switch]$SkipRemoteFetch
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$Version = $Tag.TrimStart('v')
$VersionUnderscore = $Version.Replace('.', '_')
$ValidatorName = "VALIDAR_TMAD_V$VersionUnderscore.bat"
$TempRoot = Join-Path $env:TEMP ("tmad_deploy_{0}_{1}" -f $VersionUnderscore, [guid]::NewGuid().ToString('N'))
$ArchivePath = Join-Path $TempRoot 'release.zip'
$ReleaseRoot = Join-Path $TempRoot 'release'
$BackupBase = Join-Path ([Environment]::GetFolderPath('Desktop')) 'TMAD_DEPLOY_BACKUPS'
$BackupDir = Join-Path $BackupBase ("{0}_{1}" -f $Timestamp, $Tag)
$DeploymentLogDir = Join-Path $ProductionRoot '04_RUNTIME\deployments'
$DeploymentLog = Join-Path $DeploymentLogDir ("deploy_{0}_{1}.log" -f $Timestamp, $VersionUnderscore)
$SwapCompleted = $false
$BotsStopped = $false
$ProductionPrefix = $ProductionRoot.TrimEnd('\') + '\'

$RootDeployFiles = @(
    'ABRIR_REPORTES.bat',
    'CONFIGURAR_CANALES.bat',
    'DETENER_TMAD_DUAL_BOT.bat',
    'DIAGNOSTICO_ESTRUCTURA.bat',
    'DIAGNOSTICO_LANZADOR.bat',
    'ESTADO_COORDINACION.bat',
    'INICIAR_TMAD_DUAL_BOT.bat',
    'REINTENTAR_PRIMERA_EJECUCION.bat',
    'PUBLICAR_Y_DESPLEGAR_TMAD.ps1',
    'DESPLEGAR_RELEASE_TMAD.ps1',
    'VERSION.txt',
    $ValidatorName,
    ("FUENTES_SHA256_V{0}.txt" -f $VersionUnderscore),
    ("PATCH_MANIFEST_V{0}.txt" -f $VersionUnderscore)
)

function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'OK', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $color = switch ($Level) {
        'OK' { 'Green' }
        'WARN' { 'Yellow' }
        'ERROR' { 'Red' }
        default { 'Cyan' }
    }
    Write-Host $line -ForegroundColor $color
    try {
        New-Item -ItemType Directory -Path $DeploymentLogDir -Force | Out-Null
        Add-Content -Path $DeploymentLog -Value $line -Encoding UTF8
    } catch {}
}

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = & git -C $RepoRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Git fallo: git -C `"$RepoRoot`" $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return @($output)
}

function Invoke-Validator {
    param([Parameter(Mandatory = $true)][string]$Root)
    $validator = Join-Path $Root $ValidatorName
    if (-not (Test-Path $validator -PathType Leaf)) {
        throw "La release no contiene el validador esperado: $ValidatorName"
    }

    # El validador v6.1.13 comprueba tambien el publicador, pero ese archivo es
    # una herramienta de control y no se instala permanentemente en produccion.
    # Para mantener la validacion exacta del tag sin contaminar ProductionRoot,
    # se aporta solo durante la ejecucion y se elimina siempre al terminar.
    $publisherPath = Join-Path $Root 'PUBLICAR_Y_DESPLEGAR_TMAD.bat'
    $temporaryPublisher = $false
    if (-not (Test-Path $publisherPath -PathType Leaf)) {
        $publisherSource = Join-Path $RepoRoot 'PUBLICAR_Y_DESPLEGAR_TMAD.bat'
        if (-not (Test-Path $publisherSource -PathType Leaf)) {
            throw 'No existe PUBLICAR_Y_DESPLEGAR_TMAD.bat para completar el contexto de validacion.'
        }
        Copy-Item -LiteralPath $publisherSource -Destination $publisherPath -Force
        $temporaryPublisher = $true
        Write-Log 'Contexto temporal de publicacion creado para validar produccion.'
    }

    Write-Log "Ejecutando $ValidatorName en $Root"
    Push-Location $Root
    try {
        & cmd.exe /d /c "`"$validator`""
        if ($LASTEXITCODE -ne 0) {
            throw "La validacion termino con codigo $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
        if ($temporaryPublisher -and (Test-Path $publisherPath -PathType Leaf)) {
            Remove-Item -LiteralPath $publisherPath -Force -ErrorAction SilentlyContinue
            Write-Log 'Contexto temporal de publicacion eliminado de produccion.'
        }
    }
    Write-Log 'Validacion completada correctamente.' 'OK'
}

function Get-TmadProcesses {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine.IndexOf($ProductionPrefix, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.Name -match 'python|powershell|cmd'
    })
}

function Stop-TmadBots {
    # No se ejecuta 01_PROGRAMAS\launcher\detener-bots.ps1: las versiones
    # anteriores comparaban por prefijo y podían confundir la carpeta de
    # trabajo TMAD_Dual_Bot_OneClick_2 con producción.
    Write-Log 'Deteniendo procesos TMAD de producción con coincidencia de ruta exacta...'

    $remaining = @(Get-TmadProcesses)
    foreach ($process in $remaining) {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            Write-Log ("Proceso TMAD detenido: PID {0} · {1}" -f $process.ProcessId, $process.Name) 'WARN'
        }
        catch {
            throw "No se pudo detener el proceso TMAD PID $($process.ProcessId)."
        }
    }

    Start-Sleep -Seconds 2
    Remove-Item -Force -ErrorAction SilentlyContinue `
        (Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_core_data_bot.lock'), `
        (Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_alerts_adaptive_bot.lock')

    Write-Log ("Bots detenidos: {0}. Locks limpiados." -f $remaining.Count) 'OK'
}

function Start-TmadBots {
    $startBat = Join-Path $ProductionRoot 'INICIAR_TMAD_DUAL_BOT.bat'
    if (-not (Test-Path $startBat -PathType Leaf)) {
        throw 'No existe INICIAR_TMAD_DUAL_BOT.bat en produccion.'
    }

    Write-Log 'Arrancando Data Bot y Alerts Bot...'
    & cmd.exe /d /c "`"$startBat`""
    if ($LASTEXITCODE -ne 0) {
        throw "El lanzador devolvio codigo $LASTEXITCODE."
    }

    $dataLock = Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_core_data_bot.lock'
    $alertsLock = Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_alerts_adaptive_bot.lock'
    $deadline = (Get-Date).AddSeconds(45)

    while ((Get-Date) -lt $deadline) {
        if ((Test-Path $dataLock) -and (Test-Path $alertsLock)) {
            Write-Log 'Data Bot y Alerts Bot han creado sus locks de ejecucion.' 'OK'
            return
        }
        Start-Sleep -Seconds 3
    }

    $missing = @()
    if (-not (Test-Path $dataLock)) { $missing += 'Data Bot' }
    if (-not (Test-Path $alertsLock)) { $missing += 'Alerts Bot' }
    throw "No se confirmo el arranque mediante locks: $($missing -join ', ')."
}

function Invoke-RobocopyMirror {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Operation
    )

    if (-not (Test-Path $Source -PathType Container)) {
        throw "No existe el origen para $Operation`: $Source"
    }

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null

    # Evita que atributos heredados dejen ficheros sin posibilidad de sustitucion.
    try {
        & attrib.exe -R (Join-Path $Destination '*') /S /D 2>$null | Out-Null
    }
    catch {}

    Write-Log "$Operation mediante copia espejo..."
    $args = @(
        $Source,
        $Destination,
        '/MIR',
        '/COPY:DAT',
        '/DCOPY:DAT',
        '/R:12',
        '/W:2',
        '/XJ',
        '/FFT',
        '/NP',
        '/NFL',
        '/NDL'
    )
    $output = & robocopy.exe @args 2>&1
    $code = $LASTEXITCODE

    # Robocopy usa 0-7 para resultados correctos o con diferencias copiadas.
    if ($code -gt 7) {
        $tail = @($output | Select-Object -Last 30)
        throw "$Operation fallo con codigo Robocopy $code.`n$($tail -join [Environment]::NewLine)"
    }

    Write-Log "$Operation completada · Robocopy=$code" 'OK'
}

function Wait-NoTmadProcesses {
    param([int]$TimeoutSeconds = 20)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $remaining = @(Get-TmadProcesses)
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    $details = $remaining | ForEach-Object { "PID=$($_.ProcessId) $($_.Name)" }
    throw "Persisten procesos TMAD despues de la parada: $($details -join '; ')"
}

function Restore-Backup {
    param([switch]$Restart)

    Write-Log 'Iniciando rollback automatico...' 'WARN'
    try { Stop-TmadBots } catch { Write-Log $_.Exception.Message 'WARN' }

    $currentPrograms = Join-Path $ProductionRoot '01_PROGRAMAS'
    $backupPrograms = Join-Path $BackupDir '01_PROGRAMAS'
    if (-not (Test-Path $backupPrograms -PathType Container)) {
        throw 'No existe una copia valida de 01_PROGRAMAS para el rollback.'
    }
    Invoke-RobocopyMirror -Source $backupPrograms -Destination $currentPrograms -Operation 'Rollback de 01_PROGRAMAS'

    $backupRootFiles = Join-Path $BackupDir 'root_files'
    if (Test-Path $backupRootFiles) {
        $backedUpNames = @(Get-ChildItem $backupRootFiles -File | ForEach-Object { $_.Name })
        foreach ($file in $RootDeployFiles) {
            if ($backedUpNames -notcontains $file) {
                Remove-Item -LiteralPath (Join-Path $ProductionRoot $file) -Force -ErrorAction SilentlyContinue
            }
        }
        Get-ChildItem $backupRootFiles -File | ForEach-Object {
            Copy-Item $_.FullName (Join-Path $ProductionRoot $_.Name) -Force
        }
    }

    Write-Log 'Rollback de archivos completado.' 'OK'
    if ($Restart) {
        Start-TmadBots
        Write-Log 'Version anterior arrancada despues del rollback.' 'OK'
    }
}

try {
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host " TMAD · DESPLIEGUE DE RELEASE $Tag" -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan

    if (-not (Test-Path $RepoRoot -PathType Container)) { throw "No existe RepoRoot: $RepoRoot" }
    if (-not (Test-Path (Join-Path $RepoRoot '.git') -PathType Container)) { throw "RepoRoot no contiene .git: $RepoRoot" }
    if (-not (Test-Path $ProductionRoot -PathType Container)) { throw "No existe ProductionRoot: $ProductionRoot" }
    if (-not (Test-Path (Join-Path $ProductionRoot '01_PROGRAMAS') -PathType Container)) { throw 'Produccion no contiene 01_PROGRAMAS.' }
    if (-not (Test-Path $PowerShellExe -PathType Leaf)) { throw "No existe Windows PowerShell: $PowerShellExe" }

    New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    New-Item -ItemType Directory -Path $DeploymentLogDir -Force | Out-Null

    Write-Log "Repositorio: $RepoRoot"
    Write-Log "Produccion: $ProductionRoot"
    Write-Log "Release solicitada: $Tag"

    if (-not $SkipRemoteFetch) {
        Write-Log 'Actualizando tags desde GitHub...'
        [void](Invoke-Git @('fetch', 'origin', '--tags', '--prune'))
    }

    $localCommit = (Invoke-Git @('rev-list', '-n', '1', $Tag) | Select-Object -First 1).Trim()
    if (-not $localCommit) { throw "No existe el tag local $Tag." }

    $remoteLines = @(& git -C $RepoRoot ls-remote --tags origin "refs/tags/$Tag*" 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "No se pudo verificar el tag remoto $Tag." }
    $remoteCommit = $null
    foreach ($line in $remoteLines) {
        if ($line -match '^([0-9a-f]{40})\s+refs/tags/.+\^\{\}$') { $remoteCommit = $Matches[1]; break }
    }
    if (-not $remoteCommit) {
        foreach ($line in $remoteLines) {
            if ($line -match '^([0-9a-f]{40})\s+refs/tags/') { $remoteCommit = $Matches[1]; break }
        }
    }
    if (-not $remoteCommit) { throw "El tag $Tag no existe en origin." }
    if ($localCommit -ne $remoteCommit) {
        throw "El tag local y remoto no apuntan al mismo commit: local=$localCommit remoto=$remoteCommit"
    }
    Write-Log "Tag verificado en GitHub: $Tag · commit $($localCommit.Substring(0, 7))" 'OK'

    Write-Log 'Generando paquete exacto desde el tag...'
    [void](Invoke-Git @('archive', '--format=zip', "--output=$ArchivePath", $Tag))
    Expand-Archive -Path $ArchivePath -DestinationPath $ReleaseRoot -Force

    if (-not (Test-Path (Join-Path $ReleaseRoot '01_PROGRAMAS') -PathType Container)) {
        throw 'El tag no contiene 01_PROGRAMAS.'
    }
    foreach ($forbidden in @('.env', '02_CONFIG', '03_DATOS', '04_RUNTIME')) {
        if (Test-Path (Join-Path $ReleaseRoot $forbidden)) {
            throw "La release contiene una ruta prohibida: $forbidden"
        }
    }

    $releaseVersionFile = Join-Path $ReleaseRoot 'VERSION.txt'
    if (-not (Test-Path $releaseVersionFile -PathType Leaf)) { throw 'La release no contiene VERSION.txt.' }
    $releaseVersion = (Get-Content $releaseVersionFile -Raw).Trim()
    if ($releaseVersion -ne $Version) {
        throw "VERSION.txt indica '$releaseVersion', pero el tag solicitado es '$Tag'."
    }
    Write-Log "Contenido de release verificado: VERSION.txt=$releaseVersion" 'OK'

    Invoke-Validator -Root $ReleaseRoot

    Write-Log "Creando backup en $BackupDir..."
    Copy-Item (Join-Path $ProductionRoot '01_PROGRAMAS') (Join-Path $BackupDir '01_PROGRAMAS') -Recurse -Force
    $backupRoot = Join-Path $BackupDir 'root_files'
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    foreach ($file in $RootDeployFiles) {
        $source = Join-Path $ProductionRoot $file
        if (Test-Path $source -PathType Leaf) {
            Copy-Item $source (Join-Path $backupRoot $file) -Force
        }
    }
    @{
        created_at = (Get-Date).ToString('o')
        target_tag = $Tag
        target_commit = $localCommit
        production_root = $ProductionRoot
        repo_root = $RepoRoot
        previous_version = if (Test-Path (Join-Path $ProductionRoot 'VERSION.txt')) { (Get-Content (Join-Path $ProductionRoot 'VERSION.txt') -Raw).Trim() } else { $null }
    } | ConvertTo-Json | Set-Content (Join-Path $BackupDir 'backup_manifest.json') -Encoding UTF8
    Write-Log 'Backup completo creado.' 'OK'

    Write-Log 'Preparando actualizacion espejo de 01_PROGRAMAS...'

    $BotsStopped = $true
    Stop-TmadBots
    Wait-NoTmadProcesses -TimeoutSeconds 20

    $currentPrograms = Join-Path $ProductionRoot '01_PROGRAMAS'
    Invoke-RobocopyMirror `
        -Source (Join-Path $ReleaseRoot '01_PROGRAMAS') `
        -Destination $currentPrograms `
        -Operation 'Actualizacion de 01_PROGRAMAS'
    $SwapCompleted = $true

    foreach ($file in $RootDeployFiles) {
        $source = Join-Path $ReleaseRoot $file
        if (Test-Path $source -PathType Leaf) {
            Copy-Item $source (Join-Path $ProductionRoot $file) -Force
        }
    }
    Write-Log 'Programas y lanzadores actualizados en produccion.' 'OK'

    Invoke-Validator -Root $ProductionRoot

    if (-not $NoStart) {
        Start-TmadBots
    }
    else {
        Write-Log 'NoStart activo: los bots permanecen detenidos.' 'WARN'
    }


    Set-Content -Path (Join-Path $DeploymentLogDir 'last_backup.txt') -Value $BackupDir -Encoding UTF8
    @{
        deployed_at = (Get-Date).ToString('o')
        tag = $Tag
        commit = $localCommit
        version = $Version
        backup = $BackupDir
        started = (-not $NoStart)
    } | ConvertTo-Json | Set-Content (Join-Path $DeploymentLogDir 'last_successful_deployment.json') -Encoding UTF8

    Write-Log "DESPLIEGUE COMPLETADO · $Tag · backup=$BackupDir" 'OK'
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Green
    Write-Host " TMAD $Tag DESPLEGADA CORRECTAMENTE" -ForegroundColor Green
    Write-Host '============================================================' -ForegroundColor Green
    exit 0
}
catch {
    Write-Log $_.Exception.Message 'ERROR'
    if ($SwapCompleted) {
        try {
            Restore-Backup -Restart:(-not $NoStart)
        }
        catch {
            Write-Log ("ROLLBACK FALLIDO: {0}" -f $_.Exception.Message) 'ERROR'
        }
    }
    elseif ($BotsStopped -and -not $NoStart) {
        try { Start-TmadBots } catch { Write-Log ("No se pudo reanudar la version anterior: {0}" -f $_.Exception.Message) 'ERROR' }
    }
    Write-Host ''
    Write-Host "DESPLIEGUE FALLIDO · Revisa: $DeploymentLog" -ForegroundColor Red
    exit 1
}
finally {
    if (Test-Path $TempRoot) {
        Remove-Item $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
