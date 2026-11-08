[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v\d+\.\d+\.\d+$')]
    [string]$Tag,

    [string]$RepoRoot = '',
    [string]$CommitMessage = '',
    [switch]$NoDeploy
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# En Windows PowerShell 5.1, $PSScriptRoot no debe usarse como valor
# predeterminado dentro de param(), porque puede evaluarse como cadena vacía.
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        $RepoRoot = $PSScriptRoot
    }
    elseif ($MyInvocation.MyCommand.Path) {
        $RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    else {
        throw 'No se pudo determinar la carpeta del repositorio TMAD.'
    }
}
$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)

$Version = $Tag.TrimStart('v')
$VersionUnderscore = $Version.Replace('.', '_')
$ValidatorName = "VALIDAR_TMAD_V$VersionUnderscore.bat"
$PatchManifestName = "PATCH_MANIFEST_V$VersionUnderscore.txt"
$ValidatorPath = Join-Path $RepoRoot $ValidatorName
$PatchManifestPath = Join-Path $RepoRoot $PatchManifestName
$DeployScript = Join-Path $RepoRoot 'DESPLEGAR_RELEASE_TMAD.ps1'
if (-not $CommitMessage) { $CommitMessage = "Release TMAD $Tag" }

function Write-Step {
    param([string]$Message, [string]$Level = 'INFO')
    $color = switch ($Level) {
        'OK' { 'Green' }
        'WARN' { 'Yellow' }
        'ERROR' { 'Red' }
        default { 'Cyan' }
    }
    Write-Host ("[{0}] {1}" -f $Level, $Message) -ForegroundColor $color
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 convierte stderr nativo en NativeCommandError.
        # Se captura sin hacerlo terminante y se decide exclusivamente por exit code.
        $ErrorActionPreference = 'Continue'
        $output = & git -C $RepoRoot @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($AllowedExitCodes -notcontains $code) {
        throw "Git fallo ($code): git -C `"$RepoRoot`" $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return @($output)
}

function Invoke-Gh {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int[]]$AllowedExitCodes = @(0)
    )
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & gh @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($AllowedExitCodes -notcontains $code) {
        throw "GitHub CLI fallo ($code): gh $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return @($output)
}

function Get-ExactTagCommit {
    param([string]$Name)
    $known = @(Invoke-Git -Arguments @('tag', '--list', $Name))
    if ($known.Count -eq 0 -or -not ([string]$known[0]).Trim()) { return '' }
    $result = @(Invoke-Git -Arguments @('rev-list', '-n', '1', $Name))
    if ($result.Count -eq 0) { return '' }
    return ([string]$result[0]).Trim()
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host " TMAD · PUBLICAR Y DESPLEGAR $Tag" -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $RepoRoot '.git') -PathType Container)) {
    throw "La carpeta no es el repositorio Git de TMAD: $RepoRoot"
}
if (-not (Test-Path $ValidatorPath -PathType Leaf)) {
    throw "Falta el validador de la versión: $ValidatorName"
}
if (-not (Test-Path $DeployScript -PathType Leaf) -and -not $NoDeploy) {
    throw 'Falta DESPLEGAR_RELEASE_TMAD.ps1 en el repositorio de trabajo.'
}

$branch = (Invoke-Git -Arguments @('branch', '--show-current') | Select-Object -First 1).Trim()
if ($branch -ne 'main') {
    throw "Debes ejecutar la publicación desde la rama main. Rama actual: $branch"
}

$versionFile = Join-Path $RepoRoot 'VERSION.txt'
if (-not (Test-Path $versionFile -PathType Leaf)) { throw 'Falta VERSION.txt.' }
$declaredVersion = (Get-Content $versionFile -Raw).Trim()
if ($declaredVersion -ne $Version) {
    throw "VERSION.txt contiene '$declaredVersion' pero el tag solicitado es '$Tag'."
}

Write-Step 'Comprobando Git, GitHub CLI y sincronización remota...'
& git --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Git no está disponible.' }
& gh --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI no está disponible.' }
& cmd.exe /d /c "gh auth status >nul 2>nul"
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI no está autenticado.' }

Invoke-Git -Arguments @('fetch', 'origin', 'main', '--tags') | Out-Null
$aheadBehind = (Invoke-Git -Arguments @('rev-list', '--left-right', '--count', 'HEAD...origin/main') | Select-Object -First 1).Trim() -split '\s+'
$behind = if ($aheadBehind.Count -ge 2) { [int]$aheadBehind[1] } else { 0 }
if ($behind -gt 0) {
    throw "El repositorio local está $behind commit(s) por detrás de origin/main. Integra primero los cambios remotos."
}

Write-Step "Ejecutando $ValidatorName..."
Push-Location $RepoRoot
try {
    & cmd.exe /d /c "`"$ValidatorPath`""
    if ($LASTEXITCODE -ne 0) { throw "La validación de $Tag ha fallado." }
}
finally {
    Pop-Location
}
Write-Step 'Validación completada.' 'OK'

Write-Step 'Preparando únicamente los archivos del patch...'
if (Test-Path $PatchManifestPath -PathType Leaf) {
    $manifestLines = @(
        Get-Content $PatchManifestPath |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith('#') }
    )
    $patchFiles = @($manifestLines | Where-Object { -not $_.StartsWith('DELETE:') })
    $deleteFiles = @(
        $manifestLines |
        Where-Object { $_.StartsWith('DELETE:') } |
        ForEach-Object { $_.Substring(7).Trim() }
    )
    $allManifestPaths = @($patchFiles + $deleteFiles)
    foreach ($relativePath in $allManifestPaths) {
        $normalized = $relativePath.Replace('\', '/')
        if (
            -not $normalized -or
            $normalized.StartsWith('/') -or
            $normalized -match '^[A-Za-z]:' -or
            $normalized -match '(^|/)\.\.($|/)'
        ) {
            throw "Ruta no válida en el manifest del patch: $relativePath"
        }
    }
    foreach ($relativePath in $patchFiles) {
        $localPath = Join-Path $RepoRoot ($relativePath.Replace('/', '\'))
        if (-not (Test-Path $localPath -PathType Leaf)) {
            throw "El manifest del patch referencia un archivo inexistente: $relativePath"
        }
        Invoke-Git -Arguments @('add', '-f', '--', $relativePath) | Out-Null
    }
    foreach ($relativePath in $deleteFiles) {
        $localPath = Join-Path $RepoRoot ($relativePath.Replace('/', '\'))
        if (Test-Path $localPath -PathType Leaf) {
            Remove-Item -LiteralPath $localPath -Force
        }
        Invoke-Git -Arguments @('rm', '-f', '--ignore-unmatch', '--', $relativePath) | Out-Null
    }
    Invoke-Git -Arguments @('add', '-f', '--', $PatchManifestName) | Out-Null

    $allowed = @($allManifestPaths + $PatchManifestName | ForEach-Object { $_.Replace('\', '/') })
    $unstagedTracked = @(Invoke-Git -Arguments @('diff', '--name-only'))
    $untracked = @(Invoke-Git -Arguments @('ls-files', '--others', '--exclude-standard'))
    $unexpected = @(
        @($unstagedTracked + $untracked) |
        ForEach-Object { ([string]$_).Trim().Replace('\', '/') } |
        Where-Object { $_ -and $allowed -notcontains $_ } |
        Sort-Object -Unique
    )
    if ($unexpected.Count -gt 0) {
        throw "Hay cambios ajenos al patch sin preparar. Revisa antes de publicar:`n$($unexpected -join [Environment]::NewLine)"
    }
}
else {
    Write-Step "No existe $PatchManifestName; se usarán todos los cambios no ignorados." 'WARN'
    Invoke-Git -Arguments @('add', '--all') | Out-Null
}

$staged = @(Invoke-Git -Arguments @('diff', '--cached', '--name-only', '--diff-filter=ACMRD'))
$forbidden = @(
    $staged | Where-Object {
        $_ -match '(^|/)(\.env($|\.)|02_CONFIG($|/)|03_DATOS($|/)|04_RUNTIME($|/))' -or
        $_ -match '(?i)(secret|credential|token|password)' -or
        $_ -match '(?i)\.(db|sqlite|sqlite3|log|wal|shm)$'
    }
)
if ($forbidden.Count -gt 0) {
    throw "Se intentaron preparar rutas prohibidas:`n$($forbidden -join [Environment]::NewLine)"
}

$stagedDiff = (Invoke-Git -Arguments @('diff', '--cached', '--no-ext-diff', '--unified=0')) -join "`n"
$secretPatterns = @(
    'https://(?:discord(?:app)?\.com)/api/webhooks/\d+/[A-Za-z0-9_-]{20,}',
    'github_pat_[A-Za-z0-9_]{20,}',
    'ghp_[A-Za-z0-9]{20,}',
    'sk-[A-Za-z0-9_-]{20,}'
)
foreach ($pattern in $secretPatterns) {
    if ($stagedDiff -match $pattern) {
        throw 'Se ha detectado un posible secreto real en los cambios preparados. Publicación cancelada.'
    }
}

if ($staged.Count -gt 0) {
    Write-Step ("Archivos preparados: {0}" -f $staged.Count)
    Invoke-Git -Arguments @('commit', '-m', $CommitMessage) | ForEach-Object { Write-Host $_ }
    Write-Step 'Commit creado.' 'OK'
}
else {
    Write-Step 'No hay cambios nuevos que commitear; se continúa de forma idempotente.' 'WARN'
}

Invoke-Git -Arguments @('push', 'origin', 'main') | ForEach-Object { Write-Host $_ }
Write-Step 'Rama main subida a GitHub.' 'OK'

$head = (Invoke-Git -Arguments @('rev-parse', 'HEAD') | Select-Object -First 1).Trim()
$tagCommit = Get-ExactTagCommit -Name $Tag
if ($tagCommit) {
    if ($tagCommit -ne $head) {
        throw "El tag $Tag ya existe pero apunta a otro commit. No se modifican releases publicadas."
    }
    Write-Step "El tag $Tag ya existe y coincide con HEAD." 'WARN'
}
else {
    Invoke-Git -Arguments @('tag', '-a', $Tag, '-m', "TMAD $Tag") | Out-Null
    Write-Step "Tag local $Tag creado." 'OK'
}

$remoteTag = @(Invoke-Git -Arguments @('ls-remote', '--tags', 'origin', "refs/tags/$Tag") -AllowedExitCodes @(0))
if ($remoteTag.Count -eq 0) {
    Invoke-Git -Arguments @('push', 'origin', $Tag) | ForEach-Object { Write-Host $_ }
    Write-Step "Tag $Tag subido." 'OK'
}
else {
    Write-Step "El tag $Tag ya está en GitHub." 'WARN'
}

Push-Location $RepoRoot
try {
    & cmd.exe /d /c "gh release view $Tag >nul 2>nul"
    $releaseExists = ($LASTEXITCODE -eq 0)
    if (-not $releaseExists) {
        Invoke-Gh -Arguments @('release', 'create', $Tag, '--title', "TMAD $Tag", '--generate-notes', '--verify-tag') |
            ForEach-Object { Write-Host $_ }
        Write-Step "Release $Tag publicada." 'OK'
    }
    else {
        Write-Step "La release $Tag ya existe." 'WARN'
    }
}
finally {
    Pop-Location
}

if (-not $NoDeploy) {
    Write-Step 'Iniciando despliegue seguro en producción...'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $DeployScript -Tag $Tag
    if ($LASTEXITCODE -ne 0) { throw "El despliegue de $Tag ha fallado." }
    Write-Step "TMAD $Tag publicado y desplegado." 'OK'
}
else {
    Write-Step 'Publicación completada sin despliegue por -NoDeploy.' 'OK'
}
