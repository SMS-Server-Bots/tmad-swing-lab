[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [string]$VenvPath = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$RepoRoot = (Resolve-Path $RepoRoot).Path
if (-not $VenvPath) {
    $base = [Environment]::GetFolderPath('LocalApplicationData')
    if (-not $base) { $base = $env:TEMP }
    $VenvPath = Join-Path $base 'TMAD\validation_venv'
}

$alertsReq = Join-Path $RepoRoot '01_PROGRAMAS\alerts_bot\requirements.txt'
$dataReq = Join-Path $RepoRoot '01_PROGRAMAS\data_bot\requirements.txt'
foreach ($path in @($alertsReq, $dataReq)) {
    if (-not (Test-Path $path -PathType Leaf)) { throw "Falta requirements de validacion: $path" }
}

$venvPython = Join-Path $VenvPath 'Scripts\python.exe'
$stampPath = Join-Path $VenvPath '.tmad_requirements.sha256'
$requirementsSignature = ((Get-FileHash $alertsReq -Algorithm SHA256).Hash + ':' + (Get-FileHash $dataReq -Algorithm SHA256).Hash).ToLowerInvariant()

function New-ValidationVenv {
    New-Item -ItemType Directory -Path (Split-Path $VenvPath -Parent) -Force | Out-Null
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source -m venv $VenvPath
    } else {
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $py) { throw 'No se encontro python.exe ni py.exe para crear el venv de validacion.' }
        & $py.Source -3 -m venv $VenvPath
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython -PathType Leaf)) {
        throw 'No se pudo crear el venv de validacion TMAD.'
    }
}

function Install-ValidationRequirements {
    Write-Host '[SETUP] Instalando requirements Data + Alerts en venv de validacion...'
    & $venvPython -m pip install --disable-pip-version-check -q -r $alertsReq -r $dataReq
    if ($LASTEXITCODE -ne 0) { throw "pip de validacion termino con codigo $LASTEXITCODE" }
    Set-Content -Path $stampPath -Value $requirementsSignature -Encoding ASCII
}

if (-not (Test-Path $venvPython -PathType Leaf)) {
    Write-Host "[SETUP] Creando venv de validacion: $VenvPath"
    New-ValidationVenv
}

$installedSignature = ''
if (Test-Path $stampPath -PathType Leaf) {
    $installedSignature = (Get-Content $stampPath -Raw -ErrorAction SilentlyContinue).Trim().ToLowerInvariant()
}
if ($installedSignature -ne $requirementsSignature) {
    Install-ValidationRequirements
}

& $venvPython -c "import dotenv, requests, httpx; print('PASS | validation deps | dotenv+requests+httpx')"
if ($LASTEXITCODE -ne 0) {
    Write-Host '[SETUP] Entorno incompleto; reparando requirements...'
    Install-ValidationRequirements
    & $venvPython -c "import dotenv, requests, httpx; print('PASS | validation deps repaired')"
    if ($LASTEXITCODE -ne 0) { throw 'El venv de validacion sigue sin poder importar dotenv/requests/httpx.' }
}

Write-Host "[OK] TMAD validation python: $venvPython"
exit 0
