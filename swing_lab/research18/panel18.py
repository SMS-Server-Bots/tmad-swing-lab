import pandas as pd,numpy as np
o=pd.read_pickle('ohlcv_raw.pkl')
for k in ['open','high','low','close','volume']: o[k]=pd.to_numeric(o[k],errors='coerce')
o=o[(o.close>0)&(o.open>0)&(o.high>=o.low)&~((o.high==o.low)&(o.volume<=0))]
cnt=o.groupby('price_date').size(); good=cnt[cnt>=1000].index; o=o[o.price_date.isin(good)]
dates=sorted(o.price_date.unique()); di={d:i for i,d in enumerate(dates)}; o['di']=o.price_date.map(di)
o=o.sort_values(['ticker','di']).reset_index(drop=True); g=o.groupby('ticker')
o['prev_close']=g.close.shift(); o['gapd']=o.di-g.di.shift()
o['ret1']=np.where(o.gapd==1,o.close/o.prev_close-1,np.nan)
o['step_bad']=(o.ret1.abs()>0.6)|((o.open/o.prev_close-1).abs()>0.6)
o['tr']=np.maximum(o.high-o.low,np.maximum((o.high-o.prev_close).abs(),(o.low-o.prev_close).abs()))
g=o.groupby('ticker')
for k in [3,5,10,20,60]: o[f'ret{k}']=o.close/g.close.shift(k)-1
o['vol20']=g.ret1.transform(lambda x:x.rolling(20,min_periods=15).std())
o['atr14']=g.tr.transform(lambda x:x.rolling(14,min_periods=10).mean())/o.close
o['adv20']=(o.close*o.volume).groupby(o.ticker).transform(lambda x:x.rolling(20,min_periods=15).mean())
o['vsurge']=o.volume/g.volume.transform(lambda x:x.rolling(20,min_periods=15).mean().shift())
o['clv']=((o.close-o.low)-(o.high-o.close))/(o.high-o.low).replace(0,np.nan)
o['gap_t']=o.open/o.prev_close-1; o['intra_t']=o.close/o.open-1
o['dist_hi20']=o.close/g.high.transform(lambda x:x.rolling(20,min_periods=15).max())-1
o['dist_lo20']=o.close/g.low.transform(lambda x:x.rolling(20,min_periods=15).min())-1
d=o.ret1.clip(-0.5,0.5); up=d.clip(lower=0); dn=(-d).clip(lower=0)
o['rsi14']=100-100/(1+up.groupby(o.ticker).transform(lambda x:x.rolling(14,min_periods=10).mean())/dn.groupby(o.ticker).transform(lambda x:x.rolling(14,min_periods=10).mean()).replace(0,np.nan))
# any split-like jump in the last 60 days poisons history features
o['jump60']=g.step_bad.transform(lambda x:x.astype(float).rolling(60,min_periods=1).max())
# forward path: entry next open, days 1..5 OHLC
for k in range(1,6):
    for c in ['open','high','low','close','step_bad','di']: o[f'{c}_{k}']=g[c].shift(-k)
o['entry']=o.open_1
ok=(o.di_5-o.di==5)
fwd_bad=o[[f'step_bad_{k}' for k in range(1,6)]].fillna(True).astype(bool).any(axis=1)
o['path_ok']=ok&~fwd_bad
for k in range(1,6):
    v=(o[f'di_{k}']-o.di==k)&~o[[f'step_bad_{j}' for j in range(1,k+1)]].fillna(True).astype(bool).any(axis=1)
    o[f'r{k}']=np.where(v,o[f'close_{k}']/o.entry-1,np.nan)
o['date']=o.price_date
# Algunos tickers del proveedor mezclan OHL sin ajustar con cierre ajustado por dividendos:
# se conservan solo los tickers con barras coherentes (cierre y apertura dentro de [mínimo, máximo]).
co=(o.close>o.high*1.0005)|(o.close<o.low*0.9995); oo=(o.open>o.high*1.0005)|(o.open<o.low*0.9995)
t=pd.DataFrame({'c':co,'o':oo,'t':o.ticker}).groupby('t').agg(c=('c','mean'),o=('o','mean'),n=('c','size'))
o['consistent']=o.ticker.isin(t[(t.c<=0.01)&(t.o<=0.01)&(t.n>=200)].index)
o.to_pickle('panel18.pkl')
print(o.shape,'dates',len(dates),dates[0],dates[-1])
U=o[(o.category=='Stocks')&(o.close>=5)&(o.adv20>=5e6)&(o.jump60==0)]
print('universe rows',len(U),'per day median',U.groupby('date').size().median(),'r1 known',U.r1.notna().mean().round(3),'path_ok',U.path_ok.mean().round(3))
print(U.r1.describe(percentiles=[.001,.01,.5,.99,.999]).round(4))
