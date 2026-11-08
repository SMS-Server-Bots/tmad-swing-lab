"""Variables conocidas al cierre EOD de la sesión t (sin información futura)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_features(s: pd.DataFrame, intraday_range: pd.DataFrame | None = None) -> pd.DataFrame:
    s = s.sort_values(["ticker", "trade_date"]).copy()
    if "si" not in s:
        from .data import sessions
        cal = sessions(s.trade_date.min(), s.trade_date.max()); idx = {d: i for i, d in enumerate(cal)}
        s["si"] = s.trade_date.map(idx)
    g = s.groupby("ticker")
    for k in (1, 3, 5):
        prev, psi = g.current_price.shift(k), g.si.shift(k)
        s[f"past{k}"] = np.where((s.si - psi) <= k + 2, s.current_price / prev - 1, np.nan)
    s["vol10"] = g.change_percent.transform(lambda x: x.rolling(10, min_periods=5).std())
    hi10 = g.current_price.transform(lambda x: x.rolling(10, min_periods=5).max())
    s["dist_hi10"] = s.current_price / hi10 - 1
    sig = s.option_signal.fillna("")
    s["opt_bull"] = (sig.str.contains("Call Buying|Put Selling").astype(float)
                     - sig.str.contains("Put Buying|Call Selling").astype(float))
    # Liquidez conocida al cierre: el precio se movió ese día (excluye cotizaciones congeladas).
    s["live_ok"] = s.change_percent.abs() > 0
    if intraday_range is not None:
        r = intraday_range.set_index(["ticker", "date"])
        rng = (r.i_hi - r.i_lo).reindex(pd.MultiIndex.from_arrays([s.ticker, s.trade_date])).values
        s["live_ok"] &= np.nan_to_num(rng, nan=1.0) > 0
    return s


def universe(s: pd.DataFrame, min_price: float = 5.0) -> pd.Series:
    """Universo operable: acciones, precio >= min_price, con cotización viva."""
    return (s.category == "Stocks") & (s.current_price >= min_price) & s.live_ok
