"""«GEX Activity» de tmad.app (CALL/PUT SURGE y FADE) frente al retorno posterior.

La web la calcula en el navegador: SURGE si otmCallVolumeZ/otmPutVolumeZ >= +2, FADE si <= -2
(comprobado con el histórico de ZSL, 13 de 13 días). Los dos valores ya están en ticker_daily_history.

Retorno en exceso frente a la media del universo ese día; t con un valor por día (agrupado por fecha).
Entrada al cierre de la señal (t) y, más realista, al cierre del día siguiente (t+1).
Uso: python gex_activity.py <dir con tdh_2026-Www.sqlite>
"""
import sys, glob, sqlite3, numpy as np, pandas as pd

D = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/data/weekly"
parts = []
for f in sorted(glob.glob(f"{D}/tdh_*.sqlite")):
    c = sqlite3.connect(f)
    try:
        parts.append(pd.read_sql("select trade_date, ticker, category, current_price as px, otm_call_volume_z as cz, "
                                 "otm_put_volume_z as pz from tdh", c))
    except Exception:
        pass
X = pd.concat(parts).dropna(subset=["px"]).drop_duplicates(["trade_date", "ticker"], keep="last")
X = X[X.px >= 3]
dates = sorted(X.trade_date.unique())
P = X.pivot(index="trade_date", columns="ticker", values="px").reindex(dates)
print(f"{len(dates)} sesiones {dates[0]}–{dates[-1]}, {X.ticker.nunique()} tickers, filas con z: {X.cz.notna().sum()}")

def fwd(h, lag):
    r = P.shift(-(h + lag)) / P.shift(-lag) - 1
    r = r.where(r.abs() < 0.6)                      # splits / datos rotos
    return r.sub(r.mean(axis=1), axis=0)            # exceso sobre el universo del día

X = X.set_index(["trade_date", "ticker"])
lab = pd.Series("-", index=X.index)
lab[X.cz >= 2] = "CALL SURGE"; lab[X.pz >= 2] = "PUT SURGE"
lab[X.cz <= -2] = "CALL FADE"; lab[X.pz <= -2] = "PUT FADE"
both = ((X.cz >= 2) | (X.cz <= -2)) & ((X.pz >= 2) | (X.pz <= -2))
lab[both] = "AMBAS"
X["lab"] = lab
print(X.lab.value_counts().to_string())

half = dates[len(dates) // 2]
rows = []
for lag in (0, 1):
    for h in (1, 3, 5, 10):
        R = fwd(h, lag).stack().rename("ex")
        J = X.join(R, how="inner")
        for name, g in J.groupby("lab"):
            if name == "-":
                continue
            for per, gg in (("todo", g), ("jul–sep", g[g.index.get_level_values(0) < half]), ("sep–nov", g[g.index.get_level_values(0) >= half])):
                daily = gg.groupby(level=0).ex.mean()
                if len(daily) < 5:
                    continue
                rows.append(dict(entrada="cierre t" if lag == 0 else "cierre t+1", h=h, señal=name, periodo=per, n=len(gg), dias=len(daily),
                                 exceso_pct=daily.mean() * 100, t=daily.mean() / daily.std(ddof=1) * np.sqrt(len(daily)),
                                 acierto_pct=(gg.ex > 0).mean() * 100))
T = pd.DataFrame(rows)
T.to_csv("gex_activity_resultados.csv", index=False)
pd.set_option("display.width", 200)
for lag in ("cierre t", "cierre t+1"):
    print(f"\n=== Entrada al {lag}: exceso medio sobre el universo (%), t por días ===")
    S = T[(T.entrada == lag) & (T.periodo == "todo")].pivot(index="señal", columns="h", values=["exceso_pct", "t"]).round(2)
    print(S.to_string())
print("\n=== Estabilidad (entrada cierre t+1, 5 sesiones) ===")
print(T[(T.entrada == "cierre t+1") & (T.h == 5)][["señal", "periodo", "n", "dias", "exceso_pct", "t", "acierto_pct"]].round(2).to_string(index=False))
