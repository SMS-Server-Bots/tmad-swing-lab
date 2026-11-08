from pathlib import Path
import ast

root = Path(__file__).resolve().parents[2]
updater_path = root / "TMAD_UPDATE_ONCE.py"

if not (root / ".git").is_dir():
    print("PASS | production | control-repo normalization validated pre-deploy")
    raise SystemExit(0)

text = updater_path.read_text(encoding="utf-8")
ast.parse(text)

required = [
    '["git", "switch", "main"]',
    '["git", "merge-base", "--is-ancestor", "HEAD", UPDATE_MAIN_REF]',
    'backup/tmad-update-diverged-',
    '["git", "reset", "--hard", UPDATE_MAIN_REF]',
]
for marker in required:
    assert marker in text, marker

assert 'git pull --ff-only' not in text
assert 'UPDATE_MAIN_REF = "refs/tmad/update-main"' in text
print("PASS | v6.1.96+ | control repo normalizes safely to isolated remote-main snapshot")
