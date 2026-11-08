import os
import sqlite3,glob,pandas as pd
fr=[]
for f in sorted(glob.glob(''+os.environ.get('TMAD_EXTRACTED','data/ex')+'/*/scanner_*.sqlite')):
    c=sqlite3.connect(f)
    df=pd.read_sql("select * from ticker_daily_history",c)
    runs=pd.read_sql("select id as last_run_id, captured_at_utc as run_utc, session_type as run_session, total_tickers_ok from scanner_runs",c)
    df=df.merge(runs,on='last_run_id',how='left'); df['week']=f.split('/')[-2]; fr.append(df)
p=pd.concat(fr,ignore_index=True)
print(p.shape, p.trade_date.nunique(), p.ticker.nunique())
print(p.duplicated(['ticker','trade_date']).sum(),'dups')
print(p.groupby('trade_date').size().describe())
print(p[['category','side','session_type','run_session']].describe(include='all'))
print(p.isna().mean().sort_values().round(2).to_string())
p.to_pickle("panel_raw.pkl")
