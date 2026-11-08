[CmdletBinding()]
param(
    [string]$RepoPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$GitHubRepo = 'SMS-Server-Bots/tmad-dual-bot',
    [switch]$ArchiveOnly,
    [switch]$MutableOnly,
    [switch]$PurgeVerifiedArchives
)

$ErrorActionPreference = 'Stop'
$DataRoot = Join-Path $ProductionPath '03_DATOS'
$RuntimeRoot = Join-Path $RepoPath 'runtime_observability\tmad_data_backup'
New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
$LogFile = Join-Path $RuntimeRoot 'tmad_data_backup.log'

function Log([string]$Level,[string]$Message) {
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),$Level,$Message
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}
function Resolve-7Zip {
    foreach ($p in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe", '7z.exe','7zz.exe')) {
        if (-not $p) { continue }
        if (Test-Path -LiteralPath $p -PathType Leaf) { return (Resolve-Path -LiteralPath $p).Path }
        $cmd = Get-Command $p -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw 'No se encontró 7-Zip.'
}
function Resolve-Python {
    $venv = Join-Path $ProductionPath 'venvs\data_bot\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv -PathType Leaf) { return @($venv) }
    if (Get-Command py.exe -ErrorAction SilentlyContinue) { return @('py.exe','-3') }
    if (Get-Command python.exe -ErrorAction SilentlyContinue) { return @('python.exe') }
    throw 'No se encontró Python para snapshot SQLite.'
}
function Invoke-Gh([string[]]$Arguments,[switch]$AllowFailure) {
    $old = $ErrorActionPreference; $ErrorActionPreference='Continue'
    try { $out = @(& gh @Arguments 2>&1 | ForEach-Object { $_.ToString() }); $code=$LASTEXITCODE }
    finally { $ErrorActionPreference=$old }
    if ($code -ne 0 -and -not $AllowFailure) { throw "gh $($Arguments -join ' ') falló ($code): $($out -join ' ')" }
    return [pscustomobject]@{ Code=$code; Output=$out }
}
function Ensure-Release([string]$Tag,[string]$Title,[string]$Notes) {
    $view = Invoke-Gh @('release','view',$Tag,'--repo',$GitHubRepo) -AllowFailure
    if ($view.Code -ne 0) {
        [void](Invoke-Gh @('release','create',$Tag,'--repo',$GitHubRepo,'--title',$Title,'--notes',$Notes))
    }
}
function Sanitize-AssetBase([string]$Name) {
    $s = [regex]::Replace($Name,'[^A-Za-z0-9._-]+','_')
    if ($s.Length -gt 120) { $s = $s.Substring($s.Length-120) }
    return $s
}

if (-not (Test-Path -LiteralPath $RepoPath)) { throw "RepoPath inexistente: $RepoPath" }
if (-not (Test-Path -LiteralPath $DataRoot)) { throw "03_DATOS inexistente: $DataRoot" }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'gh CLI no disponible.' }
[void](Invoke-Gh @('auth','status'))
$sevenZip = Resolve-7Zip
$tempRoot = Join-Path $env:TEMP ("tmad_data_backup_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

try {
    if (-not $MutableOnly) {
        $indexRelease = 'tmad-data-index'
        $indexPath = Join-Path $tempRoot 'archive-index.json'
        $downloadDir = Join-Path $tempRoot 'index_download'
        New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
        $existing = Invoke-Gh @('release','view',$indexRelease,'--repo',$GitHubRepo) -AllowFailure
        if ($existing.Code -eq 0) {
            $dl = Invoke-Gh @('release','download',$indexRelease,'--repo',$GitHubRepo,'--pattern','archive-index.json','--dir',$downloadDir,'--clobber') -AllowFailure
            if ($dl.Code -eq 0 -and (Test-Path (Join-Path $downloadDir 'archive-index.json'))) {
                Copy-Item (Join-Path $downloadDir 'archive-index.json') $indexPath -Force
            }
        }
        if (Test-Path $indexPath) {
            $index = Get-Content $indexPath -Raw | ConvertFrom-Json
            $entries = New-Object System.Collections.ArrayList
            foreach ($e in @($index.entries)) { [void]$entries.Add($e) }
        } else { $entries = New-Object System.Collections.ArrayList }

        $byPath = @{}
        foreach ($e in $entries) { $byPath[[string]$e.relative_path] = $e }
        $newVerified = New-Object System.Collections.ArrayList
        $archives = @(Get-ChildItem $DataRoot -Recurse -File -Filter '*.zip' -ErrorAction SilentlyContinue | Sort-Object FullName)
        Log 'INFO' "ZIP históricos detectados: $($archives.Count)"

        foreach ($file in $archives) {
            $relative = $file.FullName.Substring($DataRoot.Length).TrimStart('\') -replace '\\','/'
            $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
            $oldEntry = $byPath[$relative]
            if ($oldEntry -and [string]$oldEntry.sha256 -eq $hash) { continue }
            if ($file.Length -gt 1900MB) {
                Log 'WARN' "ZIP >1.9GB no se purga ni sube automáticamente: $relative ($($file.Length) bytes)"
                continue
            }

            $releaseTag = 'tmad-data-archive-' + $file.LastWriteTime.ToString('yyyy-MM')
            Ensure-Release $releaseTag ("TMAD data archive " + $file.LastWriteTime.ToString('yyyy-MM')) 'Immutable verified TMAD historical archives.'
            $asset = $hash.Substring(0,16) + '__' + (Sanitize-AssetBase $file.Name)
            $assetPath = Join-Path $tempRoot $asset
            Copy-Item -LiteralPath $file.FullName -Destination $assetPath -Force
            [void](Invoke-Gh @('release','upload',$releaseTag,$assetPath,'--repo',$GitHubRepo,'--clobber'))

            $verifyDir = Join-Path $tempRoot ('verify_' + $hash.Substring(0,12))
            New-Item -ItemType Directory -Force -Path $verifyDir | Out-Null
            [void](Invoke-Gh @('release','download',$releaseTag,'--repo',$GitHubRepo,'--pattern',$asset,'--dir',$verifyDir,'--clobber'))
            $remoteFile = Join-Path $verifyDir $asset
            $remoteHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $remoteFile).Hash.ToLowerInvariant()
            if ($remoteHash -ne $hash) { throw "SHA remoto distinto para $relative" }

            $entry = [pscustomobject]@{
                relative_path=$relative; sha256=$hash; bytes=$file.Length; release_tag=$releaseTag;
                asset=$asset; uploaded_at=(Get-Date).ToString('o')
            }
            if ($oldEntry) { [void]$entries.Remove($oldEntry) }
            [void]$entries.Add($entry); $byPath[$relative]=$entry; [void]$newVerified.Add($file.FullName)
            Log 'OK' "Archive verificado en GitHub: $relative"
        }

        $indexOut = [ordered]@{ schema_version=1; generated_at=(Get-Date).ToString('o'); data_root='03_DATOS'; entries=@($entries | Sort-Object relative_path) }
        $indexOut | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $indexPath -Encoding UTF8
        Ensure-Release $indexRelease 'TMAD data archive index' 'Machine-readable index for immutable TMAD data assets.'
        [void](Invoke-Gh @('release','upload',$indexRelease,$indexPath,'--repo',$GitHubRepo,'--clobber'))

        if ($PurgeVerifiedArchives) {
            foreach ($path in $newVerified) {
                Remove-Item -LiteralPath $path -Force
                Log 'OK' "ZIP local purgado tras verificación remota: $path"
            }
        }
    }

    if (-not $ArchiveOnly) {
        $snapshotRoot = Join-Path $tempRoot 'mutable\03_DATOS'
        New-Item -ItemType Directory -Force -Path $snapshotRoot | Out-Null
        $python = Resolve-Python
        $helper = Join-Path $RepoPath 'TMAD_DATA_SNAPSHOT.py'
        if (-not (Test-Path $helper -PathType Leaf)) { throw "Falta $helper" }
        if ($python.Count -eq 2) { & $python[0] $python[1] $helper $DataRoot $snapshotRoot }
        else { & $python[0] $helper $DataRoot $snapshotRoot }
        if ($LASTEXITCODE -ne 0) { throw "TMAD_DATA_SNAPSHOT.py terminó con código $LASTEXITCODE" }

        $archiveBase = Join-Path $tempRoot 'tmad-mutable-latest.7z'
        Push-Location (Join-Path $tempRoot 'mutable')
        try {
            & $sevenZip a -t7z -mx=7 -v1800m $archiveBase '.\03_DATOS\*' | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "7-Zip mutable snapshot terminó con código $LASTEXITCODE" }
        } finally { Pop-Location }
        $parts = @(Get-ChildItem $tempRoot -File -Filter 'tmad-mutable-latest.7z.*' | Sort-Object Name)
        if (-not $parts) { throw '7-Zip no generó volúmenes mutable.' }

        $metadataPath = Join-Path $tempRoot 'tmad-mutable-latest.metadata.json'
        [ordered]@{
            schema_version=1; generated_at=(Get-Date).ToString('o'); source='03_DATOS excluding immutable ZIP/log/temp';
            parts=@($parts | ForEach-Object { [ordered]@{name=$_.Name; bytes=$_.Length; sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()} })
        } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $metadataPath -Encoding UTF8

        $latestTag='tmad-data-latest'
        Ensure-Release $latestTag 'TMAD mutable data latest' 'Latest consistent mutable TMAD databases/state. Immutable ZIP history is stored separately.'
        $assetsRaw = Invoke-Gh @('release','view',$latestTag,'--repo',$GitHubRepo,'--json','assets')
        try {
            $assetsObj = (($assetsRaw.Output -join "`n") | ConvertFrom-Json).assets
            foreach ($a in @($assetsObj)) {
                $name=[string]$a.name
                if ($name -like 'tmad-mutable-latest.7z.*' -or $name -eq 'tmad-mutable-latest.metadata.json') {
                    [void](Invoke-Gh @('release','delete-asset',$latestTag,$name,'--repo',$GitHubRepo,'--yes') -AllowFailure)
                }
            }
        } catch {}
        foreach ($part in $parts) { [void](Invoke-Gh @('release','upload',$latestTag,$part.FullName,'--repo',$GitHubRepo)) }
        [void](Invoke-Gh @('release','upload',$latestTag,$metadataPath,'--repo',$GitHubRepo))

        $verifyLatest = Join-Path $tempRoot 'verify_latest'; New-Item -ItemType Directory -Force -Path $verifyLatest | Out-Null
        [void](Invoke-Gh @('release','download',$latestTag,'--repo',$GitHubRepo,'--pattern','tmad-mutable-latest.7z.*','--dir',$verifyLatest,'--clobber'))
        $firstPart = Get-ChildItem $verifyLatest -File -Filter 'tmad-mutable-latest.7z.*' | Sort-Object Name | Select-Object -First 1
        if (-not $firstPart) { throw 'No se descargó ningún volumen mutable para verificar.' }
        & $sevenZip t $firstPart.FullName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Verificación remota del snapshot mutable falló.' }
        Log 'OK' "Snapshot mutable GitHub verificado: $($parts.Count) volumen(es)"
    }

    Log 'OK' 'Backup TMAD data finalizado.'
} catch {
    Log 'ERROR' $_.Exception.Message
    throw
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
