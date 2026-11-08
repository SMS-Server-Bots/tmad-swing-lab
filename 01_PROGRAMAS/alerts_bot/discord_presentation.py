from __future__ import annotations

import re
from typing import Iterable


def normalize_message(content: str) -> str:
    """Normalize Discord markdown without changing its meaning."""
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def build_group(
    title: str,
    cards: Iterable[str] = (),
    *,
    context: str | None = None,
    footer: str | None = None,
) -> str:
    """Build one ordered Discord section from complete semantic cards."""
    parts = [f"## {str(title).strip().lstrip('#').strip()}"]
    if context:
        parts.append(str(context).strip())
    clean_cards = [normalize_message(card) for card in cards if normalize_message(card)]
    if clean_cards:
        parts.append("\n\n".join(clean_cards))
    if footer:
        parts.append(str(footer).strip())
    return normalize_message("\n\n".join(parts))


def ensure_heading(content: str, title: str) -> str:
    text = normalize_message(content)
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first.startswith("#"):
        return text
    return build_group(title, [text]) if text else f"## {title}"


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    if len(stripped) > 110 or not stripped:
        return False
    letters = [char for char in stripped if char.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(char.isupper() for char in letters) / len(letters)
    return upper_ratio >= 0.72 or any(icon in stripped for icon in "🚨⚠️✅❌🟥🟡🟢🧪📘📥📊🌙🔁")


def _split_code_block(block: str, limit: int) -> list[str]:
    stripped = block.strip()
    if not (stripped.startswith("```") and stripped.endswith("```")):
        return []
    first_newline = stripped.find("\n")
    opening = stripped[:first_newline] if first_newline >= 0 else "```"
    inner = stripped[first_newline + 1 : -3] if first_newline >= 0 else ""
    room = max(80, limit - len(opening) - 5)
    pieces = [inner[i : i + room] for i in range(0, len(inner), room)] or [""]
    return [f"{opening}\n{piece}\n```" for piece in pieces]


def _split_large_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    code = _split_code_block(block, limit)
    if code:
        return code
    pieces: list[str] = []
    current = ""
    for raw_line in block.splitlines() or [block]:
        line = raw_line
        if len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(line[i : i + limit] for i in range(0, len(line), limit))
            continue
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


DISCORD_HARD_LIMIT = 2000
DISCORD_SAFE_LIMIT = 1950

def semantic_chunks(content: str, max_chars: int = DISCORD_SAFE_LIMIT) -> list[str]:
    """Split Discord content into deterministic semantic pages.

    Detailed operational cards are allowed to span multiple messages. Every
    resulting page repeats the original heading and receives a stable [i/n]
    suffix for ENTER/UPDATE/SETUP HIT/RUNNER/CLOSE messages.
    """
    # Discord content is capped at 2,000 characters. Keep a safety margin so
    # page labels/markdown can never push a webhook payload over the hard cap.
    max_chars = min(int(max_chars or DISCORD_SAFE_LIMIT), DISCORD_SAFE_LIMIT, DISCORD_HARD_LIMIT)
    text = normalize_message(content)
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    lines = text.splitlines()
    header = lines[0].strip() if lines and _looks_like_heading(lines[0]) else ""
    body = "\n".join(lines[1:] if header else lines).strip()
    operational = bool(header and any(token in header.upper() for token in (
        "ENTER", "SETUP GANADO", "OBJETIVO GEX", "UPDATE POSITION",
        "UPDATE RUNNER", "UPDATE DETERIORO", "CLOSE",
    )))

    # Reserve enough room for the longest practical page suffix. Splitting the
    # body first avoids the old "Parte n/?" fallback for one oversized card.
    header_reserve = len(header) + (18 if operational else 28) if header else 0
    body_limit = max(200, max_chars - header_reserve - 2)
    raw_blocks = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    atomic: list[str] = []
    for block in raw_blocks or [body]:
        atomic.extend(_split_large_block(block, body_limit))

    bodies: list[str] = []
    current = ""
    for block in atomic:
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= body_limit:
            current = candidate
        else:
            if current:
                bodies.append(current)
            current = block
    if current:
        bodies.append(current)
    if not bodies:
        bodies = [""]

    # A line-level edge case can still exceed the final allowance. Flatten it
    # before assigning page numbers so every page knows the real total.
    flattened: list[str] = []
    for chunk_body in bodies:
        flattened.extend(_split_large_block(chunk_body, body_limit))
    bodies = flattened or [""]

    total = len(bodies)
    chunks: list[str] = []
    for index, chunk_body in enumerate(bodies, start=1):
        if header:
            if operational:
                prefix = f"{header} [{index}/{total}]" if total > 1 else header
            else:
                prefix = header + (f"\n*Parte {index}/{total}*" if total > 1 else "")
            chunk = f"{prefix}\n\n{chunk_body}".strip()
        else:
            chunk = chunk_body.strip()
        if len(chunk) > max_chars:
            # Defensive final split; recompute local suffixes and never cut a
            # message silently above Discord's limit.
            allowance = max(100, max_chars - (len(header) + 24 if header else 0))
            pieces = _split_large_block(chunk_body, allowance)
            local_total = len(pieces)
            for local_index, piece in enumerate(pieces, start=1):
                if header and operational:
                    local_prefix = f"{header} [{local_index}/{local_total}]"
                elif header:
                    local_prefix = header + (f"\n*Parte {local_index}/{local_total}*" if local_total > 1 else "")
                else:
                    local_prefix = ""
                chunks.append(f"{local_prefix}\n\n{piece}".strip() if local_prefix else piece)
        else:
            chunks.append(chunk)
    return chunks


def signal_source_label(payload: dict | None = None, *, rule_name: str | None = None, feature_tier: str | None = None) -> str:
    """Return the exact discovery route shown beside every ticker.

    The label is intentionally explicit. CORE+GEX is not collapsed to CORE,
    and graph propagation is presented as EXPANSIÓN so every Discord alert can
    be traced back to the engine that originated it.
    """
    data = payload or {}
    origin = str(data.get("signal_origin") or "").upper()
    rule = str(rule_name or data.get("rule_name") or "").upper()
    tier = str(feature_tier or data.get("feature_tier") or "").upper()

    labels = {
        "CORE_GEX_ALIGNED": "CORE+GEX",
        "CORE_GEX_DIVERGENCE": "CORE↔GEX",
        "GEX_STANDALONE": "GEX",
        "CORE_DIRECTIONAL": "CORE",
        "GRAPH_PROPAGATION": "EXPANSIÓN",
        "GEX_TIMING": "GEX-TIMING",
    }
    if origin in labels:
        return labels[origin]
    if "CORE_GEX_ALIGNED" in origin or "CORE_GEX_ALIGNED" in rule:
        return "CORE+GEX"
    if "CORE_GEX_DIVERGENCE" in origin or "CORE_GEX_DIVERGENCE" in rule:
        return "CORE↔GEX"
    if "GEX_STANDALONE" in origin or "GEX_STANDALONE" in rule:
        return "GEX"
    if origin == "GRAPH_PROPAGATION" or "GRAPH_PROPAGATION" in rule:
        return "EXPANSIÓN"
    if origin.startswith("CORE_") or rule.startswith("CORE_"):
        return "CORE"

    contributors = [str(item).upper() for item in (data.get("contributor_engines") or [])]
    if "CORE" in contributors and "GEX" in contributors:
        return "CORE+GEX"
    if "GEX" in contributors:
        return "GEX"
    if "EXPANSION" in contributors or "EXPANSIÓN" in contributors or "GRAPH" in contributors:
        return "EXPANSIÓN"
    if data.get("core_rule") and str(data.get("target_source") or "").upper().startswith("GEX"):
        return "CORE+GEX"
    if data.get("core_rule") or data.get("probability_pct") is not None or data.get("probability_source"):
        return "CORE"
    target_source = str(data.get("target_source") or "").upper()
    if target_source.startswith("GEX") or tier == "FULL" or origin.startswith("GEX"):
        return "GEX"
    return "CORE" if tier in {"CORE", "BOOTSTRAP", ""} else tier


def signal_model_lineage(
    payload: dict | None = None,
    *,
    model_id: object | None = None,
    model_key: object | None = None,
) -> str:
    """Human-readable engine/model lineage persisted and shown in Discord."""
    data = payload or {}
    lineage = data.get("model_lineage") if isinstance(data.get("model_lineage"), dict) else {}
    source = signal_source_label(data)
    core_id = lineage.get("core_model_id") or data.get("core_model_id") or model_id
    core_key = lineage.get("core_model_key") or data.get("core_model_key") or model_key
    core_status = str(lineage.get("core_model_status") or data.get("core_model_status") or "").upper()
    gex_status = str(lineage.get("gex_daily_status") or data.get("gex_daily_model_status") or data.get("model_status") or "").upper()
    timing_status = str(lineage.get("timing_status") or data.get("gex_intraday_model_status") or "").upper()
    graph_status = str(lineage.get("expansion_status") or data.get("graph_model_status") or "").upper()
    graph_source = str(data.get("graph_source") or lineage.get("expansion_type") or "").upper()

    parts: list[str] = []
    if "CORE" in source:
        core = "CORE"
        if core_id not in (None, "", 0, "0"):
            core += f" #{core_id}"
        if core_status:
            core += f" {core_status}"
        parts.append(core)
    if "GEX" in source:
        parts.append("GEX" + (f" {gex_status}" if gex_status else ""))
        if timing_status:
            parts.append(f"TIMING {timing_status}")
    if source == "EXPANSIÓN" or graph_source or "EXPANSION" in [str(x).upper() for x in (data.get("contributor_engines") or [])]:
        expansion = "EXPANSIÓN"
        if graph_status:
            expansion += f" {graph_status}"
        if graph_source:
            expansion += f" ({graph_source})"
        parts.append(expansion)
    if not parts and core_key:
        parts.append(str(core_key))
    return " · ".join(dict.fromkeys(parts)) or source


def ticker_source_label(
    ticker: object,
    payload: dict | None = None,
    *,
    source: str | None = None,
    rule_name: str | None = None,
    feature_tier: str | None = None,
) -> str:
    """Discord ticker label required across Alerts surfaces."""
    symbol = str(ticker or "N/A").upper().lstrip("$")
    label = str(source or signal_source_label(payload, rule_name=rule_name, feature_tier=feature_tier)).upper()
    return f"**${symbol}** [{label}]"

STATE_DISPLAY = {
    "ENTER": "ENTER",
    "READY": "READY",
    "RETEST": "RETEST",
    "EARLY": "EARLY",
    "DISCOVERED": "DESCUBIERTA",
    "WATCH": "WATCH",
    "DIVERGENCE": "DIVERGENCIA",
    "CAPPED": "LIMITADA",
    "EXTENDED": "EXTENDIDA",
    "MARKET_BLOCKED": "BLOQUEADA",
    "WAIT": "EN ESPERA",
}


def state_display(value: object) -> str:
    raw = str(value or "WATCH").upper()
    return STATE_DISPLAY.get(raw, raw.replace("_", " "))


def ticker_bullet_header(
    ticker: object,
    payload: dict | None = None,
    *,
    source: str | None = None,
    rule_name: str | None = None,
    feature_tier: str | None = None,
    side: str | None = None,
    state: str | None = None,
    icon: str | None = None,
) -> str:
    """Uniform bullet-first ticker header used on every Alerts surface."""
    prefix = "•" + (f" {icon}" if icon else "")
    parts = [f"{prefix} {ticker_source_label(ticker, payload, source=source, rule_name=rule_name, feature_tier=feature_tier)}"]
    if side:
        parts.append(f"**{str(side).upper()}**")
    if state:
        parts.append(f"`{state_display(state)}`")
    return " · ".join(parts)



def fmt_int_es(value: object) -> str:
    try:
        return f"{int(value or 0):,}".replace(",", ".")
    except Exception:
        return "0"


def status_display(value: object) -> str:
    raw = str(value or "").strip().upper()
    labels = {
        "CHAMPION": "VALIDADO",
        "PROVISIONAL_CHAMPION": "PROVISIONAL",
        "SCHEDULED_CHAMPION": "VALIDADO PROGRAMADO",
        "CHALLENGER": "CANDIDATO",
        "DISCOVERY_SHADOW_ACTIVE": "DESCUBRIMIENTO SHADOW",
        "ACCUMULATING": "ACUMULANDO",
        "VALIDATED": "VALIDADO",
        "SHADOW_READY": "SHADOW LISTO",
        "LIVE_READY": "LIVE LISTO",
        "WAITING_DATA": "ESPERANDO DATA",
        "BUILDING_CORE": "CONSTRUYENDO CORE",
        "CALIBRATING": "CALIBRANDO",
        "ERROR": "BLOQUEADO",
        "REST": "DESCANSO",
        "OFF": "BLOQUEADO",
        "SHADOW": "SHADOW",
        "LIVE": "LIVE",
        "UNKNOWN": "PENDIENTE",
    }
    return labels.get(raw, raw.replace("_", " ") or "PENDIENTE")


def gex_intent_display(value: object) -> str:
    raw = str(value or "").strip().upper()
    labels = {
        "SQUEEZE": "COMPRESIÓN",
        "HEDGING": "COBERTURA",
        "UNCLEAR": "NO CONFIRMADO",
        "DIRECTIONAL": "DIRECCIONAL",
        "ACCUMULATION": "ACUMULACIÓN",
        "CALL_ACCUMULATION": "ACUMULACIÓN CALL",
        "PUT_ACCUMULATION": "ACUMULACIÓN PUT",
    }
    return labels.get(raw, raw.replace("_", " ") or "PENDIENTE")

def shown_count_line(total: int, shown: int) -> str:
    total_i = max(0, int(total or 0))
    shown_i = max(0, min(int(shown or 0), total_i))
    return f"**Mostradas:** `{shown_i}` de `{total_i}` · ordenadas por prioridad"

