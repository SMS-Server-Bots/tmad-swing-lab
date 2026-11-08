import pandas as pd,numpy as np,itertools
from features import load
COST=0.002
S,U=load(5.0); U=U[(U.category=='Stocks')&U.vol10.notna()&U.path_full].copy()
dates=sorted(U.trade_date.unique()); half=dates[len(dates)//2]
def sim(df,side,k,m,H,trail=None,be=None):
    e=df.entry.values; v=(df.vol10.values/100).clip(0.01,0.15); sg=1 if side=='L' else -1
    out=np.full(len(df),np.nan); done=np.zeros(len(df),bool); best=e.copy(); days=np.full(len(df),H)
    for j in range(1,H+1):
        cl=df[f'close_{j}'].values; hi=df[f'hi_{j}'].values; lo=df[f'lo_{j}'].values
        valid=~done; adv=lo if sg==1 else hi; fav=hi if sg==1 else lo
        stp=e*(1-sg*k*v) if k else np.full(len(df),np.nan)
        if trail is not None:
            tr=best*(1-sg*trail*v); stp=np.where(np.isnan(stp),tr,np.fmax(stp,tr) if sg==1 else np.fmin(stp,tr))
        if be is not None:  # move stop to breakeven after favorable excursion be*v
            moved=(sg*(best/e-1))>=be*v; stp=np.where(moved, np.fmax(stp,e) if sg==1 else np.fmin(stp,e), stp)
        hit=valid&~np.isnan(stp)&((adv<=stp) if sg==1 else (adv>=stp))
        gap_thru=np.abs(adv/stp-1)>0.5*v   # crossed far beyond stop -> fill at worst sample
        fill=np.where(gap_thru,adv,stp)
        out[hit]=sg*(fill[hit]/e[hit]-1); done|=hit; days[hit]=j; valid&=~hit
        if m:
            tg=e*(1+sg*m*v); h2=valid&((fav>=tg) if sg==1 else (fav<=tg)); out[h2]=sg*(tg[h2]/e[h2]-1); done|=h2; days[h2]=j; valid&=~h2
        best=np.where(valid, np.fmax(best,fav) if sg==1 else np.fmin(best,fav), best)
        if j==H: out[valid]=sg*(cl[valid]/e[valid]-1); done|=valid
    return out-COST, days
rows=[]
for side in ['L','S']:
    for H in [2,3,5]:
        for k,m,tr,be in itertools.product([None,1.0,1.5,2.0,2.5],[None,2,3],[None,1.5,2.5],[None,1.0]):
            if (tr or be) and k is None and tr is None: continue
            r,dd=sim(U,side,k,m,H,tr,be); s=pd.Series(r,index=U.index); d=U.trade_date
            daily=s.groupby(d).mean(); a=daily[daily.index<half]; b=daily[daily.index>=half]
            rows.append((side,H,k,m,tr,be,a.mean()*100,b.mean()*100,daily.mean()*100,np.mean(r>0)*100,np.nanpercentile(r,5)*100,dd.mean()))
G=pd.DataFrame(rows,columns=['side','H','stop','tgt','trail','be','h1_%','h2_%','all_%','win_%','p5_%','avg_days'])
G['min_half']=G[['h1_%','h2_%']].min(axis=1)
G.to_csv('exit_grid_v3.csv',index=False)
for side in ['L','S']:
    g=G[G.side==side]
    b=g[g.stop.isna()&g.tgt.isna()&g.trail.isna()&g.be.isna()]
    print('==',side,'hold baseline'); print(b.round(2).to_string(index=False))
    print('== best by worst half'); print(g.sort_values('min_half',ascending=False).head(8).round(2).to_string(index=False))
