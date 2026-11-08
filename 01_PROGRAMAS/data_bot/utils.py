import hashlib
import json
from typing import Any


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def side_from_signal(signal: str | None) -> str | None:
    if not signal:
        return None
    s = str(signal).lower()
    if "call" in s or "bull" in s or "buy" in s:
        return "CALL"
    if "put" in s or "bear" in s or "sell" in s:
        return "PUT"
    return None


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(obj: Any) -> str:
    return hashlib.sha256(json_dumps(obj).encode("utf-8")).hexdigest()


def flatten_json(obj: Any, prefix: str = "") -> dict[str, Any]:
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(flatten_json(v, key))
    elif isinstance(obj, list):
        # Registry de estructura: no guardar todos los índices. Usamos [] para marcar arrays.
        if not obj:
            out[f"{prefix}[]"] = []
        else:
            out[f"{prefix}[]"] = []
            # Muestreo de primeros elementos para detectar campos internos.
            for item in obj[:3]:
                out.update(flatten_json(item, f"{prefix}[]"))
    else:
        out[prefix] = obj
    return out


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "str"


def rows_from_scanner_payload(payload: dict) -> list[dict]:
    data = payload.get("data", {}) if payload else {}
    rows = data.get("rows") or []
    return rows if isinstance(rows, list) else []


def ticker_from_row(row: dict) -> str | None:
    for key in ("ticker", "symbol", "name"):
        val = row.get(key)
        if val:
            return str(val).strip().upper()
    return None
