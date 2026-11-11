import pandas as pd,numpy as np
pn=pd.read_pickle("panel_raw.pkl")
print([c for c in pn.columns if 'score' in c or 'long' in c][:20])
pn=pn.sort_values('run_utc').drop_duplicates(['ticker','trade_date'],keep='last')
px=pd.read_pickle("daily_px.pkl")
C=px.pivot(index='date',columns='ticker',values='close').sort_index()
O=px.pivot(index='date',columns='ticker',values='open_px').reindex(C.index)
H=11
fwd=(C.shift(-H)/O.shift(-1)-1); fwd=fwd.where(fwd.abs()<1.5)
F=fwd.stack().reset_index(); F.columns=['trade_date','ticker','fwd']
r1=(C.pct_change(20,fill_method=None)).stack().reset_index(); r1.columns=['trade_date','ticker','ret20']
vol=(np.log(C).diff().rolling(20,min_periods=10).std()).stack().reset_index(); vol.columns=['trade_date','ticker','vol20']
pn['trade_date']=pn.trade_date.astype(str)
for X in (F,r1,vol): X['trade_date']=X.trade_date.astype(str)
D=pn.merge(F,on=['trade_date','ticker']).merge(r1,on=['trade_date','ticker'],how='left').merge(vol,on=['trade_date','ticker'],how='left')
for c_ in ["long_score","fwd","ret20","vol20"]: D[c_]=pd.to_numeric(D[c_],errors="coerce")
D=D[D.fwd.notna()&D.long_score.notna()]
rows=[]
for d,U in D.groupby('trade_date'):
    if len(U)<300: continue
    U=U.assign(pv=U.vol20.rank(pct=True))
    top=U.nlargest(10,'long_score')
    mid=U[(U.pv>0.3)&(U.pv<0.8)]
    rows.append({'date':d,'ls10':top.fwd.mean(),'univ':U.fwd.mean(),'mom10':U.nlargest(10,'ret20').fwd.mean(),
                 'mom10_midvol':mid.nlargest(10,'ret20').fwd.mean(),'highvol':U[U.pv>0.8].fwd.mean(),
                 'ls_dec':U[U.long_score.rank(pct=True)>0.9].fwd.mean(),'ls_bottom':U[U.long_score.rank(pct=True)<0.1].fwd.mean(),
                 'ic':U.long_score.rank().corr(U.fwd.rank()),'top_vol_pct':top.pv.mean()})
R=pd.DataFrame(rows); print("dates",len(R),R.date.min(),R.date.max())
def stat(x,lab):
    x=x.dropna(); m=x.mean(); t=m/x.std()*np.sqrt(len(x))/np.sqrt(H)
    print(f"{lab:22s} {m*100:6.2f}%  hit {np.mean(x>0)*100:4.0f}%  t_adj {t:5.2f}")
for k in ['ls10','univ','mom10','mom10_midvol','highvol','ls_dec','ls_bottom']: stat(R[k],k)
for k in ['ls10','ls_dec','mom10_midvol']: stat(R[k]-R.univ,k+'-univ')
stat(R.ls_dec-R.ls_bottom,'ls top-bottom dec')
print("IC mean %.3f  t_adj %.2f  top vol pct %.2f"%(R.ic.mean(),R.ic.mean()/R.ic.std()*np.sqrt(len(R))/np.sqrt(H),R.top_vol_pct.mean()))
R['m']=R.date.str[:7]; print(R.groupby('m')[['ls10','univ','mom10_midvol']].mean().mul(100).round(2))
