"""Backup en streaming de una base SQLite grande, sin copiarla antes a disco.

Abre la base en SOLO LECTURA, fija una transacción de lectura (instantánea coherente aunque el bot siga
escribiendo en WAL) y vuelca el SQL comprimido con gzip en partes de tamaño fijo. Solo necesita espacio
libre para la salida comprimida (normalmente un 10-20 % del tamaño de la base), no para una copia completa.

    python tmad_sqlite_stream_backup.py dump    <base.db> <carpeta_salida> [--part-mb 1800] [--stem nombre]
    python tmad_sqlite_stream_backup.py restore <carpeta_salida> <nueva_base.db>
    python tmad_sqlite_stream_backup.py verify  <carpeta_salida>

La salida contiene ``<nombre>.sql.gz.partNNN`` y ``<nombre>.manifest.json`` (SHA-256 de cada parte y del
flujo completo, recuento de filas por tabla en la instantánea).
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import os
import sqlite3
import sys


class _PartWriter(io.RawIOBase):
    def __init__(self, folder: str, stem: str, part_bytes: int):
        self.folder, self.stem, self.part_bytes = folder, stem, part_bytes
        self.idx, self.fh, self.written, self.parts = 0, None, 0, []
        self.total = hashlib.sha256()

    def _open(self):
        self.idx += 1
        path = os.path.join(self.folder, f"{self.stem}.sql.gz.part{self.idx:03d}")
        self.fh = open(path, "wb"); self.written = 0; self.cur = hashlib.sha256()
        self.parts.append({"name": os.path.basename(path)})

    def _close_part(self):
        if self.fh:
            self.fh.close()
            self.parts[-1].update(bytes=self.written, sha256=self.cur.hexdigest()); self.fh = None

    def writable(self):
        return True

    def write(self, b):
        b = bytes(b); view = memoryview(b); n = 0
        while n < len(b):
            if self.fh is None or self.written >= self.part_bytes:
                self._close_part(); self._open()
            take = min(len(b) - n, self.part_bytes - self.written)
            chunk = view[n:n + take]
            self.fh.write(chunk); self.cur.update(chunk); self.total.update(chunk)
            self.written += take; n += take
        return len(b)

    def close(self):
        self._close_part(); super().close()


def dump(db: str, out: str, part_mb: int = 1800, stem: str | None = None) -> dict:
    os.makedirs(out, exist_ok=True)
    stem = stem or os.path.splitext(os.path.basename(db))[0]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    con.execute("PRAGMA busy_timeout=120000")
    con.execute("BEGIN")                     # instantánea de lectura coherente durante todo el volcado
    import time
    t0 = time.time()
    def log(msg):
        print(f"[{time.time()-t0:7.0f}s] {msg}", file=sys.stderr, flush=True)
    log(f"python {sys.version.split()[0]} sqlite {sqlite3.sqlite_version} base {os.path.getsize(db)/1e9:.2f} GB")
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    counts = {}
    for t in tables:
        counts[t] = con.execute(f'select count(*) from "{t}"').fetchone()[0]
    log(f"{len(tables)} tablas, {sum(counts.values()):,} filas; mayores: " +
        ", ".join(f"{t}={n:,}" for t, n in sorted(counts.items(), key=lambda x: -x[1])[:5]))
    started = dt.datetime.now(dt.timezone.utc)
    pw = _PartWriter(out, stem, part_mb * 1024 * 1024)
    last = time.time(); n = 0; cur_table = ""
    with gzip.GzipFile(fileobj=pw, mode="wb", compresslevel=6, mtime=0) as gz:
        for line in con.iterdump():
            gz.write((line + "\n").encode("utf-8")); n += 1
            if line.startswith("CREATE TABLE"):
                import re as _re; m = _re.match(r'CREATE TABLE\s+(?:IF NOT EXISTS\s+)?["\[`]?([^"\]`\s(]+)', line); cur_table = m.group(1) if m else line[:40]
                log(f"tabla {cur_table} ({counts.get(cur_table, '?'):,} filas)" if isinstance(counts.get(cur_table), int) else f"tabla {cur_table}")
            if time.time() - last > 30:
                last = time.time()
                log(f"  {n:,} sentencias | {cur_table} | comprimido {sum(p.get('bytes', 0) for p in pw.parts[:-1]) + pw.written:,} bytes")
    pw.close(); con.rollback(); con.close()
    manifest = dict(schema=1, source=os.path.abspath(db), source_bytes=os.path.getsize(db),
                    started_utc=started.isoformat(timespec="seconds"),
                    finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    stream_sha256=pw.total.hexdigest(), parts=pw.parts, row_counts=counts)
    with open(os.path.join(out, f"{stem}.manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def _manifest(folder: str) -> tuple[dict, str]:
    m = [f for f in os.listdir(folder) if f.endswith(".manifest.json")]
    if len(m) != 1:
        raise SystemExit(f"Se esperaba 1 manifest en {folder}, hay {len(m)}")
    return json.load(open(os.path.join(folder, m[0]), encoding="utf-8")), folder


def verify(folder: str) -> dict:
    man, _ = _manifest(folder); total = hashlib.sha256()
    for p in man["parts"]:
        h = hashlib.sha256()
        with open(os.path.join(folder, p["name"]), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk); total.update(chunk)
        if h.hexdigest() != p["sha256"]:
            raise SystemExit(f"SHA distinto en {p['name']}")
    if total.hexdigest() != man["stream_sha256"]:
        raise SystemExit("SHA del flujo completo distinto")
    return man


class _PartReader(io.RawIOBase):
    def __init__(self, folder, parts):
        self.files = [os.path.join(folder, p["name"]) for p in parts]; self.fh = None

    def readable(self):
        return True

    def readinto(self, b):
        while True:
            if self.fh is None:
                if not self.files:
                    return 0
                self.fh = open(self.files.pop(0), "rb")
            n = self.fh.readinto(b)
            if n:
                return n
            self.fh.close(); self.fh = None


def restore(folder: str, target: str) -> None:
    man = verify(folder)
    if os.path.exists(target):
        raise SystemExit(f"{target} ya existe: no se sobrescribe")
    con = sqlite3.connect(target); con.execute("PRAGMA journal_mode=OFF"); con.execute("PRAGMA synchronous=OFF")
    stmt = []
    with gzip.GzipFile(fileobj=io.BufferedReader(_PartReader(folder, man["parts"]), 1 << 20)) as gz:
        for raw in io.TextIOWrapper(gz, encoding="utf-8"):
            stmt.append(raw)
            if sqlite3.complete_statement("".join(stmt)):
                s = "".join(stmt).strip(); stmt = []
                if s in ("BEGIN TRANSACTION;", "COMMIT;"):
                    continue
                con.execute(s)
    con.commit()
    bad = {t: (n, con.execute(f'select count(*) from "{t}"').fetchone()[0]) for t, n in man["row_counts"].items()}
    bad = {t: v for t, v in bad.items() if v[0] != v[1]}
    con.close()
    if bad:
        raise SystemExit(f"Recuentos distintos tras restaurar: {bad}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "dump":
        part = int(sys.argv[sys.argv.index("--part-mb") + 1]) if "--part-mb" in sys.argv else 1800
        stem = sys.argv[sys.argv.index("--stem") + 1] if "--stem" in sys.argv else None
        m = dump(sys.argv[2], sys.argv[3], part, stem)
        print(json.dumps({k: m[k] for k in ("source_bytes", "started_utc", "finished_utc", "stream_sha256")}
                         | {"parts": len(m["parts"]), "compressed_bytes": sum(p["bytes"] for p in m["parts"])}))
    elif cmd == "verify":
        verify(sys.argv[2]); print("VERIFY_OK")
    elif cmd == "restore":
        restore(sys.argv[2], sys.argv[3]); print("RESTORE_OK")
    else:
        raise SystemExit(__doc__)
