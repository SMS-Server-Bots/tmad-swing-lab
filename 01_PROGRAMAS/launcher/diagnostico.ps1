$ErrorActionPreference = 'Continue'
$ProgramLauncherDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ProgramLauncherDir '..\..')).Path
$VersionFile = Join-Path $Root 'VERSION.txt'
$DisplayVersion = if (Test-Path $VersionFile) { (Get-Content $VersionFile -Raw).Trim() } else { 'desconocida' }

function Read-EnvFile([string]$Path) {
    $values = @{}
    if (!(Test-Path $Path)) { return $values }
    foreach ($line in Get-Content $Path -Encoding UTF8) {
        $trim = $line.Trim()
        if (!$trim -or $trim.StartsWith('#') -or !$trim.Contains('=')) { continue }
        $parts = $trim.Split('=', 2)
        $values[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $values
}
function Has-AnyValue($maps, [string[]]$Names) {
    foreach ($map in $maps) {
        foreach ($name in $Names) {
            if ($map.ContainsKey($name) -and [string]$map[$name]) { return $true }
        }
    }
    return $false
}
function Show-Pipeline([string]$Bot, [string]$Path) {
    Write-Host ""
    Write-Host ("--- {0} PIPELINE ---" -f $Bot) -ForegroundColor Cyan
    if (!(Test-Path $Path)) {
        Write-Host "MISSING: $Path" -ForegroundColor Yellow
        return
    }
    try {
        $document = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($document.stages) {
            foreach ($property in $document.stages.PSObject.Properties) {
                $stage = $property.Value
                $color = switch ([string]$stage.status) {
                    'OK' {'Green'} 'ERROR' {'Red'} 'WARN' {'Yellow'} 'WAIT' {'DarkYellow'} default {'Gray'}
                }
                Write-Host ("{0} | {1} | {2}" -f $stage.code,$stage.status,$stage.title) -ForegroundColor $color
                if ($stage.reason) { Write-Host ("  Motivo: " + $stage.reason) -ForegroundColor $color }
                if ($stage.action) { Write-Host ("  Accion: " + $stage.action) }
                if ($null -ne $stage.found) { Write-Host ("  Encontrado: " + ($stage.found | ConvertTo-Json -Compress -Depth 8)) }
            }
        } else {
            $stage = if ($document.latest) { $document.latest } else { $document }
            Write-Host ("{0} | {1} | {2}" -f $stage.code,$stage.status,$stage.title)
            if ($stage.reason) { Write-Host ("Motivo: " + $stage.reason) -ForegroundColor Yellow }
            if ($stage.action) { Write-Host ("Accion: " + $stage.action) }
        }
    } catch {
        Write-Host ("ERROR leyendo pipeline: " + $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host '============================================================'
Write-Host (" TMAD v{0} - DIAGNOSTICO RAPIDO" -f $DisplayVersion)
Write-Host '============================================================'
Write-Host "Root: $Root"

$paths = @(
 '01_PROGRAMAS\data_bot\main.py','01_PROGRAMAS\alerts_bot\main.py',
 '02_CONFIG\data_bot.env','02_CONFIG\alerts_bot.env','02_CONFIG\configuracion_canales.env','02_CONFIG\tmad_session.json',
 '03_DATOS\data_bot\db\live','03_DATOS\alerts_bot\db\live','04_RUNTIME\venvs'
)
foreach($rel in $paths){
    $full=Join-Path $Root $rel
    $ok=Test-Path $full
    $color=if($ok){'Green'}else{'Red'}
    Write-Host ((($ok).ToString().PadRight(6)) + ' ' + $rel) -ForegroundColor $color
}

Write-Host ''
Write-Host 'Versiones de programas:' -ForegroundColor Cyan
Select-String -Path (Join-Path $Root '01_PROGRAMAS\data_bot\config.py') -Pattern '^BOT_VERSION' -ErrorAction SilentlyContinue
Select-String -Path (Join-Path $Root '01_PROGRAMAS\alerts_bot\config.py') -Pattern '^BOT_VERSION' -ErrorAction SilentlyContinue

$channels = Read-EnvFile (Join-Path $Root '02_CONFIG\configuracion_canales.env')
$dataSecret = Read-EnvFile (Join-Path $Root '02_CONFIG\data_bot.secret.env')
$alertsSecret = Read-EnvFile (Join-Path $Root '02_CONFIG\alerts_bot.secret.env')
$maps = @($channels,$dataSecret,$alertsSecret)
$webhooks = [ordered]@{
 DATA_LOG = Has-AnyValue $maps @('DATA_LOG_WEBHOOK','DATA_BOT_LOG_WEBHOOK','DISCORD_WEBHOOK_URL_DATA_LOG','DISCORD_DATA_LOG_WEBHOOK','WEBHOOK_DATA_LOG')
 DATA_ERROR = Has-AnyValue $maps @('DATA_ERROR_WEBHOOK','DATA_BOT_ERROR_WEBHOOK','DISCORD_WEBHOOK_URL_DATA_ERROR','DISCORD_DATA_ERROR_WEBHOOK','WEBHOOK_DATA_ERROR')
 ALERTS_LIVE = Has-AnyValue $maps @('ALERTS_LIVE_WEBHOOK','ALERTS_WEBHOOK','DISCORD_WEBHOOK_URL_ALERTS','WEBHOOK_ALERTS_LIVE')
 ALERTS_LOG = Has-AnyValue $maps @('ALERTS_LOG_WEBHOOK','DISCORD_WEBHOOK_URL_ALERTS_LOG','WEBHOOK_ALERTS_LOG')
 CALIBRATION = Has-AnyValue $maps @('DAILY_CALIBRATION_WEBHOOK','ALERTS_CALIBRATION_WEBHOOK','DISCORD_WEBHOOK_URL_DAILY','WEBHOOK_CALIBRATION')
}
Write-Host ''
Write-Host 'Webhooks (sin mostrar URLs):' -ForegroundColor Cyan
foreach($entry in $webhooks.GetEnumerator()) {
    $state=if($entry.Value){'OK'}else{'MISSING'}
    $color=if($entry.Value){'Green'}else{'Yellow'}
    Write-Host ("{0}: {1}" -f $entry.Key,$state) -ForegroundColor $color
}

$coveragePath = Join-Path $Root '03_DATOS\data_bot\output\history_coverage.json'
Write-Host ''
Write-Host 'Cobertura Data Bot:' -ForegroundColor Cyan
if(Test-Path $coveragePath) {
    try {
        $coverage=Get-Content $coveragePath -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host ("version={0} complete={1}" -f $coverage.bot_version,$coverage.bootstrap_complete)
        Write-Host ("CORE TMAD: {0}/{1} ready={2} tickers={3} end={4}" -f `
            $coverage.core_history_dates,$coverage.core_history_target_sessions,$coverage.core_history_ready,`
            $coverage.core_history_tickers,$coverage.core_history_end_date)
        Write-Host ("FULL DEEP: {0}/{1} end={2} zips={3}" -f `
            $coverage.paired_trading_dates,$coverage.target_daily_closes,$coverage.coverage_end_date,`
            $coverage.contributing_zip_count)
    } catch { Write-Host $_.Exception.Message -ForegroundColor Red }
} else { Write-Host 'MISSING history_coverage.json' -ForegroundColor Yellow }

Show-Pipeline 'DATA' (Join-Path $Root '03_DATOS\data_bot\output\pipeline_status.json')
Show-Pipeline 'ALERTS' (Join-Path $Root '03_DATOS\alerts_bot\output\pipeline_status.json')

Write-Host ''
Write-Host 'Logs detallados:' -ForegroundColor Cyan
Write-Host (Join-Path $Root '03_DATOS\data_bot\output\pipeline_diagnostics.jsonl')
Write-Host (Join-Path $Root '03_DATOS\alerts_bot\output\pipeline_diagnostics.jsonl')
Write-Host ''
Write-Host 'Fin del diagnostico.' -ForegroundColor Green
