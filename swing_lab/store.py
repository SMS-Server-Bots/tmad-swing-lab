"""Almacén de variables TOP10: una fila por ticker y sesión, todo conocido al cierre EOD de t.

Parte del panel EOD del Scanner (``ticker_daily_history``) y añade variables derivadas:
precio y volatilidad, cambios de CORE, opciones (z y sus cambios, GEX Activity), GEX y muros,
zonas de oferta/demanda, contexto de los ETF principales y amplitud del mercado, y percentiles del día.

Ninguna variable usa datos posteriores a t. Las ventanas por ticker se cortan si faltan sesiones
(``si`` = índice de sesión NYSE), para no mezclar huecos de captura con movimientos reales.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import sessions

MAIN_ETFS = ("SPY", "QQQ", "IWM", "DIA")
STORE_VERSION = "store_v1"

RAW_NUM = [
    "current_price", "change_percent", "total_gex", "gex_change", "rsi14", "rvol", "momentum_score",
    "iv_z_spread", "iv_change", "otm_call_volume_z", "otm_put_volume_z", "pc_ratio", "put_call_ratio",
    "long_score", "long_score_signals", "put_wall_pct", "call_wall_pct", "wall_range_pct",
    "put_wall_strength", "call_wall_strength", "put_wall_gex", "call_wall_gex", "gamma_flip", "vwap",
    "max_pos_strike", "max_neg_strike", "max_delta_gex_strike", "nearest_supply", "nearest_demand",
    "daily_supply_top", "daily_supply_bottom", "daily_demand_top", "daily_demand_bottom",
    "weekly_supply_top", "weekly_supply_bottom", "weekly_demand_top", "weekly_demand_bottom",
    "daily_supply_strength", "daily_demand_strength", "weekly_supply_strength", "weekly_demand_strength",
    "put_wall", "call_wall",
]
TREND_MAP = {"↑": 1.0, "→": 0.0, "↓": -1.0}
FLOW_LEVEL = {"Very Low": -2.0, "Low": -1.0, "High": 1.0, "Very High": 2.0}


def _flow_parts(s: pd.Series) -> pd.DataFrame:
    """unusual_flow_signal ('High Put GEX', 'Very High Call GEX', 'Divergent Flow'...) -> niveles por lado."""
    s = s.fillna("")
    lvl = pd.Series(0.0, index=s.index)
    for k in ("Very Low", "Very High", "Low", "High"):   # primero los compuestos
        m = s.str.startswith(k) & (lvl == 0)
        lvl[m] = FLOW_LEVEL[k]
    call = np.where(s.str.contains("Call") | s.str.contains("Both"), lvl, 0.0)
    put = np.where(s.str.contains("Put") | s.str.contains("Both"), lvl, 0.0)
    return pd.DataFrame({"flow_call_lvl": call, "flow_put_lvl": put,
                         "flow_divergent": s.str.contains("Divergent").astype(float)}, index=s.index)


def build(eod: pd.DataFrame) -> pd.DataFrame:
    s = eod.copy()
    for c in RAW_NUM:
        s[c] = pd.to_numeric(s[c], errors="coerce") if c in s else np.nan
    s = s[s.current_price > 0].copy()
    cal = sessions(s.trade_date.min(), s.trade_date.max())
    si = {d: i for i, d in enumerate(cal)}
    s["si"] = s.trade_date.map(si)
    s = s[s.si.notna()].sort_values(["ticker", "si"]).reset_index(drop=True)
    g = s.groupby("ticker", sort=False)
    px = s.current_price

    def lag(col, k):
        v, sk = g[col].shift(k), g.si.shift(k)
        return v.where((s.si - sk) <= k + 2)

    # --- precio y volatilidad
    s["ret1"] = s.change_percent / 100.0
    for k in (1, 3, 5, 10, 20):
        s[f"past{k}"] = px / lag("current_price", k) - 1
    s["absret"] = s.ret1.abs()
    for w in (5, 10, 20):
        s[f"atr{w}"] = g.absret.transform(lambda x: x.rolling(w, min_periods=max(3, w // 2)).mean())
        s[f"vol{w}"] = g.ret1.transform(lambda x: x.rolling(w, min_periods=max(3, w // 2)).std())
    for w in (10, 20):
        s[f"dist_hi{w}"] = px / g.current_price.transform(lambda x: x.rolling(w, min_periods=5).max()) - 1
        s[f"dist_lo{w}"] = px / g.current_price.transform(lambda x: x.rolling(w, min_periods=5).min()) - 1
    s["vol_ratio"] = s.atr5 / s.atr20
    s["ret1_z"] = s.ret1 / s.vol20
    s["past5_z"] = s.past5 / (s.vol20 * np.sqrt(5))

    # --- CORE
    s["trend_n"] = s.trend.map(TREND_MAP)
    s["trend_prev"] = lag("trend_n", 1)
    s["trend_chg"] = s.trend_n - s.trend_prev
    run = (s.trend_n != s.trend_prev).astype(int)
    s["trend_age"] = run.groupby(s.ticker).cumsum()
    s["trend_age"] = s.groupby(["ticker", "trend_age"]).cumcount() + 1
    for k in (1, 3, 5):
        s[f"mom_chg{k}"] = s.momentum_score - lag("momentum_score", k)
    s["rsi_chg3"] = s.rsi14 - lag("rsi14", 3)
    sig = s.option_signal.fillna("")
    s["opt_bull"] = (sig.str.contains("Call Buying|Put Selling").astype(float)
                     - sig.str.contains("Put Buying|Call Selling").astype(float))
    s["opt_bull_prev"] = lag("opt_bull", 1)
    s["long_score_chg1"] = s.long_score - lag("long_score", 1)

    # --- opciones
    s = pd.concat([s, _flow_parts(s.unusual_flow_signal)], axis=1)
    cz, pz = s.otm_call_volume_z, s.otm_put_volume_z
    s["call_surge"], s["put_surge"] = (cz >= 2).astype(float), (pz >= 2).astype(float)
    s["call_fade"], s["put_fade"] = (cz <= -2).astype(float), (pz <= -2).astype(float)
    s["cz_minus_pz"] = cz - pz
    for col, nm in (("otm_call_volume_z", "cz"), ("otm_put_volume_z", "pz"), ("iv_z_spread", "ivz")):
        s[f"{nm}_chg1"] = s[col] - lag(col, 1)
        s[f"{nm}_mean3"] = g[col].transform(lambda x: x.rolling(3, min_periods=2).mean())
    s["pc_log"] = np.log(s.put_call_ratio.where(s.put_call_ratio > 0))

    # --- GEX y muros
    s["gex_sign"] = np.sign(s.total_gex)
    s["gex_chg_pct"] = s.gex_change / s.total_gex.abs().replace(0, np.nan)
    s["gex_chg3"] = g.gex_change.transform(lambda x: x.rolling(3, min_periods=2).sum()) / s.total_gex.abs().replace(0, np.nan)
    for col in ("gamma_flip", "max_pos_strike", "max_neg_strike", "max_delta_gex_strike", "vwap",
                "nearest_supply", "nearest_demand", "daily_supply_bottom", "daily_demand_top",
                "weekly_supply_bottom", "weekly_demand_top"):
        s[f"d_{col}"] = s[col] / px - 1
    s["call_wall_shift"] = s.call_wall / lag("call_wall", 1) - 1
    s["put_wall_shift"] = s.put_wall / lag("put_wall", 1) - 1
    s["wall_pos"] = (px - s.put_wall) / (s.call_wall - s.put_wall)
    s["wall_strength_bal"] = s.call_wall_strength - s.put_wall_strength

    # --- contexto de mercado (mismo valor para todos los tickers del día)
    ctx = {}
    for e in MAIN_ETFS:
        m = s[s.ticker == e].set_index("trade_date")
        for c in ("ret1", "past5", "trend_n", "momentum_score", "otm_put_volume_z", "otm_call_volume_z", "put_surge"):
            ctx[f"{e.lower()}_{c}"] = m[c]
    C = pd.DataFrame(ctx)
    stocks = s[s.category == "Stocks"]
    C["breadth_up"] = stocks.groupby("trade_date").trend_n.apply(lambda x: (x > 0).mean())
    C["breadth_ret1"] = stocks.groupby("trade_date").ret1.mean()
    C["breadth_past5"] = stocks.groupby("trade_date").past5.median()
    C["mkt_put_surge_share"] = stocks.groupby("trade_date").put_surge.mean()
    s = s.merge(C, left_on="trade_date", right_index=True, how="left")
    s["rel_past5"] = s.past5 - s.spy_past5

    # --- percentiles del día (estables entre regímenes)
    for c in ("past1", "past5", "past20", "atr10", "rvol", "momentum_score", "otm_call_volume_z", "otm_put_volume_z",
              "iv_z_spread", "long_score", "wall_range_pct", "gex_chg_pct", "dist_hi20", "rsi14"):
        s[f"rk_{c}"] = s.groupby("trade_date")[c].rank(pct=True)
    num = s.select_dtypes("number").columns
    s[num] = s[num].replace([np.inf, -np.inf], np.nan)
    return s


FEATURES = None  # se fija en feature_columns()


def feature_columns(s: pd.DataFrame) -> list[str]:
    drop = {"current_price", "si", "trend", "option_signal", "unusual_flow_signal", "ticker", "trade_date", "name",
            "category", "side", "session_type", "zone_scope", "absret", "put_wall", "call_wall", "gamma_flip", "vwap",
            "max_pos_strike", "max_neg_strike", "max_delta_gex_strike", "nearest_supply", "nearest_demand",
            "daily_supply_top", "daily_supply_bottom", "daily_demand_top", "daily_demand_bottom", "weekly_supply_top",
            "weekly_supply_bottom", "weekly_demand_top", "weekly_demand_bottom", "put_wall_gex", "call_wall_gex",
            "total_gex", "gex_change", "price_change", "max_pos_gex", "max_neg_gex", "max_delta_gex_value", "rsi",
            "id", "last_run_id", "is_live_price", "signals_available", "detail_iv_z_spread", "detail_momentum_score",
            "detail_trend", "pc_ratio"}
    return [c for c in s.columns if c not in drop and pd.api.types.is_numeric_dtype(s[c]) and not c.startswith("_")]
