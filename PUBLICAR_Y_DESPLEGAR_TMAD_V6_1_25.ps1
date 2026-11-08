[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick'
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Tag = 'v6.1.25'
$Version = '6.1.25'
$PreviousVersion = '6.1.24'
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$ProductionRoot = [System.IO.Path]::GetFullPath($ProductionRoot)
$StablePublisher = Join-Path $RepoRoot 'PUBLICAR_Y_DESPLEGAR_TMAD.ps1'
function Write-Step { param([string]$Message,[string]$Level='INFO'); $Color=switch($Level){'OK'{'Green'}'WARN'{'Yellow'}'ERROR'{'Red'}default{'Cyan'}}; Write-Host ('[{0}] {1}' -f $Level,$Message) -ForegroundColor $Color }
Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host ' TMAD v6.1.25 - PUBLICAR Y DESPLEGAR CON FLUJO ESTABLE' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
try {
    if (-not (Test-Path $RepoRoot -PathType Container)) { throw ('No existe el repositorio: {0}' -f $RepoRoot) }
    if (-not (Test-Path (Join-Path $RepoRoot '.git') -PathType Container)) { throw ('La carpeta no contiene .git: {0}' -f $RepoRoot) }
    if (-not (Test-Path $ProductionRoot -PathType Container)) { throw ('No existe produccion: {0}' -f $ProductionRoot) }
    if (-not (Test-Path $StablePublisher -PathType Leaf)) { throw ('Falta el publicador estable: {0}' -f $StablePublisher) }
    $VersionFile=Join-Path $RepoRoot 'VERSION.txt'
    $DeclaredVersion=(Get-Content -LiteralPath $VersionFile -Raw).Trim()
    if ($DeclaredVersion -ne $Version) { throw ('VERSION.txt contiene {0}; se esperaba {1}.' -f $DeclaredVersion,$Version) }
    $ProductionVersionFile=Join-Path $ProductionRoot 'VERSION.txt'
    $ProductionVersion=(Get-Content -LiteralPath $ProductionVersionFile -Raw).Trim()
    if ($ProductionVersion -notin @($PreviousVersion,$Version)) { throw ('Produccion contiene una version inesperada: {0}' -f $ProductionVersion) }
    $PowerShellExe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path $PowerShellExe -PathType Leaf)) { throw ('No existe Windows PowerShell: {0}' -f $PowerShellExe) }
    Write-Step 'Ejecutando el mismo publicador/desplegador estable usado en v6.1.23/v6.1.24...'
    & $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $StablePublisher -Tag $Tag -RepoRoot $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw ('El publicador estable termino con codigo {0}.' -f $LASTEXITCODE) }
    $FinalVersion=(Get-Content -LiteralPath $ProductionVersionFile -Raw).Trim()
    if ($FinalVersion -ne $Version) { throw ('Produccion no quedo en {0}; VERSION.txt={1}' -f $Version,$FinalVersion) }
    Write-Step ('TMAD {0} publicada y desplegada correctamente.' -f $Tag) 'OK'
    exit 0
}
catch {
    Write-Step $_.Exception.Message 'ERROR'
    Write-Host 'El desplegador estable conserva su backup y rollback automatico.' -ForegroundColor Yellow
    exit 41
}
