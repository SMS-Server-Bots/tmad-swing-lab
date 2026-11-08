[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick',
    [string]$GitHubRepo = 'SMS-Server-Bots/tmad-dual-bot',
    [int]$WarmDays = 45,
    [int]$CacheHours = 24
)

$ErrorActionPreference = 'Stop'
$DataRoot = Join-Path $ProductionRoot '03_DATOS'
$ArchiveRoot = Join-Path $DataRoot 'data_bot\db\archive'
$AlertsLayerArchiveRoot = Join-Path $DataRoot 'alerts_bot\db\archive\layers'
$ArchiveRoots = @($ArchiveRoot,$AlertsLayerArchiveRoot)
$OutputRoot = Join-Path $DataRoot 'data_bot\output'
$RuntimeRoot = Join-Path $ProductionRoot '04_RUNTIME'
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$statusPath = Join-Path $OutputRoot 'history_storage_status.json'

$freed = [long]0
$removed = New-Object System.Collections.ArrayList
$warnings = New-Object System.Collections.ArrayList

$alertsDb = Join-Path $DataRoot 'alerts_bot\db\live\tmad_alerts_adaptive.db'
$alertsPython = Join-Path $RuntimeRoot 'venvs\alerts_bot\Scripts\python.exe'

# Fail closed before any cache/archive mutation while historical research is active.
if ((Test-Path $alertsDb -PathType Leaf) -and (Test-Path $alertsPython -PathType Leaf)) {
    $env:TMAD_HISTORY_ALERTS_DB = $alertsDb
    $statePy = "import os,sqlite3,json; p=os.environ['TMAD_HISTORY_ALERTS_DB']; c=sqlite3.connect('file:'+p+'?mode=ro',uri=True); keys=('historical_replay_status','fusion_gate_calibration_status'); rows=dict(c.execute('SELECT state_key,state_value FROM runtime_state WHERE state_key IN (?,?)',keys).fetchall()); print(json.dumps(rows)); c.close()"
    $stateRaw = & $alertsPython -c $statePy 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $stateRaw) {
        $payload = [ordered]@{schema_version=1; generated_at=(Get-Date).ToString('o'); status='SKIPPED_RESEARCH_STATE_UNAVAILABLE'; warm_days=$WarmDays; cache_hours=$CacheHours; freed_bytes=0; freed_gb=0; removed=@(); warnings=@('runtime_state unavailable')}
        $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
        Write-Host '[SKIP] TMAD history storage manager: no se pudo verificar research_state.' -ForegroundColor Yellow
        exit 0
    }
    $researchState = $stateRaw | ConvertFrom-Json
    $replayRunning = ([string]$researchState.historical_replay_status).ToLowerInvariant() -eq 'running'
    $fusionRunning = ([string]$researchState.fusion_gate_calibration_status).ToLowerInvariant() -eq 'running'
    if ($replayRunning -or $fusionRunning) {
        $payload = [ordered]@{schema_version=1; generated_at=(Get-Date).ToString('o'); status='SKIPPED_RESEARCH_ACTIVE'; warm_days=$WarmDays; cache_hours=$CacheHours; freed_bytes=0; freed_gb=0; removed=@(); warnings=@(); replay_running=$replayRunning; fusion_running=$fusionRunning}
        $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
        Write-Host '[SKIP] TMAD history storage manager: research activo.' -ForegroundColor Yellow
        exit 0
    }
}

# La copia _2 es control/update: nunca debe acumular datos runtime.
$repoData = Join-Path $RepoRoot '03_DATOS'
if (Test-Path $repoData -PathType Container) {
    try {
        $size = (Get-ChildItem $repoData -File -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        Remove-Item -LiteralPath $repoData -Recurse -Force
        $freed += [long]$size
        [void]$removed.Add([ordered]@{type='repo_runtime_copy'; path=$repoData; bytes=[long]$size})
    } catch {
        [void]$warnings.Add("No se pudo limpiar _2\03_DATOS: $($_.Exception.Message)")
    }
}

# Cache cold bajo demanda: temporal y acotada.
$cache = Join-Path $RuntimeRoot 'history_cache'
if (Test-Path $cache -PathType Container) {
    Get-ChildItem $cache -File -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-1 * $CacheHours) } |
        ForEach-Object {
            try {
                $n = [long]$_.Length
                Remove-Item -LiteralPath $_.FullName -Force
                $freed += $n
                [void]$removed.Add([ordered]@{type='history_cache'; path=$_.FullName; bytes=$n})
            } catch {}
        }
}

# Purga de ZIPs: solo si 1) son antiguos, 2) Alerts ya los importo,
# y 3) el hash local coincide con el indice remoto previamente verificado.
$index = $null
$temp = Join-Path $env:TEMP ('tmad_history_index_' + [guid]::NewGuid().ToString('N'))
try {
    if ((Get-Command gh -ErrorAction SilentlyContinue) -and (@($ArchiveRoots | Where-Object { Test-Path $_ -PathType Container }).Count -gt 0)) {
        New-Item -ItemType Directory -Force -Path $temp | Out-Null
        & gh release download tmad-data-index --repo $GitHubRepo --pattern archive-index.json --dir $temp --clobber 2>$null | Out-Null
        $idxPath = Join-Path $temp 'archive-index.json'
        if (Test-Path $idxPath -PathType Leaf) {
            $index = Get-Content $idxPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }

        $imported = @{}
        if ($index -and (Test-Path $alertsDb) -and (Test-Path $alertsPython)) {
            $env:TMAD_HISTORY_ALERTS_DB = $alertsDb
            $py = "import os,sqlite3,json; p=os.environ['TMAD_HISTORY_ALERTS_DB']; c=sqlite3.connect(p); imports=[r[0] for r in c.execute('SELECT archive_name FROM archive_imports')]; layers=[{'name':'layers_'+r[0]+'.zip','sha256':r[1]} for r in c.execute('SELECT trade_date,sha256 FROM layer_archive_catalog WHERE verified=1')]; print(json.dumps({'imports':imports,'layers':layers})); c.close()"
            $raw = & $alertsPython -c $py 2>$null
            $layerCatalog = @{}
            if ($LASTEXITCODE -eq 0 -and $raw) {
                $meta = $raw | ConvertFrom-Json
                foreach ($name in @($meta.imports)) { $imported[[string]$name] = $true }
                foreach ($row in @($meta.layers)) {
                    $name = [string]$row.name
                    $imported[$name] = $true
                    $layerCatalog[$name] = ([string]$row.sha256).ToLowerInvariant()
                }
            }
        }

        if ($index -and $imported.Count -gt 0) {
            $byPath = @{}
            foreach ($e in @($index.entries)) { $byPath[[string]$e.relative_path] = $e }
            $cutoff = (Get-Date).AddDays(-1 * [math]::Max(7,$WarmDays))

            $archiveFiles = @()
            foreach ($root in $ArchiveRoots) {
                if (Test-Path $root -PathType Container) {
                    $archiveFiles += @(Get-ChildItem $root -File -Recurse -Filter '*.zip' -ErrorAction SilentlyContinue)
                }
            }
            $archiveFiles |
                Where-Object { $_.LastWriteTime -lt $cutoff } |
                ForEach-Object {
                    $file = $_
                    $relative = $file.FullName.Substring($DataRoot.Length).TrimStart('\') -replace '\\','/'
                    $entry = $byPath[$relative]
                    if (-not $entry) { return }

                    $importKey = $file.Name
                    if ($file.FullName -match '\\intraday\\') { $importKey = 'intraday/' + $file.Name }
                    if (-not $imported.ContainsKey($importKey)) { return }

                    try {
                        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
                        if ($hash -ne ([string]$entry.sha256).ToLowerInvariant()) { return }

                        $isAlertsLayer = $file.FullName.StartsWith($AlertsLayerArchiveRoot,[System.StringComparison]::OrdinalIgnoreCase)
                        if ($isAlertsLayer) {
                            if (-not $layerCatalog.ContainsKey($importKey)) { return }
                            if ($hash -ne $layerCatalog[$importKey]) { return }

                            Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
                            $zip = [System.IO.Compression.ZipFile]::OpenRead($file.FullName)
                            try {
                                $manifestEntry = $zip.GetEntry('manifest.json')
                                if (-not $manifestEntry) { return }
                                $reader = New-Object System.IO.StreamReader($manifestEntry.Open())
                                try { $manifest = ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
                                if (-not [bool]$manifest.source_counts.complete) { return }
                            } finally { $zip.Dispose() }
                        }

                        $n = [long]$file.Length
                        Remove-Item -LiteralPath $file.FullName -Force
                        $freed += $n
                        [void]$removed.Add([ordered]@{type='verified_archive'; path=$relative; bytes=$n; import_key=$importKey})
                    } catch {
                        [void]$warnings.Add("No se pudo purgar ${relative}: $($_.Exception.Message)")
                    }
                }
        }
    }
} catch {
    [void]$warnings.Add("History manager remoto: $($_.Exception.Message)")
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}

$payload = [ordered]@{
    schema_version = 1
    generated_at = (Get-Date).ToString('o')
    warm_days = $WarmDays
    cache_hours = $CacheHours
    freed_bytes = $freed
    freed_gb = [math]::Round($freed / 1GB, 3)
    removed = @($removed)
    warnings = @($warnings)
}
$payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
Write-Host ("[OK] TMAD history storage manager: liberados {0:N3} GB; elementos={1}; warnings={2}" -f ($freed/1GB),$removed.Count,$warnings.Count) -ForegroundColor Green
exit 0
