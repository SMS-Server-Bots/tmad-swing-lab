from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
SKIP_DIR_NAMES = {"logs", "__pycache__", "temp", "tmp"}
SKIP_SUFFIXES = {".zip", ".log", ".lock", ".pid", ".tmp", ".bak"}


def copy_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination, timeout=30)
    try:
        src.backup(dst, pages=2048)
        row = dst.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise RuntimeError(f"integrity_check failed for {source}: {row}")
    finally:
        dst.close()
        src.close()


def snapshot(source_root: Path, destination_root: Path) -> dict:
    source_root = source_root.resolve()
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []
    errors: list[dict] = []

    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        rel = source.relative_to(source_root)
        if any(part.lower() in SKIP_DIR_NAMES for part in rel.parts[:-1]):
            continue
        lower_name = source.name.lower()
        suffix = source.suffix.lower()
        if suffix in SKIP_SUFFIXES or lower_name.endswith(("-wal", "-shm", "-journal")):
            continue
        destination = destination_root / rel
        try:
            if suffix in DB_EXTENSIONS:
                copy_sqlite(source, destination)
                mode = "sqlite-backup"
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                mode = "copy"
            files.append({"path": rel.as_posix(), "bytes": destination.stat().st_size, "mode": mode})
        except Exception as exc:
            errors.append({"path": rel.as_posix(), "error": f"{type(exc).__name__}: {exc}"})

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "destination_root": str(destination_root),
        "file_count": len(files),
        "error_count": len(errors),
        "files": files,
        "errors": errors,
    }
    (destination_root / "_tmad_mutable_snapshot.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    result = snapshot(Path(args.source), Path(args.destination))
    print(json.dumps({"file_count": result["file_count"], "error_count": result["error_count"]}))
    return 0 if result["error_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
