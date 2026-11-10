"""Captura puntual (solo GET) de los endpoints de tmad.app que el bot NO está guardando.

Usa el cliente y la sesión del propio Data Bot (02_CONFIG/tmad_session.json). No escribe cookies en disco
ni en la salida. Guarda cada respuesta JSON tal cual y un index.json con estado, tamaño y estructura.

    <venv data_bot>\\python.exe capturar_tmad_api.py --install-root C:\\...\\TMAD_Dual_Bot_OneClick --out <carpeta>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time


def shape(x, depth=0):
    if depth > 3:
        return type(x).__name__
    if isinstance(x, dict):
        return {k: shape(v, depth + 1) for k, v in list(x.items())[:60]}
    if isinstance(x, list):
        return [f"list[{len(x)}]", shape(x[0], depth + 1) if x else None]
    return type(x).__name__


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tickers", default="SPY,AAPL,NVDA,TSLA")
    a = ap.parse_args()
    root = os.path.abspath(a.install_root)
    os.environ.setdefault("TMAD_INSTALL_ROOT", root)
    os.environ.setdefault("TMAD_CONFIG_DIR", os.path.join(root, "02_CONFIG"))
    os.environ.setdefault("TMAD_DATA_DIR", os.path.join(root, "03_DATOS"))
    os.environ.setdefault("TMAD_RUNTIME_DIR", os.path.join(root, "04_RUNTIME"))
    sys.path.insert(0, os.path.join(root, "01_PROGRAMAS", "data_bot"))
    import tmad_client as T  # noqa: E402

    os.makedirs(a.out, exist_ok=True)
    index = {"captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "items": []}
    client = T.make_client()

    def grab(name: str, fn, *args):
        t0 = time.time()
        try:
            payload, status = fn(client, *args)
            path = os.path.join(a.out, f"{name}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            item = {"name": name, "status": status, "bytes": os.path.getsize(path), "seconds": round(time.time() - t0, 1),
                    "shape": shape(payload)}
            print(f"OK   {name:40s} HTTP {status}  {item['bytes']/1024:9.1f} KB")
            return payload, item
        except Exception as e:  # noqa: BLE001
            item = {"name": name, "error": type(e).__name__, "detail": str(e)[:300]}
            print(f"FAIL {name:40s} {type(e).__name__}: {str(e)[:120]}")
            return None, item

    for name, fn in [("score_top_longs", T.fetch_score_top_longs), ("score_backtest", T.fetch_score_backtest),
                     ("score_basket_history", T.fetch_score_basket_history), ("watchlists", T.fetch_watchlists)]:
        p, it = grab(name, fn); index["items"].append(it)
        if name == "watchlists" and isinstance(p, (dict, list)):
            rows = p if isinstance(p, list) else (p.get("watchlists") or p.get("data") or p.get("items") or [])
            for w in (rows or [])[:5]:
                wid = w.get("id") if isinstance(w, dict) else None
                if wid is not None:
                    _, it2 = grab(f"dashboard_watchlist_{wid}", T.fetch_watchlist_dashboard, wid); index["items"].append(it2)
    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    for t in tickers:
        for kind, fn in (("detail", T.fetch_stock_detail), ("intraday", T.fetch_stock_intraday), ("gex", T.fetch_stock_gex)):
            _, it = grab(f"stock_{t}_{kind}", fn, t); index["items"].append(it)
    try:
        sp, it = grab("sparklines", T.fetch_sparklines, ",".join(tickers)); index["items"].append(it)
    except Exception:
        pass
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    ok = sum(1 for i in index["items"] if "error" not in i)
    print(f"CAPTURA_TMAD_API_OK {ok}/{len(index['items'])} -> {a.out}")


if __name__ == "__main__":
    main()
