"""Walk-forward honesto del objetivo 'toque': ¿predice el modelo qué lado toca primero?

Etiqueta: entrada en la apertura siguiente; gana si toca +X antes que -Y en 5 sesiones (barras diarias;
si toca los dos el mismo día cuenta como pérdida). Pago: +X, -Y, o cierre de la 5.ª sesión; coste 0,2 %.
Uso: python wf.py fixed|<m>   (m = múltiplo del ATR10 para objetivo y stop simétricos)
"""
import sys
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

warnings.filterwarnings("ignore")
sys.path.insert(0, "calib")
from outcome import first_touch  # noqa: E402

COST = 0.2
M = pd.read_pickle("calib/labeled2.pkl").reset_index(drop=True)
EXCL = {'id', 'price', 'entry_px', 'open1', 'prevclose', 'ret5', 'gap_next', 'scanner_run_id', 'gex_run_id', 'core_model_id',
        'market_core_model_id', 'put_wall', 'call_wall', 'level_price', 'target_price', 'target_zone_low', 'target_zone_high',
        'gamma_flip', 'all_call_wall_strike', 'all_put_wall_strike', 'near_gamma_flip', 'near_call_wall_strike',
        'near_put_wall_strike', 'swing_gamma_flip', 'swing_call_wall_strike', 'swing_put_wall_strike', 'long_gamma_flip',
        'long_call_wall_strike', 'long_put_wall_strike', 'vwap'}
EXCL |= {f'{a}{k}' for a in ('h', 'l', 'c', 'mfe', 'mae') for k in range(1, 6)}
CAT = ['trend', 'option_signal', 'market_regime', 'unusual_flow_signal', 'flow_direction', 'sector_etf', 'category', 'asset_type']
for c in CAT:
    if c in M:
        M[c + '_cat'] = M[c].astype('category').cat.codes
NUM = [c for c in M.columns if c not in EXCL and c not in CAT and c not in ('date', 'ticker')
       and pd.api.types.is_numeric_dtype(M[c]) and M[c].notna().mean() > 0.3]

MODE = sys.argv[1] if len(sys.argv) > 1 else 'fixed'
if MODE == 'fixed':
    X = np.full(len(M), 5.0)
else:
    X = float(MODE) * M.p_atr10.fillna(M.p_atr10.median()).clip(1, 15).values
pl, wl, _ = first_touch(M, X, X, 'long')
ps, ws, _ = first_touch(M, X, X, 'short')
M['pay_l'] = pl - COST; M['pay_s'] = ps - COST; M['win_l'] = wl.astype(int); M['win_s'] = ws.astype(int)
M['mv'] = ((M.mfe5 >= X) | (M.mae5 <= -X)).astype(int)

dates = sorted(M.date.unique())
START, STEP, PURGE = 25, 5, 6
parts, aucs = [], []
for s in range(START, len(dates), STEP):
    tr = M[M.date.isin(dates[:s - PURGE])]
    te = M[M.date.isin(dates[s:s + STEP])].copy()
    for side in ('l', 's'):
        clf = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=200,
                                             l2_regularization=1.0, random_state=0)
        clf.fit(tr[NUM], tr['win_' + side])
        te['prob_' + side] = clf.predict_proba(te[NUM])[:, 1]
    te['prob_dir'] = te.prob_l - te.prob_s
    for d, g in te.groupby('date'):
        for side in ('l', 's'):
            if g['win_' + side].nunique() == 2:
                aucs.append((side, d, roc_auc_score(g['win_' + side], g['prob_' + side])))
    parts.append(te)
R = pd.concat(parts)
A = pd.DataFrame(aucs, columns=['side', 'date', 'auc'])
print(f"modo={MODE} features={len(NUM)} test={R.date.nunique()} días ({R.date.min()}..{R.date.max()})  objetivo medio ±{np.mean(X):.1f}%")
print("AUC diaria media  long %.3f  short %.3f   (0,5 = azar)" % (A[A.side == 'l'].auc.mean(), A[A.side == 's'].auc.mean()))

rows = []
for d, g in R.groupby('date'):
    r = {'date': d, 'base_l': g.pay_l.mean(), 'base_s': g.pay_s.mean(), 'base_wl': g.win_l.mean(), 'base_ws': g.win_s.mean()}
    for K in (3, 10):
        tl = g.nlargest(K, 'prob_l'); ts = g.nlargest(K, 'prob_s')
        # dirección pura: el que más se inclina a un lado frente al otro
        dl = g.nlargest(K, 'prob_dir'); dsh = g.nsmallest(K, 'prob_dir')
        r.update({f'L{K}': tl.pay_l.mean(), f'L{K}_win': tl.win_l.mean(), f'S{K}': ts.pay_s.mean(), f'S{K}_win': ts.win_s.mean(),
                  f'DL{K}': dl.pay_l.mean(), f'DS{K}': dsh.pay_s.mean(), f'DLS{K}': (dl.pay_l.mean() + dsh.pay_s.mean()) / 2})
    rows.append(r)
P = pd.DataFrame(rows)


def st(x, lab, H=5):
    x = x.dropna(); m = x.mean(); t = m / x.std() * np.sqrt(len(x)) / np.sqrt(H)
    print(f"  {lab:34s} {m:+6.2f}%   t_adj {t:+5.2f}")


print("Pago medio por operación (tras 0,2 % de coste) y t ajustado por solape (5 sesiones):")
for c, lab in [('base_l', 'universo largo'), ('base_s', 'universo corto'), ('L3', 'top-3 largos'), ('L10', 'top-10 largos'),
               ('S3', 'top-3 cortos'), ('S10', 'top-10 cortos'), ('DLS10', 'largo-corto direccional top-10')]:
    st(P[c], lab)
print("Exceso sobre el universo del mismo día:")
for c, b, lab in [('L3', 'base_l', 'top-3 largos'), ('L10', 'base_l', 'top-10 largos'), ('S3', 'base_s', 'top-3 cortos'),
                  ('S10', 'base_s', 'top-10 cortos'), ('DL10', 'base_l', 'direccional largos top-10'), ('DS10', 'base_s', 'direccional cortos top-10')]:
    st(P[c] - P[b], lab)
print("Acierto (toca objetivo antes que stop): universo L %.0f%% S %.0f%% | top-10 L %.0f%% S %.0f%%" % (
    P.base_wl.mean() * 100, P.base_ws.mean() * 100, P.L10_win.mean() * 100, P.S10_win.mean() * 100))
P['m'] = P.date.str[:7]
print(P.groupby('m')[['base_l', 'L10', 'base_s', 'S10', 'DLS10']].mean().round(2).to_string())
R.to_pickle(f"calib/wf_pred_{MODE}.pkl"); P.to_csv(f"calib/wf_daily_{MODE}.csv", index=False)
