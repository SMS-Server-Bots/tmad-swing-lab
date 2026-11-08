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
    """Split on cards/paragraphs and repeat the title; never cut blindly mid-card."""
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
    header_budget = len(header) + 24 if header else 0
    body_limit = max(200, max_chars - header_budget)
    raw_blocks = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    blocks: list[str] = []
    for block in raw_blocks:
        blocks.extend(_split_large_block(block, body_limit))

    bodies: list[str] = []
    current = ""
    for block in blocks:
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
        bodies = _split_large_block(body, body_limit)

    total = len(bodies)
    chunks: list[str] = []
    for index, chunk_body in enumerate(bodies, start=1):
        if header:
            page = f"*Parte {index}/{total}*" if total > 1 else ""
            prefix = header + (f"\n{page}" if page else "")
            chunk = f"{prefix}\n\n{chunk_body}".strip()
        else:
            chunk = chunk_body.strip()
        # The pagination prefix may consume a few extra characters.
        if len(chunk) > max_chars:
            overflow_limit = max(100, max_chars - (len(prefix) + 2 if header else 0))
            for piece in _split_large_block(chunk_body, overflow_limit):
                page = f"*Parte {len(chunks)+1}/?*" if header else ""
                chunks.append(f"{header}\n{page}\n\n{piece}".strip() if header else piece)
        else:
            chunks.append(chunk)
    return chunks
