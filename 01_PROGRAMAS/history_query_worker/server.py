from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import boto3
import uvicorn
from botocore.config import Config as BotoConfig
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


PROGRAM_DIR = Path(__file__).resolve().parent
INSTALL_ROOT = Path(os.getenv("TMAD_INSTALL_ROOT", PROGRAM_DIR.parents[1])).resolve()
CONFIG_DIR = Path(os.getenv("TMAD_CONFIG_DIR", INSTALL_ROOT / "02_CONFIG")).resolve()
RUNTIME_ROOT = Path(os.getenv("TMAD_RUNTIME_DIR", INSTALL_ROOT / "04_RUNTIME")).resolve()

for env_file in (
    CONFIG_DIR / "tmad_history_query.env",
    CONFIG_DIR / "tmad_cloud.secret.env",
    CONFIG_DIR / "tmad_history_query.secret.env",
):
    if env_file.exists():
        load_dotenv(env_file, override=False)


def env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return min(hi, max(lo, value))


def env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    return min(hi, max(lo, value))


BIND_HOST = os.getenv("TMAD_HISTORY_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
BIND_PORT = env_int("TMAD_HISTORY_BIND_PORT", 8787, 1, 65535)
AUTH_TOKEN = os.getenv("TMAD_HISTORY_QUERY_TOKEN", "").strip()

R2_ENDPOINT = os.getenv("TMAD_R2_ENDPOINT", "").strip()
R2_BUCKET = os.getenv("TMAD_R2_BUCKET", "").strip()
R2_ACCESS_KEY = (
    os.getenv("TMAD_R2_ACCESS_KEY_ID", "").strip()
    or os.getenv("TMAD_R2_READ_ACCESS_KEY", "").strip()
)
R2_SECRET_KEY = (
    os.getenv("TMAD_R2_SECRET_ACCESS_KEY", "").strip()
    or os.getenv("TMAD_R2_READ_SECRET_KEY", "").strip()
)
R2_REGION = os.getenv("TMAD_R2_REGION", "auto").strip() or "auto"
MANIFEST_KEY = os.getenv(
    "TMAD_HISTORY_MANIFEST_KEY",
    "manifests/data_bot_archive/latest.json",
).strip()

CACHE_ROOT = Path(
    os.getenv(
        "TMAD_HISTORY_CACHE_DIR",
        str(RUNTIME_ROOT / "history_query_cache"),
    )
).resolve()
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
PID_FILE = RUNTIME_ROOT / "history_query_worker.pid"

MANIFEST_TTL_SECONDS = env_int("TMAD_HISTORY_MANIFEST_TTL_SECONDS", 300, 30, 86400)
CACHE_TTL_HOURS = env_int("TMAD_HISTORY_CACHE_TTL_HOURS", 24, 1, 720)
CACHE_MAX_GB = env_float("TMAD_HISTORY_CACHE_MAX_GB", 8.0, 0.5, 100.0)
QUERY_TIMEOUT_SECONDS = env_float("TMAD_HISTORY_QUERY_TIMEOUT_SECONDS", 20.0, 1.0, 300.0)
MAX_SQL_ROWS = env_int("TMAD_HISTORY_MAX_SQL_ROWS", 1000, 1, 10000)
MAX_CSV_ROWS = env_int("TMAD_HISTORY_MAX_CSV_ROWS", 5000, 1, 50000)
MAX_TEXT_LINES = env_int("TMAD_HISTORY_MAX_TEXT_LINES", 1000, 1, 10000)
MAX_BLOB_BYTES = env_int("TMAD_HISTORY_MAX_BLOB_BYTES", 1048576, 1024, 16777216)


def validate_startup() -> None:
    missing = []
    for name, value in (
        ("TMAD_HISTORY_QUERY_TOKEN", AUTH_TOKEN),
        ("TMAD_R2_ENDPOINT", R2_ENDPOINT),
        ("TMAD_R2_BUCKET", R2_BUCKET),
        ("TMAD_R2_ACCESS_KEY_ID", R2_ACCESS_KEY),
        ("TMAD_R2_SECRET_ACCESS_KEY", R2_SECRET_KEY),
    ):
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing history query configuration: " + ", ".join(missing))


@dataclass(frozen=True)
class ArchiveEntry:
    dataset: str
    object_key: str
    byte_size: int
    sha256: str


class HistoryStore:
    def __init__(self) -> None:
        validate_startup()
        self.s3 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            region_name=R2_REGION,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )
        self.archive_dir = CACHE_ROOT / "archives"
        self.extract_dir = CACHE_ROOT / "extracted"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.extract_dir.mkdir(parents=True, exist_ok=True)
        self._catalog_lock = threading.RLock()
        self._catalog_loaded_at = 0.0
        self._catalog: dict[str, ArchiveEntry] = {}
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock_for(self, key: str) -> threading.Lock:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._locks_guard:
            return self._locks.setdefault(digest, threading.Lock())

    def refresh_catalog(self, force: bool = False) -> None:
        with self._catalog_lock:
            if (
                not force
                and self._catalog
                and time.monotonic() - self._catalog_loaded_at <= MANIFEST_TTL_SECONDS
            ):
                return

            response = self.s3.get_object(Bucket=R2_BUCKET, Key=MANIFEST_KEY)
            raw = json.loads(response["Body"].read().decode("utf-8-sig"))
            entries: dict[str, ArchiveEntry] = {}

            for item in raw.get("entries", []):
                key = str(item.get("object_key") or "").strip()
                sha = str(item.get("sha256") or "").strip().lower()
                size = int(item.get("byte_size") or 0)
                verified = bool(
                    item.get("remote_verified", item.get("verified", False))
                ) and not item.get("error")
                if not key or len(sha) != 64 or size < 0 or not verified:
                    continue
                entries[key] = ArchiveEntry(
                    dataset=str(item.get("dataset") or "unknown"),
                    object_key=key,
                    byte_size=size,
                    sha256=sha,
                )

            if not entries:
                raise RuntimeError("R2 manifest contains no verified archive entries")

            self._catalog = entries
            self._catalog_loaded_at = time.monotonic()

    def catalog_entry(self, object_key: str) -> ArchiveEntry:
        self.refresh_catalog()
        key = str(object_key or "").strip()
        entry = self._catalog.get(key)
        if not entry:
            raise KeyError(f"Object is not verified in TMAD manifest: {key}")
        return entry

    def catalog_summary(self) -> dict[str, Any]:
        self.refresh_catalog()
        datasets: dict[str, dict[str, int]] = {}
        for item in self._catalog.values():
            summary = datasets.setdefault(item.dataset, {"objects": 0, "bytes": 0})
            summary["objects"] += 1
            summary["bytes"] += item.byte_size
        return {
            "manifest_key": MANIFEST_KEY,
            "objects": len(self._catalog),
            "datasets": datasets,
        }

    def _archive_path(self, entry: ArchiveEntry) -> Path:
        return self.archive_dir / f"{entry.sha256}.zip"

    def _meta_path(self, entry: ArchiveEntry) -> Path:
        return self.archive_dir / f"{entry.sha256}.json"

    def _write_meta(self, entry: ArchiveEntry) -> None:
        target = self._meta_path(entry)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "object_key": entry.object_key,
                    "dataset": entry.dataset,
                    "byte_size": entry.byte_size,
                    "sha256": entry.sha256,
                    "last_access": time.time(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, target)

    def _cached_valid(self, entry: ArchiveEntry) -> bool:
        path = self._archive_path(entry)
        if not path.is_file() or path.stat().st_size != entry.byte_size:
            return False
        try:
            meta = json.loads(self._meta_path(entry).read_text(encoding="utf-8"))
        except Exception:
            return False
        return (
            meta.get("object_key") == entry.object_key
            and meta.get("sha256") == entry.sha256
            and int(meta.get("byte_size") or -1) == entry.byte_size
        )

    def ensure_archive(self, object_key: str) -> tuple[ArchiveEntry, Path]:
        entry = self.catalog_entry(object_key)
        with self._lock_for(entry.object_key):
            if self._cached_valid(entry):
                self._write_meta(entry)
                return entry, self._archive_path(entry)

            target = self._archive_path(entry)
            part = target.with_suffix(".zip.part")
            part.unlink(missing_ok=True)

            response = self.s3.get_object(Bucket=R2_BUCKET, Key=entry.object_key)
            body = response["Body"]
            sha = hashlib.sha256()
            total = 0
            try:
                with part.open("wb") as out:
                    while True:
                        chunk = body.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        sha.update(chunk)
                        total += len(chunk)
            finally:
                try:
                    body.close()
                except Exception:
                    pass

            if total != entry.byte_size:
                part.unlink(missing_ok=True)
                raise RuntimeError(
                    f"R2 size mismatch for {entry.object_key}: {total} != {entry.byte_size}"
                )
            digest = sha.hexdigest()
            if digest != entry.sha256:
                part.unlink(missing_ok=True)
                raise RuntimeError(
                    f"R2 SHA256 mismatch for {entry.object_key}: {digest} != {entry.sha256}"
                )

            with zipfile.ZipFile(part, "r") as zf:
                bad = zf.testzip()
                if bad:
                    part.unlink(missing_ok=True)
                    raise RuntimeError(f"ZIP CRC verification failed at {bad}")

            os.replace(part, target)
            self._write_meta(entry)
            self.prune_cache(exclude={target})
            return entry, target

    def list_entries(self, object_key: str) -> tuple[ArchiveEntry, list[dict[str, Any]]]:
        entry, path = self.ensure_archive(object_key)
        files = []
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                files.append(
                    {
                        "name": info.filename,
                        "directory": info.is_dir(),
                        "byte_size": info.file_size,
                        "compressed_size": info.compress_size,
                    }
                )
        return entry, files

    def ensure_extracted(self, object_key: str, entry_name: str) -> tuple[ArchiveEntry, Path]:
        archive, zip_path = self.ensure_archive(object_key)
        name = str(entry_name or "").strip()
        if not name:
            raise ValueError("entry_name is required")

        suffix = Path(name).suffix.lower()
        entry_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]
        directory = self.extract_dir / archive.sha256
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{entry_hash}{suffix}"
        meta_path = target.with_suffix(target.suffix + ".json")

        with self._lock_for(f"{archive.object_key}\n{name}"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

            if (
                target.is_file()
                and meta.get("archive_sha256") == archive.sha256
                and meta.get("entry_name") == name
                and int(meta.get("byte_size") or -1) == target.stat().st_size
            ):
                meta["last_access"] = time.time()
                meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
                return archive, target

            with zipfile.ZipFile(zip_path, "r") as zf:
                try:
                    info = zf.getinfo(name)
                except KeyError as exc:
                    raise KeyError(f"ZIP entry not found: {name}") from exc
                if info.is_dir():
                    raise ValueError("ZIP entry is a directory")

                tmp = target.with_suffix(target.suffix + ".part")
                tmp.unlink(missing_ok=True)
                with zf.open(info, "r") as src, tmp.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
                if tmp.stat().st_size != info.file_size:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError("Extracted entry size mismatch")
                os.replace(tmp, target)

            meta_path.write_text(
                json.dumps(
                    {
                        "archive_sha256": archive.sha256,
                        "object_key": archive.object_key,
                        "entry_name": name,
                        "byte_size": target.stat().st_size,
                        "last_access": time.time(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            return archive, target

    def open_zip_entry(self, object_key: str, entry_name: str):
        archive, zip_path = self.ensure_archive(object_key)
        zf = zipfile.ZipFile(zip_path, "r")
        try:
            info = zf.getinfo(entry_name)
        except Exception:
            zf.close()
            raise
        if info.is_dir():
            zf.close()
            raise ValueError("ZIP entry is a directory")
        return archive, zf, info

    def prune_cache(self, exclude: set[Path] | None = None) -> dict[str, int]:
        excluded = {p.resolve() for p in (exclude or set())}
        now = time.time()
        ttl_seconds = CACHE_TTL_HOURS * 3600
        candidates: list[tuple[float, Path, Path | None, int]] = []

        for zip_path in self.archive_dir.glob("*.zip"):
            if zip_path.resolve() in excluded:
                continue
            meta_path = zip_path.with_suffix(".json")
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                last_access = float(meta.get("last_access") or zip_path.stat().st_mtime)
            except Exception:
                last_access = zip_path.stat().st_mtime
                meta_path = None
            candidates.append((last_access, zip_path, meta_path, zip_path.stat().st_size))

        removed_files = 0
        removed_bytes = 0
        kept = []
        for item in candidates:
            last_access, path, meta_path, size = item
            if now - last_access > ttl_seconds:
                path.unlink(missing_ok=True)
                if meta_path:
                    meta_path.unlink(missing_ok=True)
                shutil.rmtree(self.extract_dir / path.stem, ignore_errors=True)
                removed_files += 1
                removed_bytes += size
            else:
                kept.append(item)

        max_bytes = int(CACHE_MAX_GB * 1024**3)
        total = sum(size for _, path, _, size in kept if path.exists())
        for _, path, meta_path, size in sorted(kept, key=lambda row: row[0]):
            if total <= max_bytes:
                break
            if path.resolve() in excluded:
                continue
            path.unlink(missing_ok=True)
            if meta_path:
                meta_path.unlink(missing_ok=True)
            shutil.rmtree(self.extract_dir / path.stem, ignore_errors=True)
            total -= size
            removed_files += 1
            removed_bytes += size

        return {
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "remaining_archive_bytes": max(total, 0),
        }

    def cache_status(self) -> dict[str, Any]:
        files = list(self.archive_dir.glob("*.zip"))
        return {
            "cache_root": str(CACHE_ROOT),
            "archive_files": len(files),
            "archive_bytes": sum(p.stat().st_size for p in files if p.is_file()),
            "ttl_hours": CACHE_TTL_HOURS,
            "max_gb": CACHE_MAX_GB,
        }


store: HistoryStore | None = None


def get_store() -> HistoryStore:
    global store
    if store is None:
        store = HistoryStore()
        store.refresh_catalog(force=True)
    return store


app = FastAPI(
    title="TMAD History Query Worker",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)


def require_auth(x_tmad_history_token: str | None = Header(default=None)) -> None:
    supplied = x_tmad_history_token or ""
    if not AUTH_TOKEN or not secrets.compare_digest(supplied, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


class ObjectRequest(BaseModel):
    object_key: str = Field(min_length=1)


class EntryRequest(ObjectRequest):
    entry_name: str = Field(min_length=1)


class SqliteQueryRequest(EntryRequest):
    sql: str = Field(min_length=1, max_length=20000)
    params: list[Any] | dict[str, Any] | None = None
    max_rows: int = Field(default=200, ge=1, le=10000)
    blob_mode: Literal["omit", "base64", "hex"] = "omit"


class CsvFilter(BaseModel):
    column: str = Field(min_length=1)
    op: Literal["eq", "ne", "contains", "icontains", "gt", "gte", "lt", "lte", "in"] = "eq"
    value: Any


class CsvQueryRequest(EntryRequest):
    filters: list[CsvFilter] = Field(default_factory=list, max_length=20)
    columns: list[str] | None = None
    limit: int = Field(default=200, ge=1, le=50000)
    offset: int = Field(default=0, ge=0)
    encoding: str = "utf-8-sig"


class TextHeadRequest(EntryRequest):
    lines: int = Field(default=100, ge=1, le=10000)
    encoding: str = "utf-8-sig"


def read_only_sql(sql: str) -> str:
    query = str(sql or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="sql_required")
    without_final = query[:-1].rstrip() if query.endswith(";") else query
    if ";" in without_final:
        raise HTTPException(status_code=400, detail="multiple_statements_not_allowed")
    match = re.match(r"^([A-Za-z]+)", without_final)
    keyword = match.group(1).lower() if match else ""
    if keyword not in {"select", "with", "explain"}:
        raise HTTPException(status_code=400, detail="read_only_sql_required")
    return without_final


def sqlite_connection(path: Path) -> sqlite3.Connection:
    quoted = urllib.parse.quote(path.resolve().as_posix(), safe="/:")
    conn = sqlite3.connect(
        f"file:{quoted}?mode=ro&immutable=1",
        uri=True,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        conn.execute("PRAGMA trusted_schema = OFF")
    except sqlite3.DatabaseError:
        pass

    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS

    def progress() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(progress, 20000)
    return conn


def json_cell(value: Any, blob_mode: str) -> Any:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return value
    raw = bytes(value)
    if blob_mode == "omit":
        return {"blob_bytes": len(raw)}
    if len(raw) > MAX_BLOB_BYTES:
        return {
            "blob_bytes": len(raw),
            "truncated": True,
            "max_blob_bytes": MAX_BLOB_BYTES,
        }
    if blob_mode == "hex":
        return {"blob_hex": raw.hex(), "blob_bytes": len(raw)}
    return {
        "blob_base64": base64.b64encode(raw).decode("ascii"),
        "blob_bytes": len(raw),
    }


def numeric(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def csv_match(row: dict[str, str], flt: CsvFilter) -> bool:
    current = row.get(flt.column)
    expected = flt.value
    if flt.op == "eq":
        return str(current) == str(expected)
    if flt.op == "ne":
        return str(current) != str(expected)
    if flt.op == "contains":
        return str(expected) in str(current or "")
    if flt.op == "icontains":
        return str(expected).lower() in str(current or "").lower()
    if flt.op == "in":
        return isinstance(expected, list) and str(current) in {str(v) for v in expected}

    left = numeric(current)
    right = numeric(expected)
    if left is None or right is None:
        return False
    if flt.op == "gt":
        return left > right
    if flt.op == "gte":
        return left >= right
    if flt.op == "lt":
        return left < right
    if flt.op == "lte":
        return left <= right
    return False


@app.get("/health")
def health():
    summary = get_store().catalog_summary()
    return {
        "ok": True,
        "service": "tmad-history-query-worker",
        "version": "1.0.0",
        "catalog_objects": summary["objects"],
    }


@app.get("/v1/catalog/summary", dependencies=[Depends(require_auth)])
def catalog_summary():
    return {"ok": True, **get_store().catalog_summary()}


@app.post("/v1/list", dependencies=[Depends(require_auth)])
def list_archive(req: ObjectRequest):
    try:
        entry, files = get_store().list_entries(req.object_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "ok": True,
        "object_key": entry.object_key,
        "dataset": entry.dataset,
        "byte_size": entry.byte_size,
        "sha256": entry.sha256,
        "files": files,
    }


@app.post("/v1/text/head", dependencies=[Depends(require_auth)])
def text_head(req: TextHeadRequest):
    zf = None
    try:
        archive, zf, info = get_store().open_zip_entry(req.object_key, req.entry_name)
        with zf.open(info, "r") as raw:
            text = io.TextIOWrapper(raw, encoding=req.encoding, errors="replace", newline="")
            lines = []
            for _ in range(min(req.lines, MAX_TEXT_LINES)):
                line = text.readline()
                if line == "":
                    break
                lines.append(line.rstrip("\r\n"))
        return {
            "ok": True,
            "object_key": archive.object_key,
            "entry_name": req.entry_name,
            "returned_lines": len(lines),
            "lines": lines,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        if zf is not None:
            zf.close()


@app.post("/v1/sqlite/schema", dependencies=[Depends(require_auth)])
def sqlite_schema(req: EntryRequest):
    try:
        archive, path = get_store().ensure_extracted(req.object_key, req.entry_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn = sqlite_connection(path)
    try:
        objects = conn.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table','view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        result = []
        for obj in objects:
            name = str(obj["name"])
            escaped = name.replace('"', '""')
            columns = [
                dict(row)
                for row in conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()
            ]
            result.append({"name": name, "type": obj["type"], "columns": columns})
        return {
            "ok": True,
            "object_key": archive.object_key,
            "entry_name": req.entry_name,
            "sqlite_bytes": path.stat().st_size,
            "objects": result,
        }
    finally:
        conn.close()


@app.post("/v1/sqlite/query", dependencies=[Depends(require_auth)])
def sqlite_query(req: SqliteQueryRequest):
    query = read_only_sql(req.sql)
    max_rows = min(req.max_rows, MAX_SQL_ROWS)
    try:
        archive, path = get_store().ensure_extracted(req.object_key, req.entry_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn = sqlite_connection(path)
    try:
        try:
            cursor = conn.execute(query, req.params or ())
            raw_rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise HTTPException(status_code=408, detail="sqlite_query_timeout") from exc
            raise HTTPException(status_code=400, detail=f"sqlite_error: {exc}") from exc

        truncated = len(raw_rows) > max_rows
        rows = [
            {k: json_cell(v, req.blob_mode) for k, v in dict(row).items()}
            for row in raw_rows[:max_rows]
        ]
        return {
            "ok": True,
            "object_key": archive.object_key,
            "entry_name": req.entry_name,
            "returned_rows": len(rows),
            "truncated": truncated,
            "rows": rows,
        }
    finally:
        conn.close()


@app.post("/v1/csv/query", dependencies=[Depends(require_auth)])
def csv_query(req: CsvQueryRequest):
    limit = min(req.limit, MAX_CSV_ROWS)
    zf = None
    started = time.monotonic()
    try:
        archive, zf, info = get_store().open_zip_entry(req.object_key, req.entry_name)
        with zf.open(info, "r") as raw:
            text = io.TextIOWrapper(raw, encoding=req.encoding, errors="replace", newline="")
            reader = csv.DictReader(text)
            fieldnames = reader.fieldnames or []

            if req.columns:
                missing = [c for c in req.columns if c not in fieldnames]
                if missing:
                    raise HTTPException(
                        status_code=400,
                        detail={"missing_columns": missing, "available_columns": fieldnames},
                    )
            for flt in req.filters:
                if flt.column not in fieldnames:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "missing_filter_column": flt.column,
                            "available_columns": fieldnames,
                        },
                    )

            skipped_matches = 0
            scanned = 0
            rows = []
            for row in reader:
                scanned += 1
                if time.monotonic() - started > QUERY_TIMEOUT_SECONDS:
                    raise HTTPException(status_code=408, detail="csv_query_timeout")
                if req.filters and not all(csv_match(row, flt) for flt in req.filters):
                    continue
                if skipped_matches < req.offset:
                    skipped_matches += 1
                    continue
                if req.columns:
                    row = {name: row.get(name, "") for name in req.columns}
                rows.append(row)
                if len(rows) >= limit:
                    break

        return {
            "ok": True,
            "object_key": archive.object_key,
            "entry_name": req.entry_name,
            "scanned_rows": scanned,
            "returned_rows": len(rows),
            "limit": limit,
            "rows": rows,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        if zf is not None:
            zf.close()


@app.get("/v1/cache/status", dependencies=[Depends(require_auth)])
def cache_status():
    return {"ok": True, **get_store().cache_status()}


@app.post("/v1/cache/prune", dependencies=[Depends(require_auth)])
def cache_prune():
    return {"ok": True, **get_store().prune_cache()}


if __name__ == "__main__":
    validate_startup()
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    try:
        uvicorn.run(
            "server:app",
            host=BIND_HOST,
            port=BIND_PORT,
            log_level="info",
            reload=False,
        )
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text(encoding="ascii").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except Exception:
            pass
