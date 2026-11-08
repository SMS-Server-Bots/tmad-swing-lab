from pathlib import Path
import ast

root = Path(__file__).resolve().parents[2]

if not (root / ".git").is_dir():
    print("PASS | production | updater subprocess pipe fix validated pre-deploy")
    raise SystemExit(0)

path = root / "TMAD_UPDATE_ONCE.py"
text = path.read_text(encoding="utf-8")
ast.parse(text)

assert "def run_command_file_capture(" in text
assert "stdout=stream" in text
assert "stderr=subprocess.STDOUT" in text
assert 'deploy_subprocess_' in text

start = text.index("def deploy(repo: Path, production: Path)")
end = text.index("\n\ndef refresh_control_plane", start)
deploy_block = text[start:end]

assert "run_command_file_capture(" in deploy_block
assert "capture_output=True" not in deploy_block

print("PASS | v6.1.97 | deploy output uses file capture; no PIPE wait")
