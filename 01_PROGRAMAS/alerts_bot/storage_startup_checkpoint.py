from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import config

MIN_WAL_MB = 512.0
LOW_FREE_GB = 15.0


def main() -> int:
    db = Path(config.ALERTS_DB)
    wal = Path(str(db) + "-wal")
    if not db.exists() or not wal.exists():
        print("SKIP | startup WAL checkpoint | no WAL")
        return 0

    wal_mb = wal.stat().st_size / (1024 * 1024)
    free_gb = shutil.disk_usage(db.parent).free / (1024 ** 3)
    if wal_mb < MIN_WAL_MB and free_gb >= LOW_FREE_GB:
        print(f"SKIP | startup WAL checkpoint | wal={wal_mb:.1f}MB free={free_gb:.2f}GB")
        return 0

    before = wal.stat().st_size if wal.exists() else 0
    try:
        con = sqlite3.connect(db, timeout=30, isolation_level=None)
        try:
            con.execute("PRAGMA busy_timeout=30000")
            row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
    except Exception as exc:
        print(f"WARN | startup WAL checkpoint failed | {type(exc).__name__}: {exc}")
        return 0

    after = wal.stat().st_size if wal.exists() else 0
    print(
        "OK | startup WAL checkpoint TRUNCATE | "
        f"before_mb={before/(1024*1024):.1f} after_mb={after/(1024*1024):.1f} "
        f"free_before_gb={free_gb:.2f} result={tuple(row or ())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
