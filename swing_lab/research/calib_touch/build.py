import sqlite3,pandas as pd,numpy as np,json
c=sqlite3.connect("file:/home/claude/restore_check/restored.db?mode=ro&immutable=1",uri=True)
KEEP_STR={'trend','option_signal','sector_etf','market_regime','unusual_flow_signal','flow_direction','asset_type','category'}
rows=[]
cur=c.execute("select id,snapshot_date,ticker,current_price,has_gex,features_json from feature_observations where observation_layer='daily_close' and is_canonical_daily_close=1")
for oid,d,t,p,hg,fj in cur:
    try: f=json.loads(fj or "{}")
    except Exception: f={}
    r={'id':oid,'date':d,'ticker':t,'price':p,'has_gex':hg}
    for k,v in f.items():
        if isinstance(v,bool): r[k]=int(v)
        elif isinstance(v,(int,float)): r[k]=float(v)
        elif k in KEEP_STR and isinstance(v,str): r[k]=v
    rows.append(r)
D=pd.DataFrame(rows); del rows
num=D.select_dtypes('number').columns; D[num]=D[num].astype('float32')
O=pd.read_sql("""select o.observation_id,o.horizon_sessions h,o.entry_price e,o.return_long_pct r,o.mfe_long_pct mfe,o.mae_long_pct mae from observation_outcomes o join feature_observations f on f.id=o.observation_id where o.complete=1 and f.observation_layer='daily_close' and f.is_canonical_daily_close=1""",c)
W=O.pivot(index='observation_id',columns='h',values=['r','mfe','mae']); W.columns=[f"{a}{b}" for a,b in W.columns]
E=O.groupby('observation_id').e.first()
D=D.merge(W,left_on='id',right_index=True,how='left').merge(E.rename('entry_px'),left_on='id',right_index=True,how='left')
print(D.shape, D.date.nunique(), D.date.min(), D.date.max(), "con h5:",D.mfe5.notna().mean().round(3))
print("entry_px vs price ratio median", (D.entry_px/D.price).median())
D.to_pickle("/home/claude/calib/full.pkl")
