import pandas as pd,numpy as np,warnings; warnings.filterwarnings('ignore')
from features import load,FEATS
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor
COST=0.002  # 20 bps round trip
S,U=load(5.0); U=U[U.category=='Stocks'].copy()
dates=sorted(U.trade_date.unique()); di={d:i for i,d in enumerate(dates)}; U['di']=U.trade_date.map(di)
spy=S[S.ticker=='SPY'].set_index('trade_date')
R=lambda c: U.groupby('trade_date')[c].rank(pct=True)
X=pd.DataFrame({f:R(f) for f in FEATS}).fillna(0.5)
for c in [c for c in U.columns if c.startswith('m_')]: X[c]=U[c].fillna(0)/10
U[[f'x_{c}' for c in X.columns]]=X.values; XC=[f'x_{c}' for c in X.columns]
def topk(df,score,h,k=5):
    out=[]
    for d,g in df.groupby('trade_date'):
        g=g[g[f'r{h}'].notna()&g[score].notna()]
        if len(g)<50: continue
        g=g.sort_values(score)
        L=g.tail(k)[f'r{h}'].mean()-COST; Sh=-g.head(k)[f'r{h}'].mean()-COST
        base=g[f'r{h}'].mean()
        out.append((d,L,Sh,base))
    return pd.DataFrame(out,columns=['date','L','S','univ']).set_index('date')
res={}
for h in [1,3,5]:
    U['y']=U.groupby('trade_date')[f'r{h}'].rank(pct=True)-0.5
    pr_r=pd.Series(np.nan,index=U.index); pr_g=pr_r.copy()
    for t in range(20,len(dates)):
        tr=U[(U.di<=t-h-1)&U.y.notna()]; te=U.di==t
        m=Ridge(alpha=10).fit(tr[XC],tr.y); pr_r[te]=m.predict(U.loc[te,XC])
        if t%5==0 or 'mg' not in dir():
            mg=HistGradientBoostingRegressor(max_depth=3,learning_rate=0.05,max_iter=150,min_samples_leaf=200).fit(tr[XC],tr.y)
        pr_g[te]=mg.predict(U.loc[te,XC])
    U[f'ridge{h}']=pr_r; U[f'gbm{h}']=pr_g
    T=U[U.di>=20].copy()
    T['mom']=T.momentum_score; T['ls']=T.long_score
    T['c5050']=0.5*T.groupby('trade_date').rvol.rank(pct=True)+0.5*T.groupby('trade_date').momentum_score.rank(pct=True)
    T['core4']=0.4*R('momentum_score')[T.index]+0.25*R('change_percent')[T.index]+0.25*R('rsi14')[T.index]+0.1*R('rvol')[T.index]
    T['rev1']=-T.past1
    T['rnd']=np.random.default_rng(h).random(len(T))
    for pol in ['rnd','mom','ls','c5050','core4','rev1',f'ridge{h}',f'gbm{h}']:
        r=topk(T,pol,h); res[(h,pol.rstrip('135') if pol.startswith(('ridge','gbm')) else pol)]=r
    U.to_pickle(f'wf_scores_h{h}.pkl')
rows=[]
for (h,p),r in res.items():
    n=len(r); adj=np.sqrt(h)
    for side in ['L','S']:
        x=r[side]; rows.append((h,p,side,n,x.mean()*100,(x>0).mean()*100,x.mean()/x.std()*np.sqrt(n)/adj))
    x=(r.L+r.S)/2; rows.append((h,p,'L+S',n,x.mean()*100,(x>0).mean()*100,x.mean()/x.std()*np.sqrt(n)/adj))
O=pd.DataFrame(rows,columns=['h','policy','side','days','net_ret_%','win_days_%','t_adj'])
O.to_csv('wf_results.csv',index=False)
print(O.round(2).to_string(index=False))
