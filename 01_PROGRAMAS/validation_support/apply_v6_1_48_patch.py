from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def replace_once(rel: str, old: str, new: str) -> None:
    path = ROOT / rel
    text = path.read_text(encoding="utf-8-sig")
    if new in text:
        print(f"already patched: {rel}")
        return
    if old not in text:
        raise SystemExit(f"v6.1.48 patch anchor not found in {rel}")
    text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8", newline="")
    print(f"patched: {rel}")


mutex_anchor = 'New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null\n'
replace_once(
    "SYNC_RUNTIME_LOGS_TMAD.ps1",
    mutex_anchor,
    mutex_anchor + '''\n# Serialize all runtime-logs publishers (watcher/fallback/watchdog). A named\n# mutex is released by Windows automatically if a publisher crashes.\n$syncMutex = New-Object System.Threading.Mutex($false, "Global\\TMAD_RuntimeLogs_GitSync")\n$mutexAcquired = $false\ntry { $mutexAcquired = $syncMutex.WaitOne(0) }\ncatch [System.Threading.AbandonedMutexException] { $mutexAcquired = $true }\nif (-not $mutexAcquired) {\n    Write-Host "[INFO] runtime-logs sync ya esta en curso; ejecucion omitida." -ForegroundColor DarkGray\n    exit 0\n}\n''',
)

old_logs_worktree = '''if (Test-Path $Worktree) {\n    try { git -C $RepoPath worktree remove --force $Worktree | Out-Null } catch {}\n    if (Test-Path $Worktree) { Remove-Item $Worktree -Recurse -Force }\n}\n\ngit -C $RepoPath fetch origin main --quiet\nif ($LASTEXITCODE -ne 0) { throw "git fetch origin main fallo" }\n$remoteRefs = git -C $RepoPath ls-remote --heads origin $Branch\nif ($LASTEXITCODE -ne 0) { throw "No se pudo consultar la rama $Branch" }\nif ($remoteRefs) {\n    git -C $RepoPath fetch origin $Branch --quiet\n    if ($LASTEXITCODE -ne 0) { throw "No se pudo descargar $Branch" }\n    git -C $RepoPath worktree add --force -B $Branch $Worktree "origin/$Branch" | Out-Null\n} else {\n    git -C $RepoPath worktree add --force -B $Branch $Worktree "origin/main" | Out-Null\n}\nif ($LASTEXITCODE -ne 0) { throw "No se pudo preparar worktree runtime-logs" }\n\nPush-Location $Worktree\ntry {\n    git reset --hard origin/main | Out-Null\n    if ($LASTEXITCODE -ne 0) { throw "No se pudo basar runtime-logs en origin/main" }\n'''
# Source currently contains accented 'falló'; normalize anchor fallback.
logs_path = ROOT / "SYNC_RUNTIME_LOGS_TMAD.ps1"
logs_text = logs_path.read_text(encoding="utf-8-sig")
old_logs_worktree = old_logs_worktree.replace('fallo" }', 'falló" }')
new_logs_worktree = '''try { git -C $RepoPath worktree remove --force $Worktree 2>$null | Out-Null } catch {}\nif (Test-Path $Worktree) { Remove-Item $Worktree -Recurse -Force -ErrorAction SilentlyContinue }\ngit -C $RepoPath worktree prune --expire now 2>$null | Out-Null\n\ngit -C $RepoPath fetch origin main --quiet\nif ($LASTEXITCODE -ne 0) { throw "git fetch origin main falló" }\ngit -C $RepoPath worktree add --detach --force $Worktree "origin/main" | Out-Null\nif ($LASTEXITCODE -ne 0) { throw "No se pudo preparar worktree detached runtime-logs" }\n\nPush-Location $Worktree\ntry {\n'''
if new_logs_worktree not in logs_text:
    if old_logs_worktree not in logs_text:
        raise SystemExit("v6.1.48 logs worktree anchor not found")
    logs_text = logs_text.replace(old_logs_worktree, new_logs_worktree, 1)
    logs_path.write_text(logs_text, encoding="utf-8", newline="")
    print("patched: SYNC_RUNTIME_LOGS_TMAD.ps1 worktree")

replace_once(
    "SYNC_RUNTIME_DATA_TMAD.ps1",
    mutex_anchor,
    mutex_anchor + '''\n# Serialize runtime-data publishers to avoid overlapping worktree/export jobs.\n$syncMutex = New-Object System.Threading.Mutex($false, "Global\\TMAD_RuntimeData_GitSync")\n$mutexAcquired = $false\ntry { $mutexAcquired = $syncMutex.WaitOne(0) }\ncatch [System.Threading.AbandonedMutexException] { $mutexAcquired = $true }\nif (-not $mutexAcquired) {\n    Log "INFO" "runtime-data sync ya esta en curso; ejecucion omitida."\n    exit 0\n}\n''',
)

old_data_worktree = '''    $remote = Invoke-Git @("ls-remote","--heads","origin",$Branch) -AllowFailure\n\n    if (Test-Path $Worktree) {\n        [void](Invoke-Git @("worktree","remove","--force",$Worktree) -AllowFailure)\n        Remove-Item -LiteralPath $Worktree -Recurse -Force -ErrorAction SilentlyContinue\n    }\n\n    if ($remote.Code -eq 0 -and $remote.Output.Count -gt 0 -and ($remote.Output -join "").Trim()) {\n        [void](Invoke-Git @("fetch","origin",$Branch,"--quiet"))\n        [void](Invoke-Git @("worktree","add","--force","-B",$Branch,$Worktree,"origin/$Branch"))\n    } else {\n        [void](Invoke-Git @("worktree","add","--force","-B",$Branch,$Worktree,"origin/main"))\n    }\n\n    Push-Location $Worktree\n    try {\n        & git reset --hard origin/main | Out-Null\n        if ($LASTEXITCODE -ne 0) { throw "No se pudo basar runtime-data en origin/main" }\n'''
new_data_worktree = '''    if (Test-Path $Worktree) {\n        [void](Invoke-Git @("worktree","remove","--force",$Worktree) -AllowFailure)\n        Remove-Item -LiteralPath $Worktree -Recurse -Force -ErrorAction SilentlyContinue\n    }\n    [void](Invoke-Git @("worktree","prune","--expire","now") -AllowFailure)\n    [void](Invoke-Git @("worktree","add","--detach","--force",$Worktree,"origin/main"))\n\n    Push-Location $Worktree\n    try {\n'''
replace_once("SYNC_RUNTIME_DATA_TMAD.ps1", old_data_worktree, new_data_worktree)

old_installer = '''if ($FromDeploy -or $RunNow) {\n    & $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $WatchdogScript -MaxStaleHours 3 -WriteGraceSeconds 120 -ProductionPath $ProductionPath -RepoPath $RepoPath\n    $watchdogExit = $LASTEXITCODE\n    if ($watchdogExit -eq 0) {\n        Write-Host "[OK] Watchdog inicial confirma publicación convergente." -ForegroundColor Green\n    } elseif ($RunNow -and -not $FromDeploy) {\n        throw "Watchdog inicial TMAD no consiguió convergencia; código $watchdogExit"\n    } else {\n        Write-Host "[WARN] Watchdog inicial no convergió (código $watchdogExit). El despliegue continúa; la reparación independiente seguirá cada 30 min." -ForegroundColor Yellow\n    }\n}\n'''
new_installer = '''if ($FromDeploy) {\n    Write-Host "[INFO] Observabilidad instalada fuera del camino critico; el watchdog empezara en background." -ForegroundColor Cyan\n} elseif ($RunNow) {\n    & $PowerShellExe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $WatchdogScript -MaxStaleHours 3 -WriteGraceSeconds 120 -ProductionPath $ProductionPath -RepoPath $RepoPath\n    $watchdogExit = $LASTEXITCODE\n    if ($watchdogExit -ne 0) { throw "Watchdog manual TMAD no consiguió convergencia; código $watchdogExit" }\n    Write-Host "[OK] Watchdog manual confirma publicación convergente." -ForegroundColor Green\n}\n'''
replace_once("INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1", old_installer, new_installer)

old_deploy = '''    $runtimeLogInstaller = Join-Path $RepoRoot 'INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1'\n    if (-not (Test-Path $runtimeLogInstaller -PathType Leaf)) { throw "Falta instalador runtime logs: $runtimeLogInstaller" }\n    Write-Log 'Actualizando tarea horaria de runtime logs TMAD...'\n    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $runtimeLogInstaller -RepoPath $RepoRoot -ProductionPath $ProductionRoot -RetentionDays 7 -FromDeploy\n    if ($LASTEXITCODE -ne 0) { throw "Instalador runtime logs fallo con codigo $LASTEXITCODE" }\n    Write-Log 'Tarea runtime logs TMAD actualizada.' 'OK'\n'''
new_deploy = '''    $runtimeLogInstaller = Join-Path $RepoRoot 'INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1'\n    if (Test-Path $runtimeLogInstaller -PathType Leaf) {\n        Write-Log 'Actualizando observabilidad TMAD (best-effort, fuera del camino critico)...'\n        try {\n            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $runtimeLogInstaller -RepoPath $RepoRoot -ProductionPath $ProductionRoot -RetentionDays 7 -FromDeploy\n            if ($LASTEXITCODE -ne 0) { throw "Instalador observabilidad devolvio $LASTEXITCODE" }\n            Write-Log 'Observabilidad TMAD actualizada.' 'OK'\n        } catch {\n            Write-Log ('Observabilidad no pudo actualizarse; el deploy continua: ' + $_.Exception.Message) 'WARN'\n        }\n    } else {\n        Write-Log 'Instalador de observabilidad no presente; el deploy continua.' 'WARN'\n    }\n'''
replace_once("DESPLEGAR_TMAD.ps1", old_deploy, new_deploy)

validator_path = ROOT / "VALIDAR_TMAD_CURRENT.bat"
validator = validator_path.read_text(encoding="utf-8-sig")
old_parse = '''echo [RUN] parse PowerShell watchdog + installer v6.1.46\npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$files=@('01_PROGRAMAS\\observability\\runtime_publication_watchdog.ps1','INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1'); foreach($f in $files){$tokens=$null;$errors=$null;[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f),[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){$errors|%%{Write-Host $_.Message -ForegroundColor Red};exit 1}};Write-Host 'PASS ^| PowerShell parse v6.1.46'" || goto :fail\n'''
new_parse = '''echo [RUN] parse PowerShell observability v6.1.48\npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$files=@('01_PROGRAMAS\\observability\\runtime_publication_watchdog.ps1'); foreach($optional in @('INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1','SYNC_RUNTIME_LOGS_TMAD.ps1','SYNC_RUNTIME_DATA_TMAD.ps1')){if(Test-Path $optional){$files+=$optional}}; foreach($f in $files){$tokens=$null;$errors=$null;[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f),[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){$errors|%%{Write-Host $_.Message -ForegroundColor Red};exit 1}};Write-Host 'PASS ^| PowerShell parse v6.1.48'" || goto :fail\n'''
if new_parse not in validator:
    if old_parse not in validator:
        raise SystemExit("v6.1.48 validator parse anchor not found")
    validator = validator.replace(old_parse, new_parse, 1)

anchor = '%PY% "01_PROGRAMAS\\validation_support\\validation_runner.py" "01_PROGRAMAS\\validation_support\\validar_v6_1_46.py" || goto :fail\n'
add = anchor + 'echo [RUN] stable updater + detached runtime worktrees v6.1.48\n%PY% "01_PROGRAMAS\\validation_support\\validation_runner.py" "01_PROGRAMAS\\validation_support\\validar_v6_1_48.py" || goto :fail\n'
if 'validar_v6_1_48.py' not in validator:
    if anchor not in validator:
        raise SystemExit("v6.1.48 validator insertion anchor not found")
    validator = validator.replace(anchor, add, 1)
validator_path.write_text(validator, encoding="utf-8", newline="")
print("patched: VALIDAR_TMAD_CURRENT.bat")
