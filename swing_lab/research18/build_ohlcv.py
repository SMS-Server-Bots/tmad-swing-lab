import sqlite3,pandas as pd,numpy as np
c=sqlite3.connect('file:'+__import__('os').environ.get('TMAD_LEGACY_DB','data/legacy/tmad_scanner_live.db')+'?mode=ro',uri=True)
# latest snapshot per (ticker, price_date) -> consistent adjustment basis
q="""select ticker,price_date,open,high,low,close,volume,max(snapshot_date) as snap from ticker_price_history
     where price_date>='2024-11-01' group by ticker,price_date"""
o=pd.read_sql(q,c)
print(o.shape, o.price_date.min(), o.price_date.max())
cat=pd.read_sql("select ticker,category,max(trade_date) d from ticker_daily_history group by ticker",c)
o=o.merge(cat[['ticker','category']],on='ticker',how='left')
print(o.category.value_counts(dropna=False))
o.to_pickle('ohlcv_raw.pkl')
# basic validity
for k in ['open','high','low','close','volume']: o[k]=pd.to_numeric(o[k],errors='coerce')
bad=(o.close<=0)|(o.high<o.low)|(o.open<=0)
print('bad rows',bad.sum(), ' zero-volume',(o.volume<=0).mean().round(3))
print(o.groupby('price_date').size().describe())
