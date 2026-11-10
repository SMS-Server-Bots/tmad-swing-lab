"""Descarga OHLCV diario OFICIAL de todo el mercado de EE. UU. (Massive/Polygon grouped daily) para el VPS.

Una llamada por sesión NYSE. adjusted=true (ajustado por splits, no por dividendos). Excluye OTC.
La clave se lee de MASSIVE_API_KEY o se pide por teclado SIN mostrarla; nunca se escribe en disco,
en la URL ni en la salida.

    python massive_grouped_daily_fetch.py --out C:\\...\\massive_daily [--start 2024-01-02] [--end 2026-11-09] [--sleep 12.5]

Salida: un CSV.gz por mes (date,ticker,o,h,l,c,v,vw,n) y status.json. Reanudable: si se corta, al volver a
ejecutarlo solo descarga los días que faltan. Con un plan gratuito (5 llamadas/min) usa --sleep 12.5.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/"
HOLIDAYS = {
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04",
    "2024-09-02", "2024-11-28", "2024-12-25", "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17",
    "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
}


def sessions(start: str, end: str) -> list[str]:
    d, e, out = dt.date.fromisoformat(start), dt.date.fromisoformat(end), []
    while d <= e:
        s = d.isoformat()
        if d.weekday() < 5 and s not in HOLIDAYS:
            out.append(s)
        d += dt.timedelta(days=1)
    return out


def fetch(day: str, key: str, timeout: int = 60) -> list[dict]:
    req = urllib.request.Request(API + day + "?adjusted=true&include_otc=false",
                                 headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                                          "User-Agent": "tmad-swing-lab/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8"))
    res = payload.get("results") or []
    if not isinstance(res, list):
        raise ValueError("respuesta sin results")
    return res


def month_file(out: str, day: str) -> str:
    return os.path.join(out, f"grouped_daily_{day[:7]}.csv.gz")


def days_present(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with gzip.open(path, "rt", newline="") as f:
        return {row["date"] for row in csv.DictReader(f)}


def append_rows(path: str, day: str, rows: list[dict]) -> None:
    new = not os.path.exists(path)
    buf = io.StringIO(); w = csv.writer(buf)
    if new:
        w.writerow(["date", "ticker", "o", "h", "l", "c", "v", "vw", "n"])
    for x in rows:
        if not x.get("T"):
            continue
        w.writerow([day, x["T"], x.get("o"), x.get("h"), x.get("l"), x.get("c"), x.get("v"), x.get("vw"), x.get("n")])
    with gzip.open(path, "at", newline="") as f:   # miembros gzip concatenados: válidos para cualquier lector
        f.write(buf.getvalue())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    ap.add_argument("--sleep", type=float, default=12.5, help="segundos entre llamadas (plan gratuito: 12.5)")
    a = ap.parse_args()
    key = os.environ.get("MASSIVE_API_KEY", "").strip() or getpass.getpass("MASSIVE_API_KEY (no se muestra): ").strip()
    if not key:
        sys.exit("Falta la clave")
    os.makedirs(a.out, exist_ok=True)
    todo = sessions(a.start, a.end)
    have: set[str] = set()
    for m in sorted({d[:7] for d in todo}):
        have |= days_present(month_file(a.out, m + "-01"))
    pending = [d for d in todo if d not in have]
    print(f"Sesiones {len(todo)} | ya descargadas {len(todo) - len(pending)} | pendientes {len(pending)}", flush=True)
    status = {"ok": [], "empty": [], "denied": [], "error": {}}
    for i, day in enumerate(pending, 1):
        for attempt in range(5):
            try:
                rows = fetch(day, key)
                if rows:
                    append_rows(month_file(a.out, day), day, rows); status["ok"].append(day)
                else:
                    status["empty"].append(day)
                print(f"[{i}/{len(pending)}] {day}: {len(rows)} tickers", flush=True)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print(f"  límite de llamadas, espero 60 s ({day})", flush=True); time.sleep(60); continue
                if e.code in (401, 403):
                    status["denied"].append(day)
                    print(f"[{i}/{len(pending)}] {day}: HTTP {e.code} (fuera del histórico de tu plan o clave no válida)", flush=True)
                    break
                status["error"][day] = f"HTTP {e.code}"; print(f"  HTTP {e.code} en {day}", flush=True); time.sleep(10)
            except Exception as e:  # red, JSON...
                status["error"][day] = type(e).__name__; print(f"  {type(e).__name__} en {day}, reintento", flush=True); time.sleep(10)
        time.sleep(a.sleep)
    status["summary"] = {k: len(v) for k, v in status.items() if k != "summary"}
    with open(os.path.join(a.out, "status.json"), "w") as f:
        json.dump(status, f, indent=1)
    print("FIN", json.dumps(status["summary"]))
    if status["denied"]:
        print(f"Días denegados: {min(status['denied'])} .. {max(status['denied'])}. Si son los más antiguos, es el límite de histórico del plan.")


if __name__ == "__main__":
    main()
