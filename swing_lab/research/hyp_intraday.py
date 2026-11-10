import pandas as pd,numpy as np,warnings; warnings.filterwarnings('ignore')
from features import load
I=pd.read_pickle('intraday_full.pkl'); I=I[I.category=='Stocks']
S,_=load(5.0)
# previous session EOD close and vol10 for each ticker/date
E=S[['ticker','trade_date','current_price','vol10','si']].copy()
from data_sessions import si_map, sess
E['next_date']=E.si.map(lambda i: sess[i+1] if i+1<len(sess) else None)
prev=E.rename(columns={'current_price':'prev_close','next_date':'date'})[['ticker','date','prev_close','vol10']]
W={c:I.pivot_table(index=['ticker','date'],columns='slot',values=c,aggfunc='first') for c in ['current_price','call_wall','put_wall','total_gex','wall_range_pct']}
px=W['current_price']
X=pd.DataFrame(index=px.index)
X['p0']=px[0]; X['p1']=px[1]; X['p6']=px[6]
X=X.join(prev.set_index(['ticker','date']),how='left')
X=X[(X.p0>=5)&X.p1.notna()&X.p6.notna()]
X['first_hour']=X.p1/X.p0-1
X['rest']=X.p6/X.p1-1                  # 10:35 -> 15:35 (operable after observing first hour)
X['from0']=X.p6/X.p0-1                 # 09:35 -> 15:35
X['gap']=X.p0/X.prev_close-1; X['gap_z']=X.gap*100/X.vol10
cw=W['call_wall']; pw=W['put_wall']; tg=W['total_gex']
X['wall_shift']=(np.sign(cw[1]-cw[0]).fillna(0)+np.sign(pw[1]-pw[0]).fillna(0)).reindex(X.index)
X['gex_chg']=np.sign(tg[1]-tg[0]).reindex(X.index)
X['neg_gamma']=(tg[0]<0).astype(float).reindex(X.index)
X['wall_range']=W['wall_range_pct'][0].reindex(X.index)
X=X.reset_index()
spy=X[X.ticker=='SPY'].set_index('date') if 'SPY' in X.ticker.values else None
# SPY is an ETF -> take from full intraday
J=pd.read_pickle('intraday_full.pkl'); sp=J[J.ticker=='SPY'].pivot_table(index='date',columns='slot',values='current_price',aggfunc='first')
X['spy_fh']=X.date.map(sp[1]/sp[0]-1); X['spy_rest']=X.date.map(sp[6]/sp[1]-1)
X['resid_fh']=X.first_hour-X.spy_fh
X=X[(X.rest.abs()<0.5)&(X.first_hour.abs()<0.5)&(X.gap.abs()<0.6)]
X['abs_rest']=X.rest.abs()
dates=sorted(X.date.unique()); cut=dates[len(dates)//2]
def ic(df,f,y):
    return df.groupby('date').apply(lambda d: d[f].corr(d[y],method='spearman') if d[[f,y]].dropna().shape[0]>50 else np.nan).dropna()
def mt(x): return x.mean(), x.mean()/x.std()*np.sqrt(len(x)), len(x)
def gapfade(d,z):
    q=d[d.gap_z>z]; return q.groupby('date').apply(lambda g: -(g.from0.mean()-d.loc[d.date==g.name,'from0'].mean()) if len(g)>0 else np.nan).dropna()
res=[]
def H(n,fn):
    a=fn(X[X.date<cut]); b=fn(X[X.date>=cut]); res.append((n,)+mt(a)+mt(b))
H('I2 desplazamiento muros 1ª hora -> resto del día',lambda d:ic(d,'wall_shift','rest'))
H('I3 cambio GEX total 1ª hora -> resto del día',lambda d:ic(d,'gex_chg','rest'))
H('I4a gamma NEG: 1ª hora continúa',lambda d:ic(d[d.neg_gamma==1],'first_hour','rest'))
H('I4b gamma POS: 1ª hora revierte (IC<0)',lambda d:ic(d[d.neg_gamma==0],'first_hour','rest'))
H('I5 SPY 1ª hora -> ticker resto (retardo)',lambda d:ic(d,'spy_fh','rest'))
H('I5b mov. propio 1ª hora (vs SPY) -> resto',lambda d:ic(d,'resid_fh','rest'))
H('I6 hueco >2 vol a las 9:35: corto hasta 15:35 (alfa)',lambda d:gapfade(d,2))
H('I6b hueco >3 vol (alfa)',lambda d:gapfade(d,3))
H('I7 rango muros -> tamaño resto del día',lambda d:ic(d,'wall_range','abs_rest'))
H('I8 momentum 1ª hora -> resto (todos)',lambda d:ic(d,'first_hour','rest'))
R=pd.DataFrame(res,columns=['supuesto','efecto_desc','t_desc','dias_d','efecto_conf','t_conf','dias_c'])
R.to_pickle('hyp_intraday.pkl'); pd.set_option('display.width',220); print(R.round(4).to_string(index=False))
print('cut',cut,'dates',len(dates))
