from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = Path(r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2")
DEFAULT_PRODUCTION = Path(r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick")


def _version(root: Path) -> str:
    path = root / "VERSION.txt"
    if not path.exists():
        return "unknown"
    return path.read_text(encoding="utf-8-sig", errors="ignore").strip() or "unknown"


def _state_path(repo: Path) -> Path:
    path = repo / "runtime_observability" / "release_consumer_quarantine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_state(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _background_creation_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def main() -> int:
    repo = Path(os.getenv("TMAD_CONTROL_REPO", str(DEFAULT_REPO))).resolve()
    production = Path(os.getenv("TMAD_PRODUCTION_ROOT", str(DEFAULT_PRODUCTION))).resolve()
    worker = repo / "TMAD_UPDATE_ONCE.py"
    if not worker.exists():
        return 2

    target = _version(repo)
    current = _version(production)
    state_file = _state_path(repo)
    state = _load_state(state_file)
    failed_target = str(state.get("failed_target") or "")

    if current == target:
        if state_file.exists():
            state_file.unlink(missing_ok=True)
        return 0

    # Exact version tags are immutable. If the automatic consumer already
    # failed this target, do not hammer the same invalid release every five
    # minutes. A new version automatically clears the quarantine. Manual
    # Discord %update bypasses this consumer and remains available.
    if failed_target and failed_target == target:
        return 0

    if failed_target and failed_target != target:
        state_file.unlink(missing_ok=True)

    proc = subprocess.run(
        [sys.executable, str(worker), "--quiet-if-current", "--quiet-failures", "--quiet-progress"],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        timeout=35 * 60,
        creationflags=_background_creation_flags(),
    )
    code = int(proc.returncode)
    if code != 0:
        _save_state(
            state_file,
            {
                "failed_target": target,
                "production_version": current,
                "returncode": code,
                "policy": "skip_same_target_until_version_changes_manual_update_bypasses",
            },
        )
    elif state_file.exists():
        state_file.unlink(missing_ok=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
