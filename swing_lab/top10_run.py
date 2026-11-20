"""Ejecución diaria del sistema TOP10: lista congelada, seguimiento y marcador.

Estrategias en papel (CONGELADAS; un cambio = estrategia nueva con contador desde cero):
  TOP10_GBM_V1       10 acciones con mayor valor esperado del modelo (lado del modelo).
  MAG_SIDE_V1        10 con más probabilidad de movimiento (magnitud) y el lado que prefiere el modelo.
  SHORT_HIGHVOL_V1   10 con más volatilidad (atr10), siempre en corto. Mejor resultado del backtest 03/09–13/11
                     (+0,27 R/operación, t 2,2) y positivo en las dos mitades; puede ser solo el régimen bajista.
  RANDOM10           control: 10 al azar con lado al azar (semilla por fecha).
Reglas de salida comunes (labels.py): entrada en la apertura siguiente, objetivo 1,5·ATR, stop 1·ATR,
máximo 5 sesiones, costes 0,2 %. Solo cuentan para promoción las listas PROSPECTIVE (generadas antes de abrir).
Promoción: ≥ 40 sesiones PROSPECTIVE cerradas, t ≥ 2 y mejor que RANDOM10.
"""
from __future__ import annotations

import hashlib
import os

import numpy as np
import pandas as pd

from . import labels as LB
from . import top10 as T
from .store import build, feature_columns

STRATEGIES = ("TOP10_GBM_V1", "MAG_SIDE_V1", "SHORT_HIGHVOL_V1", "RANDOM10")
PICKS = os.path.join("ledger", "top10_picks.csv")
PROMO_SESSIONS, PROMO_T = 40, 2.0


def _seed(date: str) -> int:
    return int(hashlib.sha256(("RANDOM10" + date).encode()).hexdigest()[:8], 16)


def make_lists(s: pd.DataFrame, L: pd.DataFrame, date: str, mode: str) -> tuple[pd.DataFrame, dict]:
    D = s[T.universe(s)].merge(L, on=["ticker", "trade_date"], how="left")
    si = D.loc[D.trade_date == date, "si"].iloc[0]
    train = D[(D.si + LB.HORIZON + 1 < si) & D.long_res.notna()]
    cols = [c for c in feature_columns(s) if s[c].notna().mean() > 0.05]
    models = T.fit_side_models(train, cols)
    day = T.score_day(models, D[D.trade_date == date])
    fp = T.fingerprint(models["cols"])
    rng = np.random.default_rng(_seed(date))
    lists = {
        "TOP10_GBM_V1": day.nlargest(T.TOP_N, "ev"),
        "MAG_SIDE_V1": day.nlargest(T.TOP_N, "p_move"),
        "SHORT_HIGHVOL_V1": day.nlargest(T.TOP_N, "atr10").assign(side="SHORT"),
        "RANDOM10": day.sample(T.TOP_N, random_state=int(rng.integers(1e9))).assign(
            side=lambda x: np.where(rng.random(len(x)) < 0.5, "LONG", "SHORT")),
    }
    out = []
    for name, x in lists.items():
        x = x.copy(); x["strategy"] = name; x["rank"] = range(1, len(x) + 1)
        out.append(x)
    P = pd.concat(out)
    P["mode"], P["fingerprint"], P["train_rows"] = mode, fp, len(train)
    P["tp_pct"] = LB.TP_ATR * P.atr10.clip(lower=0.005) * 100
    P["sl_pct"] = LB.SL_ATR * P.atr10.clip(lower=0.005) * 100
    info = {"train_rows": len(train), "train_sessions": train.trade_date.nunique(), "features": len(models["cols"]),
            "fingerprint": fp}
    keep = ["trade_date", "strategy", "rank", "ticker", "side", "current_price", "atr10", "tp_pct", "sl_pct",
            "p_win", "p_loss", "ev", "p_move", "mode", "fingerprint", "train_rows"]
    return P[keep], info


def append(P: pd.DataFrame) -> int:
    os.makedirs("ledger", exist_ok=True)
    if os.path.exists(PICKS):
        old = pd.read_csv(PICKS)
        key = set(zip(old.trade_date, old.strategy))
        new = P[[(d, s) not in key for d, s in zip(P.trade_date, P.strategy)]]
        pd.concat([old, new]).to_csv(PICKS, index=False)
        return len(new)
    P.to_csv(PICKS, index=False)
    return len(P)


def track(L: pd.DataFrame) -> pd.DataFrame:
    if not os.path.exists(PICKS):
        return pd.DataFrame()
    pk = pd.read_csv(PICKS)
    m = pk.merge(L, on=["ticker", "trade_date"], how="left")
    lg = m.side == "LONG"
    m["res"] = np.where(lg, m.long_res, m.short_res)
    m["R"] = np.where(lg, m.long_R, m.short_R)
    m["ret"] = np.where(lg, m.long_ret, m.short_ret)
    m["days"] = np.where(lg, m.long_days, m.short_days)
    m["status"] = np.select([m.res == 1, m.res == 0, m.res == 0.5], ["OBJETIVO", "STOP", "TIEMPO"], "ABIERTA")
    m.to_csv(os.path.join("ledger", "top10_positions.csv"), index=False)
    return m


def scoreboard(m: pd.DataFrame) -> str:
    lines = ["## Marcador TOP10 (papel)", "",
             "Salida: objetivo 1,5·ATR, stop 1·ATR, máx. 5 sesiones, costes 0,2 %. R = ganancia en unidades de stop.",
             f"Promoción: ≥ {PROMO_SESSIONS} sesiones PROSPECTIVE cerradas, t ≥ {PROMO_T} y mejor que RANDOM10.", "",
             "| Estrategia | Modo | Sesiones cerradas | Operaciones | R medio | t | Objetivo | Stop | Abiertas |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    if m.empty:
        return "\n".join(lines + ["", "Sin listas todavía."])
    res = {}
    for (st, mode), g in m.groupby(["strategy", "mode"]):
        c = g[g.status != "ABIERTA"]
        daily = c.groupby("trade_date").R.mean()
        t = T.nw_t(daily.values, LB.HORIZON) if len(daily) >= 5 else np.nan
        res[(st, mode)] = (len(daily), daily.mean() if len(daily) else np.nan, t)
        lines.append(f"| {st} | {mode} | {len(daily)} | {len(c)} | {daily.mean() if len(daily) else float('nan'):+.3f} | "
                     f"{t:+.2f} | {(c.status == 'OBJETIVO').mean() * 100 if len(c) else float('nan'):.0f} % | "
                     f"{(c.status == 'STOP').mean() * 100 if len(c) else float('nan'):.0f} % | {(g.status == 'ABIERTA').sum()} |")
    rnd = res.get(("RANDOM10", "PROSPECTIVE"), (0, np.nan, np.nan))[1]
    lines += ["", "Promoción (solo PROSPECTIVE):"]
    for st in STRATEGIES[:-1]:
        n, r, t = res.get((st, "PROSPECTIVE"), (0, np.nan, np.nan))
        ok = n >= PROMO_SESSIONS and t >= PROMO_T and (np.isnan(rnd) or r > rnd)
        lines.append(f"- {st}: {n}/{PROMO_SESSIONS} sesiones, R {r:+.3f}, t {t:+.2f} → {'PROMOCIONABLE' if ok else 'en prueba'}")
    return "\n".join(lines)


def list_markdown(P: pd.DataFrame, date: str, info: dict) -> str:
    out = [f"# TOP10 EOD — señal {date} (PAPEL, sin ventaja demostrada)", "",
           f"Entrada: apertura de la sesión siguiente. Objetivo {LB.TP_ATR}·ATR, stop {LB.SL_ATR}·ATR, máximo {LB.HORIZON} sesiones.",
           f"Modelo `{T.MODEL_VERSION}` ({info['fingerprint']}): {info['features']} variables, entrenado con "
           f"{info['train_rows']:,} casos de {info['train_sessions']} sesiones.", ""]
    for st in STRATEGIES:
        x = P[P.strategy == st]
        out += [f"## {st}", "", "| # | Ticker | Lado | Cierre | Objetivo | Stop | P(objetivo) | P(stop) | EV (R) |",
                "|---:|---|---|---:|---:|---:|---:|---:|---:|"]
        for r in x.itertuples():
            out.append(f"| {r.rank} | {r.ticker} | {r.side} | {r.current_price:.2f} | {r.tp_pct:.1f} % | {r.sl_pct:.1f} % | "
                       f"{r.p_win * 100:.0f} % | {r.p_loss * 100:.0f} % | {r.ev:+.2f} |")
        out.append("")
    return "\n".join(out)


def run(eod: pd.DataFrame, px: pd.DataFrame, out_dir: str = "out", date: str | None = None) -> str:
    s = build(eod)
    L = LB.add_labels(s, px)
    dates = sorted(s.trade_date.unique())
    d = date or dates[-1]
    mode = "PROSPECTIVE" if date is None and px.date.max() <= d else "HISTORICAL"
    P, info = make_lists(s, L, d, mode)
    n = append(P)
    m = track(L)
    os.makedirs(out_dir, exist_ok=True)
    md = list_markdown(P, d, info)
    sb = scoreboard(m)
    open(os.path.join(out_dir, f"top10_{d}.md"), "w", encoding="utf-8-sig").write(md)
    open(os.path.join(out_dir, "top10_scoreboard.md"), "w", encoding="utf-8-sig").write(sb)
    print(f"{d}: TOP10 {n} filas nuevas ({mode}) | {info}")
    return md + "\n\n" + sb
