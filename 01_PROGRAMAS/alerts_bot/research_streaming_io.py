from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def atomic_stream_json(path: Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, prefix=target.name + '.tmp.', delete=False) as handle:
            tmp = Path(handle.name)
            encoder = json.JSONEncoder(ensure_ascii=False, default=str)
            buffer = []
            chars = 0
            for part in encoder.iterencode(payload):
                buffer.append(part)
                chars += len(part)
                if chars >= 262144:
                    handle.write(''.join(buffer))
                    buffer.clear()
                    chars = 0
            if buffer:
                handle.write(''.join(buffer))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
