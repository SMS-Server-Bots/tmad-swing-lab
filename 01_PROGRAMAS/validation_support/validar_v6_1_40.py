from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
alerts_cfg = (ROOT / "01_PROGRAMAS/alerts_bot/config.py").read_text(encoding="utf-8-sig")
data_cfg = (ROOT / "01_PROGRAMAS/data_bot/config.py").read_text(encoding="utf-8-sig")
data_main = (ROOT / "01_PROGRAMAS/data_bot/main.py").read_text(encoding="utf-8-sig")
lifecycle = (ROOT / "01_PROGRAMAS/alerts_bot/lifecycle.py").read_text(encoding="utf-8-sig")
validator_bat = (ROOT / "VALIDAR_TMAD_CURRENT.bat").read_text(encoding="utf-8-sig")
bootstrap_path = ROOT / "01_PROGRAMAS/validation_support/PREPARAR_VALIDATION_ENV_TMAD.ps1"
bootstrap = bootstrap_path.read_text(encoding="utf-8-sig")

version_tuple = tuple(int(part) for part in version.split("."))
assert version_tuple >= (6, 1, 40), version
assert f'BOT_VERSION = "{version}"' in alerts_cfg
assert f'BOT_VERSION = "{version}"' in data_cfg

# v6.1.39 behaviour must remain intact.
assert 'ThreadPoolExecutor' in data_main
assert 'GEX_SELECTIVE_FETCH_WORKERS' in data_cfg
assert 'for target in ("live", "shadow", "research", "preliminary")' in lifecycle
assert 'retry_pending_exit_deliveries()' in lifecycle

# v6.1.40: validation has its own deterministic dependency environment.
assert bootstrap_path.is_file()
assert 'alerts_bot\\requirements.txt' in bootstrap
assert 'data_bot\\requirements.txt' in bootstrap
assert 'import dotenv, requests, httpx' in bootstrap
assert '.tmad_requirements.sha256' in bootstrap
assert '-m pip install' in bootstrap

bat_lower = validator_bat.lower()
assert 'preparar_validation_env_tmad.ps1' in bat_lower
assert 'tmad_validation_python' in bat_lower
assert 'tmad_validation_venv' in bat_lower
assert "import dotenv, requests, httpx" in validator_bat
assert 'validar_v6_1_40.py' in bat_lower
assert bat_lower.index('preparar_validation_env_tmad.ps1') < bat_lower.index('set "tmad_network_allowed=0"')

print('PASS | TMAD v6.1.40+ deterministic validation environment')
