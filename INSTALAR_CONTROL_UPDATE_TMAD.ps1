[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [switch]$FromUpdater
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path $RepoRoot).Path
$ProductionRoot = (Resolve-Path $ProductionRoot).Path
$TaskName = 'Trumppetta_TMAD_Discord_Update_Controller'
$LegacyReleaseFinalizerTask = 'TMAD Release Finalizer'
$ReleaseConsumerTask = 'Trumppetta_TMAD_Release_Consumer'
$Controller = Join-Path $RepoRoot 'TMAD_DISCORD_UPDATE_CONTROLLER.py'
$Worker = Join-Path $RepoRoot 'TMAD_UPDATE_ONCE.py'
$ReleaseConsumer = Join-Path $RepoRoot 'TMAD_RELEASE_CONSUMER.py'

function Log([string]$Level,[string]$Text) {
    Write-Host ('[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),$Level,$Text)
}

if (-not (Test-Path (Join-Path $RepoRoot '.git') -PathType Container)) { throw "Repo de control invalido: $RepoRoot" }
if (-not (Test-Path $ProductionRoot -PathType Container)) { throw "Produccion TMAD inexistente: $ProductionRoot" }
if (-not (Test-Path $Controller -PathType Leaf)) { throw "Falta $Controller" }
if (-not (Test-Path $Worker -PathType Leaf)) { throw "Falta $Worker" }
if (-not (Test-Path $ReleaseConsumer -PathType Leaf)) { throw "Falta $ReleaseConsumer" }

# The controller must NOT use a Python executable below ProductionRoot. The TMAD
# deployer intentionally stops production Data/Alerts trees only; using a global
# or repo-local Python keeps the control plane independent from production.
$PythonExe = $null
$pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCmd -and $pythonCmd.Source) { $PythonExe = $pythonCmd.Source }
if (-not $PythonExe) {
    $repoPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $repoPython -PathType Leaf) { $PythonExe = $repoPython }
}
if (-not $PythonExe) {
    throw 'No se encontro Python global ni .venv en el repo _2. El controlador no usara los venv de produccion.'
}

$productionPrefix = $ProductionRoot.TrimEnd('\') + '\'
if ($PythonExe.StartsWith($productionPrefix,[System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Python del controlador no puede vivir bajo produccion: $PythonExe"
}

Log INFO "Repo control: $RepoRoot"
Log INFO "Produccion: $ProductionRoot"
Log INFO "Python controlador: $PythonExe"

& $PythonExe -m py_compile $Controller $Worker $ReleaseConsumer
if ($LASTEXITCODE -ne 0) { throw 'Los scripts Python de control TMAD no compilan.' }
Log OK 'Sintaxis Python valida'

if (-not $FromUpdater) {
    Log INFO 'Validando DISCORD_TOKEN + EXECUTION_WEBHOOK + acceso al canal...'
    & $PythonExe $Controller --check-config
    if ($LASTEXITCODE -ne 0) { throw 'La validacion del controlador %update TMAD fallo.' }
    Log OK 'Canal EXECUTION y credenciales Discord validados'
}

Import-Module ScheduledTasks -ErrorAction Stop

# GitHub Actions is the single release authority (version sync + validation +
# exact tag). The legacy five-minute VPS finalizer can race with that workflow
# and dirty the immutable control clone after a failed validator. Retire it.
$legacyTask = Get-ScheduledTask -TaskName $LegacyReleaseFinalizerTask -ErrorAction SilentlyContinue
if ($legacyTask) {
    try {
        Stop-ScheduledTask -TaskName $LegacyReleaseFinalizerTask -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $LegacyReleaseFinalizerTask -ErrorAction Stop | Out-Null
        Log OK "Legacy release finalizer deshabilitado: $LegacyReleaseFinalizerTask"
    }
    catch {
        Log WARN ("No se pudo deshabilitar {0}: {1}" -f $LegacyReleaseFinalizerTask,$_.Exception.Message)
    }
}
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path $PowerShellExe -PathType Leaf)) { $PowerShellExe = 'powershell.exe' }
$pythonLiteral = $PythonExe.Replace("'","''")
$controllerLiteral = $Controller.Replace("'","''")
$argument = ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "& ''{0}'' ''{1}''"' -f $pythonLiteral,$controllerLiteral)
$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $argument -WorkingDirectory $RepoRoot

# Two independent safeguards:
# 1) AtLogOn starts the listener after a server/user session restart.
# 2) A one-minute recurring watchdog trigger starts it again if the process ever
#    exits. MultipleInstances=IgnoreNew means a healthy listener is never duplicated.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$triggers = @($logonTrigger, $watchdogTrigger)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentIdentity -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description 'Control remoto seguro TMAD: solo %update en EXECUTION; watchdog cada minuto y sin listeners duplicados.' `
    -Force | Out-Null

try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Milliseconds 500
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
Log OK "Controlador %update instalado: $TaskName"
Log INFO "Estado tarea: $($task.State)"
Log INFO "LastTaskResult: $($info.LastTaskResult)"
Log INFO 'Comando Discord: %update (solo canal EXECUTION)'
Log INFO 'Watchdog: cada 1 minuto; MultipleInstances=IgnoreNew evita duplicados.'
Log INFO 'El controlador vive en _2, usa Python fuera de produccion y no es detenido por el despliegue Data/Alerts.'

# Fallback independiente de Discord: cada 5 minutos comprueba main/tag y solo
# despliega si producción está por detrás. En no-op no publica mensajes Discord.
$consumerLiteral = $ReleaseConsumer.Replace("'","''")
$consumerArgument = ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "& ''{0}'' ''{1}''"' -f $pythonLiteral,$consumerLiteral)
$consumerAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $consumerArgument -WorkingDirectory $RepoRoot
$consumerTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$consumerSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 40)

Register-ScheduledTask `
    -TaskName $ReleaseConsumerTask `
    -Action $consumerAction `
    -Trigger $consumerTrigger `
    -Settings $consumerSettings `
    -Principal $principal `
    -Description 'Fallback TMAD: consume releases GitHub validadas si Discord %update no esta disponible.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $ReleaseConsumerTask
$consumerInfo = Get-ScheduledTaskInfo -TaskName $ReleaseConsumerTask -ErrorAction Stop
Log OK "Fallback de release instalado: $ReleaseConsumerTask"
Log INFO "Release consumer LastTaskResult: $($consumerInfo.LastTaskResult)"
