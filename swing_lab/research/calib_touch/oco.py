"""Variante ejecutable: una sola orden stop en el sentido del mercado (SPY vs SMA20), válida 2 sesiones.
Selección: top-N por ATR10 % (con ADV>5M en 18m). Stop inicial = otro extremo del día; trailing K x ATR; máx H."""
import sys, numpy as np, pandas as pd
K=2.0; MAXH=10; VALID=2; COST=0.2; CANCEL=True
def run(O,H,L,C,ATR,elig,spy_up,topn,label):
    Ov,Hv,Lv,Cv=O.values,H.values,L.values,C.values; dates=list(O.index); rows=[]
    for i in range(20,len(dates)-MAXH-1):
        up=spy_up.iloc[i]
        if pd.isna(up): continue
        a=ATR.iloc[i][elig.iloc[i]].dropna(); a=a[a<25]
        if len(a)<200: continue
        sel=a.nlargest(topn) if topn else a[a.rank(pct=True)>0.9]
        for t,atr in sel.items():
            j=O.columns.get_loc(t); hi0,lo0=Hv[i,j],Lv[i,j]
            if not(hi0>lo0>0): continue
            side='L' if up else 'S'; trig=hi0 if up else lo0; stop=lo0 if up else hi0; entry=None
            for k in range(1,MAXH+1):
                o,h,l,c=Ov[i+k,j],Hv[i+k,j],Lv[i+k,j],Cv[i+k,j]
                if np.isnan([o,h,l,c]).any(): entry='bad'; break
                if entry is None:
                    if k>VALID: break
                    hit_trig=(o>trig or h>=trig) if side=='L' else (o<trig or l<=trig)
                    hit_cancel=(o<stop or l<=stop) if side=='L' else (o>stop or h>=stop)
                    if CANCEL and hit_cancel and not hit_trig: entry='cancel'; break
                    if CANCEL and hit_cancel and hit_trig and ((side=='L' and o<stop) or (side=='S' and o>stop)): entry='cancel'; break
                    if side=='L' and hit_trig: entry=max(o,trig); best=entry
                    elif side=='S' and hit_trig: entry=min(o,trig); best=entry
                    else: continue
                    # mismo día: si también toca el stop inicial -> pérdida
                    if side=='L' and l<=stop and o<=trig: rows.append((dates[i],t,side,(stop/entry-1)*100-COST,k)); entry='done'; break
                    if side=='S' and h>=stop and o>=trig: rows.append((dates[i],t,side,-(stop/entry-1)*100-COST,k)); entry='done'; break
                    best=h if side=='L' else l
                    stop=max(stop,best*(1-K*atr/100)) if side=='L' else min(stop,best*(1+K*atr/100))
                    continue
                if side=='L':
                    if l<=stop: rows.append((dates[i],t,side,(min(o,stop)/entry-1)*100-COST,k)); entry='done'; break
                    best=max(best,h); stop=max(stop,best*(1-K*atr/100))
                else:
                    if h>=stop: rows.append((dates[i],t,side,-(max(o,stop)/entry-1)*100-COST,k)); entry='done'; break
                    best=min(best,l); stop=min(stop,best*(1+K*atr/100))
                if k==MAXH: rows.append((dates[i],t,side,((c/entry-1) if side=='L' else -(c/entry-1))*100-COST,k)); entry='done'
    T=pd.DataFrame(rows,columns=['date','ticker','side','pay','days']); T['date']=T.date.astype(str)
    by=T.groupby('date').pay.mean(); t=by.mean()/by.std()*np.sqrt(len(by))/np.sqrt(MAXH/2)
    q=pd.PeriodIndex(pd.to_datetime(T.date),freq='Q').astype(str)
    print(f"{label}: n={len(T)} días={T.date.nunique()} pago {T.pay.mean():+.2f}% gana {np.mean(T.pay>0)*100:.0f}% >+10% {np.mean(T.pay>10)*100:.0f}% L {T[T.side=='L'].pay.mean():+.2f}% ({(T.side=='L').sum()}) S {T[T.side=='S'].pay.mean():+.2f}% ({(T.side=='S').sum()}) t_adj {t:+.2f}")
    print("   por trimestre:",T.groupby(q).pay.mean().round(2).to_dict())
    return T
# 18 meses
p=pd.read_pickle("lab18/panel18.pkl"); p=p[~p.step_bad.fillna(False).astype(bool)]
piv=lambda v:p.pivot(index='price_date',columns='ticker',values=v).sort_index()
O=piv('open');H=piv('high').reindex(O.index);L=piv('low').reindex(O.index);C=piv('close').reindex(O.index);ADV=piv('adv20').reindex(O.index)
ATR=((H-L)/C*100).rolling(10,min_periods=8).mean(); spy=C['SPY']; up=(spy>spy.rolling(20).mean()).where(spy.rolling(20).mean().notna())
elig=(ADV>5e6)
for n in (10,0): run(O,H,L,C,ATR,elig,up,n,f"18m top{n or 'decil'}")
# jul-oct (precios del laboratorio)
px=pd.read_pickle("lab/daily_px.pkl"); px['date']=px.date.astype(str)
pv=lambda v:px.pivot(index='date',columns='ticker',values=v).sort_index()
O=pv('open_px');H=pv('hi').reindex(O.index);L=pv('lo').reindex(O.index);C=pv('close').reindex(O.index)
ATR=((H-L)/C*100).rolling(10,min_periods=5).mean(); spy=C['SPY']; up=(spy>spy.rolling(20).mean()).where(spy.rolling(20).mean().notna())
elig=C.notna()
for n in (10,0): run(O,H,L,C,ATR,elig,up,n,f"jul-oct top{n or 'decil'}")
