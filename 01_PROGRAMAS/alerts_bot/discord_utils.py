from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import re
import sys
import time
import requests
import config
from time_utils import now_madrid
from discord_presentation import ensure_heading, normalize_message, semantic_chunks

_DISCORD_API = "https://discord.com/api/v10"
_ALPHA_CHANNEL_CACHE = 0
_DEFAULT_ALPHA_OUTPUT_CHANNEL_ID = 1542744485182701609
_DISCORD_MAX_ATTEMPTS = 3
_DISCORD_TRANSIENT_HTTP_STATUS = {502, 503, 504}
_DISCORD_TRANSIENT_DELAYS = (1.0, 3.0, 7.0)
_DISCORD_CHANNEL_NAMES = {
    "system_status": "tmad-system-status",
    "alerts_log": "tmad-alerts-log",
    "alerts_error": "tmad-alerts-error",
    "alerts_live": "tmad-alerts-live",
    "alerts_shadow": "tmad-alerts-shadow",
    "alerts_positions": "tmad-alerts-positions",
    "alerts_research": "tmad-alerts-research",
    "alpha_output": "tmad-alpha-output",
    "model_learning": "tmad-model-learning",
    "model_calibration": "tmad-model-calibration",
}


def _sync_runtime_version() -> None:
    try:
        version = (Path(__file__).resolve().parents[2] / "VERSION.txt").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", version):
            config.BOT_VERSION = version
    except Exception:
        pass


_sync_runtime_version()


def _env_url(*names: str) -> str:
    if bool(getattr(config, "VALIDATION_MODE", False)):
        return ""
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _env_value(*names: str) -> str:
    if bool(getattr(config, "VALIDATION_MODE", False)):
        return ""
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _safe_print(content: str) -> None:
    text = str(content or "")
    try:
        print(text, flush=True)
        return
    except UnicodeEncodeError:
        pass
    stream = getattr(sys, "stdout", None)
    if stream is not None and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            print(text, flush=True)
            return
        except (AttributeError, OSError, ValueError, UnicodeEncodeError):
            pass
    try:
        encoding = getattr(stream, "encoding", None) or "ascii"
        safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)
    except Exception:
        pass


def _append_local(kind: str, content: str) -> None:
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = config.LOG_DIR / f"tmad_alerts_{kind}_{now_madrid().strftime('%Y-%m-%d')}.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_madrid().isoformat()}] {content}\n")
    except Exception:
        pass


def _discord_channel_name(route: str) -> str:
    return _DISCORD_CHANNEL_NAMES.get(str(route or ""), str(route or "unknown"))


def _audit_title(content: str) -> str:
    for line in str(content or "").splitlines():
        value = line.strip()
        if value:
            return value[:180]
    return ""


def _safe_delivery_error(exc: Exception, response=None) -> str:
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"{type(exc).__name__}: HTTP {status}"
    return type(exc).__name__


def _append_discord_audit(*, route: str, delivery_id: str, chunk_index: int, chunk_count: int,
                          status: str, content: str, transport: str = "webhook", attempts: int = 1,
                          error: str = "") -> None:
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = config.LOG_DIR / f"tmad_alerts_discord_audit_{now_madrid().strftime('%Y-%m-%d')}.log"
        item = {
            "timestamp_madrid": now_madrid().isoformat(),
            "source_bot": "alerts",
            "discord_channel": _discord_channel_name(route),
            "route": route,
            "delivery_id": delivery_id,
            "chunk_index": int(chunk_index),
            "chunk_count": int(chunk_count),
            "status": str(status or "").upper(),
            "transport": transport,
            "attempts": int(attempts),
            "title": _audit_title(content),
            "content": str(content or ""),
        }
        if error:
            item["error"] = str(error)[:240]
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _retry_after_seconds(response, attempt: int) -> float:
    values = []
    try:
        values.append(float(response.headers.get("Retry-After") or 0))
    except Exception:
        pass
    try:
        payload = response.json()
        values.append(float(payload.get("retry_after") or 0))
    except Exception:
        pass
    positive = [x for x in values if x > 0]
    delay = max(positive) if positive else 0.75 * max(1, attempt)
    return max(0.25, min(delay, 5.0))


def _transient_retry_seconds(attempt: int) -> float:
    index = max(0, min(int(attempt) - 1, len(_DISCORD_TRANSIENT_DELAYS) - 1))
    return float(_DISCORD_TRANSIENT_DELAYS[index])

def _send(url: str, content: str, *, channel: str) -> bool:
    content = content or ""
    delivery_id = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    chunks = semantic_chunks(content, config.MAX_DISCORD_MESSAGE_CHARS)
    if not url:
        _append_local("discord_delivery_error", f"MISSING_WEBHOOK channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id}")
        for index, chunk in enumerate(chunks, start=1):
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="ERROR", content=chunk, error="MISSING_WEBHOOK")
        return False
    success = True
    for index, chunk in enumerate(chunks, start=1):
        response = None
        attempts = 0
        try:
            while attempts < _DISCORD_MAX_ATTEMPTS:
                attempts += 1
                response = requests.post(url, json={"content": chunk}, timeout=10)
                if response.status_code == 429 and attempts < _DISCORD_MAX_ATTEMPTS:
                    delay = _retry_after_seconds(response, attempts)
                    _append_local("discord_delivery_retry", f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} attempt={attempts} http=429 retry_in={delay:.2f}s")
                    time.sleep(delay)
                    continue
                if response.status_code in _DISCORD_TRANSIENT_HTTP_STATUS and attempts < _DISCORD_MAX_ATTEMPTS:
                    delay = _transient_retry_seconds(attempts)
                    _append_local("discord_delivery_retry", f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} attempt={attempts} http={response.status_code} retry_in={delay:.2f}s")
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                break
            _append_local("discord_outbox", f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} status=OK attempts={attempts}\n{chunk}")
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="OK", content=chunk, attempts=attempts)
        except Exception as exc:
            success = False
            safe_error = _safe_delivery_error(exc, response)
            _append_local("discord_outbox", f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} status=ERROR attempts={attempts} error={safe_error}\n{chunk}")
            _append_local("discord_delivery_error", f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} attempts={attempts} {safe_error}")
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="ERROR", content=chunk, attempts=attempts, error=safe_error)
    if success:
        _append_local("discord_delivery", f"OK channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunks={len(chunks)}")
    return success


def _resolve_alpha_output_channel_id() -> int:
    global _ALPHA_CHANNEL_CACHE
    if _ALPHA_CHANNEL_CACHE > 0:
        return _ALPHA_CHANNEL_CACHE
    explicit = _env_value("TMAD_ALPHA_OUTPUT_CHANNEL_ID", "ALPHA_OUTPUT_CHANNEL_ID")
    if explicit.isdigit():
        _ALPHA_CHANNEL_CACHE = int(explicit)
        return _ALPHA_CHANNEL_CACHE
    # Stable deployment fallback for the dedicated TMAD Alpha channel.
    # Environment configuration still has priority; no webhook secret is stored in source.
    if _DEFAULT_ALPHA_OUTPUT_CHANNEL_ID > 0:
        _ALPHA_CHANNEL_CACHE = _DEFAULT_ALPHA_OUTPUT_CHANNEL_ID
        return _ALPHA_CHANNEL_CACHE
    token = _env_value("DISCORD_TOKEN", "EXECUTION_BOT_TOKEN", "DISCORD_BOT_TOKEN")
    if not token:
        return 0
    seed_webhook = _env_url(
        "EXECUTION_WEBHOOK", "ALERTS_LIVE_WEBHOOK", "DISCORD_WEBHOOK_URL_ALERTS",
        "SYSTEM_STATUS_WEBHOOK", "ALERTS_LOG_WEBHOOK",
    )
    if not seed_webhook:
        return 0
    try:
        meta = requests.get(seed_webhook, timeout=10).json()
        guild_id = str(meta.get("guild_id") or "")
        if not guild_id:
            return 0
        response = requests.get(
            f"{_DISCORD_API}/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {token}", "User-Agent": "TMAD-Alpha-Output/1.0"},
            timeout=10,
        )
        response.raise_for_status()
        target_name = (_env_value("TMAD_ALPHA_OUTPUT_CHANNEL_NAME") or "tmad-alpha-output").strip().lower()
        matches = [row for row in response.json() if str(row.get("name") or "").strip().lower() == target_name]
        if len(matches) != 1:
            _append_local("discord_delivery_error", f"ALPHA_CHANNEL_RESOLUTION name={target_name} matches={len(matches)}")
            return 0
        _ALPHA_CHANNEL_CACHE = int(matches[0].get("id") or 0)
        return _ALPHA_CHANNEL_CACHE
    except Exception as exc:
        _append_local("discord_delivery_error", f"ALPHA_CHANNEL_RESOLUTION {type(exc).__name__}")
        return 0


def _send_bot_channel(channel_id: int, content: str, *, channel: str) -> bool:
    token = _env_value("DISCORD_TOKEN", "EXECUTION_BOT_TOKEN", "DISCORD_BOT_TOKEN")
    chunks = semantic_chunks(content or "", config.MAX_DISCORD_MESSAGE_CHARS)
    delivery_id = hashlib.sha1((content or "").encode("utf-8", errors="replace")).hexdigest()[:12]
    if not token or channel_id <= 0:
        for index, chunk in enumerate(chunks, start=1):
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="ERROR", content=chunk, transport="bot", error="MISSING_BOT_ROUTE")
        return False
    success = True
    for index, chunk in enumerate(chunks, start=1):
        response = None
        attempts = 0
        try:
            while attempts < _DISCORD_MAX_ATTEMPTS:
                attempts += 1
                response = requests.post(
                    f"{_DISCORD_API}/channels/{channel_id}/messages",
                    headers={"Authorization": f"Bot {token}", "User-Agent": "TMAD-Alpha-Output/1.0"},
                    json={"content": chunk},
                    timeout=10,
                )
                if response.status_code == 429 and attempts < _DISCORD_MAX_ATTEMPTS:
                    delay = _retry_after_seconds(response, attempts)
                    _append_local("discord_delivery_retry", f"channel={channel} discord_channel={_discord_channel_name(channel)} transport=bot id={delivery_id} chunk={index}/{len(chunks)} attempt={attempts} http=429 retry_in={delay:.2f}s")
                    time.sleep(delay)
                    continue
                if response.status_code in _DISCORD_TRANSIENT_HTTP_STATUS and attempts < _DISCORD_MAX_ATTEMPTS:
                    delay = _transient_retry_seconds(attempts)
                    _append_local("discord_delivery_retry", f"channel={channel} discord_channel={_discord_channel_name(channel)} transport=bot id={delivery_id} chunk={index}/{len(chunks)} attempt={attempts} http={response.status_code} retry_in={delay:.2f}s")
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                break
            _append_local("discord_outbox", f"channel={channel} discord_channel={_discord_channel_name(channel)} transport=bot id={delivery_id} chunk={index}/{len(chunks)} status=OK attempts={attempts}\n{chunk}")
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="OK", content=chunk, transport="bot", attempts=attempts)
        except Exception as exc:
            success = False
            safe_error = _safe_delivery_error(exc, response)
            _append_local("discord_outbox", f"channel={channel} discord_channel={_discord_channel_name(channel)} transport=bot id={delivery_id} chunk={index}/{len(chunks)} status=ERROR attempts={attempts} error={safe_error}\n{chunk}")
            _append_local("discord_delivery_error", f"channel={channel} discord_channel={_discord_channel_name(channel)} transport=bot id={delivery_id} chunk={index}/{len(chunks)} attempts={attempts} {safe_error}")
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="ERROR", content=chunk, transport="bot", attempts=attempts, error=safe_error)
    return success


def _is_system_message(msg: str) -> bool:
    upper = str(msg or "").upper()
    markers = ("ALERTS · ARRANQUE", "DATA → ALERTS", "[ALERTS SYNC]", "[ALERTS STATE]", "[ALERTS HEARTBEAT]", "[ALERTS GENERATION START]", "[ALERTS GENERATION END]", "COORDINATION", "HEARTBEAT")
    return any(marker in upper for marker in markers)


def log_info(msg: str, send_discord: bool = True) -> bool:
    _safe_print(msg); _append_local("log", msg)
    if not send_discord:
        return False
    if _is_system_message(msg):
        return _send(_env_url("SYSTEM_STATUS_WEBHOOK", "ALERTS_STATUS_WEBHOOK", "DISCORD_WEBHOOK_URL_SYSTEM_STATUS"), msg, channel="system_status")
    return _send(_env_url("ALERTS_LOG_WEBHOOK", "DISCORD_WEBHOOK_URL_ALERTS_LOG", "WEBHOOK_ALERTS_LOG"), msg, channel="alerts_log")


def _discord_error_summary(msg: str, limit: int = 900) -> str:
    text = normalize_message(msg)
    text = re.sub(r"```.*?```", "", text, flags=re.S).strip()
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(("File ", "Traceback", "During handling"))]
    compact = "\n".join(lines[:8]).strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def log_warning(msg: str) -> bool:
    formatted = ensure_heading(msg, "🟧 ALERTS · AVISO")
    _safe_print(formatted); _append_local("warning", formatted)
    return _send(_env_url("ALERTS_LOG_WEBHOOK", "DISCORD_WEBHOOK_URL_ALERTS_LOG", "WEBHOOK_ALERTS_LOG"), formatted, channel="alerts_log")


def log_error(msg: str) -> bool:
    full = normalize_message(msg); formatted = ensure_heading(_discord_error_summary(full), "🟥 ALERTS · ERROR")
    _safe_print(formatted)
    if full and full.strip() != formatted.strip():
        _safe_print("🧾 [ALERTS ERROR DETAIL · LOCAL CONSOLE]\n" + full[-7000:] + "\n🧾 [END ALERTS ERROR DETAIL]")
    _append_local("error", full)
    return _send(_env_url("ALERTS_ERROR_WEBHOOK", "DISCORD_WEBHOOK_URL_ALERTS_ERROR", "WEBHOOK_ALERTS_ERROR"), formatted, channel="alerts_error")


def send_alert(msg: str) -> bool:
    _safe_print(msg); _append_local("live_alerts", msg)
    return _send(_env_url("ALERTS_LIVE_WEBHOOK", "DISCORD_WEBHOOK_URL_ALERTS", "WEBHOOK_ALERTS_LIVE"), msg, channel="alerts_live")


def send_shadow(msg: str) -> bool:
    _safe_print(msg); _append_local("shadow_alerts", msg)
    return _send(_env_url("ALERTS_SHADOW_WEBHOOK", "DISCORD_WEBHOOK_URL_ALERTS_SHADOW", "WEBHOOK_ALERTS_SHADOW"), msg, channel="alerts_shadow")


def send_status(msg: str) -> bool:
    _safe_print(msg); _append_local("positions", msg)
    return _send(_env_url("ALERTS_POSITIONS_WEBHOOK", "DISCORD_WEBHOOK_URL_ALERTS_POSITIONS", "WEBHOOK_ALERTS_POSITIONS"), msg, channel="alerts_positions")


def send_preliminary_alert(msg: str) -> bool:
    _safe_print(msg); _append_local("preliminary_alerts", msg)
    return _send(_env_url("ALERTS_RESEARCH_WEBHOOK", "DISCORD_WEBHOOK_URL_RESEARCH", "WEBHOOK_ALERTS_RESEARCH"), msg, channel="alerts_research")


def send_research(msg: str) -> bool:
    _safe_print(msg); _append_local("research", msg)
    return _send(_env_url("ALERTS_RESEARCH_WEBHOOK", "DISCORD_WEBHOOK_URL_RESEARCH", "WEBHOOK_ALERTS_RESEARCH"), msg, channel="alerts_research")


def send_alpha_output(msg: str) -> bool:
    """Unified user-facing Alpha product, isolated from Research/Learning/Live."""
    _safe_print(msg); _append_local("alpha_output", msg)
    dedicated = _env_url(
        "TMAD_ALPHA_OUTPUT_WEBHOOK", "ALPHA_OPTIONS_WEBHOOK", "EOD_LIST_WEBHOOK",
        "DISCORD_WEBHOOK_URL_ALPHA_OUTPUT", "DISCORD_WEBHOOK_URL_ALPHA_OPTIONS", "WEBHOOK_ALPHA_OPTIONS",
    )
    if dedicated:
        return _send(dedicated, msg, channel="alpha_output")
    channel_id = _resolve_alpha_output_channel_id()
    if channel_id > 0:
        return _send_bot_channel(channel_id, msg, channel="alpha_output")
    delivery_id = hashlib.sha1((msg or "").encode("utf-8", errors="replace")).hexdigest()[:12]
    chunks = semantic_chunks(msg or "", config.MAX_DISCORD_MESSAGE_CHARS)
    for index, chunk in enumerate(chunks, start=1):
        _append_discord_audit(route="alpha_output", delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="ERROR", content=chunk, transport="unresolved", error="MISSING_ALPHA_OUTPUT_ROUTE")
    _append_local("discord_delivery_error", "MISSING_ALPHA_OUTPUT_ROUTE webhook=none bot_channel=unresolved")
    return False


def _is_learning_audit_message(msg: str) -> bool:
    upper = str(msg or "").upper()
    markers = ("VALIDACIÓN PROSPECTIVA", "MARKET OUTCOME AUDITOR", "APRENDIZAJE ADAPTATIVO", "LEARNING ", "DRIFT ", "PROMOTION ", "AUTO ROLLBACK", "CHALLENGER FACTORY")
    return any(marker in upper for marker in markers)


def _normalize_report_version(msg: str) -> str:
    return re.sub(r"v6\.1\.25\b", f"v{getattr(config, 'BOT_VERSION', '6.1.26')}", str(msg or ""))


def send_learning_audit(msg: str) -> bool:
    msg = _normalize_report_version(msg)
    _safe_print(msg); _append_local("model_learning", msg)
    return _send(_env_url("MODEL_LEARNING_WEBHOOK", "LEARNING_AUDIT_WEBHOOK", "DISCORD_WEBHOOK_URL_MODEL_LEARNING", "DISCORD_WEBHOOK_URL_LEARNING_AUDIT"), msg, channel="model_learning")


def send_daily(msg: str) -> bool:
    if _is_learning_audit_message(msg):
        return send_learning_audit(msg)
    _safe_print(msg); _append_local("model_calibration", msg)
    return _send(_env_url("MODEL_CALIBRATION_WEBHOOK", "DAILY_CALIBRATION_WEBHOOK", "DISCORD_WEBHOOK_URL_MODEL_CALIBRATION", "DISCORD_WEBHOOK_URL_DAILY", "WEBHOOK_CALIBRATION"), msg, channel="model_calibration")


def send_setup_report(msg: str) -> bool:
    _safe_print(msg); _append_local("setup_reports", msg)
    return _send(_env_url("ALERTS_RESEARCH_WEBHOOK", "DISCORD_WEBHOOK_URL_RESEARCH", "WEBHOOK_ALERTS_RESEARCH"), msg, channel="alerts_research")
