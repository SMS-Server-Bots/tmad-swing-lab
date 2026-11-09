import pandas as pd,numpy as np
COST=0.002
o=pd.read_pickle('panel18.pkl')
U=o[(o.category=='Stocks')&(o.close>=5)&(o.adv20>=5e6)&(o.jump60==0)&o.consistent].copy()
R=lambda c: U.groupby('date')[c].rank(pct=True)
U['REV_FROZEN']=-(2*R('ret1')+R('ret3'))          # PAPER_V1_REV price-only part, as frozen
U['REV1']=-U.ret1
U['MOM20']=U.ret20
U['LOWVOL']=-U.vol20
U['RND']=np.random.default_rng(7).random(len(U))
def evalp(pol,h,k):
    rows=[]
    for d,g in U.groupby('date'):
        g=g[g[f'r{h}'].notna()&g[pol].notna()]
        if len(g)<200: continue
        g=g.sort_values([pol,'ticker']); u=g[f'r{h}'].mean()
        L=g.tail(k)[f'r{h}'].mean(); S=g.head(k)[f'r{h}'].mean()
        rows.append((d,L-COST,-S-COST,L-u,u-S))
    return pd.DataFrame(rows,columns=['date','L','S','aL','aS']).set_index('date')
out=[]
for pol in ['REV_FROZEN','REV1','MOM20','LOWVOL','RND']:
    for h in [1,2,3,5]:
        for k in [5,20]:
            r=evalp(pol,h,k); n=len(r); adj=np.sqrt(h)
            for c in ['L','S','aL','aS']:
                x=r[c]; out.append((pol,h,k,c,n,x.mean()*100,x.mean()/x.std()*np.sqrt(n)/adj,
                    x[r.index<'2025-09-01'].mean()*100, x[r.index>='2025-09-01'].mean()*100))
O=pd.DataFrame(out,columns=['policy','h','k','stat','days','mean_%','t_adj','first_%','second_%'])
O.to_csv('indep_results.csv',index=False)
pd.set_option('display.width',200)
print(O[O.policy=='REV_FROZEN'].round(3).to_string(index=False))
print(O[(O.k==20)&(O.stat.isin(['aL','aS']))].pivot_table(index='policy',columns=['h','stat'],values='t_adj').round(2))
