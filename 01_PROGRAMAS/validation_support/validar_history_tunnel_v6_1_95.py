from pathlib import Path
import ast

root = Path(__file__).resolve().parents[2]
is_control_repo = (root / ".git").is_dir()

updater_path = root / "TMAD_UPDATE_ONCE.py"
launcher_path = root / "01_PROGRAMAS" / "launcher" / "launcher.ps1"
deploy_path = root / "DESPLEGAR_TMAD.ps1"
example_path = root / "01_PROGRAMAS" / "history_query_worker" / "history_query_example.txt"

launcher = launcher_path.read_text(encoding="utf-8")
example = example_path.read_text(encoding="utf-8")

assert "history_query_tunnel.pid" in launcher
assert "tmad-history-worker-register" in launcher
assert "cloudflared.exe" in launcher
assert "127.0.0.1:8787" in launcher
assert "x-tmad-history-token" in launcher

assert "TMAD_HISTORY_TUNNEL_ENABLED=1" in example
assert "TMAD_HISTORY_REGISTER_URL=" in example

if is_control_repo:
    updater = updater_path.read_text(encoding="utf-8")
    deploy = deploy_path.read_text(encoding="utf-8")

    ast.parse(updater)

    assert "def _pid_is_alive(" in updater
    assert "def _lock_owner_pid(" in updater
    assert "if _lock_owner_pid(lock_path) == os.getpid():" in updater
    assert "owner_pid is not None and _pid_is_alive(owner_pid)" in updater

    assert "history_query_tunnel.pid" in deploy
    assert "cloudflared.exe" in deploy
    assert "127.0.0.1:8787" in deploy

    print("PASS | control repo | updater lock ownership + History Worker tunnel lifecycle")
else:
    print("PASS | production | History Worker tunnel runtime files; control-plane checks skipped")

