import pandas as pd, numpy as np
P=pd.read_pickle('panel_raw.pkl'); I=pd.read_pickle('intraday.pkl')
num=['current_price','change_percent','total_gex','gex_change','gamma_flip','rsi14','rvol','momentum_score','iv_z_spread','iv_change','otm_call_volume_z','otm_put_volume_z','pc_ratio','long_score','max_pos_strike','max_neg_strike','put_wall_pct','call_wall_pct','wall_range_pct','call_wall','put_wall']
for c in num: P[c]=pd.to_numeric(P[c],errors='coerce')
P=P[P.trade_date!='2026-09-07']  # Labor Day: duplicate of Friday
# NYSE sessions
sess=[d.strftime('%Y-%m-%d') for d in pd.bdate_range('2026-07-20','2026-11-06') if d.strftime('%Y-%m-%d') not in {'2026-09-07'}]
si={d:i for i,d in enumerate(sess)}
# intraday first-hour entry and per-session hourly path
I=I[I.et_date.isin(si)&I.current_price.gt(0)].copy()
I['mins']=I.et.dt.hour*60+I.et.dt.minute
I=I[(I.mins>=570)&(I.mins<=960)]
first=I[I.mins<=645].sort_values('mins').groupby(['ticker','et_date']).first()[['current_price','mins']].rename(columns={'current_price':'open_px','mins':'open_min'})
hp=I.groupby(['ticker','et_date']).current_price.agg(i_hi='max',i_lo='min',i_last='last',i_n='count')
last=I[I.mins>=900].sort_values('mins').groupby(['ticker','et_date']).current_price.last().rename('late_px')
D=pd.DataFrame(index=pd.MultiIndex.from_tuples([],names=['ticker','et_date']))
D=first.join(hp,how='outer').join(last,how='left').reset_index().rename(columns={'et_date':'date'})
E=P[['ticker','trade_date','current_price']].rename(columns={'trade_date':'date','current_price':'eod_close'})
D=D.merge(E,on=['ticker','date'],how='outer')
D['close']=D.eod_close.fillna(D.late_px); D['close_src']=np.where(D.eod_close.notna(),'EOD',np.where(D.late_px.notna(),'1535','NA'))
D['hi']=D[['i_hi','close']].max(axis=1); D['lo']=D[['i_lo','close']].min(axis=1)
D['si']=D.date.map(si); D=D[D.si.notna()]; D['si']=D.si.astype(int)
D.to_pickle('daily_px.pkl')
print('daily px rows',len(D),'sessions',D.date.nunique()); print(D.close_src.value_counts())
# signal table: EOD features at t, entry next session open, path t+1..t+5
S=P.copy(); S['si']=S.trade_date.map(si).astype(int)
px=D.set_index(['ticker','si'])
for k in range(1,6):
    j=px[['open_px','close','hi','lo']].add_suffix(f'_{k}')
    S=S.join(j,on=['ticker',S.si+k] if False else None) if False else S
key=S[['ticker','si']].copy()
for k in range(1,6):
    t=px[['open_px','close','hi','lo','close_src']].copy(); t.columns=[f'{c}_{k}' for c in t.columns]
    t=t.reset_index(); t['si']=t.si-k
    S=S.merge(t,on=['ticker','si'],how='left')
S['entry']=S.open_px_1
for k in range(1,6):
    S[f'r{k}']=S[f'close_{k}']/S.entry-1
    S[f'mfe{k}']=S[[f'hi_{j}' for j in range(1,k+1)]].max(axis=1)/S.entry-1
    S[f'mae{k}']=S[[f'lo_{j}' for j in range(1,k+1)]].min(axis=1)/S.entry-1
S['gap']=S.entry/S.current_price-1
S.to_pickle('signals.pkl')
print('signals',S.shape,'dates',S.trade_date.nunique())
print('entry known',S.entry.notna().mean().round(3),' r1 known',S.r1.notna().mean().round(3),' r5 known',S.r5.notna().mean().round(3))
print(S.groupby('trade_date').entry.apply(lambda x:x.notna().mean()).round(2).to_string())
