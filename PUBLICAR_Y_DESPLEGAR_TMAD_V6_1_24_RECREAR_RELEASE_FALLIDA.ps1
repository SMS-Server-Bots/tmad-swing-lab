[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Tag = 'v6.1.24'
$Version = '6.1.24'
$PreviousVersion = '6.1.23'
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$ProductionRoot = [System.IO.Path]::GetFullPath($ProductionRoot)
$StablePublisher = Join-Path $RepoRoot 'PUBLICAR_Y_DESPLEGAR_TMAD.ps1'

function Write-Step {
    param([string]$Message, [string]$Level = 'INFO')
    $Color = switch ($Level) {
        'OK' { 'Green' }
        'WARN' { 'Yellow' }
        'ERROR' { 'Red' }
        default { 'Cyan' }
    }
    Write-Host ('[{0}] {1}' -f $Level, $Message) -ForegroundColor $Color
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )
    $PreviousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $Output = & $FilePath @Arguments 2>&1
        $Code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($AllowedExitCodes -notcontains $Code) {
        throw ('Comando fallo ({0}): {1} {2}`n{3}' -f $Code, $FilePath, ($Arguments -join ' '), ($Output -join [Environment]::NewLine))
    }
    return [pscustomobject]@{ ExitCode = $Code; Output = @($Output) }
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' TMAD v6.1.24 - PUBLICAR Y DESPLEGAR CON FLUJO ESTABLE' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan

try {
    if (-not (Test-Path $RepoRoot -PathType Container)) { throw ('No existe el repositorio: {0}' -f $RepoRoot) }
    if (-not (Test-Path (Join-Path $RepoRoot '.git') -PathType Container)) { throw ('La carpeta no contiene .git: {0}' -f $RepoRoot) }
    if (-not (Test-Path $ProductionRoot -PathType Container)) { throw ('No existe produccion: {0}' -f $ProductionRoot) }
    if (-not (Test-Path $StablePublisher -PathType Leaf)) { throw ('Falta el publicador estable: {0}' -f $StablePublisher) }

    $VersionFile = Join-Path $RepoRoot 'VERSION.txt'
    if (-not (Test-Path $VersionFile -PathType Leaf)) { throw 'Falta VERSION.txt en _2.' }
    $DeclaredVersion = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
    if ($DeclaredVersion -ne $Version) {
        throw ('VERSION.txt contiene {0}; se esperaba {1}.' -f $DeclaredVersion, $Version)
    }

    $ProductionVersionFile = Join-Path $ProductionRoot 'VERSION.txt'
    if (-not (Test-Path $ProductionVersionFile -PathType Leaf)) { throw 'Falta VERSION.txt en produccion.' }
    $ProductionVersion = (Get-Content -LiteralPath $ProductionVersionFile -Raw).Trim()
    if ($ProductionVersion -notin @($PreviousVersion, $Version)) {
        throw ('Produccion contiene una version inesperada: {0}' -f $ProductionVersion)
    }

    $GitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $GitCommand) { $GitCommand = Get-Command git -ErrorAction SilentlyContinue }
    if (-not $GitCommand) { throw 'Git no disponible.' }
    $GitExe = $GitCommand.Source

    $GhCommand = Get-Command gh.exe -ErrorAction SilentlyContinue
    if (-not $GhCommand) { $GhCommand = Get-Command gh -ErrorAction SilentlyContinue }
    if (-not $GhCommand) { throw 'GitHub CLI no disponible.' }
    $GhExe = $GhCommand.Source

    [void](Invoke-Native -FilePath $GhExe -Arguments @('auth', 'status'))
    [void](Invoke-Native -FilePath $GitExe -Arguments @('-C', $RepoRoot, 'fetch', 'origin', 'main', '--tags', '--prune'))

    # v6.1.24 ya se publico pero nunca llego a produccion. Se elimina solo esa
    # release/tag fallida para que el publicador estable pueda recrearla sobre
    # el commit correcto generado por este patch.
    if ($ProductionVersion -eq $PreviousVersion) {
        $ReleaseView = Invoke-Native -FilePath $GhExe -Arguments @('release', 'view', $Tag) -AllowedExitCodes @(0, 1)
        if ($ReleaseView.ExitCode -eq 0) {
            Write-Step ('Eliminando release fallida {0}...' -f $Tag) 'WARN'
            [void](Invoke-Native -FilePath $GhExe -Arguments @('release', 'delete', $Tag, '--yes'))
        }

        Write-Step ('Eliminando tag fallido local/remoto {0}...' -f $Tag) 'WARN'
        [void](Invoke-Native -FilePath $GitExe -Arguments @('-C', $RepoRoot, 'push', 'origin', (':refs/tags/{0}' -f $Tag)) -AllowedExitCodes @(0, 1))
        [void](Invoke-Native -FilePath $GitExe -Arguments @('-C', $RepoRoot, 'tag', '-d', $Tag) -AllowedExitCodes @(0, 1))
    }

    Write-Step 'Ejecutando el mismo publicador/desplegador estable usado en v6.1.23...'
    $PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path $PowerShellExe -PathType Leaf)) { throw ('No existe Windows PowerShell: {0}' -f $PowerShellExe) }

    & $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $StablePublisher -Tag $Tag -RepoRoot $RepoRoot
    $PublishCode = $LASTEXITCODE
    if ($PublishCode -ne 0) {
        throw ('El publicador estable termino con codigo {0}.' -f $PublishCode)
    }

    $FinalVersion = (Get-Content -LiteralPath $ProductionVersionFile -Raw).Trim()
    if ($FinalVersion -ne $Version) {
        throw ('Produccion no quedo en {0}; VERSION.txt={1}' -f $Version, $FinalVersion)
    }

    Write-Step ('TMAD {0} publicada y desplegada correctamente.' -f $Tag) 'OK'
    exit 0
}
catch {
    Write-Step $_.Exception.Message 'ERROR'
    Write-Host 'El desplegador estable conserva su backup y rollback automatico.' -ForegroundColor Yellow
    exit 41
}
