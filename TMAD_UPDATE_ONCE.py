import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

DEFAULT_REPO = Path(r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2")
DEFAULT_PRODUCTION = Path(r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick")
DEPLOY_TIMEOUT_SECONDS = 30 * 60
LOCK_STALE_SECONDS = 45 * 60
UPDATE_MAIN_REF = "refs/tmad/update-main"
DISCORD_TRANSIENT_HTTP_STATUS = {502, 503, 504}
DISCORD_REQUEST_MAX_ATTEMPTS = 3
DISCORD_TRANSIENT_DELAYS = (1.0, 3.0, 7.0)
AUTO_REPAIR_TRACKED_PATHS = {
    "01_PROGRAMAS/alerts_bot/config.py",
    "01_PROGRAMAS/data_bot/config.py",
    "01_PROGRAMAS/alerts_bot/maintenance_v620.py",
    "01_PROGRAMAS/data_bot/maintenance_v620.py",
    "01_PROGRAMAS/data_bot/gex_negative_cache.py",
    "01_PROGRAMAS/alerts_bot/start-bot.ps1",
    "01_PROGRAMAS/data_bot/start-bot.ps1",
}


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_control_config(production: Path) -> Dict[str, str]:
    config_dir = production / "02_CONFIG"
    merged: Dict[str, str] = {}
    for path in (
        config_dir / "configuracion_canales.env",
        config_dir / "tmad_execution.secret.env",
    ):
        merged.update(read_env_file(path))
    return merged


def _discord_transient_delay(attempt: int) -> float:
    index = max(0, min(int(attempt) - 1, len(DISCORD_TRANSIENT_DELAYS) - 1))
    return float(DISCORD_TRANSIENT_DELAYS[index])


def webhook_send(url: str, text: str) -> None:
    payload = json.dumps({"content": str(text)[:1950]}, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, DISCORD_REQUEST_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "TMAD-Update-Once/1.2"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            wrapped = RuntimeError(f"Discord webhook HTTP {exc.code}: {detail[:500]}")
            last_error = wrapped
            if int(exc.code or 0) not in DISCORD_TRANSIENT_HTTP_STATUS or attempt >= DISCORD_REQUEST_MAX_ATTEMPTS:
                raise wrapped from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            wrapped = RuntimeError(f"Discord webhook transport error: {exc}")
            last_error = wrapped
            if attempt >= DISCORD_REQUEST_MAX_ATTEMPTS:
                raise wrapped from exc
        time.sleep(_discord_transient_delay(attempt))

    if last_error is not None:
        raise last_error
    raise RuntimeError("Discord webhook failed without an exception")


def notify_webhook(url: str, text: str, *, context: str) -> bool:
    """Best-effort Discord delivery. Notification failure never changes deploy truth."""
    try:
        webhook_send(url, text)
        return True
    except Exception as exc:
        logging.warning(
            "Discord notification failed | context=%s error=%s",
            context,
            tail_text(str(exc), 700),
        )
        return False


def _background_creation_flags() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def run_command(args: List[str], cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
        creationflags=_background_creation_flags(),
    )


def run_command_file_capture(args: List[str], cwd: Path, output_path: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", errors="replace") as stream:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            shell=False,
            creationflags=_background_creation_flags(),
        )
    try:
        captured = output_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        captured = ""
    return subprocess.CompletedProcess(args, proc.returncode, stdout=captured, stderr="")

def tail_text(text: str, limit: int = 1500) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return "…" + value[-limit:]


def version(root: Path) -> str:
    path = root / "VERSION.txt"
    if not path.exists():
        return "unknown"
    return path.read_text(encoding="utf-8-sig", errors="ignore").strip() or "unknown"


def _dirty_tracked_paths(status_text: str) -> list[str]:
    paths: list[str] = []
    for raw in str(status_text or "").splitlines():
        line = raw.rstrip()
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[1].strip()
        value = value.strip('"').replace("\\", "/")
        if value:
            paths.append(value)
    return paths


def _repair_known_marker_dirt(repo: Path, status_text: str) -> list[str]:
    dirty = _dirty_tracked_paths(status_text)
    unknown = sorted(set(dirty) - AUTO_REPAIR_TRACKED_PATHS)
    if unknown:
        raise RuntimeError(
            "El repo de control tiene cambios locales versionados fuera de los marcadores reparables: "
            + ", ".join(unknown)
        )
    if not dirty:
        return []

    runtime_dir = repo / "runtime_observability"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    patch_path = runtime_dir / f"control_dirty_{stamp}.patch"
    diff = run_command(["git", "diff", "HEAD", "--binary", "--", *dirty], repo)
    if diff.returncode == 0 and diff.stdout:
        patch_path.write_text(diff.stdout, encoding="utf-8", errors="replace")

    restored = run_command(["git", "restore", "--staged", "--worktree", "--", *dirty], repo)
    if restored.returncode != 0:
        raise RuntimeError(f"No se pudieron restaurar marcadores locales: {tail_text(restored.stderr or restored.stdout)}")
    check = run_command(["git", "status", "--porcelain", "--untracked-files=no"], repo)
    if check.returncode != 0 or check.stdout.strip():
        raise RuntimeError(
            "El repo de control sigue con cambios tras la autorreparación: "
            + tail_text(check.stdout or check.stderr, 900)
        )
    logging.warning(
        "Control repo auto-repaired known marker dirt | files=%s backup_patch=%s",
        ",".join(dirty),
        patch_path if patch_path.exists() else "none",
    )
    return dirty


def git_pull_main(repo: Path) -> tuple[str, str]:
    proc = run_command(["git", "status", "--porcelain", "--untracked-files=no"], repo)
    if proc.returncode != 0:
        raise RuntimeError(f"git status falló: {tail_text(proc.stderr or proc.stdout)}")
    if proc.stdout.strip():
        _repair_known_marker_dirt(repo, proc.stdout)

    before = run_command(["git", "rev-parse", "HEAD"], repo)
    if before.returncode != 0:
        raise RuntimeError("No se pudo leer HEAD antes del update.")
    before_sha = before.stdout.strip()

    origin_proc = run_command(["git", "remote", "get-url", "origin"], repo)
    if origin_proc.returncode != 0 or not origin_proc.stdout.strip():
        raise RuntimeError("No se pudo resolver la URL remota origin.")
    origin_url = origin_proc.stdout.strip()

    # Fetch main through the repository URL into a private ref. Using the remote
    # name here would opportunistically update refs/remotes/origin/main, which is
    # also touched by the 24/7 runtime publishers and can race with %update.
    proc = run_command(
        [
            "git", "fetch", origin_url,
            f"+refs/heads/main:{UPDATE_MAIN_REF}",
            "--force", "--no-tags",
        ],
        repo,
        timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git fetch main aislado falló: {tail_text(proc.stderr or proc.stdout)}")

    current_branch = run_command(["git", "branch", "--show-current"], repo)
    if current_branch.returncode != 0:
        raise RuntimeError("No se pudo determinar la rama actual del repo de control.")
    branch_name = current_branch.stdout.strip()

    if branch_name != "main":
        switch = run_command(["git", "switch", "main"], repo)
        if switch.returncode != 0:
            raise RuntimeError(
                "No se pudo volver a main desde "
                f"{branch_name or 'detached HEAD'}: {tail_text(switch.stderr or switch.stdout)}"
            )
        logging.warning("Control repo branch normalized to main | previous_branch=%s", branch_name or "detached")

    ancestor = run_command(["git", "merge-base", "--is-ancestor", "HEAD", UPDATE_MAIN_REF], repo)
    if ancestor.returncode not in (0, 1):
        raise RuntimeError(f"No se pudo comparar main local con el snapshot remoto: {tail_text(ancestor.stderr or ancestor.stdout)}")
    if ancestor.returncode == 1:
        backup_name = f"backup/tmad-update-diverged-{time.strftime('%Y%m%d-%H%M%S')}"
        backup = run_command(["git", "branch", backup_name, "HEAD"], repo)
        if backup.returncode != 0:
            raise RuntimeError(
                "main local diverge de origin/main y no se pudo crear backup: "
                + tail_text(backup.stderr or backup.stdout)
            )
        logging.warning("Diverged local main preserved before reset | backup_branch=%s", backup_name)

    reset = run_command(["git", "reset", "--hard", UPDATE_MAIN_REF], repo)
    if reset.returncode != 0:
        raise RuntimeError(f"No se pudo sincronizar main con el snapshot remoto: {tail_text(reset.stderr or reset.stdout)}")

    after = run_command(["git", "rev-parse", "HEAD"], repo)
    if after.returncode != 0:
        raise RuntimeError("No se pudo leer HEAD después del update.")
    return before_sha, after.stdout.strip()


def verify_tag(repo: Path, target: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", target):
        raise RuntimeError(f"VERSION.txt no válida: {target}")
    tag = f"v{target}"

    origin_proc = run_command(["git", "remote", "get-url", "origin"], repo)
    if origin_proc.returncode != 0 or not origin_proc.stdout.strip():
        raise RuntimeError("No se pudo resolver la URL remota origin para verificar el tag.")
    origin_url = origin_proc.stdout.strip()

    # Verify the remote tag without fetching it into the shared local refspace.
    # This avoids tag/main ref transactions racing with runtime-data/runtime-logs.
    tag_proc = run_command(
        [
            "git", "ls-remote", "--tags", origin_url,
            f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}",
        ],
        repo,
        timeout=120,
    )
    if tag_proc.returncode != 0:
        raise RuntimeError(f"No se pudo verificar el tag remoto {tag}: {tail_text(tag_proc.stderr or tag_proc.stdout)}")

    direct_sha = ""
    peeled_sha = ""
    for raw in tag_proc.stdout.splitlines():
        parts = raw.strip().split()
        if len(parts) < 2:
            continue
        sha, ref_name = parts[0], parts[1]
        if ref_name == f"refs/tags/{tag}^{{}}":
            peeled_sha = sha
        elif ref_name == f"refs/tags/{tag}":
            direct_sha = sha
    tag_sha = peeled_sha or direct_sha
    if not tag_sha:
        raise RuntimeError(f"La versión {target} está en main pero no existe el tag remoto {tag}.")

    head_proc = run_command(["git", "rev-parse", "HEAD"], repo)
    if head_proc.returncode != 0:
        raise RuntimeError("No se pudo verificar HEAD.")
    head_sha = head_proc.stdout.strip()
    if tag_sha == head_sha:
        return tag

    # Control-plane/validation-only commits may legitimately live after the
    # latest exact runtime tag. Mirror the GitHub release gate semantics so a
    # harmless control-plane hotfix does not make %update unusable.
    ancestor = run_command(["git", "merge-base", "--is-ancestor", tag_sha, head_sha], repo)
    classifier = repo / ".github" / "scripts" / "classify_release_impact.py"
    if ancestor.returncode == 0 and classifier.exists():
        impact = run_command(
            [sys.executable, str(classifier), tag_sha, head_sha],
            repo,
            timeout=120,
        )
        if impact.returncode == 0:
            logging.warning(
                "Allowing control-plane-only HEAD after exact release tag | tag=%s tag_sha=%s head=%s",
                tag,
                tag_sha,
                head_sha,
            )
            return tag

    raise RuntimeError(
        f"{tag} no apunta al HEAD actual de main y existen cambios runtime/source; despliegue abortado."
    )


def deploy(repo: Path, production: Path) -> subprocess.CompletedProcess:
    script = repo / "DESPLEGAR_TMAD.ps1"
    if not script.exists():
        raise RuntimeError(f"Falta {script}")
    capture_path = repo / "runtime_observability" / f"deploy_subprocess_{time.strftime('%Y%m%d_%H%M%S')}.log"
    return run_command_file_capture(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-RepoRoot",
            str(repo),
            "-ProductionRoot",
            str(production),
        ],
        repo,
        output_path=capture_path,
        timeout=DEPLOY_TIMEOUT_SECONDS,
    )

def refresh_control_plane(repo: Path, production: Path) -> subprocess.CompletedProcess:
    installer = repo / "INSTALAR_CONTROL_UPDATE_TMAD.ps1"
    if not installer.exists():
        raise RuntimeError(f"Falta {installer}")
    return run_command(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            "-RepoRoot",
            str(repo),
            "-ProductionRoot",
            str(production),
            "-FromUpdater",
        ],
        repo,
        timeout=180,
    )


def _lock_owner_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    match = re.search(r"(?m)^pid=(\d+)\s*$", raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                shell=False,
                creationflags=_background_creation_flags(),
            )
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        return re.search(rf'"[^"]+","{pid}",', proc.stdout or "") is not None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def acquire_lock(path: Path) -> int:
    if path.exists():
        owner_pid = _lock_owner_pid(path)
        if owner_pid is not None and _pid_is_alive(owner_pid):
            raise RuntimeError(f"Ya hay una actualización TMAD en curso (PID {owner_pid}).")

        try:
            age = time.time() - path.stat().st_mtime
        except Exception:
            age = 0

        # A dead recorded owner is stale immediately. If the lock has no
        # readable owner, keep the legacy age safety net before removing it.
        stale = owner_pid is not None or age > LOCK_STALE_SECONDS
        if not stale:
            raise RuntimeError("Ya hay una actualización TMAD en curso.")

        try:
            path.unlink()
            logging.warning(
                "Removed stale TMAD update lock | owner_pid=%s age_seconds=%.1f",
                owner_pid,
                age,
            )
        except FileNotFoundError:
            pass
        except Exception as exc:
            raise RuntimeError(f"No se pudo retirar el lock TMAD obsoleto: {exc}") from exc

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("Ya hay una actualización TMAD en curso.") from exc

    os.write(fd, f"pid={os.getpid()}\nstarted={time.time()}\n".encode("utf-8"))
    try:
        os.fsync(fd)
    except Exception:
        pass
    return fd


def main() -> int:
    repo = Path(os.getenv("TMAD_CONTROL_REPO", str(DEFAULT_REPO))).resolve()
    production = Path(os.getenv("TMAD_PRODUCTION_ROOT", str(DEFAULT_PRODUCTION))).resolve()
    runtime_dir = repo / "runtime_observability"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(runtime_dir / "tmad_update_once.log", encoding="utf-8")],
    )

    quiet_if_current = "--quiet-if-current" in sys.argv
    quiet_failures = "--quiet-failures" in sys.argv
    quiet_progress = "--quiet-progress" in sys.argv
    webhook = ""

    lock_path = runtime_dir / "tmad_update.lock"
    lock_fd = None
    try:
        config = load_control_config(production)
        webhook = str(config.get("EXECUTION_WEBHOOK") or os.getenv("EXECUTION_WEBHOOK") or "").strip()
        if not webhook:
            raise RuntimeError("Falta EXECUTION_WEBHOOK en 02_CONFIG\\configuracion_canales.env")

        lock_fd = acquire_lock(lock_path)
        production_before = version(production)
        logging.info("%%update started | production=%s repo=%s quiet_if_current=%s", production_before, repo, quiet_if_current)

        _, after_sha = git_pull_main(repo)
        target = version(repo)
        tag = verify_tag(repo, target)

        if production_before == target:
            if not quiet_if_current:
                notify_webhook(
                    webhook,
                    "✅ **TMAD ya está actualizado.**\n"
                    f"Producción: `{production_before}` · GitHub: `{target}` · Tag: `{tag}`\n"
                    "No se reiniciaron Data Bot ni Alerts Bot.",
                    context="no_op",
                )
            logging.info("%%update no-op | version=%s head=%s", target, after_sha)
            return 0

        if not quiet_progress:
            notify_webhook(
                webhook,
                "⬆️ **Actualización TMAD detectada.**\n"
                f"Producción: `{production_before}` · objetivo: `{target}` · Tag: `{tag}`\n"
                "02_CONFIG, 03_DATOS y 04_RUNTIME se preservan.",
                context="update_detected",
            )
            notify_webhook(
                webhook,
                f"📦 Release verificada: `{tag}`. Desplegando `{production_before}` → `{target}`…",
                context="release_verified",
            )
        proc = deploy(repo, production)
        output = tail_text((proc.stdout or "") + "\n" + (proc.stderr or ""))
        if proc.returncode != 0:
            raise RuntimeError(f"DESPLEGAR_TMAD.ps1 terminó con código {proc.returncode}. {output}")

        production_after = version(production)
        if production_after != target:
            raise RuntimeError(f"Producción quedó en {production_after}; esperada {target}.")

        controller_refresh = refresh_control_plane(repo, production)
        controller_output = tail_text((controller_refresh.stdout or "") + "\n" + (controller_refresh.stderr or ""))
        if controller_refresh.returncode != 0:
            logging.error("TMAD controller refresh failed after deploy | %s", controller_output)
            notify_webhook(
                webhook,
                "⚠️ **TMAD actualizado, pero el controlador de %update no pudo refrescarse.**\n"
                f"Versión producción: `{production_after}`\n"
                f"Detalle: `{controller_output[:900]}`",
                context="controller_refresh_warning",
            )
        else:
            logging.info("TMAD controller refreshed after deploy")

        # Operational truth is established before the best-effort notification.
        logging.info("%%update success | version=%s head=%s", production_after, after_sha)
        notify_webhook(
            webhook,
            "✅ **Actualización TMAD completada correctamente.**\n"
            f"Versión: `{production_after}`\n"
            "Data Bot + Alerts Bot desplegados con validación, backup y rollback disponible.\n"
            "02_CONFIG, 03_DATOS y 04_RUNTIME preservados.",
            context="update_success",
        )
        return 0
    except subprocess.TimeoutExpired as exc:
        logging.exception("%%update timeout")
        if not quiet_failures and webhook:
            try:
                notify_webhook(
                    webhook,
                    f"❌ **Actualización TMAD fallida.** Timeout: `{tail_text(str(exc), 1200)}`",
                    context="update_timeout",
                )
            except Exception:
                pass
        return 2
    except Exception as exc:
        logging.exception("%%update failed")
        if not quiet_failures and webhook:
            try:
                notify_webhook(
                    webhook,
                    f"❌ **Actualización TMAD fallida.**\nError: `{tail_text(str(exc), 1500)}`",
                    context="update_failure",
                )
            except Exception:
                pass
        return 1
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
            try:
                if _lock_owner_pid(lock_path) == os.getpid():
                    lock_path.unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())