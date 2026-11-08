from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import re
import sys
import time
import httpx

import config
from time_utils import now_madrid
from discord_presentation import ensure_heading, normalize_message, semantic_chunks

_DISCORD_MAX_ATTEMPTS = 3
_DISCORD_TRANSIENT_HTTP_STATUS = {502, 503, 504}
_DISCORD_TRANSIENT_DELAYS = (1.0, 3.0, 7.0)
_DISCORD_CHANNEL_NAMES = {
    "system_status": "tmad-system-status",
    "data_log": "tmad-data-log",
    "data_error": "tmad-data-error",
    "data_quality": "tmad-data-quality",
}


def _sync_runtime_version() -> None:
    """Use VERSION.txt as deployment source of truth without touching persistent config."""
    try:
        version = (Path(__file__).resolve().parents[2] / "VERSION.txt").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", version):
            config.BOT_VERSION = version
    except Exception:
        pass


_sync_runtime_version()


def _env_url(*names: str) -> str:
    """Resolve same-purpose aliases only. Cross-channel fallback is forbidden."""
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
        except Exception:
            pass
    try:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), flush=True)
    except Exception:
        pass


def _append_local(kind: str, content: str) -> None:
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = config.LOG_DIR / f"tmad_data_{kind}_{now_madrid().strftime('%Y-%m-%d')}.log"
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
                          status: str, content: str, attempts: int = 1, error: str = "") -> None:
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = config.LOG_DIR / f"tmad_data_discord_audit_{now_madrid().strftime('%Y-%m-%d')}.log"
        item = {
            "timestamp_madrid": now_madrid().isoformat(),
            "source_bot": "data",
            "discord_channel": _discord_channel_name(route),
            "route": route,
            "delivery_id": delivery_id,
            "chunk_index": int(chunk_index),
            "chunk_count": int(chunk_count),
            "status": str(status or "").upper(),
            "transport": "webhook",
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

def _send(webhook: str, content: str, *, channel: str) -> bool:
    content = content or ""
    delivery_id = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    chunks = semantic_chunks(content, config.MAX_DISCORD_MESSAGE_CHARS)
    if not webhook:
        _append_local("discord_delivery_error", f"MISSING_WEBHOOK channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id}")
        for index, chunk in enumerate(chunks, start=1):
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="ERROR", content=chunk, error="MISSING_WEBHOOK")
        _safe_print(f"🟥 Discord sin webhook para {_discord_channel_name(channel)} · delivery_id={delivery_id}")
        return False
    success = True
    for index, chunk in enumerate(chunks, start=1):
        response = None
        attempts = 0
        try:
            while attempts < _DISCORD_MAX_ATTEMPTS:
                attempts += 1
                response = httpx.post(webhook, json={"content": chunk}, timeout=10)
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
            _append_local(
                "discord_outbox",
                f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} status=OK attempts={attempts}\n{chunk}",
            )
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="OK", content=chunk, attempts=attempts)
        except Exception as exc:
            success = False
            safe_error = _safe_delivery_error(exc, response)
            _append_local(
                "discord_outbox",
                f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} status=ERROR attempts={attempts} error={safe_error}\n{chunk}",
            )
            _append_local(
                "discord_delivery_error",
                f"channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunk={index}/{len(chunks)} attempts={attempts} {safe_error}",
            )
            _append_discord_audit(route=channel, delivery_id=delivery_id, chunk_index=index, chunk_count=len(chunks), status="ERROR", content=chunk, attempts=attempts, error=safe_error)
    if success:
        _append_local("discord_delivery", f"OK channel={channel} discord_channel={_discord_channel_name(channel)} id={delivery_id} chunks={len(chunks)}")
    return success


def _is_system_message(content: str) -> bool:
    upper = str(content or "").upper()
    markers = (
        "DATA · ARRANQUE",
        "DATA → ALERTS",
        "[DATA HEALTH]",
        "[DATA HEARTBEAT]",
        "COORDINATION",
        "HEARTBEAT",
    )
    return any(marker in upper for marker in markers)


def log_info(content: str, send_discord: bool = True) -> bool:
    """Persist granular telemetry locally; route only semantic cards to Discord."""
    _append_local("log", content)
    if not send_discord:
        return False
    if _is_system_message(content):
        return _send(
            _env_url("SYSTEM_STATUS_WEBHOOK", "ALERTS_STATUS_WEBHOOK", "DISCORD_WEBHOOK_URL_SYSTEM_STATUS"),
            content,
            channel="system_status",
        )
    return _send(
        _env_url("DATA_LOG_WEBHOOK", "DISCORD_WEBHOOK_URL_DATA_LOG", "WEBHOOK_DATA_LOG"),
        content,
        channel="data_log",
    )


def _discord_error_summary(content: str, limit: int = 900) -> str:
    text = normalize_message(content)
    text = re.sub(r"```.*?```", "", text, flags=re.S).strip()
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(("File ", "Traceback", "During handling"))]
    compact = "\n".join(lines[:8]).strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def log_error(content: str) -> bool:
    full = normalize_message(content)
    formatted = ensure_heading(_discord_error_summary(full), "🟥 DATA · ERROR")
    _safe_print(formatted)
    if full and full.strip() != formatted.strip():
        _safe_print("🧾 [DATA ERROR DETAIL · LOCAL CONSOLE]\n" + full[-7000:] + "\n🧾 [END DATA ERROR DETAIL]")
    _append_local("error", full)
    return _send(
        _env_url("DATA_ERROR_WEBHOOK", "DISCORD_WEBHOOK_URL_DATA_ERROR", "WEBHOOK_DATA_ERROR"),
        formatted,
        channel="data_error",
    )


def send_quality(content: str, *, heading: str = "🧪 DATA · QUALITY") -> bool:
    formatted = ensure_heading(content, heading)
    _safe_print(formatted)
    _append_local("quality", formatted)
    return _send(
        _env_url(
            "DATA_QUALITY_WEBHOOK", "DATA_SCHEMA_WEBHOOK",
            "DISCORD_WEBHOOK_URL_DATA_QUALITY", "DISCORD_WEBHOOK_URL_DATA_SCHEMA",
            "WEBHOOK_DATA_QUALITY", "WEBHOOK_DATA_SCHEMA",
        ),
        formatted,
        channel="data_quality",
    )


def log_schema(content: str) -> bool:
    """Schema drift is a Data Quality concern, never a generic Data Log fallback."""
    formatted = ensure_heading(content, "🧩 DATA · CAMBIO DE ESQUEMA")
    _append_local("schema", formatted)
    return _send(
        _env_url(
            "DATA_QUALITY_WEBHOOK", "DATA_SCHEMA_WEBHOOK",
            "DISCORD_WEBHOOK_URL_DATA_QUALITY", "DISCORD_WEBHOOK_URL_DATA_SCHEMA",
            "WEBHOOK_DATA_QUALITY", "WEBHOOK_DATA_SCHEMA",
        ),
        formatted,
        channel="data_quality",
    )
