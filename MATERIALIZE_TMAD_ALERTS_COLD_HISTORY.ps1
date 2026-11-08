[CmdletBinding()]
param(
    [string]$ProductionPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$GitHubRepo = 'SMS-Server-Bots/tmad-dual-bot',
    [int]$WarmDays = 45,
    [int]$MaxFiles = 120
)

$ErrorActionPreference='Stop'
$DataRoot=Join-Path $ProductionPath '03_DATOS'
$RuntimeRoot=Join-Path $ProductionPath '04_RUNTIME'
$CacheRoot=Join-Path $RuntimeRoot 'history_cache\alerts_layers'
New-Item -ItemType Directory -Force -Path $CacheRoot | Out-Null

$temp=Join-Path $env:TEMP ('tmad_alerts_cold_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $temp | Out-Null
try {
    & gh release download tmad-data-index --repo $GitHubRepo --pattern archive-index.json --dir $temp --clobber 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo descargar archive-index.json.' }
    $idx=Get-Content (Join-Path $temp 'archive-index.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $cutoff=(Get-Date).Date.AddDays(-1 * [math]::Max(7,$WarmDays))

    $entries=@($idx.entries | Where-Object {
        ([string]$_.relative_path) -like 'alerts_bot/db/archive/layers/*/layers_*.zip'
    } | Sort-Object relative_path)
    if ($MaxFiles -gt 0 -and $entries.Count -gt $MaxFiles) {
        $entries=@($entries | Select-Object -Last $MaxFiles)
    }

    $downloaded=0; $reused=0
    foreach($entry in $entries) {
        $relative=([string]$entry.relative_path) -replace '/','\'
        $name=[IO.Path]::GetFileName($relative)
        if ($name -notmatch '^layers_(\d{4}-\d{2}-\d{2})\.zip$') { continue }
        $tradeDate=[datetime]::ParseExact($Matches[1],'yyyy-MM-dd',$null)
        if ($tradeDate -ge $cutoff) { continue }

        $local=Join-Path $DataRoot $relative
        if (Test-Path $local -PathType Leaf) { $reused++; continue }

        $suffix=$relative.Substring(('alerts_bot\db\archive\layers\').Length)
        $target=Join-Path $CacheRoot $suffix
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        $expectedHash=([string]$entry.sha256).ToLowerInvariant()
        $expectedBytes=[long]$entry.bytes

        if (Test-Path $target -PathType Leaf) {
            $hash=(Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
            if ((Get-Item $target).Length -eq $expectedBytes -and $hash -eq $expectedHash) {
                (Get-Item $target).LastWriteTime=Get-Date
                $reused++; continue
            }
            Remove-Item -LiteralPath $target -Force
        }

        $one=Join-Path $temp ([guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $one | Out-Null
        & gh release download ([string]$entry.release_tag) --repo $GitHubRepo --pattern ([string]$entry.asset) --dir $one --clobber 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Descarga cold fallida: $($entry.relative_path)" }
        $remote=Join-Path $one ([string]$entry.asset)
        if ((Get-Item $remote).Length -ne $expectedBytes) { throw "Size cold distinto: $($entry.relative_path)" }
        $remoteHash=(Get-FileHash -Algorithm SHA256 -LiteralPath $remote).Hash.ToLowerInvariant()
        if ($remoteHash -ne $expectedHash) { throw "SHA cold distinto: $($entry.relative_path)" }

        $tmpTarget=$target + '.tmp'
        Copy-Item -LiteralPath $remote -Destination $tmpTarget -Force
        Move-Item -LiteralPath $tmpTarget -Destination $target -Force
        $downloaded++
    }

    Write-Host ("[OK] Alerts cold history: downloaded={0} reused={1} cache={2}" -f $downloaded,$reused,$CacheRoot) -ForegroundColor Green
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
