import sqlite3,json,zlib,numpy as np,pandas as pd
c=sqlite3.connect("file:/home/claude/flow/tmad_flow_20261111_1447.db?mode=ro",uri=True)
rows=[]
for t,side,sd,spot,blob in c.execute("select ticker,side,session_date,spot,contracts_blob from option_heatmap_snapshots where session_date='2026-11-10'"):
    d=json.loads(zlib.decompress(blob)); df=pd.DataFrame(d)
    df=df[(df.dte>=5)&(df.dte<=45)&df.impliedVolatility.notna()&(df.impliedVolatility>0.01)]
    if df.empty or not spot: continue
    e=df.dte.min(); x=df[df.dte==e].copy(); x['dist']=(x.strike/spot-1).abs()
    a=x.nsmallest(2,'dist')
    rows.append(dict(ticker=t,side=side,dte=e,iv=a.impliedVolatility.mean(),dist=a.dist.min()))
I=pd.DataFrame(rows); I=I[I.dist<0.05].groupby('ticker').agg(iv=('iv','mean'),dte=('dte','first')).reset_index()
D=pd.read_pickle("days.pkl"); last=D[D.date=='2026-11-10'][['ticker','vol10','atr10','close']]
# volatilidad realizada de 20 sesiones
rv=D.groupby('ticker').ret_cc.apply(lambda s:s.tail(20).std()).rename('rv20').reset_index()
M=I.merge(last,on='ticker').merge(rv,on='ticker').dropna()
M['iv_d']=M.iv/np.sqrt(252); M['ratio_iv_rv']=M.iv_d/M.rv20
M['dec']=pd.qcut(M.atr10.rank(method='first'),10,labels=False)
print("tickers con IV ATM:",len(M), "| IV anual mediana %.0f%%"%(M.iv.median()*100))
print(M.groupby('dec').agg(n=('ticker','size'),atr10=('atr10','median'),iv_anual=('iv','median'),rv20_diaria=('rv20','median'),iv_diaria=('iv_d','median'),iv_sobre_rv=('ratio_iv_rv','median')).round(3).to_string())
print("Movimiento esperado 5 sesiones (decil 9): opciones %.1f%% vs realizado reciente %.1f%%"%(M[M.dec==9].iv_d.median()*np.sqrt(5)*100*0.8,M[M.dec==9].rv20.median()*np.sqrt(5)*100*0.8))
M.to_csv("iv_rv_20261110.csv",index=False)
