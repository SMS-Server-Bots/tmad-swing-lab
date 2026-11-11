import pandas as pd,numpy as np
D=pd.read_pickle("days.pkl")
D['r']=D.close/D.p935-1
D=D[D.r.abs()<0.6]
D['ex']=D.r-D.groupby('date').r.transform('mean')
def rep(sub,lab):
    sub=sub.copy(); sub['pay']=-np.sign(sub.gap)*sub.ex-0.001  # contraria al hueco, coste 0,1 % (intradía)
    by=sub.groupby('date').pay.mean()
    t=by.mean()/by.std()*np.sqrt(len(by)) if len(by)>2 else np.nan
    print(f"{lab:38s} n={len(sub):4d} días={len(by):2d} exceso medio {sub.pay.mean()*100:+.2f}% gana {np.mean(sub.pay>0)*100:3.0f}%  t(día) {t:+.2f}")
for k in (2,3,4):
    s=D[D.gapz.abs()>k]; rep(s,f"|hueco| > {k} vol")
    rep(s[s.gap>0],f"   solo huecos al alza > {k} vol (corto)"); rep(s[s.gap<0],f"   solo huecos a la baja > {k} vol (largo)")
for g in (0.05,0.10):
    rep(D[D.gap.abs()>g],f"|hueco| > {g:.0%}")
# con volumen premarket alto (noticia) vs bajo
s=D[D.gapz.abs()>3].copy(); s['pv']=s.pre_vol/s.groupby('ticker').vol.transform('median')
rep(s[s.pv>s.pv.median()],"  >3 vol, premarket volumen alto"); rep(s[s.pv<=s.pv.median()],"  >3 vol, premarket volumen bajo")
