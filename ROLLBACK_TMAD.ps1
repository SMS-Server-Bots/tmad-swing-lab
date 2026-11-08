[CmdletBinding()]
param(
    [string]$BackupPath,
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [switch]$NoStart
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$DeploymentDir = Join-Path $ProductionRoot '04_RUNTIME\deployments'
$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'


function Invoke-RobocopyMirror {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path $Source -PathType Container)) { throw "No existe el origen: $Source" }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    try { & attrib.exe -R (Join-Path $Destination '*') /S /D 2>$null | Out-Null } catch {}
    $output = & robocopy.exe $Source $Destination /MIR /COPY:DAT /DCOPY:DAT /R:12 /W:2 /XJ /FFT /NP /NFL /NDL 2>&1
    $code = $LASTEXITCODE
    if ($code -gt 7) {
        throw "Robocopy fallo con codigo $code.`n$((@($output | Select-Object -Last 30)) -join [Environment]::NewLine)"
    }
    Write-Host "01_PROGRAMAS restaurado · Robocopy=$code" -ForegroundColor Green
}

function Stop-TmadBots {
    $stopScript = Join-Path $ProductionRoot '01_PROGRAMAS\launcher\detener-bots.ps1'
    if (Test-Path $stopScript) {
        & $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $stopScript
    }
    Start-Sleep -Seconds 2
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and
        $_.CommandLine.IndexOf($ProductionRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.Name -match 'python|powershell|cmd'
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Force -ErrorAction SilentlyContinue `
        (Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_core_data_bot.lock'), `
        (Join-Path $ProductionRoot '04_RUNTIME\locks\tmad_alerts_adaptive_bot.lock')
}

function Start-TmadBots {
    $startBat = Join-Path $ProductionRoot 'INICIAR_TMAD_DUAL_BOT.bat'
    if (-not (Test-Path $startBat)) { throw 'No existe INICIAR_TMAD_DUAL_BOT.bat.' }
    & cmd.exe /d /c "`"$startBat`""
    if ($LASTEXITCODE -ne 0) { throw "El arranque devolvio codigo $LASTEXITCODE." }
}

if (-not $BackupPath) {
    $lastBackupFile = Join-Path $DeploymentDir 'last_backup.txt'
    if (-not (Test-Path $lastBackupFile)) {
        throw 'No se indico BackupPath y no existe 04_RUNTIME\deployments\last_backup.txt.'
    }
    $BackupPath = (Get-Content $lastBackupFile -Raw).Trim()
}

if (-not (Test-Path $BackupPath -PathType Container)) { throw "No existe el backup: $BackupPath" }
if (-not (Test-Path (Join-Path $BackupPath '01_PROGRAMAS') -PathType Container)) { throw 'El backup no contiene 01_PROGRAMAS.' }
if (-not (Test-Path $ProductionRoot -PathType Container)) { throw "No existe produccion: $ProductionRoot" }

Write-Host ''
Write-Host '============================================================' -ForegroundColor Yellow
Write-Host ' TMAD · ROLLBACK MANUAL' -ForegroundColor Yellow
Write-Host '============================================================' -ForegroundColor Yellow
Write-Host "Backup: $BackupPath"
Write-Host "Produccion: $ProductionRoot"

$SafetyBackup = Join-Path ([Environment]::GetFolderPath('Desktop')) ("TMAD_PRE_ROLLBACK_{0}" -f $Timestamp)
New-Item -ItemType Directory -Path $SafetyBackup -Force | Out-Null
if (Test-Path (Join-Path $ProductionRoot '01_PROGRAMAS')) {
    Copy-Item (Join-Path $ProductionRoot '01_PROGRAMAS') (Join-Path $SafetyBackup '01_PROGRAMAS') -Recurse -Force
}
Write-Host "Copia de seguridad de la version actual: $SafetyBackup" -ForegroundColor Cyan

Stop-TmadBots

$currentPrograms = Join-Path $ProductionRoot '01_PROGRAMAS'
Invoke-RobocopyMirror -Source (Join-Path $BackupPath '01_PROGRAMAS') -Destination $currentPrograms

$backupRootFiles = Join-Path $BackupPath 'root_files'
if (Test-Path $backupRootFiles) {
    Get-ChildItem $backupRootFiles -File | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $ProductionRoot $_.Name) -Force
    }
}

$version = if (Test-Path (Join-Path $ProductionRoot 'VERSION.txt')) {
    (Get-Content (Join-Path $ProductionRoot 'VERSION.txt') -Raw).Trim()
} else { $null }
if ($version) {
    $validator = Join-Path $ProductionRoot ("VALIDAR_TMAD_V{0}.bat" -f $version.Replace('.', '_'))
    if (Test-Path $validator) {
        Push-Location $ProductionRoot
        try {
            & cmd.exe /d /c "`"$validator`""
            if ($LASTEXITCODE -ne 0) { throw "La version restaurada no supera su validador; codigo $LASTEXITCODE." }
        }
        finally { Pop-Location }
    }
}

if (-not $NoStart) { Start-TmadBots }

@{
    rolled_back_at = (Get-Date).ToString('o')
    restored_from = $BackupPath
    safety_backup = $SafetyBackup
    restored_version = $version
    started = (-not $NoStart)
} | ConvertTo-Json | Set-Content (Join-Path $DeploymentDir 'last_manual_rollback.json') -Encoding UTF8

Write-Host ''
Write-Host 'ROLLBACK COMPLETADO CORRECTAMENTE.' -ForegroundColor Green
exit 0
