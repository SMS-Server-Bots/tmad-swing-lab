"""Políticas CONGELADAS para el registro en papel.

No se reajustan con los resultados del registro. Cualquier cambio crea una política nueva con
otro nombre y empieza su propio contador de sesiones desde cero.

PAPER_V1_REV
    Única combinación cuyo signo se mantuvo entre la primera y la segunda mitad del histórico
    22/07–30/10 (reversión a 1–3 días + distancia al put wall). Factores elegidos SOLO con la
    primera mitad (|t| >= 2 a H1), pesos iguales por signo, sin ajuste. Evidencia previa débil:
    alfa SHORT Top-20 +0,31 %/día, t = 1,26 en la segunda mitad. NO es una ventaja demostrada.
C5050
    Comparador equivalente al control 50/50 del programa H (0,5·RVOL + 0,5·momentum).
RANDOM
    Control: selección aleatoria con semilla por fecha.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

TOP_N = 5
PRIMARY_HOLD = 2          # cierre del 2º día tras la entrada
STOP_VOL_MULT = 2.5       # stop protector = 2,5 × volatilidad diaria (vol10); recorta cola, no crea ventaja
WATCH_VOL_MULT = 1.0      # aviso de deterioro
MAX_HOLD = 5
COST_ROUNDTRIP = 0.002

PAPER_V1_WEIGHTS = {"change_percent": -1, "put_wall_pct": +1, "opt_bull": -1, "past1": -1, "past3": -1}


def _rank(day: pd.DataFrame, col: str) -> pd.Series:
    return day[col].rank(pct=True).fillna(0.5)


def score(day: pd.DataFrame, policy: str) -> pd.Series:
    if policy == "PAPER_V1_REV":
        return sum(w * _rank(day, c) for c, w in PAPER_V1_WEIGHTS.items())
    if policy == "C5050":
        return 0.5 * _rank(day, "rvol") + 0.5 * _rank(day, "momentum_score")
    if policy == "RANDOM":
        seed = int(hashlib.sha256(str(day.trade_date.iloc[0]).encode()).hexdigest()[:8], 16)
        return pd.Series(np.random.default_rng(seed).random(len(day)), index=day.index)
    raise KeyError(policy)


POLICIES = ("PAPER_V1_REV", "C5050", "RANDOM")


def policy_fingerprint() -> str:
    spec = repr((POLICIES, PAPER_V1_WEIGHTS, TOP_N, PRIMARY_HOLD, STOP_VOL_MULT, MAX_HOLD, COST_ROUNDTRIP))
    return hashlib.sha256(spec.encode()).hexdigest()[:12]
