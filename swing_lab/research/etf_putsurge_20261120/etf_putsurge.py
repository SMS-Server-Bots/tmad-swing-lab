"""Hipótesis de Raul: en los ETF principales, durante un movimiento continuista (CORE alcista),
un día de PUT SURGE anticipa una corrección al día siguiente.

PUT SURGE = otm_put_volume_z >= 2 (la regla de la web). CORE alcista = trend '↑'.
Datos: ticker_daily_history de los ZIP semanales (22/07–13/11) + junio del archivo legacy (1–9/06).
Retorno de cierre a cierre, absoluto (es la dirección del mercado lo que importa aquí).
"""
import sys, glob, sqlite3, numpy as np, pandas as pd

W = "/home/claude/data/weekly"; LEG = "/home/claude/data/legacy/ticker_daily_history_2026-06.csv"
cols = "trade_date,ticker,category,current_price,trend,momentum_score,otm_call_volume_z,otm_put_volume_z,option_signal"
parts = [pd.read_sql(f"select {cols} from tdh", sqlite3.connect(f)) for f in sorted(glob.glob(f"{W}/tdh_*.sqlite"))
         if sqlite3.connect(f).execute("select count(*) from sqlite_master where name='tdh'").fetchone()[0]]
leg = pd.read_csv(LEG, usecols=cols.split(","))
X = pd.concat(parts + [leg]).drop_duplicates(["trade_date", "ticker"], keep="last")
X = X[X.category.eq("ETFs") | X.ticker.isin(["SPY", "QQQ", "IWM", "DIA"])].copy()
X = X.sort_values(["ticker", "trade_date"])
g = X.groupby("ticker")
for h in (1, 2, 3, 5):
    X[f"r{h}"] = g.current_price.shift(-h) / X.current_price - 1
X["prev5"] = X.current_price / g.current_price.shift(5) - 1
X.loc[X.r5.abs() > 0.5, ["r1", "r2", "r3", "r5"]] = np.nan
X["ps"] = X.otm_put_volume_z >= 2
X["up"] = X.trend.eq("↑")
X["cont_up"] = X.up & (X.prev5 > 0)

def table(S, label):
    out = []
    for name, m in (("PUT SURGE + CORE ↑ (continuista)", S.ps & S.cont_up), ("PUT SURGE (cualquier tendencia)", S.ps),
                    ("CORE ↑ continuista sin PUT SURGE", S.cont_up & ~S.ps), ("Todos los días", S.ps | ~S.ps)):
        z = S[m]
        row = dict(grupo=name, n=len(z))
        for h in (1, 2, 3, 5):
            v = z[f"r{h}"].dropna()
            row[f"{h}d %"] = round(v.mean() * 100, 2) if len(v) else np.nan
            row[f"{h}d baja %"] = round((v < 0).mean() * 100, 0) if len(v) else np.nan
        out.append(row)
    print(f"\n=== {label} ===\n" + pd.DataFrame(out).to_string(index=False))

M = X[X.ticker.isin(["SPY", "QQQ", "IWM", "DIA"])]
print("Sesiones:", X.trade_date.nunique(), X.trade_date.min(), "→", X.trade_date.max(), "| ETF:", X.ticker.nunique())
table(M, "ETF principales (SPY, QQQ, IWM, DIA)")
ev = M[M.ps & M.cont_up][["trade_date", "ticker", "otm_put_volume_z", "momentum_score", "prev5", "r1", "r3"]]
print("\nEventos en los ETF principales:\n" + ev.assign(prev5=(ev.prev5*100).round(1), r1=(ev.r1*100).round(2), r3=(ev.r3*100).round(2)).to_string(index=False))
table(X, f"Todos los ETF ({X.ticker.nunique()})")
# por fecha (los ETF caen juntos): un valor por día de evento
E = X[X.ps & X.cont_up].groupby("trade_date").r1.mean().dropna()
B = X[~X.ps & X.cont_up].groupby("trade_date").r1.mean().dropna()
d = E - B.reindex(E.index)
d = d.dropna()
print(f"\nTodos los ETF, PUT SURGE+↑ menos ↑ sin surge el mismo día, 1 sesión: {d.mean()*100:+.2f} % "
      f"(t {d.mean()/d.std(ddof=1)*np.sqrt(len(d)):+.2f}, {len(d)} días)")
for h in (3, 5):
    E = X[X.ps & X.cont_up].groupby("trade_date")[f"r{h}"].mean()
    B = X[~X.ps & X.cont_up].groupby("trade_date")[f"r{h}"].mean()
    d = (E - B.reindex(E.index)).dropna()
    top = E.groupby(E.index).size()
    print(f"Todos los ETF, diferencia por día a {h} sesiones: {d.mean()*100:+.2f} % (t {d.mean()/d.std(ddof=1)*np.sqrt(len(d)):+.2f}, {len(d)} días); "
          f"eventos por mes: {X[X.ps & X.cont_up].trade_date.str[:7].value_counts().sort_index().to_dict()}")
