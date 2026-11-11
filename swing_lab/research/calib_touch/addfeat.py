import pandas as pd,numpy as np
M=pd.read_pickle("calib/labeled.pkl")
M=M[M.c5.notna()].copy()
px=pd.read_pickle("lab/daily_px.pkl"); px['date']=px.date.astype(str)
C=px.pivot(index='date',columns='ticker',values='close').sort_index(); H=px.pivot(index='date',columns='ticker',values='hi').reindex(C.index); L=px.pivot(index='date',columns='ticker',values='lo').reindex(C.index)
lr=np.log(C).diff()
lr=lr.where(lr.abs()<0.5)
feat={'p_ret1':C.pct_change(1,fill_method=None)*100,'p_ret5':C.pct_change(5,fill_method=None)*100,'p_ret10':C.pct_change(10,fill_method=None)*100,
      'p_rv10':lr.rolling(10,min_periods=5).std()*100,'p_rv20':lr.rolling(20,min_periods=8).std()*100,
      'p_atr10':((H-L)/C).rolling(10,min_periods=5).mean()*100,
      'p_dhi10':(C/C.rolling(10,min_periods=5).max()-1)*100,'p_dlo10':(C/C.rolling(10,min_periods=5).min()-1)*100}
for k,v in feat.items():
    s=v.stack().rename(k).reset_index(); s.columns=['date','ticker',k]; M=M.merge(s,on=['date','ticker'],how='left')
# market context: SPY ret5, universe breadth
spy=C['SPY'] if 'SPY' in C else None
if spy is not None:
    m=pd.DataFrame({'date':C.index,'spy_ret1':spy.pct_change(fill_method=None).values*100,'spy_ret5':spy.pct_change(5,fill_method=None).values*100})
    M=M.merge(m,on='date',how='left')
br=(C.pct_change(fill_method=None)>0).mean(axis=1).rename('univ_up_frac').reset_index(); br.columns=['date','univ_up_frac']; M=M.merge(br,on='date',how='left')
M['gap_next']=(M.open1/M.prevclose-1)*100   # NO usar como feature (es futuro): solo diagnóstico
print(M.shape, M[['p_rv20','p_atr10','spy_ret5']].notna().mean().round(2).to_dict())
M.to_pickle("calib/labeled2.pkl")
