[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2',
    [string]$ProductionRoot = 'C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    # Windows PowerShell 5.1 can promote native stderr to an ErrorRecord when
    # ErrorActionPreference=Stop. Git writes normal progress messages (for example
    # "From https://github.com/...") to stderr even when the command succeeds.
    # Capture both streams as text and decide success strictly from LASTEXITCODE.
    $previousEap = $ErrorActionPreference
    $output = @()
    $exitCode = 1
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& git -C $RepoRoot @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousEap
    }

    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') fallo (exit=$exitCode):`n$($output -join [Environment]::NewLine)"
    }
    return @($output)
}

try {
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host ' TMAD - GIT PULL + DESPLIEGUE DIRECTO' -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git no esta disponible en PATH.' }
    if (-not (Test-Path (Join-Path $RepoRoot '.git') -PathType Container)) { throw "Repo invalido: $RepoRoot" }

    $branch = (Invoke-Git @('branch','--show-current') | Select-Object -First 1).Trim()
    if ($branch -ne 'main') { throw "La carpeta _2 debe estar en main; rama actual=$branch" }

    $dirty = @(Invoke-Git @('status','--porcelain'))
    if ($dirty.Count -gt 0) {
        throw "Working tree no limpio en _2. No se hara pull/despliegue.`n$($dirty -join [Environment]::NewLine)"
    }

    $origin = (Invoke-Git @('remote','get-url','origin') | Select-Object -First 1).Trim()
    if ($origin -notmatch 'SMS-Server-Bots[/\\]tmad-dual-bot(\.git)?$') {
        throw "origin inesperado: $origin"
    }

    $before = (Invoke-Git @('rev-parse','HEAD') | Select-Object -First 1).Trim()
    $manualMainRef = 'refs/tmad/manual-update-main'
    [void](Invoke-Git @('fetch',$origin,('+refs/heads/main:' + $manualMainRef),'--no-tags'))
    [void](Invoke-Git @('merge','--ff-only',$manualMainRef))
    $after = (Invoke-Git @('rev-parse','HEAD') | Select-Object -First 1).Trim()

    Write-Host ("[OK] main actualizado - {0} -> {1}" -f $before.Substring(0,7),$after.Substring(0,7)) -ForegroundColor Green

    $deploy = Join-Path $RepoRoot 'DESPLEGAR_TMAD.ps1'
    if (-not (Test-Path $deploy -PathType Leaf)) { throw "Falta $deploy" }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $deploy -RepoRoot $RepoRoot -ProductionRoot $ProductionRoot
    exit $LASTEXITCODE
}
catch {
    Write-Host ('[ERROR] ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
