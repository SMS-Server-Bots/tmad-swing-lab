from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

PRIMARY_EXTS = {".db", ".sqlite", ".sqlite3", ".csv", ".zip"}
AUX_EXTS = {".json", ".jsonl", ".har", ".png"}
TEXT_EXTS = {".json", ".jsonl", ".har"}
SECRET_EXTS = {".env", ".secret", ".pem", ".key", ".pfx", ".p12"}
SECRET_NAME = re.compile(r"(?i)(secret|credential|password|token|cookie|\.env|\.pem|\.key)")
REDACTIONS = (
    (re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[^\s\"']+", re.I), "[REDACTED_WEBHOOK]"),
    (re.compile(r"\b(?:gho_|ghp_|github_pat_)[A-Za-z0-9_\-]+\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r'(?i)("?(?:authorization|cookie|set-cookie|token|secret|password|api[_-]?key)"?\s*[:=]\s*")([^"]+)(")'), r"\1[REDACTED]\3"),
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode})\n{p.stdout}\n{p.stderr}")
    return p


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def redact_line(line: str) -> str:
    for pattern, replacement in REDACTIONS:
        line = pattern.sub(replacement, line)
    return line


def zip_safe(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return not any(SECRET_NAME.search(name) or Path(name).suffix.lower() in SECRET_EXTS for name in z.namelist())
    except Exception:
        return False


def inventory(root: Path) -> tuple[list[dict], list[dict], list[dict]]:
    primary, auxiliary, excluded = [], [], []
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        size = p.stat().st_size
        if ext in SECRET_EXTS or SECRET_NAME.search(p.name):
            excluded.append({"path": rel, "bytes": size, "reason": "secret/config"})
            continue
        if size == 0:
            excluded.append({"path": rel, "bytes": 0, "reason": "empty file"})
            continue
        if ext == ".zip" and not zip_safe(p):
            excluded.append({"path": rel, "bytes": size, "reason": "zip contains sensitive/unreadable material"})
            continue
        item = {"path": rel, "bytes": size, "mode": "redacted_text" if ext in TEXT_EXTS else "raw"}
        if ext in PRIMARY_EXTS:
            primary.append(item)
        elif ext in AUX_EXTS:
            auxiliary.append(item)
        else:
            excluded.append({"path": rel, "bytes": size, "reason": "runtime/code/not historical data"})
    return primary, auxiliary, excluded


def ensure_release(repo: str, tag: str) -> None:
    if run(["gh", "release", "view", tag, "--repo", repo], check=False).returncode:
        run([
            "gh", "release", "create", tag, "--repo", repo,
            "--title", "TMAD legacy Scanner cold history",
            "--notes", "Verified cold archive for historical old_2 Scanner data. Secrets/runtime code excluded; text research artifacts redacted.",
        ])


def release_id(repo: str, tag: str) -> str:
    p = run(["gh", "api", f"repos/{repo}/releases/tags/{tag}", "--jq", ".id"])
    value = p.stdout.strip()
    if not value:
        raise RuntimeError("Could not resolve GitHub release id")
    return value


def remote_assets(repo: str, tag: str) -> set[str]:
    rid = release_id(repo, tag)
    p = run([
        "gh", "api", "--paginate",
        f"repos/{repo}/releases/{rid}/assets?per_page=100",
        "--jq", ".[].name",
    ])
    return {line.strip() for line in p.stdout.splitlines() if line.strip()}


def verify_remote_asset(repo: str, tag: str, asset: str, expected_size: int, expected_sha: str, work: Path) -> None:
    verify_dir = work / "verify"
    shutil.rmtree(verify_dir, ignore_errors=True)
    verify_dir.mkdir(parents=True, exist_ok=True)
    try:
        run(["gh", "release", "download", tag, "--repo", repo, "--pattern", asset, "--dir", str(verify_dir), "--clobber"])
        downloaded = verify_dir / asset
        if not downloaded.exists():
            raise RuntimeError(f"remote asset missing after download: {asset}")
        if downloaded.stat().st_size != expected_size:
            raise RuntimeError(f"remote size mismatch for {asset}")
        if sha256(downloaded) != expected_sha:
            raise RuntimeError(f"remote SHA256 mismatch for {asset}")
    finally:
        shutil.rmtree(verify_dir, ignore_errors=True)


def make_gzip_chunk(src: Path, offset: int, raw_limit: int, dst: Path) -> dict:
    raw_hash = hashlib.sha256()
    raw_bytes = 0
    with src.open("rb") as fi, dst.open("wb") as fo:
        fi.seek(offset)
        with gzip.GzipFile(fileobj=fo, mode="wb", compresslevel=6, mtime=0) as gz:
            left = raw_limit
            while left > 0:
                block = fi.read(min(4 * 1024 * 1024, left))
                if not block:
                    break
                gz.write(block)
                raw_hash.update(block)
                raw_bytes += len(block)
                left -= len(block)
    return {
        "raw_bytes": raw_bytes,
        "raw_sha256": raw_hash.hexdigest(),
        "asset_bytes": dst.stat().st_size,
        "asset_sha256": sha256(dst),
    }


def store_primary(
    root: Path,
    files: list[dict],
    state: dict,
    assets: set[str],
    repo: str,
    tag: str,
    chunk_size: int,
    work: Path,
) -> None:
    for meta in files:
        rel = meta["path"]
        src = root / Path(rel)
        key = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]
        entry = state["files"].setdefault(rel, {"parts": [], "verified": False, "kind": "primary"})

        source_sha = sha256(src)
        total = (src.stat().st_size + chunk_size - 1) // chunk_size
        known = {int(p["index"]): p for p in entry.get("parts", []) if "index" in p}

        for index in range(total):
            offset = index * chunk_size
            expected = min(chunk_size, src.stat().st_size - offset)
            asset = f"legacy-{key}-{re.sub(r'[^A-Za-z0-9._-]+','_',src.name)[-80:]}.part{index+1:04d}-of-{total:04d}.gz"
            old = known.get(index)
            if old and old.get("verified") and old.get("asset") == asset and asset in assets:
                continue

            part = work / asset
            try:
                info = make_gzip_chunk(src, offset, chunk_size, part)
                if info["raw_bytes"] != expected:
                    raise RuntimeError(f"chunk size mismatch {rel} part {index+1}")
                run(["gh", "release", "upload", tag, str(part), "--repo", repo, "--clobber"])
                verify_remote_asset(repo, tag, asset, info["asset_bytes"], info["asset_sha256"], work)
                known[index] = {
                    "index": index,
                    "asset": asset,
                    **info,
                    "offset": offset,
                    "verified": True,
                }
                assets.add(asset)
            finally:
                part.unlink(missing_ok=True)

            entry.update({
                "kind": "primary",
                "source_bytes": src.stat().st_size,
                "source_sha256": source_sha,
                "mode": meta["mode"],
                "parts": [known[i] for i in sorted(known)],
            })
            entry["verified"] = len(entry["parts"]) == total and all(x.get("verified") for x in entry["parts"])
            save_json(state["_state_path"], {k: v for k, v in state.items() if k != "_state_path"})

        entry.update({
            "kind": "primary",
            "source_bytes": src.stat().st_size,
            "source_sha256": source_sha,
            "mode": meta["mode"],
            "parts": [known[i] for i in sorted(known)],
        })
        entry["verified"] = len(entry["parts"]) == total and all(x.get("verified") for x in entry["parts"])
        save_json(state["_state_path"], {k: v for k, v in state.items() if k != "_state_path"})


def build_aux_bundle(root: Path, files: list[dict], bundle: Path) -> list[dict]:
    manifest: list[dict] = []
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for meta in files:
            rel = meta["path"]
            src = root / Path(rel)
            source_sha = sha256(src)
            if meta["mode"] == "redacted_text":
                member = f"redacted/{rel}"
                with z.open(member, "w", force_zip64=True) as dst, src.open("r", encoding="utf-8", errors="replace") as fi:
                    for line in fi:
                        dst.write(redact_line(line).encode("utf-8"))
                storage_mode = "redacted_text"
            else:
                member = f"raw/{rel}"
                z.write(src, arcname=member)
                storage_mode = "raw"
            manifest.append({
                "path": rel,
                "source_bytes": src.stat().st_size,
                "source_sha256": source_sha,
                "bundle_member": member,
                "mode": storage_mode,
            })
        z.writestr("MANIFEST.json", json.dumps({"files": manifest}, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest


def store_auxiliary(
    root: Path,
    files: list[dict],
    state: dict,
    assets: set[str],
    repo: str,
    tag: str,
    work: Path,
) -> dict:
    if not files:
        return {"verified": True, "files": [], "asset": None, "asset_bytes": 0}

    aux_key = "auxiliary_bundle_v1"
    existing = state.setdefault("bundles", {}).get(aux_key) or {}
    asset = "legacy-auxiliary-bundle-v1.zip"
    if existing.get("verified") and existing.get("asset") == asset and asset in assets:
        return existing

    bundle = work / asset
    try:
        manifest = build_aux_bundle(root, files, bundle)
        asset_sha = sha256(bundle)
        asset_bytes = bundle.stat().st_size
        run(["gh", "release", "upload", tag, str(bundle), "--repo", repo, "--clobber"])
        verify_remote_asset(repo, tag, asset, asset_bytes, asset_sha, work)
        result = {
            "verified": True,
            "asset": asset,
            "asset_bytes": asset_bytes,
            "asset_sha256": asset_sha,
            "file_count": len(manifest),
            "source_bytes": sum(int(x["source_bytes"]) for x in manifest),
            "files": manifest,
        }
        state.setdefault("bundles", {})[aux_key] = result
        assets.add(asset)
        save_json(state["_state_path"], {k: v for k, v in state.items() if k != "_state_path"})
        return result
    finally:
        bundle.unlink(missing_ok=True)


def cold_store(root: Path, production: Path, repo: str, tag: str, chunk_mb: int) -> dict:
    run(["gh", "auth", "status"])
    ensure_release(repo, tag)
    primary, auxiliary, excluded = inventory(root)

    state_path = production / "04_RUNTIME" / "history_migration" / "legacy_cold_state.json"
    catalog_path = production / "03_DATOS" / "data_bot" / "output" / "legacy_history_catalog.json"
    work = production / "04_RUNTIME" / "history_migration" / "work"
    work.mkdir(parents=True, exist_ok=True)

    state = load_json(state_path, {"schema_version": 2, "files": {}, "bundles": {}})
    state["schema_version"] = 2
    state.setdefault("files", {})
    state.setdefault("bundles", {})
    state["_state_path"] = state_path

    assets = remote_assets(repo, tag)
    chunk_size = max(32, chunk_mb) * 1024 * 1024

    store_primary(root, primary, state, assets, repo, tag, chunk_size, work)
    aux_bundle = store_auxiliary(root, auxiliary, state, assets, repo, tag, work)

    primary_verified = all(state["files"].get(x["path"], {}).get("verified") for x in primary)
    verified = bool(primary or auxiliary) and primary_verified and bool(aux_bundle.get("verified"))

    pending_primary = [x["path"] for x in primary if not state["files"].get(x["path"], {}).get("verified")]
    catalog = {
        "schema_version": 2,
        "source": "old_2/old Bot Scanner_data",
        "source_root": str(root),
        "repo": repo,
        "release_tag": tag,
        "verified": verified,
        "included_bytes": sum(int(x["bytes"]) for x in primary + auxiliary),
        "excluded_bytes": sum(int(x["bytes"]) for x in excluded),
        "primary_file_count": len(primary),
        "auxiliary_file_count": len(auxiliary),
        "pending_primary": pending_primary,
        "primary_files": [{**x, **state["files"].get(x["path"], {})} for x in primary],
        "auxiliary_bundle": aux_bundle,
        "excluded": excluded,
        "notes": [
            "DB/SQLite/CSV/ZIP are stored as independently verified gzip chunks.",
            "JSON/JSONL/HAR/PNG are stored in one verified ZIP bundle to avoid thousands of release assets.",
            "JSON/JSONL/HAR are redacted before remote storage.",
            "Credentials, environments, executable/runtime code and empty files are intentionally excluded.",
        ],
    }
    save_json(catalog_path, catalog)
    run(["gh", "release", "upload", tag, str(catalog_path), "--repo", repo, "--clobber"])
    return catalog


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--legacy-root", default=r"C:\Users\Administrator\Desktop\old_2\old Bot Scanner_data")
    p.add_argument("--production-root", default=r"C:\Users\Administrator\Desktop\TMAD_Dual_Bot_OneClick")
    p.add_argument("--repo", default="SMS-Server-Bots/tmad-dual-bot")
    p.add_argument("--tag", default="tmad-legacy-scanner-cold-v1")
    p.add_argument("--chunk-mb", type=int, default=128)
    args = p.parse_args()
    try:
        result = cold_store(Path(args.legacy_root), Path(args.production_root), args.repo, args.tag, args.chunk_mb)
        print(json.dumps({
            "verified": result["verified"],
            "included_bytes": result["included_bytes"],
            "primary_files": result["primary_file_count"],
            "auxiliary_files": result["auxiliary_file_count"],
            "pending_primary": result["pending_primary"],
        }, ensure_ascii=False))
        return 0 if result["verified"] else 2
    except Exception as exc:
        production = Path(args.production_root)
        error_path = production / "04_RUNTIME" / "history_migration" / "legacy_cold_error.json"
        save_json(error_path, {
            "error": f"{type(exc).__name__}: {exc}",
            "legacy_root": args.legacy_root,
            "repo": args.repo,
            "tag": args.tag,
        })
        print(json.dumps({"verified": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
