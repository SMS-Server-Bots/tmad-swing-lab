[CmdletBinding()]
param(
    [string]$RepoPath = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [int]$EveryMinutes = 5
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if ($EveryMinutes -lt 1) { $EveryMinutes = 5 }
$script = Join-Path $RepoPath 'PREPARAR_RELEASE_TMAD.py'
if (-not (Test-Path $script -PathType Leaf)) { throw "Falta $script" }

$python = $null
foreach ($candidate in @('python.exe','py.exe')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) { throw 'No se encontro python.exe/py.exe en PATH.' }

$runtime = Join-Path $RepoPath 'runtime_observability'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$runner = Join-Path $runtime 'run_release_finalizer.cmd'
$log = Join-Path $runtime 'release_finalizer.log'

$pythonArgs = if ([IO.Path]::GetFileName($python).ToLowerInvariant() -eq 'py.exe') { '-3 ' } else { '' }
$body = @(
    '@echo off',
    ('cd /d "{0}"' -f $RepoPath),
    ('"{0}" {1}"{2}" --auto >> "{3}" 2>&1' -f $python,$pythonArgs,$script,$log)
) -join "`r`n"
Set-Content -Path $runner -Value $body -Encoding ASCII

$taskName = 'TMAD Release Finalizer'
$taskCmd = ('"{0}"' -f $runner)
& schtasks.exe /Create /TN $taskName /TR $taskCmd /SC MINUTE /MO $EveryMinutes /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks /Create fallo con codigo $LASTEXITCODE" }

Write-Host "OK - $taskName cada $EveryMinutes minutos" -ForegroundColor Green
Write-Host "Runner: $runner"
Write-Host "Log: $log"
