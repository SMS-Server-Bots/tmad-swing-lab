"""Velas 1 min (ET) -> tabla por ticker y sesión con lo necesario para las pruebas intradía."""
import sqlite3, json, zlib, numpy as np, pandas as pd
c=sqlite3.connect("file:/home/claude/flow/tmad_flow_20261111_1447.db?mode=ro",uri=True)
rows=[]; paths={}
for t,blob in c.execute("select ticker,bars_blob from intraday_bars_1m"):
    d=json.loads(zlib.decompress(blob))
    df=pd.DataFrame({'ts':d['ts'],'o':d['open'],'h':d['high'],'l':d['low'],'c':d['close'],'v':d['volume']})
    df['date']=df.ts.str[:10]; df['m']=df.ts.str[11:13].astype(int)*60+df.ts.str[14:16].astype(int)
    for k in 'ohlcv': df[k]=pd.to_numeric(df[k],errors='coerce')
    for dt,gall in df.groupby('date'):
        g=gall[(gall.m>=570)&(gall.m<960)]
        if len(g)<30 or g.m.iloc[0]>575: continue
        pre=gall[gall.m<570]
        o=g.o.iloc[0]; cl=g.c.iloc[-1]
        p935=g[g.m<=575].c.iloc[-1]
        orb=g[g.m<600]; post=g[g.m>=600]
        rows.append(dict(ticker=t,date=dt,open=o,close=cl,high=g.h.max(),low=g.l.min(),p935=p935,
            or_hi=orb.h.max(),or_lo=orb.l.min(),vol=g.v.sum(),pre_vol=pre.v.sum(),pre_last=(pre.c.iloc[-1] if len(pre) else np.nan),n=len(g)))
        paths[(t,dt)]=(post.m.values,post.h.values,post.l.values,post.c.values)
D=pd.DataFrame(rows).sort_values(['ticker','date'])
D['prev_close']=D.groupby('ticker').close.shift(1)
D['ret_cc']=D.close/D.prev_close-1
D['vol10']=D.groupby('ticker').ret_cc.transform(lambda s:s.shift(1).rolling(10,min_periods=5).std())
D['atr10']=D.assign(r=(D.high-D.low)/D.close).groupby('ticker').r.transform(lambda s:s.shift(1).rolling(10,min_periods=5).mean())
D['gap']=D.open/D.prev_close-1
D['gapz']=D.gap/D.vol10
D.to_pickle("/home/claude/flowan/days.pkl")
import pickle; pickle.dump(paths,open("/home/claude/flowan/paths.pkl","wb"))
print(D.shape, D.date.nunique(), D.date.min(), D.date.max(), D.ticker.nunique())
print(D[['gap','gapz','atr10']].describe().round(3).T)
