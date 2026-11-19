"""Straddle: ¿se mueven las acciones más de lo que descuentan las opciones?

Datos (todo de las capturas del propio bot):
  - IV ATM: mapa de calor diario (session_date = cierre del día d), ZIP semanal W46 (flow_2026-W46.sqlite).
  - Precio a las 15:35 ET: velas de 1 min (hasta 10/11) y Scanner intradía de las 21:35 Madrid (11–18/11).
  - Selección del bot: las 75 acciones con más movimiento previsto (option_heatmap_intraday.selection_rank).

Para cada día d y horizonte h (sesiones):
  movimiento implícito E|r| = IV * sqrt(h/252) * sqrt(2/pi)
  cociente = |P(d+h)/P(d) - 1| / movimiento implícito      (>1: se movió más de lo que se pagaba)
  P&L aproximado de un straddle ATM comprado al cierre de d y vendido en d+h con la misma IV (Black-Scholes).

Uso: python straddle.py <dir_datos>   (dir con w46/2026-W46/*.sqlite e intra/scanner_intraday_*.sqlite)
"""
import sys, glob, json, zlib, sqlite3, math
import numpy as np, pandas as pd

BASE = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/data"
FLOW = f"{BASE}/w46/2026-W46/flow_2026-W46.sqlite"
SESSIONS = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18"]
START_DAYS = ["2026-11-10", "2026-11-11", "2026-11-12"]


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


# 1) Precio 15:35 ET y ATR10 desde velas de 1 min (historia hasta 10/11)
hist = []
for t, blob in ro(FLOW).execute("select ticker,bars_blob from intraday_bars_1m"):
    d = json.loads(zlib.decompress(blob))
    df = pd.DataFrame({"ts": d["ts"], "h": d["high"], "l": d["low"], "c": d["close"]})
    for k in "hlc":
        df[k] = pd.to_numeric(df[k], errors="coerce")
    df["date"] = df.ts.str[:10]
    df["m"] = df.ts.str[11:13].astype(int) * 60 + df.ts.str[14:16].astype(int)
    rth = df[(df.m >= 570) & (df.m < 960)]
    for dt, g in rth.groupby("date"):
        if len(g) < 30:
            continue
        upto = g[g.m <= 935]
        if upto.empty:
            continue
        hist.append(dict(ticker=t, date=dt, p=upto.c.iloc[-1], rng=(g.h.max() - g.l.min()) / g.c.iloc[-1]))
H = pd.DataFrame(hist).sort_values(["ticker", "date"])
atr = H.groupby("ticker").tail(10).groupby("ticker").rng.mean().rename("atr10")

# 2) Precio 15:35 ET desde el Scanner intradía (run 21:35 Madrid)
px = []
for p in sorted(glob.glob(f"{BASE}/intra/scanner_intraday_*.sqlite")):
    c = ro(p)
    rid, sd = c.execute("select id,snapshot_date from scanner_runs where captured_at_madrid like '%T21:35%' order by id desc limit 1").fetchone()
    for t, pr in c.execute("select ticker,current_price from scanner_snapshots where scanner_run_id=?", (rid,)):
        if pr:
            px.append(dict(ticker=t, date=sd, p=float(pr)))
P = pd.DataFrame(px).drop_duplicates(["ticker", "date"])
# control: 10/11 por las dos vías
chk = P[P.date == "2026-11-10"].merge(H[H.date == "2026-11-10"], on="ticker", suffixes=("_scan", "_bars"))
print("Control 10/11 Scanner 21:35 vs vela 15:35 ET: mediana |dif| %.3f%% (n=%d)" % (
    (chk.p_scan / chk.p_bars - 1).abs().median() * 100, len(chk)))
W = P.pivot(index="ticker", columns="date", values="p")[SESSIONS]

# 3) IV ATM por ticker y día (vencimiento más próximo de 5–45 días, 2 strikes más cercanos, media call/put)
ivr = []
for t, side, sd, spot, codec, blob in ro(FLOW).execute(
        "select ticker,side,session_date,spot,contracts_codec,contracts_blob from option_heatmap_snapshots "
        "where session_date in ('2026-11-10','2026-11-11','2026-11-12')"):
    if not spot or not blob:
        continue
    x = pd.DataFrame(json.loads(zlib.decompress(blob)))
    if x.empty or "impliedVolatility" not in x:
        continue
    x = x[(x.dte >= 5) & (x.dte <= 45) & (x.impliedVolatility > 0.10)]  # <10 % anual: datos rotos (p. ej. CABA 1,5 %)
    if x.empty:
        continue
    e = x.dte.min()
    y = x[x.dte == e].assign(dist=lambda z: (z.strike / spot - 1).abs()).nsmallest(2, "dist")
    if y.dist.min() < 0.05:
        ivr.append(dict(ticker=t, date=sd, iv=y.impliedVolatility.mean(), dte=int(e)))
IV = pd.DataFrame(ivr).groupby(["ticker", "date"]).agg(iv=("iv", "mean"), dte=("dte", "min")).reset_index()

# 4) Selección del bot (75 de más movimiento previsto) del día siguiente a d, primer slot
sel = pd.read_sql("select session_date as date, ticker, min(selection_rank) as rank from option_heatmap_intraday "
                  "where ticker not in ('SPY','QQQ','IWM') group by 1,2", ro(FLOW))


def ncdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def straddle(S, K, T, s):
    if T <= 0:
        return abs(S - K)
    d1 = (math.log(S / K) + 0.5 * s * s * T) / (s * math.sqrt(T)); d2 = d1 - s * math.sqrt(T)
    call = S * ncdf(d1) - K * ncdf(d2)
    return call + (call - S + K)  # paridad con r=0


rows = []
for d in START_DAYS:
    i0 = SESSIONS.index(d)
    nxt = SESSIONS[i0 + 1]
    selset = set(sel[sel.date == nxt].ticker)
    for h in (1, 2, 3, 4, 5):
        if i0 + h >= len(SESSIONS):
            break
        e = SESSIONS[i0 + h]
        M = IV[IV.date == d].set_index("ticker").join(W[[d, e]].rename(columns={d: "p0", e: "p1"}), how="inner").dropna()
        M = M[(M.p0 > 1)]
        M["r"] = M.p1 / M.p0 - 1
        M["imp"] = M.iv * np.sqrt(h / 252) * np.sqrt(2 / np.pi)
        M["ratio"] = M.r.abs() / M.imp
        T0 = M.dte / 365.0; T1 = (M.dte - (pd.Timestamp(e) - pd.Timestamp(d)).days) / 365.0
        M["pnl"] = [(straddle(p1, p0, t1, s) - straddle(p0, p0, t0, s)) / straddle(p0, p0, t0, s)
                    for p0, p1, t0, t1, s in zip(M.p0, M.p1, T0, T1, M.iv)]
        M["start"] = d; M["h"] = h; M["bot75"] = M.index.isin(selset)
        rows.append(M.reset_index())
R = pd.concat(rows).merge(atr.reset_index(), on="ticker", how="left")
R["dec"] = R.groupby(["start", "h"]).atr10.transform(lambda s: pd.qcut(s.rank(method="first"), 10, labels=False))
R.to_csv("straddle_rows.csv", index=False)

mk = W.loc[["SPY", "QQQ", "IWM"]] if set(["SPY", "QQQ", "IWM"]) <= set(W.index) else None
if mk is not None:
    print("\nMercado (15:35 ET):\n", (mk.div(mk["2026-11-10"], axis=0) - 1).mul(100).round(1).to_string())


def summ(g):
    return pd.Series(dict(n=len(g), iv=g.iv.median(), imp=g.imp.median() * 100, real=g.r.abs().median() * 100,
                          ratio_med=g.ratio.median(), pct_supera=(g.ratio > 1).mean() * 100,
                          pnl_med=g.pnl.median() * 100, pnl_media=g.pnl.mean() * 100))


for h in (4, 5):
    S = R[R.h == h]
    print(f"\n=== Horizonte {h} sesiones · días de inicio: {sorted(S.start.unique())} ===")
    print("Por decil de ATR10 (0 = menos movimiento):")
    print(S.groupby("dec").apply(summ).round(2).to_string())
    print("\nSelección del bot (75 de más movimiento previsto) vs resto:")
    print(S.groupby("bot75").apply(summ).round(2).to_string())
    print("\nPor día de inicio (decil 9 vs 0–6):")
    print(S.assign(grp=np.where(S.dec == 9, "dec9", np.where(S.dec <= 6, "dec0-6", "dec7-8")))
          .groupby(["start", "grp"]).apply(summ).round(2).to_string())
