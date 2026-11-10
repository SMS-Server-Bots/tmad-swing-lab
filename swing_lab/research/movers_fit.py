import pandas as pd,numpy as np,json,warnings; warnings.filterwarnings('ignore')
from features import load
S,U=load(5.0); U=U[U.category=='Stocks'].copy()
U['c2c1']=(U.close_1/U.current_price-1).abs()          # cierre t -> cierre t+1 (sin depender de la entrada)
U=U[U.c2c1.notna()&(U.c2c1<0.6)]
U['abs_chg']=U.change_percent.abs()
U['neg_gex']=(U.total_gex<0).astype(float)
FE=['vol10','wall_range_pct','call_wall_pct','rvol','pc_ratio','abs_chg']
for f in FE: U['k_'+f]=U.groupby('trade_date')[f].rank(pct=True).fillna(0.5)
K=['k_'+f for f in FE]+['neg_gex']
dates=sorted(U.trade_date.unique()); cut=dates[len(dates)//2]
A=U[U.trade_date<cut]; B=U[U.trade_date>=cut]
X=lambda d: np.column_stack([np.ones(len(d))]+[d[k].values for k in K])
y=lambda d: np.log(d.c2c1.values+0.002)
beta,*_=np.linalg.lstsq(X(A),y(A),rcond=None)
B=B.assign(pred=np.exp(X(B)@beta)-0.002)
ic=B.groupby('trade_date').apply(lambda d:d.pred.corr(d.c2c1,method='spearman'))
icv=B.groupby('trade_date').apply(lambda d:d.vol10.corr(d.c2c1,method='spearman'))
print('2ª mitad: IC modelo %.3f (t %.1f)  vs solo volatilidad %.3f'%(ic.mean(),ic.mean()/ic.std()*np.sqrt(len(ic)),icv.mean()))
B['dec']=B.groupby('trade_date').pred.transform(lambda s:pd.qcut(s.rank(method='first'),10,labels=False))
t=B.groupby('dec').agg(pred_med=('pred','median'),real_med=('c2c1','median'),real_p80=('c2c1',lambda s:s.quantile(.8)),n=('c2c1','size'))
print((t[['pred_med','real_med','real_p80']]*100).round(2).assign(n=t.n).to_string())
top=B[B.dec==9]; bot=B[B.dec==0]
print('Top 10%% se mueve de mediana %.2f%% vs bottom 10%% %.2f%%  (x%.1f)'%(top.c2c1.median()*100,bot.c2c1.median()*100,top.c2c1.median()/bot.c2c1.median()))
# freeze on ALL data
beta_all,*_=np.linalg.lstsq(X(U),y(U),rcond=None)
json.dump({'version':'MOVERS_V1','features':FE,'extra':['neg_gex'],'target':'log(|close_t+1/close_t-1|+0.002)','beta':[float(b) for b in beta_all],
           'fit_dates':[dates[0],dates[-1]],'oos_ic_second_half':round(float(ic.mean()),4)},open('movers_v1.json','w'),indent=1)
print(json.load(open('movers_v1.json'))['beta'])
