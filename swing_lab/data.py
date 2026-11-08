"""Carga de datos TMAD para el laboratorio de swings.

Fuentes (todas de solo lectura):
  * ZIP semanales del Data Bot  (``data_bot/db/archive/2026-Wxx.zip``)  -> ``ticker_daily_history`` (EOD)
  * ZIP diarios intradía        (``data_bot/db/archive/intraday/.../intraday_YYYY-MM-DD.zip``) -> snapshots horarios
  * Opcional: base viva del VPS (``tmad_scanner_live.db``) para los últimos días aún no archivados.

Las ZIP se pueden descargar de las Releases de ``SMS-Server-Bots/tmad-dual-bot`` con ``fetch_archives``
(necesita ``gh`` autenticado) o leerse directamente de ``03_DATOS`` en el VPS.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import zipfile

import numpy as np
import pandas as pd

REPO = "SMS-Server-Bots/tmad-dual-bot"
NYSE_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}
NUMERIC = [
    "current_price", "change_percent", "total_gex", "gex_change", "gamma_flip", "rsi14", "rvol",
    "momentum_score", "iv_z_spread", "iv_change", "otm_call_volume_z", "otm_put_volume_z", "pc_ratio",
    "long_score", "put_wall_pct", "call_wall_pct", "wall_range_pct",
]


def sessions(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)
            if d.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS_2026]


# --------------------------------------------------------------------------- descarga
def _gh_json(path: str):
    return json.loads(subprocess.check_output(["gh", "api", path]))


def fetch_archives(dest: str, kinds=("weekly", "intraday")) -> None:
    """Descarga (con verificación SHA-256) las ZIP indexadas en la release ``tmad-data-index``."""
    os.makedirs(dest, exist_ok=True)
    idx_rel = _gh_json(f"repos/{REPO}/releases/tags/tmad-data-index")
    aid = idx_rel["assets"][0]["id"]
    raw = subprocess.check_output(["gh", "api", "-H", "Accept: application/octet-stream",
                                   f"repos/{REPO}/releases/assets/{aid}"])
    index = json.loads(raw.decode("utf-8-sig"))
    want = []
    for e in index["entries"]:
        p = e["relative_path"]
        if "weekly" in kinds and p.startswith("data_bot/db/archive/2026-W"):
            want.append((e, os.path.join(dest, "weekly")))
        if "intraday" in kinds and p.startswith("data_bot/db/archive/intraday/"):
            want.append((e, os.path.join(dest, "intraday")))
    assets: dict[str, dict[str, int]] = {}
    for e, d in want:
        os.makedirs(d, exist_ok=True)
        fn = os.path.join(d, os.path.basename(e["relative_path"]))
        if os.path.exists(fn) and hashlib.sha256(open(fn, "rb").read()).hexdigest() == e["sha256"]:
            continue
        tag = e["release_tag"]
        if tag not in assets:
            assets[tag] = {a["name"]: a["id"] for a in _gh_json(f"repos/{REPO}/releases/tags/{tag}")["assets"]}
        with open(fn, "wb") as f:
            subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                            f"repos/{REPO}/releases/assets/{assets[tag][e['asset']]}"], stdout=f, check=True)
        if hashlib.sha256(open(fn, "rb").read()).hexdigest() != e["sha256"]:
            raise RuntimeError(f"SHA mismatch {fn}")


# --------------------------------------------------------------------------- lectura
def _read_sqlite_from_zip(zpath: str, prefix: str, query: str) -> pd.DataFrame:
    zf = zipfile.ZipFile(zpath)
    names = [n for n in zf.namelist() if os.path.basename(n).startswith(prefix) and n.endswith(".sqlite")]
    if not names:
        return pd.DataFrame()
    with tempfile.TemporaryDirectory() as td:
        zf.extract(names[0], td)
        con = sqlite3.connect(os.path.join(td, names[0]))
        try:
            return pd.read_sql(query, con)
        finally:
            con.close()


def load_eod(weekly_dir: str, live_db: str | None = None) -> pd.DataFrame:
    """Panel EOD (una fila por ticker y sesión) desde ZIP semanales + base viva opcional."""
    q = "select * from ticker_daily_history"
    parts = [_read_sqlite_from_zip(z, "scanner_", q) for z in sorted(glob.glob(os.path.join(weekly_dir, "**", "20??-W*.zip"), recursive=True))]
    if live_db and os.path.exists(live_db):
        con = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
        parts.append(pd.read_sql(q, con)); con.close()
    p = pd.concat([x for x in parts if len(x)], ignore_index=True)
    p = p[~p.trade_date.isin(NYSE_HOLIDAYS_2026)]
    p = p.sort_values("updated_at_utc").drop_duplicates(["ticker", "trade_date"], keep="last")
    for c in NUMERIC:
        if c in p:
            p[c] = pd.to_numeric(p[c], errors="coerce")
    return p.reset_index(drop=True)


def load_intraday(intraday_dir: str, live_db: str | None = None) -> pd.DataFrame:
    """Snapshots intradía (hora ET) desde ZIP diarios + base viva opcional."""
    q = "select ticker,captured_at_utc,current_price from scanner_snapshots"
    parts = [_read_sqlite_from_zip(z, "scanner_intraday", q)
             for z in sorted(glob.glob(os.path.join(intraday_dir, "**", "intraday_*.zip"), recursive=True))]
    if live_db and os.path.exists(live_db):
        con = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
        parts.append(pd.read_sql(q, con)); con.close()
    i = pd.concat([x for x in parts if len(x)], ignore_index=True).drop_duplicates()
    i["current_price"] = pd.to_numeric(i.current_price, errors="coerce")
    i = i[i.current_price > 0]
    ts = pd.to_datetime(i.captured_at_utc, utc=True, format="ISO8601").dt.tz_convert("America/New_York")
    i["date"] = ts.dt.strftime("%Y-%m-%d")
    i["mins"] = ts.dt.hour * 60 + ts.dt.minute
    return i[(i.mins >= 570) & (i.mins <= 960)].reset_index(drop=True)


def daily_prices(eod: pd.DataFrame, intra: pd.DataFrame) -> pd.DataFrame:
    """Precio de apertura operativa (primer snapshot <=10:45 ET), máximo/mínimo muestreado y cierre por sesión."""
    first = (intra[intra.mins <= 645].sort_values("mins").groupby(["ticker", "date"])
             .current_price.first().rename("open_px"))
    hl = intra.groupby(["ticker", "date"]).current_price.agg(i_hi="max", i_lo="min")
    late = intra[intra.mins >= 900].sort_values("mins").groupby(["ticker", "date"]).current_price.last().rename("late_px")
    d = pd.concat([first, hl, late], axis=1).reset_index()
    e = eod[["ticker", "trade_date", "current_price"]].rename(columns={"trade_date": "date", "current_price": "eod_close"})
    d = d.merge(e, on=["ticker", "date"], how="outer")
    d["close"] = d.eod_close.fillna(d.late_px)
    d["hi"] = d[["i_hi", "close"]].max(axis=1)
    d["lo"] = d[["i_lo", "close"]].min(axis=1)
    return d


def attach_paths(eod: pd.DataFrame, px: pd.DataFrame, horizon: int = 5) -> pd.DataFrame:
    """Para cada señal EOD (t): entrada en la apertura de t+1 y trayectoria t+1..t+horizon."""
    cal = sessions(min(eod.trade_date.min(), px.date.min()), max(eod.trade_date.max(), px.date.max()))
    si = {d: i for i, d in enumerate(cal)}
    s = eod.copy(); s["si"] = s.trade_date.map(si)
    p = px.copy(); p["si"] = p.date.map(si); p = p[p.si.notna()]
    for k in range(1, horizon + 1):
        t = p[["ticker", "si", "open_px", "close", "hi", "lo"]].copy()
        t.columns = ["ticker", "si"] + [f"{c}_{k}" for c in ("open_px", "close", "hi", "lo")]
        t["si"] = t.si - k
        s = s.merge(t, on=["ticker", "si"], how="left")
    s["entry"] = s.open_px_1
    s["gap"] = s.entry / s.current_price - 1
    pc = [f"{c}_{j}" for j in range(1, horizon + 1) for c in ("close", "hi", "lo")]
    ratio = s[pc].div(s.entry, axis=0)
    steps = pd.concat([s[f"close_{j}"] / (s[f"close_{j-1}"] if j > 1 else s.entry) for j in range(1, horizon + 1)], axis=1)
    # Saltos imposibles (splits, ajustes, errores de dato): la trayectoria se descarta entera.
    s["path_bad"] = ((ratio.max(axis=1) > 2.0) | (ratio.min(axis=1) < 0.5) | (steps.max(axis=1) > 1.6)
                     | (steps.min(axis=1) < 0.6) | (s.gap.abs() > 0.4))
    s.loc[s.path_bad, pc] = np.nan
    for k in range(1, horizon + 1):
        s[f"r{k}"] = s[f"close_{k}"] / s.entry - 1
    return s
