import pandas as pd,numpy as np
from features import load,FEATS
COST=0.002
S,U=load(5.0); U=U[(U.category=='Stocks')].copy()
dates=sorted(U.trade_date.unique()); half=dates[len(dates)//2]
A=U[U.trade_date<half]; B=U[U.trade_date>=half]
def ic(df,f,h):
    x=df.groupby('trade_date').apply(lambda d:d[f].rank().corr(d[f'r{h}'].rank())).dropna(); return x.mean(),x.mean()/x.std()*np.sqrt(len(x))
sel={}
for h in [1,3]:
    t=pd.DataFrame([(f,)+ic(A,f,h) for f in FEATS if f!='gap'],columns=['f','ic','t']).set_index('f')
    pick=t[t.t.abs()>=2.0]; sel[h]=pick; print('H',h,'first-half factors |t|>=2:\n',pick.round(3))
for h in [1,3]:
    w=np.sign(sel[h].ic)
    if len(w)==0: continue
    B2=B.copy(); B2['sc']=sum(w[f]*B2.groupby('trade_date')[f].rank(pct=True).fillna(0.5) for f in w.index)
    for k in [5,10,20]:
        L=[];Sh=[];AL=[];AS=[]
        for d,g in B2.groupby('trade_date'):
            g=g[g[f'r{h}'].notna()].sort_values('sc')
            if len(g)<50: continue
            u=g[f'r{h}'].mean()
            L.append(g.tail(k)[f'r{h}'].mean()-COST); Sh.append(-g.head(k)[f'r{h}'].mean()-COST); AL.append(g.tail(k)[f'r{h}'].mean()-u); AS.append(u-g.head(k)[f'r{h}'].mean())
        f=lambda x: f'{np.mean(x)*100:+.2f}% t={np.mean(x)/np.std(x)*np.sqrt(len(x))/np.sqrt(h):+.2f}'
        print(f'H{h} top{k} 2nd half ({len(L)}d): LONG net {f(L)} | SHORT net {f(Sh)} | alphaL {f(AL)} | alphaS {f(AS)}')
