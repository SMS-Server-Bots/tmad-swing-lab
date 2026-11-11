import pandas as pd,numpy as np,sqlite3
c=sqlite3.connect("file:restore_check/restored.db?mode=ro&immutable=1",uri=True)
t=pd.read_sql("select id,model_version_id,ticker,direction,entry_at_madrid,entry_date,entry_price,target_pct,stop_pct,max_hold_sessions,status,best_return_pct,exit_return_pct,exit_reason,target_touch_count,setup_hit from open_trades where status in ('closed','archived','open')",c)
print(t.groupby(['status']).size()); print(t.target_pct.describe()[['mean','min','max']].round(2).to_dict(), t.stop_pct.describe()[['mean','min','max']].round(2).to_dict(), t.max_hold_sessions.value_counts().to_dict())
print(t.exit_reason.value_counts().head(8))
px=pd.read_pickle("lab/daily_px.pkl"); px['date']=px.date.astype(str)
H=px.pivot(index='date',columns='ticker',values='hi'); L=px.pivot(index='date',columns='ticker',values='lo'); C=px.pivot(index='date',columns='ticker',values='close')
dates=list(H.index)
rows=[]
for r in t.itertuples():
    if r.ticker not in H.columns or r.entry_date not in dates: continue
    i=dates.index(r.entry_date); seg=dates[i+1:i+6]
    if len(seg)<5: continue
    hi=H.loc[seg,r.ticker].values; lo=L.loc[seg,r.ticker].values; cl=C.loc[seg,r.ticker].values
    if np.isnan(hi).any() or np.isnan(lo).any(): continue
    e=r.entry_price
    if r.direction=='long': fav=(np.maximum.accumulate(hi)/e-1)*100; adv=(np.minimum.accumulate(lo)/e-1)*100; clr=(cl[-1]/e-1)*100
    else: fav=-(np.minimum.accumulate(lo)/e-1)*100; adv=-(np.maximum.accumulate(hi)/e-1)*100; clr=-(cl[-1]/e-1)*100
    up=hi.max()/e-1; dn=lo.min()/e-1
    rows.append(dict(id=r.id,dir=r.direction,model=r.model_version_id,touch5=fav[-1]>=5,touch_opp5=adv[-1]<=-5,any5=(up>=0.05)|(dn<=-0.05),mfe=fav[-1],mae=adv[-1],close5=clr,best_bot=r.best_return_pct,exit=r.exit_return_pct,entry=r.entry_date))
A=pd.DataFrame(rows); print("trades con precios:",len(A))
print("toca +5%% a favor en 5 sesiones: %.0f%% | toca 5%% en contra: %.0f%% | se mueve 5%% en algún sentido: %.0f%%"%(A.touch5.mean()*100,A.touch_opp5.mean()*100,A.any5.mean()*100))
print("MFE medio %.2f | MAE medio %.2f | cierre 5 sesiones medio %.2f | best registrado por el bot medio %.2f"%(A.mfe.mean(),A.mae.mean(),A.close5.mean(),A.best_bot.mean()))
print(A.groupby('dir')[['touch5','touch_opp5']].mean().mul(100).round(0))
A.to_csv("audit_live_trades.csv",index=False)
