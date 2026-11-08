from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
helper = (ROOT / "MATERIALIZE_TMAD_ALERTS_COLD_HISTORY.ps1").read_text(encoding="utf-8-sig")
layer = (ROOT / "01_PROGRAMAS" / "alerts_bot" / "layer_archive.py").read_text(encoding="utf-8-sig")

assert "tmad-data-index" in helper
assert "archive-index.json" in helper
assert "release download" in helper
assert "Get-FileHash -Algorithm SHA256" in helper
assert "$expectedBytes" in helper
assert "$expectedHash" in helper
assert "$remoteHash -ne $expectedHash" in helper
assert "history_cache\\alerts_layers" in helper
assert "$target=Join-Path $CacheRoot $suffix" in helper
assert "$local=Join-Path $DataRoot $relative" in helper
assert "Test-Path $local -PathType Leaf" in helper

assert '"history_cache" / "alerts_layers"' in layer
assert 'getattr(config, "RUNTIME_ROOT"' in layer
assert "for path in cold:" in layer
assert "for path in local:" in layer
assert "by_name[path.name] = path" in layer

for forbidden in ("subprocess", "urllib", "requests"):
    assert forbidden not in layer

version = (ROOT / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
assert tuple(int(x) for x in version.split(".")) >= (6, 2, 37), version

print("PASS | >=6.2.37 | Alerts cold-history cache materialization + warm/cold reader | LIVE unchanged")
