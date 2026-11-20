"""TOP10 EOD calibrado: modelo walk-forward, lista diaria y comparación con los controles.

Para cada lado (LARGO / CORTO) se estiman dos probabilidades con el almacén de variables:
  p_win  = P(toca el objetivo 1,5·ATR antes que el stop 1·ATR en 5 sesiones)
  p_loss = P(toca antes el stop)
y el valor esperado en R:  EV = 1,5·p_win − 1·p_loss + (1 − p_win − p_loss)·R_tiempo
(R_tiempo = media de R de las salidas por tiempo en el entrenamiento). El TOP10 son las 10 acciones con
mayor EV de su mejor lado.

Walk-forward sin mirar al futuro: se reentrena cada REFIT sesiones solo con señales cuya etiqueta ya estaba
cerrada (t + HORIZON + 1 < sesión de predicción). Calibración isotónica con el último 25 % de las fechas de
entrenamiento. Cada versión del modelo tiene una huella; si se cambia algo, es otra versión y su marcador
empieza de cero.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

from . import labels as LB
from .store import feature_columns

MODEL_VERSION = "TOP10_GBM_V1"
TOP_N = 10
MIN_TRAIN_SESSIONS = 25
REFIT = 5
MIN_PRICE = 5.0
GBM = dict(max_depth=3, learning_rate=0.05, max_iter=250, min_samples_leaf=300, l2_regularization=1.0,
           max_features=0.5, random_state=7)


def fingerprint(cols: list[str]) -> str:
    spec = repr((MODEL_VERSION, TOP_N, MIN_TRAIN_SESSIONS, REFIT, MIN_PRICE, sorted(GBM.items()), LB.TP_ATR, LB.SL_ATR,
                 LB.HORIZON, LB.COST, sorted(cols)))
    return hashlib.sha256(spec.encode()).hexdigest()[:12]


def universe(s: pd.DataFrame) -> pd.Series:
    """Acciones >= 5 $, con cotización viva. Fuera las ordinarias extranjeras OTC (5 letras acabadas en F:
    RNMBF, BABAF…): poco líquidas y casi imposibles de vender en corto."""
    otc = (s.ticker.str.len() == 5) & s.ticker.str.endswith("F")
    return ((s.category == "Stocks") & (s.current_price >= MIN_PRICE) & s.atr10.notna()
            & (s.change_percent.abs() > 0) & ~otc)


class SideModel:
    def __init__(self):
        self.win = HistGradientBoostingClassifier(**GBM)
        self.loss = HistGradientBoostingClassifier(**GBM)
        self.iso_w = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
        self.iso_l = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1)
        self.r_timeout = 0.0

    def fit(self, X: pd.DataFrame, res: pd.Series, R: pd.Series, dates: pd.Series):
        ud = np.sort(dates.unique()); cut = ud[int(len(ud) * 0.75)]
        fit_m, cal_m = dates < cut, dates >= cut
        yw, yl = (res == 1).astype(int), (res == 0).astype(int)
        self.win.fit(X[fit_m], yw[fit_m]); self.loss.fit(X[fit_m], yl[fit_m])
        self.iso_w.fit(self.win.predict_proba(X[cal_m])[:, 1], yw[cal_m])
        self.iso_l.fit(self.loss.predict_proba(X[cal_m])[:, 1], yl[cal_m])
        # Sin reentrenar con el tramo de calibración: la isotónica solo vale para el modelo que no lo ha visto.
        to = res == 0.5
        self.r_timeout = float(R[to].mean()) if to.any() else 0.0
        return self

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        rw, rl = self.win.predict_proba(X)[:, 1], self.loss.predict_proba(X)[:, 1]
        pw, pl = self.iso_w.predict(rw), self.iso_l.predict(rl)
        tot = pw + pl
        scale = np.where(tot > 1, 1 / tot, 1.0)
        pw, pl = pw * scale, pl * scale
        ev = LB.TP_ATR / LB.SL_ATR * pw - pl + (1 - pw - pl) * self.r_timeout
        # La isotónica da escalones (empates); el valor sin calibrar solo desempata el orden.
        ev = ev + 1e-6 * (rw - rl)
        return pd.DataFrame({"p_win": pw, "p_loss": pl, "ev": ev, "raw_win": rw}, index=X.index)


def fit_side_models(train: pd.DataFrame, cols: list[str]) -> dict:
    ud = np.sort(train.trade_date.unique()); early = train[train.trade_date < ud[int(len(ud) * 0.75)]]
    cols = [c for c in cols if early[c].nunique() > 1]       # fuera columnas vacías o constantes en este tramo
    out = {"cols": cols}
    for side in ("long", "short"):
        m = train[f"{side}_res"].notna()
        t = train[m]
        out[side] = SideModel().fit(t[cols], t[f"{side}_res"], t[f"{side}_R"], t.trade_date)
    return out


def score_day(models: dict, day: pd.DataFrame, cols: list[str] | None = None) -> pd.DataFrame:
    cols = models["cols"]
    pl, ps = models["long"].predict(day[cols]), models["short"].predict(day[cols])
    d = day[["ticker", "trade_date", "current_price", "atr10"]].copy()
    d[["p_win_L", "p_loss_L", "ev_L", "raw_L"]] = pl.values
    d[["p_win_S", "p_loss_S", "ev_S", "raw_S"]] = ps.values
    d["side"] = np.where(d.ev_L >= d.ev_S, "LONG", "SHORT")
    d["ev"] = d[["ev_L", "ev_S"]].max(axis=1)
    d["p_win"] = np.where(d.side == "LONG", d.p_win_L, d.p_win_S)
    d["p_loss"] = np.where(d.side == "LONG", d.p_loss_L, d.p_loss_S)
    d["p_move"] = d.p_win_L + d.p_win_S + 1e-6 * (d.raw_L + d.raw_S)   # magnitud: prob. de tocar algún objetivo
    return d


def walk_forward(s: pd.DataFrame, L: pd.DataFrame, start_si: int | None = None) -> pd.DataFrame:
    """Predicciones fuera de muestra para cada sesión con etiqueta disponible."""
    cols = feature_columns(s)
    cols = [c for c in cols if s[c].notna().mean() > 0.05]
    D = s[universe(s)].merge(L, on=["ticker", "trade_date"], how="left")
    sis = np.sort(D.si.unique())
    first = start_si if start_si is not None else sis[0] + MIN_TRAIN_SESSIONS
    preds, models, last_fit = [], None, None
    for si in sis[sis >= first]:
        if models is None or si - last_fit >= REFIT:
            train = D[(D.si + LB.HORIZON + 1 < si) & D.long_res.notna()]
            models, last_fit = fit_side_models(train, cols), si
        day = D[D.si == si]
        sc = score_day(models, day, cols)
        sc = sc.join(day[["long_res", "short_res", "long_R", "short_R", "long_ret", "short_ret", "move_atr"]])
        preds.append(sc)
    P = pd.concat(preds)
    P.attrs["cols"] = cols
    return P


def realized(P: pd.DataFrame, side_col: str = "side") -> pd.DataFrame:
    long = P[side_col] == "LONG"
    return pd.DataFrame({"R": np.where(long, P.long_R, P.short_R), "ret": np.where(long, P.long_ret, P.short_ret),
                         "res": np.where(long, P.long_res, P.short_res)}, index=P.index)


def evaluate(P: pd.DataFrame, n: int = TOP_N, seed: int = 11) -> pd.DataFrame:
    """Compara el TOP10 con controles, por fecha (un valor por día) y t de Newey-West (lag = horizonte)."""
    rng = np.random.default_rng(seed)
    P = P[P.long_res.notna()].copy()
    P = P.join(realized(P))
    rows = {}

    def pick(name, fn):
        rows[name] = pd.concat([fn(d.assign(trade_date=k)) for k, d in P.groupby("trade_date")])

    pick("TOP10 modelo", lambda d: d.nlargest(n, "ev"))
    pick("Azar (acción y lado)", lambda d: d.sample(n, random_state=int(rng.integers(1e9))).assign(
        side=lambda x: np.where(rng.random(len(x)) < 0.5, "LONG", "SHORT")))
    pick("Magnitud + lado al azar", lambda d: d.nlargest(n, "p_move").assign(
        side=lambda x: np.where(rng.random(len(x)) < 0.5, "LONG", "SHORT")))
    pick("Magnitud + lado del modelo", lambda d: d.nlargest(n, "p_move"))
    pick("Solo cortos, más ATR", lambda d: d.nlargest(n, "atr10").assign(side="SHORT"))
    out = []
    for name, g in rows.items():
        g = g.drop(columns=["R", "ret", "res"], errors="ignore").join(realized(g), rsuffix="")
        daily = g.groupby("trade_date").agg(R=("R", "mean"), ret=("ret", "mean"), win=("res", lambda x: (x == 1).mean()),
                                            stop=("res", lambda x: (x == 0).mean()))
        out.append(dict(estrategia=name, dias=len(daily), operaciones=len(g), R_medio=daily.R.mean(),
                        ret_medio_pct=daily.ret.mean() * 100, objetivo_pct=daily.win.mean() * 100,
                        stop_pct=daily.stop.mean() * 100, t_NW=nw_t(daily.R.values, LB.HORIZON),
                        largos_pct=(g.side == "LONG").mean() * 100))
    return pd.DataFrame(out)


def nw_t(x: np.ndarray, lag: int) -> float:
    x = np.asarray(x, float); x = x[~np.isnan(x)]; n = len(x)
    if n < 5:
        return np.nan
    e = x - x.mean(); v = e @ e / n
    for l in range(1, lag + 1):
        v += 2 * (1 - l / (lag + 1)) * (e[l:] @ e[:-l]) / n
    return x.mean() / np.sqrt(v / n)


def calibration(P: pd.DataFrame, bins: int = 5) -> pd.DataFrame:
    """Probabilidad prevista vs frecuencia real de tocar el objetivo, en las acciones elegidas por cada lado."""
    rows = []
    for side, s in (("LONG", "L"), ("SHORT", "S")):
        d = P[P[f"{side.lower()}_res"].notna()]
        q = pd.qcut(d[f"p_win_{s}"], bins, duplicates="drop")
        g = d.groupby(q, observed=True)
        for k, x in g:
            rows.append(dict(lado=side, tramo=str(k), n=len(x), p_prevista=x[f"p_win_{s}"].mean(),
                             p_real=(x[f"{side.lower()}_res"] == 1).mean(), R_real=x[f"{side.lower()}_R"].mean()))
    return pd.DataFrame(rows)
