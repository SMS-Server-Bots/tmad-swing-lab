import pandas as pd,numpy as np
rows=[]
for h in [1,3,5]:
    U=pd.read_pickle(f'wf_scores_h{h}.pkl'); dates=sorted(U.trade_date.unique()); T=U[U.di>=20].copy()
    R=lambda c: T.groupby('trade_date')[c].rank(pct=True)
    T['mom']=T.momentum_score; T['ls']=T.long_score; T['rev1']=-T.past1
    T['c5050']=0.5*R('rvol')+0.5*R('momentum_score'); T['core4']=0.4*R('momentum_score')+0.25*R('change_percent')+0.25*R('rsi14')+0.1*R('rvol')
    T['lowvol']=-T.vol10; T['nearhi']=T.dist_hi10
    for pol in ['mom','ls','c5050','core4','rev1','lowvol','nearhi',f'ridge{h}',f'gbm{h}']:
        for k in [5,20]:
            a=[]
            for d,g in T.groupby('trade_date'):
                g=g[g[f'r{h}'].notna()&g[pol].notna()].sort_values(pol)
                if len(g)<50: continue
                u=g[f'r{h}'].mean(); a.append((g.tail(k)[f'r{h}'].mean()-u, u-g.head(k)[f'r{h}'].mean()))
            a=np.array(a); n=len(a)
            for i,s in enumerate(['L','S']):
                x=a[:,i]; rows.append((h,pol.rstrip('135'),k,s,n,x.mean()*100,(x>0).mean()*100,x.mean()/x.std()*np.sqrt(n)/np.sqrt(h)))
O=pd.DataFrame(rows,columns=['h','policy','k','side','days','alpha_%','pos_days_%','t_adj'])
O.to_csv('alpha_results.csv',index=False)
print(O[O.k==5].pivot_table(index='policy',columns=['h','side'],values='t_adj').round(2).to_string())
print(O[O.k==20].pivot_table(index='policy',columns=['h','side'],values='t_adj').round(2).to_string())
print(O[O.k==20].pivot_table(index='policy',columns=['h','side'],values='alpha_%').round(2).to_string())
