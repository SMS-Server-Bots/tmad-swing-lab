import pandas as pd,numpy as np,warnings; warnings.filterwarnings('ignore')
exec(open('core_gex_fusion.py').read().split("px=pd.read_pickle")[0])   # P: daily panel with core, gex_dir, gex_score, mkt
D=P[P.live_ok].copy()
D['abs1']=D.r1.abs()
D['neg_gamma']=(D.total_gex<0).astype(float)
D['side_call']=(D.side=='CALL').astype(float)-(D.side=='PUT').astype(float)
D['move_t']=np.sign(D.change_percent)
spy=S[S.ticker=='SPY'].set_index('trade_date')
D['spy_t']=D.trade_date.map(spy.change_percent)
D['resid_t']=D.change_percent-D.spy_t
dates=sorted(D.trade_date.unique()); cut=dates[len(dates)//2]
def daily_ic(df,f,y,signed_by=None):
    def one(d):
        d=d[[f,y]+([signed_by] if signed_by else [])].dropna()
        if len(d)<50: return np.nan
        yy=d[y]*(d[signed_by] if signed_by else 1)
        return d[f].corr(yy,method='spearman')
    return df.groupby('trade_date').apply(one).dropna()
def mean_t(x): return (x.mean(), x.mean()/x.std()*np.sqrt(len(x)) if len(x)>2 else np.nan, len(x))
res=[]
def H(name,series_fn,adj=1.0):
    a=series_fn(D[D.trade_date<cut]); b=series_fn(D[D.trade_date>=cut])
    ma,ta,na=mean_t(a); mb,tb,nb=mean_t(b)
    res.append((name,ma,ta/adj,na,mb,tb/adj,nb))
# CORE
H('CORE1 trend -> dirección día sig.',lambda d:daily_ic(d,'core','r1'))
H('CORE1b trend -> dirección 3 días',lambda d:daily_ic(d,'core','r3'),np.sqrt(3))
H('CORE2 inicio tendencia -> continuación (r1·dir)',lambda d:d[d.core_start!=0].assign(x=lambda z:z.r1*z.core_start-z.groupby('trade_date').r1.transform('mean')*0).groupby('trade_date').x.mean())
H('CORE3 momentum_score -> dirección',lambda d:daily_ic(d,'momentum_score','r1'))
H('CORE4 side CALL/PUT -> dirección',lambda d:daily_ic(d,'side_call','r1'))
# GEX
H('GEX1 rango muros -> tamaño |r1|',lambda d:daily_ic(d,'wall_range_pct','abs1'))
H('GEX2 desplazamiento strikes -> dirección día sig.',lambda d:daily_ic(d,'gex_score','r1'))
H('GEX5 distancia put wall -> dirección',lambda d:daily_ic(d,'put_wall_pct','r1'))
H('GEX6a gamma NEG: el movimiento de hoy continúa',lambda d:daily_ic(d[d.neg_gamma==1],'change_percent','r1'))
H('GEX6b gamma POS: el movimiento de hoy revierte (IC<0)',lambda d:daily_ic(d[d.neg_gamma==0],'change_percent','r1'))
# FUSION
H('FUS2 movimiento propio (vs SPY) revierte',lambda d:daily_ic(d,'resid_t','r1'))
# EXIT: within names that rose >=3 days (in uptrend), GEX turning down predicts worse next day
def exit_test(d):
    z=d[(d.past3>0.03)].copy(); z['gd']=z.gex_dir
    return z.groupby('trade_date').apply(lambda g: g.loc[g.gd==1,'r1'].mean()-g.loc[g.gd==-1,'r1'].mean() if (g.gd==1).sum()>=3 and (g.gd==-1).sum()>=3 else np.nan).dropna()
H('EXIT1 en subida: GEX gira abajo -> peor día sig.',exit_test)
R=pd.DataFrame(res,columns=['supuesto','efecto_desc','t_desc','dias_d','efecto_conf','t_conf','dias_c'])
R.to_pickle('hyp_daily.pkl'); pd.set_option('display.width',220); print(R.round(3).to_string(index=False))
