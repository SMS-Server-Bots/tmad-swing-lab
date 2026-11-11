"""Misma estrategia (ruptura del rango del día + stop dinámico K x ATR) en 18 meses de OHLCV (dic-24..jun-26).
La probabilidad de movimiento se aproxima con el ATR10 % (correlación 0,92 con el modelo)."""
import sys, numpy as np, pandas as pd
K=float(sys.argv[1]) if len(sys.argv)>1 else 2.0; MAXH=int(sys.argv[2]) if len(sys.argv)>2 else 10; VALID=2; COST=0.2
p=pd.read_pickle("lab18/panel18.pkl")
p=p[~p.step_bad.fillna(False).astype(bool)]
O=p.pivot(index='price_date',columns='ticker',values='open').sort_index(); H=p.pivot(index='price_date',columns='ticker',values='high').reindex(O.index)
L=p.pivot(index='price_date',columns='ticker',values='low').reindex(O.index); C=p.pivot(index='price_date',columns='ticker',values='close').reindex(O.index)
ADV=p.pivot(index='price_date',columns='ticker',values='adv20').reindex(O.index)
ATR=((H-L)/C*100).rolling(10,min_periods=8).mean()
Ov,Hv,Lv,Cv=O.values,H.values,L.values,C.values
dates=list(O.index); n=len(dates)
rows=[]
rng=np.random.default_rng(0)
for i in range(30,n-MAXH-1):
    a=ATR.iloc[i]; ok=(ADV.iloc[i]>5e6)&a.notna()&(a<25)
    a=a[ok]
    if len(a)<200: continue
    dec=pd.qcut(a.rank(method='first'),10,labels=False)
    for d_ in (0,5,9):
        cols=dec[dec==d_].index
        cols=rng.choice(cols,size=min(40,len(cols)),replace=False)
        for t in cols:
            j=O.columns.get_loc(t); hi0,lo0=Hv[i,j],Lv[i,j]; atr=a[t]
            if not(hi0>lo0>0): continue
            side=None
            for k in range(1,MAXH+1):
                o,h,l,c=Ov[i+k,j],Hv[i+k,j],Lv[i+k,j],Cv[i+k,j]
                if np.isnan([o,h,l,c]).any(): side='bad'; break
                if side is None:
                    if k>VALID: break
                    up=h>=hi0; dn=l<=lo0
                    if o>hi0: side,entry='L',o
                    elif o<lo0: side,entry='S',o
                    elif up and dn: rows.append((dates[i],t,d_,'X',-(hi0-lo0)/hi0*100-COST,k)); side='done'; break
                    elif up: side,entry='L',hi0
                    elif dn: side,entry='S',lo0
                    else: continue
                    stop=lo0 if side=='L' else hi0; best=entry
                else:
                    if side=='L':
                        if l<=stop: rows.append((dates[i],t,d_,'L',(min(o,stop)/entry-1)*100-COST,k)); side='done'; break
                        best=max(best,h); stop=max(stop,best*(1-K*atr/100))
                    else:
                        if h>=stop: rows.append((dates[i],t,d_,'S',-(max(o,stop)/entry-1)*100-COST,k)); side='done'; break
                        best=min(best,l); stop=min(stop,best*(1+K*atr/100))
                if side in ('L','S') and k==MAXH:
                    rows.append((dates[i],t,d_,side,((c/entry-1) if side=='L' else -(c/entry-1))*100-COST,k)); side='done'
T=pd.DataFrame(rows,columns=['date','ticker','dec','side','pay','days'])
T['date']=T.date.astype(str)
print(f"K={K} máx {MAXH}: {T.date.min()}..{T.date.max()}")
for d_,g in T.groupby('dec'):
    by=g.groupby('date').pay.mean(); t=by.mean()/by.std()*np.sqrt(len(by))/np.sqrt(MAXH/2)
    print(f" decil ATR {d_}: n={len(g)} pago {g.pay.mean():+.2f}%  gana {np.mean(g.pay>0)*100:.0f}%  >+10% {np.mean(g.pay>10)*100:.0f}%  L {g[g.side=='L'].pay.mean():+.2f}% S {g[g.side=='S'].pay.mean():+.2f}%  t_adj {t:+.2f}")
g=T[T.dec==9].copy(); g['q']=pd.PeriodIndex(pd.to_datetime(g.date),freq='Q').astype(str)
print(" decil 9 por trimestre:"); print(g.groupby('q').agg(n=('pay','size'),pago=('pay','mean'),L=('pay',lambda x: x[g.loc[x.index,'side']=='L'].mean()),S=('pay',lambda x: x[g.loc[x.index,'side']=='S'].mean())).round(2).to_string())
g9=T[T.dec==9].groupby('date').pay.mean(); g0=T[T.dec==0].groupby('date').pay.mean(); d=(g9-g0).dropna()
print(" decil9 - decil0: %.2f%% t_adj %.2f"%(d.mean(), d.mean()/d.std()*np.sqrt(len(d))/np.sqrt(MAXH/2)))
T.to_csv(f"calib/ride18_K{K}_H{MAXH}.csv",index=False)
