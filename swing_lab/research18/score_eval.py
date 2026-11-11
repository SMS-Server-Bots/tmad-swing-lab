import sqlite3,pandas as pd,numpy as np
c=sqlite3.connect("file:"+__import__("os").environ.get("TMAD_LEGACY_DB","tmad_scanner_live.db")+"?mode=ro",uri=True)
P=pd.read_sql("select basket_date,ticker,rank,score,forward_return from score_basket_history_positions where score_run_id=30",c)
pn=pd.read_pickle("panel18.pkl")
px=pd.read_pickle("ohlcv_raw.pkl").drop_duplicates(['ticker','price_date'])
C=px.pivot(index='price_date',columns='ticker',values='close').sort_index()
O=px.pivot(index='price_date',columns='ticker',values='open').reindex(C.index)
H=11
fwd=(C.shift(-H)/O.shift(-1)-1)            # open t+1 -> close t+11
fwd=fwd.where(fwd.abs()<1.5)               # drop split artefacts
F=fwd.stack().rename('fwd').reset_index().rename(columns={'level_1':'ticker'})
F.columns=['price_date','ticker','fwd']
pn=pn.merge(F,on=['ticker','price_date'],how='left')
pn=pn[(pn.adv20>5e6)&pn.fwd.notna()&(~pn.step_bad.fillna(False).astype(bool))]
dates=sorted(set(P.basket_date)&set(pn.price_date))
rows=[]
for d in dates:
    U=pn[pn.price_date==d]
    if len(U)<300: continue
    picks=P[P.basket_date==d].ticker
    s=U[U.ticker.isin(picks)]
    if len(s)<5: continue
    um=U.fwd.mean()
    r={'date':d,'n':len(s),'score':s.fwd.mean(),'univ':um,'reported':P[P.basket_date==d].forward_return.mean()}
    for f in ['ret20','ret60','rsi14','vsurge']:
        r['top_'+f]=U.nlargest(10,f).fwd.mean()
    U=U.assign(pr60=U.ret60.rank(pct=True),pr20=U.ret20.rank(pct=True),pv=U.vol20.rank(pct=True))
    s2=U[U.ticker.isin(picks)]
    r['pick_ret60_pct']=s2.pr60.mean(); r['pick_ret20_pct']=s2.pr20.mean(); r['pick_vol_pct']=s2.pv.mean()
    # volatility-matched random baseline: top-decile vol, momentum>median
    hv=U[(U.pv>0.8)]
    r['highvol']=hv.fwd.mean()
    rows.append(r)
R=pd.DataFrame(rows)
print("baskets:",len(R),R.date.min(),R.date.max())
def stat(x,lab):
    x=x.dropna(); m=x.mean(); t_naive=m/x.std()*np.sqrt(len(x)); t_adj=t_naive/np.sqrt(H)
    print(f"{lab:28s} mean {m*100:6.2f}%  hit {np.mean(x>0)*100:4.0f}%  t_overlap_adj {t_adj:5.2f}")
for k in ['score','reported','univ','top_ret20','top_ret60','top_rsi14','top_vsurge','highvol']: stat(R[k],k)
print('--- alpha vs universe')
for k in ['score','top_ret20','top_ret60','highvol']: stat(R[k]-R.univ,k+'-univ')
stat(R.score-R.top_ret60,'score-top_ret60'); stat(R.score-R.highvol,'score-highvol')
print("picks avg pct-rank: ret60 %.2f ret20 %.2f vol20 %.2f"%(R.pick_ret60_pct.mean(),R.pick_ret20_pct.mean(),R.pick_vol_pct.mean()))
R['month']=R.date.str[:7]
print(R.groupby('month')[['score','univ','top_ret60','highvol']].mean().mul(100).round(2))
# non-overlapping
idx=list(range(0,len(R),H)); NO=R.iloc[idx]
print("non-overlap n",len(NO),"score-univ mean %.2f%% hit %.0f%%"%(((NO.score-NO.univ).mean()*100),((NO.score-NO.univ)>0).mean()*100))
R.to_csv('score_eval_results.csv',index=False)
