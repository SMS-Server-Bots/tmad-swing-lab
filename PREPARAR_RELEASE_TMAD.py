import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
VERSION_FILE = REPO / "VERSION.txt"

MARKERS = {
    "01_PROGRAMAS/alerts_bot/config.py": [
        (r'(?m)^BOT_VERSION\s*=\s*["\'][^"\']+["\']\s*$', 'BOT_VERSION = "{version}"'),
    ],
    "01_PROGRAMAS/data_bot/config.py": [
        (r'(?m)^BOT_VERSION\s*=\s*["\'][^"\']+["\']\s*$', 'BOT_VERSION = "{version}"'),
    ],
    "01_PROGRAMAS/alerts_bot/maintenance_v620.py": [
        (r'(?m)^VERSION\s*=\s*["\'][^"\']+["\']\s*$', 'VERSION = "{version}"'),
    ],
    "01_PROGRAMAS/data_bot/maintenance_v620.py": [
        (r'(?m)^VERSION\s*=\s*["\'][^"\']+["\']\s*$', "VERSION='{version}'"),
    ],
    "01_PROGRAMAS/data_bot/gex_negative_cache.py": [
        (r'(?m)^VERSION\s*=\s*["\'][^"\']+["\']\s*$', 'VERSION = "{version}"'),
    ],
    "01_PROGRAMAS/alerts_bot/start-bot.ps1": [
        (r'TMAD Alerts Adaptive Bot v\d+\.\d+\.\d+', 'TMAD Alerts Adaptive Bot v{version}'),
    ],
    "01_PROGRAMAS/data_bot/start-bot.ps1": [
        (r'TMAD Core Data Bot v\d+\.\d+\.\d+', 'TMAD Core Data Bot v{version}'),
    ],
}


def run(args, *, check=True, capture=True):
    proc = subprocess.run(
        args,
        cwd=str(REPO),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        shell=False,
    )
    if check and proc.returncode != 0:
        detail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        raise RuntimeError(f"{' '.join(args)} fallo ({proc.returncode}): {detail[-3000:]}")
    return proc


def git(*args, check=True):
    return run(["git", *args], check=check)


def read_version():
    value = VERSION_FILE.read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise RuntimeError(f"VERSION.txt invalido: {value!r}")
    return value


def ensure_main_clean_and_current():
    branch = git("branch", "--show-current").stdout.strip()
    if branch != "main":
        raise RuntimeError(f"La carpeta de control debe estar en main; actual={branch}")
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise RuntimeError("Working tree con cambios versionados; release finalizer no actuara.")
    git("fetch", "origin", "main", "--tags", "--force", "--prune")
    git("pull", "--ff-only", "origin", "main")


def tag_state(version):
    tag = f"v{version}"
    head = git("rev-parse", "HEAD").stdout.strip()
    p = git("rev-list", "-n", "1", tag, check=False)
    tagged = p.stdout.strip() if p.returncode == 0 else ""
    return tag, head, tagged


def sync_markers(version):
    changed = []
    for relative, rules in MARKERS.items():
        path = REPO / relative
        if not path.exists():
            raise RuntimeError(f"Falta marcador esperado: {relative}")
        raw = path.read_bytes()
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        original = text
        for pattern, template in rules:
            text, count = re.subn(pattern, template.format(version=version), text)
            if count != 1:
                raise RuntimeError(f"{relative}: marcador ambiguo/no encontrado ({count})")
        if text != original:
            path.write_text(text, encoding="utf-8-sig" if had_bom else "utf-8", newline="")
            changed.append(relative)
    return changed


def restore_marker_changes(changed):
    """Rollback local marker edits if release validation/commit fails.

    The control clone is an immutable deployment plane. A failed validator must
    never leave version-marker edits behind, because that dirties the working
    tree and blocks both the release finalizer and Discord %update.
    """
    paths = [str(path) for path in (changed or []) if str(path)]
    if not paths:
        return
    git("restore", "--staged", "--worktree", "--", *paths, check=False)
    residual = git("status", "--porcelain", "--untracked-files=no", "--", *paths, check=False).stdout.strip()
    if residual:
        raise RuntimeError(f"No se pudieron revertir marcadores tras fallo: {residual}")




def validate():
    validator = REPO / "VALIDAR_TMAD_CURRENT.bat"
    if not validator.exists():
        raise RuntimeError("Falta VALIDAR_TMAD_CURRENT.bat")
    proc = subprocess.run(
        ["cmd.exe", "/d", "/c", str(validator)],
        cwd=str(REPO),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        shell=False,
    )
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        raise RuntimeError(f"VALIDACION FALLIDA ({proc.returncode}): {detail[-6000:]}")


def finalize(auto=False):
    ensure_main_clean_and_current()
    version = read_version()
    tag, head_before, tagged = tag_state(version)
    if tagged:
        if tagged == head_before:
            print(f"READY {tag} -> {head_before}")
            return 0
        raise RuntimeError(
            f"{tag} ya existe en {tagged}, pero main esta en {head_before}. Incrementa VERSION.txt; nunca se mueve un tag publicado."
        )

    changed = sync_markers(version)
    marker_commit_created = False
    try:
        validate()

        if changed:
            git("add", "--", *changed)
            staged = git("diff", "--cached", "--name-only").stdout.splitlines()
            unexpected = sorted(set(staged) - set(changed))
            if unexpected:
                raise RuntimeError(f"Cambios staged inesperados: {unexpected}")
            git("commit", "-m", f"Sync TMAD version {version}")
            marker_commit_created = True
            git("push", "origin", "main")
    except Exception:
        if changed and not marker_commit_created:
            restore_marker_changes(changed)
        raise

    # Re-fetch after any marker commit and tag exactly the remote/main HEAD.
    git("fetch", "origin", "main", "--tags", "--force", "--prune")
    local_head = git("rev-parse", "HEAD").stdout.strip()
    remote_head = git("rev-parse", "origin/main").stdout.strip()
    if local_head != remote_head:
        raise RuntimeError(f"main local/remoto divergentes: {local_head} != {remote_head}")

    # Validate once more on the exact commit to be tagged.
    validate()
    git("tag", tag)
    git("push", "origin", f"refs/tags/{tag}")
    tagged_after = git("rev-list", "-n", "1", tag).stdout.strip()
    final_head = git("rev-parse", "HEAD").stdout.strip()
    if tagged_after != final_head:
        raise RuntimeError(f"Tag no exacto tras push: {tagged_after} != {final_head}")
    print(f"READY {tag} -> {final_head}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Finaliza una release TMAD fuera del camino critico de %update.")
    parser.add_argument("--auto", action="store_true", help="Modo tarea programada; errores se reportan por salida y no alteran produccion.")
    args = parser.parse_args()
    try:
        return finalize(auto=args.auto)
    except Exception as exc:
        print(f"RELEASE_NOT_READY: {exc}", file=sys.stderr)
        return 2 if args.auto else 1


if __name__ == "__main__":
    raise SystemExit(main())
