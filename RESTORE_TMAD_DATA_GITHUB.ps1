[CmdletBinding()]
param(
    [string]$ProductionPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$GitHubRepo = 'SMS-Server-Bots/tmad-dual-bot',
    [switch]$MutableOnly,
    [switch]$ArchivesOnly,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$DataRoot = Join-Path $ProductionPath '03_DATOS'
$LockRoot = Join-Path $ProductionPath '04_RUNTIME\locks'

function Resolve-7Zip {
    foreach ($p in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe", '7z.exe','7zz.exe')) {
        if (-not $p) { continue }
        if (Test-Path -LiteralPath $p -PathType Leaf) { return (Resolve-Path -LiteralPath $p).Path }
        $cmd = Get-Command $p -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw 'No se encontró 7-Zip.'
}
function Invoke-Gh([string[]]$Arguments) {
    $old=$ErrorActionPreference; $ErrorActionPreference='Continue'
    try { $out=@(& gh @Arguments 2>&1 | ForEach-Object {$_.ToString()}); $code=$LASTEXITCODE }
    finally { $ErrorActionPreference=$old }
    if ($code -ne 0) { throw "gh $($Arguments -join ' ') falló ($code): $($out -join ' ')" }
    return @($out)
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh CLI no disponible.' }
[void](Invoke-Gh @('auth','status'))
$sevenZip=Resolve-7Zip

if (-not $Force -and (Test-Path $LockRoot)) {
    $activeLocks=@(Get-ChildItem $LockRoot -File -Filter '*.lock' -ErrorAction SilentlyContinue)
    if ($activeLocks.Count -gt 0) { throw 'TMAD parece activo (hay locks). Restaura con los bots detenidos; usa -Force solo si sabes que los locks son stale.' }
}
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
$tempRoot=Join-Path $env:TEMP ("tmad_data_restore_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

try {
    if (-not $ArchivesOnly) {
        $mutableDir=Join-Path $tempRoot 'mutable'
        New-Item -ItemType Directory -Force -Path $mutableDir | Out-Null
        [void](Invoke-Gh @('release','download','tmad-data-latest','--repo',$GitHubRepo,'--pattern','tmad-mutable-latest.7z.*','--dir',$mutableDir,'--clobber'))
        $first=Get-ChildItem $mutableDir -File -Filter 'tmad-mutable-latest.7z.*' | Sort-Object Name | Select-Object -First 1
        if (-not $first) { throw 'No existe snapshot mutable en tmad-data-latest.' }
        $extract=Join-Path $tempRoot 'mutable_extract'
        New-Item -ItemType Directory -Force -Path $extract | Out-Null
        & $sevenZip x $first.FullName ("-o{0}" -f $extract) '-aoa' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo extraer snapshot mutable.' }
        $restored=Join-Path $extract '03_DATOS'
        if (-not (Test-Path $restored -PathType Container)) { throw 'Snapshot mutable no contiene 03_DATOS.' }
        & robocopy.exe $restored $DataRoot /E /COPY:DAT /DCOPY:DAT /R:3 /W:2 /XJ /NP | Out-Null
        if ($LASTEXITCODE -gt 7) { throw "Robocopy mutable falló: $LASTEXITCODE" }
        Write-Host '[OK] Capa mutable 03_DATOS restaurada.' -ForegroundColor Green
    }

    if (-not $MutableOnly) {
        $indexDir=Join-Path $tempRoot 'index'; New-Item -ItemType Directory -Force -Path $indexDir | Out-Null
        [void](Invoke-Gh @('release','download','tmad-data-index','--repo',$GitHubRepo,'--pattern','archive-index.json','--dir',$indexDir,'--clobber'))
        $indexPath=Join-Path $indexDir 'archive-index.json'
        if (-not (Test-Path $indexPath)) { throw 'No existe archive-index.json.' }
        $index=Get-Content $indexPath -Raw | ConvertFrom-Json
        $restoredCount=0; $skippedCount=0
        foreach ($entry in @($index.entries)) {
            $relative=([string]$entry.relative_path) -replace '/','\'
            $target=Join-Path $DataRoot $relative
            $expected=[string]$entry.sha256
            if (Test-Path $target -PathType Leaf) {
                $existing=(Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
                if ($existing -eq $expected) { $skippedCount++; continue }
            }
            $assetDir=Join-Path $tempRoot ('asset_' + $expected.Substring(0,12))
            New-Item -ItemType Directory -Force -Path $assetDir | Out-Null
            [void](Invoke-Gh @('release','download',[string]$entry.release_tag,'--repo',$GitHubRepo,'--pattern',[string]$entry.asset,'--dir',$assetDir,'--clobber'))
            $asset=Join-Path $assetDir ([string]$entry.asset)
            $actual=(Get-FileHash -Algorithm SHA256 -LiteralPath $asset).Hash.ToLowerInvariant()
            if ($actual -ne $expected) { throw "SHA inválido descargando $($entry.relative_path)" }
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Move-Item -LiteralPath $asset -Destination $target -Force
            $restoredCount++
        }
        Write-Host "[OK] ZIP históricos restaurados=$restoredCount · ya presentes=$skippedCount" -ForegroundColor Green
    }
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
