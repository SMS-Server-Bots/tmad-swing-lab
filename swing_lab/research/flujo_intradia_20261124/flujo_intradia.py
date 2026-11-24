"""Primera lectura: ¿el flujo de opciones durante la sesión anticipa el resto del día o el día siguiente?

Datos: option_heatmap_intraday (W46–W47, 11–20/11): por acción, lado y hora, volumen OTM acumulado del día,
cambio de GEX y cambio de OI. Precio de entrada = spot de la propia captura. Salidas: cierre del día y cierre del día
siguiente (foto canónica del Scanner). Todo frente a SPY en la misma ventana.

Señales (positivo = sesgo alcista):
  vol_imb   = (vol calls OTM − vol puts OTM) / suma, acumulado del día
  dvol_imb  = lo mismo con el volumen de la ÚLTIMA hora (diferencia entre capturas)
  gex_imb   = (cambio GEX calls − |cambio GEX puts|) / (|calls| + |puts|)
  oi_imb    = (cambio OI OTM calls − cambio OI OTM puts) / suma de absolutos
Medida: correlación de rangos entre señal y retorno posterior, en cada (día, hora); media por día y t sobre días.
"""
import sqlite3, numpy as np, pandas as pd

F = ["/home/claude/data/w46/2026-W46/flow_2026-W46.sqlite", "/home/claude/data/w47/2026-W47/flow_2026-W47.sqlite"]
cols = "session_date,slot_et,ticker,side,spot,volume_otm,gex_change_total,oi_change_otm,selection_rank"
H = pd.concat([pd.read_sql(f"select {cols} from option_heatmap_intraday", sqlite3.connect(f)) for f in F])
H = H[H.session_date >= "2026-11-11"]
W = H.pivot_table(index=["session_date", "slot_et", "ticker"], columns="side",
                  values=["spot", "volume_otm", "gex_change_total", "oi_change_otm"], aggfunc="last")
W.columns = [f"{a}_{b}" for a, b in W.columns]
W = W.reset_index()
W["spot"] = W.spot_call.fillna(W.spot_put)

def imb(a, b):
    den = a.abs() + b.abs()
    return ((a - b) / den).where(den > 0)

W["vol_imb"] = imb(W.volume_otm_call, W.volume_otm_put)
W = W.sort_values(["session_date", "ticker", "slot_et"])
g = W.groupby(["session_date", "ticker"])
W["dvc"] = g.volume_otm_call.diff(); W["dvp"] = g.volume_otm_put.diff()
W["dvol_imb"] = imb(W.dvc.clip(lower=0), W.dvp.clip(lower=0))
W["gex_imb"] = imb(W.gex_change_total_call, W.gex_change_total_put.abs())
W["oi_imb"] = imb(W.oi_change_otm_call, W.oi_change_otm_put)

e = pd.read_pickle("/home/claude/data/eod_canon.pkl")[["ticker", "trade_date", "current_price"]]
e = e.sort_values(["ticker", "trade_date"]); e["next_close"] = e.groupby("ticker").current_price.shift(-1)
W = W.merge(e.rename(columns={"trade_date": "session_date", "current_price": "close"}), on=["session_date", "ticker"], how="left")
W["r_eod"] = W.close / W.spot - 1
W["r_next"] = W.next_close / W.spot - 1
spy = W[W.ticker == "SPY"].set_index(["session_date", "slot_et"])[["r_eod", "r_next"]]
W = W.join(spy, on=["session_date", "slot_et"], rsuffix="_spy")
W["x_eod"] = W.r_eod - W.r_eod_spy; W["x_next"] = W.r_next - W.r_next_spy
S = W[~W.ticker.isin(["SPY", "QQQ", "IWM"])].copy()
S = S[(S.r_eod.abs() < 0.4) & S.spot.gt(0)]

rows = []
for sig in ("vol_imb", "dvol_imb", "gex_imb", "oi_imb"):
    for tgt in ("x_eod", "x_next"):
        ic = S.dropna(subset=[sig, tgt]).groupby(["session_date", "slot_et"]).apply(
            lambda d: d[sig].corr(d[tgt], method="spearman") if len(d) > 15 else np.nan, include_groups=False).dropna()
        byday = ic.groupby(level=0).mean()
        q = S.dropna(subset=[sig, tgt]).copy()
        q["qq"] = q.groupby(["session_date", "slot_et"])[sig].transform(lambda x: pd.qcut(x.rank(method="first"), 5, labels=False))
        spread = q.groupby(["session_date", "slot_et"]).apply(lambda d: d.loc[d.qq == 4, tgt].mean() - d.loc[d.qq == 0, tgt].mean(), include_groups=False)
        sd = spread.groupby(level=0).mean()
        rows.append(dict(señal=sig, hasta="cierre del día" if tgt == "x_eod" else "cierre día siguiente",
                         IC_medio=byday.mean(), t_IC=byday.mean() / byday.std(ddof=1) * np.sqrt(len(byday)),
                         dias_IC_positivo=f"{(byday > 0).sum()}/{len(byday)}",
                         quintil_alto_menos_bajo_pct=sd.mean() * 100, t_spread=sd.mean() / sd.std(ddof=1) * np.sqrt(len(sd))))
pd.set_option("display.width", 220)
print(f"{S.session_date.nunique()} sesiones, {len(S)} observaciones acción-hora, {S.ticker.nunique()} acciones")
print(pd.DataFrame(rows).round(3).to_string(index=False))

# Mercado: desequilibrio de SPY/QQQ/IWM a cada hora frente a su retorno hasta el cierre
M = W[W.ticker.isin(["SPY", "QQQ", "IWM"])].dropna(subset=["vol_imb", "r_eod"])
print("\nETF principales: correlación (rangos) desequilibrio de volumen vs retorno hasta el cierre: "
      f"{M.vol_imb.corr(M.r_eod, method='spearman'):+.2f} (n={len(M)}, {M.session_date.nunique()} días)")
print(M.groupby("session_date").agg(vol_imb_media=("vol_imb", "mean"), r_eod_media=("r_eod", "mean")).round(3).to_string())
