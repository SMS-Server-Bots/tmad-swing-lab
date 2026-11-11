"""Lista EOD de recorrido esperado (MOVERS_V1) y regla del hueco de apertura (GAP_FADE_V1), en modo papel.

MOVERS_V1  -> qué acciones se van a mover y cuánto (NO la dirección). Modelo congelado en movers_v1.json:
              volatilidad de 10 días calibrada; el GEX y el RVOL aportan ~0,6 % de R² fuera de muestra.
MOVERS_V2  -> probabilidad de que la acción TOQUE +5 % o −5 % en las 5 sesiones siguientes (el objetivo del bot),
              logística congelada en movers_v2.json (AUC fuera de muestra 0,77; decil alto ~88 % frente a 13 % del
              bajo). Sigue siendo tamaño, no dirección: el sentido del toque no es predecible con estos datos.
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
_M2 = json.load(open(os.path.join(os.path.dirname(__file__), "movers_v2.json")))
MOVERS_V2_FREEZE = "2026-11-11"
TOUCH_PCT = 5.0
TOUCH_SESSIONS = 5


def touch_prob(df: pd.DataFrame) -> pd.Series:
    """P(tocar ±5 % en 5 sesiones) con el modelo congelado MOVERS_V2."""
    num = lambda c: pd.to_numeric(df.get(c), errors="coerce") if c in df.columns else pd.Series(np.nan, index=df.index)
    wr = num("wall_range_pct").abs()
    gex = num("total_gex")
    X = pd.DataFrame({
        "log_vol10": np.log(num("vol10").clip(lower=0.3)),
        "log_rvol": np.log1p(num("rvol").clip(lower=0, upper=20).fillna(1)),
        "log_wall_range": np.log(wr.clip(lower=0.5, upper=200).fillna(wr.median() if wr.notna().any() else 10.0)),
        "abs_chg": num("change_percent").abs().clip(upper=30).fillna(0),
        "gex_neg": (gex < 0).astype(float),
        "log_abs_gex": np.log10(1 + gex.abs()).fillna(0),
    }, index=df.index)
    z = _M2["intercept"] + sum(_M2["coef"][k] * (X[k] - _M2["mean"][k]) / _M2["std"][k] for k in _M2["features"])
    return 1.0 / (1.0 + np.exp(-z))


def expected_move(vol10_pct: pd.Series) -> pd.Series:
    """Movimiento absoluto esperado (fracción) de cierre a cierre al día siguiente."""
    v = vol10_pct.clip(lower=0.3)
    return np.exp(_M["a"] + _M["b"] * np.log(v)) - 0.002


def movers_list(feats: pd.DataFrame, date: str, top: int = 20) -> pd.DataFrame:
    day = feats[(feats.trade_date == date) & universe(feats) & feats.vol10.notna()].copy()
    day["exp_move"] = expected_move(day.vol10)
    day["move_p80"] = day.exp_move * _M["p80_multiplier"]
    day["p_touch5"] = touch_prob(day)
    cols = ["ticker", "current_price", "change_percent", "vol10", "p_touch5", "exp_move", "move_p80", "call_wall", "put_wall", "total_gex", "trend"]
    return day.sort_values("p_touch5", ascending=False).head(top)[[c for c in cols if c in day.columns]]


def movers_tracking(feats: pd.DataFrame, px: pd.DataFrame, top: int = 20) -> pd.DataFrame:
    """Para cada señal con 5 sesiones de precios: ¿tocó ±5 % el top de MOVERS_V2? Entrada = apertura siguiente.
    Con máx/mín de las capturas del Scanner (subestima un poco los toques)."""
    cal = D.sessions(min(feats.trade_date.min(), px.date.min()), max(feats.trade_date.max(), px.date.max()))
    si = {d: i for i, d in enumerate(cal)}
    pxi = px.set_index(["ticker", "date"])
    rows = []
    for d in sorted(feats.trade_date.unique()):
        if d not in si or si[d] + TOUCH_SESSIONS >= len(cal):
            continue
        nxt = cal[si[d] + 1: si[d] + 1 + TOUCH_SESSIONS]
        if nxt[-1] > px.date.max():
            continue
        day = feats[(feats.trade_date == d) & universe(feats) & feats.vol10.notna()].copy()
        if day.empty:
            continue
        day["p"] = touch_prob(day)
        day["sel"] = day.p.rank(ascending=False, method="first") <= top
        for _, r in day.iterrows():
            try:
                fut = pxi.loc[[(r.ticker, x) for x in nxt]]
            except KeyError:
                continue
            e = fut.open_px.iloc[0] if pd.notna(fut.open_px.iloc[0]) else fut.close.iloc[0]
            if not e or pd.isna(e):
                continue
            up = fut.hi.max() / e - 1 >= TOUCH_PCT / 100
            dn = fut.lo.min() / e - 1 <= -TOUCH_PCT / 100
            rows.append((d, r.ticker, bool(r.sel), float(r.p), bool(up or dn), bool(up), bool(dn)))
    t = pd.DataFrame(rows, columns=["date", "ticker", "selected", "p_touch5", "touched", "touched_up", "touched_down"])
    if len(t):
        t["mode"] = np.where(t.date >= MOVERS_V2_FREEZE, "PROSPECTIVE", "HISTORICAL")
    return t


def movers_scoreboard(t: pd.DataFrame) -> str:
    out = ["## MOVERS_V2 — ¿se movió lo previsto? (toque ±5 % en 5 sesiones)", "",
           "| Modo | Días | Top-20: prob. media | Top-20: tocaron | …al alza | …a la baja | Universo: tocaron |", "|---|---:|---:|---:|---:|---:|---:|"]
    for mode in ("PROSPECTIVE", "HISTORICAL"):
        m = t[t["mode"] == mode] if len(t) else t
        if m is None or m.empty:
            out.append(f"| {mode} | 0 | — | — | — | — | — |"); continue
        s = m[m.selected]
        out.append(f"| {mode} | {m.date.nunique()} | {s.p_touch5.mean()*100:.0f} % | {s.touched.mean()*100:.0f} % | "
                   f"{s.touched_up.mean()*100:.0f} % | {s.touched_down.mean()*100:.0f} % | {m.touched.mean()*100:.0f} % |")
    out += ["", "Mide si el detector de movimiento acierta el TAMAÑO. Que toque al alza o a la baja es ~mitad y mitad: no da dirección."]
    return "\n".join(out)


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
    lines = [f"## Detector de movimiento — señal {date} (MOVERS_V2 + MOVERS_V1)", "",
             "Dice **cuánto** se puede mover cada acción, **no hacia dónde**. P80 = movimiento que solo se supera 1 de cada 5 días (referencia para el stop).", "",
             "| # | Ticker | Cierre | Var. día | P(toca ±5 % en 5 ses.) | Recorrido esperado mañana | P80 | Call wall | Put wall | Trend CORE |", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for i, (_, r) in enumerate(m.iterrows(), 1):
        cw = f"{r.call_wall:.2f}" if pd.notna(r.get("call_wall")) else "—"
        pw = f"{r.put_wall:.2f}" if pd.notna(r.get("put_wall")) else "—"
        pt = f"{r.p_touch5*100:.0f} %" if pd.notna(r.get("p_touch5")) else "—"
        lines.append(f"| {i} | {r.ticker} | {r.current_price:.2f} | {r.change_percent:+.2f} % | {pt} | ±{r.exp_move*100:.1f} % | ±{r.move_p80*100:.1f} % | {cw} | {pw} | {r.get('trend', '')} |")
    return "\n".join(lines)
