"""Hueco de apertura extremo (> 2 volatilidades diarias) -> apertura a cierre del mismo día. Ver REPORT."""
import pandas as pd, numpy as np
COST = 0.002
o = pd.read_pickle('panel18.pkl')
U = o[(o.category == 'Stocks') & (o.close >= 5) & (o.adv20 >= 5e6) & (o.jump60 == 0) & o.consistent & (o.gapd == 1)].copy()
U['oc'] = U.close / U.open - 1
U['a_oc'] = U.oc - U.groupby('date').oc.transform('mean')
U['gap_z'] = U.gap_t / U.vol20
x = U[(U.gap_z > 2) & (U.open < U.high * 0.9995)]
d = x.groupby('date').oc.mean() * -1 - COST
print('SHORT open->close gap_z>2: net/day %.3f%% t %.2f win %.0f%% days %d' % (d.mean() * 100, d.mean() / d.std() * np.sqrt(len(d)), (d > 0).mean() * 100, len(d)))
