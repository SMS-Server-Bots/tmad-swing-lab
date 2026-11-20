"""Etiquetas de éxito con el criterio de Raul: si toca el objetivo, es un acierto (no hace falta esperar al cierre).

Para la señal EOD de t: entrada en la apertura operativa de t+1 (primera foto <= 10:45 ET). Cada sesión
t+1..t+H se mira el máximo y el mínimo (fotos horarias del Scanner) y el cierre:
  LARGO gana si toca entrada·(1 + TP·ATR) antes que entrada·(1 − SL·ATR); CORTO, al revés.
  Si el mismo día toca los dos, cuenta como pérdida (conservador: con fotos horarias no se sabe el orden).
  Si no toca ninguno, sale al cierre de t+H.
ATR = media de |cambio diario| de 10 sesiones (``atr10`` del almacén), conocido en t.
Resultado en unidades R (1R = distancia al stop) y en %, ya con costes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import attach_paths

TP_ATR = 1.5
SL_ATR = 1.0
HORIZON = 5
COST = 0.002          # ida y vuelta


def add_labels(feats: pd.DataFrame, px: pd.DataFrame, horizon: int = HORIZON, tp: float = TP_ATR,
               sl: float = SL_ATR, atr_col: str = "atr10") -> pd.DataFrame:
    f = feats[["ticker", "trade_date", "current_price", atr_col]].copy()
    f["current_price"] = pd.to_numeric(f.current_price, errors="coerce")
    px = px.copy()
    for c in ("open_px", "close", "hi", "lo"):
        px[c] = pd.to_numeric(px[c], errors="coerce")
    s = attach_paths(f, px, horizon)
    a = s[atr_col].clip(lower=0.005)
    e = s.entry
    out = {}
    for side, sgn in (("long", 1), ("short", -1)):
        tp_px = e * (1 + sgn * tp * a); sl_px = e * (1 - sgn * sl * a)
        res = pd.Series(np.nan, index=s.index); day = pd.Series(np.nan, index=s.index)
        exit_ret = pd.Series(np.nan, index=s.index)
        open_ = e.notna() & ~s.path_bad
        for k in range(1, horizon + 1):
            hi, lo, cl = s[f"hi_{k}"], s[f"lo_{k}"], s[f"close_{k}"]
            if sgn == 1:
                hit_tp, hit_sl = hi >= tp_px, lo <= sl_px
            else:
                hit_tp, hit_sl = lo <= tp_px, hi >= sl_px
            avail = open_ & res.isna() & cl.notna()
            loss = avail & hit_sl
            win = avail & hit_tp & ~hit_sl
            res[loss] = 0; exit_ret[loss] = -sl * a[loss]; day[loss] = k
            res[win] = 1; exit_ret[win] = tp * a[win]; day[win] = k
            if k == horizon:
                to = avail & ~hit_sl & ~hit_tp
                res[to] = 0.5; exit_ret[to] = sgn * (cl[to] / e[to] - 1); day[to] = k
        net = exit_ret - COST
        out[f"{side}_res"] = res                 # 1 objetivo, 0 stop, 0,5 salida por tiempo
        out[f"{side}_win"] = (res == 1).astype(float).where(res.notna())
        out[f"{side}_loss"] = (res == 0).astype(float).where(res.notna())
        out[f"{side}_ret"] = net
        out[f"{side}_R"] = net / (sl * a)
        out[f"{side}_days"] = day
    hi_cols = [f"hi_{k}" for k in range(1, horizon + 1)]; lo_cols = [f"lo_{k}" for k in range(1, horizon + 1)]
    out["mfe_up"] = s[hi_cols].max(axis=1) / e - 1
    out["mfe_dn"] = 1 - s[lo_cols].min(axis=1) / e
    out["move_atr"] = np.maximum(out["mfe_up"], out["mfe_dn"]) / a
    out["entry"] = e
    out["label_end_si"] = None
    L = pd.DataFrame(out, index=s.index)
    L["ticker"], L["trade_date"] = s.ticker.values, s.trade_date.values
    return L.drop(columns=["label_end_si"])
