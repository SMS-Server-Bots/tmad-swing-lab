from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"command failed ({proc.returncode}): {' '.join(args)}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout.strip()


def _assert_source_boundaries() -> None:
    updater = (ROOT / "TMAD_UPDATE_ONCE.py").read_text(encoding="utf-8-sig")
    logs = (ROOT / "SYNC_RUNTIME_LOGS_TMAD.ps1").read_text(encoding="utf-8-sig")
    data = (ROOT / "SYNC_RUNTIME_DATA_TMAD.ps1").read_text(encoding="utf-8-sig")
    manual = (ROOT / "ACTUALIZAR_Y_DESPLEGAR_TMAD.ps1").read_text(encoding="utf-8-sig")

    assert 'UPDATE_MAIN_REF = "refs/tmad/update-main"' in updater
    assert 'f"+refs/heads/main:{UPDATE_MAIN_REF}"' in updater
    assert '"ls-remote", "--tags", origin_url' in updater
    assert '["git", "reset", "--hard", UPDATE_MAIN_REF]' in updater
    assert '["git", "fetch", "origin", "main"' not in updater
    assert '"origin/main"' not in updater

    assert 'refs/tmad/runtime-logs-main' in logs
    assert 'fetch $originUrl "+refs/heads/main:$runtimeMainRef"' in logs
    assert 'worktree add --detach --force $Worktree $runtimeMainRef' in logs
    assert not re.search(r'fetch\s+origin\s+main', logs, re.I)
    assert '"origin/main"' not in logs

    assert 'refs/tmad/runtime-data-main' in data
    assert '@("fetch",$originUrl,"+refs/heads/main:$runtimeMainRef","--quiet","--no-tags")' in data
    assert '@("worktree","add","--detach","--force",$Worktree,$runtimeMainRef)' in data
    assert not re.search(r'["\']fetch["\']\s*,\s*["\']origin["\']\s*,\s*["\']main["\']', data, re.I)
    assert '"origin/main"' not in data

    assert "refs/tmad/manual-update-main" in manual
    assert "@('fetch',$origin,('+refs/heads/main:' + $manualMainRef),'--no-tags')" in manual
    assert "@('merge','--ff-only',$manualMainRef)" in manual
    assert not re.search(r"fetch['\"]?\s*,?\s*['\"]origin['\"]\s*,?\s*['\"]main", manual, re.I)
    assert "pull','--ff-only','origin','main" not in manual


def _functional_private_ref_test() -> None:
    if not shutil.which("git"):
        raise AssertionError("git is required for v6.2.32 validation")

    with tempfile.TemporaryDirectory(prefix="tmad_git_isolation_") as tmp:
        root = Path(tmp)
        remote = root / "origin.git"
        seed = root / "seed"
        control = root / "control"

        _run(["git", "init", "--bare", str(remote)], root)
        seed.mkdir()
        _run(["git", "init", "-b", "main"], seed)
        _run(["git", "config", "user.name", "TMAD Validation"], seed)
        _run(["git", "config", "user.email", "validation@example.invalid"], seed)
        (seed / "VERSION.txt").write_text("6.2.31\n", encoding="utf-8")
        _run(["git", "add", "VERSION.txt"], seed)
        _run(["git", "commit", "-m", "base"], seed)
        _run(["git", "remote", "add", "origin", str(remote)], seed)
        _run(["git", "push", "-u", "origin", "main"], seed)

        _run(["git", "clone", "-b", "main", str(remote), str(control)], root)
        old_origin_main = _run(["git", "rev-parse", "refs/remotes/origin/main"], control)

        (seed / "VERSION.txt").write_text("6.2.32\n", encoding="utf-8")
        _run(["git", "add", "VERSION.txt"], seed)
        _run(["git", "commit", "-m", "next"], seed)
        _run(["git", "push", "origin", "main"], seed)
        new_main = _run(["git", "rev-parse", "HEAD"], seed)
        origin_url = _run(["git", "remote", "get-url", "origin"], control)

        for private_ref in (
            "refs/tmad/update-main",
            "refs/tmad/runtime-logs-main",
            "refs/tmad/runtime-data-main",
        ):
            _run(
                [
                    "git", "fetch", origin_url,
                    f"+refs/heads/main:{private_ref}",
                    "--force", "--no-tags",
                ],
                control,
            )
            assert _run(["git", "rev-parse", private_ref], control) == new_main

        # Critical regression guard: URL + explicit private destination must not
        # opportunistically move the shared origin/main remote-tracking ref.
        assert _run(["git", "rev-parse", "refs/remotes/origin/main"], control) == old_origin_main
        assert old_origin_main != new_main


def main() -> int:
    _assert_source_boundaries()
    _functional_private_ref_test()

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    assert tuple(int(x) for x in version.split(".")) >= (6, 2, 32), version

    print(
        "PASS | >=6.2.32 | updater/runtime publishers fetch main into isolated private refs "
        "| shared origin/main is not mutated | tag verified remotely"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
