import pandas as pd,numpy as np
pn=pd.read_pickle("panel_raw.pkl").sort_values('run_utc').drop_duplicates(['ticker','trade_date'],keep='last')
pn['trade_date']=pn.trade_date.astype(str)
print(pn.unusual_flow_signal.value_counts(dropna=False).head(8))
px=pd.read_pickle("daily_px.pkl")
C=px.pivot(index='date',columns='ticker',values='close').sort_index()
O=px.pivot(index='date',columns='ticker',values='open_px').reindex(C.index)
out=[]
for H in (1,3,5,11):
    f=(C.shift(-H)/O.shift(-1)-1).where(lambda x:x.abs()<1.5)
    F=f.stack().reset_index(); F.columns=['trade_date','ticker','fwd']; F['trade_date']=F.trade_date.astype(str)
    D=pn.merge(F,on=['trade_date','ticker'])
    D['ex']=D.fwd-D.groupby('trade_date').fwd.transform('mean')
    for sig,g in D[D.unusual_flow_signal.notna()].groupby('unusual_flow_signal'):
        by=g.groupby('trade_date').ex.mean()
        t=by.mean()/by.std()*np.sqrt(len(by))/np.sqrt(H) if len(by)>2 else np.nan
        out.append((H,sig,len(g),len(by),round(g.ex.mean()*100,2),round(t,2),round((g.ex>0).mean()*100)))
print(pd.DataFrame(out,columns=['H','signal','n','dias','exceso_%','t_adj','hit_%']).to_string())
