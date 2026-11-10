import pandas as pd,numpy as np,warnings; warnings.filterwarnings('ignore')
exec(open('core_gex_fusion.py').read().split("px=pd.read_pickle")[0])
F=pd.read_pickle('fusion.pkl').rename(columns={'snapshot_date':'trade_date'})
sc=['market_context_score','market_breadth_score','vix_stress_score','credit_risk_on_score','sector_context_score','sector_breadth_score','relationship_context_score','relationship_lead_lag_score','leveraged_confirmation_score','relative_strength_vs_market','relative_strength_vs_sector','graph_context_score','relational_deterioration_score']
D=P[P.live_ok].merge(F[['ticker','trade_date','market_regime']+sc],on=['ticker','trade_date'],how='inner')
print('filas con FUSION',len(D),'fechas',D.trade_date.nunique())
dates=sorted(D.trade_date.unique()); cut=dates[len(dates)//2]
def ic(df,f,y): return df.groupby('trade_date').apply(lambda d: d[f].corr(d[y],method='spearman') if d[[f,y]].dropna().shape[0]>50 and d[f].nunique()>5 else np.nan).dropna()
def mt(x): return (x.mean(), x.mean()/x.std()*np.sqrt(len(x)) if len(x)>2 else np.nan, len(x))
res=[]
for f in sc:
    for y,adj in [('r1',1),('r3',np.sqrt(3))]:
        a=mt(ic(D[D.trade_date<cut],f,y)); b=mt(ic(D[D.trade_date>=cut],f,y))
        res.append((f'{f} -> {y}',a[0],a[1]/adj,a[2],b[0],b[1]/adj,b[2]))
# regime: does next-day universe mean return / dispersion differ by regime? (time series, small n)
U=D.groupby('trade_date').agg(reg=('market_regime',lambda s:s.mode().iat[0]),r1=('r1','mean'),absr=('r1',lambda s:s.abs().mean()))
print(U.groupby('reg').agg(dias=('r1','size'),ret_med_sig=('r1','mean'),mov_abs=('absr','mean')).assign(ret_med_sig=lambda x:x.ret_med_sig*100,mov_abs=lambda x:x.mov_abs*100).round(3))
R=pd.DataFrame(res,columns=['supuesto','efecto_desc','t_desc','dias_d','efecto_conf','t_conf','dias_c'])
R.to_pickle('hyp_fusion.pkl'); pd.set_option('display.width',220); print(R.round(3).to_string(index=False)); print('cut',cut)
