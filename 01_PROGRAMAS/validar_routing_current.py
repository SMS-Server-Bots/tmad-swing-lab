from pathlib import Path
import ast
import re

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / '01_PROGRAMAS'
checks = []


def ok(name, cond):
    checks.append((name, bool(cond)))
    print(('PASS' if cond else 'FAIL'), '|', name)


version = (ROOT / 'VERSION.txt').read_text(encoding='utf-8').strip()
alerts_cfg = (P / 'alerts_bot' / 'config.py').read_text(encoding='utf-8-sig')
data_cfg = (P / 'data_bot' / 'config.py').read_text(encoding='utf-8-sig')
alerts = (P / 'alerts_bot' / 'discord_utils.py').read_text(encoding='utf-8')
data = (P / 'data_bot' / 'discord_utils.py').read_text(encoding='utf-8')
pos = (P / 'alerts_bot' / 'positions_overview.py').read_text(encoding='utf-8')
eod = (P / 'alerts_bot' / 'eod_discord.py').read_text(encoding='utf-8')
repo_control = (ROOT / '.git').is_dir()

ok('VERSION semver', bool(re.fullmatch(r'\d+\.\d+\.\d+', version)))
ok('Alerts BOT_VERSION sincronizada', f'BOT_VERSION = "{version}"' in alerts_cfg)
ok('Data BOT_VERSION sincronizada', f'BOT_VERSION = "{version}"' in data_cfg)
ok('scope detectado', repo_control or not (ROOT / '.git').exists())

if repo_control:
    deploy = (ROOT / 'DESPLEGAR_TMAD.ps1').read_text(encoding='utf-8-sig')
    update = (ROOT / 'ACTUALIZAR_Y_DESPLEGAR_TMAD.ps1').read_text(encoding='utf-8-sig')
    gitignore = (ROOT / '.gitignore').read_text(encoding='utf-8-sig')
    controller_path = ROOT / 'TMAD_DISCORD_UPDATE_CONTROLLER.py'
    worker_path = ROOT / 'TMAD_UPDATE_ONCE.py'
    installer_path = ROOT / 'INSTALAR_CONTROL_UPDATE_TMAD.ps1'
    stable_bat = ROOT / 'VALIDAR_TMAD_CURRENT.bat'
    runtime_sync_path = ROOT / 'SYNC_RUNTIME_LOGS_TMAD.ps1'
    runtime_installer_path = ROOT / 'INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1'
    data_start_path = P / 'data_bot' / 'start-bot.ps1'
    alerts_start_path = P / 'alerts_bot' / 'start-bot.ps1'

    ok('controller existe', controller_path.is_file())
    ok('worker existe', worker_path.is_file())
    ok('installer existe', installer_path.is_file())
    ok('validador estable existe', stable_bat.is_file())
    ok('runtime sync existe', runtime_sync_path.is_file())
    ok('runtime installer existe', runtime_installer_path.is_file())

    controller = controller_path.read_text(encoding='utf-8')
    worker = worker_path.read_text(encoding='utf-8')
    installer = installer_path.read_text(encoding='utf-8-sig')
    runtime_sync = runtime_sync_path.read_text(encoding='utf-8-sig')
    runtime_installer = runtime_installer_path.read_text(encoding='utf-8-sig')
    data_start = data_start_path.read_text(encoding='utf-8-sig')
    alerts_start = alerts_start_path.read_text(encoding='utf-8-sig')
    ast.parse(controller, filename=str(controller_path))
    ast.parse(worker, filename=str(worker_path))
    ok('control python compila', True)
    ok('comando exacto %update', 'command == "%update"' in controller)
    ok('ignora bots/webhooks', 'author.get("bot")' in controller and 'message.get("webhook_id")' in controller)
    ok('channel_id desde metadata webhook', 'data.get("channel_id")' in controller and 'webhook_info' in controller)
    ok('sin shell arbitrario', 'shell=True' not in controller and 'shell=True' not in worker)
    ok('worker usa deploy oficial', 'DESPLEGAR_TMAD.ps1' in worker)
    ok('worker verifica tag', 'verify_tag' in worker and 'rev-list' in worker)
    ok('worker preserva config/datos/runtime', '02_CONFIG, 03_DATOS y 04_RUNTIME' in worker)
    ok('worker no reinicia listener durante update', 'refresh_controller' not in worker)
    ok('controller fuera de Python produccion', 'no usara los venv de produccion' in installer)
    ok('tarea inicia al logon', 'Trumppetta_TMAD_Discord_Update_Controller' in installer and 'New-ScheduledTaskTrigger -AtLogOn' in installer)
    ok('listener watchdog cada minuto', 'RepetitionInterval (New-TimeSpan -Minutes 1)' in installer and '$watchdogTrigger' in installer and '$triggers' in installer)
    ok('listener sin duplicados', '-MultipleInstances IgnoreNew' in installer)
    ok('listener heartbeat observable', 'HEALTH_LOG_SECONDS = 60.0' in controller and 'TMAD controller heartbeat' in controller)
    ok('runtime_observability ignorado', 'runtime_observability/' in gitignore)
    ok('deploy usa validador estable', "VALIDAR_TMAD_CURRENT.bat" in deploy)
    ok('deploy cierra arbol PowerShell anterior', 'Stop-TmadProcessTreeVerified -RootPid ([int]$target.ProcessId)' in deploy and 'taskkill.exe /PID $RootPid /T /F' in deploy)
    ok('deploy cierra shells daemon huerfanas', 'Get-LingeringTmadDaemonShells' in deploy)
    ok('deploy exige pareja nueva', 'Pareja nueva confirmada' in deploy and 'CreationDate -ge $startedAt' in deploy)
    ok('deploy verifica venv Data', "\\venvs\\data_bot\\Scripts\\python.exe" in deploy)
    ok('deploy verifica venv Alerts', "\\venvs\\alerts_bot\\Scripts\\python.exe" in deploy)
    ok('deploy instala runtime logs', 'INSTALAR_SYNC_RUNTIME_LOGS_TMAD.ps1' in deploy)
    ok('Data captura consola completa', 'Start-Transcript' in data_start and 'tmad_data_console_' in data_start)
    ok('Alerts captura consola completa', 'Start-Transcript' in alerts_start and 'tmad_alerts_console_' in alerts_start)
    ok('runtime logs 7 dias', '[int]$RetentionDays = 7' in runtime_sync)
    ok('runtime logs rama dedicada', '$Branch = "runtime-logs"' in runtime_sync)
    ok('runtime logs sanitiza secretos', 'REDACTED_DISCORD_WEBHOOK' in runtime_sync and 'DISCORD_TOKEN' in runtime_sync)
    ok('runtime logs Madrid', 'Romance Standard Time' in runtime_sync)
    ok('runtime logs ventana 12-00', '$madridNow.Hour -ge 12' in runtime_sync and '$closingMidnight' in runtime_sync)
    ok('runtime logs horario cada hora', 'New-TimeSpan -Hours 1' in runtime_installer)
else:
    deploy = ''
    update = ''
    ok('production mirror sin artefactos repo-only', True)

for key in [
    'ALERTS_LIVE_WEBHOOK', 'ALERTS_LOG_WEBHOOK', 'ALERTS_ERROR_WEBHOOK',
    'ALERTS_RESEARCH_WEBHOOK', 'ALERTS_SHADOW_WEBHOOK', 'ALERTS_POSITIONS_WEBHOOK',
    'MODEL_CALIBRATION_WEBHOOK', 'MODEL_LEARNING_WEBHOOK', 'SYSTEM_STATUS_WEBHOOK',
    'TMAD_ALPHA_OUTPUT_WEBHOOK',
]:
    ok('Alerts routing ' + key, key in alerts)

for key in ['DATA_LOG_WEBHOOK', 'DATA_QUALITY_WEBHOOK', 'DATA_ERROR_WEBHOOK', 'SYSTEM_STATUS_WEBHOOK']:
    ok('Data routing ' + key, key in data)

ok('LIVE canal estricto', 'channel="alerts_live"' in alerts and 'send_alert' in alerts)
ok('SHADOW canal estricto', 'channel="alerts_shadow"' in alerts and 'send_shadow' in alerts)
ok('Research canal estricto', 'channel="alerts_research"' in alerts)
ok('Alpha Output canal estricto', 'channel="alpha_output"' in alerts and 'def send_alpha_output' in alerts)
ok('Alpha EOD usa routing central', 'from discord_utils import send_alpha_output' in eod and 'send_alpha_output(msg)' in eod)
ok('Alpha Output sin fallback Research/Live', 'ALERTS_RESEARCH_WEBHOOK' not in alerts[alerts.index('def send_alpha_output'):alerts.index('def _is_learning_audit_message')] and 'ALERTS_LIVE_WEBHOOK' not in alerts[alerts.index('def send_alpha_output'):alerts.index('def _is_learning_audit_message')])
ok('Positions canal estricto', 'channel="alerts_positions"' in alerts and 'send_status' in pos)
ok('Alerts Log canal estricto', 'channel="alerts_log"' in alerts)
ok('Alerts Error canal estricto', 'channel="alerts_error"' in alerts)
ok('Model Calibration propio', 'channel="model_calibration"' in alerts)
ok('Model Learning propio', 'channel="model_learning"' in alerts)
ok('System Status propio Alerts', 'channel="system_status"' in alerts)
ok('System Status propio Data', 'channel="system_status"' in data)

if repo_control:
    ok('deploy directo sin archive/tag', 'git archive' not in deploy.lower() and 'refs/tags' not in deploy.lower())
    ok('deploy valida antes y despues', deploy.count('Invoke-Validator') >= 3)
    ok('deploy preserva persistentes', '02_CONFIG, 03_DATOS y 04_RUNTIME preservados' in deploy)
    ok('deploy espejo programas', 'robocopy /MIR' in deploy)
    ok('deploy rollback automatico', 'function Restore-Backup' in deploy and 'Restore-Backup -RootFiles $rootFiles' in deploy)
    ok('pull ff-only', "'pull','--ff-only','origin','main'" in update)
    ok('update exige main', "branch -ne 'main'" in update)
    ok('update exige arbol limpio', "status','--porcelain" in update)

failed = [name for name, passed in checks if not passed]
print(f'SCOPE={"repo_control" if repo_control else "production_mirror"}')
print(f'TOTAL_PASS={len(checks)-len(failed)}/{len(checks)}')
if failed:
    raise SystemExit('Fallos: ' + ', '.join(failed))
