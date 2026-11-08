import pandas as pd, numpy as np
def load(min_price=5.0):
    S=pd.read_pickle('signals.pkl')
    S=S.sort_values(['ticker','si'])
    g=S.groupby('ticker')
    # past returns from EOD closes (sessions may be missing -> use si gaps)
    for k in [1,3,5,10]:
        prev=g.current_price.shift(k); psi=g.si.shift(k)
        S[f'past{k}']=np.where((S.si-psi)<=k+2, S.current_price/prev-1, np.nan)
    S['hi10']=g.current_price.transform(lambda x:x.rolling(10,min_periods=5).max()); S['lo10']=g.current_price.transform(lambda x:x.rolling(10,min_periods=5).min())
    S['dist_hi10']=S.current_price/S.hi10-1; S['dist_lo10']=S.current_price/S.lo10-1
    S['vol10']=g.change_percent.transform(lambda x:x.rolling(10,min_periods=5).std())
    S['lgex']=np.sign(S.total_gex)*np.log1p(S.total_gex.abs())
    S['gex_rel']=S.total_gex/(S.total_gex.abs().groupby(S.trade_date).transform('median')+1)
    S['flip_dist']=S.current_price/S.gamma_flip-1
    S['trend_up']=(S.trend=='↑').astype(float)-(S.trend=='↓').astype(float)
    S['opt_bull']=S.option_signal.fillna('').str.contains('Call Buying|Put Selling').astype(float)-S.option_signal.fillna('').str.contains('Put Buying|Call Selling').astype(float)
    S['side_call']=(S.side=='CALL').astype(float)-(S.side=='PUT').astype(float)
    S['lprice']=np.log(S.current_price.clip(lower=0.01))
    # market context from ETFs present in the universe
    for etf in ['SPY','QQQ','IWM','UVXY','VXX']:
        m=S[S.ticker==etf].set_index('trade_date')
        S[f'm_{etf}_1']=S.trade_date.map(m.change_percent); S[f'm_{etf}_5']=S.trade_date.map(m.past5)
    # path validity: drop split-like paths
    for k in range(1,6):
        cols=[f'close_{j}' for j in range(1,k+1)]
        ratio=S[cols].div(S.entry,axis=0)
        bad=(ratio.max(axis=1)>2.5)|(ratio.min(axis=1)<0.4)|(S.gap.abs()>0.4)
        S.loc[bad,[f'r{k}',f'mfe{k}',f'mae{k}']]=np.nan
    px=pd.read_pickle('daily_px.pkl').set_index(['ticker','si'])
    t0=px[['i_hi','i_lo','i_n']].reindex(pd.MultiIndex.from_arrays([S.ticker,S.si])).values
    S['t_range']=(t0[:,0]-t0[:,1])/S.current_price; S['t_n']=t0[:,2]
    S['live_ok']=(S.change_percent.abs()>0)&(S.t_range>0)
    pcols=[f'{c}_{j}' for j in range(1,6) for c in ('close','hi','lo')]
    ratio=S[pcols].div(S.entry,axis=0)
    steps=pd.concat([(S[f'close_{j}']/S[f'close_{j-1}'] if j>1 else S.close_1/S.entry).rename(j) for j in range(1,6)],axis=1)
    S['path_bad']=(ratio.max(axis=1)>2.0)|(ratio.min(axis=1)<0.5)|(steps.max(axis=1)>1.6)|(steps.min(axis=1)<0.6)|(S.gap.abs()>0.4)
    S['path_full']=S[[f'close_{j}' for j in range(1,6)]].notna().all(axis=1)&~S.path_bad
    S.loc[S.path_bad,pcols+[f'r{k}' for k in range(1,6)]]=np.nan
    U=S[(S.current_price>=min_price)&S.category.isin(['Stocks','ETFs'])&S.entry.notna()&S.live_ok].copy()
    return S,U
FEATS=['change_percent','momentum_score','rsi14','rvol','iv_z_spread','iv_change','otm_call_volume_z','otm_put_volume_z','pc_ratio','long_score','lgex','gex_change','flip_dist','put_wall_pct','call_wall_pct','wall_range_pct','trend_up','opt_bull','side_call','lprice','past1','past3','past5','past10','dist_hi10','dist_lo10','vol10','gap']
