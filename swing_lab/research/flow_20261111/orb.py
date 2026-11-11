import pandas as pd,numpy as np,pickle
D=pd.read_pickle("days.pkl"); P=pickle.load(open("paths.pkl","rb"))
D=D[D.atr10.notna()&(D.or_hi>D.or_lo)&(D.open>=5)].copy()
D['dec']=D.groupby('date').atr10.transform(lambda s:pd.qcut(s.rank(method='first'),10,labels=False))
def orb(r):
    m,h,l,c=P[(r.ticker,r.date)]
    if len(m)==0: return np.nan,None
    side=None
    for i in range(len(m)):
        if side is None:
            up=h[i]>r.or_hi; dn=l[i]<r.or_lo
            if up and dn: return -(r.or_hi-r.or_lo)/r.or_hi,'X'
            if up: side,e,stop='L',r.or_hi,r.or_lo
            elif dn: side,e,stop='S',r.or_lo,r.or_hi
            else: continue
            if (side=='L' and l[i]<=stop) or (side=='S' and h[i]>=stop): return (stop/e-1)*(1 if side=='L' else -1),side
            continue
        if side=='L' and l[i]<=stop: return stop/e-1,side
        if side=='S' and h[i]>=stop: return -(stop/e-1),side
    if side is None: return 0.0,None
    return (c[-1]/e-1)*(1 if side=='L' else -1),side
res=[orb(r) for r in D.itertuples()]
D['pay']=[x[0] for x in res]; D['side']=[x[1] for x in res]
T=D[D.side.notna()].copy(); T['pay']=T.pay-0.001
# exceso frente a la media del día (p935->close) para controlar el mercado: aquí usamos el pago bruto por lado
def rep(s,lab):
    by=s.groupby('date').pay.mean(); t=by.mean()/by.std()*np.sqrt(len(by))
    print(f"{lab:30s} n={len(s):5d} pago medio {s.pay.mean()*100:+.2f}% gana {np.mean(s.pay>0)*100:3.0f}% t(día) {t:+.2f}  | L {s[s.side=='L'].pay.mean()*100:+.2f}% S {s[s.side=='S'].pay.mean()*100:+.2f}%")
rep(T,"todas"); rep(T[T.dec==9],"decil 9 (más movimiento)"); rep(T[T.dec>=7],"deciles 7-9"); rep(T[T.dec<=2],"deciles 0-2")
print(T[T.dec==9].groupby('date').pay.mean().mul(100).round(2).describe()[['mean','std','min','max']].to_dict())
