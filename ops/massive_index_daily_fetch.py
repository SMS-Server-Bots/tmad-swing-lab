"""Descarga el histórico diario de índices (VIX, S&P 500, Nasdaq-100, Russell 2000...) desde Massive.

Una llamada por índice (rango completo). La clave se lee de MASSIVE_API_KEY o se pide por teclado SIN mostrarla;
nunca se escribe en disco, en la URL de los logs ni en la salida. Con el plan gratuito: pausa de 12,5 s entre llamadas.

    python massive_index_daily_fetch.py --out C:\\Users\\Administrator\\Desktop\\massive_daily [--start 2023-01-01]

Salida: indices_daily.csv.gz (ticker,date,o,h,l,c) en la misma carpeta que el grouped daily, y indices_status.json
con lo que el plan ha permitido (ok / denegado / vacío) para cada índice.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.massive.com/v2/aggs/ticker/{t}/range/1/day/{a}/{b}"
INDICES = ["I:VIX", "I:VIX3M", "I:VVIX", "I:SPX", "I:NDX", "I:RUT", "I:DJI"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--sleep", type=float, default=12.5)
    a = ap.parse_args()
    for st in (sys.stdout, sys.stderr):        # consola de Windows (cp1252)
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.makedirs(a.out, exist_ok=True)
    key = os.environ.get("MASSIVE_API_KEY", "").strip() or getpass.getpass("MASSIVE_API_KEY (no se muestra): ").strip()
    rows, status = [], {}
    for i, t in enumerate(INDICES):
        if i:
            time.sleep(a.sleep)
        url = API.format(t=t, a=a.start, b=a.end) + "?adjusted=true&sort=asc&limit=50000"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode()).get("results") or []
        except urllib.error.HTTPError as e:
            status[t] = f"denegado (HTTP {e.code})" if e.code in (401, 403) else f"HTTP {e.code}"
            print(f"{t}: {status[t]}", flush=True)
            continue
        except Exception as e:  # noqa: BLE001
            status[t] = type(e).__name__; print(f"{t}: {status[t]}", flush=True); continue
        for b in res:
            d = dt.datetime.fromtimestamp(b["t"] / 1000, dt.timezone.utc).date().isoformat()
            rows.append([t, d, b.get("o"), b.get("h"), b.get("l"), b.get("c")])
        status[t] = f"ok {len(res)} días ({res[0]['t'] and dt.datetime.fromtimestamp(res[0]['t']/1000, dt.timezone.utc).date()} -> " \
                    f"{dt.datetime.fromtimestamp(res[-1]['t']/1000, dt.timezone.utc).date()})" if res else "vacío"
        print(f"{t}: {status[t]}", flush=True)
    with gzip.open(os.path.join(a.out, "indices_daily.csv.gz"), "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["ticker", "date", "o", "h", "l", "c"]); w.writerows(rows)
    json.dump(status, open(os.path.join(a.out, "indices_status.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"INDICES_OK {len(rows)} filas -> {a.out}")


if __name__ == "__main__":
    main()
