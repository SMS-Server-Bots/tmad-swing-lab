import pandas as pd,numpy as np
D=pd.read_pickle("calib/full.pkl")
px=pd.read_pickle("lab/daily_px.pkl"); px['date']=px.date.astype(str)
P={k:px.pivot(index='date',columns='ticker',values=v) for k,v in [('o','open_px'),('h','hi'),('l','lo'),('c','close')]}
dates=list(P['c'].index); pos={d:i for i,d in enumerate(dates)}
N=5
D=D[D.date.isin(dates)].drop(columns=[c for c in D.columns if c[:-1] in ('r','mfe','mae') and c[-1].isdigit()]).copy()
# sanity: bar consistency
out=[]
for d,g in D.groupby('date'):
    i=pos[d]
    if i+N>=len(dates): continue
    seg=dates[i+1:i+1+N]
    tk=[t for t in g.ticker if t in P['c'].columns]
    o=P['o'].loc[seg[0],tk]; H=P['h'].loc[seg,tk].values; L=P['l'].loc[seg,tk].values; C=P['c'].loc[seg,tk].values
    prev=P['c'].loc[d,tk]
    df=pd.DataFrame({'date':d,'ticker':tk,'open1':o.values,'prevclose':prev.values})
    for k in range(N):
        df[f'h{k+1}']=H[k]; df[f'l{k+1}']=L[k]; df[f'c{k+1}']=C[k]
    out.append(df)
Lb=pd.concat(out)
e=Lb.open1
bad=(Lb[[f'h{k}' for k in range(1,6)]].max(axis=1)<Lb[[f'l{k}' for k in range(1,6)]].min(axis=1))|(e<=0)|e.isna()
gap=(e/Lb.prevclose-1).abs()
Lb=Lb[~bad & (gap<0.5)]
for k in range(1,6):
    Lb[f'mfe{k}']=(Lb[[f'h{j}' for j in range(1,k+1)]].max(axis=1)/Lb.open1-1)*100
    Lb[f'mae{k}']=(Lb[[f'l{j}' for j in range(1,k+1)]].min(axis=1)/Lb.open1-1)*100
Lb['ret5']=(Lb.c5/Lb.open1-1)*100
Lb=Lb[(Lb.mfe5<80)&(Lb.mae5>-80)]
M=D.merge(Lb,on=['date','ticker'],suffixes=('','_lb'))
print(M.shape, M.date.nunique(), M.date.min(), M.date.max())
M.to_pickle("calib/labeled.pkl")
