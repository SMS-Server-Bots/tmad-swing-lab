"""'Posicionarse en el movimiento y aguantarlo lo que dure': ruptura + stop dinámico.

Para cada señal EOD (día d) con su probabilidad de movimiento (walk-forward, fuera de muestra):
  * Orden de compra en el máximo de d y orden de venta en el mínimo de d (entra en el lado que rompa primero).
    Si abre por encima/debajo, entra a la apertura. Si el mismo día rompe por los dos lados -> se cuenta como
    pérdida (no sabemos el orden con barras diarias).
  * Stop inicial: el otro extremo del día d. Después stop dinámico tipo chandelier: k x ATR desde el extremo
    más favorable alcanzado (máximo para largos). Máximo MAXH sesiones; si no rompe en VALID sesiones, se anula.
Pago en % sobre la entrada, menos 0,2 % de coste. Se compara por deciles de probabilidad de movimiento.
"""
import sys

import numpy as np
import pandas as pd

K = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
MAXH = int(sys.argv[2]) if len(sys.argv) > 2 else 10
VALID = 2
COST = 0.2
R = pd.read_pickle("calib/mag_pred.pkl")[['date', 'ticker', 'pm', 'p_atr10']]
px = pd.read_pickle("lab/daily_px.pkl"); px['date'] = px.date.astype(str)
O = px.pivot(index='date', columns='ticker', values='open_px').sort_index()
H = px.pivot(index='date', columns='ticker', values='hi').reindex(O.index)
L = px.pivot(index='date', columns='ticker', values='lo').reindex(O.index)
C = px.pivot(index='date', columns='ticker', values='close').reindex(O.index)
dates = list(O.index); pos = {d: i for i, d in enumerate(dates)}


def simulate(d, t, atr):
    i = pos.get(d)
    if i is None or t not in O.columns or i + MAXH >= len(dates):
        return None
    hi0, lo0 = H.iat[i, O.columns.get_loc(t)], L.iat[i, O.columns.get_loc(t)]
    if not (hi0 > lo0 > 0) or not atr or np.isnan(atr):
        return None
    j = O.columns.get_loc(t)
    side = entry = stop = best = None
    for k in range(1, MAXH + 1):
        o, h, l, c = O.iat[i + k, j], H.iat[i + k, j], L.iat[i + k, j], C.iat[i + k, j]
        if np.isnan([o, h, l, c]).any():
            return None
        if side is None:
            if k > VALID:
                return {'traded': 0, 'pay': 0.0}
            up = h >= hi0; dn = l <= lo0
            if o > hi0: side, entry = 'L', o
            elif o < lo0: side, entry = 'S', o
            elif up and dn: return {'traded': 1, 'side': 'X', 'pay': -(hi0 - lo0) / hi0 * 100 - COST, 'days': k}
            elif up: side, entry = 'L', hi0
            elif dn: side, entry = 'S', lo0
            else:
                continue
            stop = lo0 if side == 'L' else hi0
            best = entry
            # el mismo día de entrada: si toca el stop inicial (rango muy estrecho) -> pérdida
            if (side == 'L' and l <= stop and o <= hi0) or (side == 'S' and h >= stop and o >= lo0):
                pass  # ya cubierto por el caso up&dn; con apertura en gap el stop está lejos
        else:
            if side == 'L':
                if l <= stop:
                    ex = min(o, stop); return {'traded': 1, 'side': 'L', 'pay': (ex / entry - 1) * 100 - COST, 'days': k}
                best = max(best, h); stop = max(stop, best * (1 - K * atr / 100))
            else:
                if h >= stop:
                    ex = max(o, stop); return {'traded': 1, 'side': 'S', 'pay': -(ex / entry - 1) * 100 - COST, 'days': k}
                best = min(best, l); stop = min(stop, best * (1 + K * atr / 100))
        if side is not None and k == MAXH:
            return {'traded': 1, 'side': side, 'pay': ((c / entry - 1) if side == 'L' else -(c / entry - 1)) * 100 - COST, 'days': k}
    return None


R['dec'] = R.groupby('date').pm.transform(lambda s: pd.qcut(s.rank(method='first'), 10, labels=False))
sub = R[R.dec.isin([0, 4, 5, 9])].copy()
out = []
for r in sub.itertuples():
    s = simulate(r.date, r.ticker, r.p_atr10)
    if s:
        out.append({'date': r.date, 'ticker': r.ticker, 'dec': r.dec, **s})
T = pd.DataFrame(out)
T = T[T.traded == 1]
print(f"K={K} ATR, máx {MAXH} sesiones, señales {T.date.min()}..{T.date.max()}")
for dec, g in T.groupby('dec'):
    by = g.groupby('date').pay.mean(); t = by.mean() / by.std() * np.sqrt(len(by)) / np.sqrt(MAXH / 2)
    print(f" decil {int(dec)}: n={len(g):5d} pago medio {g.pay.mean():+6.2f}%  mediana {g.pay.median():+5.2f}%  "
          f"gana {np.mean(g.pay > 0) * 100:3.0f}%  >+10% {np.mean(g.pay > 10) * 100:3.0f}%  ambiguos {np.mean(g.side == 'X') * 100:3.0f}%  "
          f"largos {np.mean(g.side == 'L') * 100:3.0f}%  días {g.days.mean():.1f}  t_adj {t:+.2f}")
top = T[T.dec == 9]
print(" decil 9 por lado:", top.groupby('side').pay.agg(['size', 'mean']).round(2).to_dict('index'))
top = top.assign(m=top.date.str[:7]); print(" decil 9 por mes:", top.groupby('m').pay.mean().round(2).to_dict())
T.to_csv(f"calib/ride_K{K}_H{MAXH}.csv", index=False)
