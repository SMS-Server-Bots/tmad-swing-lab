import pandas as pd,numpy as np,warnings; warnings.filterwarnings('ignore')
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor
COST=0.002
o=pd.read_pickle('panel18.pkl')
U=o[(o.category=='Stocks')&(o.close>=5)&(o.adv20>=5e6)&(o.jump60==0)&o.consistent].copy()
F=['ret1','ret3','ret5','ret10','ret20','ret60','vol20','atr14','adv20','vsurge','clv','gap_t','intra_t','dist_hi20','dist_lo20','rsi14']
spy=o[o.ticker=='SPY'].set_index('date')
for c in ['ret1','ret5','ret20','vol20']: U[f'spy_{c}']=U.date.map(spy[c])
X=pd.DataFrame({f:U.groupby('date')[f].rank(pct=True) for f in F}).fillna(0.5)
for c in ['spy_ret1','spy_ret5','spy_ret20','spy_vol20']: X[c]=U[c].fillna(0)*10
XC=list(X.columns); U[XC]=X.values
dates=sorted(U.date.unique()); di={d:i for i,d in enumerate(dates)}; U['di']=U.date.map(di)
res=[]
for h in [1,2,5]:
    U['y']=U.groupby('date')[f'r{h}'].rank(pct=True)-0.5
    pr={'ridge':pd.Series(np.nan,index=U.index),'gbm':pd.Series(np.nan,index=U.index)}
    mg=None
    for t in range(120,len(dates)):
        tr=U[(U.di<=t-h-1)&(U.di>=t-h-1-250)&U.y.notna()]; te=U.di==t
        pr['ridge'][te]=Ridge(alpha=10).fit(tr[XC],tr.y).predict(U.loc[te,XC])
        if mg is None or t%10==0: mg=HistGradientBoostingRegressor(max_depth=3,learning_rate=0.05,max_iter=200,min_samples_leaf=500).fit(tr[XC],tr.y)
        pr['gbm'][te]=mg.predict(U.loc[te,XC])
    print(h, {m:p.notna().sum() for m,p in pr.items()}, flush=True)
    for m,p in pr.items():
        U['sc']=p; T=U[U.di>=120]
        for k in [5,20]:
            a=[]
            for d,g in T.groupby('date'):
                g=g[g[f'r{h}'].notna()&g.sc.notna()].sort_values(['sc','ticker'])
                if len(g)<200: continue
                u=g[f'r{h}'].mean()
                a.append((g.tail(k)[f'r{h}'].mean()-COST,-g.head(k)[f'r{h}'].mean()-COST,g.tail(k)[f'r{h}'].mean()-u,u-g.head(k)[f'r{h}'].mean()))
            a=np.array(a); n=len(a)
            for i,s in enumerate(['L','S','aL','aS']):
                x=a[:,i]; res.append((m,h,k,s,n,x.mean()*100,x.mean()/x.std()*np.sqrt(n)/np.sqrt(h)))
R=pd.DataFrame(res,columns=['model','h','k','stat','days','mean_%','t_adj']); R.to_csv('wf18_results.csv',index=False)
print(R.pivot_table(index=['model','k'],columns=['h','stat'],values='t_adj').round(2).to_string())
print(R.pivot_table(index=['model','k'],columns=['h','stat'],values='mean_%').round(2).to_string())
