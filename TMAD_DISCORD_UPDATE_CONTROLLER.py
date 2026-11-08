import json
import logging
import os
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_REPO = Path(r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick_2")
DEFAULT_PRODUCTION = Path(r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick")
DISCORD_API = "https://discord.com/api/v10"
POLL_SECONDS = 5.0
HEALTH_LOG_SECONDS = 60.0
AUTHORIZED_UPDATE_WEBHOOK_IDS = {"1535770800441786468", "1550424659118526615"}
DISCORD_TRANSIENT_HTTP_STATUS = {502, 503, 504}
DISCORD_REQUEST_MAX_ATTEMPTS = 3
DISCORD_TRANSIENT_DELAYS = (1.0, 3.0, 7.0)
POLL_ALERT_AFTER_CONSECUTIVE_FAILURES = 3


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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
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


def normalize_command(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return text.strip()


def _transient_delay(attempt: int) -> float:
    index = max(0, min(int(attempt) - 1, len(DISCORD_TRANSIENT_DELAYS) - 1))
    return float(DISCORD_TRANSIENT_DELAYS[index])


def _urlopen_with_retry(req, *, timeout: float = 20.0):
    last_exc = None
    for attempt in range(1, DISCORD_REQUEST_MAX_ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if int(exc.code or 0) not in DISCORD_TRANSIENT_HTTP_STATUS or attempt >= DISCORD_REQUEST_MAX_ATTEMPTS:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt >= DISCORD_REQUEST_MAX_ATTEMPTS:
                raise
        time.sleep(_transient_delay(attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Discord request failed without an exception")


def webhook_info(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TMAD-Discord-Update-Controller/1.2"},
        method="GET",
    )
    try:
        with _urlopen_with_retry(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"EXECUTION_WEBHOOK no válido: Discord HTTP {exc.code}: {detail[:500]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("EXECUTION_WEBHOOK no devolvió metadata válida.")
    return data


def channel_id_from_webhook(url: str) -> int:
    data = webhook_info(url)
    try:
        channel_id = int(data.get("channel_id") or 0)
    except Exception:
        channel_id = 0
    if channel_id <= 0:
        raise RuntimeError("EXECUTION_WEBHOOK no contiene un channel_id válido.")
    return channel_id


def discord_request(token: str, path: str) -> object:
    req = urllib.request.Request(
        f"{DISCORD_API}{path}",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "TMAD-Discord-Update-Controller/1.2",
        },
        method="GET",
    )
    try:
        with _urlopen_with_retry(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord HTTP {exc.code}: {detail[:500]}") from exc


def webhook_send(url: str, text: str) -> None:
    payload = json.dumps({"content": str(text)[:1950]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "TMAD-Discord-Update-Controller/1.2"},
        method="POST",
    )
    try:
        with _urlopen_with_retry(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord webhook HTTP {exc.code}: {detail[:500]}") from exc


def get_credentials(production: Path) -> Tuple[str, str, int]:
    config = load_control_config(production)
    webhook = str(config.get("EXECUTION_WEBHOOK") or os.getenv("EXECUTION_WEBHOOK") or "").strip()
    token = str(
        config.get("DISCORD_TOKEN")
        or config.get("EXECUTION_BOT_TOKEN")
        or config.get("DISCORD_BOT_TOKEN")
        or os.getenv("DISCORD_TOKEN")
        or os.getenv("EXECUTION_BOT_TOKEN")
        or os.getenv("DISCORD_BOT_TOKEN")
        or ""
    ).strip()
    if not webhook:
        raise RuntimeError("Falta EXECUTION_WEBHOOK en 02_CONFIG\\configuracion_canales.env")
    if not token:
        raise RuntimeError("Falta DISCORD_TOKEN en 02_CONFIG\\configuracion_canales.env")
    return token, webhook, channel_id_from_webhook(webhook)


def load_state(path: Path) -> Dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def save_state(path: Path, last_message_id: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"last_message_id": str(last_message_id), "updated_at": time.time()}, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def fetch_messages(token: str, channel_id: int, after: str = "") -> List[dict]:
    params = {"limit": "50"}
    if after:
        params["after"] = after
    query = urllib.parse.urlencode(params)
    data = discord_request(token, f"/channels/{channel_id}/messages?{query}")
    if not isinstance(data, list):
        raise RuntimeError("Discord no devolvió una lista de mensajes.")
    return sorted((m for m in data if isinstance(m, dict)), key=lambda m: int(m.get("id") or 0))


def launch_update(repo: Path) -> int:
    worker = repo / "TMAD_UPDATE_ONCE.py"
    if not worker.exists():
        raise RuntimeError(f"Falta {worker}")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, str(worker)],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    return int(proc.pid)


def check_config(repo: Path, production: Path) -> int:
    token, webhook, channel_id = get_credentials(production)
    channel = discord_request(token, f"/channels/{channel_id}")
    if not isinstance(channel, dict) or int(channel.get("id") or 0) != channel_id:
        raise RuntimeError("El bot no pudo validar el canal EXECUTION.")
    fetch_messages(token, channel_id)
    info = webhook_info(webhook)
    if int(info.get("channel_id") or 0) != channel_id:
        raise RuntimeError("EXECUTION_WEBHOOK y canal Discord no coinciden.")
    channel_name = str(channel.get("name") or "").encode("ascii", "backslashreplace").decode("ascii")
    print(f"OK channel_id={channel_id} channel_name={channel_name} repo={repo}")
    return 0


def main() -> int:
    repo = Path(os.getenv("TMAD_CONTROL_REPO", str(DEFAULT_REPO))).resolve()
    production = Path(os.getenv("TMAD_PRODUCTION_ROOT", str(DEFAULT_PRODUCTION))).resolve()
    check_only = "--check-config" in sys.argv

    if not (repo / ".git").exists():
        raise RuntimeError(f"Repo de control inválido: {repo}")
    if not production.exists():
        raise RuntimeError(f"Producción TMAD inexistente: {production}")

    if check_only:
        return check_config(repo, production)

    runtime_dir = repo / "runtime_observability"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "tmad_discord_update_controller.log"
    state_path = runtime_dir / "tmad_discord_update_controller.state.json"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )

    logging.info(
        "TMAD Discord update controller process starting | pid=%s repo=%s",
        os.getpid(),
        repo,
    )

    state = load_state(state_path)
    last_id = str(state.get("last_message_id") or "").strip()
    token = ""
    webhook = ""
    channel_id = 0
    credentials_ready = False
    last_error_notice = 0.0
    last_health_log = 0.0
    consecutive_poll_failures = 0

    while True:
        if not credentials_ready:
            try:
                token, webhook, channel_id = get_credentials(production)
                logging.info(
                    "TMAD Discord update controller connected | pid=%s channel=%s repo=%s",
                    os.getpid(),
                    channel_id,
                    repo,
                )
                if not last_id:
                    initial = fetch_messages(token, channel_id)
                    if initial:
                        last_id = str(initial[-1].get("id") or "")
                        if last_id:
                            save_state(state_path, last_id)
                    logging.info("Initial cursor established | last_message_id=%s", last_id or "none")
                credentials_ready = True
                last_health_log = 0.0
            except Exception:
                logging.exception("TMAD controller startup/config error; retrying")
                time.sleep(30)
                continue

        try:
            messages = fetch_messages(token, channel_id, last_id)
            for message in messages:
                message_id = str(message.get("id") or "").strip()
                author = message.get("author") if isinstance(message.get("author"), dict) else {}
                webhook_id = str(message.get("webhook_id") or "").strip()
                is_human_command = not bool(author.get("bot")) and not webhook_id
                is_authorized_webhook_command = webhook_id in AUTHORIZED_UPDATE_WEBHOOK_IDS
                command = normalize_command(str(message.get("content") or "")).lower()
                if command == "%update":
                    source = "human" if is_human_command else ("authorized_webhook" if is_authorized_webhook_command else "other")
                    logging.info(
                        "TMAD %%update command observed | message=%s source=%s webhook_id=%s author_bot=%s accepted=%s",
                        message_id,
                        source,
                        webhook_id or "none",
                        bool(author.get("bot")),
                        bool(is_human_command or is_authorized_webhook_command),
                    )

                if (is_human_command or is_authorized_webhook_command) and command == "%update":
                    try:
                        pid = launch_update(repo)
                        logging.info("TMAD %%update one-shot launched | pid=%s message=%s", pid, message_id)
                    except Exception as exc:
                        logging.exception("TMAD %%update launch failed | message=%s", message_id)
                        try:
                            webhook_send(webhook, f"❌ **No se pudo iniciar la actualización TMAD.** `{str(exc)[:1200]}`")
                        except Exception:
                            pass

                if message_id:
                    last_id = message_id
                    save_state(state_path, last_id)

            consecutive_poll_failures = 0
            now = time.time()
            if now - last_health_log >= HEALTH_LOG_SECONDS:
                logging.info(
                    "TMAD controller heartbeat | pid=%s channel=%s last_message_id=%s",
                    os.getpid(),
                    channel_id,
                    last_id or "none",
                )
                last_health_log = now
            time.sleep(POLL_SECONDS)
        except Exception as exc:
            consecutive_poll_failures += 1
            logging.exception(
                "TMAD controller polling error | consecutive_failures=%s",
                consecutive_poll_failures,
            )
            now = time.time()
            if (
                consecutive_poll_failures >= POLL_ALERT_AFTER_CONSECUTIVE_FAILURES
                and now - last_error_notice > 300
                and webhook
            ):
                try:
                    webhook_send(webhook, f"⚠️ **TMAD control Discord con error.** `{str(exc)[:1200]}`")
                    last_error_notice = now
                except Exception:
                    pass
            # Re-read secrets/channel metadata after any polling failure. This
            # turns transient Discord/network/auth errors into a self-healing
            # reconnect instead of a silent process death.
            credentials_ready = False
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
