"""Descarga (solo GET) las velas de 1 minuto de tmad.app (30 días, incluye pre y post-market) para todo el universo
del Scanner y las guarda comprimidas. El bot NO archiva estas velas: solo las pide y descarta.

Usa el cliente y la sesión del propio Data Bot (02_CONFIG/tmad_session.json). No escribe cookies.
Salida:
  <out>/intraday_1min_<fecha>.csv.gz      ticker,ts,open,high,low,close,volume  (ts = hora de Nueva York)
  <out>/intraday_sessionstats_<fecha>.json  sessionStats + keyLevels por ticker
  <out>/intraday_index_<fecha>.json         estado por ticker

    <venv data_bot>\\python.exe capturar_intradia_1min.py --install-root C:\\...\\TMAD_Dual_Bot_OneClick --out <carpeta>
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import sys
import time


def find_rows(x, best=None):
    """Lista más larga de dicts con 'ticker' o 'symbol' dentro del payload del Scanner."""
    best = best or []
    if isinstance(x, list):
        if x and all(isinstance(r, dict) for r in x[:5]) and any(("ticker" in r or "symbol" in r) for r in x[:5]):
            if len(x) > len(best):
                best = x
        for v in x[:5]:
            best = find_rows(v, best)
    elif isinstance(x, dict):
        for v in x.values():
            best = find_rows(v, best)
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tickers", default="", help="lista separada por comas; vacío = universo del Scanner")
    ap.add_argument("--sleep", type=float, default=0.25)
    a = ap.parse_args()
    root = os.path.abspath(a.install_root)
    os.environ.setdefault("TMAD_INSTALL_ROOT", root)
    os.environ.setdefault("TMAD_CONFIG_DIR", os.path.join(root, "02_CONFIG"))
    os.environ.setdefault("TMAD_DATA_DIR", os.path.join(root, "03_DATOS"))
    os.environ.setdefault("TMAD_RUNTIME_DIR", os.path.join(root, "04_RUNTIME"))
    sys.path.insert(0, os.path.join(root, "01_PROGRAMAS", "data_bot"))
    import tmad_client as T  # noqa: E402

    os.makedirs(a.out, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d")
    client = T.make_client()

    if a.tickers.strip():
        tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    else:
        payload, _ = T.fetch_scanner(client)
        rows = find_rows(payload)
        tickers = sorted({str(r.get("ticker") or r.get("symbol")).upper() for r in rows if (r.get("ticker") or r.get("symbol"))})
    print(f"Universo: {len(tickers)} tickers")

    bars_path = os.path.join(a.out, f"intraday_1min_{stamp}.csv.gz")
    stats, index = {}, []
    n_bars = 0
    t0 = time.time()
    with gzip.open(bars_path, "wt", newline="", encoding="utf-8", compresslevel=6) as gz:
        w = csv.writer(gz)
        w.writerow(["ticker", "ts", "open", "high", "low", "close", "volume"])
        for i, t in enumerate(tickers, 1):
            try:
                p, status = T.fetch_stock_intraday(client, t)
                d = p.get("data", p) if isinstance(p, dict) else {}
                bars = d.get("intradayBars") or []
                for b in bars:
                    w.writerow([t, b.get("timestamp"), b.get("open"), b.get("high"), b.get("low"), b.get("close"), b.get("volume")])
                n_bars += len(bars)
                stats[t] = {"tradingDate": d.get("tradingDate"), "sessionStats": d.get("sessionStats"), "keyLevels": d.get("keyLevels"),
                            "dailyZones": d.get("dailyZones")}
                first = bars[0].get("timestamp") if bars else None
                last = bars[-1].get("timestamp") if bars else None
                index.append({"ticker": t, "status": status, "bars": len(bars), "first": first, "last": last})
            except Exception as e:  # noqa: BLE001
                index.append({"ticker": t, "error": type(e).__name__, "detail": str(e)[:200]})
            if i % 50 == 0 or i == len(tickers):
                ok = sum(1 for x in index if "error" not in x)
                print(f"  {i}/{len(tickers)}  ok={ok}  velas={n_bars:,}  {time.time()-t0:,.0f}s", flush=True)
            time.sleep(a.sleep)

    with open(os.path.join(a.out, f"intraday_sessionstats_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False)
    with open(os.path.join(a.out, f"intraday_index_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump({"captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "items": index}, f, indent=1)
    ok = sum(1 for x in index if "error" not in x)
    mb = os.path.getsize(bars_path) / 1e6
    print(f"CAPTURA_INTRADIA_OK {ok}/{len(tickers)} tickers, {n_bars:,} velas, {mb:,.0f} MB -> {a.out}")


if __name__ == "__main__":
    main()
