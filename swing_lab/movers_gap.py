"""Lista EOD de recorrido esperado (MOVERS_V1) y regla del hueco de apertura (GAP_FADE_V1), en modo papel.

MOVERS_V1  -> qué acciones se van a mover y cuánto (NO la dirección). Modelo congelado en movers_v1.json:
              volatilidad de 10 días calibrada; el GEX y el RVOL aportan ~0,6 % de R² fuera de muestra.
GAP_FADE_V1 -> dirección: si a las 09:35 una acción (precio >= 5 $) abre con hueco al alza de más de 3
              volatilidades diarias, CORTO a las 09:35 y cierre al final del día. Fijada el 11/11/2026 antes de
              cualquier dato posterior. Evidencia previa: 18 meses OHLCV a la apertura (t 4,7, gap_z > 2);
              Scanner 09:35 jul–oct (gap_z > 3: +0,73 %, t 2,3; en la 2ª mitad t 1,3). No demostrada.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from . import data as D
from .features import universe

FREEZE_DATE = "2026-11-11"
GAP_Z_MIN = 3.0
GAP_COST = 0.002
_M = json.load(open(os.path.join(os.path.dirname(__file__), "movers_v1.json")))


def expected_move(vol10_pct: pd.Series) -> pd.Series:
    """Movimiento absoluto esperado (fracción) de cierre a cierre al día siguiente."""
    v = vol10_pct.clip(lower=0.3)
    return np.exp(_M["a"] + _M["b"] * np.log(v)) - 0.002


def movers_list(feats: pd.DataFrame, date: str, top: int = 20) -> pd.DataFrame:
    day = feats[(feats.trade_date == date) & universe(feats) & feats.vol10.notna()].copy()
    day["exp_move"] = expected_move(day.vol10)
    day["move_p80"] = day.exp_move * _M["p80_multiplier"]
    cols = ["ticker", "current_price", "change_percent", "vol10", "exp_move", "move_p80", "call_wall", "put_wall", "total_gex", "trend"]
    return day.sort_values("exp_move", ascending=False).head(top)[[c for c in cols if c in day.columns]]


def gap_trades(feats: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """Operaciones en papel de GAP_FADE_V1 para cada sesión con snapshot de 09:35 y cierre conocidos."""
    cal = D.sessions(min(feats.trade_date.min(), px.date.min()), max(feats.trade_date.max(), px.date.max()))
    si = {d: i for i, d in enumerate(cal)}
    prev = feats[universe(feats) & feats.vol10.notna()][["ticker", "trade_date", "current_price", "vol10"]].copy()
    prev["date"] = prev.trade_date.map(lambda d: cal[si[d] + 1] if d in si and si[d] + 1 < len(cal) else None)
    day = px[["ticker", "date", "open_px", "close"]].dropna()
    x = prev.merge(day, on=["ticker", "date"], how="inner")
    x["gap"] = x.open_px / x.current_price - 1
    x["gap_z"] = x.gap * 100 / x.vol10
    x["oc"] = x.close / x.open_px - 1
    x = x[(x.gap.abs() < 0.6) & (x.oc.abs() < 0.6)]
    x["univ_oc"] = x.groupby("date").oc.transform("mean")
    t = x[x.gap_z > GAP_Z_MIN].copy()
    t["side"] = "SHORT"
    t["ret_net"] = -t.oc - GAP_COST
    t["alpha"] = -(t.oc - t.univ_oc)
    t["mode"] = np.where(t.date >= FREEZE_DATE, "PROSPECTIVE", "HISTORICAL")
    return t[["date", "ticker", "side", "current_price", "open_px", "close", "gap", "gap_z", "ret_net", "alpha", "mode"]].rename(
        columns={"current_price": "prev_close", "open_px": "entry_0935", "close": "exit_close"}).sort_values(["date", "gap_z"], ascending=[True, False])


def gap_scoreboard(t: pd.DataFrame) -> str:
    out = ["## GAP_FADE_V1 (corto 09:35 → cierre, hueco > 3 vol.)", "",
           "| Modo | Días | Operaciones | Ret. neto medio | Alfa medio | t alfa (por día) | % días ganadores |", "|---|---:|---:|---:|---:|---:|---:|"]
    for mode in ("PROSPECTIVE", "HISTORICAL"):
        m = t[t["mode"] == mode]
        if m.empty:
            out.append(f"| {mode} | 0 | 0 | — | — | — | — |"); continue
        d = m.groupby("date").agg(ret=("ret_net", "mean"), alpha=("alpha", "mean"))
        tt = d.alpha.mean() / d.alpha.std() * np.sqrt(len(d)) if len(d) > 2 else float("nan")
        out.append(f"| {mode} | {len(d)} | {len(m)} | {d.ret.mean()*100:+.2f} % | {d.alpha.mean()*100:+.2f} % | {tt:+.2f} | {(d.ret > 0).mean()*100:.0f} % |")
    out += ["", "Promoción: ≥ 40 días PROSPECTIVE con alfa t ≥ 2. Hasta entonces, solo papel."]
    return "\n".join(out)


def movers_markdown(m: pd.DataFrame, date: str) -> str:
    lines = [f"## Recorrido esperado mañana — señal {date} (MOVERS_V1)", "",
             "Dice **cuánto** se puede mover cada acción, **no hacia dónde**. P80 = movimiento que solo se supera 1 de cada 5 días (referencia para el stop).", "",
             "| # | Ticker | Cierre | Var. día | Recorrido esperado | P80 | Call wall | Put wall | Trend CORE |", "|---:|---|---:|---:|---:|---:|---:|---:|---|"]
    for i, (_, r) in enumerate(m.iterrows(), 1):
        cw = f"{r.call_wall:.2f}" if pd.notna(r.get("call_wall")) else "—"
        pw = f"{r.put_wall:.2f}" if pd.notna(r.get("put_wall")) else "—"
        lines.append(f"| {i} | {r.ticker} | {r.current_price:.2f} | {r.change_percent:+.2f} % | ±{r.exp_move*100:.1f} % | ±{r.move_p80*100:.1f} % | {cw} | {pw} | {r.get('trend', '')} |")
    return "\n".join(lines)
